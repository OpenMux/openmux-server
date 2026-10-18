"""Issue #88: interactive `mux> ` command mode for the CLI client.

Covers the line editor (echo, backspace, Ctrl+C, Enter), the command
vocabulary (local help, wire-form normalization, local refusal of unknown
lines), command-mode read rendering (LIST rows, the attach transition, the
quiet-stream re-prompt), and the escape `d` detach. The help text is pinned
against docs/design/cli_command_mode.md so code and doc cannot drift.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openmux.client.console import COMMAND_MODE_HELP_LINES, COMMAND_PROMPT, NOT_ATTACHED_NOTICE, ConsoleUI


class FakeStdout:
    """Captures text writes and raw byte echo into one ordered log.

    The console writes notices through ``write`` (str) and raw echo bytes
    through ``buffer.write`` (bytes); keeping both in one chunk list preserves
    the real interleaved order for assertions.
    """

    def __init__(self):
        self.chunks = []
        outer = self

        class _Buffer:
            def write(self, b):
                outer.chunks.append(bytes(b))

            def flush(self):
                pass

        self.buffer = _Buffer()

    def write(self, s):
        self.chunks.append(s)

    def flush(self):
        pass

    @property
    def out(self):
        blob = bytearray()
        for c in self.chunks:
            blob += c.encode("utf-8") if isinstance(c, str) else c
        return blob.decode("utf-8", errors="replace")


def _cm_ui(**conn_attrs):
    """ConsoleUI with command mode on, over a capable MagicMock connection."""
    conn = MagicMock()
    conn.supports_command_phase = True
    conn.is_connected = True
    conn.is_authenticated = True
    conn.access_mode = None
    conn.current_port = None
    conn.close = AsyncMock()
    conn.send_data = AsyncMock(return_value=True)
    conn.detach_port = AsyncMock(return_value=True)
    conn.last_detach_reply = None
    for key, value in conn_attrs.items():
        setattr(conn, key, value)
    ui = ConsoleUI(conn, command_mode=True)
    ui._stdin_fd = None
    return ui


async def _keys(ui, data: bytes) -> str:
    """Feed raw keystrokes to the command-mode line editor; return shown output."""
    fake = FakeStdout()
    with patch("sys.stdout", fake):
        for i in range(len(data)):
            await ui._command_key_byte(data[i : i + 1])
    return fake.out


def _render(ui, data):
    """Feed one read payload through the render dispatch; return (rc, output)."""
    fake = FakeStdout()
    with patch("sys.stdout", fake):
        rc = ui._read_render(data)
    return rc, fake.out


def _ui_plain():
    """ConsoleUI over a MagicMock connection, command mode left at its default."""
    conn = MagicMock()
    conn.close = AsyncMock()
    conn.send_data = AsyncMock()
    conn.access_mode = None
    return ConsoleUI(conn)


# --- mode gating -------------------------------------------------------------


def test_prompt_constant():
    assert COMMAND_PROMPT == "mux> "


def test_command_mode_requires_opt_in_and_capability():
    capable = MagicMock()
    capable.supports_command_phase = True
    # Opt-in without capability (WebSocket adapter): off.
    incapable = MagicMock()
    incapable.supports_command_phase = False
    assert ConsoleUI(incapable, command_mode=True).command_mode is False
    # Capability without opt-in (attached-port runs, existing callers): off.
    assert ConsoleUI(capable).command_mode is False
    # Both: on.
    assert ConsoleUI(capable, command_mode=True).command_mode is True


def test_default_construction_has_no_command_mode():
    assert _ui_plain().command_mode is False


# --- help text drift gate -----------------------------------------------------


def test_help_text_matches_design_doc():
    doc = (Path(__file__).resolve().parents[2] / "docs" / "design" / "cli_command_mode.md").read_text(encoding="utf-8")
    block = doc[doc.index("<!-- BEGIN HELP -->") : doc.index("<!-- END HELP -->")]
    body = block.split("```text", 1)[1].split("```", 1)[0]
    doc_lines = body.strip("\n").split("\n")
    assert doc_lines == COMMAND_MODE_HELP_LINES


# --- line editor ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_typing_echoes_edits_and_submits():
    ui = _cm_ui()
    out = await _keys(ui, b"lix\x7fst\r")  # type "lix", erase the x, "st", Enter
    assert "li" in out
    assert "\b \b" in out
    ui.connection.send_data.assert_awaited_once_with("LIST\n")
    assert ui._cmd_awaiting_reply is True
    assert ui._cmd_quiet_polls == 0
    assert ui._cmd_line == bytearray()


@pytest.mark.asyncio
async def test_blank_line_reprompts_with_zero_bytes():
    ui = _cm_ui()
    out = await _keys(ui, b"\r")
    ui.connection.send_data.assert_not_awaited()
    assert out.endswith(COMMAND_PROMPT)


@pytest.mark.asyncio
async def test_ctrl_c_cancels_line_without_sending():
    ui = _cm_ui()
    out = await _keys(ui, b"ab\x03\r")
    ui.connection.send_data.assert_not_awaited()
    assert "\r\n" + COMMAND_PROMPT in out


@pytest.mark.asyncio
async def test_backspace_on_empty_line_is_noop():
    ui = _cm_ui()
    await _keys(ui, b"\x7f\x08")
    assert ui._cmd_line == bytearray()


@pytest.mark.asyncio
async def test_control_bytes_are_dropped():
    ui = _cm_ui()
    await _keys(ui, b"\x01list\x07")
    assert bytes(ui._cmd_line) == b"list"


# --- command vocabulary ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "keys,expected",
    [
        (b"list\r", "LIST\n"),
        (b"L\r", "LIST\n"),
        (b"c p1\r", "CONNECT:p1\n"),
        (b"connect p1\r", "CONNECT:p1\n"),
        (b"CONNECT:p2\r", "CONNECT:p2\n"),
        (b"  CoNNect  x1  \r", "CONNECT:x1\n"),
        (b"power rack1 on\r", "POWER rack1 on\n"),
        (b"POWER rack1.1 ON\r", "POWER rack1.1 ON\n"),
        (b"POWER\r", "POWER\n"),
    ],
)
async def test_commands_forward_expected_wire_line(keys, expected):
    ui = _cm_ui()
    await _keys(ui, keys)
    ui.connection.send_data.assert_awaited_once_with(expected)


@pytest.mark.asyncio
async def test_help_is_local_and_prints_the_table():
    ui = _cm_ui()
    out = await _keys(ui, b"?\r")
    ui.connection.send_data.assert_not_awaited()
    for line in COMMAND_MODE_HELP_LINES:
        assert line in out
    assert out.endswith(COMMAND_PROMPT)


@pytest.mark.asyncio
async def test_unknown_command_refused_locally():
    ui = _cm_ui()
    out = await _keys(ui, b"frobnicate\r")
    ui.connection.send_data.assert_not_awaited()
    assert '[Unknown command: "frobnicate" — type "help"]' in out
    assert out.endswith(COMMAND_PROMPT)


@pytest.mark.asyncio
async def test_connect_without_target_prints_usage():
    ui = _cm_ui()
    out = await _keys(ui, b"connect\r")
    ui.connection.send_data.assert_not_awaited()
    assert "[Usage: connect <port>]" in out
    assert '[Run "list" to see ports]' in out


@pytest.mark.asyncio
async def test_disconnect_while_unattached_is_informational():
    ui = _cm_ui()
    out = await _keys(ui, b"disconnect\r")
    ui.connection.send_data.assert_not_awaited()
    assert NOT_ATTACHED_NOTICE in out


@pytest.mark.asyncio
async def test_quit_ends_session_like_escape_dot():
    ui = _cm_ui()
    await _keys(ui, b"q\r")
    ui.connection.close.assert_awaited_once()
    assert ui.is_running is False
    ui.connection.send_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_failure_notices_without_arming_reply_wait():
    ui = _cm_ui(send_data=AsyncMock(return_value=False))
    out = await _keys(ui, b"list\r")
    assert "[Send failed - connection down]" in out
    assert ui._cmd_awaiting_reply is False


# --- read-side rendering ----------------------------------------------------------


def test_list_payload_renders_one_row_per_port():
    ui = _cm_ui()
    payload = b'LIST:{"ports":[{"name":"p1","description":"Console 1"},{"name":"p2"}]}\n'
    rc, out = _render(ui, payload)
    assert rc is False
    assert "p1  -  Console 1" in out
    assert "p2" in out
    assert ui.command_mode is True  # LIST does not change mode


def test_list_payload_unparseable_falls_back_to_raw_line():
    ui = _cm_ui()
    rc, out = _render(ui, b"LIST:{oops\n")
    assert rc is False
    assert "LIST:{oops" in out


def test_connected_line_switches_to_stream_mode():
    ui = _cm_ui()
    rc, out = _render(ui, b"CONNECTED:p3:READ_ONLY\r\n")
    assert rc is False
    assert ui.command_mode is False
    assert "[Connected to p3 (read-only)]" in out
    assert ui.connection.current_port == "p3"
    assert ui.connection.access_mode == "read-only"
    assert ui.read_only_mode is True


def test_connected_read_write_no_warning():
    ui = _cm_ui()
    rc, out = _render(ui, b"CONNECTED:p3:READ_WRITE\n")
    assert ui.command_mode is False
    assert "[Connected to p3 (read-write)]" in out
    assert ui.connection.access_mode == "read-write"


def test_bytes_after_connected_line_are_port_data():
    ui = _cm_ui()
    rc, out = _render(ui, b"CONNECTED:p3:READ_WRITE\nhe\r\nllo")
    assert rc is False
    # The normal renderer normalizes one line end to CRLF and passes the rest through.
    assert out.endswith("he\r\nllo")


def test_partial_line_is_held_until_newline():
    ui = _cm_ui()
    rc, out = _render(ui, b"AUTH:SUCC")
    assert rc is False
    assert out == ""
    assert ui._cmd_rx == b"AUTH:SUCC"
    rc, out = _render(ui, b"ESS:ok\n")
    assert "AUTH:SUCCESS:ok" in out


def test_shutdown_in_command_mode_stops():
    ui = _cm_ui()
    rc, out = _render(ui, b"SERVER:SHUTDOWN\n")
    assert rc is True
    assert ui.is_running is False
    assert "[Server shutdown]" in out


# --- quiet-stream re-prompt --------------------------------------------------------


def test_reply_settle_prompts_after_three_quiet_polls():
    ui = _cm_ui()
    ui._cmd_awaiting_reply = True
    prompts = []
    for _ in range(3):
        fake = FakeStdout()
        with patch("sys.stdout", fake):
            ui._command_reply_settle()
        prompts.append(fake.out)
    assert prompts[0] == ""
    assert prompts[1] == ""
    assert prompts[2] == COMMAND_PROMPT
    assert ui._cmd_awaiting_reply is False


def test_reply_settle_ignores_when_not_waiting():
    ui = _cm_ui()
    fake = FakeStdout()
    with patch("sys.stdout", fake):
        ui._command_reply_settle()
    assert fake.out == ""


def test_data_received_resets_quiet_counter():
    ui = _cm_ui()
    ui._cmd_awaiting_reply = True
    ui._cmd_quiet_polls = 2
    _render(ui, b"ERROR:Unknown command: LIST\n")
    # Rendering does not settle; the read step resets the counter on payload.
    assert ui._cmd_awaiting_reply is True


# --- escape `d` detach ---------------------------------------------------------------


async def _detach(ui) -> str:
    fake = FakeStdout()
    with patch("sys.stdout", fake):
        await ui._esc_cmd_detach_port()
    return fake.out


@pytest.mark.asyncio
async def test_detach_success_enters_command_mode():
    ui = _cm_ui()
    ui.command_mode = False  # attached sessions run in stream mode
    conn = ui.connection
    conn.current_port = "portA"
    conn.access_mode = "read-write"

    def _ack(_=None):
        conn.last_detach_reply = {"type": "detached", "ok": True, "port": "portA"}
        return True

    conn.detach_port = AsyncMock(side_effect=_ack)
    out = await _detach(ui)
    conn.detach_port.assert_awaited_once()
    assert "[Detached from portA]" in out
    assert conn.current_port is None
    assert conn.access_mode is None
    assert ui.command_mode is True
    assert out.endswith(COMMAND_PROMPT)


@pytest.mark.asyncio
async def test_detach_when_not_attached_is_informational():
    ui = _cm_ui()
    out = await _detach(ui)
    assert NOT_ATTACHED_NOTICE in out
    ui.connection.detach_port.assert_not_awaited()


@pytest.mark.asyncio
async def test_detach_refused_keeps_attach_state():
    ui = _cm_ui()
    ui.command_mode = False
    conn = ui.connection
    conn.current_port = "portA"

    def _ack(_=None):
        conn.last_detach_reply = {"type": "detached", "ok": False, "port": None}
        return True

    conn.detach_port = AsyncMock(side_effect=_ack)
    out = await _detach(ui)
    assert "[Detach refused by server]" in out
    assert conn.current_port == "portA"
    assert ui.command_mode is False


@pytest.mark.asyncio
async def test_detach_unsupported_adapter():
    ui = _cm_ui()
    conn = ui.connection
    conn.current_port = "portA"
    conn.detach_port = AsyncMock(return_value=False)
    out = await _detach(ui)
    assert "[Detach not supported on this connection]" in out


@pytest.mark.asyncio
async def test_detach_wait_gives_up_when_connection_drops():
    ui = _cm_ui()
    conn = ui.connection
    conn.current_port = "portA"
    conn.is_connected = False  # drops between send and poll
    out = await _detach(ui)
    assert "[Detach timed out]" in out


def test_escape_menu_registers_detach_handler():
    ui = _cm_ui()
    assert ui._esc_cmd_handlers()["d"] == ui._esc_cmd_detach_port
