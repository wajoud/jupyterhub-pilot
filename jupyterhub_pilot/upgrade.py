"""`sudo jupyterhub-pilot upgrade`: new package + only the changes a release needs.

Run it on the Hub and on every worker. It never re-runs the install and never
touches keys, tokens, passwords, teams or users' files.

  1. pip upgrades the package (skip with --no-pip).
  2. The new code runs every step newer than the version recorded in
     /etc/jupyterhub-pilot/version. Each step is safe to run twice.
  3. Restarts what changed: the metrics agent on a worker; the Hub only after
     you confirm (or --yes), because a Hub restart stops every user's server.
  On the Hub it also checks the workers' versions over SSH and warns if one is behind.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Callable, Dict, List, Optional, Tuple

ETC_DIR = "/etc/jupyterhub-pilot"
VERSION_FILE = os.path.join(ETC_DIR, "version")
HUB_UNIT = "/etc/systemd/system/jupyterhub.service"
METRICS_UNIT = "/etc/systemd/system/jupyterhub_pilot-metrics.service"
PIP_EXTRAS = {"hub": "hub", "worker": "worker,ai"}

# hub_settings.json keys added after 0.5.1, with their defaults. Existing values are kept.
NEW_SETTINGS = {
    "on_user_delete": "archive",
    "idle_cull": {"timeout": 10800, "every": 300},
}


def parse_version(v: str) -> Tuple[int, ...]:
    """'0.5.1' -> (0, 5, 1); anything unparsable (no file, '0+unknown') -> (0,)."""
    parts: List[int] = []
    for p in v.strip().split("+")[0].split("."):
        if not p.isdigit():
            break
        parts.append(int(p))
    return tuple(parts) or (0,)


def detect_role() -> Optional[str]:
    if os.path.exists(HUB_UNIT):
        return "hub"
    if os.path.exists(METRICS_UNIT):
        return "worker"
    return None


# ── Steps: (release that needs it, role, function). Each returns what it did, or "". ──


def add_settings_defaults(config_dir: str) -> str:
    path = os.path.join(config_dir, "hub_settings.json")
    with open(path) as f:
        settings = json.load(f)
    added = [k for k in NEW_SETTINGS if k not in settings]
    if not added:
        return ""
    for k in added:
        settings[k] = NEW_SETTINGS[k]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)  # atomic: a crash never leaves a half-written settings file
    return f"hub_settings.json: added {', '.join(f'{k}={json.dumps(NEW_SETTINGS[k])}' for k in added)}"


def fix_metrics_url(_config_dir: str) -> str:
    """0.4.0 moved the agent URL under /hub/ (old one cost a 302 per connect)."""
    with open(METRICS_UNIT) as f:
        unit = f.read()
    if ":8000/monitoring/ws" not in unit:
        return ""
    with open(METRICS_UNIT, "w") as f:
        f.write(unit.replace(":8000/monitoring/ws", ":8000/hub/monitoring/ws"))
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    return "metrics agent URL moved to /hub/monitoring/ws"


STEPS: List[Tuple[str, str, Callable[[str], str]]] = [
    ("0.4.0", "worker", fix_metrics_url),
    ("0.6.0", "hub", add_settings_defaults),
]


def run_steps(role: str, from_version: str, config_dir: str) -> List[str]:
    done = []
    for release, step_role, step in STEPS:
        if step_role == role and parse_version(from_version) < parse_version(release):
            msg = step(config_dir)
            if msg:
                done.append(msg)
    return done


def read_version() -> str:
    try:
        with open(VERSION_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return "0"  # installed before 0.6.0: run every step


def write_version(v: str) -> None:
    os.makedirs(ETC_DIR, exist_ok=True)
    with open(VERSION_FILE, "w") as f:
        f.write(v + "\n")


# ── Hub: are the workers on the same version? ──────────────────────────────────

_REMOTE_VERSION = "python3 -c \"import importlib.metadata as m; print(m.version('jupyterhub-pilot'))\""


def worker_versions(config_dir: str) -> Dict[str, str]:
    """{team: version or error} for every team in the SQLite mapping."""
    import sqlite3

    with open(os.path.join(config_dir, "hub_settings.json")) as f:
        db = json.load(f).get("db_path", "/var/lib/jupyterhub/jupyterhub_pilot_state.db")
    rows = sqlite3.connect(db).execute("SELECT team, server_ip, ssh_key, admin_ssh_user FROM team_mappings").fetchall()
    out = {}
    for team, ip, key, user in rows:
        if not user:
            out[team] = "unknown (no admin_ssh_user)"
            continue
        r = subprocess.run(
            ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             "-o", "StrictHostKeyChecking=accept-new", f"{user}@{ip}", _REMOTE_VERSION],
            capture_output=True, text=True,
        )
        out[team] = r.stdout.strip() if r.returncode == 0 else f"unreachable ({ip})"
    return out


def migrate(yes: bool) -> int:
    """Runs with the new code (after pip): apply steps, record version, restart."""
    from jupyterhub_pilot import __version__
    from jupyterhub_pilot.paths import CONFIG_DIR

    role = detect_role()
    if role is None:
        print("❌ Neither a Hub nor a worker install found here. Run install-hub or install-worker first.")
        return 1
    old = read_version()
    print(f"{role}: {old if old != '0' else 'before 0.6.0'} → {__version__}")
    for msg in run_steps(role, old, CONFIG_DIR):
        print(f"  ✓ {msg}")
    write_version(__version__)

    if role == "worker":
        subprocess.run(["systemctl", "restart", "jupyterhub_pilot-metrics"], check=True)
        print("  ✓ metrics agent restarted. Running notebook servers keep the old code until their next start.")
        return 0

    behind = {t: v for t, v in worker_versions(CONFIG_DIR).items() if v != __version__}
    for team, v in behind.items():
        print(f"  ⚠️  worker for {team}: {v}. Run `sudo jupyterhub-pilot upgrade` there too.")
    if not yes:
        answer = input("Restart the Hub now? This stops every user's running server. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Not restarted. Run `sudo systemctl restart jupyterhub` when it suits you.")
            return 0
    subprocess.run(["systemctl", "restart", "jupyterhub"], check=True)
    print("  ✓ Hub restarted.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="jupyterhub-pilot upgrade", description=__doc__.split("\n")[0])
    ap.add_argument("--yes", action="store_true", help="restart the Hub without asking")
    ap.add_argument("--no-pip", action="store_true", help="skip pip (package already upgraded)")
    ap.add_argument("--migrate", action="store_true", help=argparse.SUPPRESS)  # internal: second stage
    args = ap.parse_args(argv)
    if os.geteuid() != 0:
        print("❌ Run with sudo: sudo jupyterhub-pilot upgrade")
        return 1
    if args.migrate:
        return migrate(args.yes)
    role = detect_role()
    if role is None:
        print("❌ Neither a Hub nor a worker install found here. Run install-hub or install-worker first.")
        return 1
    if not args.no_pip:
        pip = [sys.executable, "-m", "pip", "install", "--break-system-packages", "--ignore-installed",
               "-U", f"jupyterhub-pilot[{PIP_EXTRAS[role]}]"]
        if subprocess.run(pip).returncode != 0:
            print("❌ pip failed; nothing else was changed.")
            return 1
    # Fresh interpreter, so the steps come from the version pip just installed.
    second = [sys.executable, "-m", "jupyterhub_pilot.upgrade", "--migrate"] + (["--yes"] if args.yes else [])
    return subprocess.run(second).returncode


if __name__ == "__main__":
    sys.exit(main())
