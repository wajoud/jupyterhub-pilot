# Changelog

Each release says where to upgrade (**Hub**, **workers**, or both) and whether a restart is needed. Upgrade with `sudo jupyterhub-pilot upgrade` on each machine (README → Upgrading).

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

- **Shared home folders (EFS):** `home_base` in `hub_settings.json`; every user gets the same Linux user ID on every worker (README → Shared home folders).
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
