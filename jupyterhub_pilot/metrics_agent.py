"""
jupyterhub_pilot/metrics_agent.py
──────────────────────────────
Lightweight psutil-based metrics agent for JupyterHub-Pilot Worker VMs.

Run this script on each Worker VM. It collects live system metrics every
2 seconds and streams them as JSON to the Hub's WebSocket monitoring endpoint.
The agent automatically reconnects with exponential backoff if the Hub is
temporarily unreachable.

Metrics collected
─────────────────
  - CPU usage  (percent, per-core)
  - Memory     (used, total, percent)
  - Disk       (used, total, percent — root filesystem)
  - GPU        (per card: usage, memory, temperature; only where nvidia-smi exists)
  - Network    (bytes_sent, bytes_recv, packets_sent, packets_recv,
                upload_mbps, download_mbps — rolling 2-second rates)

Usage
─────
    # On the Worker VM:
    python3 /opt/jupyterhub-pilot/jupyterhub_pilot/metrics_agent.py \\
        --hub ws://172.31.30.186:8000/hub/monitoring/ws \\
        --interval 2

Environment variables (alternative to CLI flags):
    JUPYTERPILOT_HUB_WS      – WebSocket URL of the Hub monitoring endpoint
    JUPYTERPILOT_INTERVAL    – Polling interval in seconds (default: 2)
    JUPYTERPILOT_AGENT_TOKEN – Shared secret; must match the Hub's value (required)
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
import os
import pwd
import shutil
import socket
import subprocess
import time
from typing import Any, Dict, List, Optional

import psutil

log = logging.getLogger("metrics_agent")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [metrics_agent] %(levelname)s %(message)s",
)

# ---------------------------------------------------------------------------
# Metric collection
# ---------------------------------------------------------------------------


# psutil measures a process's CPU between two cpu_percent() calls on the *same*
# Process object, so keep them across samples (pid -> Process).
_PROC_CACHE: Dict[int, psutil.Process] = {}
# Home-folder sizes (Linux user -> bytes), refreshed in the background by
# _refresh_home_sizes() because walking a home folder can take seconds.
_HOME_BYTES: Dict[str, int] = {}


# GPUs, refreshed in the background by _refresh_gpu(). Stays empty on a worker without an NVIDIA driver.
_GPU: Dict[str, Any] = {"gpus": [], "proc_mb": {}}


def _nvidia(query: str) -> List[List[str]]:
    """Rows of ``nvidia-smi --query-<query>`` as lists of stripped strings."""
    out = subprocess.run(
        ["nvidia-smi", f"--query-{query}", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10,
    )
    if out.returncode:
        raise OSError(out.stderr.strip() or "nvidia-smi failed")
    return [[c.strip() for c in line.split(",")] for line in out.stdout.splitlines() if line.strip()]


def _float(text: str) -> Optional[float]:
    try:
        return float(text)
    except ValueError:  # "[N/A]" on cards that do not report the value
        return None


def _read_gpus() -> Dict[str, Any]:
    """{"gpus": [one dict per card], "proc_mb": {pid: GPU memory in MB}}."""
    gpus = [
        {
            "index": int(row[0]), "name": row[1], "percent": _float(row[2]),
            "mem_used_mb": _float(row[3]), "mem_total_mb": _float(row[4]), "temp_c": _float(row[5]),
        }
        for row in _nvidia("gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu")
        if len(row) == 6
    ]
    proc_mb: Dict[int, float] = {}
    for row in _nvidia("compute-apps=pid,used_memory"):
        mb = _float(row[1]) if len(row) == 2 else None
        if mb is not None and row[0].isdigit():
            proc_mb[int(row[0])] = proc_mb.get(int(row[0]), 0.0) + mb  # one row per card the process uses
    return {"gpus": gpus, "proc_mb": proc_mb}


async def _refresh_gpu(every: float = 5.0) -> None:
    """Every *every* seconds, read the GPUs. Returns at once on a worker without ``nvidia-smi``."""
    if not shutil.which("nvidia-smi"):
        return
    failed = False
    while True:
        try:
            _GPU.update(await asyncio.to_thread(_read_gpus))
            failed = False
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            _GPU.update(gpus=[], proc_mb={})
            if not failed:  # once, not every 5 seconds
                log.warning("nvidia-smi found but not working (driver not loaded? reboot needed?): %s", exc)
            failed = True
        await asyncio.sleep(every)


def _cgroup_limits(pid: int) -> Dict[str, Optional[float]]:
    """Memory use/limit (MB) and CPU limit (% of one core) of *pid*'s cgroup v2.

    The spawner starts each server under ``systemd-run --scope`` with MemoryMax and
    CPUQuota; that scope's cgroup is what the kernel enforces (and OOM-kills on).
    """
    out: Dict[str, Optional[float]] = {"mem_mb": None, "mem_limit_mb": None, "cpu_limit_pct": None}
    try:
        with open(f"/proc/{pid}/cgroup") as fh:
            rel = next(line.split("::", 1)[1].strip() for line in fh if line.startswith("0::"))
        base = f"/sys/fs/cgroup{rel}"
        with open(f"{base}/memory.current") as fh:
            out["mem_mb"] = int(fh.read()) / 1048576
        with open(f"{base}/memory.max") as fh:
            raw = fh.read().strip()
            out["mem_limit_mb"] = None if raw == "max" else int(raw) / 1048576
        with open(f"{base}/cpu.max") as fh:
            quota, period = fh.read().split()
            out["cpu_limit_pct"] = None if quota == "max" else 100 * int(quota) / int(period)
    except (OSError, StopIteration, ValueError):
        pass  # not under cgroup v2, or no limits: report what we have
    return out


def _collect_users(cpu_count: int) -> List[Dict[str, Any]]:
    """One row per running notebook server: the server process plus its kernels/terminals.

    The JupyterHub user comes from the server's ``JUPYTERHUB_USER`` environment,
    so usage is right even when a team shares one Linux account.
    """
    rows: List[Dict[str, Any]] = []
    seen: set = set()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        cmd = " ".join(proc.info.get("cmdline") or [])
        if "jupyterhub-singleuser" not in cmd:
            continue
        try:
            tree = [proc, *proc.children(recursive=True)]
            linux_user = proc.username()
            jh_user = proc.environ().get("JUPYTERHUB_USER") or linux_user
            started = proc.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        cpu = rss = 0.0
        kernels = 0
        for p in tree:
            cached = _PROC_CACHE.setdefault(p.pid, p)
            seen.add(p.pid)
            try:
                cpu += cached.cpu_percent(None)  # % of one core since the last sample
                rss += cached.memory_info().rss
                kernels += "ipykernel" in " ".join(cached.cmdline())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        limits = _cgroup_limits(proc.pid)
        rows.append({
            "name": jh_user,
            "linux_user": linux_user,
            "cpu_percent": round(cpu / cpu_count, 1),  # share of the whole worker
            "cpu_cores": round(cpu / 100, 2),
            "cpu_limit_pct": limits["cpu_limit_pct"],  # % of one core, None = no limit
            "mem_mb": round(limits["mem_mb"] if limits["mem_mb"] is not None else rss / 1048576, 1),
            "mem_limit_mb": limits["mem_limit_mb"],
            "kernels": kernels,
            "processes": len(tree),
            "uptime_s": int(time.time() - started),
            "home_bytes": _HOME_BYTES.get(linux_user),
            # GPU memory of the server's kernels; None on a worker without a GPU.
            "gpu_mem_mb": round(sum(_GPU["proc_mb"].get(p.pid, 0.0) for p in tree), 1) if _GPU["gpus"] else None,
        })
    for pid in list(_PROC_CACHE):
        if pid not in seen:
            del _PROC_CACHE[pid]
    return sorted(rows, key=lambda r: r["mem_mb"], reverse=True)


_LAST_RUN_GLOB = ".config/jupyterhub-pilot/jobs/*.last"  # in each home; written by schedule.run()


def _failed_jobs() -> List[Dict[str, Any]]:
    """Scheduled notebooks (%schedule) whose last run failed: [{user, job, at}]."""
    out: List[Dict[str, Any]] = []
    # Homes of this worker's accounts (they can be on a shared disk, outside /home).
    paths = [
        (p.pw_name, path)
        for p in pwd.getpwall()
        if p.pw_uid >= 1000 and p.pw_dir.startswith("/")
        for path in glob.glob(os.path.join(glob.escape(p.pw_dir), _LAST_RUN_GLOB))
    ]
    for user, path in paths:
        try:
            # The file is the user's and the agent is root: no symlinks, never block on a FIFO.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                state, at = os.read(fd, 64).decode().split()
            finally:
                os.close(fd)
            if state == "failed":
                out.append({"user": user, "job": os.path.basename(path)[:-5], "at": int(at)})
        except (OSError, ValueError):
            continue
    return out


def _home_size(path: str) -> Optional[int]:
    """Bytes used under *path* (same filesystem only); None if du fails or is slow."""
    try:
        out = subprocess.run(["du", "-sxb", path], capture_output=True, text=True, timeout=30)
        return int(out.stdout.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


async def _refresh_home_sizes(every: float = 60.0) -> None:
    """Every *every* seconds, measure the home folder of each user with a server."""
    while True:
        users = set()
        for proc in psutil.process_iter(["cmdline", "username"]):
            if "jupyterhub-singleuser" in " ".join(proc.info.get("cmdline") or []):
                users.add(proc.info.get("username"))
        for linux_user in users - {None}:
            try:
                home = pwd.getpwnam(linux_user).pw_dir
            except KeyError:
                continue
            size = await asyncio.to_thread(_home_size, home)
            if size is not None:
                _HOME_BYTES[linux_user] = size
        await asyncio.sleep(every)


def _collect_metrics(prev_net: Dict[str, int], elapsed: float) -> Dict[str, Any]:
    """
    Collect a full system-metrics snapshot.

    Args:
        prev_net:  The previous network I/O counters dict (bytes_sent,
                   bytes_recv) used to compute per-second rates.
        elapsed:   Seconds since ``prev_net`` was recorded.

    Returns:
        A serialisable dict ready to JSON-encode and send over WebSocket.
    """
    # CPU — PERF FIX: call cpu_percent once with percpu=False, then call
    # the per-core variant separately.  The priming call in _stream() ensures
    # both are already warmed; no duplicate non-percpu call needed.
    cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
    cpu_percent: float = sum(cpu_per_core) / len(cpu_per_core) if cpu_per_core else 0.0
    cpu_count: int = psutil.cpu_count(logical=True) or 1

    # Memory
    mem = psutil.virtual_memory()
    mem_info = {
        "used_mb": round(mem.used / 1024 / 1024, 1),
        "total_mb": round(mem.total / 1024 / 1024, 1),
        "percent": mem.percent,
    }

    # Disk (root filesystem)
    disk = psutil.disk_usage("/")
    disk_info = {
        "used_gb": round(disk.used / 1024 / 1024 / 1024, 2),
        "total_gb": round(disk.total / 1024 / 1024 / 1024, 2),
        "percent": disk.percent,
    }

    # Network I/O — compute rolling rates
    net = psutil.net_io_counters()
    curr_sent = net.bytes_sent
    curr_recv = net.bytes_recv
    dt = max(elapsed, 0.1)  # guard against division by zero

    upload_bps = (curr_sent - prev_net.get("bytes_sent", curr_sent)) / dt
    download_bps = (curr_recv - prev_net.get("bytes_recv", curr_recv)) / dt

    net_info = {
        "bytes_sent": curr_sent,
        "bytes_recv": curr_recv,
        "packets_sent": net.packets_sent,
        "packets_recv": net.packets_recv,
        "upload_mbps": round(upload_bps / 1024 / 1024, 3),
        "download_mbps": round(download_bps / 1024 / 1024, 3),
        "upload_kbps": round(upload_bps / 1024, 1),
        "download_kbps": round(download_bps / 1024, 1),
    }

    # Load average (1-min, 5-min, 15-min) — Linux only
    try:
        load_avg = [round(x, 2) for x in os.getloadavg()]
    except (AttributeError, OSError):
        load_avg = [0.0, 0.0, 0.0]

    return {
        "uptime_s": int(time.time() - psutil.boot_time()),
        "users": _collect_users(cpu_count),
        "failed_jobs": _failed_jobs(),
        "hostname": socket.gethostname(),
        "ip": socket.gethostbyname(socket.gethostname()),
        "timestamp": time.time(),
        "cpu": {
            "percent": cpu_percent,
            "per_core": cpu_per_core,
            "core_count": cpu_count,
            "load_avg": load_avg,
        },
        "memory": mem_info,
        "gpus": _GPU["gpus"],  # [] = no GPU (or no driver) on this worker
        "disk": disk_info,
        "network": net_info,
        # Updated counters returned separately so the caller can pass them
        # back as prev_net on the next iteration.
        "_net_counters": {"bytes_sent": curr_sent, "bytes_recv": curr_recv},
    }


# ---------------------------------------------------------------------------
# WebSocket streaming loop
# ---------------------------------------------------------------------------


async def _stream(hub_ws_url: str, interval: float, token: str) -> None:
    """
    Connect to *hub_ws_url* and stream metrics every *interval* seconds.
    Reconnects with exponential back-off on failure.
    """
    # Import websockets lazily so the module can be imported without it.
    # websockets >=14 moved the client to websockets.asyncio.client.connect;
    # the top-level alias was kept for compat but removed in v15.
    try:
        try:
            from websockets.asyncio.client import connect as ws_connect  # type: ignore  # websockets >=14
            headers_kw = "additional_headers"
        except ImportError:
            from websockets import connect as ws_connect  # type: ignore  # websockets 12/13
            headers_kw = "extra_headers"
    except ImportError:
        log.error(
            "The 'websockets' package is required for the metrics agent. "
            "Install it with: pip install websockets"
        )
        return

    backoff = 1.0
    prev_net: Dict[str, int] = {}
    home_task = asyncio.create_task(_refresh_home_sizes())  # noqa: F841  (runs for the agent's lifetime)
    gpu_task = asyncio.create_task(_refresh_gpu())  # noqa: F841
    last_time: float = time.time()

    log.info("Metrics agent starting. Hub WS: %s  interval: %ss", hub_ws_url, interval)

    while True:
        try:
            auth = {headers_kw: {"Authorization": f"Bearer {token}"}}
            async with ws_connect(hub_ws_url, ping_interval=20, **auth) as ws:
                log.info("Connected to Hub monitoring endpoint.")
                backoff = 1.0  # reset on successful connection

                # Prime the CPU sampler (first reading is always 0.0)
                psutil.cpu_percent(interval=None)
                await asyncio.sleep(interval)

                while True:
                    now = time.time()
                    elapsed = now - last_time
                    last_time = now

                    snapshot = _collect_metrics(prev_net, elapsed)
                    prev_net = snapshot.pop("_net_counters")

                    payload = json.dumps(snapshot)
                    await ws.send(payload)
                    log.debug("Sent %d bytes to Hub.", len(payload))

                    await asyncio.sleep(interval)

        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Connection to Hub lost: %s. Reconnecting in %ds...",
                exc,
                int(backoff),
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10)  # cap at 10 s: a Hub restart shows up within seconds


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="JupyterHub-Pilot metrics agent — streams psutil metrics to the Hub."
    )
    parser.add_argument(
        "--hub",
        default=os.environ.get("JUPYTERPILOT_HUB_WS", ""),
        help="WebSocket URL of the Hub monitoring endpoint "
        "(e.g. ws://172.31.30.186:8000/hub/monitoring/ws)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("JUPYTERPILOT_INTERVAL", "2")),
        help="Polling interval in seconds (default: 2)",
    )
    args = parser.parse_args()

    if not args.hub:
        parser.error("Please supply --hub <WS_URL> or set JUPYTERPILOT_HUB_WS env var.")
    token = os.environ.get("JUPYTERPILOT_AGENT_TOKEN", "")
    if not token:
        parser.error("Set JUPYTERPILOT_AGENT_TOKEN (same value as on the Hub).")

    asyncio.run(_stream(args.hub, args.interval, token))


if __name__ == "__main__":
    main()
