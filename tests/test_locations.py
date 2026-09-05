import os

import pytest

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


def test_env_file_provides_unset_value(clean_env, tmp_path, monkeypatch):
    env_file = tmp_path / "defaults"
    env_file.write_text("OPENMUX_RUN_DIR=/run/openmux\nOPENMUX_LOG_DIR=/var/log/openmux\n")
    monkeypatch.setenv("OPENMUX_ENV_FILE", str(env_file))
    locations._ENV_FILE_CACHE = None
    assert locations.run_dir() == "/run/openmux"
    assert locations.log_dir() == "/var/log/openmux"


def test_process_env_beats_env_file(clean_env, tmp_path, monkeypatch):
    env_file = tmp_path / "defaults"
    env_file.write_text("OPENMUX_RUN_DIR=/from/file\n")
    monkeypatch.setenv("OPENMUX_ENV_FILE", str(env_file))
    monkeypatch.setenv("OPENMUX_RUN_DIR", "/from/env")
    locations._ENV_FILE_CACHE = None
    assert locations.run_dir() == "/from/env"


def test_env_file_format_and_quotes(clean_env, tmp_path, monkeypatch):
    env_file = tmp_path / "defaults"
    env_file.write_text(
        "# a comment\n"
        "  \n"
        "OPENMUX_LOG_DIR='single'\n"
        'OPENMUX_STATE_DIR="with space"\n'
        "OPENMUX_CTL_SOCK=/run/openmux/custom.sock\n"
        "not a line without equals\n"
    )
    monkeypatch.setenv("OPENMUX_ENV_FILE", str(env_file))
    locations._ENV_FILE_CACHE = None
    parsed = locations._env_file_values()
    assert parsed == {
        "OPENMUX_LOG_DIR": "single",
        "OPENMUX_STATE_DIR": "with space",
        "OPENMUX_CTL_SOCK": "/run/openmux/custom.sock",
    }
    # Resolution uses the parsed value for every location variable.
    assert locations.control_socket_path() == "/run/openmux/custom.sock"
    assert locations.state_dir() == "with space"


def test_env_file_missing_is_noop(clean_env, monkeypatch):
    # Missing file: resolution falls back to defaults, no error.
    monkeypatch.setenv("OPENMUX_ENV_FILE", "/nonexistent/openmux/defaults")
    locations._ENV_FILE_CACHE = None
    assert locations.run_dir() is None
    assert locations.log_dir() == "logs"


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


def test_systemd_dir_vars_used_when_nothing_else_set(clean_env, monkeypatch):
    # No shell env, no defaults file: systemd's exported dir vars apply.
    monkeypatch.setenv("OPENMUX_ENV_FILE", "/nonexistent/openmux/defaults")
    monkeypatch.setenv("RUNTIME_DIRECTORY", "/run/openmux")
    monkeypatch.setenv("STATE_DIRECTORY", "/var/lib/openmux")
    monkeypatch.setenv("LOGS_DIRECTORY", "/var/log/openmux")
    assert locations.run_dir() == "/run/openmux"
    assert locations.pidfile_path() == "/run/openmux/openmux.pid"
    assert locations.control_socket_path() == "/run/openmux/openmux.sock"
    assert locations.state_dir() == "/var/lib/openmux"
    assert locations.log_dir() == "/var/log/openmux"


def test_env_file_beats_systemd_dir_vars(clean_env, tmp_path, monkeypatch):
    env_file = tmp_path / "defaults"
    env_file.write_text("OPENMUX_RUN_DIR=/from/file\n")
    monkeypatch.setenv("OPENMUX_ENV_FILE", str(env_file))
    monkeypatch.setenv("RUNTIME_DIRECTORY", "/run/openmux")
    assert locations.run_dir() == "/from/file"
    assert locations.control_socket_path() == "/from/file/openmux.sock"


def test_process_env_beats_systemd_dir_vars(clean_env, monkeypatch):
    monkeypatch.setenv("OPENMUX_RUN_DIR", "/from/env")
    monkeypatch.setenv("RUNTIME_DIRECTORY", "/run/openmux")
    assert locations.run_dir() == "/from/env"
    monkeypatch.setenv("OPENMUX_LOG_DIR", "/var/log/other")
    monkeypatch.setenv("LOGS_DIRECTORY", "/var/log/openmux")
    assert locations.log_dir() == "/var/log/other"


def test_systemd_var_partial_set_falls_through_per_location(clean_env, monkeypatch):
    # Only RUNTIME_DIRECTORY set (e.g. older without LogsDirectory): state
    # and logs keep their built-in defaults, run dir uses the var.
    monkeypatch.setenv("OPENMUX_ENV_FILE", "/nonexistent/openmux/defaults")
    monkeypatch.setenv("RUNTIME_DIRECTORY", "/run/openmux")
    assert locations.run_dir() == "/run/openmux"
    assert locations.state_dir() == os.path.expanduser("~/.openmux")
    assert locations.log_dir() == "logs"


def test_pkg_run_dir_constant(clean_env):
    assert locations.PACKAGED_RUN_DIR == "/run/openmux"


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


class _WarnLog:
    def __init__(self) -> None:
        self.messages: list = []

    def warning(self, fmt, *args) -> None:
        self.messages.append(fmt % args if args else fmt)


def test_absorb_strips_and_warns_once_per_key(clean_env):
    log = _WarnLog()
    cfg = {
        "server": {"control_socket": "x.sock", "pidfile": "x.pid", "id": "s1"},
        "logging": {"log_dir": "logs", "file": "/tmp/a.log"},
        "muxcon": {
            "federated_cache_path": "/tmp/cache.json",
            "listeners": [{"port": 7822, "tls_dir": "~/.openmux/muxcon"}],
        },
        "web_console": {"static_dir": "/a", "template_dir": "/b", "port": 8080},
    }
    found = locations.absorb_removed_location_keys(cfg, logger=log)
    assert sorted(found) == sorted(
        [
            "server.control_socket",
            "server.pidfile",
            "logging.log_dir",
            "muxcon.federated_cache_path",
            "muxcon.listeners[0].tls_dir",
            "web_console.static_dir",
            "web_console.template_dir",
        ]
    )
    # Values stripped in place, kept keys untouched.
    assert cfg["server"] == {"id": "s1"}
    assert cfg["logging"] == {"file": "/tmp/a.log"}
    assert cfg["muxcon"]["listeners"] == [{"port": 7822}]
    assert cfg["web_console"] == {"port": 8080}
    # Second pass finds nothing (idempotent) and warns nothing new.
    before = len(log.messages)
    assert locations.absorb_removed_location_keys(cfg, logger=log) == []
    assert len(log.messages) == before


def test_absorb_warns_one_line_per_key(clean_env):
    log = _WarnLog()
    cfg = {
        "server": {"pidfile": "x.pid", "control_socket": "x.sock"},
        "muxcon": {"listeners": [{"port": 7822, "tls_dir": "a"}, {"port": 7823, "tls_dir": "b"}]},
    }
    locations.absorb_removed_location_keys(cfg, logger=log)
    assert len(log.messages) == 4
    assert any("server.pidfile" in m for m in log.messages)
    assert any("server.control_socket" in m for m in log.messages)
    assert any("muxcon.listeners[1].tls_dir" in m for m in log.messages)


def test_absorb_noop_on_clean_config(clean_env):
    log = _WarnLog()
    cfg = {"server": {"id": "s1"}, "logging": {"file": "/tmp/a.log"}}
    assert locations.absorb_removed_location_keys(cfg, logger=log) == []
    assert log.messages == []
