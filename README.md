<div align="center">

# 🚀 JupyterHub-Pilot

**Enterprise-grade AI coding partner & secure multi-team notebook isolation for JupyterHub**

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776ab?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![JupyterHub](https://img.shields.io/badge/JupyterHub-Compatible-f37726?style=flat-square&logo=jupyter&logoColor=white)](https://jupyterhub.readthedocs.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-22c55e?style=flat-square)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-107%20passing-22c55e?style=flat-square)](tests/)
[![AWS EC2](https://img.shields.io/badge/AWS-EC2%20Ready-FF9900?style=flat-square&logo=amazon-aws&logoColor=white)](AWS_EC2_DEPLOYMENT.md)

*One Hub. Many teams. Every user isolated. AI in every cell.*

</div>

---

## 📖 Table of Contents

| Section | What you'll learn |
|---|---|
| [🗺️ What is JupyterHub-Pilot?](#️-what-is-jupyterhub_pilot) | High-level purpose and design philosophy |
| [🏗️ Architecture: 1 Hub → Many Worker EC2s](#️-architecture-1-hub--many-worker-ec2s) | How a single hub routes 100s of users across multiple EC2s |
| [👥 Use Case 1 — Multi-Team EC2 Routing](#-use-case-1--multi-team-ec2-routing) | Group A on EC2-1, Group B on EC2-2, automatically |
| [🔐 Use Case 2 — Per-User Isolation & RBAC](#-use-case-2--per-user-isolation--rbac) | Each user gets their own OS account, process & resource cap |
| [🤖 Use Case 3 — AI Magic Commands](#-use-case-3--ai-magic-commands) | `%do`, `%fix`, `%review`, `%rework` inside notebooks |
| [🔑 Use Case 4 — Vault Secret Injection](#-use-case-4--vault-secret-injection) | Per-user API keys injected at spawn, zero disk I/O |
| [📊 Use Case 5 — Live Monitoring Dashboard](#-use-case-5--live-monitoring-dashboard) | Real-time CPU/RAM/network for every worker EC2 |
| [🔄 Use Case 6 — Crash Recovery](#-use-case-6--crash-recovery) | Hub restarts do not lose running user sessions |
| [🛠️ Installation Guide](INSTALL.md) | **Full step-by-step Hub & Worker EC2 setup** |
| [🧪 Running Tests](#-running-tests) | Full test suite, no real SSH or AWS needed |
| [🗺️ Roadmap](#️-roadmap) | What is built and what is next |

---

## 🗺️ What is JupyterHub-Pilot?

JupyterHub-Pilot solves two problems that every data team running JupyterHub eventually hits:

**Problem 1 — "How do we run notebooks for 100+ users without one user crashing everyone else?"**
> JupyterHub-Pilot's SSH spawner teleports each user's notebook server onto an isolated EC2 worker, wraps it in cgroup resource limits, and tracks all sessions in SQLite so nothing is lost if the Hub reboots.

**Problem 2 — "How do we get AI coding assistance inside notebooks without a brittle plugin?"**
> JupyterHub-Pilot's IPython extension adds four magic commands (`%do`, `%fix`, `%review`, `%rework`) that speak to any LLM backend — local Ollama, OpenAI, Claude, Gemini, or an MCP server — and inject clean Python directly into the next cell.

---

## 🏗️ Architecture: 1 Hub → Many Worker EC2s

This is the **core deployment model** JupyterHub-Pilot is built for.

```
                        ┌──────────────────────────────────────────┐
                        │          Hub EC2  (always on)             │
                        │                                           │
                        │  ┌─────────────┐   ┌──────────────────┐  │
                        │  │ JupyterHub  │   │  SQLite DB        │  │
                        │  │ + Google    │──▶│  team_mappings    │  │
                        │  │   OAuth     │   │  user_sessions    │  │
                        │  └──────┬──────┘   └──────────────────┘  │
                        │         │ CustomSpawner                   │
                        └─────────┼───────────────────────────────-─┘
                                  │  SSH (Paramiko, per spawn)
                    ┌─────────────┴──────────────────────┐
                    │                                    │
                    ▼                                    ▼
┌──────────────────────────┐          ┌──────────────────────────┐
│   Worker EC2 — group_a   │          │   Worker EC2 — group_b   │
│   IP: 10.0.1.10          │          │   IP: 10.0.1.20          │
│                          │          │                          │
│  alice  → port 8901      │          │  carol  → port 8901      │
│  bob    → port 8902      │          │  dave   → port 8902      │
│  ...                     │          │  ...                     │
│  (up to 50 users)        │          │  (up to 50 users)        │
│                          │          │                          │
│  metrics_agent.py ──WS───┼──────────┼──▶ /monitoring/ws       │
└──────────────────────────┘          └──────────────────────────┘
```

**Key properties:**
- Each **group** maps to exactly **one worker EC2** — all group members share that machine
- Each **user** on a worker gets their own **OS account**, **process**, and **TCP port** — completely isolated
- The Hub uses **SQLite** to remember who is on which EC2 — survives hub restarts
- Worker EC2s send **live metrics** back to the hub's monitoring dashboard over WebSocket
- Adding a new worker EC2 requires **zero hub restarts** — just insert a row in SQLite

---

## 👥 Use Case 1 — Multi-Team EC2 Routing

> **Goal:** Group A's 50 users run on EC2-1. Group B's 50 users run on EC2-2. The Hub routes automatically.

→ *[Back to Table of Contents](#-table-of-contents)*

### How it works

When a user logs in, the spawner:
1. Reads the user's JupyterHub group (e.g., `group_a`)
2. Looks up `group_a` in the SQLite `team_mappings` table → gets `10.0.1.10`
3. SSHes into `10.0.1.10` as that user
4. Launches `jupyterhub-singleuser` on a free port
5. Stores the session in SQLite (`username`, `vm_ip`, `port`, `group_name`)

The Hub's proxy then routes `https://hub.yourco.com/user/alice/` → `10.0.1.10:8901`.

### Setup

**Step 1 — Define your groups in `user_mapping.json`:**

```json
{
  "group_a": {
    "server_ip":      "10.0.1.10",
    "server_ssh_key": "/etc/jupyterhub/keys/group_a.pem",
    "admin_ssh_user": "ubuntu"
  },
  "group_b": {
    "server_ip":      "10.0.1.20",
    "server_ssh_key": "/etc/jupyterhub/keys/group_b.pem",
    "admin_ssh_user": "ubuntu"
  },
  "group_c": {
    "server_ip":      "10.0.1.30",
    "server_ssh_key": "/etc/jupyterhub/keys/group_c.pem",
    "admin_ssh_user": "ubuntu"
  }
}
```

**Step 2 — Seed the routing table into SQLite (run once on deploy):**

```bash
python -m jupyterhub_pilot.seed_sqlite \
    --db      /var/lib/jupyterhub/jupyterhub_pilot_state.db \
    --mapping /etc/jupyterhub/user_mapping.json
# Output:
#   ✓ group_a    → 10.0.1.10
#   ✓ group_b    → 10.0.1.20
#   ✓ group_c    → 10.0.1.30
# [DONE] Seeded 3 team(s), skipped 0.
```

**Step 3 — Assign users to groups in the JupyterHub Admin Panel:**

```
JupyterHub Admin → Groups → group_a → Add Users: alice, bob, charlie, ...
JupyterHub Admin → Groups → group_b → Add Users: carol, dave, eve, ...
```

That's it. From this point on:
- `alice` logs in → spawned on `10.0.1.10`
- `carol` logs in → spawned on `10.0.1.20`
- Neither can see or affect the other

### Adding a new worker EC2 without any downtime

```bash
# On the Hub VM — no JupyterHub restart needed:
python3 - <<'EOF'
from jupyterhub_pilot.session_store import SessionStore
store = SessionStore("/var/lib/jupyterhub/jupyterhub_pilot_state.db")
store.set_mapping("group_d", "10.0.1.40", "/etc/jupyterhub/keys/group_d.pem", "ubuntu")
print("group_d is live!")
EOF
```

→ [See full AWS deployment guide](AWS_EC2_DEPLOYMENT.md)

---

## 🔐 Use Case 2 — Per-User Isolation & RBAC

> **Goal:** On a shared EC2, user A's notebook cannot see user B's files, processes, or memory. Admins can manage any session; regular users can only manage their own.

→ *[Back to Table of Contents](#-table-of-contents)*

### Per-User OS Isolation

Every new user is **automatically provisioned** on the worker EC2 on their first login (Just-In-Time provisioning). No manual `adduser` needed:

```
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

### cgroups v2 Resource Limits

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

### 2-Tier RBAC

| Role | What they can do | How to grant |
|---|---|---|
| `user` | Start/stop/poll **their own** server only | Default for all users |
| `admin` | Start/stop/poll **any** user's server | JupyterHub Admin Panel → Users → ☑ Make Admin |

No code changes, no restarts — promote a user to admin live via the UI.

---

## 🤖 Use Case 3 — AI Magic Commands

> **Goal:** Data scientists get an AI pair-programmer that understands their notebook context, fixes their errors, and refactors their code — all without leaving Jupyter.

→ *[Back to Table of Contents](#-table-of-contents)*

### Load the extension

```python
# In any Jupyter cell:
%load_ext jupyterhub_pilot.extension
# ✅ JupyterHub-Pilot loaded. Available: %do, %fix, %review, %rework
```

Or auto-load in every session:

```bash
mkdir -p ~/.ipython/profile_default/startup/
echo "%load_ext jupyterhub_pilot.extension" > ~/.ipython/profile_default/startup/00-jupyterhub_pilot.ipy
```

---

### `%do` — Generate code from natural language

```python
%do load the CSV at data/sales.csv, parse the date column, and plot monthly revenue
```

JupyterHub-Pilot injects into the **next cell**:

```python
import pandas as pd
import matplotlib.pyplot as plt

df = pd.read_csv('data/sales.csv', parse_dates=['date'])
df['month'] = df['date'].dt.to_period('M')
monthly = df.groupby('month')['revenue'].sum()

monthly.plot(kind='bar', title='Monthly Revenue', figsize=(12, 5))
plt.tight_layout()
plt.show()
```

Run immediately instead of injecting:

```python
%do --run create a sample dataframe with 100 rows and columns name, age, score
```

The AI always sees the **last 3 cells of your notebook** as context — it knows what variables you have already defined.

---

### `%fix` — Auto-heal the last exception

```python
# You ran this and it crashed:
df.groupby('date')['revenue'].sum().plot()
# KeyError: 'date'
```

```python
# Just run:
%fix
# 🔧 JupyterHub-Pilot — analysing error and generating fix …
# Injects into next cell:
df['date'] = pd.to_datetime(df['date'])
df.groupby('date')['revenue'].sum().plot()
```

`%fix` strips all JupyterHub/IPython internal stack frames before sending to the LLM — only your code's error reaches the model.

---

### `%review` — AST-aware code quality review

```python
%review          # review last cell
%review -n 3     # review last 3 cells
%review --all    # review the entire notebook session
```

```
============================================================
📋 JupyterHub-Pilot Code Review
============================================================

🔍 Static Analysis (AST):
  - Line 3: bare `except:` — catches all exceptions including KeyboardInterrupt
  - Line 7: `process_data()` missing return type annotation
  - Line 7: argument `df` in `process_data()` missing type hint

🤖 LLM Review:
  • Overall: The code works but lacks defensive programming and type safety.
  • Use `except Exception as e:` instead of bare `except`
  • Add type hint `df: pd.DataFrame` and return `-> pd.DataFrame`
  • Consider extracting date parsing into a helper for reusability
  • No security concerns found
============================================================
```

---

### `%rework` — LLM-powered refactoring

```python
%rework add type hints and a docstring
%rework --diff convert this to method chaining   # preview before applying
```

With `--diff`:

```
────────────────────────────────────────────────────────────
📝 Proposed changes (use %rework without --diff to apply):
────────────────────────────────────────────────────────────
--- original
+++ reworked
-def process(df):
-    result = df[df['age'] > 18]
-    result = result.groupby('city')['score'].mean()
-    return result
+def process(df: pd.DataFrame) -> pd.Series:
+    """Return mean score by city for adult users."""
+    return (
+        df[df['age'] > 18]
+        .groupby('city')['score']
+        .mean()
+    )
────────────────────────────────────────────────────────────
```

### LLM Backend Configuration (`~/.jupyterhub-pilot/config.json`)

```json
{
  "mode": "local",

  "local": {
    "url":     "http://localhost:11434/api/generate",
    "model":   "qwen2.5-coder:7b",
    "timeout": 30
  },

  "cloud": {
    "provider": "openai",
    "model":    "gpt-4o-mini",
    "api_key":  "sk-...",
    "timeout":  30
  },

  "mcp": {
    "server_url": "http://localhost:3000",
    "tools":      ["generate_code"],
    "timeout":    30
  },

  "rate_limit": {
    "requests_per_minute": 20
  }
}
```

| Mode | Best for | Notes |
|---|---|---|
| `local` | Air-gapped teams, low latency | Requires Ollama running on the worker EC2 |
| `cloud` | Best quality (GPT-4, Claude, Gemini) | Requires API key; costs per token |
| `mcp` | Custom enterprise tool servers | MCP protocol over HTTP |

---

## 🔑 Use Case 4 — Vault Secret Injection

> **Goal:** Each user's notebook automatically gets their personal API keys (OpenAI key, database URL, S3 credentials) injected as environment variables at spawn time — no secrets on disk, no manual `.env` files.

→ *[Back to Table of Contents](#-table-of-contents)*

### How it works

```
User spawns →
  pre_spawn_hook() →
    VaultClient.get_user_secrets("alice") →
      GET https://vault.internal/v1/secret/data/jupyterhub_pilot/alice →
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

---

## 📊 Use Case 5 — Live Monitoring Dashboard

> **Goal:** The hub admin can see real-time CPU, memory, disk, and network I/O for every connected worker EC2 — without SSH-ing into anything.

→ *[Back to Table of Contents](#-table-of-contents)*

### Architecture

```
Worker EC2 (metrics_agent.py)
  ↓  WebSocket every 2 seconds
Hub EC2 (AgentWebSocketHandler at /monitoring/ws)
  ↓  stores latest snapshot per hostname in _WORKER_SNAPSHOTS
  ↓  broadcasts to all connected browser tabs
Browser (monitoring.html)
  ↓  dark-mode real-time dashboard with per-worker panels
```

### Start the agent on each worker EC2

```bash
python3 /opt/jupyterhub-pilot/jupyterhub_pilot/metrics_agent.py \
    --hub      ws://10.0.0.10:8000/monitoring/ws \
    --interval 2
```

Or as a systemd service so it survives reboots:

```ini
# /etc/systemd/system/jupyterhub_pilot-agent.service
[Unit]
Description=JupyterHub-Pilot Metrics Agent
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/jupyterhub-pilot/jupyterhub_pilot/metrics_agent.py \
    --hub ws://10.0.0.10:8000/monitoring/ws --interval 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now jupyterhub_pilot-agent
```

**Visit `http://<HUB-PUBLIC-IP>:8000/monitoring` to see the live dashboard.**

What you see per worker EC2:
- Live CPU % (total + per-core) and 1/5/15-min load averages
- RAM: used / total / percent
- Disk: used / total / percent (root filesystem)
- Network: upload/download Mbps (rolling 2-second rate)

**Access control:**
- All authenticated JupyterHub users can view the dashboard
- Regular users see only their own worker EC2's panel
- Admins see all connected workers

---

## 🔄 Use Case 6 — Crash Recovery

> **Goal:** The Hub EC2 reboots at 3am. When it comes back up, every user's running notebook server is automatically detected and re-linked — no user needs to restart their server.

→ *[Back to Table of Contents](#-table-of-contents)*

### How SQLite makes this safe

Every spawn writes a row to `user_sessions`:

| `username` | `vm_ip` | `port` | `pid` | `status` | `group_name` |
|---|---|---|---|---|---|
| `alice` | `10.0.1.10` | `8901` | `4821` | `running` | `group_a` |
| `bob` | `10.0.1.10` | `8902` | `4830` | `running` | `group_a` |
| `carol` | `10.0.1.20` | `8901` | `5012` | `running` | `group_b` |

The SQLite DB lives at `/var/lib/jupyterhub/jupyterhub_pilot_state.db` — **outside** the Python process. After a Hub restart:

1. `poll()` reads `vm_ip` from SQLite (not from `self.ip`, which is empty after restart)
2. SSHes to the worker EC2 and checks if the PID is still alive
3. If alive → `None` returned → Hub re-attaches the proxy → user session continues
4. If dead → `0` returned → JupyterHub prompts user to restart their server

### The JSON fallback

If SQLite itself is corrupted or the disk is full, the spawner automatically falls back to `user_mapping.json` for group-to-EC2 routing. A warning is logged, but no exception is raised and no user loses connectivity.

---

## 🛠️ Installation Guide

> The full installation guide lives in a dedicated document — it covers every step for both the Hub EC2 and each Worker EC2, including SSH key exchange, SQLite seeding, optional Vault secrets, the metrics agent, and a verification checklist.

### [📖 Read the full Installation Guide →](INSTALL.md)

Quick outline of what it covers:

| Step | What you are doing |
|---|---|
| [Step 1 — Hub EC2 Setup](INSTALL.md#-step-1--hub-ec2-setup) | Install JupyterHub-Pilot, configure hub_settings.json, generate SSH key |
| [Step 2 — Worker EC2 Setup](INSTALL.md#-step-2--worker-ec2-setup) | Install jupyterhub-singleuser, get_port.py, enable lingering |
| [Step 3 — SSH Key Exchange](INSTALL.md#-step-3--ssh-key-exchange) | Paste Hub public key into each worker's authorized_keys |
| [Step 4 — Register Workers](INSTALL.md#-step-4--register-worker-ec2s) | Seed user_mapping.json into SQLite |
| [Step 5 — Start JupyterHub](INSTALL.md#-step-5--start-jupyterhub) | Assign users to groups, start hub, switch to production auth |
| [Step 6 — Metrics Agent](INSTALL.md#-step-6--optional-metrics-agent) | systemd service for live monitoring on each worker |
| [Step 7 — Vault Secrets](INSTALL.md#-step-7--optional-vault-secret-injection) | Per-user secret injection at spawn |
| [Step 8 — AI Magic Commands](INSTALL.md#-step-8--optional-ai-magic-commands) | Auto-load `%do`, `%fix`, `%review`, `%rework` |
| [Verification Checklist](INSTALL.md#-verification-checklist) | End-to-end smoke tests |
| [Troubleshooting](INSTALL.md#-troubleshooting) | Common errors and fixes |

---

## 📦 Repository Structure

```
JupyterHub-Pilot/
├── spawner.py                      # SSH spawner — RBAC, cgroups, Vault, crash recovery
├── jupyterhub_config.py            # JupyterHub daemon config
├── jupyterhub_pilot_extension.py       # Standalone IPython extension entry point
├── hub_settings.json               # Network, auth, resource limits & Vault config
├── user_mapping.json               # Worker EC2 routing (seed source / SQLite fallback)
│
├── jupyterhub_pilot/                   # Core package
│   ├── __init__.py                 #   Version & lazy extension loader
│   ├── admin.py                    #   RBACManager — 2-tier role enforcement
│   ├── session_store.py            #   SQLite: team_mappings + user_sessions tables
│   ├── seed_sqlite.py              #   CLI: bootstrap SQLite from JSON mapping
│   ├── vault_client.py             #   HashiCorp Vault KV-v2 client (pure requests)
│   ├── env_setup.py                #   AI venv bootstrapper (user-venv isolation)
│   ├── extension.py                #   %do / %fix / %review / %rework implementations
│   ├── provider.py                 #   LLM provider (Ollama / LiteLLM / MCP + rate limit)
│   ├── metrics_agent.py            #   psutil agent — runs on each worker EC2
│   ├── monitoring_handler.py       #   Tornado WS + HTTP handlers for live dashboard
│   └── static/
│       └── monitoring.html         #   Dark-mode real-time monitoring dashboard
│
├── scripts/
│   ├── install_hub.sh              #   Automated Hub EC2 setup script
│   └── install_worker.sh           #   Automated Worker EC2 setup script
│
├── AWS_EC2_DEPLOYMENT.md           #   End-to-end AWS deployment walkthrough
└── tests/                          #   107-test mock-based unit suite
    ├── conftest.py                 #     Module mock bootstrap (no real SSH/DB needed)
    ├── test_magics.py              #     Magic command unit tests
    ├── test_provider.py            #     LLM provider backend tests
    ├── test_spawner.py             #     SSH spawner lifecycle tests
    ├── test_spawner_lifecycle.py   #     RBAC, SessionStore & crash recovery tests
    └── test_config.py              #     Hub config & security handler tests
```

---

## 🔐 Security Model

| Control | How it works |
|---|---|
| **OAuth Domain Lock** | `hosted_domain` in `hub_settings.json` restricts login to one Google Workspace domain |
| **Admin Isolation** | `admin_access = False` — admins cannot enter user containers via JupyterHub |
| **2-Tier RBAC** | `user.admin` from JupyterHub panel; `PermissionError` raised before any cross-user action |
| **Cross-User Blocking** | `BlockOtherUsersHandler` returns `403` on `/user/<other>/` path access attempts |
| **OOM Enforcement** | `oom_score_adj = 500` written to `/proc/self/oom_score_adj` inside each kernel at startup |
| **cgroups v2 Hard Limits** | `systemd-run --scope` caps every kernel's memory and CPU at the OS level |
| **Vault Secret Zero-Disk** | Secrets fetched from Vault at spawn time, passed via environment — never written to disk |

---

## 🧪 Running Tests

All 107 tests run with **zero real infrastructure** — no SSH, no EC2, no Vault:

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run the full suite
pytest -v tests/

# Individual suites
pytest -v tests/test_magics.py              # Magic commands (32 tests)
pytest -v tests/test_provider.py            # LLM provider backends (18 tests)
pytest -v tests/test_spawner.py             # SSH spawner lifecycle (11 tests)
pytest -v tests/test_spawner_lifecycle.py   # RBAC + SQLite + crash recovery (43 tests)
pytest -v tests/test_config.py             # Hub config & security (3 tests)
```

Expected:
```
107 passed, 1 warning in 2.90s
```

---

## 🗺️ Roadmap

| Task | Status | Description |
|---|---|---|
| **Task 1** — Admin Core & Lifecycle | ✅ Done | SQLite session state, 2-tier RBAC, `start/stop/poll/clear_state` |
| **Task 2** — cgroups v2 Isolation | ✅ Done | `systemd-run` hard `MemoryMax` + `CPUQuota` via `pre_spawn_hook` |
| **Task 3** — Vault Secret Injection | ✅ Done | KV-v2 client; zero-disk env injection at spawn; graceful degradation |
| **Task 4** — Live Monitoring Dashboard | ✅ Done | psutil agent → WebSocket → dark-mode real-time dashboard |
| **Task 5** — Controlled Env Strategy | ✅ Done | User venv at `~/.jupyterhub-pilot/venv`; `[ai]` optional package extra |
| **Task 6** — LLM & MCP Connectivity | ✅ Done | 3 backends (Ollama, LiteLLM, MCP) with retry, rate-limiting & Vault key reads |
| **Task 7** — Core Magics | ✅ Done | `%do`, `%fix` with cell context, traceback stripping, `--run` flag |
| **Task 8** — Agentic Magics | ✅ Done | `%review` (AST + LLM) and `%rework --diff` (unified diff preview) |
| **Task 9** — JIT User Provisioning | ✅ Done | Auto-creates OS users on Worker VMs on first login |
| **Future** — Redis/Postgres State | 🔜 Planned | Replace SQLite for multi-hub horizontal scaling |
| **Future** — Auto-scaling Workers | 🔜 Planned | Spin up new EC2 workers via AWS SDK when group capacity is low |

---

## 🤝 Contributing

1. **Fork** the repository
2. **Create a branch**: `git checkout -b feat/your-feature`
3. **Make changes** and ensure tests pass: `pytest -v tests/`
4. **Open a Pull Request** with a clear description

```bash
# Development setup:
git clone https://github.com/wajoud/jupyterhub-pilot.git
cd JupyterHub-Pilot
pip install -e ".[dev]"
pytest -v tests/   # all 107 should pass
```

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

<div align="center">

Built with 🔧 by [@wajoud](https://github.com/wajoud) · Star ⭐ if this helps your team!

**[🛠️ Installation Guide](INSTALL.md)** &nbsp;·&nbsp; **[🏗️ Deploy to AWS EC2](AWS_EC2_DEPLOYMENT.md)** &nbsp;·&nbsp; **[🤖 AI Magic Commands](#-use-case-3--ai-magic-commands)** &nbsp;·&nbsp; **[📊 Live Monitoring](#-use-case-5--live-monitoring-dashboard)**

</div>
