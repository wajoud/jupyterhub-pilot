#!/usr/bin/env bash
# Worker VM setup. Run through `sudo jupyterhub-pilot install-worker` after
# `pip install "jupyterhub-pilot[worker,ai]"` (system-wide, so every user's
# notebook server finds jupyterhub-singleuser and the AI magics).
# Ubuntu (apt) and Amazon Linux 2023 (dnf).
set -e

PYTHON="${PYTHON:-python3}"

echo "JupyterHub-Pilot: worker setup"
if [ "$EUID" -ne 0 ]; then
    echo "❌ Run with sudo: sudo jupyterhub-pilot install-worker"
    exit 1
fi

echo "1. OS packages"
if command -v apt-get >/dev/null; then
    apt-get update -y
    apt-get install -y net-tools psmisc acl git
elif command -v dnf >/dev/null; then
    dnf install -y net-tools psmisc acl git
else
    echo "❌ Needs apt (Ubuntu) or dnf (Amazon Linux 2023)."
    exit 1
fi

# NVIDIA GPU (PCI vendor 10de): the driver is installed only on a worker that has one.
GPU_REBOOT=""
if grep -qsi 0x10de /sys/bus/pci/devices/*/vendor; then
    if nvidia-smi >/dev/null 2>&1; then
        echo "✅ NVIDIA GPU found, driver working."
    elif command -v apt-get >/dev/null; then
        echo "NVIDIA GPU found without a working driver: installing Ubuntu's recommended one (several minutes)."
        # Plain `install`, not `--gpgpu`: the server-only set leaves out nvidia-smi, which the metrics agent reads.
        if apt-get install -y ubuntu-drivers-common && ubuntu-drivers install; then
            GPU_REBOOT=1
        else
            echo "⚠️  The GPU driver did not install. The worker works without it; notebooks just cannot use the GPU."
            echo "   Install it by hand later, or start from an AWS GPU image (Deep Learning AMI)."
        fi
    else
        echo "⚠️  NVIDIA GPU found without a driver. On Amazon Linux install it yourself:"
        echo "   https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/install-nvidia-driver.html"
    fi
else
    echo "No NVIDIA GPU on this machine: no driver needed."
fi

if ! command -v jupyterhub-singleuser >/dev/null; then
    echo "❌ jupyterhub-singleuser not found. Install the worker extras system-wide first:"
    echo "   sudo pip3 install --break-system-packages \"jupyterhub-pilot[worker,ai]\""
    exit 1
fi

echo "2. Hub access"
read -p "OS admin user the Hub logs in as [${SUDO_USER:-ubuntu}]: " ADMIN_USER
ADMIN_USER="${ADMIN_USER:-${SUDO_USER:-ubuntu}}"
if ! id "$ADMIN_USER" &>/dev/null; then
    echo "❌ User $ADMIN_USER does not exist on this machine."
    exit 1
fi
read -p "Hub's public SSH key (printed by install-hub): " HUB_PUB_KEY
if [ -z "$HUB_PUB_KEY" ]; then
    echo "❌ The SSH key cannot be empty."
    exit 1
fi
ADMIN_HOME=$(getent passwd "$ADMIN_USER" | cut -d: -f6)
mkdir -p "$ADMIN_HOME/.ssh"
touch "$ADMIN_HOME/.ssh/authorized_keys"
grep -qxF "$HUB_PUB_KEY" "$ADMIN_HOME/.ssh/authorized_keys" || echo "$HUB_PUB_KEY" >> "$ADMIN_HOME/.ssh/authorized_keys"
chown -R "$ADMIN_USER:$ADMIN_USER" "$ADMIN_HOME/.ssh"
chmod 600 "$ADMIN_HOME/.ssh/authorized_keys"

echo "3. Metrics agent"
read -p "Hub's private IP: " HUB_IP
read -p "Metrics agent token (printed by install-hub): " AGENT_TOKEN
if [ -z "$AGENT_TOKEN" ]; then
    echo "❌ The agent token cannot be empty."
    exit 1
fi
mkdir -p /etc/jupyterhub-pilot
( umask 077; echo "JUPYTERPILOT_AGENT_TOKEN=$AGENT_TOKEN" > /etc/jupyterhub-pilot/agent.env )
cat > /etc/systemd/system/jupyterhub_pilot-metrics.service << EOF
[Unit]
Description=JupyterHub-Pilot Metrics Agent
After=network.target

[Service]
Type=simple
User=root
EnvironmentFile=/etc/jupyterhub-pilot/agent.env
ExecStart=$PYTHON -m jupyterhub_pilot.metrics_agent --hub ws://$HUB_IP:8000/hub/monitoring/ws
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable jupyterhub_pilot-metrics
systemctl restart jupyterhub_pilot-metrics
# `jupyterhub-pilot upgrade` runs only the steps newer than this.
"$PYTHON" -c "import jupyterhub_pilot as p; print(p.__version__)" > /etc/jupyterhub-pilot/version

echo ""
echo "✅ Worker set up. Agent logs: sudo journalctl -u jupyterhub_pilot-metrics -f"
echo "On the Hub: sudo jupyterhub-pilot add-worker <team> <this worker's private IP> $ADMIN_USER"
if [ -n "$GPU_REBOOT" ]; then
    echo "⚠️  GPU driver installed: reboot this worker once (sudo reboot), then check with nvidia-smi."
fi
