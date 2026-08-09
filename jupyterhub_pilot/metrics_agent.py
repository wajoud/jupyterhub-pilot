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
  - Network    (bytes_sent, bytes_recv, packets_sent, packets_recv,
                upload_mbps, download_mbps — rolling 2-second rates)

Usage
─────
    # On the Worker VM:
    python3 /opt/jupyterhub-pilot/jupyterhub_pilot/metrics_agent.py \\
        --hub ws://172.31.30.186:8000/monitoring/ws \\
        --interval 2

Environment variables (alternative to CLI flags):
    JUPYTERPILOT_HUB_WS   – WebSocket URL of the Hub monitoring endpoint
    JUPYTERPILOT_INTERVAL – Polling interval in seconds (default: 2)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import time
from typing import Any, Dict

import psutil

log = logging.getLogger("metrics_agent")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [metrics_agent] %(levelname)s %(message)s",
)

# ---------------------------------------------------------------------------
# Metric collection
# ---------------------------------------------------------------------------


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
        "disk": disk_info,
        "network": net_info,
        # Updated counters returned separately so the caller can pass them
        # back as prev_net on the next iteration.
        "_net_counters": {"bytes_sent": curr_sent, "bytes_recv": curr_recv},
    }


# ---------------------------------------------------------------------------
# WebSocket streaming loop
# ---------------------------------------------------------------------------


async def _stream(hub_ws_url: str, interval: float) -> None:
    """
    Connect to *hub_ws_url* and stream metrics every *interval* seconds.
    Reconnects with exponential back-off on failure.
    """
    # Import websockets lazily so the module can be imported without it
    try:
        import websockets  # type: ignore
    except ImportError:
        log.error(
            "The 'websockets' package is required for the metrics agent. "
            "Install it with: pip install websockets"
        )
        return

    backoff = 1.0
    prev_net: Dict[str, int] = {}
    last_time: float = time.time()

    log.info("Metrics agent starting. Hub WS: %s  interval: %ss", hub_ws_url, interval)

    while True:
        try:
            async with websockets.connect(hub_ws_url, ping_interval=20) as ws:
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
            backoff = min(backoff * 2, 60)  # cap at 60 s


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
        "(e.g. ws://172.31.30.186:8000/monitoring/ws)",
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

    asyncio.run(_stream(args.hub, args.interval))


if __name__ == "__main__":
    main()
