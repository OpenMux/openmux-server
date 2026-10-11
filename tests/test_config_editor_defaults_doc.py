"""Tests for _read_defaults_doc in openmux/server/web_plugins/config_editor.

The function parses docs/DEFAULTS.md into a {dot, sections} mapping used for
UI hinting. These tests pin the parse behavior (doc-file discovery, heading
matching, muxcon subsection routing, value parsing) before the complexity
split of _read_defaults_doc.

Discovery (repo-root copy, then cwd copy) is exercised by patching
``pathlib.Path`` with a two-root stub: the old function re-imports
``from pathlib import Path`` at call time, so patching the module attribute
drives it; the stub is also patched onto the plugin module so the refactored
module-level binding is covered.
"""

import pathlib

import pytest

from openmux.server.web_plugins import config_editor
from openmux.server.web_plugins.config_editor import _read_defaults_doc


class _StubDocPath:
    """Path stand-in with class-level switches for repo-/cwd-file existence."""

    repo_exists = False
    cwd_exists = False
    text = ""
    read_raises = False

    def __new__(cls, *args, **kwargs):
        inst = object.__new__(cls)
        inst._root = "repo"
        return inst

    @classmethod
    def cwd(cls):
        inst = object.__new__(cls)
        inst._root = "cwd"
        return inst

    def __truediv__(self, other):
        inst = object.__new__(__class__)
        inst._root = self._root
        return inst

    def resolve(self):
        return self

    @property
    def parents(self):
        return [self, self, self, self, self]

    def is_file(self):
        return self.__class__.repo_exists if self._root == "repo" else self.__class__.cwd_exists

    def read_text(self, encoding=None):
        if self.__class__.read_raises:
            raise OSError("stub read failure")
        return self.__class__.text


@pytest.fixture
def stub_path(monkeypatch):
    _StubDocPath.repo_exists = False
    _StubDocPath.cwd_exists = False
    _StubDocPath.text = ""
    _StubDocPath.read_raises = False
    monkeypatch.setattr(pathlib, "Path", _StubDocPath)
    monkeypatch.setattr(config_editor, "Path", _StubDocPath, raising=False)
    yield _StubDocPath


def _set_doc(stub_path, text):
    stub_path.repo_exists = True
    stub_path.text = text
    return _read_defaults_doc()


def test_doc_missing_everywhere_returns_empty(stub_path):
    assert _read_defaults_doc() == {"dot": {}, "sections": {}}


def test_cwd_fallback_is_used_and_parsed(stub_path):
    stub_path.cwd_exists = True
    stub_path.text = "server\n- host: 0.0.0.0\n"
    result = _read_defaults_doc()
    assert result["dot"]["server.host"] == "0.0.0.0"


def test_repo_copy_is_preferred_when_present(stub_path):
    stub_path.repo_exists = True
    stub_path.cwd_exists = True
    stub_path.text = "server\n- host: 0.0.0.0\n"
    result = _read_defaults_doc()
    assert result["dot"]["server.host"] == "0.0.0.0"


def test_leading_zero_value_is_kept_as_string(stub_path):
    # 0-prefixed digits without a dot must stay a string, not become int()
    result = _set_doc(stub_path, "server\n- unit: 007\n")
    assert result["dot"]["server.unit"] == "007"
    assert isinstance(result["dot"]["server.unit"], str)


def test_unrecognized_heading_skips_its_bullets(stub_path):
    result = _set_doc(stub_path, "Unrelated heading\n- ghost: 1\nserver\n- host: 0.0.0.0\n")
    assert "ghost" not in result["dot"]
    assert "server.ghost" not in result["dot"]
    assert result["dot"]["server.host"] == "0.0.0.0"


def test_muxcon_initiator_options_go_to_table(stub_path):
    text = "muxcon\n" "- initiators[*]:\n" "- options.host: localhost\n" "- options.port: 7822\n"
    result = _set_doc(stub_path, text)
    table = result["sections"]["muxcon.initiators"]
    # the full 'options.*' key is kept for the UI mapping
    assert table["options.host"] == "localhost"
    assert table["options.port"] == 7822


def test_read_failure_returns_empty(stub_path):
    stub_path.repo_exists = True
    stub_path.read_raises = True
    assert _read_defaults_doc() == {"dot": {}, "sections": {}}


def test_real_doc_pops_known_tables_and_dot_values():
    # No stub: parse the actual docs/DEFAULTS.md next to the repo.
    result = _read_defaults_doc()
    assert result["dot"]["muxcon.federated_cache_enabled"] is True
    serial = result["sections"]["serial_ports"]
    assert serial["baudrate"] == 9600
    assert serial["dtr"] is True
    assert serial["stopbits"] == 1
    tcp = result["sections"]["tcp_initiator_ports"]
    assert tcp["use_tls"] is False
    assert tcp["batch_size"] == 1024
    assert tcp["reconnect_delay"] == 5.0
    listeners = result["sections"]["muxcon.listeners"]
    assert listeners["port"] == 7822
    assert listeners["tls_autogen"] is True
    assert listeners["host"] == "0.0.0.0"
    # Annotated values keep the annotation and stay strings; doc bullet keys
    # already carry the 'logging.' prefix, so the dotted key doubles it
    logged_result = result["dot"]["logging.logging.console"]
    assert logged_result == "true (stdout handler can be turned off with false)"
    assert isinstance(logged_result, str)
