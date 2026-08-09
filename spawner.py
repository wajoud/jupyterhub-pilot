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

RBAC
────
  2-tier model:
    admin  — can stop/inspect any user's session  (set via JupyterHub Admin Panel)
    user   — can only manage their own session

  To promote a user to admin, use the JupyterHub Admin Panel or add their name
  to ``admin_users`` in ``hub_settings.json`` and restart the hub.  No code or
  DB changes required.

Lifecycle Hooks
───────────────
  ``pre_spawn_hook``  — stub for Task 2 (cgroups v2) and Task 3 (Vault secrets)
  ``post_stop_hook``  — stub for Task 4 (resource locks + telemetry flush)
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import paramiko

try:
    import paramiko.ssh_exception as _paramiko_ssh_exc
except (ImportError, ModuleNotFoundError):
    # Under test mocks paramiko is a MagicMock; ssh_exception is an attribute.
    _paramiko_ssh_exc = getattr(paramiko, "ssh_exception", paramiko)  # type: ignore
from jupyterhub.spawner import Spawner

from jupyterhub_pilot.admin import RBACManager
from jupyterhub_pilot.session_store import SessionStore
from jupyterhub_pilot.vault_client import VaultClient

# ──────────────────────────────────────────────────────────────────────────────
# Module-level config bootstrap
# ──────────────────────────────────────────────────────────────────────────────

BASE_DIR: str = os.path.dirname(__file__)
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

# PERF FIX: cache the result of ping() so start/stop/poll don't hit SQLite
# twice per operation.  The TTL of 5 s means a recovered DB is re-detected
# quickly without hammering it on every spawner call.
import time as _time  # noqa: E402  (already imported above via asyncio)
_PING_CACHE: Optional[bool] = None
_PING_CACHE_AT: float = 0.0
_PING_TTL: float = 5.0  # seconds


def _db_available() -> bool:
    """Return cached _store.ping() result; re-checks at most every _PING_TTL seconds."""
    global _PING_CACHE, _PING_CACHE_AT
    now = _time.monotonic()
    if _PING_CACHE is None or (now - _PING_CACHE_AT) > _PING_TTL:
        _PING_CACHE = _store.ping()
        _PING_CACHE_AT = now
    return _PING_CACHE


# ──────────────────────────────────────────────────────────────────────────────
# CustomSpawner
# ──────────────────────────────────────────────────────────────────────────────


class CustomSpawner(Spawner):
    """
    SSH-based JupyterHub spawner with SQLite session state and 2-tier RBAC.

    Attributes:
        _pid_file (Optional[str]): Remote path to the PID file for the
            singleuser server process.
        _session_meta (Dict[str, Any]): In-memory copy of the last-written
            session record (pid, vm_ip, port, start_time, role, group_name).
    """

    _pid_file: Optional[str] = None
    _session_meta: Dict[str, Any] = {}  # noqa: RUF012

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

    def _load_mapping(self) -> Dict[str, Any]:
        """
        Load the team→VM mapping.

        Tries SQLite first; falls back to user_mapping.json if the DB is
        unreachable.

        Returns:
            Dict mapping team names to server info dicts.
        """
        # PERF FIX: use cached ping result — no extra DB call here.
        if _db_available():
            return {}  # callers that need a single team use _get_server_info
        return self._load_mapping_fallback()

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
        # PERF FIX: reuse the cached ping result — avoids a second DB round-trip
        # on the same request path that _load_mapping() already checked.
        if _db_available():
            row = _store.get_mapping(group)
            if row is not None:
                # Normalise key names to match existing code paths
                return {
                    "server_ip": row["server_ip"],
                    "server_ssh_key": row["ssh_key"],
                    "ssh_user": row.get("ssh_user"),
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

    def _get_ssh_username(self, server_info: Dict[str, Any]) -> str:
        """
        Return the SSH login username.

        Uses the optional ``ssh_user`` field from the mapping; falls back to
        the JupyterHub username so the spawner SSHs as the individual user.
        """
        return server_info.get("ssh_user") or self.user.name

    def _provision_user_jit(self, server_info: Dict[str, Any]) -> None:
        """
        Just-In-Time provision the OS user on the remote worker VM.
        Connects as 'admin_ssh_user' (e.g. 'ubuntu') using the Hub's SSH key,
        creates the user, adds their SSH key, and installs dependencies.
        """
        admin_user = server_info.get("admin_ssh_user")
        if not admin_user:
            return  # Legacy mode, no admin user configured for provisioning

        target_user = self.user.name

        # Open SSH connection as admin user
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
        except Exception as e:
            self.log.warning(f"JIT Provisioner failed to connect as {admin_user}: {e}")
            return

        # Provisioning script
        script = f"""
        if id "{target_user}" &>/dev/null; then
            exit 0
        fi

        echo "Creating user {target_user}..."
        sudo adduser --disabled-password --gecos "" "{target_user}"
        sudo loginctl enable-linger "{target_user}"

        sudo -u "{target_user}" mkdir -p "/home/{target_user}/notebook"
        sudo -u "{target_user}" mkdir -p "/home/{target_user}/.ssh"

        sudo cp "/home/{admin_user}/.ssh/authorized_keys" "/home/{target_user}/.ssh/authorized_keys"
        sudo chown -R "{target_user}:{target_user}" "/home/{target_user}/.ssh"
        sudo chmod 600 "/home/{target_user}/.ssh/authorized_keys"

        sudo -u "{target_user}" pip3 install jupyterhub notebook jupyterhub_pilot[ai]@git+https://github.com/wajoud/jupyterhub-pilot.git --break-system-packages

        # Copy the get_port.py script so port allocation works
        sudo cp /opt/jupyterhub-pilot/get_port.py "/home/{target_user}/get_port.py"
        sudo chown "{target_user}:{target_user}" "/home/{target_user}/get_port.py"
        """

        stdin, stdout, stderr = ssh.exec_command(script)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            err = stderr.read().decode()
            self.log.error(f"JIT Provisioning script failed with status {exit_status}:\\n{err}")
        else:
            self.log.info(f"JIT Provisioning succeeded for user {target_user}.")

        ssh.close()

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

    # Kept for backward-compatibility with existing unit tests
    def get_remote_ssh(
        self, server_info: Dict[str, Any], ssh_user: str
    ) -> paramiko.SSHClient:
        """Open SSH connection using *ssh_user* as the login name."""
        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            server_info["server_ip"],
            username=ssh_user,
            key_filename=server_info["server_ssh_key"],
            timeout=10,
            banner_timeout=15,
            auth_timeout=15,
        )
        return ssh

    # ── Lifecycle hooks ───────────────────────────────────────────────────────

    async def pre_spawn_hook(self, spawner: Optional[Spawner] = None) -> None:
        """
        Pre-spawn lifecycle hook.

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

        # ── Task 2: cache cgroup limits for use in start() ────────────────────
        limits: Optional[Dict[str, Any]] = hub_settings.get("resource_limits")
        if limits:
            self._memory_max: Optional[str] = limits.get("memory_max")
            self._cpu_quota: Optional[str] = limits.get("cpu_quota")
            self.log.info(
                "CustomSpawner: cgroups v2 limits for '%s': "
                "MemoryMax=%s CPUQuota=%s",
                username,
                self._memory_max,
                self._cpu_quota,
            )
        else:
            self._memory_max = None
            self._cpu_quota = None
            self.log.debug(
                "CustomSpawner: no resource_limits configured — "
                "launching '%s' without cgroup wrapping.",
                username,
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

    async def post_stop_hook(self, spawner: Optional[Spawner] = None) -> None:
        """
        Post-stop lifecycle hook — no-op stub for future tasks.

        Task 4 integration point: resource lock release and telemetry flush.
        """
        self.log.debug(
            "CustomSpawner: post_stop_hook (stub) for user '%s'", self.user.name
        )

    # ── Core Spawner API ──────────────────────────────────────────────────────

    async def start(self) -> Tuple[str, int]:
        """
        Resolve the target VM, open an SSH connection, and launch
        ``jupyterhub-singleuser`` on the remote host.

        Steps:
            1. RBAC check — only the user themselves (or an admin) may spawn.
            2. Resolve team → VM mapping (SQLite → JSON fallback).
            3. Run ``pre_spawn_hook`` stub.
            4. Connect via SSH.
            5. Acquire a free port; kill stale occupants if necessary.
            6. Write the OOM enforcement IPython startup script.
            7. Launch ``jupyterhub-singleuser`` in the background.
            8. Persist session metadata to SQLite.
            9. Return ``(ip, port)`` to the JupyterHub proxy.

        Returns:
            Tuple of ``(ip_address, port)`` for the JupyterHub proxy.

        Raises:
            RuntimeError: On group/mapping resolution failure.
            paramiko.ssh_exception.SSHException: On SSH connectivity failure.
        """
        username: str = self.user.name

        # 1. RBAC
        _rbac.assert_can_act_on(self.user, username)

        # 2. Resolve group and VM
        user_groups = [g.name for g in self.user.groups]
        if not user_groups:
            raise RuntimeError(
                f"User '{username}' has no groups assigned. "
                "Assign the user to a team group in JupyterHub before spawning."
            )

        group: str = user_groups[0]
        server_info: Dict[str, Any] = self._get_server_info(group)
        self.ip = server_info["server_ip"]
        self._pid_file = f"/tmp/jupyterhub-{username}.pid"

        role: str = _rbac.get_role(self.user)

        # 3. Pre-spawn hook
        await self.pre_spawn_hook()

        try:
            # 4. Provision User JIT
            self._provision_user_jit(server_info)

            # 5. SSH connect as the target user
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
            # 5a. Acquire port
            _, stdout, _ = ssh.exec_command("python3 ~/get_port.py")
            self.port = int(stdout.readline().strip())

            # 5b. Check for stale process on that port
            check_cmd = f"netstat -tuln | grep ':{self.port} ' || true"
            _, stdout, _ = ssh.exec_command(check_cmd)
            if stdout.read().strip():
                self.log.warning(
                    "CustomSpawner: port %d on %s already in use for '%s'. "
                    "Attempting to clear stale process...",
                    self.port,
                    self.ip,
                    username,
                )
                _, fuser_out, _ = ssh.exec_command(f"fuser -k {self.port}/tcp || true")
                fuser_out.channel.recv_exit_status()  # wait for completion
                await asyncio.sleep(1)

            # 6. OOM enforcement startup script
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

            # 7. Build environment and launch singleuser server
            hub_api_url: Optional[str] = hub_settings.get("hub_api_url")
            if not hub_api_url:
                raise RuntimeError("hub_api_url not found in hub_settings.json")

            env = self.get_env()
            env["JUPYTERHUB_API_URL"] = hub_api_url

            exports: str = " ".join(
                f"{k}={shlex.quote(str(v))}"
                for k, v in env.items()
            )

            log_file: str = f"/tmp/jupyterhub-{username}.log"

            # Build the base singleuser command
            singleuser_cmd: str = (
                f"nohup jupyterhub-singleuser "
                f"--ip=0.0.0.0 "
                f"--port={self.port} "
                f"--ServerApp.base_url={self.server.base_url} "
                f"--notebook-dir='~/notebook/'"
            )

            # Task 2: wrap with systemd-run if cgroup limits are configured
            mem = getattr(self, "_memory_max", None)
            cpu = getattr(self, "_cpu_quota", None)
            if mem or cpu:
                unit_name = f"jupyterhub-{username}-{int(datetime.now(timezone.utc).timestamp())}"
                props = ""
                if mem:
                    props += f" --property=MemoryMax={mem}"
                if cpu:
                    props += f" --property=CPUQuota={cpu}"
                singleuser_cmd = (
                    f"systemd-run --user --scope "
                    f"--unit={unit_name}{props} "
                    f"-- {singleuser_cmd}"
                )
                self.log.info(
                    "CustomSpawner: wrapping '%s' with systemd-run "
                    "(MemoryMax=%s, CPUQuota=%s)",
                    username,
                    mem,
                    cpu,
                )

            cmd: str = (
                f"{exports} "
                f"{singleuser_cmd} "
                f"&> {log_file} & "
                f"echo $! > {self._pid_file} && disown"
            )

            self.log.info(
                "CustomSpawner: spawning server for '%s' on %s:%d (role=%s)",
                username,
                self.ip,
                self.port,
                role,
            )
            ssh.exec_command(f"bash -l -c {shlex.quote(cmd)}")

        finally:
            ssh.close()

        # 8. Persist session to SQLite
        start_time: str = datetime.now(timezone.utc).isoformat()
        self._session_meta = {
            "pid": None,  # PID is written async by the remote shell
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

        # 9. Tell the JupyterHub proxy where to route traffic
        return (self.ip, self.port)

    async def stop(self, now: bool = False) -> None:
        """
        Terminate the remote ``jupyterhub-singleuser`` process for this user.

        Reads the target VM IP and PID from SQLite so that this method works
        correctly even after a hub crash and restart (crash recovery).

        Termination sequence:
            SIGTERM → wait 5 seconds → SIGKILL (if process still alive)

        Args:
            now: Ignored (present for Spawner API compatibility).
        """
        username: str = self.user.name

        # Prefer session info from SQLite (crash recovery) over instance vars
        session = _store.get_session(username)

        if session:
            vm_ip: str = session.get("vm_ip", self.ip or "")
            group_name: str = session.get("group_name", "")
        else:
            # Fallback to in-memory metadata if SQLite row is missing
            vm_ip = self.ip or ""
            group_name = self._session_meta.get("group_name", "")

        if not vm_ip:
            self.log.warning(
                "CustomSpawner: no VM IP found for '%s'; cannot stop.", username
            )
            return

        # Resolve SSH connection details
        try:
            server_info = self._get_server_info(group_name) if group_name else {}
            if not server_info:
                # Fallback: build minimal server_info from session data
                server_info = {"server_ip": vm_ip, "server_ssh_key": ""}
        except Exception as exc:
            self.log.error(
                "CustomSpawner.stop: mapping lookup failed for '%s': %s",
                username,
                exc,
            )
            return

        pid_file: str = self._pid_file or f"/tmp/jupyterhub-{username}.pid"

        # SIGTERM → 5 s → SIGKILL
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

        try:
            self.log.info(
                "CustomSpawner: stopping server for '%s' on %s", username, vm_ip
            )
            ssh = self._open_ssh(server_info)
            try:
                _, stop_out, _ = ssh.exec_command(cleanup_cmd)
                stop_out.channel.recv_exit_status()  # wait for completion
            finally:
                ssh.close()
        except Exception as exc:  # noqa: BLE001
            self.log.error("CustomSpawner: SSH stop failed for '%s': %s", username, exc)

        # Update SQLite — mark stopped, then remove the row
        _store.set_session(username, status="stopped")
        _store.clear_session(username)

        # Post-stop hook (telemetry / lock-release stub)
        await self.post_stop_hook()

    async def poll(self) -> Optional[int]:
        """
        Check whether the remote ``jupyterhub-singleuser`` process is alive.

        Reads the VM IP and PID from SQLite so that ``poll()`` remains correct
        after a hub restart (crash recovery — no local state required).

        Returns:
            ``None`` if the process is running.
            ``0`` if the process has stopped or an error occurred.
        """
        username: str = self.user.name

        # Prefer SQLite session data for crash-safe polling
        session = _store.get_session(username)
        if not session:
            # No session row → assume not running
            return 0

        vm_ip: str = session.get("vm_ip", self.ip or "")
        group_name: str = session.get("group_name", "")

        if not vm_ip:
            return 0

        pid_file: str = self._pid_file or f"/tmp/jupyterhub-{username}.pid"

        try:
            server_info = (
                self._get_server_info(group_name)
                if group_name
                else {"server_ip": vm_ip, "server_ssh_key": ""}
            )
            ssh = self._open_ssh(server_info)
            try:
                _, stdout, _ = ssh.exec_command(
                    f"ps -p $(cat {pid_file} 2>/dev/null) > /dev/null 2>&1 "
                    f"&& echo 0 || echo 1"
                )
                status: str = stdout.read().decode().strip()
            finally:
                ssh.close()
        except Exception as exc:  # noqa: BLE001
            self.log.warning(
                "CustomSpawner.poll: SSH check failed for '%s': %s", username, exc
            )
            return 0

        if status == "0":
            return None  # Running — JupyterHub Spawner API convention

        # Process is gone — update SQLite
        _store.set_session(username, status="stopped")
        self.log.info("CustomSpawner.poll: process stopped for '%s'", username)
        return 0

    def clear_state(self) -> None:
        """
        Reset all session state for this user.

        Removes the SQLite session row and zeroes all instance-level
        tracking variables.  Called by JupyterHub on logout or hub restart,
        and may be called explicitly by an admin action.
        """
        username: str = self.user.name
        _store.clear_session(username)
        self._pid_file = None
        self._session_meta = {}
        self.ip = ""
        self.port = 0
        self.log.info("CustomSpawner: state cleared for user '%s'", username)
