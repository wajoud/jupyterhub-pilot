# TODO

Findings from the 2026-11-07 EC2 test (Ubuntu 24.04 Arm, 1 Hub + 2 workers, us-east-1, JupyterHub 6.2.0) are grouped
under "Next release". Hub install, worker installs, team routing, JIT user creation, cgroup limits and metrics agents all worked.

## Next release (branch `fix/ec2-test-findings`)

Released in 0.4.0. Verified on 2026-11-08 in ap-south-1 (Ubuntu 26.04 Arm, 1 Hub + 2 workers, JupyterHub 6.2.0).

### Monitoring
- [x] Metrics agent URL is now `ws://HUB:8000/hub/monitoring/ws` (JupyterHub serves extra handlers under `/hub/`; the old URL cost a `302` per connect).
  Workers installed from older `main` need: `sudo sed -i 's#:8000/monitoring/ws#:8000/hub/monitoring/ws#' /etc/systemd/system/jupyterhub_pilot-metrics.service`.
- [x] Verified: admin sees both workers; a `team_beta` user sees only beta's.
- [ ] Later (not this release): `JupyterHub.extra_handlers` is deprecated since 3.1 but still works on 6.2. Moving monitoring to a JupyterHub service is a larger change.

### Speed
- [x] Docs: pick the region closest to users (measured from India: us-east-1 ~300 ms / ~20 KB/s vs ap-south-1 ~40 ms); keep Caddy in front even for tests.
- [x] `encode zstd gzip` in the Caddyfiles (README, AWS guide). Measured 8× faster downloads through Caddy.
- [x] No-domain test setup in README: Caddy `tls internal` on the public IP, with the `default_sni` line it needs.
- [x] Verified: from India, ap-south-1 via Caddy: 0.03 s connect, ~870 KB/s (us-east-1 was ~20 KB/s); Notebook loads in seconds.

### Install and first run
- [x] `scripts/add_worker.sh <team> <ip> [admin_ssh_user]`: upserts the team, re-seeds SQLite, tests SSH.
- [x] Test login without Google: set `JUPYTERPILOT_TEST_PASSWORD` + `JUPYTERPILOT_TEST_ADMIN_PASSWORD` (uses JupyterHub's `shared-password` authenticator). No config file edits.
- [x] Re-running `install_hub.sh`: `git pull --autostash` (and `safe.directory`, since root runs git in a user-owned clone); keeps teams already in `user_mapping.json`.
- [x] Install scripts clone the same branch as the clone they are run from (so testing a branch installs that branch).
- [x] No team group → a `403` page with "ask an admin to add you to a team" instead of a bare `500`.
- [x] `jupyterhub>=5.3,<7` in `pyproject.toml`; README badge says 5.3+ | 6.x.
- [x] `install_hub.sh` writes `/etc/systemd/system/jupyterhub.service` and a root-only `/etc/jupyterhub-pilot/hub.env` (agent token filled in, test-login lines ready). Start with `sudo systemctl enable --now jupyterhub`.
- [x] `cookie_options` moved into `tornado_settings`, where JupyterHub reads it. The login cookie is now really `Secure`, so plain `http://` logins loop: use Caddy.
- [x] Verified in ap-south-1: install scripts, systemd unit, shared-password login, `add_worker.sh`, team routing, Secure cookie, HTTPS via Caddy.

## Next release: findings from the 2026-11-08 ap-south-1 run

- [x] `add_worker.sh` run on a worker by mistake wrote `user_mapping.json` there, then crashed with a SQLite traceback. Check it is on the Hub (key + DB present) before changing anything, and say "run this on the Hub".
- [x] Monitoring page: the CPU history line is drawn over the "Last update" text.
- [x] A test password under 8 characters stops the Hub from starting. Say "8+ characters" in the `hub.env` comment, and check the length in `install_hub.sh` docs.
- [x] Docs: default EC2 root disk (~8 GB) is half full after the worker install. Recommend 20–30 GiB per worker and show how to grow the volume.
- [x] The first metrics-agent connect after a Hub start can hit `404` (agent connects before the Hub's routes are added); it recovers on the next retry, but backoff could reach 60 s. Backoff now caps at 10 s.
- [x] A stopped worker left its users' servers listed as running forever (`poll()` treated every failed SSH check as a blip), and Stop Server failed for them. A worker unreachable for 5 minutes now marks its servers stopped.
- [x] Verified on EC2 (2026-11-08): stop a worker, its card leaves the dashboard at once and its servers show stopped after ~5 min; start it, the card returns and users can start again.
- [ ] Check the dashboard card: the CPU history line no longer covers the "Last update" text.

## Test Amazon Linux 2023 support end to end

Amazon Linux 2023 (default user `ec2-user`) now has its own install scripts next to the Ubuntu ones:
`scripts/install_hub_amazon_linux.sh` and `scripts/install_worker_amazon_linux.sh`. The spawner falls back to
`useradd` when Debian's `adduser` flags are rejected. None of this has run on a real AL2023 instance yet.

- [ ] Run both scripts on fresh AL2023 instances (run `sudo dnf install -y git` first so you can clone).
- [ ] Check that `dnf install nodejs npm` provides `npm`; if not, use `nodejs-npm`.
- [ ] Check that pip on the system Python 3.9 resolves working versions of JupyterHub 5, oauthenticator, notebook and the `[ai]` extra. If not, switch the scripts to `python3.11`.
- [ ] Check that the Vault RPM repo installs, or that the script skips it cleanly.
- [ ] Spawn a server: the user is created with `useradd`, SSH key login works for the new user, and the `systemd-run --user` cgroup memory/CPU limits apply.
- [ ] The metrics agent service starts and shows up on `/hub/monitoring`.
- [ ] After a passing run, add AL2023 to the tested OS list in `README.md`.

## Make `pip install jupyterhub-pilot` enough (no git clone)

Done on branch `feature/pip-install` (release as 0.5.0).

- [x] `spawner.py`, `jupyterhub_config.py`, `templates/` and the setup scripts moved into the package; the repo-root `jupyterhub_config.py` is a shim so clone-based installs keep working.
- [x] Settings and keys live in `/opt/jupyterhub-pilot` (`JUPYTERHUB_PILOT_DIR`), code comes from pip.
- [x] `jupyterhub-pilot` command: `install-hub`, `install-worker`, `add-worker`, `load-test`. One script per role for Ubuntu and Amazon Linux.
- [x] Metrics agent and seeding run as `python -m jupyterhub_pilot.…`; no `get_port.py` (inline `python3 -c`).
- [x] Docs (README, AWS guide, INSTALL.md, MONITORING.md).
- [x] Verified on EC2 from PyPI (0.5.1), see below.

## 2026-11-08 test: 0.5.1 from PyPI, 1 Hub + 4 workers

ap-south-1, Ubuntu 26.04 Arm, `t4g.small` everywhere, JupyterHub 6.2.0, Notebook 7.6.5, limit `1G`/`50%`, test login behind Caddy `tls internal`. Steps are in README → Test a cluster.

- [x] `install-hub`, `install-worker` (answers piped in), `add-worker` for 4 teams.
- [x] 8 users, no workload: 8/8 ready, median 7.6 s, max 9.3 s.
- [x] 20 users, 300 MB + 60 s CPU each: 20/20 ready, median 8.8 s, max 13.3 s, 20 kernels finished.
- [x] 1200 MB kernel against `1G`: kernel killed, notebook server kept running.
- [x] Worker stopped: servers marked stopped after ~5 min; worker restarted, users start again.
- Docs gaps found and fixed in PR #14: `apt update` before pip, public IP + local check for no-domain Caddy, admin uses the admin password, no-prompt `install-worker`, per-user limit how-to.

## Delete the worker's Linux user when a Hub user is deleted

Today deleting a user in the Hub (Admin page, API, or `load-test --cleanup`) leaves their Linux account and `/home/<user>` on the team's worker.

- [x] `CustomSpawner.delete_forever()` (JupyterHub calls it on user delete): SSH as `admin_ssh_user`, kill leftover processes, disable linger, remove the account.
- [x] `on_user_delete` setting: `archive` (default: home moved to `/home/.deleted/<user>-<date>`), `delete`, `keep`.
- [x] Shared `ssh_user` teams skipped; unreachable worker logs `NOT deleted`, the Hub user is still removed.
- [x] Tests; README (Delete a user, Remove a worker server, Test a cluster cleanup), `load_test.py` docstring.
- [ ] Check on a cluster: `load-test --cleanup` removes `lt-*` accounts and archives their homes.

## Change RAM/CPU limits from the Admin panel

Today `resource_limits` in `hub_settings.json` is one limit for every user, and changing it needs a Hub restart.

- [x] Team limits from group properties (`memory_max`, `cpu_quota` in Admin → Groups); per-user limits on a new admin-only **Limits** page (`/hub/limits`, stored in SQLite).
- [x] Order per value: user → team → `hub_settings.json` default; read at every spawn, no Hub restart.
- [x] Values validated (`512M`/`1G`/`2T`, `50%`/`200%`); the page refuses bad ones, a bad group property is skipped with a log warning.
- [x] The Hub log says which limit applied and from where; `/hub/monitoring` already shows the real cgroup limit. Tests; README.
- [ ] Check on a cluster: set a team property and a user override, restart the user's server, `/hub/monitoring` shows the new limit.

## Make Vault clear: how it works and how to use it

`install-hub` only installs the `vault` binary (on Ubuntu 26.04 from HashiCorp's 24.04 repo). It does not start a Vault server, create a token or a policy, or say how a user reads their secrets. `docs/SECURITY.md` §4 assumes a Vault server already exists.

- [x] Docs (`docs/SECURITY.md` §4): what `install-hub` does and doesn't do, dev-mode and production server, read-only policy + periodic token (+ weekly renew cron), `hub.env` + `vault_enabled`, adding and reading secrets, log lines, who can see what.
- [x] README → Test a cluster step 6 (Vault).
- [ ] Check on the Hub whether `vault` got installed on Ubuntu 26.04 (`vault version`); if `install-hub` printed "Vault not installed", say so in the docs.
- [ ] Run step 6 on a cluster.
- [ ] Later: have the Hub renew its own token (`/v1/auth/token/renew-self`) so the cron job is not needed.

## Upgrades: update only what changed, keep everything working

Today an upgrade is `pip install -U` + a restart (README → Upgrading). That only updates the Python code. When a release also changes the systemd unit, `hub.env`, `hub_settings.json` keys or the SQLite tables, the admin has to fix it by hand (0.4.0's metrics URL change needed a `sed` on every worker) or run `install-hub` / `install-worker` again.

- [ ] `sudo jupyterhub-pilot upgrade` on the Hub and on each worker: upgrade the package, apply only what changed, restart only the services that need it. Never re-run the full install, never touch keys, tokens, passwords, teams or users' files.
- [ ] Keep a version in `/opt/jupyterhub-pilot` so `upgrade` knows which steps to run (e.g. 0.5 → 0.6: add a new setting with its default, rewrite the unit, add a DB column). Each step safe to run twice.
- [ ] Add new `hub_settings.json` keys with defaults; keep the admin's existing values.
- [ ] Hub and workers on the same version: `upgrade` checks the workers (over SSH) and warns if one is behind; Hub restart warning, since it stops users' servers.
- [ ] Release notes say for each release: Hub only, workers only, or both; and whether a restart is needed.
- [ ] Test: install the previous release, run the Test a cluster steps, `upgrade`, run them again; settings, teams and users' files unchanged.

## Scheduled notebooks (cron tasks), e.g. with Airflow

Users want to pick an `.ipynb` and have it run on a schedule (every night, every hour), without keeping a browser or kernel open.

- [ ] Decide the engine. Airflow is the full option (DAGs, retries, history UI) but is heavy: its own DB, scheduler and web server. Lighter options to compare first: `papermill` run by a systemd timer on the team's worker, or the `jupyter-scheduler` extension (check it works in Notebook 7).
- [ ] The run must happen **on the user's team worker, as that user**, under their RAM/CPU limit and with their Vault secrets, never on the Hub and never as root. With Airflow: one Airflow on the Hub (or its own VM) that SSHes to the worker and runs `papermill` as the user (same path as the spawner).
- [ ] How a user schedules: a `%schedule "0 2 * * *"` magic in the notebook, or a Schedule button; list / pause / delete their own jobs. Admins see all jobs.
- [ ] Output: save the run's executed notebook next to the original (`<name>-<date>.ipynb`) and keep N runs; failed runs visible to the user (and on `/hub/monitoring`).
- [ ] Jobs survive Hub/worker restarts; a run whose worker is down is retried or marked failed, not lost silently. Delete the user → delete their jobs.
- [ ] Docs (README section + Airflow setup if chosen) and a step in Test a cluster.

## Terminal access and JupyterLab

Notebook 7 is built on JupyterLab and pulls in `jupyterlab` and the terminal extension, so both are probably available already but untested and undocumented: **New → Terminal** in the file browser, and `https://<hub>/user/<name>/lab`. The Hub opens `/tree` (`c.Spawner.default_url`).

- [ ] Check on a cluster: terminal opens, runs as the user's own Linux account on the team worker, no `sudo`, and stays under their RAM/CPU limit (a `stress` in the terminal is capped like a kernel). `/lab` loads with the AI magics working.
- [ ] Decide the default: Notebook (`/tree`) or JupyterLab (`/lab`), and whether users can switch (Notebook's "Open in JupyterLab" menu).
- [ ] Admin switch to turn terminals off (whole Hub, later per team): `ServerApp.terminals_enabled=False` in the spawn command.
- [ ] Note for security docs: a terminal gives the same rights as code in a kernel (it already can run `os.system`), so it is not a new hole, but it does make long-running processes easy; they keep running after the browser closes and count against the user's limit.
- [ ] README: how to open a terminal, how to use JupyterLab, how to turn terminals off.

## Ideas for a better analysis / data science setup

Ranked by value for effort. Each needs a decision before it becomes real work.

### First (small effort)
- [x] **Idle culling:** `jupyterhub-idle-culler` as a Hub service, `idle_cull` in `hub_settings.json` (default 3 h, on for new installs, off for old ones until the key is added). Branch `feature/todo-0.6.0`.
  - [ ] Check on a cluster: README → Test a cluster step 5 (server stopped after a 120 s timeout).
  - [ ] Check whether a kernel that runs a long silent cell (no output) keeps the server alive; if not, keep the "print progress" advice in README or raise the default.
- [ ] **Shared team folder:** `/data/<team>` writable by the team (a Linux group per team, users added at spawn) and a read-only `/data/shared` for reference datasets; set up by `install-worker`.
- [ ] **Same data stack on every worker:** pinned pandas, polars, DuckDB, scikit-learn, matplotlib/plotly, `jupysql`, so a notebook runs the same on any team's worker.

### Next (protect work, reach data)
- [ ] **Notebook backups:** home folders live only on the worker disk; nightly sync to S3 or EBS snapshots, or EFS homes (also makes moving a team to a new worker free).
- [ ] **Data connections from Vault:** turn a user's Vault secrets into ready `%sql` / S3 connections, no passwords in notebooks (after the Vault section above).
- [ ] **Git in the notebook:** `jupyterlab-git` + `nbdime` for readable notebook diffs.

### Later (bigger)
- [ ] **Worker sizes per team:** bigger or GPU workers for some teams; stop idle workers to save cost.
- [ ] **Share results:** publish a notebook as read-only HTML or a Voilà dashboard.
- [ ] **Data-aware AI magics:** give `%do` / `%fix` the columns and dtypes of DataFrames in memory; add `%explain` for results.
- [ ] **Real-time collaboration:** `jupyter-collaboration` (needs JupyterLab, see the Terminal/JupyterLab section).
- [ ] **Usage report per team:** monthly CPU/RAM per team from the monitoring data.
