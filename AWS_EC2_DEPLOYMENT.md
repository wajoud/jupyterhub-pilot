# ☁️ AWS EC2 Automated Deployment Guide

This guide walks through deploying JupyterHub-Pilot across two EC2 instances (a Hub node and a Worker node) using our automated bash setup scripts.

## 🏗️ 1. AWS Infrastructure Setup

### Instance Types
- **Recommended**: `t4g.small` (2 vCPU, 2 GiB Memory)
- **Architecture**: 64-bit Arm (Graviton)
- **Region**: the one closest to your users (latency matters: Notebook loads ~40 JavaScript files on first open).
- **OS**: Ubuntu 26.04 LTS. On **Amazon Linux 2023** log in as `ec2-user`, run `sudo dnf install -y git` before cloning, use `scripts/install_hub_amazon_linux.sh` / `scripts/install_worker_amazon_linux.sh` instead of the Ubuntu scripts, and answer `ec2-user` wherever the steps say `ubuntu` (not yet tested end to end; see `TODO.md`).
- **Storage**: 15 GiB `gp3` per instance (stays within the 30 GB AWS Free Tier limit).

### Security Groups
1. **EC2 #1 (The Hub)**:
   - Allow `SSH (Port 22)` from Anywhere.
   - Allow `HTTP (Port 80)` and `HTTPS (Port 443)` from Anywhere (Caddy serves the web UI over HTTPS; port 80 is needed to get the certificate).
   - Allow `Custom TCP (Port 8000)` from the **Worker's Private IP** only (the metrics agent connects here). Do not open 8000 to the internet.
   - Allow `Custom TCP (Port 8081)` from the **Worker's Private IP** (Allows the Worker to authenticate with the Hub API on startup).

### Domain name (required)
Google login only accepts an **HTTPS** callback on a **real domain name** — never `http://` or a bare IP. Point a DNS `A` record (e.g. `hub.example.com`) at the Hub's public IP (use an Elastic IP so it does not change).
2. **EC2 #2 (The Worker)**:
   - Allow `All Traffic` from the **Hub's Private IP** (Allows the Hub to SSH in and proxy to random high ports).

---

## 🚀 2. EC2 #1 (Hub) Setup

SSH into your Hub instance and run:

```bash
# 1. Clone the repo (Ubuntu 26 doesn't support sudo bash <(curl...) process substitution)
git clone https://github.com/wajoud/jupyterhub-pilot.git

# 2. Run the Hub setup script
sudo bash jupyterhub-pilot/scripts/install_hub.sh
```

When prompted, enter:
- Your **Hub's Private IP** (shown in the EC2 console)
- Your **JupyterHub admin username** (e.g. `wajoud`) — this must match your login exactly
- Your **Worker's Private IP**

**⚠️ Important:** At the end of the script, it will print out a public SSH key starting with `ssh-ed25519 ...`. Copy this key — you will need it in the next step.

---

## 🛠️ 3. EC2 #2 (Worker) Setup

SSH into your Worker instance in a new terminal tab and run:

```bash
# 1. Clone the repo
git clone https://github.com/wajoud/jupyterhub-pilot.git

# 2. Run the Worker setup script
sudo bash jupyterhub-pilot/scripts/install_worker.sh
```

When prompted:
- Enter the **OS admin username** (e.g. `ubuntu`) — this grants the Hub SSH access to provision the worker
- Paste the **SSH public key** printed at the end of the Hub setup
- Enter the **Hub's Private IP**

---

## 🔒 4. HTTPS with Caddy

The Hub's login cookie is marked `Secure`, so browsers only keep it over HTTPS — over plain `http://` the login loops forever. [Caddy](https://caddyserver.com/) gets and renews a free Let's Encrypt certificate automatically.

On the **Hub VM**:

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

# Replace the domain and the Hub's PRIVATE IP:
echo 'hub.example.com {
    encode zstd gzip
    reverse_proxy <HUB-PRIVATE-IP>:8000
}' | sudo tee /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

No domain yet? See the no-domain test setup (self-signed certificate on the public IP) in the README's *HTTPS with Caddy* section.

In the Google Cloud console, set the OAuth client's **Authorized redirect URI** to `https://hub.example.com/hub/oauth_callback`.

---

## 🚦 5. Start JupyterHub

On the **Hub VM**:

```bash
export CLIENT_ID=... CLIENT_SECRET=...
export OAUTH_CALLBACK_URL=https://hub.example.com/hub/oauth_callback
export JUPYTERPILOT_AGENT_TOKEN=$(cat /etc/jupyterhub-pilot/agent_token)
python3 -m jupyterhub -f /opt/jupyterhub-pilot/jupyterhub_config.py
```

> ⚠️ Never start the Hub with `DummyAuthenticator` on a reachable network: anyone could log in as any user, including the admin.

Then open `https://hub.example.com` and:
1. Log in with your Google admin account
2. Go to **Admin** → **Groups** → create `team_alpha` → add your user
3. Click **Start My Server**

---

## 🔁 Adding More Users (Per-User Isolation)

You **do not** need to manually create OS accounts or run setup scripts for new users!

JupyterHub-Pilot includes **Just-In-Time (JIT) Provisioning**. 
When a new user (like `alice`) logs in for the very first time, the Hub automatically SSHes into the assigned Worker EC2 and:
1. Creates the `alice` OS account
2. Enables systemd lingering (so her notebooks stay alive after SSH closes)
3. Creates her notebook folder (Jupyter and the AI magics are already installed once for all users by `install_worker.sh`)
4. Injects the Hub's SSH key so the Hub can manage her server

All you need to do is add the user to the correct group in the JupyterHub Admin panel, and they can immediately start their server.
