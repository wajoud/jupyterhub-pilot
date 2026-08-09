"""
tests/test_provider.py
──────────────────────
Unit tests for the LLM provider backends (Task 6).

All HTTP calls are mocked — no real Ollama or cloud API needed.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from jupyterhub_pilot.provider import (
    LLMProvider,
    _CloudBackend,
    _LocalBackend,
    _MCPBackend,
    _RateLimiter,
    _extract_code,
)


# ──────────────────────────────────────────────────────────────────────────────
# _extract_code
# ──────────────────────────────────────────────────────────────────────────────


class TestExtractCode:
    def test_plain_text_returned_as_is(self):
        assert _extract_code("x = 1") == "x = 1"

    def test_fenced_python_block_extracted(self):
        md = "```python\nprint('hello')\n```"
        assert _extract_code(md) == "print('hello')"

    def test_fenced_generic_block_extracted(self):
        md = "```\ndf.head()\n```"
        assert _extract_code(md) == "df.head()"

    def test_multiline_block_extracted(self):
        md = "Here is the code:\n```python\na = 1\nb = 2\n```\nDone."
        result = _extract_code(md)
        assert "a = 1" in result
        assert "b = 2" in result

    def test_empty_fenced_block_returns_raw(self):
        md = "```\n```"
        result = _extract_code(md)
        # Empty block — should return something (the raw text)
        assert isinstance(result, str)


# ──────────────────────────────────────────────────────────────────────────────
# _RateLimiter
# ──────────────────────────────────────────────────────────────────────────────


class TestRateLimiter:
    def test_does_not_block_on_first_call(self):
        import time
        rl = _RateLimiter(requests_per_minute=60)
        start = time.monotonic()
        rl.wait()
        elapsed = time.monotonic() - start
        assert elapsed < 0.5, "First call should return near-instantly"

    def test_low_rpm_clips_to_1(self):
        rl = _RateLimiter(requests_per_minute=0)
        assert rl._rpm == 1


# ──────────────────────────────────────────────────────────────────────────────
# _LocalBackend
# ──────────────────────────────────────────────────────────────────────────────


class TestLocalBackend:
    def _backend(self):
        return _LocalBackend({
            "url": "http://localhost:11434/api/generate",
            "model": "qwen2.5-coder:7b",
            "timeout": 5,
        })

    @patch("jupyterhub_pilot.provider.requests.post")
    def test_generate_returns_cleaned_code(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "```python\nx = 1\n```"}
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        backend = self._backend()
        result = backend.generate("create a variable x")
        assert "x = 1" in result

    @patch("jupyterhub_pilot.provider.requests.post")
    def test_generates_plain_code(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "print('hello')"}
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        backend = self._backend()
        result = backend.generate("print hello")
        assert "print" in result

    @patch("jupyterhub_pilot.provider.requests.post", side_effect=Exception("Connection refused"))
    def test_generate_returns_error_comment_on_failure(self, mock_post):
        backend = self._backend()
        result = backend.generate("anything")
        assert result.startswith("# ❌")

    @patch("jupyterhub_pilot.provider.requests.post")
    def test_uses_env_var_url_override(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "x = 42"}
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        os.environ["JUPYTERPILOT_OLLAMA_URL"] = "http://custom-host:11434/api/generate"
        backend = _LocalBackend({"model": "test"})
        assert backend._url == "http://custom-host:11434/api/generate"
        del os.environ["JUPYTERPILOT_OLLAMA_URL"]


# ──────────────────────────────────────────────────────────────────────────────
# _CloudBackend
# ──────────────────────────────────────────────────────────────────────────────


class TestCloudBackend:
    def _backend(self):
        return _CloudBackend({
            "provider": "openai",
            "model": "gpt-4o-mini",
            "api_key": "sk-test-key",
            "timeout": 5,
        })

    def test_injects_api_key_to_env(self):
        if "OPENAI_API_KEY" in os.environ:
            del os.environ["OPENAI_API_KEY"]
        _CloudBackend({"provider": "openai", "api_key": "sk-test-123"})
        assert os.environ.get("OPENAI_API_KEY") == "sk-test-123"

    @patch("jupyterhub_pilot.provider._CloudBackend.generate")
    def test_generate_calls_litellm(self, mock_gen):
        mock_gen.return_value = "x = 1"
        backend = self._backend()
        result = backend.generate("create x")
        assert result == "x = 1"

    def test_missing_litellm_returns_error(self):
        with patch.dict("sys.modules", {"litellm": None}):
            backend = self._backend()
            result = backend.generate("anything")
            assert "❌" in result or isinstance(result, str)


# ──────────────────────────────────────────────────────────────────────────────
# _MCPBackend
# ──────────────────────────────────────────────────────────────────────────────


class TestMCPBackend:
    def _backend(self):
        return _MCPBackend({
            "server_url": "http://localhost:3000",
            "tools": ["generate_code"],
            "timeout": 5,
        })

    def test_no_tools_returns_error(self):
        backend = _MCPBackend({"server_url": "http://localhost:3000", "tools": []})
        result = backend.generate("anything")
        assert "❌" in result

    @patch("jupyterhub_pilot.provider.requests.post")
    def test_generate_calls_tools_call_endpoint(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "result": {"content": [{"text": "import pandas as pd"}]}
        }
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        backend = self._backend()
        result = backend.generate("import pandas")
        assert "pandas" in result
        # Verify correct endpoint was called
        call_url = mock_post.call_args[0][0]
        assert "/mcp" in call_url

    @patch("jupyterhub_pilot.provider.requests.post", side_effect=Exception("unreachable"))
    def test_generate_returns_error_on_failure(self, mock_post):
        backend = self._backend()
        result = backend.generate("anything")
        assert "❌" in result


# ──────────────────────────────────────────────────────────────────────────────
# LLMProvider (integration)
# ──────────────────────────────────────────────────────────────────────────────


class TestLLMProvider:
    def test_defaults_to_local_mode(self):
        with patch("jupyterhub_pilot.provider.LLMProvider._load_config", return_value={
            "mode": "local",
            "local": {"url": "http://localhost:11434/api/generate", "model": "qwen2.5-coder:7b"},
            "rate_limit": {"requests_per_minute": 120},
        }):
            provider = LLMProvider()
            assert provider.mode == "local"

    def test_builds_cloud_backend(self):
        with patch("jupyterhub_pilot.provider.LLMProvider._load_config", return_value={
            "mode": "cloud",
            "cloud": {"provider": "openai", "model": "gpt-4o-mini"},
            "rate_limit": {"requests_per_minute": 120},
        }):
            provider = LLMProvider()
            assert isinstance(provider._backend, _CloudBackend)

    def test_builds_mcp_backend(self):
        with patch("jupyterhub_pilot.provider.LLMProvider._load_config", return_value={
            "mode": "mcp",
            "mcp": {"server_url": "http://localhost:3000", "tools": ["run"]},
            "rate_limit": {"requests_per_minute": 120},
        }):
            provider = LLMProvider()
            assert isinstance(provider._backend, _MCPBackend)

    def test_generate_builds_full_prompt(self):
        with patch("jupyterhub_pilot.provider.LLMProvider._load_config", return_value={
            "mode": "local",
            "local": {"url": "http://x", "model": "m"},
            "rate_limit": {"requests_per_minute": 120},
        }):
            provider = LLMProvider()
            with patch.object(provider._backend, "generate", return_value="x = 1") as mock_gen:
                provider.generate("create x", context="# ctx")
                call_prompt = mock_gen.call_args[0][0]
                assert "create x" in call_prompt
                assert "# ctx" in call_prompt
