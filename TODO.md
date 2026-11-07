# TODO

Findings from the 2026-11-07 EC2 test (Ubuntu 24.04 Arm, 1 Hub + 2 workers, us-east-1, JupyterHub 6.2.0) are grouped
under "Next release". Hub install, worker installs, team routing, JIT user creation, cgroup limits and metrics agents all worked.

## Next release (branch `fix/ec2-test-findings`)

Code is done for every item marked [x]; the items marked "verify" need the ap-south-1 re-test.

### Monitoring
- [x] Metrics agent URL is now `ws://HUB:8000/hub/monitoring/ws` (JupyterHub serves extra handlers under `/hub/`; the old URL cost a `302` per connect).
  Workers installed from older `main` need: `sudo sed -i 's#:8000/monitoring/ws#:8000/hub/monitoring/ws#' /etc/systemd/system/jupyterhub_pilot-metrics.service`.
- [ ] Verify: `/hub/monitoring` shows both workers to an admin, and only `team_alpha`'s worker to a `team_alpha` user.
- [ ] Later (not this release): `JupyterHub.extra_handlers` is deprecated since 3.1 but still works on 6.2. Moving monitoring to a JupyterHub service is a larger change.

### Speed
- [x] Docs: pick the region closest to users (measured from India: us-east-1 ~300 ms / ~20 KB/s vs ap-south-1 ~40 ms); keep Caddy in front even for tests.
- [x] `encode zstd gzip` in the Caddyfiles (README, AWS guide). Measured 8× faster downloads through Caddy.
- [x] No-domain test setup in README: Caddy `tls internal` on the public IP, with the `default_sni` line it needs.
- [ ] Verify: first Notebook load from ap-south-1 takes seconds.

### Install and first run
- [x] `scripts/add_worker.sh <team> <ip> [admin_ssh_user]`: upserts the team, re-seeds SQLite, tests SSH.
- [x] Test login without Google: set `JUPYTERPILOT_TEST_PASSWORD` + `JUPYTERPILOT_TEST_ADMIN_PASSWORD` (uses JupyterHub's `shared-password` authenticator). No config file edits.
- [x] Re-running `install_hub.sh`: `git pull --autostash` (and `safe.directory`, since root runs git in a user-owned clone); keeps teams already in `user_mapping.json`.
- [x] Install scripts clone the same branch as the clone they are run from (so testing a branch installs that branch).
- [x] No team group → a `403` page with "ask an admin to add you to a team" instead of a bare `500`.
- [x] `jupyterhub>=5.3,<7` in `pyproject.toml`; README badge says 5.3+ | 6.x.
- [x] `install_hub.sh` writes `/etc/systemd/system/jupyterhub.service` and a root-only `/etc/jupyterhub-pilot/hub.env` (agent token filled in, test-login lines ready). Start with `sudo systemctl enable --now jupyterhub`.
- [x] `cookie_options` moved into `tornado_settings`, where JupyterHub reads it. The login cookie is now really `Secure`, so plain `http://` logins loop: use Caddy.
- [ ] Verify all of the above in the ap-south-1 run.

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

Deferred until after the ap-south-1 test: install scripts that `pip install` from PyPI can only test a version that is already released, so this goes in its own release.

Today the PyPI package ships only `jupyterhub_pilot/`. A deployment also needs files outside it, and the scripts expect a clone at `/opt/jupyterhub-pilot`.

- [ ] Move `spawner.py` into the package (`jupyterhub_pilot/spawner.py`) and update imports in `jupyterhub_config.py` and the tests.
- [ ] Ship `jupyterhub_config.py` and `templates/page.html` as package data, or add a `jupyterhub-pilot init` command that writes them (plus `hub_settings.json` / `user_mapping.json`) to a config directory.
- [ ] Add a console script for the metrics agent (e.g. `jupyterhub-pilot-metrics`) so the systemd unit no longer runs `/opt/jupyterhub-pilot/jupyterhub_pilot/metrics_agent.py`.
- [ ] Ship `get_port.py` in the package (or inline it in the spawner) instead of copying it from `/opt/jupyterhub-pilot/get_port.py`.
- [ ] Change all four install scripts (Ubuntu + Amazon Linux) to `pip install "jupyterhub-pilot[hub]"` / `"jupyterhub-pilot[worker]"` instead of `git clone`.
- [ ] Update README, INSTALL.md and AWS_EC2_DEPLOYMENT.md.
