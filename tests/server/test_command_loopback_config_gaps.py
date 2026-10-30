"""Command and loopback adapter tests for config gaps (issue #105).

Both adapters can refuse a port: a command entry with no ``command`` and a loopback entry with an
unusable ``max_read_write_users`` never produce a port object. These tests pin the two properties
issue #105 asks for: a refused entry must not cost a running port its session, and it must never
be reported as a port the reload added.

Port construction spawns nothing in either adapter, so the tests start no processes.
"""

from __future__ import annotations

import pytest

from openmux.server.adapters.command import CommandAdapter, CommandPort
from openmux.server.adapters.loopback import LoopbackAdapter, LoopbackPort

CMD = {"name": "c0", "command": "/bin/cat"}
CMD_NO_COMMAND = {"name": "c1"}
LB = {"name": "lb0"}
LB_BAD_MODE = {"name": "lb1", "max_read_write_users": "two"}


# --- command ---


def _command_adapter(*ports):
    adapter = CommandAdapter("gapcmd", {"command_ports": [dict(p) for p in ports]})
    adapter.main_port_manager = None
    for port_cfg in ports:
        port = CommandPort(str(port_cfg["name"]), dict(port_cfg), adapter)
        adapter.ports[port.name] = port
    return adapter


@pytest.mark.asyncio
async def test_a_refused_command_change_keeps_the_running_process_port():
    adapter = _command_adapter(CMD)
    running = adapter.ports["c0"]

    summary = await adapter.reconcile_ports([dict(CMD, command="")])

    assert adapter.ports["c0"] is running
    assert summary["failed"] == ["c0"]
    assert summary["kept_previous"] == ["c0"]
    assert "command" in summary["failed_detail"]["c0"]["error"]


@pytest.mark.asyncio
async def test_a_refused_command_port_is_failed_once_and_never_added():
    adapter = _command_adapter(CMD)

    first = await adapter.reconcile_ports([CMD, CMD_NO_COMMAND])
    second = await adapter.reconcile_ports([CMD, CMD_NO_COMMAND])

    assert first.get("added", []) == []
    assert first["failed"] == ["c1"]
    assert second.get("added", []) == []
    assert second["failed"] == ["c1"]
    assert [entry["name"] for entry in adapter.config["command_ports"]] == ["c0"]


@pytest.mark.asyncio
async def test_a_command_port_that_applies_is_listed_as_added():
    adapter = _command_adapter(CMD)

    summary = await adapter.reconcile_ports([CMD, {"name": "c9", "command": "/bin/echo hi", "spawn_on_demand": True}])

    assert summary["added"] == ["c9"]
    assert summary["unchanged"] == ["c0"]
    assert "failed" not in summary
    assert "c9" in adapter.ports
    # An on-demand port spawns nothing until a client attaches, so nothing needs stopping here.


@pytest.mark.asyncio
async def test_a_command_port_that_cannot_be_created_is_reported_as_failed(monkeypatch):
    async def _refuse(self, port_name, config):
        return None

    monkeypatch.setattr(CommandAdapter, "create_port", _refuse)
    adapter = _command_adapter(CMD)

    summary = await adapter.reconcile_ports([CMD, {"name": "c9", "command": "/bin/echo hi"}])

    assert summary["failed"] == ["c9"]
    assert summary.get("added", []) == []


# --- loopback ---


def _loopback_adapter(*ports):
    adapter = LoopbackAdapter("gaplb", {"loopback_ports": [dict(p) for p in ports]})
    adapter.main_port_manager = None
    for port_cfg in ports:
        port = LoopbackPort(str(port_cfg["name"]), dict(port_cfg), adapter)
        adapter.ports[port.name] = port
    return adapter


@pytest.mark.asyncio
async def test_a_refused_loopback_change_keeps_the_running_port():
    adapter = _loopback_adapter(LB)
    running = adapter.ports["lb0"]

    summary = await adapter.reconcile_ports([dict(LB, max_read_write_users="two")])

    assert adapter.ports["lb0"] is running
    assert summary["failed"] == ["lb0"]
    assert summary["kept_previous"] == ["lb0"]
    assert "max_read_write_users" in summary["failed_detail"]["lb0"]["error"]


@pytest.mark.asyncio
async def test_a_refused_loopback_port_is_failed_once_and_never_added():
    adapter = _loopback_adapter(LB)

    first = await adapter.reconcile_ports([LB, LB_BAD_MODE])
    second = await adapter.reconcile_ports([LB, LB_BAD_MODE])

    assert first.get("added", []) == []
    assert first["failed"] == ["lb1"]
    assert second.get("added", []) == []
    assert second["failed"] == ["lb1"]
    assert [entry["name"] for entry in adapter.config["loopback_ports"]] == ["lb0"]
    assert sorted(adapter.config_gaps) == ["lb1"]


@pytest.mark.asyncio
async def test_a_loopback_port_that_applies_is_listed_as_added():
    adapter = _loopback_adapter(LB)

    summary = await adapter.reconcile_ports([LB, {"name": "lb9"}])

    assert summary["added"] == ["lb9"]
    assert summary["unchanged"] == ["lb0"]
    assert "failed" not in summary
    for port in list(adapter.ports.values()):
        await port.stop()


@pytest.mark.asyncio
async def test_a_loopback_port_that_cannot_be_created_is_reported_as_failed(monkeypatch):
    async def _refuse(self, port_name, config):
        return None

    monkeypatch.setattr(LoopbackAdapter, "create_port", _refuse)
    adapter = _loopback_adapter(LB)

    summary = await adapter.reconcile_ports([LB, {"name": "lb9"}])

    assert summary["failed"] == ["lb9"]
    assert summary.get("added", []) == []


# --- gaps the operator removed from the file ---


@pytest.mark.asyncio
async def test_a_deleted_loopback_entry_stops_being_a_gap_on_the_next_reload():
    adapter = _loopback_adapter(LB)

    first = await adapter.reconcile_ports([LB, LB_BAD_MODE])
    assert first["failed"] == ["lb1"]
    assert sorted(adapter.config_gaps) == ["lb1"]

    # The operator fixes the file by deleting the entry, so the reload must stop
    # claiming it: the file no longer asks for lb1 at all.
    second = await adapter.reconcile_ports([LB])

    assert sorted(adapter.config_gaps) == []
    assert adapter.config_gaps_kept == []
    assert "failed" not in second
    assert "lb1" not in adapter.ports


@pytest.mark.asyncio
async def test_a_kept_previous_loopback_entry_that_the_file_deletes_stops_being_a_gap():
    adapter = _loopback_adapter(LB)

    refused = await adapter.reconcile_ports([dict(LB, max_read_write_users="two")])
    assert refused["kept_previous"] == ["lb0"]
    assert sorted(adapter.config_gaps) == ["lb0"]

    deleted = await adapter.reconcile_ports([])

    assert sorted(adapter.config_gaps) == []
    assert adapter.config_gaps_kept == []
    assert deleted["removed"] == ["lb0"]
    assert "lb0" not in adapter.ports


@pytest.mark.asyncio
async def test_a_deleted_command_entry_stops_being_a_gap_on_the_next_reload():
    adapter = _command_adapter(CMD)

    first = await adapter.reconcile_ports([CMD, CMD_NO_COMMAND])
    assert first["failed"] == ["c1"]
    assert sorted(adapter.config_gaps) == ["c1"]

    second = await adapter.reconcile_ports([CMD])

    assert sorted(adapter.config_gaps) == []
    assert "failed" not in second
    assert "c1" not in adapter.ports
