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

### 1b. Install JupyterHub-Pilot

```bash
sudo pip3 install --break-system-packages --ignore-installed "jupyterhub-pilot[hub]"
sudo mkdir -p /opt/jupyterhub-pilot/keys   # this machine's settings and keys
```

The code comes from PyPI; `/opt/jupyterhub-pilot` holds only this Hub's own files. (`sudo jupyterhub-pilot install-hub` does all of part 1 for you; this guide shows the steps by hand.)

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
  "admin_users":   ["admin"],

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

> ⚠️ `admin_users` takes the part of the email **before the `@`**: with `hosted_domain` set, Google login turns `admin@your-company.com` into the username `admin`. A full email here never gets admin rights.

### 1d. Generate the Hub SSH keypair

The Hub needs an SSH key to connect to Worker EC2s. This key is dedicated to JupyterHub-Pilot — do not reuse your personal key.

```bash
sudo mkdir -p /opt/jupyterhub-pilot/keys
sudo ssh-keygen -t ed25519 -f /opt/jupyterhub-pilot/keys/hub_worker -N ""
sudo chmod 600 /opt/jupyterhub-pilot/keys/hub_worker
# The Hub runs as you (not root), so it must own its key
sudo chown $(whoami):$(whoami) /opt/jupyterhub-pilot/keys/hub_worker /opt/jupyterhub-pilot/keys/hub_worker.pub

# Shared secret for the metrics agents (Step 6); the Hub needs it at start
sudo mkdir -p /etc/jupyterhub-pilot
[ -f /etc/jupyterhub-pilot/agent_token ] || openssl rand -hex 32 | sudo tee /etc/jupyterhub-pilot/agent_token > /dev/null
sudo chown $(whoami):$(whoami) /etc/jupyterhub-pilot/agent_token
sudo chmod 600 /etc/jupyterhub-pilot/agent_token

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

### 2c. Install JupyterHub-Pilot on the worker

```bash
sudo pip3 install --break-system-packages --ignore-installed "jupyterhub-pilot[worker,ai]"
```

Install system-wide, so every user's notebook server finds `jupyterhub-singleuser` and the AI magics. No port helper script is needed: the spawner picks a free port with an inline `python3 -c` over SSH. (`sudo jupyterhub-pilot install-worker` does part 2 for you.)

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

Add the new worker to `/opt/jupyterhub-pilot/user_mapping.json` the same way as in 4a:

```json
  "group_c": {
    "server_ip":      "10.0.1.30",
    "server_ssh_key": "/opt/jupyterhub-pilot/keys/hub_worker",
    "admin_ssh_user": "ubuntu"
  }
```

Then re-run the seed command from 4b. It is safe to re-run: existing groups are updated, not duplicated. The Hub picks up the new group on the next server start, so no restart is needed.

> Leave out `ssh_user` unless you want every user of the group to share one Linux account. Without it, each user gets their own account on the worker (created through `admin_ssh_user`).

---

## 🚀 Step 5 — Start JupyterHub

### 5a. Assign users to groups

Google login needs HTTPS on a real domain name: Google rejects `http://` and bare-IP callbacks, and the Hub's `Secure` login cookie is dropped by browsers over plain HTTP. Put the Hub behind HTTPS first — see [HTTPS with Caddy](AWS_EC2_DEPLOYMENT.md#-4-https-with-caddy) — and keep port 8000 closed to the internet.

Start the Hub with Google login:

```bash
cd /opt/jupyterhub-pilot
export CLIENT_ID=... CLIENT_SECRET=...
export OAUTH_CALLBACK_URL=https://hub.example.com/hub/oauth_callback
export JUPYTERPILOT_AGENT_TOKEN=$(cat /etc/jupyterhub-pilot/agent_token)
python3 -m jupyterhub -f "$(python3 -c 'import jupyterhub_pilot.paths as p; print(p.PACKAGE_DIR)')/jupyterhub_config.py"
```

The Hub config ships in the package and reads this machine's settings from `/opt/jupyterhub-pilot` (change with `JUPYTERHUB_PILOT_DIR`).

> ⚠️ Never start the Hub with `DummyAuthenticator` on a reachable network: anyone could log in as any user, including the admin.

Open `https://hub.example.com` and:

1. Log in with your Google admin account
2. Go to **Admin** → **Groups** → **Add Group**: `group_a`
3. Click `group_a` → **Add Users**: `alice`, `bob`, `charlie`, ...
4. Repeat for each group

### 5b. Run as a persistent systemd service (production)

Put the secrets the Hub needs in an env file only root can read:

```bash
sudo tee /etc/jupyterhub-pilot/hub.env > /dev/null << EOF
CLIENT_ID=<google-client-id>
CLIENT_SECRET=<google-client-secret>
OAUTH_CALLBACK_URL=https://hub.example.com/hub/oauth_callback
JUPYTERPILOT_AGENT_TOKEN=$(sudo cat /etc/jupyterhub-pilot/agent_token)
EOF
sudo chmod 600 /etc/jupyterhub-pilot/hub.env
```

Create the service. It runs as `ubuntu`, which owns the SSH key and the SQLite DB; change `User=` if your Hub user differs:

```bash
sudo tee /etc/systemd/system/jupyterhub.service > /dev/null << 'INI'
[Unit]
Description=JupyterHub
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/opt/jupyterhub-pilot
Environment=JUPYTERHUB_PILOT_DIR=/opt/jupyterhub-pilot
EnvironmentFile=/etc/jupyterhub-pilot/hub.env
# <PACKAGE_DIR>: python3 -c 'import jupyterhub_pilot.paths as p; print(p.PACKAGE_DIR)'
ExecStart=/usr/bin/python3 -m jupyterhub -f <PACKAGE_DIR>/jupyterhub_config.py
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
# Contains JUPYTERPILOT_AGENT_TOKEN=<same value as the Hub>; chmod 600
EnvironmentFile=/etc/jupyterhub-pilot/agent.env
ExecStart=/usr/bin/python3 -m jupyterhub_pilot.metrics_agent --hub ws://HUB_PRIVATE_IP:8000/hub/monitoring/ws --interval 2
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

Visit `https://hub.example.com/hub/monitoring` to see the live dashboard.

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

# 2. The Hub can SSH to each worker, which has Jupyter installed
ssh -i /opt/jupyterhub-pilot/keys/hub_worker -o StrictHostKeyChecking=no \
    ubuntu@10.0.1.10 "command -v jupyterhub-singleuser"
# Expected: a path such as /usr/local/bin/jupyterhub-singleuser

# 3. JupyterHub API is responding
curl -s http://localhost:8081/hub/api/info | python3 -m json.tool | grep version
# Expected: "version": "..."
```

### Worker EC2

```bash
# 1. jupyterhub-singleuser is installed
jupyterhub-singleuser --version

# 3. systemd lingering is enabled
loginctl show-user ubuntu | grep Linger
# Expected: Linger=yes
# Fix if needed: sudo loginctl enable-linger ubuntu
```

### End-to-end

1. Open `https://hub.example.com` and log in as `alice` (who is in `group_a`)
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
# The --hub flag should be: ws://<HUB-PRIVATE-IP>:8000/hub/monitoring/ws
```

Also verify port `8000` on the Hub security group allows inbound from the Worker private IP.

---

<div align="center">

← [Back to Main README](README.md) &nbsp;·&nbsp; [☁️ AWS EC2 Deployment Guide](AWS_EC2_DEPLOYMENT.md)

Built with 🔧 by [@wajoud](https://github.com/wajoud)

</div>
