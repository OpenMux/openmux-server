"""Tests for the client logging manager's console-only fallback (issue #42)."""

import logging

import pytest

from openmux.client.logging_manager import ClientLoggingManager
from openmux.common import fsutil


@pytest.fixture(autouse=True)
def _reset_warned():
    fsutil._warned.clear()
    yield
    fsutil._warned.clear()


def test_client_console_only_when_dir_uncreatable(tmp_path):
    """issue #42: an uncreatable log dir keeps console output, adds no file handler,
    and does not raise (previously it raised on the bare os.makedirs call)."""
    blocker = tmp_path / "logs"
    blocker.write_text("a regular file, not a directory")
    cfg = {"log_level": "INFO", "log_dir": str(tmp_path / "logs"), "file_only": True}
    root = logging.getLogger()
    orig_handlers = list(root.handlers)
    try:
        ClientLoggingManager(cfg)
        # file_only mode: when the dir is uncreatable, no file handler is attached.
        assert not any(h.__class__.__name__ in ("RotatingFileHandler", "FileHandler") for h in root.handlers)
        # The uncreatable dir was flagged (warned once) rather than raising.
        assert fsutil._norm(str(tmp_path / "logs")) in fsutil._warned
    finally:
        root.handlers = orig_handlers


def test_client_file_logging_enabled_creates_file(tmp_path):
    """Happy path: a writable dir still attaches a rotating file handler."""
    cfg = {
        "log_level": "INFO",
        "log_dir": str(tmp_path / "logs"),
        "file_logging_enabled": True,
        "log_max_size_mb": 1,
        "log_backups": 2,
    }
    root = logging.getLogger()
    orig_handlers = list(root.handlers)
    try:
        ClientLoggingManager(cfg)
        assert any(h.__class__.__name__ == "RotatingFileHandler" for h in root.handlers)
        assert (tmp_path / "logs" / "openmux_client.log").exists()
    finally:
        root.handlers = orig_handlers
