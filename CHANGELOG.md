# Changelog

Each release says where to upgrade (**Hub**, **workers**, or both) and whether a restart is needed. Upgrade with `sudo jupyterhub-pilot upgrade` on each machine (Admin guide → Upgrading).

## 0.9.0 (not released yet)

**Upgrade: Hub only.** Hub restart needed. To use the new page, write the Vault policy again (Security → Vault, step 2).

- **Secrets page** (`/hub/secrets`, admins, shown when Vault is on): add, replace and delete the Vault variables of a team or a user from the browser. It shows names only, never values, and marks users who must restart to get a change.
- **Team secrets:** variables stored at `<vault_secret_path>/_team/<team>` go to every member's server. A user's own variable with the same name wins.
- A Vault key that is not a valid variable name is now skipped with a warning (it used to go into the start script as it was).

## 0.8.0 (not released yet)

**Upgrade: Hub and every worker** (workers for the GPU numbers and per-user process counts). Hub restart needed.

Hub:
- **Limits page redone:** every user with the limit they get and where it comes from (user, team or default), what their running server uses, and a mark when it is still on an older limit. Team limits are set on the same page. Saving a user's row with empty boxes now clears their limits (was an error).
- **Monitoring page shows more:** worker cards titled with the team name, each worker's size, one bar per CPU core, the busiest users right now, GPU and fullest-disk totals, what the memory limits on a worker add up to.
- **Usage history:** a "1 hour" range (a new cluster no longer looks empty), totals for the chosen period, and a **Most active users** table for admins.
- Prometheus: GPU gauges (`worker_gpu_*`, `user_gpu_memory_mb`).
- `install-hub` installs `nodejs` and `npm` without the recommended extras (340+ fewer packages).

Docs:
- The README is now a short overview with screenshots. Its how-to sections moved to `docs/ADMIN.md` (install, teams, workers, limits, upgrades, troubleshooting), `docs/USING.md` (notebooks) and `docs/DEVELOPMENT.md`.

Workers:
- **GPU workers:** the metrics agent reports NVIDIA GPUs (usage, memory, temperature, memory per user) where `nvidia-smi` exists. `install-worker` installs the driver only on a machine with a GPU (Ubuntu) and asks for a reboot. Workers without a GPU are unchanged.

## 0.7.0 (2026-11-10)

Includes everything listed under 0.6.0 below, which was never published on its own.

**Upgrade: Hub and every worker** (workers only for the failed-runs list). Hub restart needed.

Hub:
- **Users in several teams choose one at start** ("Which team?" on the start page). One-team users see no change. Deleting such a user removes their account on every team's worker.
- **The Hub renews its own Vault token** daily; drop the `vault-renew` cron job. Add the `auth/token/renew-self` path to the policy (`docs/SECURITY.md` §4, step 2).
- Stop Server returns as soon as the server has exited (was always 5 s).
- A slow Vault no longer freezes the Hub while a server starts.
- After a worker reboot a server no longer shows as running because another program got its process number.
- If a user's Linux account cannot be created, the start page says so (was an SSH login error).
- Shared homes: the user's own group gets the same ID as the user on every worker; team-folder links work for users not yet moved.

- **Shared home folders (EFS):** `home_base` in `hub_settings.json`; every user gets the same Linux user ID on every worker (Admin guide → Shared home folders).
- **Monitoring page redesigned:** what needs attention first, compact worker cards, light and dark, no external scripts.
- **15 days of per-user usage** (CPU, memory) stored in the state DB and graphed on the monitoring page.
- **Prometheus endpoint** for Grafana: `/hub/monitoring/metrics`, off until `JUPYTERPILOT_METRICS_TOKEN` is set.

Workers:
- Scheduled notebooks whose last run failed are listed on `/hub/monitoring`.
- `sudo jupyterhub-pilot mount-homes <efs-dns-name>` mounts the shared home filesystem.
- A scheduled notebook now runs only on the worker where it was scheduled (matters with shared homes).

## 0.6.0 (not published; part of 0.7.0)

**Upgrade: Hub and every worker.** The Hub restart stops every running server; `upgrade` asks first.
The first time, install the new version by hand, since 0.5.x has no `upgrade` command:
`sudo pip3 install --break-system-packages --ignore-installed -U "jupyterhub-pilot[hub]"` (`[worker,ai]` on workers), then `sudo jupyterhub-pilot upgrade --no-pip`.

Hub:
- **Idle servers are stopped after 3 hours** (`jupyterhub-idle-culler`, `idle_cull` in `hub_settings.json`). `upgrade` adds the setting; set it to `null` to keep culling off.
- **Deleting a Hub user removes their Linux account on the worker.** Their home is archived in `/home/.deleted/` (`on_user_delete`: `archive`, `delete` or `keep`).
- **RAM/CPU limits per team** (group properties `memory_max`, `cpu_quota` in Admin → Groups) **and per user** (new admin-only **Limits** page). No Hub restart needed.
- Terminal off for everyone (`terminals_enabled: false`) or for one user (**Terminal off** on the Limits page).
- `jupyterhub-pilot upgrade` command.

Workers:
- **Team folder** `/data/<team>` and read-only `/data/shared`, linked in every user's file browser.
- **Scheduled notebooks:** `%load_ext jupyterhub_pilot.schedule`, then `%schedule add daily report.ipynb`.
- Same data stack on every worker: pandas, polars, DuckDB, scikit-learn, matplotlib, plotly, jupysql, papermill (about 600 MB more disk).
- Git panel (`jupyterlab-git`) and `nbdime`; `git` and `acl` OS packages (`upgrade` installs them).
- AI magics see DataFrame columns and dtypes; new `%explain`.

Docs: Vault end to end (`docs/SECURITY.md` §4), Terminal and JupyterLab, Test a cluster steps 5 (idle culling) and 6 (Vault).
