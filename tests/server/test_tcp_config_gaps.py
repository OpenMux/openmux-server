"""TCP initiator adapter tests for config gaps (issue #105).

A TCP initiator entry without ``host`` or ``port`` cannot become a port, so the server never
connects it and a reboot does not help. These tests pin what a reload reports and, above all,
that a refused change leaves the running port alone. Ports are real ``TcpInitiatorPort``
objects; construction opens no socket.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from openmux.server.adapters.tcp_initiator import TcpInitiatorAdapter, TcpInitiatorPort

GOOD = {"name": "t0", "host": "127.0.0.1", "port": 5599}
NO_HOST = {"name": "t1", "port": 5599}


def _adapter(*ports: Dict[str, Any]) -> TcpInitiatorAdapter:
    """Build a TCP initiator adapter holding the given ports, without starting anything."""
    adapter = TcpInitiatorAdapter("gaptcp", {"tcp_initiator_ports": [dict(p) for p in ports]})
    adapter.main_port_manager = None
    for port_cfg in ports:
        port = TcpInitiatorPort(str(port_cfg["name"]), dict(port_cfg), adapter)
        adapter.ports[port.name] = port
    return adapter


@pytest.mark.asyncio
async def test_a_refused_change_keeps_the_running_port_and_says_so():
    adapter = _adapter(GOOD)
    running = adapter.ports["t0"]

    summary = await adapter.reconcile_ports([dict(GOOD, host="")])
    try:
        assert adapter.ports["t0"] is running
        assert summary["failed"] == ["t0"]
        assert summary["kept_previous"] == ["t0"]
        assert "t0" not in summary["updated"]
        assert "host" in summary["failed_detail"]["t0"]["error"]
    finally:
        await running.stop()


@pytest.mark.asyncio
async def test_a_refused_new_port_is_reported_as_failed_and_never_as_added():
    adapter = _adapter(GOOD)

    first = await adapter.reconcile_ports([GOOD, NO_HOST])
    second = await adapter.reconcile_ports([GOOD, NO_HOST])
    try:
        assert first.get("added", []) == []
        assert first["failed"] == ["t1"]
        assert second.get("added", []) == []
        assert second["failed"] == ["t1"]
        assert sorted(adapter.config_gaps) == ["t1"]
    finally:
        await adapter.ports["t0"].stop()


@pytest.mark.asyncio
async def test_the_snapshot_keeps_only_the_ports_that_exist():
    adapter = _adapter(GOOD)

    await adapter.reconcile_ports([GOOD, NO_HOST])
    try:
        assert [entry["name"] for entry in adapter.config["tcp_initiator_ports"]] == ["t0"]
    finally:
        await adapter.ports["t0"].stop()


@pytest.mark.asyncio
async def test_a_clean_reload_reports_only_the_four_lists(monkeypatch):
    async def _no_connect(self) -> bool:  # no peer is listening; this test is about the summary
        return True

    monkeypatch.setattr(TcpInitiatorPort, "start", _no_connect)
    adapter = _adapter(GOOD)

    summary = await adapter.reconcile_ports([GOOD, {"name": "t9", "host": "127.0.0.1", "port": 5601}])
    try:
        assert summary == {"added": ["t9"], "removed": [], "updated": [], "unchanged": ["t0"]}
        assert "t9" in adapter.ports
    finally:
        for port in adapter.ports.values():
            await port.stop()


@pytest.mark.asyncio
async def test_a_port_the_adapter_cannot_start_is_reported_as_failed(monkeypatch):
    async def _cannot_start(self) -> bool:
        return False

    monkeypatch.setattr(TcpInitiatorPort, "start", _cannot_start)
    adapter = _adapter(GOOD)

    summary = await adapter.reconcile_ports([GOOD, {"name": "t9", "host": "127.0.0.1", "port": 5601}])

    assert summary["failed"] == ["t9"]
    assert summary.get("added", []) == []
    assert "t9" not in adapter.ports
