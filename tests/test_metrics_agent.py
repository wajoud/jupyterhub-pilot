"""Per-user usage rows built by the worker metrics agent."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

psutil = pytest.importorskip("psutil")
from jupyterhub_pilot import metrics_agent as ma  # noqa: E402


class FakeProc:
    def __init__(self, pid, cmd, cpu=0.0, rss_mb=0, user="u", env=None, children=()):
        self.pid = pid
        self.info = {"pid": pid, "cmdline": cmd.split()}
        self._cpu, self._rss, self._user = cpu, rss_mb * 1048576, user
        self._env, self._children = env or {}, list(children)

    def children(self, recursive=False):
        return self._children

    def username(self):
        return self._user

    def environ(self):
        return self._env

    def create_time(self):
        return 0.0

    def cpu_percent(self, interval=None):
        return self._cpu

    def memory_info(self):
        return SimpleNamespace(rss=self._rss)

    def cmdline(self):
        return self.info["cmdline"]


@pytest.fixture(autouse=True)
def _clean():
    ma._PROC_CACHE.clear()
    ma._HOME_BYTES.clear()
    yield
    ma._PROC_CACHE.clear()


def test_rows_add_up_each_server_with_its_kernels(monkeypatch):
    kernel = FakeProc(11, "python -m ipykernel_launcher -f k.json", cpu=150, rss_mb=500, user="shared")
    alice = FakeProc(10, "python jupyterhub-singleuser --port 1", cpu=10, rss_mb=100,
                     user="shared", env={"JUPYTERHUB_USER": "alice"}, children=[kernel])
    bob = FakeProc(20, "python jupyterhub-singleuser --port 2", cpu=40, rss_mb=50, user="bob")
    other = FakeProc(30, "sshd: ubuntu", cpu=99, rss_mb=999, user="ubuntu")
    monkeypatch.setattr(ma.psutil, "process_iter", lambda attrs=None: [alice, bob, other])
    monkeypatch.setattr(ma, "_cgroup_limits", lambda pid: {
        "mem_mb": None, "mem_limit_mb": 1024.0 if pid == 10 else None, "cpu_limit_pct": None})
    ma._HOME_BYTES["shared"] = 4096

    rows = {r["name"]: r for r in ma._collect_users(cpu_count=2)}

    assert set(rows) == {"alice", "bob"}  # sshd is not a notebook server
    a = rows["alice"]  # named from JUPYTERHUB_USER even though the account is shared
    assert (a["linux_user"], a["kernels"], a["processes"]) == ("shared", 1, 2)
    assert a["cpu_percent"] == 80.0 and a["cpu_cores"] == 1.6  # (10 + 150) % over 2 cores
    assert a["mem_mb"] == 600.0 and a["mem_limit_mb"] == 1024.0
    assert a["home_bytes"] == 4096
    assert rows["bob"]["home_bytes"] is None  # not measured yet


def test_cgroup_memory_beats_rss_and_stale_pids_are_dropped(monkeypatch):
    srv = FakeProc(10, "jupyterhub-singleuser", rss_mb=100, env={"JUPYTERHUB_USER": "a"})
    monkeypatch.setattr(ma.psutil, "process_iter", lambda attrs=None: [srv])
    monkeypatch.setattr(ma, "_cgroup_limits", lambda pid: {
        "mem_mb": 777.0, "mem_limit_mb": 1024.0, "cpu_limit_pct": 50.0})
    ma._PROC_CACHE[999] = srv  # a process that has exited

    row = ma._collect_users(cpu_count=1)[0]

    assert row["mem_mb"] == 777.0 and row["cpu_limit_pct"] == 50.0
    assert set(ma._PROC_CACHE) == {10}


def test_failed_jobs_lists_only_failed_and_never_follows_symlinks(tmp_path, monkeypatch):
    home = tmp_path / "shared" / "alice"  # homes can be outside /home (shared disk)
    jobs = home / ".config" / "jupyterhub-pilot" / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "report.last").write_text("failed 1762650000\n")
    (jobs / "fine.last").write_text("ok 1762650000\n")
    (jobs / "junk.last").write_text("not the format at all")
    secret = tmp_path / "root-only"
    secret.write_text("failed 1\n")
    (jobs / "link.last").symlink_to(secret)
    accounts = [
        SimpleNamespace(pw_name="alice", pw_uid=20000, pw_dir=str(home)),
        SimpleNamespace(pw_name="daemon", pw_uid=1, pw_dir=str(home)),  # system accounts are skipped
    ]
    monkeypatch.setattr(ma.pwd, "getpwall", lambda: accounts)
    assert ma._failed_jobs() == [{"user": "alice", "job": "report", "at": 1762650000}]
