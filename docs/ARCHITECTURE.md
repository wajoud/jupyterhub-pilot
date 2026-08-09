# 🏗️ Architecture & Routing

JupyterHub-Pilot is designed to scale horizontally across multiple EC2 instances while maintaining strict boundaries between teams.

## The Core Model: 1 Hub → Many Worker EC2s

```text
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
- Adding a new worker EC2 requires **zero hub restarts** — just insert a row in SQLite

---

## 👥 Multi-Team EC2 Routing

> **Goal:** Group A's 50 users run on EC2-1. Group B's 50 users run on EC2-2. The Hub routes automatically.

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
# [DONE] Seeded 2 team(s), skipped 0.
```

**Step 3 — Assign users to groups in the JupyterHub Admin Panel:**

```text
JupyterHub Admin → Groups → group_a → Add Users: alice, bob, charlie, ...
JupyterHub Admin → Groups → group_b → Add Users: carol, dave, eve, ...
```

---

### Adding a new worker EC2 without any downtime

```bash
# On the Hub VM — no JupyterHub restart needed:
python3 - <<'EOF'
from jupyterhub_pilot.session_store import SessionStore
store = SessionStore("/var/lib/jupyterhub/jupyterhub_pilot_state.db")
store.set_mapping("group_c", "10.0.1.30", "/etc/jupyterhub/keys/group_c.pem", "ubuntu")
print("group_c is live!")
EOF
```

---

## 🔄 Crash Recovery

> **Goal:** The Hub EC2 reboots at 3am. When it comes back up, every user's running notebook server is automatically detected and re-linked — no user needs to restart their server.

### How SQLite makes this safe

Every spawn writes a row to `user_sessions`:

| `username` | `vm_ip` | `port` | `pid` | `status` | `group_name` |
|---|---|---|---|---|---|
| `alice` | `10.0.1.10` | `8901` | `4821` | `running` | `group_a` |

The SQLite DB lives at `/var/lib/jupyterhub/jupyterhub_pilot_state.db` — **outside** the Python process. After a Hub restart:

1. `poll()` reads `vm_ip` from SQLite (not from `self.ip`, which is empty after restart)
2. SSHes to the worker EC2 and checks if the PID is still alive
3. If alive → `None` returned → Hub re-attaches the proxy → user session continues
4. If dead → `0` returned → JupyterHub prompts user to restart their server

### The JSON fallback

If SQLite itself is corrupted or the disk is full, the spawner automatically falls back to `user_mapping.json` for group-to-EC2 routing. A warning is logged, but no exception is raised and no user loses connectivity.
