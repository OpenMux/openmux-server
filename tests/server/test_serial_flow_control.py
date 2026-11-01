"""Flow-control tests for the serial adapter: the mode named in config must reach the device.

``_connect`` used to leave ``flow_control`` out of the call that opened the device, so a port configured
for ``rtscts`` or ``xonxoff`` opened its device with no handshake at all. These tests bind the config
value to the arguments handed to ``open_serial_connection``; the end-to-end tier proves the termios that
comes out of it (``tests/e2e/test_serial_device.py``, `SER-06`).
"""

import logging
import sys
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from openmux.server.adapters.serial import SerialPortWrapper


def _make_port(flow_control: Optional[str] = None) -> SerialPortWrapper:
    """One port with the given ``flow_control``, or none at all to take the default."""
    config: Dict[str, Any] = {"name": "fp", "description": "fp", "device": "/dev/ttyX0"}
    if flow_control is not None:
        config["flow_control"] = flow_control
    return SerialPortWrapper(config, logging.getLogger("test.serial.flow"))


class _Reader:
    async def read(self, _n: int = 1024) -> bytes:
        return b""


class _Writer:
    def __init__(self) -> None:
        self.transport = SimpleNamespace(serial=SimpleNamespace(dtr=True, rts=True))

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


async def _opened_kwargs(port: SerialPortWrapper, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """The keyword arguments ``_connect`` passes to ``open_serial_connection``."""
    seen: List[Dict[str, Any]] = []

    async def _fake_open(**kwargs):
        seen.append(kwargs)
        return (_Reader(), _Writer())

    monkeypatch.setattr("os.path.exists", lambda _path: True)
    monkeypatch.setitem(sys.modules, "serial_asyncio", SimpleNamespace(open_serial_connection=_fake_open))
    assert await port._connect() is True, "the port did not reach the fake device"
    return seen[0]


class TestFlowControlKwargs:
    """The translation from the config value to the flags pyserial applies."""

    @pytest.mark.parametrize(
        "configured, expected",
        [
            (None, {"rtscts": False, "xonxoff": False}),
            ("none", {"rtscts": False, "xonxoff": False}),
            ("rtscts", {"rtscts": True, "xonxoff": False}),
            ("xonxoff", {"rtscts": False, "xonxoff": True}),
            # The mode is matched the same way the Config Manager matches it: trimmed and lower-cased.
            ("RTSCTS", {"rtscts": True, "xonxoff": False}),
            ("  xonxoff  ", {"rtscts": False, "xonxoff": True}),
            # An unknown value opens the line clean rather than guessing a handshake.
            ("rts-cts", {"rtscts": False, "xonxoff": False}),
        ],
    )
    def test_matrix(self, configured: Optional[str], expected: Dict[str, bool]) -> None:
        assert _make_port(configured)._flow_control_kwargs() == expected

    def test_dsrdtr_is_deliberately_not_passed(self) -> None:
        """``dsrdtr`` opens no kernel flag, so it must not reach pyserial either.

        On POSIX pyserial treats that mode as "do not raise DTR when the port opens", and the ``dtr``
        line policy owns that pin. Passing it would let two writers drive one pin.
        """
        assert _make_port("dsrdtr")._flow_control_kwargs() == {"rtscts": False, "xonxoff": False}


class TestOpenCallCarriesFlowControl:
    """The flags must be on the call that opens the device, which is where they were dropped."""

    @pytest.mark.asyncio
    async def test_rtscts_reaches_the_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        kwargs = await _opened_kwargs(_make_port("rtscts"), monkeypatch)
        assert kwargs["rtscts"] is True
        assert kwargs["xonxoff"] is False

    @pytest.mark.asyncio
    async def test_xonxoff_reaches_the_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        kwargs = await _opened_kwargs(_make_port("xonxoff"), monkeypatch)
        assert kwargs["xonxoff"] is True
        assert kwargs["rtscts"] is False

    @pytest.mark.asyncio
    async def test_the_line_settings_travel_with_them(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The fix must not cost the settings that already worked."""
        port = _make_port("rtscts")
        port.baudrate = 57600
        port.parity = "E"
        port.stopbits = 2
        kwargs = await _opened_kwargs(port, monkeypatch)
        assert (kwargs["baudrate"], kwargs["parity"], kwargs["stopbits"]) == (57600, "E", 2)
        assert kwargs["url"] == "/dev/ttyX0"
