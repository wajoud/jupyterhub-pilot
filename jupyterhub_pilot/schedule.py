"""
Scheduled notebooks: ``%schedule`` runs a notebook on a timer, on the user's
team worker, as the user, under their RAM/CPU limit.

    %load_ext jupyterhub_pilot.schedule
    %schedule add daily report.ipynb           # every day at midnight
    %schedule add "*-*-* 02:00" report.ipynb --keep 10
    %schedule list
    %schedule run report | pause report | resume report | delete report

How: a systemd *user* timer + service in ~/.config/systemd/user (jhp-<name>.*).
The service runs the notebook with papermill and saves the executed copy next
to it as <name>-<YYYYmmdd-HHMM>.ipynb (-FAILED if it raised), keeping the last
N. Linger is on for every user, so timers run with no browser or server open
and survive reboots (Persistent=true catches up a run missed while the worker
was down). Deleting the Hub user removes the account, and with it the jobs.
"""

from __future__ import annotations

import glob
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from typing import List

UNIT_DIR = os.path.expanduser("~/.config/systemd/user")
ENV_DIR = os.path.expanduser("~/.config/jupyterhub-pilot/jobs")
PREFIX = "jhp-"
_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_RUN_RE = re.compile(r"-\d{8}-\d{4}(-FAILED)?\.ipynb$")

USAGE = __doc__.split("\n\n")[1]


def _q(arg: str) -> str:
    """Quote one argument for a systemd unit file (ExecStart and friends)."""
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def job_name(notebook: str) -> str:
    stem = os.path.splitext(os.path.basename(notebook))[0]
    return re.sub(r"[^A-Za-z0-9_-]+", "_", stem)[:64] or "notebook"


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")  # user manager's socket
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, env=env)


def current_limits() -> List[str]:
    """MemoryMax/CPUQuota lines matching this kernel's own cgroup (the user's limit)."""
    from jupyterhub_pilot.metrics_agent import _cgroup_limits

    lim = _cgroup_limits(os.getpid())
    out = []
    if lim["mem_limit_mb"]:
        out.append(f"MemoryMax={int(lim['mem_limit_mb'])}M")
    if lim["cpu_limit_pct"]:
        out.append(f"CPUQuota={int(lim['cpu_limit_pct'])}%")
    return out


def secret_env() -> str:
    """The Vault secrets this server got (names in JUPYTERHUB_PILOT_SECRET_KEYS), as an EnvironmentFile."""
    lines = []
    for key in filter(None, os.environ.get("JUPYTERHUB_PILOT_SECRET_KEYS", "").split(",")):
        value = os.environ.get(key)
        if value is None or "\n" in value:
            continue
        lines.append(f'{key}="' + value.replace("\\", "\\\\").replace('"', '\\"') + '"')
    return "".join(line + "\n" for line in lines)


def render_units(name: str, notebook: str, calendar: str, keep: int, limits: List[str]) -> tuple:
    service = (
        "[Unit]\n"
        f"Description=jupyterhub-pilot scheduled notebook {name}\n"
        # With a shared home every worker sees this unit: run only where it was scheduled.
        f"ConditionHost={socket.gethostname()}\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"EnvironmentFile=-{_q(os.path.join(ENV_DIR, name + '.env'))}\n"
        f"ExecStart={_q(sys.executable)} -m jupyterhub_pilot.schedule _run {_q(notebook)} {keep}\n"
        + "".join(line + "\n" for line in limits)
    )
    timer = (
        "[Unit]\n"
        f"Description=Timer for jupyterhub-pilot scheduled notebook {name}\n\n"
        "[Timer]\n"
        f"OnCalendar={calendar.replace('%', '%%')}\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def add(calendar: str, notebook: str, keep: int = 5) -> str:
    notebook = os.path.abspath(os.path.expanduser(notebook))
    if not notebook.endswith(".ipynb") or not os.path.isfile(notebook):
        return f"❌ No notebook at {notebook}"
    if keep < 1:
        return "❌ --keep must be 1 or more"
    if "\n" in calendar or subprocess.run(["systemd-analyze", "calendar", calendar], capture_output=True).returncode:
        return (f"❌ {calendar!r} is not a systemd calendar time. Examples: hourly, daily, weekly, "
                '"Mon..Fri 08:00", "*-*-* 02:30" (see `man systemd.time`).')
    name = job_name(notebook)
    service, timer = render_units(name, notebook, calendar, keep, current_limits())
    os.makedirs(UNIT_DIR, exist_ok=True)
    os.makedirs(ENV_DIR, mode=0o700, exist_ok=True)
    env_path = os.path.join(ENV_DIR, name + ".env")
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # secrets: only the user can read
    with os.fdopen(fd, "w") as f:
        f.write(secret_env())
    with open(os.path.join(UNIT_DIR, f"{PREFIX}{name}.service"), "w") as f:
        f.write(service)
    with open(os.path.join(UNIT_DIR, f"{PREFIX}{name}.timer"), "w") as f:
        f.write(timer)
    _systemctl("daemon-reload")
    r = _systemctl("enable", "--now", f"{PREFIX}{name}.timer")
    if r.returncode:
        return f"❌ systemctl failed: {r.stderr.strip()}"
    nxt = _systemctl("show", f"{PREFIX}{name}.timer", "-p", "NextElapseUSecRealtime", "--value").stdout.strip()
    return f"✅ '{name}' scheduled ({calendar}); next run {nxt or 'soon'}. Results: {name}-<date>.ipynb next to it."


def list_jobs() -> str:
    names = sorted(os.path.basename(p)[len(PREFIX):-6] for p in glob.glob(os.path.join(UNIT_DIR, PREFIX + "*.timer")))
    if not names:
        return "No scheduled notebooks."
    rows = ["name | when | next | last result"]
    for n in names:
        t = _show(f"{PREFIX}{n}.timer", "TimersCalendar", "NextElapseUSecRealtime", "UnitFileState")
        s = _show(f"{PREFIX}{n}.service", "Result", "ExecMainExitTimestamp")
        # TimersCalendar looks like "{ OnCalendar=*-*-* 02:00:00 ; next_elapse=... }"
        when = t.get("TimersCalendar", "?").split("OnCalendar=")[-1].split(" ;")[0]
        nxt = "paused" if t.get("UnitFileState") != "enabled" else (t.get("NextElapseUSecRealtime") or "-")
        ended = s.get("ExecMainExitTimestamp")
        last = f"{s.get('Result')} at {ended}" if ended else "never run"
        rows.append(f"{n} | {when} | {nxt} | {last}")
    return "\n".join(rows)


def _show(unit: str, *props: str) -> dict:
    """systemctl show as {property: value}; (--value output order is not guaranteed)."""
    args = ["show", unit] + [a for p in props for a in ("-p", p)]
    return dict(line.split("=", 1) for line in _systemctl(*args).stdout.splitlines() if "=" in line)


def control(action: str, name: str) -> str:
    if not _NAME_RE.fullmatch(name) or not os.path.exists(os.path.join(UNIT_DIR, f"{PREFIX}{name}.timer")):
        return f"❌ No scheduled notebook '{name}'. `%schedule list` shows them."
    unit = f"{PREFIX}{name}"
    if action == "run":
        r = _systemctl("start", "--no-block", f"{unit}.service")
        msg = f"▶️ '{name}' started; the result appears next to the notebook when it finishes."
    elif action == "pause":
        r = _systemctl("disable", "--now", f"{unit}.timer")
        msg = f"⏸ '{name}' paused."
    elif action == "resume":
        r = _systemctl("enable", "--now", f"{unit}.timer")
        msg = f"▶️ '{name}' resumed."
    else:  # delete
        r = _systemctl("disable", "--now", f"{unit}.timer")
        for path in (f"{unit}.timer", f"{unit}.service"):
            try:
                os.remove(os.path.join(UNIT_DIR, path))
            except FileNotFoundError:
                pass
        for ext in (".env", ".last"):
            try:
                os.remove(os.path.join(ENV_DIR, name + ext))
            except FileNotFoundError:
                pass
        _systemctl("daemon-reload")
        msg = f"🗑 '{name}' deleted (past results are kept)."
    return f"❌ systemctl failed: {r.stderr.strip()}" if r.returncode else msg


def schedule(line: str) -> str:
    try:
        args = shlex.split(line)
    except ValueError as e:
        return f"❌ {e}"
    if not args or args[0] in ("help", "-h", "--help"):
        return USAGE
    cmd, rest = args[0], args[1:]
    if cmd == "add":
        keep = 5
        if "--keep" in rest:
            i = rest.index("--keep")
            try:
                keep = int(rest[i + 1])
            except (IndexError, ValueError):
                return "❌ --keep needs a number"
            del rest[i:i + 2]
        if len(rest) != 2:
            return '❌ Usage: %schedule add <when> <notebook.ipynb> [--keep N], e.g. %schedule add daily report.ipynb'
        return add(rest[0], rest[1], keep)
    if cmd == "list":
        return list_jobs()
    if cmd in ("run", "pause", "resume", "delete") and len(rest) == 1:
        return control(cmd, rest[0])
    return USAGE


def prune(notebook: str, keep: int) -> None:
    base = notebook[: -len(".ipynb")]
    runs = sorted(p for p in glob.glob(glob.escape(base) + "-*.ipynb") if _RUN_RE.fullmatch(p[len(base):]))
    for old in runs[:-keep]:
        os.remove(old)


def run(notebook: str, keep: int) -> int:
    """The service's entry point: execute *notebook*, save the result, prune old ones."""
    import papermill as pm

    out = f"{notebook[:-len('.ipynb')]}-{time.strftime('%Y%m%d-%H%M')}.ipynb"
    status = 0
    try:
        pm.execute_notebook(notebook, out, cwd=os.path.dirname(notebook))
    except Exception as exc:  # noqa: BLE001  (papermill saved the notebook with the error in it)
        print(f"run failed: {exc}", file=sys.stderr)
        if os.path.exists(out):
            os.replace(out, out[: -len(".ipynb")] + "-FAILED.ipynb")
        status = 1
    prune(notebook, keep)
    # Last result, read by the worker's metrics agent: failed runs show on /hub/monitoring.
    os.makedirs(ENV_DIR, mode=0o700, exist_ok=True)
    with open(os.path.join(ENV_DIR, job_name(notebook) + ".last"), "w") as f:
        f.write(f"{'failed' if status else 'ok'} {int(time.time())}\n")
    return status


def load_ipython_extension(ipython) -> None:
    def _magic(line: str) -> None:
        print(schedule(line))

    ipython.register_magic_function(_magic, "line", "schedule")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "_run":
        sys.exit(run(sys.argv[2], int(sys.argv[3])))
    print(USAGE)
    sys.exit(2)
