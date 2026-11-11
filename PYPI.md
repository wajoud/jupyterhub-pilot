# JupyterHub-Pilot

One JupyterHub for many teams. Each team gets its own worker VM, and every notebook has an AI assistant.

- **A worker VM per team.** The Hub starts each user's notebook server over SSH on their team's worker, so one heavy team cannot slow down another.
- **A Linux account per user**, with cgroup memory and CPU limits on every notebook server.
- **AI magics** in every notebook: `%do`, `%fix`, `%review`, `%rework` (Ollama, OpenAI, Claude or Gemini through LiteLLM, or an MCP server).
- **Live monitoring** at `/hub/monitoring`: each worker's CPU, memory, disk and network, and each user's usage against their limits.
- **Google login**, SQLite state that survives Hub restarts, optional Vault secrets, and a built-in load test.

## Install

Ubuntu 24.04 or 26.04: one Hub VM plus one worker VM per team, on the same private network.

**Hub**

```bash
sudo apt update && sudo apt install -y python3-pip
sudo pip3 install --break-system-packages --ignore-installed "jupyterhub-pilot[hub]"
sudo jupyterhub-pilot install-hub
```

It asks for the Hub's private IP, your admin username and the first worker's IP, and prints an SSH key and an agent token for the workers.

**Each worker**

```bash
sudo apt update && sudo apt install -y python3-pip
sudo pip3 install --break-system-packages --ignore-installed "jupyterhub-pilot[worker,ai]"
sudo jupyterhub-pilot install-worker
```

**Then, on the Hub**

```bash
sudo jupyterhub-pilot add-worker team_beta <worker-private-ip>   # one per extra team
sudo nano /etc/jupyterhub-pilot/hub.env                          # Google client, or test passwords
sudo systemctl enable --now jupyterhub
```

Put [Caddy](https://caddyserver.com/) in front for HTTPS, create a group per team in **Admin → Groups**, and users can start their servers.

## In a notebook

```python
%load_ext jupyterhub_pilot.extension
%do load data/sales.csv and plot monthly revenue
```

`%do` writes the code into the next cell for you to check and run.

## Learn more

- [Admin guide](https://github.com/wajoud/jupyterhub-pilot/blob/main/docs/ADMIN.md): HTTPS, test login, teams, limits, upgrading, troubleshooting
- [Using the notebooks](https://github.com/wajoud/jupyterhub-pilot/blob/main/docs/USING.md) · [Screenshots](https://github.com/wajoud/jupyterhub-pilot#what-it-looks-like)
- [AWS EC2 walkthrough](https://github.com/wajoud/jupyterhub-pilot/blob/main/AWS_EC2_DEPLOYMENT.md)
- [AI magics](https://github.com/wajoud/jupyterhub-pilot/blob/main/docs/AI_MAGICS.md) · [Monitoring](https://github.com/wajoud/jupyterhub-pilot/blob/main/docs/MONITORING.md) · [Security](https://github.com/wajoud/jupyterhub-pilot/blob/main/docs/SECURITY.md)
- [Releases](https://github.com/wajoud/jupyterhub-pilot/releases) · [Issues](https://github.com/wajoud/jupyterhub-pilot/issues)

MIT licensed.
