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
