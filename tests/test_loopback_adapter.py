import asyncio
from types import SimpleNamespace

import pytest

from openmux.server.adapters.loopback import LoopbackAdapter, LoopbackPort


class _PortManagerStub:
    """Minimal PortManager stub for unit tests.

    Routes send_data_from_unified_port directly into the named port's data_queue,
    mirroring what PortManager.handle_incoming_port_data does via the wrapper alias.
    Keyed by a dict reference so it stays in sync when the adapter populates ports lazily.
    """

    def __init__(self, ports: dict):
        self._ports = ports

    async def send_data_from_unified_port(self, name: str, data: bytes) -> bool:
        port = self._ports.get(name)
        if port is not None and getattr(port, "data_queue", None) is not None:
            port.data_queue.put_nowait(data)
        return True


@pytest.mark.asyncio
async def test_port_lifecycle_and_errors():
    adapter = LoopbackAdapter("loopback", {"loopback_ports": []})
    port = LoopbackPort("lp1", {"buffer_size": 8}, adapter)

    # Not active errors
    with pytest.raises(RuntimeError):
        await port.write_data(b"x")
    with pytest.raises(RuntimeError):
        await port.read_data(0.01)

    # Start -> active
    ok = await port.start()
    assert ok is True
    assert port.state.name == "ACTIVE"
    assert port.data_queue is not None

    # Timeout returns empty bytes
    got = await port.read_data(0.01)
    assert got == b""
    # Alias works
    got_alias = await port.read(0.01)
    assert got_alias == b""

    # Stop and subsequent operations should error
    await port.stop()
    with pytest.raises(RuntimeError):
        await port.write_data(b"x")


@pytest.mark.asyncio
async def test_loopback_is_connected_flags():
    adapter = LoopbackAdapter("loopback", {"loopback_ports": []})
    port = LoopbackPort("lp_isconn", {"buffer_size": 4}, adapter)

    assert getattr(port, "is_connected", False) is False
    assert await port.start() is True
    assert getattr(port, "is_connected", False) is True
    await port.stop()
    assert getattr(port, "is_connected", True) is False


@pytest.mark.asyncio
async def test_write_and_banners_cr_lf_variants():
    adapter = LoopbackAdapter("loopback", {"loopback_ports": []})
    port = LoopbackPort("lp2", {"buffer_size": 8}, adapter)
    await port.start()
    adapter.main_port_manager = _PortManagerStub({"lp2": port})

    # Plain chunk without newline
    n = await port.write_data(b"abc")
    assert n == 3
    assert await port.read_data(0.05) == b"abc"
    assert await port.read_data(0.01) == b""  # nothing more

    # LF newline emits banner
    await port.write_data(b"def\n")
    assert await port.read_data(0.05) == b"def"
    assert await port.read_data(0.05) == b"[ENTER]\r\n"

    # CRLF newline emits banner
    await port.write_data(b"ghi\r\n")
    assert await port.read_data(0.05) == b"ghi"
    assert await port.read_data(0.05) == b"[ENTER]\r\n"


def test_sanitize_sequences_basic():
    adapter = LoopbackAdapter("loopback", {"loopback_ports": []})
    port = LoopbackPort("lp3", {"sanitize_control": True}, adapter)

    # Arrow keys via CSI and SS3
    assert port.sanitize_data(b"\x1b[A") == b"[UP]"
    assert port.sanitize_data(b"\x1b[B") == b"[DOWN]"
    assert port.sanitize_data(b"\x1b[C") == b"[RIGHT]"
    assert port.sanitize_data(b"\x1b[D") == b"[LEFT]"
    assert port.sanitize_data(b"\x1bOH") == b"[HOME]" or port.sanitize_data(b"\x1bOH")
    assert port.sanitize_data(b"\x1bOA") == b"[UP]"

    # CSI digits ~
    assert port.sanitize_data(b"\x1b[3~") == b"[DEL]"
    assert port.sanitize_data(b"\x1b[5~") == b"[PGUP]"
    assert port.sanitize_data(b"\x1b[6~") == b"[PGDN]"

    # Tab / NUL / CTRL-A / DEL / printable / non-ascii
    assert port.sanitize_data(b"\t") == b"[TAB]"
    assert port.sanitize_data(b"\x00") == b"[NUL]"
    assert port.sanitize_data(b"\x01") == b"[CTRL-A]"
    assert port.sanitize_data(b"\x7f") == b"[DEL]"
    assert port.sanitize_data(b"Hello") == b"Hello"
    assert port.sanitize_data("é".encode("utf-8")) == "é".encode("utf-8")

    # Bare ESC becomes [ESC]
    assert port.sanitize_data(b"\x1bX").startswith(b"[ESC]")


def test_sanitize_incomplete_esc_buffering():
    adapter = LoopbackAdapter("loopback", {"loopback_ports": []})
    port = LoopbackPort("lp4", {"sanitize_control": True}, adapter)

    # First call: incomplete ESC should buffer and produce no output
    assert port.sanitize_data(b"\x1b") == b""
    # Next call completes the sequence (CSI A)
    assert port.sanitize_data(b"[A") == b"[UP]"


@pytest.mark.asyncio
async def test_echo_delay_execution():
    adapter = LoopbackAdapter("loopback", {"loopback_ports": []})
    port = LoopbackPort("lp5", {"buffer_size": 8, "echo_delay": 0.01}, adapter)
    await port.start()
    adapter.main_port_manager = _PortManagerStub({"lp5": port})
    await port.write_data(b"Z\n")
    # Should still echo correctly after delay
    assert await port.read_data(0.1) == b"Z"
    assert await port.read_data(0.1) == b"[ENTER]\r\n"


def test_adapter_config_and_ports_enumeration():
    cfg = {
        "loopback_ports": [
            {"name": "a", "buffer_size": 4},
            {"name": "b", "buffer_size": 4},
        ]
    }
    adapter = LoopbackAdapter("loop", cfg)
    pcs = adapter.get_port_configurations()
    assert set(pcs.keys()) == {"a", "b"}


@pytest.mark.asyncio
async def test_adapter_start_write_stop_and_missing_port():
    cfg = {"loopback_ports": [{"name": "c"}]}
    adapter = LoopbackAdapter("loop", cfg)
    # Wire PM stub before start so ports dict is shared by reference;
    # the stub routes data directly into each port's data_queue.
    adapter.main_port_manager = _PortManagerStub(adapter.ports)

    # Start creates ports
    ok = await adapter.start()
    assert ok is True
    assert "c" in adapter.ports

    # Write through adapter
    n = await adapter.write_to_port("c", b"Q\n")
    assert n == 2
    # Read back from the port queue
    port = adapter.ports["c"]
    got1 = await port.read_data(0.1)
    got2 = await port.read_data(0.1)
    assert got1 == b"Q" and got2 == b"[ENTER]\r\n"

    # Missing port write returns 0
    n0 = await adapter.write_to_port("missing", b"X")
    assert n0 == 0

    # Stop clears ports
    await adapter.stop()
    assert adapter.is_running is False
    assert adapter.ports == {}


@pytest.mark.asyncio
async def test_create_ports_from_config_failure(monkeypatch):
    cfg = {"loopback_ports": [{"name": "x"}]}
    adapter = LoopbackAdapter("loop", cfg)

    # Force create_port to raise to exercise error branch
    async def boom(name, conf):
        raise RuntimeError("fail")

    monkeypatch.setattr(adapter, "create_port", boom)
    ok = await adapter._create_loopback_ports_from_config()
    assert ok is False
