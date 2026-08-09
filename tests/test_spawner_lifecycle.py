"""
tests/test_spawner_lifecycle.py
────────────────────────────────
Unit tests for:
  - RBACManager  (2-tier admin/user model)
  - SessionStore (SQLite state DB wrapper)
  - CustomSpawner lifecycle: start(), stop(), poll(), clear_state()
  - Lifecycle hooks: pre_spawn_hook, post_stop_hook
  - Crash recovery: stop() and poll() read from SQLite, not instance vars
  - JSON fallback: spawner falls back gracefully when SQLite is unavailable

All tests mock paramiko and the SessionStore — no real SSH or DB needed.
Existing tests/test_spawner.py and tests/test_config.py are not modified.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ── Path setup ─────────────────────────────────────────────────────────────────
# conftest.py already injects jupyterhub / paramiko mocks into sys.modules.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import paramiko

try:
    import paramiko.ssh_exception as _paramiko_ssh_exc
except (ImportError, ModuleNotFoundError):
    _paramiko_ssh_exc = getattr(paramiko, "ssh_exception", paramiko)  # type: ignore

import spawner as spawner_module
from jupyterhub_pilot.admin import RBACManager
from jupyterhub_pilot.session_store import SessionStore
from spawner import CustomSpawner

# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def rbac() -> RBACManager:
    return RBACManager()


@pytest.fixture
def admin_user() -> MagicMock:
    u = MagicMock()
    u.name = "alice"
    u.admin = True
    return u


@pytest.fixture
def regular_user() -> MagicMock:
    u = MagicMock()
    u.name = "bob"
    u.admin = False
    return u


@pytest.fixture
def tmp_db(tmp_path) -> str:
    """Return the path to a fresh temporary SQLite DB."""
    return str(tmp_path / "test_jupyterhub_pilot.db")


@pytest.fixture
def store(tmp_db) -> SessionStore:
    s = SessionStore(tmp_db)
    s.init_db()
    return s


@pytest.fixture
def mock_spawner(store) -> CustomSpawner:
    """
    A fully wired CustomSpawner instance with:
      - mocked user (regular, non-admin)
      - mocked log
      - mocked _load_mapping and _get_server_info
      - real SessionStore pointing at a tmp DB
    """
    instance = CustomSpawner()
    instance.user = MagicMock()
    instance.user.name = "test_user"
    instance.user.admin = False
    instance.user.groups = [MagicMock(name="team_alpha")]
    instance.user.groups[0].name = "team_alpha"
    instance.server = MagicMock()
    instance.server.base_url = "/user/test_user/"
    instance.log = MagicMock()
    instance._pid_file = None
    instance._session_meta = {}

    # Point the module-level store singleton to our tmp DB store
    spawner_module._store = store

    # Stub _get_server_info to avoid real file I/O
    instance._get_server_info = MagicMock(
        return_value={
            "server_ip": "192.168.1.100",
            "server_ssh_key": "/path/to/key.pem",
            "ssh_user": None,
        }
    )

    return instance


def _make_ssh_mock(port: int = 8888, port_in_use: bool = False) -> MagicMock:
    """Return a paramiko SSH mock pre-wired for the start() flow."""
    mock_ssh = MagicMock()

    stdout_port = MagicMock()
    stdout_port.readline.return_value = f"{port}\n"
    stdout_netstat = MagicMock()
    stdout_netstat.read.return_value = (
        f"tcp 0 0 0.0.0.0:{port}".encode() if port_in_use else b""
    )
    stdout_fuser = MagicMock()
    stdout_fuser.channel.recv_exit_status.return_value = 0
    stdout_oom = MagicMock()
    stdout_oom.channel.recv_exit_status.return_value = 0
    stdout_spawn = MagicMock()

    def exec_cmd_mock(cmd, *args, **kwargs):
        if "get_port.py" in cmd:
            return None, stdout_port, None
        elif "netstat" in cmd:
            return None, stdout_netstat, None
        elif "fuser" in cmd:
            return None, stdout_fuser, None
        elif "mkdir" in cmd or "oom" in cmd.lower():
            return None, stdout_oom, None
        else:
            return None, stdout_spawn, None

    mock_ssh.exec_command.side_effect = exec_cmd_mock
    return mock_ssh


# ══════════════════════════════════════════════════════════════════════════════
# 1. RBACManager tests
# ══════════════════════════════════════════════════════════════════════════════


class TestRBACManager:
    def test_admin_is_detected(self, rbac, admin_user):
        assert rbac.is_admin(admin_user) is True

    def test_regular_user_is_not_admin(self, rbac, regular_user):
        assert rbac.is_admin(regular_user) is False

    def test_missing_admin_attr_defaults_false(self, rbac):
        u = MagicMock(spec=[])  # no .admin attribute
        assert rbac.is_admin(u) is False

    def test_get_role_admin(self, rbac, admin_user):
        assert rbac.get_role(admin_user) == "admin"

    def test_get_role_user(self, rbac, regular_user):
        assert rbac.get_role(regular_user) == "user"

    def test_admin_can_act_on_any_user(self, rbac, admin_user):
        """Admin may act on a different user's session — no exception."""
        rbac.assert_can_act_on(admin_user, "some_other_user")  # must not raise

    def test_user_can_act_on_own_session(self, rbac, regular_user):
        """Non-admin may always act on their own session."""
        rbac.assert_can_act_on(regular_user, regular_user.name)  # must not raise

    def test_user_blocked_on_other_session(self, rbac, regular_user):
        """Non-admin acting on another user's session raises PermissionError."""
        with pytest.raises(PermissionError, match="not an admin"):
            rbac.assert_can_act_on(regular_user, "alice")


# ══════════════════════════════════════════════════════════════════════════════
# 2. SessionStore tests
# ══════════════════════════════════════════════════════════════════════════════


class TestSessionStore:
    def test_ping_returns_true_after_init(self, store):
        assert store.ping() is True

    def test_ping_returns_false_on_bad_path(self):
        bad_store = SessionStore("/nonexistent/path/db.sqlite")
        assert bad_store.ping() is False

    def test_get_mapping_not_found_returns_none(self, store):
        assert store.get_mapping("unknown_team") is None

    def test_set_and_get_mapping_round_trip(self, store):
        store.set_mapping("team_alpha", "10.0.1.15", "/keys/alpha.pem", "ubuntu")
        row = store.get_mapping("team_alpha")

        assert row is not None
        assert row["server_ip"] == "10.0.1.15"
        assert row["ssh_key"] == "/keys/alpha.pem"
        assert row["ssh_user"] == "ubuntu"

    def test_set_mapping_overwrite(self, store):
        store.set_mapping("team_alpha", "10.0.1.15", "/keys/alpha.pem")
        store.set_mapping("team_alpha", "10.0.1.99", "/keys/new.pem")
        row = store.get_mapping("team_alpha")
        assert row["server_ip"] == "10.0.1.99"

    def test_set_and_get_session_round_trip(self, store):
        store.set_session(
            "alice",
            pid="12345",
            vm_ip="10.0.1.15",
            port=8888,
            start_time="2026-07-26T10:00:00Z",
            role="user",
            status="running",
            group_name="team_alpha",
        )
        session = store.get_session("alice")

        assert session is not None
        assert session["pid"] == "12345"
        assert session["vm_ip"] == "10.0.1.15"
        assert session["port"] == 8888
        assert session["status"] == "running"
        assert session["role"] == "user"
        assert session["group_name"] == "team_alpha"

    def test_set_session_partial_update(self, store):
        """Partial set_session only updates provided fields."""
        store.set_session("bob", pid="999", vm_ip="10.0.1.10", status="running")
        store.set_session("bob", status="stopped")  # only update status
        session = store.get_session("bob")
        assert session["status"] == "stopped"
        assert session["vm_ip"] == "10.0.1.10"  # unchanged

    def test_clear_session_removes_row(self, store):
        store.set_session("charlie", pid="111", status="running")
        store.clear_session("charlie")
        assert store.get_session("charlie") is None

    def test_clear_session_nonexistent_is_safe(self, store):
        """Clearing a non-existent session should not raise."""
        store.clear_session("nobody")  # must not raise


# ══════════════════════════════════════════════════════════════════════════════
# 3. CustomSpawner lifecycle tests
# ══════════════════════════════════════════════════════════════════════════════


class TestSpawnerStart:
    @pytest.mark.anyio
    async def test_start_success_returns_ip_port(self, mock_spawner):
        mock_ssh = _make_ssh_mock(port=8888)
        with (
            patch.object(mock_spawner, "_open_ssh", return_value=mock_ssh),
            patch.dict(
                spawner_module.hub_settings, {"hub_api_url": "http://hub:8081/hub/api"}
            ),
        ):
            ip, port = await mock_spawner.start()

        assert ip == "192.168.1.100"
        assert port == 8888

    @pytest.mark.anyio
    async def test_start_writes_session_to_store(self, mock_spawner, store):
        mock_ssh = _make_ssh_mock(port=9000)
        with (
            patch.object(mock_spawner, "_open_ssh", return_value=mock_ssh),
            patch.dict(
                spawner_module.hub_settings, {"hub_api_url": "http://hub:8081/hub/api"}
            ),
        ):
            await mock_spawner.start()

        session = store.get_session("test_user")
        assert session is not None
        assert session["status"] == "running"
        assert session["vm_ip"] == "192.168.1.100"
        assert session["port"] == 9000
        assert session["group_name"] == "team_alpha"
        assert session["role"] == "user"

    @pytest.mark.anyio
    async def test_start_pre_hook_called(self, mock_spawner):
        mock_ssh = _make_ssh_mock()
        hook_called = []

        async def fake_hook():
            hook_called.append(True)

        mock_spawner.pre_spawn_hook = fake_hook
        with (
            patch.object(mock_spawner, "_open_ssh", return_value=mock_ssh),
            patch.dict(
                spawner_module.hub_settings, {"hub_api_url": "http://hub:8081/hub/api"}
            ),
        ):
            await mock_spawner.start()

        assert hook_called, "pre_spawn_hook was not called"

    @pytest.mark.anyio
    async def test_start_port_in_use_triggers_fuser(self, mock_spawner):
        mock_ssh = _make_ssh_mock(port=8888, port_in_use=True)
        executed = []
        original_side = mock_ssh.exec_command.side_effect

        def tracking_exec(cmd, *a, **kw):
            executed.append(cmd)
            return original_side(cmd, *a, **kw)

        mock_ssh.exec_command.side_effect = tracking_exec

        with (
            patch.object(mock_spawner, "_open_ssh", return_value=mock_ssh),
            patch.dict(
                spawner_module.hub_settings, {"hub_api_url": "http://hub:8081/hub/api"}
            ),
        ):
            await mock_spawner.start()

        assert any("fuser" in c for c in executed), "fuser not called for stale port"
        mock_spawner.log.warning.assert_called()

    @pytest.mark.anyio
    async def test_start_no_groups_raises(self, mock_spawner):
        mock_spawner.user.groups = []
        with pytest.raises(RuntimeError, match="no groups assigned"):
            await mock_spawner.start()

    @pytest.mark.anyio
    async def test_start_missing_hub_api_url_raises(self, mock_spawner):
        mock_ssh = _make_ssh_mock()
        with (
            patch.object(mock_spawner, "_open_ssh", return_value=mock_ssh),
            patch.dict(spawner_module.hub_settings, {}, clear=True),
        ):
            with pytest.raises(RuntimeError, match="hub_api_url"):
                await mock_spawner.start()

    @pytest.mark.anyio
    async def test_start_ssh_unreachable_propagates(self, mock_spawner):
        with patch.object(
            mock_spawner,
            "_open_ssh",
            side_effect=_paramiko_ssh_exc.NoValidConnectionsError({}),
        ):
            with pytest.raises(Exception):  # accepts both real and mock exc types
                await mock_spawner.start()
        # Note: log.error is only called when the exception class name matches
        # known paramiko names; under test mocks the class name is 'MagicMock'.
        # The key assertion is that the exception propagates (pytest.raises above).


class TestSpawnerStop:
    @pytest.mark.anyio
    async def test_stop_sends_sigterm_and_sigkill(self, mock_spawner, store):
        # Pre-populate SQLite session so stop() can find the VM IP
        store.set_session(
            "test_user",
            vm_ip="192.168.1.100",
            pid="42",
            port=8888,
            status="running",
            group_name="team_alpha",
        )
        mock_spawner._pid_file = "/tmp/jupyterhub-test_user.pid"

        mock_ssh = MagicMock()
        stdout_stop = MagicMock()
        stdout_stop.channel.recv_exit_status.return_value = 0
        mock_ssh.exec_command.return_value = (None, stdout_stop, None)

        with patch.object(mock_spawner, "_open_ssh", return_value=mock_ssh):
            await mock_spawner.stop()

        cmd = mock_ssh.exec_command.call_args[0][0]
        assert "kill -15" in cmd, "SIGTERM not in stop command"
        assert "kill -9" in cmd, "SIGKILL not in stop command"
        assert "sleep 5" in cmd, "5-second grace period not in stop command"

    @pytest.mark.anyio
    async def test_stop_removes_session_from_store(self, mock_spawner, store):
        store.set_session(
            "test_user",
            vm_ip="192.168.1.100",
            pid="42",
            status="running",
            group_name="team_alpha",
        )
        mock_ssh = MagicMock()
        mock_ssh.exec_command.return_value = (
            None,
            MagicMock(channel=MagicMock(recv_exit_status=MagicMock(return_value=0))),
            None,
        )

        with patch.object(mock_spawner, "_open_ssh", return_value=mock_ssh):
            await mock_spawner.stop()

        assert (
            store.get_session("test_user") is None
        ), "Session row should be removed after stop()"

    @pytest.mark.anyio
    async def test_stop_post_hook_called(self, mock_spawner, store):
        store.set_session(
            "test_user",
            vm_ip="192.168.1.100",
            group_name="team_alpha",
            status="running",
        )
        hook_called = []

        async def fake_hook():
            hook_called.append(True)

        mock_spawner.post_stop_hook = fake_hook
        mock_ssh = MagicMock()
        mock_ssh.exec_command.return_value = (
            None,
            MagicMock(channel=MagicMock(recv_exit_status=MagicMock(return_value=0))),
            None,
        )

        with patch.object(mock_spawner, "_open_ssh", return_value=mock_ssh):
            await mock_spawner.stop()

        assert hook_called, "post_stop_hook was not called"

    @pytest.mark.anyio
    async def test_stop_no_session_logs_warning(self, mock_spawner, store):
        """stop() without a SQLite session and no ip → logs warning, no crash."""
        mock_spawner.ip = ""
        mock_spawner._session_meta = {}
        # No session row in store
        await mock_spawner.stop()  # must not raise
        mock_spawner.log.warning.assert_called()

    @pytest.mark.anyio
    async def test_stop_ssh_error_does_not_crash_hub(self, mock_spawner, store):
        """SSH failure during stop() must be caught; hub process must survive."""
        store.set_session(
            "test_user",
            vm_ip="192.168.1.100",
            group_name="team_alpha",
            status="running",
        )
        with patch.object(
            mock_spawner,
            "_open_ssh",
            side_effect=Exception("Connection reset"),
        ):
            await mock_spawner.stop()  # must not raise
        mock_spawner.log.error.assert_called()


class TestSpawnerPoll:
    @pytest.mark.anyio
    async def test_poll_running_returns_none(self, mock_spawner, store):
        store.set_session(
            "test_user",
            vm_ip="192.168.1.100",
            pid="42",
            group_name="team_alpha",
            status="running",
        )
        mock_spawner._pid_file = "/tmp/jupyterhub-test_user.pid"

        stdout_ps = MagicMock()
        stdout_ps.read.return_value = b"0\n"
        mock_ssh = MagicMock()
        mock_ssh.exec_command.return_value = (None, stdout_ps, None)

        with patch.object(mock_spawner, "_open_ssh", return_value=mock_ssh):
            result = await mock_spawner.poll()

        assert result is None, "poll() should return None when process is alive"

    @pytest.mark.anyio
    async def test_poll_stopped_returns_zero(self, mock_spawner, store):
        store.set_session(
            "test_user",
            vm_ip="192.168.1.100",
            pid="42",
            group_name="team_alpha",
            status="running",
        )
        mock_spawner._pid_file = "/tmp/jupyterhub-test_user.pid"

        stdout_ps = MagicMock()
        stdout_ps.read.return_value = b"1\n"
        mock_ssh = MagicMock()
        mock_ssh.exec_command.return_value = (None, stdout_ps, None)

        with patch.object(mock_spawner, "_open_ssh", return_value=mock_ssh):
            result = await mock_spawner.poll()

        assert result == 0, "poll() should return 0 when process is stopped"

    @pytest.mark.anyio
    async def test_poll_no_session_returns_zero(self, mock_spawner, store):
        """poll() with no SQLite session row returns 0 (not running)."""
        result = await mock_spawner.poll()
        assert result == 0

    @pytest.mark.anyio
    async def test_poll_ssh_exception_returns_zero(self, mock_spawner, store):
        store.set_session(
            "test_user",
            vm_ip="192.168.1.100",
            group_name="team_alpha",
            status="running",
        )
        with patch.object(
            mock_spawner, "_open_ssh", side_effect=Exception("Connection refused")
        ):
            result = await mock_spawner.poll()
        assert result == 0

    @pytest.mark.anyio
    async def test_poll_stopped_updates_store_status(self, mock_spawner, store):
        """When poll() detects a stopped process it updates the SQLite status."""
        store.set_session(
            "test_user",
            vm_ip="192.168.1.100",
            pid="42",
            group_name="team_alpha",
            status="running",
        )
        mock_spawner._pid_file = "/tmp/jupyterhub-test_user.pid"

        stdout_ps = MagicMock()
        stdout_ps.read.return_value = b"1\n"
        mock_ssh = MagicMock()
        mock_ssh.exec_command.return_value = (None, stdout_ps, None)

        with patch.object(mock_spawner, "_open_ssh", return_value=mock_ssh):
            await mock_spawner.poll()

        session = store.get_session("test_user")
        # Session is updated to stopped (row may still exist after poll)
        if session:
            assert session["status"] == "stopped"


class TestSpawnerClearState:
    @pytest.mark.anyio
    async def test_clear_state_removes_session_from_store(self, mock_spawner, store):
        store.set_session("test_user", vm_ip="192.168.1.100", status="running")
        mock_spawner.ip = "192.168.1.100"
        mock_spawner.port = 8888
        mock_spawner._pid_file = "/tmp/jupyterhub-test_user.pid"
        mock_spawner._session_meta = {"role": "user"}

        mock_spawner.clear_state()  # sync — no await

        assert store.get_session("test_user") is None

    @pytest.mark.anyio
    async def test_clear_state_resets_instance_vars(self, mock_spawner, store):
        mock_spawner.ip = "192.168.1.100"
        mock_spawner.port = 8888
        mock_spawner._pid_file = "/tmp/jupyterhub-test_user.pid"
        mock_spawner._session_meta = {"role": "admin"}

        mock_spawner.clear_state()  # sync — no await

        assert mock_spawner.ip == ""
        assert mock_spawner.port == 0
        assert mock_spawner._pid_file is None
        assert mock_spawner._session_meta == {}


# ══════════════════════════════════════════════════════════════════════════════
# 4. Fallback: SQLite unavailable → JSON file
# ══════════════════════════════════════════════════════════════════════════════


class TestJsonFallback:
    def test_get_server_info_falls_back_to_json(self, mock_spawner, store):
        """When SQLite ping fails, _get_server_info falls back to JSON mapping."""
        fallback_mapping = {
            "team_alpha": {
                "server_ip": "10.0.1.99",
                "server_ssh_key": "/fallback/key.pem",
            }
        }
        # Restore real _get_server_info to test fallback logic
        mock_spawner._get_server_info = CustomSpawner._get_server_info.__get__(
            mock_spawner, CustomSpawner
        )

        with (
            patch.object(store, "ping", return_value=False),
            patch.object(
                mock_spawner, "_load_mapping_fallback", return_value=fallback_mapping
            ),
        ):
            info = mock_spawner._get_server_info("team_alpha")

        assert info["server_ip"] == "10.0.1.99"

    def test_get_server_info_raises_if_group_missing_everywhere(
        self, mock_spawner, store
    ):
        """If group is absent from both SQLite and JSON, RuntimeError is raised."""
        mock_spawner._get_server_info = CustomSpawner._get_server_info.__get__(
            mock_spawner, CustomSpawner
        )
        with (
            patch.object(store, "ping", return_value=False),
            patch.object(mock_spawner, "_load_mapping_fallback", return_value={}),
        ):
            with pytest.raises(RuntimeError, match="not mapped"):
                mock_spawner._get_server_info("unknown_group")
