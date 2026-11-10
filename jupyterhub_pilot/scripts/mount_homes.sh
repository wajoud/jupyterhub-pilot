#!/usr/bin/env bash
# Mount a shared NFS filesystem (AWS EFS) as the users' home base on this worker.
# Run through `sudo jupyterhub-pilot mount-homes <efs-dns-name> [mount-point]` on
# every worker, then set "home_base" in the Hub's hub_settings.json to the mount point.
# Safe to run again. Ubuntu (apt) and Amazon Linux 2023 (dnf).
set -e

FS="$1"
DIR="${2:-/efs/home}"

if [ "$EUID" -ne 0 ]; then
    echo "❌ Run with sudo: sudo jupyterhub-pilot mount-homes <efs-dns-name> [mount-point]"
    exit 1
fi
if ! [[ "$FS" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "❌ '$FS' is not a DNS name or IP (EFS: fs-0123abcd.efs.<region>.amazonaws.com)."
    exit 1
fi
if ! [[ "$DIR" =~ ^(/[A-Za-z0-9_.-]+)+$ ]] || [ "$DIR" = "/home" ]; then
    # Mounting over /home would hide the admin user's SSH keys and lock the Hub out.
    echo "❌ Mount point must be an absolute path other than /home (default /efs/home)."
    exit 1
fi

if command -v apt-get >/dev/null; then
    apt-get install -y nfs-common
elif command -v dnf >/dev/null; then
    dnf install -y nfs-utils
else
    echo "❌ Needs apt (Ubuntu) or dnf (Amazon Linux 2023)."
    exit 1
fi

mkdir -p "$DIR"
# AWS's recommended NFS options. nofail: the worker still boots if EFS is unreachable
# (the Hub then refuses to start servers there instead of using the local disk).
LINE="$FS:/ $DIR nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport,_netdev,nofail 0 0"
if grep -qE "[[:space:]]$DIR[[:space:]]" /etc/fstab; then
    echo "ℹ️  /etc/fstab already has an entry for $DIR; left as it is."
else
    echo "$LINE" >> /etc/fstab
fi
mountpoint -q "$DIR" || mount "$DIR"

if mountpoint -q "$DIR"; then
    echo "✅ $FS mounted at $DIR."
    echo "On the Hub: set \"home_base\": \"$DIR\" in hub_settings.json and restart the Hub (once, after every worker is mounted)."
else
    echo "❌ $DIR is not mounted. Check the EFS security group allows NFS (TCP 2049) from this worker."
    exit 1
fi
