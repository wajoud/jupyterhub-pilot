"""
jupyterhub_pilot/provider.py
────────────────────────
LLM & MCP Connectivity Layer (Task 6).

Three pluggable backends selected by ``config["mode"]``:

  "local"  → Ollama-compatible REST API (default: http://localhost:11434)
  "cloud"  → LiteLLM proxy (OpenAI, Claude, Gemini, …)
  "mcp"    → Model Context Protocol server (stdio or HTTP)

Config priority (highest → lowest):
  1. ~/.jupyterhub-pilot/config.json  (user-level)
  2. /etc/jupyterhub-pilot/config.json (system-level, written by install_hub.sh)
  3. Vault-injected env vars at spawn time (Task 3 integration)
  4. Built-in defaults
"""

from __future__ import annotations

import json
import logging
import os
import time
from functools import lru_cache
from threading import Lock
from typing import Any, Dict, List

import requests

log = logging.getLogger("jupyterhub_pilot")

# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: Dict[str, Any] = {
    "mode": "local",
    "local": {
        "url": "http://localhost:11434/api/generate",
        "model": "qwen2.5-coder:7b",
        "timeout": 30,
    },
    "cloud": {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "api_key": None,
        "timeout": 30,
    },
    "mcp": {
        "enabled": False,
        "server_url": "http://localhost:3000",
        "tools": [],
        "timeout": 30,
    },
    "rate_limit": {
        "requests_per_minute": 20,
    },
}

_SYSTEM_PROMPT = (
    "You are JupyterHub-Pilot, an expert Python coding assistant running inside "
    "a Jupyter notebook. Return ONLY executable Python code — no markdown "
    "fences, no prose, no explanations unless explicitly asked."
)

# ---------------------------------------------------------------------------
# Rate limiter (token bucket)
# ---------------------------------------------------------------------------


class _RateLimiter:
    """Simple token-bucket rate limiter (thread-safe)."""

    def __init__(self, requests_per_minute: int) -> None:
        self._rpm = max(1, requests_per_minute)
        self._interval = 60.0 / self._rpm
        self._last_call = 0.0
        self._lock = Lock()

    def wait(self) -> None:
        # PERF FIX: compute wait_time inside the lock, but sleep *outside*
        # so other threads are not blocked during the sleep period.
        with self._lock:
            now = time.monotonic()
            wait_time = self._interval - (now - self._last_call)
            if wait_time > 0:
                self._last_call += self._interval
            else:
                self._last_call = now
                wait_time = 0.0
        if wait_time > 0:
            time.sleep(wait_time)


# ---------------------------------------------------------------------------
# Code extractor (shared by all backends)
# ---------------------------------------------------------------------------


def _extract_code(text: str) -> str:
    """
    Extract Python code from an LLM response.

    Tries (in order):
      1. Fenced ```python ... ``` block
      2. Fenced ``` ... ``` block (language-agnostic)
      3. Raw text (stripped)
    """
    # PERF FIX: fast path — avoid scanning the string twice when no fences
    if "```" not in text:
        return text.strip("\r\n")

    lines = text.splitlines()
    code_lines: List[str] = []
    in_block = False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_block = not in_block
            continue
        if in_block:
            code_lines.append(line)

    extracted = "\n".join(code_lines)
    return extracted.strip("\r\n") if extracted else text.strip("\r\n")


# ---------------------------------------------------------------------------
# Config loader (cached per-process)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_config_cached() -> str:
    """
    Load and merge config exactly once per process, returned as JSON string
    (lru_cache requires hashable return types; callers call json.loads).
    """
    search_paths = [
        os.path.expanduser("~/.jupyterhub-pilot/config.json"),
        "/etc/jupyterhub-pilot/config.json",
    ]
    for path in search_paths:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    cfg = json.load(f)
                # Deep-merge user config into defaults
                merged: Dict[str, Any] = json.loads(json.dumps(_DEFAULT_CONFIG))
                for k, v in cfg.items():
                    if isinstance(v, dict) and k in merged:
                        merged[k].update(v)
                    else:
                        merged[k] = v
                return json.dumps(merged)
            except Exception as exc:  # noqa: BLE001
                log.warning("LLMProvider: failed to parse %s: %s", path, exc)
    return json.dumps(_DEFAULT_CONFIG)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class _LocalBackend:
    """
    Ollama-compatible local inference backend.

    Supports any server implementing the Ollama REST API
    (``POST /api/generate``).
    """

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self._url = (
            os.environ.get("JUPYTERPILOT_OLLAMA_URL")
            or cfg.get("url", "http://localhost:11434/api/generate")
        )
        self._model = cfg.get("model", "qwen2.5-coder:7b")
        self._timeout = int(cfg.get("timeout", 30))

    def generate(self, prompt: str) -> str:
        try:
            resp = requests.post(
                self._url,
                json={"model": self._model, "prompt": prompt, "stream": False},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return _extract_code(resp.json().get("response", "").strip())
        except requests.Timeout:
            return f"# ⏱ Local inference timed out after {self._timeout}s"
        except Exception as exc:  # noqa: BLE001
            return f"# ❌ Local inference error: {exc}"


class _CloudBackend:
    """
    LiteLLM-powered cloud backend (OpenAI, Claude, Gemini, …).

    Reads API keys from (priority order):
      1. config.json ``cloud.api_key``
      2. Vault-injected env var  (e.g. OPENAI_API_KEY from Task 3)
      3. Standard provider env var
    """

    _MAX_RETRIES = 3
    _BACKOFF_BASE = 1.5  # seconds

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self._model = cfg.get("model", "gpt-4o-mini")
        self._timeout = int(cfg.get("timeout", 30))

        # Inject API key into env if provided in config
        api_key = cfg.get("api_key")
        if api_key:
            provider = cfg.get("provider", "openai").upper()
            os.environ.setdefault(f"{provider}_API_KEY", api_key)

    def generate(self, prompt: str) -> str:
        try:
            import litellm  # lazy import — only needed for cloud mode
        except ImportError:
            return "# ❌ litellm not installed. Run: pip install jupyterhub_pilot[ai]"

        for attempt in range(self._MAX_RETRIES):
            try:
                response = litellm.completion(
                    model=self._model,
                    messages=[{"role": "user", "content": prompt}],
                    timeout=self._timeout,
                )
                text = response.choices[0].message.content or ""
                return _extract_code(text.strip())
            except Exception as exc:  # noqa: BLE001
                if attempt == self._MAX_RETRIES - 1:
                    return f"# ❌ Cloud inference error after {self._MAX_RETRIES} retries: {exc}"
                backoff = self._BACKOFF_BASE ** attempt
                log.warning(
                    "LLMProvider: cloud attempt %d/%d failed (%s), retrying in %.1fs",
                    attempt + 1,
                    self._MAX_RETRIES,
                    exc,
                    backoff,
                )
                time.sleep(backoff)

        return "# ❌ Cloud inference failed after all retries"


class _MCPBackend:
    """
    Model Context Protocol backend.

    Connects to an MCP server over HTTP (``tools/call`` endpoint) and
    executes the configured tools to generate code.
    """

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self._server_url = cfg.get("server_url", "http://localhost:3000").rstrip("/")
        self._tools: List[str] = cfg.get("tools", [])
        self._timeout = int(cfg.get("timeout", 30))

    def generate(self, prompt: str) -> str:
        if not self._tools:
            return "# ❌ MCP: no tools configured in config.json mcp.tools list"

        tool_name = self._tools[0]  # Use the first configured tool
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": {"prompt": prompt},
            },
        }
        try:
            resp = requests.post(
                f"{self._server_url}/mcp",
                json=payload,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            result = resp.json().get("result", {})
            content = result.get("content", [{}])
            text = content[0].get("text", "") if content else ""
            return _extract_code(text)
        except requests.Timeout:
            return f"# ⏱ MCP server timed out after {self._timeout}s"
        except Exception as exc:  # noqa: BLE001
            return f"# ❌ MCP error: {exc}"


# ---------------------------------------------------------------------------
# Public LLMProvider
# ---------------------------------------------------------------------------


class LLMProvider:
    """
    Provider-agnostic LLM engine.

    Selects the active backend from config and wraps it with rate limiting.
    Thread-safe for concurrent notebook kernels on the same VM.

    Config is loaded once per process (cached) — not on every instantiation.
    """

    def __init__(self) -> None:
        # PERF FIX: deserialise from the process-level cached JSON string
        self.config: Dict[str, Any] = self._load_config()
        rpm = self.config.get("rate_limit", {}).get("requests_per_minute", 20)
        self._rate_limiter = _RateLimiter(rpm)
        self._backend = self._build_backend()

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    @staticmethod
    def _load_config() -> Dict[str, Any]:
        """Load config with cascading priority (delegates to cached loader)."""
        return json.loads(_load_config_cached())

    def _build_backend(self) -> _LocalBackend | _CloudBackend | _MCPBackend:
        mode = self.config.get("mode", "local")
        if mode == "cloud":
            return _CloudBackend(self.config.get("cloud", {}))
        if mode == "mcp":
            return _MCPBackend(self.config.get("mcp", {}))
        return _LocalBackend(self.config.get("local", {}))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self, prompt: str, context: str = "") -> str:
        """
        Generate Python code for *prompt* given *context* from recent cells.

        Rate-limited and backend-agnostic.
        """
        full_prompt = self._build_prompt(prompt, context)
        self._rate_limiter.wait()
        return self._backend.generate(full_prompt)

    @staticmethod
    def _build_prompt(prompt: str, context: str) -> str:
        parts = [_SYSTEM_PROMPT]
        if context.strip():
            parts.append(f"\n# Context from recent notebook cells:\n{context}")
        parts.append(f"\n# Task:\n{prompt}")
        return "\n".join(parts)

    @property
    def mode(self) -> str:
        return self.config.get("mode", "local")

    @property
    def active_model(self) -> str:
        cfg = self.config.get(self.mode, {})
        return str(cfg.get("model", "unknown"))
