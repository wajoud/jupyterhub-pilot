#!/usr/bin/env bash
# install_worker.sh - Automates the Worker VM setup for JupyterHub-Pilot
set -e

echo "🚀 JupyterHub-Pilot Worker VM Setup"
echo "────────────────────────────────────"

# Ensure script is run with sudo
if [ "$EUID" -ne 0 ]; then
  echo "❌ Please run this script with sudo: sudo bash install_worker.sh"
  exit 1
fi

echo "📦 1. Installing OS dependencies..."
apt update -y
apt install -y python3-pip python3-venv net-tools psmisc

echo "📥 2. Cloning JupyterHub-Pilot repository..."
if [ -d "/opt/jupyterhub-pilot" ]; then
    echo "ℹ️  /opt/jupyterhub-pilot already exists. Updating..."
    cd /opt/jupyterhub-pilot && git pull && cd -
else
    git clone https://github.com/wajoud/jupyterhub-pilot.git /opt/jupyterhub-pilot
fi

# Ask for admin username
read -p "Enter the OS admin username to grant Hub access (e.g. ubuntu): " ADMIN_USER

if [ -z "$ADMIN_USER" ]; then
    echo "❌ Username cannot be empty. Setup aborted."
    exit 1
fi

echo "👤 3. Verifying admin user: $ADMIN_USER..."
if id "$ADMIN_USER" &>/dev/null; then
    echo "ℹ️  User $ADMIN_USER exists."
else
    echo "❌ User $ADMIN_USER does not exist. Please run this script with an existing user."
    exit 1
fi

echo "🔑 4. Configuring SSH access for Hub Provisioner..."
mkdir -p /home/$ADMIN_USER/.ssh
read -p "Paste the public SSH key from the Hub setup step: " HUB_PUB_KEY

if [ -z "$HUB_PUB_KEY" ]; then
    echo "❌ SSH key cannot be empty. Setup aborted."
    exit 1
fi

echo "$HUB_PUB_KEY" > /home/$ADMIN_USER/.ssh/authorized_keys
chown -R $ADMIN_USER:$ADMIN_USER /home/$ADMIN_USER/.ssh
chmod 600 /home/$ADMIN_USER/.ssh/authorized_keys

echo "🛡️ 5. Installing global dependencies for Metrics Agent..."
pip3 install psutil websockets --break-system-packages # Install globally for the metrics agent

echo "🔌 6. Setting up Port Allocation script for JIT provisioner..."
cat > /opt/jupyterhub-pilot/get_port.py << 'EOF'
import socket
s = socket.socket()
s.bind(('', 0))
port = s.getsockname()[1]
s.close()
print(port)
EOF
chmod +x /opt/jupyterhub-pilot/get_port.py

echo "📊 8. Configuring the Metrics Agent systemd service..."
read -p "Enter the Hub's Private IP (to connect the agent to): " HUB_IP

cat > /etc/systemd/system/jupyterhub_pilot-metrics.service << EOF
[Unit]
Description=JupyterHub-Pilot Metrics Agent
After=network.target

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 /opt/jupyterhub-pilot/jupyterhub_pilot/metrics_agent.py --hub ws://$HUB_IP:8000/monitoring/ws
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable jupyterhub_pilot-metrics
systemctl restart jupyterhub_pilot-metrics

echo ""
echo "🎉 Worker Setup Complete!"
echo "────────────────────────────────────"
echo "Isolated user '$JUPYTER_USER' is ready on this Worker VM."
echo "The metrics agent is running in the background. Check logs with:"
echo "sudo journalctl -u jupyterhub_pilot-metrics -f"
