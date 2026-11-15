"""Inline code suggestions (complete.py) and the Jupyter files shipped with them."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from jupyterhub_pilot import complete

ROOT = os.path.dirname(os.path.dirname(__file__))


@pytest.fixture(autouse=True)
def _fresh():
    complete._paused_until = 0.0
    yield
    complete._paused_until = 0.0


def test_fences_and_repeated_line_are_dropped():
    assert complete._clean("```python\ntotal = a + b\n```", "a = 1\ntotal = ") == "a + b"
    assert complete._clean("print(x)\n", "x = 1\n") == "print(x)"


def test_suggest_returns_the_cleaned_answer_and_trims_the_prompt():
    with patch.object(complete, "_ask", return_value="y = x + 1") as ask:
        assert complete.suggest("x" * 5000 + "\ny = ", "z" * 5000) == {"text": "x + 1"}
    prompt = ask.call_args[0][1]
    assert len(prompt) < complete.PREFIX_CHARS + complete.SUFFIX_CHARS + 400


def test_no_model_call_for_an_empty_cell_or_while_one_is_running():
    with patch.object(complete, "_ask") as ask:
        assert complete.suggest("  \n") == {"text": ""}
        with complete._one_at_a_time:
            assert complete.suggest("x = ") == {"text": ""}
    ask.assert_not_called()


def test_a_failed_call_pauses_suggestions():
    with patch.object(complete, "_ask", side_effect=OSError("no model")) as ask:
        assert complete.suggest("x = ") == {"text": ""}
        assert complete.suggest("x = ") == {"text": ""}
    assert ask.call_count == 1


def test_turned_off_in_config():
    cfg = {"mode": "local", "autocomplete": {"enabled": False}}
    with patch.object(complete, "LLMProvider") as provider, patch.object(
        complete, "_ask"
    ) as ask:
        provider.return_value.config = cfg
        assert complete.suggest("x = ") == {"text": "", "off": True}
    ask.assert_not_called()


def test_local_call_is_short_and_can_use_its_own_model():
    cfg = {
        "mode": "local",
        "local": {"url": "http://ollama/api/generate", "model": "big"},
        "autocomplete": {"model": "small"},
    }
    with patch.dict(os.environ, {}, clear=False), patch.object(
        complete.requests, "post"
    ) as post:
        os.environ.pop("JUPYTERPILOT_OLLAMA_URL", None)
        post.return_value.json.return_value = {"response": "1"}
        assert complete._ask(cfg, "p") == "1"
    sent = post.call_args
    assert sent.kwargs["json"]["model"] == "small"
    assert sent.kwargs["json"]["options"] == {"num_predict": complete.MAX_TOKENS}
    assert sent.kwargs["timeout"] == complete.TIMEOUT


def test_jupyter_files_are_valid_and_packaged():
    with open(os.path.join(ROOT, "pyproject.toml")) as f:
        pyproject = f.read()
    for path in (
        "jupyter/labextension/package.json",
        "jupyter/settings/jupyterhub-pilot.json",
        "jupyter/server/jupyterhub-pilot.json",
    ):
        with open(os.path.join(ROOT, path)) as f:
            data = json.load(f)
        assert path in pyproject
    assert data["ServerApp"]["jpserver_extensions"] == {"jupyterhub_pilot.complete": True}
    with open(os.path.join(ROOT, "jupyter/labextension/package.json")) as f:
        load = json.load(f)["jupyterlab"]["_build"]["load"]
    assert os.path.exists(os.path.join(ROOT, "jupyter/labextension", load))
    assert f"jupyter/labextension/{load}" in pyproject
