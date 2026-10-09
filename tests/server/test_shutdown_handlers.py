"""Tests for ``openmux.server.main._setup_shutdown_handlers``.

The function registers signal handlers on an event loop and attaches a
``shutdown_event`` to the server. Actual signal delivery is not exercised;
instead the setup (handler registration, guarded SIGHUP/SIGUSR1
registration, event attachment) is verified with a fake loop and fake
server. ``asyncio.run_coroutine_threadsafe`` is stubbed so the registered
handlers can be invoked directly and the scheduled coroutines can be
awaited synchronously to verify their behavior.
"""

import asyncio
import concurrent.futures
import signal
from typing import Any, Dict

import pytest

from openmux.server.main import _setup_shutdown_handlers


class FakeServer:
    """Just enough of OpenMuxServer for the shutdown/reload handlers."""

    def __init__(self):
        self.shutdown_calls = 0
        self.shutdown_error: Any = None
        self.soft_calls = 0
        self.full_calls = 0
        self.soft_error: Any = None
        self.full_error: Any = None
        self.apply_logging_calls = 0
        self.apply_logging_error: Any = None
        self.shutdown_event = None

    async def shutdown(self):
        self.shutdown_calls += 1
        if self.shutdown_error is not None:
            raise self.shutdown_error

    def _stop_control_socket(self):
        return None

    def _reload_config_from_disk(self):
        return None

    def _apply_logging_from_config(self):
        self.apply_logging_calls += 1
        if self.apply_logging_error is not None:
            raise self.apply_logging_error

    async def reload_adapters_soft(self, context):
        self.soft_calls += 1
        if self.soft_error is not None:
            raise self.soft_error
        return "soft-ok"

    async def reload_adapters_full(self, context):
        self.full_calls += 1
        if self.full_error is not None:
            raise self.full_error
        return "full-ok"


class FakeLoop:
    """Records signal handler registration and call_soon_threadsafe calls."""

    def __init__(self):
        self.handlers: Dict[Any, Any] = {}
        self.soon_calls: list = []
        self.unsupported = set()

    def add_signal_handler(self, sig, handler):
        if sig in self.unsupported:
            raise RuntimeError(f"signal {sig} not supported")
        self.handlers[sig] = handler

    def call_soon_threadsafe(self, cb, *args):
        self.soon_calls.append((cb, args))

    def stop(self):
        pass

    def stop_registered(self) -> bool:
        return any(
            getattr(cb, "__name__", "") == "stop" and getattr(cb, "__self__", None) is self for cb, _ in self.soon_calls
        )


@pytest.fixture
def env(monkeypatch):
    """Fake loop + server; stub run_coroutine_threadsafe to capture coroutines."""
    loop = FakeLoop()
    server = FakeServer()
    captured: Dict[str, Any] = {"coroutines": []}

    def fake_rcts(coro, loop_arg):
        captured["coroutines"].append(coro)
        return concurrent.futures.Future()

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", fake_rcts)
    return loop, server, captured


def _assert_loop_stopped(loop: FakeLoop):
    assert loop.stop_registered(), f"loop.stop not queued via call_soon_threadsafe: {loop.soon_calls}"


class TestSetupShutdownHandlers:
    def test_registers_all_handlers_when_supported(self, env):
        loop, server, _ = env
        _setup_shutdown_handlers(loop, server)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGUSR1):
            assert sig in loop.handlers
        assert isinstance(server.shutdown_event, asyncio.Event)

    def test_sighup_guard_when_unsupported(self, env):
        loop, server, _ = env
        loop.unsupported = {signal.SIGHUP}
        _setup_shutdown_handlers(loop, server)
        assert signal.SIGHUP not in loop.handlers
        assert signal.SIGINT in loop.handlers and signal.SIGTERM in loop.handlers
        assert signal.SIGUSR1 in loop.handlers

    def test_sigusr1_guard_when_unsupported(self, env):
        loop, server, _ = env
        loop.unsupported = {signal.SIGUSR1}
        _setup_shutdown_handlers(loop, server)
        assert signal.SIGUSR1 not in loop.handlers
        assert signal.SIGINT in loop.handlers and signal.SIGTERM in loop.handlers
        assert signal.SIGHUP in loop.handlers

    def test_shutdown_handler_sets_event_and_schedules(self, env):
        loop, server, captured = env
        _setup_shutdown_handlers(loop, server)
        loop.handlers[signal.SIGTERM]()
        assert server.shutdown_event.is_set() is True
        assert len(captured["coroutines"]) == 1
        captured["coroutines"][0].close()  # not driving the shutdown here

    @pytest.mark.asyncio
    async def test_shutdown_coroutine_cancels_tasks_and_stops(self, env, monkeypatch):
        loop, server, captured = env
        _setup_shutdown_handlers(loop, server)
        victim = asyncio.ensure_future(asyncio.sleep(30))
        monkeypatch.setattr(asyncio, "all_tasks", lambda loop=None: [asyncio.current_task(), victim])
        loop.handlers[signal.SIGTERM]()
        (coro,) = captured["coroutines"]
        await coro
        assert server.shutdown_calls == 1
        assert victim.cancelled()
        _assert_loop_stopped(loop)

    @pytest.mark.asyncio
    async def test_shutdown_coroutine_stops_loop_on_error(self, env):
        loop, server, captured = env
        server.shutdown_error = RuntimeError("shutdown boom")
        _setup_shutdown_handlers(loop, server)
        loop.handlers[signal.SIGTERM]()
        (coro,) = captured["coroutines"]
        await coro
        assert server.shutdown_calls == 1
        _assert_loop_stopped(loop)

    @pytest.mark.asyncio
    async def test_soft_reload_coroutine_reloads_and_applies_logging(self, env):
        loop, server, captured = env
        _setup_shutdown_handlers(loop, server)
        loop.handlers[signal.SIGHUP]()
        (coro,) = captured["coroutines"]
        await coro
        assert server.soft_calls == 1
        assert server.apply_logging_calls == 1

    @pytest.mark.asyncio
    async def test_soft_reload_logging_error_is_swallowed(self, env):
        loop, server, captured = env
        server.apply_logging_error = RuntimeError("logging boom")
        _setup_shutdown_handlers(loop, server)
        loop.handlers[signal.SIGHUP]()
        (coro,) = captured["coroutines"]
        await coro  # must not raise
        assert server.soft_calls == 1
        assert server.apply_logging_calls == 1

    @pytest.mark.asyncio
    async def test_soft_reload_failure_is_logged_only(self, env):
        loop, server, captured = env
        server.soft_error = RuntimeError("soft reload boom")
        _setup_shutdown_handlers(loop, server)
        loop.handlers[signal.SIGHUP]()
        (coro,) = captured["coroutines"]
        await coro  # must not raise
        assert server.soft_calls == 1

    @pytest.mark.asyncio
    async def test_full_reload_coroutine_reloads(self, env):
        loop, server, captured = env
        _setup_shutdown_handlers(loop, server)
        loop.handlers[signal.SIGUSR1]()
        (coro,) = captured["coroutines"]
        await coro
        assert server.full_calls == 1

    @pytest.mark.asyncio
    async def test_full_reload_failure_is_logged_only(self, env):
        loop, server, captured = env
        server.full_error = RuntimeError("full reload boom")
        _setup_shutdown_handlers(loop, server)
        loop.handlers[signal.SIGUSR1]()
        (coro,) = captured["coroutines"]
        await coro  # must not raise
        assert server.full_calls == 1


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
