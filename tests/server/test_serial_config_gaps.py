"""Serial adapter tests for config gaps (issue #105).

Two facts drive these tests:

* A port the adapter cannot build never exists, so rebooting the server does not create it.
  Before issue #105 one such entry ended the whole serial adapter at boot and, on reload,
  reappeared as ``added`` on every reload.
* A reload must not tear down a running port before it knows the new config is buildable.

Ports here are real ``SerialPortWrapper`` objects. Construction is pure (it opens no device), so
the tests stay fast and still run the code that decides whether a port can exist.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from openmux.server.adapters.serial import SerialAdapter

GOOD = {"name": "ser0", "device": "/tmp/openmux-gap-absent-tty", "baudrate": 9600, "parity": "N"}
BAD_PARITY = {"name": "ser1", "device": "/tmp/openmux-gap-absent-tty", "baudrate": 9600, "parity": "X"}
NO_DEVICE = {"name": "ser2", "baudrate": 9600}


def _adapter(*ports: Dict[str, Any]) -> SerialAdapter:
    """Build a serial adapter from port entries, without starting it."""
    adapter = SerialAdapter("gapser", {"serial_ports": [dict(p) for p in ports]})
    adapter.port_manager = None
    adapter.main_port_manager = None
    return adapter


async def _shutdown(*adapters: SerialAdapter) -> None:
    """Stop every port object an adapter holds, so no reconnect task outlives the test."""
    for adapter in adapters:
        for port in list(adapter.serial_ports.values()):
            try:
                await port.stop()
            except Exception:
                # justification: test cleanup; a port that will not stop is not what these assert on
                pass


def test_boot_builds_the_good_port_and_skips_one_with_a_bad_parity():
    adapter = SerialAdapter("gapser", {"serial_ports": [dict(GOOD), dict(BAD_PARITY)]})
    adapter.port_manager = None

    assert "ser0" in adapter.serial_ports
    assert "ser1" not in adapter.serial_ports


def test_boot_records_why_each_skipped_port_is_missing():
    adapter = SerialAdapter("gapser", {"serial_ports": [dict(GOOD), dict(BAD_PARITY), dict(NO_DEVICE)]})
    adapter.port_manager = None

    assert sorted(adapter.config_gaps) == ["ser1", "ser2"]
    assert "parity" in adapter.config_gaps["ser1"].lower()
    assert "device" in adapter.config_gaps["ser2"].lower()


@pytest.mark.asyncio
async def test_a_refused_change_keeps_the_running_port_and_says_so():
    adapter = _adapter(GOOD)
    running = adapter.serial_ports["ser0"]
    changed = dict(GOOD, parity="X")

    summary = await adapter.reconcile_ports([changed])
    try:
        assert adapter.serial_ports["ser0"] is running
        assert summary["failed"] == ["ser0"]
        assert summary["kept_previous"] == ["ser0"]
        assert "ser0" not in summary["updated"]
        assert summary["failed_detail"]["ser0"]["kept_previous"] is True
        assert adapter.config_gaps_kept == ["ser0"]
    finally:
        await _shutdown(adapter)


@pytest.mark.asyncio
async def test_a_refused_new_port_is_reported_as_failed_and_never_as_added():
    adapter = _adapter(GOOD)

    first = await adapter.reconcile_ports([GOOD, BAD_PARITY])
    second = await adapter.reconcile_ports([GOOD, BAD_PARITY])
    try:
        assert first.get("added", []) == []
        assert first["failed"] == ["ser1"]
        assert first.get("kept_previous", []) == []
        # The same bad entry must not be announced as a new port on the next reload (#105).
        assert second.get("added", []) == []
        assert second["failed"] == ["ser1"]
    finally:
        await _shutdown(adapter)


@pytest.mark.asyncio
async def test_the_snapshot_keeps_only_the_ports_that_exist():
    adapter = _adapter(GOOD)

    await adapter.reconcile_ports([GOOD, BAD_PARITY])
    try:
        names = sorted(entry["name"] for entry in adapter.config["serial_ports"])
        assert names == ["ser0"]
    finally:
        await _shutdown(adapter)


@pytest.mark.asyncio
async def test_a_port_that_really_appears_is_still_listed_as_added(monkeypatch):
    async def _no_io_start(self) -> bool:  # no device exists; construction is what is under test
        return True

    monkeypatch.setattr("openmux.server.adapters.serial.SerialPortWrapper.start", _no_io_start)
    adapter = _adapter(GOOD)

    summary = await adapter.reconcile_ports([GOOD, dict(GOOD, name="ser9", device="/tmp/openmux-gap-other-tty")])
    try:
        assert summary["added"] == ["ser9"]
        assert summary["unchanged"] == ["ser0"]
        assert "failed" not in summary
        assert "ser9" in adapter.serial_ports
    finally:
        await _shutdown(adapter)


@pytest.mark.asyncio
async def test_a_port_the_adapter_refuses_to_start_is_reported_as_failed(monkeypatch):
    created: List[str] = []

    async def _refuse(self, port_name: str, config: Dict[str, Any]):
        created.append(port_name)
        return None

    monkeypatch.setattr(SerialAdapter, "create_port", _refuse)
    adapter = _adapter(GOOD)

    summary = await adapter.reconcile_ports([GOOD, dict(GOOD, name="ser9", device="/tmp/openmux-gap-other-tty")])
    try:
        assert created == ["ser9"]
        assert summary["failed"] == ["ser9"]
        assert summary.get("added", []) == []
        assert "could not be created" in summary["failed_detail"]["ser9"]["error"]
    finally:
        await _shutdown(adapter)


@pytest.mark.asyncio
async def test_a_reload_that_applies_everything_reports_no_failure_keys():
    adapter = _adapter(GOOD)

    summary = await adapter.reconcile_ports([GOOD])
    try:
        assert summary == {"added": [], "removed": [], "updated": [], "unchanged": ["ser0"]}
    finally:
        await _shutdown(adapter)


@pytest.mark.asyncio
async def test_a_removed_port_is_not_reported_as_failed():
    adapter = _adapter(GOOD)

    summary = await adapter.reconcile_ports([])

    assert summary["removed"] == ["ser0"]
    assert "failed" not in summary
    assert adapter.serial_ports == {}
