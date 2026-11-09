"""
Per-team and per-user RAM/CPU limits.

Order, per value: user override (``/hub/limits`` page, SQLite) → team (the
group's properties in Admin → Groups) → ``resource_limits`` in hub_settings.json.
Read at every server start, so a change applies on the user's next start.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Optional, Tuple

from jupyterhub.handlers import BaseHandler
from tornado import web

KEYS = ("memory_max", "cpu_quota")
_VALID = {
    "memory_max": re.compile(r"[1-9][0-9]*[MGT]"),  # MemoryMax; no bare bytes or K (would kill every kernel)
    "cpu_quota": re.compile(r"[1-9][0-9]*%"),  # systemd CPUQuota: 50%, 200%
}
_HINT = {"memory_max": "a size like 512M or 2G", "cpu_quota": "a percentage like 50% or 200%"}


def check(key: str, value: Any) -> Optional[str]:
    """Error message for a bad value, or None if it is valid."""
    if isinstance(value, str) and _VALID[key].fullmatch(value):
        return None
    return f"{key} must be {_HINT[key]}, got {value!r}"


def resolve(
    user: Optional[Dict[str, Any]],
    team: Optional[Dict[str, Any]],
    default: Optional[Dict[str, Any]],
    warn: Callable[[str], None],
) -> Dict[str, Tuple[Optional[str], str]]:
    """{key: (value, where it came from)}; invalid values are skipped with a warning."""
    out: Dict[str, Tuple[Optional[str], str]] = {}
    for key in KEYS:
        out[key] = (None, "none")
        for source, layer in (("user", user), ("team", team), ("default", default)):
            value = (layer or {}).get(key)
            if value in (None, ""):
                continue
            err = check(key, value)
            if err:
                warn(f"{source} limit ignored: {err}")
                continue
            out[key] = (value, source)
            break
    return out


class LimitsPageHandler(BaseHandler):
    """Admin-only ``/hub/limits``: list, set and clear per-user limits and the per-user terminal switch."""

    def initialize(self, store: Any) -> None:
        self.store = store

    def _require_admin(self) -> None:
        if not self.current_user.admin:
            raise web.HTTPError(403, "Only admins can change limits.")

    async def _render(self, error: str = "", message: str = "") -> None:
        html = await self.render_template(
            "limits.html", overrides=self.store.list_user_limits(), error=error, message=message
        )
        self.finish(html)

    @web.authenticated
    async def get(self) -> None:
        self._require_admin()
        await self._render()

    @web.authenticated
    async def post(self) -> None:
        self._require_admin()
        username = self.get_body_argument("username", "").strip()
        if not username:
            return await self._render(error="Enter a user name.")
        if self.get_body_argument("action", "set") == "clear":
            self.store.delete_user_limits(username)
            return await self._render(message=f"{username}: back to team / default limits.")
        values = {k: self.get_body_argument(k, "").strip() or None for k in KEYS}
        terminals_off = self.get_body_argument("terminals_off", "") == "on"  # an unticked checkbox sends nothing
        errors = [e for k, v in values.items() if v is not None and (e := check(k, v))]
        if errors:
            self.set_status(400)
            return await self._render(error="; ".join(errors))
        if not any(values.values()) and not terminals_off:
            return await self._render(error="Set memory_max, cpu_quota or Terminal off (or use Clear).")
        self.store.set_user_limits(username, values["memory_max"], values["cpu_quota"], terminals_off)
        await self._render(message=f"{username}: saved. Applies on their next server start.")
