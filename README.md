# JupyterHub-Pilot

One JupyterHub for many teams. Each team gets its own worker VM, and every notebook has an AI assistant.

[![PyPI](https://img.shields.io/pypi/v/jupyterhub-pilot)](https://pypi.org/project/jupyterhub-pilot/)
[![Python](https://img.shields.io/pypi/pyversions/jupyterhub-pilot)](https://pypi.org/project/jupyterhub-pilot/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/wajoud/jupyterhub-pilot/blob/main/LICENSE)

## Contents

1. [What it does](#what-it-does)
2. [How it works](#how-it-works)
3. [Before you start](#before-you-start)
4. [Install: Hub and first worker](#install-hub-and-first-worker)
5. [Start, stop and restart](#start-stop-and-restart)
6. [Add users and teams](#add-users-and-teams)
7. [Add more worker servers](#add-more-worker-servers)
8. [Remove a worker server](#remove-a-worker-server)
9. [Using the notebooks and AI magics](#using-the-notebooks-and-ai-magics)
10. [Monitoring dashboard](#monitoring-dashboard)
11. [Configuration reference](#configuration-reference)
12. [Logs and troubleshooting](#logs-and-troubleshooting)
13. [Development and tests](#development-and-tests)
14. [More documentation](#more-documentation)

## What it does

- **Team isolation.** Every JupyterHub group (a "team") is mapped to its own worker VM. When a user starts a notebook, the Hub SSHes into that team's worker and launches the user's server there, so one heavy user cannot slow down the other teams.
- **Per-user Linux accounts.** On first login the worker creates a Linux account for the user (`alice@corp.com` → `alice`), so users cannot read each other's files.
- **Resource limits.** Each notebook server runs under cgroups v2 memory and CPU limits (`systemd-run`).
- **Crash-safe state.** Sessions and team→VM mappings live in SQLite, so a Hub restart does not lose track of running servers.
- **Secrets.** Optional HashiCorp Vault integration injects per-user secrets as environment variables.
- **AI magics.** `%do`, `%fix`, `%review` and `%rework` in any notebook, backed by Ollama, OpenAI/Claude/Gemini (through LiteLLM) or an MCP server.
- **Live monitoring.** A dashboard at `/hub/monitoring` shows each worker's CPU, memory, disk and network, and each user's usage against their limits. Admins see everything; users see their own team's workers and their own row.
- **Load testing.** `jupyterhub-pilot load-test` starts many test users at once and reports start times and which kernels hit their limits.

## How it works

```text
                    Browser (https://hub.example.com)
                                │
                     Caddy (HTTPS, port 443)
                                │
┌───────────────────────── Hub VM ──────────────────────────┐
│  configurable-http-proxy :8000  →  JupyterHub :8081         │
│  Google login · CustomSpawner · SQLite state · /hub/monitoring
└───────────────┬───────────────────────────┬────────────────┘
                │ SSH (port 22)             │ SSH (port 22)
                ▼                           ▼
     ┌──── Worker VM: team_alpha ──┐   ┌──── Worker VM: team_beta ───┐
     │ alice's jupyterhub-singleuser│   │ dave's jupyterhub-singleuser│
     │ bob's   jupyterhub-singleuser│   │ ...                         │
     │ metrics agent ──WS──▶ Hub    │   │ metrics agent ──WS──▶ Hub   │
     └─────────────────────────────┘   └─────────────────────────────┘
```

When `alice` (group `team_alpha`) clicks **Start My Server**:

1. The Hub looks up `team_alpha` in SQLite and gets the worker's IP and SSH key.
2. It SSHes in as the worker's admin user (`admin_ssh_user`) and creates the Linux user `alice` if she doesn't exist yet.
3. It SSHes in as `alice`, picks a free port and starts `jupyterhub-singleuser` inside a cgroup scope.
4. The Hub's proxy routes `https://hub.example.com/user/alice/` to `worker-ip:port`.

## Before you start

You need:

- **Two or more Ubuntu VMs** (tested on 24.04 and 26.04): one Hub, plus one worker per team, in the region closest to your users. Notebook loads about 40 files on first open, so distance shows. Amazon Linux 2023 is supported by the same commands but not yet tested end to end.
- **Disk:** 20–30 GiB per worker rather than the ~8 GiB launch default. The worker install alone uses ~3.4 GB, and all of that team's notebooks and packages share the disk.
- **A domain name** pointing at the Hub's public IP (for example `hub.example.com`). Google login only works over HTTPS on a real domain, never on `http://` or a bare IP.
- **A Google OAuth client** (Google Cloud console → APIs & Services → Credentials → OAuth client ID → Web application) with the redirect URI `https://hub.example.com/hub/oauth_callback`.
- **Network rules** (AWS security groups or your firewall):

| On | Allow | From | Why |
|---|---|---|---|
| Hub | 22 | your IP | your SSH access |
| Hub | 80, 443 | anywhere | Caddy serves HTTPS (80 is needed to get the certificate) |
| Hub | 8000 | worker private IPs only | metrics agents connect here. Never open 8000 to the internet |
| Hub | 8081 | worker private IPs only | notebook servers talk to the Hub API |
| Worker | all TCP | Hub private IP | Hub SSHes in and proxies to random high ports |

## Install: Hub and first worker

Full walkthroughs: [AWS EC2 Deployment Guide](https://github.com/wajoud/jupyterhub-pilot/blob/main/AWS_EC2_DEPLOYMENT.md) and [Installation Guide](https://github.com/wajoud/jupyterhub-pilot/blob/main/INSTALL.md). The short version:

Everything installs from PyPI; there is nothing to clone. Each machine keeps its own settings and keys in `/opt/jupyterhub-pilot`.

### 1. Hub VM

```bash
sudo apt update && sudo apt install -y python3-pip            # Amazon Linux: sudo dnf install -y python3-pip
sudo pip3 install --break-system-packages --ignore-installed "jupyterhub-pilot[hub]"
sudo jupyterhub-pilot install-hub
```

`--break-system-packages` and `--ignore-installed` let pip install next to Ubuntu's own Python packages; leave them out on Amazon Linux.

`install-hub` asks for the Hub's private IP, your admin username and the first worker's private IP. It then:

- installs the proxy (and, optionally, Vault)
- creates an SSH key at `/opt/jupyterhub-pilot/keys/worker`
- writes `/opt/jupyterhub-pilot/hub_settings.json` and `/opt/jupyterhub-pilot/user_mapping.json` (first team: `team_alpha`)
- generates the metrics agent token at `/etc/jupyterhub-pilot/agent_token`
- seeds the SQLite DB at `/var/lib/jupyterhub/jupyterhub_pilot_state.db`
- writes the `jupyterhub` systemd service and a root-only `/etc/jupyterhub-pilot/hub.env` (not started yet)

At the end it prints a public SSH key and an agent token. Keep both for the worker step.

> **Note:** with Google login the JupyterHub username is the part of the email before the `@`. For `admin@company.com`, enter `admin`, or that account will not get admin rights.

Then edit `/opt/jupyterhub-pilot/hub_settings.json` and set `hosted_domain` to your Google Workspace domain (for example `company.com`). Only accounts from that domain can log in.

### 2. Worker VM

```bash
sudo apt update && sudo apt install -y python3-pip
sudo pip3 install --break-system-packages --ignore-installed "jupyterhub-pilot[worker,ai]"
sudo jupyterhub-pilot install-worker
```

Install system-wide (with `sudo`), so every user's notebook server finds Jupyter and the AI magics. `install-worker` asks for:

1. the OS admin user the Hub may SSH in as (default: the user running sudo, e.g. `ubuntu`)
2. the Hub's public SSH key (printed by `install-hub`, or `cat /opt/jupyterhub-pilot/keys/worker.pub` on the Hub)
3. the Hub's private IP
4. the agent token (printed by `install-hub`, or `sudo cat /etc/jupyterhub-pilot/agent_token` on the Hub)

**No prompts (handy for several workers):** fill in the four answers once, in the order above, and paste the same block on every worker:

```bash
sudo apt update && sudo apt install -y python3-pip
sudo pip3 install --break-system-packages --ignore-installed "jupyterhub-pilot[worker,ai]"
printf '%s\n' ubuntu \
  "<HUB_PUBLIC_SSH_KEY>" \
  <HUB_PRIVATE_IP> \
  <AGENT_TOKEN> \
  | sudo jupyterhub-pilot install-worker
hostname -I     # this worker's private IP, for add-worker on the Hub
```

It starts the `jupyterhub_pilot-metrics` service. The first worker is already registered as `team_alpha`; check the Hub can reach it (on the Hub):

```bash
sudo jupyterhub-pilot add-worker team_alpha <WORKER_PRIVATE_IP>
```

### Upgrading

`sudo pip3 install --break-system-packages --ignore-installed -U "jupyterhub-pilot[hub]"` on the Hub (`[worker,ai]` on workers), then `sudo systemctl restart jupyterhub` (or `jupyterhub_pilot-metrics`). Your settings in `/opt/jupyterhub-pilot` are kept.

Installs made with `git clone` (0.4.0 and earlier) keep working after a `git pull`. To move one to the pip layout, install the package as above and run `sudo jupyterhub-pilot install-hub` (or `install-worker`) again: it keeps the keys and team mapping in `/opt/jupyterhub-pilot` and rewrites the service.

### 3. HTTPS with Caddy (on the Hub)

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

echo 'hub.example.com {
    encode zstd gzip
    reverse_proxy <HUB_PRIVATE_IP>:8000
}' | sudo tee /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Keep Caddy in front even for a quick test: HTTPS gives HTTP/2 and `encode` compresses Notebook's JavaScript about 4×. Over plain `http://<ip>:8000` from far away, the Notebook page can time out and stay blank (`ChunkLoadError`).

**No domain yet (testing only):** Caddy can serve the Hub's public IP with its own self-signed certificate. Browsers show a warning you click through, and Google login will not work, so use [test login](#option-c-test-login-no-google-testing-only).

```bash
# On EC2 this reads the Hub's public IP; elsewhere set PUBLIC_IP=<HUB_PUBLIC_IP> by hand
TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
PUBLIC_IP=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4)
echo $PUBLIC_IP

printf '{\n\tdefault_sni %s\n}\n\nhttps://%s {\n\ttls internal\n\tencode zstd gzip\n\treverse_proxy <HUB_PRIVATE_IP>:8000\n}\n' "$PUBLIC_IP" "$PUBLIC_IP" | sudo tee /etc/caddy/Caddyfile
sudo systemctl restart caddy

# Check Caddy answers (should print 200, once the Hub is running)
curl -sk -o /dev/null -w '%{http_code}\n' --resolve $PUBLIC_IP:443:127.0.0.1 https://$PUBLIC_IP/hub/login
```

`default_sni` is needed because browsers send no server name for a bare IP; without it the HTTPS handshake fails. Test from the Hub with `--resolve` as above: a plain `curl https://$PUBLIC_IP` from the Hub itself goes out and back in through the security group, which only allows your IP, so it prints `000` even when Caddy works. Then open `https://<HUB_PUBLIC_IP>` from your own machine.

Now continue with [Start, stop and restart](#start-stop-and-restart).

## Start, stop and restart

### Option A: run in the foreground (first try, debugging)

Run on the Hub, as the user you ran `install-hub` with sudo from (usually `ubuntu`):

```bash
cd /opt/jupyterhub-pilot
export CLIENT_ID=<google-client-id>
export CLIENT_SECRET=<google-client-secret>
export OAUTH_CALLBACK_URL=https://hub.example.com/hub/oauth_callback
export JUPYTERPILOT_AGENT_TOKEN=$(sudo cat /etc/jupyterhub-pilot/agent_token)
python3 -m jupyterhub -f "$(python3 -c 'import jupyterhub_pilot.paths as p; print(p.PACKAGE_DIR)')/jupyterhub_config.py"
```

**Stop it** with `Ctrl+C`.

### Option B: run as a systemd service (production)

`install-hub` already wrote the service (`/etc/systemd/system/jupyterhub.service`) and a root-only env file (`/etc/jupyterhub-pilot/hub.env`) with the agent token filled in. Fill in the Google lines, then start it:

```bash
sudo nano /etc/jupyterhub-pilot/hub.env
sudo systemctl enable --now jupyterhub
```

The service runs the Hub config that ships in the package and reads this machine's settings from `/opt/jupyterhub-pilot` (`JUPYTERHUB_PILOT_DIR`).

### Option C: test login, no Google (testing only)

For a trial without a Google client or domain, set two passwords in `/etc/jupyterhub-pilot/hub.env` (the file has commented lines ready) and leave the Google lines empty:

```
JUPYTERPILOT_TEST_PASSWORD=<8+ characters>
JUPYTERPILOT_TEST_ADMIN_PASSWORD=<32+ characters, different>
```

Any user name logs in with the first password. Admins (the names in `admin_users`, e.g. the one you gave `install-hub`) must use the **second** password; the first one does not work for them. The Hub will not start if either is shorter than that. Restart the Hub. Use it only behind Caddy with access limited to your IP, never on a public Hub.

If a worker is stopped or unreachable for more than 5 minutes, its users' servers are shown as stopped; they can start again once a worker for their team is back.

### Everyday commands

| What | Command |
|---|---|
| Start the Hub | `sudo systemctl start jupyterhub` |
| Stop the Hub | `sudo systemctl stop jupyterhub` |
| Restart the Hub | `sudo systemctl restart jupyterhub` |
| Hub status | `sudo systemctl status jupyterhub` |
| Hub logs | `sudo journalctl -u jupyterhub -f` |
| Stop / start HTTPS | `sudo systemctl stop caddy` / `sudo systemctl start caddy` |
| Stop / start a worker's metrics agent (on the worker) | `sudo systemctl stop jupyterhub_pilot-metrics` / `sudo systemctl start jupyterhub_pilot-metrics` |

> **Note:** stopping the Hub stops every user's notebook server (JupyterHub's default), and unsaved work in running kernels is lost. Warn users before a restart.

### Stop one user's notebook server

- **The user:** Hub home page → **Stop My Server**.
- **An admin:** **Admin** page → the user's row → **Stop Server**.
- **By API** (admin token from **Token** page):

```bash
curl -X DELETE -H "Authorization: token <ADMIN_API_TOKEN>" \
  https://hub.example.com/hub/api/users/alice/server
```

### When do I need a restart?

| You changed | Restart needed? |
|---|---|
| `hub_settings.json` (IPs, admins, domain, limits, Vault) | Yes: restart the Hub |
| `user_mapping.json` + re-seeded the DB (add/change a worker) | No |
| Groups or group members in the Admin panel | No (the user restarts their own server to move workers) |
| The agent token | Yes: Hub and every worker's metrics agent |

## Add users and teams

A **team** is a JupyterHub group whose name matches a key in `user_mapping.json` (for example `team_alpha`). Users in that group run their notebooks on that team's worker.

1. The user logs in once with Google (this creates their JupyterHub account), or an admin adds them on the **Admin** page → **Add Users** using the name before the `@`.
2. **Admin** → **Groups** → open the team group (create it with **Add Group** if it does not exist, using the exact team name) → **Add Users**.
3. The user clicks **Start My Server**.

Rules:

- A user with **no group** cannot start a server ("User has no groups assigned").
- Keep each user in **one** team group. If a user is in several, the first group is used.
- **Make someone an admin:** add their username to `admin_users` in `hub_settings.json` and restart the Hub, or tick **Admin** on their user in the Admin panel.

## Add more worker servers

Use this to give a new team its own VM (or to move a team to a bigger VM). No Hub restart is needed.

**1. Create the VM** and apply the worker network rules from [Before you start](#before-you-start). Also allow the new worker's private IP on the Hub's ports 8000 and 8081.

**2. Set up the worker** (on the new VM), as in [Worker VM](#2-worker-vm):

```bash
sudo apt update && sudo apt install -y python3-pip
sudo pip3 install --break-system-packages --ignore-installed "jupyterhub-pilot[worker,ai]"
sudo jupyterhub-pilot install-worker
```

Paste the same Hub public key and agent token as for the first worker. Get them on the Hub with:

```bash
cat /opt/jupyterhub-pilot/keys/worker.pub
sudo cat /etc/jupyterhub-pilot/agent_token
```

Or skip the prompts with the same block as for the first worker:

```bash
sudo apt update && sudo apt install -y python3-pip
sudo pip3 install --break-system-packages --ignore-installed "jupyterhub-pilot[worker,ai]"
printf '%s\n' ubuntu \
  "<HUB_PUBLIC_SSH_KEY>" \
  <HUB_PRIVATE_IP> \
  <AGENT_TOKEN> \
  | sudo jupyterhub-pilot install-worker
hostname -I     # this worker's private IP, for add-worker on the Hub
```

**3. Register it on the Hub:**

```bash
sudo jupyterhub-pilot add-worker team_beta 172.31.10.22
```

It adds or updates the team in `/opt/jupyterhub-pilot/user_mapping.json`, loads it into SQLite and checks the Hub can SSH in. No restart needed. Pass the worker's sudo user as a third argument if it differs from the Hub's (e.g. `ec2-user`). Each team entry looks like:

```json
"team_beta": {
  "server_ip": "172.31.10.22",
  "server_ssh_key": "/opt/jupyterhub-pilot/keys/worker",
  "admin_ssh_user": "ubuntu"
}
```

| Field | Meaning |
|---|---|
| `server_ip` | Worker's **private** IP. It must be the address the worker connects from, or the dashboard will not show it to the team |
| `server_ssh_key` | Hub's private key for this worker |
| `admin_ssh_user` | Sudo user on the worker that creates Linux accounts for new users. Leave it out to turn that off |
| `ssh_user` | Optional. Makes every user of the team log in as this one shared account. Usually leave it out, so each user gets their own account |

**4. Create the group** `team_beta` in **Admin** → **Groups** and add its users. They can start servers right away.

To move a team to another VM: set up the new worker (step 2) and run `add-worker` with the same team name and the new IP. Users get the new VM the next time they start their server. Their notebook files stay on the old VM (`/home/<user>/notebook`); copy them over if needed.

## Remove a worker server

1. Stop the servers of that team's users (Admin page → **Stop Server**).
2. Remove the team from `user_mapping.json`, then delete it from SQLite. `seed_sqlite.py` only adds and updates, so this step is needed:

   ```bash
   sudo -u ubuntu sqlite3 /var/lib/jupyterhub/jupyterhub_pilot_state.db \
     "DELETE FROM team_mappings WHERE team = 'team_beta';"
   ```

3. Delete the group in **Admin** → **Groups**, or move its users to another team.
4. Copy any notebooks you want to keep from the worker's `/home/<user>/notebook`, then shut the VM down.

## Using the notebooks and AI magics

Users open `https://hub.example.com`, log in with Google and click **Start My Server**. Files live on their team's worker in `/home/<user>/notebook`.

Load the magics in a notebook:

```python
%load_ext jupyterhub_pilot.extension
```

| Magic | What it does |
|---|---|
| `%do <instruction>` | Writes code for the instruction into the next cell (`--run` runs it right away) |
| `%fix` | Reads the last error and proposes fixed code |
| `%review` | Reviews recent cells for bugs and style |
| `%rework <instruction>` | Rewrites the last cell as instructed |

The LLM backend is set in `~/.jupyterhub-pilot/config.json` (per user) or `/etc/jupyterhub-pilot/config.json` (whole worker). The default is a local Ollama at `http://localhost:11434`. Setup, auto-loading for all users and cloud/MCP backends: [AI Magic Commands](https://github.com/wajoud/jupyterhub-pilot/blob/main/docs/AI_MAGICS.md).

## Monitoring dashboard

Open `https://hub.example.com/hub/monitoring` (or **Monitoring** in the Hub's nav bar).

- You must be logged in.
- Admins see every worker and every user's CPU, memory (against their limit), kernels and home-folder size. Other users see only their own team's workers and their own usage row.
- A worker appears when its metrics agent connects with the right token and disappears when the agent stops.

Agent status, on a worker: `sudo systemctl status jupyterhub_pilot-metrics`. More: [Monitoring](https://github.com/wajoud/jupyterhub-pilot/blob/main/docs/MONITORING.md).

## Configuration reference

`/opt/jupyterhub-pilot/hub_settings.json` (restart the Hub after editing):

| Key | Meaning |
|---|---|
| `hub_ip` | Hub's private IP |
| `hub_port` | Public proxy port (default `8000`; Caddy forwards to it) |
| `hub_bind_url` | Where JupyterHub itself listens (`http://<HUB_IP>:8081`) |
| `hub_api_url` | Hub API URL notebook servers call (`http://<HUB_IP>:8081/hub/api`) |
| `proxy_api_url` | Proxy's control API |
| `hosted_domain` | Google Workspace domain allowed to log in |
| `admin_users` | List of admin usernames (no `@domain`) |
| `mapping_file` | JSON fallback for team→VM mappings, used if SQLite is unreachable |
| `db_path` | SQLite state DB |
| `resource_limits` | `memory_max` (e.g. `"1G"`) and `cpu_quota` (e.g. `"50%"`) per notebook server; `null` turns limits off |
| `vault_enabled`, `vault_secret_path` | Vault secret injection; see [Security](https://github.com/wajoud/jupyterhub-pilot/blob/main/docs/SECURITY.md) |

### Change the per-user RAM and CPU limit

Every user's notebook server gets the same limit (default `1G` RAM, `50%` of one core); a kernel over `memory_max` is killed, the worker and other users are not affected. The limit is per user, not per worker, so size it for how many users share a worker (5 users on a 2 GiB worker: about `300M`). To change it, on the Hub:

```bash
F=/opt/jupyterhub-pilot/hub_settings.json
sudo jq '.resource_limits = {"memory_max": "512M", "cpu_quota": "100%"}' $F | sudo tee $F.tmp >/dev/null && sudo mv $F.tmp $F
sudo jq .resource_limits $F      # check
sudo systemctl restart jupyterhub
```

- `memory_max`: `300M`, `512M`, `1G`, `2G`, ...
- `cpu_quota`: `50%` = half a core, `100%` = one core, `200%` = two cores
- No limits: `sudo jq '.resource_limits = null' ...`

The restart stops every running notebook server; new limits apply to servers started after it. Check a user's row on `/hub/monitoring`, which shows memory against the limit.

Environment variables:

| Variable | Where | Meaning |
|---|---|---|
| `CLIENT_ID`, `CLIENT_SECRET` | Hub | Google OAuth client |
| `OAUTH_CALLBACK_URL` | Hub | `https://<domain>/hub/oauth_callback` |
| `JUPYTERPILOT_AGENT_TOKEN` | Hub and every worker agent | Shared secret for the metrics agents. The Hub refuses all agents if it is unset |
| `VAULT_ADDR`, `VAULT_TOKEN` | Hub | Only when Vault is enabled |

## Logs and troubleshooting

| Where | Log |
|---|---|
| Hub | `sudo journalctl -u jupyterhub -f` |
| A user's notebook server (on the worker) | `/home/<user>/.jupyterhub-pilot-<user>.log` |
| Metrics agent (on the worker) | `sudo journalctl -u jupyterhub_pilot-metrics -f` |
| HTTPS | `sudo journalctl -u caddy -f` |

| Problem | Fix |
|---|---|
| Login keeps looping back to the login page | You are on `http://`. Use the `https://` domain behind Caddy |
| "User has no groups assigned" | Add the user to a team group (Admin → Groups) |
| "group ... not mapped" | The group name has no entry in SQLite. Add it to `user_mapping.json` and re-seed |
| Spawn fails with SSH timeout / refused | Check the worker's network rules and run the SSH test from [Add more worker servers](#add-more-worker-servers), step 5 |
| Admin account has no Admin page | `admin_users` must hold the name before the `@`, then restart the Hub |
| Server starts then dies | Read the user's notebook log on the worker (table above) |
| Dashboard shows no workers | Agent not running, wrong token, or the team's `server_ip` is not the worker's private IP |

More cases: [Installation Guide → Troubleshooting](https://github.com/wajoud/jupyterhub-pilot/blob/main/INSTALL.md#-troubleshooting).

## Development and tests

The tests run without real infrastructure (no SSH, EC2 or Vault):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -v tests/
```

```text
jupyterhub-pilot/
├── jupyterhub_pilot/      # Python package: AI magics, LLM providers, SQLite store,
│                          # monitoring handlers + dashboard, metrics agent, Vault client
│   ├── spawner.py         # CustomSpawner: SSH launch, JIT user creation, cgroups
│   ├── jupyterhub_config.py  # Hub config (Google login, spawner, monitoring routes)
│   ├── cli.py             # `jupyterhub-pilot` command
│   ├── scripts/           # install-hub, install-worker, add-worker (bash)
│   └── templates/         # adds the Monitoring link to the Hub nav bar
├── jupyterhub_config.py   # shim for installs made with git clone (0.4.0 and earlier)
├── tests/                 # unit tests (see tests/README.md)
└── docs/                  # architecture, security, AI magics, monitoring
```

### Test a cluster

Run these on the Hub after setup, with an admin API token (log in as the admin → **Token** → **Request new API token**). Use a test cluster only: it creates real users and servers. Watch `/hub/monitoring` while each step runs.

```bash
export JUPYTERHUB_API_TOKEN=<token>
HUB=http://<HUB_PRIVATE_IP>:8000
TEAMS=team_alpha,team_beta,team_gamma,team_delta   # your team names
```

**1. Servers start (2 users per worker, no workload)**

```bash
jupyterhub-pilot load-test --hub $HUB --teams $TEAMS --users 8 --no-workload
jupyterhub-pilot load-test --hub $HUB --users 8 --cleanup
```

Expect every user `ready`. Each user gets its own Linux account on its team's worker.

**2. Load (5 users per worker, each kernel holds 300 MB and keeps a core busy for 60 s)**

```bash
jupyterhub-pilot load-test --hub $HUB --teams $TEAMS --users 20
jupyterhub-pilot load-test --hub $HUB --users 20 --cleanup
```

Expect every kernel `finished`. The dashboard shows each worker's memory and CPU rising and one row per user.

**3. RAM limit (one kernel asks for more than `memory_max`)**

```bash
jupyterhub-pilot load-test --hub $HUB --teams team_alpha --users 1 --prefix oom --mem-mb 1200
jupyterhub-pilot load-test --hub $HUB --users 1 --prefix oom --cleanup
```

Expect `1 killed`. Only the kernel dies; the user's notebook server keeps running (check Admin, or `ps -u oom-001` on the worker).

**4. A worker goes down**

1. Start one server: `jupyterhub-pilot load-test --hub $HUB --teams team_delta --users 1 --no-workload`
2. Stop that team's worker VM. Its card leaves `/hub/monitoring` within seconds.
3. Watch `sudo journalctl -u jupyterhub -f | grep CustomSpawner.poll`. For 5 minutes the server still shows running (`SSH check failed`); then `marking 'lt-001' stopped` and Admin shows it stopped. **Stop Server** in Admin also clears it right away.
4. Start the VM again (its private IP stays the same, no `add-worker` needed). The card comes back in about a minute and the user can start again.
5. `jupyterhub-pilot load-test --hub $HUB --users 1 --cleanup`

`--cleanup` removes `--users` users with that `--prefix` (default `lt`, 10 users), so pass the same numbers you tested with. When done, revoke the token on the Token page.

Reference (2026-11-08, ap-south-1, Ubuntu 26.04 Arm, `t4g.small` Hub + 4 workers, 0.5.1, JupyterHub 6.2.0, limit `1G`/`50%`): step 1 8/8 ready, median 7.6 s; step 2 20/20 ready, median 8.8 s, max 13.3 s, 20 kernels finished; step 3 kernel killed, server kept running.

## More documentation

- [AWS EC2 Deployment Guide](https://github.com/wajoud/jupyterhub-pilot/blob/main/AWS_EC2_DEPLOYMENT.md): end-to-end on AWS
- [Installation Guide](https://github.com/wajoud/jupyterhub-pilot/blob/main/INSTALL.md): detailed manual setup
- [Architecture & EC2 Routing](https://github.com/wajoud/jupyterhub-pilot/blob/main/docs/ARCHITECTURE.md)
- [Security, Isolation & RBAC](https://github.com/wajoud/jupyterhub-pilot/blob/main/docs/SECURITY.md)
- [AI Magic Commands](https://github.com/wajoud/jupyterhub-pilot/blob/main/docs/AI_MAGICS.md)
- [Live Monitoring Dashboard](https://github.com/wajoud/jupyterhub-pilot/blob/main/docs/MONITORING.md)
- [Tests](https://github.com/wajoud/jupyterhub-pilot/blob/main/tests/README.md)

