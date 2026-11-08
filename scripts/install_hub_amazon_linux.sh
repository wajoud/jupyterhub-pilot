#!/usr/bin/env bash
# install_hub_amazon_linux.sh - Hub VM setup on Amazon Linux 2023 for JupyterHub-Pilot
set -e

echo "🚀 JupyterHub-Pilot Hub VM Setup (Amazon Linux 2023)"
echo "────────────────────────────────────"

# Ensure script is run with sudo
if [ "$EUID" -ne 0 ]; then
  echo "❌ Please run this script with sudo: sudo bash install_hub_amazon_linux.sh"
  exit 1
fi

echo "📦 1. Installing OS dependencies..."
# curl and gpg are preinstalled as curl-minimal / gnupg2-minimal; installing the
# full packages conflicts with them, so they are left out.
dnf install -y python3-pip sqlite nodejs npm jq git openssl
npm install -g configurable-http-proxy

echo "🔐 1b. Installing HashiCorp Vault (optional)..."
dnf install -y dnf-plugins-core
dnf config-manager --add-repo https://rpm.releases.hashicorp.com/AmazonLinux/hashicorp.repo \
    && dnf install -y vault \
    || echo "⚠️  Vault install failed — skipping. You can install it later with: sudo dnf install vault"

echo "🐍 2. Installing Python dependencies..."
# AL2023's pip (21.x) has no --break-system-packages and the system Python is
# not marked externally managed. --ignore-installed skips RPM-owned packages
# that pip cannot uninstall.
pip3 install jupyterhub oauthenticator --ignore-installed
# If running from within a cloned repo, install it. Otherwise, clone it.
if [ -d "/opt/jupyterhub-pilot" ]; then
    echo "ℹ️  /opt/jupyterhub-pilot already exists. Updating..."
    cd /opt/jupyterhub-pilot
    # --autostash: this script rewrites tracked files (hub_settings.json, user_mapping.json)
    git -c safe.directory=/opt/jupyterhub-pilot pull --autostash
else
    echo "📥 Cloning JupyterHub-Pilot..."
    # Install the same branch as the clone this script runs from (main if unknown).
    BRANCH=$(git -c safe.directory='*' -C "$(dirname "$0")" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)
    git clone --branch "$BRANCH" https://github.com/wajoud/jupyterhub-pilot.git /opt/jupyterhub-pilot
    chown -R $SUDO_USER:$SUDO_USER /opt/jupyterhub-pilot
fi
# [hub] pulls in paramiko, which the spawner needs for SSH.
pip3 install -e "/opt/jupyterhub-pilot[hub]"

echo "🗄️ 3. Setting up Database..."
mkdir -p /var/lib/jupyterhub
chown -R ${SUDO_USER:-ec2-user}:${SUDO_USER:-ec2-user} /var/lib/jupyterhub

echo "🔑 4. Generating Spawner SSH Key..."
mkdir -p /opt/jupyterhub-pilot/keys
if [ ! -f "/opt/jupyterhub-pilot/keys/worker" ]; then
    ssh-keygen -t ed25519 -f /opt/jupyterhub-pilot/keys/worker -N "" -q
    echo "✅ New SSH key generated."
else
    echo "ℹ️  SSH key already exists."
fi
# The Hub runs as the calling user (ec2-user), not root — fix key permissions
RUNNING_USER="${SUDO_USER:-ec2-user}"
chown "${RUNNING_USER}:${RUNNING_USER}" /opt/jupyterhub-pilot/keys/worker /opt/jupyterhub-pilot/keys/worker.pub
chmod 600 /opt/jupyterhub-pilot/keys/worker

# Interactive configuration
echo "⚙️  5. Configuring JupyterHub-Pilot"
read -p "Enter the Hub's Private IP (e.g. 172.31.x.x): " HUB_IP
# Google login drops the domain: admin@example.com logs in as "admin".
read -p "Enter your admin username, the part before @ (e.g. admin for admin@example.com): " ADMIN_USER
ADMIN_USER="${ADMIN_USER%%@*}"
read -p "Enter the Worker's Private IP (e.g. 172.31.y.y): " WORKER_IP

# Upsert team_alpha so a re-run keeps teams added later with add_worker.sh.
MAPPING=/opt/jupyterhub-pilot/user_mapping.json
# The tracked file holds sample teams: start empty unless it was edited by an earlier run.
git -c safe.directory=/opt/jupyterhub-pilot -C /opt/jupyterhub-pilot diff --quiet -- user_mapping.json && echo '{}' > "$MAPPING"
jq --arg ip "$WORKER_IP" \
    '.team_alpha = {server_ip: $ip, server_ssh_key: "/opt/jupyterhub-pilot/keys/worker", admin_ssh_user: "ec2-user"}' \
    "$MAPPING" > "$MAPPING.tmp" && mv "$MAPPING.tmp" "$MAPPING"

cat > /opt/jupyterhub-pilot/hub_settings.json << EOF
{
  "hub_ip": "$HUB_IP",
  "proxy_api_url": "http://$HUB_IP:5432",
  "hub_api_url": "http://$HUB_IP:8081/hub/api",
  "hub_bind_url": "http://$HUB_IP:8081",
  "hub_port": 8000,
  "hosted_domain": "example.com",
  "admin_users": ["$ADMIN_USER"],
  "mapping_file": "/opt/jupyterhub-pilot/user_mapping.json",
  "db_path": "/var/lib/jupyterhub/jupyterhub_pilot_state.db",
  "resource_limits": {
    "memory_max": "1G",
    "cpu_quota": "50%"
  },
  "vault_enabled": false,
  "vault_secret_path": "secret/jupyterhub-pilot"
}
EOF

echo "🔐 5b. Generating the metrics agent token..."
mkdir -p /etc/jupyterhub-pilot
if [ ! -f /etc/jupyterhub-pilot/agent_token ]; then
    openssl rand -hex 32 > /etc/jupyterhub-pilot/agent_token
fi
chown "${RUNNING_USER}:${RUNNING_USER}" /etc/jupyterhub-pilot/agent_token
chmod 600 /etc/jupyterhub-pilot/agent_token

echo "🌱 6. Seeding the SQLite database..."
python3 /opt/jupyterhub-pilot/jupyterhub_pilot/seed_sqlite.py --db /var/lib/jupyterhub/jupyterhub_pilot_state.db --mapping /opt/jupyterhub-pilot/user_mapping.json
# Ensure the DB file is writable by the ec2-user user (Hub runs as ec2-user, not root)
chown -R ${SUDO_USER:-ec2-user}:${SUDO_USER:-ec2-user} /var/lib/jupyterhub

echo "⚙️  7. Writing the Hub systemd service (not started yet)..."
# Secrets live in a root-only env file; an existing one is kept so re-runs don't wipe them.
if [ ! -f /etc/jupyterhub-pilot/hub.env ]; then
    ( umask 077; cat > /etc/jupyterhub-pilot/hub.env << EOF
# Google login (production):
CLIENT_ID=
CLIENT_SECRET=
OAUTH_CALLBACK_URL=https://hub.example.com/hub/oauth_callback
JUPYTERPILOT_AGENT_TOKEN=$(cat /etc/jupyterhub-pilot/agent_token)
# TEST ONLY, no Google or domain: set both and leave the Google lines empty.
# Any user name logs in with the first password (8+ characters); admins with the second (32+ characters).
#JUPYTERPILOT_TEST_PASSWORD=
#JUPYTERPILOT_TEST_ADMIN_PASSWORD=$(openssl rand -hex 16)
EOF
    )
fi
cat > /etc/systemd/system/jupyterhub.service << EOF
[Unit]
Description=JupyterHub-Pilot Hub
After=network.target

[Service]
User=${RUNNING_USER}
WorkingDirectory=/opt/jupyterhub-pilot
EnvironmentFile=/etc/jupyterhub-pilot/hub.env
ExecStart=/usr/bin/python3 -m jupyterhub -f /opt/jupyterhub-pilot/jupyterhub_config.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

echo ""
echo "🎉 Hub Setup Complete!"
echo "────────────────────────────────────"
echo "⚠️  CRITICAL NEXT STEP: Copy the key below and paste it into the Worker VM when running the worker install script:"
echo ""
cat /opt/jupyterhub-pilot/keys/worker.pub
echo ""
echo "⚠️  Paste this metrics agent token into the worker install script on each Worker:"
cat /etc/jupyterhub-pilot/agent_token
echo ""
echo "Then:"
echo "  1. sudo nano /etc/jupyterhub-pilot/hub.env   (Google client, or the TEST ONLY passwords)"
echo "  2. Google login: set hosted_domain in /opt/jupyterhub-pilot/hub_settings.json"
echo "  3. sudo systemctl enable --now jupyterhub    (logs: sudo journalctl -u jupyterhub -f)"
echo "  4. Put Caddy in front for HTTPS (README → HTTPS with Caddy)"
echo "  5. More workers: sudo bash /opt/jupyterhub-pilot/scripts/add_worker.sh team_beta <worker-private-ip> ec2-user"
