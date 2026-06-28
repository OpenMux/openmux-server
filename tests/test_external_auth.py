"""Tests for external authentication (auth_manager._external_authenticate and related).

All external process calls are mocked via unittest.mock so no real helper binary
is needed.
"""

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openmux.server.auth_manager import AuthManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_proc(returncode: int = 0, stdout: Any = None, stderr: bytes = b""):
    """Return a mock subprocess.CompletedProcess-like object."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = json.dumps(stdout).encode() if isinstance(stdout, dict) else (stdout or b"")
    proc.stderr = stderr
    return proc


def _auth(cfg: dict) -> AuthManager:
    """Build an AuthManager with external_auth config."""
    return AuthManager({"external_auth": cfg})


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestLoadExtAuthConfig:
    def test_defaults_when_absent(self):
        am = AuthManager({})
        assert am._ext_auth_enabled is False
        assert am._ext_auth_service == "openmux"
        assert am._ext_auth_helper is None
        assert am._ext_auth_timeout == 10.0
        assert am._ext_auth_allow_root is False
        assert am._ext_auth_allowed_users is None
        assert am._ext_auth_default_permission is None

    def test_external_auth_key(self):
        am = _auth({"enabled": True, "service": "myapp", "timeout": 5, "default_permission": "read-only"})
        assert am._ext_auth_enabled is True
        assert am._ext_auth_service == "myapp"
        assert am._ext_auth_timeout == 5.0
        assert am._ext_auth_default_permission == "read-only"

    def test_helper_string(self):
        am = _auth({"helper": "/usr/bin/my_helper"})
        assert am._ext_auth_helper == "/usr/bin/my_helper"

    def test_helper_list(self):
        am = _auth({"helper": ["sudo", "/usr/bin/my_helper"]})
        assert am._ext_auth_helper == ["sudo", "/usr/bin/my_helper"]

    def test_deprecated_pam_key_migrated(self):
        am = AuthManager({"pam": {"enabled": True, "service_name": "ssh"}})
        assert am._ext_auth_enabled is True
        assert am._ext_auth_service == "ssh"

    def test_deprecated_pam_key_logs_warning(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="openmux.auth"):
            AuthManager({"pam": {"enabled": True}})
        assert any("deprecated" in r.message.lower() for r in caplog.records)

    def test_external_auth_takes_precedence_over_pam(self):
        am = AuthManager({
            "external_auth": {"enabled": True, "service": "ext"},
            "pam": {"enabled": False, "service_name": "pam"},
        })
        assert am._ext_auth_service == "ext"

    def test_allowed_users_list(self):
        am = _auth({"allowed_users": ["alice", "bob"]})
        assert am._ext_auth_allowed_users == {"alice", "bob"}

    def test_allowed_users_absent(self):
        am = _auth({})
        assert am._ext_auth_allowed_users is None

    def test_group_mapping(self):
        am = _auth({"groups": {"admin_group": "wheel", "write_group": "staff", "read_group": "guests"}})
        assert am._ext_auth_groups["admin_group"] == "wheel"
        assert am._ext_auth_groups["write_group"] == "staff"
        assert am._ext_auth_groups["read_group"] == "guests"

    def test_update_config_reloads(self):
        am = _auth({"enabled": False})
        asyncio.get_event_loop().run_until_complete(
            am.update_config({"external_auth": {"enabled": True, "service": "new"}})
        )
        assert am._ext_auth_enabled is True
        assert am._ext_auth_service == "new"

    def test_update_config_clears_groups_cache(self):
        am = _auth({"enabled": True})
        am._ext_auth_groups_cache["alice"] = {"groups": ["admin"], "expires": time.time() + 300}
        asyncio.get_event_loop().run_until_complete(am.update_config({"external_auth": {}}))
        assert am._ext_auth_groups_cache == {}


# ---------------------------------------------------------------------------
# _external_authenticate
# ---------------------------------------------------------------------------

HELPER_PATH = "/usr/local/bin/openmux_pam_helper"


@pytest.fixture
def am_ext():
    """AuthManager with external_auth enabled, helper as string."""
    return _auth({"enabled": True, "helper": HELPER_PATH, "service": "openmux"})


class TestExternalAuthenticate:

    def _run(self, proc_mock, am=None, username="alice", password="pw"):
        if am is None:
            am = _auth({"enabled": True, "helper": HELPER_PATH, "service": "openmux"})
        with patch("subprocess.run", return_value=proc_mock) as mock_run, \
             patch("os.path.isfile", return_value=True), \
             patch("shutil.which", return_value=None):
            result = am._external_authenticate(username, password)
        return result, mock_run

    def test_success_exit0_ok_true(self):
        proc = _make_proc(0, {"ok": True, "groups": []})
        result, mock_run = self._run(proc)
        assert result is True

    def test_failure_exit1(self):
        proc = _make_proc(1, {"ok": False})
        result, _ = self._run(proc)
        assert result is False

    def test_failure_ok_false_despite_exit0(self):
        proc = _make_proc(0, {"ok": False})
        result, _ = self._run(proc)
        assert result is False

    def test_groups_cached_on_success(self):
        am = _auth({"enabled": True, "helper": HELPER_PATH, "service": "openmux"})
        proc = _make_proc(0, {"ok": True, "groups": ["openmux_admin", "users"]})
        with patch("subprocess.run", return_value=proc), \
             patch("os.path.isfile", return_value=True), \
             patch("shutil.which", return_value=None):
            am._external_authenticate("alice", "pw")
        assert "alice" in am._ext_auth_groups_cache
        assert am._ext_auth_groups_cache["alice"]["groups"] == ["openmux_admin", "users"]

    def test_groups_not_cached_on_failure(self):
        am = _auth({"enabled": True, "helper": HELPER_PATH, "service": "openmux"})
        proc = _make_proc(1, {"ok": False, "groups": ["openmux_admin"]})
        with patch("subprocess.run", return_value=proc), \
             patch("os.path.isfile", return_value=True), \
             patch("shutil.which", return_value=None):
            am._external_authenticate("alice", "pw")
        assert "alice" not in am._ext_auth_groups_cache

    def test_stdin_contains_username_and_password(self):
        proc = _make_proc(0, {"ok": True})
        am = _auth({"enabled": True, "helper": HELPER_PATH, "service": "openmux"})
        with patch("subprocess.run", return_value=proc) as mock_run, \
             patch("os.path.isfile", return_value=True), \
             patch("shutil.which", return_value=None):
            am._external_authenticate("alice", "secret")
        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs["input"] == b"alice\nsecret\n"

    def test_service_appended_as_argument(self):
        proc = _make_proc(0, {"ok": True})
        am = _auth({"enabled": True, "helper": HELPER_PATH, "service": "myservice"})
        with patch("subprocess.run", return_value=proc) as mock_run, \
             patch("os.path.isfile", return_value=True), \
             patch("shutil.which", return_value=None):
            am._external_authenticate("alice", "pw")
        cmd = mock_run.call_args.args[0]
        assert cmd[-1] == "myservice"
        assert HELPER_PATH in cmd

    def test_list_helper_passed_directly(self):
        proc = _make_proc(0, {"ok": True})
        am = _auth({"enabled": True, "helper": ["sudo", HELPER_PATH], "service": "svc"})
        with patch("subprocess.run", return_value=proc) as mock_run, \
             patch("os.path.isfile", return_value=True), \
             patch("shutil.which", return_value=None):
            am._external_authenticate("alice", "pw")
        cmd = mock_run.call_args.args[0]
        assert cmd[0] == "sudo"
        assert cmd[1] == HELPER_PATH
        assert cmd[-1] == "svc"

    def test_helper_not_found_returns_false(self):
        am = _auth({"enabled": True, "helper": "/nonexistent/helper"})
        with patch("os.path.isfile", return_value=False), \
             patch("shutil.which", return_value=None):
            result = am._external_authenticate("alice", "pw")
        assert result is False

    def test_auto_resolve_from_path(self):
        proc = _make_proc(0, {"ok": True})
        am = _auth({"enabled": True})  # no helper configured
        with patch("subprocess.run", return_value=proc) as mock_run, \
             patch("os.path.isfile", return_value=True), \
             patch("shutil.which", return_value="/usr/bin/openmux-pam-helper"):
            result = am._external_authenticate("alice", "pw")
        assert result is True
        cmd = mock_run.call_args.args[0]
        assert "/usr/bin/openmux-pam-helper" in cmd

    def test_timeout_returns_false(self):
        import subprocess
        am = _auth({"enabled": True, "helper": HELPER_PATH, "timeout": 1})
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=HELPER_PATH, timeout=1)), \
             patch("os.path.isfile", return_value=True), \
             patch("shutil.which", return_value=None):
            result = am._external_authenticate("alice", "pw")
        assert result is False

    def test_subprocess_exception_returns_false(self):
        am = _auth({"enabled": True, "helper": HELPER_PATH})
        with patch("subprocess.run", side_effect=OSError("permission denied")), \
             patch("os.path.isfile", return_value=True), \
             patch("shutil.which", return_value=None):
            result = am._external_authenticate("alice", "pw")
        assert result is False

    def test_root_denied_by_default(self):
        am = _auth({"enabled": True, "helper": HELPER_PATH, "allow_root": False})
        with patch("subprocess.run") as mock_run, \
             patch("os.path.isfile", return_value=True):
            result = am._external_authenticate("root", "pw")
        mock_run.assert_not_called()
        assert result is False

    def test_root_allowed_when_configured(self):
        proc = _make_proc(0, {"ok": True})
        am = _auth({"enabled": True, "helper": HELPER_PATH, "allow_root": True})
        with patch("subprocess.run", return_value=proc), \
             patch("os.path.isfile", return_value=True), \
             patch("shutil.which", return_value=None):
            result = am._external_authenticate("root", "pw")
        assert result is True

    def test_allowed_users_blocks_unlisted(self):
        am = _auth({"enabled": True, "helper": HELPER_PATH, "allowed_users": ["alice"]})
        with patch("subprocess.run") as mock_run, \
             patch("os.path.isfile", return_value=True):
            result = am._external_authenticate("bob", "pw")
        mock_run.assert_not_called()
        assert result is False

    def test_allowed_users_permits_listed(self):
        proc = _make_proc(0, {"ok": True})
        am = _auth({"enabled": True, "helper": HELPER_PATH, "allowed_users": ["alice", "bob"]})
        with patch("subprocess.run", return_value=proc), \
             patch("os.path.isfile", return_value=True), \
             patch("shutil.which", return_value=None):
            result = am._external_authenticate("alice", "pw")
        assert result is True

    def test_malformed_json_stdout_still_uses_exit_code(self):
        proc = _make_proc(0)
        proc.stdout = b"not json {"
        am = _auth({"enabled": True, "helper": HELPER_PATH})
        with patch("subprocess.run", return_value=proc), \
             patch("os.path.isfile", return_value=True), \
             patch("shutil.which", return_value=None):
            result = am._external_authenticate("alice", "pw")
        assert result is True  # exit 0 is enough


# ---------------------------------------------------------------------------
# _ext_auth_group_permissions
# ---------------------------------------------------------------------------

class TestExtAuthGroupPermissions:

    def _am_with_cache(self, username, groups, expired=False):
        am = _auth({"enabled": True})
        am._ext_auth_groups_cache[username] = {
            "groups": groups,
            "expires": time.time() + (-1 if expired else 300),
        }
        return am

    def test_admin_group_match(self):
        am = self._am_with_cache("alice", ["openmux_admin"])
        assert am._ext_auth_group_permissions("alice") == "admin"

    def test_write_group_match(self):
        am = self._am_with_cache("alice", ["openmux_write"])
        assert am._ext_auth_group_permissions("alice") == "read-write"

    def test_read_group_match(self):
        am = self._am_with_cache("alice", ["openmux_read"])
        assert am._ext_auth_group_permissions("alice") == "read-only"

    def test_admin_takes_precedence_over_write(self):
        am = self._am_with_cache("alice", ["openmux_write", "openmux_admin"])
        assert am._ext_auth_group_permissions("alice") == "admin"

    def test_no_matching_group_returns_none_without_default(self):
        am = self._am_with_cache("bob", ["staff", "users"])
        with patch("pwd.getpwnam", side_effect=KeyError), \
             patch("grp.getgrall", return_value=[]):
            assert am._ext_auth_group_permissions("bob") is None

    def test_expired_cache_skipped(self):
        am = self._am_with_cache("alice", ["openmux_admin"], expired=True)
        with patch("pwd.getpwnam", side_effect=KeyError), \
             patch("grp.getgrall", return_value=[]):
            result = am._ext_auth_group_permissions("alice")
        assert result is None  # expired cache not used, Unix lookup also fails

    def test_default_permission_fallback(self):
        am = _auth({"enabled": True, "default_permission": "read-only"})
        with patch("pwd.getpwnam", side_effect=KeyError), \
             patch("grp.getgrall", return_value=[]):
            result = am._ext_auth_group_permissions("unknown_user")
        assert result == "read-only"

    def test_no_default_returns_none(self):
        am = _auth({"enabled": True})
        with patch("pwd.getpwnam", side_effect=KeyError), \
             patch("grp.getgrall", return_value=[]):
            assert am._ext_auth_group_permissions("unknown_user") is None

    def test_custom_group_names(self):
        am = _auth({
            "enabled": True,
            "groups": {"admin_group": "wheel", "write_group": "staff", "read_group": "guests"},
        })
        am._ext_auth_groups_cache["charlie"] = {
            "groups": ["wheel"],
            "expires": time.time() + 300,
        }
        assert am._ext_auth_group_permissions("charlie") == "admin"


# ---------------------------------------------------------------------------
# get_user_permissions integration
# ---------------------------------------------------------------------------

class TestGetUserPermissions:

    def test_static_user_takes_precedence(self):
        am = AuthManager({
            "users": [{"username": "admin", "password_hash": "x", "permissions": "admin"}],
            "external_auth": {"enabled": True},
        })
        # admin is a static user — external auth groups should not be consulted
        assert am.get_user_permissions("admin") == "admin"

    def test_static_user_without_explicit_permissions_defaults_read_write(self):
        am = AuthManager({
            "users": [{"username": "admin", "password_hash": "x"}],
        })
        assert am.get_user_permissions("admin") == "read-write"

    def test_external_user_gets_permission_from_cached_groups(self):
        am = AuthManager({
            "users": [{"username": "admin", "password_hash": "x", "permissions": "admin"}],
            "external_auth": {"enabled": True},
        })
        am._ext_auth_groups_cache["alice"] = {
            "groups": ["openmux_admin"],
            "expires": time.time() + 300,
        }
        assert am.get_user_permissions("alice") == "admin"

    def test_external_user_no_group_no_default_returns_none(self):
        am = AuthManager({
            "users": [{"username": "admin", "password_hash": "x"}],
            "external_auth": {"enabled": True},
        })
        with patch("pwd.getpwnam", side_effect=KeyError), \
             patch("grp.getgrall", return_value=[]):
            assert am.get_user_permissions("bob") is None

    def test_external_user_gets_default_permission(self):
        am = AuthManager({
            "external_auth": {"enabled": True, "default_permission": "read-write"},
        })
        with patch("pwd.getpwnam", side_effect=KeyError), \
             patch("grp.getgrall", return_value=[]):
            assert am.get_user_permissions("bob") == "read-write"


# ---------------------------------------------------------------------------
# Full authenticate() flow
# ---------------------------------------------------------------------------

class TestAuthenticateFlow:

    def test_local_user_bypasses_external_auth(self):
        import hashlib
        pw_hash = hashlib.sha256(b"secret").hexdigest()
        am = AuthManager({
            "users": [{"username": "admin", "password_hash": pw_hash, "permissions": "admin"}],
            "external_auth": {"enabled": True, "helper": HELPER_PATH},
        })
        with patch("subprocess.run") as mock_run, \
             patch("os.path.isfile", return_value=True):
            result = am.authenticate("admin", "secret")
        mock_run.assert_not_called()
        assert result is True

    def test_external_auth_called_when_local_fails(self):
        import hashlib
        pw_hash = hashlib.sha256(b"secret").hexdigest()
        am = AuthManager({
            "users": [{"username": "admin", "password_hash": pw_hash}],
            "external_auth": {"enabled": True, "helper": HELPER_PATH},
        })
        proc = _make_proc(0, {"ok": True, "groups": ["openmux_admin"]})
        with patch("subprocess.run", return_value=proc), \
             patch("os.path.isfile", return_value=True), \
             patch("shutil.which", return_value=None):
            result = am.authenticate("alice", "alicepw")
        assert result is True

    def test_external_auth_not_called_when_disabled(self):
        am = AuthManager({
            "external_auth": {"enabled": False, "helper": HELPER_PATH},
        })
        with patch("subprocess.run") as mock_run, \
             patch("os.path.isfile", return_value=True):
            result = am.authenticate("alice", "pw")
        mock_run.assert_not_called()
        assert result is False

    def test_auth_cache_prevents_duplicate_helper_calls(self):
        am = AuthManager({
            "external_auth": {"enabled": True, "helper": HELPER_PATH},
        })
        proc = _make_proc(0, {"ok": True})
        with patch("subprocess.run", return_value=proc) as mock_run, \
             patch("os.path.isfile", return_value=True), \
             patch("shutil.which", return_value=None):
            am.authenticate("alice", "pw")
            am.authenticate("alice", "pw")  # second call: should use cache
        assert mock_run.call_count == 1
