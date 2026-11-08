#!/usr/bin/env bash
# add_worker.sh - Register a team's worker VM on the Hub (run on the Hub).
#   sudo bash add_worker.sh <team_name> <worker_private_ip> [admin_ssh_user]
# Adds or updates the team in user_mapping.json, re-seeds SQLite and tests SSH.
# No Hub restart needed. admin_ssh_user defaults to ubuntu (use ec2-user on Amazon Linux).
set -e

TEAM="$1"; WORKER_IP="$2"; ADMIN_SSH_USER="${3:-ubuntu}"
MAPPING=/opt/jupyterhub-pilot/user_mapping.json
DB=/var/lib/jupyterhub/jupyterhub_pilot_state.db
KEY=/opt/jupyterhub-pilot/keys/worker
RUNNING_USER="${SUDO_USER:-$(stat -c %U /var/lib/jupyterhub)}"

if [ "$EUID" -ne 0 ] || [ -z "$TEAM" ] || [ -z "$WORKER_IP" ]; then
    echo "Usage: sudo bash add_worker.sh <team_name> <worker_private_ip> [admin_ssh_user]"
    exit 1
fi
# The team name becomes a JupyterHub group name; keep it simple.
if ! [[ "$TEAM" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "❌ Team name may only contain letters, digits, _ and -"
    exit 1
fi

[ -f "$MAPPING" ] || echo '{}' > "$MAPPING"
jq --arg t "$TEAM" --arg ip "$WORKER_IP" --arg key "$KEY" --arg u "$ADMIN_SSH_USER" \
    '.[$t] = {server_ip: $ip, server_ssh_key: $key, admin_ssh_user: $u}' \
    "$MAPPING" > "$MAPPING.tmp" && mv "$MAPPING.tmp" "$MAPPING"
echo "✅ $TEAM → $WORKER_IP written to $MAPPING"

sudo -u "$RUNNING_USER" python3 /opt/jupyterhub-pilot/jupyterhub_pilot/seed_sqlite.py --db "$DB" --mapping "$MAPPING"

echo "🔌 Testing SSH from the Hub to $ADMIN_SSH_USER@$WORKER_IP ..."
if PORT=$(sudo -u "$RUNNING_USER" ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 \
        "$ADMIN_SSH_USER@$WORKER_IP" 'python3 /opt/jupyterhub-pilot/get_port.py'); then
    echo "✅ SSH works (worker offered port $PORT)."
    echo "Next: in the Hub UI, Admin → Groups → create '$TEAM' and add its users."
else
    echo "❌ SSH failed. Check the worker ran install_worker.sh with this Hub's public key,"
    echo "   and that the security group allows the Hub to reach the worker."
    exit 1
fi
