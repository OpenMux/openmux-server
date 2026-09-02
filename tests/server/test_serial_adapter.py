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
        dtr=None,  # issue #63: omitted = untouched (was the True default)
        rts=None,
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
    # Running port has all defaults (timeout=1.0, no dtr/rts = untouched,
    # flow_control="none", …)
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
    duplicates are flagged offline before anything opens the device."""
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
async def test_create_port_duplicate_device_offline(monkeypatch):
    """Issue #57: create_port (soft-reload add path) leaves the duplicate
    registered and offline; destroying the claimant clears the flag."""
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


def _make_spw_with_groups(**config_overrides) -> SimpleNamespace:
    """Like _make_spw, but the port also mirrors the group lists on itself.

    A real SerialPortWrapper exposes ``read_write_groups`` / ``read_only_groups``
    as properties over ``config``; this mock mirrors that so the reconcile
    in-place update has the same read/write surface (spw.config.* and spw.*).
    """
    cfg = _make_spw(**config_overrides).config
    cfg.read_write_groups = list(config_overrides.get("read_write_groups", []))
    cfg.read_only_groups = list(config_overrides.get("read_only_groups", []))

    class _Spw:
        def __init__(self):
            self.config = cfg
            self.description = ""
            self.state = PortState.ACTIVE
            self.status_message = ""
            self._stop = None

        @property
        def read_write_groups(self):
            return self.config.read_write_groups

        @read_write_groups.setter
        def read_write_groups(self, value):
            self.config.read_write_groups = list(value or [])

        @property
        def read_only_groups(self):
            return self.config.read_only_groups

        @read_only_groups.setter
        def read_only_groups(self, value):
            self.config.read_only_groups = list(value or [])

        async def stop(self):
            self._stop = True

    return _Spw()


@pytest.mark.asyncio
async def test_serial_wrapper_group_attrs_are_live_views_on_config():
    """The wrapper's group attrs must track config in both directions.

    This is what lets a Soft Reload's in-place config update become visible to
    the access ladder through the PM wrapper, with no port re-creation.
    """
    spw = _make_spw_with_groups()
    assert spw.read_write_groups == []
    assert spw.read_only_groups == []

    # Property write lands in config.
    spw.read_write_groups = ["ops"]
    spw.read_only_groups = ["viewers"]
    assert spw.config.read_write_groups == ["ops"]
    assert spw.config.read_only_groups == ["viewers"]

    # A direct config rebind (what reconcile does) is visible through the property.
    spw.config.read_write_groups = ["oncall"]
    assert spw.read_write_groups == ["oncall"]


@pytest.mark.asyncio
async def test_reconcile_ports_updates_groups_in_place_without_restart(monkeypatch):
    """A groups-only change is not material: no destroy/create; lists update in place.

    The old behavior re-created the port (disconnecting users) only on a Full
    Reload when a group changed; soft reload must now just rewrite the lists.
    """
    adapter = _make_adapter()
    adapter.serial_ports["a"] = _make_spw_with_groups(  # type: ignore
        device="/dev/ttyUSB0",
        read_write_groups=["ops"],
        read_only_groups=["viewers"],
    )

    destroyed: list[str] = []
    created: list[str] = []

    async def fake_destroy(name):
        destroyed.append(name)

    async def fake_create(name, cfg):
        created.append(name)

    monkeypatch.setattr(SerialAdapter, "destroy_port", fake_destroy)
    monkeypatch.setattr(SerialAdapter, "create_port", fake_create)

    summary = await adapter.reconcile_ports(
        {
            "serial_ports": [
                {
                    "name": "a",
                    "device": "/dev/ttyUSB0",
                    "baudrate": 9600,
                    "read_write_groups": ["ops", "oncall"],
                    "read_only_groups": [],
                },
            ]
        }
    )

    assert summary["unchanged"] == ["a"], f"groups change must not recreate: {summary}"
    assert summary["updated"] == []
    assert summary["removed"] == []
    assert summary["added"] == []
    assert destroyed == []
    assert created == []
    spw = adapter.serial_ports["a"]
    assert list(spw.read_write_groups) == ["ops", "oncall"]
    assert list(spw.read_only_groups) == []
