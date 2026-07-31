"""Tests for the Config Editor's writable-section enforcement wired to SecurityPolicy.

Exercises `_get_writable_metadata` and `_enforce_writable_sections` against a
real ConfigManager backed by an on-disk security.yaml, covering both the
allow and disable side of the `config_editor` block.
"""

import textwrap

from openmux.server.config_manager import ConfigManager
from openmux.server.web_plugins.config_editor import _enforce_writable_sections, _get_writable_metadata


def _write_server_config(tmp_path, extra_sections="") -> str:
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
            logging:
              level: WARNING
            """
        )
        + extra_sections
    )
    return str(config_file)


def _manager(tmp_path, security_yaml=None, extra_sections="") -> ConfigManager:
    if security_yaml is not None:
        (tmp_path / "security.yaml").write_text(textwrap.dedent(security_yaml))
    manager = ConfigManager(_write_server_config(tmp_path, extra_sections))
    manager.load_config()
    return manager


def test_get_writable_metadata_defaults_allow_everything(tmp_path):
    cm = _manager(tmp_path)

    sections, enforced = _get_writable_metadata(cm)

    assert enforced is True
    assert "server" in sections
    assert "logging" in sections


def test_get_writable_metadata_none_config_manager():
    sections, enforced = _get_writable_metadata(None)

    assert sections == []
    assert enforced is False


def test_get_writable_metadata_reflects_disabled_sections(tmp_path):
    cm = _manager(
        tmp_path,
        security_yaml="""
        config_editor:
          allowed: ["*"]
          disabled: ["logging"]
        """,
    )

    sections, enforced = _get_writable_metadata(cm)

    assert enforced is True
    assert "server" in sections
    assert "logging" not in sections


def test_enforce_writable_sections_allows_modification_of_writable_section(tmp_path):
    cm = _manager(
        tmp_path,
        security_yaml="""
        config_editor:
          allowed: ["*"]
          disabled: ["logging"]
        """,
    )
    payload = dict(cm.config)
    payload["server"] = {"id": "changed"}

    disallowed = _enforce_writable_sections(cm, payload)

    assert disallowed == set()


def test_enforce_writable_sections_blocks_modification_of_disabled_section(tmp_path):
    cm = _manager(
        tmp_path,
        security_yaml="""
        config_editor:
          allowed: ["*"]
          disabled: ["logging"]
        """,
    )
    payload = dict(cm.config)
    payload["logging"] = {"level": "DEBUG"}

    disallowed = _enforce_writable_sections(cm, payload)

    assert disallowed == {"logging"}


def test_enforce_writable_sections_no_op_when_nothing_changed(tmp_path):
    cm = _manager(
        tmp_path,
        security_yaml="""
        config_editor:
          allowed: ["*"]
          disabled: ["logging"]
        """,
    )
    payload = dict(cm.config)

    disallowed = _enforce_writable_sections(cm, payload)

    assert disallowed == set()


def test_enforce_writable_sections_fully_read_only_blocks_any_change(tmp_path):
    cm = _manager(
        tmp_path,
        security_yaml="""
        config_editor:
          allowed: ["*"]
          disabled: ["*"]
        """,
    )
    payload = dict(cm.config)
    payload["server"] = {"id": "changed"}

    disallowed = _enforce_writable_sections(cm, payload)

    assert disallowed == {"server"}
