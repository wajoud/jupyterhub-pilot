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


def _browser(admin, groups):
    b = mh.BrowserWebSocketHandler.__new__(mh.BrowserWebSocketHandler)
    b.initialize(worker_ip_for_group=MAPPING.get)
    b.current_user = SimpleNamespace(
        admin=admin, groups=[SimpleNamespace(name=g) for g in groups]
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
