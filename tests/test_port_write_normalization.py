import asyncio
import types

import pytest

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
async def test_openmux_write_data_returns_len(monkeypatch):
    """TcpInitiatorPort with openmux protocol writes via writer and returns byte count."""
    cfg = {
        "host": "h", "port": 1, "enable_batching": False,
        "protocol": {"type": "openmux", "remote_port": "r", "api_key": "k"},
    }
    port = TcpInitiatorPort("p", cfg, adapter=types.SimpleNamespace())  # type: ignore[arg-type]
    port.is_connected = True

    class _Writer:
        def __init__(self):
            self.buf = bytearray()
        def write(self, d):
            self.buf.extend(d)
        async def drain(self):
            return

    port.writer = _Writer()  # type: ignore[assignment]
    wrote = await port.write_data(b"xyz")
    assert wrote == 3


@pytest.mark.asyncio
async def test_loopback_write_data_round_trip():
    adapter = types.SimpleNamespace()
    lb = LoopbackPort("lb", {}, adapter=adapter)  # type: ignore[arg-type]
    await lb.start()

    # Wire a minimal PortManager stub so the primary I/O path is exercised.
    class _StubPM:
        def __init__(self):
            self.output_queue: asyncio.Queue = asyncio.Queue()

        async def send_data(self, name: str, data: bytes, **kwargs) -> bool:
            await self.output_queue.put(data)
            return True

        async def read(self, timeout: float = 0.1) -> bytes:
            try:
                return await asyncio.wait_for(self.output_queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                return b""

    stub = _StubPM()
    adapter.main_port_manager = stub
    lb.data_callback = stub.send_data

    wrote = await lb.write_data(b"hello")
    assert wrote == 5
    data = await stub.read(0.1)
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
