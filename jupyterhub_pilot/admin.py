"""
jupyterhub_pilot/admin.py
──────────────────────
Role-Based Access Control (RBAC) for JupyterHub-Pilot.

Design decisions:
  - 2-tier model only: ``admin`` and ``user``.
  - Admin status is determined exclusively by JupyterHub's native ``user.admin``
    boolean, which is populated from ``admin_users`` in ``hub_settings.json``
    and can be changed live via the JupyterHub Admin Panel.
  - No JSON files, no DB reads, no restarts needed to change a user's role.
  - Pure-Python with no JupyterHub import — fully unit-testable with mocks.

Roles
─────
  admin  → Recorded with the session; sees every worker on the dashboard.
  user   → Sees only the workers of their own groups on the dashboard.

Who may start/stop which server is enforced by JupyterHub's own scopes.

To promote a user to admin:
  JupyterHub Admin Panel → Users → ☑ Make Admin
  (or add their name to ``admin_users`` in ``hub_settings.json`` and restart the hub)
"""

from __future__ import annotations

from typing import Literal

# ──────────────────────────────────────────────────────────────────────────────
# Role type alias
# ──────────────────────────────────────────────────────────────────────────────

Role = Literal["admin", "user"]


# ──────────────────────────────────────────────────────────────────────────────
# RBACManager
# ──────────────────────────────────────────────────────────────────────────────


class RBACManager:
    """
    Lightweight RBAC helper for JupyterHub-Pilot.

    All methods accept a *user* object that exposes:
      - ``user.name``  (str)
      - ``user.admin`` (bool)  — set by JupyterHub from ``admin_users`` config.

    Example::

        rbac = RBACManager()

        # Promotion is handled entirely via the JupyterHub Admin Panel.
        rbac.is_admin(user)                           # True / False
        rbac.get_role(user)                           # "admin" | "user"
    """

    # ── Role resolution ───────────────────────────────────────────────────────

    def is_admin(self, user: object) -> bool:
        """
        Return True if *user* holds admin rights in JupyterHub.

        Reads the native ``user.admin`` boolean that JupyterHub sets from
        ``admin_users`` configuration.  Defaults to ``False`` if the attribute
        is absent (e.g., in unit-test mocks that do not set it explicitly).

        Args:
            user: A JupyterHub User object (or any object with an ``admin``
                  attribute).

        Returns:
            ``True`` if the user is an admin; ``False`` otherwise.
        """
        return bool(getattr(user, "admin", False))

    def get_role(self, user: object) -> Role:
        """
        Return the string role label for *user*.

        Returns:
            ``"admin"`` or ``"user"``.
        """
        return "admin" if self.is_admin(user) else "user"
