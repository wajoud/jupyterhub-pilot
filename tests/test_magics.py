"""
tests/test_magics.py
────────────────────
Unit tests for all JupyterHub-Pilot magic commands (Tasks 7 & 8).

Uses a minimal fake IPython shell — no real kernel needed.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from jupyterhub_pilot.extension import (
    JupyterHubPilotMagics,
    _analyse_ast,
    _clean_traceback,
    _get_context,
    _get_last_traceback,
    _truncate,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _make_shell(history=None, ns=None):
    """Return a minimal fake IPython shell for testing."""
    shell = MagicMock()
    shell.history_manager.input_hist_raw = history or []
    shell.user_ns = ns or {}
    shell._output_cache = {}
    shell._last_traceback = None
    return shell


def _make_magics(history=None, ns=None, provider_response="x = 1"):
    shell = _make_shell(history=history, ns=ns)
    magics = JupyterHubPilotMagics.__new__(JupyterHubPilotMagics)
    magics.shell = shell
    magics.provider = MagicMock()
    magics.provider.generate.return_value = provider_response
    magics.provider.mode = "local"
    magics.provider.active_model = "qwen2.5-coder:7b"
    magics.provider._backend = MagicMock()
    magics.provider._backend.generate.return_value = "# review output"
    return magics


# ──────────────────────────────────────────────────────────────────────────────
# _truncate
# ──────────────────────────────────────────────────────────────────────────────


class TestTruncate:
    def test_short_text_unchanged(self):
        assert _truncate("hello", 100) == "hello"

    def test_long_text_truncated(self):
        text = "a" * 2000
        result = _truncate(text, 800)
        assert len(result) < len(text)
        assert "omitted" in result

    def test_exact_limit_not_truncated(self):
        text = "b" * 800
        assert _truncate(text, 800) == text


# ──────────────────────────────────────────────────────────────────────────────
# _get_context
# ──────────────────────────────────────────────────────────────────────────────


class TestGetContext:
    def test_returns_empty_for_no_history(self):
        shell = _make_shell(history=[])
        ctx = _get_context(shell)
        assert isinstance(ctx, str)

    def test_filters_magic_commands(self):
        shell = _make_shell(history=["x = 1", "%do something", "y = 2"])
        ctx = _get_context(shell, n=5)
        assert "%do" not in ctx

    def test_returns_last_n_cells(self):
        history = ["a = 1", "b = 2", "c = 3", "d = 4"]
        shell = _make_shell(history=history)
        ctx = _get_context(shell, n=2)
        assert "c = 3" in ctx or "d = 4" in ctx
        assert "a = 1" not in ctx

    def test_includes_namespace_types(self):
        shell = _make_shell(history=["x = 1"], ns={"my_var": 42})
        ctx = _get_context(shell)
        assert "my_var" in ctx
        assert "int" in ctx


# ──────────────────────────────────────────────────────────────────────────────
# _analyse_ast
# ──────────────────────────────────────────────────────────────────────────────


class TestAnalyseAst:
    def test_clean_code_passes(self):
        result = _analyse_ast("x = 1\ny = 2")
        assert "✅" in result

    def test_bare_except_flagged(self):
        code = "try:\n    pass\nexcept:\n    pass"
        result = _analyse_ast(code)
        assert "bare" in result.lower() or "except" in result.lower()

    def test_missing_return_annotation_flagged(self):
        code = "def foo():\n    return 1"
        result = _analyse_ast(code)
        assert "foo" in result

    def test_missing_docstring_flagged(self):
        code = "def bar():\n    x = 1"
        result = _analyse_ast(code)
        assert "bar" in result

    def test_syntax_error_handled(self):
        result = _analyse_ast("def (:")
        assert "SyntaxError" in result or "syntax" in result.lower()

    def test_global_flagged(self):
        code = "x = 1\ndef foo():\n    global x\n    x = 2"
        result = _analyse_ast(code)
        assert "global" in result.lower()


# ──────────────────────────────────────────────────────────────────────────────
# _clean_traceback
# ──────────────────────────────────────────────────────────────────────────────


class TestCleanTraceback:
    def test_removes_ipython_frames(self):
        tb = (
            "Traceback (most recent call last):\n"
            '  File "IPython/core/interactiveshell.py", line 123\n'
            "    result = fn()\n"
            '  File "user_code.py", line 5\n'
            "    x = 1 / 0\n"
            "ZeroDivisionError: division by zero"
        )
        cleaned = _clean_traceback(tb)
        assert "IPython" not in cleaned
        assert "ZeroDivisionError" in cleaned
        assert "user_code.py" in cleaned

    def test_passes_through_clean_traceback(self):
        tb = "Traceback:\n  File user.py line 1\nValueError: bad"
        assert _clean_traceback(tb) == tb


# ──────────────────────────────────────────────────────────────────────────────
# _get_last_traceback
# ──────────────────────────────────────────────────────────────────────────────


class TestGetLastTraceback:
    def test_returns_none_with_no_error(self):
        shell = _make_shell(history=["x = 1"])
        # Clear any real sys.last_traceback
        for attr in ("last_traceback", "last_type", "last_value"):
            if hasattr(sys, attr):
                delattr(sys, attr)
        result = _get_last_traceback(shell)
        assert result is None

    def test_reads_shell_last_traceback(self):
        shell = _make_shell(history=["1/0"])
        shell._last_traceback = "ZeroDivisionError: division by zero"
        result = _get_last_traceback(shell)
        assert result is not None
        cell, tb = result
        assert "1/0" in cell
        assert "ZeroDivisionError" in tb


# ──────────────────────────────────────────────────────────────────────────────
# %do magic
# ──────────────────────────────────────────────────────────────────────────────


class TestDoMagic:
    def test_empty_prompt_prints_usage(self, capsys):
        m = _make_magics()
        m.do("")
        out = capsys.readouterr().out
        assert "Usage" in out

    def test_generates_and_injects_cell(self):
        m = _make_magics(history=["x = 1"], provider_response="y = 2")
        m.do("create y")
        m.provider.generate.assert_called_once()
        m.shell.set_next_input.assert_called_once_with("y = 2", replace=False)

    def test_run_flag_executes_immediately(self):
        m = _make_magics(provider_response="print('hi')")
        m.do("--run print hi")
        m.shell.run_cell.assert_called_once_with("print('hi')")

    def test_provider_called_with_prompt_and_context(self):
        m = _make_magics(history=["a = 1"])
        m.do("make b")
        args = m.provider.generate.call_args
        assert "make b" in args[0][0]


# ──────────────────────────────────────────────────────────────────────────────
# %fix magic
# ──────────────────────────────────────────────────────────────────────────────


class TestFixMagic:
    def test_no_error_prints_message(self, capsys):
        m = _make_magics(history=["x = 1"])
        m.shell._last_traceback = None
        for attr in ("last_traceback", "last_type", "last_value"):
            if hasattr(sys, attr):
                delattr(sys, attr)
        m.fix("")
        out = capsys.readouterr().out
        assert "No recent error" in out

    def test_injects_fixed_code_on_error(self):
        m = _make_magics(history=["1/0"], provider_response="x = 0  # fixed")
        m.shell._last_traceback = "ZeroDivisionError: division by zero"
        m.fix("")
        m.shell.set_next_input.assert_called_once()
        injected = m.shell.set_next_input.call_args[0][0]
        assert "fixed" in injected

    def test_fix_calls_provider_with_traceback(self):
        m = _make_magics(history=["bad_code()"])
        m.shell._last_traceback = "NameError: bad_code"
        m.fix("")
        call_prompt = m.provider.generate.call_args[0][0]
        assert "NameError" in call_prompt or "bad_code" in call_prompt


# ──────────────────────────────────────────────────────────────────────────────
# %review magic
# ──────────────────────────────────────────────────────────────────────────────


class TestReviewMagic:
    def test_no_history_prints_message(self, capsys):
        m = _make_magics(history=[])
        m.review("")
        out = capsys.readouterr().out
        assert "No cells" in out

    def test_review_runs_ast_and_llm(self, capsys):
        m = _make_magics(history=["x = 1\ny = 2"])
        m.review("")
        out = capsys.readouterr().out
        assert "Review" in out

    def test_review_n_flag(self, capsys):
        m = _make_magics(history=["a = 1", "b = 2", "c = 3"])
        m.review("-n 2")
        # Should not crash, output should appear
        out = capsys.readouterr().out
        assert "Review" in out

    def test_review_all_flag(self, capsys):
        m = _make_magics(history=["a = 1", "b = 2"])
        m.review("--all")
        out = capsys.readouterr().out
        assert "Review" in out


# ──────────────────────────────────────────────────────────────────────────────
# %rework magic
# ──────────────────────────────────────────────────────────────────────────────


class TestReworkMagic:
    def test_no_history_prints_message(self, capsys):
        m = _make_magics(history=[])
        m.rework("")
        out = capsys.readouterr().out
        assert "No cells" in out

    def test_injects_refactored_cell(self):
        m = _make_magics(history=["x=1"], provider_response="x = 1  # refactored")
        m.rework("add spacing")
        m.shell.set_next_input.assert_called_once()

    def test_diff_flag_prints_diff(self, capsys):
        m = _make_magics(history=["x=1"], provider_response="x = 1  # refactored")
        m.rework("--diff add spacing")
        out = capsys.readouterr().out
        # diff output or no-change message
        assert "diff" in out.lower() or "Proposed" in out or "No changes" in out
        # Should NOT inject the cell
        m.shell.set_next_input.assert_not_called()

    def test_default_instruction_used_when_empty(self):
        m = _make_magics(history=["x=1"], provider_response="x = 1")
        m.rework("")
        call_prompt = m.provider.generate.call_args[0][0]
        assert "clarity" in call_prompt.lower() or "refactor" in call_prompt.lower()
