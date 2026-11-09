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

- [x] `sudo jupyterhub-pilot upgrade` (Hub and workers): pip upgrade, then the new code runs only the steps newer than `/etc/jupyterhub-pilot/version`; each step idempotent. Never touches keys, tokens, passwords, teams or homes.
- [x] Steps so far: 0.4.0 worker metrics URL fix; 0.6.0 Hub `on_user_delete` + `idle_cull` defaults (existing values kept); 0.6.0 worker `acl` + `git` packages. `install-hub` / `install-worker` write the version file.
- [x] Hub checks workers' versions over SSH and lists those behind; asks before restarting the Hub (`--yes` to skip).
- [x] `CHANGELOG.md`: per release, Hub / workers / both and whether a restart is needed.
- [ ] Test on a cluster: install 0.5.1 from PyPI, Test a cluster steps, `upgrade` on Hub + workers, steps again; settings, teams and homes unchanged.
- [ ] Later steps that need it: rewrite the systemd units (when a release changes them) and SQLite migrations, added to `STEPS` in `upgrade.py`.

## Scheduled notebooks (cron tasks), e.g. with Airflow

Users want to pick an `.ipynb` and have it run on a schedule (every night, every hour), without keeping a browser or kernel open.

- [x] Engine: `papermill` + systemd **user** timers on the team's worker (no Airflow: no extra DB/scheduler/web server, and the run is already on the right machine as the right user). `jupyterhub_pilot/schedule.py`.
- [x] Runs on the user's worker as the user, under the limit of the server that scheduled it (`MemoryMax`/`CPUQuota` from the kernel's cgroup), with its Vault secrets (`600` env file; the spawner now passes `JUPYTERHUB_PILOT_SECRET_KEYS` names).
- [x] `%schedule add <when> <nb> [--keep N]`, `list`, `run`, `pause`, `resume`, `delete`; admins list all jobs on a worker (README command).
- [x] Output `<name>-<YYYYmmdd-HHMM>.ipynb` (or `-FAILED`) next to the original, last N kept; `list` shows the last result.
- [x] Survives restarts (linger, `Persistent=true` catches up missed runs). Delete the user → account and timers gone.
- [x] Docs: README → Scheduled notebooks; `docs/SECURITY.md` (env file). Tests (units mocked).
- [ ] Check on a cluster: `%schedule add "*:0/2" test.ipynb` (every 2 minutes), result appears, `-FAILED` on a raising cell, a 2 GB cell is killed under a `1G` limit, job runs again after a worker reboot, `systemd-analyze` exists on Ubuntu 26.04 and AL2023.
- [ ] Later: failed runs on `/hub/monitoring`; a Schedule button in the Notebook UI.

## Terminal access and JupyterLab

Notebook 7 is built on JupyterLab and pulls in `jupyterlab` and the terminal extension, so both are probably available already but untested and undocumented: **New → Terminal** in the file browser, and `https://<hub>/user/<name>/lab`. The Hub opens `/tree` (`c.Spawner.default_url`).

- [x] Decided: keep Notebook (`/tree`) as the default; JupyterLab at `/user/<name>/lab` and via **View → Open JupyterLab**.
- [x] `terminals_enabled: false` in `hub_settings.json` adds `--ServerApp.terminals_enabled=False` to the spawn command (whole Hub). Test.
- [x] Docs: README → Terminal and JupyterLab; `docs/SECURITY.md` §1 terminal note.
- [ ] Check on a cluster: terminal opens as the user on the team worker, no `sudo`, a `stress` there is capped like a kernel; `/lab` loads with the AI magics; `terminals_enabled: false` removes the menu.
- [x] Terminal off per user: **Terminal off** on the admin Limits page (SQLite `user_limits.terminals_off`); off if that or the Hub-wide switch says so. Tests; README. Branch `feature/terminal-per-user`.
  - [ ] Check on a cluster: tick it for alice, she restarts her server, New has no Terminal; bob still has one.

## Users in more than one team

Today a user's **first** JupyterHub group decides their worker, team limit and `team/` folder, and JupyterHub doesn't guarantee the group order. So a user in two teams can't choose, and moving a user to another team leaves their notebooks on the old worker's disk (`/home/<user>/notebook`). README says "keep each user in one team group".

- [ ] Decide whether anyone needs more than one team. If yes:
- [ ] **Choose the team at start:** for a user in several team groups, a "Which team?" choice on the spawn page (`options_form`); the choice picks the worker, limit and `team/` folder. One-team users see no change.
- [ ] **Shared homes (EFS):** mount the same `/home` on every worker so switching teams keeps all files (also covers moving a team to a new worker, and backups; see Notebook backups below).
- [ ] Until then: README "Move a user to another team" steps (remove from the old group, copy `/home/<user>/notebook` to the new worker, restart their server).

## Ideas for a better analysis / data science setup

Ranked by value for effort. Each needs a decision before it becomes real work.

### First (small effort)
- [x] **Idle culling:** `jupyterhub-idle-culler` as a Hub service, `idle_cull` in `hub_settings.json` (default 3 h, on for new installs, off for old ones until the key is added). Branch `feature/todo-0.6.0`.
  - [ ] Check on a cluster: README → Test a cluster step 5 (server stopped after a 120 s timeout).
  - [ ] Check whether a kernel that runs a long silent cell (no output) keeps the server alive; if not, keep the "print progress" advice in README or raise the default.
- [x] **Shared team folder:** `/data/<team>` (Linux group per team, setgid + default ACL) and read-only `/data/shared`, created and linked into `~/notebook` at every server start (JIT step), so no worker re-install. `install-worker` adds the `acl` package.
  - [ ] Check on a cluster: two users of one team both write `team/`; a user of another team on the same worker can't; `shared/` is read-only.
- [x] **Same data stack on every worker:** pandas, polars, DuckDB (+ `duckdb-engine`), scikit-learn, matplotlib, plotly, `jupysql` in the `worker` extra (minimum versions, not exact pins; `upgrade` keeps workers together).
  - [ ] Check worker disk after install (~600 MB more) and that `%sql duckdb://` works.

### Next (protect work, reach data)
- [ ] **Notebook backups:** home folders live only on the worker disk; nightly sync to S3 or EBS snapshots, or EFS homes (also makes moving a team to a new worker free).
- [ ] **Data connections from Vault:** turn a user's Vault secrets into ready `%sql` / S3 connections, no passwords in notebooks (after the Vault section above).
- [x] **Git in the notebook:** `jupyterlab-git` + `nbdime` in the worker extra, `git` in `install-worker`; README → Git.
  - [ ] Check on a cluster: the Git tab in `/lab` (and whether Notebook 7's `/tree` shows a Git menu); `nbdiff` works.

### Later (bigger)
- [ ] **Worker sizes per team:** bigger or GPU workers for some teams; stop idle workers to save cost.
- [ ] **Share results:** publish a notebook as read-only HTML or a Voilà dashboard.
- [x] **Data-aware AI magics:** the context lists pandas/polars DataFrames with shape and column dtypes (first 30); new `%explain [question]`. Docs + tests.
  - [ ] Check with a real model on a cluster that `%do` uses the real column names.
- [ ] **Real-time collaboration:** `jupyter-collaboration` (needs JupyterLab, see the Terminal/JupyterLab section).
- [ ] **Usage report per team:** monthly CPU/RAM per team from the monitoring data.
