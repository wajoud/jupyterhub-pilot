"""The `jupyterhub-pilot` command and the files it needs from the package."""

import os
import subprocess
from unittest.mock import patch

import pytest

from jupyterhub_pilot import cli, paths


@pytest.mark.parametrize("script", ["install_hub.sh", "install_worker.sh", "add_worker.sh"])
def test_setup_scripts_ship_in_the_package(script):
    path = os.path.join(paths.PACKAGE_DIR, "scripts", script)
    assert subprocess.run(["bash", "-n", path]).returncode == 0  # exists and parses


def test_hub_config_and_templates_ship_in_the_package():
    assert os.path.isfile(os.path.join(paths.PACKAGE_DIR, "jupyterhub_config.py"))
    assert os.path.isfile(os.path.join(paths.PACKAGE_DIR, "templates", "page.html"))


def test_get_port_cmd_prints_a_free_port():
    out = subprocess.run(paths.GET_PORT_CMD, shell=True, capture_output=True, text=True)
    assert 1024 < int(out.stdout) < 65536


def test_install_stops_with_the_pip_command_when_extras_are_missing(capsys):
    with patch.object(cli, "missing_modules", return_value=["paramiko"]), \
            patch.object(cli, "run_script") as run:
        assert cli.main(["install-hub"]) == 1
    run.assert_not_called()
    assert 'pip3 install --break-system-packages "jupyterhub-pilot[hub]"' in capsys.readouterr().err


def test_commands_run_their_script_with_this_python():
    with patch.object(cli, "missing_modules", return_value=[]), \
            patch.object(cli.subprocess, "run") as run:
        run.return_value.returncode = 0
        assert cli.main(["add-worker", "team_beta", "10.0.2.5", "ec2-user"]) == 0
    cmd, env = run.call_args.args[0], run.call_args.kwargs["env"]
    assert cmd[0] == "bash" and cmd[1].endswith(os.path.join("scripts", "add_worker.sh"))
    assert cmd[2:] == ["team_beta", "10.0.2.5", "ec2-user"]
    assert env["PYTHON"] == cli.sys.executable


def test_load_test_gets_the_remaining_arguments():
    with patch("jupyterhub_pilot.load_test.main", return_value=0) as lt_main:
        assert cli.main(["load-test", "--hub", "http://h:8000", "--users", "3", "--cleanup"]) == 0
    lt_main.assert_called_once_with(["--hub", "http://h:8000", "--users", "3", "--cleanup"])


def test_unknown_arguments_are_rejected():
    with pytest.raises(SystemExit):
        cli.main(["install-worker", "--bogus"])
