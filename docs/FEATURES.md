# Features

Everything a JupyterHub-Pilot cluster can do, by where it runs: the **Hub** (one VM: login, routing, admin pages), each team's **worker** (a VM where users' notebooks run), and the **notebook** itself (what users type). How-to steps are in the [README](../README.md); this page is the list.

**Release:** `0.5.1` is on PyPI. `0.6.0` is merged into `main` but not on PyPI yet ([CHANGELOG](../CHANGELOG.md)).
**EC2:** **Tested** = checked on a real cluster (2026-11-07/08, Ubuntu 26.04 Arm, JupyterHub 6.2). **Not yet** = unit-tested only; [README → Test a cluster](../README.md#test-a-cluster) and the EC2 checklist cover it. **—** = no cluster test recorded.

## Hub

### Users and teams

| Feature | What it does | How | Release | EC2 |
|---|---|---|---|---|
| Team routing | Each JupyterHub group is a team with its own worker VM. A user's notebook runs on their team's worker | Admin → Groups; `sudo jupyterhub-pilot add-worker <team> <ip>` ([Add users and teams](../README.md#add-users-and-teams)) | 0.5.1 | Tested |
| Google login | Only accounts from your Google Workspace domain can log in; the user name is the part before `@` | `hosted_domain` in `hub_settings.json`, Google client in `hub.env` | 0.5.1 | — |
| Test login | Log in with a shared password, no Google needed (testing only) | `JUPYTERPILOT_TEST_PASSWORD` + `JUPYTERPILOT_TEST_ADMIN_PASSWORD` in `hub.env` ([Option C](../README.md#option-c-test-login-no-google-testing-only)) | 0.5.1 | Tested |
| Admins | Admins manage users, groups and limits, but cannot open other users' notebooks | `admin_users` in `hub_settings.json`, or the Admin page | 0.5.1 | Tested |
| No-team message | A user with no team gets "ask an admin to add you to a team" instead of an error page | Automatic | 0.5.1 | Tested |
| Linux account per user | On first start the worker creates the user's own Linux account (`alice`), so users can't read each other's files | Automatic | 0.5.1 | Tested |
| Delete a user cleans the worker | Deleting a Hub user removes their Linux account on the worker; their home is archived in `/home/.deleted/` (or deleted, or kept) | `on_user_delete` ([Delete a user](../README.md#delete-a-user)) | 0.6.0 | Not yet |

### Limits and control

| Feature | What it does | How | Release | EC2 |
|---|---|---|---|---|
| RAM/CPU limit for everyone | Each notebook server runs under a memory and CPU cap; a kernel over the memory cap is killed, the server and other users are not | `resource_limits` in `hub_settings.json` (default `1G`, `50%`) | 0.5.1 | Tested |
| RAM/CPU limit per team | A different cap for one team | Admin → Groups → team → Properties: `memory_max`, `cpu_quota` ([limits](../README.md#change-the-per-user-ram-and-cpu-limit)) | 0.6.0 | Not yet |
| RAM/CPU limit per user | A different cap for one user; wins over the team's | **Limits** page in the nav bar (admins) | 0.6.0 | Not yet |
| Terminal off | Remove New → Terminal for everyone, or for one user | `terminals_enabled: false`; **Terminal off** on the Limits page ([Terminal](../README.md#terminal-and-jupyterlab)) | 0.6.0 | Not yet |
| Idle servers stopped | A server unused for 3 hours is stopped, freeing worker RAM; files are kept | `idle_cull` ([Idle servers](../README.md#idle-servers-are-stopped-after-3-hours)) | 0.6.0 | Not yet |
| Worker down handling | If a worker is unreachable for 5 minutes, its users' servers are shown stopped so they can start again later | Automatic | 0.5.1 | Tested |
| Crash-safe state | Running servers and team mappings live in SQLite (JSON fallback), so a Hub restart doesn't lose them | Automatic | 0.5.1 | Tested |

### Secrets

| Feature | What it does | How | Release | EC2 |
|---|---|---|---|---|
| Vault secrets | Each user's keys and passwords from HashiCorp Vault become environment variables in their notebook | `vault_enabled`, `VAULT_ADDR`/`VAULT_TOKEN` ([walkthrough](SECURITY.md#4-vault-secret-injection)) | 0.5.1 (docs 0.6.0) | Not yet |
| Secrets never on the command line | The Hub sends tokens and secrets to the worker over SSH stdin, so `ps` on the worker can't show them | Automatic | 0.5.1 | Tested |

### Monitoring and operations

| Feature | What it does | How | Release | EC2 |
|---|---|---|---|---|
| Monitoring dashboard | Live CPU, RAM, disk and network per worker, and each user's memory/CPU against their limit, kernels, home size, uptime. Admins see all; users see their team's workers and their own row | `/hub/monitoring` ([Monitoring](MONITORING.md)) | 0.5.1 | Tested |
| Install from pip | One command per role; settings and keys in `/opt/jupyterhub-pilot`, no git clone | `jupyterhub-pilot install-hub`, `install-worker`, `add-worker` | 0.5.1 | Tested |
| Upgrade in place | Upgrades the package and applies only what the new release needs; warns about workers on older versions, asks before restarting the Hub | `sudo jupyterhub-pilot upgrade` ([Upgrading](../README.md#upgrading)) | 0.6.0 | Not yet |
| Load test | Starts many test users at once; reports start times and which kernels hit their limit | `jupyterhub-pilot load-test` ([Test a cluster](../README.md#test-a-cluster)) | 0.5.1 | Tested |
| systemd service | The Hub runs as `jupyterhub.service`, secrets in a root-only `hub.env` | `sudo systemctl enable --now jupyterhub` | 0.5.1 | Tested |

## Worker

| Feature | What it does | How | Release | EC2 |
|---|---|---|---|---|
| Users' notebook servers | Each user's server runs as their Linux user, in a cgroup with their RAM/CPU limit, files in `/home/<user>/notebook` | Automatic on Start My Server | 0.5.1 | Tested |
| Metrics agent | Sends the worker's and each user's usage to the Hub dashboard | `jupyterhub_pilot-metrics` service, set up by `install-worker` | 0.5.1 | Tested |
| Team folder | `/data/<team>`: read/write for everyone in the team, shown as `team/` in the file browser | Automatic at server start ([Team folder](../README.md#team-folder-and-shared-data)) | 0.6.0 | Not yet |
| Shared data folder | `/data/shared`: read-only for users, admins copy datasets there; shown as `shared/` | `sudo cp data.parquet /data/shared/` on the worker | 0.6.0 | Not yet |
| Same data stack everywhere | pandas, polars, DuckDB, scikit-learn, matplotlib, plotly, jupysql, papermill on every worker | Part of the `worker` install ([Data packages](../README.md#data-packages)) | 0.6.0 | Not yet |
| Git tools | `git`, the JupyterLab Git panel and `nbdime` notebook diffs | Part of the `worker` install ([Git](../README.md#git)) | 0.6.0 | Not yet |
| Scheduled runs | Notebooks run on a timer as the user, under their limit, with their Vault secrets; survive restarts | User timers in `~/.config/systemd/user` (see `%schedule` below) | 0.6.0 | Not yet |

## Notebook (for users)

| Feature | What it does | How | Release | EC2 |
|---|---|---|---|---|
| `%do` | Writes code for an instruction into the next cell (`--run` runs it) | `%load_ext jupyterhub_pilot.extension` ([AI magics](AI_MAGICS.md)) | 0.5.1 | — |
| `%fix` | Reads the last error and proposes fixed code | same | 0.5.1 | — |
| `%review` | Reviews recent cells for bugs and style | same | 0.5.1 | — |
| `%rework` | Rewrites the last cell as instructed (`--diff` to preview) | same | 0.5.1 | — |
| `%explain` | Explains the last result in plain words | same | 0.6.0 | Not yet |
| DataFrame-aware AI | The magics see the column names and types of DataFrames in memory (never the data) | Automatic | 0.6.0 | Not yet |
| AI backends | Local Ollama (default), OpenAI / Claude / Gemini through LiteLLM, or an MCP server | `~/.jupyterhub-pilot/config.json` or `/etc/jupyterhub-pilot/config.json` | 0.5.1 | — |
| `%schedule` | Run a notebook hourly/daily/on any calendar time; results saved next to it as `name-<date>.ipynb`, last N kept; `list`, `run`, `pause`, `resume`, `delete` | `%load_ext jupyterhub_pilot.schedule` ([Scheduled notebooks](../README.md#scheduled-notebooks)) | 0.6.0 | Not yet |
| SQL in cells | `%sql` over Parquet/CSV files with DuckDB | `%load_ext sql`, `%sql duckdb://` | 0.6.0 | Not yet |
| Terminal | A shell as the user on the team's worker, no sudo, same limits | New → Terminal | 0.5.1 (documented 0.6.0) | Not yet |
| JupyterLab | The full Lab interface, AI magics included | `https://<hub>/user/<name>/lab` | 0.5.1 (documented 0.6.0) | Not yet |
| Secrets | The user's Vault secrets as environment variables | `os.environ["KEY"]` | 0.5.1 | Not yet |

## Not yet

What the cluster does **not** do today (most are ideas in [TODO.md](../TODO.md)):

- **Backups:** notebooks live only on the worker's disk. Losing the worker's disk loses them.
- **One team per user:** a user in several groups runs on the first one's worker (order not guaranteed), and moving a user to another team leaves their notebooks on the old worker. Planned: choose the team at start, or shared EFS homes ([TODO.md](../TODO.md#users-in-more-than-one-team)).
- **Moving a team** to a new worker doesn't move its users' files.
- **GPU or per-team worker sizes**, and stopping idle workers to save cost.
- **Sharing a notebook** as a read-only page or dashboard; **real-time collaboration**.
- **Usage reports** per team over time (the dashboard is live only).
- **Tested systems:** Ubuntu 24.04 and 26.04. Amazon Linux 2023 has install scripts but has not run on a real instance.
