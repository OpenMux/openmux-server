import asyncio
from typing import Any, Dict, List, Optional

import pytest

from openmux.server.adapters.tcp_initiator import TcpInitiatorAdapter, TcpInitiatorPort


class FakeReader:
    def __init__(self, chunks: Optional[List[bytes]] = None):
        self.chunks = list(chunks or [])

    async def read(self, n: int) -> bytes:  # pragma: no cover - covered via tests
        if self.chunks:
            return self.chunks.pop(0)
        await asyncio.sleep(0)
        return b""


class FakeWriter:
    def __init__(self):
        self.buffer: bytearray = bytearray()
        self.closed = False
        self.close_calls = 0
        self.wait_closed_calls = 0

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True

    async def wait_closed(self) -> None:
        self.wait_closed_calls += 1
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_port_init_requires_host_and_port():
    adapter = TcpInitiatorAdapter("ti", {})
    with pytest.raises(ValueError):
        TcpInitiatorPort("p1", {"port": 123}, adapter)  # missing host
    with pytest.raises(ValueError):
        TcpInitiatorPort("p1", {"host": "h"}, adapter)  # missing port


@pytest.mark.asyncio
async def test_port_connect_tls_and_read_loop(monkeypatch):
    # Patch asyncio.open_connection to capture ssl context and return fakes
    captured: Dict[str, Any] = {}

    async def fake_open_connection(host, port, ssl=None):  # type: ignore[override]
        captured["host"] = host
        captured["port"] = port
        captured["ssl"] = ssl
        # Reader returns one chunk then EOF to stop read loop
        return FakeReader([b"hello", b""]), FakeWriter()

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    adapter = TcpInitiatorAdapter("ti", {})
    cfg = {"host": "example.com", "port": 9999, "use_tls": True, "ssl_verify": False, "auto_reconnect": False}
    port = TcpInitiatorPort("p1", cfg, adapter)

    got: Dict[str, Any] = {}

    async def cb(name: str, data: bytes):
        got.setdefault("data", []).append((name, data))

    port.data_callback = cb
    ok = await port._connect()
    assert ok is True
    assert port.is_connected is True
    # SSL context should be created and verification disabled
    assert captured.get("ssl") is not None
    import ssl as _ssl

    assert getattr(captured["ssl"], "verify_mode", None) == _ssl.CERT_NONE
    # Allow read loop to process the one chunk
    await asyncio.sleep(0.01)
    # After receiving empty chunk, connection should be marked disconnected by read loop
    assert got["data"][0] == ("p1", b"hello")

    await port.stop()


@pytest.mark.asyncio
async def test_port_write_direct_and_error(monkeypatch):
    # open_connection returns fake streams
    async def fake_open_connection(host, port, ssl=None):  # type: ignore[override]
        return FakeReader([b"\n\n\n", b""]), FakeWriter()

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    adapter = TcpInitiatorAdapter("ti", {})
    cfg = {"host": "h", "port": 1, "auto_reconnect": False, "enable_batching": False}
    port = TcpInitiatorPort("p1", cfg, adapter)
    await port._connect()

    # Normal direct write (no batching)
    n = await port.write_data(b"abc")
    assert n == 3

    # Simulate write error by patching writer.write to raise
    assert port.writer is not None
    orig_write = port.writer.write
    def raising_write(data: bytes) -> None:  # type: ignore[override]
        raise RuntimeError("boom")
    port.writer.write = raising_write  # type: ignore[assignment]
    n2 = await port.write_data(b"x")
    assert n2 == 0
    assert port.is_connected is False
    # restore for cleanup safety
    if port.writer is not None:
        port.writer.write = orig_write  # type: ignore[assignment]
    await port.stop()


@pytest.mark.asyncio
async def test_port_batched_write_and_flush(monkeypatch):
    # Provide writer and reader
    wr = FakeWriter()

    async def fake_open_connection(host, port, ssl=None):  # type: ignore[override]
        return FakeReader([b"\n\n", b""]), wr

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    adapter = TcpInitiatorAdapter("ti", {})
    cfg = {
        "host": "h",
        "port": 1,
        "auto_reconnect": False,
        "enable_batching": True,
        "batch_size": 1024,
        "batch_timeout": 0.001,
    }
    port = TcpInitiatorPort("p1", cfg, adapter)
    await port._connect()

    # Queue two small writes; expect flush by timeout
    await port.write_data(b"hello")
    await port.write_data(b" world")
    await asyncio.sleep(0.02)
    assert wr.buffer == b"hello world"
    await port.stop()


@pytest.mark.asyncio
async def test_port_flush_error_when_writer_none(monkeypatch):
    wr = FakeWriter()
    async def fake_open_connection(host, port, ssl=None):  # type: ignore[override]
        return FakeReader([b"\n", b""]), wr
    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    adapter = TcpInitiatorAdapter("ti", {})
    port = TcpInitiatorPort("p1", {"host": "h", "port": 1, "auto_reconnect": False, "enable_batching": True, "batch_timeout": 0.001}, adapter)
    await port._connect()
    await port.write_data(b"x")
    # Force writer to None to exercise error branch in flush loop
    port.writer = None  # type: ignore[assignment]
    await asyncio.sleep(0.01)
    await port.stop()


@pytest.mark.asyncio
async def test_port_disconnect(monkeypatch):
    wr = FakeWriter()

    async def fake_open_connection(host, port, ssl=None):  # type: ignore[override]
        return FakeReader([b"\n", b""]), wr

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    adapter = TcpInitiatorAdapter("ti", {})
    port = TcpInitiatorPort("p1", {"host": "h", "port": 1, "auto_reconnect": False}, adapter)
    await port._connect()
    await port._disconnect()
    assert wr.close_calls == 1
    assert wr.wait_closed_calls >= 1
    assert port.is_connected is False


def test_adapter_validate_config_variants():
    # Preferred dict with tcp_initiator_ports
    assert TcpInitiatorAdapter.validate_config({"tcp_initiator_ports": [{"name": "a", "host": "h", "port": 1}]})
    # Legacy dict with client_initiator_ports
    assert TcpInitiatorAdapter.validate_config({"client_initiator_ports": [{"name": "a", "host": "h", "port": 1}]})
    # validate_config expects a dict; legacy list case is covered via get_port_configurations()
    # Bad shapes
    assert not TcpInitiatorAdapter.validate_config({})
    assert not TcpInitiatorAdapter.validate_config({"tcp_initiator_ports": ["bad"]})


def test_adapter_get_port_configurations_variants():
    a1 = TcpInitiatorAdapter("ti1", {"tcp_initiator_ports": [{"name": "a", "host": "h", "port": 1}]})
    assert a1.get_port_configurations()["a"]["port"] == 1
    a2 = TcpInitiatorAdapter("ti2", {"client_initiator_ports": [{"name": "b", "host": "h", "port": 2}]})
    assert a2.get_port_configurations()["b"]["port"] == 2
    a3 = TcpInitiatorAdapter("ti3", {})
    # Emulate legacy top-level list by directly assigning to config
    a3.config = [{"name": "c", "host": "h", "port": 3}]  # type: ignore[assignment]
    assert a3.get_port_configurations()["c"]["port"] == 3
    a4 = TcpInitiatorAdapter("ti4", {})
    assert a4.get_port_configurations() == {}


@pytest.mark.asyncio
async def test_adapter_create_start_stop_and_write(monkeypatch):
    # Patch TcpInitiatorPort.start to avoid spinning tasks
    async def fake_start(self):
        self.state = type("S", (), {"value": "active"})
        self.is_connected = False
        return True

    monkeypatch.setattr(TcpInitiatorPort, "start", fake_start, raising=True)

    adapter = TcpInitiatorAdapter("ti", {"tcp_initiator_ports": [{"name": "p1", "host": "h", "port": 1}]})
    # Provide port manager hooks (async) so adapter.create_port can await them
    class PM:
        async def register_unified_port(self, *a, **k):
            return None
        async def unregister_unified_port(self, *a, **k):
            return None
    adapter.main_port_manager = PM()
    ok = await adapter.start()
    assert ok is True
    assert adapter.is_running is True
    assert "p1" in adapter.ports

    # write_to_port missing
    n0 = await adapter.write_to_port("nope", b"x")
    assert n0 == 0

    # Provide a port with write_data method
    class P:
        def __init__(self):
            self.called = 0
            self.is_connected = True
            self.host = "h"
            self.port = 1
            self.use_tls = False
            self.auto_reconnect = True
        async def write_data(self, data: bytes) -> int:
            self.called += 1
            return len(data)

    adapter.ports["p1"] = P()  # type: ignore[assignment]
    n1 = await adapter.write_to_port("p1", b"abc")
    assert n1 == 3

    # Status queries
    si = adapter.get_status_info()
    assert si["type"] == "TCPInitiator"
    ps = await adapter.get_port_status("p1")
    assert ps["name"] == "p1"
    assert (await adapter.get_port_status("nope")).get("error")
    assert sorted(await adapter.list_ports()) == ["p1"]

    # Stop
    await adapter.stop()
    assert adapter.is_running is False


@pytest.mark.asyncio
async def test_adapter_handle_port_data_routes_to_port_manager():
    adapter = TcpInitiatorAdapter("ti", {})
    called: Dict[str, Any] = {}

    class PM:
        async def send_data(self, name: str, data: bytes) -> None:
            called["args"] = (name, data)

    adapter.main_port_manager = PM()
    await adapter._handle_port_data("p1", b"xyz")
    assert called["args"] == ("p1", b"xyz")


@pytest.mark.asyncio
async def test_adapter_reconcile_ports(monkeypatch):
    adapter = TcpInitiatorAdapter("ti", {"tcp_initiator_ports": [{"name": "a", "host": "h1", "port": 1}]})
    # Seed existing port 'a' with materialized config (all fields tracked by old_cfg)
    class PortObj:
        host = "h1"
        port = 1
        use_tls = False
        ssl_verify = True
        timeout = 10.0
        auto_reconnect = True
        reconnect_delay = 5.0
        # stored under private names in TcpInitiatorPort
        _batching_enabled = True
        _batch_size = 1024
        _batch_timeout = 0.015

    adapter.ports["a"] = PortObj()  # type: ignore[assignment]

    destroyed: List[str] = []
    created: List[str] = []

    async def fake_destroy(name):
        destroyed.append(name)

    async def fake_create(name, cfg):
        created.append(name)

    monkeypatch.setattr(adapter, "destroy_port", fake_destroy)
    monkeypatch.setattr(adapter, "create_port", fake_create)

    # New config: keep 'a' unchanged (with a description change) and add 'b'; remove 'c' if present
    new_cfg = {
        "tcp_initiator_ports": [
            {"name": "a", "host": "h1", "port": 1, "use_tls": False, "timeout": 10.0, "auto_reconnect": True, "description": "updated desc"},
            {"name": "b", "host": "h2", "port": 2},
        ]
    }

    summary = await adapter.reconcile_ports(new_cfg)
    assert summary["added"] == ["b"]
    assert summary["removed"] == []
    assert summary["updated"] == []
    assert summary["unchanged"] == ["a"]
    assert created == ["b"]
    assert destroyed == []
