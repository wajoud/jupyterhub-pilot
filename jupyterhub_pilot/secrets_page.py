"""
Admin page for the environment variables servers get from Vault.

A team's variables go to every member's server, a user's own variables win over
the team's. The page lists names only: a value can be replaced or deleted, never
read back. Read at every server start, so a change applies on the next start.
"""

from __future__ import annotations

import asyncio
import calendar
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from jupyterhub.handlers import BaseHandler
from tornado import web

from jupyterhub_pilot.vault_client import ENV_NAME_RE, VaultClient, VaultError, secret_path

Stored = Dict[str, Tuple[List[str], str]]  # name -> (variable names, time of the last change)


def check_name(name: str) -> Optional[str]:
    """Error message for a bad variable name, or None if it is valid."""
    if not ENV_NAME_RE.fullmatch(name):
        return f"{name!r} is not a variable name: use letters, digits and _ (like OPENAI_API_KEY)."
    if name.startswith("JUPYTERHUB_"):
        return "Names starting with JUPYTERHUB_ belong to the Hub; the server would not start."
    return None


def _epoch(vault_time: str) -> float:
    """Vault's ``2026-11-12T10:00:00.123456789Z`` as seconds since 1970 (0 if absent)."""
    try:
        return calendar.timegm(time.strptime(vault_time[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0.0


def build_view(
    users: Iterable[Tuple[str, Dict[str, Any]]],
    groups: Iterable[Tuple[str, Any, int]],
    worker_ip_for_group: Callable[[str], Optional[str]],
    snapshots: Dict[str, Dict[str, Any]],
    user_secrets: Stored,
    team_secrets: Stored,
    now: float,
) -> Dict[str, Any]:
    """What the Secrets page shows: one row per team and per user, with variable names only.

    *users* and *groups* are as in ``limits.build_view``; *snapshots* tells which servers run and since when.
    """
    users = list(users)
    live = {u.get("name"): u for s in snapshots.values() for u in s.get("users") or [] if isinstance(u, dict)}
    teams = [
        {"name": name, "members": members, "keys": team_secrets.get(name, ([], ""))[0]}
        for name, _props, members in groups
        if worker_ip_for_group(name)
    ]
    team_names = {t["name"] for t in teams}

    rows = []
    for name, user_groups in [*users, *((n, None) for n in sorted(set(user_secrets) - {u[0] for u in users}))]:
        mine = sorted(set(user_groups or {}) & team_names)
        team = mine[0] if mine else ""  # as the spawner: the first team by name
        own, own_changed = user_secrets.get(name, ([], ""))
        inherited, team_changed = team_secrets.get(team, ([], ""))
        u = live.get(name)
        started = now - (u.get("uptime_s") or 0) if u else None
        rows.append({
            "username": name, "teams": mine, "known": user_groups is not None, "keys": own,
            "team_keys": [k for k in inherited if k not in own],
            "running": u is not None,
            # shortcut: deleting the last variable leaves no change time, so no note then; store one if admins miss it
            "restart": started is not None and max(_epoch(own_changed), _epoch(team_changed)) > started,
        })
    return {"teams": teams, "users": rows}


class SecretsPageHandler(BaseHandler):
    """Admin-only ``/hub/secrets``: variable names per team and user; add, replace and delete them."""

    def initialize(
        self,
        enabled: Callable[[], bool] = lambda: False,
        vault: Callable[[], Any] = VaultClient,
        worker_ip_for_group: Callable[[str], Optional[str]] = lambda group: None,
        snapshots: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        self._enabled = enabled
        self._vault = vault
        self._worker_ip_for_group = worker_ip_for_group
        self._snapshots = snapshots if snapshots is not None else {}

    def _require_admin(self) -> None:
        if not self.current_user.admin:
            raise web.HTTPError(403, "Only admins can change secrets.")

    def _hub_users(self) -> List[Tuple[str, Dict[str, Any]]]:
        from jupyterhub import orm

        return [(u.name, {g.name: g.properties for g in u.groups}) for u in self.db.query(orm.User).order_by(orm.User.name)]

    def _hub_groups(self) -> List[Tuple[str, Any, int]]:
        from jupyterhub import orm

        return [(g.name, g.properties, len(g.users)) for g in self.db.query(orm.Group).order_by(orm.Group.name)]

    def _load(self, teams: List[str]) -> Tuple[Stored, Stored]:
        """Names and change times from Vault. Values are read (KV has no names-only call) and dropped here."""
        vault = self._vault()

        def names(path: str) -> Tuple[List[str], str]:
            data, changed = vault.read(path)
            return sorted(data), changed

        # shortcut: one Vault call per user that has secrets, fine for hundreds; cache if the page gets slow
        by_user = {n: names(secret_path("user", n)) for n in vault.list_names()}
        by_team = {t: names(secret_path("team", t)) for t in teams}
        return by_user, {t: v for t, v in by_team.items() if v[0]}

    def _change(self, path: str, name: str, value: Optional[str]) -> None:
        """Set (or with None delete) one variable, keeping the others at *path*."""
        vault = self._vault()
        data, _changed = vault.read(path)
        if value is None:
            data.pop(name, None)
        else:
            data[name] = value
        vault.write(path, data)

    async def _render(self, error: str = "", message: str = "") -> None:
        view: Dict[str, Any] = {"teams": [], "users": []}
        off = not self._enabled()
        if not off:
            groups = self._hub_groups()
            teams = [g[0] for g in groups if self._worker_ip_for_group(g[0])]
            try:
                by_user, by_team = await asyncio.to_thread(self._load, teams)
                view = build_view(
                    self._hub_users(), groups, self._worker_ip_for_group, self._snapshots, by_user, by_team, time.time()
                )
            except VaultError as exc:
                error = error or str(exc)
        self.finish(await self.render_template("secrets.html", error=error, message=message, off=off, **view))

    @web.authenticated
    async def get(self) -> None:
        self._require_admin()
        await self._render()

    @web.authenticated
    async def post(self) -> None:
        self._require_admin()

        async def refuse(why: str) -> None:
            self.set_status(400)
            await self._render(error=why)

        if not self._enabled():
            return await refuse("Vault is off.")
        scope = "team" if self.get_body_argument("scope", "user") == "team" else "user"
        target = self.get_body_argument("target", "").strip()
        name = self.get_body_argument("name", "").strip()
        value: Optional[str] = self.get_body_argument("value", "")
        if self.get_body_argument("action", "set") == "delete":
            value = None
        if not target or (scope == "team" and not self._worker_ip_for_group(target)):
            return await refuse("Enter a user name." if scope == "user" else f"{target or 'That'} is not a team with a worker.")
        if value is None:  # any stored name can be deleted, also one this page would not accept
            err = None if name else "Which variable?"
        else:
            err = check_name(name) or (None if value else "Enter a value.")
        if err:
            return await refuse(err)
        try:
            await asyncio.to_thread(self._change, secret_path(scope, target), name, value)
        except VaultError as exc:
            return await refuse(str(exc))
        who = f"Team {target}" if scope == "team" else target
        self.log.info("Secrets page: %s %s %s for %s", self.current_user.name, "deleted" if value is None else "set", name, who)
        await self._render(message=f"{who}: {name} {'deleted' if value is None else 'saved'}. Applies on the next server start.")
