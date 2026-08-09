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
  admin  → Can stop/inspect any user's session.  Set via JupyterHub Admin Panel.
  user   → Can only start/stop/poll their own session.

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
        rbac.assert_can_act_on(actor, "other_user")   # raises PermissionError
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

    # ── Access enforcement ────────────────────────────────────────────────────

    def assert_can_act_on(self, actor: object, target_username: str) -> None:
        """
        Raise ``PermissionError`` if *actor* is not allowed to perform an
        action that affects *target_username*'s session.

        Rules:
          - Any user can act on their **own** session.
          - An ``admin`` can act on **any** session.
          - A plain ``user`` acting on **another** user's session is denied.

        Args:
            actor:           The JupyterHub User object initiating the action.
            target_username: The username whose session is being acted upon.

        Raises:
            PermissionError: If *actor* is a plain user and *target_username*
                             differs from ``actor.name``.

        Example::

            rbac = RBACManager()

            # Alice (admin) can stop Bob's server:
            rbac.assert_can_act_on(alice_user, "bob")   # no exception

            # Charlie (user) cannot stop Bob's server:
            rbac.assert_can_act_on(charlie_user, "bob") # raises PermissionError
        """
        actor_name: str = getattr(actor, "name", "")

        if actor_name == target_username:
            return  # Always allowed — acting on own session.

        if self.is_admin(actor):
            return  # Admins may act on any session.

        raise PermissionError(
            f"Access denied: user '{actor_name}' is not an admin "
            f"and cannot act on session owned by '{target_username}'. "
            f"To grant admin rights, use the JupyterHub Admin Panel."
        )
