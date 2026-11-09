import json
import os
import sys

# Add root folder to sys.path so the package imports from the checkout
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Import builtins config_mock from conftest/builtins
import builtins

config_mock = builtins.get_config()  # type: ignore[attr-defined]

# We need to make sure hub_settings.json exists or is mocked during import of jupyterhub_config.
# Since jupyterhub_config.py reads from hub_settings.json, let's mock open specifically for it or
# verify that hub_settings.json is parsed successfully.
# Let's see: the repository already has a hub_settings.json with dummy values.
# To prevent tests from failing if hub_settings.json changes or is missing, we patch builtins.open during import.
dummy_hub_settings = {
    "hub_ip": "127.0.0.1",
    "hub_port": 8000,
    "hub_bind_url": "http://127.0.0.1:8081",
    "proxy_api_url": "http://127.0.0.1:5432",
    "hub_api_url": "http://127.0.0.1:8081/hub/api",
    "hosted_domain": "test-team.com",
    "admin_users": ["admin_user"],
    "mapping_file": "user_mapping.json",
}

# Simple manual patch of builtins.open for import
orig_open = builtins.open


def custom_open(file, *args, **kwargs):
    if "hub_settings.json" in str(file):
        from io import StringIO

        return StringIO(json.dumps(dummy_hub_settings))
    return orig_open(file, *args, **kwargs)


builtins.open = custom_open
try:
    from jupyterhub_pilot import jupyterhub_config  # noqa: F401
finally:
    builtins.open = orig_open


def test_jupyterhub_config_loaded():
    # Verify values set on config mock during import
    assert config_mock.JupyterHub.ip == "127.0.0.1"
    assert config_mock.JupyterHub.port == 8000
    assert config_mock.GoogleOAuthenticator.hosted_domain == "test-team.com"
    assert config_mock.Authenticator.admin_users == {"admin_user"}
    assert config_mock.JupyterHub.admin_access is False
    assert len(config_mock.JupyterHub.extra_handlers) > 0
    # Cookie flags must sit in tornado_settings; JupyterHub ignores a top-level cookie_options.
    assert config_mock.JupyterHub.tornado_settings["cookie_options"]["secure"] is True


def test_test_password_switches_to_shared_password_login(monkeypatch):
    import importlib

    from unittest.mock import MagicMock

    fresh = MagicMock()
    monkeypatch.setattr(builtins, "get_config", lambda: fresh, raising=False)
    monkeypatch.setenv("JUPYTERPILOT_TEST_PASSWORD", "test-password-123")
    monkeypatch.setenv("JUPYTERPILOT_TEST_ADMIN_PASSWORD", "a" * 32)
    builtins.open = custom_open
    try:
        importlib.reload(jupyterhub_config)
    finally:
        builtins.open = orig_open
    assert fresh.JupyterHub.authenticator_class == "shared-password"
    assert fresh.SharedPasswordAuthenticator.user_password == "test-password-123"
    assert fresh.SharedPasswordAuthenticator.admin_password == "a" * 32


def _reload_with(monkeypatch, settings):
    import importlib
    from io import StringIO
    from unittest.mock import MagicMock

    fresh = MagicMock()
    monkeypatch.setattr(builtins, "get_config", lambda: fresh, raising=False)

    def fake_open(file, *args, **kwargs):
        if "hub_settings.json" in str(file):
            return StringIO(json.dumps(settings))
        return orig_open(file, *args, **kwargs)

    builtins.open = fake_open
    try:
        importlib.reload(jupyterhub_config)
    finally:
        builtins.open = orig_open
    return fresh


def test_idle_cull_registers_culler_service(monkeypatch):
    fresh = _reload_with(monkeypatch, {**dummy_hub_settings, "idle_cull": {"timeout": 10800, "every": 300}})
    (service,) = fresh.JupyterHub.services
    assert service["name"] == "idle-culler"
    assert "--timeout=10800" in service["command"]
    assert "--cull-every=300" in service["command"]
    assert not any("cull-users" in arg for arg in service["command"])
    (role,) = fresh.JupyterHub.load_roles
    assert role["services"] == ["idle-culler"]
    assert "delete:servers" in role["scopes"]
    assert "admin:users" not in role["scopes"]


def test_idle_cull_off_when_missing_or_null(monkeypatch):
    for settings in (dummy_hub_settings, {**dummy_hub_settings, "idle_cull": None}):
        fresh = _reload_with(monkeypatch, settings)
        # MagicMock auto-creates attributes; check nothing was assigned.
        assert "services" not in vars(fresh.JupyterHub)
