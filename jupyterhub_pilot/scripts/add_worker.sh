#!/usr/bin/env bash
# Register a team's worker VM on the Hub. Run on the Hub:
#   sudo jupyterhub-pilot add-worker <team_name> <worker_private_ip> [admin_ssh_user]
# Adds or updates the team in user_mapping.json, re-seeds SQLite and tests SSH.
# No Hub restart needed. admin_ssh_user defaults to the Hub's own user
# (ubuntu on Ubuntu, ec2-user on Amazon Linux).
set -e

PYTHON="${PYTHON:-python3}"
CONF=/opt/jupyterhub-pilot
MAPPING=$CONF/user_mapping.json
DB=/var/lib/jupyterhub/jupyterhub_pilot_state.db
KEY=$CONF/keys/worker
RUNNING_USER="${SUDO_USER:-$(stat -c %U /var/lib/jupyterhub 2>/dev/null || id -un)}"
TEAM="$1"; WORKER_IP="$2"; ADMIN_SSH_USER="${3:-$RUNNING_USER}"

if [ "$EUID" -ne 0 ] || [ -z "$TEAM" ] || [ -z "$WORKER_IP" ]; then
    echo "Usage: sudo jupyterhub-pilot add-worker <team_name> <worker_private_ip> [admin_ssh_user]"
    exit 1
fi
# The team name becomes a JupyterHub group name; keep it simple.
if ! [[ "$TEAM" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "❌ Team name may only contain letters, digits, _ and -"
    exit 1
fi

# Only the Hub has the spawner key and the state DB; on a worker, stop before touching anything.
if [ ! -f "$KEY" ] || [ ! -d "$(dirname "$DB")" ]; then
    echo "❌ This is not the Hub ($KEY or $(dirname "$DB") is missing). Run add-worker on the Hub VM."
    exit 1
fi

[ -f "$MAPPING" ] || echo '{}' > "$MAPPING"
jq --arg t "$TEAM" --arg ip "$WORKER_IP" --arg key "$KEY" --arg u "$ADMIN_SSH_USER" \
    '.[$t] = {server_ip: $ip, server_ssh_key: $key, admin_ssh_user: $u}' \
    "$MAPPING" > "$MAPPING.tmp" && mv "$MAPPING.tmp" "$MAPPING"
chown "$RUNNING_USER:$RUNNING_USER" "$MAPPING"
echo "✅ $TEAM → $WORKER_IP written to $MAPPING"

sudo -u "$RUNNING_USER" "$PYTHON" -m jupyterhub_pilot.seed_sqlite --db "$DB" --mapping "$MAPPING"

echo "Testing SSH from the Hub to $ADMIN_SSH_USER@$WORKER_IP ..."
if sudo -u "$RUNNING_USER" ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 \
        "$ADMIN_SSH_USER@$WORKER_IP" 'command -v jupyterhub-singleuser' >/dev/null; then
    echo "✅ SSH works and the worker has jupyterhub-singleuser."
    echo "Next: in the Hub UI, Admin → Groups → create '$TEAM' and add its users."
else
    echo "❌ SSH failed, or the worker is missing jupyterhub-singleuser. Check the worker ran"
    echo "   'jupyterhub-pilot install-worker' with this Hub's public key, and that the"
    echo "   security group lets the Hub reach it."
    exit 1
fi
