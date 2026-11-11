# Using the notebooks

For the people who write notebooks: AI magics, the terminal, team folders, scheduled notebooks, Git and packages.

← [Back to the README](../README.md)

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
| `%explain [question]` | Explains the last result in plain words |

The magics see the column names and dtypes of DataFrames in memory, so `%do plot amount by region` uses your real columns.

The LLM backend is set in `~/.jupyterhub-pilot/config.json` (per user) or `/etc/jupyterhub-pilot/config.json` (whole worker). The default is a local Ollama at `http://localhost:11434`. Setup, auto-loading for all users and cloud/MCP backends: [AI Magic Commands](https://github.com/wajoud/jupyterhub-pilot/blob/main/docs/AI_MAGICS.md).

### Terminal and JupyterLab

Notebook 7 is built on JupyterLab, so both come with the worker install:

- **Terminal:** in the file browser, **New** → **Terminal**. It is a shell on the team's worker as the user's own Linux account: no `sudo`, same files as the notebooks, and the same RAM/CPU limit (it runs inside the server's cgroup).
- **JupyterLab:** open `https://hub.example.com/user/<name>/lab`, or **View** → **Open JupyterLab** in a notebook. The AI magics work there too. The Hub opens the classic Notebook view (`/tree`) by default.

Programs started in a terminal keep running after the browser is closed and count against the user's limit until they end, the user's server stops, or the idle culler stops the server.

**Turn the terminal off for one user:** **Limits** in the nav bar (admins only) → the user's row → tick **off** under Terminal → **Save**. It applies on their next server start, no Hub restart. **Clear** on their row turns it back on.

**Turn terminals off for everyone:** set `terminals_enabled` to `false` in `hub_settings.json` and restart the Hub; each server started after that has no **Terminal** menu.

```bash
F=/opt/jupyterhub-pilot/hub_settings.json
sudo jq '.terminals_enabled = false' $F | sudo tee $F.tmp >/dev/null && sudo mv $F.tmp $F
sudo systemctl restart jupyterhub
```

This does not stop users running shell commands: a kernel can do the same with `!ls` or `os.system`. It only removes the interactive shell.

### Team folder and shared data

Every user's file browser has two links, set up on each worker at server start:

| Folder | Path on the worker | Who can write |
|---|---|---|
| `team` | `/data/<team>` | Everyone in the team (a Linux group with the team's name; new files stay team-writable) |
| `shared` | `/data/shared` | Admins only (read-only for users): put reference datasets here |

```bash
# On a worker, as the admin user: publish a dataset to every user of that worker
sudo cp sales_2026.parquet /data/shared/
```

A user added to a team gets the `team` link on their next server start. If they already had their own file or folder named `team` or `shared` in `~/notebook`, it is left alone and no link is made. Teams with a shared `ssh_user` don't get the links. `/data` lives on the worker's disk, so it is not shared between workers and not backed up.

### Scheduled notebooks

Run a notebook on a timer, with no browser or server open. It runs on the team's worker as the user, under their RAM/CPU limit and with their Vault secrets:

```python
%load_ext jupyterhub_pilot.schedule
%schedule add daily report.ipynb                 # every day at 00:00 (worker's clock, usually UTC)
%schedule add "Mon..Fri 07:30" report.ipynb --keep 10
%schedule list                                    # name, when, next run, last result
%schedule run report                              # run now
%schedule pause report      /  %schedule resume report
%schedule delete report                           # past results are kept
```

- **When:** systemd calendar times: `hourly`, `daily`, `weekly`, `"Mon..Fri 08:00"`, `"*-*-* 02:30"`, `"*-*-01 06:00"` (monthly). `man systemd.time` has the full syntax; a bad one is refused with examples.
- **Results:** each run saves the executed notebook next to the original as `report-20261109-0730.ipynb`, or `report-20261109-0730-FAILED.ipynb` if a cell raised (open it to see the error). The last 5 (`--keep N`) are kept.
- **Restarts:** jobs live in the user's systemd timers on the worker (`~/.config/systemd/user/jhp-*`), so they survive Hub and worker restarts. A run missed while the worker was off runs when it comes back.
- The limit and secrets are copied when you run `%schedule add`. After your limit or secrets change, run the same `add` again to update the job.
- Deleting the user removes their jobs with their Linux account.
- A job whose last run failed is listed on `/hub/monitoring` under its worker (the user sees their own, admins see all) until its next good run or `%schedule delete`.

Admins, on a worker: every job is `sudo sh -c 'ls /home/*/.config/systemd/user/jhp-*.timer'`, and a user's run log is `sudo journalctl _UID=$(id -u <user>) --user-unit 'jhp-*'` (or `systemctl --user` as that user).

### Git

Workers have `git`, the JupyterLab Git panel (`jupyterlab-git`) and `nbdime` for notebook diffs. In JupyterLab (`/user/<name>/lab`), the **Git** tab on the left clones, commits, pushes and shows notebook diffs cell by cell. In a terminal, `nbdiff old.ipynb new.ipynb` shows the same, and `nbdime config-git --enable --global` makes `git diff` use it.

To push to GitHub, each user adds their own key from a terminal (`ssh-keygen -t ed25519`, then add `~/.ssh/id_ed25519.pub` to their GitHub account) or uses an HTTPS token. Keys stay in the user's home on the team's worker.

### Data packages

Every worker has the same data stack (part of the `worker` install): pandas, polars, DuckDB, scikit-learn, matplotlib, plotly and `jupysql`, so a notebook runs the same on any team's worker. SQL over files with DuckDB:

```python
%load_ext sql
%sql duckdb://
%sql SELECT region, sum(amount) FROM 'shared/sales_2026.parquet' GROUP BY region
```

The packages are installed system-wide with minimum versions. Run `sudo jupyterhub-pilot upgrade` on all workers at the same time to keep them on the same versions. Users can add their own in a terminal or a `!` cell: `pip install --user --break-system-packages <package>` (Ubuntu refuses plain `pip install` on the system Python; `--user` keeps it in their home).
