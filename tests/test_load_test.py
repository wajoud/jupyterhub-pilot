"""Pure helpers of scripts/load_test.py (the Hub/kernel parts need a live cluster)."""

from jupyterhub_pilot import load_test as lt


def test_users_are_spread_evenly_over_teams():
    users = lt.usernames("lt", 7)
    assert users[0] == "lt-001" and users[-1] == "lt-007"
    plan = lt.assign_teams(users, ["a", "b", "c"])
    assert [len(v) for v in plan.values()] == [3, 2, 2]
    assert sorted(u for v in plan.values() for u in v) == users


def test_percentile():
    assert lt.percentile([], 50) is None
    assert lt.percentile([5.0, 1.0, 3.0], 50) == 3.0
    assert lt.percentile([1.0, 2.0, 3.0, 4.0], 100) == 4.0


def test_workload_code_runs_and_holds_memory():
    out = {}
    exec(compile(lt.workload_code(1, 0), "<workload>", "exec"), out)  # noqa: S102
    assert len(out["buf"]) == 1024 * 1024
