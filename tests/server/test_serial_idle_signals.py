"""Signal-line (DTR/RTS) policy tests for the serial adapter (issue #63).

Covers the policy resolution matrix, the rtscts flow-control guard, and the
runtime line driving: initial application on connect, idle transitions on
client attach/detach, idempotency, fixed-level and untouched lines, and the
reconnect reset.
"""

import logging
import sys
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from openmux.server.adapters.serial import (
    LINE_POLICY_VALUES,
    SerialPortConfig,
    SerialPortWrapper,
    resolve_line_policy,
)


class FakeSerial:
    """Stand-in for the underlying serial.Serial instance.

    Records every dtr/rts assignment so tests can assert which levels were
    driven and in what order.
    """

    def __init__(self, dtr: bool = True, rts: bool = True):
        self._dtr = dtr
        self._rts = rts
        self.calls: List[str] = []

    @property
    def dtr(self) -> bool:
        return self._dtr

    @dtr.setter
    def dtr(self, value: bool) -> None:
        self._dtr = value
        self.calls.append(f"dtr={value!r}")

    @property
    def rts(self) -> bool:
        return self._rts

    @rts.setter
    def rts(self, value: bool) -> None:
        self._rts = value
        self.calls.append(f"rts={value!r}")

    @property
    def last(self) -> Optional[str]:
        return self.calls[-1] if self.calls else None


class IdleWriter:
    """Fake stream writer exposing a real FakeSerial via .transport.serial."""

    def __init__(self, serial_obj: FakeSerial):
        self.transport = SimpleNamespace(serial=serial_obj)

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _FakeReader:
    def __init__(self):
        self._chunks: List[bytes] = []

    async def read(self, _n: int = 1024):
        return b""


def _make_port(
    dtr: Any = None,
    rts: Any = None,
    flow_control: str = "none",
    captured: Optional[List[Dict[str, Any]]] = None,
) -> SerialPortWrapper:
    def _capture(_pname: str, payload: Dict[str, Any]) -> None:
        if captured is not None:
            captured.append(payload)

    cfg = SerialPortConfig(
        name="testport",
        description="test",
        device="/dev/ttyTEST0",
        dtr=dtr,
        rts=rts,
        flow_control=flow_control,
    )
    wrapper = SerialPortWrapper(cfg, logging.getLogger("test.serial"), meta_notify=_capture)
    return wrapper


@pytest.mark.asyncio
async def _connect_with_driver(port: SerialPortWrapper, serial_obj: FakeSerial, monkeypatch) -> bool:
    """Run _connect() against a fake serial_asyncio exposing our FakeSerial."""
    monkeypatch.setattr("os.path.exists", lambda _p: True)
    writer = IdleWriter(serial_obj)
    reader = _FakeReader()

    async def _fake_open(**_kwargs):
        return (reader, writer)

    fake_mod = SimpleNamespace(open_serial_connection=_fake_open)
    monkeypatch.setitem(sys.modules, "serial_asyncio", fake_mod)
    port.reader = reader  # _serial_driver() resolves via the writer/transport
    port.writer = writer
    return await port._connect()


# ---------------------------------------------------------------------------
# Policy resolution
# ---------------------------------------------------------------------------


class TestResolveLinePolicy:
    @pytest.mark.parametrize(
        "value, expected",
        [
            (None, (None, None)),
            ("none", (None, None)),
            (True, (True, True)),
            (False, (False, False)),
            ("on", (True, True)),
            ("off", (False, False)),
            ("presence-on", (True, False)),
            ("presence-off", (False, True)),
        ],
    )
    def test_matrix(self, value, expected):
        assert resolve_line_policy(value) == expected

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError):
            resolve_line_policy("bogus")

    def test_values_constant(self):
        assert LINE_POLICY_VALUES == ("none", "on", "off", "presence-on", "presence-off")


class TestPolicyConfigValidation:
    def test_default_is_untouched(self):
        cfg = SerialPortConfig(name="a", description="a", device="/dev/ttyX0")
        assert cfg.dtr is None
        assert cfg.rts is None

    def test_invalid_policy_rejected(self):
        for bad in ("bogus", "", "presence", 3):
            with pytest.raises(ValueError):
                SerialPortConfig(name="a", description="a", device="/dev/ttyX0", dtr=bad)
            with pytest.raises(ValueError):
                SerialPortConfig(name="a", description="a", device="/dev/ttyX0", rts=bad)

    def test_rtscts_with_managed_rts_rejected(self):
        for bad in ("on", "off", "presence-on", "presence-off", True, False):
            with pytest.raises(ValueError, match="rtscts"):
                SerialPortConfig(name="a", description="a", device="/dev/ttyX0", flow_control="rtscts", rts=bad)

    def test_rtscts_with_unmanaged_rts_ok(self):
        for ok in (None, "none"):
            cfg = SerialPortConfig(name="a", description="a", device="/dev/ttyX0", flow_control="rtscts", rts=ok)
            assert cfg.rts in (None, "none")

    def test_rtscts_with_any_dtr_ok(self):
        dtr = SerialPortConfig(name="a", description="a", device="/dev/ttyX0", flow_control="rtscts", dtr="presence-on")
        assert dtr.dtr == "presence-on"

    def test_dsrdtr_and_xonxoff_with_any_lines_ok(self):
        for flow in ("dsrdtr", "xonxoff", "none"):
            cfg = SerialPortConfig(
                name="a",
                description="a",
                device="/dev/ttyX0",
                flow_control=flow,
                dtr="presence-on",
                rts="presence-off",
            )
            assert cfg.dtr == "presence-on"


# ---------------------------------------------------------------------------
# Runtime line driving
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_presence_on_full_lifecycle(monkeypatch):
    """presence-on: low while idle, high with clients, drops on last detach."""
    port = _make_port(dtr="presence-on")
    driver = FakeSerial()
    assert await _connect_with_driver(port, driver, monkeypatch) is True

    # No clients attached at open -> idle level (low)
    assert driver.dtr is False
    assert driver.last == "dtr=False"

    port.on_client_count_changed(1)
    assert driver.dtr is True
    port.on_client_count_changed(2)  # 1 -> 2 stays high; no new drive
    assert driver.last == "dtr=True" and len(driver.calls) == 2

    port.on_client_count_changed(1)  # 2 -> 1 stays high
    assert driver.dtr is True and len(driver.calls) == 2

    port.on_client_count_changed(0)  # last client leaves -> drop
    assert driver.dtr is False
    assert driver.last == "dtr=False"

    await port._disconnect()
    # Reset to active level, cleared tracking (no re-drive while disconnected)
    assert driver.dtr is True
    assert port._dtr_driven is None
    port.on_client_count_changed(0)  # disconnected: no-op
    assert driver.last == "dtr=True"
    try:
        await port.stop()
    except Exception:
        pass


@pytest.mark.asyncio
async def test_presence_off_inverse(monkeypatch):
    """presence-off: high while idle, low with clients, returns on last detach."""
    port = _make_port(rts="presence-off")
    driver = FakeSerial()
    assert await _connect_with_driver(port, driver, monkeypatch) is True
    assert driver.rts is True  # idle high

    port.on_client_count_changed(1)
    assert driver.rts is False
    port.on_client_count_changed(0)
    assert driver.rts is True


@pytest.mark.asyncio
async def test_fixed_on_and_off_applied_at_open(monkeypatch):
    """on/off: both levels equal, so the line moves once at open and never again."""
    port = _make_port(dtr="on", rts="off")
    # Start the driver at the opposite levels so both connect-time drives are visible
    driver = FakeSerial(dtr=False, rts=True)
    assert await _connect_with_driver(port, driver, monkeypatch) is True
    assert driver.dtr is True and driver.rts is False

    port.on_client_count_changed(1)
    port.on_client_count_changed(0)
    # No new drives beyond the connect-time ones
    assert sorted(driver.calls) == ["dtr=True", "rts=False"]


@pytest.mark.asyncio
async def test_none_policy_never_touches_lines(monkeypatch):
    port = _make_port()  # dtr=None, rts=None
    driver = FakeSerial(dtr=True, rts=True)
    assert await _connect_with_driver(port, driver, monkeypatch) is True
    assert driver.calls == []
    port.on_client_count_changed(1)
    port.on_client_count_changed(0)
    assert driver.calls == []


@pytest.mark.asyncio
async def test_legacy_bool_shorthand(monkeypatch):
    """dtr=True means on, dtr=False means off (schema backward compat)."""
    port = _make_port(dtr=True, rts=False)
    driver = FakeSerial(dtr=True, rts=True)
    assert await _connect_with_driver(port, driver, monkeypatch) is True
    # rts was forced from the driver default True down to False; dtr already
    # matched so no drive was needed (idempotency vs. the open-time state).
    assert driver.dtr is True and driver.rts is False
    assert sorted(driver.calls) == ["rts=False"]


@pytest.mark.asyncio
async def test_idempotent_on_equal_counts_and_driver_unavailable():
    """Equal counts are no-ops; a missing driver never raises."""
    port = _make_port(dtr="presence-on")
    port.is_connected = True
    port.on_client_count_changed(0)  # no driver (no writer): swallowed
    port.on_client_count_changed(0)  # idempotent
    port._client_count = 1
    port.on_client_count_changed(1)  # equal count: no-op
    assert port._client_count == 1

    # A failing driver must not raise or corrupt state.
    class _BrokenSerial:
        def __init__(self):
            self._v = True
            self.rts = True

        @property
        def dtr(self):
            return self._v

        @dtr.setter
        def dtr(self, _value):
            raise OSError("ioctl failed")

    port.writer = SimpleNamespace(transport=SimpleNamespace(serial=_BrokenSerial()))
    port.is_connected = True
    port._client_count = 0
    try:
        port.on_client_count_changed(1)  # driver broken, state must still track
    except Exception:
        pytest.fail("driver failure must never propagate")
    assert port._client_count == 1

    # Disconnect while disconnected is a safe no-op
    port.is_connected = False
    await port._disconnect()


@pytest.mark.asyncio
async def test_reconnect_starts_at_idle_level_then_restores(monkeypatch):
    """A second connect starts at the idle level; disconnect resets to active."""
    port = _make_port(dtr="presence-on")
    driver1 = FakeSerial()
    assert await _connect_with_driver(port, driver1, monkeypatch) is True
    assert driver1.dtr is False  # idle
    port.on_client_count_changed(1)
    assert driver1.dtr is True

    await port._disconnect()
    assert driver1.dtr is True  # reset to active

    # The client still attached (count survives a device reconnect): the line
    # goes straight to the active level on re-open.
    assert port._client_count == 1
    driver2 = FakeSerial(dtr=True)
    assert await _connect_with_driver(port, driver2, monkeypatch) is True
    assert driver2.dtr is True
    await port._disconnect()

    # If the client left while the port was down, a reconnect idles the line.
    port._client_count = 0
    driver3 = FakeSerial(dtr=True)
    assert await _connect_with_driver(port, driver3, monkeypatch) is True
    assert driver3.dtr is False
    await port._disconnect()


def _config_manager(config: Dict[str, Any]):
    from openmux.server.config_manager import ConfigManager

    cm = ConfigManager.__new__(ConfigManager)
    cm.config = config
    return cm
