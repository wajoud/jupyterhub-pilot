<div align="center">

# 🚀 JupyterHub-Pilot

**Enterprise-grade AI coding partner & secure multi-team notebook isolation for JupyterHub**

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776ab?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![JupyterHub](https://img.shields.io/badge/JupyterHub-Compatible-f37726?style=flat-square&logo=jupyter&logoColor=white)](https://jupyterhub.readthedocs.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-22c55e?style=flat-square)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-107%20passing-22c55e?style=flat-square)](tests/)
[![AWS EC2](https://img.shields.io/badge/AWS-EC2%20Ready-FF9900?style=flat-square&logo=amazon-aws&logoColor=white)](AWS_EC2_DEPLOYMENT.md)

*One Hub. Many teams. Every user isolated. AI in every cell.*

</div>

---

## 🗺️ What is JupyterHub-Pilot?

JupyterHub-Pilot solves two massive problems for data teams running JupyterHub:

1. **"How do we run notebooks for 100+ users without one user crashing everyone else?"**
   > JupyterHub-Pilot's SSH spawner teleports each user's notebook server onto an isolated EC2 worker, wraps it in cgroup resource limits, and tracks all sessions in SQLite so nothing is lost if the Hub reboots.

2. **"How do we get AI coding assistance inside notebooks without a brittle plugin?"**
   > JupyterHub-Pilot's IPython extension adds four magic commands (`%do`, `%fix`, `%review`, `%rework`) that speak to any LLM backend — local Ollama, OpenAI, Claude, Gemini, or an MCP server — and inject clean Python directly into the next cell.

---

## 📚 Documentation Directory

We have split our documentation into focused guides to help you build exactly what you need.

### 🏗️ [Architecture & EC2 Routing](docs/ARCHITECTURE.md)
Learn how a single Hub instance effortlessly routes hundreds of users across multiple Worker EC2s based on their team group. Covers horizontal scaling and SQLite crash recovery.

### 🔐 [Security, Isolation & RBAC](docs/SECURITY.md)
Deep dive into how we isolate users using OS-level `cgroups v2`, enforce a 2-Tier Role-Based Access Control, and inject secrets seamlessly using **HashiCorp Vault**.

### 🤖 [AI Magic Commands](docs/AI_MAGICS.md)
See how `%do`, `%fix`, `%review`, and `%rework` act as a context-aware pair programmer right inside your Jupyter cells, and how to configure local/cloud LLM backends.

### 📊 [Live Monitoring Dashboard](docs/MONITORING.md)
Setup guide for our real-time, dark-mode WebSocket dashboard that monitors CPU, RAM, Disk, and Network I/O across your entire cluster of EC2 workers.

---

## ⚡ Quick Start Installation

JupyterHub-Pilot is modular. You only install the pieces you need for the specific machine you are on.

**1. On the JupyterHub VM (The Hub):**
```bash
pip install "jupyterhub-pilot[hub]"
```

**2. On the Notebook Server VM (The Worker):**
```bash
pip install "jupyterhub-pilot[worker]"
```

**3. Inside a Jupyter Notebook (AI features for users):**
```bash
pip install "jupyterhub-pilot[ai]"
```

### 📖 Complete Setup Guides

For a detailed, step-by-step walkthrough of deploying this across an actual cluster, check out our full guides:

- 🛠️ **[General Installation Guide](INSTALL.md)** - Detailed, OS-agnostic setup steps.
- ☁️ **[AWS EC2 Deployment Guide](AWS_EC2_DEPLOYMENT.md)** - End-to-end walkthrough specifically tailored for AWS.

---

## 🧪 Testing

All 107 tests run with **zero real infrastructure** — no SSH, no EC2, no Vault:

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run the full suite
pytest -v tests/
```

---

## 📦 Repository Structure

```text
JupyterHub-Pilot/
├── jupyterhub_pilot/                   # Core Python package
├── docs/                               # 📖 Modular Documentation Guides
├── scripts/                            # Automated bash setup scripts
├── tests/                              # 107-test mock-based unit suite
├── spawner.py                          # SSH spawner core logic
├── INSTALL.md                          # Full installation guide
└── AWS_EC2_DEPLOYMENT.md               # End-to-end AWS walkthrough
```

---

<div align="center">

Built with 🔧 by [@wajoud](https://github.com/wajoud) · Star ⭐ if this helps your team!

</div>
