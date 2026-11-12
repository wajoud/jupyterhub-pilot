"""The files no Python test imports: the setup scripts, the monitoring page and the Limits template."""

from __future__ import annotations

import glob
import os
import re
import subprocess

import pytest

from jupyterhub_pilot import limits, secrets_page

PKG = os.path.join(os.path.dirname(__file__), "..", "jupyterhub_pilot")
SCRIPTS = sorted(glob.glob(os.path.join(PKG, "scripts", "*.sh")))  # install_hub.sh, install_worker.sh, ...


@pytest.mark.parametrize("script", SCRIPTS, ids=os.path.basename)
def test_setup_script_is_valid_bash(script):
    done = subprocess.run(["bash", "-n", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_install_worker_installs_a_gpu_driver_only_where_a_gpu_is_found():
    with open(os.path.join(PKG, "scripts", "install_worker.sh")) as fh:
        text = fh.read()
    found = text.index("grep -qsi 0x10de /sys/bus/pci/devices/*/vendor")  # NVIDIA's PCI vendor id
    assert found < text.index("ubuntu-drivers install") < text.index("No NVIDIA GPU on this machine")


def test_monitoring_page_script_only_uses_ids_the_page_has():
    with open(os.path.join(PKG, "static", "monitoring.html")) as fh:
        html = fh.read()
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    used = set(re.findall(r"byId\('([^']+)'", html))
    used |= {i + suffix for i in re.findall(r"setTile\('([^']+)'", html) for suffix in ("", "-sub")}
    assert used and used <= ids, sorted(used - ids)


def test_limits_template_renders_what_build_view_gives_it():
    jinja2 = pytest.importorskip("jinja2")
    with open(os.path.join(PKG, "templates", "limits.html")) as fh:
        page = fh.read()
    env = jinja2.Environment(
        loader=jinja2.DictLoader({"page.html": "{% block main %}{% endblock %}", "limits.html": page}),
        autoescape=True,
        undefined=jinja2.StrictUndefined,  # a name the view does not provide is an error, not an empty string
    )
    view = limits.build_view(
        users=[("bob", {"team_alpha": {"memory_max": "3G"}}), ("<b>eve</b>", {})],
        groups=[("team_alpha", {"memory_max": "3G"}, 1)],
        overrides=[{"username": "bob", "memory_max": None, "cpu_quota": "300%", "terminals_off": 1}],
        default={"memory_max": "1G", "cpu_quota": "50%"},
        worker_ip_for_group={"team_alpha": "10.0.1.5"}.get,
        snapshots={"w1": {
            "ip": "10.0.1.5", "cpu": {"core_count": 4}, "memory": {"total_mb": 16384},
            "users": [{"name": "bob", "mem_mb": 512, "mem_limit_mb": 1024, "cpu_limit_pct": 50}],
        }},
    )
    html = env.get_template("limits.html").render(base_url="/hub/", xsrf_token="x", error="", message="", **view)
    assert "team team_alpha" in html and "512 MB of 1.0 GB" in html and "still on the old limit" in html
    assert "<b>eve</b>" not in html  # user names are escaped


def test_secrets_template_renders_names_and_never_trusts_them():
    jinja2 = pytest.importorskip("jinja2")
    pages = {"page.html": "{% block main %}{% endblock %}"}
    with open(os.path.join(PKG, "templates", "secrets.html")) as fh:
        pages["secrets.html"] = fh.read()
    env = jinja2.Environment(loader=jinja2.DictLoader(pages), autoescape=True, undefined=jinja2.StrictUndefined)
    view = secrets_page.build_view(
        [("bob", {"team_alpha": {}}), ("o'<b>eve</b>", {})], [("team_alpha", {}, 1)], {"team_alpha": "10.0.1.5"}.get,
        {"w": {"users": [{"name": "bob", "uptime_s": 9999}]}},
        {"bob": (["API_KEY"], "2026-11-12T10:00:00Z"), "o'<b>eve</b>": (["x'y"], "")}, {"team_alpha": (["REGION"], "")},
        secrets_page._epoch("2026-11-12T10:00:00Z") + 60,
    )
    render = env.get_template("secrets.html").render
    html = render(base_url="/hub/", xsrf_token="x", error="", message="", off=False, **view)
    assert "API_KEY" in html and "REGION" in html and "restart needed" in html
    assert "<b>eve</b>" not in html and "x'y" not in html  # names are escaped, also inside attributes
    assert 'type="password"' in html and "Vault is off" not in html
    assert "Vault is off" in render(base_url="/hub/", xsrf_token="x", error="", message="", off=True, teams=[], users=[])
