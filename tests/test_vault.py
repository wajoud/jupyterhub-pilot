import threading
from unittest.mock import MagicMock, patch

from jupyterhub_pilot import vault_client as vc


def test_renew_self_posts_to_vault_with_the_token(monkeypatch):
    monkeypatch.setenv("VAULT_ADDR", "http://10.0.0.9:8200/")
    monkeypatch.setenv("VAULT_TOKEN", "t0ken")
    with patch.object(vc.requests, "post", return_value=MagicMock(ok=True)) as post:
        assert vc.VaultClient().renew_self() is True
    assert post.call_args.args[0] == "http://10.0.0.9:8200/v1/auth/token/renew-self"
    assert post.call_args.kwargs["headers"] == {"X-Vault-Token": "t0ken"}


def test_renew_self_reports_refusal_and_outage_without_raising(monkeypatch):
    monkeypatch.setenv("VAULT_ADDR", "http://v:8200")
    monkeypatch.setenv("VAULT_TOKEN", "t")
    with patch.object(vc.requests, "post", return_value=MagicMock(ok=False, status_code=403)):
        assert vc.VaultClient().renew_self() is False
    with patch.object(vc.requests, "post", side_effect=vc.requests.exceptions.ConnectionError("down")):
        assert vc.VaultClient().renew_self() is False


def test_renewer_does_nothing_without_vault_settings(monkeypatch):
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    before = threading.active_count()
    vc.start_renewer()
    assert threading.active_count() == before
