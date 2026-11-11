"""
Per-team and per-user RAM/CPU limits.

Order, per value: user override (SQLite) → team (the group's properties) →
``resource_limits`` in hub_settings.json. Both the user and the team value are
set on the ``/hub/limits`` page. Read at every server start, so a change
applies on the user's next start.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

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


def _mb(value: str) -> int:
    return int(value[:-1]) * {"M": 1, "G": 1024, "T": 1048576}[value[-1]]


def _fmt_mb(mb: float) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def _differs(wanted: Optional[float], running: Any) -> bool:
    if wanted is None or not isinstance(running, (int, float)):
        return (wanted is None) != (not isinstance(running, (int, float)))
    return abs(wanted - running) > 1


def build_view(
    users: Iterable[Tuple[str, Dict[str, Any]]],
    groups: Iterable[Tuple[str, Any, int]],
    overrides: List[Dict[str, Any]],
    default: Optional[Dict[str, Any]],
    worker_ip_for_group: Callable[[str], Optional[str]],
    snapshots: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """What the Limits page shows: one row per team and per user, with the limit that applies and where it is from.

    *users* is ``[(name, {group name: group properties})]``, *groups* ``[(name, properties, member count)]``,
    *snapshots* the monitoring page's latest worker snapshots (what each running server's limit really is).
    """
    users = list(users)
    by_ip = {s.get("ip"): s for s in snapshots.values()}
    live = {u.get("name"): u for s in snapshots.values() for u in s.get("users") or [] if isinstance(u, dict)}
    by_user = {r["username"]: r for r in overrides}
    default = default or {}

    teams = []
    for name, props, members in groups:
        ip = worker_ip_for_group(name)
        if not ip:
            continue  # not a team: no worker
        props = props if isinstance(props, dict) else {}
        snap = by_ip.get(ip)
        if snap:
            size = [f"{snap['cpu']['core_count']} cores", _fmt_mb(snap["memory"]["total_mb"]) + " RAM"]
            size += [f"GPU {g.get('name')}" for g in snap.get("gpus") or []]
            worker = " · ".join(size)
        else:
            worker = "not reporting"
        teams.append({
            "name": name, "ip": ip, "members": members, "worker": worker,
            "servers": len(snap.get("users") or []) if snap else 0,
            **{k: props.get(k) or "" for k in KEYS},
            **{f"{k}_default": default.get(k) or "no limit" for k in KEYS},
        })
    team_names = {t["name"] for t in teams}

    rows = []
    for name, user_groups in [*users, *((n, None) for n in sorted(set(by_user) - {u[0] for u in users}))]:
        mine = sorted(set(user_groups or {}) & team_names)
        team = mine[0] if mine else ""  # as the spawner: the first team by name
        own = by_user.get(name) or {}
        resolved = resolve(own, (user_groups or {}).get(team), default, lambda _msg: None)
        row: Dict[str, Any] = {
            "username": name, "teams": mine, "known": user_groups is not None, "has_override": name in by_user,
            "terminals_off": bool(own.get("terminals_off")), "running": None,
        }
        for key in KEYS:
            value, source = resolved[key]
            row[key] = own.get(key) or ""
            row[f"{key}_now"] = value or "no limit"
            row[f"{key}_from"] = {"user": "set here", "team": f"team {team}", "default": "default", "none": ""}[source]
        u = live.get(name)
        if u:
            mem, mem_limit, cpu_limit = u.get("mem_mb") or 0, u.get("mem_limit_mb"), u.get("cpu_limit_pct")
            wanted_mem, wanted_cpu = resolved["memory_max"][0], resolved["cpu_quota"][0]
            row["running"] = {
                "memory": _fmt_mb(mem) + (f" of {_fmt_mb(mem_limit)}" if mem_limit else ""),
                "percent": min(round(mem / mem_limit * 100), 100) if mem_limit else None,
                "cpu": f"CPU limit {cpu_limit / 100:.2f} cores" if cpu_limit else "no CPU limit",
                # A user in several teams may be running under another team's limit: no warning for them.
                "old_limit": len(mine) < 2 and (
                    _differs(_mb(wanted_mem) if wanted_mem else None, mem_limit)
                    or _differs(int(wanted_cpu[:-1]) if wanted_cpu else None, cpu_limit)
                ),
            }
        rows.append(row)
    return {"teams": teams, "users": rows, "default": {k: default.get(k) or "no limit" for k in KEYS}}


class LimitsPageHandler(BaseHandler):
    """Admin-only ``/hub/limits``: every team and user with the limit that applies; set and clear them."""

    def initialize(
        self,
        store: Any,
        worker_ip_for_group: Callable[[str], Optional[str]] = lambda group: None,
        defaults: Callable[[], Optional[Dict[str, Any]]] = lambda: None,
        snapshots: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        self.store = store
        self._worker_ip_for_group = worker_ip_for_group
        self._defaults = defaults
        self._snapshots = snapshots if snapshots is not None else {}

    def _require_admin(self) -> None:
        if not self.current_user.admin:
            raise web.HTTPError(403, "Only admins can change limits.")

    # The three touch points with JupyterHub's own database (users, groups, group properties).
    def _hub_users(self) -> List[Tuple[str, Dict[str, Any]]]:
        from jupyterhub import orm

        return [(u.name, {g.name: g.properties for g in u.groups}) for u in self.db.query(orm.User).order_by(orm.User.name)]

    def _hub_groups(self) -> List[Tuple[str, Any, int]]:
        from jupyterhub import orm

        return [(g.name, g.properties, len(g.users)) for g in self.db.query(orm.Group).order_by(orm.Group.name)]

    def _save_team(self, team: str, values: Dict[str, Optional[str]]) -> bool:
        from jupyterhub import orm

        group = orm.Group.find(self.db, team)
        if group is None:
            return False
        props = {k: v for k, v in (group.properties or {}).items() if k not in KEYS}
        props.update({k: v for k, v in values.items() if v})
        group.properties = props  # a new dict: the column does not notice changes made in place
        self.db.commit()
        return True

    async def _render(self, error: str = "", message: str = "") -> None:
        view = build_view(
            self._hub_users(), self._hub_groups(), self.store.list_user_limits(),
            self._defaults(), self._worker_ip_for_group, self._snapshots,
        )
        self.finish(await self.render_template("limits.html", error=error, message=message, **view))

    @web.authenticated
    async def get(self) -> None:
        self._require_admin()
        await self._render()

    @web.authenticated
    async def post(self) -> None:
        self._require_admin()
        action = self.get_body_argument("action", "set")
        values = {k: self.get_body_argument(k, "").strip() or None for k in KEYS}
        errors = [e for k, v in values.items() if v is not None and (e := check(k, v))]
        if errors and action != "clear":
            self.set_status(400)
            return await self._render(error="; ".join(errors))

        if action == "team":
            team = self.get_body_argument("team", "").strip()
            if not self._worker_ip_for_group(team) or not self._save_team(team, values):
                self.set_status(400)
                return await self._render(error=f"{team or 'That'} is not a team with a worker.")
            return await self._render(message=f"Team {team}: saved. Applies on each member's next server start.")

        username = self.get_body_argument("username", "").strip()
        if not username:
            return await self._render(error="Enter a user name.")
        terminals_off = self.get_body_argument("terminals_off", "") == "on"  # an unticked checkbox sends nothing
        if action == "clear" or (not any(values.values()) and not terminals_off):
            self.store.delete_user_limits(username)
            return await self._render(message=f"{username}: back to team / default limits.")
        self.store.set_user_limits(username, values["memory_max"], values["cpu_quota"], terminals_off)
        await self._render(message=f"{username}: saved. Applies on their next server start.")
