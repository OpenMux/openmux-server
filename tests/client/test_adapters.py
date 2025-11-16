"""
Tests for OpenMux client adapters
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openmux.client.adapters import (
    BaseClientAdapter,
    ClientAdapterFactory,
    TcpClientAdapter,
    WebSocketClientAdapter,
)


class TestClientAdapterFactory:
    """Test the ClientAdapterFactory"""

    def test_get_supported_types(self):
        """Test getting supported adapter types"""
        types = ClientAdapterFactory.get_supported_types()
        assert "tcp" in types
        assert "websocket" in types

    def test_create_tcp_adapter(self):
        """Test creating a TCP adapter"""
        adapter = ClientAdapterFactory.create_adapter(
            host="localhost",
            port=8023,
            adapter_type="tcp",
            config={"use_tls": False},
        )
        assert isinstance(adapter, TcpClientAdapter)
        assert adapter.host == "localhost"
        assert adapter.port == 8023
        # protocol_type removed; default behavior is standard

    def test_create_websocket_adapter(self):
        """Test creating a WebSocket adapter"""
        adapter = ClientAdapterFactory.create_adapter(
            host="localhost",
            port=8080,
            adapter_type="websocket",
            config={"use_tls": False},
        )
        assert isinstance(adapter, WebSocketClientAdapter)
        assert adapter.host == "localhost"
        assert adapter.port == 8080
        assert adapter.use_tls == False

    def test_create_unknown_adapter(self):
        """Test creating an unknown adapter type"""
        with pytest.raises(ValueError, match="Unknown adapter type"):
            ClientAdapterFactory.create_adapter(host="localhost", port=8023, adapter_type="unknown")

    # Backward compatibility helper removed; tests focus on create_adapter only


class TestTcpClientAdapter:
    """Test the TcpClientAdapter"""

    @pytest.fixture
    def tcp_adapter(self):
        """Create a TCP adapter for testing"""
        return TcpClientAdapter(
            host="localhost",
            port=8023,
            config={"protocol_type": "standard", "use_tls": False},
        )

    # Management adapter fixture removed

    def test_init(self, tcp_adapter):
        """Test TCP adapter initialization"""
        assert tcp_adapter.host == "localhost"
        assert tcp_adapter.port == 8023
        # protocol_type no longer present
        assert tcp_adapter.use_tls == False
        assert not tcp_adapter.is_connected
        assert not tcp_adapter.is_authenticated

    def test_get_connection_info(self, tcp_adapter):
        """Test getting connection info"""
        info = tcp_adapter.get_connection_info()
        assert info["host"] == "localhost"
        assert info["port"] == 8023
        assert info["connected"] == False
        assert info["authenticated"] == False
        assert info["adapter_type"] == "TcpClientAdapter"

    def test_is_ready(self, tcp_adapter):
        """Test ready state check"""
        assert not tcp_adapter.is_ready()

        tcp_adapter.is_connected = True
        assert not tcp_adapter.is_ready()  # Still not authenticated

        tcp_adapter.is_authenticated = True
        assert tcp_adapter.is_ready()  # Now ready

    @pytest.mark.asyncio
    async def test_connect_standard_protocol(self, tcp_adapter):
        """Test connecting with standard protocol"""
        with patch("asyncio.open_connection") as mock_open:
            # Mock the connection
            mock_reader = AsyncMock()
            mock_writer = MagicMock()
            mock_open.return_value = (mock_reader, mock_writer)

            # Mock authentication prompt
            mock_reader.readline.return_value = b"Authentication required\n"

            # Test connection
            result = await tcp_adapter.connect()

            assert result == True
            assert tcp_adapter.is_connected == True
            mock_open.assert_called_once_with("localhost", 8023)

    # Management protocol connect test removed

    @pytest.mark.asyncio
    async def test_authenticate_standard_password(self, tcp_adapter):
        """Test password authentication with standard protocol"""
        # Setup connected adapter
        tcp_adapter.is_connected = True
        tcp_adapter.reader = AsyncMock()
        tcp_adapter.writer = MagicMock()
        tcp_adapter.writer.drain = AsyncMock()  # Mock drain as async

        # Prepare deterministic HMAC challenge
        import base64
        import hashlib
        import hmac

        nonce_raw = b"test-nonce"
        nonce_b64 = base64.b64encode(nonce_raw).decode()
        pw_hash = hashlib.sha256("admin".encode()).digest()
        sig = hmac.new(pw_hash, nonce_raw, hashlib.sha256).digest()
        sig_b64 = base64.b64encode(sig).decode()

        # Mock challenge then success
        tcp_adapter.reader.readline.side_effect = [
            f"AUTH:CHALLENGE:{nonce_b64}\n".encode(),
            b"AUTH:SUCCESS\n",
        ]

        # Test authentication
        result = await tcp_adapter.authenticate_with_password("admin", "admin")

        assert result is True
        assert tcp_adapter.is_authenticated is True
        assert tcp_adapter.username == "admin"

        # Verify HMAC auth sequence
        # First write: initiate HMAC
        first_call = tcp_adapter.writer.write.call_args_list[0][0][0]
        assert first_call == b"AUTH:USER:HMAC:admin\n"
        # Second write: HMAC response with expected signature
        second_call = tcp_adapter.writer.write.call_args_list[1][0][0]
        assert second_call == f"AUTH:RESPONSE:{sig_b64}\n".encode()
        assert tcp_adapter.writer.drain.await_count >= 2

    @pytest.mark.asyncio
    async def test_authenticate_standard_key(self, tcp_adapter):
        """Test API key authentication with standard protocol"""
        # Setup connected adapter
        tcp_adapter.is_connected = True
        tcp_adapter.reader = AsyncMock()
        tcp_adapter.writer = MagicMock()
        tcp_adapter.writer.drain = AsyncMock()  # Mock drain as async

        # Mock successful authentication
        tcp_adapter.reader.readline.return_value = b"Authentication successful\n"

        # Test authentication
        result = await tcp_adapter.authenticate_with_key("test-key-123")

        assert result == True
        assert tcp_adapter.is_authenticated == True
        assert tcp_adapter.username == "api-user"

        # Verify command was sent
        tcp_adapter.writer.write.assert_called_with(b"AUTH:KEY:test-key-123\n")

    @pytest.mark.asyncio
    async def test_list_ports_standard(self, tcp_adapter):
        """Test listing ports with standard protocol"""
        # Setup authenticated adapter
        tcp_adapter.is_connected = True
        tcp_adapter.is_authenticated = True
        tcp_adapter.reader = AsyncMock()
        tcp_adapter.writer = MagicMock()
        tcp_adapter.writer.drain = AsyncMock()  # Mock drain as async

        # Mock port list response
        tcp_adapter.reader.read.return_value = b"Port List:\nconsole1\nconsole2\nconsole3\n"

        # Test listing ports
        ports = await tcp_adapter.list_ports()

        assert ports == ["console1", "console2", "console3"]
        tcp_adapter.writer.write.assert_called_with(b"LIST\n")

    @pytest.mark.asyncio
    async def test_send_data(self, tcp_adapter):
        """Test sending data"""
        # Setup connected adapter
        tcp_adapter.is_connected = True
        tcp_adapter.writer = MagicMock()
        tcp_adapter.writer.drain = AsyncMock()  # Mock drain as async

        # Test sending string data
        result = await tcp_adapter.send_data("test command")

        assert result == True
        tcp_adapter.writer.write.assert_called_with(b"test command")
        tcp_adapter.writer.drain.assert_called_once()

        # Test sending bytes data
        tcp_adapter.writer.reset_mock()
        result = await tcp_adapter.send_data(b"test bytes")

        assert result == True
        tcp_adapter.writer.write.assert_called_with(b"test bytes")

    @pytest.mark.asyncio
    async def test_read_data(self, tcp_adapter):
        """Test reading data"""
        # Setup connected adapter
        tcp_adapter.is_connected = True
        tcp_adapter.reader = AsyncMock()

        # Mock data response
        tcp_adapter.reader.read.return_value = b"test response"

        # Test reading data
        data = await tcp_adapter.read_data()

        assert data == b"test response"
        tcp_adapter.reader.read.assert_called_with(4096)

    @pytest.mark.asyncio
    async def test_close(self, tcp_adapter):
        """Test closing connection"""
        # Setup connected adapter
        tcp_adapter.is_connected = True
        tcp_adapter.writer = MagicMock()
        tcp_adapter.writer.drain = AsyncMock()  # Mock drain as async
        tcp_adapter.writer.close = MagicMock()  # Mock close
        tcp_adapter.writer.wait_closed = AsyncMock()  # Mock wait_closed

        # Store reference to writer for verification
        writer_mock = tcp_adapter.writer

        # Test closing
        await tcp_adapter.close()

        assert tcp_adapter.is_connected == False
        assert tcp_adapter.is_authenticated == False
        writer_mock.write.assert_called_with(b"QUIT\n")
        writer_mock.close.assert_called_once()


class TestWebSocketClientAdapter:
    """Test the WebSocketClientAdapter"""

    @pytest.fixture
    def ws_adapter(self):
        """Create a WebSocket adapter for testing"""
        return WebSocketClientAdapter(
            host="localhost",
            port=8080,
            config={"use_tls": False, "path": "/ws"},
        )

    def test_init(self, ws_adapter):
        """Test WebSocket adapter initialization"""
        assert ws_adapter.host == "localhost"
        assert ws_adapter.port == 8080
        assert ws_adapter.use_tls == False
        assert ws_adapter.path == "/ws"
        assert not ws_adapter.is_connected
        assert not ws_adapter.is_authenticated

    @pytest.mark.asyncio
    async def test_connect(self, ws_adapter):
        """Test WebSocket connection"""
        # Mock the websockets module by patching the specific method call
        with patch("asyncio.wait_for") as mock_wait_for:
            # Create a mock that represents websockets.connect
            mock_websocket = AsyncMock()

            # Import websockets should work, and connect should return our mock
            with patch("websockets.connect", return_value=mock_websocket):
                # Mock wait_for to return our mock websocket
                mock_wait_for.return_value = mock_websocket

                # Test connection
                result = await ws_adapter.connect()

                assert result == True
                assert ws_adapter.is_connected == True

    @pytest.mark.asyncio
    async def test_connect_import_error(self, ws_adapter):
        """Test WebSocket connection with missing websockets library"""
        with patch("builtins.__import__", side_effect=ImportError):
            # Test connection
            result = await ws_adapter.connect()

            assert result == False
            assert not ws_adapter.is_connected
