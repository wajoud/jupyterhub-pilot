#!/usr/bin/env bash
# install_hub.sh - Automates the Hub VM setup for JupyterHub-Pilot
set -e

echo "🚀 JupyterHub-Pilot Hub VM Setup"
echo "────────────────────────────────────"

# Ensure script is run with sudo
if [ "$EUID" -ne 0 ]; then
  echo "❌ Please run this script with sudo: sudo bash install_hub.sh"
  exit 1
fi

echo "📦 1. Installing OS dependencies..."
apt update -y
apt install -y python3-pip sqlite3 nodejs npm curl jq gpg
npm install -g configurable-http-proxy

echo "🔐 1b. Installing HashiCorp Vault (optional)..."
# HashiCorp only publishes packages up to Ubuntu 24.04 (noble).
# Fall back to noble if running on a newer release (e.g. 26.04 resolute).
UBUNTU_CODENAME=$(lsb_release -cs)
HASHICORP_CODENAME="noble"
case "$UBUNTU_CODENAME" in
    focal|jammy|noble) HASHICORP_CODENAME="$UBUNTU_CODENAME" ;;
    *) echo "ℹ️  Ubuntu '$UBUNTU_CODENAME' not yet in HashiCorp repo — using 'noble' packages." ;;
esac
wget -qO- https://apt.releases.hashicorp.com/gpg | gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com ${HASHICORP_CODENAME} main" \
    | tee /etc/apt/sources.list.d/hashicorp.list > /dev/null
apt update -y
apt install -y vault || echo "⚠️  Vault install failed — skipping. You can install it later with: sudo apt install vault"

echo "🐍 2. Installing Python dependencies..."
# Ubuntu 26 ships several packages (typing_extensions, etc.) via apt without
# RECORD files, so pip cannot uninstall them. --ignore-installed skips the
# version conflict check entirely and installs what we need on top.
pip3 install jupyterhub oauthenticator --break-system-packages --ignore-installed
# If running from within a cloned repo, install it. Otherwise, clone it.
if [ -d "/opt/jupyterhub-pilot" ]; then
    echo "ℹ️  /opt/jupyterhub-pilot already exists. Updating..."
    cd /opt/jupyterhub-pilot
    git pull
else
    echo "📥 Cloning JupyterHub-Pilot..."
    git clone https://github.com/wajoud/jupyterhub-pilot.git /opt/jupyterhub-pilot
    chown -R $SUDO_USER:$SUDO_USER /opt/jupyterhub-pilot
fi
pip3 install -e /opt/jupyterhub-pilot --break-system-packages

echo "🗄️ 3. Setting up Database..."
mkdir -p /var/lib/jupyterhub
chown -R ${SUDO_USER:-ubuntu}:${SUDO_USER:-ubuntu} /var/lib/jupyterhub

echo "🔑 4. Generating Spawner SSH Key..."
mkdir -p /opt/jupyterhub-pilot/keys
if [ ! -f "/opt/jupyterhub-pilot/keys/worker" ]; then
    ssh-keygen -t ed25519 -f /opt/jupyterhub-pilot/keys/worker -N "" -q
    echo "✅ New SSH key generated."
else
    echo "ℹ️  SSH key already exists."
fi
# The Hub runs as the calling user (ubuntu), not root — fix key permissions
RUNNING_USER="${SUDO_USER:-ubuntu}"
chown "${RUNNING_USER}:${RUNNING_USER}" /opt/jupyterhub-pilot/keys/worker /opt/jupyterhub-pilot/keys/worker.pub
chmod 600 /opt/jupyterhub-pilot/keys/worker

# Interactive configuration
echo "⚙️  5. Configuring JupyterHub-Pilot"
read -p "Enter the Hub's Private IP (e.g. 172.31.x.x): " HUB_IP
read -p "Enter your admin username (e.g. admin@example.com): " ADMIN_USER
read -p "Enter the Worker's Private IP (e.g. 172.31.y.y): " WORKER_IP

cat > /opt/jupyterhub-pilot/user_mapping.json << EOF
{
  "team_alpha": {
    "server_ip": "$WORKER_IP",
    "server_ssh_key": "/opt/jupyterhub-pilot/keys/worker",
    "admin_ssh_user": "ubuntu"
  }
}
EOF

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

echo "🌱 6. Seeding the SQLite database..."
python3 /opt/jupyterhub-pilot/jupyterhub_pilot/seed_sqlite.py --db /var/lib/jupyterhub/jupyterhub_pilot_state.db --mapping /opt/jupyterhub-pilot/user_mapping.json
# Ensure the DB file is writable by the ubuntu user (Hub runs as ubuntu, not root)
chown -R ${SUDO_USER:-ubuntu}:${SUDO_USER:-ubuntu} /var/lib/jupyterhub

echo ""
echo "🎉 Hub Setup Complete!"
echo "────────────────────────────────────"
echo "⚠️  CRITICAL NEXT STEP: Copy the key below and paste it into the Worker VM when running install_worker.sh:"
echo ""
cat /opt/jupyterhub-pilot/keys/worker.pub
echo ""
echo "Start the Hub anytime using:"
echo "python3 -m jupyterhub -f /opt/jupyterhub-pilot/jupyterhub_config.py --JupyterHub.authenticator_class=dummy --DummyAuthenticator.password=test"
