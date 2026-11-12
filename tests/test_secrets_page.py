from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jupyterhub_pilot import secrets_page, spawner
from jupyterhub_pilot import vault_client as vc


class FakeVault:
    """Stands in for VaultClient: paths -> {name: value}, every write stamped with the same time."""

    def __init__(self, stored=None, changed="2026-11-12T10:00:00.5Z"):
        self.stored = dict(stored or {})
        self.changed = changed

    def list_names(self):
        return [p for p in self.stored if "/" not in p]

    def read(self, path):
        return dict(self.stored.get(path, {})), self.changed if path in self.stored else ""

    def write(self, path, data):
        if data:
            self.stored[path] = data
        else:
            self.stored.pop(path, None)


def _handler(vault, admin=True, enabled=True, **form):
    h = secrets_page.SecretsPageHandler()
    h.initialize(lambda: enabled, lambda: vault, {"team_alpha": "10.0.1.5"}.get, {})
    h._hub_users = lambda: [("bob", {"team_alpha": {}})]
    h._hub_groups = lambda: [("team_alpha", {}, 1), ("admins", {}, 1)]
    h.current_user = MagicMock(admin=admin)
    h.get_body_argument = lambda k, default=None: form.get(k, default)
    h.render_template = AsyncMock(return_value="html")
    h.finish = MagicMock()
    h.set_status = MagicMock()
    h.log = MagicMock()
    return h


async def _post(h):
    await secrets_page.SecretsPageHandler.post.__wrapped__(h)
    return h.render_template.call_args.kwargs


@pytest.mark.anyio
async def test_page_adds_a_variable_and_keeps_the_others():
    vault = FakeVault({"bob": {"OLD": "1"}})
    shown = await _post(_handler(vault, target="bob", name="API_KEY", value="s3cret"))
    assert vault.stored["bob"] == {"OLD": "1", "API_KEY": "s3cret"}
    assert shown["users"][0]["keys"] == ["API_KEY", "OLD"]
    assert "s3cret" not in repr(shown)  # names only, in the page and in the message


@pytest.mark.anyio
async def test_page_team_variable_reaches_the_member_unless_they_have_their_own():
    vault = FakeVault({"_team/team_alpha": {"API_KEY": "team", "REGION": "x"}, "bob": {"API_KEY": "own"}})
    shown = await _post(_handler(vault, scope="team", target="team_alpha", name="BUCKET", value="b"))
    assert shown["teams"] == [{"name": "team_alpha", "members": 1, "keys": ["API_KEY", "BUCKET", "REGION"]}]
    assert shown["users"][0]["team_keys"] == ["BUCKET", "REGION"]


@pytest.mark.anyio
async def test_page_deleting_the_last_variable_removes_the_secret():
    vault = FakeVault({"bob": {"my-odd.name": "1"}})  # stored with the vault CLI; this page would not accept the name
    await _post(_handler(vault, target="bob", name="my-odd.name", action="delete"))
    assert vault.stored == {}


@pytest.mark.parametrize(
    "form, why",
    [
        ({"target": "bob", "name": "MY-KEY", "value": "v"}, "not a variable name"),
        ({"target": "bob", "name": "A; rm -rf /", "value": "v"}, "not a variable name"),
        ({"target": "bob", "name": "JUPYTERHUB_API_TOKEN", "value": "v"}, "belong to the Hub"),
        ({"target": "bob", "name": "KEY", "value": ""}, "Enter a value"),
        ({"target": "", "name": "KEY", "value": "v"}, "Enter a user name"),
        ({"target": "..", "name": "KEY", "value": "v"}, "not a valid name"),
        ({"scope": "team", "target": "admins", "name": "KEY", "value": "v"}, "not a team with a worker"),
    ],
)
@pytest.mark.anyio
async def test_page_refuses(form, why):
    vault = FakeVault()
    h = _handler(vault, **form)
    assert why in (await _post(h))["error"]
    h.set_status.assert_called_with(400)
    assert vault.stored == {}


@pytest.mark.anyio
async def test_page_admin_only_and_off_without_vault():
    with pytest.raises(secrets_page.web.HTTPError):
        await _post(_handler(FakeVault(), admin=False, target="bob", name="K", value="v"))
    vault = FakeVault()
    shown = await _post(_handler(vault, enabled=False, target="bob", name="K", value="v"))
    assert shown["off"] and vault.stored == {}


@pytest.mark.anyio
async def test_page_shows_vault_errors_instead_of_failing():
    vault = FakeVault()
    vault.list_names = MagicMock(side_effect=vc.VaultError("Vault refused this"))
    h = _handler(vault)
    await secrets_page.SecretsPageHandler.get.__wrapped__(h)
    assert "Vault refused" in h.render_template.call_args.kwargs["error"]


def test_view_marks_servers_started_before_the_last_change():
    changed = "2026-11-12T10:00:00.123456789Z"
    now = secrets_page._epoch(changed) + 600
    snap = lambda uptime: {"w": {"users": [{"name": "bob", "uptime_s": uptime}]}}  # noqa: E731
    view = lambda uptime, **kw: secrets_page.build_view(  # noqa: E731
        [("bob", {"team_alpha": {}}), ("eve", {})], [("team_alpha", {}, 1)], {"team_alpha": "10.0.1.5"}.get,
        snap(uptime), kw.get("users", {}), kw.get("teams", {}), now,
    )["users"]
    assert view(3600, users={"bob": (["K"], changed)})[0]["restart"] is True
    assert view(3600, teams={"team_alpha": (["K"], changed)})[0]["restart"] is True
    assert view(60, users={"bob": (["K"], changed)})[0]["restart"] is False  # started after the change
    assert view(3600, users={"eve": (["K"], changed)})[1]["restart"] is False  # not running
    assert view(3600, users={"gone": (["K"], changed)})[2] == {
        "username": "gone", "teams": [], "known": False, "keys": ["K"], "team_keys": [], "running": False, "restart": False,
    }


def _client(monkeypatch):
    monkeypatch.setenv("VAULT_ADDR", "http://v:8200/")
    monkeypatch.setenv("VAULT_TOKEN", "t")
    return vc.VaultClient("secret/jupyterhub-pilot")


def _reply(status=200, body=None):
    return MagicMock(status_code=status, ok=status < 400, content=b"x" if body is not None else b"", json=lambda: body)


def test_client_lists_reads_writes_and_deletes(monkeypatch):
    client = _client(monkeypatch)
    with patch.object(vc.requests, "request") as req:
        req.return_value = _reply(body={"data": {"keys": ["alice", "_team/"]}})
        assert client.list_names() == ["alice"]
        assert req.call_args.args == ("GET", "http://v:8200/v1/secret/metadata/jupyterhub-pilot")
        assert req.call_args.kwargs["params"] == {"list": "true"}

        req.return_value = _reply(body={"data": {"data": {"K": "v"}, "metadata": {"created_time": "T"}}})
        assert client.read(vc.secret_path("team", "team_alpha")) == ({"K": "v"}, "T")
        assert req.call_args.args == ("GET", "http://v:8200/v1/secret/data/jupyterhub-pilot/_team/team_alpha")

        req.return_value = _reply(204)
        client.write("alice", {"K": "v"})
        assert req.call_args.args[0] == "POST" and req.call_args.kwargs["json"] == {"data": {"K": "v"}}
        client.write("alice", {})
        assert req.call_args.args == ("DELETE", "http://v:8200/v1/secret/metadata/jupyterhub-pilot/alice")

        req.return_value = _reply(404)
        assert client.read("nobody") == ({}, "") and client.list_names() == []


def test_client_errors_are_readable_and_never_show_the_token(monkeypatch):
    client = _client(monkeypatch)
    with patch.object(vc.requests, "request", return_value=_reply(403)):
        with pytest.raises(vc.VaultError, match="policy"):
            client.write("alice", {"K": "v"})
    with patch.object(vc.requests, "request", side_effect=vc.requests.exceptions.ConnectionError("t down")):
        with pytest.raises(vc.VaultError, match="Cannot reach Vault at http://v:8200") as err:
            client.list_names()
    assert "down" not in str(err.value)


def test_secret_path_cannot_leave_the_base():
    assert vc.secret_path("user", "a/../b") == "a%2F..%2Fb"
    assert vc.secret_path("user", "first.last@x.org") == "first.last%40x.org"
    for bad in ("", ".", ".."):
        with pytest.raises(vc.VaultError):
            vc.secret_path("user", bad)


@pytest.mark.anyio
async def test_spawner_merges_team_and_user_secrets_and_skips_bad_names(monkeypatch):
    monkeypatch.setattr(spawner, "hub_settings", {"vault_enabled": True})
    stored = {"_team/team_alpha": {"API_KEY": "team", "REGION": "x"}, "bob": {"API_KEY": "own", "BAD NAME; id": "1"}}
    monkeypatch.setattr(spawner, "VaultClient", lambda secret_base_path: MagicMock(get_user_secrets=lambda p: dict(stored.get(p, {}))))
    inst = spawner.CustomSpawner()
    inst.user.name = "bob"
    inst._team = lambda: "team_alpha"
    inst.user.groups = []
    inst.environment = {}
    await inst._prepare_spawn()
    assert inst.environment == {"API_KEY": "own", "REGION": "x", "JUPYTERHUB_PILOT_SECRET_KEYS": "API_KEY,REGION"}
