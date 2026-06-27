import asyncio
import types

import pytest

from openmux.server.adapters.client_initiator import OpenMuxClientPort
from openmux.server.adapters.command import CommandPort
from openmux.server.adapters.lifecycle import PortState
from openmux.server.adapters.loopback import LoopbackPort
from openmux.server.adapters.serial import SerialPortConfig, SerialPortWrapper
from openmux.server.adapters.tcp_initiator import TcpInitiatorPort


@pytest.mark.asyncio
async def test_tcp_initiator_write_data_returns_len(monkeypatch):
    port = TcpInitiatorPort("p", {"host": "localhost", "port": 1, "enable_batching": False}, adapter=types.SimpleNamespace())  # type: ignore[arg-type]
    # Simulate connected state
    port.is_connected = True

    class _DummyWriter:
        def __init__(self):
            self.buf = bytearray()

        def write(self, d):
            self.buf.extend(d)

        async def drain(self):
            return

    port.writer = _DummyWriter()  # type: ignore[assignment]
    wrote = await port.write_data(b"abc")
    assert wrote == 3


@pytest.mark.asyncio
async def test_openmux_client_write_data_returns_len(monkeypatch):
    # Minimal fake underlying connection
    class _FakeConn:
        async def connect(self):
            return True

        async def authenticate_with_key(self, key):
            return True

        async def connect_to_port(self, name):
            return True

        async def send_data(self, data):
            return True

        async def read_data(self):
            await asyncio.sleep(0.01)
            return b""

        async def close(self):
            return

    adapter_ns = types.SimpleNamespace()
    port = OpenMuxClientPort("p", {"host": "h", "port": 1, "remote_port": "r", "api_key": "k"}, adapter=adapter_ns)  # type: ignore[arg-type]
    # Inject fake connection and mark connected
    port.conn = _FakeConn()  # type: ignore[assignment]
    port.is_connected = True
    wrote = await port.write_data(b"xyz")
    assert wrote == 3


@pytest.mark.asyncio
async def test_loopback_write_data_round_trip():
    adapter = types.SimpleNamespace()
    lb = LoopbackPort("lb", {}, adapter=adapter)  # type: ignore[arg-type]
    await lb.start()

    # Wire a minimal PortManager stub so the primary I/O path is exercised.
    # Without PM the fallback no longer buffers (PM absence is an error per contract).
    class _StubPM:
        async def send_data_from_unified_port(self, name: str, data: bytes) -> bool:
            if lb.data_queue is not None:
                lb.data_queue.put_nowait(data)
            return True

    adapter.main_port_manager = _StubPM()

    wrote = await lb.write_data(b"hello")
    assert wrote == 5
    data = await lb.read_data(0.1)
    assert data == b"hello"


@pytest.mark.asyncio
async def test_serial_write_data_requires_connection(monkeypatch):
    cfg = SerialPortConfig(name="s1", description="d", device="/dev/null")
    wrapper = SerialPortWrapper(cfg, logger=__import__("logging").getLogger("test.serial"))
    # Not connected; expect RuntimeError
    with pytest.raises(RuntimeError):
        await wrapper.write_data(b"hi")


@pytest.mark.asyncio
async def test_command_port_write_data_no_writer_returns_zero():
    cp = CommandPort("c1", {"command": "echo"}, adapter=types.SimpleNamespace())  # type: ignore[arg-type]
    wrote = await cp.write_data(b"abc")
    assert wrote == 0
