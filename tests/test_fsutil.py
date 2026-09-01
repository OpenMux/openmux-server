"""Tests for openmux.common.fsutil.ensure_directory (issue #42)."""

import logging
from pathlib import Path

import pytest

from openmux.common import fsutil
from openmux.common.fsutil import _norm, ensure_directory


@pytest.fixture(autouse=True)
def _reset_warned():
    """Clear the process-wide warned set around every test."""
    fsutil._warned.clear()
    yield
    fsutil._warned.clear()


def test_norm_resolves_relative_and_dot_relative_the_same(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    expected = str(tmp_path / "logs")
    assert _norm("logs") == expected
    assert _norm("./logs") == expected
    assert _norm(str(tmp_path / "logs")) == expected


def test_ensure_directory_creates_path_and_returns_true(tmp_path):
    target = tmp_path / "a" / "b" / "c"
    assert ensure_directory(target) is True
    assert target.is_dir()
    # Second call stays silent and still True
    assert ensure_directory(target) is True
    assert fsutil._warned == set()


def test_ensure_directory_warns_once_and_returns_false(tmp_path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("file, not a directory")
    target = tmp_path / "blocker" / "sub"
    with caplog.at_level(logging.WARNING, logger=""):
        assert ensure_directory(target) is False
        assert ensure_directory(target) is False

    warned = [r for r in caplog.records if "cannot create log directory" in r.getMessage().lower()]
    assert len(warned) == 1, "the warning must fire exactly once for the same directory"
    # The single warning carries no traceback (the error is a plain, known condition)
    assert warned[0].exc_info is None


def test_ensure_directory_warns_again_for_a_different_path(tmp_path, caplog):
    blocker_a = tmp_path / "a"
    blocker_b = tmp_path / "b"
    blocker_a.write_text("x")
    blocker_b.write_text("y")
    with caplog.at_level(logging.WARNING, logger=""):
        assert ensure_directory(tmp_path / "a" / "s") is False
        assert ensure_directory(tmp_path / "b" / "s") is False
    warned = [r for r in caplog.records if "cannot create log directory" in r.getMessage().lower()]
    assert len(warned) == 2


def test_ensure_directory_normalizes_equivalent_forms_to_one_warning(tmp_path, caplog, monkeypatch):
    monkeypatch.chdir(tmp_path)
    blocker = tmp_path / "logs"
    blocker.write_text("file")
    with caplog.at_level(logging.WARNING, logger=""):
        assert ensure_directory("logs") is False
        assert ensure_directory("./logs") is False
        assert ensure_directory(str(tmp_path / "logs")) is False
    warned = [r for r in caplog.records if "cannot create log directory" in r.getMessage().lower()]
    assert len(warned) == 1
