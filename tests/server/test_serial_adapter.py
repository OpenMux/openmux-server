"""Unit tests for SerialAdapter.reconcile_ports change-detection logic."""

import logging
from types import SimpleNamespace

import pytest

from openmux.server.adapters.lifecycle import PortState
from openmux.server.adapters.serial import SerialAdapter, SerialPortConfig, SerialPortWrapper


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

    port = SimpleNamespace(config=cfg, description="", stop=_stop, state=PortState.ACTIVE, status_message="")
    return port


def _make_adapter() -> SerialAdapter:
    """Return a SerialAdapter constructed with no ports (empty list is legitimate)."""
    return SerialAdapter("serial_ports", {"serial_ports": []})


def test_construct_with_empty_port_list():
    """Constructing with no ports must not raise, matching other adapters."""
    adapter = SerialAdapter("serial_ports", {"serial_ports": []})
    assert adapter.serial_ports == {}


@pytest.mark.asyncio
async def test_start_with_empty_port_list_succeeds():
    """Starting with no ports configured must succeed (warns, doesn't fail)."""
    adapter = SerialAdapter("serial_ports", {"serial_ports": []})
    assert await adapter.start() is True
    assert adapter.is_running is True


@pytest.mark.asyncio
async def test_reconcile_ports_unchanged():
    """Port whose config matches running defaults is not restarted on reconcile."""
    adapter = _make_adapter()
    adapter.serial_ports["a"] = _make_spw(device="/dev/ttyUSB0", baudrate=9600)  # type: ignore

    # YAML with same material values — only description differs (non-material)
    summary = await adapter.reconcile_ports(
        {
            "serial_ports": [
                {"name": "a", "device": "/dev/ttyUSB0", "baudrate": 9600, "description": "new"},
            ]
        }
    )

    assert summary["unchanged"] == ["a"], f"Expected unchanged, got: {summary}"
    assert summary["updated"] == []


@pytest.mark.asyncio
async def test_reconcile_ports_optional_fields_default():
    """Port with only required fields in YAML matches a running port using all defaults."""
    adapter = _make_adapter()
    # Running port has all defaults (timeout=1.0, dtr=True, flow_control="none", …)
    adapter.serial_ports["a"] = _make_spw(device="/dev/ttyUSB0", baudrate=115200)  # type: ignore

    # New YAML omits optional fields entirely — must still be unchanged
    summary = await adapter.reconcile_ports(
        {
            "serial_ports": [
                {"name": "a", "device": "/dev/ttyUSB0", "baudrate": 115200},
            ]
        }
    )

    assert summary["unchanged"] == ["a"], f"Expected unchanged, got: {summary}"
    assert summary["updated"] == []


@pytest.mark.asyncio
async def test_reconcile_ports_detects_baudrate_change():
    """A baudrate change is classified as updated."""
    adapter = _make_adapter()
    adapter.serial_ports.clear()
    adapter.serial_ports["a"] = _make_spw(device="/dev/ttyUSB0", baudrate=9600)  # type: ignore

    summary = await adapter.reconcile_ports(
        {
            "serial_ports": [
                {"name": "a", "device": "/dev/ttyUSB0", "baudrate": 115200},  # changed
            ]
        }
    )

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
    summary = await adapter.reconcile_ports(
        {
            "serial_ports": [
                {"name": "a", "device": "/dev/ttyUSB0"},
                {"name": "c", "device": "/dev/ttyUSB2"},
            ]
        }
    )

    assert summary["unchanged"] == ["a"]
    assert summary["removed"] == ["b"]
    assert summary["added"] == ["c"]
    assert summary["updated"] == []


@pytest.mark.asyncio
async def test_reconcile_ports_uses_overridable_destroy_and_create(monkeypatch):
    """destroy_port/create_port must be regular methods, patchable like other adapters."""
    adapter = _make_adapter()
    adapter.serial_ports["a"] = _make_spw(device="/dev/ttyUSB0")  # type: ignore
    adapter.serial_ports["b"] = _make_spw(device="/dev/ttyUSB1")  # type: ignore

    destroyed: list[str] = []
    created: list[tuple] = []

    async def fake_destroy_port(self, port_name):
        destroyed.append(port_name)

    async def fake_create_port(self, port_name, cfg):
        created.append((port_name, cfg))

    monkeypatch.setattr(SerialAdapter, "destroy_port", fake_destroy_port)
    monkeypatch.setattr(SerialAdapter, "create_port", fake_create_port)

    summary = await adapter.reconcile_ports(
        {
            "serial_ports": [
                {"name": "a", "device": "/dev/ttyUSB0", "baudrate": 115200},  # changed -> destroy+create
                {"name": "c", "device": "/dev/ttyUSB2"},  # added -> create only
            ]
        }
    )

    assert summary["updated"] == ["a"]
    assert summary["removed"] == ["b"]
    assert summary["added"] == ["c"]
    assert set(destroyed) == {"a", "b"}
    assert {name for name, _ in created} == {"a", "c"}


def _make_real_adapter_with_ports(ports: dict) -> SerialAdapter:
    """Build a SerialAdapter whose serial_ports holds real wrappers.

    ``ports`` maps port name -> device path. The wrappers carry a real logger
    and config; nothing is connected.
    """
    adapter = _make_adapter()
    for name, device in ports.items():
        cfg = SerialPortConfig(name=name, description=name, device=device)
        adapter.serial_ports[name] = SerialPortWrapper(cfg, logging.getLogger("test.serial"))
    return adapter


@pytest.mark.asyncio
async def test_parse_port_configs_flags_duplicate_device():
    """Issue #57: at load time the first port claims a device; later
    duplicates are flagged unstartable before anything opens the device."""
    adapter = SerialAdapter(
        "serial_ports",
        {"serial_ports": [{"name": "a", "device": "/dev/ttyUSB0"}, {"name": "b", "device": "/dev/ttyUSB0"}]},
    )
    a, b = adapter.serial_ports["a"], adapter.serial_ports["b"]
    assert a.status_message == ""
    assert b.state is PortState.DEGRADED
    assert "/dev/ttyUSB0" in b.status_message and "a" in b.status_message
    # The duplicate never spawns a connection supervisor on start, and start
    # reports not started; the port stays in serial_ports (registered, visible).
    assert await b.start() is False
    assert b.connection_task is None
    assert b.is_connected is False
    # start() does not clear the flag
    assert b.status_message
    # A unique device still starts
    assert await a.start() is True
    assert a.connection_task is not None
    await a.stop()


@pytest.mark.asyncio
async def test_recompute_duplicate_device_flags_flags_and_clears():
    """Issue #57: recomputing clears the flag when the other port goes away
    (its supervisor resumes)."""
    adapter = _make_real_adapter_with_ports({"a": "/dev/ttyUSB0", "b": "/dev/ttyUSB0"})
    b = adapter.serial_ports["b"]
    b.state = PortState.ACTIVE  # simulate a previously healthy port

    adapter._recompute_duplicate_device_flags()
    assert adapter.serial_ports["a"].status_message == ""
    assert b.status_message
    assert b.state is PortState.DEGRADED

    del adapter.serial_ports["a"]
    adapter._recompute_duplicate_device_flags()
    assert b.status_message == ""
    assert b.state is PortState.ACTIVE
    assert b.connection_task is not None  # supervisor resumed
    await b.stop()


@pytest.mark.asyncio
async def test_create_port_duplicate_device_unstartable(monkeypatch):
    """Issue #57: create_port (soft-reload add path) leaves the duplicate
    registered and unstartable; destroying the claimant clears the flag."""
    adapter = _make_adapter()
    adapter.main_port_manager = None
    adapter.serial_ports["a"] = _make_spw(device="/dev/ttyUSB0")

    async def real_create_port(self, port_name, cfg):
        import logging

        serial_cfg = SerialPortConfig(name=port_name, description=cfg.get("description", ""), device=cfg["device"])
        wrapper = SerialPortWrapper(serial_cfg, logging.getLogger("test.serial"))
        self.serial_ports[serial_cfg.name] = wrapper
        self._recompute_duplicate_device_flags()
        await wrapper.start()
        if self.main_port_manager:
            await self.main_port_manager.register_unified_port(serial_cfg.name, wrapper, self)
        return wrapper

    monkeypatch.setattr(SerialAdapter, "create_port", real_create_port)
    wrapper_b = await adapter.create_port("b", {"name": "b", "device": "/dev/ttyUSB0"})
    assert wrapper_b is not None
    assert wrapper_b.status_message
    assert wrapper_b.state is PortState.DEGRADED
    assert wrapper_b.connection_task is None
    assert adapter.serial_ports["a"].status_message == ""

    # Releasing the claimant must clear the duplicate flag on its next pass.
    async def noop_destroy(self, port_name):
        if port_name in self.serial_ports:
            del self.serial_ports[port_name]
        self._recompute_duplicate_device_flags()

    monkeypatch.setattr(SerialAdapter, "destroy_port", noop_destroy)
    await adapter.destroy_port("a")
    assert wrapper_b.status_message == ""
    assert wrapper_b.state is PortState.ACTIVE
    assert wrapper_b.connection_task is not None
    await wrapper_b.stop()
