# 🔐 Security, Isolation & Role-Based Access

This document details the three core security pillars of JupyterHub-Pilot: OS-level isolation (cgroups), role-based access control (RBAC), and zero-disk secret management.

---

## 1. Per-User OS Isolation

> **Goal:** On a shared EC2, user A's notebook cannot see user B's files, processes, or memory.

Every new user is **automatically provisioned** on the worker EC2 on their first login (Just-In-Time provisioning). No manual `adduser` needed by admins:

```text
alice logs in for the first time
  ↓
spawner calls _provision_user_jit()
  ↓ SSH as admin (ubuntu) to 10.0.1.10
  ↓ sudo adduser --allow-bad-names --disabled-password alice
  ↓ sudo loginctl enable-linger alice      ← keeps process alive after SSH closes
  ↓ sudo mkdir /home/alice/notebook
  ↓ copy Hub SSH key → /home/alice/.ssh/authorized_keys
  ↓
spawner SSHes as alice, finds a free port, launches singleuser
```

After provisioning:
- `alice` owns `/home/alice/` — Bob cannot read it
- `alice`'s notebook process runs as OS user `alice` — Bob's kernel cannot signal it
- Each process is wrapped in a cgroup scope — memory/CPU cannot bleed across

**Terminals.** Notebook's **New → Terminal** gives a shell as the same Linux user, inside the same cgroup. It is not a new permission: code in a kernel can already run any command (`!cmd`, `os.system`). What it does change is that long-running processes are easy to start and keep running after the browser closes (they count against the user's limit until the server stops). `terminals_enabled: false` in `hub_settings.json` removes the terminal for everyone, **Terminal off** on the admin Limits page for one user; neither stops shell commands from kernels.

---

## 2. cgroups v2 Resource Limits

Configure per-user hard limits in `hub_settings.json`:

```json
{
  "resource_limits": {
    "memory_max": "2G",
    "cpu_quota":  "100%"
  }
}
```

The spawner automatically wraps the launch command with `systemd-run`:

```bash
# What actually runs on the worker EC2:
systemd-run --user --scope \
  --unit=jupyterhub-alice-1722000000 \
  --property=MemoryMax=2G \
  --property=CPUQuota=100% \
  -- nohup jupyterhub-singleuser --ip=0.0.0.0 --port=8901 ...
```

If Alice's notebook eats 2.1GB of RAM, the kernel OOM-kills her process — **not Bob's, not the hub's**.

> ⚠️ **Required on each worker EC2:** `sudo loginctl enable-linger <username>` for every user, otherwise Ubuntu kills the process 7 seconds after the SSH session closes. JIT provisioning handles this automatically.

---

## 3. 2-Tier RBAC

| Role | What they can do | How to grant |
|---|---|---|
| `user` | Start/stop/poll **their own** server only; see their own group's workers on the dashboard | Default for all users |
| `admin` | Start/stop/poll **any** user's server; see every worker on the dashboard | JupyterHub Admin Panel → Users → ☑ Make Admin |

No code changes, no restarts — promote a user to admin live via the JupyterHub UI. JupyterHub's own permission scopes enforce who may start or stop which server: a normal user's API request for another user's server is refused with `403 Forbidden`.

---

## 4. Vault Secret Injection

> **Goal:** each user's notebook gets their own API keys, database URLs and cloud credentials as environment variables, with no secrets in notebooks, on the worker's disk, or in `.env` files.

### How it works

`install-hub` only installs the `vault` command (when HashiCorp's package repo works on your OS; otherwise it prints `Vault not installed (optional)`). It does **not** start a Vault server, create a policy or create a token. You do that once, below.

```text
alice clicks Start My Server
  → the Hub reads secret/jupyterhub-pilot/alice from Vault (KV v2), using the Hub's token
  → the key/values are added to alice's server environment
  → the Hub sends them to the worker over SSH stdin (never on the command line, so `ps` can't show them)
  → in alice's notebook: os.environ["OPENAI_API_KEY"]
```

- Secrets are read **once, at server start**. After you change a user's secret, they stop and start their server.
- The path is `<vault_secret_path>/<Hub user name>` (the name in the Admin list, e.g. `alice` or `first.last`).
- Vault is optional and never blocks a start: if Vault is down or the token is wrong, the server starts without secrets and the Hub logs a warning.

### Who can see what

| Who | Can see |
|---|---|
| The user | Their own secrets, in their kernel and terminal (`os.environ`, `env`) |
| Other users on the same worker | Nothing: another user's process environment (`/proc/<pid>/environ`) is readable only by that user and root |
| Admins in the Hub | Nothing through the Hub (`admin_access` is off) |
| Root on the worker | The user's secrets while their server runs |
| Root on the Hub | **Every** user's secrets: the Hub's Vault token is in `/etc/jupyterhub-pilot/hub.env` (root only). Give it read-only access to this one path, nothing else |

**Scheduled notebooks** (`%schedule`) are the one place a secret is written to disk: `%schedule add` saves the secrets the server got into `~/.config/jupyterhub-pilot/jobs/<job>.env` (mode `600`, only the user and root can read it) so the timed run gets them too. `%schedule delete` removes the file.

A user can print their own secret in a notebook and save it in the `.ipynb`. Tell users to read them with `os.environ`, never to print them.

### Step 1: a Vault server

**Testing (dev mode, on the Hub).** Keeps everything in memory, so secrets are gone when it stops. KV v2 is already enabled at `secret/`:

```bash
vault version                     # check install-hub installed it
vault server -dev -dev-listen-address=127.0.0.1:8200 > /tmp/vault-dev.log 2>&1 &
grep "Root Token" /tmp/vault-dev.log
export VAULT_ADDR=http://127.0.0.1:8200
export VAULT_TOKEN=<the root token>
```

**Production.** Use your company's Vault if it has one (skip to step 2 with its address and an admin token). Otherwise run one: the `vault` package ships `/etc/vault.d/vault.hcl` and a `vault` systemd service. Follow HashiCorp's [deployment guide](https://developer.hashicorp.com/vault/tutorials/day-one-raft/raft-deployment-guide) (`vault operator init`, keep the unseal keys and root token safe, `vault operator unseal` after every restart), then enable KV v2:

```bash
vault secrets enable -path=secret kv-v2
```

Run Vault on a private IP only; the Hub is its only client.

### Step 2: a read-only policy and token for the Hub

```bash
vault policy write jupyterhub-pilot - << 'POLICY'
path "secret/data/jupyterhub-pilot/*" {
  capabilities = ["read"]
}
# Lets the Hub renew its own token (-no-default-policy below takes this away otherwise)
path "auth/token/renew-self" {
  capabilities = ["update"]
}
POLICY

# Periodic token: lives as long as it is renewed within 32 days.
vault token create -policy=jupyterhub-pilot -no-default-policy -orphan -period=768h -field=token
```

The Hub renews the token itself: at every start and then once a day (no cron job). If the Hub is off for more than 32 days, create a new token. A policy written before 0.7.0 lacks the `renew-self` path: run the `vault policy write` above again (existing tokens pick it up), otherwise the Hub log says `token renewal refused`.

If the token expires, servers start without secrets and the Hub log says `permission denied reading secrets`.

### Step 3: turn it on in the Hub

```bash
# Address and token go in the root-only env file the jupyterhub service reads
echo 'VAULT_ADDR=http://127.0.0.1:8200' | sudo tee -a /etc/jupyterhub-pilot/hub.env > /dev/null
echo 'VAULT_TOKEN=<token from step 2>' | sudo tee -a /etc/jupyterhub-pilot/hub.env > /dev/null

F=/opt/jupyterhub-pilot/hub_settings.json
sudo jq '.vault_enabled = true | .vault_secret_path = "secret/jupyterhub-pilot"' $F | sudo tee $F.tmp >/dev/null && sudo mv $F.tmp $F
sudo systemctl restart jupyterhub
```

The restart stops running servers.

### Step 4: add a user's secrets

With an admin token (not the Hub's read-only one):

```bash
vault kv put secret/jupyterhub-pilot/alice \
    OPENAI_API_KEY="sk-alice-..." \
    DATABASE_URL="postgres://alice:pass@db.internal/mydb"

vault kv patch secret/jupyterhub-pilot/alice S3_BUCKET="alice-data"   # add one key
vault kv get secret/jupyterhub-pilot/alice                           # check
vault kv metadata delete secret/jupyterhub-pilot/alice               # remove all of alice's secrets
```

Keys become environment variable names, so use `UPPER_CASE` names without spaces or dashes.

### Step 5: the user reads them

Alice stops and starts her server (Hub home → **Stop My Server** → **Start My Server**), then in a notebook:

```python
import os
key = os.environ["OPENAI_API_KEY"]
```

### Check it works

```bash
sudo journalctl -u jupyterhub | grep -i vault
```

| Log line | Meaning |
|---|---|
| `Vault injected 2 secret(s) for 'alice'` | Working |
| *(nothing for that user)* | No secret stored at `secret/jupyterhub-pilot/alice`, or `vault_enabled` is false |
| `cannot connect to Vault at ...` | Vault down, or wrong `VAULT_ADDR` |
| `permission denied reading secrets` | Token expired, or its policy doesn't cover the path |
| `Vault request timed out` | Vault slower than 5 s |

Test the Hub's own token from the Hub: `sudo sh -c 'set -a; . /etc/jupyterhub-pilot/hub.env; vault kv get secret/jupyterhub-pilot/alice'`.
