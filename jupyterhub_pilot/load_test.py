#!/usr/bin/env python3
"""
Load test for a JupyterHub-Pilot cluster. Run it on the Hub.

  1. Creates N test users (lt-001, lt-002, ...) and spreads them over the teams.
  2. Starts all their servers at once and times how long each takes to be ready.
  3. In each ready server, runs a kernel that holds --mem-mb of memory and keeps
     one CPU busy for --cpu-seconds, and records whether it finished, failed or
     was killed (a kernel over the user's memory limit dies).
  4. Prints spawn times, failures and kernel results per team.
  --cleanup stops and deletes the test users instead. The Hub then deletes their
  Linux accounts on the workers (homes archived in /home/.deleted, see
  on_user_delete in README).

Needs an admin API token (Hub → Token page while logged in as an admin):

  export JUPYTERHUB_API_TOKEN=<token>
  jupyterhub-pilot load-test --hub http://<HUB_PRIVATE_IP>:8000 --teams team_1,team_2 --users 10

Only for test clusters: it creates real servers on real workers.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import requests


def usernames(prefix: str, n: int) -> List[str]:
    return [f"{prefix}-{i:03d}" for i in range(1, n + 1)]


def assign_teams(users: List[str], teams: List[str]) -> Dict[str, List[str]]:
    """Round-robin users over teams, so each worker gets an even share."""
    out: Dict[str, List[str]] = {t: [] for t in teams}
    for i, user in enumerate(users):
        out[teams[i % len(teams)]].append(user)
    return out


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1))))]


def workload_code(mem_mb: int, cpu_seconds: int) -> str:
    """Python run inside each user's kernel: hold memory, keep one core busy."""
    return (
        "import time\n"
        f"buf = bytearray({mem_mb} * 1024 * 1024)\n"
        "for i in range(0, len(buf), 4096):\n"
        "    buf[i] = 1  # touch every page so the memory is really used\n"
        f"end = time.time() + {cpu_seconds}\n"
        "n = 0\n"
        "while time.time() < end:\n"
        "    n += 1\n"
        "print('ok', n)\n"
    )


class Hub:
    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"token {token}"

    def api(self, method: str, path: str, ok=(200, 201, 202, 204), **kw: Any) -> requests.Response:
        r = self.s.request(method, f"{self.url}/hub/api{path}", timeout=30, **kw)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {path}: {r.status_code} {r.text[:200]}")
        return r

    def setup_users(self, teams: Dict[str, List[str]]) -> None:
        all_users = [u for us in teams.values() for u in us]
        self.api("POST", "/users", ok=(201, 409), json={"usernames": all_users})
        for team, users in teams.items():
            self.api("POST", f"/groups/{team}", ok=(201, 409))
            self.api("POST", f"/groups/{team}/users", json={"users": users})

    def spawn(self, user: str, timeout: float) -> Dict[str, Any]:
        t0 = time.monotonic()
        try:
            self.api("POST", f"/users/{user}/server", ok=(201, 202, 400))  # 400 = already running
            while time.monotonic() - t0 < timeout:
                server = self.api("GET", f"/users/{user}").json().get("servers", {}).get("")
                if server and server.get("ready"):
                    return {"user": user, "ok": True, "seconds": time.monotonic() - t0}
                if server is None and time.monotonic() - t0 > 5:
                    return {"user": user, "ok": False, "error": "spawn failed (see Hub log)"}
                time.sleep(1)
            return {"user": user, "ok": False, "error": f"not ready after {timeout:.0f}s"}
        except Exception as exc:  # noqa: BLE001
            return {"user": user, "ok": False, "error": str(exc)}

    def run_kernel(self, user: str, code: str, timeout: float) -> Dict[str, Any]:
        """Start a kernel in *user*'s server, run *code*, report how it ended."""
        from websockets.sync.client import connect

        try:
            tok = self.api("POST", f"/users/{user}/tokens", json={"expires_in": 3600, "note": "load test"}).json()["token"]
            headers = {"Authorization": f"token {tok}"}
            base = f"{self.url}/user/{user}/api"
            r = requests.post(f"{base}/kernels", headers=headers, json={}, timeout=60)
            r.raise_for_status()
            kid = r.json()["id"]
            ws_url = base.replace("http", "ws", 1) + f"/kernels/{kid}/channels"
            msg_id = uuid.uuid4().hex
            with connect(ws_url, additional_headers=headers, open_timeout=30) as ws:
                ws.send(json.dumps({
                    "header": {"msg_id": msg_id, "username": user, "session": uuid.uuid4().hex,
                               "msg_type": "execute_request", "version": "5.3"},
                    "parent_header": {}, "metadata": {}, "channel": "shell",
                    "content": {"code": code, "silent": False, "store_history": False,
                                "user_expressions": {}, "allow_stdin": False, "stop_on_error": True},
                }))
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    msg = json.loads(ws.recv(timeout=max(1.0, deadline - time.monotonic())))
                    kind = msg.get("msg_type") or msg.get("header", {}).get("msg_type")
                    if kind == "status" and msg["content"].get("execution_state") in ("restarting", "dead"):
                        return {"user": user, "result": "killed (over memory limit?)"}
                    if kind == "execute_reply" and msg.get("parent_header", {}).get("msg_id") == msg_id:
                        status = msg["content"].get("status")
                        return {"user": user, "result": "finished" if status == "ok" else "code error"}
            return {"user": user, "result": f"no reply after {timeout:.0f}s"}
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            return {"user": user, "result": "killed (connection dropped)" if "close" in text.lower() else f"error: {text[:120]}"}

    def cleanup(self, users: List[str]) -> None:
        for u in users:
            self.api("DELETE", f"/users/{u}/server", ok=(202, 204, 400, 404))
        time.sleep(5)
        for u in users:
            self.api("DELETE", f"/users/{u}", ok=(204, 404))


def fmt(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.1f}s"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hub", required=True, help="Hub URL from the Hub VM, e.g. http://172.31.17.142:8000")
    ap.add_argument("--teams", default="", help="comma-separated team groups (as in user_mapping.json)")
    ap.add_argument("--users", type=int, default=10)
    ap.add_argument("--prefix", default="lt")
    ap.add_argument("--mem-mb", type=int, default=300, help="memory each kernel holds (limit is memory_max)")
    ap.add_argument("--cpu-seconds", type=int, default=60)
    ap.add_argument("--spawn-timeout", type=float, default=180)
    ap.add_argument("--parallel", type=int, default=20, help="how many spawns/kernels run at once")
    ap.add_argument("--no-workload", action="store_true", help="only time the spawns")
    ap.add_argument("--cleanup", action="store_true", help="stop and delete the test users, then exit")
    args = ap.parse_args(argv)

    token = os.environ.get("JUPYTERHUB_API_TOKEN", "")
    if not token:
        ap.error("export JUPYTERHUB_API_TOKEN=<admin token from the Hub's Token page>")
    hub = Hub(args.hub, token)
    users = usernames(args.prefix, args.users)

    if args.cleanup:
        hub.cleanup(users)
        print(f"Stopped and deleted {len(users)} test users ({users[0]} … {users[-1]}).")
        return 0

    teams = [t for t in args.teams.split(",") if t]
    if not teams:
        ap.error("--teams is required (e.g. --teams team_1,team_2)")
    plan = assign_teams(users, teams)
    team_of = {u: t for t, us in plan.items() for u in us}
    hub.setup_users(plan)
    print(f"Spawning {len(users)} servers over {len(teams)} teams, {args.parallel} at a time…")

    with ThreadPoolExecutor(args.parallel) as pool:
        spawns = list(pool.map(lambda u: hub.spawn(u, args.spawn_timeout), users))
    ready = [s["user"] for s in spawns if s["ok"]]

    kernels: List[Dict[str, Any]] = []
    if ready and not args.no_workload:
        print(f"Running {args.mem_mb} MB / {args.cpu_seconds}s CPU in {len(ready)} kernels… "
              "(watch /hub/monitoring)")
        code = workload_code(args.mem_mb, args.cpu_seconds)
        with ThreadPoolExecutor(args.parallel) as pool:
            kernels = list(pool.map(lambda u: hub.run_kernel(u, code, args.cpu_seconds + 120), ready))
    result_of = {k["user"]: k["result"] for k in kernels}

    print(f"\n{'team':<14}{'users':>6}{'ready':>7}{'p50':>8}{'p95':>8}{'max':>8}   kernels")
    for team, us in plan.items():
        times = [s["seconds"] for s in spawns if s["ok"] and team_of[s["user"]] == team]
        outcomes: Dict[str, int] = {}
        for u in us:
            if u in result_of:
                outcomes[result_of[u]] = outcomes.get(result_of[u], 0) + 1
        summary = ", ".join(f"{n} {r}" for r, n in sorted(outcomes.items())) or "—"
        print(f"{team:<14}{len(us):>6}{len(times):>7}{fmt(percentile(times, 50)):>8}"
              f"{fmt(percentile(times, 95)):>8}{fmt(max(times) if times else None):>8}   {summary}")
    all_times = [s["seconds"] for s in spawns if s["ok"]]
    print(f"\nall: {len(ready)}/{len(users)} ready, median {fmt(statistics.median(all_times) if all_times else None)}")
    for s in spawns:
        if not s["ok"]:
            print(f"  ✗ {s['user']} ({team_of[s['user']]}): {s['error']}")
    print(f"\nWhen done: jupyterhub-pilot load-test --hub {args.hub} --users {args.users} --prefix {args.prefix} --cleanup")
    return 0 if len(ready) == len(users) else 1


if __name__ == "__main__":
    sys.exit(main())
