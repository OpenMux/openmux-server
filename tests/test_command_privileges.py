import logging
from types import SimpleNamespace

import pytest

from openmux.server.adapters import command as command_module
from openmux.server.adapters.command import CommandPort
from openmux.server.security_policy import CommandPrivilegePolicy


class _DummyAdapter:
    def __init__(self):
        self.logger = logging.getLogger("test.command.adapter")


def _make_port(policy: CommandPrivilegePolicy) -> CommandPort:
    adapter = _DummyAdapter()
    config = {"command": "/bin/true"}
    return CommandPort("test", config, adapter, privilege_policy=policy)


def test_command_port_privilege_spec_root(monkeypatch):
    policy = CommandPrivilegePolicy(
        enabled=True,
        user="openmux",
        group="tty",
        supplementary_groups={"dialout"},
        umask=0o077,
    )
    port = _make_port(policy)

    monkeypatch.setattr(command_module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        command_module.pwd,
        "getpwnam",
        lambda name: SimpleNamespace(pw_uid=2000, pw_name=name, pw_gid=3000, pw_dir=f"/home/{name}"),
    )
    monkeypatch.setattr(
        command_module.grp,
        "getgrnam",
        lambda name: SimpleNamespace(gr_gid=4000 if name == "tty" else 5000, gr_name=name),
    )
    monkeypatch.setattr(
        command_module.grp,
        "getgrgid",
        lambda gid: SimpleNamespace(gr_gid=gid, gr_name=f"gid{gid}"),
    )

    spec = port._resolve_privilege_drop_spec()
    assert spec["uid"] == 2000
    assert spec["gid"] == 4000
    assert spec["user_name"] == "openmux"
    assert spec["home"] == "/home/openmux"
    assert spec["supplementary_gids"] == [5000]
    assert spec["umask"] == 0o077


def test_command_port_privilege_spec_skips_when_not_root(monkeypatch):
    policy = CommandPrivilegePolicy(enabled=True, user="openmux", umask=0o022)
    port = _make_port(policy)

    monkeypatch.setattr(command_module.os, "geteuid", lambda: 1000)

    spec = port._resolve_privilege_drop_spec()
    # User drop skipped (not root) but umask still applied
    assert spec == {"uid": None, "gid": None, "user_name": None, "group_name": None, "home": None, "supplementary_gids": None, "umask": 0o022}


def test_command_port_privilege_spec_invalid_user(monkeypatch):
    policy = CommandPrivilegePolicy(enabled=True, user="missing")
    port = _make_port(policy)

    monkeypatch.setattr(command_module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(command_module.pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError("no user")))

    with pytest.raises(RuntimeError):
        port._resolve_privilege_drop_spec()
