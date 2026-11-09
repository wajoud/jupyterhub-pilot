import json
from unittest.mock import patch

import pytest

from jupyterhub_pilot import cli, upgrade


@pytest.mark.parametrize(
    "v,expected",
    [("0.5.1", (0, 5, 1)), ("0.6.0\n", (0, 6, 0)), ("0", (0,)), ("0+unknown", (0,)), ("", (0,)), ("1.2rc1", (1,))],
)
def test_parse_version(v, expected):
    assert upgrade.parse_version(v) == expected


def test_settings_defaults_added_and_existing_values_kept(tmp_path):
    f = tmp_path / "hub_settings.json"
    f.write_text(json.dumps({"hub_ip": "10.0.0.1", "idle_cull": None}))
    msg = upgrade.add_settings_defaults(str(tmp_path))
    settings = json.loads(f.read_text())
    assert settings["on_user_delete"] == "archive"
    assert settings["idle_cull"] is None  # admin turned it off: keep that
    assert settings["hub_ip"] == "10.0.0.1"
    assert "on_user_delete" in msg and "idle_cull" not in msg
    assert upgrade.add_settings_defaults(str(tmp_path)) == ""  # second run: no change


def test_metrics_url_fixed_once(tmp_path, monkeypatch):
    unit = tmp_path / "metrics.service"
    unit.write_text("ExecStart=python -m jupyterhub_pilot.metrics_agent --hub ws://10.0.0.1:8000/monitoring/ws\n")
    monkeypatch.setattr(upgrade, "METRICS_UNIT", str(unit))
    with patch.object(upgrade.subprocess, "run") as run:
        assert upgrade.fix_metrics_url("") != ""
        assert upgrade.fix_metrics_url("") == ""  # already fixed
    assert "ws://10.0.0.1:8000/hub/monitoring/ws" in unit.read_text()
    run.assert_called_once_with(["systemctl", "daemon-reload"], check=True)


def test_only_newer_steps_for_this_role_run(monkeypatch):
    calls = []
    monkeypatch.setattr(
        upgrade,
        "STEPS",
        [("0.4.0", "worker", lambda d: calls.append("w040") or "w"),
         ("0.6.0", "hub", lambda d: calls.append("h060") or "h"),
         ("0.7.0", "hub", lambda d: calls.append("h070") or "")],
    )
    assert upgrade.run_steps("hub", "0", "/x") == ["h"]  # empty message = nothing to do
    assert calls == ["h060", "h070"]
    calls.clear()
    upgrade.run_steps("hub", "0.6.0", "/x")
    assert calls == ["h070"]
    calls.clear()
    upgrade.run_steps("worker", "0.5.1", "/x")
    assert calls == []


def test_upgrade_needs_root(capsys):
    with patch.object(upgrade.os, "geteuid", return_value=1000):
        assert cli.main(["upgrade"]) == 1
    assert "sudo" in capsys.readouterr().out


def test_pip_failure_stops_before_any_change(monkeypatch):
    monkeypatch.setattr(upgrade.os, "geteuid", lambda: 0)
    monkeypatch.setattr(upgrade, "detect_role", lambda: "worker")
    with patch.object(upgrade.subprocess, "run") as run:
        run.return_value.returncode = 1
        assert upgrade.main([]) == 1
    assert run.call_count == 1
    assert "jupyterhub-pilot[worker,ai]" in run.call_args.args[0]


def test_hub_restart_needs_confirmation(monkeypatch, tmp_path):
    monkeypatch.setattr(upgrade, "detect_role", lambda: "hub")
    monkeypatch.setattr(upgrade, "VERSION_FILE", str(tmp_path / "version"))
    monkeypatch.setattr(upgrade, "ETC_DIR", str(tmp_path))
    monkeypatch.setattr(upgrade, "run_steps", lambda *a: [])
    monkeypatch.setattr(upgrade, "worker_versions", lambda d: {"team_a": "0.5.1"})
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    with patch.object(upgrade.subprocess, "run") as run:
        assert upgrade.migrate(yes=False) == 0
    run.assert_not_called()
    assert (tmp_path / "version").read_text().strip()


def test_worker_packages_installed_only_when_missing(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda c: None if c == "git" else "/usr/bin/" + c)
    with patch.object(upgrade.subprocess, "run") as run:
        assert upgrade.install_worker_packages("") == "installed git"
    run.assert_called_once_with(["apt-get", "install", "-y", "git"], check=True)
    monkeypatch.setattr(shutil, "which", lambda c: "/usr/bin/" + c)
    with patch.object(upgrade.subprocess, "run") as run:
        assert upgrade.install_worker_packages("") == ""
    run.assert_not_called()
