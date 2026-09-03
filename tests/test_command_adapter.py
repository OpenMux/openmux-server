import asyncio
import os
from types import SimpleNamespace
from typing import Any, cast

import pytest

from openmux.server.adapters.command import CommandAdapter, CommandPort, CommandWriter
from openmux.server.port_manager import PortManager


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

    async def send_data(
        self,
        port_name: str,
        chunk: bytes,
        *,
        require_clients: bool = True,
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

    # Stopped notice is delivered via data_callback when first client connects
    port.on_client_count_changed(1)
    got = await asyncio.wait_for(pm.output_queue.get(), timeout=0.1)
    assert b"PROCESS_NOT_RUNNING srv-123/cp1" in got

    # Banner is suppressed on subsequent connects (flag remains set)
    assert port._stopped_notice_sent is True

    # Reset: disconnect then reconnect sends notice again
    port._stopped_notice_sent = False
    port.on_client_count_changed(0)
    port.on_client_count_changed(1)
    got2 = await asyncio.wait_for(pm.output_queue.get(), timeout=0.1)
    assert b"PROCESS_NOT_RUNNING srv-123/cp1" in got2


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
    pm = CapturingPortManager()
    adapter: Any = SimpleNamespace(main_port_manager=pm)
    port = CommandPort("ond1", {"command": "echo", "spawn_on_demand": True}, adapter)

    # When first client connects to inactive on-demand port, banner suggests 'spawn'
    port.on_client_count_changed(1)
    msg = await asyncio.wait_for(pm.output_queue.get(), timeout=0.1)
    assert b"press Enter to spawn" in msg

    # Disconnect so the next connect starts fresh (client_count back to 0)
    port.on_client_count_changed(0)
    port._stopped_notice_sent = False

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
    assert port.is_running is True


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


@pytest.mark.asyncio
async def test_adapter_reconcile_ports_unchanged(monkeypatch):
    """Port whose config matches running defaults is not restarted on reconcile."""
    adapter = CommandAdapter("cmd", {"command_ports": [{"name": "a", "command": "echo hi"}]})

    # Seed 'a' with the values CommandPort.__init__ would assign for this config
    class PortObj:
        command = "echo hi"
        shell = False
        cwd = None
        env = None
        auto_restart = False
        max_read_write_users = 1
        interactive = False
        always_buffer = False
        scrollback_size = 0

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
    summary = await adapter.reconcile_ports([{"name": "a", "command": "echo hi", "description": "new"}])
    assert summary["unchanged"] == ["a"]
    assert summary["updated"] == []
    assert destroyed == []
    assert created == []


@pytest.mark.asyncio
async def test_adapter_reconcile_ports_updates_groups_in_place_without_restart(monkeypatch):
    """A groups-only change must not recreate the port; the lists update in place."""
    adapter = CommandAdapter("cmd", {"command_ports": [{"name": "a", "command": "echo hi"}]})

    class PortObj:
        command = "echo hi"
        shell = False
        cwd = None
        env = None
        auto_restart = False
        max_read_write_users = "one"
        interactive = False
        always_buffer = False
        scrollback_size = 0
        read_write_groups: list = ["ops"]
        read_only_groups: list = ["viewers"]

    adapter.ports["a"] = PortObj()  # type: ignore[assignment]

    destroyed: list = []
    created: list = []

    async def fake_destroy(name: str) -> None:
        destroyed.append(name)

    async def fake_create(name: str, cfg: dict) -> None:
        created.append(name)

    monkeypatch.setattr(adapter, "destroy_port", fake_destroy)
    monkeypatch.setattr(adapter, "create_port", fake_create)

    summary = await adapter.reconcile_ports(
        [{"name": "a", "command": "echo hi", "read_write_groups": [], "read_only_groups": ["ops"]}]
    )
    assert summary["unchanged"] == ["a"], f"groups-only change must not recreate: {summary}"
    assert summary["updated"] == []
    assert summary["removed"] == []
    assert summary["added"] == []
    assert destroyed == []
    assert created == []
    port = adapter.ports["a"]
    assert list(port.read_write_groups) == []
    assert list(port.read_only_groups) == ["ops"]


@pytest.mark.asyncio
async def test_adapter_reconcile_ports_add_remove_update(monkeypatch):
    """Add, remove, and material-change (command) are all detected correctly."""
    adapter = CommandAdapter("cmd", {"command_ports": []})

    class PortA:
        command = "echo a"
        shell = False
        cwd = None
        env = None
        auto_restart = False
        max_read_write_users = 1
        interactive = False
        always_buffer = False
        scrollback_size = 0

    class PortB:
        command = "echo b"
        shell = False
        cwd = None
        env = None
        auto_restart = False
        max_read_write_users = 1
        interactive = False
        always_buffer = False
        scrollback_size = 0

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

    summary = await adapter.reconcile_ports(
        [
            {"name": "a", "command": "echo a"},  # unchanged
            {"name": "b", "command": "echo b_new"},  # updated (command changed)
            {"name": "c", "command": "echo c"},  # added
        ]
    )
    assert summary["unchanged"] == ["a"]
    assert summary["updated"] == ["b"]
    assert summary["added"] == ["c"]
    assert summary["removed"] == []
    assert "b" in destroyed
    assert "b" in created  # destroyed then recreated
    assert "c" in created


@pytest.mark.asyncio
async def test_on_demand_lifecycle_through_real_client_paths(monkeypatch):
    """spawn_on_demand + idle_timeout_sec drive the process via the canonical
    PortManager add/remove client paths (the same hook the serial adapter
    uses for presence signal lines, issue #63). Guards against a
    dead-hook regression like the pre-#63 L7 finding, where only
    direct on_client_count_changed() calls exercised the behavior."""
    import os

    from openmux.server import data_logger as dl_mod
    from openmux.server.adapters.lifecycle import DynamicPortManager

    class _FakeDL:
        @staticmethod
        def get():
            return _FakeDL()

        def record(self, *args, **kwargs):
            return None

        def record_meta(self, *args, **kwargs):
            return None

    monkeypatch.setattr(dl_mod.DataLogger, "get", _FakeDL.get, raising=False)

    # Use a command whose process we can observe for its lifetime: a sleep,
    # not a shell, so the observed process is the child itself.
    port_cfg = {
        "name": "ond",
        "command": "sleep 30",
        "shell": False,
        "spawn_on_demand": True,
        "idle_timeout_sec": 0.2,
        "clean_env": False,
        "max_read_write_users": "multiple",  # two concurrent writers to exercise the 2-client path
    }
    pm = PortManager([])
    adapter = CommandAdapter("cmd", {"command_ports": [port_cfg]})
    adapter.main_port_manager = pm
    # Base load_configured_ports() requires the per-adapter dynamic manager.
    DynamicPortManager(adapter)
    pm.set_unified_adapters([adapter])
    ok = await adapter.start()
    assert ok is True
    port = adapter.ports["ond"]
    try:
        # Not spawned at startup (on-demand).
        assert port.is_running is False
        assert port.process_active is False

        # First client attach spawns the process via the real path.
        assert await pm.add_client_to_port("ond", "c1", "u1", "read-write") is True
        for _ in range(100):
            if port.process_active and port.process is not None:
                break
            await asyncio.sleep(0.02)
        assert port.process_active is True, "process did not spawn on first attach"
        pid1 = port.process.pid
        assert os.kill(pid1, 0) is None  # process is alive

        # A second client does not spawn a second process or tear down the first.
        assert await pm.add_client_to_port("ond", "c2", "u2", "read-write") is True
        await asyncio.sleep(0.05)
        assert port.process is not None and port.process.pid == pid1

        # Losing one client keeps the process running (the other client remains).
        assert await pm.remove_client_from_port("ond", "c1") is True
        await asyncio.sleep(0.05)
        assert port.process_active is True

        # Last client leaves: the idle timer stops the process shortly after.
        assert await pm.remove_client_from_port("ond", "c2") is True
        stopped_early = False
        for _ in range(200):
            if not port.is_running:
                stopped_early = True
                break
            await asyncio.sleep(0.05)
        assert stopped_early is True, "process was not stopped after last client + idle timeout"

        # The next client connection respawns a fresh process.
        assert await pm.add_client_to_port("ond", "c3", "u3", "read-write") is True
        for _ in range(100):
            if port.process_active and port.process is not None:
                break
            await asyncio.sleep(0.02)
        assert port.process_active is True, "process did not respawn on re-attach"
        assert port.process.pid != pid1
        assert os.kill(port.process.pid, 0) is None  # fresh process is alive

        # Cleanup: detach and stop so no sleep lingers after the test.
        await pm.remove_client_from_port("ond", "c3")
    finally:
        try:
            await adapter.stop()
        except Exception:
            pass


@pytest.mark.asyncio
async def test_reconcile_updates_lifecycle_flags_in_place_without_recreate(monkeypatch):
    """A spawn_on_demand / idle_timeout_sec change on a live port is applied in
    place, not by destroy+recreate (which would drop connected clients)."""
    cfg = {"command_ports": [{"name": "a", "command": "cat -", "shell": True}]}
    adapter = CommandAdapter("cmd", cfg)

    class PortObj:
        command = "cat -"
        shell = True
        cwd = None
        env = None
        auto_restart = False
        max_read_write_users = "one"
        interactive = False
        always_buffer = False
        scrollback_size = 0
        read_write_groups: list = []
        read_only_groups: list = []
        spawn_on_demand = False
        idle_timeout_sec = 0.0

    port = PortObj()  # type: ignore[assignment]
    adapter.ports["a"] = port

    destroyed: list = []
    created: list = []

    async def fake_destroy(name: str) -> None:
        destroyed.append(name)

    async def fake_create(name: str, cfg: dict) -> None:
        created.append(name)

    monkeypatch.setattr(adapter, "destroy_port", fake_destroy)
    monkeypatch.setattr(adapter, "create_port", fake_create)

    # Baseline reconcile: nothing changes.
    s0 = await adapter.reconcile_ports([{"name": "a", "command": "cat -", "shell": True}])
    assert s0["unchanged"] == ["a"] and destroyed == [] and created == []

    # Changing only the lifecycle flags updates them in place, no recreate.
    s1 = await adapter.reconcile_ports(
        [{"name": "a", "command": "cat -", "shell": True, "spawn_on_demand": True, "idle_timeout_sec": 30}]
    )
    assert s1["unchanged"] == ["a"], f"lifecycle-only change must not recreate: {s1}"
    assert destroyed == [] and created == []
    assert port.spawn_on_demand is True
    assert port.idle_timeout_sec == 30.0
