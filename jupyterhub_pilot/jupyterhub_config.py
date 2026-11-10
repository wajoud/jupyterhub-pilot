# ===============================
# JupyterHub Secure Configuration
# ===============================

# Ships in the package; run with `jupyterhub -f <this file>`. This machine's
# settings come from CONFIG_DIR (default /opt/jupyterhub-pilot).

import json
import os
import sys

from oauthenticator.google import GoogleOAuthenticator

from jupyterhub_pilot.limits import LimitsPageHandler
from jupyterhub_pilot import monitoring_handler
from jupyterhub_pilot.monitoring_handler import (
    AgentWebSocketHandler,
    BrowserWebSocketHandler,
    MonitoringPageHandler,
    PrometheusHandler,
    UsageHistoryHandler,
)
from jupyterhub_pilot.paths import CONFIG_DIR, PACKAGE_DIR
from jupyterhub_pilot.spawner import _store, CustomSpawner, worker_ip_for_group
from jupyterhub_pilot.vault_client import start_renewer

with open(os.path.join(CONFIG_DIR, "hub_settings.json"), "r") as f:
    hub_settings = json.load(f)

# -------------------------------------------------
# Base Config
# -------------------------------------------------
c = get_config()  # type: ignore[name-defined]  # noqa: F821  (JupyterHub injects it)

c.Application.log_level = "INFO"

# -------------------------------------------------
# Hub Network Settings
# -------------------------------------------------
c.JupyterHub.ip = hub_settings["hub_ip"]
c.JupyterHub.hub_connect_ip = hub_settings["hub_ip"]
c.JupyterHub.port = hub_settings["hub_port"]
c.JupyterHub.hub_bind_url = hub_settings["hub_bind_url"]

# Monitoring link in the nav bar: templates/page.html extends JupyterHub's own page.
c.JupyterHub.template_paths = [os.path.join(PACKAGE_DIR, "templates")]

c.ConfigurableHTTPProxy.api_url = hub_settings["proxy_api_url"]

# -------------------------------------------------
# Authentication & Authorization
# -------------------------------------------------
if os.getenv("JUPYTERPILOT_TEST_PASSWORD"):
    # ⚠️ TEST ONLY (no Google client or domain needed): any user name logs in with
    # this password; admin_users log in with JUPYTERPILOT_TEST_ADMIN_PASSWORD (32+ chars).
    c.JupyterHub.authenticator_class = "shared-password"
    c.SharedPasswordAuthenticator.user_password = os.environ["JUPYTERPILOT_TEST_PASSWORD"]
    c.SharedPasswordAuthenticator.admin_password = os.getenv("JUPYTERPILOT_TEST_ADMIN_PASSWORD", "")
else:
    c.JupyterHub.authenticator_class = GoogleOAuthenticator

    c.GoogleOAuthenticator.client_id = os.getenv("CLIENT_ID")
    c.GoogleOAuthenticator.client_secret = os.getenv("CLIENT_SECRET")
    c.GoogleOAuthenticator.oauth_callback_url = os.getenv("OAUTH_CALLBACK_URL")

    # 🔐 CRITICAL: Strictly enforce hosted domain from config
    c.GoogleOAuthenticator.hosted_domain = hub_settings["hosted_domain"]

# allow_all=True at the Authenticator level allows anyone from the hosted_domain
c.Authenticator.allow_all = True

# Admin users from config
c.Authenticator.admin_users = set(hub_settings["admin_users"])

# 🔐 CRITICAL: prevent admin jumping into user servers unless explicitly needed
c.JupyterHub.admin_access = False

# One user → one server
c.JupyterHub.allow_named_servers = False

# -------------------------------------------------
# Spawner
# -------------------------------------------------
c.JupyterHub.spawner_class = CustomSpawner
c.Spawner.default_url = "/tree"

if hub_settings.get("vault_enabled"):
    start_renewer()  # keeps the Hub's periodic Vault token alive

# -------------------------------------------------
# Idle culling: stop servers nobody used for `timeout` seconds (frees worker RAM)
# -------------------------------------------------
# Missing or null in hub_settings.json = off. Files stay; only in-memory state is lost.
idle_cull = hub_settings.get("idle_cull")
if idle_cull:
    c.JupyterHub.services = [
        {
            "name": "idle-culler",
            "command": [
                sys.executable,
                "-m",
                "jupyterhub_idle_culler",
                f"--timeout={int(idle_cull['timeout'])}",
                f"--cull-every={int(idle_cull.get('every', 300))}",
            ],
        }
    ]
    # Stop servers only; never --cull-users (that would delete Hub users).
    c.JupyterHub.load_roles = [
        {
            "name": "idle-culler",
            "scopes": ["list:users", "read:users:activity", "read:servers", "delete:servers"],
            "services": ["idle-culler"],
        }
    ]

# -------------------------------------------------
# Cookie & Security Settings
# -------------------------------------------------
# JupyterHub reads these from tornado_settings; a top-level JupyterHub.cookie_options
# is not a real option and was silently ignored.
c.JupyterHub.tornado_settings = {
    "cookie_options": {
        "httponly": True,
        "secure": True,
        "samesite": "Lax",
    }
}

# Register monitoring endpoints.  Cross-user access to /user/<name> is already
# blocked by JupyterHub's own access scopes (admin_access = False above).
monitoring_handler.USAGE_STORE = _store  # 15 days of per-user usage in the state DB

c.JupyterHub.extra_handlers = [
    # Task 4: Monitoring — Agent WebSocket (Worker VMs connect here)
    (r"/monitoring/ws", AgentWebSocketHandler),
    # Task 4: Monitoring — Browser WebSocket (Dashboard live updates)
    # (each user only sees the workers of their own groups; admins see all)
    (r"/monitoring/browser", BrowserWebSocketHandler, {"worker_ip_for_group": worker_ip_for_group}),
    # Task 4: Monitoring — Dashboard page (all authenticated users)
    (r"/monitoring", MonitoringPageHandler),
    # Per-user usage of the last 15 days (the graphs on the dashboard)
    (r"/monitoring/history", UsageHistoryHandler),
    # Prometheus text format for Grafana etc.; off until JUPYTERPILOT_METRICS_TOKEN is set
    (r"/monitoring/metrics", PrometheusHandler),
    # Per-user RAM/CPU limits (admins only; team limits live in group properties)
    (r"/limits", LimitsPageHandler, {"store": _store}),
]
