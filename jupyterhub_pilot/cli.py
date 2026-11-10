"""`jupyterhub-pilot` command: set up a Hub or worker from the pip package (no git clone).

    sudo jupyterhub-pilot install-hub
    sudo jupyterhub-pilot add-worker <team> <worker-private-ip> [admin-ssh-user]
    sudo jupyterhub-pilot install-worker
    sudo jupyterhub-pilot mount-homes <efs-dns-name> [mount-point]   (on a worker: shared homes)
    sudo jupyterhub-pilot upgrade          (Hub and every worker)
    jupyterhub-pilot load-test --hub http://<hub-private-ip>:8000 --teams team_1 ...
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from typing import List, Optional

from jupyterhub_pilot import __version__
from jupyterhub_pilot.paths import PACKAGE_DIR

# What each role needs importable, and the pip extra that provides it.
NEEDS = {
    "hub": (["jupyterhub", "oauthenticator", "paramiko"], "hub"),
    "worker": (["notebook", "psutil", "websockets"], "worker,ai"),
}


def missing_modules(role: str) -> List[str]:
    return [m for m in NEEDS[role][0] if importlib.util.find_spec(m) is None]


def run_script(name: str, args: List[str]) -> int:
    """Run a bundled setup script with this interpreter as its PYTHON."""
    path = os.path.join(PACKAGE_DIR, "scripts", name)
    env = {**os.environ, "PYTHON": sys.executable}
    return subprocess.run(["bash", path, *args], env=env).returncode


def _require(role: str) -> Optional[int]:
    missing = missing_modules(role)
    if not missing:
        return None
    print(f"❌ Missing {', '.join(missing)}. Install the {role} extras system-wide first:\n"
          f'   sudo pip3 install --break-system-packages "jupyterhub-pilot[{NEEDS[role][1]}]"',
          file=sys.stderr)
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jupyterhub-pilot",
        description="Set up a JupyterHub-Pilot Hub or worker. Run the install commands with sudo.",
    )
    parser.add_argument("--version", action="version", version=f"jupyterhub-pilot {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("install-hub", help="set up this machine as the Hub")
    sub.add_parser("install-worker", help="set up this machine as a team's worker")
    aw = sub.add_parser("add-worker", help="on the Hub: register a team's worker")
    aw.add_argument("team")
    aw.add_argument("worker_ip")
    aw.add_argument("admin_ssh_user", nargs="?", help="sudo user on the worker (default: the Hub's own user)")
    mh = sub.add_parser("mount-homes", help="on a worker: mount a shared NFS/EFS filesystem for the users' homes")
    mh.add_argument("filesystem", help="NFS server, e.g. fs-0123abcd.efs.ap-south-1.amazonaws.com")
    mh.add_argument("mount_point", nargs="?", default="/efs/home")
    sub.add_parser("load-test", add_help=False, help="on the Hub: start many test users (see load-test --help)")
    sub.add_parser("upgrade", add_help=False, help="upgrade this Hub or worker in place (see upgrade --help)")
    args, rest = parser.parse_known_args(argv)

    if args.cmd == "load-test":
        from jupyterhub_pilot import load_test

        return load_test.main(rest)
    if args.cmd == "upgrade":
        from jupyterhub_pilot import upgrade

        return upgrade.main(rest)
    if rest:
        parser.error(f"unrecognized arguments: {' '.join(rest)}")
    if args.cmd == "install-hub":
        return _require("hub") or run_script("install_hub.sh", [])
    if args.cmd == "mount-homes":
        return run_script("mount_homes.sh", [args.filesystem, args.mount_point])
    if args.cmd == "install-worker":
        return _require("worker") or run_script("install_worker.sh", [])
    extra = [args.admin_ssh_user] if args.admin_ssh_user else []
    return run_script("add_worker.sh", [args.team, args.worker_ip, *extra])


if __name__ == "__main__":
    sys.exit(main())
