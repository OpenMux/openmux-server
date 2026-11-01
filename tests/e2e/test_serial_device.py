"""SERIAL DEVICE: a Port wired to a real pty pair, with the test as the device (SER-01 to SER-06).

The Serial adapter reads and drives a character device. Its unit tests in ``tests/server/`` replace
``serial_asyncio.open_serial_connection`` or the port object, so no unit test ever opens a device, and
none of them can say what the device sees. Every scenario here opens one: ``SerialStation`` runs
``socat`` over a pty pair, the server opens one link as its ``device``, and the test holds the other,
so the tier proves the bytes on the wire, the termios on the device, and what an operator is told when
the cable comes out.

Six measured facts shape these bodies, and each is used where it applies:

* Nothing is added and nothing is rewritten. A console that types ``boot\\x1b[A\\x03 ... end\\r`` is
  read by the device as those same bytes, UTF-8 included, and the console is shown nothing of its own
  typing: a Serial Port forwards, it does not echo.
* The line settings land on the device. Before the server opens the link the pty carries ``socat``'s
  own line (9600, no parity, one stop bit, ``ONLCR`` set); after the server opens it with
  ``baudrate: 57600``, ``parity: "E"`` and ``stopbits: 2`` the device carries exactly that, with
  ``CLOCAL`` raised and ``ONLCR`` cleared.
* An absent device never stops the server. The Port is listed, ``state`` stays ``active``,
  ``connected`` is false, and ``status_message`` reads ``Serial device <path> not found`` beside one
  logged warning ``Serial device <path> does not exist``. A console may still attach and is answered
  with ``[Write blocked: port is disconnected on server]``.
* The Port recovers by itself. It retries every five seconds (``SerialPortWrapper.reconnect_delay``),
  so a device that appears, or a cable that goes back in, needs no reload, and the console that kept
  its seat is told ``[Reconnected]`` and reaches the device again.
* A device pulled mid-session says so on every surface: the console gets ``[Port disconnected on
  server]``, the API carries ``Read error on <path>: device reports readiness to read but returned no
  data``, and the log adds ``Port <name> is not connected`` plus ``WRITE FAILED`` for the next write.
* Bytes are neither lost nor reordered. 4,096 bytes of every value arrive at the console whole and in
  order, and 2,000 bytes typed in one action arrive at the device whole, where the reads come up as
  three chunks.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from .story import step, surfaces

pytestmark = [pytest.mark.e2e, pytest.mark.feature]

#: The one Serial Port most scenarios need.
PORT = "con1"

#: The three lines a console is pushed about its Port's link. Measured against the product, not written
#: from its source: the wording is what a viewer reads.
NOTICE_PORT_DOWN = "[Port disconnected on server]"
NOTICE_WRITE_BLOCKED = "[Write blocked: port is disconnected on server]"
NOTICE_RECONNECTED = "[Reconnected]"

#: Every byte value, sixteen times over: one block that cannot be ordered correctly by accident.
BLOCK = bytes(range(256)) * 16


def _serial_world(
    servers: Any,
    device: Any,
    name: str,
    *,
    port: str = PORT,
    baudrate: int = 9600,
    parity: str = "N",
    stopbits: float = 1,
    flow_control: str = "none",
) -> Any:
    """One server whose only Port is a Serial Port on ``device``'s near link.

    The Telnet listener is off: these scenarios read the product log for adapter lines, and a second
    listener adds lines a scenario then has to search past.
    """

    def configure(builder: Any) -> Any:
        builder.with_serial_port(
            port, str(device.path), baudrate=baudrate, parity=parity, stopbits=stopbits, flow_control=flow_control
        )
        return builder

    return servers(name=name, loopback=(), telnet=False, configure=configure, start=True)


def _port_entry(api: Any, name: str) -> Dict[str, Any]:
    """One Port's ``/api/ports`` entry, so a scenario reads that Port and not the whole world."""
    payload = api.json("/api/ports")
    ports = payload.get("ports") if isinstance(payload, dict) else payload
    for entry in ports or []:
        if entry.get("name") == name:
            return dict(entry)
    names = [str(entry.get("name")) for entry in ports or []]
    raise AssertionError(f"{name} is not listed by /api/ports; ports present: {names}")


def _wait_connected(world: Any, api: Any, name: str, timeout: float = 30.0) -> None:
    """Wait for the Port's own reconnect to reach the device.

    The adapter waits five seconds between attempts, so this is a poll and never a fixed sleep: the
    five seconds is the adapter's, and a scenario must not assume the attempt lands on the same second.
    """
    world.wait_until(lambda: _port_entry(api, name)["connected"] is True, timeout=timeout, what=f"{name} to connect")


# --- what the device reads, and what it carries -------------------------------------------------------


@surfaces("cli:admin", "serial:device")
def test_ser01_what_an_operator_types_is_what_the_device_reads(devices, servers, clients, story):
    """Typed bytes reach the device unchanged, with nothing echoed back to the console."""
    device = devices.serial("ser01")
    world = _serial_world(servers, device, "ser01")
    world.wait_for_log("Successfully connected to", timeout=25)
    cli = clients.cli(server=world, attach_to=PORT, surface="cli:admin")

    typed = "boot\x1b[A\x03 then \u03b1 end\r"
    with step("the device reads the control characters and the non-ASCII bytes exactly as typed"):
        cli.write(typed)
        wanted = typed.encode("utf-8")
        world.wait_until(lambda: device.received() == wanted, timeout=15, what="the device to read what was typed")
        # An arrow key is three bytes and Ctrl+C is one, and no sanitizer turns either into a token.
        assert device.received() == wanted, device.received()

    with step("the console is shown none of its own typing, because a Serial Port forwards rather than echoes"):
        cli.expect_no_text("boot", for_seconds=1.0)

    with step("what the device sends reaches the console unchanged"):
        device.send(b"device says hi\r\n")
        cli.wait_for("device says hi\r\n", timeout=15)


@surfaces("serial:device")
def test_ser02_the_speed_and_framing_in_the_config_are_the_ones_on_the_device(devices, servers, api_for_server, story):
    """The device itself carries the configured speed, parity, and stop bits."""
    device = devices.serial("ser02")
    # What socat's own line looks like before the server touches it: measured once here, so the
    # assertions below can tell the server's doing from the pty's defaults.
    before = device.line_settings()
    assert (before["speed"], before["parity"], before["stopbits"]) == (9600, "N", 1), before

    world = _serial_world(servers, device, "ser02", baudrate=57600, parity="E", stopbits=2)
    api = api_for_server(world)
    world.wait_for_log("Successfully connected to", timeout=25)

    with step("the listing states the settings, which is only the server's own opinion"):
        config = _port_entry(api, PORT)["serial_config"]
        assert (config["baudrate"], config["parity"], config["stopbits"]) == (57600, "E", 2), config
        assert config["device"] == str(device.path), config

    with step("the device carries them too, on its own termios"):
        settings = device.line_settings()
        assert settings["speed"] == 57600, settings
        assert settings["bytesize"] == 8, settings
        assert settings["parity"] == "E", settings
        assert settings["stopbits"] == 2, settings
        # pyserial raises CLOCAL and clears output post-processing when it opens a line. Clearing
        # ONLCR is what keeps a line feed the server forwards a line feed on the wire.
        assert settings["clocal"] is True and before["clocal"] is False, (before, settings)
        assert settings["onlcr"] is False and before["onlcr"] is True, (before, settings)


# --- a device that is not there, and one that comes back ----------------------------------------------


@surfaces("api:http", "cli:admin", "serial:device")
def test_ser03_a_port_whose_device_is_missing_reports_it_and_recovers_on_its_own(
    devices, servers, clients, api_for_server, story
):
    """A Port configured for a device no host has says why, and starts when the device arrives."""
    device = devices.serial("ser03", plugged_in=False)
    world = _serial_world(servers, device, "ser03")
    api = api_for_server(world)
    world.wait_for_log(f"Serial device {device.path} does not exist", timeout=25)

    with step("the Port is listed and states the reason, while the server stays up"):
        entry = _port_entry(api, PORT)
        assert entry["adapter"] == "Serial", entry
        assert entry["state"] == "active" and entry["is_running"] is True, entry
        assert entry["connected"] is False, entry
        assert entry["status_message"] == f"Serial device {device.path} not found", entry

    cli = clients.cli(server=world, attach_to=PORT, surface="cli:admin")
    with step("a console may attach to a down Port, and is told where its typing went"):
        assert cli.mode() == "read-write", cli.mode()
        assert _port_entry(api, PORT)["connected_clients"] == 1, _port_entry(api, PORT)
        cli.write("are you there\r")
        cli.wait_for(NOTICE_WRITE_BLOCKED, timeout=20)
        assert device.received() == b"", device.received()

    with step("the device arrives and the Port finds it, with no reload and no re-attach"):
        device.plug_back_in()
        _wait_connected(world, api, PORT)
        assert "status_message" not in _port_entry(api, PORT), _port_entry(api, PORT)
        cli.write("now then\r")
        world.wait_until(lambda: b"now then\r" in device.received(), timeout=20, what="the device to read the console")
        assert device.received() == b"now then\r", device.received()
        # The seat was granted while the device was down and carried across the connect, so the
        # console is told the link stands before its next byte is answered.
        cli.wait_for(NOTICE_RECONNECTED, timeout=20)


@surfaces("api:http", "cli:admin", "serial:device")
def test_ser04_a_pulled_cable_is_reported_where_the_operator_looks(devices, servers, clients, api_for_server, story):
    """Unplugging a live device is shown on the console, in the API, and in the log, and heals itself."""
    device = devices.serial("ser04")
    world = _serial_world(servers, device, "ser04")
    api = api_for_server(world)
    world.wait_for_log("Successfully connected to", timeout=25)
    cli = clients.cli(server=world, attach_to=PORT, surface="cli:admin")
    cli.write("before-pull\r")
    world.wait_until(lambda: device.received() == b"before-pull\r", timeout=15, what="the first exchange")
    device.clear()

    with step("the console is told the link went down, and the Port says why"):
        device.unplug()
        cli.wait_for(NOTICE_PORT_DOWN, timeout=20)
        entry = _port_entry(api, PORT)
        assert entry["connected"] is False, entry
        assert str(entry["status_message"]).startswith(f"Read error on {device.path}: device reports readiness to read"), entry
        world.wait_for_log(f"Error reading from {device.path}", timeout=20)

    with step("typing into a down Port is refused in words, and nothing reaches the device"):
        cli.write("during-outage\r")
        cli.wait_for(NOTICE_WRITE_BLOCKED, timeout=20)
        assert b"during-outage" not in device.received(), device.received()
        world.wait_for_log(f"WRITE FAILED: adapter reported write failure for port={PORT}", timeout=20)

    with step("the cable goes back in and the same console is working again"):
        device.plug_back_in()
        _wait_connected(world, api, PORT)
        cli.wait_for(NOTICE_RECONNECTED, timeout=20)
        cli.write("after-replug\r")
        world.wait_until(
            lambda: b"after-replug\r" in device.received(), timeout=20, what="the device to read the console again"
        )
        assert device.received() == b"after-replug\r", device.received()
        assert device.plugged_in is True


# --- byte fidelity ------------------------------------------------------------------------------------


@surfaces("cli:admin", "serial:device")
def test_ser05_no_byte_is_lost_or_reordered_in_either_direction(devices, servers, clients, story):
    """A block of every byte value crosses the server whole, in both directions."""
    device = devices.serial("ser05")
    world = _serial_world(servers, device, "ser05")
    world.wait_for_log("Successfully connected to", timeout=25)
    cli = clients.cli(server=world, attach_to=PORT, surface="cli:admin")

    with step("one 4,096 byte block from the device arrives whole and in order"):
        mark = cli.byte_offset()
        device.send(BLOCK)
        world.wait_until(
            lambda: len(cli.reply_bytes_after(mark)) >= len(BLOCK), timeout=25, what="the whole block to reach the console"
        )
        received = cli.reply_bytes_after(mark)
        # The first difference, if any, is the byte the reader wants rather than a count alone.
        first_difference = next(
            (index for index in range(min(len(received), len(BLOCK))) if received[index] != BLOCK[index]), None
        )
        assert received == BLOCK, f"{len(received)} of {len(BLOCK)} bytes arrived, first difference at {first_difference}"

    with step("2,000 bytes typed in one action reach the device whole, though the device reads them in pieces"):
        device.clear()
        cli.write("a" * 2000)
        world.wait_until(lambda: len(device.received()) >= 2000, timeout=25, what="the device to read every byte")
        assert device.received() == b"a" * 2000, len(device.received())


# --- the settings a unit test cannot reach ------------------------------------------------------------


@surfaces("cli:admin", "serial:device")
def test_ser06_flow_control_from_the_config_is_on_the_device(devices, servers, clients, story):
    """The kernel handshake a config asks for is the one the device carries."""
    ports = {"none": "plain", "rtscts": "hard", "xonxoff": "soft"}
    stations = {mode: devices.serial(f"ser06{mode}") for mode in ports}

    def configure(builder: Any) -> Any:
        for mode, station in stations.items():
            builder.with_serial_port(ports[mode], str(station.path), baudrate=38400, flow_control=mode)
        return builder

    world = servers(name="ser06", loopback=(), telnet=False, configure=configure, start=True)
    with step("every Port reaches its device, so a reading below is a connected line and not a half-open one"):
        world.wait_until(
            lambda: sum(1 for line in world.log_lines() if "Successfully connected to" in line) >= len(ports),
            timeout=30,
            what="all three Ports to connect",
        )

    with step("a Port that asks for no flow control leaves both kernel handshakes off"):
        plain = stations["none"].line_settings()
        assert (plain["rtscts"], plain["xonxoff"]) == (False, False), plain

    with step("flow_control rtscts hands the RTS pin to the kernel, which is what the config promises"):
        hard = stations["rtscts"].line_settings()
        assert hard["rtscts"] is True, hard

    with step("flow_control xonxoff turns the in-band handshake on"):
        soft = stations["xonxoff"].line_settings()
        assert soft["xonxoff"] is True, soft

    with step("a line under the hardware handshake still carries characters"):
        # CRTSCTS is only worth having if the port keeps working with it on: on a pty the CTS side is
        # always ready, so a stalled write here would be the flag applied wrongly rather than the device.
        cli = clients.cli(server=world, attach_to=ports["rtscts"])
        cli.write("handshake\r")
        world.wait_until(
            lambda: b"handshake\r" in stations["rtscts"].received(), timeout=20, what="the rtscts device to read the console"
        )
        assert stations["rtscts"].received() == b"handshake\r", stations["rtscts"].received()
