"""
tests/test_monitoring.py
────────────────────────
Security-relevant behaviour of the monitoring handlers: the agent token check
and which workers each browser user is allowed to see.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from tornado import web

from jupyterhub_pilot import monitoring_handler as mh

MAPPING = {"team_alpha": "10.0.1.5", "team_beta": "10.0.2.5"}


@pytest.fixture(autouse=True)
def _clean_state():
    mh._WORKER_SNAPSHOTS.clear()
    mh._BROWSER_CLIENTS.clear()
    yield
    mh._WORKER_SNAPSHOTS.clear()
    mh._BROWSER_CLIENTS.clear()


def _agent(auth=None, ip="10.0.1.5"):
    h = mh.AgentWebSocketHandler.__new__(mh.AgentWebSocketHandler)
    h.request = SimpleNamespace(
        headers={"Authorization": auth} if auth else {}, remote_ip=ip
    )
    return h


def _browser(admin, groups, name="someone"):
    b = mh.BrowserWebSocketHandler.__new__(mh.BrowserWebSocketHandler)
    b.initialize(worker_ip_for_group=MAPPING.get)
    b.current_user = SimpleNamespace(
        name=name, admin=admin, groups=[SimpleNamespace(name=g) for g in groups]
    )
    b.write_message = MagicMock()
    return b


def _messages(browser, kind):
    msgs = [json.loads(c.args[0]) for c in browser.write_message.call_args_list]
    return [m for m in msgs if m["type"] == kind]


def _send(conn_ip, hostname, **payload):
    a = _agent(ip=conn_ip)
    a.open()
    a.on_message(json.dumps({"hostname": hostname, **payload}))
    return a


# ── Agent token ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "token, auth",
    [(None, "Bearer anything"), ("s3cret", None), ("s3cret", "Bearer wrong")],
)
def test_agent_rejected_without_valid_token(monkeypatch, token, auth):
    if token is None:
        monkeypatch.delenv("JUPYTERPILOT_AGENT_TOKEN", raising=False)
    else:
        monkeypatch.setenv("JUPYTERPILOT_AGENT_TOKEN", token)
    with pytest.raises(web.HTTPError) as exc:
        _agent(auth).prepare()
    assert exc.value.status_code == 403


def test_agent_accepted_with_token(monkeypatch):
    monkeypatch.setenv("JUPYTERPILOT_AGENT_TOKEN", "s3cret")
    _agent("Bearer s3cret").prepare()  # must not raise


# ── Who sees which worker ─────────────────────────────────────────────────────


def test_users_see_only_their_groups_workers_admins_see_all():
    alice, admin = _browser(False, ["team_alpha"]), _browser(True, [])
    alice.open()
    admin.open()
    _send("10.0.1.5", "w-alpha")
    _send("10.0.2.5", "w-beta")

    assert {m["worker"] for m in _messages(alice, "snapshot")} == {"w-alpha"}
    assert {m["worker"] for m in _messages(admin, "snapshot")} == {"w-alpha", "w-beta"}


def test_init_is_filtered_and_uses_the_connection_ip():
    # The agent claims 127.0.1.1; the Hub must trust the connection's address.
    _send("10.0.2.5", "w-beta", ip="127.0.1.1")
    assert mh._WORKER_SNAPSHOTS["w-beta"]["ip"] == "10.0.2.5"

    alice, bob = _browser(False, ["team_alpha"]), _browser(False, ["team_beta"])
    alice.open()
    bob.open()
    assert _messages(alice, "init") == [{"type": "init", "workers": {}}]
    assert list(_messages(bob, "init")[0]["workers"]) == ["w-beta"]


def test_user_without_mapped_group_sees_nothing():
    _send("10.0.1.5", "w-alpha")
    nobody = _browser(False, ["unmapped_team"])
    nobody.open()
    assert _messages(nobody, "init")[0]["workers"] == {}


def test_agent_disconnect_removes_worker():
    admin = _browser(True, [])
    admin.open()
    agent = _send("10.0.1.5", "w-alpha")
    agent.on_close()

    assert "w-alpha" not in mh._WORKER_SNAPSHOTS
    assert _messages(admin, "gone") == [{"type": "gone", "worker": "w-alpha"}]


# ── Per-user usage rows ───────────────────────────────────────────────────────

USERS = [{"name": "alice", "mem_mb": 300}, {"name": "carol", "mem_mb": 200}]


def test_users_see_only_their_own_usage_row_admins_see_everyone():
    alice = _browser(False, ["team_alpha"], name="alice")
    admin = _browser(True, [], name="boss")
    alice.open()
    admin.open()
    _send("10.0.1.5", "w-alpha", users=USERS)

    assert _messages(alice, "snapshot")[0]["data"]["users"] == [USERS[0]]
    assert _messages(admin, "snapshot")[0]["data"]["users"] == USERS
    # A later browser gets the same filtering in its init message
    late = _browser(False, ["team_alpha"], name="carol")
    late.open()
    assert _messages(late, "init")[0]["workers"]["w-alpha"]["users"] == [USERS[1]]


def test_gone_message_is_not_rewritten_for_non_admins():
    alice = _browser(False, ["team_alpha"], name="alice")
    alice.open()
    _send("10.0.1.5", "w-alpha", users=USERS).on_close()
    assert _messages(alice, "gone") == [{"type": "gone", "worker": "w-alpha"}]


def test_users_see_only_their_own_failed_scheduled_jobs():
    jobs = [{"user": "alice", "job": "report", "at": 1}, {"user": "carol", "job": "etl", "at": 2}]
    alice = _browser(False, ["team_alpha"], name="Alice@corp.com")  # Linux user is the part before @, lowercased
    admin = _browser(True, [], name="boss")
    alice.open()
    admin.open()
    _send("10.0.1.5", "w-alpha", failed_jobs=jobs)
    assert _messages(alice, "snapshot")[0]["data"]["failed_jobs"] == [jobs[0]]
    assert _messages(admin, "snapshot")[0]["data"]["failed_jobs"] == jobs


# ── Usage history and the Prometheus endpoint ─────────────────────────────────


@pytest.fixture
def usage_store(tmp_path, monkeypatch):
    from jupyterhub_pilot.session_store import SessionStore

    store = SessionStore(str(tmp_path / "state.db"))
    store.init_db()
    monkeypatch.setattr(mh, "USAGE_STORE", store)
    monkeypatch.setattr(mh, "_USAGE_BUCKET", 0)
    mh._USAGE_ACC.clear()
    return store


def _row(name, cpu, mem):
    return {"name": name, "cpu_cores": cpu, "mem_mb": mem, "cpu_limit_pct": 50, "mem_limit_mb": 1024}


def test_usage_is_stored_as_five_minute_averages_with_the_peak(usage_store):
    t0 = 1_762_650_000 // 300 * 300
    mh.record_usage("w1", {"users": [_row("alice", 0.2, 100)]}, now=t0 + 2)
    mh.record_usage("w1", {"users": [_row("alice", 0.4, 300), "junk", {"no": "name"}]}, now=t0 + 4)
    assert usage_store.get_usage("alice", 0) == []  # the 5 minutes are not over yet
    mh.record_usage("w1", {"users": []}, now=t0 + 300)
    [[ts, cpu, mem, peak, cpu_limit, mem_limit]] = usage_store.get_usage("alice", 0)
    assert (ts, round(cpu, 2), mem, peak, cpu_limit, mem_limit) == (t0, 0.3, 200, 300, 50, 1024)


def test_usage_older_than_15_days_is_dropped_and_long_ranges_are_averaged(usage_store):
    now = 1_762_650_000
    usage_store.add_usage([("alice", now - 16 * 86400, "w", 1, 1, 1, None, None)], now)
    usage_store.add_usage([("alice", now - 600, "w", 1.0, 100, 100, None, None),
                           ("alice", now - 300, "w", 3.0, 300, 400, None, None)], now)
    assert len(usage_store.get_usage("alice", 0)) == 2
    [[_, cpu, mem, peak, *_rest]] = usage_store.get_usage("alice", 0, step=86400 * 30)
    assert (cpu, mem, peak) == (2.0, 200, 400)
    assert usage_store.usage_users() == ["alice"]


def _history(viewer, admin, usage_store, **args):
    h = mh.UsageHistoryHandler.__new__(mh.UsageHistoryHandler)
    h.current_user = SimpleNamespace(name=viewer, admin=admin)
    h.get_argument = lambda key, default=None: args.get(key, default)
    h.set_header = MagicMock()
    h.write = MagicMock()
    mh.UsageHistoryHandler.get.__wrapped__(h)
    return json.loads(h.write.call_args.args[0])


def test_history_is_own_only_unless_admin(usage_store):
    import time

    usage_store.add_usage([("alice", int(time.time()) - 600, "w", 1.0, 100, 100, None, None)], int(time.time()))
    assert len(_history("alice", False, usage_store)["points"]) == 1
    assert _history("alice", False, usage_store)["users"] == []  # the user list is for admins
    assert _history("boss", True, usage_store, user="alice", days="15")["users"] == ["alice"]
    assert _history("boss", True, usage_store, user="alice", days="999")["days"] == 15
    with pytest.raises(web.HTTPError) as err:
        _history("bob", False, usage_store, user="alice")
    assert err.value.status_code == 403


def test_prometheus_text_has_worker_and_user_gauges_with_escaped_labels():
    text = mh.prometheus_text({
        "w1": {"cpu": {"percent": 12.5}, "memory": {"used_mb": 100, "total_mb": 200}, "disk": "junk",
               "users": [{"name": 'a"b', "cpu_cores": 0.5, "mem_mb": 300, "mem_limit_mb": None, "cpu_limit_pct": 50}]},
    })
    assert 'jupyterhub_pilot_worker_cpu_percent{worker="w1"} 12.5' in text
    assert 'jupyterhub_pilot_user_memory_mb{worker="w1",user="a\\"b"} 300' in text
    assert 'jupyterhub_pilot_user_cpu_limit_cores{worker="w1",user="a\\"b"} 0.5' in text
    assert "memory_limit_mb" not in text and "disk_percent" not in text  # no value, no sample


@pytest.mark.parametrize("token, auth, ok", [(None, "Bearer x", False), ("s3", "Bearer no", False), ("s3", "Bearer s3", True)])
def test_prometheus_endpoint_needs_its_own_token(monkeypatch, token, auth, ok):
    if token:
        monkeypatch.setenv("JUPYTERPILOT_METRICS_TOKEN", token)
    else:
        monkeypatch.delenv("JUPYTERPILOT_METRICS_TOKEN", raising=False)
    h = mh.PrometheusHandler.__new__(mh.PrometheusHandler)
    h.request = SimpleNamespace(headers={"Authorization": auth})
    h.set_header = MagicMock()
    h.write = MagicMock()
    if ok:
        h.get()
        h.write.assert_called_once()
    else:
        with pytest.raises(web.HTTPError):
            h.get()


def test_one_hour_range_and_most_active_users_for_admins(usage_store):
    import time

    now = int(time.time())
    usage_store.add_usage(
        [("alice", now - 600, "w1", 2.0, 100, 300, None, None), ("alice", now - 7200, "w1", 2.0, 100, 900, None, None),
         ("bob", now - 600, "w2", 0.5, 50, 60, None, None)], now)
    hour = _history("boss", True, usage_store, user="alice", hours="1")
    assert (hour["span_s"], len(hour["points"])) == (3600, 1)
    assert [r[0] for r in hour["top"]] == ["alice", "bob"]  # most CPU first
    assert hour["top"][0][1:4] == [pytest.approx(2 / 12), 300, pytest.approx(1 / 12)]  # core-hours, peak MB, hours
    assert _history("alice", False, usage_store, hours="1")["top"] == []  # other people's totals are for admins


def test_card_gets_its_team_names_and_prometheus_its_gpu(usage_store):
    usage_store.set_mapping("team_alpha", "10.0.1.5", "/k")
    usage_store.set_mapping("team_gamma", "10.0.1.5", "/k")
    _send("10.0.1.5", "w1", gpus=[{"index": 0, "name": "Tesla T4", "percent": 37.0, "mem_used_mb": 5900.0}])
    assert mh._WORKER_SNAPSHOTS["w1"]["teams"] == ["team_alpha", "team_gamma"]
    text = mh.prometheus_text(mh._WORKER_SNAPSHOTS)
    assert 'jupyterhub_pilot_worker_gpu_percent{worker="w1",gpu="0",model="Tesla T4"} 37.0' in text
