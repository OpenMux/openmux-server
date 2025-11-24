import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# Import server components
from openmux.server.auth_manager import AuthManager
from openmux.server.config_manager import ConfigManager
from openmux.server.console_manager import ConsoleManager
from openmux.server.port_manager import PortManager
from tests.support.protocol_handler import OpenMuxProtocolHandler as ClientManager


@pytest.fixture
def sample_config():
    """Return a sample configuration for testing"""
    return {
        "server": {"host": "127.0.0.1", "port": 8023},
        "authentication": {
            "users": [
                {
                    "username": "admin",
                    "password_hash": "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8",
                    "permissions": "admin",
                },
                {
                    "username": "user",
                    "password_hash": "e606e38b0d8c19b24cf0ee3808183162ea7cd63ff7912dbb22b5e803286b4446",
                    "permissions": "read-write",
                },
            ],
            "api_keys": [
                {
                    "name": "test_key",
                    "key": "12345abcde",
                    "permissions": "read-only",
                }
            ],
        },
        "serial_ports": [
            {
                "name": "console1",
                "description": "Test Console 1",
                "adapter": "serial",
                "device": "/dev/ttyS0",
                "baudrate": 9600,
                "bytesize": 8,
                "parity": "N",
                "stopbits": 1,
                "max_read_write_users": 1,
            },
            {
                "name": "console2",
                "description": "Test Console 2",
                "adapter": "command",
                "command": "ssh localhost",
                "max_read_write_users": 2,
            },
        ],
    }


@pytest.fixture
def auth_manager(sample_config):
    """Create an auth manager for testing"""
    return AuthManager(sample_config["authentication"])


@pytest.fixture
def config_manager(sample_config, tmp_path):
    """Create a config manager for testing"""
    config_file = tmp_path / "test_config.yaml"
    import yaml

    with open(config_file, "w") as f:
        yaml.dump(sample_config, f)

    manager = ConfigManager(str(config_file))
    manager.load_config()
    return manager


@pytest_asyncio.fixture
async def port_manager(sample_config):
    """Create a port manager for testing (unified-only)"""
    manager = PortManager([])
    yield manager
    # Cleanup: port manager will handle cleanup internally


@pytest_asyncio.fixture
async def console_manager(port_manager, auth_manager):
    """Create a console manager for testing"""
    return ConsoleManager(port_manager, auth_manager)


@pytest_asyncio.fixture
async def client_manager(console_manager, auth_manager):
    """Create a client manager for testing"""
    return ClientManager(console_manager, auth_manager)


class TestAuthManager:
    """Test the AuthManager component"""

    @pytest.mark.unit
    def test_init(self, auth_manager):
        """Test AuthManager initialization"""
        assert auth_manager is not None
        assert len(auth_manager.users) == 2
        assert len(auth_manager.api_keys) == 1

    @pytest.mark.unit
    def test_authenticate_user_valid(self, auth_manager):
        """Test authentication with valid credentials"""
        assert auth_manager.authenticate_user("admin", "password") is True

    @pytest.mark.unit
    def test_authenticate_user_invalid(self, auth_manager):
        """Test authentication with invalid credentials"""
        assert auth_manager.authenticate_user("admin", "wrong_password") is False
        assert auth_manager.authenticate_user("unknown", "password") is False

    @pytest.mark.unit
    def test_authenticate_key_valid(self, auth_manager):
        """Test authentication with valid API key"""
        assert auth_manager.authenticate_key("12345abcde") is True

    @pytest.mark.unit
    def test_authenticate_key_invalid(self, auth_manager):
        """Test authentication with invalid API key"""
        assert auth_manager.authenticate_key("invalid_key") is False

    @pytest.mark.unit
    def test_get_user_permissions(self, auth_manager):
        """Test getting user permissions"""
        assert auth_manager.get_user_permissions("admin") == "admin"
        assert auth_manager.get_user_permissions("user") == "read-write"
        assert auth_manager.get_user_permissions("unknown") is None

    @pytest.mark.unit
    def test_get_key_permissions(self, auth_manager):
        """Test getting API key permissions"""
        assert auth_manager.get_key_permissions("12345abcde") == "read-only"
        assert auth_manager.get_key_permissions("invalid_key") is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_update_config(self, auth_manager):
        """Test updating configuration"""
        new_config = {
            "users": [
                {
                    "username": "admin2",
                    "password_hash": "hash2",
                    "permissions": "admin",
                }
            ],
            "api_keys": [
                {
                    "name": "new_key",
                    "key": "newkey",
                    "permissions": "read-write",
                }
            ],
        }

        await auth_manager.update_config(new_config)
        assert len(auth_manager.users) == 1
        assert len(auth_manager.api_keys) == 1
        assert auth_manager.get_user_permissions("admin2") == "admin"
        assert auth_manager.get_key_permissions("newkey") == "read-write"


class TestConfigManager:
    """Test the ConfigManager component"""

    @pytest.mark.unit
    def test_init(self, config_manager):
        """Test ConfigManager initialization"""
        assert config_manager is not None
        assert config_manager.config_path is not None

    @pytest.mark.unit
    def test_load_config(self, config_manager, sample_config):
        """Test loading configuration"""
        config = config_manager.load_config()
        assert config is not None
        assert "server" in config
        assert "authentication" in config
        assert "serial_ports" in config

    @pytest.mark.unit
    def test_save_config(self, config_manager, tmp_path):
        """Test saving configuration"""
        test_config = {"test": "value"}
        config_file = tmp_path / "save_test_config.yaml"

        config_manager.config_path = str(config_file)
        config_manager.save_config(test_config)

        import yaml

        with open(config_file, "r") as f:
            loaded_config = yaml.safe_load(f)

        assert loaded_config == test_config


@pytest.mark.asyncio
class TestPortManager:
    """Test the PortManager component"""

    @pytest.mark.unit
    async def test_init(self, port_manager):
        """Test PortManager initialization"""
        assert port_manager is not None

    @pytest.mark.unit
    async def test_port_list(self, port_manager):
        """Test getting port list"""
        ports = await port_manager.get_port_list()
        assert isinstance(ports, list)

    @pytest.mark.unit
    async def test_port_exists(self, port_manager):
        """Test checking if port exists"""
        # With no unified adapters, no ports exist
        assert port_manager.port_exists("console1") is False
        assert port_manager.port_exists("console2") is False
        assert port_manager.port_exists("nonexistent") is False


@pytest.mark.asyncio
class TestConsoleManager:
    """Test the ConsoleManager component"""

    @pytest.mark.unit
    async def test_init(self, console_manager, port_manager, auth_manager):
        """Test ConsoleManager initialization"""
        assert console_manager is not None
        assert console_manager.port_manager == port_manager
        assert console_manager.auth_manager == auth_manager
        assert console_manager.console_clients == {}

    @pytest.mark.unit
    async def test_list_consoles(self, console_manager, port_manager):
        """Test listing consoles"""
        consoles = await console_manager.list_consoles()
        assert isinstance(consoles, list)
        assert len(consoles) == 0  # No unified ports configured

        # Check that console structure is correct
        if len(consoles) > 0:
            console = consoles[0]
            assert "name" in console
            assert "description" in console
            assert "is_connected" in console
            assert "clients" in console

    @pytest.mark.unit
    async def test_connect_client(self, console_manager, port_manager):
        """Test connecting a client to a console"""
        # Create mock client
        client = MagicMock()
        client.username = "user1"
        client.permissions = "read-write"

        result = await console_manager.connect_client(client, "console1")
        assert isinstance(result, bool)
        # Since we're testing with mock adapters that don't really connect,
        # we just verify the method doesn't crash

    @pytest.mark.unit
    async def test_disconnect_client(self, console_manager, port_manager):
        """Test disconnecting a client from a console"""
        # Create mock client
        client = MagicMock()
        client.username = "user1"

        # Add client to console first
        console_manager.console_clients["console1"] = [client]

        await console_manager.disconnect_client(client)
        # Just verify the method doesn't crash and removes the client
        assert "console1" not in console_manager.console_clients or client not in console_manager.console_clients.get(
            "console1", []
        )


@pytest.mark.asyncio
class TestClientManager:
    """Test the ClientManager component"""

    @pytest.mark.unit
    async def test_init(self, client_manager, console_manager, auth_manager):
        """Test ClientManager initialization"""
        assert client_manager is not None
        assert client_manager.console_manager == console_manager
        assert client_manager.auth_manager == auth_manager
        assert client_manager.clients == []

    @pytest.mark.unit
    async def test_handle_new_connection(self, client_manager):
        """Test handling a new connection"""
        # Create mock reader and writer
        reader = AsyncMock()
        writer = AsyncMock()
        writer.get_extra_info = MagicMock(return_value=("127.0.0.1", 12345))

        # Patch the handle_client method
        with patch.object(client_manager, "handle_client", new_callable=AsyncMock) as mock_handle_client:
            await client_manager.handle_new_connection(reader, writer)
            mock_handle_client.assert_called_once()

    @pytest.mark.unit
    async def test_close_all_connections(self, client_manager):
        """Test closing all connections"""
        # Create mock clients
        client1 = AsyncMock()
        client2 = AsyncMock()

        client_manager.clients = [client1, client2]

        await client_manager.close_all_connections()

        # Verify that close was called on both clients
        client1.close.assert_called_once()
        client2.close.assert_called_once()
