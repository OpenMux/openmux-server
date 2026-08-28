"""Tests for OpenMuxServer's fail-safe security-policy load/reload behavior.

Covers the two documented failure modes in `_refresh_security_policy`:
  - Invalid security.yaml at initial startup: hard-fail (raise), server must
    not come up with an unknown/ambiguous policy.
  - Invalid security.yaml on a later reload: log loudly and keep the
    last-known-good policy in effect, rather than crashing a running server.
"""

import textwrap

import pytest

from openmux.server.main import OpenMuxServer
from openmux.server.security_policy import SecurityPolicyError


def _write_isolated_config(tmp_path) -> str:
    """Minimal, adapter-free server config (see test_reload_signals.py for why
    this must never be the repo's real config/server.yaml)."""
    auth_path = tmp_path / "authentication.yaml"
    auth_path.write_text(
        textwrap.dedent(
            """
            users:
              - username: admin
                password_hash: 5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8
                permissions: admin
            """
        )
    )
    cfg_path = tmp_path / "server.yaml"
    cfg_path.write_text(
        textwrap.dedent(
            """
            server:
              id: test
            logging:
              level: WARNING
            """
        )
    )
    return str(cfg_path)


def test_startup_hard_fails_on_invalid_security_yaml(tmp_path):
    cfg_path = _write_isolated_config(tmp_path)
    (tmp_path / "security.yaml").write_text(
        textwrap.dedent(
            """
            adapters:
              allowed: ["not_a_real_adapter_type"]
            """
        )
    )

    with pytest.raises(SecurityPolicyError):
        OpenMuxServer(cfg_path, log_level="DEBUG")


def test_reload_keeps_last_known_good_policy_on_invalid_security_yaml(tmp_path):
    cfg_path = _write_isolated_config(tmp_path)
    security_path = tmp_path / "security.yaml"
    security_path.write_text(
        textwrap.dedent(
            """
            adapters:
              disabled: ["serial"]
            """
        )
    )

    server = OpenMuxServer(cfg_path, log_level="DEBUG")
    good_policy = server.security_policy
    assert not good_policy.is_adapter_allowed(adapter_type="serial")

    # Corrupt security.yaml, then simulate a config reload.
    security_path.write_text(
        textwrap.dedent(
            """
            adapters:
              allowed: ["not_a_real_adapter_type"]
            """
        )
    )

    server._reload_config_from_disk()

    assert server.security_policy is good_policy
    assert not server.security_policy.is_adapter_allowed(adapter_type="serial")


def test_console_manager_receives_policy_and_follows_reloads(tmp_path):
    """issue #58: the console ladder reads security_policy, and a soft-reload
    path update (access_default allow -> deny) must be visible to it without
    restarting any adapter. Also covers the invalid-file last-known-good
    path: the console manager must never see the invalid policy."""
    cfg_path = _write_isolated_config(tmp_path)
    security_path = tmp_path / "security.yaml"
    security_path.write_text("access_default: allow")

    server = OpenMuxServer(cfg_path, log_level="DEBUG")
    assert server.console_manager.security_policy is server.security_policy
    assert server.console_manager.security_policy.get_access_default() == "allow"

    security_path.write_text("access_default: deny")
    server._reload_config_from_disk()
    assert server.console_manager.security_policy is server.security_policy
    assert server.console_manager.security_policy.get_access_default() == "deny"

    # Corrupt: last-known-good (deny) must stay, console manager included.
    security_path.write_text("access_default: AllowAll")
    server._reload_config_from_disk()
    assert server.console_manager.security_policy is server.security_policy
    assert server.console_manager.security_policy.get_access_default() == "deny"
