"""Lifecycle tests for TcpInitiatorAdapter with openmux protocol.

Replaces the old OpenMuxClientAdapter tests. The adapter now uses
TcpInitiatorPort + OpenMuxHandler internally for openmux connections.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from openmux.server.adapters.tcp_initiator import TcpInitiatorAdapter


def _openmux_cfg(name, host, port, remote_port, username=None, password=None, api_key=None, **kwargs):
    """Build a tcp_initiator_ports entry for an openmux connection."""
    return {
        "name": name,
        "host": host,
        "port": port,
        "protocol": {
            "type": "openmux",
            "remote_port": remote_port,
            **({"api_key": api_key} if api_key else {}),
            **({"username": username, "password": password} if username else {}),
        },
        **kwargs,
    }


def _make_mock_conn():
    """Return a mock TcpClientAdapter with reader/writer set up for TcpInitiatorPort."""
    mock = AsyncMock()
    mock.connect = AsyncMock(return_value=True)
    mock.authenticate_with_password = AsyncMock(return_value=True)
    mock.authenticate_with_key = AsyncMock(return_value=True)
    mock.connect_to_port = AsyncMock(return_value=True)

    reader = MagicMock()
    reader.read = AsyncMock(return_value=b"")

    writer = MagicMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    writer.write = MagicMock()
    writer.drain = AsyncMock()

    mock.reader = reader
    mock.writer = writer
    return mock


class TestTcpInitiatorOpenmux:
    """Lifecycle tests for TcpInitiatorAdapter using openmux protocol."""

    def setup_method(self):
        self.created_adapters = []

    @pytest_asyncio.fixture(autouse=True)
    async def _cleanup(self, request):
        yield
        inst = getattr(request, "instance", None)
        if not inst:
            return
        for adapter in list(getattr(inst, "created_adapters", [])):
            try:
                await adapter.stop()
            except Exception:
                pass

    def _create_adapter(self, name, ports):
        adapter = TcpInitiatorAdapter(name, {"tcp_initiator_ports": ports})
        self.created_adapters.append(adapter)
        return adapter

    @pytest.fixture
    def user_pass_cfg(self):
        return _openmux_cfg("test_port", "remote.example.com", 8080, "test_device",
                            username="admin", password="secret", timeout=10.0)

    @pytest.fixture
    def api_key_cfg(self):
        return _openmux_cfg("api_port", "secure.example.com", 8443, "secure_device",
                            api_key="test-api-key-123", timeout=15.0)

    def test_adapter_initialization(self, user_pass_cfg):
        adapter = self._create_adapter("om_client", [user_pass_cfg])
        ports = adapter.get_port_configurations()
        assert "test_port" in ports
        cfg = ports["test_port"]
        assert cfg["host"] == "remote.example.com"
        assert cfg["port"] == 8080
        assert cfg["protocol"]["remote_port"] == "test_device"

    def test_api_key_adapter_initialization(self, api_key_cfg):
        adapter = self._create_adapter("om_client", [api_key_cfg])
        ports = adapter.get_port_configurations()
        assert "api_port" in ports
        cfg = ports["api_port"]
        assert cfg["timeout"] == 15.0
        assert cfg["protocol"]["api_key"] == "test-api-key-123"

    @pytest.mark.asyncio
    async def test_missing_required_config(self):
        adapter = self._create_adapter("om_client", [])
        port = await adapter.create_port("bad", {"name": "bad", "remote_port": "p"})
        assert port is None

    def test_config_defaults(self):
        cfg = _openmux_cfg("test", "example.com", 8023, "device", api_key="k")
        adapter = self._create_adapter("om_client", [cfg])
        ports = adapter.get_port_configurations()
        c = ports["test"]
        assert c.get("use_tls", False) is False
        assert c.get("timeout", 10.0) == 10.0
        assert c.get("auto_reconnect", True) is True

    @pytest.mark.asyncio
    async def test_connect_success(self, user_pass_cfg):
        adapter = self._create_adapter("om_client", [user_pass_cfg])
        mock_conn = _make_mock_conn()
        calls = {"n": 0}

        async def paced_read(_n):
            await asyncio.sleep(0.01)
            calls["n"] += 1
            return b"ok" if calls["n"] < 3 else b""

        mock_conn.reader.read = AsyncMock(side_effect=paced_read)

        with patch("openmux.client.adapters.TcpClientAdapter", return_value=mock_conn):
            port = await adapter.create_port("test_port", user_pass_cfg)
            assert port is not None
            for _ in range(50):
                if port.is_connected:
                    break
                await asyncio.sleep(0.01)
            assert port.is_connected is True
            mock_conn.connect.assert_called_once()
            mock_conn.authenticate_with_password.assert_called_once_with("admin", "secret")
            mock_conn.connect_to_port.assert_called_once_with("test_device")
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_connect_with_api_key(self, api_key_cfg):
        adapter = self._create_adapter("om_client", [api_key_cfg])
        mock_conn = _make_mock_conn()
        calls = {"n": 0}

        async def paced_read(_n):
            await asyncio.sleep(0.01)
            calls["n"] += 1
            return b"ok" if calls["n"] < 3 else b""

        mock_conn.reader.read = AsyncMock(side_effect=paced_read)

        with patch("openmux.client.adapters.TcpClientAdapter", return_value=mock_conn):
            port = await adapter.create_port("api_port", api_key_cfg)
            assert port is not None
            for _ in range(50):
                if port.is_connected:
                    break
                await asyncio.sleep(0.01)
            mock_conn.authenticate_with_key.assert_called_once_with("test-api-key-123")
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_connect_timeout(self, user_pass_cfg):
        # Use a small timeout and a connect mock that sleeps longer than it,
        # so asyncio.wait_for genuinely fires instead of relying on the mock
        # raising TimeoutError directly (which confuses wait_for internals).
        cfg = {**user_pass_cfg, "timeout": 0.05, "auto_reconnect": False}
        adapter = self._create_adapter("om_client", [cfg])
        mock_conn = _make_mock_conn()

        async def slow_connect():
            await asyncio.sleep(5.0)  # longer than timeout=0.05
            return True

        mock_conn.connect = AsyncMock(side_effect=slow_connect)

        with patch("openmux.client.adapters.TcpClientAdapter", return_value=mock_conn):
            port = await adapter.create_port("test_port", cfg)
            await asyncio.sleep(0.2)  # enough for the 0.05s timeout to fire
            assert port is not None
            assert port.is_connected is False
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_connect_auth_failure(self, user_pass_cfg):
        # Disable reconnect so the connection manager exits after one attempt.
        cfg = {**user_pass_cfg, "auto_reconnect": False}
        adapter = self._create_adapter("om_client", [cfg])
        mock_conn = _make_mock_conn()
        mock_conn.authenticate_with_password = AsyncMock(return_value=False)

        with patch("openmux.client.adapters.TcpClientAdapter", return_value=mock_conn):
            port = await adapter.create_port("test_port", cfg)
            await asyncio.sleep(0.05)
            assert port is not None
            assert port.is_connected is False
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_disconnect(self, user_pass_cfg):
        adapter = self._create_adapter("om_client", [user_pass_cfg])
        mock_conn = _make_mock_conn()
        calls = {"n": 0}

        async def paced_read(_n):
            await asyncio.sleep(0.01)
            calls["n"] += 1
            return b"ok" if calls["n"] < 3 else b""

        mock_conn.reader.read = AsyncMock(side_effect=paced_read)

        with patch("openmux.client.adapters.TcpClientAdapter", return_value=mock_conn):
            port = await adapter.create_port("test_port", user_pass_cfg)
            for _ in range(50):
                if port.is_connected:
                    break
                await asyncio.sleep(0.01)
            await port.stop()
            assert port.is_connected is False
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_write_data_success(self, user_pass_cfg):
        adapter = self._create_adapter("om_client", [user_pass_cfg])
        mock_conn = _make_mock_conn()
        calls = {"n": 0}

        async def paced_read(_n):
            await asyncio.sleep(0.01)
            calls["n"] += 1
            return b"ok" if calls["n"] < 3 else b""

        mock_conn.reader.read = AsyncMock(side_effect=paced_read)

        with patch("openmux.client.adapters.TcpClientAdapter", return_value=mock_conn):
            port = await adapter.create_port("test_port", user_pass_cfg)
            for _ in range(50):
                if port.is_connected:
                    break
                await asyncio.sleep(0.01)
            wrote = await port.write_data(b"test command")
            assert wrote == len(b"test command")
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_write_data_not_connected(self, user_pass_cfg):
        cfg = {**user_pass_cfg, "timeout": 0.05, "auto_reconnect": False}
        adapter = self._create_adapter("om_client", [cfg])
        mock_conn = _make_mock_conn()

        async def slow_connect():
            await asyncio.sleep(5.0)
            return True

        mock_conn.connect = AsyncMock(side_effect=slow_connect)

        with patch("openmux.client.adapters.TcpClientAdapter", return_value=mock_conn):
            port = await adapter.create_port("test_port", cfg)
            await asyncio.sleep(0.2)
            assert port.is_connected is False
            wrote = await port.write_data(b"test data")
            assert wrote == 0
            await adapter.stop()

    def test_adapter_type(self, user_pass_cfg):
        adapter = self._create_adapter("om_client", [user_pass_cfg])
        assert adapter.get_adapter_type() == "tcp_initiator"

    def test_string_representation(self, user_pass_cfg):
        adapter = self._create_adapter("om_client", [user_pass_cfg])
        assert isinstance(str(adapter), str)
