"""
jupyterhub_pilot/vault_client.py
─────────────────────────────
HashiCorp Vault integration for JupyterHub-Pilot.

Reads per-user secrets from Vault's KV v2 engine and returns them as a plain
dict so the caller can inject them directly into ``os.environ`` / the spawner
environment — zero disk I/O, zero subprocess, zero new pip dependencies.

Configuration
─────────────
Set these environment variables on the Hub VM before starting JupyterHub:

    VAULT_ADDR   – Vault server URL, e.g. http://127.0.0.1:8200
    VAULT_TOKEN  – Root or policy-scoped token with read access to the path

Secret path convention:
    ``secret/jupyterhub-pilot/<username>``

All key-value pairs stored at that path are injected as env vars into the
user's Jupyter kernel at spawn time.

Graceful degradation
────────────────────
If ``VAULT_ADDR`` or ``VAULT_TOKEN`` are missing, or if Vault is unreachable,
``get_user_secrets()`` returns ``{}`` and logs a warning.  The spawner
continues uninterrupted — Vault is opt-in.

Example::

    client = VaultClient(secret_base_path="secret/jupyterhub-pilot")
    secrets = client.get_user_secrets("alice")
    # {"DATABASE_URL": "postgres://...", "API_KEY": "sk-..."}
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

log = logging.getLogger("jupyterhub")

# Connection timeout for Vault HTTP calls (seconds)
_VAULT_TIMEOUT: int = 5

# Environment variable names only: the spawner writes `export NAME=...` into a shell script on the worker.
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class VaultError(Exception):
    """A Vault call for the Secrets page failed; the message is written for the admin."""


def secret_path(scope: str, name: str) -> str:
    """Path under the base for a user's or a team's secrets (``alice``, ``_team/team_alpha``)."""
    if name in ("", ".", ".."):
        raise VaultError(f"{name!r} is not a valid name.")
    return ("_team/" if scope == "team" else "") + quote(name, safe="")


class VaultClient:
    """
    Lightweight HashiCorp Vault KV-v2 client using only ``requests``.

    Args:
        secret_base_path: The KV mount + base path, e.g. ``"secret/jupyterhub-pilot"``.
                          The username is appended automatically:
                          ``secret/jupyterhub-pilot/<username>``.

    Attributes:
        _addr:  Vault server URL from ``VAULT_ADDR`` env var.
        _token: Vault token from ``VAULT_TOKEN`` env var.
        _base:  The resolved KV v2 API base path.
    """

    def __init__(self, secret_base_path: str = "secret/jupyterhub-pilot") -> None:
        self._addr: str = os.environ.get("VAULT_ADDR", "").rstrip("/")
        self._token: str = os.environ.get("VAULT_TOKEN", "")
        # KV v2 API path: /v1/<mount>/data/<subpath>
        # e.g. "secret/jupyterhub-pilot" -> "/v1/secret/data/jupyterhub_pilot"
        parts = secret_base_path.split("/", 1)
        mount = parts[0]
        subpath = parts[1] if len(parts) > 1 else ""
        self._mount, self._sub = mount, subpath
        self._base: str = (
            f"/v1/{mount}/data/{subpath}" if subpath else f"/v1/{mount}/data"
        )

    # -- Public API ------------------------------------------------------------

    def is_configured(self) -> bool:
        """Return True if both VAULT_ADDR and VAULT_TOKEN are set."""
        return bool(self._addr and self._token)

    def renew_self(self) -> bool:
        """Renew the Hub's own token (a periodic token lives as long as it is renewed in time)."""
        if not self.is_configured():
            return False
        try:
            r = requests.post(
                f"{self._addr}/v1/auth/token/renew-self",
                headers={"X-Vault-Token": self._token},
                timeout=_VAULT_TIMEOUT,
            )
        except requests.exceptions.RequestException as exc:
            log.warning("VaultClient: token renewal failed: %s", exc)
            return False
        if not r.ok:
            log.warning(
                "VaultClient: token renewal refused (HTTP %d). The token's policy needs "
                '`path "auth/token/renew-self" { capabilities = ["update"] }` (docs/SECURITY.md §4).',
                r.status_code,
            )
        return r.ok

    # -- For the Secrets page: errors are raised, not swallowed -------------------

    def _call(self, method: str, kind: str, path: str = "", **kwargs: Any) -> Optional[Dict[str, Any]]:
        """One KV v2 request (*kind* is ``data`` or ``metadata``). None when nothing is stored there."""
        if not self.is_configured():
            raise VaultError("VAULT_ADDR and VAULT_TOKEN are not set in /etc/jupyterhub-pilot/hub.env.")
        url = "/".join(p for p in (self._addr, "v1", self._mount, kind, self._sub, path) if p)
        try:
            r = requests.request(
                method, url, headers={"X-Vault-Token": self._token}, timeout=_VAULT_TIMEOUT, **kwargs
            )
        except requests.exceptions.RequestException as exc:
            raise VaultError(f"Cannot reach Vault at {self._addr} ({type(exc).__name__}).") from exc
        if r.status_code == 404:
            return None
        if r.status_code == 403:
            raise VaultError(
                "Vault refused this: the Hub's token may only read. "
                "Write the policy in docs/SECURITY.md §4 step 2 again to let this page list and change secrets."
            )
        if not r.ok:
            raise VaultError(f"Vault returned HTTP {r.status_code}.")
        return r.json() if r.content else {}

    def list_names(self) -> List[str]:
        """Names that have secrets directly under the base path (the users)."""
        found = self._call("GET", "metadata", params={"list": "true"}) or {}
        return [k for k in found.get("data", {}).get("keys", []) if not k.endswith("/")]

    def read(self, path: str) -> Tuple[Dict[str, Any], str]:
        """(key/values, time of the last change as Vault writes it) at *path*; ({}, "") when nothing is stored."""
        found = (self._call("GET", "data", path) or {}).get("data") or {}
        return found.get("data") or {}, (found.get("metadata") or {}).get("created_time", "")

    def write(self, path: str, data: Dict[str, str]) -> None:
        """Replace everything at *path*; an empty dict removes the secret with all its old versions."""
        if data:
            self._call("POST", "data", path, json={"data": data})
        else:
            self._call("DELETE", "metadata", path)

    def get_user_secrets(self, username: str) -> Dict[str, Any]:
        """
        Fetch the KV-v2 secret for *username* and return it as a flat dict.

        The secret is read from ``<secret_base_path>/<username>``.

        Returns:
            A dict of key -> value pairs from Vault, ready for env injection.
            Returns ``{}`` on any error (Vault unreachable, path missing, bad
            token, etc.).

        Raises:
            Nothing -- all exceptions are caught and logged as warnings so the
            spawner is never interrupted by a Vault failure.
        """
        if not self.is_configured():
            log.debug(
                "VaultClient: VAULT_ADDR/VAULT_TOKEN not set -- "
                "skipping secret injection for '%s'.",
                username,
            )
            return {}

        url = f"{self._addr}{self._base}/{username}"
        headers = {"X-Vault-Token": self._token}

        try:
            response = requests.get(url, headers=headers, timeout=_VAULT_TIMEOUT)
        except requests.exceptions.ConnectionError:
            log.warning(
                "VaultClient: cannot connect to Vault at %s -- "
                "proceeding without secrets for '%s'.",
                self._addr,
                username,
            )
            return {}
        except requests.exceptions.Timeout:
            log.warning(
                "VaultClient: Vault request timed out after %ds for '%s'.",
                _VAULT_TIMEOUT,
                username,
            )
            return {}
        except Exception as exc:  # noqa: BLE001
            log.warning("VaultClient: unexpected error for '%s': %s", username, exc)
            return {}

        if response.status_code == 404:
            log.debug(
                "VaultClient: no secrets at path '%s/%s' -- none injected.",
                self._base,
                username,
            )
            return {}

        if response.status_code == 403:
            log.warning(
                "VaultClient: permission denied reading secrets for '%s'. "
                "Check your Vault token policy.",
                username,
            )
            return {}

        if not response.ok:
            log.warning(
                "VaultClient: Vault returned HTTP %d for '%s'.",
                response.status_code,
                username,
            )
            return {}

        try:
            # KV v2 response shape: {"data": {"data": {...secrets...}}}
            payload = response.json()
            secrets: Dict[str, Any] = payload["data"]["data"]
            # Sanitise: keep only string-convertible values, skip None
            clean: Dict[str, str] = {
                k: str(v) for k, v in secrets.items() if v is not None
            }
            log.info(
                "VaultClient: injected %d secret(s) for '%s'.",
                len(clean),
                username,
            )
            return clean
        except (KeyError, ValueError) as exc:
            log.warning(
                "VaultClient: unexpected Vault response shape for '%s': %s",
                username,
                exc,
            )
            return {}


def start_renewer(every: float = 86400.0) -> None:
    """Renew the Hub's Vault token now and then daily, in a daemon thread (no cron job needed)."""
    client = VaultClient()
    if not client.is_configured():
        return

    def loop() -> None:
        while True:
            client.renew_self()
            time.sleep(every)

    threading.Thread(target=loop, name="vault-renew", daemon=True).start()
