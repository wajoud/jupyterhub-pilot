import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from jupyterhub_pilot import schedule


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "UNIT_DIR", str(tmp_path / "units"))
    monkeypatch.setattr(schedule, "ENV_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(schedule, "current_limits", lambda: ["MemoryMax=1024M", "CPUQuota=50%"])
    nb = tmp_path / "My Report.ipynb"
    nb.write_text("{}")
    return tmp_path, str(nb)


def _ok(*a, **k):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_unit_quoting_survives_spaces_percent_dollar_quotes():
    assert schedule._q('a b%c$d"e\\f') == '"a b%%c$$d\\"e\\\\f"'


def test_add_writes_units_under_the_users_limit(home, monkeypatch):
    tmp, nb = home
    monkeypatch.setenv("JUPYTERHUB_PILOT_SECRET_KEYS", "API_KEY")
    monkeypatch.setenv("API_KEY", 'sk-"1"')
    with patch.object(schedule.subprocess, "run", side_effect=_ok) as run:
        msg = schedule.schedule(f'add "*-*-* 02:00" "{nb}" --keep 3')
    assert msg.startswith("✅ 'My_Report' scheduled")
    service = (tmp / "units" / "jhp-My_Report.service").read_text()
    timer = (tmp / "units" / "jhp-My_Report.timer").read_text()
    assert f'-m jupyterhub_pilot.schedule _run "{nb}" 3' in service
    assert "MemoryMax=1024M" in service and "CPUQuota=50%" in service
    assert "OnCalendar=*-*-* 02:00" in timer and "Persistent=true" in timer
    env = tmp / "jobs" / "My_Report.env"
    assert env.read_text() == 'API_KEY="sk-\\"1\\""\n'
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    assert ["systemctl", "--user", "enable", "--now", "jhp-My_Report.timer"] in [c.args[0] for c in run.call_args_list]


def test_add_refuses_bad_calendar_and_missing_notebook(home):
    tmp, nb = home
    with patch.object(schedule.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
        assert "not a systemd calendar" in schedule.schedule(f"add nightly '{nb}'")
    assert "No notebook" in schedule.schedule(f"add daily {tmp}/nope.ipynb")
    assert not os.path.exists(tmp / "units")


@pytest.mark.parametrize("line", ["", "help", "add daily", "frobnicate x", "add daily x.ipynb --keep"])
def test_bad_usage_explains(line, home):
    out = schedule.schedule(line)
    assert "%schedule" in out or out.startswith("❌")


def test_control_rejects_unknown_or_unsafe_names(home):
    assert "No scheduled notebook" in schedule.schedule("delete ../../etc")
    assert "No scheduled notebook" in schedule.schedule("run nothing")


def test_delete_removes_units_and_secrets_keeps_results(home):
    tmp, nb = home
    with patch.object(schedule.subprocess, "run", side_effect=_ok):
        schedule.schedule(f"add daily '{nb}'")
        result = tmp / "My Report-20261109-0200.ipynb"
        result.write_text("{}")
        assert "deleted" in schedule.schedule("delete My_Report")
    assert not os.listdir(tmp / "units") and not os.listdir(tmp / "jobs")
    assert result.exists()


def test_list_parses_systemctl_show(home):
    tmp, nb = home
    shows = {
        "jhp-My_Report.timer": "UnitFileState=disabled\nTimersCalendar={ OnCalendar=*-*-* 00:00:00 ; next_elapse=x }\n",
        "jhp-My_Report.service": "Result=exit-code\nExecMainExitTimestamp=Mon 2026-11-09 02:00:05 UTC\n",
    }

    def fake(cmd, **k):
        if cmd[2] == "show" and cmd[3] in shows:
            return SimpleNamespace(returncode=0, stdout=shows[cmd[3]], stderr="")
        return _ok()

    with patch.object(schedule.subprocess, "run", side_effect=fake):
        schedule.schedule(f"add daily '{nb}'")
        out = schedule.list_jobs()
    assert "My_Report | *-*-* 00:00:00 | paused | exit-code at Mon 2026-11-09 02:00:05 UTC" in out


def test_prune_keeps_last_n_runs_only(tmp_path):
    nb = tmp_path / "r.ipynb"
    names = ["r-20261101-0200.ipynb", "r-20261102-0200-FAILED.ipynb", "r-20261103-0200.ipynb", "r-final.ipynb"]
    for n in names:
        (tmp_path / n).write_text("{}")
    schedule.prune(str(nb), 2)
    assert sorted(os.listdir(tmp_path)) == ["r-20261102-0200-FAILED.ipynb", "r-20261103-0200.ipynb", "r-final.ipynb"]


@pytest.mark.parametrize("fails", [False, True])
def test_run_saves_result_and_marks_failures(tmp_path, fails, monkeypatch):
    nb = tmp_path / "r.ipynb"
    nb.write_text("{}")

    def execute(src, out, cwd):
        open(out, "w").write("{}")
        if fails:
            raise RuntimeError("cell 3 failed")

    monkeypatch.setitem(sys.modules, "papermill", MagicMock(execute_notebook=execute))
    assert schedule.run(str(nb), 5) == (1 if fails else 0)
    outs = [p for p in os.listdir(tmp_path) if p != "r.ipynb"]
    assert len(outs) == 1 and outs[0].endswith("-FAILED.ipynb") is fails
