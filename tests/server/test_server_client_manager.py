"""
Tests for the OpenMux server client manager
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from tests.support.protocol_handler import (
    ClientConnection,
)
from tests.support.protocol_handler import OpenMuxProtocolHandler as ClientManager


class TestClientConnection:
    @pytest.fixture
    def client_connection(self):
        """Create a ClientConnection instance"""
        reader = AsyncMock()
        writer = MagicMock()
        writer.write = MagicMock()  # write is sync
        writer.drain = AsyncMock()  # drain is async
        writer.close = MagicMock()  # close is sync
        writer.wait_closed = AsyncMock()  # wait_closed is async
        client_id = "test-client-id"

        # Mock the asyncio.get_running_loop() call in the constructor
        with patch("asyncio.get_running_loop") as mock_get_loop:
            mock_loop = MagicMock()
            mock_loop.time.return_value = 123456789.0
            mock_get_loop.return_value = mock_loop
            return ClientConnection(reader, writer, client_id)

    def test_init(self, client_connection):
        """Test initialization of ClientConnection"""
        assert client_connection.reader is not None
        assert client_connection.writer is not None
        assert client_connection.client_id == "test-client-id"
        assert client_connection.username is None
        assert client_connection.connected_port is None
        assert client_connection.mode == "read-only"
        assert client_connection.addr is not None
        assert client_connection.connected_at is not None

    @pytest.mark.asyncio
    async def test_send_success(self, client_connection):
        """Test sending data successfully"""
        result = await client_connection.send(b"test data")

        assert result is True
        client_connection.writer.write.assert_called_once_with(b"test data")
        client_connection.writer.drain.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_exception(self, client_connection):
        """Test sending data with exception"""
        client_connection.writer.write.side_effect = Exception("Write error")
        # Add a mock for drain so we can control its behavior
        client_connection.writer.drain = AsyncMock(side_effect=Exception("Drain error"))

        result = await client_connection.send(b"test data")

        assert result is False

    @pytest.mark.asyncio
    async def test_close(self, client_connection):
        """Test closing the connection"""
        await client_connection.close()

        client_connection.writer.close.assert_called_once()
        client_connection.writer.wait_closed.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_exception(self, client_connection):
        """Test closing the connection with exception"""
        client_connection.writer.close.side_effect = Exception("Close error")

        # Should not raise exception
        await client_connection.close()


class TestClientManager:
    @pytest.fixture
    def console_manager(self):
        """Create a mock console manager"""
        console_manager = AsyncMock()
        console_manager.register_client_manager = MagicMock()
        return console_manager

    @pytest.fixture
    def auth_manager(self):
        """Create a mock auth manager"""
        return AsyncMock()

    @pytest.fixture
    def client_manager(self, console_manager, auth_manager):
        """Create a ClientManager instance"""
        return ClientManager(console_manager, auth_manager)

    def test_init(self, client_manager, console_manager):
        """Test initialization of ClientManager"""
        assert client_manager.console_manager is not None
        assert client_manager.auth_manager is not None
        assert client_manager.clients == []
        assert client_manager.logger is not None

        # Verify console manager registration
        console_manager.register_client_manager.assert_called_once_with(client_manager)

    @pytest.mark.asyncio
    @patch(
        "tests.support.protocol_handler.secrets.token_hex",
        return_value="client-id",
    )
    async def test_handle_new_connection(self, mock_token_hex, client_manager):
        """Test handling a new client connection"""
        # Mock reader and writer
        reader = AsyncMock()
        writer = MagicMock()
        writer.write = MagicMock()  # write is sync
        writer.drain = AsyncMock()  # drain is async
        writer.close = MagicMock()  # close is sync
        writer.wait_closed = AsyncMock()  # wait_closed is async
        writer.get_extra_info.return_value = ("127.0.0.1", 12345)

        # Mock handle_client to avoid running the full method
        client_manager.handle_client = AsyncMock()

        await client_manager.handle_new_connection(reader, writer)

        # Verify client was created and added to clients
        assert len(client_manager.clients) == 1
        client = client_manager.clients[0]
        assert client.client_id == "client-id"
        assert client.reader == reader
        assert client.writer == writer

        # Verify handle_client was called
        client_manager.handle_client.assert_called_once_with(client)

    @pytest.mark.asyncio
    async def test_handle_client_success(self, client_manager):
        """Test handling a client successfully"""
        # Create a test client
        client = AsyncMock()
        client.client_id = "test-client"
        client_manager.clients.append(client)

        # Mock authenticate and handle commands methods
        client_manager._authenticate_client = AsyncMock(return_value=True)
        client_manager._handle_client_commands = AsyncMock()

        await client_manager.handle_client(client)

        # Verify methods were called
        client_manager._authenticate_client.assert_called_once_with(client)

        # Verify all send calls (terminal setup + welcome message)
        expected_calls = [
            call(b"\x1b[?25h"),  # Show cursor
            call(b"\x1b[?7h"),  # Enable line wrapping
            call(b"Welcome to OpenMux Server\r\n"),  # Welcome message
        ]
        client.send.assert_has_calls(expected_calls)
        assert client.send.call_count == 3

        client_manager._handle_client_commands.assert_called_once_with(client)

    @pytest.mark.asyncio
    async def test_handle_client_auth_failure(self, client_manager):
        """Test handling a client with authentication failure"""
        # Create a test client
        client = AsyncMock()
        client.client_id = "test-client"
        client_manager.clients.append(client)

        # Mock authenticate method to return False
        client_manager._authenticate_client = AsyncMock(return_value=False)

        await client_manager.handle_client(client)

        # Verify all send calls (terminal setup + auth failure message)
        expected_calls = [
            call(b"\x1b[?25h"),  # Show cursor
            call(b"\x1b[?7h"),  # Enable line wrapping
            call(b"Authentication failed\r\n"),  # Auth failure message
        ]
        client.send.assert_has_calls(expected_calls)
        assert client.send.call_count == 3

        # Verify client was closed and removed
        client.close.assert_called_once()
        assert client not in client_manager.clients

    @pytest.mark.asyncio
    async def test_handle_client_exception(self, client_manager):
        """Test handling a client with exception"""
        # Create a test client
        client = AsyncMock()
        client.client_id = "test-client"
        client_manager.clients.append(client)

        # Mock authenticate method to raise exception
        client_manager._authenticate_client = AsyncMock(side_effect=Exception("Auth error"))
        client_manager._disconnect_client = AsyncMock()

        await client_manager.handle_client(client)

        # Verify disconnect was called
        client_manager._disconnect_client.assert_called_once_with(client.client_id)

    @pytest.mark.asyncio
    async def test_authenticate_client_api_key_success(self, client_manager):
        """Test authenticating client with API key successfully"""
        # Create a test client
        client = AsyncMock()
        client.reader.readline.return_value = b"AUTH:KEY:api-key-123\n"

        # Mock auth manager
        # Use MagicMock instead of AsyncMock for verify_api_key as it should return a value directly
        client_manager.auth_manager.verify_api_key = MagicMock(return_value=True)

        result = await client_manager._authenticate_client(client)

        # Verify result and client state
        assert result is True
        assert client.username.startswith("api-")

    @pytest.mark.asyncio
    async def test_authenticate_client_api_key_failure(self, client_manager):
        """Test authenticating client with invalid API key"""
        # Create a test client
        client = AsyncMock()
        client.reader.readline.return_value = b"AUTH:KEY:invalid-key\n"

        # Mock auth manager
        # Use MagicMock instead of AsyncMock for verify_api_key as it should return a value directly
        client_manager.auth_manager.verify_api_key = MagicMock(return_value=False)

        result = await client_manager._authenticate_client(client)

        # Verify result
        assert result is False

    @pytest.mark.asyncio
    async def test_authenticate_client_username_password_success(self, client_manager):
        """Test authenticating client with username/password successfully"""
        # Create a test client
        client = AsyncMock()
        client.reader.readline.return_value = b"AUTH:USER:admin:password\n"

        # Mock auth manager
        # Use MagicMock instead of AsyncMock for authenticate as it should return a value directly
        client_manager.auth_manager.authenticate = MagicMock(return_value=True)

        result = await client_manager._authenticate_client(client)

        # Verify result and client state
        assert result is True
        assert client.username == "admin"

    @pytest.mark.asyncio
    async def test_authenticate_client_username_password_failure(self, client_manager):
        """Test authenticating client with invalid username/password"""
        # Create a test client
        client = AsyncMock()
        client.reader.readline.return_value = b"AUTH:USER:admin:wrongpass\n"

        # Mock auth manager
        # Use MagicMock instead of AsyncMock for authenticate as it should return a value directly
        client_manager.auth_manager.authenticate = MagicMock(return_value=False)

        result = await client_manager._authenticate_client(client)

        # Verify result
        assert result is False

    @pytest.mark.asyncio
    async def test_authenticate_client_invalid_format(self, client_manager):
        """Test authenticating client with invalid format"""
        # Create a test client
        client = AsyncMock()
        client.reader.readline.return_value = b"INVALID\n"

        result = await client_manager._authenticate_client(client)

        # Verify result
        assert result is False

    @pytest.mark.asyncio
    async def test_handle_client_commands_list(self, client_manager):
        """Test handling LIST command"""
        # Create a test client
        client = AsyncMock()
        client.reader.readline.side_effect = [b"LIST\n", b"QUIT\n"]

        # Mock _handle_list_command
        client_manager._handle_list_command = AsyncMock()

        await client_manager._handle_client_commands(client)

        # Verify _handle_list_command was called
        client_manager._handle_list_command.assert_called_once_with(client)

    @pytest.mark.asyncio
    async def test_handle_client_commands_connect(self, client_manager):
        """Test handling CONNECT command"""
        # Create a test client
        client = AsyncMock()
        client.reader.readline.side_effect = [b"CONNECT:port1\n", b"QUIT\n"]

        # Mock _handle_connect_command
        client_manager._handle_connect_command = AsyncMock()

        await client_manager._handle_client_commands(client)

        # Verify _handle_connect_command was called
        client_manager._handle_connect_command.assert_called_once_with(client, "port1")

    @pytest.mark.asyncio
    async def test_handle_client_commands_reload(self, client_manager):
        """Test handling RELOAD command"""
        # Create a test client
        client = AsyncMock()
        client.reader.readline.side_effect = [b"RELOAD\n", b"QUIT\n"]

        await client_manager._handle_client_commands(client)

        # Verify client.send was called with reload message
        client.send.assert_any_call(b"Reloading configuration...\r\n")

    @pytest.mark.asyncio
    async def test_handle_client_commands_quit(self, client_manager):
        """Test handling QUIT command"""
        # Create a test client
        client = AsyncMock()
        client.reader.readline.return_value = b"QUIT\n"

        await client_manager._handle_client_commands(client)

        # Verify client.send was called with goodbye message
        client.send.assert_called_once_with(b"Goodbye\r\n")

    @pytest.mark.asyncio
    async def test_handle_client_commands_exit(self, client_manager):
        """Test handling EXIT command"""
        # Create a test client
        client = AsyncMock()
        client.reader.readline.return_value = b"EXIT\n"

        await client_manager._handle_client_commands(client)

        # Verify client.send was called with goodbye message
        client.send.assert_called_once_with(b"Goodbye\r\n")

    @pytest.mark.asyncio
    async def test_handle_client_commands_connected_port(self, client_manager):
        """Test handling data when connected to a port"""
        # Create a test client
        client = AsyncMock()
        client.connected_port = "port1"
        client.client_id = "client-1"
        client.reader.readline.side_effect = [b"data to port\n", b"QUIT\n"]

        # Mock console_manager.write_to_port
        client_manager.console_manager.write_to_port = AsyncMock()

        await client_manager._handle_client_commands(client)

        # Verify write_to_port was called
        client_manager.console_manager.write_to_port.assert_called_once_with("port1", b"data to port\n", "client-1")

    @pytest.mark.asyncio
    async def test_handle_client_commands_unknown(self, client_manager):
        """Test handling unknown command"""
        # Create a test client
        client = AsyncMock()
        client.connected_port = None
        client.reader.readline.side_effect = [b"UNKNOWN\n", b"QUIT\n"]

        await client_manager._handle_client_commands(client)

        # Verify client.send was called with unknown command message
        client.send.assert_any_call(b"Unknown command\r\n")

    @pytest.mark.asyncio
    async def test_handle_client_commands_disconnect(self, client_manager):
        """Test handling client disconnect"""
        # Create a test client
        client = AsyncMock()
        client.reader.readline.return_value = b""  # Empty data indicates disconnect

        await client_manager._handle_client_commands(client)

        # Verify no exceptions were raised
        assert True

    @pytest.mark.asyncio
    async def test_handle_list_command(self, client_manager):
        """Test handling LIST command"""
        # Create a test client
        client = AsyncMock()

        # Mock console_manager.get_port_list
        port_list = [
            {
                "name": "port1",
                "description": "Serial Port 1",
                "device": "/dev/ttyS0",
                "is_connected": True,
                "clients": 2,
                "read_write_clients": 1,
            },
            {
                "name": "port2",
                "description": "Serial Port 2",
                "device": "/dev/ttyS1",
                "is_connected": False,
                "clients": 0,
                "read_write_clients": 0,
            },
        ]
        client_manager.console_manager.get_port_list.return_value = port_list

        await client_manager._handle_list_command(client)

        # Verify client.send was called with formatted port list
        client.send.assert_called_once()
        send_data = client.send.call_args[0][0].decode()
        assert "Available ports" in send_data
        assert "port1" in send_data
        assert "port2" in send_data
        assert "Connected" in send_data
        assert "Disconnected" in send_data
        assert "2 clients, 1 read-write" in send_data

    @pytest.mark.asyncio
    async def test_handle_connect_command_port_not_found(self, client_manager):
        """Test handling CONNECT command with non-existent port"""
        # Create a test client
        client = AsyncMock()

        # Mock console_manager.port_exists
        client_manager.console_manager.port_exists.return_value = False

        await client_manager._handle_connect_command(client, "nonexistent")

        # Verify client.send was called with port not found message
        client.send.assert_called_once()
        assert b"not found" in client.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_handle_connect_command_success_read_only(self, client_manager):
        """Test handling CONNECT command successfully in read-only mode"""
        # Create a test client
        client = AsyncMock()
        client.client_id = "client-1"
        client.username = "user"

        # Mock console_manager methods
        client_manager.console_manager.port_exists.return_value = True
        client_manager.console_manager.connect_client_to_port.return_value = (
            True,
            "read-only",
        )

        # Mock _handle_console_mode to avoid running the console mode loop
        client_manager._handle_console_mode = AsyncMock()

        await client_manager._handle_connect_command(client, "port1")

        # Verify client state and messages
        assert client.connected_port == "port1"
        assert client.mode == "read-only"
        client.send.assert_any_call(b"Connected to port1 in read-only mode\r\n")
        client.send.assert_any_call(b"Use Ctrl+] to access control menu\r\n")

        # Verify console mode was entered
        client_manager._handle_console_mode.assert_called_once_with(client)

    @pytest.mark.asyncio
    async def test_handle_connect_command_success_read_write(self, client_manager):
        """Test handling CONNECT command successfully in read-write mode"""
        # Create a test client
        client = AsyncMock()
        client.client_id = "client-1"
        client.username = "admin"

        # Mock console_manager methods
        client_manager.console_manager.port_exists.return_value = True
        client_manager.console_manager.connect_client_to_port.return_value = (
            True,
            "read-write",
        )

        # Mock _handle_console_mode to avoid running the console mode loop
        client_manager._handle_console_mode = AsyncMock()

        await client_manager._handle_connect_command(client, "port1")

        # Verify client state and messages
        assert client.connected_port == "port1"
        assert client.mode == "read-write"
        client.send.assert_any_call(b"Connected to port1 in read-write mode\r\n")

    @pytest.mark.asyncio
    async def test_handle_connect_command_failure(self, client_manager):
        """Test handling CONNECT command with connection failure"""
        # Create a test client
        client = AsyncMock()
        client.client_id = "client-1"
        client.username = "user"
        client.connected_port = None  # Ensure this is initially None

        # Instead of mocking methods directly, patch them on the instance
        with (
            patch.object(
                client_manager.console_manager,
                "port_exists",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                client_manager.console_manager,
                "connect_client_to_port",
                new=AsyncMock(return_value=(False, None)),
            ),
        ):

            await client_manager._handle_connect_command(client, "port1")

            # Verify failure message
            client.send.assert_called_once()
            assert b"Failed to connect" in client.send.call_args[0][0]

            # Verify client state remains None
            assert client.connected_port is None

    @pytest.mark.asyncio
    async def test_handle_console_mode(self, client_manager):
        """Test handling console mode"""
        # Create a test client
        client = AsyncMock()
        client.connected_port = "port1"
        client.client_id = "client-1"

        # Setup reader to send some data, then an escape sequence, then some more data
        client.reader.read.side_effect = [
            b"normal data",  # Normal data
            b"before\x1dafter",  # Data with escape sequence
            b"",  # End of data (disconnect)
        ]

        # Mock console_manager.write_to_port
        client_manager.console_manager.write_to_port = AsyncMock()

        # Mock _handle_control_menu to avoid running the control menu loop
        client_manager._handle_control_menu = AsyncMock()

        await client_manager._handle_console_mode(client)

        # Verify write_to_port was called for normal data
        client_manager.console_manager.write_to_port.assert_any_call("port1", b"normal data", "client-1")

        # Verify write_to_port was called for data before escape sequence
        client_manager.console_manager.write_to_port.assert_any_call("port1", b"before", "client-1")

        # Verify control menu was entered
        client_manager._handle_control_menu.assert_called_once_with(client)

        # Verify disconnect was called
        client_manager.console_manager.disconnect_client_from_port.assert_called_once_with("client-1", "port1")
        assert client.connected_port is None

    @pytest.mark.asyncio
    async def test_handle_console_mode_exception(self, client_manager):
        """Test handling console mode with exception"""
        # Create a test client
        client = AsyncMock()
        client.connected_port = "port1"
        client.client_id = "client-1"

        # Setup reader to raise exception
        client.reader.read.side_effect = Exception("Read error")

        # Mock console_manager.disconnect_client_from_port
        client_manager.console_manager.disconnect_client_from_port = AsyncMock()

        await client_manager._handle_console_mode(client)

        # Verify disconnect was called
        client_manager.console_manager.disconnect_client_from_port.assert_called_once_with("client-1", "port1")
        assert client.connected_port is None

    @pytest.mark.asyncio
    async def test_handle_control_menu_show_menu(self, client_manager):
        """Test handling control menu - show menu"""
        # Create a test client
        client = AsyncMock()
        client.connected_port = "port1"

        # Setup reader to send ? (show menu) then q (quit)
        client.reader.readline.side_effect = [b"?\n", b"q\n"]

        await client_manager._handle_control_menu(client)

        # Verify menu was sent twice (initial + after ?)
        assert client.send.call_count >= 2
        assert b"OpenMux Control Menu" in client.send.call_args_list[0][0][0]

    @pytest.mark.asyncio
    async def test_handle_control_menu_read_write_already(self, client_manager):
        """Test handling control menu - request read-write when already have it"""
        # Create a test client
        client = AsyncMock()
        client.connected_port = "port1"
        client.mode = "read-write"

        # Setup reader to send r (request read-write) then q (quit)
        client.reader.readline.side_effect = [b"r\n", b"q\n"]

        await client_manager._handle_control_menu(client)

        # Verify already have read-write message
        assert any(b"already have read-write access" in call[0][0] for call in client.send.call_args_list)

    @pytest.mark.asyncio
    async def test_handle_control_menu_read_write_success(self, client_manager):
        """Test handling control menu - request read-write successfully"""
        # Create a test client
        client = AsyncMock()
        client.connected_port = "port1"
        client.client_id = "client-1"
        client.mode = "read-only"

        # Setup reader to send r (request read-write) then q (quit)
        client.reader.readline.side_effect = [b"r\n", b"q\n"]

        # Mock promote_client_to_read_write
        client_manager.console_manager.promote_client_to_read_write.return_value = True

        await client_manager._handle_control_menu(client)

        # Verify promote was called
        client_manager.console_manager.promote_client_to_read_write.assert_called_once_with("client-1", "port1")

        # Verify success message and mode change
        assert any(b"Promoted to read-write access" in call[0][0] for call in client.send.call_args_list)
        assert client.mode == "read-write"

    @pytest.mark.asyncio
    async def test_handle_control_menu_read_write_failure(self, client_manager):
        """Test handling control menu - request read-write with failure"""
        # Create a test client
        client = AsyncMock()
        client.connected_port = "port1"
        client.client_id = "client-1"
        client.mode = "read-only"

        # Setup reader to send r (request read-write) then q (quit)
        client.reader.readline.side_effect = [b"r\n", b"q\n"]

        # Mock promote_client_to_read_write
        client_manager.console_manager.promote_client_to_read_write.return_value = False

        await client_manager._handle_control_menu(client)

        # Verify failure message and mode unchanged
        assert any(b"Failed to get read-write access" in call[0][0] for call in client.send.call_args_list)
        assert client.mode == "read-only"

    @pytest.mark.asyncio
    async def test_handle_control_menu_disconnect_reconnect_success(self, client_manager):
        """Test handling control menu - disconnect/reconnect successfully"""
        # Create a test client
        client = AsyncMock()
        client.connected_port = "port1"
        client.client_id = "client-1"
        client.username = "user1"

        # Setup reader to send d (disconnect/reconnect) then q (quit)
        client.reader.readline.side_effect = [b"d\n", b"q\n"]

        # Mock console manager unified attach/detach methods
        client_manager.console_manager.disconnect_client_from_port = AsyncMock()
        client_manager.console_manager.connect_client_to_port = AsyncMock(return_value=(True, "read-only"))

        # Mock asyncio.sleep
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await client_manager._handle_control_menu(client)

        # Verify detach and re-attach were called (disconnect happens on 'd' and again on 'q')
        client_manager.console_manager.disconnect_client_from_port.assert_any_call("client-1", "port1")
        client_manager.console_manager.connect_client_to_port.assert_called_once_with("client-1", "port1", "user1")
        assert mock_sleep.await_count >= 1

        # Verify success message
        assert any(b"Reconnected to port1" in c[0][0] for c in client.send.call_args_list)

    @pytest.mark.asyncio
    async def test_handle_control_menu_disconnect_reconnect_failure(self, client_manager):
        """Test handling control menu - disconnect/reconnect with failure"""
        # Create a test client
        client = AsyncMock()
        client.connected_port = "port1"
        client.client_id = "client-1"
        client.username = "user1"

        # Setup reader to send d (disconnect/reconnect) then q (quit)
        client.reader.readline.side_effect = [b"d\n", b"q\n"]

        # Mock console manager unified attach/detach methods
        client_manager.console_manager.disconnect_client_from_port = AsyncMock()
        client_manager.console_manager.connect_client_to_port = AsyncMock(return_value=(False, None))

        # Mock asyncio.sleep
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await client_manager._handle_control_menu(client)

        # Verify disconnect client was called once
        client_manager.console_manager.disconnect_client_from_port.assert_called_once_with("client-1", "port1")

        # Verify failure message
        assert any(b"Failed to reconnect to port1" in c[0][0] for c in client.send.call_args_list)

        # Verify client was disconnected
        assert client.connected_port is None

    @pytest.mark.asyncio
    async def test_handle_control_menu_quit(self, client_manager):
        """Test handling control menu - quit"""
        # Create a test client
        client = AsyncMock()
        client.connected_port = "port1"
        client.client_id = "client-1"

        # Setup reader to send q (quit)
        client.reader.readline.side_effect = [b"q\n"]

        # Mock disconnect_client_from_port
        client_manager.console_manager.disconnect_client_from_port = AsyncMock()

        await client_manager._handle_control_menu(client)

        # Verify disconnect was called
        client_manager.console_manager.disconnect_client_from_port.assert_called_once_with("client-1", "port1")

        # Verify disconnected message
        assert any(b"Disconnected from port1" in call[0][0] for call in client.send.call_args_list)

        # Verify client state
        assert client.connected_port is None

    @pytest.mark.asyncio
    async def test_handle_control_menu_unknown(self, client_manager):
        """Test handling control menu - unknown command"""
        # Create a test client
        client = AsyncMock()
        client.connected_port = "port1"

        # Setup reader to send x (unknown) then q (quit)
        client.reader.readline.side_effect = [b"x\n", b"q\n"]

        await client_manager._handle_control_menu(client)

        # Verify returning to console mode message
        assert any(b"Returning to console mode" in call[0][0] for call in client.send.call_args_list)

    @pytest.mark.asyncio
    async def test_disconnect_client_not_found(self, client_manager):
        """Test disconnecting a client that doesn't exist"""
        # Client not in list
        await client_manager._disconnect_client("nonexistent")

        # Should not raise exception
        assert True

    @pytest.mark.asyncio
    async def test_disconnect_client_with_port(self, client_manager):
        """Test disconnecting a client that's connected to a port"""
        # Create a test client
        client = AsyncMock()
        client.client_id = "client-1"
        client.connected_port = "port1"
        client_manager.clients.append(client)

        # Mock console_manager.disconnect_client_from_port
        client_manager.console_manager.disconnect_client_from_port = AsyncMock()

        await client_manager._disconnect_client("client-1")

        # Verify disconnect was called
        client_manager.console_manager.disconnect_client_from_port.assert_called_once_with("client-1", "port1")

        # Verify client was closed and removed
        client.close.assert_called_once()
        assert client not in client_manager.clients

    @pytest.mark.asyncio
    async def test_disconnect_client_without_port(self, client_manager):
        """Test disconnecting a client that's not connected to a port"""
        # Create a test client
        client = AsyncMock()
        client.client_id = "client-1"
        client.connected_port = None
        client_manager.clients.append(client)

        await client_manager._disconnect_client("client-1")

        # Verify client was closed and removed
        client.close.assert_called_once()
        assert client not in client_manager.clients

    @pytest.mark.asyncio
    async def test_close_all_connections(self, client_manager):
        """Test closing all connections"""
        # Create test clients
        client1 = AsyncMock()
        client1.client_id = "client-1"
        client2 = AsyncMock()
        client2.client_id = "client-2"
        client_manager.clients = [client1, client2]

        await client_manager.close_all_connections()

        # Verify all clients were closed
        client1.close.assert_called_once()
        client2.close.assert_called_once()

        # Verify clients list was cleared
        assert len(client_manager.clients) == 0

    @pytest.mark.asyncio
    async def test_send_data_to_client_found(self, client_manager):
        """Test sending data to a client that exists"""
        # Create a test client
        client = AsyncMock()
        client.client_id = "client-1"
        client.send.return_value = True
        client_manager.clients.append(client)

        result = await client_manager.send_data_to_client("client-1", b"test data")

        # Verify data was sent and result is True
        client.send.assert_called_once_with(b"test data")
        assert result is True

    @pytest.mark.asyncio
    async def test_send_data_to_client_not_found(self, client_manager):
        """Test sending data to a client that doesn't exist"""
        result = await client_manager.send_data_to_client("nonexistent", b"test data")

        # Verify result is False
        assert result is False
