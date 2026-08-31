"""Tests for the DataLogger base-directory handling (issue #47)."""

from pathlib import Path
from typing import Any

import pytest

from openmux.server.data_logger import DataLogger


@pytest.fixture
def data_logger():
    """Fresh DataLogger instance (bypasses the process-wide singleton)."""
    dl = DataLogger()
    yield dl
    for fh in list(dl._files.values()):
        try:
            fh.close()
        except Exception:
            pass
    dl._files.clear()


def test_default_path_uses_default_base(data_logger, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = data_logger._default_path("console1")
    assert p == Path("logs/ports/console1.log")
    assert (tmp_path / "logs/ports").is_dir()


def test_default_path_honors_set_base_dir(data_logger, tmp_path):
    base = tmp_path / "varlog" / "openmux"
    data_logger.set_base_dir(str(base))
    p = data_logger._default_path("console1")
    assert p == base / "ports" / "console1.log"
    # Repeated call returns the same path and the dir exists
    assert data_logger._default_path("console1") == base / "ports" / "console1.log"
    assert (base / "ports").is_dir()


def test_set_base_dir_noop_when_same(data_logger, tmp_path):
    base = str(tmp_path / "srv")
    data_logger.set_base_dir(base)
    data_logger.set_base_dir(base)
    assert data_logger.base_dir == base
    # Equivalent None / empty-string handling: both map to the "logs" default
    data_logger.set_base_dir(None)
    assert data_logger.base_dir is None
    data_logger.set_base_dir("   ")
    assert data_logger.base_dir is None


def test_set_base_dir_closes_stale_handles(data_logger, tmp_path):
    old_base = tmp_path / "old"
    new_base = tmp_path / "new"
    data_logger.set_base_dir(str(old_base))
    # Open a stale per-port handle under the old base
    stale_path = data_logger._default_path("p1")
    fh = open(stale_path, "a", encoding="utf-8")
    data_logger._files[str(stale_path)] = fh
    data_logger._line_buffers[str(stale_path)] = bytearray(b"x")

    data_logger.set_base_dir(str(new_base))

    assert str(stale_path) not in data_logger._files
    assert fh.closed, "stale handle should be closed"
    assert str(stale_path) not in data_logger._line_buffers
    # New base resolves under the new directory
    assert data_logger._default_path("p1") == new_base / "ports" / "p1.log"


def test_log_file_override_unaffected_by_base_dir(data_logger, tmp_path):
    data_logger.set_base_dir(str(tmp_path / "base"))
    port_obj = SimpleNamespaceLike(config={"log_file": str(tmp_path / "override.log")})
    # A port-level `log_file` override still wins over the base dir
    p = data_logger._resolve_path_for_port("p1", port_obj)
    assert p == tmp_path / "override.log"
    assert p.parent.exists()


def test_get_log_path_follows_base_dir(data_logger, tmp_path):
    data_logger.set_base_dir(str(tmp_path))
    assert data_logger.get_log_path("p1") == tmp_path / "ports" / "p1.log"


class SimpleNamespaceLike:
    """Minimal stand-in for a port object with a config dict."""

    def __init__(self, **attrs: Any):
        for k, v in attrs.items():
            setattr(self, k, v)
