import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from openmux.server.port_manager import PortManager


@pytest.fixture
def simple_ports_config():
    # Placeholder retained for backward compatibility; unified-only now
    return []


class DummyDataLogger:
    def __init__(self):
        self.events: List[Dict[str, Any]] = []

    def record(self, port_name, data, direction, client_id=None, meta=None, port_obj=None):
        # Track calls but do nothing (no file IO)
        self.events.append(
            {
                "port": port_name,
                "bytes": bytes(data),
                "dir": direction,
                "client": client_id,
            }
        )


@pytest.mark.asyncio
async def test_unified_port_lifecycle_and_io(monkeypatch):
    # Stub DataLogger.get() to avoid filesystem writes
    from openmux.server import data_logger as dl_mod

    dummy = DummyDataLogger()
    monkeypatch.setattr(dl_mod.DataLogger, "get", classmethod(lambda cls: dummy))

    pm = PortManager([])
    # Create a unified loopback adapter with one port and register it
    from openmux.server.adapters.loopback import LoopbackAdapter

    # issue #59: legacy 2 maps to "multiple" (unlimited), so use "one" to test the
    # full-port rejection path (previously exercised with an explicit count of 2).
    adapter = LoopbackAdapter("loop", {"loopback_ports": [{"name": "p1", "max_read_write_users": "one"}]})
    adapter.main_port_manager = pm
    pm.set_unified_adapters([adapter])
    assert await adapter.start() is True

    # Add clients and verify read-only does not consume a rw slot
    ok1 = await pm.add_client_to_port("p1", client_id="c1", username="u1", mode="read-only")
    ok2 = await pm.add_client_to_port("p1", client_id="c2", username="u2", mode="read-write")
    assert ok1 is True and ok2 is True
    # read-only clients do not count against max_read_write_users; always admitted
    ok3 = await pm.add_client_to_port("p1", client_id="c3", username="u3", mode="read-only")
    assert ok3 is True

    # c2 now holds the single write slot; promotion of the read-only c1 is refused.
    class Client:  # console compatibility path
        username = "c1"

    assert await pm.promote_client("p1", Client()) is False
    assert pm.get_client_mode("c1", "p1") == "read-only"

    # The one rw slot is occupied; a new read-write client is rejected
    ok4 = await pm.add_client_to_port("p1", client_id="c4", username="u4", mode="read-write")
    assert ok4 is False

    # Write should be allowed only for connected read-write clients
    blocked = await pm.write_to_port("p1", b"hello\n", client_id="c2X")
    assert blocked is False
    allowed = await pm.write_to_port("p1", b"hello\n", client_id="c2")
    assert allowed is True

    # Data is echoed by loopback: first the content without newline, then an ENTER banner
    data = await pm.get_port_data("p1")
    assert data == b"hello"
    enter = await pm.get_port_data("p1")
    assert enter == b"[ENTER]\r\n"

    # No explicit disconnect path; adapter manages lifecycle
    await adapter.stop()


class FakeUnifiedPort:
    def __init__(self, name: str, description: str = "Unified Port"):
        self.name = name
        self.description = description
        self.is_running = True
        self.max_read_write_users = 5
        # data_queue remains on the port as a test-only staging buffer;
        # the wrapper creates its own delivery queue (no longer aliased)
        self.data_queue = asyncio.Queue()
        # Optional state with .value used by status
        self.state = SimpleNamespace(value="active")


class FakeUnifiedAdapter:
    def __init__(self, name="UA", adapter_type="loopback"):
        self.name = name
        self.adapter_type = adapter_type
        self.ports: Dict[str, FakeUnifiedPort] = {}
        self._writes: List[bytes] = []

    def get_adapter_type(self):
        return self.adapter_type

    async def write_to_port(self, port_name: str, data: bytes):
        # Return int sometimes and bool sometimes to exercise wrapper logic
        self._writes.append(bytes(data))
        if len(self._writes) % 2 == 0:
            return len(data)  # int path
        return True  # bool path


@pytest.mark.asyncio
async def test_unified_wrapper_and_queueing():
    pm = PortManager([])

    adapter = FakeUnifiedAdapter()
    up = FakeUnifiedPort("u1")
    adapter.ports["u1"] = up
    pm.set_unified_adapters([adapter])

    # The port doesn't exist in legacy list but should be surfaced via wrapper
    assert pm.port_exists("u1") is True

    # Register unified port explicitly and get wrapper instance
    assert await pm.register_unified_port("u1", up, adapter) is True
    wrapper = pm.get_port("u1")
    assert wrapper is not None and hasattr(wrapper, "unified_port")
    dq = wrapper.data_queue
    assert dq is not None

    # Exercise wrapper write paths (bool and int conversions)
    ok_a = await wrapper.write_data(b"abc")
    ok_b = await wrapper.write_data(b"def")
    assert ok_a is True and ok_b is True

    # No clients -> send_data should drop but return True
    assert await pm.send_data("u1", b"X") is True
    assert dq.empty()

    # Force enqueue even without clients
    assert await pm.send_data("u1", b"forced", require_clients=False) is True
    assert dq.get_nowait() == b"forced"

    # Add a client and enqueue data
    wrapper.connected_clients.append({"client_id": "c1", "mode": "read-only"})
    assert await pm.send_data("u1", b"Y") is True
    # Read from the unified port's queue via the shared queue reference
    got = dq.get_nowait()
    assert got == b"Y"

    # dropped_chunks starts at zero and is surfaced via get_status()
    assert wrapper.get_status()["dropped_chunks"] == 0

    # Overflowing a per-client queue evicts the oldest chunk and increments the counter
    wrapper.client_queues["c1"] = asyncio.Queue(maxsize=1)
    await wrapper.client_queues["c1"].put(b"old")
    assert await pm.send_data("u1", b"new") is True
    assert wrapper.client_queues["c1"].get_nowait() == b"new"
    assert wrapper.get_status()["dropped_chunks"] == 1

    # Unregister unified port
    assert await pm.unregister_unified_port("u1") is True


@dataclass
class OriginInfo:
    server_id: str
    hostname: str = "h"
    port: int = 0
    server_type: Any = SimpleNamespace(value="server")
    description: str = "desc"

    def to_dict(self):
        return {
            "server_id": self.server_id,
            "hostname": self.hostname,
            "port": self.port,
            "server_type": self.server_type.value,
            "description": self.description,
        }


class FakeRemoteProxy:
    def __init__(self, name: str, server_id: str):
        self.remote_port_name = name
        self.connected_clients: List[Dict[str, Any]] = []
        self.metadata = SimpleNamespace(
            name=name,
            origin_server=OriginInfo(server_id=server_id),
            server_chain=[OriginInfo(server_id=f"chain-{server_id}")],
            federation_type=SimpleNamespace(value="muxcon"),
        )

        class _ServerAdapter:
            async def handle_port_session_close(self, pn: str):
                return None

        self.server_adapter = _ServerAdapter()
        self.data_queue: asyncio.Queue = asyncio.Queue()
        self._cb = None
        self.is_connected = True

    def set_port_manager(self, pm):
        self.pm = pm

    def set_data_callback(self, cb):
        self._cb = cb

    async def write_data(self, data: bytes, client_id: str):
        # Pretend to send out
        return len(data)

    async def open_stream_for_client(self, client_id: str):
        return True

    async def close_stream_for_client(self, client_id: str):
        return True

    def get_status(self) -> Dict[str, Any]:
        # Minimal status to satisfy enumeration in get_port_list_with_federation
        return {
            "name": self.remote_port_name,
            "description": f"Remote proxy for {self.remote_port_name}",
            "adapter_type": "muxcon-remote",
            "connected": self.is_connected,
            "client_count": 0,
            "max_read_write_users": 1,
            "adapter_status": {"status": "connected" if self.is_connected else "disconnected"},
        }


@pytest.mark.asyncio
async def test_register_unregister_federated_and_enrichment(monkeypatch, simple_ports_config):
    # Disable DataLogger side effects
    from openmux.server import data_logger as dl_mod

    dummy = DummyDataLogger()
    monkeypatch.setattr(dl_mod.DataLogger, "get", classmethod(lambda cls: dummy))

    pm = PortManager([])

    # Register two federated ports from different servers
    rp1 = FakeRemoteProxy("rf1", server_id="S1")
    rp2 = FakeRemoteProxy("rf2", server_id="S2")

    meta1 = SimpleNamespace(name="rf1", origin_server=rp1.metadata.origin_server)
    meta2 = SimpleNamespace(name="rf2", origin_server=rp2.metadata.origin_server)

    pname1 = await pm.register_federated_port(meta1, rp1)
    pname2 = await pm.register_federated_port(meta2, rp2)
    assert pname1 == "rf1" and pname2 == "rf2"

    # Simulate inbound data via callback and retrieve using get_port_data
    assert rp1._cb is not None
    await rp1._cb(b"hello")
    data = await pm.get_port_data("rf1")
    assert data == b"hello"

    # get_port_list_with_federation should include enrichment for remote ports
    lst = await pm.get_port_list_with_federation()
    names = {e["name"] for e in lst}
    assert {"rf1", "rf2"}.issubset(names)
    rf1_entry = next(e for e in lst if e["name"] == "rf1")
    assert rf1_entry.get("origin_server") and rf1_entry.get("server_chain_info")
    assert rf1_entry.get("remote") is True

    # Removing clients from federated port should not error (no clients present)
    assert await pm.remove_client_from_port("rf1", "nonexistent") is False

    # Unregister by server id
    removed = await pm.unregister_federated_ports("S1")
    assert "rf1" in removed and "rf2" not in removed


@pytest.mark.asyncio
async def test_get_port_list_is_empty_without_unified_ports():
    pm = PortManager([])
    ports = await pm.get_port_list()
    assert ports == []


@pytest.mark.asyncio
async def test_force_enqueue_and_drop_oldest(monkeypatch):
    from openmux.server import data_logger as dl_mod

    dummy = DummyDataLogger()
    monkeypatch.setattr(dl_mod.DataLogger, "get", classmethod(lambda cls: dummy))

    pm = PortManager([])

    class DummyPort:
        def __init__(self):
            self.name = "pbuf"
            self.description = "buffered"
            self.state = SimpleNamespace(value="active")
            self.data_queue = asyncio.Queue(maxsize=2)
            self.connected_clients: List[Dict[str, Any]] = []
            self.max_read_write_users = 1
            self.always_buffer = False

    port = DummyPort()
    pm.ports["pbuf"] = port

    # Default behavior: no clients -> data only logged
    assert await pm.send_data("pbuf", b"A") is True
    assert port.data_queue.empty()

    # Force enqueue with zero clients
    assert await pm.send_data("pbuf", b"B", require_clients=False) is True
    assert port.data_queue.get_nowait() == b"B"

    # Fill queue and ensure oldest entry is dropped when full
    await port.data_queue.put(b"1")
    await port.data_queue.put(b"2")
    assert await pm.send_data("pbuf", b"3", require_clients=False) is True
    first = port.data_queue.get_nowait()
    second = port.data_queue.get_nowait()
    assert first == b"2" and second == b"3"
    # Drop-oldest eviction is counted (surfaced via UnifiedPortWrapper.get_status())
    assert port.dropped_chunks == 1


@pytest.mark.asyncio
async def test_write_permissions_and_queuefull(monkeypatch):
    # Stub DataLogger
    from openmux.server import data_logger as dl_mod

    dummy = DummyDataLogger()
    monkeypatch.setattr(dl_mod.DataLogger, "get", classmethod(lambda cls: dummy))

    pm = PortManager([])

    # Register a federated port and block write when disconnected
    rp = FakeRemoteProxy("rfX", server_id="Sx")
    rp.is_connected = False
    meta = SimpleNamespace(name="rfX", origin_server=rp.metadata.origin_server)
    await pm.register_federated_port(meta, rp)
    blocked = await pm.write_to_port("rfX", b"hello", client_id="fed:relay")
    assert blocked is False

    # Unified port queue full branch
    adapter = FakeUnifiedAdapter()
    up = FakeUnifiedPort("uQ")
    adapter.ports["uQ"] = up
    pm.set_unified_adapters([adapter])
    # Ensure wrapper exists in ports
    w = pm.get_port("uQ")
    assert w is not None and hasattr(w, "data_queue") and hasattr(w, "connected_clients")
    # Replace queue with size=1 and add a client to enable queuing
    w.data_queue = asyncio.Queue(maxsize=1)
    w.connected_clients.append({"client_id": "c", "mode": "read-only"})
    # Fill queue and trigger drop-oldest on next send
    w.data_queue.put_nowait(b"A")
    ok = await pm.send_data("uQ", b"B")
    assert ok is True
    assert w.data_queue.get_nowait() == b"B"


@pytest.mark.asyncio
async def test_federated_last_client_closes_session(monkeypatch):
    # Stub DataLogger
    from openmux.server import data_logger as dl_mod

    dummy = DummyDataLogger()
    monkeypatch.setattr(dl_mod.DataLogger, "get", classmethod(lambda cls: dummy))

    pm = PortManager([])

    rp = FakeRemoteProxy("rfC", server_id="SC")
    meta = SimpleNamespace(name="rfC", origin_server=rp.metadata.origin_server)
    await pm.register_federated_port(meta, rp)

    # Add a single client, then remove it, expecting close session path
    rp.connected_clients.append({"client_id": "one", "mode": "read-only"})
    removed = await pm.remove_client_from_port("rfC", "one")
    assert removed is True


@pytest.mark.asyncio
async def test_remove_client_unified_wrapper_path():
    # Prepare unified adapter with a port but do not cache wrapper in pm.ports
    pm = PortManager([])
    adapter = FakeUnifiedAdapter()
    up = FakeUnifiedPort("uR")
    adapter.ports["uR"] = up
    pm.set_unified_adapters([adapter])
    # Ensure wrapper is not cached; call remove will use get_port path
    res = await pm.remove_client_from_port("uR", "nope")
    assert res is False


@pytest.mark.asyncio
async def test_add_client_to_port_dedupes_by_client_id():
    # Regression (issue #54): re-adding a client_id that already has a record
    # must update that record in place, never append a duplicate that skews
    # read-write slot accounting.
    class Port:
        def __init__(self):
            self.connected_clients: List[Dict[str, Any]] = []
            self.client_queues: Dict[str, Any] = {}
            self.max_read_write_users = 1

    pm = PortManager([])
    pm.ports["p1"] = Port()
    port = pm.ports["p1"]

    # A read-write client occupies the only seat.
    assert await pm.add_client_to_port("p1", "rw1", "u1", "read-write") is True
    # Re-adding the same client_id is an in-place seat update (mode change).
    assert await pm.add_client_to_port("p1", "rw1", "u1", "read-only") is True
    recs = [c for c in port.connected_clients if c.get("client_id") == "rw1"]
    assert len(recs) == 1
    assert recs[0]["mode"] == "read-only"
    # The in-place update keeps the existing delivery queue: a live forwarding
    # task may hold a reference to it, replacing it would orphan new data.
    queue_ref = port.client_queues["rw1"]
    assert await pm.add_client_to_port("p1", "rw1", "u1", "read-write") is True
    assert port.client_queues["rw1"] is queue_ref
    # The seat is occupied again by rw1 itself; a NEW read-write client is
    # rejected, and rw1 did not lose its own seat by counting itself.
    assert await pm.add_client_to_port("p1", "rw2", "u2", "read-write") is False
    # A new read-only client is always admitted and recorded once.
    assert await pm.add_client_to_port("p1", "ro1", "u3", "read-only") is True
    assert sum(1 for c in port.connected_clients if c.get("client_id") == "ro1") == 1


def test_wrapper_connected_reads_live_state():
    # Regression: a unified port without an ``is_connected`` attribute (e.g.
    # an on-demand command port registered before its first spawn) must
    # report its CURRENT ``is_running`` value, not the one captured once at
    # registration. The old frozen value kept the web console's info panel
    # and the 'Port is disconnected on server' banner stuck for such ports.
    pm = PortManager([])
    fake = SimpleNamespace(name="live1", is_running=False, description="d")
    adapter = SimpleNamespace()
    adapter.get_adapter_type = lambda: "command"
    adapter.ports = {"live1": fake}
    pm.set_unified_adapters([adapter])
    wrapper = pm.get_port("live1")
    assert wrapper is not None
    st = wrapper.get_status()
    assert st["is_running"] is False
    assert st["connected"] is False
    # The underlying port starts; the wrapper must report the new state.
    fake.is_running = True
    st2 = wrapper.get_status()
    assert st2["is_running"] is True
    assert st2["connected"] is True
    # Explicit is_connected (serial/loopback/tcp style) still wins.
    fake2 = SimpleNamespace(name="live2", is_running=False, description="d", is_connected=True)
    adapter2 = SimpleNamespace()
    adapter2.get_adapter_type = lambda: "loopback"
    adapter2.ports = {"live2": fake2}
    pm2 = PortManager([])
    pm2.set_unified_adapters([adapter2])
    wrapper2 = pm2.get_port("live2")
    assert wrapper2 is not None
    assert wrapper2.get_status()["connected"] is True
