import json
import os
import sys
from unittest.mock import MagicMock, mock_open, patch

import pytest

# Add root folder to sys.path so we can import spawner
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import spawner
from spawner import CustomSpawner

# Configure global hub_settings inside the spawner module for testing
spawner.hub_settings = {
    "hub_api_url": "http://10.0.0.1:8081/hub/api",
    "mapping_file": "user_mapping.json",
}


@pytest.fixture
def mock_spawner_instance():
    import os
    import tempfile

    import spawner as spawner_module
    from jupyterhub_pilot.session_store import SessionStore

    instance = CustomSpawner()
    instance.user.name = "test_user"
    instance.user.admin = False

    # Mock user group named 'team_alpha'
    group_mock = MagicMock()
    group_mock.name = "team_alpha"
    instance.user.groups = [group_mock]

    # Mock spawner log
    instance.log = MagicMock()

    # Use a real tmp SQLite store so start()/stop()/poll() don't crash
    tmp_db = os.path.join(tempfile.mkdtemp(), "test.db")
    tmp_store = SessionStore(tmp_db)
    tmp_store.init_db()
    tmp_store.set_mapping("team_alpha", "192.168.1.100", "/path/to/key.pem")
    spawner_module._store = tmp_store

    # Stub _get_server_info (replaces the old _load_mapping stub)
    instance._get_server_info = MagicMock(
        return_value={
            "server_ip": "192.168.1.100",
            "server_ssh_key": "/path/to/key.pem",
            "ssh_user": None,
        }
    )

    return instance


def test_load_mapping():
    spawner_inst = CustomSpawner()
    fake_mapping = {"team_alpha": {"server_ip": "1.1.1.1", "server_ssh_key": "key"}}

    with patch("builtins.open", mock_open(read_data=json.dumps(fake_mapping))):
        mapping = spawner_inst._load_mapping_fallback()
        assert mapping == fake_mapping


def test_load_mapping_failure():
    spawner_inst = CustomSpawner()
    spawner_inst.log = MagicMock()

    with patch("builtins.open", side_effect=Exception("Read Error")):
        with pytest.raises(Exception):
            spawner_inst._load_mapping_fallback()
        assert spawner_inst.log.error.called


@pytest.mark.anyio
async def test_start_no_groups(mock_spawner_instance):
    mock_spawner_instance.user.groups = []
    with pytest.raises(Exception, match="has no groups assigned"):
        await mock_spawner_instance.start()


@pytest.mark.anyio
async def test_start_group_not_mapped(mock_spawner_instance):
    mock_spawner_instance.user.groups[0].name = "unknown_group"
    # _get_server_info now raises RuntimeError when group not found
    mock_spawner_instance._get_server_info = MagicMock(
        side_effect=RuntimeError("unknown_group not mapped to a server")
    )
    with pytest.raises(Exception, match="not mapped to a server"):
        await mock_spawner_instance.start()


@pytest.mark.anyio
async def test_start_success(mock_spawner_instance):
    mock_ssh = MagicMock()

    # Mock exec_command return values
    # Command 1: get_port.py -> returns port
    stdout_port = MagicMock()
    stdout_port.readline.return_value = "8888\n"

    # Command 2: netstat -> empty (port not in use)
    stdout_netstat = MagicMock()
    stdout_netstat.read.return_value = b""

    # Command 3: setup OOM script -> wait for exit status
    stdout_oom = MagicMock()
    stdout_oom.channel.recv_exit_status.return_value = 0

    # Command 4: spawn singleuser server
    stdout_spawn = MagicMock()

    def exec_cmd_mock(cmd, *args, **kwargs):
        if "get_port.py" in cmd:
            return None, stdout_port, None
        elif "netstat" in cmd:
            return None, stdout_netstat, None
        elif "99-admin-oom-enforcement.py" in cmd:
            return None, stdout_oom, None
        else:
            return None, stdout_spawn, None

    mock_ssh.exec_command.side_effect = exec_cmd_mock

    with patch.object(
        mock_spawner_instance, "_open_ssh", return_value=mock_ssh
    ), patch.dict(spawner.hub_settings, {"hub_api_url": "http://hub:8081/hub/api"}):
        ip, port = await mock_spawner_instance.start()

        assert ip == "192.168.1.100"
        assert port == 8888
        assert mock_spawner_instance.ip == "192.168.1.100"
        assert mock_spawner_instance.port == 8888

        # Verify SSH calls (get_port, netstat, OOM, spawn = 4)
        assert mock_ssh.exec_command.call_count == 4
        mock_ssh.close.assert_called_once()


@pytest.mark.anyio
async def test_start_port_in_use_cleanup(mock_spawner_instance):
    mock_ssh = MagicMock()

    stdout_port = MagicMock()
    stdout_port.readline.return_value = "8888\n"

    # netstat returns listening port (port is in use)
    stdout_netstat = MagicMock()
    stdout_netstat.read.return_value = b"tcp 0 0 0.0.0.0:8888"

    stdout_oom = MagicMock()
    stdout_oom.channel.recv_exit_status.return_value = 0

    stdout_spawn = MagicMock()

    executed_commands = []

    def exec_cmd_mock(cmd, *args, **kwargs):
        executed_commands.append(cmd)
        if "get_port.py" in cmd:
            return None, stdout_port, None
        elif "netstat" in cmd:
            return None, stdout_netstat, None
        elif "99-admin-oom-enforcement.py" in cmd:
            return None, stdout_oom, None
        else:
            return None, stdout_spawn, None

    mock_ssh.exec_command.side_effect = exec_cmd_mock

    with patch.object(
        mock_spawner_instance, "_open_ssh", return_value=mock_ssh
    ), patch.dict(spawner.hub_settings, {"hub_api_url": "http://hub:8081/hub/api"}):
        ip, port = await mock_spawner_instance.start()

        # Check if fuser command was executed to kill stale process
        fuser_run = any("fuser -k 8888/tcp" in c for c in executed_commands)
        assert fuser_run
        assert mock_spawner_instance.log.warning.called


@pytest.mark.anyio
async def test_start_oom_warning(mock_spawner_instance):
    mock_ssh = MagicMock()

    stdout_port = MagicMock()
    stdout_port.readline.return_value = "8888\n"

    stdout_netstat = MagicMock()
    stdout_netstat.read.return_value = b""

    # OOM setup fails (returns non-zero exit status)
    stdout_oom = MagicMock()
    stdout_oom.channel.recv_exit_status.return_value = 1

    stdout_spawn = MagicMock()

    def exec_cmd_mock(cmd, *args, **kwargs):
        if "get_port.py" in cmd:
            return None, stdout_port, None
        elif "netstat" in cmd:
            return None, stdout_netstat, None
        elif "mkdir" in cmd or "oom" in cmd.lower():
            return None, stdout_oom, None
        else:
            return None, stdout_spawn, None

    mock_ssh.exec_command.side_effect = exec_cmd_mock

    with patch.object(
        mock_spawner_instance, "_open_ssh", return_value=mock_ssh
    ), patch.dict(spawner.hub_settings, {"hub_api_url": "http://hub:8081/hub/api"}):
        await mock_spawner_instance.start()

        # Verify it logged a warning for OOM script failure but did not raise exception
        warning_logged = any(
            "Failed to create OOM script" in args[0]
            for args, kwargs in mock_spawner_instance.log.warning.call_args_list
        )
        assert warning_logged


@pytest.mark.anyio
async def test_stop(mock_spawner_instance):
    import spawner as spawner_module

    mock_ssh = MagicMock()
    mock_spawner_instance._pid_file = "/tmp/jupyterhub-test_user.pid"
    mock_spawner_instance.ip = "192.168.1.100"

    # Pre-populate SQLite so stop() can find the VM
    spawner_module._store.set_session(
        "test_user", vm_ip="192.168.1.100", group_name="team_alpha", status="running"
    )

    stop_stdout = MagicMock()
    stop_stdout.channel.recv_exit_status.return_value = 0
    mock_ssh.exec_command.return_value = (None, stop_stdout, None)

    with patch.object(mock_spawner_instance, "_open_ssh", return_value=mock_ssh):
        await mock_spawner_instance.stop()

        # Verify cleanup command execution
        mock_ssh.exec_command.assert_called_once()
        cmd = mock_ssh.exec_command.call_args[0][0]
        assert "kill -15" in cmd
        assert "/tmp/jupyterhub-test_user.pid" in cmd
        mock_ssh.close.assert_called_once()


@pytest.mark.anyio
async def test_poll_running(mock_spawner_instance):
    import spawner as spawner_module

    mock_ssh = MagicMock()
    mock_spawner_instance._pid_file = "/tmp/jupyterhub-test_user.pid"

    # Pre-populate SQLite so poll() can find the VM and PID
    spawner_module._store.set_session(
        "test_user",
        vm_ip="192.168.1.100",
        pid="42",
        group_name="team_alpha",
        status="running",
    )

    # ps command succeeds (returns "0")
    stdout_ps = MagicMock()
    stdout_ps.read.return_value = b"0\n"
    mock_ssh.exec_command.return_value = (None, stdout_ps, None)

    with patch.object(mock_spawner_instance, "_open_ssh", return_value=mock_ssh):
        status = await mock_spawner_instance.poll()
        assert status is None  # None indicates the server is running
        assert "ps -p" in mock_ssh.exec_command.call_args[0][0]


@pytest.mark.anyio
async def test_poll_stopped(mock_spawner_instance):
    import spawner as spawner_module

    mock_ssh = MagicMock()
    mock_spawner_instance._pid_file = "/tmp/jupyterhub-test_user.pid"

    spawner_module._store.set_session(
        "test_user",
        vm_ip="192.168.1.100",
        pid="42",
        group_name="team_alpha",
        status="running",
    )

    # ps command fails (returns "1")
    stdout_ps = MagicMock()
    stdout_ps.read.return_value = b"1\n"
    mock_ssh.exec_command.return_value = (None, stdout_ps, None)

    with patch.object(mock_spawner_instance, "_open_ssh", return_value=mock_ssh):
        status = await mock_spawner_instance.poll()
        assert status == 0  # 0 indicates the server is stopped


@pytest.mark.anyio
async def test_poll_exception(mock_spawner_instance):
    import spawner as spawner_module

    # Pre-populate so poll() proceeds past the "no session" early return
    spawner_module._store.set_session(
        "test_user", vm_ip="192.168.1.100", group_name="team_alpha", status="running"
    )
    # SSH or execution raises exception
    with patch.object(
        mock_spawner_instance,
        "_open_ssh",
        side_effect=Exception("SSH Connection Refused"),
    ):
        status = await mock_spawner_instance.poll()
        assert status == 0  # Should assume stopped on exception
