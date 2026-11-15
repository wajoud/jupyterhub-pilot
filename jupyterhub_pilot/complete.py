"""
Inline code suggestions (the grey text ahead of the cursor in a notebook cell).

A Jupyter server extension in each user's notebook server: the page posts the
code around the cursor to ``jupyterhub-pilot/complete`` and gets a few lines back
from the model set in ``config.json`` (the same one the AI magics use).

Kept light on purpose: one model call at a time per user, a short answer, a
short timeout, and a 60-second pause after a failed call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from threading import Lock
from typing import Any, Dict

import requests

from .provider import LLMProvider, _extract_code

log = logging.getLogger("jupyterhub_pilot")

MAX_TOKENS = 64  # shortcut: reasoning models can spend all of it thinking and return nothing; use a plain model
TIMEOUT = 8  # seconds
PAUSE_AFTER_ERROR = 60  # seconds without calls after a failed one
PREFIX_CHARS, SUFFIX_CHARS = 2000, 500

_PROMPT = (
    "Complete the Python code from a Jupyter notebook at <CURSOR>. Reply with only "
    "the text to insert at <CURSOR>: at most 3 lines, no explanation, no markdown, "
    "and do not repeat code that is already there.\n\n{prefix}<CURSOR>{suffix}"
)

_one_at_a_time = Lock()
_paused_until = 0.0


def _ask(cfg: Dict[str, Any], prompt: str) -> str:
    """One short model call. Raises on any failure."""
    mode = cfg.get("mode", "local")
    backend = cfg.get(mode, {})
    model = cfg.get("autocomplete", {}).get("model") or backend.get("model")
    if mode == "local":
        resp = requests.post(
            os.environ.get("JUPYTERPILOT_OLLAMA_URL") or backend["url"],
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": MAX_TOKENS},
            },
            timeout=TIMEOUT,  # closing the connection also stops Ollama generating
        )
        resp.raise_for_status()
        return str(resp.json().get("response", ""))
    if mode == "cloud":
        import litellm

        response = litellm.completion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=MAX_TOKENS,
            timeout=TIMEOUT,
        )
        return response.choices[0].message.content or ""
    return ""  # mcp mode: no suggestions


def _clean(text: str, prefix: str) -> str:
    """Drop fences and a repeat of the line being typed."""
    text = _extract_code(text)
    current_line = prefix.rsplit("\n", 1)[-1]
    if current_line.strip() and text.startswith(current_line):
        text = text[len(current_line):]
    return text.rstrip()


def suggest(prefix: str, suffix: str = "") -> Dict[str, Any]:
    """``{"text": ...}`` for the page; ``"off": True`` tells it to stop asking."""
    global _paused_until
    # LLMProvider() also puts a cloud api_key from config.json into the environment.
    cfg = LLMProvider().config
    if not cfg.get("autocomplete", {}).get("enabled", True):
        return {"text": "", "off": True}
    if not prefix.strip() or time.monotonic() < _paused_until:
        return {"text": ""}
    if not _one_at_a_time.acquire(blocking=False):
        return {"text": ""}  # a call is running: skip, do not queue
    try:
        prompt = _PROMPT.format(
            prefix=prefix[-PREFIX_CHARS:], suffix=suffix[:SUFFIX_CHARS]
        )
        return {"text": _clean(_ask(cfg, prompt), prefix)}
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Code suggestions paused for %ds: %s", PAUSE_AFTER_ERROR, exc
        )
        _paused_until = time.monotonic() + PAUSE_AFTER_ERROR
        return {"text": ""}
    finally:
        _one_at_a_time.release()


def _jupyter_server_extension_points() -> list:
    return [{"module": "jupyterhub_pilot.complete"}]


def _load_jupyter_server_extension(server_app: Any) -> None:
    # Imported here: jupyter_server exists on workers, not on the Hub.
    from jupyter_server.base.handlers import APIHandler
    from jupyter_server.utils import url_path_join
    from tornado.web import authenticated

    class CompleteHandler(APIHandler):
        @authenticated
        async def post(self) -> None:
            body = self.get_json_body() or {}
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                suggest,
                str(body.get("prefix", "")),
                str(body.get("suffix", "")),
            )
            self.finish(json.dumps(result))

    web_app = server_app.web_app
    route = url_path_join(web_app.settings["base_url"], "jupyterhub-pilot/complete")
    web_app.add_handlers(".*$", [(route, CompleteHandler)])
