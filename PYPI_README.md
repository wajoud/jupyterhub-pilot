# 🚀 JupyterHub-Pilot

**Agentic AI coding partner for JupyterHub and IPython.**

JupyterHub-Pilot solves two major problems for data teams running JupyterHub:
1. **Isolated execution & State Persistence:** A custom SSH spawner teleports each user's notebook server onto isolated EC2 workers, wraps it in cgroup resource limits, and tracks all sessions in SQLite.
2. **AI Magic Commands:** An IPython extension adds `%do`, `%fix`, `%review`, and `%rework` magic commands that speak to any LLM backend (Ollama, OpenAI, Claude, Gemini, or MCP servers) to inject clean Python directly into your next cell.

## Installation

You can install JupyterHub-Pilot and its specific components via pip:

```bash
# Install core package
pip install jupyterhub-pilot

# Install Hub-side dependencies
pip install jupyterhub-pilot[hub]

# Install Worker-side dependencies
pip install jupyterhub-pilot[worker]

# Install AI features (for users)
pip install jupyterhub-pilot[ai]
```

## Documentation & Full Setup Guide

For full documentation, architecture diagrams, and automated AWS EC2 deployment guides, please visit our GitHub repository:

🔗 **[github.com/wajoud/jupyterhub-pilot](https://github.com/wajoud/jupyterhub-pilot)**
