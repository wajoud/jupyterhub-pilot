"""
jupyterhub_pilot/seed_sqlite.py
────────────────────────────
CLI bootstrap script: load ``user_mapping.json`` into the SQLite DB.

Run this once after first deploy (or after a DB file loss) to populate
the ``team_mappings`` table from the existing JSON backup:

    python -m jupyterhub_pilot.seed_sqlite \\
        --db  /var/lib/jupyterhub/jupyterhub_pilot_state.db \\
        --mapping  /etc/jupyterhub/user_mapping.json

The script is idempotent: running it a second time on the same mapping
will overwrite existing rows with the same team key (INSERT OR REPLACE).
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m jupyterhub_pilot.seed_sqlite",
        description=(
            "Seed the JupyterHub-Pilot SQLite state DB from a user_mapping.json file. "
            "Run once on first deploy or after DB file recovery."
        ),
    )
    parser.add_argument(
        "--db",
        default="jupyterhub_pilot_state.db",
        help="Path to the SQLite DB file (default: %(default)s)",
    )
    parser.add_argument(
        "--mapping",
        default="user_mapping.json",
        help="Path to user_mapping.json (default: %(default)s)",
    )
    return parser.parse_args()


def seed(db_path: str, mapping_path: str) -> None:
    """
    Read *mapping_path* and write every team entry into the SQLite DB at
    *db_path*.

    Args:
        db_path:      Absolute (or relative) path to the ``.db`` file.
        mapping_path: Path to ``user_mapping.json``.

    Raises:
        SystemExit: On file-not-found or JSON parse errors.
    """
    # ── Load mapping JSON ──────────────────────────────────────────────────
    if not os.path.isfile(mapping_path):
        print(f"[ERROR] Mapping file not found: {mapping_path}", file=sys.stderr)
        sys.exit(1)

    with open(mapping_path, "r") as fh:
        try:
            mapping: dict = json.load(fh)
        except json.JSONDecodeError as exc:
            print(f"[ERROR] Invalid JSON in {mapping_path}: {exc}", file=sys.stderr)
            sys.exit(1)

    # ── Import SessionStore (must be importable from this context) ─────────
    # Adjust sys.path so this script works both as a module and directly.
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from jupyterhub_pilot.session_store import SessionStore  # noqa: PLC0415

    store = SessionStore(db_path)
    store.init_db()

    seeded = 0
    skipped = 0

    for team, info in mapping.items():
        # Skip comment keys (used in hub_settings.json)
        if team.startswith("_"):
            skipped += 1
            continue

        server_ip = info.get("server_ip", "")
        ssh_key = info.get("server_ssh_key", "")
        ssh_user = info.get("ssh_user")  # optional field

        if not server_ip or not ssh_key:
            print(
                f"[WARN] Skipping '{team}': missing server_ip or server_ssh_key.",
                file=sys.stderr,
            )
            skipped += 1
            continue

        store.set_mapping(team, server_ip, ssh_key, ssh_user)
        print(f"  ✓ {team:30s} → {server_ip}")
        seeded += 1

    print(f"\n[DONE] Seeded {seeded} team(s), skipped {skipped}.")
    print(f"       DB: {os.path.abspath(db_path)}")


if __name__ == "__main__":
    args = _parse_args()
    seed(args.db, args.mapping)
