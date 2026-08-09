# ===============================
# JupyterHub Secure Configuration
# ===============================

import json
import logging
import os
import shutil
import sys

from jupyterhub.handlers import BaseHandler
from oauthenticator.google import GoogleOAuthenticator

# Load generalized settings
config_path = os.path.join(os.path.dirname(__file__), "hub_settings.json")
with open(config_path, "r") as f:
    hub_settings = json.load(f)

# Add custom spawner path dynamically
sys.path.append(os.path.dirname(__file__))
from jupyterhub_pilot.monitoring_handler import (
    AgentWebSocketHandler,
    BrowserWebSocketHandler,
    MonitoringPageHandler,
)
from spawner import CustomSpawner

# -------------------------------------------------
# Base Config
# -------------------------------------------------
c = get_config()  # noqa: F821

c.Application.log_level = "INFO"

# -------------------------------------------------
# Hub Network Settings
# -------------------------------------------------
c.JupyterHub.ip = hub_settings["hub_ip"]
c.JupyterHub.hub_connect_ip = hub_settings["hub_ip"]
c.JupyterHub.port = hub_settings["hub_port"]
c.JupyterHub.hub_bind_url = hub_settings["hub_bind_url"]

# ── Custom nav: inject Monitoring link into JupyterHub's page.html ──────────────────
# Strategy: copy the real system page.html into our templates dir and
# inject a small JS snippet that appends the nav link after page load.
# Copying (not extending) avoids Jinja2's infinite recursion bug.
try:
    import jupyterhub as _jh

    _jh_templates = os.path.join(os.path.dirname(_jh.__file__), "templates")
    _custom_templates = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "templates"
    )
    _src = os.path.join(_jh_templates, "page.html")
    _dst = os.path.join(_custom_templates, "page.html")

    os.makedirs(_custom_templates, exist_ok=True)
    shutil.copy2(_src, _dst)  # always sync with installed JupyterHub version

    _monitoring_js = (
        "\n<!-- JupyterHub-Pilot: Monitoring nav link -->\n"
        "<script>\n"
        "document.addEventListener('DOMContentLoaded', function () {\n"
        "  var nav = document.querySelector('ul.navbar-nav');\n"
        "  if (nav && !document.getElementById('jp-monitoring-link')) {\n"
        "    var li = document.createElement('li');\n"
        "    li.className = 'nav-item';\n"
        "    var a  = document.createElement('a');\n"
        "    a.id        = 'jp-monitoring-link';\n"
        "    a.className = 'nav-link';\n"
        "    a.href      = '/monitoring';\n"
        "    a.title     = 'Live VM Monitoring';\n"
        "    a.textContent = '\U0001f4ca Monitoring';\n"
        "    li.appendChild(a);\n"
        "    nav.appendChild(li);\n"
        "  }\n"
        "});\n"
        "</script>\n"
    )

    with open(_dst) as _f:
        _page_content = _f.read()

    if "jp-monitoring-link" not in _page_content:
        # Inject before </body> — works regardless of which Jinja blocks are used
        if "</body>" in _page_content:
            _page_content = _page_content.replace(
                "</body>", _monitoring_js + "</body>", 1
            )
        else:
            _page_content += _monitoring_js
        with open(_dst, "w") as _f:
            _f.write(_page_content)

    c.JupyterHub.template_paths = [_custom_templates]
    logging.getLogger("jupyterhub").info(
        "JupyterHub-Pilot: Monitoring nav link injected into %s", _dst
    )
except Exception as _exc:
    logging.getLogger("jupyterhub").warning(
        "JupyterHub-Pilot: Could not inject Monitoring nav link: %s", _exc
    )

c.ConfigurableHTTPProxy.api_url = hub_settings["proxy_api_url"]

# -------------------------------------------------
# Authentication & Authorization
# -------------------------------------------------
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

# -------------------------------------------------
# Cookie & Security Settings
# -------------------------------------------------
c.JupyterHub.cookie_options = {
    "httponly": True,
    "secure": True,
    "samesite": "Lax",
}

# -------------------------------------------------
# Extra Security: Hard Block /user/otheruser Access
# -------------------------------------------------


class BlockOtherUsersHandler(BaseHandler):
    async def prepare(self):
        user = self.current_user
        if not user:
            self.redirect("/hub/login")
            return

        path = self.request.path
        if path.startswith("/user/"):
            try:
                target_user = path.split("/")[2]
            except IndexError:
                return

            if target_user != user.name:
                self.set_status(403)
                self.finish("🚫 Access denied: You cannot access another user's server")


# Register all handlers: monitoring endpoints + cross-user blocker
c.JupyterHub.extra_handlers = [
    # Task 4: Monitoring — Agent WebSocket (Worker VMs connect here)
    (r"/monitoring/ws", AgentWebSocketHandler),
    # Task 4: Monitoring — Browser WebSocket (Dashboard live updates)
    (r"/monitoring/browser", BrowserWebSocketHandler),
    # Task 4: Monitoring — Dashboard page (all authenticated users)
    (r"/monitoring", MonitoringPageHandler),
    # Security: block cross-user /user/<other> access
    (r"/user/.*", BlockOtherUsersHandler),
]
