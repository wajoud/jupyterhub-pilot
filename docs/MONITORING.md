# 📊 Live Monitoring Dashboard

> **Goal:** The hub admin can see real-time CPU, memory, disk, and network I/O for every connected worker EC2 — without SSH-ing into anything.

## Architecture

The monitoring system runs completely independently of the JupyterHub core application logic to ensure it doesn't impact spawn performance.

```text
Worker EC2 (metrics_agent.py)
  ↓  WebSocket every 2 seconds
Hub EC2 (AgentWebSocketHandler at /monitoring/ws)
  ↓  stores latest snapshot per hostname in _WORKER_SNAPSHOTS
  ↓  broadcasts to all connected browser tabs
Browser (monitoring.html)
  ↓  dark-mode real-time dashboard with per-worker panels
```

---

## Starting the Agent on each Worker EC2

To see a worker EC2 in the dashboard, you must run the lightweight `metrics_agent.py` on it.

```bash
python3 /opt/jupyterhub-pilot/jupyterhub_pilot/metrics_agent.py \
    --hub      ws://10.0.0.10:8000/monitoring/ws \
    --interval 2
```

*(Where `10.0.0.10` is the private IP of your Hub EC2).*

### Running as a systemd service

You'll usually want this to survive reboots. Create a systemd service file:

```ini
# /etc/systemd/system/jupyterhub_pilot-agent.service
[Unit]
Description=JupyterHub-Pilot Metrics Agent
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/jupyterhub-pilot/jupyterhub_pilot/metrics_agent.py \
    --hub ws://10.0.0.10:8000/monitoring/ws --interval 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl enable --now jupyterhub_pilot-agent
```

---

## Viewing the Dashboard

**Visit `http://<HUB-PUBLIC-IP>:8000/monitoring` to see the live dashboard.**

What you see per worker EC2:
- **CPU:** Live CPU % (total + per-core) and 1/5/15-min load averages
- **RAM:** used / total / percent
- **Disk:** used / total / percent (root filesystem)
- **Network:** upload/download Mbps (rolling 2-second rate)

### Access control

- All authenticated JupyterHub users can view the dashboard.
- Regular users see **only** their own worker EC2's panel (the machine their group is assigned to).
- Admins see **all** connected workers across the entire cluster.
