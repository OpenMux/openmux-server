"""Tests for ``_discover_default_config_path`` search order.

The function reads only ``os`` (no instance state) so it is called directly.
Each case points the directory lookups (cwd, XDG config home, home) at
isolated empty directories and creates exactly one candidate, asserting it
wins.
"""

import os

import pytest

from openmux.client.main import _discover_default_config_path


def _isolate(monkeypatch, tmp_path, home) -> None:
    """Point cwd, XDG config home and expanduser at empty, isolated dirs."""
    cwd = tmp_path / "cwd"
    xdg = tmp_path / "xdg"
    for d in (cwd, xdg, home):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(os, "getcwd", lambda: str(cwd))
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(home) if p in ("~", "") else p)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("OPENMUX_CLIENT_CONFIG", raising=False)


class TestConfigDiscovery:
    def test_env_var_wins(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        _isolate(monkeypatch, tmp_path, home)
        env_file = tmp_path / "env_client.yaml"
        env_file.write_text("servers: []")
        monkeypatch.setenv("OPENMUX_CLIENT_CONFIG", str(env_file))
        # A cwd candidate must NOT beat the env var
        (tmp_path / "cwd" / "client.yaml").write_text("servers: []")
        assert _discover_default_config_path() == str(env_file)

    def test_env_var_missing_falls_through(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        _isolate(monkeypatch, tmp_path, home)
        monkeypatch.setenv("OPENMUX_CLIENT_CONFIG", str(tmp_path / "does_not_exist.yaml"))
        (tmp_path / "cwd" / "client.yml").write_text("servers: []")
        assert _discover_default_config_path() == str(tmp_path / "cwd" / "client.yml")

    def test_cwd_client_yaml(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        _isolate(monkeypatch, tmp_path, home)
        target = tmp_path / "cwd" / "client.json"
        target.write_text("{}")
        assert _discover_default_config_path() == str(target)

    def test_cwd_prefers_list_order_not_extension(self, monkeypatch, tmp_path):
        # `client.*` is checked before the openmux_* names, and extensions are
        # tried in the fixed order yaml, yml, json
        home = tmp_path / "home"
        _isolate(monkeypatch, tmp_path, home)
        (tmp_path / "cwd" / "openmux_client.yaml").write_text("{}")
        (tmp_path / "cwd" / "client.yaml").write_text("{}")
        got = _discover_default_config_path()
        assert got == str(tmp_path / "cwd" / "client.yaml")

    def test_xdg_config_home(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        _isolate(monkeypatch, tmp_path, home)
        # Code joins XDG_CONFIG_HOME + "openmux" itself; create that subdir here
        (tmp_path / "xdg" / "openmux").mkdir(parents=True)
        target = tmp_path / "xdg" / "openmux" / "client.yaml"
        target.write_text("{}")
        assert _discover_default_config_path() == str(target)

    def test_mac_application_support(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        _isolate(monkeypatch, tmp_path, home)
        target = home / "Library" / "Application Support" / "OpenMux" / "client.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}")
        assert _discover_default_config_path() == str(target)

    def test_home_dotfile(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        _isolate(monkeypatch, tmp_path, home)
        target = home / ".openmux-client.yaml"
        target.write_text("{}")
        assert _discover_default_config_path() == str(target)

    def test_home_nested_openmux_dir(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        _isolate(monkeypatch, tmp_path, home)
        target = home / ".openmux" / "client.yml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}")
        assert _discover_default_config_path() == str(target)

    def test_nothing_found_returns_none(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        _isolate(monkeypatch, tmp_path, home)
        assert _discover_default_config_path() is None


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
