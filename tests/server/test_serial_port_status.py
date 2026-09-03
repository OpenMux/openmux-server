"""Tests for SerialPortWrapper.status_message lifecycle (issue #62).

Covers the new lifecycle in serial.py:
- failed connect (missing device, missing module, open error) sets a specific reason
- read-loop disconnect (empty read, read error) sets a drop reason
- successful connect clears any prior status_message
- stop() clears the reason (intentional stop = resting state)
- get_status_snapshot() surfaces the reason when set

The meta-notify callback is a simple closure, so tests pass a capture list.
"""

import logging
import sys
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from openmux.server.adapters.serial import SerialPortWrapper


def _make_port(
    device: str = "/dev/ttyTEST0",
    captured: Optional[List[Dict[str, Any]]] = None,
) -> SerialPortWrapper:
    """Build a real SerialPortWrapper with a capturing meta_notify."""
    if captured is None:
        captured = []

    def _capture(pname: str, payload: Dict[str, Any]) -> None:
        captured.append({"pname": pname, **payload})

    cfg = {"name": "testport", "description": "test", "device": device}
    wrapper = SerialPortWrapper(cfg, logging.getLogger("test.serial"), meta_notify=_capture)
    # Suppress the rate-limited "device does not exist" warning timestamp so
    # multiple tests don't share state.
    wrapper._last_missing_warn_ts = None
    return wrapper


def _patch_serial_asyncio(monkeypatch, open_result=None, open_error: Optional[Exception] = None) -> None:
    """Monkeypatch ``sys.modules["serial_asyncio"]`` with a fake ``open_serial_connection``."""

    async def _fake_open(**_kwargs):
        if open_error is not None:
            raise open_error
        assert open_result is not None
        return open_result

    fake_mod = SimpleNamespace(open_serial_connection=_fake_open)
    monkeypatch.setitem(sys.modules, "serial_asyncio", fake_mod)


class _FakeReader:
    """Minimal async StreamReader replacement for read-loop tests."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self._raise = None

    def set_raise(self, exc: Exception) -> None:
        self._raise = exc

    async def read(self, _n: int = 1024):
        if self._raise is not None:
            raise self._raise
        return self._chunks.pop(0)


class _FakeWriter:
    async def wait_closed(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_serial_missing_device_sets_status(monkeypatch):
    """Device not found: status is set to 'Serial device /dev/... not found' and the
    snapshot surfaces it."""
    monkeypatch.setattr("os.path.exists", lambda _p: False)
    captured: List[Dict[str, Any]] = []
    port = _make_port(captured=captured)
    ok = await port._connect()
    assert ok is False
    assert port.status_message == f"Serial device {port.device} not found"
    snap = port.get_status_snapshot()
    assert snap["status_message"] == port.status_message
    # The state flip from None -> offline must have fired a meta event
    assert any(e.get("event") == "serial_status_changed" and e.get("status_message") == port.status_message for e in captured)


@pytest.mark.asyncio
async def test_serial_open_error_sets_status(monkeypatch):
    """open_serial_connection raising OSError sets a 'Failed to open ...' reason."""
    monkeypatch.setattr("os.path.exists", lambda _p: True)
    _patch_serial_asyncio(monkeypatch, open_error=OSError("No such device or address"))
    captured: List[Dict[str, Any]] = []
    port = _make_port(captured=captured)
    ok = await port._connect()
    assert ok is False
    assert port.status_message.startswith("Failed to open")
    assert "No such device or address" in port.status_message
    assert "status_message" in port.get_status_snapshot()


@pytest.mark.asyncio
async def test_serial_missing_module_sets_status(monkeypatch):
    """serial_asyncio ImportError: reason is set to a helpful message."""
    monkeypatch.setattr("os.path.exists", lambda _p: True)
    # Ensure the import fails
    monkeypatch.setitem(sys.modules, "serial_asyncio", None)  # type: ignore[assignment]
    captured: List[Dict[str, Any]] = []
    port = _make_port(captured=captured)
    ok = await port._connect()
    assert ok is False
    assert port.status_message == "pyserial-asyncio not installed"
    assert "status_message" in port.get_status_snapshot()


@pytest.mark.asyncio
async def test_serial_successful_connect_clears_previous_status(monkeypatch):
    """A prior 'Disconnected from ...' reason is cleared on a fresh successful connect."""
    monkeypatch.setattr("os.path.exists", lambda _p: True)
    reader = _FakeReader([])
    writer = _FakeWriter()
    _patch_serial_asyncio(monkeypatch, open_result=(reader, writer))
    captured: List[Dict[str, Any]] = []
    port = _make_port(captured=captured)

    # Simulate a prior drop leaving a reason
    port._set_status_message("Disconnected from /dev/ttyTEST0")
    assert port.status_message != ""

    # Now a successful connect should clear it
    ok = await port._connect()
    assert ok is True
    assert port.status_message == ""
    assert "status_message" not in port.get_status_snapshot()

    # Clean up so later tests don't see dangling state
    await port.stop()


@pytest.mark.asyncio
async def test_serial_read_empty_read_sets_drop_reason():
    """Empty read in _read_loop leaves a 'Disconnected from ...' reason set."""
    port = _make_port()
    port.reader = _FakeReader([b""])
    port.is_connected = True
    await port._read_loop()
    assert port.is_connected is False
    assert port.status_message == f"Disconnected from {port.device}"
    assert "status_message" in port.get_status_snapshot()


@pytest.mark.asyncio
async def test_serial_read_error_sets_drop_reason():
    """An exception in _read_loop leaves a 'Read error on ...: <err>' reason."""
    port = _make_port()
    reader = _FakeReader([])
    reader.set_raise(OSError("device disconnected"))
    port.reader = reader
    port.is_connected = True
    await port._read_loop()
    assert port.is_connected is False
    assert port.status_message.startswith("Read error on")
    assert "device disconnected" in port.status_message


@pytest.mark.asyncio
async def test_serial_stop_clears_reason():
    """stop() is an intentional resting state for the port (issue #62), so any
    pending 'Disconnected from ...' reason is cleared."""
    port = _make_port()
    port._set_status_message("Disconnected from /dev/ttyTEST0")
    assert port.status_message != ""
    await port.stop()
    assert port.status_message == ""
    assert "status_message" not in port.get_status_snapshot()


def test_serial_get_status_snapshot_omits_healthy():
    """No reason, connected=True: snapshot does not include status_message."""
    port = _make_port()
    port.is_connected = True
    snap = port.get_status_snapshot()
    assert "status_message" not in snap


def test_serial_set_status_message_no_duplicate_notifies():
    """Setting the same non-empty reason twice fires meta only on the state flip."""
    captured: List[Dict[str, Any]] = []
    port = _make_port(captured=captured)
    port._set_status_message("Disconnected from /dev/ttyTEST0")
    port._set_status_message("Disconnected from /dev/ttyTEST0")
    # Only the first call flips state (None -> offline); the second is a no-op
    # in terms of _status_changed.
    status_events = [e for e in captured if e.get("event") == "serial_status_changed"]
    assert len(status_events) == 1


def test_serial_set_status_message_transitions_to_connected_fire():
    """Transitions offline-with-reason -> online-empty must fire to clear the reason."""
    captured: List[Dict[str, Any]] = []
    port = _make_port(captured=captured)
    port._set_status_message("Disconnected from /dev/ttyTEST0")
    port._set_status_message("", connected=True)
    assert port.status_message == ""
    status_events = [e for e in captured if e.get("event") == "serial_status_changed"]
    # First event: None -> offline (with message). Second event: offline -> online
    # (empty message), so the web console can clear the reason.
    assert len(status_events) == 2
    assert status_events[0]["status_message"] == "Disconnected from /dev/ttyTEST0"
    assert status_events[1]["status_message"] == ""
