"""
jupyterhub_pilot/monitoring_handler.py
────────────────────────────────────
Tornado handlers for the JupyterHub-Pilot live monitoring dashboard.

Two handlers are registered:

AgentWebSocketHandler  (``/monitoring/ws``)
    Accepts WebSocket connections from Worker VM metrics agents.
    Stores the latest snapshot per Worker hostname in the shared
    ``_WORKER_SNAPSHOTS`` dict and broadcasts updates to all connected
    Admin browser clients.

MonitoringPageHandler  (``/monitoring``) and BrowserWebSocketHandler
    Serve the dashboard and its live feed to logged-in JupyterHub users only.
    Admins see every worker; other users only the workers of their groups.

Authentication
──────────────
  - Browser endpoints require a JupyterHub login (cookie).
  - Agents must send ``Authorization: Bearer <JUPYTERPILOT_AGENT_TOKEN>``.
    The Hub refuses every agent while that env var is unset.

Usage (wire in jupyterhub_config.py)
──────────────────────────────────────
    from jupyterhub_pilot.monitoring_handler import (
        AgentWebSocketHandler,
        MonitoringPageHandler,
        BrowserWebSocketHandler,
    )

    c.JupyterHub.extra_handlers = [
        (r"/monitoring/ws",      AgentWebSocketHandler),
        (r"/monitoring/browser", BrowserWebSocketHandler,
         {"worker_ip_for_group": worker_ip_for_group}),  # group name -> worker IP
        (r"/monitoring",         MonitoringPageHandler),
    ]

Performance notes (PERF FIX):
  - Each update is JSON-encoded once for all admins; non-admin browsers get
    a per-user copy that lists only their own usage row.
  - Dead client cleanup uses discard() inline instead of building a
    temporary set and calling difference_update().
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from jupyterhub.handlers import BaseHandler
from tornado import web
from tornado.websocket import WebSocketHandler

log = logging.getLogger("jupyterhub")

# ---------------------------------------------------------------------------
# Shared in-process state
# ---------------------------------------------------------------------------

# Latest snapshot per Worker, keyed by hostname.  snapshot["ip"] is the IP the
# Hub sees for the agent's connection (used to filter what users can see).
# { "ip-172-31-27-155": { ...metrics snapshot... } }
_WORKER_SNAPSHOTS: Dict[str, Dict[str, Any]] = {}

# All active browser WebSocket clients waiting for live updates.
_BROWSER_CLIENTS: Set["BrowserWebSocketHandler"] = set()


# Usage history: jupyterhub_config.py sets this to the SessionStore; None = history off.
USAGE_STORE: Any = None
_BUCKET_S = 300
# Running sums for the current 5 minutes:
# (username, worker) -> [cpu_sum, mem_sum, mem_peak, samples, cpu_limit_pct, mem_limit_mb]
_USAGE_ACC: Dict[Tuple[str, str], List[Any]] = {}
_USAGE_BUCKET: int = 0


def _num(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def record_usage(hostname: str, snapshot: Dict[str, Any], now: Optional[float] = None) -> None:
    """Add *snapshot*'s user rows to the 5-minute averages; store them when the 5 minutes end."""
    global _USAGE_BUCKET
    if USAGE_STORE is None:
        return
    now = time.time() if now is None else now
    bucket = int(now) // _BUCKET_S * _BUCKET_S
    if bucket != _USAGE_BUCKET:
        rows = [
            (user, _USAGE_BUCKET, worker, a[0] / a[3], a[1] / a[3], a[2], a[4], a[5])
            for (user, worker), a in _USAGE_ACC.items()
        ]
        _USAGE_ACC.clear()
        _USAGE_BUCKET = bucket
        if rows:
            try:
                USAGE_STORE.add_usage(rows, int(now))
            except Exception as exc:  # noqa: BLE001  (history must never break live monitoring)
                log.warning("MonitoringHandler: could not store usage history: %s", exc)
    users = snapshot.get("users")
    for u in users if isinstance(users, list) else []:
        if not isinstance(u, dict) or not u.get("name"):
            continue
        a = _USAGE_ACC.setdefault((str(u["name"]), hostname), [0.0, 0.0, 0.0, 0, None, None])
        mem = _num(u.get("mem_mb"))
        a[0] += _num(u.get("cpu_cores"))
        a[1] += mem
        a[2] = max(a[2], mem)
        a[3] += 1
        a[4], a[5] = u.get("cpu_limit_pct"), u.get("mem_limit_mb")


# ---------------------------------------------------------------------------
# Agent WebSocket handler  (Worker VM → Hub)
# ---------------------------------------------------------------------------


class AgentWebSocketHandler(WebSocketHandler):
    """
    Accepts WebSocket connections from ``metrics_agent.py`` running on each
    Worker VM.

    On each message:
      1. Parses the JSON metrics snapshot.
      2. Stores it in ``_WORKER_SNAPSHOTS`` keyed by hostname.
      3. Builds a single envelope JSON string and broadcasts it to all
         connected browser clients (no per-client re-serialisation).
    """

    def check_origin(self, origin: str) -> bool:
        # Agents are not browsers; they authenticate with the token below.
        return True

    def prepare(self) -> None:
        expected = os.environ.get("JUPYTERPILOT_AGENT_TOKEN", "")
        got = self.request.headers.get("Authorization", "")
        if not expected or not hmac.compare_digest(got, f"Bearer {expected}"):
            log.warning(
                "MonitoringHandler: rejected agent from %s (bad or missing token)",
                self.request.remote_ip,
            )
            raise web.HTTPError(403)

    def open(self, *args: str, **kwargs: str) -> None:
        self.hostname: Optional[str] = None
        log.info(
            "MonitoringHandler: Worker agent connected from %s", self.request.remote_ip
        )

    def on_message(self, message: Union[str, bytes]) -> None:
        try:
            snapshot = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            snapshot = None
        if not isinstance(snapshot, dict):
            log.warning("MonitoringHandler: received malformed JSON from agent.")
            return

        # The agent's own "ip" can be 127.0.1.1; the connection's address is
        # the worker's private IP, which is what the team mapping holds.
        # (remote_ip honours the proxy's X-Forwarded-For: the Hub runs with xheaders.)
        snapshot["ip"] = self.request.remote_ip
        # Team names for the card title; the agent only knows its hostname.
        snapshot["teams"] = USAGE_STORE.teams_for_ip(snapshot["ip"]) if USAGE_STORE else []
        hostname = str(snapshot.get("hostname") or self.request.remote_ip)
        self.hostname = hostname
        _WORKER_SNAPSHOTS[hostname] = snapshot
        record_usage(hostname, snapshot)

        _broadcast(snapshot, {"type": "snapshot", "worker": hostname, "data": snapshot})

    def on_close(self) -> None:
        log.info(
            "MonitoringHandler: Worker agent disconnected (%s)", self.request.remote_ip
        )
        # Drop the worker so the dashboard does not show stale numbers forever.
        snapshot = _WORKER_SNAPSHOTS.pop(getattr(self, "hostname", None) or "", None)
        if snapshot is not None:
            _broadcast(snapshot, {"type": "gone", "worker": self.hostname})


def _broadcast(snapshot: Dict[str, Any], message: Dict[str, Any]) -> None:
    """Send *message* to every browser allowed to see *snapshot*'s worker.

    Encoded once for admins; a message carrying the snapshot (``"data"``) is
    re-encoded per non-admin so it lists only that user's own usage.
    """
    full = json.dumps(message)
    for client in list(_BROWSER_CLIENTS):
        if not client.can_see(snapshot):
            continue
        try:
            if "data" in message and client._allowed_ips is not None:
                client.write_message(json.dumps({**message, "data": client.view(snapshot)}))
            else:
                client.write_message(full)
        except Exception:  # noqa: BLE001
            _BROWSER_CLIENTS.discard(client)


# ---------------------------------------------------------------------------
# Browser WebSocket handler  (Hub → Browser)
# ---------------------------------------------------------------------------


class BrowserWebSocketHandler(BaseHandler, WebSocketHandler):
    """
    Accepts WebSocket connections from logged-in browser clients.

    Tornado's default ``check_origin`` (same origin only) stays in place so
    another site cannot open this socket with the user's cookie.

    Admins see every worker.  Other users see only the workers mapped to
    their JupyterHub groups (``worker_ip_for_group``); if the lookup fails
    they see nothing.

    On connection:
      1. Works out which worker IPs this user may see.
      2. Registers the client in ``_BROWSER_CLIENTS``.
      3. Immediately pushes the visible snapshots as an "init" message.

    On close: removes the client from the set.
    """

    def initialize(
        self,
        worker_ip_for_group: Callable[[str], Optional[str]] = lambda group: None,
    ) -> None:
        self._worker_ip_for_group = worker_ip_for_group
        self._allowed_ips: Optional[Set[str]] = set()  # None = all (admin)

    def can_see(self, snapshot: Dict[str, Any]) -> bool:
        return self._allowed_ips is None or snapshot.get("ip") in self._allowed_ips

    def view(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """*snapshot* as this browser may see it: non-admins see only their own user row and failed jobs."""
        if self._allowed_ips is None:
            return snapshot
        me = self.current_user.name
        linux_me = me.split("@", 1)[0].lower()  # same mapping as the spawner's _os_username()
        return {
            **snapshot,
            "users": [u for u in snapshot.get("users", []) if u.get("name") == me],
            "failed_jobs": [j for j in snapshot.get("failed_jobs", []) if j.get("user") == linux_me],
        }

    @web.authenticated
    async def get(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]  # web.authenticated's typing
        await WebSocketHandler.get(self, *args, **kwargs)

    def open(self, *args: str, **kwargs: str) -> None:
        user = self.current_user
        if user.admin:
            self._allowed_ips = None
        else:
            ips = (self._worker_ip_for_group(g.name) for g in user.groups)
            self._allowed_ips = {ip for ip in ips if ip}
        _BROWSER_CLIENTS.add(self)
        log.debug("MonitoringHandler: browser client connected.")
        # Always send init (even empty) so the page can show "no workers".
        visible = {h: self.view(s) for h, s in _WORKER_SNAPSHOTS.items() if self.can_see(s)}
        try:
            self.write_message(json.dumps({"type": "init", "workers": visible}))
        except Exception:  # noqa: BLE001
            pass

    def on_message(self, message: Union[str, bytes]) -> None:
        # Browser sends no messages; ignore gracefully
        pass

    def on_close(self) -> None:
        _BROWSER_CLIENTS.discard(self)
        log.debug("MonitoringHandler: browser client disconnected.")


# ---------------------------------------------------------------------------
# HTTP page handler  (serves the dashboard HTML)
# ---------------------------------------------------------------------------

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# PERF FIX: cache the HTML content in memory after first read so subsequent
# page loads do not hit the filesystem at all.
_DASHBOARD_HTML: str = ""


class MonitoringPageHandler(BaseHandler):
    """
    Serves the monitoring dashboard HTML at ``GET /monitoring``.

    Accessible to all logged-in JupyterHub users; the live feed
    (``BrowserWebSocketHandler``) decides which workers each user sees.
    """

    @web.authenticated
    def get(self) -> None:
        global _DASHBOARD_HTML
        if not _DASHBOARD_HTML:
            html_path = os.path.join(_STATIC_DIR, "monitoring.html")
            try:
                with open(html_path, "r", encoding="utf-8") as fh:
                    _DASHBOARD_HTML = fh.read()
            except FileNotFoundError:
                self.set_status(500)
                self.write(
                    "Monitoring dashboard HTML not found. "
                    "Ensure jupyterhub_pilot/static/monitoring.html exists."
                )
                return
        self.set_header("Content-Type", "text/html; charset=utf-8")
        self.write(_DASHBOARD_HTML)


# ---------------------------------------------------------------------------
# Usage history  (the per-user graphs)
# ---------------------------------------------------------------------------


class UsageHistoryHandler(BaseHandler):
    """``GET /monitoring/history?user=<name>&days=<1-15>`` (or ``&hours=1``): a user's stored usage as JSON.

    Users may read only their own history; admins anyone's, plus the list of users
    and the most active users of the same period (``top``).
    """

    @web.authenticated
    def get(self) -> None:
        me = self.current_user
        user = self.get_argument("user", "") or me.name
        if user != me.name and not me.admin:
            raise web.HTTPError(403, "You can only see your own usage.")
        try:
            days = min(max(int(self.get_argument("days", "1")), 1), 15)
        except ValueError:
            days = 1
        span = days * 86400
        if self.get_argument("hours", "") == "1":  # the one range shorter than a day
            span = 3600
        # About 300-700 points whatever the range: 5-minute rows for a day, 30-minute averages for 15 days.
        step = max(_BUCKET_S, span // 700 // _BUCKET_S * _BUCKET_S)
        store = USAGE_STORE
        since = int(time.time()) - span
        points = store.get_usage(user, since, step) if store else []
        users = store.usage_users() if store and me.admin else []
        top = store.top_usage(since) if store and me.admin else []
        self.set_header("Content-Type", "application/json")
        self.write(json.dumps({
            "user": user, "days": days, "span_s": span, "step_s": step, "points": points, "users": users, "top": top,
        }))


# ---------------------------------------------------------------------------
# Prometheus endpoint  (Grafana and other tools)
# ---------------------------------------------------------------------------


def _label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def prometheus_text(snapshots: Dict[str, Dict[str, Any]]) -> str:
    """The latest snapshot of every worker in the Prometheus text format."""
    metrics: Dict[str, List[str]] = {}

    def add(name: str, labels: Dict[str, Any], value: Any) -> None:
        if isinstance(value, (int, float)):
            lab = ",".join(f'{k}="{_label(v)}"' for k, v in labels.items())
            metrics.setdefault(name, []).append(f"jupyterhub_pilot_{name}{{{lab}}} {value}")

    def part(d: Dict[str, Any], key: str) -> Dict[str, Any]:
        value = d.get(key)
        return value if isinstance(value, dict) else {}

    for host, s in snapshots.items():
        w = {"worker": host}
        add("worker_cpu_percent", w, part(s, "cpu").get("percent"))
        add("worker_memory_used_mb", w, part(s, "memory").get("used_mb"))
        add("worker_memory_total_mb", w, part(s, "memory").get("total_mb"))
        add("worker_disk_percent", w, part(s, "disk").get("percent"))
        gpus = s.get("gpus")
        for g in gpus if isinstance(gpus, list) else []:
            if not isinstance(g, dict):
                continue
            wg = {**w, "gpu": g.get("index", ""), "model": g.get("name", "")}
            add("worker_gpu_percent", wg, g.get("percent"))
            add("worker_gpu_memory_used_mb", wg, g.get("mem_used_mb"))
            add("worker_gpu_memory_total_mb", wg, g.get("mem_total_mb"))
        users = s.get("users")
        for u in users if isinstance(users, list) else []:
            if not isinstance(u, dict):
                continue
            wu = {**w, "user": u.get("name", "")}
            add("user_cpu_cores", wu, u.get("cpu_cores"))
            add("user_cpu_limit_cores", wu, u["cpu_limit_pct"] / 100 if isinstance(u.get("cpu_limit_pct"), (int, float)) else None)
            add("user_memory_mb", wu, u.get("mem_mb"))
            add("user_memory_limit_mb", wu, u.get("mem_limit_mb"))
            add("user_kernels", wu, u.get("kernels"))
            add("user_gpu_memory_mb", wu, u.get("gpu_mem_mb"))
    lines = []
    for name, samples in metrics.items():
        lines.append(f"# TYPE jupyterhub_pilot_{name} gauge")
        lines.extend(samples)
    return "\n".join(lines) + "\n"


class PrometheusHandler(web.RequestHandler):
    """``GET /monitoring/metrics`` for Prometheus/Grafana: ``Authorization: Bearer <JUPYTERPILOT_METRICS_TOKEN>``.

    Off (403 for everyone) while that variable is unset. Its own token, so a
    scraper cannot post as a worker agent.
    """

    def get(self) -> None:
        expected = os.environ.get("JUPYTERPILOT_METRICS_TOKEN", "")
        got = self.request.headers.get("Authorization", "")
        if not expected or not hmac.compare_digest(got, f"Bearer {expected}"):
            raise web.HTTPError(403)
        self.set_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.write(prometheus_text(_WORKER_SNAPSHOTS))
