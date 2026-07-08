"""Unit tests for SerialAdapter.reconcile_ports change-detection logic."""
from types import SimpleNamespace

import pytest

from openmux.server.adapters.serial import SerialAdapter, SerialPortConfig


def _make_spw(**config_overrides) -> SimpleNamespace:
    """Build a minimal SerialPortWrapper-like mock with a .config matching defaults.

    The reconcile logic reads fields off ``spw.config`` to build old_cfg, so the
    config namespace must carry all fields tracked by _material_config.
    """
    cfg = SimpleNamespace(
        device="/dev/ttyUSB0",
        baudrate=9600,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=1.0,
        flow_control="none",
        dtr=True,
        rts=True,
        max_read_write_users=1,
        log_file=None,
        log_format=None,
        log_line_template=None,
        log_direction=None,
        log_directions=None,
        scrollback_size=0,
    )
    for k, v in config_overrides.items():
        setattr(cfg, k, v)

    async def _stop():
        pass

    port = SimpleNamespace(config=cfg, description="", stop=_stop)
    return port


def _make_adapter() -> SerialAdapter:
    """Return a SerialAdapter with one seed port so __init__ validates successfully."""
    return SerialAdapter("serial_ports", {
        "serial_ports": [{"name": "_seed", "device": "/dev/null"}],
    })


@pytest.mark.asyncio
async def test_reconcile_ports_unchanged():
    """Port whose config matches running defaults is not restarted on reconcile."""
    adapter = _make_adapter()
    adapter.serial_ports.clear()
    adapter.serial_ports["a"] = _make_spw(device="/dev/ttyUSB0", baudrate=9600)  # type: ignore

    # YAML with same material values — only description differs (non-material)
    summary = await adapter.reconcile_ports({"serial_ports": [
        {"name": "a", "device": "/dev/ttyUSB0", "baudrate": 9600, "description": "new"},
    ]})

    assert summary["unchanged"] == ["a"], f"Expected unchanged, got: {summary}"
    assert summary["updated"] == []


@pytest.mark.asyncio
async def test_reconcile_ports_optional_fields_default():
    """Port with only required fields in YAML matches a running port using all defaults."""
    adapter = _make_adapter()
    adapter.serial_ports.clear()
    # Running port has all defaults (timeout=1.0, dtr=True, flow_control="none", …)
    adapter.serial_ports["a"] = _make_spw(device="/dev/ttyUSB0", baudrate=115200)  # type: ignore

    # New YAML omits optional fields entirely — must still be unchanged
    summary = await adapter.reconcile_ports({"serial_ports": [
        {"name": "a", "device": "/dev/ttyUSB0", "baudrate": 115200},
    ]})

    assert summary["unchanged"] == ["a"], f"Expected unchanged, got: {summary}"
    assert summary["updated"] == []


@pytest.mark.asyncio
async def test_reconcile_ports_detects_baudrate_change():
    """A baudrate change is classified as updated."""
    adapter = _make_adapter()
    adapter.serial_ports.clear()
    adapter.serial_ports["a"] = _make_spw(device="/dev/ttyUSB0", baudrate=9600)  # type: ignore

    summary = await adapter.reconcile_ports({"serial_ports": [
        {"name": "a", "device": "/dev/ttyUSB0", "baudrate": 115200},  # changed
    ]})

    assert summary["updated"] == ["a"]
    assert summary["unchanged"] == []


@pytest.mark.asyncio
async def test_reconcile_ports_add_remove():
    """Added and removed ports are reported correctly."""
    adapter = _make_adapter()
    adapter.serial_ports.clear()
    adapter.serial_ports["a"] = _make_spw(device="/dev/ttyUSB0")  # type: ignore
    adapter.serial_ports["b"] = _make_spw(device="/dev/ttyUSB1")  # type: ignore

    # Keep 'a', remove 'b', add 'c'
    summary = await adapter.reconcile_ports({"serial_ports": [
        {"name": "a", "device": "/dev/ttyUSB0"},
        {"name": "c", "device": "/dev/ttyUSB2"},
    ]})

    assert summary["unchanged"] == ["a"]
    assert summary["removed"] == ["b"]
    assert summary["added"] == ["c"]
    assert summary["updated"] == []
