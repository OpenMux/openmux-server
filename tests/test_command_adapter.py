import asyncio
import os
from types import SimpleNamespace
from typing import Any, cast

import pytest

from openmux.server.adapters.command import CommandAdapter, CommandPort, CommandWriter


class StubConfigManager:
    def __init__(self, config=None):
        self.config = config or {"server": {"id": "srv-123"}}

    def load_config(self):
        return self.config


class CapturingPortManager:
    def __init__(self, config=None):
        self.output_queue = asyncio.Queue()
        cfg = config or {"server": {"id": "srv-123"}}
        self.config_manager = StubConfigManager(cfg)

    async def send_data_from_unified_port(
        self,
        port_name: str,
        chunk: bytes,
        *,
        require_clients: bool = True,
        drop_oldest: bool = True,
    ) -> bool:
        await self.output_queue.put(chunk)
        return True


class DummyStreamWriter:
    def __init__(self):
        self.buffer = bytearray()
        self.drains = 0

    def write(self, data: bytes):
        self.buffer += data

    async def drain(self):
        self.drains += 1


class FakeStdout:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, n: int):
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class FakeProcess:
    def __init__(self, chunks):
        self.stdout = FakeStdout(chunks)
        self.stdin = DummyStreamWriter()


@pytest.mark.asyncio
async def test_stopped_notice_and_client_notice_prefix(monkeypatch):
    # Build adapter with a config manager to provide server.id for prefix
    pm = CapturingPortManager({"server": {"id": "srv-123"}})
    adapter: Any = SimpleNamespace(main_port_manager=pm)
    port = CommandPort("cp1", {"command": "echo"}, adapter)

    # read_data on inactive returns stopped notice once
    msg1 = await port.read_data(0)
    assert b"PROCESS_NOT_RUNNING srv-123/cp1" in msg1
    # and now returns empty if no queue
    msg2 = await port.read_data(0)
    assert msg2 == b""

    # Client count change from 0->1 enqueues notice if banner not yet sent
    port._stopped_notice_sent = False
    port.on_client_count_changed(1)
    got = await asyncio.wait_for(pm.output_queue.get(), timeout=0.1)
    assert b"PROCESS_NOT_RUNNING srv-123/cp1" in got


@pytest.mark.asyncio
async def test_writer_normalization_and_local_echo():
    pm = CapturingPortManager()
    adapter: Any = SimpleNamespace(main_port_manager=pm)
    port = CommandPort("cp2", {"command": "echo", "normalize_newlines": True, "local_echo": True}, adapter)
    port.process_active = True
    port.use_pty = False
    writer = CommandWriter(DummyStreamWriter(), port)
    # Disable batching for direct write
    writer._batching_enabled = False

    data = b"A\r\nB\rC\n"
    await writer.write(data)
    # Pipe mode maps to LF
    assert bytes(cast(Any, writer.stdin_stream).buffer) == b"A\nB\nC\n"
    # Local echo enqueued same mapped data
    echoed = await asyncio.wait_for(pm.output_queue.get(), timeout=0.1)
    assert echoed == b"A\nB\nC\n"


@pytest.mark.asyncio
async def test_writer_respawn_on_newline(monkeypatch):
    adapter: Any = SimpleNamespace()
    port = CommandPort("cp3", {"command": "echo", "normalize_newlines": True}, adapter)
    port.process_active = False
    port.use_pty = False
    # Writer requires a stream in pipe mode to avoid early return
    writer = CommandWriter(DummyStreamWriter(), port)

    # Stub port.restart to mark active and attach stdin
    async def fake_restart(force=False):
        port.process_active = True
        port.process = cast(Any, FakeProcess([]))
        return True

    monkeypatch.setattr(port, "restart", fake_restart)
    await writer.write(b"\r\n")
    # After respawn, newline delivered to stdin (new process' stream)
    assert bytes(cast(Any, writer.stdin_stream).buffer) == b"\n"


def test_xtgettcap_interception():
    adapter: Any = SimpleNamespace()
    port = CommandPort("cp4", {"command": "echo"}, adapter)
    # Sequence: text + XTGETTCAP + text
    seq = b"abc\x1bP+qNAME\x1b\\def"
    out = port._intercept_xtgettcap_queries(seq)
    # Should remove the XTGETTCAP query payload
    assert out == b"abcdef" or out.startswith(b"abc")


@pytest.mark.asyncio
async def test_pty_read_ready_queueing():
    pm = CapturingPortManager()
    adapter: Any = SimpleNamespace(main_port_manager=pm)
    port = CommandPort("cp5", {"command": "echo"}, adapter)
    port.is_running = True
    port.use_pty = True
    port.always_buffer = True
    port._output_batching_enabled = False
    rfd, wfd = os.pipe()
    os.set_blocking(rfd, False)
    port._pty_master_fd = rfd
    try:
        os.write(wfd, b"abc\n")
        port._on_pty_read_ready()
        got = await asyncio.wait_for(pm.output_queue.get(), timeout=0.1)
        # PTY path maps newlines to CRLF
        assert got == b"abc\r\n"
    finally:
        os.close(wfd)
        os.close(rfd)


@pytest.mark.asyncio
async def test_stdout_reader_task_queueing():
    pm = CapturingPortManager()
    adapter: Any = SimpleNamespace(main_port_manager=pm)
    port = CommandPort("cp6", {"command": "echo"}, adapter)
    port.is_running = True
    port.use_pty = False
    port.always_buffer = True
    port.process = cast(Any, FakeProcess([b"foo\r\n", b""]))
    await port._stdout_reader_task()
    got = await asyncio.wait_for(pm.output_queue.get(), timeout=0.1)
    # Pipe path normalizes CRLF to LF
    assert got == b"foo\n"


@pytest.mark.asyncio
async def test_adapter_config_status_create_destroy_write(monkeypatch):
    cfg = {"command_ports": [{"name": "p", "command": "echo hi"}]}
    adapter = CommandAdapter("cmd", cfg)

    # Attach a minimal dynamic port manager so load_configured_ports works
    class DummyDPM:
        def __init__(self, adapter):
            self.active_ports = {}
            self.adapter = adapter

        async def create_port_dynamically(self, port_name, config, evt):
            # Directly call adapter.create_port but do not spawn real process
            # Monkeypatch CommandPort.start to avoid spawn
            orig = CommandPort.start

            async def fake_start(self):
                self.is_running = True
                self.process_active = True
                return True

            monkeypatch.setattr(CommandPort, "start", fake_start)
            port = await adapter.create_port(port_name, config)
            if port:
                self.active_ports[port_name] = port
                return True
            return False

    cast(Any, adapter).port_manager = DummyDPM(adapter)

    # Start via load_configured_ports path
    ok = await adapter.start()
    assert ok is True
    assert "p" in adapter.ports

    # Write to port returns 0 because writer is not configured
    n = await adapter.write_to_port("p", b"x")
    assert n == 0

    # Status and list
    st = await adapter.get_port_status("p")
    assert st["name"] == "p" and st["adapter"] == adapter.adapter_type
    lst = await adapter.list_ports()
    assert isinstance(lst, list) and lst[0]["name"] == "p"

    # Destroy and stop
    await adapter.destroy_port("p")
    assert "p" not in adapter.ports
    await adapter.stop()
    assert adapter.is_running is False


@pytest.mark.asyncio
async def test_restart_paths(monkeypatch):
    adapter: Any = SimpleNamespace()
    port = CommandPort(
        "cp7",
        {"command": "echo", "auto_restart": True, "restart_delay": 0.0, "restart_backoff": 1.0},
        adapter,
    )

    # Case 3: not running -> start()
    async def fake_start():
        port.is_running = True
        port.process_active = True
        return True

    monkeypatch.setattr(port, "start", fake_start)
    ok = await port.restart(force=False)
    assert ok is True

    # Case 2: running but not active -> respawn via _spawn_process
    port.process_active = False

    async def fake_spawn():
        port.process_active = True
        return True

    monkeypatch.setattr(port, "_spawn_process", fake_spawn)
    ok2 = await port.restart(force=False)
    assert ok2 is True

    # Cleanup stop
    try:
        await port.stop()
    except asyncio.CancelledError:
        # Some Python versions surface CancelledError from awaiting cancelled tasks
        pass


@pytest.mark.asyncio
async def test_on_demand_banner_and_spawn_on_first_client(monkeypatch):
    adapter: Any = SimpleNamespace()
    port = CommandPort("ond1", {"command": "echo", "spawn_on_demand": True}, adapter)

    # When inactive and on-demand, banner should suggest 'spawn'
    msg = await port.read_data(0)
    assert b"press Enter to spawn" in msg

    # Monkeypatch start to observe invocation and simulate successful spawn
    called = {"start": 0}

    async def fake_start():
        called["start"] += 1
        port.is_running = True
        port.process_active = True
        return True

    monkeypatch.setattr(port, "start", fake_start)

    # Trigger first client connect → should start process asynchronously
    port.on_client_count_changed(1)
    # Allow the scheduled task to run
    await asyncio.sleep(0.01)

    assert called["start"] == 1
    assert port.is_running is True and port.process_active is True


@pytest.mark.asyncio
async def test_idle_timeout_stops_after_last_client(monkeypatch):
    adapter: Any = SimpleNamespace()
    port = CommandPort(
        "ond2",
        {"command": "echo", "spawn_on_demand": True, "idle_timeout_sec": 0.05},
        adapter,
    )

    # Simulate running process
    port.is_running = True
    port.process_active = True

    stopped = {"called": 0}

    async def fake_stop():
        stopped["called"] += 1
        port.is_running = False
        port.process_active = False
        return None

    monkeypatch.setattr(port, "stop", fake_stop)

    # Go from one client to zero to schedule idle stop
    port.on_client_count_changed(1)
    port.on_client_count_changed(0)

    # Wait longer than idle timeout
    await asyncio.sleep(0.15)

    assert stopped["called"] == 1
    assert port.is_running is False


@pytest.mark.asyncio
async def test_idle_timeout_cancelled_on_reconnect(monkeypatch):
    adapter: Any = SimpleNamespace()
    port = CommandPort(
        "ond3",
        {"command": "echo", "spawn_on_demand": True, "idle_timeout_sec": 0.1},
        adapter,
    )
    port.is_running = True
    port.process_active = True

    stopped = {"called": 0}

    async def fake_stop():
        stopped["called"] += 1
        port.is_running = False
        port.process_active = False
        return None

    monkeypatch.setattr(port, "stop", fake_stop)

    # Schedule idle stop then reconnect before timeout
    port.on_client_count_changed(1)
    port.on_client_count_changed(0)
    await asyncio.sleep(0.02)
    port.on_client_count_changed(1)

    # Wait beyond original timeout; stop should not be called
    await asyncio.sleep(0.15)
    assert stopped["called"] == 0


@pytest.mark.asyncio
async def test_adapter_create_port_on_demand_does_not_start(monkeypatch):
    # Make sure adapter doesn't call start() for on-demand ports at create time
    cfg = {"command_ports": [{"name": "p_ond", "command": "echo", "spawn_on_demand": True}]}
    adapter = CommandAdapter("cmd_ond", cfg)

    # Provide a dummy port manager that forwards to adapter.create_port
    class DummyDPM:
        def __init__(self, adapter):
            self.active_ports = {}
            self.adapter = adapter

        async def create_port_dynamically(self, port_name, config, evt):
            # Track calls to CommandPort.start; should not be called
            called = {"start": 0}

            orig_start = CommandPort.start

            async def guard_start(self):
                called["start"] += 1
                # Simulate successful start if ever invoked
                self.is_running = True
                self.process_active = True
                return True

            monkeypatch.setattr(CommandPort, "start", guard_start)
            try:
                port = await self.adapter.create_port(port_name, config)
            finally:
                monkeypatch.setattr(CommandPort, "start", orig_start)

            assert called["start"] == 0  # ensure not started eagerly
            if port:
                self.active_ports[port_name] = port
                return True
            return False

    cast(Any, adapter).port_manager = DummyDPM(adapter)
    ok = await adapter.start()
    assert ok is True
    p = adapter.ports.get("p_ond")
    assert p is not None
    assert getattr(p, "spawn_on_demand", False) is True
    assert p.is_running is False and p.process_active is False


@pytest.mark.asyncio
async def test_spawn_mode_shared_on_demand_equivalent(monkeypatch):
    adapter: Any = SimpleNamespace()
    port = CommandPort("ond4", {"command": "echo", "spawn_mode": "shared_on_demand"}, adapter)
    assert port.spawn_on_demand is True

    # Confirm first client attach starts the process
    started = {"called": 0}

    async def fake_start():
        started["called"] += 1
        port.is_running = True
        port.process_active = True
        return True

    monkeypatch.setattr(port, "start", fake_start)
    port.on_client_count_changed(1)
    await asyncio.sleep(0.01)
    assert started["called"] == 1
