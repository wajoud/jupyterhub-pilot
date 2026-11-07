"""
jupyterhub_pilot/extension.py
─────────────────────────
Core IPython magic commands for JupyterHub-Pilot (Tasks 7 & 8).

Magics
──────
  %do <prompt>               Generate code from natural language
  %do --run <prompt>         Generate AND immediately execute the code
  %fix                       Auto-heal the last exception with LLM
  %review                    AST-aware code quality review of last cell
  %review -n <N>             Review the last N cells
  %review --all              Review the entire notebook session
  %rework <instruction>      Refactor the last cell with an LLM instruction
  %rework --diff <instr>     Show a unified diff before replacing the cell
"""

from __future__ import annotations

import ast
import difflib
import sys
import traceback
from typing import List, Optional, Tuple

from IPython.core.magic import Magics, line_magic, magics_class
from IPython.core.magic_arguments import argument, magic_arguments, parse_argstring

from .provider import LLMProvider

_MAGIC_PREFIXES = ("%do", "%fix", "%review", "%rework")

# ──────────────────────────────────────────────────────────────────────────────
# Context builder
# ──────────────────────────────────────────────────────────────────────────────


def _truncate(text: str, max_chars: int = 800) -> str:
    """Keep first 400 + last 400 chars of a long string."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + f"\n… [{len(text) - max_chars} chars omitted] …\n" + text[-half:]


def _user_cells(shell) -> List[Tuple[int, str]]:
    """Return (execution_count, source) for non-empty, non-magic cells."""
    # input_hist_raw[i] is the cell run with execution count i.
    return [
        (i, h.strip())
        for i, h in enumerate(shell.history_manager.input_hist_raw)
        if h.strip() and not h.strip().startswith(_MAGIC_PREFIXES)
    ]


def _get_context(shell, n: int = 3) -> str:
    """
    Build a rich context string from the last *n* executed cells.

    Includes:
      - Cell source (input)
      - Cell stdout/display output (if available and non-empty)
      - Top-10 namespace variable names + types (as a comment block)
    """
    recent = _user_cells(shell)[-n:]

    # IPython keeps each cell's result in Out[<execution count>]
    out_cache = shell.user_ns.get("Out", {}) or {}

    sections: List[str] = []
    for count, cell_src in recent:
        label = f"# ── Cell {count} ──"
        cell_text = f"{label}\n{_truncate(cell_src)}"
        # `in`, not truthiness: a DataFrame result has no truth value
        if count in out_cache:
            cell_text += f"\n# Output:\n# {_truncate(str(out_cache[count]), 400)}"
        sections.append(cell_text)

    # PERF FIX: single-pass with early-exit counter — avoids slicing twice.
    ns_lines: List[str] = []
    for k, v in reversed(shell.user_ns.items()):
        if len(ns_lines) >= 10:
            break
        if k.startswith("_") or callable(v):
            continue
        ns_lines.append(f"#   {k}: {type(v).__name__}")
    ns_lines.reverse()
    if ns_lines:
        sections.append("# Namespace:\n" + "\n".join(ns_lines))

    return "\n\n".join(sections)


# ──────────────────────────────────────────────────────────────────────────────
# AST analyser (for %review)
# ──────────────────────────────────────────────────────────────────────────────


def _analyse_ast(source: str) -> str:
    """
    Parse *source* with the ``ast`` module and return a structured summary
    of code quality issues.
    """
    issues: List[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"⚠️  SyntaxError — cannot parse: {exc}"

    for node in ast.walk(tree):
        # Bare except:
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            issues.append(f"  - Line {node.lineno}: bare `except:` — catches all exceptions including KeyboardInterrupt")

        # Function without return type annotation
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is None:
                issues.append(f"  - Line {node.lineno}: `{node.name}()` missing return type annotation")
            # Missing arg annotations
            for arg in node.args.args:
                if arg.annotation is None and arg.arg != "self":
                    issues.append(f"  - Line {node.lineno}: argument `{arg.arg}` in `{node.name}()` missing type hint")
            # Missing docstring
            if not (node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)):
                issues.append(f"  - Line {node.lineno}: `{node.name}()` missing docstring")

        # Global statement
        if isinstance(node, ast.Global):
            issues.append(f"  - Line {node.lineno}: `global` statement — consider refactoring to avoid mutable globals")

    if not issues:
        return "  ✅ No obvious issues found by static analysis."
    return "\n".join(issues)


# ──────────────────────────────────────────────────────────────────────────────
# Traceback cleaner (for %fix)
# ──────────────────────────────────────────────────────────────────────────────


_IPYTHON_INTERNALS = (
    "IPython/", "ipykernel/", "tornado/", "zmq/",
    "traitlets/", "jupyter_client/",
)


def _clean_traceback(tb_str: str) -> str:
    """Strip IPython/Jupyter internal frames from a traceback string."""
    lines = tb_str.splitlines()
    cleaned: List[str] = []
    skip_next = False
    for line in lines:
        if skip_next and line.startswith("    "):
            continue
        skip_next = False
        if any(internal in line for internal in _IPYTHON_INTERNALS):
            skip_next = True
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def _get_last_traceback(shell) -> Optional[Tuple[str, str]]:
    """
    Return (last_cell_source, cleaned_traceback) or None if no error.

    IPython sets ``sys.last_*`` whenever a cell raises.  (ipykernel's
    ``shell._last_traceback`` is no use here: it is reset when ``%fix`` runs.)
    """
    tb = getattr(sys, "last_traceback", None)
    if tb is None:
        return None
    raw_tb = "".join(traceback.format_exception(sys.last_type, sys.last_value, tb))
    cleaned = _clean_traceback(raw_tb)

    # history[-1] is the %fix cell itself; take the last real code cell.
    cells = _user_cells(shell)
    last_cell = cells[-1][1] if cells else ""
    return last_cell, cleaned


# ──────────────────────────────────────────────────────────────────────────────
# Magic class
# ──────────────────────────────────────────────────────────────────────────────


@magics_class
class JupyterHubPilotMagics(Magics):
    def __init__(self, shell):
        super().__init__(shell)
        self.provider = LLMProvider()

    def _inject_cell(self, code: str, run: bool = False) -> None:
        """Inject *code* as the next cell, or run it immediately if *run* is True."""
        if run:
            self.shell.run_cell(code)
        else:
            try:
                self.shell.set_next_input(code, replace=False)
            except TypeError:
                # Older IPython versions don't have replace=
                self.shell.set_next_input(code)

    # ── %do ───────────────────────────────────────────────────────────────────

    @magic_arguments()
    @argument("--run", action="store_true", help="Execute the generated code immediately")
    @argument("prompt", nargs="*", help="Natural language instruction")
    @line_magic
    def do(self, line):
        """%do [--run] <prompt> — Generate Python code from natural language."""
        args = parse_argstring(self.do, line)
        prompt = " ".join(args.prompt).strip()

        if not prompt:
            print("Usage: %do [--run] <what you want to do>\nExample: %do create a bar chart from df")
            return

        print(f"# 🤖 JupyterHub-Pilot ({self.provider.mode}/{self.provider.active_model}) — generating …")
        context = _get_context(self.shell)
        code = self.provider.generate(prompt, context)

        if args.run:
            print("# ▶️  Running generated code …")
            self._inject_cell(code, run=True)
        else:
            self._inject_cell(code, run=False)

    # ── %fix ──────────────────────────────────────────────────────────────────

    @line_magic
    def fix(self, line):
        """%fix — Auto-heal the last exception using the LLM."""
        result = _get_last_traceback(self.shell)
        if result is None:
            print("ℹ️  No recent error found. Run some code that raises an exception first.")
            return

        last_cell, cleaned_tb = result
        if not last_cell:
            print("ℹ️  Could not retrieve the last executed cell.")
            return

        print("# 🔧 JupyterHub-Pilot — analysing error and generating fix …")
        context = _get_context(self.shell)
        prompt = (
            "The following Python code raised an exception. "
            "Fix the code. Return ONLY the corrected Python code.\n\n"
            f"Code:\n{last_cell}\n\n"
            f"Error:\n{cleaned_tb}"
        )
        fixed = self.provider.generate(prompt, context)
        self._inject_cell(fixed)

    # ── %review ───────────────────────────────────────────────────────────────

    @magic_arguments()
    @argument("-n", type=int, default=1, help="Number of recent cells to review (default: 1)")
    @argument("--all", dest="all_cells", action="store_true", help="Review the entire session")
    @line_magic
    def review(self, line):
        """%review [-n N] [--all] — AST-aware code quality review."""
        args = parse_argstring(self.review, line)

        valid = [src for _, src in _user_cells(self.shell)]

        if not valid:
            print("ℹ️  No cells to review yet.")
            return

        if args.all_cells:
            cells = valid
        else:
            cells = valid[-args.n:]

        combined_source = "\n\n".join(cells)

        # Static analysis first
        print("=" * 60)
        print("📋 JupyterHub-Pilot Code Review")
        print("=" * 60)
        print("\n🔍 Static Analysis (AST):")
        print(_analyse_ast(combined_source))

        # LLM qualitative review
        print("\n🤖 LLM Review:")
        review_prompt = (
            "You are a senior Python engineer performing a code review. "
            "Analyse the following notebook code and provide:\n"
            "  1. A 1-sentence overall assessment\n"
            "  2. Up to 5 specific, actionable improvements\n"
            "  3. Any security or performance concerns\n\n"
            "Be concise. Use bullet points.\n\n"
            f"Code to review:\n```python\n{_truncate(combined_source, 2000)}\n```"
        )
        # For %review we want prose, not just code — call backend directly
        raw_review = self.provider._backend.generate(review_prompt)
        print(raw_review)
        print("=" * 60)

    # ── %rework ───────────────────────────────────────────────────────────────

    @magic_arguments()
    @argument("--diff", action="store_true", help="Show a unified diff instead of replacing the cell")
    @argument("instruction", nargs="*", help="Refactoring instruction (optional)")
    @line_magic
    def rework(self, line):
        """%rework [--diff] [instruction] — Refactor the last cell with LLM guidance."""
        args = parse_argstring(self.rework, line)
        instruction = " ".join(args.instruction).strip() or "Refactor for clarity, readability, and best practices"

        valid = [src for _, src in _user_cells(self.shell)]

        if not valid:
            print("ℹ️  No cells to rework yet.")
            return

        original = valid[-1]
        context = _get_context(self.shell)

        print("# ♻️  JupyterHub-Pilot — reworking cell …")
        prompt = (
            "You are a senior Python engineer. Refactor the following code "
            "according to the instruction. Return ONLY the refactored Python code.\n\n"
            f"Instruction: {instruction}\n\n"
            f"Code:\n```python\n{original}\n```"
        )
        refactored = self.provider.generate(prompt, context)

        if args.diff:
            # Show a coloured unified diff
            diff = list(difflib.unified_diff(
                original.splitlines(keepends=True),
                refactored.splitlines(keepends=True),
                fromfile="original",
                tofile="reworked",
                lineterm="",
            ))
            if diff:
                print("─" * 60)
                print("📝 Proposed changes (use %rework without --diff to apply):")
                print("─" * 60)
                for dl in diff:
                    print(dl, end="")
                print("\n" + "─" * 60)
            else:
                print("ℹ️  No changes proposed — the code is already well-structured!")
        else:
            self._inject_cell(refactored)


# ──────────────────────────────────────────────────────────────────────────────
# Extension entry point
# ──────────────────────────────────────────────────────────────────────────────


def load_ipython_extension(ipython) -> None:
    """Register JupyterHub-Pilot magics with the running IPython kernel."""
    ipython.register_magics(JupyterHubPilotMagics(ipython))
    print(
        "✅ JupyterHub-Pilot loaded. Available: %do, %fix, %review, %rework\n"
        "   Run %do? for help on any command."
    )
