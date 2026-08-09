# ☁️ AWS EC2 Automated Deployment Guide

This guide walks through deploying JupyterHub-Pilot across two EC2 instances (a Hub node and a Worker node) using our automated bash setup scripts.

## 🏗️ 1. AWS Infrastructure Setup

### Instance Types
- **Recommended**: `t4g.small` (2 vCPU, 2 GiB Memory)
- **Architecture**: 64-bit Arm (Graviton)
- **OS**: Ubuntu 26.04 LTS
- **Storage**: 15 GiB `gp3` per instance (stays within the 30 GB AWS Free Tier limit).

### Security Groups
1. **EC2 #1 (The Hub)**:
   - Allow `SSH (Port 22)` from Anywhere.
   - Allow `Custom TCP (Port 8000)` from Anywhere (this is the JupyterHub web UI).
   - Allow `Custom TCP (Port 8081)` from the **Worker's Private IP** (Allows the Worker to authenticate with the Hub API on startup).
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
- Enter your **JupyterHub username** — must exactly match what you typed in the Hub setup (e.g. `wajoud`)
- Paste the **SSH public key** printed at the end of the Hub setup
- Enter the **Hub's Private IP**

---

## 🚦 4. Start JupyterHub

On the **Hub VM**:

```bash
python3 -m jupyterhub -f /opt/jupyterhub-pilot/jupyterhub_config.py \
    --JupyterHub.authenticator_class=dummy \
    --DummyAuthenticator.password=test
```

Then open `http://<HUB-PUBLIC-IP>:8000` and:
1. Log in with your username and password `test`
2. Go to **Admin** → **Groups** → create `team_alpha` → add your user
3. Click **Start My Server**

---

## 🔁 Adding More Users (Per-User Isolation)

You **do not** need to manually create OS accounts or run setup scripts for new users!

JupyterHub-Pilot includes **Just-In-Time (JIT) Provisioning**. 
When a new user (like `alice`) logs in for the very first time, the Hub automatically SSHes into the assigned Worker EC2 and:
1. Creates the `alice` OS account
2. Enables systemd lingering (so her notebooks stay alive after SSH closes)
3. Provisions her virtual environment and installs the AI magics
4. Injects the Hub's SSH key so the Hub can manage her server

All you need to do is add the user to the correct group in the JupyterHub Admin panel, and they can immediately start their server.
