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
    remote_ref,
    split_remote_ref,
)
from openmux.server.data_logger import DataLogger


class _CapDL:
    """DataLogger stand-in: captures record_meta calls, keeps nothing on disk."""

    def __init__(self):
        self.calls = []
        self.base_dir = None

    def record_meta(self, port_name, event, client_id=None, meta=None, port_obj=None):
        self.calls.append({"port_name": port_name, "event": event, "client_id": client_id, "meta": meta})


@pytest.fixture(autouse=True)
def _cap_data_logger(monkeypatch):
    """Route every DataLogger use in this module to a capture stub."""
    cap = _CapDL()
    monkeypatch.setattr(DataLogger, "get", classmethod(lambda cls: cap))
    yield cap


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


# --- control audit log + port-log notice ----------------------------------------


@asyncio_test
async def test_set_outlet_audit_line_and_port_log_notice(caplog):
    """A successful switch writes one audit line and one port-log meta event
    per affected console port, with the attached-session notice text."""
    pm = _FakePortManager({"console1": _FakePort("console1", ["rack1.1"])})
    adapter = await _start(_make_adapter(pm))
    with caplog.at_level("INFO", logger="openmux.adapter.power"):
        res = await adapter.set_outlet("rack1.1", False, user="alice", client_id="cid-9")
    assert res["ok"] is True
    audit = [r for r in caplog.records if "POWER CONTROL" in r.getMessage()]
    assert len(audit) == 1
    msg = audit[0].getMessage()
    assert "user alice turned rack1.1 off" in msg
    assert "losing all power: console1" in msg
    assert "client cid-9" in msg
    # The port data log gains the SAME text the attached sessions see.
    dlc = DataLogger.get()
    assert len(dlc.calls) == 1
    call = dlc.calls[0]
    assert call["port_name"] == "console1"
    assert call["event"] == "power_control_notice"
    assert call["client_id"] == "cid-9"
    assert call["meta"]["outlet"] == "rack1.1"
    assert call["meta"]["state"] == "off"
    assert call["meta"]["user"] == "alice"
    assert call["meta"]["text"] == "[POWER WARNING] all power feeds to this console are now off"
    await adapter.stop()


@asyncio_test
async def test_set_outlet_port_log_distinguishes_dual_feed():
    """Turning an outlet OFF: a console with ANOTHER live feed gets the plain
    feed-off line (it stays up); a console whose last feed dies gets the
    all-power-lost warning. Turning ON always gets the plain feed-on line."""
    pm = _FakePortManager(
        {
            "c_duo": _FakePort("c_duo", ["rack1.1", "rack1.2"]),  # stays up via rack1.2
            "c_single": _FakePort("c_single", ["rack1.1"]),  # loses all power
        }
    )
    adapter = await _start(_make_adapter(pm))
    await adapter.set_outlet("rack1.1", False, user="bob")
    dlc = DataLogger.get()
    by_port = {c["port_name"]: c for c in dlc.calls}
    assert set(by_port) == {"c_duo", "c_single"}
    assert by_port["c_duo"]["meta"]["text"] == "[POWER] feed rack1.1 is now off"
    assert by_port["c_single"]["meta"]["text"] == "[POWER WARNING] all power feeds to this console are now off"
    # Back on: plain notice for both consoles.
    dlc.calls.clear()
    await adapter.set_outlet("rack1.1", True, user="bob")
    by_port = {c["port_name"]: c for c in dlc.calls}
    assert set(by_port) == {"c_duo", "c_single"}
    assert all(c["meta"]["text"] == "[POWER] feed rack1.1 is now on" for c in by_port.values())
    await adapter.stop()


@asyncio_test
async def test_set_outlet_failure_writes_no_audit_or_port_log(caplog):
    """Driver failure: the error line is logged, but no audit line and no
    port-log notice are written."""
    pm = _FakePortManager({"console1": _FakePort("console1", ["rack1.1"])})
    adapter = await _start(_make_adapter(pm))
    state = adapter.pdus["rack1"]
    orig = state.driver.set_state

    async def _boom(outlet_id, on):
        raise RuntimeError("driver exploded")

    state.driver.set_state = _boom
    try:
        with caplog.at_level("INFO", logger="openmux.adapter.power"):
            res = await adapter.set_outlet("rack1.1", False, user="carol")
    finally:
        state.driver.set_state = orig
    assert res["ok"] is False and "driver exploded" in res["error"]
    assert not [r for r in caplog.records if "POWER CONTROL" in r.getMessage()]
    assert [r for r in caplog.records if "set_state failed" in r.getMessage()]
    dlc = DataLogger.get()
    assert dlc.calls == []
    await adapter.stop()


@asyncio_test
async def test_set_outlet_audit_without_mapped_ports_is_still_recorded(caplog):
    """A switch with no console mapped to the outlet still logs the audit line,
    but writes no port-log notice (no port to write to)."""
    adapter = await _start(_make_adapter())
    dlc = DataLogger.get()
    with caplog.at_level("INFO", logger="openmux.adapter.power"):
        res = await adapter.set_outlet("rack1.1", True, user="dave")
    assert res["ok"] is True
    audit = [r for r in caplog.records if "POWER CONTROL" in r.getMessage()]
    assert len(audit) == 1
    assert "user dave turned rack1.1 on" in audit[0].getMessage()
    assert dlc.calls == []
    await adapter.stop()


@asyncio_test
async def test_set_outlet_audit_unknown_user_is_recorded_as_unknown(caplog):
    """Callers that do not pass a user (none in v1 does; defense in depth)
    still produce an audit line with user=unknown."""
    adapter = await _start(_make_adapter())
    with caplog.at_level("INFO", logger="openmux.adapter.power"):
        res = await adapter.set_outlet("rack1.1", False)
    assert res["ok"] is True
    audit = [r for r in caplog.records if "POWER CONTROL" in r.getMessage()]
    assert len(audit) == 1
    assert "user unknown turned rack1.1 off" in audit[0].getMessage()
    assert "client" not in audit[0].getMessage()
    await adapter.stop()


# --- OMXCTRL power frames (console CLI `p` menu, TCP + WebSocket) -----------


class _FrameAuth:
    def __init__(self, perms, groups=None):
        self._perm = perms
        self._groups = groups or {}

    def get_user_permissions(self, username):
        return self._perm.get(username)

    def get_user_groups(self, username):
        if self._perm.get(username) is None:
            return set()
        return {"user"} | set(self._groups.get(username) or [])


class _FrameCm:
    """Console-manager stand-in mirroring the attach-time group ACL ladder."""

    def __init__(self, pm, auth):
        self.port_manager = pm
        self.auth_manager = auth

    def blocked_ports_for_user(self, port_names, username):
        if self.auth_manager.get_user_permissions(username) == "admin":
            return []
        user_groups = self.auth_manager.get_user_groups(username)
        blocked = []
        for name in port_names:
            port = self.port_manager.ports.get(name)
            rw = set(getattr(port, "read_write_groups", None) or [])
            ro = set(getattr(port, "read_only_groups", None) or [])
            if rw or ro:
                if not (user_groups & rw):
                    blocked.append(name)
        return blocked


async def _frame_adapter():
    pm = _FakePortManager({"c1": _FramePort("c1", ["rack1.1", "rack1.2"])})
    auth = _FrameAuth({"u1": "read-write"})
    adapter = await _start(_make_adapter(pm))
    adapter.set_auth_manager(auth)
    adapter.set_console_manager(_FrameCm(pm, auth))
    return adapter


class _FramePort:
    def __init__(self, name, power=(), rw_groups=(), ro_groups=()):
        self.name = name
        self.power = list(power)
        self.unified_port = self
        self.read_write_groups = list(rw_groups)
        self.read_only_groups = list(ro_groups)


@asyncio_test
async def test_handle_power_frame_ignores_other_types():
    adapter = await _frame_adapter()
    assert await adapter.handle_power_frame("c1", {"type": "request_rw"}, "u1") is None
    await adapter.stop()


@asyncio_test
async def test_handle_power_frame_query_feed_shape():
    adapter = await _frame_adapter()
    res = await adapter.handle_power_frame("c1", {"type": "power_query"}, "u1")
    assert res["type"] == "power_feeds"
    assert [f["ref"] for f in res["feeds"]] == ["rack1.1", "rack1.2"]
    assert all(f["on"] is True for f in res["feeds"])
    assert res["feeds_total"] == 2
    assert res["state"] == "all"
    await adapter.stop()


@asyncio_test
async def test_handle_power_frame_query_unknown_port_is_empty():
    adapter = await _frame_adapter()
    res = await adapter.handle_power_frame("ghost", {"type": "power_query"}, "u1")
    assert res["type"] == "power_feeds"
    assert res["feeds"] == []
    assert res["state"] == "unknown"
    await adapter.stop()


@asyncio_test
async def test_handle_power_frame_switch_requires_read_write():
    adapter = await _frame_adapter()
    auth_ro = _FrameAuth({"ro": "read-only"})
    adapter.set_auth_manager(auth_ro)
    res = await adapter.handle_power_frame("c1", {"type": "power_switch", "ref": "rack1.1", "on": False}, "ro")
    assert res == {"type": "power_switch", "ok": False, "error": "insufficient permission (need read-write)"}
    assert adapter.pdus["rack1"].readings["1"].on is True
    await adapter.stop()


@asyncio_test
async def test_handle_power_frame_switch_validates_shape():
    adapter = await _frame_adapter()
    bad_ref = await adapter.handle_power_frame("c1", {"type": "power_switch", "ref": 7, "on": False}, "u1")
    assert bad_ref == {"type": "power_switch", "ok": False, "error": "invalid power switch request"}
    bad_on = await adapter.handle_power_frame("c1", {"type": "power_switch", "ref": "rack1.1", "on": "off"}, "u1")
    assert bad_on == {"type": "power_switch", "ok": False, "error": "invalid power switch request"}
    await adapter.stop()


@asyncio_test
async def test_handle_power_frame_switch_ok_reports_state_and_impact():
    adapter = await _frame_adapter()
    res = await adapter.handle_power_frame("c1", {"type": "power_switch", "ref": "rack1.1", "on": False}, "u1")
    assert res["ok"] is True and res["ref"] == "rack1.1" and res["on"] is False and res["state"] == "off"
    assert res["impact"]["change"] == "off"
    # c1 keeps rack1.2, so it is NOT in the losing-power list
    assert adapter.pdus["rack1"].readings["1"].on is False
    await adapter.stop()


@asyncio_test
async def test_handle_power_frame_switch_unknown_ref_fails_cleanly():
    adapter = await _frame_adapter()
    res = await adapter.handle_power_frame("c1", {"type": "power_switch", "ref": "ghost.9", "on": False}, "u1")
    assert res["ok"] is False
    assert "not configured" in res["error"]
    # The refusal carries the off-impact preview (parity with the web
    # POST /api/power/outlets path the in-session power menu used), even
    # though nothing changes.
    assert isinstance(res.get("impact"), dict)
    assert res["impact"]["losing_power"] is not None
    await adapter.stop()


@asyncio_test
async def test_handle_power_frame_switch_blocked_outside_groups():
    pm = _FakePortManager(
        {
            "c2": _FramePort("c2", ["rack1.2"], rw_groups=["ops"]),
            "c9": _FramePort("c9", ["rack1.2"], rw_groups=["lab"]),
        }
    )
    auth = _FrameAuth({"ops": "read-write"}, groups={"ops": ["ops"]})
    adapter = await _start(_make_adapter(pm))
    adapter.set_auth_manager(auth)
    adapter.set_console_manager(_FrameCm(pm, auth))
    res = await adapter.handle_power_frame("c9", {"type": "power_switch", "ref": "rack1.2", "on": False}, "ops")
    assert res["ok"] is False
    assert "outside your groups" in res["error"]
    assert "c9" in res["error"]
    assert "needs admin" in res["error"]
    assert adapter.pdus["rack1"].readings["2"].on is True
    await adapter.stop()


@asyncio_test
async def test_handle_power_frame_switch_admin_bypasses_groups():
    pm = _FakePortManager({"c9": _FramePort("c9", ["rack1.2"], rw_groups=["lab"])})
    auth = _FrameAuth({"boss": "admin"})
    adapter = await _start(_make_adapter(pm))
    adapter.set_auth_manager(auth)
    adapter.set_console_manager(_FrameCm(pm, auth))
    res = await adapter.handle_power_frame("c9", {"type": "power_switch", "ref": "rack1.2", "on": False}, "boss")
    assert res["ok"] is True and res["state"] == "off"
    await adapter.stop()


# --- remote awareness (outlet federation) --------------------------------------


class _FakeOrigin:
    def __init__(self, server_id):
        self.server_id = server_id


class _FakeRemoteMeta:
    def __init__(self, server_id):
        self.origin_server = _FakeOrigin(server_id)
        self.power = None


class _FakeRemotePort:
    """Stands in for a muxcon RemotePortProxy (the port objects it never is).

    ``feeds`` are the ORIGIN-LOCAL refs (as the muxcon registration layer
    receives them on the wire); the proxy stores them GLOBALLY qualified as
    "<origin>::<ref>" (outlet federation), mirroring `RemotePortProxy`.
    ``states`` is keyed by the same global form.
    """

    def __init__(self, name, feeds=(), states=None, origin_id="peerO", sessions=None):
        self.name = name
        self.remote_port_name = name
        self.power = [remote_ref(origin_id, r) for r in feeds]
        self._feed_states = {remote_ref(origin_id, k): v for k, v in (states or {}).items()}
        self.metadata = _FakeRemoteMeta(origin_id)
        self.description = f"Remote {name}"
        self._client_sessions = dict(sessions or {})  # open federated sessions (relay anchor)


def _remote_pm(feeds=("rack9.1", "rack9.2"), states=None, origin_id="peerO", local_ports=None, sessions=None):
    ports = dict(local_ports or {})
    ports["remote1"] = _FakeRemotePort("remote1", feeds, states, origin_id, sessions)
    return _FakePortManager(ports)


@asyncio_test
async def test_feed_states_remote_uses_cached_states():
    pm = _remote_pm(feeds=("rack9.1", "rack9.2"), states={"rack9.1": True, "rack9.2": None})
    adapter = await _start(_make_adapter(pm))
    # Both the declared feeds and the cached POWER:STATE are keyed by the
    # GLOBAL "origin::<ref>" form, so the read is a plain passthrough
    # (outlet federation).
    assert adapter.feed_states("remote1") == {"peerO::rack9.1": True, "peerO::rack9.2": None}
    # A local port still reads the live local readings.
    pm.ports["c1"] = _FakePort("c1", ["rack1.1", "rack1.2"])
    assert adapter.feed_states("c1") == {"rack1.1": True, "rack1.2": True}
    # A remote port with no cached state for a feed: unknown.
    assert adapter.feed_states("remote1")["peerO::rack9.2"] is None
    # The raw origin-LOCAL form is not a feed here (only the global form is).
    assert "rack9.1" not in adapter.feed_states("remote1")
    await adapter.stop()


@asyncio_test
async def test_feed_states_unknown_port_empty():
    adapter = await _start(_make_adapter())
    assert adapter.feed_states("missing") == {}
    await adapter.stop()


@asyncio_test
async def test_port_power_map_works_for_remote_port():
    pm = _remote_pm(feeds=("rack9.1", "rack9.2"))
    adapter = await _start(_make_adapter(pm))
    # The mapping returns the GLOBALLY qualified refs (outlet federation).
    assert adapter.port_power_map("remote1") == ["peerO::rack9.1", "peerO::rack9.2"]
    await adapter.stop()


@asyncio_test
async def test_port_power_payload_remote_states():
    pm = _remote_pm(feeds=("rack9.1", "rack9.2"), states={"rack9.1": True, "rack9.2": True})
    adapter = await _start(_make_adapter(pm))
    payload = adapter.port_power_payload("remote1")
    assert payload["state"] == "all"
    assert payload["feeds_on"] == 2
    assert payload["all_power_lost"] is False
    # Remote feeds carry the global ref and never local watts.
    assert all(f["watts"] is None for f in payload["feeds"])
    assert {f["ref"] for f in payload["feeds"]} == {"peerO::rack9.1", "peerO::rack9.2"}
    # Simulate the origin turning one feed off (POWER:STATE applied on the
    # peer, which caches by the global ref).
    pm.ports["remote1"]._feed_states["peerO::rack9.2"] = False
    payload = adapter.port_power_payload("remote1")
    assert payload["state"] == "some"
    assert payload["feeds_on"] == 1
    pm.ports["remote1"]._feed_states["peerO::rack9.1"] = False
    payload = adapter.port_power_payload("remote1")
    assert payload["state"] == "none"
    assert payload["all_power_lost"] is True
    # All states unknown -> "unknown".
    pm.ports["remote1"]._feed_states = {"peerO::rack9.1": None, "peerO::rack9.2": None}
    payload = adapter.port_power_payload("remote1")
    assert payload["state"] == "unknown"
    await adapter.stop()


@asyncio_test
async def test_remote_origin_classification():
    pm = _remote_pm(feeds=("rack9.1", "rack9.2"), origin_id="peerO")
    adapter = await _start(_make_adapter(pm))
    # A globally qualified fed ref -> owned by that origin (direct, no scan).
    assert adapter._remote_origin_for_ref("peerO::rack9.1") == "peerO"
    # A bare ref only on the federated port (defensive: unprefixed feed) ->
    # owned by that origin via the registry scan.
    pm.ports["remote1"].power = ["rack9.1"]
    pm.ports["remote1"]._feed_states = {"rack9.1": True}
    assert adapter._remote_origin_for_ref("rack9.1") == "peerO"
    # A local PDU's ref -> never "remote" (local wins).
    assert adapter._remote_origin_for_ref("rack1.1") is None
    # A ref on no port -> unknown (None), which set_outlet turns into its own
    # "not configured" error path.
    assert adapter._remote_origin_for_ref("ghost.9") is None
    # A foreign origin's qualified ref resolves to its CLAIMED origin; the
    # relay then refuses ("unreachable") when this node has no link to it.
    assert adapter._remote_origin_for_ref("other::rack9.1") == "other"
    await adapter.stop()


@asyncio_test
async def test_remote_origin_local_wins_when_local_port_declares_ref():
    # A local port declaring the same BARE ref as a federated port: the ref
    # is local, so no origin is reported (no false "federated" refusal).
    pm = _remote_pm(feeds=("rack1.3",), local_ports={"c1": _FakePort("c1", ["rack1.3"])})
    adapter = await _start(_make_adapter(pm))
    assert adapter._remote_origin_for_ref("rack1.3") is None
    res = await adapter.set_outlet("rack1.3", False)
    assert res["ok"] is True  # switched locally, not refused
    await adapter.stop()


@asyncio_test
async def test_set_outlet_remote_named_like_local_relays_not_local():
    # The collision regression: the local node AND the origin both have an
    # outlet named rack1.1. The port declares the fed outlet as the global
    # "peerO::rack1.1" ref, so switching THAT relays to the origin; the bare
    # "rack1.1" ref is the local outlet. The same name on both nodes never
    # shadows (outlet federation).

    class _FakeMuxcon:
        def __init__(self, reply):
            self.reply = reply
            self.calls = []

        def get_adapter_type(self):
            return "muxcon"

        async def relay_power_switch(self, port_name, ref, on, claims, client_id=None):
            self.calls.append((port_name, ref, on, list(claims), client_id))
            return dict(self.reply)

    pm = _remote_pm(
        feeds=("rack1.1",),
        states={"rack1.1": True},
        origin_id="peerO",
        local_ports={"c1": _FakePort("c1", ["rack1.1"])},
        sessions={"u1": 3},
    )
    adapter = await _start(_make_adapter(pm))
    mx = _FakeMuxcon({"ok": True, "on": False})
    pm.unified_adapters = [adapter, mx]
    # The GLOBAL ref relays to the origin (the local PDU is NOT touched)...
    res = await adapter.set_outlet("peerO::rack1.1", False, client_id="u1")
    assert res["ok"] is True
    # ...with the ORIGIN-LOCAL ref on the wire; claims are port names.
    assert mx.calls == [("remote1", "rack1.1", False, ["remote1"], "u1")]
    # ...and the local PDU still reports on.
    assert adapter.pdus["rack1"].readings["1"].on is True
    # The BARE ref is the local outlet: it switches locally, no relay.
    res = await adapter.set_outlet("rack1.1", False, client_id="u1")
    assert res["ok"] is True
    assert adapter.pdus["rack1"].readings["1"].on is False
    assert mx.calls == [("remote1", "rack1.1", False, ["remote1"], "u1")]
    await adapter.stop()


@asyncio_test
async def test_set_outlet_relays_remote_owned_ref_to_origin():
    class _FakeMuxcon:
        def __init__(self, reply):
            self.reply = reply
            self.calls = []

        def get_adapter_type(self):
            return "muxcon"

        async def relay_power_switch(self, port_name, ref, on, claims, client_id=None):
            self.calls.append((port_name, ref, on, list(claims), client_id))
            return dict(self.reply)

    # Success: the origin's reading comes back, watts stay None (no PDU here).
    pm = _remote_pm(feeds=("rack9.1", "rack9.2"), states={"rack9.1": True}, origin_id="peerO", local_ports=None)
    pm.ports["remote1"]._client_sessions = {"u1": 3}  # an open federated session (anchor)
    adapter = await _start(_make_adapter(pm))
    mx = _FakeMuxcon({"ok": True, "on": False})
    pm.unified_adapters = [adapter, mx]
    # The caller passes the GLOBAL ref; the wire carries the origin-local one,
    # and claims are the PLAIN port names this node declares the ref on.
    res = await adapter.set_outlet("peerO::rack9.1", False, client_id="u1")
    assert res["ok"] is True
    assert res["reading"]["on"] is False
    assert mx.calls == [("remote1", "rack9.1", False, ["remote1"], "u1")]
    await adapter.stop()

    # Error propagation: the origin's typed refusal is surfaced verbatim.
    pm2 = _remote_pm(feeds=("rack9.1",), origin_id="peerO")
    pm2.ports["remote1"]._client_sessions = {"u1": 4}  # anchor session present (relay reached)
    adapter2 = await _start(_make_adapter(pm2))
    mx2 = _FakeMuxcon({"ok": False, "error": "console session is not read-write; switch it from a read-write session"})
    pm2.unified_adapters = [adapter2, mx2]
    res = await adapter2.set_outlet("peerO::rack9.1", True, client_id="u1")
    assert res["ok"] is False
    assert "not read-write" in res["error"]
    assert res["impact"] == {"change": "on", "losing_power": [], "staying_up": []}
    await adapter2.stop()


@asyncio_test
async def test_set_outlet_relays_dotted_fqdn_origin_ref():
    # The dotted-FQDN regression: an origin whose server_id is a hostname
    # ("openmux.borge.nu") makes the global ref "openmux.borge.nu::rack1.1".
    # That ref must be recognized as remote (not a malformed local ref) and
    # relayed with the ORIGIN-LOCAL ref on the wire, and "owned by federated
    # node <fqdn>" refusals must name the dotted origin.
    class _FakeMuxcon:
        def __init__(self, reply):
            self.reply = reply
            self.calls = []

        def get_adapter_type(self):
            return "muxcon"

        async def relay_power_switch(self, port_name, ref, on, claims, client_id=None):
            self.calls.append((port_name, ref, on, list(claims), client_id))
            return dict(self.reply)

    origin_id = "openmux.borge.nu"
    pm = _remote_pm(feeds=("rack1.1",), states={"rack1.1": True}, origin_id=origin_id)
    pm.ports["remote1"]._client_sessions = {"u1": 3}
    adapter = await _start(_make_adapter(pm))
    mx = _FakeMuxcon({"ok": True, "on": False})
    pm.unified_adapters = [adapter, mx]
    res = await adapter.set_outlet(f"{origin_id}::rack1.1", False, client_id="u1")
    assert res["ok"] is True
    assert res["reading"]["on"] is False
    # ORIGIN-LOCAL ref on the wire, claims are port names.
    assert mx.calls == [("remote1", "rack1.1", False, ["remote1"], "u1")]
    await adapter.stop()

    # No session: the typed refusal names the dotted origin (no "invalid
    # outlet ref" leak).
    pm2 = _remote_pm(feeds=("rack1.1",), origin_id=origin_id)
    adapter2 = await _start(_make_adapter(pm2))
    res = await adapter2.set_outlet(f"{origin_id}::rack1.1", False)
    assert res["ok"] is False
    assert f"owned by federated node {origin_id}" in res["error"]
    await adapter2.stop()


@asyncio_test
async def test_set_outlet_remote_ref_without_federated_session_refused():
    # No console session anchored: the relay cannot pick a stream, so the
    # typed refusal (naming the origin) is returned. This is the path the
    # web REST Power page hits for remote refs (it has no session).
    pm = _remote_pm(feeds=("rack9.1",), origin_id="peerO")
    adapter = await _start(_make_adapter(pm))
    for direction in (False, True):
        res = await adapter.set_outlet("peerO::rack9.1", direction)
        assert res["ok"] is False
        assert "owned by federated node peerO" in res["error"]
    await adapter.stop()


@asyncio_test
async def test_set_outlet_remote_ref_without_muxcon_relays_nothing():
    adapter = await _start(_make_adapter(_remote_pm(feeds=("rack9.1",), origin_id="peerO")))
    # Give the anchor a live stream so the only missing piece is the muxcon
    # adapter itself.
    adapter.main_port_manager.ports["remote1"]._client_sessions = {"u1": 7}
    res = await adapter.set_outlet("peerO::rack9.1", False, client_id="u1")
    assert res["ok"] is False
    assert "no active federation link" in res["error"]
    await adapter.stop()


@asyncio_test
async def test_snapshot_skips_remote_refs_in_unresolved():
    # The remote port declares refs to the ORIGIN's PDUs (globally
    # qualified); locally they resolve to nothing, but they are not
    # "unresolved" noise (the origin answers for them). A genuinely broken
    # LOCAL ref is still surfaced.
    pm = _remote_pm(feeds=("rack9.1",), local_ports={"c1": _FakePort("c1", ["ghost.7"])})
    adapter = await _start(_make_adapter(pm))
    snap = adapter.get_power_snapshot()
    assert "peerO::rack9.1" not in snap["unresolved_refs"]
    assert snap["unresolved_refs"] == ["ghost.7"]
    await adapter.stop()


# --- global ref helpers (outlet federation) -----------------------------------


def test_split_remote_ref_round_trip_and_reject_local_refs():
    assert remote_ref("peerO", "rack1.1") == "peerO::rack1.1"
    assert split_remote_ref("peerO::rack1.1") == ("peerO", "rack1.1")
    # Local bare refs are not remote refs.
    assert split_remote_ref("rack1.1") is None
    assert split_remote_ref("rack1.2") is None
    # Server ids are often dotted FQDNs; a dotted origin is a valid origin
    # (the first "::" is the separator, the local half must still be
    # "<pdu>.<id>").
    assert split_remote_ref("a.b::rack1.1") == ("a.b", "rack1.1")
    assert remote_ref("openmux.borge.nu", "rack1.1") == "openmux.borge.nu::rack1.1"
    assert split_remote_ref("openmux.borge.nu::rack1.1") == ("openmux.borge.nu", "rack1.1")
    # The local half must still be a well-formed "<pdu>.<id>": extra/missing
    # dots, dot-free tokens, or a nested "::" in the origin are rejected.
    assert split_remote_ref("openmux.borge.nu::rack1.1.9") is None
    assert split_remote_ref("openmux.borge.nu::nodots") is None
    assert split_remote_ref("a::b::rack1.1") is None
    # Empty origin / malformed pdu part are rejected too.
    assert split_remote_ref("::rack1.1") is None
    assert split_remote_ref("peerO::nodots") is None
    assert split_remote_ref(None) is None
    assert split_remote_ref(42) is None
