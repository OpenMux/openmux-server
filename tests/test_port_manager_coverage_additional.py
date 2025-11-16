import asyncio
from typing import Any, Dict

import pytest

from openmux.server.port_manager import PortManager


class DummyDataLogger:
    def __init__(self):
        self.records = []
        self.meta = []

    @classmethod
    def get(cls):  # will be monkeypatched via module-level replacement
        return cls()

    def record(self, port_name: str, data: bytes, direction: str, client_id: Any, meta: Any, port_obj: Any):
        self.records.append((port_name, bytes(data), direction, client_id))

    def record_meta(self, port_name: str, event: str, client_id: Any, meta: Any, port_obj: Any):
        self.meta.append((port_name, event, client_id, meta))


class DummyWrapper:
    def __init__(self, name: str, max_rw: int = 2):
        self.name = name
        self.description = f"Wrapper for {name}"
        self.adapter_type = "dummy"
        self.connected_clients: list[Dict[str, Any]] = []
        self.max_read_write_users = max_rw
        self.data_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)

    def get_status(self):
        return {
            "name": self.name,
            "description": self.description,
            "adapter": self.adapter_type,
            "state": "active",
            "is_running": True,
            "connected_clients": len(self.connected_clients),
            "max_read_write_users": self.max_read_write_users,
        }

    async def write_data(self, data: bytes) -> int:
        # echo-style write for tests
        await self.data_queue.put(bytes(data))
        return len(data)


class DummyFederatedPort:
    def __init__(self, name: str):
        self.name = name
        self.remote_port_name = name
        self.is_connected = True
        self.connected_clients: list[Dict[str, Any]] = []
        self._written = []

    async def write_data(self, data: bytes, client_id: str):
        self._written.append((bytes(data), client_id))
        return len(data)


class DummyOrigin:
    def __init__(self, server_id: str, hostname: str = "h", port: int = 1, server_type: Any = None):
        self.server_id = server_id
        self.hostname = hostname
        self.port = port
        self.server_type = type("T", (), {"value": server_type}) if server_type else None

    def to_dict(self):
        return {
            "server_id": self.server_id,
            "hostname": self.hostname,
            "port": self.port,
            "server_type": getattr(self.server_type, "value", None),
        }


class DummyMeta:
    def __init__(self, server_id: str):
        self.origin_server = DummyOrigin(server_id)
        self.server_chain = [DummyOrigin(server_id)]
        self.federation_type = type("FT", (), {"value": "proxy"})


class DummyRemoteProxy:
    def __init__(self, name: str, server_id: str):
        self.name = name
        self.metadata = DummyMeta(server_id)
        self.data_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=10)
        self._cb = None
        self.server_adapter = object()

    def set_port_manager(self, pm):
        self.pm = pm

    def set_data_callback(self, cb):
        self._cb = cb

    def get_status(self):
        return {
            "name": self.name,
            "description": f"Federated {self.name}",
            "adapter": "federation",
            "state": "active",
            "is_running": True,
            "connected_clients": 0,
            "max_read_write_users": 5,
        }


@pytest.mark.asyncio
async def test_handle_incoming_enqueue_semantics(monkeypatch):
    # Patch DataLogger.get()
    from openmux.server import data_logger as dl_mod

    dummy = DummyDataLogger()
    monkeypatch.setattr(dl_mod.DataLogger, "get", classmethod(lambda cls: dummy))

    pm = PortManager([])

    # No clients: log but do not enqueue
    w1 = DummyWrapper("p1")
    pm.ports["p1"] = w1
    assert pm.handle_incoming_port_data("p1", b"A") is True
    assert w1.data_queue.empty()

    # With clients: enqueue until queue full
    w2 = DummyWrapper("p2")
    w2.data_queue = asyncio.Queue(maxsize=1)
    w2.connected_clients.append({"client_id": "c1", "mode": "read-only"})
    pm.ports["p2"] = w2
    assert pm.handle_incoming_port_data("p2", b"B") is True
    # now full
    assert pm.handle_incoming_port_data("p2", b"C") is False


@pytest.mark.asyncio
async def test_write_to_port_permissions_and_paths(monkeypatch):
    from openmux.server import data_logger as dl_mod

    dummy = DummyDataLogger()
    monkeypatch.setattr(dl_mod.DataLogger, "get", classmethod(lambda cls: dummy))

    pm = PortManager([])

    # Blocked write (no read-write client)
    w = DummyWrapper("pw")
    w.connected_clients.append({"client_id": "c1", "mode": "read-only"})
    pm.ports["pw"] = w
    ok = await pm.write_to_port("pw", b"hi", client_id="c1")
    assert ok is False

    # Allowed write (read-write client)
    w.connected_clients[0]["mode"] = "read-write"
    ok = await pm.write_to_port("pw", b"hi", client_id="c1")
    assert ok is True
    # read back data via queue (echo behavior of DummyWrapper)
    assert await w.data_queue.get() == b"hi"

    # Federated path: remote_port_name set and write_data consumes client_id
    f = DummyFederatedPort("pf")
    f.connected_clients.append({"client_id": "c2", "mode": "read-write"})
    pm.ports["pf"] = f
    ok2 = await pm.write_to_port("pf", b"zz", client_id="c2")
    assert ok2 is True
    assert f._written == [(b"zz", "c2")]


@pytest.mark.asyncio
async def test_get_port_data_and_send_from_unified():
    pm = PortManager([])
    w = DummyWrapper("pd")
    pm.ports["pd"] = w
    # prefill
    await w.data_queue.put(b"X")
    data1 = await pm.get_port_data("pd")
    assert data1 == b"X"
    assert await pm.get_port_data("pd") is None

    # send_data_from_unified_port only enqueues when clients exist
    assert await pm.send_data_from_unified_port("pd", b"Y") is True
    assert await pm.get_port_data("pd") is None
    w.connected_clients.append({"client_id": "c1", "mode": "read-only"})
    assert await pm.send_data_from_unified_port("pd", b"Z") is True
    assert await pm.get_port_data("pd") == b"Z"


@pytest.mark.asyncio
async def test_client_add_remove_promote_and_mode(monkeypatch):
    from openmux.server import data_logger as dl_mod

    dummy = DummyDataLogger()
    monkeypatch.setattr(dl_mod.DataLogger, "get", classmethod(lambda cls: dummy))

    pm = PortManager([])
    w = DummyWrapper("pc", max_rw=1)
    pm.ports["pc"] = w

    ok1 = await pm.add_client_to_port("pc", client_id="u1", username="alice", mode="read-only")
    assert ok1 is True
    # capacity reached
    ok2 = await pm.add_client_to_port("pc", client_id="u2", username="bob", mode="read-only")
    assert ok2 is False

    # promote
    class C:
        username = "u1"

    assert await pm.promote_client("pc", C()) is True
    assert pm.get_client_mode("u1", "pc") == "read-write"

    # remove
    assert await pm.remove_client_from_port("pc", "u1") is True
    assert pm.get_client_mode("u1", "pc") is None


@pytest.mark.asyncio
async def test_register_federated_and_enrichment():
    pm = PortManager([])

    # Register federated port
    proxy = DummyRemoteProxy("rf", server_id="srv1")

    class M:
        def __init__(self, name):
            self.name = name

    meta = type("Meta", (), {"name": "rf", "origin_server": proxy.metadata.origin_server, "server_chain": proxy.metadata.server_chain, "federation_type": proxy.metadata.federation_type})

    name = await pm.register_federated_port(meta, proxy)
    assert name == "rf"
    # callback should add data to queue
    assert proxy._cb is not None
    proxy._cb(b"data")
    assert not proxy.data_queue.empty()

    # Enrichment in list
    lst = await pm.get_port_list_with_federation()
    assert any(p.get("name") == "rf" and p.get("origin_server_id") is not None for p in lst)

    # Unregister federated for server id
    removed = await pm.unregister_federated_ports("srv1")
    assert "rf" in removed