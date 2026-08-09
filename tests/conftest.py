import os
import sys

# Ensure the root of the project is on sys.path so that the root-level
# `jupyterhub_pilot/` package and `spawner.py` are always importable in tests,
# even without a `pip install -e .` step.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from unittest.mock import MagicMock


# 1. Setup Mock for jupyterhub and oauthenticator before imports happen
class MockSpawner:
    def __init__(self, *args, **kwargs):
        self.user = MagicMock()
        self.user.name = "test_user"
        self.user.groups = []
        self.server = MagicMock()
        self.server.base_url = "/user/test_user/"
        self.log = MagicMock()
        self.ip = ""
        self.port = 0

    def get_env(self):
        return {"JUPYTERHUB_API_KEY": "test_api_key"}


class MockBaseHandler:
    def __init__(self, *args, **kwargs):
        self.request = MagicMock()
        self.current_user = None
        self.status_code = None
        self.finished_content = None
        self.redirect_url = None

    def redirect(self, url):
        self.redirect_url = url

    def set_status(self, code):
        self.status_code = code

    def finish(self, content):
        self.finished_content = content


# Inject mocks into sys.modules
jupyterhub_mock = MagicMock()
jupyterhub_mock.spawner = MagicMock()
jupyterhub_mock.spawner.Spawner = MockSpawner
jupyterhub_mock.handlers = MagicMock()
jupyterhub_mock.handlers.BaseHandler = MockBaseHandler

sys.modules["jupyterhub"] = jupyterhub_mock
sys.modules["jupyterhub.spawner"] = jupyterhub_mock.spawner
sys.modules["jupyterhub.handlers"] = jupyterhub_mock.handlers
sys.modules["oauthenticator"] = MagicMock()
sys.modules["oauthenticator.google"] = MagicMock()
sys.modules["paramiko"] = MagicMock()

# Inject get_config() into builtins so jupyterhub_config.py can find it
import builtins

config_mock = MagicMock()
builtins.get_config = lambda: config_mock
