# Changelog

Each release says where to upgrade (**Hub**, **workers**, or both) and whether a restart is needed. Upgrade with `sudo jupyterhub-pilot upgrade` on each machine (README → Upgrading).

## 0.6.0 (unreleased)

**Upgrade: Hub and every worker.** The Hub restart stops every running server; `upgrade` asks first.
The first time, install 0.6.0 by hand, since 0.5.x has no `upgrade` command:
`sudo pip3 install --break-system-packages --ignore-installed -U "jupyterhub-pilot[hub]"` (`[worker,ai]` on workers), then `sudo jupyterhub-pilot upgrade --no-pip`.

Hub:
- **Idle servers are stopped after 3 hours** (`jupyterhub-idle-culler`, `idle_cull` in `hub_settings.json`). `upgrade` adds the setting; set it to `null` to keep culling off.
- **Deleting a Hub user removes their Linux account on the worker.** Their home is archived in `/home/.deleted/` (`on_user_delete`: `archive`, `delete` or `keep`).
- **RAM/CPU limits per team** (group properties `memory_max`, `cpu_quota` in Admin → Groups) **and per user** (new admin-only **Limits** page). No Hub restart needed.
- `terminals_enabled: false` turns off the Terminal for every server.
- `jupyterhub-pilot upgrade` command.

Workers:
- **Team folder** `/data/<team>` and read-only `/data/shared`, linked in every user's file browser.
- **Scheduled notebooks:** `%load_ext jupyterhub_pilot.schedule`, then `%schedule add daily report.ipynb`.
- Same data stack on every worker: pandas, polars, DuckDB, scikit-learn, matplotlib, plotly, jupysql, papermill (about 600 MB more disk).
- Git panel (`jupyterlab-git`) and `nbdime`; `git` and `acl` OS packages (`upgrade` installs them).
- AI magics see DataFrame columns and dtypes; new `%explain`.

Docs: Vault end to end (`docs/SECURITY.md` §4), Terminal and JupyterLab, Test a cluster steps 5 (idle culling) and 6 (Vault).
