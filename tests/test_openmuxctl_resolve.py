"""openmuxctl control-socket resolution.

openmuxctl runs in a user shell (no systemd vars, no server env), so it
resolves purely from CLI args, the OPENMUX_* variables (shell or the
defaults file), the packaged run dir probe, and the dev fallback.
"""

import os

import pytest

from openmux.cli.openmuxctl import resolve_socket_path
from openmux.server import locations

_ENV_VARS = (
    "OPENMUX_LOG_DIR",
    "OPENMUX_RUN_DIR",
    "OPENMUX_STATE_DIR",
    "OPENMUX_CTL_SOCK",
)
_SYSTEMD_VARS = ("RUNTIME_DIRECTORY", "STATE_DIRECTORY", "LOGS_DIRECTORY")


@pytest.fixture
def clean_env(monkeypatch):
    for var in _ENV_VARS + ("OPENMUX_ENV_FILE",) + _SYSTEMD_VARS:
        monkeypatch.delenv(var, raising=False)
    locations._ENV_FILE_CACHE = None
    return monkeypatch


def test_cli_arg_wins(clean_env):
    assert resolve_socket_path("/cli.sock") == "/cli.sock"


def test_ctl_sock_from_shell(clean_env):
    clean_env.setenv("OPENMUX_CTL_SOCK", "/override.sock")
    assert resolve_socket_path(None) == "/override.sock"


def test_ctl_sock_from_defaults_file_beats_run_dir(clean_env, tmp_path):
    env_file = tmp_path / "defaults"
    env_file.write_text("OPENMUX_CTL_SOCK=/file.sock\nOPENMUX_RUN_DIR=/x\n")
    clean_env.setenv("OPENMUX_ENV_FILE", str(env_file))
    assert resolve_socket_path(None) == "/file.sock"


def test_shell_ctl_sock_beats_defaults_file(clean_env, tmp_path):
    env_file = tmp_path / "defaults"
    env_file.write_text("OPENMUX_CTL_SOCK=/file.sock\n")
    clean_env.setenv("OPENMUX_ENV_FILE", str(env_file))
    clean_env.setenv("OPENMUX_CTL_SOCK", "/shell.sock")
    assert resolve_socket_path(None) == "/shell.sock"


def test_run_dir_from_shell(clean_env):
    clean_env.setenv("OPENMUX_RUN_DIR", "/run/custom")
    assert resolve_socket_path(None) == os.path.join("/run/custom", "openmux.sock")


def test_run_dir_from_defaults_file(clean_env, tmp_path):
    env_file = tmp_path / "defaults"
    env_file.write_text("OPENMUX_RUN_DIR=/run/custom\n")
    clean_env.setenv("OPENMUX_ENV_FILE", str(env_file))
    assert resolve_socket_path(None) == os.path.join("/run/custom", "openmux.sock")


def test_shell_run_dir_beats_defaults_file(clean_env, tmp_path):
    env_file = tmp_path / "defaults"
    env_file.write_text("OPENMUX_RUN_DIR=/from/file\n")
    clean_env.setenv("OPENMUX_ENV_FILE", str(env_file))
    clean_env.setenv("OPENMUX_RUN_DIR", "/from/env")
    assert resolve_socket_path(None) == os.path.join("/from/env", "openmux.sock")


def test_packaged_run_dir_probe(clean_env, tmp_path):
    # The client probes /run/openmux; point PACKAGED_RUN_DIR at a tmp dir
    # we can create in the test.
    packaged = tmp_path / "run"
    packaged.mkdir()
    clean_env.setattr(locations, "PACKAGED_RUN_DIR", str(packaged))
    assert resolve_socket_path(None) == os.path.join(str(packaged), "openmux.sock")


def test_packaged_run_dir_missing_falls_to_dev(clean_env, tmp_path):
    packaged = tmp_path / "does-not-exist"
    clean_env.setattr(locations, "PACKAGED_RUN_DIR", str(packaged))
    # Dev fallback is CWD-relative (logs/openmux.sock).
    assert resolve_socket_path(None) == os.path.join("logs", "openmux.sock")
