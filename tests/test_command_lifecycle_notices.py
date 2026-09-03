"""Process lifecycle notices for the command adapter.

Bracketed ``[OpenMux:PROCESS_*]`` lines are broadcast to attached clients when
the process starts, exits, or is intentionally stopped. With no clients
attached, nothing is emitted (same convention as the existing
``PROCESS_NOT_RUNNING`` notice). The caller side gates on client_count; the
helper itself only formats and schedules.
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from openmux.server.adapters.command import CommandPort


def _make_port():
    pm = SimpleNamespace(config_manager=SimpleNamespace(config={"server": {"id": "srv-9"}}))
    adapter: Any = SimpleNamespace(main_port_manager=pm)
    port = CommandPort("cp1", {"command": "sleep 30"}, adapter)
    chunks = []

    async def _cb(port_name: str, chunk: bytes, *, require_clients: bool = True) -> bool:
        chunks.append(chunk)
        return True

    port.data_callback = _cb
    return port, chunks


@pytest.mark.asyncio
async def test_notice_helper_formats_and_emits():
    """The helper itself is ungated; callers gate on client_count."""
    port, chunks = _make_port()
    port._schedule_lifecycle_notice("PROCESS_STARTED", "process started")
    await asyncio.sleep(0.05)
    assert chunks == [b"\r\n[OpenMux:PROCESS_STARTED srv-9/cp1 process started]\r\n"]


@pytest.mark.asyncio
async def test_started_notice_on_spawn_with_attached_clients():
    port, chunks = _make_port()
    port.client_count = 1
    assert await port.start() is True
    await asyncio.sleep(0.05)
    assert any(b"[OpenMux:PROCESS_STARTED srv-9/cp1 process started]" in c for c in chunks)
    await port.stop()
    await asyncio.sleep(0.05)
    assert any(b"[OpenMux:PROCESS_STOPPED" in c for c in chunks)


@pytest.mark.asyncio
async def test_started_notice_suppressed_without_clients():
    """Server-startup spawn with nobody attached emits no notice."""
    port, chunks = _make_port()
    port.client_count = 0
    assert await port.start() is True
    await asyncio.sleep(0.05)
    assert chunks == []
    await port.stop()


@pytest.mark.asyncio
async def test_exited_notice_from_monitor_loop(monkeypatch):
    port, chunks = _make_port()
    port.auto_restart = True
    port.restart_delay = 0.1
    port.max_restarts = 1  # fails after one respawn attempt
    port.is_running = True
    port.client_count = 1

    fake = SimpleNamespace()

    async def _wait() -> int:
        return 3

    fake.wait = _wait
    port.process = fake

    async def _failed_spawn() -> bool:
        return False

    monkeypatch.setattr(port, "_spawn_process", _failed_spawn)
    port._monitor_task = asyncio.create_task(port._monitor_loop())

    try:
        await asyncio.sleep(0.5)
    finally:
        port._monitor_task.cancel()
        try:
            await port._monitor_task
        except (asyncio.CancelledError, Exception):
            pass

    joined = b"".join(chunks)
    assert b"[OpenMux:PROCESS_EXITED srv-9/cp1 process exited (code 3, restarting in 0.1s)]" in joined


@pytest.mark.asyncio
async def test_exited_notice_suppressed_without_clients(monkeypatch):
    port, chunks = _make_port()
    port.auto_restart = True
    port.restart_delay = 0.1
    port.max_restarts = 1
    port.is_running = True
    port.client_count = 0

    fake = SimpleNamespace()

    async def _wait() -> int:
        return 1

    fake.wait = _wait
    port.process = fake

    async def _failed_spawn() -> bool:
        return False

    monkeypatch.setattr(port, "_spawn_process", _failed_spawn)
    port._monitor_task = asyncio.create_task(port._monitor_loop())
    try:
        await asyncio.sleep(0.4)
    finally:
        port._monitor_task.cancel()
        try:
            await port._monitor_task
        except (asyncio.CancelledError, Exception):
            pass
    assert chunks == []


@pytest.mark.asyncio
async def test_stopped_notice_on_intentional_stop():
    port, chunks = _make_port()
    port.is_running = True
    port.client_count = 2
    await port.stop()
    await asyncio.sleep(0.05)
    assert chunks == [b"\r\n[OpenMux:PROCESS_STOPPED srv-9/cp1 process was stopped]\r\n"]
    assert port.client_count == 0


@pytest.mark.asyncio
async def test_start_failure_does_not_require_clients():
    """start() with 0 clients and a bad command reports a plain failure."""
    port, _chunks = _make_port()
    port.command = "definitely_not_a_real_command_xyz"
    assert await port.start() is False


@pytest.mark.asyncio
async def test_exit_drains_residual_output_before_notice(monkeypatch):
    """auto_restart off + batching on: buffered tail output beats the exit notice."""
    port, chunks = _make_port()
    port.auto_restart = False
    port.spawn_on_demand = True
    port.is_running = True
    port.process_active = True
    port.client_count = 1
    port._output_buffer = bytearray(b"last-line\n")

    fake = SimpleNamespace()

    async def _wait() -> int:
        return 0

    fake.wait = _wait
    port.process = fake

    port._monitor_task = asyncio.create_task(port._monitor_loop())
    try:
        await asyncio.wait_for(asyncio.shield(port._monitor_task), timeout=1.0)
    except (asyncio.CancelledError, Exception):
        pass

    joined = b"".join(chunks)
    i_tail = joined.find(b"last-line\n")
    i_exit = joined.find(b"PROCESS_EXITED")
    assert i_tail != -1 and i_exit != -1 and i_tail < i_exit, joined
    assert b"press Enter to spawn" in joined
    assert port.is_running is False
    # Banner flag reset so the next attach shows a fresh PROCESS_NOT_RUNNING.
    assert port._stopped_notice_sent is False


@pytest.mark.asyncio
async def test_start_always_spawns_monitor_even_without_auto_restart():
    port, _chunks = _make_port()
    port.auto_restart = False
    assert await port.start() is True
    try:
        assert port._monitor_task is not None and not port._monitor_task.done()
    finally:
        await port.stop()


@pytest.mark.asyncio
async def test_stop_closes_pty_master_fd_even_when_reader_detached():
    """A reader that already detached on EOF must not leak the master fd."""
    import os

    rfd, wfd = os.pipe()
    os.close(wfd)
    port, _chunks = _make_port()
    port.is_running = True
    port.use_pty = True
    port._pty_master_fd = rfd
    port._pty_reader_added = False  # cleared earlier by the EOF path
    await port.stop()
    with pytest.raises(OSError):
        os.fstat(rfd)  # EBADF => closed
