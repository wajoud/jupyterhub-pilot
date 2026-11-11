# TODO

Findings from the 2026-11-07 EC2 test (Ubuntu 24.04 Arm, 1 Hub + 2 workers, us-east-1, JupyterHub 6.2.0) are grouped
under "Next release". Hub install, worker installs, team routing, JIT user creation, cgroup limits and metrics agents all worked.

## 0.8.0 release test plan

0.8.0 is merged to `main` and tested from there **before** it goes to PyPI (a PyPI version can never be replaced). Fixes found by the test go to `main` through a normal branch and PR; then reinstall with the same line. Tick each line with the date; write what went wrong under it. A failed **must** line blocks the release; a failed *later* line becomes a TODO for 0.8.1.

Install line used below (Hub: `hub`; worker: `worker,ai`):

```bash
ZIP=https://github.com/wajoud/jupyterhub-pilot/archive/refs/heads/main.zip
sudo pip3 install --no-cache-dir --break-system-packages --ignore-installed "jupyterhub-pilot[hub] @ $ZIP"
```

### A. Upgrade the 0.7.0 cluster (must) — about 20 minutes

The 2026-11-10 cluster (1 Hub + 2 workers, no GPU). If the instances were stopped, the Hub's public IP changed: fix the Caddyfile first.

- [ ] A1. Before: on the Hub note `cat /etc/jupyterhub-pilot/version` (0.7.0), the teams in Admin → Groups, one user's limit on `/hub/limits`, and start one server and leave it running.
- [ ] A2. Hub: the install line with `hub`, then `sudo jupyterhub-pilot upgrade --no-pip`. Expect `hub: 0.7.0 → 0.8.0`, a warning for both workers (still 0.7.0), and the restart question.
- [ ] A3. Each worker: the install line with `worker,ai`, then `sudo jupyterhub-pilot upgrade --no-pip`. Expect `worker: 0.7.0 → 0.8.0` and `metrics agent restarted`.
- [ ] A4. After: `sudo jupyterhub-pilot upgrade --no-pip --yes` on the Hub again shows no worker warning. Teams, the user's limit, homes and the admin login are unchanged. `sudo journalctl -u jupyterhub -n 50` has no traceback.

### B. The new pages on the upgraded cluster (must) — about 30 minutes

- [ ] B1. `/hub/monitoring` as admin: each card is titled with its team name and shows its size and "no GPU"; one bar per CPU core; the Fullest disk tile; no GPU tile, no GPU column.
- [ ] B2. Start servers for two users (`jupyterhub-pilot load-test --hub $HUB --teams team_alpha,team_beta --users 4`): "Busiest right now" appears; the cards show processes and "Memory limits add up to".
- [ ] B3. Usage history: "1 hour" shows the points of the last hour; "Most active users" lists the load-test users; History on a row opens that user; the four totals above the graphs make sense.
- [ ] B4. The same page as a normal user (test login): only their team's worker and their own row; no "Most active users"; no "Busiest right now".
- [ ] B5. `/hub/limits` loads and lists **every** user with team, limit and source. (This is the code that never ran on a real JupyterHub.)
- [ ] B6. Limits, one user: type `2G` in a user's Memory box → Save → "Gets now" says `2G set here`; their running server shows "still on the old limit"; after they restart the mark is gone and `/hub/monitoring` shows 2.0 GB. Clear → back to the team or default.
- [ ] B7. Limits, a team: set `3G` / `150%` on team_beta → Save. Admin → Groups → team_beta → properties shows `memory_max` and `cpu_quota`. A team_beta user's next start gets it (Hub log: `MemoryMax=3G (team)`). Empty both boxes → Save → the properties are gone.
- [ ] B8. Limits: a bad value (`2 gigs`) is refused with a message; Terminal off still works for one user; a normal user gets 403 on `/hub/limits`.
- [ ] B9. Dark mode and a phone (or a narrow window): both pages readable, nothing cut off.

### C. A fresh install (must) — about 40 minutes, 2 new instances

One new Hub VM and one new worker VM (Ubuntu 26.04), nothing installed. Tests the trimmed `nodejs`/`npm` install and the guide.

- [ ] C1. Follow [docs/ADMIN.md → Install](docs/ADMIN.md#install-hub-and-first-worker) exactly, with the install line above in place of the PyPI one. Note anything the guide gets wrong.
- [ ] C2. `install-hub`: step "1. OS packages" is clearly faster than on 2026-11-10; `configurable-http-proxy --version` works; the closing message points to `docs/ADMIN.md`.
- [ ] C3. `install-worker` on a machine without a GPU prints `No NVIDIA GPU on this machine: no driver needed.`
- [ ] C4. Test login behind Caddy, start a server, the worker's card is on `/hub/monitoring`.
- [ ] C5. `aws --version` still works on a machine with the apt `awscli` after the pip install (skip if not installed).

### D. A GPU worker (must for "GPU tested"; else ship it marked "not yet tested") — about 40 minutes

One GPU instance as a third worker, for example `g4dn.xlarge` (x86, Tesla T4) with plain Ubuntu 26.04, 40 GiB disk. It costs more per hour than the others: stop it after the test.

- [ ] D1. `install-worker` prints `NVIDIA GPU found without a working driver` and installs it; at the end it asks for a reboot. Note how long it took.
- [ ] D2. After `sudo reboot`: `nvidia-smi` lists the card. `sudo journalctl -u jupyterhub_pilot-metrics -n 20` has no `nvidia-smi found but not working`.
- [ ] D3. Hub: `sudo jupyterhub-pilot add-worker team_gpu <ip>`, create the group, add a user. The card shows `GPU <model>`, a GPU meter with temperature, and the GPU memory tile appears.
- [ ] D4. In that user's notebook: `!nvidia-smi` works. `pip install --user --break-system-packages torch`, then `import torch; x = torch.zeros(1024, 1024, 1024, device="cuda")` (4 GB): the user's row shows about 4 GB under GPU memory within 10 seconds.
- [ ] D5. Fill the card to 90% or more: "Needs attention" lists the GPU. Restart the kernel: it clears.
- [ ] D6. `/hub/limits` shows `GPU <model>` in team_gpu's Worker column. The other workers still say "no GPU".
- [ ] D7. *Later:* the same on Arm (`g5g.xlarge`) and with an AWS Deep Learning image (driver already there: expect `driver working`, no install).

### E. Features never run on a cluster (later; not changed in 0.8.0)

Each has its steps where linked. Do as many as time allows; none blocks 0.8.0.

- [ ] E1. EFS shared homes: the checks under "Users in more than one team" below (same uid **and** gid on both workers first).
- [ ] E2. Two-team user: "Which team?" on the start page; lands on the chosen worker with that team's limit.
- [ ] E3. Worker reboot with a server running: the Hub shows it stopped within 30 s and Start works.
- [ ] E4. [Test a cluster](docs/ADMIN.md#test-a-cluster) steps 5 (idle stop) and 6 (Vault); `load-test --cleanup` archives homes in `/home/.deleted`.
- [ ] E5. Prometheus: scrape `/hub/monitoring/metrics` with the token; on the GPU worker the `worker_gpu_*` lines are there.
- [ ] E6. In a notebook: `team/` and `shared/` folders, `%schedule add "*:0/2" test.ipynb`, New → Terminal, the Git tab in `/lab`, `%sql duckdb://`, `%do` with a real model.

### F. Release (after A–C pass, and D or a "not yet tested" note)

- [ ] F1. Fixes from the test are merged to `main`; CI green; `CHANGELOG.md` has the date; `docs/FEATURES.md` says what was tested.
- [ ] F2. On `main`: `python -m build`, `twine check --strict dist/jupyterhub_pilot-0.8.0*`.
- [ ] F3. Upload from `~/projects/Github/JupyterPilot`: `.venv/bin/python -m twine upload dist/jupyterhub_pilot-0.8.0*`.
- [ ] F4. Tag `v0.8.0` on the commit that was built, GitHub release with the dist files.
- [ ] F5. On the test cluster: `sudo jupyterhub-pilot upgrade` (now from PyPI) reports 0.8.0 and changes nothing.

## 2026-11-10 test: 0.7.0 from PyPI, 1 Hub + 2 workers (ap-south-1, Ubuntu 26.04 Arm)

Findings so far; fixes go into 0.7.1.

- [x] Seen working on 2026-11-10 (as reported during the test, 1 Hub + 2 workers, 2 cores / 3.7 GB each): install from PyPI, `install-hub`, `install-worker`, `add-worker`, test login, Caddy `tls internal` on the public IP, team routing, default limits, per-user limit + Terminal off (after a restart), a dotted user name (`a.a.a`), live monitoring, usage history, load test.
- [ ] Still to do on this cluster: EFS (mount, same uid/gid on both workers, second user cannot read a home, unmounted → 503, move an existing user), two-team "Which team?", worker reboot → server shown stopped, a normal user's view of `/hub/monitoring`, load-test `--cleanup` archives in `/home/.deleted`, Prometheus endpoint, Vault.

- [x] **`install-hub` is slow at "1. OS packages":** `apt-get install nodejs npm` downloads 340+ small packages one by one, many of them only "recommended" extras (Perl libraries, a compiler toolchain). `--no-install-recommends` added in `install_hub.sh` (0.8.0).
  - [ ] Check on a fresh Hub that `npm install -g configurable-http-proxy` still works and the Hub starts.
- [x] **Usage history looks empty on a new cluster:** "1 hour" range added, and the page says a user appears after their first 5 minutes (0.8.0).
- [x] Docs: the admin guide says to run the `apt install python3-pip` line first and that pip's `botocore` / `pyopenssl` conflict message is expected (0.8.0).
  - [ ] Check `aws --version` still works where the apt `awscli` is used.

## 0.8.0: GPU workers, Limits page, monitoring page (branch `feature/0.8.0`)

- [x] Metrics agent reads `nvidia-smi` (per card and per user); a worker without it reports no GPU. `install-worker` installs the driver only where a GPU is found (Ubuntu: `ubuntu-drivers install`, then a reboot).
- [x] Monitoring page: team name on each card, worker size, per-core bars, GPU meter and per-user GPU memory, busiest users, fullest disk, sum of memory limits, 1-hour range, period totals, most active users (admins). Looked at with made-up workers only.
- [x] Limits page: all users with the limit that applies and its source, running server's use, "still on the old limit" mark, team limits on the same page.
- [ ] Check on a cluster with one GPU worker and one without: `install-worker` on a fresh GPU instance (Ubuntu 26.04, x86 and Arm) installs a driver and `nvidia-smi` works after the reboot; the GPU card and a user's GPU memory show on `/hub/monitoring`; the worker without a GPU shows "no GPU".
- [ ] Check on a cluster: save a team limit on the Limits page and see it in Admin → Groups → properties; the "old limit" mark goes after the user restarts; the page with 50+ users.
- [ ] Later, if asked: per-user GPU limits (cgroups cannot do it; needs one GPU per user or NVIDIA MPS), a filter box on the Limits page, usage per team.

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

Amazon Linux 2023 (default user `ec2-user`) is handled by the same scripts as Ubuntu (`install_hub.sh`, `install_worker.sh` pick `apt` or `dnf`). The spawner falls back to
`useradd` when Debian's `adduser` flags are rejected. None of this has run on a real AL2023 instance yet.

- [ ] Run `install-hub` and `install-worker` on fresh AL2023 instances.
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

ap-south-1, Ubuntu 26.04 Arm, `t4g.small` everywhere, JupyterHub 6.2.0, Notebook 7.6.5, limit `1G`/`50%`, test login behind Caddy `tls internal`. Steps are in Admin guide → Test a cluster.

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
- [x] Admin guide → Test a cluster step 6 (Vault).
- [ ] Check on the Hub whether `vault` got installed on Ubuntu 26.04 (`vault version`); if `install-hub` printed "Vault not installed", say so in the docs.
- [ ] Run step 6 on a cluster.
- [x] The Hub renews its own token (`/v1/auth/token/renew-self`, at start and daily); no cron job. The policy needs the `auth/token/renew-self` path (the old cron job needed it too and would have got a `403`).
  - [ ] Check on a cluster: Hub log has no `token renewal refused`; `vault token lookup` shows the TTL back at 768h after a Hub restart.

## Upgrades: update only what changed, keep everything working

Today an upgrade is `pip install -U` + a restart (Admin guide → Upgrading). That only updates the Python code. When a release also changes the systemd unit, `hub.env`, `hub_settings.json` keys or the SQLite tables, the admin has to fix it by hand (0.4.0's metrics URL change needed a `sed` on every worker) or run `install-hub` / `install-worker` again.

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
- [x] Docs: Using the notebooks → Scheduled notebooks; `docs/SECURITY.md` (env file). Tests (units mocked).
- [ ] Check on a cluster: `%schedule add "*:0/2" test.ipynb` (every 2 minutes), result appears, `-FAILED` on a raising cell, a 2 GB cell is killed under a `1G` limit, job runs again after a worker reboot, `systemd-analyze` exists on Ubuntu 26.04 and AL2023.
- [x] Failed runs on `/hub/monitoring` (each run writes `~/.config/jupyterhub-pilot/jobs/<name>.last`; the metrics agent reports the failed ones). Not shown to users of a shared `ssh_user` team (admins see them).
  - [ ] Check on a cluster: a failing job appears under its worker, and goes after a good run.
- [ ] Later: a Schedule button in the Notebook UI (needs a JupyterLab extension in TypeScript, with its own build and release; `%schedule` covers it until someone asks).

## Terminal access and JupyterLab

Notebook 7 is built on JupyterLab and pulls in `jupyterlab` and the terminal extension, so both are probably available already but untested and undocumented: **New → Terminal** in the file browser, and `https://<hub>/user/<name>/lab`. The Hub opens `/tree` (`c.Spawner.default_url`).

- [x] Decided: keep Notebook (`/tree`) as the default; JupyterLab at `/user/<name>/lab` and via **View → Open JupyterLab**.
- [x] `terminals_enabled: false` in `hub_settings.json` adds `--ServerApp.terminals_enabled=False` to the spawn command (whole Hub). Test.
- [x] Docs: Using the notebooks → Terminal and JupyterLab; `docs/SECURITY.md` §1 terminal note.
- [ ] Check on a cluster: terminal opens as the user on the team worker, no `sudo`, a `stress` there is capped like a kernel; `/lab` loads with the AI magics; `terminals_enabled: false` removes the menu.
- [x] Terminal off per user: **Terminal off** on the admin Limits page (SQLite `user_limits.terminals_off`); off if that or the Hub-wide switch says so. Tests; README. Branch `feature/terminal-per-user`.
  - [ ] Check on a cluster: tick it for alice, she restarts her server, New has no Terminal; bob still has one.

## Users in more than one team

Today a user's **first** JupyterHub group decides their worker, team limit and `team/` folder, and JupyterHub doesn't guarantee the group order. So a user in two teams can't choose, and moving a user to another team leaves their notebooks on the old worker's disk (`/home/<user>/notebook`). README says "keep each user in one team group".

- [x] **Choose the team at start:** for a user in several team groups, a "Which team?" choice on the spawn page; the choice picks the worker, limit and `team/` folder. One-team users see no change. Groups without a worker are ignored; the default is the first team by name (group order no longer matters). Tests.
  - [ ] Check on a cluster: a user in two teams sees the choice, lands on the chosen worker with that team's limit; an API/culler start with no choice uses the last one.
- [x] **Shared homes (EFS):** `sudo jupyterhub-pilot mount-homes <efs-dns>` on every worker (mounts at `/efs/home`, not `/home`: that would hide the admin user's SSH keys), then `home_base` in `hub_settings.json`. The Hub gives each user one Linux UID for every worker (SQLite `user_uids`, from 20000) and refuses to start a server where the filesystem is not mounted. Scheduled jobs are pinned to their worker (`ConditionHost`). Tests; Admin guide → Shared home folders.
  - [ ] Check on a cluster: two workers + EFS; a two-team user sees the same files on both with the same owner; unmount on one worker → start refused with the 503 message; the migration steps for an existing user (`rsync`, `userdel`, start); `adduser --uid/--home` on Ubuntu and `useradd -u/-d` on AL2023.
  - [ ] Check on a cluster: `id <user>` shows the same uid **and** gid on both workers (the `groupmod` line); a second user cannot read the first one's home on either worker; a user not yet moved still gets the `team`/`shared` links in `/home/<user>/notebook`.
- [ ] Check on a cluster: reboot a worker with a server running → within 30 s the Hub shows it stopped and Start works (`poll()` checks the process is `jupyterhub-singleuser`).
  - [ ] Check on EFS: IPython history and Jupyter's SQLite files work on NFS (no "database is locked"); start time compared with a local home; `du` of a large home by the metrics agent.
- [x] Admin guide → Move a user to another team.
  - [ ] Check the `tar | ssh` copy command there on a cluster.

## Ideas for a better analysis / data science setup

Ranked by value for effort. Each needs a decision before it becomes real work.

### First (small effort)
- [x] **Idle culling:** `jupyterhub-idle-culler` as a Hub service, `idle_cull` in `hub_settings.json` (default 3 h, on for new installs, off for old ones until the key is added). Branch `feature/todo-0.6.0`.
  - [ ] Check on a cluster: Admin guide → Test a cluster step 5 (server stopped after a 120 s timeout).
  - [ ] Check whether a kernel that runs a long silent cell (no output) keeps the server alive; if not, keep the "print progress" advice in README or raise the default.
- [x] **Shared team folder:** `/data/<team>` (Linux group per team, setgid + default ACL) and read-only `/data/shared`, created and linked into `~/notebook` at every server start (JIT step), so no worker re-install. `install-worker` adds the `acl` package.
  - [ ] Check on a cluster: two users of one team both write `team/`; a user of another team on the same worker can't; `shared/` is read-only.
- [x] **Same data stack on every worker:** pandas, polars, DuckDB (+ `duckdb-engine`), scikit-learn, matplotlib, plotly, `jupysql` in the `worker` extra (minimum versions, not exact pins; `upgrade` keeps workers together).
  - [ ] Check worker disk after install (~600 MB more) and that `%sql duckdb://` works.

### Next (protect work, reach data)
- [ ] **Notebook backups:** with shared homes (above), turn on AWS Backup for the EFS filesystem (console, no code). Without EFS: nightly sync to S3 or EBS snapshots, still to do.
- [ ] **Data connections from Vault:** turn a user's Vault secrets into ready `%sql` / S3 connections, no passwords in notebooks (after the Vault section above).
- [x] **Git in the notebook:** `jupyterlab-git` + `nbdime` in the worker extra, `git` in `install-worker`; Using the notebooks → Git.
  - [ ] Check on a cluster: the Git tab in `/lab` (and whether Notebook 7's `/tree` shows a Git menu); `nbdiff` works.

### Later (bigger)
- [x] **Worker sizes per team:** each team's worker can be any size, with or without a GPU (0.8.0, see the top).
- [ ] **Stop idle workers** to save cost.
- [ ] **Share results:** publish a notebook as read-only HTML or a Voilà dashboard.
- [x] **Data-aware AI magics:** the context lists pandas/polars DataFrames with shape and column dtypes (first 30); new `%explain [question]`. Docs + tests.
  - [ ] Check with a real model on a cluster that `%do` uses the real column names.
- [ ] **Real-time collaboration:** `jupyter-collaboration` (needs JupyterLab, see the Terminal/JupyterLab section).
- [x] **Usage history per user:** 5-minute CPU/memory points kept 15 days in SQLite (`user_usage`), graphed on `/hub/monitoring` (24 h / 7 d / 15 d); Prometheus endpoint `/hub/monitoring/metrics` for Grafana. Monitoring page redesigned (attention list, compact cards, light/dark, no CDN script). Tests; `docs/MONITORING.md`.
  - [ ] Check on a cluster: points appear after 5 minutes, a user sees only their own history, the page on a phone, a Prometheus scrape with the token.
- [ ] **Usage report per team:** monthly CPU/RAM per team. The data is now in `user_usage` (15 days); a monthly report needs a longer retention or a monthly roll-up.
