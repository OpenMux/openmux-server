"""
Tests for the OpenMux client console UI
"""

import asyncio
import os
import select
import sys
import termios
import tty
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
