"""Tests for the PDU power adapter (openmux/server/adapters/pdu.py).

Covers: config validation (incl. 3-phase-style string ids), dummy driver,
outlet discovery + polling, state control, off-impact math (dual feed and
multi-device), live console-side mapping reads, snapshot consistency,
meta-event fan-out, per-PDU poll intervals, soft reload reconciliation,
and PDU-offline behavior.
"""

import asyncio

import pytest

asyncio_test = pytest.mark.asyncio

from openmux.server.adapters.pdu import (
    DEFAULT_POLL_INTERVAL,
    DRIVER_INFO,
    DRIVERS,
    DummyDriver,
    OutletReading,
    PduAdapter,
    PduState,
    driver_catalog,
)

POWER_SECTION = {
    "power": {
        "enabled": True,
        "pdus": [
            {
                "name": "rack1",
                "description": "Rack 1 PDU",
                "driver": "dummy",
                "poll_interval": 0,  # keep tests free of background polling
                "options": {"outlets": ["1", "2", "3"]},
                "outlets": [{"id": "3", "description": "Switch A"}],
            },
            {
                "name": "phaseA",
                "driver": "dummy",
                "poll_interval": 0,
                "options": {"outlets": ["A1", "B1", "C2"], "watts_on": 55.0, "volts": 230.0},
            },
        ],
    }
}


class _FakePort:
    def __init__(self, name, power=()):
        self.name = name
        self.power = list(power)
        self.unified_port = self


class _FakePortManager:
    """Stands in for PortManager: a ports dict of fake port objects + meta sink."""

    def __init__(self, ports):
        self.ports = dict(ports)
        self.meta_events = []

    def notify_meta_updated(self, port_name, changes):
        self.meta_events.append((port_name, changes))


def _make_adapter(pm=None, section=None):
    adapter = PduAdapter("power", section or POWER_SECTION)
    if pm is not None:
        adapter.main_port_manager = pm
    return adapter


async def _start(adapter):
    assert await adapter.start() is True
    await asyncio.sleep(0)
    return adapter


# --- validation -------------------------------------------------------------


def test_validate_config_accepts_wrapped_section():
    assert PduAdapter.validate_config(POWER_SECTION) is True


def test_validate_config_accepts_flat_unified_form():
    cfg = {"name": "power", "type": "power", "pdus": [{"name": "rack1", "driver": "dummy"}]}
    assert PduAdapter.validate_config(cfg) is True


def test_validate_config_rejects_bad_names_and_ids():
    assert PduAdapter.validate_config({"power": {"pdus": [{"name": "ra ck", "driver": "dummy"}]}}) is False
    assert PduAdapter.validate_config({"power": {"pdus": [{"name": "a.b", "driver": "dummy"}]}}) is False
    assert PduAdapter.validate_config({"power": {"pdus": [{"name": "r", "driver": "nope"}]}}) is False
    assert PduAdapter.validate_config({"power": {"pdus": [{"name": "r", "driver": "dummy", "poll_interval": -1}]}}) is False
    assert (
        PduAdapter.validate_config({"power": {"pdus": [{"name": "r", "driver": "dummy", "outlets": [{"id": "A.1"}]}]}})
        is False
    )
    # duplicate pdu names
    assert (
        PduAdapter.validate_config({"power": {"pdus": [{"name": "r", "driver": "dummy"}, {"name": "r", "driver": "dummy"}]}})
        is False
    )
    # 3-phase style ids are accepted
    assert (
        PduAdapter.validate_config(
            {"power": {"pdus": [{"name": "phaseA", "driver": "dummy", "outlets": [{"id": "A1"}, {"id": "C2"}]}]}}
        )
        is True
    )
    # disabled section with no pdus is fine
    assert PduAdapter.validate_config({"power": {"enabled": False}}) is True
    # non-list pdus
    assert PduAdapter.validate_config({"power": {"pdus": {"name": "x"}}}) is False


# --- dummy driver ------------------------------------------------------------


@asyncio_test
async def test_dummy_driver_default_and_custom_ids():
    d = DRIVERS["dummy"]({})
    assert await d.list_outlets() == [str(i) for i in range(1, 9)]
    d2 = DRIVERS["dummy"]({"outlets": ["A1", "B1", "C2"]})
    assert await d2.list_outlets() == ["A1", "B1", "C2"]
    reading = await d2.set_state("A1", False)
    assert reading.on is False
    reading = await d2.set_state("A1", True)
    assert reading.on is True
    with pytest.raises(ValueError):
        await d2.set_state("Z9", True)


def test_dummy_driver_rejects_blank_id_list():
    with pytest.raises(ValueError):
        DRIVERS["dummy"]({"outlets": ["", "  "]})
    # blank entries alongside valid ones are dropped
    d = DRIVERS["dummy"]({"outlets": ["1", "", " 2 "]})
    assert d._ids == ["1", "2"]


def test_driver_catalog_matches_registry_and_is_json_safe():
    import json

    catalog = driver_catalog()
    # One entry per registered driver, same key set as DRIVERS.
    assert [e["driver"] for e in catalog] == list(DRIVERS)
    assert [e["driver"] for e in catalog] == list(DRIVER_INFO)
    by_name = {e["driver"]: e for e in catalog}
    # The dummy driver advertises its options and a JSON example.
    dummy = by_name["dummy"]
    assert dummy["label"] == "Dummy"
    assert dummy["description"]
    assert [k["key"] for k in dummy["options_keys"]] == ["outlets", "watts_on", "volts"]
    assert dummy["options_example"] == {"outlets": ["1", "2", "3"]}
    # The whole catalog must be JSON-serializable for the /data bootstrap.
    json.loads(json.dumps(catalog))


def test_driver_catalog_falls_back_when_info_entry_missing():
    # When a key exists in DRIVERS but DRIVER_INFO has no entry, the catalog
    # still emits a bare entry (label = the key) instead of raising.
    from openmux.server.adapters import pdu as pdu_mod

    saved_drivers = dict(pdu_mod.DRIVERS)
    saved_info = dict(pdu_mod.DRIVER_INFO)
    try:
        pdu_mod.DRIVERS["fake"] = lambda opts: None  # not called by the catalog
        # No DRIVER_INFO["fake"] on purpose.
        catalog = driver_catalog()
        fake = [e for e in catalog if e["driver"] == "fake"][0]
        assert fake["label"] == "fake"
        assert fake["description"] == ""
        assert fake["options_keys"] is None
        assert fake["options_example"] is None
    finally:
        pdu_mod.DRIVERS.clear()
        pdu_mod.DRIVERS.update(saved_drivers)
        pdu_mod.DRIVER_INFO.clear()
        pdu_mod.DRIVER_INFO.update(saved_info)


@asyncio_test
async def test_default_poll_interval_constant():
    assert DEFAULT_POLL_INTERVAL == 10


# --- discovery + readings -----------------------------------------------------


@asyncio_test
async def test_start_reads_all_outlets_and_annotates():
    adapter = await _start(_make_adapter())
    snap = adapter.get_power_snapshot()
    names = [p["name"] for p in snap["pdus"]]
    assert names == ["phaseA", "rack1"]  # sorted
    rack1 = next(p for p in snap["pdus"] if p["name"] == "rack1")
    assert rack1["online"] is True
    assert rack1["outlet_count"] == 3
    assert rack1["outlets_on"] == 3
    assert rack1["description"] == "Rack 1 PDU"
    outlet3 = next(o for o in rack1["outlets"] if o["id"] == "3")
    assert outlet3["description"] == "Switch A"
    assert outlet3["on"] is True
    assert outlet3["ref"] == "rack1.3"
    assert outlet3["watts"] is not None
    assert outlet3["volts"] is not None
    assert outlet3["mapped_ports"] == []
    assert outlet3["any_mapped"] is False
    # phase-style ids survive discovery and control surface
    phaseA = next(p for p in snap["pdus"] if p["name"] == "phaseA")
    assert [o["id"] for o in phaseA["outlets"]] == ["A1", "B1", "C2"]
    await adapter.stop()
    assert adapter.pdus == {}


@asyncio_test
async def test_unannotated_outlets_still_listed():
    adapter = await _start(_make_adapter())
    snap = adapter.get_power_snapshot()
    phaseA = next(p for p in snap["pdus"] if p["name"] == "phaseA")
    assert all(o["description"] == "" for o in phaseA["outlets"])
    assert phaseA["outlet_count"] == 3
    await adapter.stop()


# --- live console-side mapping --------------------------------------------------


@asyncio_test
async def test_port_power_map_and_off_impact_single_feed():
    pm = _FakePortManager({"console1": _FakePort("console1", ["rack1.3"])})
    adapter = await _start(_make_adapter(pm))
    assert adapter.port_power_map("console1") == ["rack1.3"]
    assert adapter.port_power_map("missing") == []
    impact = adapter.compute_off_impact("rack1.3")
    assert [e["port"] for e in impact["losing_power"]] == ["console1"]
    assert impact["staying_up"] == []
    await adapter.stop()


@asyncio_test
async def test_off_impact_dual_feed_stays_up():
    pm = _FakePortManager({"console1": _FakePort("console1", ["rack1.3", "phaseA.A1"])})
    adapter = await _start(_make_adapter(pm))
    # Both feeds currently on: losing one leaves the device powered.
    impact = adapter.compute_off_impact("rack1.3")
    assert impact["losing_power"] == []
    assert [e["port"] for e in impact["staying_up"]] == ["console1"]
    assert impact["staying_up"][0]["via"] == ["phaseA.A1"]

    # Now turn phaseA.A1 off: console1 has no live feed left.
    res = await adapter.set_outlet("phaseA.A1", False)
    assert res["ok"] is True
    impact = adapter.compute_off_impact("rack1.3")
    assert [e["port"] for e in impact["losing_power"]] == ["console1"]
    await adapter.stop()


@asyncio_test
async def test_off_impact_multi_device_warning():
    pm = _FakePortManager(
        {
            "console1": _FakePort("console1", ["rack1.3"]),
            "console2": _FakePort("console2", ["rack1.3"]),
            "console3": _FakePort("console3", ["rack1.3", "rack1.1"]),
        }
    )
    adapter = await _start(_make_adapter(pm))
    impact = adapter.compute_off_impact("rack1.3")
    assert sorted(e["port"] for e in impact["losing_power"]) == ["console1", "console2"]
    assert [e["port"] for e in impact["staying_up"]] == ["console3"]
    await adapter.stop()


@asyncio_test
async def test_unresolved_refs_surfaced_in_snapshot():
    pm = _FakePortManager({"console1": _FakePort("console1", ["rack1.3", "ghost.9"])})
    adapter = await _start(_make_adapter(pm))
    snap = adapter.get_power_snapshot()
    assert snap["unresolved_refs"] == ["ghost.9"]
    await adapter.stop()


@asyncio_test
async def test_port_power_payload_states():
    pm = _FakePortManager({"c1": _FakePort("c1", ["rack1.3", "phaseA.A1"])})
    adapter = await _start(_make_adapter(pm))
    # all on
    payload = adapter.port_power_payload("c1")
    assert payload["state"] == "all"
    assert payload["feeds_total"] == 2
    assert payload["all_power_lost"] is False
    # partial
    await adapter.set_outlet("phaseA.A1", False)
    payload = adapter.port_power_payload("c1")
    assert payload["state"] == "some"
    assert payload["feeds_on"] == 1
    # none
    await adapter.set_outlet("rack1.3", False)
    payload = adapter.port_power_payload("c1")
    assert payload["state"] == "none"
    assert payload["all_power_lost"] is True
    # unmapped port
    pm.ports["c2"] = _FakePort("c2", [])
    assert adapter.port_power_payload("c2") is None
    # unknown PDU -> grey/unknown
    pm.ports["c3"] = _FakePort("c3", ["ghost.A1"])
    payload = adapter.port_power_payload("c3")
    assert payload["state"] == "unknown"
    await adapter.stop()


# --- control + event fan-out -----------------------------------------------------


@asyncio_test
async def test_set_outlet_emits_meta_and_listener():
    pm = _FakePortManager({"console1": _FakePort("console1", ["rack1.3"])})
    adapter = await _start(_make_adapter(pm))
    events = []
    adapter.register_state_listener(lambda ref, on, ports: _append(events, (ref, on, ports)))
    res = await adapter.set_outlet("rack1.3", False)
    assert res["ok"] is True
    assert res["reading"]["on"] is False
    assert [e["port"] for e in res["impact"]["losing_power"]] == ["console1"]
    await asyncio.sleep(0)
    (port_name, changes) = pm.meta_events[-1]
    assert port_name == "console1"
    assert changes["event"] == "power_outlet_changed"
    assert changes["outlet"] == "rack1.3"
    assert changes["on"] is False
    assert changes["all_power_lost"] is True
    assert events == [("rack1.3", False, ["console1"])]
    await adapter.stop()


@asyncio_test
async def test_set_outlet_errors():
    adapter = await _start(_make_adapter())
    res = await adapter.set_outlet("badref", False)
    assert res["ok"] is False and "invalid outlet ref" in res["error"]
    res = await adapter.set_outlet("ghost.1", False)
    assert res["ok"] is False and "not configured" in res["error"]
    res = await adapter.set_outlet("rack1.9", False)
    assert res["ok"] is False and "unknown outlet" in res["error"]
    # set on with no mapped ports: empty impact
    res = await adapter.set_outlet("rack1.1", True)
    assert res["ok"] is True and res["impact"]["losing_power"] == []
    await adapter.stop()


@asyncio_test
async def test_set_outlet_disabled_feature_refused():
    adapter = PduAdapter("power", {"power": {"enabled": False, "pdus": []}})
    await _start(adapter)
    res = await adapter.set_outlet("rack1.3", False)
    assert res["ok"] is False and "disabled" in res["error"]
    await adapter.stop()


async def _append(events, item):
    events.append(item)


# --- PDU offline behavior -----------------------------------------------------------


@asyncio_test
async def test_pdu_offline_drops_on_state_and_emits():
    pm = _FakePortManager({"console1": _FakePort("console1", ["rack1.3"])})
    adapter = await _start(_make_adapter(pm))
    state = adapter.pdus["rack1"]
    state.online = True

    # Simulate the driver dying
    async def _dead():
        raise ConnectionError("pdu gone")

    state.driver.read_states = _dead  # type: ignore[method-assign]
    await adapter._refresh_readings(state)
    assert state.online is False
    assert state.readings["3"].on is None
    (port_name, changes) = pm.meta_events[-1]
    assert port_name == "console1"
    assert changes["on"] is None
    assert changes["all_power_lost"] is True

    # Recovery restores readings
    async def _ok():
        return {oid: adapter.pdus["rack1"].driver._reading(oid) for oid in adapter.pdus["rack1"].driver._ids}

    state.driver.read_states = _ok  # type: ignore[method-assign]
    await adapter._refresh_readings(state)
    assert state.online is True
    assert state.readings["3"].on is True
    await adapter.stop()


@asyncio_test
async def test_discovery_failure_keeps_adapter_running():
    section = {
        "power": {
            "pdus": [
                {"name": "ok1", "driver": "dummy", "poll_interval": 0, "options": {"outlets": ["1"]}},
                {"name": "bad", "driver": "dummy", "poll_interval": 0, "options": {"outlets": ["1"], "fail_discovery": True}},
            ]
        }
    }
    adapter = PduAdapter("power", section)
    assert await adapter.start() is True  # one dead PDU must not fail the whole adapter
    assert adapter.pdus["bad"].online is False
    assert adapter.pdus["bad"].readings == {}
    assert adapter.pdus["ok1"].online is True
    assert adapter.pdus["ok1"].readings["1"].on is True
    await adapter.stop()


# --- per-PDU poll tasks ---------------------------------------------------------------


@asyncio_test
async def test_poll_task_respects_per_pdu_interval_and_zero_cancels():
    section = {
        "power": {
            "pdus": [
                {"name": "fast", "driver": "dummy", "poll_interval": 0.02, "options": {"outlets": ["1"]}},
                {"name": "stopped", "driver": "dummy", "poll_interval": 0, "options": {"outlets": ["1"]}},
            ]
        }
    }
    adapter = PduAdapter("power", section)
    assert await adapter.start() is True
    fast, stopped = adapter.pdus["fast"], adapter.pdus["stopped"]
    assert fast.task is not None
    assert stopped.task is None  # poll_interval 0 = on-demand only
    counts = {"fast": 0}
    original = fast.driver.read_states

    async def _count():
        counts["fast"] += 1
        return await original()

    fast.driver.read_states = _count  # type: ignore[method-assign]
    await asyncio.sleep(0.1)
    assert counts["fast"] >= 2
    await adapter.stop()
    assert fast.task is None or fast.task.done()


# --- soft reload ---------------------------------------------------------------------


@asyncio_test
async def test_reconcile_adds_removes_and_in_place_notes():
    adapter = await _start(_make_adapter())
    new_section = {
        "power": {
            "enabled": True,
            "pdus": [
                # unchanged name, only description + annotation changed
                {
                    "name": "rack1",
                    "description": "NEW",
                    "driver": "dummy",
                    "poll_interval": 0,
                    "options": {"outlets": ["1", "2", "3"]},
                    "outlets": [{"id": "3", "description": "changed"}],
                },
                # new PDU
                {"name": "rack2", "driver": "dummy", "poll_interval": 0, "options": {"outlets": ["1"]}},
            ],
        }
    }
    res = await adapter.reconcile_ports(new_section)
    assert res == {"added": ["rack2"], "removed": ["phaseA"], "updated": [], "unchanged": ["rack1"]}
    assert adapter.pdus["rack1"].description == "NEW"
    assert adapter.pdus["rack1"].annotations == {"3": "changed"}
    assert "rack2" in adapter.pdus and adapter.pdus["rack2"].online is True
    assert "phaseA" not in adapter.pdus
    await adapter.stop()


@asyncio_test
async def test_reconcile_material_change_recreates():
    adapter = await _start(_make_adapter())
    new_section = {
        "power": {
            "pdus": [
                # same name/name but different options -> material -> recreate
                {"name": "rack1", "driver": "dummy", "poll_interval": 0, "options": {"outlets": ["9"]}},
                # poll_interval change is material too
                {"name": "phaseA", "driver": "dummy", "poll_interval": 7, "options": {"outlets": ["A1", "B1", "C2"]}},
            ]
        }
    }
    res = await adapter.reconcile_ports(new_section)
    assert res["updated"] == ["phaseA", "rack1"]
    assert sorted(adapter.pdus["rack1"].readings.keys()) == ["9"]
    assert adapter.pdus["phaseA"].poll_interval == 7
    assert adapter.pdus["phaseA"].task is not None  # non-zero interval started a task
    await adapter.stop()


@asyncio_test
async def test_reconcile_dropped_section_removes_all():
    adapter = await _start(_make_adapter())
    res = await adapter.reconcile_ports({})
    assert res["removed"] == ["phaseA", "rack1"]
    assert adapter.pdus == {}
    # and adding the section back (main.py bootstrap path) restores it
    res = await adapter.reconcile_ports(POWER_SECTION)
    assert sorted(res["added"]) == ["phaseA", "rack1"]
    assert adapter.pdus["rack1"].online is True
    await adapter.stop()


@asyncio_test
async def test_reconcile_wrap_shape_and_none():
    adapter = await _start(_make_adapter())
    res = await adapter.reconcile_ports({"power": POWER_SECTION["power"]})
    assert res["unchanged"] == ["phaseA", "rack1"]
    res = await adapter.reconcile_ports(None)
    assert res["removed"] == ["phaseA", "rack1"]
    await adapter.stop()


# --- misc ---------------------------------------------------------------------------


def test_get_adapter_type():
    # Stable "power" key (lowercase, matches the main.py soft-reload dispatch and
    # the security-policy adapter-type list after separator normalization).
    assert PduAdapter("power", {}).get_adapter_type() == "power"


def test_get_status_info_shape():
    adapter = PduAdapter("power", POWER_SECTION)
    info = adapter.get_status_info()
    assert info["type"] == "power"
    assert info["status"] == "stopped"
    assert info["details"]["pdus"] == []


@asyncio_test
async def test_capabilities_include_manages_power():
    from openmux.server.adapters.base_adapter import AdapterCapability

    adapter = PduAdapter("power", POWER_SECTION)
    assert AdapterCapability.MANAGES_POWER in adapter.get_capabilities()


@pytest.mark.parametrize(
    "ref,expected",
    [
        ("rack1.3", ("rack1", "3")),
        ("phaseA.A1", ("phaseA", "A1")),
    ],
)
def test_parse_ref_ok(ref, expected):
    assert PduAdapter._parse_ref(ref) == expected


@pytest.mark.parametrize("ref", ["nodot", "a.b.c", ".x", "x.", "a b.c", "a.b "])
def test_parse_ref_bad(ref):
    with pytest.raises(ValueError):
        PduAdapter._parse_ref(ref)
