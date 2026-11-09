"""
spawner.py
───────────
JupyterHub-Pilot Custom SSH Spawner.

Extends JupyterHub's ``Spawner`` to launch ``jupyterhub-singleuser`` on
remote VMs via Paramiko SSH.  Session state is persisted in a local SQLite
database (``SessionStore``) for crash recovery and live status visibility.

Architecture
────────────
  1. User logs in via Google OAuth.
  2. ``start()`` looks up the user's team → VM mapping (SQLite → JSON fallback).
  3. An SSH connection is opened to the target VM as the JupyterHub username.
  4. ``jupyterhub-singleuser`` is launched in the background; its PID is
     written to a remote PID file and stored in SQLite.
  5. ``poll()`` reads the PID from SQLite and checks the remote process.
  6. ``stop()`` reads the VM IP from SQLite (crash-safe) and terminates the
     remote process with SIGTERM → 5 s wait → SIGKILL.
  7. ``clear_state()`` removes the SQLite session row and resets instance vars.

  All SSH work runs in a worker thread (``asyncio.to_thread``) so a slow VM
  never blocks the Hub's event loop.

RBAC
────
  2-tier model:
    admin  — can stop/inspect any user's session  (set via JupyterHub Admin Panel)
    user   — can only manage their own session

  To promote a user to admin, use the JupyterHub Admin Panel or add their name
  to ``admin_users`` in ``hub_settings.json`` and restart the hub.  No code or
  DB changes required.

Spawn preparation
─────────────────
  ``_prepare_spawn`` — cgroups v2 limits (Task 2) and Vault secrets (Task 3).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import paramiko
from tornado import web

try:
    import paramiko.ssh_exception as _paramiko_ssh_exc
except (ImportError, ModuleNotFoundError):
    # Under test mocks paramiko is a MagicMock; ssh_exception is an attribute.
    _paramiko_ssh_exc = getattr(paramiko, "ssh_exception", paramiko)  # type: ignore
from jupyterhub.spawner import Spawner

from jupyterhub_pilot import limits as _limits
from jupyterhub_pilot.admin import RBACManager
from jupyterhub_pilot.paths import CONFIG_DIR, GET_PORT_CMD
from jupyterhub_pilot.session_store import SessionStore
from jupyterhub_pilot.vault_client import VaultClient

# ──────────────────────────────────────────────────────────────────────────────
# Module-level config bootstrap
# ──────────────────────────────────────────────────────────────────────────────

BASE_DIR: str = CONFIG_DIR  # hub_settings.json, user_mapping.json
SETTINGS_FILE: str = os.path.join(BASE_DIR, "hub_settings.json")


def _load_hub_settings() -> Dict[str, Any]:
    """Load hub_settings.json; return an empty dict on any error."""
    try:
        with open(SETTINGS_FILE, "r") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


hub_settings: Dict[str, Any] = _load_hub_settings()

# Fallback JSON mapping path (used when SQLite is unavailable)
MAPPING_FILE: str = os.path.join(
    BASE_DIR, hub_settings.get("mapping_file", "user_mapping.json")
)

# SQLite state DB (all session state + team mappings)
_DB_PATH: str = hub_settings.get(
    "db_path",
    os.path.join(BASE_DIR, "jupyterhub_pilot_state.db"),
)

# Module-level singletons — created once when the module is imported.
_store = SessionStore(_DB_PATH)
try:
    _store.init_db()
except Exception as _init_err:  # noqa: BLE001
    import logging as _logging

    _logging.getLogger("jupyterhub").warning(
        "CustomSpawner: could not initialise SQLite DB at %s: %s — "
        "will fall back to user_mapping.json at runtime.",
        _DB_PATH,
        _init_err,
    )

_rbac = RBACManager()


def worker_ip_for_group(group: str) -> Optional[str]:
    """Worker IP mapped to *group*, or None.  Used by the monitoring dashboard."""
    row = _store.get_mapping(group)  # returns None on DB errors: fail closed
    return row["server_ip"] if row else None


# PERF FIX: cache the result of ping() so start/stop/poll don't hit SQLite
# twice per operation.  The TTL of 5 s means a recovered DB is re-detected
# quickly without hammering it on every spawner call.
_PING_CACHE: Optional[bool] = None
_PING_CACHE_AT: float = 0.0
_PING_TTL: float = 5.0  # seconds


def _db_available() -> bool:
    """Return cached _store.ping() result; re-checks at most every _PING_TTL seconds."""
    global _PING_CACHE, _PING_CACHE_AT
    now = time.monotonic()
    if _PING_CACHE is None or (now - _PING_CACHE_AT) > _PING_TTL:
        _PING_CACHE = _store.ping()
        _PING_CACHE_AT = now
    return _PING_CACHE


# Remote files live in the user's home, not shared /tmp, so other users on the
# worker can neither read the log nor pre-create the PID file.  The Linux
# user name is in the file name because a shared ``ssh_user`` login means
# several JupyterHub users share one home directory.
# poll(): how long a worker may be unreachable over SSH before its servers are
# reported stopped. Shorter outages are treated as blips and keep the server.
_UNREACHABLE_GRACE: float = 300.0

_PID_FILE = "~/.jupyterhub-pilot-{}.pid"
_LOG_FILE = "~/.jupyterhub-pilot-{}.log"

# Linux login names.  Dots are allowed (Google strips the domain, so
# "first.last@corp.com" logs in as "first.last"); adduser needs
# --allow-bad-names for those.
_OS_USER_RE = re.compile(r"[a-z_][a-z0-9_.-]{0,31}")


# ──────────────────────────────────────────────────────────────────────────────
# CustomSpawner
# ──────────────────────────────────────────────────────────────────────────────


class CustomSpawner(Spawner):
    """
    SSH-based JupyterHub spawner with SQLite session state and 2-tier RBAC.

    Attributes:
        _session_meta (Dict[str, Any]): In-memory copy of the last-written
            session record (pid, vm_ip, port, start_time, role, group_name).
    """

    _session_meta: Dict[str, Any] = {}  # noqa: RUF012
    # When poll() first failed to reach this user's worker (None = reachable).
    _unreachable_since: Optional[float] = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _load_mapping_fallback(self) -> Dict[str, Any]:
        """
        Load the flat JSON mapping file as a fallback when SQLite is
        unreachable.

        Returns:
            Parsed dict from user_mapping.json.

        Raises:
            RuntimeError: If the file cannot be read or parsed.
        """
        try:
            with open(MAPPING_FILE, "r") as fh:
                return json.load(fh)
        except Exception as exc:
            self.log.error(
                "CustomSpawner: failed to load fallback mapping file %s: %s",
                MAPPING_FILE,
                exc,
            )
            raise RuntimeError(
                f"Cannot load mapping from SQLite or JSON fallback: {exc}"
            ) from exc

    def _get_server_info(self, group: str) -> Dict[str, Any]:
        """
        Return the server info dict for *group*, trying SQLite first then
        the JSON fallback.

        Returns:
            Dict with keys: ``server_ip``, ``ssh_key`` (or ``server_ssh_key``
            for JSON-format compat), ``ssh_user`` (may be None).

        Raises:
            RuntimeError: If the group is not found in either source.
        """
        if _db_available():
            row = _store.get_mapping(group)
            if row is not None:
                # Normalise key names to match existing code paths
                return {
                    "server_ip": row["server_ip"],
                    "server_ssh_key": row["ssh_key"],
                    "ssh_user": row.get("ssh_user"),
                    "admin_ssh_user": row.get("admin_ssh_user"),
                }
            self.log.warning(
                "CustomSpawner: group '%s' not found in SQLite; "
                "trying JSON fallback.",
                group,
            )

        fallback = self._load_mapping_fallback()
        if group not in fallback:
            raise RuntimeError(
                f"Group '{group}' is not mapped to a server in SQLite or "
                f"{MAPPING_FILE}."
            )
        return fallback[group]

    def _os_username(self) -> str:
        """
        Return the Linux login name for this JupyterHub user.

        Google OAuth usernames are emails; the worker account is the part
        before ``@``.  Anything that is not a plain Linux name is rejected
        here, before it can reach a remote shell.
        """
        name = self.user.name.split("@", 1)[0].lower()
        if not _OS_USER_RE.fullmatch(name):  # fullmatch: "$" would allow a trailing "\n"
            raise RuntimeError(
                f"Username '{self.user.name}' cannot be mapped to a Linux "
                "account name (allowed: a-z, 0-9, '_', '-', max 32 chars)."
            )
        return name

    def _get_ssh_username(self, server_info: Dict[str, Any]) -> str:
        """
        Return the SSH login username.

        Uses the optional ``ssh_user`` field from the mapping; falls back to
        the user's Linux account name so the spawner SSHs as the individual user.
        """
        return server_info.get("ssh_user") or self._os_username()

    def _provision_user_jit(self, server_info: Dict[str, Any]) -> None:
        """
        Just-In-Time provision the OS user on the remote worker VM.
        Connects as 'admin_ssh_user' (e.g. 'ubuntu') using the Hub's SSH key,
        creates the user, adds their SSH key, and installs dependencies.
        """
        admin_user = server_info.get("admin_ssh_user")
        if not admin_user:
            return  # Legacy mode, no admin user configured for provisioning

        target_user = self._os_username()

        ssh = self._open_admin_ssh(server_info)
        if ssh is None:
            return

        u = shlex.quote(target_user)
        home = shlex.quote(f"/home/{target_user}")
        admin_keys = shlex.quote(f"/home/{admin_user}/.ssh/authorized_keys")
        team = self.user.groups[0].name if self.user.groups else ""
        # A shared ssh_user team works in that account's home, not /home/<user>: no links.
        folders = "" if server_info.get("ssh_user") else self._team_folder_script(target_user, team)
        script = f"""
        if ! id {u} &>/dev/null; then
            echo "Creating user {target_user}..."
            # Amazon Linux's adduser is plain useradd and rejects the Debian flags.
            sudo adduser --allow-bad-names --disabled-password --gecos "" {u} \\
                || sudo useradd -m -s /bin/bash {u}
            sudo loginctl enable-linger {u}

            sudo -u {u} mkdir -p {home}/notebook
            sudo -u {u} mkdir -p {home}/.ssh

            sudo cp {admin_keys} {home}/.ssh/authorized_keys
            sudo chown -R {u}:{u} {home}/.ssh
            sudo chmod 600 {home}/.ssh/authorized_keys
        fi
        # jupyterhub, notebook and the AI magics are installed once per worker
        # by install_worker.sh; installing them here took minutes and blew
        # through JupyterHub's 60 s start_timeout.
        {folders}
        """

        try:
            _, stdout, stderr = ssh.exec_command(script)
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                err = stderr.read().decode()
                self.log.error(f"JIT Provisioning script failed with status {exit_status}:\n{err}")
            else:
                self.log.info(f"JIT Provisioning succeeded for user {target_user}.")
        finally:
            ssh.close()

    def _team_folder_script(self, os_user: str, team: str) -> str:
        """Shell lines (run with sudo rights) for /data/<team> and /data/shared, linked into ~/notebook.

        /data/<team>: read/write for the team's Linux group (one per team, created here).
        /data/shared: read-only reference data; admins copy files there with sudo.
        Runs on every start, so a user added to a team gets the folder on their next start.
        """
        if not _OS_USER_RE.fullmatch(team):  # team names become Linux group names
            if team:
                self.log.warning("CustomSpawner: team '%s' is not a valid Linux group name; no team folder", team)
            team = ""
        u = shlex.quote(os_user)
        nb = f"/home/{os_user}/notebook"

        def link(target: str, name: str) -> str:
            # Leave a real file or folder the user already has under that name alone.
            return f"[ -e {nb}/{name} ] && [ ! -L {nb}/{name} ] || sudo -u {u} ln -sfn {target} {nb}/{name}"

        lines = [
            "sudo mkdir -p /data/shared && sudo chmod 755 /data/shared",
            link("/data/shared", "shared"),
        ]
        if team:
            g = shlex.quote(team)
            lines += [
                f"sudo groupadd -f {g}",
                f"id -nG {u} | grep -qw {g} || sudo usermod -aG {g} {u}",
                f"sudo mkdir -p /data/{team} && sudo chgrp {g} /data/{team} && sudo chmod 2770 /data/{team}",
                # New files stay group-writable whatever the user's umask (needs the acl tools).
                f"sudo setfacl -d -m g::rwX /data/{team} 2>/dev/null || true",
                link(f"/data/{team}", "team"),
            ]
        return "\n        ".join(lines)

    def _open_admin_ssh(self, server_info: Dict[str, Any]) -> Optional[paramiko.SSHClient]:
        """SSH to the worker as ``admin_ssh_user`` (has sudo); None if it fails (logged)."""
        admin_user = server_info["admin_ssh_user"]
        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(
                server_info["server_ip"],
                username=admin_user,
                key_filename=server_info["server_ssh_key"],
                timeout=10,
            )
        except Exception as e:  # noqa: BLE001
            self.log.warning(f"CustomSpawner: cannot SSH to {server_info['server_ip']} as {admin_user}: {e}")
            return None
        return ssh

    def _open_ssh(self, server_info: Dict[str, Any]) -> paramiko.SSHClient:
        """
        Open and return an authenticated Paramiko SSH connection to the
        remote VM described by *server_info*.

        Args:
            server_info: Dict with ``server_ip`` and ``server_ssh_key``.

        Returns:
            A connected ``paramiko.SSHClient``.

        Raises:
            paramiko.ssh_exception.NoValidConnectionsError: Host unreachable.
            paramiko.ssh_exception.AuthenticationException: Key auth failed.
            paramiko.ssh_exception.SSHException: Generic SSH failure.
        """
        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            server_info["server_ip"],
            username=self._get_ssh_username(server_info),
            key_filename=server_info["server_ssh_key"],
            timeout=10,
            banner_timeout=15,
            auth_timeout=15,
        )
        return ssh

    # ── Spawn preparation ─────────────────────────────────────────────────────

    async def _prepare_spawn(self) -> None:
        """
        Prepare limits and secrets before launch.

        Not named ``pre_spawn_hook``: that is a JupyterHub config trait which
        JupyterHub already calls itself, so overriding it ran this twice.

        Performs two actions in order:

        **Task 2 — cgroups v2 resource limits:**
            Reads ``resource_limits`` from ``hub_settings.json`` and caches
            the ``memory_max`` and ``cpu_quota`` values on ``self`` so that
            the ``start()`` method can wrap the launch command with
            ``systemd-run --user --scope --property=MemoryMax=... --property=CPUQuota=...``.
            If ``resource_limits`` is absent or ``null`` in the config, the
            singleuser process is launched without cgroup wrapping.

        **Task 3 — Vault secret injection:**
            If ``vault_enabled`` is ``true`` in ``hub_settings.json`` and
            ``VAULT_ADDR`` / ``VAULT_TOKEN`` are set in the Hub's environment,
            reads the user's secrets from HashiCorp Vault at
            ``secret/jupyterhub-pilot/<username>`` and merges them into
            ``self.environment``.  JupyterHub automatically passes
            ``self.environment`` to the singleuser process at launch.
            Vault is opt-in; a missing/unreachable Vault is silently skipped.
        """
        username: str = self.user.name

        # ── Task 2: cgroup limits for start(): user → team → hub_settings default ──
        team_props = None
        if self.user.groups:  # start() uses the first group as the team
            props = getattr(self.user.groups[0], "properties", None)
            team_props = props if isinstance(props, dict) else None
        user_row = _store.get_user_limits(username)
        # Terminal off for this user (Limits page); the Hub-wide switch is checked in _launch_remote.
        self._terminals_off: bool = bool((user_row or {}).get("terminals_off"))
        resolved = _limits.resolve(
            user_row,
            team_props,
            hub_settings.get("resource_limits"),
            lambda msg: self.log.warning("CustomSpawner: '%s': %s", username, msg),
        )
        self._memory_max: Optional[str] = resolved["memory_max"][0]
        self._cpu_quota: Optional[str] = resolved["cpu_quota"][0]
        self.log.info(
            "CustomSpawner: limits for '%s': MemoryMax=%s (%s) CPUQuota=%s (%s)",
            username,
            self._memory_max,
            resolved["memory_max"][1],
            self._cpu_quota,
            resolved["cpu_quota"][1],
        )

        # ── Task 3: Vault secret injection ────────────────────────────────────
        if hub_settings.get("vault_enabled", False):
            secret_path: str = hub_settings.get(
                "vault_secret_path", "secret/jupyterhub-pilot"
            )
            vault = VaultClient(secret_base_path=secret_path)
            secrets = vault.get_user_secrets(username)
            if secrets:
                # Merge into self.environment; get_env() picks this up at launch
                self.environment.update(secrets)
                # Names only: lets %schedule give scheduled runs the same secrets.
                self.environment["JUPYTERHUB_PILOT_SECRET_KEYS"] = ",".join(sorted(secrets))
                self.log.info(
                    "CustomSpawner: Vault injected %d secret(s) for '%s'.",
                    len(secrets),
                    username,
                )
        else:
            self.log.debug(
                "CustomSpawner: Vault disabled — skipping secret injection "
                "for '%s'.",
                username,
            )

    # ── Core Spawner API ──────────────────────────────────────────────────────

    async def start(self) -> Tuple[str, int]:
        """
        Resolve the target VM, open an SSH connection, and launch
        ``jupyterhub-singleuser`` on the remote host.

        Steps:
            1. Resolve team → VM mapping (SQLite → JSON fallback).
            2. Prepare cgroup limits and Vault secrets.
            3. In a worker thread: provision the user, connect via SSH,
               acquire a port, write the OOM script, launch the server.
            4. Persist session metadata to SQLite.
            5. Return ``(ip, port)`` to the JupyterHub proxy.

        Returns:
            Tuple of ``(ip_address, port)`` for the JupyterHub proxy.

        Raises:
            RuntimeError: On group/mapping resolution failure.
            paramiko.ssh_exception.SSHException: On SSH connectivity failure.
        """
        username: str = self.user.name

        # 1. Resolve group and VM.  JupyterHub's own scopes already stop users
        # from starting someone else's server.
        user_groups = [g.name for g in self.user.groups]
        if not user_groups:
            # HTTPError passes through to the page as a readable message, not a bare 500.
            raise web.HTTPError(
                403,
                f"User '{username}' is not in a team yet. "
                "Ask an admin to add you to a team group (Admin → Groups), then start your server.",
            )

        group: str = user_groups[0]
        server_info: Dict[str, Any] = self._get_server_info(group)
        self.ip = server_info["server_ip"]
        os_user: str = self._os_username()

        role: str = _rbac.get_role(self.user)

        # 2. Limits + secrets
        await self._prepare_spawn()

        hub_api_url: Optional[str] = hub_settings.get("hub_api_url")
        if not hub_api_url:
            raise RuntimeError("hub_api_url not found in hub_settings.json")
        env = self.get_env()
        env["JUPYTERHUB_API_URL"] = hub_api_url

        # 3. All blocking SSH work off the event loop
        self.port = await asyncio.to_thread(
            self._launch_remote, server_info, os_user, env, role
        )

        # 4. Persist session to SQLite
        start_time: str = datetime.now(timezone.utc).isoformat()
        self._session_meta = {
            "pid": None,  # PID is written by the remote shell
            "vm_ip": self.ip,
            "port": self.port,
            "start_time": start_time,
            "role": role,
            "group_name": group,
        }
        _store.set_session(
            username,
            vm_ip=self.ip,
            port=self.port,
            start_time=start_time,
            role=role,
            status="running",
            group_name=group,
        )
        self.log.info("CustomSpawner: session record written for '%s'", username)

        # 5. Tell the JupyterHub proxy where to route traffic
        return (self.ip, self.port)

    def _launch_remote(
        self,
        server_info: Dict[str, Any],
        os_user: str,
        env: Dict[str, Any],
        role: str,
    ) -> int:
        """Blocking part of ``start()``; runs in a worker thread. Returns the port."""
        username: str = self.user.name
        try:
            self._provision_user_jit(server_info)
            ssh: paramiko.SSHClient = self._open_ssh(server_info)
        except Exception as exc:
            cls = type(exc).__name__
            if "NoValidConnections" in cls:
                self.log.error(
                    "CustomSpawner: cannot reach %s for user '%s': %s",
                    self.ip,
                    username,
                    exc,
                )
            elif "Authentication" in cls:
                self.log.error(
                    "CustomSpawner: SSH authentication failed for '%s' on %s: %s",
                    username,
                    self.ip,
                    exc,
                )
            else:
                self.log.error(
                    "CustomSpawner: SSH error for '%s' on %s: %s",
                    username,
                    self.ip,
                    exc,
                )
            raise

        try:
            # Acquire port
            _, stdout, _ = ssh.exec_command(GET_PORT_CMD)
            port = int(stdout.readline().strip())

            # Check for stale process on that port
            _, stdout, _ = ssh.exec_command(f"netstat -tuln | grep ':{port} ' || true")
            if stdout.read().strip():
                self.log.warning(
                    "CustomSpawner: port %d on %s already in use for '%s'. "
                    "Attempting to clear stale process...",
                    port,
                    self.ip,
                    username,
                )
                _, fuser_out, _ = ssh.exec_command(f"fuser -k {port}/tcp || true")
                fuser_out.channel.recv_exit_status()  # wait for completion
                time.sleep(1)

            # OOM enforcement startup script
            setup_oom_cmd = (
                "mkdir -p ~/.ipython/profile_default/startup/ && "
                'echo "import os\n'
                "try:\n"
                "    with open('/proc/self/oom_score_adj', 'w') as f:\n"
                "        f.write('500')\n"
                "except:\n"
                '    pass" '
                "> ~/.ipython/profile_default/startup/99-admin-oom-enforcement.py"
            )
            _, oom_stdout, _ = ssh.exec_command(setup_oom_cmd)
            if oom_stdout.channel.recv_exit_status() != 0:
                self.log.warning("Failed to create OOM script for '%s'", username)

            singleuser_cmd: str = (
                f"nohup jupyterhub-singleuser "
                f"--ip=0.0.0.0 "
                f"--port={port} "
                f"--ServerApp.base_url={shlex.quote(self.server.base_url)} "
                f"--notebook-dir='~/notebook/'"
            )
            if hub_settings.get("terminals_enabled") is False or getattr(self, "_terminals_off", False):
                singleuser_cmd += " --ServerApp.terminals_enabled=False"

            # Task 2: wrap with systemd-run if cgroup limits are configured
            mem = getattr(self, "_memory_max", None)
            cpu = getattr(self, "_cpu_quota", None)
            if mem or cpu:
                unit_name = f"jupyterhub-{os_user}-{int(datetime.now(timezone.utc).timestamp())}"
                props = ""
                if mem:
                    props += f" --property=MemoryMax={shlex.quote(str(mem))}"
                if cpu:
                    props += f" --property=CPUQuota={shlex.quote(str(cpu))}"
                singleuser_cmd = (
                    f"systemd-run --user --scope "
                    f"--unit={unit_name}.scope{props} "
                    f"-- {singleuser_cmd}"
                )
                self.log.info(
                    "CustomSpawner: wrapping '%s' with systemd-run "
                    "(MemoryMax=%s, CPUQuota=%s)",
                    username,
                    mem,
                    cpu,
                )

            # Secrets go through stdin, never argv, so `ps` on the worker
            # cannot show the API token or Vault secrets to other users.
            exports: str = "".join(
                f"export {k}={shlex.quote(str(v))}\n" for k, v in env.items()
            )
            pid_file = _PID_FILE.format(os_user)  # os_user is regex-validated
            script: str = (
                f"{exports}"
                # JupyterHub forgets a server even when stop() failed, so end
                # any server left over from an earlier start before launching.
                f'OLD=$(cat {pid_file} 2>/dev/null) && '
                f'ps -p "$OLD" -o args= | grep -q jupyterhub-singleuser && kill "$OLD"\n'
                f"{singleuser_cmd} < /dev/null &> {_LOG_FILE.format(os_user)} &\n"
                f"echo $! > {pid_file}\n"
                f"disown\n"
            )

            self.log.info(
                "CustomSpawner: spawning server for '%s' on %s:%d (role=%s)",
                username,
                self.ip,
                port,
                role,
            )
            stdin, launch_out, _ = ssh.exec_command("bash -l -s")
            stdin.write(script)
            stdin.channel.shutdown_write()
            if launch_out.channel.recv_exit_status() != 0:
                raise RuntimeError(f"Launch script failed for '{username}' on {self.ip}")
            return port
        finally:
            ssh.close()

    def _remote_server_info(self, vm_ip: str, group_name: str) -> Dict[str, Any]:
        """SSH details for an existing session; falls back to the session's IP."""
        if group_name:
            return self._get_server_info(group_name)
        return {"server_ip": vm_ip, "server_ssh_key": ""}

    def _run_remote(self, server_info: Dict[str, Any], cmd: str) -> str:
        """Run *cmd* over SSH and return stdout once it exits. Blocking."""
        ssh = self._open_ssh(server_info)
        try:
            _, stdout, _ = ssh.exec_command(cmd)
            return stdout.read().decode().strip()
        finally:
            ssh.close()

    async def stop(self, now: bool = False) -> None:
        """
        Terminate the remote ``jupyterhub-singleuser`` process for this user.

        Reads the target VM IP from SQLite so that this method works
        correctly even after a hub crash and restart (crash recovery).

        Termination sequence:
            SIGTERM → wait 5 seconds → SIGKILL (if process still alive)

        If SSH fails the error is raised so the user and admin see that the
        stop failed.  JupyterHub still forgets the server (it always calls
        ``clear_state()``); the leftover process is ended on the next start.

        Args:
            now: Ignored (present for Spawner API compatibility).
        """
        username: str = self.user.name

        # Prefer session info from SQLite (crash recovery) over instance vars
        session = _store.get_session(username)

        if session:
            vm_ip: str = session.get("vm_ip") or self.ip or ""
            group_name: str = session.get("group_name") or ""
        else:
            # Fallback to in-memory metadata if SQLite row is missing
            vm_ip = self.ip or ""
            group_name = self._session_meta.get("group_name", "")

        if not vm_ip:
            self.log.warning(
                "CustomSpawner: no VM IP found for '%s'; cannot stop.", username
            )
            return

        # SIGTERM → 5 s → SIGKILL
        pid_file = _PID_FILE.format(self._os_username())
        cleanup_cmd: str = (
            f"if [ -f {pid_file} ]; then "
            f"  PID=$(cat {pid_file}); "
            f"  if ps -p $PID > /dev/null 2>&1; then "
            f"    kill -15 $PID; "
            f"    sleep 5; "
            f"    kill -9 $PID 2>/dev/null || true; "
            f"  fi; "
            f"  rm -f {pid_file}; "
            f"fi"
        )

        self.log.info("CustomSpawner: stopping server for '%s' on %s", username, vm_ip)
        try:
            server_info = self._remote_server_info(vm_ip, group_name)
            await asyncio.to_thread(self._run_remote, server_info, cleanup_cmd)
        except Exception as exc:
            self.log.error("CustomSpawner: SSH stop failed for '%s': %s", username, exc)
            raise

        _store.clear_session(username)

    async def poll(self) -> Optional[int]:
        """
        Check whether the remote ``jupyterhub-singleuser`` process is alive.

        Reads the VM IP from SQLite so that ``poll()`` remains correct
        after a hub restart (crash recovery — no local state required).

        Returns:
            ``None`` if the process is running, or if its state cannot be
            checked right now (SSH/DB error) — a blip must not make the Hub
            forget a live server.
            ``0`` only when there is no session or the process is gone.
        """
        username: str = self.user.name

        try:
            session = _store.get_session(username)
        except Exception:  # noqa: BLE001
            return None  # DB error: state unknown, keep the server
        if not session:
            return 0

        vm_ip: str = session.get("vm_ip") or self.ip or ""
        group_name: str = session.get("group_name") or ""

        if not vm_ip:
            return 0

        try:
            server_info = self._remote_server_info(vm_ip, group_name)
            status: str = await asyncio.to_thread(
                self._run_remote,
                server_info,
                f"ps -p $(cat {_PID_FILE.format(self._os_username())} 2>/dev/null) > /dev/null 2>&1 "
                f"&& echo 0 || echo 1",
            )
        except Exception as exc:  # noqa: BLE001
            now = time.monotonic()
            if self._unreachable_since is None:
                self._unreachable_since = now
            down_for = now - self._unreachable_since
            if down_for < _UNREACHABLE_GRACE:
                self.log.warning(
                    "CustomSpawner.poll: SSH check failed for '%s' (%ds): %s",
                    username, down_for, exc,
                )
                return None  # a blip must not drop a live server
            # The worker has been gone too long (stopped, crashed): show the server as
            # stopped so the user can start again once a worker is reachable.
            self.log.warning(
                "CustomSpawner.poll: %s unreachable for %ds; marking '%s' stopped",
                vm_ip, down_for, username,
            )
            self._unreachable_since = None
            _store.set_session(username, status="stopped")
            return 0

        self._unreachable_since = None
        if status == "0":
            return None  # Running — JupyterHub Spawner API convention

        # Process is gone — update SQLite
        _store.set_session(username, status="stopped")
        self.log.info("CustomSpawner.poll: process stopped for '%s'", username)
        return 0

    def _delete_linux_user(self) -> None:
        """Blocking part of ``delete_forever()``; runs in a worker thread."""
        mode = hub_settings.get("on_user_delete", "archive")
        if mode == "keep":
            return
        if mode not in ("archive", "delete"):
            self.log.error("CustomSpawner: on_user_delete=%r is not archive/delete/keep; keeping the account", mode)
            return
        groups = [g.name for g in self.user.groups]
        if not groups:
            self.log.info("CustomSpawner: '%s' has no team; no worker account to delete", self.user.name)
            return
        server_info = self._get_server_info(groups[0])
        if server_info.get("ssh_user") or not server_info.get("admin_ssh_user"):
            # A shared ssh_user account belongs to the whole team: never delete it.
            return
        os_user = self._os_username()  # validated: safe in the shell script
        u = shlex.quote(os_user)
        if mode == "archive":
            # Notebooks are kept in /home/.deleted (root only); sudo sh -c 'rm -rf /home/.deleted/<user>-*' frees the disk.
            remove = (
                f"sudo mkdir -p /home/.deleted && sudo chmod 700 /home/.deleted\n"
                f"[ -d /home/{os_user} ] && sudo mv /home/{os_user} /home/.deleted/{os_user}-$(date +%Y%m%d-%H%M%S)\n"
                f"sudo userdel {u}\n"
            )
        else:
            remove = f"sudo userdel -r {u} 2>&1 | grep -v 'mail spool' || true\n"
        script = (
            f"id {u} >/dev/null 2>&1 || exit 0\n"
            f"sudo loginctl disable-linger {u} || true\n"
            f"sudo pkill -KILL -u {u} || true\n"
            f"sleep 1\n"
            f"{remove}"
            f"! id {u} >/dev/null 2>&1\n"
        )
        ssh = self._open_admin_ssh(server_info)
        if ssh is None:  # worker down: log, don't block deleting the Hub user
            self.log.warning(
                "CustomSpawner: Linux user '%s' on %s NOT deleted (worker unreachable); remove it by hand",
                os_user, server_info["server_ip"],
            )
            return
        try:
            _, stdout, stderr = ssh.exec_command(script)
            if stdout.channel.recv_exit_status() != 0:
                self.log.error(
                    "CustomSpawner: deleting Linux user '%s' on %s failed: %s",
                    os_user, server_info["server_ip"], stderr.read().decode(),
                )
            else:
                self.log.info("CustomSpawner: Linux user '%s' on %s deleted (%s)", os_user, server_info["server_ip"], mode)
        finally:
            ssh.close()

    async def delete_forever(self) -> None:
        """Hub user deleted: remove their Linux account on the team's worker (``on_user_delete``)."""
        try:
            _store.delete_user_limits(self.user.name)
            await asyncio.to_thread(self._delete_linux_user)
        except Exception as exc:  # noqa: BLE001
            self.log.error("CustomSpawner: could not delete Linux user for '%s': %s", self.user.name, exc)

    def clear_state(self) -> None:
        """
        Reset all session state for this user.

        Removes the SQLite session row and zeroes all instance-level
        tracking variables.  Called by JupyterHub on logout or hub restart,
        and may be called explicitly by an admin action.
        """
        username: str = self.user.name
        _store.clear_session(username)
        self._session_meta = {}
        self.ip = ""
        self.port = 0
        self.log.info("CustomSpawner: state cleared for user '%s'", username)
