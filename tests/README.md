# 🧪 JupyterHub-Pilot: Testing Subsystem

This directory contains the complete unit test suite for JupyterHub-Pilot's hub integrations — covering the SSH spawner lifecycle, SQLite session state, 2-tier RBAC, security handlers, and crash-recovery flows. All tests run without any real SSH connections, JupyterHub instance, or database file.

---

## 🏗️ Mocking Architecture

Since JupyterHub and its dependencies (`paramiko`, Google OAuthenticator, etc.) require real infrastructure to load, the suite uses a fully mocked runtime namespace:

### `conftest.py` — Global Mock Bootstrap
- **`sys.modules` injection**: Patches `jupyterhub.spawner`, `jupyterhub.handlers`, `oauthenticator.google`, and `paramiko` before any test module is imported — preventing `ModuleNotFoundError` during collection.
- **`MockSpawner`**: A minimal base-spawner stub exposing `user`, `server`, `log`, `ip`, `port`, and `get_env()`. `CustomSpawner` subclasses this transparently.
- **`MockBaseHandler`**: A stub for the JupyterHub `BaseHandler` used by `BlockOtherUsersHandler` in `jupyterhub_config.py`.
- **`get_config()` builtin**: Injected into `builtins` so `jupyterhub_config.py` can be safely imported without a running hub process.

---

## 📁 Test Files

### [`test_spawner.py`](file:///Users/wajoud/projects/Github/JupyterHub-Pilot/tests/test_spawner.py) — SSH Spawner Lifecycle
Tests for the core `CustomSpawner` SSH spawner in [`spawner.py`](file:///Users/wajoud/projects/Github/JupyterHub-Pilot/spawner.py).

| Test | What it validates |
|---|---|
| `test_load_mapping` | `_load_mapping_fallback()` parses JSON correctly |
| `test_load_mapping_failure` | Logs an error and re-raises on file read failure |
| `test_start_no_groups` | Raises `RuntimeError` when user has no groups |
| `test_start_group_not_mapped` | Raises `RuntimeError` when group is absent from all sources |
| `test_start_success` | Full happy-path spawn: verifies IP, port, SSH call count |
| `test_start_port_in_use_cleanup` | Triggers `fuser -k` when `netstat` reports an occupied port |
| `test_start_oom_warning` | Logs a warning when OOM script setup returns non-zero exit code |
| `test_stop` | Verifies `kill -15` cleanup command and SSH close |
| `test_poll_running` | Returns `None` when the remote process is alive |
| `test_poll_stopped` | Returns `0` when the remote process is gone |
| `test_poll_exception` | Returns `0` gracefully on SSH failure |

### [`test_spawner_lifecycle.py`](file:///Users/wajoud/projects/Github/JupyterHub-Pilot/tests/test_spawner_lifecycle.py) — RBAC, SessionStore & Crash Recovery
Tests for the new infrastructure added in Task 1.

**`TestRBACManager`** — 2-tier role enforcement ([`admin.py`](file:///Users/wajoud/projects/Github/JupyterHub-Pilot/jupyterhub_pilot/admin.py)):

| Test | What it validates |
|---|---|
| `test_admin_is_detected` | `is_admin()` returns `True` for `user.admin = True` |
| `test_regular_user_is_not_admin` | Returns `False` for plain users |
| `test_missing_admin_attr_defaults_false` | Safe on objects without `.admin` attribute |
| `test_get_role_admin` | Returns `"admin"` for admins |
| `test_get_role_user` | Returns `"user"` for regular users |
| `test_admin_can_act_on_any_user` | No exception for admin cross-user action |
| `test_user_can_act_on_own_session` | No exception for self-action |
| `test_user_blocked_on_other_session` | `PermissionError` for cross-user action by non-admin |

**`TestSessionStore`** — SQLite state wrapper ([`session_store.py`](file:///Users/wajoud/projects/Github/JupyterHub-Pilot/jupyterhub_pilot/session_store.py)):

| Test | What it validates |
|---|---|
| `test_ping_returns_true_after_init` | `ping()` succeeds after `init_db()` |
| `test_ping_returns_false_on_bad_path` | `ping()` returns `False` gracefully on bad path |
| `test_get_mapping_not_found_returns_none` | Returns `None` for unknown team |
| `test_set_and_get_mapping_round_trip` | `set_mapping` → `get_mapping` full round-trip |
| `test_set_mapping_overwrite` | Second write with same team key replaces the row |
| `test_set_and_get_session_round_trip` | Full session write and read |
| `test_set_session_partial_update` | Partial `set_session` only updates specified fields |
| `test_clear_session_removes_row` | Row is deleted after `clear_session` |
| `test_clear_session_nonexistent_is_safe` | No error clearing a non-existent session |

**`TestSpawnerStart`** — `start()` with SQLite and RBAC:

| Test | What it validates |
|---|---|
| `test_start_success_returns_ip_port` | Returns `(ip, port)` tuple |
| `test_start_writes_session_to_store` | SQLite row written with `status=running` after spawn |
| `test_start_pre_hook_called` | `pre_spawn_hook()` awaited before SSH |
| `test_start_port_in_use_triggers_fuser` | `fuser` executed when port is occupied |
| `test_start_no_groups_raises` | `RuntimeError` with no groups |
| `test_start_missing_hub_api_url_raises` | `RuntimeError` when `hub_api_url` absent |
| `test_start_ssh_unreachable_propagates` | SSH exception propagates after logging |

**`TestSpawnerStop`** — `stop()` with crash-safe SQLite reads:

| Test | What it validates |
|---|---|
| `test_stop_sends_sigterm_and_sigkill` | Command contains `kill -15`, `sleep 5`, `kill -9` |
| `test_stop_removes_session_from_store` | SQLite row deleted after stop |
| `test_stop_post_hook_called` | `post_stop_hook()` awaited |
| `test_stop_no_session_logs_warning` | Logs warning and exits cleanly without crashing |
| `test_stop_ssh_error_does_not_crash_hub` | SSH exception caught; hub process survives |

**`TestSpawnerPoll`** — `poll()` reads session from SQLite:

| Test | What it validates |
|---|---|
| `test_poll_running_returns_none` | Returns `None` when process is alive |
| `test_poll_stopped_returns_zero` | Returns `0` when process is gone |
| `test_poll_no_session_returns_zero` | Returns `0` without a SQLite session row |
| `test_poll_ssh_exception_returns_zero` | Returns `0` on SSH failure |
| `test_poll_stopped_updates_store_status` | Updates `status=stopped` in SQLite |

**`TestSpawnerClearState`** — `clear_state()`:

| Test | What it validates |
|---|---|
| `test_clear_state_removes_session_from_store` | SQLite row deleted |
| `test_clear_state_resets_instance_vars` | `ip`, `port`, `_pid_file`, `_session_meta` zeroed |

**`TestJsonFallback`** — Graceful fallback when SQLite is down:

| Test | What it validates |
|---|---|
| `test_get_server_info_falls_back_to_json` | Uses JSON mapping when `ping()` returns `False` |
| `test_get_server_info_raises_if_group_missing_everywhere` | `RuntimeError` if group missing in both sources |

### [`test_config.py`](file:///Users/wajoud/projects/Github/JupyterHub-Pilot/tests/test_config.py) — Hub Config & Security Handlers
Tests for [`jupyterhub_config.py`](file:///Users/wajoud/projects/Github/JupyterHub-Pilot/jupyterhub_config.py):

| Test | What it validates |
|---|---|
| `test_jupyterhub_config_loaded` | Hub IP, port, hosted domain, admin list, admin_access=False |
| `test_block_handler_no_user` | Unauthenticated requests redirect to `/hub/login` |
| `test_block_handler_allow_self` | Authenticated user accessing own `/user/<name>/` path is allowed |
| `test_block_handler_deny_other_user` | Cross-user path access returns `403 Forbidden` |
| `test_block_handler_invalid_path` | Malformed path (`/user/`) is blocked |

---

## 🚀 Running the Tests

```bash
# All tests (54 total)
pytest -v tests/

# Individual suites
pytest -v tests/test_spawner.py           # SSH spawner lifecycle (11 tests)
pytest -v tests/test_spawner_lifecycle.py  # RBAC + SessionStore + crash recovery (38 tests)
pytest -v tests/test_config.py            # Hub config & security handlers (5 tests)
```

> **Requirements**: `pytest` and `anyio` (included in `pip install -e .[dev]`). No running JupyterHub, SSH, or SQLite DB needed.
