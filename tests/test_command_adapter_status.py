"""Tests for `CommandPort.status_message` lifecycle (issue #62).

Covers the new lifecycle added to the CommandPort class:
- successful spawn clears the reason
- failure to spawn (FileNotFoundError, generic exception) sets a specific reason
- process exit with non-zero code or max-restarts / auto_restart-off sets a reason
- stop() clears the reason (intentional stop is a resting state)
- get_status_snapshot() includes the reason only when it is set

Reuses helpers from `tests/test_command_adapter.py` via import.
"""

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from openmux.server.adapters.command import CommandPort
from openmux.server.adapters.lifecycle import PortState
from tests.test_command_adapter import CapturingPortManager


class _FakeProc:
    """Minimal stand-in for asyncio.subprocess.Process for spawn/monitor tests."""

    def __init__(self, exit_code: int = 0):
        self._exit = exit_code

    async def wait(self) -> int:
        await asyncio.sleep(0)
        return self._exit


@pytest.mark.asyncio
async def test_command_spawn_not_found_sets_status(monkeypatch):
    """FileNotFoundError from the subprocess spawn is caught and reported."""

    async def fake_exec(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    pm = CapturingPortManager()
    adapter: Any = SimpleNamespace(main_port_manager=pm)
    port = CommandPort("cp-2", {"command": "no_such_binary_xyz"}, adapter)
    port.use_pty = False
    port.shell = False
    port.is_running = True
    port._read_task = None
    ok = await port._spawn_process()
    assert ok is False
    assert port.status_message.startswith("Process not found")
    snap = port.get_status_snapshot()
    assert "status_message" in snap


@pytest.mark.asyncio
async def test_command_spawn_generic_error_sets_status(monkeypatch):
    """Any other spawn-time exception is reported as 'Process spawn failed'."""

    async def fake_exec(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    pm = CapturingPortManager()
    adapter: Any = SimpleNamespace(main_port_manager=pm)
    port = CommandPort("cp-3", {"command": "cmd"}, adapter)
    port.use_pty = False
    port.shell = False
    port.is_running = True
    port._read_task = None
    ok = await port._spawn_process()
    assert ok is False
    assert port.status_message.startswith("Process spawn failed")
    assert "Process spawn failed" in port.get_status_snapshot()["status_message"]


@pytest.mark.asyncio
async def test_command_monitor_sets_message_on_nonzero_exit(monkeypatch):
    """Monitor loop: non-zero exit leaves the reason set, state DEGRADED."""
    pm = CapturingPortManager()
    adapter: Any = SimpleNamespace(main_port_manager=pm)
    port = CommandPort("cp-4", {"command": "echo", "auto_restart": False}, adapter)
    port.use_pty = False
    port.is_running = True
    port.process = _FakeProc(3)
    port._read_task = None
    # _monitor_loop reads from `self.process.wait()` and inspects state at each
    # iteration. Force the loop to exit after the first pass by flipping
    # is_running inside the monitor's finally path.
    port._monitor_task = None
    # The monitor uses asyncio.sleep only when auto-restarting; we disable it
    # so the loop exits after one iteration.
    await port._monitor_loop()
    assert "Process exited with code 3" in port.status_message
    assert port.state.value == "degraded"


@pytest.mark.asyncio
async def test_command_monitor_zero_exit_auto_restart_off_resting_online(monkeypatch):
    """Monitor loop: code 0 + auto_restart off -> resting state, port stays online.

    A clean exit is a normal, expected termination (e.g. a login shell
    closing). The port must not show an offline reason, must stay in a
    connected/resting state, and must not be marked DEGRADED.
    """
    pm = CapturingPortManager()
    adapter: Any = SimpleNamespace(main_port_manager=pm)
    port = CommandPort("cp-5", {"command": "echo", "auto_restart": False}, adapter)
    port.use_pty = False
    port.is_running = True
    port.process = _FakeProc(0)
    port._read_task = None
    await port._monitor_loop()
    assert port.status_message == ""
    assert port.is_connected is True
    assert port.state is PortState.CONFIGURED
    assert port.process_active is False
    assert port.is_running is False


@pytest.mark.asyncio
async def test_command_monitor_nonzero_exit_auto_restart_off_marks_offline(monkeypatch):
    """Monitor loop: non-zero exit + auto_restart off -> degraded + offline.

    The contrast to the clean-exit case: an unexpected failure sets the
    offline reason and flips the port to disconnected until a client
    respawns it.
    """
    pm = CapturingPortManager()
    adapter: Any = SimpleNamespace(main_port_manager=pm)
    port = CommandPort("cp-5b", {"command": "echo", "auto_restart": False}, adapter)
    port.use_pty = False
    port.is_running = True
    port.process = _FakeProc(2)
    port._read_task = None
    await port._monitor_loop()
    assert "code 2" in port.status_message
    assert port.is_connected is False
    assert port.state is PortState.DEGRADED
    assert port.is_running is False


@pytest.mark.asyncio
async def test_command_monitor_sets_message_on_max_restarts(monkeypatch):
    """Monitor loop: exit 0 + auto_restart True + max_restarts reached -> max-restarts message."""
    pm = CapturingPortManager()
    adapter: Any = SimpleNamespace(main_port_manager=pm)
    port = CommandPort(
        "cp-6",
        {"command": "echo", "auto_restart": True, "max_restarts": 2, "restart_delay": 0.0},
        adapter,
    )
    port.use_pty = False
    port.is_running = True
    port.restart_count = 2  # already at the cap, so monitor hits max_restarts
    port.process = _FakeProc(0)  # code 0 so the "exited with code N" branch skips
    port._read_task = None
    await port._monitor_loop()
    assert "Max restarts reached" in port.status_message
    assert "2" in port.status_message


@pytest.mark.asyncio
async def test_command_monitor_no_message_on_zero_exit_with_auto_restart(monkeypatch):
    """Monitor loop: code 0 with auto_restart should not set a message before respawn."""
    pm = CapturingPortManager()
    adapter: Any = SimpleNamespace(main_port_manager=pm)
    port = CommandPort("cp-7", {"command": "echo", "auto_restart": True, "restart_delay": 0.0}, adapter)
    port.use_pty = False
    port.is_running = True
    port.process = _FakeProc(0)
    port._read_task = None

    # Stub _spawn_process so the loop can respawn without real subprocesses.
    async def fake_spawn():
        return False  # stop the loop after one pass

    monkeypatch.setattr(port, "_spawn_process", fake_spawn)
    await port._monitor_loop()
    # Auto-restart respawn failed, but the original run completed with code 0,
    # so only the respawn-failure path logs; the port may have a reason or may
    # not, depending on how the monitor handles failed respawns. Here the spec
    # asks: a code-0 run before respawn failure must not leave a stale "exited
    # with code 0" message.
    assert "Process exited with code 0" not in port.status_message


@pytest.mark.asyncio
async def test_command_stop_clears_status(monkeypatch):
    """stop() clears status_message (intentional stop is a resting state)."""
    pm = CapturingPortManager()
    adapter: Any = SimpleNamespace(main_port_manager=pm)
    port = CommandPort("cp-8", {"command": "echo"}, adapter)
    pm_output: Dict[str, Any] = {}
    # Record meta pushes to verify the payload carries the cleared text.
    pushed: List[Dict[str, Any]] = []

    def fake_notify(name, payload):
        pushed.append({"name": name, **payload})

    pm.notify_meta_updated = fake_notify
    port.is_running = True
    port.state = PortState.ACTIVE
    port.status_message = "Process exited with code 1"
    port._status_changed = True
    port.process = None
    port._monitor_task = None
    port._read_task = None
    port._pty_master_fd = None
    port._output_flush_task = None
    port._writer = None
    port.use_pty = False
    await port.stop()
    assert port.status_message == ""
    # Snapshot after stop: no status_message key.
    assert "status_message" not in port.get_status_snapshot()
    # A meta push did fire on the clear, with empty text.
    assert any(p.get("event") == "command_status_changed" and p.get("status_message") == "" for p in pushed)


from openmux.server.adapters.lifecycle import PortState


@pytest.mark.asyncio
async def test_command_get_status_snapshot_includes_reason_when_set():
    """get_status_snapshot surfaces status_message only when non-empty."""
    adapter: Any = SimpleNamespace(main_port_manager=CapturingPortManager())
    port = CommandPort("cp-9", {"command": "echo"}, adapter)
    assert "status_message" not in port.get_status_snapshot()
    port.status_message = "Process exited with code 2"
    snap = port.get_status_snapshot()
    assert snap.get("status_message") == "Process exited with code 2"
