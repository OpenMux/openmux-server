"""COMMAND DRIVER: a PDU driven by real commands, spawned as a real child process (CMD-01 to CMD-06).

``driver: command`` reads and switches a power device by running a program: one batch read for the whole PDU, and
one command per switch. ``tests/test_power_command_driver.py`` covers that code with ``asyncio.create_subprocess_exec``
monkeypatched, so no unit test ever starts a process. Every scenario here starts one: the device is
``tests/e2e/power_fixtures/outlet_box.py``, a checked-in script that keeps its outlet states in a JSON file. The
tier therefore proves the parts a patched spawn cannot: the argv built from the config, the substituted
``{outlet_id}``, the exit code and stderr shown to a user, and what a killed process group leaves behind.

Six measured facts shape these bodies, and each is used where it applies:

* The device decides what the server reports. A fresh box seeds every outlet off, so the first read reports
  ``on: false`` with ``watts``, ``amps``, ``volts`` and ``total_watts`` all null. The ``dummy`` driver starts every
  outlet on and reports 120 W at 230 V, which is why ``test_power.py`` cannot show this pair.
* A listed feed carries no wattage when the device reports none: ``POWER box.1 off  -> con1``, where the dummy
  driver line reads ``POWER rack1.1 on 120W 230V  -> solo, dual``.
* A successful switch logs nothing of its own. The only server-side trace is the audit line ``POWER CONTROL: user
  <name> turned <ref> off; losing all power: <port>``.
* A notice reaches a console only when its feed changes. Switching an outlet that already reports that state
  changes nothing and says nothing, so a scenario sets the starting state first.
* A device-wide read failure is silent by contract (``power_drivers/readbackoff.py``): the driver does no further
  device IO, the adapter keeps the last readings, ``online`` stays true, and no log line appears. A switch is never
  backed off, so an explicit switch still reaches the device and the reported state follows it.
* A command that fails is reported plainly. The CLI and REST both carry the device's own words — ``power switch
  failed (exit 3) (stderr: outlet box: relay refused to move)`` — a refused switch leaves the feed on the state it
  last reported, and a command that never answers reports ``power switch timed out after 1s`` and leaves no child
  process behind.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence

import pytest

from .story import step, surfaces

pytestmark = [pytest.mark.e2e, pytest.mark.feature]

PDU = "box"
#: One feed that powers a console, one that powers nothing: the first is what a notice and an impact are about.
FEED = "box.1"
SPARE = "box.2"
PORT = "con1"

#: What a console is pushed when the switch takes its last feed away, and the milder line for any other change.
NOTICE_ALL_LOST = f"[POWER WARNING] all power feeds are now OFF for this console ({FEED})"
NOTICE_FEED_ON = f"[POWER] feed {FEED} is now on"

#: The way the fake device refuses a switch, as measured against ``power_fixtures/outlet_box.py``. The exit code
# reaches the user inside the driver's message, so a scenario quotes both halves.
REFUSED_SWITCH = "power switch failed (exit 3) (stderr: outlet box: relay refused to move)"


def _command_world(
    servers: Any,
    name: str,
    *,
    outlets: Sequence[str] = ("1", "2"),
    feeds: Optional[Dict[str, Sequence[str]]] = None,
    poll_interval: float = 1,
    box_args: Sequence[str] = (),
    timeout: Optional[float] = None,
) -> Any:
    """One server whose only PDU is driven by the checked-in fake device, plus the consoles it feeds.

    ``feeds`` maps a Port name to the outlet refs it declares, and defaults to the one console that ``box.1``
    powers. ``box_args`` and ``timeout`` go to :meth:`ConfigBuilder.with_command_pdu`, so a scenario can ship a
    device that answers too late or not at all.

    The Telnet listener is off: these scenarios read the product log for power lines, and a second listener adds
    its own start-up warnings to the lines a scenario has to search.
    """

    def configure(builder: Any) -> Any:
        builder.with_command_pdu(
            PDU, [str(outlet) for outlet in outlets], poll_interval=poll_interval, box_args=box_args, timeout=timeout
        )
        for port_name, refs in (feeds if feeds is not None else {PORT: [FEED]}).items():
            builder.with_loopback(port_name, write_slot_capacity="multiple", feeds=list(refs))
        return builder

    return servers(name=name, loopback=(), telnet=False, configure=configure, start=True)


def _outlet(snapshot: Dict[str, Any], ref: str) -> Dict[str, Any]:
    """One outlet entry of a ``/api/power`` snapshot, found by its ref."""
    for pdu in snapshot.get("pdus") or []:
        for entry in pdu.get("outlets") or []:
            if entry.get("ref") == ref:
                return dict(entry)
    seen = [str(entry.get("ref")) for pdu in snapshot.get("pdus") or [] for entry in pdu.get("outlets") or []]
    raise AssertionError(f"{ref} is not in the power snapshot; refs present: {seen}")


def _device(world: Any, pdu: str = PDU) -> Dict[str, Any]:
    """What the fake device holds right now, read from the device's own state file."""
    path = world.power_box(pdu)
    if not path.exists():
        raise AssertionError(f"{pdu} has no device state file at {path}: the device was never reached")
    return json.loads(path.read_text(encoding="utf-8"))


def _journal(world: Any, pdu: str = PDU) -> List[str]:
    """The switches the fake device was told to run, one line each, oldest first."""
    path = world.power_box_journal(pdu)
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _pid_alive(pid: Optional[int]) -> bool:
    """Whether one process still exists, asked with signal 0."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --- what a real device reports ----------------------------------------------------------------------


@surfaces("http", "cli:none")
def test_cmd01_the_device_answers_so_the_readings_are_its_own(servers, clients, api_for_server, story):
    """A command PDU reports what the device holds, and reports no power figure the device never gave."""
    world = _command_world(servers, "cmd01")
    api = api_for_server(world)

    with step("the config runs the checked-in device, with one command per read and per switch"):
        config = world.config_text("server.yaml")
        assert "driver: command" in config, config
        assert str(world.power_box(PDU)) in config, "the read and switch commands name the device's own state file"
        assert "state_cmd" in config and "on_cmd" in config and "off_cmd" in config, config

    with step("the first read reports the device's positions, which are all off"):
        world.wait_until(lambda: _outlet(api.power(), FEED)["online"] is True, timeout=20, what="the first device read")
        snapshot = api.power()
        pdu = snapshot["pdus"][0]
        assert pdu["driver"] == "command"
        assert pdu["online"] is True, "a device that answered is online, whatever the config said"
        assert [entry["ref"] for entry in pdu["outlets"]] == [FEED, SPARE]
        assert pdu["outlets_on"] == 0, "the box seeds every outlet off, so the server cannot claim an outlet is on"
        for entry in pdu["outlets"]:
            assert entry["on"] is False, entry
            assert entry["watts"] is None, "this device measures no power, so nothing is invented for it"
            assert entry["amps"] is None and entry["volts"] is None, entry
        assert pdu["total_watts"] is None, "a PDU of outlets without a wattage has no total either"

    with step("the device itself agrees, and has run no switch yet"):
        assert _device(world)["outlets"] == {"1": False, "2": False}
        assert _journal(world) == []

    with step("a listed feed says so without a wattage pair, which the dummy driver always prints"):
        cli = clients.cli(user="admin", server=world, surface="cli:none")
        cli.command("POWER")
        cli.wait_for(f"POWER {FEED} off  -> {PORT}", timeout=20)
        row = next(line for line in cli.read_text().splitlines() if line.strip().startswith(f"POWER {FEED} "))
        assert row.strip() == f"POWER {FEED} off  -> {PORT}", row
        # The dummy row at the same position reads `POWER rack1.1 on 120W 230V  -> solo, dual`: the watts and
        # volts are the device's to report, and this one reports none.
        assert not re.search(r"\d\s*W", row), row
        assert "V " not in row.split("->")[0], row


@surfaces("cli:admin")
def test_cmd02_a_switch_from_the_cli_runs_the_command_the_config_named(servers, clients, api_for_server, story):
    """One ``POWER`` line becomes a real process whose argv carries the outlet it was told to move."""
    world = _command_world(servers, "cmd02")
    api = api_for_server(world)
    cli = clients.cli(user="admin", server=world, surface="cli:admin")
    watcher = clients.cli(user="admin", server=world, surface="cli:watch")
    watcher.attach(port=PORT)

    on_mark = watcher.offset()
    with step("switching the feed on runs one command for that outlet, and the console hears about it"):
        cli.command(f"POWER {FEED} on")
        cli.wait_for(f"POWER {FEED} -> on", timeout=20)
        assert _outlet(api.power(), FEED)["on"] is True
        assert _device(world)["outlets"]["1"] is True, "the switch reached the device rather than the server's cache"
        assert _journal(world) == ["on 1"], "the device recorded the outlet id the config substituted"
        watcher.wait_for(NOTICE_FEED_ON, timeout=20)

    off_mark = watcher.offset()
    with step("switching it back off warns who loses power, and the audit line names that console"):
        cli.command(f"POWER {FEED} off")
        cli.wait_for(f"WARNING:POWER: removing all power to: {PORT}", timeout=20)
        cli.wait_for(f"POWER {FEED} -> off", timeout=20)
        line = world.wait_for_log(f"POWER CONTROL: user admin turned {FEED} off", timeout=15)
        assert f"losing all power: {PORT}" in line, line
        assert _device(world)["outlets"]["1"] is False
        assert _journal(world) == ["on 1", "off 1"]

    with step("and each console was told about the move it saw, in its own words"):
        assert NOTICE_FEED_ON in watcher.reply_after(on_mark), watcher.reply_after(on_mark)
        world.wait_until(
            lambda: NOTICE_ALL_LOST in watcher.reply_after(off_mark), timeout=20, what="the console to be told it lost power"
        )
    cli.close()
    watcher.close()


@surfaces("cli:none", "http")
def test_cmd03_a_change_made_on_the_device_is_picked_up_by_the_next_poll(servers, clients, api_for_server, story):
    """Someone throws the switch by hand: the poll finds it, and no switch command was ever run."""
    world = _command_world(servers, "cmd03")
    api = api_for_server(world)
    watcher = clients.cli(user="admin", server=world, surface="cli:none")
    watcher.attach(port=PORT)
    with step("the device answers once, so the change below is a change"):
        world.wait_until(lambda: _outlet(api.power(), FEED)["on"] is False, timeout=20, what="the first read")

    mark = watcher.offset()
    with step("the device changes with no command from the server"):
        world.power_box_flip(PDU, "1")
        assert _device(world)["outlets"]["1"] is True, "the box holds the new position before the server knows"

    with step("the poll reports it, on this Port and on the API"):
        watcher.wait_for(NOTICE_FEED_ON, timeout=20)
        world.wait_until(lambda: _outlet(api.power(), FEED)["on"] is True, timeout=20, what="the API to catch up")

    with step("and nothing was switched, because nothing was asked of the server"):
        assert _journal(world) == [], "the device changed by itself, so it recorded no switch"
        assert NOTICE_FEED_ON in watcher.reply_after(mark), watcher.reply_after(mark)
    watcher.close()


# --- when the device says no -----------------------------------------------------------------------


@surfaces("cli:none", "http")
def test_cmd04_a_device_that_refuses_the_switch_says_so_on_every_surface(servers, clients, api_for_server, story):
    """A relay that refuses to move reports its exit code and its own words, and keeps its position."""
    world = _command_world(servers, "cmd04")
    api = api_for_server(world)
    cli = clients.cli(user="admin", server=world, surface="cli:none")
    with step("the device answers and takes the switch first, so the refusal is about the attempt and not the direction"):
        world.wait_until(lambda: _outlet(api.power(), FEED)["on"] is False, timeout=20, what="the first read")
        cli.command(f"POWER {FEED} on")
        cli.wait_for(f"POWER {FEED} -> on", timeout=20)
        assert _device(world)["outlets"]["1"] is True

    world.power_box_fault(PDU, "switch")
    mark_log = len(world.log_lines())
    with step("the CLI answer carries the exit code and the device's own stderr"):
        cli.command(f"POWER {FEED} off")
        cli.wait_for(f"ERROR:POWER: {REFUSED_SWITCH}", timeout=20)
        assert f"POWER {FEED} -> off" not in cli.read_text(), "a refused switch never claims it worked"

    with step("the product log names the outlet and the failure"):
        line = world.wait_for_log(f"set_state failed for {FEED}", timeout=15, since=mark_log)
        assert REFUSED_SWITCH in line, line

    with step("REST refuses with the same message and still names the console at risk"):
        status, _, text = api.request("POST", f"/api/power/outlets/{FEED}", {"on": False}, expect=(200, 409))
        assert status == 409, text
        assert REFUSED_SWITCH in text, text
        assert PORT in text, "the refusal keeps the impact it calculated before trying"

    with step("the device kept the position it had, and recorded nothing for the attempt"):
        assert _device(world)["outlets"]["1"] is True, "a refused switch must not leave the server's belief behind"
        assert _journal(world) == ["on 1"], _journal(world)
        assert _outlet(api.power(), FEED)["on"] is True, "the report stays the device's, not the wished-for one"
    cli.close()


@surfaces("cli:none", "http")
def test_cmd05_a_command_that_never_answers_times_out_without_an_orphan(servers, clients, api_for_server, story):
    """A device that answers too late is stopped inside its timeout, including the helper it started."""
    world = _command_world(servers, "cmd05", box_args=("--slow", "8", "--child"), timeout=1.0)
    api = api_for_server(world)
    cli = clients.cli(user="admin", server=world, surface="cli:none")

    with step("the outlet stays unknown, because the device never answered once"):
        outlet = _outlet(api.power(), FEED)
        assert outlet["on"] is None, outlet
        assert outlet["online"] is None, outlet

    with step("the CLI says it timed out, in seconds the config chose"):
        cli.command(f"POWER {FEED} on")
        cli.wait_for("ERROR:POWER: power switch timed out after 1s", timeout=25)

    with step("REST says the same thing"):
        status, _, text = api.request("POST", f"/api/power/outlets/{FEED}", {"on": True}, expect=(200, 409))
        assert status == 409, text
        assert "timed out after 1s" in text, text

    with step("the process the device started is gone, and the device wrote nothing"):
        child = world.power_box_child(PDU)
        world.wait_until(lambda: child.exists(), timeout=20, what="the device to record the pid it started")
        pid = int(child.read_text(encoding="utf-8").strip())
        world.wait_until(lambda: not _pid_alive(pid), timeout=20, what="the timed-out command's helper to be stopped")
        assert not world.power_box(PDU).exists(), "the command died before it reached the device's state file"


@surfaces("http", "cli:none")
def test_cmd06_a_device_that_stops_answering_keeps_its_last_readings(servers, clients, api_for_server, story):
    """A dead sense line makes the server quiet and stale on purpose, while a switch still gets through."""
    world = _command_world(servers, "cmd06")
    api = api_for_server(world)
    cli = clients.cli(user="admin", server=world, surface="cli:none")
    watcher = clients.cli(user="admin", server=world, surface="cli:watch")
    watcher.attach(port=PORT)
    with step("the device answers once, so the server holds a real reading to keep"):
        world.wait_until(lambda: _outlet(api.power(), FEED)["on"] is False, timeout=20, what="the first read")

    world.power_box_fault(PDU, "state")
    mark = watcher.offset()
    mark_log = len(world.log_lines())
    with step("the device changes while it is unreadable, so the two disagree on purpose"):
        world.power_box_flip(PDU, "1")
        world.wait_until(lambda: _device(world)["outlets"]["1"] is True, timeout=20, what="the device to hold the change")
        report = _outlet(api.power(), FEED)
        assert report["on"] is False, "the last reading is kept, because an unknown device is not a new position"
        assert report["online"] is True, "the backoff does not declare the PDU offline: only a raising read does"

    with step("nothing is said, because the contract of the read backoff is silence"):
        assert watcher.reply_after(mark) == "", watcher.reply_after(mark)
        lines = [line for line in world.log_lines()[mark_log:] if "openmux.adapter.power" in line]
        assert lines == [], f"a suppressed read must not log once per poll: {lines[:3]}"

    with step("a switch is never backed off, so it reaches the device and the report follows it"):
        cli.command(f"POWER {FEED} on")
        cli.wait_for(f"POWER {FEED} -> on", timeout=25)
        assert _journal(world) == ["on 1"], _journal(world)
        world.wait_until(lambda: _outlet(api.power(), FEED)["on"] is True, timeout=20, what="the report to follow the switch")
        watcher.wait_for(NOTICE_FEED_ON, timeout=20)
    cli.close()
    watcher.close()
