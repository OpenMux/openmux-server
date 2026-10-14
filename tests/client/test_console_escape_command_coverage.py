"""Behavioral coverage for ConsoleUI._process_escape_command (46-bracket S1).

Pins the escape-command dispatcher line-for-line: every command branch's exact
stdout text and collaboration, the interactive input loops (`R`, `e`, `\\`)
including both stdin paths (sys.stdin and the raw _stdin_fd), every inner
error arm, and the outer catch-all.

Written (and green) against the PRE-refactor method.
"""

import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openmux.client.console import ConsoleUI


def _ui():
    """Real ConsoleUI with a mock connection; stdin disabled (fd None)."""
    conn = MagicMock()
    conn.close = AsyncMock()
    conn.reconnect = AsyncMock(return_value=True)
    conn.send_data = AsyncMock()
    conn.access_mode = None
    ui = ConsoleUI(conn)
    ui._stdin_fd = None
    return ui


async def _cmd(ui, command, monkeypatch, stdin_reads=None, os_reads=None, available_seq=None):
    """Drive one escape command to completion; return captured stdout.

    ``available_seq`` optionally scripts ``_is_data_available`` (e.g.
    [False, True, ...]) to exercise the "no data on this poll, wait and
    retry" path in the interactive input loops.
    """
    if available_seq is not None:
        avail = MagicMock(side_effect=list(available_seq))
    else:
        avail = MagicMock(return_value=True)
    with (
        patch(
            "openmux.client.console.sys.stdin",
            MagicMock(read=MagicMock(side_effect=list(stdin_reads) if stdin_reads is not None else [""])),
        ),
        patch.object(ui, "_is_data_available", avail),
    ):
        if os_reads is not None:
            reads = list(os_reads)
            monkeypatch.setattr("os.read", lambda fd, n: reads.pop(0))
        buf = io.StringIO()
        with patch("sys.stdout", new=buf):
            await ui._process_escape_command(command)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_dot_disconnects_and_stops_loop(monkeypatch):
    ui = _ui()
    out = await _cmd(ui, ".", monkeypatch)
    assert "\r\n[Disconnecting...]\r\n" in out
    ui.connection.close.assert_awaited_once()
    assert ui.is_running is False


@pytest.mark.asyncio
async def test_dot_close_error_is_swallowed(monkeypatch):
    ui = _ui()
    ui.connection.close = AsyncMock(side_effect=RuntimeError("close boom"))
    out = await _cmd(ui, ".", monkeypatch)
    assert "\r\n[Disconnecting...]\r\n" in out
    # the swallowed close error still stops the loop
    assert ui.is_running is False


@pytest.mark.asyncio
async def test_a_requests_read_write(monkeypatch):
    ui = _ui()
    monkeypatch.setattr(ui, "_request_access_mode", AsyncMock())
    await _cmd(ui, "a", monkeypatch)
    ui._request_access_mode.assert_awaited_once_with(
        "request_read_write", "[Read-write access is not supported by this connection]"
    )


@pytest.mark.asyncio
async def test_s_releases_read_write(monkeypatch):
    ui = _ui()
    monkeypatch.setattr(ui, "_request_access_mode", AsyncMock())
    await _cmd(ui, "s", monkeypatch)
    ui._request_access_mode.assert_awaited_once_with(
        "release_read_write", "[Release read-write is not supported by this connection]"
    )


@pytest.mark.asyncio
async def test_f_without_target_passes_none(monkeypatch):
    ui = _ui()
    monkeypatch.setattr(ui, "_read_take_target_input", AsyncMock(return_value=""))
    monkeypatch.setattr(ui, "_force_access_mode", AsyncMock())
    await _cmd(ui, "f", monkeypatch)
    ui._force_access_mode.assert_awaited_once_with(None)


@pytest.mark.asyncio
async def test_f_with_target_passes_id(monkeypatch):
    ui = _ui()
    monkeypatch.setattr(ui, "_read_take_target_input", AsyncMock(return_value="f4"))
    monkeypatch.setattr(ui, "_force_access_mode", AsyncMock())
    await _cmd(ui, "f", monkeypatch)
    ui._force_access_mode.assert_awaited_once_with("f4")


@pytest.mark.asyncio
async def test_i_shows_info(monkeypatch):
    ui = _ui()
    monkeypatch.setattr(ui, "_show_info", AsyncMock())
    await _cmd(ui, "i", monkeypatch)
    ui._show_info.assert_awaited_once()


@pytest.mark.asyncio
async def test_w_shows_users(monkeypatch):
    ui = _ui()
    monkeypatch.setattr(ui, "_show_users", AsyncMock())
    await _cmd(ui, "w", monkeypatch)
    ui._show_users.assert_awaited_once()


@pytest.mark.asyncio
async def test_v_prints_version(monkeypatch):
    ui = _ui()
    out = await _cmd(ui, "v", monkeypatch)
    assert "\r\n[OpenMux Client v1.0]\r\n" in out


@pytest.mark.asyncio
async def test_p_opens_power_menu(monkeypatch):
    ui = _ui()
    monkeypatch.setattr(ui, "_power_menu", AsyncMock())
    await _cmd(ui, "p", monkeypatch)
    ui._power_menu.assert_awaited_once()


@pytest.mark.asyncio
async def test_r_replay_placeholder_uses_replay_lines(monkeypatch):
    ui = _ui()
    ui.replay_lines = 7
    out = await _cmd(ui, "r", monkeypatch)
    assert "\r\n[Replay last 7 lines - not implemented]\r\n" in out


@pytest.mark.asyncio
async def test_R_sets_replay_lines(monkeypatch):
    ui = _ui()
    ui.replay_lines = 0
    out = await _cmd(ui, "R", monkeypatch, stdin_reads=["2", "5", "\r"])
    assert ui.replay_lines == 25
    # prompt, the echoed digits, and the confirmation line
    assert "[Set replay lines: 25]" in out and "[Replay lines set to 25]" in out


@pytest.mark.asyncio
async def test_R_cancel_on_empty_input(monkeypatch):
    ui = _ui()
    out = await _cmd(ui, "R", monkeypatch, stdin_reads=["\n"])
    assert ui.replay_lines == 20
    assert "[Cancelled]" in out


@pytest.mark.asyncio
async def test_R_invalid_number(monkeypatch):
    ui = _ui()
    import builtins

    real_int = int

    def boom(*args, **kwargs):
        if args and args[0] == "25":
            raise ValueError("forced")
        return real_int(*args, **kwargs)

    # restore BEFORE the test returns (monkeypatch teardown would run after
    # pytest's report phase, where the patched `int` breaks isinstance checks)
    builtins.int = boom
    try:
        out = await _cmd(ui, "R", monkeypatch, stdin_reads=["2", "5", "\r"])
    finally:
        builtins.int = real_int
    assert ui.replay_lines == 20
    assert "[Invalid number]" in out


@pytest.mark.asyncio
async def test_R_reads_via_raw_stdin_fd(monkeypatch):
    ui = _ui()
    ui._stdin_fd = 7
    out = await _cmd(ui, "R", monkeypatch, os_reads=[b"3", b"1", b"\r"])
    assert ui.replay_lines == 31
    assert "[Replay lines set to 31]" in out


@pytest.mark.asyncio
async def test_o_reconnect_success_notifies_and_resets_flag(monkeypatch):
    ui = _ui()
    ui._notified_disconnect = True
    out = await _cmd(ui, "o", monkeypatch)
    assert "[Reconnecting...]" in out and "[Reconnected]" in out
    ui.connection.reconnect.assert_awaited_once()
    assert ui._notified_disconnect is False


@pytest.mark.asyncio
async def test_o_reconnect_success_quiet_suppresses_messages(monkeypatch):
    ui = _ui()
    ui.quiet_mode = True
    ui._notified_disconnect = True
    out = await _cmd(ui, "o", monkeypatch)
    assert "[Reconnecting..." not in out and "[Reconnected]" not in out
    assert ui._notified_disconnect is False


@pytest.mark.asyncio
async def test_o_reconnect_failed_reports(monkeypatch):
    ui = _ui()
    ui.connection.reconnect = AsyncMock(return_value=False)
    ui._notified_disconnect = True
    out = await _cmd(ui, "o", monkeypatch)
    assert "[Reconnect failed]" in out
    # a failed reconnect leaves the disconnect-notice flag untouched
    assert ui._notified_disconnect is True


@pytest.mark.asyncio
async def test_o_reconnect_error_reports_failed(monkeypatch, tmp_path):
    ui = _ui()
    ui.connection.reconnect = AsyncMock(side_effect=RuntimeError("reconnect boom"))
    out = await _cmd(ui, "o", monkeypatch)
    assert "[Reconnect failed]" in out


@pytest.mark.asyncio
async def test_z_suspension_is_not_supported(monkeypatch):
    ui = _ui()
    out = await _cmd(ui, "z", monkeypatch)
    assert "\r\n[Suspend not supported - use . to disconnect]\r\n" in out


@pytest.mark.asyncio
async def test_e_changes_escape_sequence_printable(monkeypatch):
    ui = _ui()
    old1, old2 = ui.escape_char1, ui.escape_char2
    assert (old1, old2) == ("\x05", "c")
    out = await _cmd(ui, "e", monkeypatch, stdin_reads=["x", "y"])
    assert ui.escape_char1 == "x" and ui.escape_char2 == "y"
    assert ui.escape_b1 == b"x" and ui.escape_b2 == b"y"
    assert "[Escape sequence changed]" in out


@pytest.mark.asyncio
async def test_e_echoes_control_chars_as_caret(monkeypatch):
    ui = _ui()
    out = await _cmd(ui, "e", monkeypatch, stdin_reads=["\x01", "y"])
    assert "^A" in out
    assert ui.escape_char1 == "\x01" and ui.escape_b1 == b"\x01"


@pytest.mark.asyncio
async def test_e_reads_via_raw_stdin_fd(monkeypatch):
    ui = _ui()
    ui._stdin_fd = 7
    await _cmd(ui, "e", monkeypatch, os_reads=[b"f", b"9"])
    assert ui.escape_char1 == "f" and ui.escape_char2 == "9"


@pytest.mark.asyncio
async def test_e_error_while_reading(monkeypatch):
    ui = _ui()
    mock_stdin = MagicMock()
    mock_stdin.read.side_effect = RuntimeError("read boom")
    with (
        patch("openmux.client.console.sys.stdin", mock_stdin),
        patch.object(ui, "_is_data_available", return_value=True),
    ):
        buf = io.StringIO()
        with patch("sys.stdout", new=buf):
            await ui._process_escape_command("e")
    assert "[Error changing escape sequence]" in buf.getvalue()


@pytest.mark.asyncio
async def test_l_break_sequences_placeholder(monkeypatch):
    ui = _ui()
    out = await _cmd(ui, "l", monkeypatch)
    assert "\r\n[Break sequences - not implemented]\r\n" in out


@pytest.mark.asyncio
async def test_carriage_return_continues_quietly(monkeypatch):
    ui = _ui()
    out = await _cmd(ui, "\r", monkeypatch)
    assert out == ""
    ui.connection.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_newline_continues_quietly(monkeypatch):
    ui = _ui()
    out = await _cmd(ui, "\n", monkeypatch)
    assert out == ""


@pytest.mark.asyncio
async def test_ctrl_r_replay_last_line_placeholder(monkeypatch):
    ui = _ui()
    out = await _cmd(ui, "\x12", monkeypatch)
    assert "\r\n[Replay last line - not implemented]\r\n" in out


@pytest.mark.asyncio
async def test_question_shows_help(monkeypatch):
    ui = _ui()
    monkeypatch.setattr(ui, "_show_help", AsyncMock())
    await _cmd(ui, "?", monkeypatch)
    ui._show_help.assert_awaited_once()


@pytest.mark.asyncio
async def test_backslash_sends_octal_byte(monkeypatch):
    ui = _ui()
    ui.read_only_mode = False
    await _cmd(ui, "\\", monkeypatch, stdin_reads=["1", "0", "0"])
    ui.connection.send_data.assert_awaited_once_with(bytes([0o100]))


@pytest.mark.asyncio
async def test_backslash_read_only_does_not_send(monkeypatch):
    ui = _ui()
    ui.read_only_mode = True
    await _cmd(ui, "\\", monkeypatch, stdin_reads=["1", "0", "0"])
    ui.connection.send_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_backslash_reads_via_raw_stdin_fd(monkeypatch):
    ui = _ui()
    ui._stdin_fd = 7
    await _cmd(ui, "\\", monkeypatch, os_reads=[b"3", b"1", b"0"])
    ui.connection.send_data.assert_awaited_once_with(bytes([0o310]))


@pytest.mark.asyncio
async def test_backslash_invalid_octal(monkeypatch):
    ui = _ui()
    import builtins

    real_int = int

    def boom(value, base=10):
        if base == 8:
            raise ValueError("forced")
        return real_int(value, base)

    # restore int in the body (not via monkeypatch) so it is real again before
    # pytest's report phase, which breaks while `int` is patched
    builtins.int = boom
    try:
        out = await _cmd(ui, "\\", monkeypatch, stdin_reads=["1", "0", "0"])
    finally:
        builtins.int = real_int
    assert "[Invalid octal value]" in out
    ui.connection.send_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_command_reports_repr(monkeypatch):
    ui = _ui()
    out = await _cmd(ui, "q", monkeypatch)
    assert "\r\n[Unknown command: 'q']\r\n" in out


@pytest.mark.asyncio
async def test_outer_handler_error_reports_and_logs(monkeypatch, caplog):
    ui = _ui()
    monkeypatch.setattr(ui, "_request_access_mode", AsyncMock(side_effect=RuntimeError("dispatch boom")))
    out = await _cmd(ui, "a", monkeypatch)
    assert "\r\n[Error processing command: dispatch boom]\r\n" in out


@pytest.mark.asyncio
async def test_R_waits_when_no_data_then_reads(monkeypatch):
    ui = _ui()
    # first poll finds no data (loop waits, then re-polls); digits arrive on later polls
    await _cmd(ui, "R", monkeypatch, stdin_reads=["2", "5", "\r"], available_seq=[False, True, True, True])
    assert ui.replay_lines == 25


@pytest.mark.asyncio
async def test_e_waits_when_no_data_then_reads(monkeypatch):
    ui = _ui()
    await _cmd(ui, "e", monkeypatch, stdin_reads=["x", "y"], available_seq=[False, True, True])
    assert ui.escape_char1 == "x" and ui.escape_char2 == "y"


@pytest.mark.asyncio
async def test_backslash_waits_when_no_data_then_reads(monkeypatch):
    ui = _ui()
    await _cmd(ui, "\\", monkeypatch, stdin_reads=["1", "0", "0"], available_seq=[False, True, True, True])
    ui.connection.send_data.assert_awaited_once_with(bytes([0o100]))
