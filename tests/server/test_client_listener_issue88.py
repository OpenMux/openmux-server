"""Issue #88: server-side command-phase fixes for the client listener.

Covers the blank-line no-op (a bare Enter must never close a raw telnet
session), EOF still ending the command phase, the `detach` control frame
(attached and unattached), returning to the command phase after an explicit
detach, and the strict first-token POWER parser.

FakeReader/FakeWriter mirror tests/server/test_client_listener_adapter.py.
"""

import asyncio
import json
from typing import Any, Dict, List, Optional, cast

import pytest

from openmux.server.adapters import power_command as pc
from openmux.server.adapters.client_listener import CTRL_MARKER, ClientSession, TcpServerAdapter


class FakeReader:
    def __init__(self, chunks: Optional[List[bytes]] = None):
        self.chunks: List[bytes] = list(chunks or [])

    async def read(self, n: int) -> bytes:
        if not self.chunks:
            await asyncio.sleep(0)
            return b""
        data = self.chunks.pop(0)
        if n == 1 and len(data) > 1:
            self.chunks.insert(0, data[1:])
            return data[:1]
        return data


class FakeWriter:
    def __init__(self):
        self.buffer = bytearray()
        self.closed = False

    def get_extra_info(self, name: str):
        return ("127.0.0.1", 55555) if name == "peername" else None

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def is_closing(self) -> bool:
        return self.closed

    def write_eof(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


def make_line_chars(s: str) -> List[bytes]:
    return [c.encode() for c in (s + "\n")]


def make_adapter() -> TcpServerAdapter:
    return TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})


def make_client(adapter: TcpServerAdapter, reader: FakeWriter = None, writer: FakeWriter = None) -> ClientSession:
    return ClientSession(
        "c1", "127.0.0.1", cast(Any, reader or FakeReader([])), cast(Any, writer or FakeWriter()), adapter.logger
    )


def ctrl_frame(payload: Dict[str, Any]) -> bytes:
    return CTRL_MARKER + json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"


def last_ctrl_frame(writer: FakeWriter) -> Dict[str, Any]:
    for line in reversed(writer.buffer.split(b"\n")):
        if line.startswith(CTRL_MARKER):
            return json.loads(line[len(CTRL_MARKER) :])
    raise AssertionError("no control frame written")


class _PM:
    async def get_port_list_with_federation(self):
        return [{"name": "p1"}]


def _cm_with_ports():
    return type("CM", (), {"port_manager": _PM()})()


# --- blank line / EOF in the command phase --------------------------------------


@pytest.mark.asyncio
async def test_blank_line_does_not_end_command_phase():
    adapter = make_adapter()
    adapter.console_manager = _cm_with_ports()
    reader = FakeReader(make_line_chars("") + make_line_chars("LIST") + make_line_chars("QUIT"))
    writer = FakeWriter()
    client = make_client(adapter, reader, writer)

    await adapter.handle_client_protocol(client)
    out = writer.buffer.decode()
    # Before the fix the blank line broke the phase and LIST never ran.
    assert "AUTH:SUCCESS" in out
    assert "LIST:{" in out
    assert "ERROR:Unknown command" not in out


@pytest.mark.asyncio
async def test_whitespace_only_line_is_also_a_noop():
    adapter = make_adapter()
    adapter.console_manager = _cm_with_ports()
    reader = FakeReader(make_line_chars("   ") + make_line_chars("LIST") + make_line_chars("QUIT"))
    writer = FakeWriter()
    client = make_client(adapter, reader, writer)

    await adapter.handle_client_protocol(client)
    assert "LIST:{" in writer.buffer.decode()


@pytest.mark.asyncio
async def test_eof_ends_command_phase_without_output():
    adapter = make_adapter()
    writer = FakeWriter()
    client = make_client(adapter, FakeReader([]), writer)
    await adapter.handle_command_phase(client)
    assert writer.buffer == bytearray()
    assert client.connected_port is None


# --- detach control frame ---------------------------------------------------------


@pytest.mark.asyncio
async def test_detach_unattached_refuses_with_existing_error_line():
    adapter = make_adapter()
    writer = FakeWriter()
    client = make_client(adapter, FakeReader([]), writer)

    handled = await adapter._handle_control_frame(client, ctrl_frame({"type": "detach"}))
    assert handled is True
    assert b"ERROR:DISCONNECT:No port connected" in writer.buffer
    ack = last_ctrl_frame(writer)
    assert ack == {"type": "detached", "ok": False, "port": None}
    # A refused detach must not mark the session for command-phase re-entry.
    assert client.detach_requested is False


@pytest.mark.asyncio
async def test_detach_attached_replies_line_and_ack_and_flags_reentry():
    adapter = make_adapter()
    writer = FakeWriter()
    client = make_client(adapter, FakeReader([]), writer)
    client.connected_port = "portA"
    client.mode = "read-only"

    handled = await adapter._handle_control_frame(client, ctrl_frame({"type": "detach"}))
    assert handled is True
    assert b"DISCONNECTED:portA" in bytes(writer.buffer)
    ack = last_ctrl_frame(writer)
    assert ack == {"type": "detached", "ok": True, "port": "portA"}
    assert client.connected_port is None
    assert client.mode is None
    assert client.detach_requested is True


@pytest.mark.asyncio
async def test_detach_without_port_keeps_protocol_loop_ending_session():
    # destroy_port-style detach (no detach_requested) must still end the session:
    # the protocol loop only re-enters the command phase after an explicit detach.
    adapter = make_adapter()
    client = make_client(adapter)
    calls: List[str] = []

    async def cmd_phase(c: ClientSession) -> None:
        calls.append("cmd")
        if len(calls) == 1:
            c.connected_port = "portA"
        else:
            pytest.fail("command phase must not re-enter after a plain port detach")

    async def char_mode(c: ClientSession) -> None:
        calls.append("char")
        c.connected_port = None  # as disconnect_client_from_port does

    adapter.handle_command_phase = cmd_phase  # type: ignore[method-assign]
    adapter.handle_character_mode = char_mode  # type: ignore[method-assign]
    await adapter.handle_client_protocol(client)
    assert calls == ["cmd", "char"]


@pytest.mark.asyncio
async def test_detach_returns_session_to_command_phase():
    adapter = make_adapter()
    client = make_client(adapter)
    calls: List[str] = []

    async def cmd_phase(c: ClientSession) -> None:
        calls.append("cmd")
        if len([x for x in calls if x == "cmd"]) == 1:
            c.connected_port = "portA"  # as CONNECT does
        else:
            c.connected = False  # as QUIT does on the second visit

    async def char_mode(c: ClientSession) -> None:
        calls.append("char")
        c.detach_requested = True  # as the detach control frame does
        c.connected_port = None

    adapter.handle_command_phase = cmd_phase  # type: ignore[method-assign]
    adapter.handle_character_mode = char_mode  # type: ignore[method-assign]
    await adapter.handle_client_protocol(client)
    assert calls == ["cmd", "char", "cmd"]
    assert client.detach_requested is False  # consumed by the loop


class _EchoPM:
    """Port manager stub recording every write_to_port payload."""

    def __init__(self):
        self.writes: List[bytes] = []

    async def write_to_port(self, port_name: str, data: bytes, client_id: str) -> bool:
        self.writes.append(data)
        return True


class _FwCM:
    """Console manager stub: recording port manager + detach path support."""

    def __init__(self):
        self.port_manager = _EchoPM()

    async def disconnect_client_from_port(self, client_id: str, port_name: str) -> None:
        pass


@pytest.mark.asyncio
async def test_detach_frame_coalesced_behind_keystrokes_still_detach():
    # TCP can merge the Enter bytes typed just before Ctrl+E c d with the
    # detach frame into one read chunk; the frame must still be handled and
    # only the keystrokes may reach the port.
    adapter = make_adapter()
    adapter.console_manager = _FwCM()
    writer = FakeWriter()
    client = make_client(adapter, FakeReader([]), writer)
    client.connected_port = "portA"

    chunk = b"\r\r" + ctrl_frame({"type": "detach"})
    await adapter.forward_bytes_to_port(client, chunk)

    assert adapter.console_manager.port_manager.writes == [b"\r\r"]
    assert client.detach_requested is True
    assert last_ctrl_frame(writer) == {"type": "detached", "ok": True, "port": "portA"}


@pytest.mark.asyncio
async def test_detach_frame_completed_by_next_chunk():
    adapter = make_adapter()
    adapter.console_manager = _FwCM()
    writer = FakeWriter()
    client = make_client(adapter, FakeReader([]), writer)
    client.connected_port = "portA"

    whole = b"ab" + ctrl_frame({"type": "detach"}) + b"cd"
    cut = len(b"ab") + len(ctrl_frame({"type": "detach"})) - 6  # split inside the JSON body
    await adapter.forward_bytes_to_port(client, whole[:cut])
    assert adapter.console_manager.port_manager.writes == [b"ab"]
    assert client.detach_requested is False

    await adapter.forward_bytes_to_port(client, whole[cut:])
    assert client.detach_requested is True
    assert adapter.console_manager.port_manager.writes[-1] == b"cd"


@pytest.mark.asyncio
async def test_plain_data_forwards_untouched_by_framing():
    adapter = make_adapter()
    adapter.console_manager = _FwCM()
    writer = FakeWriter()
    client = make_client(adapter, FakeReader([]), writer)
    client.connected_port = "portA"

    await adapter.forward_bytes_to_port(client, b"hello\x00\x01world")
    assert adapter.console_manager.port_manager.writes == [b"hello\x00\x01world"]


# --- structured POWER parser -------------------------------------------------------


def test_power_usage_string_is_verbatim():
    assert pc.POWER_USAGE == "ERROR:POWER: usage: POWER [<pdu>[.<outlet>] [on|off]]"


@pytest.mark.parametrize(
    "command,expected",
    [
        ("POWER", (None, None, None)),
        ("POWER rack1", ("rack1", None, None)),
        ("POWER rack1.1 ON", ("rack1.1", "on", None)),
        ("POWER   peerO::rack9.1   OFF  ", ("peerO::rack9.1", "off", None)),
    ],
)
def test_parse_power_command_valid_forms(command, expected):
    assert pc.parse_power_command(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "",
        "power rack1",  # lowercase token: strict first token is POWER
        "POWERSXYZ",  # the old split() treated this as a bare list
        "PWR",
        "POWER a b c",  # too many tokens
    ],
)
def test_parse_power_command_rejects_bad_shape(command):
    arg, verb, error = pc.parse_power_command(command)
    assert (arg, verb) == (None, None)
    assert error == pc.POWER_USAGE


@pytest.mark.asyncio
async def test_run_power_command_bad_first_token_sends_usage(monkeypatch):
    monkeypatch.setattr(pc, "find_power_adapter", lambda cm: type("P", (), {"enabled": True})())
    sent: List[str] = []

    async def send_line(line: str) -> None:
        sent.append(line)

    ok = await pc.run_power_command(None, "POWERSXYZ rack1", send_line, "alice", None)
    assert ok is False
    assert sent == [pc.POWER_USAGE]
