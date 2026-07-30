"""Tests for ConsoleManager.force_promote_client / get_rw_holders_display / _resolve_client_ip."""

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from openmux.server.console_manager import ConsoleManager


class FakePort:
    def __init__(self, connected_clients: Optional[List[Dict[str, Any]]] = None):
        self.connected_clients = connected_clients or []


class FakePortManager:
    """Minimal port manager stand-in exposing only what ConsoleManager needs."""

    def __init__(self):
        self.ports: Dict[str, FakePort] = {}

    def get_port(self, name: str):
        return self.ports.get(name)

    async def promote_client(self, port_name: str, client_id: str) -> bool:
        port = self.ports.get(port_name)
        if port is None:
            return False
        for c in port.connected_clients:
            if c["client_id"] == client_id:
                c["mode"] = "read-write"
                return True
        return False

    async def demote_client(self, port_name: str, client_id: str) -> bool:
        port = self.ports.get(port_name)
        if port is None:
            return False
        for c in port.connected_clients:
            if c["client_id"] == client_id:
                c["mode"] = "read-only"
                return True
        return False


class FakeAdapterChannel:
    """Stand-in for an owning adapter, supporting cross-adapter notify/IP lookup."""

    def __init__(self, ip: str = "1.2.3.4", accept: bool = True):
        self.ip = ip
        self.accept = accept
        self.received: List[Dict[str, Any]] = []

    async def send_control_frame_to_client(self, client_id: str, payload: Dict[str, Any]) -> bool:
        self.received.append(payload)
        return self.accept

    def _resolve_client_meta(self, client_id: str) -> Dict[str, Any]:
        return {"type": "fake", "ip": self.ip, "username": "someone"}


@pytest.fixture
def port_manager() -> FakePortManager:
    return FakePortManager()


@pytest.fixture
def cm(port_manager: FakePortManager) -> ConsoleManager:
    return ConsoleManager(port_manager, MagicMock())


def _attach(cm: ConsoleManager, port: FakePort, port_name: str, client_id: str, username: str, mode: str):
    port.connected_clients.append({"client_id": client_id, "username": username, "mode": mode})
    cm.client_port_map[client_id] = port_name


# ---------------------------------------------------------------------------
# force_promote_client


@pytest.mark.asyncio
async def test_force_promote_demotes_other_holder_and_promotes_target(cm, port_manager):
    port = FakePort()
    port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "A", "alice", "read-write")
    _attach(cm, port, "p1", "B", "bob", "read-only")

    ok, undelivered = await cm.force_promote_client("B", "p1")

    assert ok is True
    modes = {c["client_id"]: c["mode"] for c in port.connected_clients}
    assert modes["A"] == "read-only"
    assert modes["B"] == "read-write"
    # No adapter registered for A -> cross-notify fails -> falls back to caller.
    assert undelivered == ["A"]


@pytest.mark.asyncio
async def test_force_promote_cross_notifies_via_client_to_manager(cm, port_manager):
    port = FakePort()
    port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "A", "alice", "read-write")
    _attach(cm, port, "p1", "B", "bob", "read-only")
    other_adapter = FakeAdapterChannel(accept=True)
    cm.client_to_manager["A"] = other_adapter

    ok, undelivered = await cm.force_promote_client("B", "p1")

    assert ok is True
    assert undelivered == []
    assert other_adapter.received == [{"type": "client_mode", "ok": False, "mode": "read-only", "reason": "demoted"}]


@pytest.mark.asyncio
async def test_force_promote_no_other_holders_just_promotes(cm, port_manager):
    port = FakePort()
    port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "B", "bob", "read-only")

    ok, undelivered = await cm.force_promote_client("B", "p1")

    assert ok is True
    assert undelivered == []
    assert port.connected_clients[0]["mode"] == "read-write"


@pytest.mark.asyncio
async def test_force_promote_returns_false_when_promote_fails(cm, port_manager):
    port = FakePort()
    port_manager.ports["p1"] = port
    # Client never attached (not in client_port_map) -> promote_client_to_read_write fails.
    ok, undelivered = await cm.force_promote_client("ghost", "p1")
    assert ok is False
    assert undelivered == []


# ---------------------------------------------------------------------------
# get_rw_holders_display / _resolve_client_ip


def test_get_rw_holders_display_unknown_ip_without_adapter(cm, port_manager):
    port = FakePort([{"client_id": "A", "username": "alice", "mode": "read-write"}])
    port_manager.ports["p1"] = port
    assert cm.get_rw_holders_display("p1") == ["alice@unknown"]


def test_get_rw_holders_display_resolves_ip_via_adapter(cm, port_manager):
    port = FakePort([{"client_id": "A", "username": "alice", "mode": "read-write"}])
    port_manager.ports["p1"] = port
    cm.client_to_manager["A"] = FakeAdapterChannel(ip="10.0.0.5")
    assert cm.get_rw_holders_display("p1") == ["alice@10.0.0.5"]


def test_get_rw_holders_display_excludes_read_only_clients(cm, port_manager):
    port = FakePort(
        [
            {"client_id": "A", "username": "alice", "mode": "read-write"},
            {"client_id": "B", "username": "bob", "mode": "read-only"},
        ]
    )
    port_manager.ports["p1"] = port
    assert cm.get_rw_holders_display("p1") == ["alice@unknown"]


def test_get_rw_holders_display_empty_for_unknown_port(cm):
    assert cm.get_rw_holders_display("does-not-exist") == []
