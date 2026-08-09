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
  ↓ sudo adduser --disabled-password alice
  ↓ sudo loginctl enable-linger alice      ← keeps process alive after SSH closes
  ↓ sudo mkdir /home/alice/notebook
  ↓ sudo pip3 install jupyterhub notebook jupyterhub_pilot[ai]
  ↓ copy Hub SSH key → /home/alice/.ssh/authorized_keys
  ↓
spawner SSHes as alice, finds a free port, launches singleuser
```

After provisioning:
- `alice` owns `/home/alice/` — Bob cannot read it
- `alice`'s notebook process runs as OS user `alice` — Bob's kernel cannot signal it
- Each process is wrapped in a cgroup scope — memory/CPU cannot bleed across

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
| `user` | Start/stop/poll **their own** server only | Default for all users |
| `admin` | Start/stop/poll **any** user's server | JupyterHub Admin Panel → Users → ☑ Make Admin |

No code changes, no restarts — promote a user to admin live via the JupyterHub UI. If a normal user attempts to start or stop another user's server via the API, a `PermissionError` is raised before any action is taken.

---

## 4. Vault Secret Injection

> **Goal:** Each user's notebook automatically gets their personal API keys (OpenAI key, database URL, S3 credentials) injected as environment variables at spawn time — no secrets on disk, no manual `.env` files.

### How it works

```text
User spawns →
  pre_spawn_hook() →
    VaultClient.get_user_secrets("alice") →
      GET https://vault.internal/v1/secret/data/jupyterhub-pilot/alice →
        {"OPENAI_API_KEY": "sk-alice-...", "DATABASE_URL": "postgres://..."}
  ↓
  self.environment.update(secrets)
  ↓
JupyterHub passes environment to jupyterhub-singleuser at launch
  ↓
Alice's kernel: os.environ["OPENAI_API_KEY"] == "sk-alice-..."
```

Alice's keys are never written to disk. Bob's kernel cannot see Alice's environment.

### Setup

```bash
# On the Hub VM before starting JupyterHub:
export VAULT_ADDR=http://10.0.0.5:8200
export VAULT_TOKEN=your-policy-scoped-token
```

```json
// hub_settings.json
{
  "vault_enabled": true,
  "vault_secret_path": "secret/jupyterhub-pilot"
}
```

```bash
# Store each user's secrets in Vault:
vault kv put secret/jupyterhub-pilot/alice \
    OPENAI_API_KEY="sk-alice-..." \
    DATABASE_URL="postgres://alice:pass@db.internal/mydb"

vault kv put secret/jupyterhub-pilot/bob \
    OPENAI_API_KEY="sk-bob-..." \
    S3_BUCKET="bob-data-bucket"
```

**Graceful degradation:** If Vault is unreachable, the spawn continues uninterrupted — Vault is opt-in. A warning is logged, but no exception is raised.
