import json
import os
import sys
from unittest.mock import MagicMock, mock_open, patch

import pytest

# Add root folder to sys.path so we can import spawner
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from jupyterhub_pilot import spawner
from jupyterhub_pilot.spawner import CustomSpawner

# Configure global hub_settings inside the spawner module for testing
spawner.hub_settings = {
    "hub_api_url": "http://10.0.0.1:8081/hub/api",
    "mapping_file": "user_mapping.json",
}


@pytest.fixture
def mock_spawner_instance():
    import os
    import tempfile

    from jupyterhub_pilot import spawner as spawner_module
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
    with pytest.raises(spawner.web.HTTPError, match="not in a team"):
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
    # Command 1: GET_PORT_CMD -> returns port
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
    stdout_spawn.channel.recv_exit_status.return_value = 0

    def exec_cmd_mock(cmd, *args, **kwargs):
        if "getsockname" in cmd:  # GET_PORT_CMD
            return None, stdout_port, None
        elif "netstat" in cmd:
            return None, stdout_netstat, None
        elif "99-admin-oom-enforcement.py" in cmd:
            return None, stdout_oom, None
        else:
            return MagicMock(), stdout_spawn, None

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
    stdout_spawn.channel.recv_exit_status.return_value = 0

    executed_commands = []

    def exec_cmd_mock(cmd, *args, **kwargs):
        executed_commands.append(cmd)
        if "getsockname" in cmd:  # GET_PORT_CMD
            return None, stdout_port, None
        elif "netstat" in cmd:
            return None, stdout_netstat, None
        elif "99-admin-oom-enforcement.py" in cmd:
            return None, stdout_oom, None
        else:
            return MagicMock(), stdout_spawn, None

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
    stdout_spawn.channel.recv_exit_status.return_value = 0

    def exec_cmd_mock(cmd, *args, **kwargs):
        if "getsockname" in cmd:  # GET_PORT_CMD
            return None, stdout_port, None
        elif "netstat" in cmd:
            return None, stdout_netstat, None
        elif "mkdir" in cmd or "oom" in cmd.lower():
            return None, stdout_oom, None
        else:
            return MagicMock(), stdout_spawn, None

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
    from jupyterhub_pilot import spawner as spawner_module

    mock_ssh = MagicMock()
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
        assert "~/.jupyterhub-pilot-test_user.pid" in cmd  # per user: ssh_user may be shared
        mock_ssh.close.assert_called_once()


@pytest.mark.anyio
async def test_poll_running(mock_spawner_instance):
    from jupyterhub_pilot import spawner as spawner_module

    mock_ssh = MagicMock()

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
    from jupyterhub_pilot import spawner as spawner_module

    mock_ssh = MagicMock()

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
    from jupyterhub_pilot import spawner as spawner_module

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
        # Unknown state must not make the Hub forget a live server
        assert status is None


@pytest.mark.anyio
async def test_start_secrets_go_through_stdin_not_argv(mock_spawner_instance):
    mock_spawner_instance.get_env = lambda: {"JUPYTERHUB_API_TOKEN": "s3cret"}
    mock_ssh = MagicMock()
    stdin = MagicMock()
    stdout = MagicMock()
    stdout.readline.return_value = "8888\n"
    stdout.read.return_value = b""
    stdout.channel.recv_exit_status.return_value = 0
    mock_ssh.exec_command.side_effect = lambda cmd, *a, **k: (stdin, stdout, None)

    with patch.object(
        mock_spawner_instance, "_open_ssh", return_value=mock_ssh
    ), patch.dict(spawner.hub_settings, {"hub_api_url": "http://hub:8081/hub/api"}):
        await mock_spawner_instance.start()

    commands = " ".join(c.args[0] for c in mock_ssh.exec_command.call_args_list)
    assert "s3cret" not in commands
    script = stdin.write.call_args[0][0]
    assert "export JUPYTERHUB_API_TOKEN=s3cret" in script
    assert "/tmp/" not in script
    assert "echo $! > ~/.jupyterhub-pilot-test_user.pid" in script
    assert 'kill "$OLD"' in script  # leftover server from a failed stop is ended


@pytest.mark.parametrize(
    "name, expected",
    [("alice@corp.com", "alice"), ("Bob", "bob"), ("a_b-1", "a_b-1"),
     ("alice.smith", "alice.smith")],  # Google strips the domain: dots are common
)
def test_os_username_maps_email_local_part(name, expected):
    inst = CustomSpawner()
    inst.user.name = name
    assert inst._os_username() == expected


@pytest.mark.parametrize("name", ['x";rm -rf ~;"', "a b", "$(id)@corp.com", "1abc", "alice\n"])
def test_os_username_rejects_shell_unsafe_names(name):
    inst = CustomSpawner()
    inst.user.name = name
    with pytest.raises(RuntimeError, match="cannot be mapped"):
        inst._os_username()


JIT_SERVER = {"admin_ssh_user": "ubuntu", "server_ip": "10.0.0.1", "server_ssh_key": "/k"}


def _jit_ssh():
    ssh = MagicMock()
    stdout = MagicMock()
    stdout.channel.recv_exit_status.return_value = 0
    ssh.exec_command.return_value = (None, stdout, MagicMock())
    return ssh


def test_jit_provisioning_script_creates_dotted_user_without_pip():
    inst = CustomSpawner()
    inst.user.name = "alice.smith"
    ssh = _jit_ssh()
    with patch.object(spawner.paramiko, "SSHClient", return_value=ssh):
        inst._provision_user_jit(JIT_SERVER)
    script = ssh.exec_command.call_args.args[0]
    uid = spawner._store.get_or_create_uid("alice.smith")  # the Hub picks the UID, the same on every worker
    assert uid >= 20000
    assert f'adduser --allow-bad-names --disabled-password --gecos "" --uid {uid} alice.smith' in script
    assert f"|| sudo useradd -m -s /bin/bash -u {uid} alice.smith" in script  # Amazon Linux fallback
    assert "mountpoint" not in script  # local homes: no shared-disk check
    assert "pip3 install" not in script  # packages are installed once per worker
    ssh.close.assert_called_once()


def test_jit_provisioning_never_sends_unsafe_name():
    inst = CustomSpawner()
    inst.user.name = 'x";sudo rm -rf /;"'
    ssh = _jit_ssh()
    with patch.object(spawner.paramiko, "SSHClient", return_value=ssh), pytest.raises(RuntimeError):
        inst._provision_user_jit(JIT_SERVER)
    ssh.exec_command.assert_not_called()


def _delete_spawner(server_info, name="alice"):
    inst = CustomSpawner()
    inst.user.name = name
    group = MagicMock()
    group.name = "team_alpha"
    inst.user.groups = [group]
    inst._get_server_info = MagicMock(return_value=server_info)
    return inst


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["archive", None])
async def test_delete_forever_archives_home_by_default(mode, monkeypatch):
    monkeypatch.setattr(spawner, "hub_settings", {} if mode is None else {"on_user_delete": mode})
    inst = _delete_spawner(JIT_SERVER)
    ssh = _jit_ssh()
    with patch.object(spawner.paramiko, "SSHClient", return_value=ssh):
        await inst.delete_forever()
    script = ssh.exec_command.call_args.args[0]
    assert "sudo pkill -KILL -u alice" in script
    assert "sudo mv /home/alice /home/.deleted/alice-" in script
    assert "sudo userdel alice" in script and "userdel -r" not in script
    ssh.connect.assert_called_once()
    assert ssh.connect.call_args.kwargs["username"] == "ubuntu"


@pytest.mark.anyio
async def test_delete_forever_delete_mode_removes_home(monkeypatch):
    monkeypatch.setattr(spawner, "hub_settings", {"on_user_delete": "delete"})
    inst = _delete_spawner(JIT_SERVER)
    ssh = _jit_ssh()
    with patch.object(spawner.paramiko, "SSHClient", return_value=ssh):
        await inst.delete_forever()
    assert "sudo userdel -r alice" in ssh.exec_command.call_args.args[0]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "settings,server",
    [
        ({"on_user_delete": "keep"}, JIT_SERVER),
        ({"on_user_delete": "typo"}, JIT_SERVER),
        ({}, {**JIT_SERVER, "ssh_user": "shared"}),  # shared team account: never deleted
        ({}, {"server_ip": "10.0.0.1", "server_ssh_key": "/k"}),  # no admin_ssh_user
    ],
)
async def test_delete_forever_skips(settings, server, monkeypatch):
    monkeypatch.setattr(spawner, "hub_settings", settings)
    inst = _delete_spawner(server)
    ssh = _jit_ssh()
    with patch.object(spawner.paramiko, "SSHClient", return_value=ssh):
        await inst.delete_forever()
    ssh.exec_command.assert_not_called()


@pytest.mark.anyio
async def test_delete_forever_unreachable_worker_only_logs(monkeypatch):
    monkeypatch.setattr(spawner, "hub_settings", {})
    inst = _delete_spawner(JIT_SERVER)
    ssh = _jit_ssh()
    ssh.connect.side_effect = OSError("no route")
    with patch.object(spawner.paramiko, "SSHClient", return_value=ssh):
        await inst.delete_forever()  # must not raise
    assert any("NOT deleted" in str(c) for c in inst.log.warning.call_args_list)


@pytest.mark.anyio
async def test_delete_forever_unsafe_name_never_reaches_shell(monkeypatch):
    monkeypatch.setattr(spawner, "hub_settings", {})
    inst = _delete_spawner(JIT_SERVER, name='x";rm -rf /;"')
    ssh = _jit_ssh()
    with patch.object(spawner.paramiko, "SSHClient", return_value=ssh):
        await inst.delete_forever()
    ssh.exec_command.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("setting,flag", [(None, False), (True, False), (False, True)])
async def test_terminals_switch(mock_spawner_instance, setting, flag):
    stdin, stdout, ssh = MagicMock(), MagicMock(), MagicMock()
    stdout.readline.return_value = "8888\n"
    stdout.read.return_value = b""
    stdout.channel.recv_exit_status.return_value = 0
    ssh.exec_command.side_effect = lambda cmd, *a, **k: (stdin, stdout, None)
    settings = {"hub_api_url": "http://hub:8081/hub/api"}
    if setting is not None:
        settings["terminals_enabled"] = setting
    with patch.object(mock_spawner_instance, "_open_ssh", return_value=ssh), \
            patch.dict(spawner.hub_settings, settings):
        await mock_spawner_instance.start()
    script = stdin.write.call_args[0][0]
    assert ("--ServerApp.terminals_enabled=False" in script) is flag


def _jit_script(server, team="team_alpha"):
    inst = CustomSpawner()
    inst.user.name = "alice"
    g = MagicMock()
    g.name = team
    inst.user.groups = [g]
    ssh = _jit_ssh()
    with patch.object(spawner.paramiko, "SSHClient", return_value=ssh):
        inst._provision_user_jit(server)
    return inst, ssh.exec_command.call_args.args[0]


def test_jit_sets_up_team_and_shared_folders():
    _, script = _jit_script(JIT_SERVER)
    assert "sudo groupadd -f team_alpha" in script
    assert "sudo usermod -aG team_alpha alice" in script
    assert "sudo chmod 2770 /data/team_alpha" in script
    assert "ln -sfn /data/team_alpha /home/alice/notebook/team" in script
    assert "ln -sfn /data/shared /home/alice/notebook/shared" in script
    assert "[ ! -L /home/alice/notebook/team ] ||" in script  # a real 'team' folder is left alone
    # Team setup runs for existing users too, so it must come after the "create user" block.
    assert script.index("fi\n") < script.index("groupadd")


def test_jit_skips_folders_for_shared_ssh_user():
    _, script = _jit_script({**JIT_SERVER, "ssh_user": "teamacct"})
    assert "/data/" not in script


def test_jit_bad_team_name_gets_shared_folder_only():
    inst, script = _jit_script(JIT_SERVER, team="Team Alpha;rm")
    assert "groupadd" not in script and "rm" not in script.split("fi\n", 1)[1]
    assert "/data/shared" in script
    inst.log.warning.assert_called()


@pytest.mark.anyio
async def test_terminal_off_for_one_user(mock_spawner_instance):
    spawner._store.set_user_limits("test_user", None, None, True)
    stdin, stdout, ssh = MagicMock(), MagicMock(), MagicMock()
    stdout.readline.return_value = "8888\n"
    stdout.read.return_value = b""
    stdout.channel.recv_exit_status.return_value = 0
    ssh.exec_command.side_effect = lambda cmd, *a, **k: (stdin, stdout, None)
    with patch.object(mock_spawner_instance, "_open_ssh", return_value=ssh), \
            patch.dict(spawner.hub_settings, {"hub_api_url": "http://hub:8081/hub/api"}):
        await mock_spawner_instance.start()
    assert "--ServerApp.terminals_enabled=False" in stdin.write.call_args[0][0]


# ── Users in more than one team ───────────────────────────────────────────────


def _team_spawner(groups, mapped=("team_alpha", "team_beta"), chosen=None):
    inst = CustomSpawner()
    inst.user.groups = []
    for name in groups:
        g = MagicMock()
        g.name = name
        inst.user.groups.append(g)

    def info(group):
        if group not in mapped:
            raise RuntimeError("not mapped")
        return {**JIT_SERVER, "server_ip": f"ip-of-{group}"}

    inst._get_server_info = MagicMock(side_effect=info)
    if chosen is not None:
        inst.user_options = {"team": chosen}
    return inst


@pytest.mark.anyio
async def test_one_team_user_gets_no_form_and_no_mapping_lookups():
    inst = _team_spawner(["team_beta"])
    assert await inst.get_options_form() == ""
    assert inst._team() == "team_beta"
    inst._get_server_info.assert_not_called()


@pytest.mark.anyio
async def test_two_team_user_chooses_and_non_team_groups_are_ignored():
    # JupyterHub's group order is not guaranteed: the default is the first by name.
    inst = _team_spawner(["team_beta", "a_non_team_group", "team_alpha"])
    assert inst._teams() == ["team_alpha", "team_beta"]
    assert inst._team() == "team_alpha"
    form = await inst.get_options_form()
    assert 'value="team_alpha" selected' in form and 'value="team_beta"' in form
    assert "a_non_team_group" not in form

    assert inst.options_from_form({"team": ["team_beta"]}) == {"team": "team_beta"}
    assert _team_spawner(["team_beta", "team_alpha"], chosen="team_beta")._team() == "team_beta"


def test_a_team_the_user_is_not_in_cannot_be_chosen():
    inst = _team_spawner(["team_alpha", "team_beta"], mapped=("team_alpha", "team_beta", "team_gamma"), chosen="team_gamma")
    assert inst._team() == "team_alpha"


@pytest.mark.anyio
async def test_delete_forever_removes_the_account_on_every_teams_worker(monkeypatch):
    monkeypatch.setattr(spawner, "hub_settings", {})
    inst = _team_spawner(["team_alpha", "team_beta"])
    inst.user.name = "alice"
    ssh = _jit_ssh()
    with patch.object(spawner.paramiko, "SSHClient", return_value=ssh):
        await inst.delete_forever()
    assert [c.args[0] for c in ssh.connect.call_args_list] == ["ip-of-team_alpha", "ip-of-team_beta"]


# ── Shared homes (home_base on EFS/NFS) ───────────────────────────────────────


def _shared_spawner(monkeypatch, name="alice", base="/efs/home"):
    monkeypatch.setattr(spawner, "hub_settings", {"home_base": base})
    inst = CustomSpawner()
    inst.user.name = name
    group = MagicMock()
    group.name = "team_alpha"
    inst.user.groups = [group]
    inst._get_server_info = MagicMock(return_value=JIT_SERVER)
    return inst


def test_shared_home_creates_the_account_there_with_the_hubs_uid(monkeypatch):
    inst = _shared_spawner(monkeypatch)
    ssh = _jit_ssh()
    with patch.object(spawner.paramiko, "SSHClient", return_value=ssh):
        inst._provision_user_jit(JIT_SERVER)
    script = ssh.exec_command.call_args.args[0]
    uid = spawner._store.get_or_create_uid("alice")
    assert uid == spawner._store.get_or_create_uid("alice")  # stable
    assert uid != spawner._store.get_or_create_uid("bob")
    assert f"--home /efs/home/alice --uid {uid} alice" in script
    assert f"useradd -m -s /bin/bash -d /efs/home/alice -u {uid} alice" in script
    assert script.index("mountpoint -q /efs/home ||") < script.index("adduser")  # checked before anything is created
    assert "ln -sfn /data/team_alpha /efs/home/alice/notebook/team" in script
    assert "chown -R alice: /efs/home/alice" in script


def test_start_is_refused_when_the_shared_home_is_not_mounted(monkeypatch):
    inst = _shared_spawner(monkeypatch)
    ssh = _jit_ssh()
    ssh.exec_command.return_value[1].channel.recv_exit_status.return_value = spawner._EXIT_NOT_MOUNTED
    with patch.object(spawner.paramiko, "SSHClient", return_value=ssh), pytest.raises(spawner.web.HTTPError) as err:
        inst._provision_user_jit(JIT_SERVER)
    assert err.value.status_code == 503
    ssh.close.assert_called_once()


@pytest.mark.anyio
async def test_delete_archives_inside_the_shared_home(monkeypatch):
    inst = _shared_spawner(monkeypatch)
    ssh = _jit_ssh()
    with patch.object(spawner.paramiko, "SSHClient", return_value=ssh):
        await inst.delete_forever()
    assert "sudo mv /efs/home/alice /efs/home/.deleted/alice-" in ssh.exec_command.call_args.args[0]


@pytest.mark.parametrize("bad", ["efs/home", "/efs/home; rm -rf /", "/efs/$(id)"])
def test_home_base_that_is_not_a_plain_path_is_refused(monkeypatch, bad):
    monkeypatch.setattr(spawner, "hub_settings", {"home_base": bad})
    with pytest.raises(RuntimeError):
        spawner._home_base()
