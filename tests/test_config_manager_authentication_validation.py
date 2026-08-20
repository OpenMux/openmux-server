"""Tests for ConfigManager authentication section validation.

Ensures at least one authentication method is present, including the
``external_auth`` key (and the deprecated ``pam`` alias).
"""

import textwrap

import pytest

from openmux.server.config_manager import ConfigManager


def _write_configs(tmp_path, auth_yaml: str) -> str:
    (tmp_path / "authentication.yaml").write_text(textwrap.dedent(auth_yaml))
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


def test_validate_config_accepts_external_auth_only(tmp_path):
    config_file = _write_configs(
        tmp_path,
        """
        external_auth:
          enabled: false
          service: openmux
          timeout: 10
        """,
    )
    manager = ConfigManager(config_file)
    manager.load_config()

    assert manager.config["authentication"]["external_auth"]["service"] == "openmux"


def test_validate_config_rejects_deprecated_pam_only(tmp_path):
    config_file = _write_configs(
        tmp_path,
        """
        pam:
          enabled: false
        """,
    )
    manager = ConfigManager(config_file)

    with pytest.raises(ValueError, match="Authentication section must contain"):
        manager.load_config()


def test_validate_config_rejects_authentication_without_any_method(tmp_path):
    config_file = _write_configs(
        tmp_path,
        """
        sessions:
          ttl: 30
        """,
    )
    manager = ConfigManager(config_file)

    with pytest.raises(ValueError, match="Authentication section must contain"):
        manager.load_config()
