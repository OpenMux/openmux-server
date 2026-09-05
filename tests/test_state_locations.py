"""Wiring tests: muxcon / ssh_listener / web_console use locations for state paths.

`test_locations.py` locks the resolvers; these lock the adapters' wiring —
the default comes from OPENMUX_STATE_DIR (or ~/.openmux) and an explicit
config value still wins.
"""
import importlib
import os

import pytest
import yaml

from openmux.server import locations
from openmux.server.adapters import ssh_listener as ssh_listener_module
from openmux.server.adapters.muxcon import UnifiedMuxConAdapter
from openmux.server.web_console import WebConsoleAdapter

_ENV_VARS = (
    "OPENMUX_LOG_DIR",
    "OPENMUX_RUN_DIR",
    "OPENMUX_STATE_DIR",
    "OPENMUX_CTL_SOCK",
    "OPENMUX_ENV_FILE",
    "OPENMUX_PIDFILE",
)


@pytest.fixture
def clean_state_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    locations._ENV_FILE_CACHE = None
    return monkeypatch


def test_muxcon_tls_dir_defaults_from_locations(clean_state_env, monkeypatch):
    monkeypatch.setenv("OPENMUX_STATE_DIR", "/state")
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    assert a._tls_dir == "/state/muxcon"
    assert a._known_peers_path == "/state/muxcon/known_peers.yaml"
    # Federated cache derives from the TLS dir.
    assert a.federated_cache_path == "/state/muxcon/federated_cache.json"


def test_muxcon_tls_dir_dev_default(clean_state_env):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Dev default tracks OPENMUX_STATE_DIR unset: ~/.openmux/muxcon.
    assert a._tls_dir == locations.muxcon_tls_dir()
    assert a._known_peers_path == locations.muxcon_known_peers_path()


def test_muxcon_listener_config_no_longer_places_state(clean_state_env, monkeypatch, tmp_path):
    """The per-listener tls_dir/tls_known_peers_path keys were removed.

    The state base tracks OPENMUX_STATE_DIR even when a listener entry
    carries the stale keys; the ConfigManager deprecation shim strips them
    before an adapter ever sees a real config.
    """
    monkeypatch.setenv("OPENMUX_STATE_DIR", "/state")
    a = UnifiedMuxConAdapter(
        "mx",
        {"listeners": [{"host": "127.0.0.1", "port": 1, "tls_dir": str(tmp_path / "tls")}]},
    )
    assert a._tls_dir == "/state/muxcon"
    assert a._known_peers_path == "/state/muxcon/known_peers.yaml"


def test_muxcon_config_manager_shim_strips_listener_tls_keys(clean_state_env, tmp_path):
    from openmux.server.config_manager import ConfigManager

    cfg_path = tmp_path / "server.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "server": {"id": "t"},
                "muxcon": {"listeners": [{"host": "127.0.0.1", "port": 7822, "tls_dir": "~/.openmux/muxcon"}]},
            },
            sort_keys=False,
        )
    )
    (tmp_path / "authentication.yaml").write_text(
        yaml.safe_dump({"users": [{"username": "u", "password_hash": "x", "permissions": "admin"}]})
    )
    cfg = ConfigManager(str(cfg_path)).load_config()
    assert "tls_dir" not in cfg["muxcon"]["listeners"][0]


def test_ssh_listener_host_key_dir_follows_env(clean_state_env, monkeypatch):
    # The module global resolves at import time (the unit sets the env before
    # exec), so the test re-imports with the env in place, then restores.
    monkeypatch.setenv("OPENMUX_STATE_DIR", "/state")
    importlib.reload(ssh_listener_module)
    try:
        assert ssh_listener_module._HOST_KEY_DIR == "/state/ssh_listener"
        assert ssh_listener_module._HOST_KEY_PATH == "/state/ssh_listener/ssh_host_key"
    finally:
        # Clear the env before re-importing so the module returns to its
        # clean-env resolution (the fixture drops the env only at teardown).
        monkeypatch.delenv("OPENMUX_STATE_DIR", raising=False)
        importlib.reload(ssh_listener_module)


def test_ssh_listener_host_key_dir_dev_default(clean_state_env):
    assert ssh_listener_module._HOST_KEY_DIR == locations.ssh_host_key_dir()
    assert ssh_listener_module._HOST_KEY_PATH == os.path.join(
        locations.ssh_host_key_dir(), "ssh_host_key"
    )


def _web_console(cfg) -> WebConsoleAdapter:
    return WebConsoleAdapter("wc", {"web_console": {"host": "127.0.0.1", "port": 8899, **cfg}})


def test_web_console_tls_dir_defaults_from_locations(clean_state_env, monkeypatch):
    monkeypatch.setenv("OPENMUX_STATE_DIR", "/state")
    assert _web_console({}).tls_dir == "/state/web_console"


def test_web_console_tls_dir_dev_default(clean_state_env):
    assert _web_console({}).tls_dir == locations.web_tls_dir()


def test_web_console_tls_dir_is_env_driven(clean_state_env, monkeypatch, tmp_path):
    """The web console has no tls_dir config key.

    State stays OPENMUX_STATE_DIR-driven even when a stale ``tls_dir``
    value is present in the config dict.
    """
    monkeypatch.setenv("OPENMUX_STATE_DIR", "/state")
    assert _web_console({"tls_dir": str(tmp_path / "wc")}).tls_dir == "/state/web_console"
