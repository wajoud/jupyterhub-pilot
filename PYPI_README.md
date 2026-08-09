# 🚀 JupyterHub-Pilot

**Agentic AI coding partner for JupyterHub and IPython.**

JupyterHub-Pilot solves two major problems for data teams running JupyterHub:
1. **Isolated execution & State Persistence:** A custom SSH spawner teleports each user's notebook server onto isolated EC2 workers, wraps it in cgroup resource limits, and tracks all sessions in SQLite.
2. **AI Magic Commands:** An IPython extension adds `%do`, `%fix`, `%review`, and `%rework` magic commands that speak to any LLM backend (Ollama, OpenAI, Claude, Gemini, or MCP servers) to inject clean Python directly into your next cell.

## Installation

JupyterHub-Pilot is designed to run across multiple machines. You only need to install the components relevant to the machine you are setting up:

**On the JupyterHub VM (The Hub):**
```bash
pip install "jupyterhub-pilot[hub]"
```

**On the Notebook Server VM (The Worker):**
```bash
pip install "jupyterhub-pilot[worker]"
```

**Inside a Jupyter Notebook (AI features for users):**
```bash
pip install "jupyterhub-pilot[ai]"
```

## Documentation & Full Setup Guide

For full documentation, architecture diagrams, and automated AWS EC2 deployment guides, please visit our GitHub repository:

🔗 **[github.com/wajoud/jupyterhub-pilot](https://github.com/wajoud/jupyterhub-pilot)**
