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
  - Raw message bytes are forwarded to browser clients without JSON
    parse + re-serialise. Only a thin envelope wrapper is built once.
  - Dead client cleanup uses discard() inline instead of building a
    temporary set and calling difference_update().
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from typing import Any, Callable, Dict, Optional, Set, Union

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
        hostname = snapshot.get("hostname") or self.request.remote_ip
        self.hostname = hostname
        _WORKER_SNAPSHOTS[hostname] = snapshot

        # PERF FIX: build the broadcast envelope *once* (not per client).
        envelope = json.dumps(
            {
                "type": "snapshot",
                "worker": hostname,
                "data": snapshot,
            }
        )

        _broadcast(snapshot, envelope)

    def on_close(self) -> None:
        log.info(
            "MonitoringHandler: Worker agent disconnected (%s)", self.request.remote_ip
        )
        # Drop the worker so the dashboard does not show stale numbers forever.
        snapshot = _WORKER_SNAPSHOTS.pop(getattr(self, "hostname", None) or "", None)
        if snapshot is not None:
            _broadcast(snapshot, json.dumps({"type": "gone", "worker": self.hostname}))


def _broadcast(snapshot: Dict[str, Any], envelope: str) -> None:
    """Send *envelope* to every browser allowed to see *snapshot*'s worker."""
    for client in list(_BROWSER_CLIENTS):
        if not client.can_see(snapshot):
            continue
        try:
            client.write_message(envelope)
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

    @web.authenticated
    async def get(self, *args: Any, **kwargs: Any) -> None:
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
        visible = {h: s for h, s in _WORKER_SNAPSHOTS.items() if self.can_see(s)}
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
