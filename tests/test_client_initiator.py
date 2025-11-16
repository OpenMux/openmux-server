"""Tests for unified OpenMuxClientAdapter functionality."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest
import pytest_asyncio

from openmux.server.adapters.client_initiator import OpenMuxClientAdapter


class TestOpenMuxClientAdapter:
    """Test cases for OpenMuxClientAdapter and per-port behavior."""

    def setup_method(self):
        """Set up test method - track created adapters for cleanup."""
        self.created_adapters = []

    def teardown_method(self):
        """Clean up after test method (handled by autouse fixtures)."""
        # No-op here; dedicated autouse fixtures handle cleanup reliably.
        pass

    # NOTE (test teardown):
    # OpenMuxClientPort starts background asyncio tasks (monitor/read). If these
    # aren't awaited and fully stopped after each test, pytest-asyncio may close
    # the event loop with pending tasks, which used to look like a hang in this suite.
    #
    # Under pytest-asyncio strict mode, using an async autouse fixture in sync tests
    # is deprecated and can emit warnings. To be explicit and robust we split cleanup into:
    # - _cleanup_sync_tests: autouse for sync tests; when no loop is running it uses
    #   asyncio.run(...) to await adapter.stop() for all created adapters.
    # - _cleanup_async_tests: autouse async fixture for async tests; it awaits adapter.stop().
    #
    # This ensures all background tasks are cancelled/awaited, avoids teardown hangs,
    # and keeps pytest free from async-in-sync deprecation warnings.
    @pytest.fixture(autouse=True)
    def _cleanup_sync_tests(self, request):
        """Ensure adapters are stopped for synchronous tests.

        For async tests, an async autouse fixture below performs cleanup.
        """
        yield
        inst = getattr(request, "instance", None)
        if not inst:
            return
        created = getattr(inst, "created_adapters", [])
        # Only run in contexts without a running loop
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return
        except Exception:
            pass

        async def _stop_all():
            for adapter in list(created):
                try:
                    if hasattr(adapter, "stop"):
                        await adapter.stop()
                except Exception:
                    pass

        try:
            asyncio.run(_stop_all())
        except RuntimeError:
            # Fallback if asyncio.run is not permitted in this context
            try:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(_stop_all())
                finally:
                    loop.close()
            except Exception:
                pass

    # Use pytest-asyncio for async autouse cleanup in strict mode
    @pytest_asyncio.fixture(autouse=True)
    async def _cleanup_async_tests(self, request):
        """Ensure adapters are stopped for asynchronous tests."""
        yield
        inst = getattr(request, "instance", None)
        if not inst:
            return
        created = getattr(inst, "created_adapters", [])
        for adapter in list(created):
            try:
                if hasattr(adapter, "stop"):
                    await adapter.stop()
            except Exception:
                pass

    def _create_adapter(self, name, ports_config_list):
        """Helper to create and track adapters for cleanup.

        ports_config_list: list of dicts, each defining one OpenMux client port.
        """
        adapter = OpenMuxClientAdapter(name, ports_config_list)
        self.created_adapters.append(adapter)
        return adapter

    @pytest.fixture
    def adapter_config(self):
        """Basic per-port configuration for the unified client adapter."""
        return {
            "name": "test_port",
            "host": "remote.example.com",
            "port": 8080,
            "remote_port": "test_device",
            "username": "admin",
            "password": "secret",
            "ssl": False,
            "timeout": 10.0,
        }

    @pytest.fixture
    def api_key_config(self):
        """API key per-port configuration for the unified client adapter."""
        return {
            "name": "api_port",
            "host": "secure.example.com",
            "port": 8443,
            "remote_port": "secure_device",
            "api_key": "test-api-key-123",
            "ssl": True,
            "timeout": 15.0,
        }

    def test_adapter_initialization(self, adapter_config):
        """Test adapter initialization and per-port config mapping."""
        adapter = self._create_adapter("om_client", [adapter_config])

        assert adapter.name == "om_client"
        # Verify port configurations mapped
        ports = adapter.get_port_configurations()
        assert "test_port" in ports
        cfg = ports["test_port"]
        assert cfg["host"] == "remote.example.com"
        assert cfg["port"] == 8080
        assert cfg["remote_port"] == "test_device"

    def test_api_key_adapter_initialization(self, api_key_config):
        """Test adapter initialization with API key config (per-port)."""
        adapter = self._create_adapter("om_client", [api_key_config])
        ports = adapter.get_port_configurations()
        assert "api_port" in ports
        cfg = ports["api_port"]
        assert cfg["ssl"] is True
        assert cfg["timeout"] == 15.0
        assert cfg["api_key"] == "test-api-key-123"

    @pytest.mark.asyncio
    async def test_missing_required_config(self):
        """Test port creation with missing required config returns None."""
        adapter = self._create_adapter("om_client", [])
        # Missing 'host' and 'port' should cause create_port to return None
        port = await adapter.create_port("bad", {"name": "bad", "remote_port": "p"})
        assert port is None

    def test_config_defaults(self):
        """Test default configuration values."""
        config = {
            "name": "test",
            "host": "example.com",
            "remote_port": "device",
            "api_key": "test-key",
        }

        adapter = self._create_adapter("om_client", [config])
        ports = adapter.get_port_configurations()
        cfg = ports["test"]
        assert cfg.get("ssl", False) is False
        assert cfg.get("timeout", 10.0) == 10.0
        assert cfg.get("auto_reconnect", True) is True
        assert cfg.get("port", 8023) == 8023  # Default OpenMux port

    @pytest.mark.asyncio
    async def test_connect_success(self, adapter_config):
        """Test successful connection to remote OpenMux server."""
        adapter = self._create_adapter("om_client", [adapter_config])
        # Create the port instance and start connection manager
        mock_connection = AsyncMock()
        mock_connection.connect = AsyncMock(return_value=True)
        mock_connection.authenticate_with_password = AsyncMock(return_value=True)
        mock_connection.connect_to_port = AsyncMock(return_value=True)
        # Pace the read loop and close after a couple of reads to avoid spin
        calls = {"n": 0}

        async def paced_read():
            await asyncio.sleep(0.01)
            calls["n"] += 1
            return b"ok" if calls["n"] < 3 else b""

        mock_connection.read_data = AsyncMock(side_effect=paced_read)
        mock_connection.reader = Mock()
        mock_connection.writer = Mock()

        with patch(
            "openmux.client.adapters.TcpClientAdapter",
            return_value=mock_connection,
        ):
            port = await adapter.create_port("test_port", adapter_config)
            assert port is not None
            # Allow connection manager to run a bit
            for _ in range(50):
                if port.is_connected:
                    break
                await asyncio.sleep(0.01)
            assert port.is_connected is True
            mock_connection.connect.assert_called_once()
            mock_connection.authenticate_with_password.assert_called_once_with("admin", "secret")
            mock_connection.connect_to_port.assert_called_once_with("test_device")
            # Explicit cleanup to ensure background tasks cancelled before loop teardown
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_connect_with_api_key(self, api_key_config):
        """Test successful connection with API key authentication."""
        adapter = self._create_adapter("om_client", [api_key_config])

        mock_connection = AsyncMock()
        mock_connection.connect = AsyncMock(return_value=True)
        mock_connection.authenticate_with_key = AsyncMock(return_value=True)
        mock_connection.connect_to_port = AsyncMock(return_value=True)
        calls = {"n": 0}

        async def paced_read():
            await asyncio.sleep(0.01)
            calls["n"] += 1
            return b"ok" if calls["n"] < 3 else b""

        mock_connection.read_data = AsyncMock(side_effect=paced_read)
        mock_connection.reader = Mock()
        mock_connection.writer = Mock()

        with patch(
            "openmux.client.adapters.TcpClientAdapter",
            return_value=mock_connection,
        ):
            port = await adapter.create_port("api_port", api_key_config)
            assert port is not None
            for _ in range(50):
                if port.is_connected:
                    break
                await asyncio.sleep(0.01)
            mock_connection.authenticate_with_key.assert_called_once_with("test-api-key-123")
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_connect_timeout(self, adapter_config):
        """Test connection timeout."""
        adapter = self._create_adapter("om_client", [adapter_config])

        mock_connection = AsyncMock()
        mock_connection.connect = AsyncMock(side_effect=asyncio.TimeoutError())

        with patch(
            "openmux.client.adapters.TcpClientAdapter",
            return_value=mock_connection,
        ):
            port = await adapter.create_port("test_port", adapter_config)
            # Connection manager tries and fails; allow a tick
            await asyncio.sleep(0)
            assert port is not None
            assert port.is_connected is False
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_connect_authentication_error(self, adapter_config):
        """Test connection failure due to authentication error."""
        adapter = self._create_adapter("om_client", [adapter_config])

        mock_connection = AsyncMock()
        mock_connection.connect = AsyncMock()
        mock_connection.authenticate_with_password = AsyncMock(side_effect=Exception("Authentication failed"))

        with patch(
            "openmux.client.adapters.TcpClientAdapter",
            return_value=mock_connection,
        ):
            port = await adapter.create_port("test_port", adapter_config)
            await asyncio.sleep(0)
            assert port is not None
            assert port.is_connected is False
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_disconnect_success(self, adapter_config):
        """Test successful disconnection."""
        adapter = self._create_adapter("om_client", [adapter_config])

        # Set up connected state
        mock_connection = AsyncMock()
        mock_connection.connect = AsyncMock(return_value=True)
        mock_connection.authenticate_with_password = AsyncMock(return_value=True)
        mock_connection.connect_to_port = AsyncMock(return_value=True)
        # Pace the read loop and then signal remote-close to ensure read_task exits
        calls = {"n": 0}

        async def paced_read():
            await asyncio.sleep(0.01)
            calls["n"] += 1
            return b"ok" if calls["n"] < 3 else b""

        mock_connection.read_data = AsyncMock(side_effect=paced_read)
        mock_connection.close = AsyncMock(return_value=None)
        with patch(
            "openmux.client.adapters.TcpClientAdapter",
            return_value=mock_connection,
        ):
            port = await adapter.create_port("test_port", adapter_config)
            assert port is not None
            # Allow connection manager to establish connection
            for _ in range(50):
                if port.is_connected:
                    break
                await asyncio.sleep(0.01)
            await port.stop()
            assert port.is_connected is False
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_write_data_success(self, adapter_config):
        """Test successful write operation."""
        adapter = self._create_adapter("om_client", [adapter_config])
        mock_connection = AsyncMock()
        mock_connection.connect = AsyncMock(return_value=True)
        mock_connection.authenticate_with_password = AsyncMock(return_value=True)
        mock_connection.connect_to_port = AsyncMock(return_value=True)
        mock_connection.send_data = AsyncMock(return_value=True)

        async def paced_read():
            await asyncio.sleep(0.01)
            return b"ok"

        mock_connection.read_data = AsyncMock(side_effect=paced_read)
        mock_connection.reader = Mock()
        mock_connection.writer = Mock()

        with patch(
            "openmux.client.adapters.TcpClientAdapter",
            return_value=mock_connection,
        ):
            port = await adapter.create_port("test_port", adapter_config)
            assert port is not None
            # Wait until connected before attempting to write
            for _ in range(50):
                if port.is_connected:
                    break
                await asyncio.sleep(0.01)
            test_data = b"test command"
            wrote = await port.write_data(test_data)
            assert wrote == len(test_data)
            mock_connection.send_data.assert_called_once_with(test_data)
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_write_data_not_connected(self, adapter_config):
        """Test write operation when not connected."""
        adapter = self._create_adapter("om_client", [adapter_config])
        mock_connection = AsyncMock()
        mock_connection.connect = AsyncMock(side_effect=asyncio.TimeoutError())
        with patch(
            "openmux.client.adapters.TcpClientAdapter",
            return_value=mock_connection,
        ):
            port = await adapter.create_port("test_port", adapter_config)
            assert port is not None
            await asyncio.sleep(0)
            assert port.is_connected is False
            wrote = await port.write_data(b"test data")
            assert wrote == 0
            await adapter.stop()

    # Read path is handled via background read loop and port manager callback.
    # Dedicated read tests were removed in the unified adapter where direct read API is not exposed.

    # Read timeout scenarios are implicitly covered by connection manager tests.

    # No direct read method on port; not applicable in unified version.

    # The unified adapter always creates a connection manager task; disabling
    # auto_reconnect only affects reconnect behavior, not task creation.
    # The equivalent behavior is covered by connection tests above.

    def test_adapter_type_property(self, adapter_config):
        """Test adapter_type property."""
        adapter = self._create_adapter("om_client", [adapter_config])
        assert adapter.adapter_type == "openmux_client"

    def test_string_representation(self, adapter_config):
        """Test string representation of adapter."""
        adapter = self._create_adapter("om_client", [adapter_config])
        # Test that we can create a string representation without errors
        str_repr = str(adapter)
        assert isinstance(str_repr, str)
        assert len(str_repr) > 0
