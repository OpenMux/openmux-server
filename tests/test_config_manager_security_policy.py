"""Integration tests for ConfigManager.get_security_policy() reading security.yaml from disk."""

import textwrap

import pytest

from openmux.server.config_manager import ConfigManager
from openmux.server.security_policy import SecurityPolicyError


def _write_server_config(tmp_path) -> str:
    (tmp_path / "authentication.yaml").write_text(
        textwrap.dedent(
            """
            users:
              - username: admin
                password_hash: 5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8
                permissions: admin
            """
        )
    )
    config_file = tmp_path / "server.yaml"
    config_file.write_text(
        textwrap.dedent(
            """
            server:
              id: test
            """
        )
    )
    return str(config_file)


def test_get_security_policy_defaults_when_file_missing(tmp_path):
    manager = ConfigManager(_write_server_config(tmp_path))
    manager.load_config()

    policy = manager.get_security_policy()

    assert policy.is_adapter_allowed(adapter_type="serial")
    assert policy.is_section_writable("server")


def test_get_security_policy_reads_disabled_adapters(tmp_path):
    (tmp_path / "security.yaml").write_text(
        textwrap.dedent(
            """
            adapters:
              allowed: ["*"]
              disabled: ["telnet_listener"]
            """
        )
    )
    manager = ConfigManager(_write_server_config(tmp_path))
    manager.load_config()

    policy = manager.get_security_policy()

    assert policy.is_adapter_allowed(adapter_type="serial")
    assert not policy.is_adapter_allowed(adapter_type="telnet_listener")


def test_get_security_policy_reads_disabled_config_editor_sections(tmp_path):
    (tmp_path / "security.yaml").write_text(
        textwrap.dedent(
            """
            config_editor:
              allowed: ["*"]
              disabled: ["authentication"]
            """
        )
    )
    manager = ConfigManager(_write_server_config(tmp_path))
    manager.load_config()

    policy = manager.get_security_policy()

    assert policy.is_section_writable("server")
    assert not policy.is_section_writable("authentication")


def test_get_security_policy_caches_until_next_load_config(tmp_path):
    security_path = tmp_path / "security.yaml"
    security_path.write_text('adapters:\n  disabled: []\n')
    manager = ConfigManager(_write_server_config(tmp_path))
    manager.load_config()

    first = manager.get_security_policy()
    assert manager.get_security_policy() is first

    security_path.write_text('adapters:\n  disabled: ["serial"]\n')
    # Cache is not invalidated just by editing the file...
    assert manager.get_security_policy() is first
    # ...only by reloading the main config, which resets the cached policy.
    manager.load_config()
    second = manager.get_security_policy()
    assert second is not first
    assert not second.is_adapter_allowed(adapter_type="serial")


def test_get_security_policy_raises_on_invalid_schema(tmp_path):
    (tmp_path / "security.yaml").write_text(
        textwrap.dedent(
            """
            adapters:
              allowed: ["not_a_real_adapter_type"]
            """
        )
    )
    manager = ConfigManager(_write_server_config(tmp_path))
    manager.load_config()

    with pytest.raises(SecurityPolicyError):
        manager.get_security_policy()
