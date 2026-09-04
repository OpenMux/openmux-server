import os

import pytest

from openmux.server import locations

_ENV_VARS = (
    "OPENMUX_LOG_DIR",
    "OPENMUX_RUN_DIR",
    "OPENMUX_STATE_DIR",
    "OPENMUX_CTL_SOCK",
)


@pytest.fixture
def clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_log_dir_default(clean_env):
    assert locations.log_dir() == "logs"


def test_log_dir_env(clean_env):
    clean_env.setenv("OPENMUX_LOG_DIR", "/var/log/openmux")
    assert locations.log_dir() == "/var/log/openmux"


def test_run_dir_default(clean_env):
    assert locations.run_dir() is None


def test_run_dir_env(clean_env):
    clean_env.setenv("OPENMUX_RUN_DIR", "/run/openmux")
    assert locations.run_dir() == "/run/openmux"


def test_pidfile_default(clean_env):
    assert locations.pidfile_path() == os.path.join("logs", "openmux.pid")


def test_pidfile_run_dir(clean_env):
    clean_env.setenv("OPENMUX_RUN_DIR", "/run/openmux")
    assert locations.pidfile_path() == os.path.join("/run/openmux", "openmux.pid")


def test_ctl_sock_default(clean_env):
    assert locations.control_socket_path() == os.path.join("logs", "openmux.sock")


def test_ctl_sock_run_dir(clean_env):
    clean_env.setenv("OPENMUX_RUN_DIR", "/run/openmux")
    assert locations.control_socket_path() == os.path.join("/run/openmux", "openmux.sock")


def test_ctl_sock_override_wins_over_run_dir(clean_env):
    clean_env.setenv("OPENMUX_RUN_DIR", "/run/openmux")
    clean_env.setenv("OPENMUX_CTL_SOCK", "/tmp/custom.sock")
    assert locations.control_socket_path() == "/tmp/custom.sock"


def test_ctl_sock_expands_user(clean_env):
    clean_env.setenv("OPENMUX_CTL_SOCK", "~/my.sock")
    assert locations.control_socket_path() == os.path.expanduser("~/my.sock")


def test_state_dir_default(clean_env):
    assert locations.state_dir() == os.path.expanduser("~/.openmux")


def test_state_dir_env(clean_env):
    clean_env.setenv("OPENMUX_STATE_DIR", "/var/lib/openmux")
    assert locations.state_dir() == "/var/lib/openmux"


def test_state_subdirs_follow_state_dir(clean_env):
    clean_env.setenv("OPENMUX_STATE_DIR", "/var/lib/openmux")
    assert locations.muxcon_tls_dir() == "/var/lib/openmux/muxcon"
    assert locations.muxcon_known_peers_path() == "/var/lib/openmux/muxcon/known_peers.yaml"
    assert locations.muxcon_federated_cache_path() == "/var/lib/openmux/muxcon/federated_cache.json"
    assert locations.ssh_host_key_dir() == "/var/lib/openmux/ssh_listener"
    assert locations.web_tls_dir() == "/var/lib/openmux/web_console"


def test_state_subdirs_dev_default(clean_env):
    home_state = os.path.expanduser("~/.openmux")
    assert locations.muxcon_tls_dir() == os.path.join(home_state, "muxcon")
    assert locations.ssh_host_key_dir() == os.path.join(home_state, "ssh_listener")
    assert locations.web_tls_dir() == os.path.join(home_state, "web_console")


def test_asset_dirs_point_at_webui_package(clean_env):
    server_dir = locations._module_dir()
    assert locations.templates_dir() == server_dir / "webui" / "templates" / "web_console"
    assert locations.static_dir() == server_dir / "webui" / "static"
    # Both must live inside the webui package dir (they do not exist yet).
    assert str(locations.templates_dir()).startswith(str(server_dir / "webui"))


def test_removed_keys_empty(clean_env):
    assert locations.removed_location_keys({}) == []
    assert locations.removed_location_keys(None) == []


def test_removed_keys_top_level(clean_env):
    cfg = {
        "server": {"control_socket": "x.sock", "pidfile": "x.pid", "id": "s1"},
        "logging": {"log_dir": "logs", "file": "/tmp/a.log"},
        "muxcon": {"federated_cache_path": "/tmp/cache.json"},
        "web_console": {"static_dir": "/a", "template_dir": "/b", "port": 8080},
    }
    found = locations.removed_location_keys(cfg)
    assert "server.control_socket" in found
    assert "server.pidfile" in found
    assert "logging.log_dir" in found
    assert "muxcon.federated_cache_path" in found
    assert "web_console.static_dir" in found
    assert "web_console.template_dir" in found
    # Kept keys are never reported.
    assert "logging.file" not in found
    assert "server.id" not in found
    assert "web_console.port" not in found


def test_removed_keys_listeners(clean_env):
    cfg = {
        "muxcon": {
            "listeners": [
                {"enabled": True, "port": 7822, "tls_dir": "~/.openmux/muxcon"},
                {"enabled": True, "port": 8823, "tls_known_peers_path": "/kp.yaml"},
                {"enabled": True, "port": 9924},
            ]
        }
    }
    found = locations.removed_location_keys(cfg)
    assert found == [
        "muxcon.listeners[0].tls_dir",
        "muxcon.listeners[1].tls_known_peers_path",
    ]


def test_removed_keys_no_listeners(clean_env):
    assert locations.removed_location_keys({"muxcon": {"listeners": "not-a-list"}}) == []


def test_removed_keys_non_dict(clean_env):
    assert locations.removed_location_keys("not-a-dict") == []
