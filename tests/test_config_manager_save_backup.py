"""Config Editor save path: where the pre-save copy goes, and the modes it gets.

A packaged install runs the server as `openmux` with `/etc/openmux` at 0750
`root:openmux`, so the service may read that directory but cannot create a file in
it. The writer used to create a sibling `server.yaml.bak` before it wrote the new
config, so every Config Editor save failed on that copy first and `save_config`
returned False with:

    PermissionError: [Errno 13] Permission denied: '/etc/openmux/server.yaml.bak'

The copy now goes to `<state dir>/config-backups`, and the two config files the
service owns are rewritten in place. These tests pin that location, the 0600 mode
(the copy of authentication.yaml holds password hashes, and a new file would
otherwise be 0644 under the service umask), and the fact that a save no longer
needs write permission on the config directory.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from openmux.server.config_manager import ConfigManager

BACKUP_DIR_NAME = "config-backups"

# The `server` section is what the loader requires; `authentication` is the
# sidecar payload save_config splits out and writes to authentication.yaml.
FIRST: Dict[str, Any] = {"server": {"server_id": "first"}, "authentication": {"users": []}}
SECOND: Dict[str, Any] = {"server": {"server_id": "second"}, "authentication": {"users": []}}

# Mode bits only bind for a non-root POSIX user, and every packaged install is
# one. Root ignores them, so the two directory-permission tests would pass
# without proving anything there.
needs_unprivileged_posix = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="the packaged mode bits do not apply to root or to Windows",
)


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    """Point the state base at a private dir, so backups never touch ~/.openmux."""
    state = tmp_path / "state"
    monkeypatch.setenv("OPENMUX_STATE_DIR", str(state))
    return state


def _config_dir(tmp_path: Path) -> Path:
    """Create a directory holding the two files a packaged server writes."""
    config_dir = tmp_path / "etc"
    config_dir.mkdir()
    (config_dir / "server.yaml").write_text(yaml.safe_dump({"server": FIRST["server"]}), encoding="utf-8")
    (config_dir / "authentication.yaml").write_text("users: []\n", encoding="utf-8")
    return config_dir


def _manager(config_dir: Path) -> ConfigManager:
    """Build a manager whose sidecar paths fall beside its server.yaml."""
    return ConfigManager(str(config_dir / "server.yaml"))


def test_previous_content_is_kept_in_the_state_dir(state_dir, tmp_path):
    """The pre-save copy lands in the state dir, never beside the config file."""
    config_dir = _config_dir(tmp_path)
    cm = _manager(config_dir)

    assert cm.save_config(dict(SECOND)) is True

    backup = state_dir / BACKUP_DIR_NAME / "server.yaml.bak"
    assert yaml.safe_load(backup.read_text(encoding="utf-8"))["server"]["server_id"] == "first"
    assert yaml.safe_load((config_dir / "server.yaml").read_text(encoding="utf-8"))["server"]["server_id"] == "second"
    assert not (config_dir / "server.yaml.bak").exists()
    assert not (config_dir / "authentication.yaml.bak").exists()


def test_both_saved_files_get_a_copy(state_dir, tmp_path):
    """save_config writes two files, so it backs up both by name."""
    config_dir = _config_dir(tmp_path)
    cm = _manager(config_dir)

    assert cm.save_config(dict(SECOND)) is True

    backups = sorted(p.name for p in (state_dir / BACKUP_DIR_NAME).iterdir())
    assert backups == ["authentication.yaml.bak", "server.yaml.bak"]


def test_backup_dir_and_hash_copy_are_not_readable_by_others(state_dir, tmp_path):
    """The copy of authentication.yaml holds hashes: owner-only modes, not 0644."""
    config_dir = _config_dir(tmp_path)
    cm = _manager(config_dir)

    assert cm.save_config(dict(SECOND)) is True

    backup_dir = state_dir / BACKUP_DIR_NAME
    for path in (backup_dir, backup_dir / "authentication.yaml.bak", backup_dir / "server.yaml.bak"):
        assert path.stat().st_mode & 0o077 == 0, f"{path} is reachable beyond its owner"


def test_a_second_save_replaces_the_stored_copy(state_dir, tmp_path):
    """One copy per file: the newest save keeps the immediately previous content."""
    config_dir = _config_dir(tmp_path)
    cm = _manager(config_dir)
    third = {"server": {"server_id": "third"}, "authentication": {"users": []}}

    assert cm.save_config(dict(SECOND)) is True
    assert cm.save_config(dict(third)) is True

    backup = yaml.safe_load((state_dir / BACKUP_DIR_NAME / "server.yaml.bak").read_text(encoding="utf-8"))
    assert backup["server"]["server_id"] == "second"


@needs_unprivileged_posix
def test_save_needs_no_write_permission_on_the_config_dir(state_dir, tmp_path):
    """Packaged case: /etc/openmux stays root's, so only the files may be written."""
    config_dir = _config_dir(tmp_path)
    cm = _manager(config_dir)
    config_dir.chmod(0o555)  # group bits of 0750 root:openmux, seen by the service user
    try:
        assert cm.save_config(dict(SECOND)) is True
    finally:
        config_dir.chmod(0o755)  # restore, or the temp dir cannot be cleaned up

    assert yaml.safe_load((config_dir / "server.yaml").read_text(encoding="utf-8"))["server"]["server_id"] == "second"
    assert (state_dir / BACKUP_DIR_NAME / "server.yaml.bak").exists()


@needs_unprivileged_posix
def test_an_unwritable_backup_dir_warns_and_still_saves(state_dir, tmp_path, caplog):
    """No backup must not cost the admin an edit they already validated."""
    config_dir = _config_dir(tmp_path)
    cm = _manager(config_dir)
    backup_dir = state_dir / BACKUP_DIR_NAME
    backup_dir.mkdir(parents=True)
    backup_dir.chmod(0o555)
    try:
        with caplog.at_level(logging.WARNING, logger="openmux.config"):
            assert cm.save_config(dict(SECOND)) is True
    finally:
        backup_dir.chmod(0o755)

    assert "without a backup" in caplog.text
    assert yaml.safe_load((config_dir / "server.yaml").read_text(encoding="utf-8"))["server"]["server_id"] == "second"
