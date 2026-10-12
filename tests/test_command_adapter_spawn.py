"""S3 coverage: CommandPort._spawn_process branches not reached by the main suite.

Targets (pre-refactor line numbers in openmux/server/adapters/command.py):
- 571/572      stale read-task cancel() raising on a respawn
- 575-580      stale PTY loop-reader detach (remove_reader) + its OSError guard
- 583-588      stale PTY master-fd close + its OSError guard
- 618          PTY + shell spawn (create_subprocess_shell with the pty slave)
- 640/641      PTY slave-fd close raising OSError after a successful spawn
- 643/644/645  PTY allocation failing -> fall back to pipes
- 680/681      os.set_blocking() raising on the PTY master fd
- 684/685      PTY reader registration (add_reader) failing

Error branches are exercised with a fake PTY pair / event loop / process so no
real fds or subprocesses are involved; the shell-PTY path uses one real PTY.
"""

import asyncio
import os
import pty
from types import SimpleNamespace

import pytest

from openmux.server.adapters.command import CommandPort


class _PM:
    """Minimal port manager stub (send_data only) for an isolated CommandPort."""

    def __init__(self):
        self.output_queue = asyncio.Queue()

    async def send_data(self, p, c, **k):
        await self.output_queue.put(c)
        return True


class _Writer:
    def __init__(self):
        self.buf = bytearray()

    def write(self, d):
        self.buf += d

    async def drain(self):
        pass


class _BlockingProc:
    """Fake process: inert stdio and a wait() that never returns.

    The monitor's ``await self.process.wait()`` blocks here, so the monitor
    never flips is_running / process_active while the test asserts.
    """

    def __init__(self):
        self.stdin = _Writer()
        self.stdout = None

    async def wait(self):
        while True:
            await asyncio.sleep(3600)


class _FakeCancelTask:
    """A stale read task whose cancel() raises, to trip the respawn guard."""

    def cancel(self):
        raise RuntimeError("cancel blocked for test")


class _FakeLoop:
    def __init__(self):
        self.removed = []

    def remove_reader(self, fd):
        self.removed.append(fd)
        raise OSError("remove blocked for test")


class _FailAddReaderLoop:
    def add_reader(self, *a):
        raise OSError("add_reader blocked for test")

    def remove_reader(self, fd):
        pass


class _RecordingLoop:
    def __init__(self):
        self.added = []

    def add_reader(self, fd, *a):
        self.added.append(fd)

    def remove_reader(self, fd):
        pass


def _stop_tasks(port) -> None:
    """Cancel the background tasks a spawn started, without a real teardown."""
    for attr in ("_monitor_task", "_output_flush_task", "_read_task"):
        t = getattr(port, attr, None)
        if t is not None and not t.done():
            t.cancel()
    if port._loop is not None and port._pty_master_fd is not None:
        try:
            port._loop.remove_reader(port._pty_master_fd)
        except Exception:
            pass


@pytest.mark.asyncio
async def test_spawn_pty_shell_exec(monkeypatch):
    """PTY + shell spawn: create_subprocess_shell with the pty slave (line 618)."""
    if not hasattr(os, "setsid"):
        pytest.skip("POSIX ptys not supported")
    pm = _PM()
    port = CommandPort(
        "s3pty", {"command": "echo S3PTY_SHELL_OK", "interactive": True, "shell": True}, SimpleNamespace(main_port_manager=pm)
    )
    assert await port.start() is True
    try:
        got = b""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 5.0
        while b"S3PTY_SHELL_OK" not in got and loop.time() < deadline:
            try:
                got += await asyncio.wait_for(pm.output_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if not port.process_active:
                    break
        assert b"S3PTY_SHELL_OK" in got
    finally:
        await port.stop()


@pytest.mark.asyncio
async def test_spawn_pty_exec_slave_close_oserror_and_set_blocking(monkeypatch):
    """PTY exec path; slave-fd close and set_blocking both raise OSError.

    Covers 640/641 (slave close), 680/681 (non-blocking optimization), and the
    add_reader failure when the fake master fd is not a real loop fd.
    """
    if not hasattr(os, "setsid"):
        pytest.skip("POSIX ptys not supported")
    port = CommandPort(
        "s3pte",
        {"command": "x", "interactive": True, "shell": False, "env": {"S3_FAKE_PTY": "1"}},
        SimpleNamespace(main_port_manager=_PM()),
    )
    rec_loop = _RecordingLoop()
    monkeypatch.setattr(pty, "openpty", lambda: (1003, 1004))

    async def fake_exec(*a, **k):
        return _BlockingProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: rec_loop)
    monkeypatch.setattr(os, "set_blocking", lambda fd, blocking: (_ for _ in ()).throw(OSError("sb blocked")))
    monkeypatch.setattr(os, "close", lambda fd: (_ for _ in ()).throw(OSError("close blocked")))
    assert await port.start() is True
    assert port.use_pty is True
    assert port._pty_master_fd == 1003  # master fd from the fake openpty
    assert rec_loop.added == [1003]  # reader registered on the fake master fd
    assert port.process_active is True
    _stop_tasks(port)


@pytest.mark.asyncio
async def test_spawn_pty_openpty_fails_falls_back_to_pipe(monkeypatch):
    """PTY allocation failing -> fall back to pipes with the same env (643/644/645)."""
    if not hasattr(os, "setsid"):
        pytest.skip("POSIX ptys not supported")
    port = CommandPort(
        "s3f3",
        {"command": "x", "interactive": True, "shell": False, "env": {"S3_PIPE_AFTER_PTY": "1"}},
        SimpleNamespace(main_port_manager=_PM()),
    )
    monkeypatch.setattr(pty, "openpty", lambda: (_ for _ in ()).throw(OSError("no pty for test")))
    seen = {}

    async def fake_exec(*a, **k):
        seen["env"] = k.get("env")
        return _BlockingProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert await port.start() is True
    assert port.use_pty is False  # fell back after PTY allocation failed
    assert seen.get("env", {}).get("S3_PIPE_AFTER_PTY") == "1"
    _stop_tasks(port)


@pytest.mark.asyncio
async def test_spawn_respawn_reset_previous_reader_and_fd(monkeypatch):
    """A respawn clears the stale read task, loop reader, and master fd (571-588)."""
    if not hasattr(os, "setsid"):
        pytest.skip("POSIX ptys not supported")
    port = CommandPort(
        "s3r4",
        {"command": "x", "shell": False, "env": {"S3_RESET": "1"}},
        SimpleNamespace(main_port_manager=_PM()),
    )
    # Simulate leftover state from a previous (PTY) spawn:
    old_task = _FakeCancelTask()
    fake_loop = _FakeLoop()
    port._read_task = old_task
    port._pty_master_fd = 5
    port._loop = fake_loop
    port._pty_reader_added = True

    async def fake_exec(*a, **k):
        return _BlockingProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(os, "close", lambda fd: (_ for _ in ()).throw(OSError("close blocked")))
    assert await port.start() is True
    assert port._read_task is not None and port._read_task is not old_task  # old cancelled, new created
    assert port._pty_master_fd is None  # stale master fd closed + cleared
    assert port._pty_reader_added is False
    assert fake_loop.removed == [5]  # detach was attempted (then raised OSError)
    assert port.process_active is True
    _stop_tasks(port)


@pytest.mark.asyncio
async def test_spawn_pty_reader_register_failure(monkeypatch):
    """PTY reader registration (add_reader) failing is logged, not fatal (684/685)."""
    if not hasattr(os, "setsid"):
        pytest.skip("POSIX ptys not supported")
    port = CommandPort(
        "s3rd5",
        {"command": "x", "interactive": True, "shell": False},
        SimpleNamespace(main_port_manager=_PM()),
    )
    monkeypatch.setattr(pty, "openpty", lambda: (1005, 1006))

    async def fake_exec(*a, **k):
        return _BlockingProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(os, "set_blocking", lambda fd, blocking: None)
    monkeypatch.setattr(os, "close", lambda fd: None)
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _FailAddReaderLoop())
    assert await port.start() is True
    assert port.process_active is True
    _stop_tasks(port)
