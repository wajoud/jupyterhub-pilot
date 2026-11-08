#!/usr/bin/env bash
# Hub VM setup. Run through `sudo jupyterhub-pilot install-hub` after
# `pip install "jupyterhub-pilot[hub]"`; the CLI passes PYTHON (the interpreter
# that has the package). Ubuntu (apt) and Amazon Linux 2023 (dnf).
set -e

CONF=/opt/jupyterhub-pilot
PYTHON="${PYTHON:-python3}"
RUNNING_USER="${SUDO_USER:-$(id -un)}"  # the Hub runs as this user, not root

echo "JupyterHub-Pilot: Hub setup"
if [ "$EUID" -ne 0 ]; then
    echo "❌ Run with sudo: sudo jupyterhub-pilot install-hub"
    exit 1
fi

echo "1. OS packages"
if command -v apt-get >/dev/null; then
    apt-get update -y
    apt-get install -y sqlite3 nodejs npm curl jq gpg openssl
    # Vault (optional). HashiCorp publishes up to Ubuntu 24.04 (noble).
    CODENAME=$(lsb_release -cs 2>/dev/null || echo noble)
    case "$CODENAME" in focal|jammy|noble) ;; *) CODENAME=noble ;; esac
    { curl -fsSL https://apt.releases.hashicorp.com/gpg | gpg --dearmor --yes -o /usr/share/keyrings/hashicorp-archive-keyring.gpg \
      && echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $CODENAME main" > /etc/apt/sources.list.d/hashicorp.list \
      && apt-get update -y && apt-get install -y vault; } || echo "ℹ️  Vault not installed (optional)."
elif command -v dnf >/dev/null; then
    # curl and gpg come preinstalled as -minimal packages; the full ones conflict.
    dnf install -y sqlite nodejs npm jq openssl
    { dnf install -y dnf-plugins-core \
      && dnf config-manager --add-repo https://rpm.releases.hashicorp.com/AmazonLinux/hashicorp.repo \
      && dnf install -y vault; } || echo "ℹ️  Vault not installed (optional)."
else
    echo "❌ Needs apt (Ubuntu) or dnf (Amazon Linux 2023)."
    exit 1
fi
npm install -g configurable-http-proxy

echo "2. Config folder $CONF and state folder /var/lib/jupyterhub"
mkdir -p "$CONF/keys" /var/lib/jupyterhub /etc/jupyterhub-pilot
if [ ! -f "$CONF/keys/worker" ]; then
    ssh-keygen -t ed25519 -f "$CONF/keys/worker" -N "" -q
fi
if [ ! -f /etc/jupyterhub-pilot/agent_token ]; then
    openssl rand -hex 32 > /etc/jupyterhub-pilot/agent_token
fi

echo "3. Settings"
read -p "Hub's private IP (e.g. 172.31.x.x): " HUB_IP
# Google login drops the domain: admin@example.com logs in as "admin".
read -p "Admin username, the part before @ (e.g. admin for admin@example.com): " ADMIN_USER
ADMIN_USER="${ADMIN_USER%%@*}"
read -p "First worker's private IP (team_alpha): " WORKER_IP

# Keep teams added earlier with add-worker; (re)set team_alpha.
MAPPING="$CONF/user_mapping.json"
[ -s "$MAPPING" ] || echo '{}' > "$MAPPING"
jq --arg ip "$WORKER_IP" --arg key "$CONF/keys/worker" --arg u "$RUNNING_USER" \
    '.team_alpha = {server_ip: $ip, server_ssh_key: $key, admin_ssh_user: $u}' \
    "$MAPPING" > "$MAPPING.tmp" && mv "$MAPPING.tmp" "$MAPPING"

cat > "$CONF/hub_settings.json" << EOF
{
  "hub_ip": "$HUB_IP",
  "proxy_api_url": "http://$HUB_IP:5432",
  "hub_api_url": "http://$HUB_IP:8081/hub/api",
  "hub_bind_url": "http://$HUB_IP:8081",
  "hub_port": 8000,
  "hosted_domain": "example.com",
  "admin_users": ["$ADMIN_USER"],
  "mapping_file": "$MAPPING",
  "db_path": "/var/lib/jupyterhub/jupyterhub_pilot_state.db",
  "resource_limits": {
    "memory_max": "1G",
    "cpu_quota": "50%"
  },
  "vault_enabled": false,
  "vault_secret_path": "secret/jupyterhub-pilot"
}
EOF

echo "4. Team database"
"$PYTHON" -m jupyterhub_pilot.seed_sqlite --db /var/lib/jupyterhub/jupyterhub_pilot_state.db --mapping "$MAPPING"
# The Hub (not root) writes its cookie secret and DB here and reads the key.
chown -R "$RUNNING_USER:$RUNNING_USER" "$CONF" /var/lib/jupyterhub /etc/jupyterhub-pilot/agent_token
chmod 600 "$CONF/keys/worker" /etc/jupyterhub-pilot/agent_token

echo "5. systemd service (not started yet)"
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
PKG_CONFIG=$("$PYTHON" -c "import jupyterhub_pilot.paths as p, os; print(os.path.join(p.PACKAGE_DIR, 'jupyterhub_config.py'))")
cat > /etc/systemd/system/jupyterhub.service << EOF
[Unit]
Description=JupyterHub-Pilot Hub
After=network.target

[Service]
User=$RUNNING_USER
WorkingDirectory=$CONF
Environment=JUPYTERHUB_PILOT_DIR=$CONF
EnvironmentFile=/etc/jupyterhub-pilot/hub.env
ExecStart=$PYTHON -m jupyterhub -f $PKG_CONFIG
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

echo ""
echo "✅ Hub set up."
echo ""
echo "On each worker:  sudo jupyterhub-pilot install-worker"
echo "It asks for this public key:"
cat "$CONF/keys/worker.pub"
echo "and this metrics agent token:"
cat /etc/jupyterhub-pilot/agent_token
echo ""
echo "Then:"
echo "  1. sudo nano /etc/jupyterhub-pilot/hub.env      (Google client, or the TEST ONLY passwords)"
echo "  2. Google login: set hosted_domain in $CONF/hub_settings.json"
echo "  3. sudo systemctl enable --now jupyterhub       (logs: sudo journalctl -u jupyterhub -f)"
echo "  4. Put Caddy in front for HTTPS (README: HTTPS with Caddy)"
echo "  5. More workers: sudo jupyterhub-pilot add-worker team_beta <worker-private-ip>"
