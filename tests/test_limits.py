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
    h.initialize(store)
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
async def test_page_refuses_empty_save(store):
    h = _handler(store, username="bob")
    await limits.LimitsPageHandler.post.__wrapped__(h)
    assert "Terminal off" in h.render_template.call_args.kwargs["error"]
    assert store.get_user_limits("bob") is None


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
