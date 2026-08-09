# 🤖 AI Magic Commands

> **Goal:** Data scientists get an AI pair-programmer that understands their notebook context, fixes their errors, and refactors their code — all without leaving Jupyter.

## Loading the extension

```python
# In any Jupyter cell:
%load_ext jupyterhub_pilot.extension
# ✅ JupyterHub-Pilot loaded. Available: %do, %fix, %review, %rework
```

Or auto-load in every session:

```bash
mkdir -p ~/.ipython/profile_default/startup/
echo "%load_ext jupyterhub_pilot.extension" > ~/.ipython/profile_default/startup/00-jupyterhub_pilot.ipy
```

---

## `%do` — Generate code from natural language

```python
%do load the CSV at data/sales.csv, parse the date column, and plot monthly revenue
```

JupyterHub-Pilot injects into the **next cell**:

```python
import pandas as pd
import matplotlib.pyplot as plt

df = pd.read_csv('data/sales.csv', parse_dates=['date'])
df['month'] = df['date'].dt.to_period('M')
monthly = df.groupby('month')['revenue'].sum()

monthly.plot(kind='bar', title='Monthly Revenue', figsize=(12, 5))
plt.tight_layout()
plt.show()
```

Run immediately instead of injecting:

```python
%do --run create a sample dataframe with 100 rows and columns name, age, score
```

The AI always sees the **last 3 cells of your notebook** as context — it knows what variables you have already defined.

---

## `%fix` — Auto-heal the last exception

```python
# You ran this and it crashed:
df.groupby('date')['revenue'].sum().plot()
# KeyError: 'date'
```

```python
# Just run:
%fix
# 🔧 JupyterHub-Pilot — analysing error and generating fix …

# Injects into next cell:
df['date'] = pd.to_datetime(df['date'])
df.groupby('date')['revenue'].sum().plot()
```

`%fix` strips all JupyterHub/IPython internal stack frames before sending to the LLM — only your code's error reaches the model.

---

## `%review` — AST-aware code quality review

```python
%review          # review last cell
%review -n 3     # review last 3 cells
%review --all    # review the entire notebook session
```

```text
============================================================
📋 JupyterHub-Pilot Code Review
============================================================

🔍 Static Analysis (AST):
  - Line 3: bare `except:` — catches all exceptions including KeyboardInterrupt
  - Line 7: `process_data()` missing return type annotation
  - Line 7: argument `df` in `process_data()` missing type hint

🤖 LLM Review:
  • Overall: The code works but lacks defensive programming and type safety.
  • Use `except Exception as e:` instead of bare `except`
  • Add type hint `df: pd.DataFrame` and return `-> pd.DataFrame`
  • Consider extracting date parsing into a helper for reusability
  • No security concerns found
============================================================
```

---

## `%rework` — LLM-powered refactoring

```python
%rework add type hints and a docstring
%rework --diff convert this to method chaining   # preview before applying
```

With `--diff`:

```diff
────────────────────────────────────────────────────────────
📝 Proposed changes (use %rework without --diff to apply):
────────────────────────────────────────────────────────────
--- original
+++ reworked
-def process(df):
-    result = df[df['age'] > 18]
-    result = result.groupby('city')['score'].mean()
-    return result
+def process(df: pd.DataFrame) -> pd.Series:
+    """Return mean score by city for adult users."""
+    return (
+        df[df['age'] > 18]
+        .groupby('city')['score']
+        .mean()
+    )
────────────────────────────────────────────────────────────
```

---

## LLM Backend Configuration (`~/.jupyterhub-pilot/config.json`)

The extension looks for its configuration on the Worker EC2 at `~/.jupyterhub-pilot/config.json`. 

```json
{
  "mode": "local",

  "local": {
    "url":     "http://localhost:11434/api/generate",
    "model":   "qwen2.5-coder:7b",
    "timeout": 30
  },

  "cloud": {
    "provider": "openai",
    "model":    "gpt-4o-mini",
    "api_key":  "sk-...",
    "timeout":  30
  },

  "mcp": {
    "server_url": "http://localhost:3000",
    "tools":      ["generate_code"],
    "timeout":    30
  },

  "rate_limit": {
    "requests_per_minute": 20
  }
}
```

| Mode | Best for | Notes |
|---|---|---|
| `local` | Air-gapped teams, low latency | Requires Ollama running on the worker EC2 |
| `cloud` | Best quality (GPT-4, Claude, Gemini) | Requires API key (can be Vault-injected); costs per token |
| `mcp` | Custom enterprise tool servers | MCP protocol over HTTP |
