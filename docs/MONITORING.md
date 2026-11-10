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
  ↓  live dashboard with per-worker panels and 15-day per-user history
```

---

## Starting the Agent on each Worker EC2

To see a worker EC2 in the dashboard, you must run the lightweight `metrics_agent.py` on it.

Agents authenticate with a shared token. Set the same `JUPYTERPILOT_AGENT_TOKEN` on the Hub process and on every agent (`jupyterhub-pilot install-hub` generates one at `/etc/jupyterhub-pilot/agent_token`; `install-worker` sets up the agent service for you). The Hub rejects all agents while the variable is unset. The dashboard itself requires a JupyterHub login.

```bash
JUPYTERPILOT_AGENT_TOKEN=<token> python3 -m jupyterhub_pilot.metrics_agent \
    --hub      ws://10.0.0.10:8000/hub/monitoring/ws \
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
# Contains JUPYTERPILOT_AGENT_TOKEN=<token>; chmod 600
EnvironmentFile=/etc/jupyterhub-pilot/agent.env
ExecStart=/usr/bin/python3 -m jupyterhub_pilot.metrics_agent \
    --hub ws://10.0.0.10:8000/hub/monitoring/ws --interval 2
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

**Visit `https://<your-hub-domain>/hub/monitoring` to see the live dashboard.** The page is one file with no external scripts, so it also works on a Hub with no internet access, and follows the browser's light or dark setting.

From top to bottom:

- **Needs attention** (only when something does): a user at 90% or more of their memory limit, a worker's disk at 85% or more, a worker's memory at 90% or more, a scheduled notebook whose last run failed.
- **Totals:** workers online, notebook servers running, CPU and memory in use across all workers.
- **One card per worker:** CPU, memory and disk (with load average and used/total), a 5-minute CPU and memory graph, network speed, and one row per notebook server:
  - **CPU:** cores in use, compared with the user's CPU limit (`cpu_quota`)
  - **Memory:** used, compared with the user's memory limit (`memory_max`). The bar turns red near the limit, where the kernel kills the user's processes.
  - **Kernels**, **home folder size** (refreshed every minute), **how long the server has run**, and a **History** button.
- **Usage history:** a user's CPU cores (average) and memory (average and peak) over the last 24 hours, 7 days or 15 days, with their limit drawn as a dashed line. Hover for exact values; **Show as a table** lists them.

Limits and memory come from the server's cgroup (the `systemd-run` scope the spawner creates). The user is taken from the server's `JUPYTERHUB_USER`, so rows are right even when a team shares one Linux account.

## Usage history

The Hub averages each user's rows into 5-minute points (average CPU cores, average and peak memory, the limits at that time) and stores them in the `user_usage` table of the state DB (`db_path`). Points older than 15 days are deleted. About 4,300 rows per always-on user; nothing to set up.

- Nothing is stored while a user's server is stopped: the graph shows a gap.
- A Hub restart loses at most the 5 minutes in progress.
- `GET /hub/monitoring/history?user=<name>&days=<1-15>` returns the points as JSON (your own; any user for admins).
- Straight from the DB, for a report: `sqlite3 /var/lib/jupyterhub/jupyterhub_pilot_state.db "SELECT username, ROUND(AVG(cpu_cores),2), ROUND(MAX(mem_peak_mb)) FROM user_usage GROUP BY username"`.

## Grafana and Prometheus

For longer history, alerts or your own dashboards, the Hub serves the latest numbers of every worker and user in the Prometheus text format. It is off until you set a token:

```bash
echo "JUPYTERPILOT_METRICS_TOKEN=$(openssl rand -hex 32)" | sudo tee -a /etc/jupyterhub-pilot/hub.env
sudo systemctl restart jupyterhub
```

```yaml
# prometheus.yml
scrape_configs:
  - job_name: jupyterhub-pilot
    metrics_path: /hub/monitoring/metrics
    authorization:
      credentials: <the token>
    static_configs:
      - targets: ["<HUB_PRIVATE_IP>:8000"]
```

Gauges, all prefixed `jupyterhub_pilot_`: `worker_cpu_percent`, `worker_memory_used_mb`, `worker_memory_total_mb`, `worker_disk_percent` (label `worker`); `user_cpu_cores`, `user_cpu_limit_cores`, `user_memory_mb`, `user_memory_limit_mb`, `user_kernels` (labels `worker`, `user`). In Grafana, add the Prometheus server as a data source and graph, for example, `jupyterhub_pilot_user_memory_mb / jupyterhub_pilot_user_memory_limit_mb`.

The token is separate from the agents' token, so a scraper cannot post metrics. Scrape over the private network; do not expose port 8000 publicly.

### Access control

- All authenticated JupyterHub users can open the dashboard.
- Regular users see **only** the workers their groups are mapped to (`server_ip` in the team mapping).
- Admins see **all** connected workers across the entire cluster, with every user's row.
- Regular users see their workers' totals but **only their own** usage row, not their teammates'.
- A worker is matched by the IP the Hub sees for its agent's connection, so `server_ip` must be the worker's **private** IP (the one the agent connects from).
- When an agent disconnects, its worker disappears from the dashboard.
