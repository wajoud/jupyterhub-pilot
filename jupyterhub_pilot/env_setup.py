"""
jupyterhub_pilot/env_setup.py
─────────────────────────
Controlled Environment Strategy (Task 5).

Ensures the AI dependencies (ipython, litellm, mcp) are available in the
current Python environment before any magic commands are registered.

Strategy (in priority order):
  1. If deps are already importable → no-op, instant return.
  2. If a user venv exists at ~/.jupyterhub-pilot/venv → activate it.
  3. Try to create the venv and install jupyterhub_pilot[ai] into it.
  4. If venv creation fails (e.g. python3-venv not installed) → fall back to
     installing deps directly via pip into the user's local site-packages.
     This keeps things working on stock Ubuntu without extra apt packages.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_VENV_DIR = Path.home() / ".jupyterhub_pilot" / "venv"
_AI_DEPS = ["ipython", "litellm"]  # mcp is optional — checked separately

# PERF FIX: cache the dependency-check result so repeated ensure_ai_deps()
# calls (e.g. from %load_ext being called more than once) return instantly.
_DEPS_OK: bool = False


def _deps_available() -> bool:
    """Return True if all required AI packages are importable."""
    for dep in _AI_DEPS:
        try:
            __import__(dep)
        except ImportError:
            return False
    return True


def _venv_site_packages() -> Path | None:
    """Return the site-packages path inside the user venv, if it exists."""
    for candidate in _VENV_DIR.glob("lib/python*/site-packages"):
        return candidate
    return None


def _activate_venv() -> bool:
    """Prepend venv site-packages to sys.path. Returns True if successful."""
    sp = _venv_site_packages()
    if sp and sp.exists():
        site_str = str(sp)
        if site_str not in sys.path:
            sys.path.insert(0, site_str)
        return True
    return False


def _create_venv() -> bool:
    """
    Create ~/.jupyterhub-pilot/venv and install AI deps into it.
    Returns True on success, False if venv module is unavailable.
    """
    _VENV_DIR.mkdir(parents=True, exist_ok=True)
    python = sys.executable

    print(f"[JupyterHub-Pilot] Creating AI venv at {_VENV_DIR} …")
    result = subprocess.run(
        [python, "-m", "venv", str(_VENV_DIR)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # python3-venv not installed — tell the user but don't crash
        print(
            "[JupyterHub-Pilot] ⚠️  Could not create venv "
            f"(install python3-venv for full isolation):\n  {result.stderr.strip()}"
        )
        return False

    pip = _VENV_DIR / "bin" / "pip"
    print("[JupyterHub-Pilot] Installing AI dependencies into venv (once-only) …")
    subprocess.run(
        [str(pip), "install", "--quiet", "jupyterhub_pilot[ai]@git+https://github.com/wajoud/jupyterhub-pilot.git"],
        check=True,
    )
    print("[JupyterHub-Pilot] ✅ AI dependencies installed into venv.")
    return True


def _install_deps_inline() -> None:
    """
    Fallback: install AI deps directly into the user's site-packages via pip.
    Used when venv creation fails (e.g. python3-venv not installed on Ubuntu).
    """
    print("[JupyterHub-Pilot] Falling back to inline pip install of AI deps …")
    subprocess.run(
        [
            sys.executable, "-m", "pip", "install", "--quiet",
            "jupyterhub_pilot[ai]@git+https://github.com/wajoud/jupyterhub-pilot.git",
            "--break-system-packages",
            "--user",
        ],
        check=True,
    )
    # Ensure user site-packages is on the path for this session
    import site
    user_site = site.getusersitepackages()
    if user_site not in sys.path:
        sys.path.insert(0, user_site)
    print("[JupyterHub-Pilot] ✅ AI dependencies installed (inline).")


def ensure_ai_deps() -> None:
    """
    Public entry point — called at magic registration time.

    Guarantees that after this function returns, all AI packages are importable.
    Raises RuntimeError only as a last resort if all strategies fail.

    PERF FIX: result is cached in ``_DEPS_OK`` so subsequent calls (e.g. from
    IPython calling ``%load_ext`` again) are essentially free.
    """
    global _DEPS_OK
    if _DEPS_OK:
        return  # Cached fast path — already verified this session

    if _deps_available():
        _DEPS_OK = True
        return  # Fast path — already available, nothing to do

    # Try activating an existing venv first
    if _activate_venv() and _deps_available():
        _DEPS_OK = True
        return

    # Try creating the venv
    venv_ok = _create_venv()
    if venv_ok:
        _activate_venv()
        if _deps_available():
            _DEPS_OK = True
            return

    # Venv unavailable — fall back to inline pip install
    try:
        _install_deps_inline()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "[JupyterHub-Pilot] Could not install AI dependencies automatically.\n"
            "Run manually:  pip install 'jupyterhub_pilot[ai]' --break-system-packages\n"
            f"Details: {exc}"
        ) from exc

    if not _deps_available():
        raise RuntimeError(
            "[JupyterHub-Pilot] AI packages still not importable after install.\n"
            "Try restarting the kernel after running:\n"
            "  pip install 'jupyterhub_pilot[ai]' --break-system-packages"
        )
    _DEPS_OK = True
