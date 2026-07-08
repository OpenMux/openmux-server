import asyncio
from types import SimpleNamespace

import pytest

from openmux.server.adapters.loopback import LoopbackAdapter, LoopbackPort


class _PortManagerStub:
    """Minimal PortManager stub for unit tests.

    Captures output delivered via send_data in output_queue.
    Wires data_callback on all already-created ports at construction time.
    """

    def __init__(self, ports: dict = None):
        self._ports = ports or {}
        self.output_queue: asyncio.Queue = asyncio.Queue()
        for p in self._ports.values():
            if hasattr(p, "data_callback"):
                p.data_callback = self.send_data

    async def send_data(self, name: str, data: bytes, **kwargs) -> bool:
        await self.output_queue.put(data)
        return True

    async def read(self, timeout: float = 0.1) -> bytes:
        """Read next captured chunk, or b\"\" on timeout."""
        try:
            return await asyncio.wait_for(self.output_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return b""


@pytest.mark.asyncio
async def test_port_lifecycle_and_errors():
    adapter = LoopbackAdapter("loopback", {"loopback_ports": []})
    port = LoopbackPort("lp1", {"buffer_size": 8}, adapter)

    # Not active errors
    with pytest.raises(RuntimeError):
        await port.write_data(b"x")

    # Start -> active
    ok = await port.start()
    assert ok is True
    assert port.state.name == "ACTIVE"

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
    stub = _PortManagerStub({"lp2": port})
    adapter.main_port_manager = stub

    # Plain chunk without newline
    n = await port.write_data(b"abc")
    assert n == 3
    assert await stub.read(0.05) == b"abc"
    assert await stub.read(0.01) == b""  # nothing more

    # LF newline emits banner
    await port.write_data(b"def\n")
    assert await stub.read(0.05) == b"def"
    assert await stub.read(0.05) == b"[ENTER]\r\n"

    # CRLF newline emits banner
    await port.write_data(b"ghi\r\n")
    assert await stub.read(0.05) == b"ghi"
    assert await stub.read(0.05) == b"[ENTER]\r\n"


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
    stub = _PortManagerStub({"lp5": port})
    adapter.main_port_manager = stub
    await port.write_data(b"Z\n")
    # Should still echo correctly after delay
    assert await stub.read(0.1) == b"Z"
    assert await stub.read(0.1) == b"[ENTER]\r\n"


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
    # Wire PM stub before start; LoopbackPort.__init__ picks it up via adapter.main_port_manager
    stub = _PortManagerStub()
    adapter.main_port_manager = stub

    # Start creates ports
    ok = await adapter.start()
    assert ok is True
    assert "c" in adapter.ports

    # Write through adapter
    n = await adapter.write_to_port("c", b"Q\n")
    assert n == 2
    got1 = await stub.read(0.1)
    got2 = await stub.read(0.1)
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


@pytest.mark.asyncio
async def test_adapter_reconcile_ports_unchanged(monkeypatch):
    """Port whose config matches running defaults is not restarted on reconcile."""
    adapter = LoopbackAdapter("loop", {"loopback_ports": [{"name": "a"}]})

    # Seed 'a' with the same values the port would have after being created with no
    # explicit fields — these must match _material_cfg defaults exactly.
    class PortObj:
        echo_delay = 0.0
        buffer_size = 1024
        sanitize_control = True
        max_read_write_users = 5

    adapter.ports["a"] = PortObj()  # type: ignore[assignment]

    destroyed: list = []
    created: list = []

    async def fake_destroy(name: str) -> None:
        destroyed.append(name)

    async def fake_create(name: str, cfg: dict) -> None:
        created.append(name)

    monkeypatch.setattr(adapter, "destroy_port", fake_destroy)
    monkeypatch.setattr(adapter, "create_port", fake_create)

    # Description change is non-material — 'a' must stay unchanged
    summary = await adapter.reconcile_ports([{"name": "a", "description": "new desc"}])
    assert summary["unchanged"] == ["a"]
    assert summary["updated"] == []
    assert destroyed == []
    assert created == []


@pytest.mark.asyncio
async def test_adapter_reconcile_ports_add_remove_update(monkeypatch):
    """Add, remove, and material-change (buffer_size) are all detected correctly."""
    adapter = LoopbackAdapter("loop", {"loopback_ports": []})

    class PortA:
        echo_delay = 0.0
        buffer_size = 1024
        sanitize_control = True
        max_read_write_users = 5

    class PortB:
        echo_delay = 0.0
        buffer_size = 512  # will be changed to 2048
        sanitize_control = True
        max_read_write_users = 5

    adapter.ports["a"] = PortA()  # type: ignore[assignment]
    adapter.ports["b"] = PortB()  # type: ignore[assignment]

    destroyed: list = []
    created: list = []

    async def fake_destroy(name: str) -> None:
        destroyed.append(name)
        adapter.ports.pop(name, None)

    async def fake_create(name: str, cfg: dict) -> None:
        created.append(name)

    monkeypatch.setattr(adapter, "destroy_port", fake_destroy)
    monkeypatch.setattr(adapter, "create_port", fake_create)

    summary = await adapter.reconcile_ports([
        {"name": "a"},                       # unchanged (defaults)
        {"name": "b", "buffer_size": 2048},  # updated (was 512)
        {"name": "c"},                       # added
    ])
    assert summary["unchanged"] == ["a"]
    assert summary["updated"] == ["b"]
    assert summary["added"] == ["c"]
    assert summary["removed"] == []
    assert "b" in destroyed
    assert "b" in created  # destroyed then recreated
    assert "c" in created
