import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from jupyterhub_pilot import limits, spawner
from jupyterhub_pilot.session_store import SessionStore

DEFAULT = {"memory_max": "1G", "cpu_quota": "50%"}


@pytest.mark.parametrize("value", ["512M", "1G", "3T"])
def test_memory_ok(value):
    assert limits.check("memory_max", value) is None


@pytest.mark.parametrize("value", ["", "1GB", "0", "2048", "64K", "-1G", "1g", "1G; rm", None, 5])
def test_memory_bad(value):
    assert "512M" in limits.check("memory_max", value)


@pytest.mark.parametrize("value", ["50", "0%", "x%", "100%\n"])
def test_cpu_bad(value):
    assert limits.check("cpu_quota", value)


def test_resolve_order_per_value():
    out = limits.resolve({"memory_max": "4G", "cpu_quota": None}, {"cpu_quota": "200%"}, DEFAULT, print)
    assert out == {"memory_max": ("4G", "user"), "cpu_quota": ("200%", "team")}
    assert limits.resolve(None, None, DEFAULT, print)["memory_max"] == ("1G", "default")
    assert limits.resolve(None, None, None, print)["cpu_quota"] == (None, "none")


def test_resolve_skips_bad_value_with_warning():
    warn = MagicMock()
    out = limits.resolve(None, {"memory_max": "lots"}, DEFAULT, warn)
    assert out["memory_max"] == ("1G", "default")
    assert "team limit ignored" in warn.call_args.args[0]


@pytest.fixture
def store(monkeypatch):
    s = SessionStore(os.path.join(tempfile.mkdtemp(), "t.db"))
    s.init_db()
    monkeypatch.setattr(spawner, "_store", s)
    return s


def test_store_roundtrip(store):
    store.set_user_limits("bob", "2G", None)
    assert store.get_user_limits("bob") == {"memory_max": "2G", "cpu_quota": None, "terminals_off": 0}
    assert store.list_user_limits() == [{"username": "bob", "memory_max": "2G", "cpu_quota": None, "terminals_off": 0}]
    store.delete_user_limits("bob")
    assert store.get_user_limits("bob") is None


@pytest.mark.anyio
async def test_spawner_uses_user_then_team_then_default(store, monkeypatch):
    monkeypatch.setattr(spawner, "hub_settings", {"resource_limits": DEFAULT})
    inst = spawner.CustomSpawner()
    inst.user.name = "bob"
    team = MagicMock()
    team.properties = {"memory_max": "3G", "cpu_quota": "150%"}
    inst.user.groups = [team]
    store.set_user_limits("bob", None, "300%")
    await inst._prepare_spawn()
    assert (inst._memory_max, inst._cpu_quota) == ("3G", "300%")


def _handler(store, admin=True, **form):
    h = limits.LimitsPageHandler()
    h.initialize(store, worker_ip_for_group={"team_alpha": "10.0.1.5"}.get)
    h._hub_users = lambda: [("bob", {"team_alpha": {"memory_max": "3G"}})]
    h._hub_groups = lambda: [("team_alpha", {"memory_max": "3G"}, 1)]
    h._save_team = MagicMock(return_value=True)
    h.current_user = MagicMock(admin=admin)
    h.get_body_argument = lambda k, default=None: form.get(k, default)
    h.render_template = AsyncMock(return_value="html")
    h.finish = MagicMock()
    h.set_status = MagicMock()
    return h


@pytest.mark.anyio
async def test_page_saves_valid_limits(store):
    h = _handler(store, username="bob", memory_max="2G", cpu_quota="")
    await limits.LimitsPageHandler.post.__wrapped__(h)
    assert store.get_user_limits("bob") == {"memory_max": "2G", "cpu_quota": None, "terminals_off": 0}


@pytest.mark.anyio
async def test_page_refuses_bad_value(store):
    h = _handler(store, username="bob", memory_max="2 gigs")
    await limits.LimitsPageHandler.post.__wrapped__(h)
    h.set_status.assert_called_with(400)
    assert store.get_user_limits("bob") is None
    assert "512M" in h.render_template.call_args.kwargs["error"]


@pytest.mark.anyio
async def test_page_clear(store):
    store.set_user_limits("bob", "2G", None)
    await limits.LimitsPageHandler.post.__wrapped__(_handler(store, username="bob", action="clear"))
    assert store.get_user_limits("bob") is None


@pytest.mark.anyio
async def test_page_admin_only(store):
    with pytest.raises(limits.web.HTTPError):
        await limits.LimitsPageHandler.post.__wrapped__(_handler(store, admin=False, username="bob", memory_max="2G"))


@pytest.mark.anyio
async def test_page_saves_terminal_off_alone(store):
    h = _handler(store, username="bob", memory_max="", cpu_quota="", terminals_off="on")
    await limits.LimitsPageHandler.post.__wrapped__(h)
    assert store.get_user_limits("bob") == {"memory_max": None, "cpu_quota": None, "terminals_off": 1}


@pytest.mark.anyio
async def test_page_saving_limits_without_the_box_turns_terminal_back_on(store):
    store.set_user_limits("bob", None, None, True)
    await limits.LimitsPageHandler.post.__wrapped__(_handler(store, username="bob", memory_max="2G"))
    assert store.get_user_limits("bob")["terminals_off"] == 0


@pytest.mark.anyio
async def test_page_empty_save_clears_the_user(store):
    store.set_user_limits("bob", "2G", None)
    await limits.LimitsPageHandler.post.__wrapped__(_handler(store, username="bob"))
    assert store.get_user_limits("bob") is None


@pytest.mark.anyio
async def test_page_saves_team_limits_and_refuses_a_group_without_a_worker(store):
    h = _handler(store, action="team", team="team_alpha", memory_max="4G", cpu_quota="")
    await limits.LimitsPageHandler.post.__wrapped__(h)
    h._save_team.assert_called_with("team_alpha", {"memory_max": "4G", "cpu_quota": None})

    h = _handler(store, action="team", team="admins", memory_max="4G")
    await limits.LimitsPageHandler.post.__wrapped__(h)
    h._save_team.assert_not_called()
    h.set_status.assert_called_with(400)

    h = _handler(store, action="team", team="team_alpha", memory_max="lots")
    await limits.LimitsPageHandler.post.__wrapped__(h)
    h._save_team.assert_not_called()


def test_view_shows_every_user_with_the_limit_that_applies_and_where_from():
    snapshots = {"w1": {
        "ip": "10.0.1.5", "cpu": {"core_count": 4}, "memory": {"total_mb": 16384}, "gpus": [{"name": "Tesla T4"}],
        "users": [{"name": "bob", "mem_mb": 512, "mem_limit_mb": 1024, "cpu_limit_pct": 50}],
    }}
    view = limits.build_view(
        users=[("alice", {}), ("bob", {"team_alpha": {"memory_max": "3G"}, "admins": {}})],
        groups=[("admins", {}, 1), ("team_alpha", {"memory_max": "3G"}, 1)],
        overrides=[{"username": "bob", "memory_max": None, "cpu_quota": "300%", "terminals_off": 1},
                   {"username": "carol", "memory_max": "2G", "cpu_quota": None, "terminals_off": 0}],
        default=DEFAULT, worker_ip_for_group={"team_alpha": "10.0.1.5"}.get, snapshots=snapshots,
    )
    assert [t["name"] for t in view["teams"]] == ["team_alpha"]  # "admins" has no worker
    assert view["teams"][0]["worker"] == "4 cores · 16.0 GB RAM · GPU Tesla T4"
    alice, bob, carol = view["users"]
    assert (alice["memory_max_now"], alice["memory_max_from"], alice["running"]) == ("1G", "default", None)
    assert (bob["memory_max_now"], bob["memory_max_from"]) == ("3G", "team team_alpha")
    assert (bob["cpu_quota_now"], bob["cpu_quota_from"], bob["terminals_off"]) == ("300%", "set here", True)
    assert bob["running"]["memory"] == "512 MB of 1.0 GB" and bob["running"]["old_limit"]  # started before the change
    assert (carol["known"], carol["memory_max"]) == (False, "2G")  # set before her first login


def test_old_user_limits_table_gets_terminals_off_column(tmp_path):
    import sqlite3

    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE user_limits (username TEXT PRIMARY KEY, memory_max TEXT, cpu_quota TEXT)")
    conn.execute("INSERT INTO user_limits VALUES ('bob', '2G', NULL)")
    conn.commit()
    conn.close()
    s = SessionStore(db)
    s.init_db()
    assert s.get_user_limits("bob") == {"memory_max": "2G", "cpu_quota": None, "terminals_off": 0}


@pytest.mark.anyio
async def test_spawner_reads_terminal_off(store, monkeypatch):
    monkeypatch.setattr(spawner, "hub_settings", {})
    inst = spawner.CustomSpawner()
    inst.user.name = "bob"
    inst.user.groups = []
    await inst._prepare_spawn()
    assert inst._terminals_off is False
    store.set_user_limits("bob", None, None, True)
    await inst._prepare_spawn()
    assert inst._terminals_off is True
