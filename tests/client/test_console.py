"""
Tests for the OpenMux client console UI
"""

import asyncio
import io
import os
import select
import sys
import termios
import tty
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openmux.client.adapters import BaseClientAdapter
from openmux.client.console import ConsoleUI


class TestConsoleUI:
    @pytest.fixture
    def mock_connection(self):
        """Create a mock adapter-style connection"""
        connection = AsyncMock(spec=BaseClientAdapter)
        connection.is_connected = True
        connection.is_authenticated = True
        return connection

    @pytest.fixture
    def console_ui(self, mock_connection):
        """Create a ConsoleUI instance with mocked connection"""
        return ConsoleUI(mock_connection)

    def test_init(self, console_ui, mock_connection):
        """Test initialization of ConsoleUI"""
        assert console_ui.connection == mock_connection
        assert console_ui.is_running is False
        assert console_ui.old_settings is None

    @pytest.mark.asyncio
    async def test_run_not_connected(self, console_ui, mock_connection):
        """Test run when not connected"""
        mock_connection.is_connected = False
        result = await console_ui.run()
        assert result is False

    @pytest.mark.asyncio
    async def test_run_not_authenticated(self, console_ui, mock_connection):
        """Test run when not authenticated"""
        mock_connection.is_authenticated = False
        result = await console_ui.run()
        assert result is False

    @pytest.mark.asyncio
    async def test_run_exception(self, console_ui, mock_connection):
        """Test run with exception"""
        mock_connection.is_connected = True
        mock_connection.is_authenticated = True

        # Mock _set_raw_mode to raise exception
        console_ui._set_raw_mode = MagicMock(side_effect=Exception("Test exception"))

        result = await console_ui.run()
        assert result is False

    @pytest.mark.asyncio
    @patch("openmux.client.console.tty")
    @patch("openmux.client.console.termios")
    @patch("asyncio.create_task")
    async def test_run_success(
        self,
        mock_create_task,
        mock_termios,
        mock_tty,
        console_ui,
        mock_connection,
    ):
        """Test successful run"""
        # Arrange
        tasks_created = []
        wrappers_created = []
        loop = asyncio.get_running_loop()

        def _create_task(coro):
            task = loop.create_task(coro)
            tasks_created.append(task)

            class _TaskWrapper:
                def __init__(self, t):
                    self._t = t
                    self.cancel = MagicMock(side_effect=self._t.cancel)

            wrapper = _TaskWrapper(task)
            wrappers_created.append(wrapper)
            return wrapper

        mock_create_task.side_effect = _create_task

        # Mock terminal methods
        mock_termios.tcgetattr.return_value = "old_settings"

        # Force keyboard handler to exit quickly
        async def keyboard_input_side_effect():
            console_ui.is_running = False

        console_ui._handle_keyboard_input = AsyncMock(side_effect=keyboard_input_side_effect)

        # Act
        result = await console_ui.run()

        # Assert
        assert result is True
        mock_termios.tcgetattr.assert_called_once()
        mock_tty.setraw.assert_called_once()
        mock_create_task.assert_called_once()
        assert len(tasks_created) == 1
        assert len(wrappers_created) == 1
        wrappers_created[0].cancel.assert_called_once()
        mock_termios.tcsetattr.assert_called_once()

    @pytest.mark.asyncio
    async def test_read_from_server_data(self, console_ui, mock_connection):
        """Test reading data from server"""
        # Setup
        console_ui.is_running = True

        # Mock connection read_data to return data once then None
        mock_connection.read_data.side_effect = [b"test data", None]

        # Mock sys.stdout.buffer methods instead of the attribute itself
        mock_write = MagicMock()
        mock_flush = MagicMock()

        original_buffer = sys.stdout.buffer
        original_write = sys.stdout.buffer.write
        original_flush = sys.stdout.buffer.flush

        try:
            # Replace methods temporarily
            sys.stdout.buffer.write = mock_write
            sys.stdout.buffer.flush = mock_flush

            # Call the method
            await console_ui._read_from_server()

            # Verify stdout was written to and flushed
            mock_write.assert_called_once_with(b"test data")
            mock_flush.assert_called_once()
        finally:
            # Restore original methods
            sys.stdout.buffer.write = original_write
            sys.stdout.buffer.flush = original_flush

    @pytest.mark.asyncio
    async def test_read_from_server_exception(self, console_ui, mock_connection):
        """Test read_from_server with exception"""
        # Setup
        console_ui.is_running = True
        mock_connection.read_data.side_effect = Exception("Test exception")

        # Call the method
        await console_ui._read_from_server()

        # Verify is_running was set to False
        assert console_ui.is_running is False

    @pytest.mark.asyncio
    async def test_read_from_server_cancelled(self, console_ui, mock_connection):
        """Test read_from_server with cancellation"""
        # Setup
        console_ui.is_running = True
        mock_connection.read_data.side_effect = asyncio.CancelledError()

        # Call the method - should not raise exception
        await console_ui._read_from_server()

        # Verify is_running is still True (cancellation is expected)
        assert console_ui.is_running is True

    @pytest.mark.asyncio
    @patch("select.select")
    async def test_handle_keyboard_input(self, mock_select, console_ui, mock_connection):
        """Test keyboard input handling"""
        # Setup
        console_ui.is_running = True

        # Setup select.select to return data available only once
        call_count = 0

        def select_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ([sys.stdin], [], [])  # First call: data available
            console_ui.is_running = False  # End loop after first iteration
            return ([], [], [])  # Subsequent calls: no data available

        mock_select.side_effect = select_side_effect

        # Mock stdin.read to return Ctrl+C
        mock_stdin = MagicMock()
        mock_stdin.read.return_value = "\x03"  # Ctrl+C

        # Mock sleep to be a passthrough function
        async def sleep_passthrough(delay):
            pass

        with (
            patch("sys.stdin", mock_stdin),
            patch("asyncio.sleep", sleep_passthrough),
        ):
            # Call the method
            await console_ui._handle_keyboard_input()

            # Verify is_running was set to False
            assert console_ui.is_running is False
            # Verify stdin.read was called
            mock_stdin.read.assert_called_once()

    @pytest.mark.asyncio
    @patch("select.select")
    async def test_handle_keyboard_input_normal_char(self, mock_select, console_ui, mock_connection):
        """Test keyboard input handling with normal character"""
        # Setup test to run once then exit
        console_ui.is_running = True

        # Setup select to only return data once, then exit loop
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ([sys.stdin], [], [])  # First call: data available
            console_ui.is_running = False  # End loop after first iteration
            return ([], [], [])  # No data available

        mock_select.side_effect = side_effect

        # Mock stdin.read to return a normal character
        mock_stdin = MagicMock()
        mock_stdin.read.return_value = "a"

        # Mock sleep to be a passthrough function
        async def sleep_passthrough(delay):
            pass

        with (
            patch("sys.stdin", mock_stdin),
            patch("asyncio.sleep", sleep_passthrough),
        ):
            # Call the method
            await console_ui._handle_keyboard_input()

            # Verify send_data was called with correct data
            mock_connection.send_data.assert_called_once_with(b"a")
            # Verify stdin.read was called
            mock_stdin.read.assert_called_once()

    @pytest.mark.asyncio
    @patch("select.select")
    async def test_handle_keyboard_input_exception(self, mock_select, console_ui, mock_connection):
        """Test keyboard input handling with exception"""
        # Setup
        console_ui.is_running = True

        # Mock select.select to raise exception
        mock_select.side_effect = Exception("Test exception")

        with patch("asyncio.sleep", new_callable=AsyncMock):
            # Call the method
            await console_ui._handle_keyboard_input()

        # Verify is_running was set to False
        assert console_ui.is_running is False

    def test_is_data_available(self, console_ui):
        """Test is_data_available method"""
        with patch("select.select") as mock_select:
            # Test data available
            mock_select.return_value = ([sys.stdin], [], [])
            assert console_ui._is_data_available() is True

            # Test no data available
            mock_select.return_value = ([], [], [])
            assert console_ui._is_data_available() is False

    def test_set_raw_mode_non_posix(self, console_ui):
        """Test set_raw_mode on non-POSIX system"""
        with patch("os.name", "nt"):
            console_ui._set_raw_mode()
            # Should do nothing on non-POSIX system
            assert console_ui.old_settings is None

    def test_restore_terminal_non_posix(self, console_ui):
        """Test restore_terminal on non-POSIX system"""
        with patch("os.name", "nt"):
            console_ui._restore_terminal()
            # Should do nothing on non-POSIX system
            assert True

    @pytest.mark.asyncio
    async def test_handle_keyboard_input_no_data(self, console_ui, mock_connection):
        """Test keyboard input handling when no data is available"""
        # Setup
        console_ui.is_running = True

        # Make is_running False after 2 iterations
        call_count = 0

        # Mock asyncio.sleep as a real coroutine that sets is_running to False after 2 calls
        async def mock_sleep(delay):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                console_ui.is_running = False

        # Mock _is_data_available to return False (no data available)
        console_ui._is_data_available = MagicMock(return_value=False)

        # Call the method with patched sleep
        with patch("asyncio.sleep", mock_sleep):
            await console_ui._handle_keyboard_input()

        # Verify that _is_data_available was called
        assert console_ui._is_data_available.call_count >= 1
        # Verify that send_data was not called (since no data was available)
        mock_connection.send_data.assert_not_called()

    @pytest.mark.asyncio
    async def test_read_from_server_no_data(self, console_ui, mock_connection):
        """Test reading no data from server (connection closed)"""
        # Setup
        console_ui.is_running = True

        # Mock connection read_data to return None (connection closed)
        mock_connection.read_data.return_value = None

        # Call the method
        await console_ui._read_from_server()

        # Verify is_running was set to False
        assert console_ui.is_running is False

    @pytest.mark.asyncio
    async def test_restore_terminal_with_old_settings(self, console_ui):
        """Test _restore_terminal with old_settings"""
        with (
            patch("os.name", "posix"),
            patch("termios.tcsetattr") as mock_tcsetattr,
        ):
            # Set old_settings
            console_ui.old_settings = "test_settings"

            # Call the method
            console_ui._restore_terminal()

            # Verify termios.tcsetattr was called
            mock_tcsetattr.assert_called_once_with(sys.stdin, termios.TCSADRAIN, "test_settings")


class TestTakeTargetInput:
    """The `f` command's targeted-takeover prompt (issue #61)."""

    @pytest.fixture
    def console(self):
        connection = AsyncMock(spec=BaseClientAdapter)
        connection.is_connected = True
        ui = ConsoleUI(connection)
        ui._stdin_fd = None
        return ui

    @pytest.mark.asyncio
    async def test_reads_id_strips_crlf(self, console):
        # "f4\r\n" -> "f4"
        with (
            patch("openmux.client.console.sys.stdin") as mock_stdin,
            patch.object(console, "_is_data_available", return_value=True),
        ):
            mock_stdin.read.side_effect = ["f", "4", "\r", "\n"]
            result = await console._read_take_target_input()
        assert result == "f4"

    @pytest.mark.asyncio
    async def test_enter_is_no_target(self, console):
        with (
            patch("openmux.client.console.sys.stdin") as mock_stdin,
            patch.object(console, "_is_data_available", return_value=True),
        ):
            mock_stdin.read.return_value = "\n"
            result = await console._read_take_target_input()
        assert result == ""

    @pytest.mark.asyncio
    async def test_backspace_edits(self, console):
        # "ab" then backspace then "c" -> "ac"
        with (
            patch("openmux.client.console.sys.stdin") as mock_stdin,
            patch.object(console, "_is_data_available", return_value=True),
        ):
            mock_stdin.read.side_effect = ["a", "b", "\x7f", "c", "\n"]
            result = await console._read_take_target_input()
        assert result == "ac"

    @pytest.mark.asyncio
    async def test_process_escape_f_sends_no_target_when_enter(self, console):
        with (
            patch("openmux.client.console.sys.stdin") as mock_stdin,
            patch.object(console, "_is_data_available", return_value=True),
        ):
            mock_stdin.read.return_value = "\n"
            await console._process_escape_command("f")
        console.connection.force_read_write.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_process_escape_f_sends_target_when_given(self, console):
        with (
            patch("openmux.client.console.sys.stdin") as mock_stdin,
            patch.object(console, "_is_data_available", return_value=True),
        ):
            mock_stdin.read.side_effect = ["f", "4", "\n"]
            await console._process_escape_command("f")
        console.connection.force_read_write.assert_awaited_once_with("f4")

    @pytest.mark.asyncio
    async def test_force_access_mode_falls_back_on_old_adapter(self, console):
        # An adapter whose force_read_write predates the target parameter:
        # calling it WITH a target raises TypeError, and the console must
        # retry without the target so takes still work against old adapters.
        calls = []

        async def force_read_write():
            calls.append("no-arg")
            return True

        console.connection.force_read_write = force_read_write
        await console._force_access_mode("f4")
        assert calls == ["no-arg"]  # fell back to the legacy no-target form

    @pytest.mark.asyncio
    async def test_force_access_mode_unsupported_shows_notice(self, console):
        console.connection.force_read_write = None
        with (patch("openmux.client.console.sys.stdout", new=io.StringIO()) as mock_stdout,):
            await console._force_access_mode("f4")
        assert "not supported by this connection" in mock_stdout.getvalue()

    @pytest.mark.asyncio
    async def test_help_mentions_target_prompt(self, console):
        import openmux.client.console as console_mod

        help_text = ""
        with patch.object(console_mod.sys, "stdout", new=io.StringIO()) as mock_stdout:
            await console._show_help()
            help_text = mock_stdout.getvalue()
        assert "take the write slot" in help_text
        assert "holder id" in help_text


def _make_ui(current_port="c1", feeds_reply=None, switch_out=True, supported=True):
    """Build a ConsoleUI over a fake connection that models the
    single-reader power flow.

    The fake connection mimics the client adapters: a power OMXCTRL frame
    arrives on `last_power_reply` (never as console bytes), and the plain
    follow-up bytes (the live [POWER] notice) are delivered through
    `read_data` a short beat after the reply - exactly like the server sends
    them and the background console loop renders them. The test's
    `_fake_console_bg` task plays that sole background reader. `_stdin_fd` is
    None so the menu reads stdin through the (patched) `sys.stdin.read`.
    `supported=False` omits the power methods to exercise the "not supported
    by this connection" guard.
    """
    from openmux.client.console import ConsoleUI

    class FakeConnection:
        def __init__(self):
            self.is_connected = True
            self.is_authenticated = True
            self.last_power_reply = None
            self.current_port = current_port
            self._pending_reply = None
            self._notice = None
            self._notice_at = None
            self._switch_plan = []
            self._query_calls = 0
            self._switch_calls = []
            self._switch_out = switch_out
            self.rendered = []
            self.read_calls = 0  # regression guard: who reads the stream

        def plan_switch(self, notice, reply, notice_delay=0.06):
            """Stage one switch round-trip: `reply` (power_switch dict) lands
            on `last_power_reply` when the next switch_power_outlet call is
            made, and `notice` (plain bytes or None) is delivered through
            read_data `notice_delay` seconds later - mirroring the server,
            which sends the live notice next to the reply."""
            self._switch_plan.append((notice, reply, notice_delay))

    async def _switch_power_outlet(conn, ref, on):
        conn._switch_calls.append((ref, bool(on)))
        if conn._switch_plan:
            notice, reply, delay = conn._switch_plan.pop(0)
            conn._pending_reply = (reply, notice, asyncio.get_event_loop().time() + delay)
        return conn._switch_out

    async def _request_power_feeds(conn):
        conn._query_calls += 1
        return True

    async def _read_data(conn, timeout=None):
        conn.read_calls += 1
        await asyncio.sleep(0)  # yield so the menu's poll ticks can run
        loop = asyncio.get_event_loop()
        if conn._pending_reply is not None:
            reply, notice, at = conn._pending_reply
            conn._pending_reply = None
            # Model the adapter's interception: stored, not console bytes.
            conn.last_power_reply = reply
            if notice is not None:
                conn._notice = notice
                conn._notice_at = at
            return b""
        if conn._notice_at is not None and loop.time() >= conn._notice_at:
            conn._notice_at = None
            notice = conn._notice
            conn._notice = None
            return notice
        return b""

    conn = FakeConnection()
    if feeds_reply is not None:
        conn._pending_reply = (feeds_reply, None, None)
    if supported:
        conn.switch_power_outlet = types.MethodType(_switch_power_outlet, conn)
        conn.request_power_feeds = types.MethodType(_request_power_feeds, conn)
    # `read_data` is always present: the background console loop (or the
    # fake `_fake_console_bg` task) is its only caller.
    conn.read_data = types.MethodType(_read_data, conn)
    ui = ConsoleUI(conn)
    ui.is_running = True
    ui._stdin_fd = None
    return ui, conn


async def _fake_console_bg(ui, conn):
    """Play the one background reader the console keeps running at all times.

    Pulls plain payloads off the fake stream and records them in
    `conn.rendered` (standing in for `_read_from_server` rendering to the
    terminal), bumping `ui._stream_activity` like the real loop does. It
    never touches `last_power_reply`.
    """
    while ui.is_running:
        data = await conn.read_data(timeout=0.05)
        if data:
            conn.rendered.append(data)
            ui._stream_activity += 1


class TestPowerMenu:
    """The `p` command's per-console PDU power menu (mirrors telnet/SSH).

    These tests run a fake background reader task (`_fake_console_bg`) that
    models the ONE reader the console keeps at all times; the menu never
    reads the stream itself - it waits for the reply the fake "adapter"
    captures plus a settle of the stream.
    """

    @pytest.mark.asyncio
    async def test_no_connection_support(self, capsys):
        ui, conn = _make_ui(supported=False)
        await ui._power_menu()
        out = capsys.readouterr().out
        assert "not supported by this connection" in out
        assert ui._stream_exclusive is False

    @pytest.mark.asyncio
    async def test_requires_attached_console(self, capsys):
        ui, conn = _make_ui(current_port=None)
        await ui._power_menu()
        out = capsys.readouterr().out
        assert "CONNECT first" in out

    @pytest.mark.asyncio
    async def test_captures_power_reply_and_clears(self):
        ui, conn = _make_ui(feeds_reply={"type": "power_feeds", "feeds": [], "feeds_total": 0, "state": "unknown"})
        conn.last_power_reply = {"type": "power_switch", "ok": False, "error": "leftover"}  # cleared by the wait
        bg = asyncio.create_task(_fake_console_bg(ui, conn))
        reply = await ui._await_power_reply()
        bg.cancel()
        assert reply["type"] == "power_feeds"
        assert conn.last_power_reply is None

    @pytest.mark.asyncio
    async def test_enter_leaves_and_markers(self, capsys):
        ui, conn = _make_ui(
            feeds_reply={"type": "power_feeds", "feeds": [{"ref": "r.1", "on": True}], "feeds_total": 1, "state": "all"}
        )
        bg = asyncio.create_task(_fake_console_bg(ui, conn))
        with (
            patch("openmux.client.console.sys.stdin") as mock_stdin,
            patch.object(ui, "_is_data_available", return_value=True),
        ):
            mock_stdin.read.return_value = "\n"  # Enter leaves
            await ui._power_menu()
        bg.cancel()
        out = capsys.readouterr().out
        assert "POWER: feeds for c1" in out
        assert "[on]" in out
        assert "[EXITING POWER]" in out
        assert ui._stream_exclusive is False

    @pytest.mark.asyncio
    async def test_toggle_single_feed_notice_before_confirm(self, capsys):
        # Feeds: r.1 on, r.2 off. "1" toggles r.1 to off. The live [POWER]
        # notice (plain bytes, delivered by the background reader a beat
        # after the reply) must reach the terminal BEFORE the menu prints
        # the "POWER r.1 -> off" confirmation and re-renders.
        ui, conn = _make_ui(
            feeds_reply={
                "type": "power_feeds",
                "feeds": [{"ref": "r.1", "on": True}, {"ref": "r.2", "on": False}],
                "feeds_total": 2,
                "state": "some",
            }
        )
        conn.plan_switch(
            b"\r\n[POWER] feed r.1 is now off\r\n",
            {"type": "power_switch", "ok": True, "ref": "r.1", "on": False, "state": "off", "impact": {}},
        )
        bg = asyncio.create_task(_fake_console_bg(ui, conn))
        with (
            patch("openmux.client.console.sys.stdin") as mock_stdin,
            patch.object(ui, "_is_data_available", return_value=True),
        ):
            mock_stdin.read.side_effect = ["1", "\n", "\n"]  # "1", Enter (post-switch), Enter (exit)
            await ui._power_menu()
        bg.cancel()
        out = capsys.readouterr().out
        assert ("r.1", False) in conn._switch_calls
        # The background reader rendered the notice (and _wait_stream_settle
        # only returned after it had), so it lands BEFORE the confirmation.
        assert b"\r\n[POWER] feed r.1 is now off\r\n" in conn.rendered
        assert out.index("POWER r.1 -> off") > 0
        assert "POWER r.1 -> off" in out

    @pytest.mark.asyncio
    async def test_feed_request_send_failure(self, capsys):
        ui, conn = _make_ui(feeds_reply={"type": "power_feeds", "feeds": [], "feeds_total": 0, "state": "unknown"})

        async def _no_send():
            return False

        conn.request_power_feeds = _no_send
        await ui._power_menu()
        out = capsys.readouterr().out
        assert "could not send the feed request" in out
        assert conn._query_calls == 0

    @pytest.mark.asyncio
    async def test_toggle_all_feeds(self, capsys):
        ui, conn = _make_ui(
            feeds_reply={
                "type": "power_feeds",
                "feeds": [{"ref": "r.1", "on": True}, {"ref": "r.2", "on": True}],
                "feeds_total": 2,
                "state": "all",
            }
        )
        for ref in ("r.1", "r.2"):
            notice = ("\r\n[POWER] feed " + ref + " is now off\r\n").encode("utf-8")
            conn.plan_switch(
                notice, {"type": "power_switch", "ok": True, "ref": ref, "on": False, "state": "off", "impact": {}}
            )
        bg = asyncio.create_task(_fake_console_bg(ui, conn))
        with (
            patch("openmux.client.console.sys.stdin") as mock_stdin,
            patch.object(ui, "_is_data_available", return_value=True),
        ):
            mock_stdin.read.side_effect = ["a", "\n", "\n"]  # "a", Enter (post-switch), Enter (exit)
            await ui._power_menu()
        bg.cancel()
        out = capsys.readouterr().out
        assert conn._switch_calls == [("r.1", False), ("r.2", False)]
        assert "POWER r.1 -> off" in out
        assert "POWER r.2 -> off" in out
        assert "[EXITING POWER]" in out
        assert ui._stream_exclusive is False

    @pytest.mark.asyncio
    async def test_denied_switch_shows_error(self, capsys):
        ui, conn = _make_ui(
            feeds_reply={"type": "power_feeds", "feeds": [{"ref": "r.1", "on": True}], "feeds_total": 1, "state": "all"}
        )
        conn.plan_switch(None, {"type": "power_switch", "ok": False, "error": "insufficient permission (need read-write)"})
        bg = asyncio.create_task(_fake_console_bg(ui, conn))
        with (
            patch("openmux.client.console.sys.stdin") as mock_stdin,
            patch.object(ui, "_is_data_available", return_value=True),
        ):
            mock_stdin.read.side_effect = ["1", "\n", "\n"]  # "1", Enter (post-switch), Enter (exit)
            await ui._power_menu()
        bg.cancel()
        out = capsys.readouterr().out
        assert ("r.1", False) in conn._switch_calls
        assert "ERROR:POWER: insufficient permission" in out
        assert "POWER r.1 ->" not in out  # no confirmation on denial
        assert "[EXITING POWER]" in out

    @pytest.mark.asyncio
    async def test_not_configured_reply(self, capsys):
        ui, conn = _make_ui(
            feeds_reply={"type": "power_feeds", "ok": False, "error": "power management is not configured", "feeds": []}
        )
        bg = asyncio.create_task(_fake_console_bg(ui, conn))
        with (
            patch("openmux.client.console.sys.stdin") as mock_stdin,
            patch.object(ui, "_is_data_available", return_value=True),
        ):
            mock_stdin.read.return_value = "\n"
            await ui._power_menu()
        bg.cancel()
        out = capsys.readouterr().out
        assert "power management is not configured" in out
        assert ui._stream_exclusive is False

    @pytest.mark.asyncio
    async def test_invalid_and_out_of_range_loop(self, capsys):
        ui, conn = _make_ui(
            feeds_reply={"type": "power_feeds", "feeds": [{"ref": "r.1", "on": True}], "feeds_total": 1, "state": "all"}
        )
        bg = asyncio.create_task(_fake_console_bg(ui, conn))
        with (
            patch("openmux.client.console.sys.stdin") as mock_stdin,
            patch.object(ui, "_is_data_available", return_value=True),
        ):
            # "z" invalid, "9" out of range, then Enter.
            mock_stdin.read.side_effect = ["z", "\n", "9", "\n", "\n"]
            await ui._power_menu()
        bg.cancel()
        out = capsys.readouterr().out
        assert "enter a feed number" in out
        assert "number out of range (1-1)" in out
        assert "[EXITING POWER]" in out
        assert conn._switch_calls == []
        assert ui._stream_exclusive is False

    @pytest.mark.asyncio
    async def test_await_power_reply_never_reads_the_stream(self):
        # Regression: the menu's waiter must ONLY poll the captured reply.
        # A second reader on the same transport raced the background loop
        # and broke the session ("could not send the switch request").
        ui, conn = _make_ui()
        reply = await ui._await_power_reply(timeout=0.05)
        assert reply is None  # nothing was sent; the wait timed out
        assert conn.read_calls == 0

    @pytest.mark.asyncio
    async def test_await_power_reply_gives_up_on_disconnect(self):
        ui, conn = _make_ui()

        async def _drop_later():
            await asyncio.sleep(0.02)
            conn.is_connected = False

        droptask = asyncio.create_task(_drop_later())
        t0 = asyncio.get_event_loop().time()
        reply = await ui._await_power_reply(timeout=6.0)
        droptask.cancel()
        assert reply is None
        # bailed out on the disconnect instead of waiting the full timeout
        assert asyncio.get_event_loop().time() - t0 < 3.0

    @pytest.mark.asyncio
    async def test_escape_p_routes_to_power_menu(self, capsys):
        ui, conn = _make_ui(
            feeds_reply={"type": "power_feeds", "feeds": [{"ref": "r.1", "on": True}], "feeds_total": 1, "state": "all"}
        )
        bg = asyncio.create_task(_fake_console_bg(ui, conn))
        with (
            patch("openmux.client.console.sys.stdin") as mock_stdin,
            patch.object(ui, "_is_data_available", return_value=True),
        ):
            mock_stdin.read.return_value = "\n"
            await ui._process_escape_command("p")
        bg.cancel()
        out = capsys.readouterr().out
        assert "POWER: feeds for" in out
        assert ui._stream_exclusive is False

    @pytest.mark.asyncio
    async def test_help_lists_power_not_playback(self, capsys):
        import openmux.client.console as console_mod

        ui, _conn = _make_ui()
        with patch.object(console_mod.sys, "stdout", new=io.StringIO()) as mock_stdout:
            await ui._show_help()
            help_text = mock_stdout.getvalue()
        assert "power (this console's feeds: number = toggle, a = all)" in help_text
        assert "playback" not in help_text
        assert "set number of playback lines" not in help_text
