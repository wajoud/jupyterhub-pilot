# Development

Work on JupyterHub-Pilot itself: set up, run the tests, build.

← [Back to the README](../README.md)

## Set up and run the tests

The tests run without real infrastructure (no SSH, EC2 or Vault):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -v tests/
```

```text
jupyterhub-pilot/
├── jupyterhub_pilot/      # Python package: AI magics, LLM providers, SQLite store,
│                          # monitoring handlers + dashboard, metrics agent, Vault client
│   ├── spawner.py         # CustomSpawner: SSH launch, JIT user creation, cgroups
│   ├── jupyterhub_config.py  # Hub config (Google login, spawner, monitoring routes)
│   ├── cli.py             # `jupyterhub-pilot` command
│   ├── scripts/           # install-hub, install-worker, add-worker (bash)
│   └── templates/         # adds the Monitoring link to the Hub nav bar
├── jupyterhub_config.py   # shim for installs made with git clone (0.4.0 and earlier)
├── tests/                 # unit tests (see tests/README.md)
└── docs/                  # architecture, security, AI magics, monitoring
```
