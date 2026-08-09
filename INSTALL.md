<div align="center">

# 🛠️ JupyterHub-Pilot — Installation Guide

**Step-by-step setup for the Hub VM and every Worker EC2**

← [Back to Main README](README.md)

</div>

---

## 📖 Table of Contents

| Step | What you are doing |
|---|---|
| [Prerequisites](#-prerequisites) | What you need before you start |
| [Step 1 — Hub EC2 Setup](#-step-1--hub-ec2-setup) | Install JupyterHub-Pilot, configure the hub, bootstrap SQLite |
| [Step 2 — Worker EC2 Setup](#-step-2--worker-ec2-setup) | Prepare each worker EC2 to receive SSH spawns |
| [Step 3 — SSH Key Exchange](#-step-3--ssh-key-exchange) | Trust the Hub key on every worker |
| [Step 4 — Register Worker EC2s](#-step-4--register-worker-ec2s) | Tell the Hub which group maps to which EC2 |
| [Step 5 — Start JupyterHub](#-step-5--start-jupyterhub) | Run the hub and verify everything works |
| [Step 6 — (Optional) Metrics Agent](#-step-6--optional-metrics-agent) | Stream live CPU/RAM/disk to the monitoring dashboard |
| [Step 7 — (Optional) Vault Secrets](#-step-7--optional-vault-secret-injection) | Auto-inject per-user API keys at spawn |
| [Step 8 — (Optional) AI Magic Commands](#-step-8--optional-ai-magic-commands) | Enable `%do`, `%fix`, `%review`, `%rework` |
| [Verification Checklist](#-verification-checklist) | Confirm everything is working end-to-end |
| [Troubleshooting](#-troubleshooting) | Common errors and how to fix them |

---

## 📋 Prerequisites

### AWS EC2 Instance Types

| VM | Recommended Type | Notes |
|---|---|---|
| **Hub EC2** | `t3.small` (2 vCPU, 2 GB) | Always-on; low CPU, mostly I/O |
| **Worker EC2** | `r7i.xlarge` (4 vCPU, 32 GB) per 50 users | Memory-optimised; each user needs ~1-2 GB RAM |

### OS

```
Ubuntu 22.04 LTS (or 24.04) on all instances
Python 3.9+ — pre-installed on Ubuntu 22.04+
```

### AWS Security Groups

**Hub EC2 Security Group:**

| Type | Port | Source | Why |
|---|---|---|---|
| SSH | 22 | Your IP | Administration |
| Custom TCP | 8000 | 0.0.0.0/0 | JupyterHub web UI |
| Custom TCP | 8081 | Worker private IPs | Hub API — workers authenticate here at startup |

**Worker EC2 Security Group (repeat for each worker):**

| Type | Port | Source | Why |
|---|---|---|---|
| All Traffic | All | Hub private IP | Hub SSHes in and proxies to random high ports |

> Worker EC2s should **not** be publicly accessible — the Hub proxies all traffic through itself.

---

## 🖥️ Step 1 — Hub EC2 Setup

SSH into your Hub EC2 and run all commands in this section.

### 1a. Install system dependencies

```bash
sudo apt-get update
sudo apt-get install -y python3-pip python3-venv git net-tools
```

### 1b. Clone and install JupyterHub-Pilot

```bash
sudo git clone https://github.com/wajoud/jupyterhub-pilot.git /opt/jupyterhub-pilot
cd /opt/jupyterhub-pilot
sudo pip3 install -e . --break-system-packages
```

### 1c. Configure hub_settings.json

```bash
sudo nano /opt/jupyterhub-pilot/hub_settings.json
```

Fill in your values — replace `10.0.0.10` with your **Hub's private IP**:

```json
{
  "hub_ip":        "10.0.0.10",
  "proxy_api_url": "http://10.0.0.10:5432",
  "hub_api_url":   "http://10.0.0.10:8081/hub/api",
  "hub_bind_url":  "http://10.0.0.10:8081",
  "hub_port":      8000,

  "hosted_domain": "your-company.com",
  "admin_users":   ["admin@your-company.com"],

  "mapping_file":  "user_mapping.json",
  "db_path":       "/var/lib/jupyterhub/jupyterhub_pilot_state.db",

  "resource_limits": {
    "memory_max": "2G",
    "cpu_quota":  "100%"
  },

  "vault_enabled":     false,
  "vault_secret_path": "secret/jupyterhub-pilot"
}
```

### 1d. Generate the Hub SSH keypair

The Hub needs an SSH key to connect to Worker EC2s. This key is dedicated to JupyterHub-Pilot — do not reuse your personal key.

```bash
sudo mkdir -p /opt/jupyterhub-pilot/keys
sudo ssh-keygen -t ed25519 -f /opt/jupyterhub-pilot/keys/hub_worker -N ""
sudo chmod 600 /opt/jupyterhub-pilot/keys/hub_worker

echo ""
echo "=== Hub Public Key (copy this — you will need it in Step 3) ==="
sudo cat /opt/jupyterhub-pilot/keys/hub_worker.pub
```

**Copy and save the public key output** — it starts with `ssh-ed25519 ...`. You will paste it onto every Worker EC2 in Step 3.

### 1e. Create the SQLite DB directory

```bash
sudo mkdir -p /var/lib/jupyterhub
sudo chown $(whoami):$(whoami) /var/lib/jupyterhub
```

---

## ⚙️ Step 2 — Worker EC2 Setup

**Repeat this section on every Worker EC2** you want to add to the cluster.

### 2a. Install system dependencies

```bash
sudo apt-get update
sudo apt-get install -y \
    python3-pip python3-venv git \
    net-tools \
    psmisc
```

`net-tools` provides `netstat` (port availability check). `psmisc` provides `fuser` (kill stale processes on a port).

### 2b. Install JupyterHub on the worker

```bash
sudo pip3 install jupyterhub notebook --break-system-packages
```

`jupyterhub-singleuser` (the per-user server binary) is part of the `jupyterhub` package and must be installed on **every worker EC2**.

### 2c. Clone the repo on the worker

```bash
sudo git clone https://github.com/wajoud/jupyterhub-pilot.git /opt/jupyterhub-pilot
sudo pip3 install -e /opt/jupyterhub-pilot --break-system-packages
```

### 2d. Create the get_port.py script

The spawner runs `python3 ~/get_port.py` on the worker to allocate a free TCP port for each user. Install it into the admin user's home directory:

```bash
cat > /home/ubuntu/get_port.py << 'PYEOF'
#!/usr/bin/env python3
"""Find a free TCP port in the high-port range (8900-9900)."""
import socket

def find_free_port(start=8900, end=9900):
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free ports available in range")

if __name__ == "__main__":
    print(find_free_port())
PYEOF

chmod +x /home/ubuntu/get_port.py
```

JIT provisioning will copy this script into each user's home directory automatically on their first login.

### 2e. Enable systemd lingering for the admin user

```bash
sudo loginctl enable-linger ubuntu
```

This prevents systemd from killing processes when the SSH session closes — required for `systemd-run --user` cgroup scopes to persist.

---

## 🔑 Step 3 — SSH Key Exchange

The Hub needs to SSH into each worker EC2 **without a password prompt**.

### On each Worker EC2

Paste the Hub's public key (from Step 1d) into the admin user's `authorized_keys`:

```bash
mkdir -p ~/.ssh
echo "ssh-ed25519 AAAAC3Nza... (paste your full hub public key here)" >> ~/.ssh/authorized_keys
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys
```

### Verify from the Hub EC2

```bash
# Replace 10.0.1.10 with your worker's private IP:
ssh -i /opt/jupyterhub-pilot/keys/hub_worker \
    -o StrictHostKeyChecking=no \
    ubuntu@10.0.1.10 "echo SSH connection successful"

# Expected output:
# SSH connection successful
```

If you see `Permission denied`, the public key was not pasted correctly or file permissions are wrong.

---

## 🗄️ Step 4 — Register Worker EC2s

This tells the Hub which JupyterHub group maps to which Worker EC2.

### 4a. Edit user_mapping.json on the Hub

```bash
sudo nano /opt/jupyterhub-pilot/user_mapping.json
```

Add one entry per worker EC2:

```json
{
  "group_a": {
    "server_ip":      "10.0.1.10",
    "server_ssh_key": "/opt/jupyterhub-pilot/keys/hub_worker",
    "admin_ssh_user": "ubuntu"
  },
  "group_b": {
    "server_ip":      "10.0.1.20",
    "server_ssh_key": "/opt/jupyterhub-pilot/keys/hub_worker",
    "admin_ssh_user": "ubuntu"
  }
}
```

`server_ssh_key` is the **private** key on the Hub (not the `.pub`). All workers can share the same keypair — the public key is in every worker's `authorized_keys`.

### 4b. Seed the routing table into SQLite

```bash
python3 -m jupyterhub_pilot.seed_sqlite \
    --db      /var/lib/jupyterhub/jupyterhub_pilot_state.db \
    --mapping /opt/jupyterhub-pilot/user_mapping.json
```

Expected output:

```
  ✓ group_a                          → 10.0.1.10
  ✓ group_b                          → 10.0.1.20

[DONE] Seeded 2 team(s), skipped 0.
       DB: /var/lib/jupyterhub/jupyterhub_pilot_state.db
```

### Adding more workers later (no restart needed)

```bash
python3 -c "
from jupyterhub_pilot.session_store import SessionStore
s = SessionStore('/var/lib/jupyterhub/jupyterhub_pilot_state.db')
s.set_mapping('group_c', '10.0.1.30', '/opt/jupyterhub-pilot/keys/hub_worker', 'ubuntu')
print('group_c added — no hub restart needed!')
"
```

---

## 🚀 Step 5 — Start JupyterHub

### 5a. Assign users to groups

Start with dummy auth for initial setup:

```bash
cd /opt/jupyterhub-pilot
python3 -m jupyterhub -f jupyterhub_config.py \
    --JupyterHub.authenticator_class=dummy \
    --DummyAuthenticator.password=setup123
```

Open `http://<HUB-PUBLIC-IP>:8000` and:

1. Log in with your admin username and `setup123`
2. Go to **Admin** → **Groups** → **Add Group**: `group_a`
3. Click `group_a` → **Add Users**: `alice`, `bob`, `charlie`, ...
4. Repeat for each group

### 5b. Run as a persistent systemd service (production)

```bash
sudo tee /etc/systemd/system/jupyterhub.service > /dev/null << 'INI'
[Unit]
Description=JupyterHub
After=network.target

[Service]
WorkingDirectory=/opt/jupyterhub-pilot
ExecStart=/usr/bin/python3 -m jupyterhub -f /opt/jupyterhub-pilot/jupyterhub_config.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
INI

sudo systemctl daemon-reload
sudo systemctl enable --now jupyterhub
sudo systemctl status jupyterhub
```

---

## 📊 Step 6 — (Optional) Metrics Agent

Run this on **each Worker EC2** to stream live CPU/RAM/network to the hub's monitoring dashboard.

### Install dependencies on the worker

```bash
sudo pip3 install psutil websockets --break-system-packages
```

### Run as a systemd service on the worker

```bash
sudo tee /etc/systemd/system/jupyterhub_pilot-agent.service > /dev/null << 'INI'
[Unit]
Description=JupyterHub-Pilot Metrics Agent
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/jupyterhub-pilot/jupyterhub_pilot/metrics_agent.py --hub ws://HUB_PRIVATE_IP:8000/monitoring/ws --interval 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
INI

# Replace HUB_PRIVATE_IP with your actual Hub private IP:
sudo sed -i 's/HUB_PRIVATE_IP/10.0.0.10/' /etc/systemd/system/jupyterhub_pilot-agent.service

sudo systemctl daemon-reload
sudo systemctl enable --now jupyterhub_pilot-agent
```

Visit `http://<HUB-PUBLIC-IP>:8000/monitoring` to see the live dashboard.

---

## 🔑 Step 7 — (Optional) Vault Secret Injection

Automatically inject per-user API keys into each user's notebook environment at spawn time.

### On the Hub EC2

```bash
export VAULT_ADDR=http://10.0.0.5:8200
export VAULT_TOKEN=your-policy-scoped-token
```

Set `"vault_enabled": true` in `hub_settings.json`, then store user secrets:

```bash
vault kv put secret/jupyterhub-pilot/alice \
    OPENAI_API_KEY="sk-alice-..." \
    DATABASE_URL="postgres://alice:pass@db.internal/mydb"

vault kv put secret/jupyterhub-pilot/bob \
    OPENAI_API_KEY="sk-bob-..."
```

When `alice` spawns, `os.environ["OPENAI_API_KEY"]` is automatically `sk-alice-...`. No secrets on disk. Vault failure is non-fatal — spawning continues uninterrupted.

---

## 🤖 Step 8 — (Optional) AI Magic Commands

Enable `%do`, `%fix`, `%review`, and `%rework` for every notebook user.

### Auto-load for all users on the worker

```bash
# Run on the worker EC2:
sudo mkdir -p /etc/ipython/profile_default/startup/
sudo tee /etc/ipython/profile_default/startup/00-jupyterhub_pilot.py > /dev/null << 'PYEOF'
try:
    get_ipython().run_line_magic("load_ext", "jupyterhub_pilot.extension")
except Exception:
    pass
PYEOF
```

### Configure the LLM backend

Each user creates `~/.jupyterhub-pilot/config.json`. Example for cloud (OpenAI):

```json
{
  "mode": "cloud",
  "cloud": {
    "provider": "openai",
    "model":    "gpt-4o-mini",
    "api_key":  "sk-..."
  }
}
```

Example for local Ollama (running on the worker EC2):

```bash
# Install Ollama on the worker:
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5-coder:7b
```

```json
{
  "mode": "local",
  "local": {
    "url":   "http://localhost:11434/api/generate",
    "model": "qwen2.5-coder:7b"
  }
}
```

---

## ✅ Verification Checklist

### Hub EC2

```bash
# 1. SQLite DB is reachable and has team mappings
python3 -c "
from jupyterhub_pilot.session_store import SessionStore
s = SessionStore('/var/lib/jupyterhub/jupyterhub_pilot_state.db')
print('DB ping:', s.ping())
print('group_a:', s.get_mapping('group_a'))
"
# Expected: DB ping: True, group_a: {..., 'server_ip': '10.0.1.10'}

# 2. SSH connection and port script work on each worker
ssh -i /opt/jupyterhub-pilot/keys/hub_worker -o StrictHostKeyChecking=no \
    ubuntu@10.0.1.10 "python3 ~/get_port.py"
# Expected: a number between 8900-9900

# 3. JupyterHub API is responding
curl -s http://localhost:8081/hub/api/info | python3 -m json.tool | grep version
# Expected: "version": "..."
```

### Worker EC2

```bash
# 1. jupyterhub-singleuser is installed
jupyterhub-singleuser --version

# 2. get_port.py returns a free port
python3 ~/get_port.py

# 3. systemd lingering is enabled
loginctl show-user ubuntu | grep Linger
# Expected: Linger=yes
# Fix if needed: sudo loginctl enable-linger ubuntu
```

### End-to-end

1. Open `http://<HUB-PUBLIC-IP>:8000` and log in as `alice` (who is in `group_a`)
2. Click **Start My Server** — it should spawn on `10.0.1.10`
3. In a notebook cell: `%load_ext jupyterhub_pilot.extension`
4. Run: `%do print hello world`
5. Verify code is injected into the next cell

---

## 🔧 Troubleshooting

### "User has no groups assigned"

```
RuntimeError: User 'alice' has no groups assigned.
```

In the JupyterHub Admin Panel go to **Groups** → `group_a` → **Add Users** and add `alice`. Every user must be in at least one group that matches a row in the SQLite `team_mappings` table.

---

### SSH connection refused or timeout

```
paramiko.ssh_exception.NoValidConnectionsError
```

1. Does the Worker EC2 security group allow **All Traffic from the Hub private IP**?
2. Is the Hub public key in `/home/ubuntu/.ssh/authorized_keys` on the worker?
3. Test manually: `ssh -i /opt/jupyterhub-pilot/keys/hub_worker ubuntu@<worker-private-ip>`

---

### Server dies 7 seconds after spawn

Ubuntu's `systemd-logind` kills the process when the SSH session closes.

Fix — run on the worker for each user:

```bash
sudo loginctl enable-linger <username>
```

JIT provisioning does this automatically for new users.

---

### "hub_api_url not found in hub_settings.json"

```
RuntimeError: hub_api_url not found in hub_settings.json
```

Check that `hub_settings.json` contains `"hub_api_url"` and that you started JupyterHub from the `/opt/jupyterhub-pilot` directory.

---

### get_port.py not found

```
FileNotFoundError: ~/get_port.py
```

The script must exist at `~/get_port.py` for each user on the worker. JIT provisioning copies it automatically. For pre-existing users, copy manually:

```bash
sudo cp /home/ubuntu/get_port.py /home/<username>/get_port.py
sudo chown <username>:<username> /home/<username>/get_port.py
```

---

### LLM magic commands return an error

```
# ❌ Local inference error: ...
```

1. Is Ollama running? `systemctl status ollama`
2. Is the model downloaded? `ollama list`
3. Is the URL correct in `~/.jupyterhub-pilot/config.json`?
4. For cloud mode: is the API key valid?

---

### Metrics dashboard shows no workers

Check the agent is running and pointing to the correct Hub WS URL:

```bash
sudo systemctl status jupyterhub_pilot-agent
# The --hub flag should be: ws://<HUB-PRIVATE-IP>:8000/monitoring/ws
```

Also verify port `8000` on the Hub security group allows inbound from the Worker private IP.

---

<div align="center">

← [Back to Main README](README.md) &nbsp;·&nbsp; [☁️ AWS EC2 Deployment Guide](AWS_EC2_DEPLOYMENT.md)

Built with 🔧 by [@wajoud](https://github.com/wajoud)

</div>
