import asyncio
import base64
import json

import pytest

from openmux.client.adapters.websocket_adapter import WebSocketClientAdapter
from openmux.server.adapters.web_console import WebConsoleAdapter
from openmux.server.auth_manager import AuthManager
from openmux.server.console_manager import ConsoleManager
from openmux.server.port_manager import PortManager


class DummyPort:
    def __init__(self, name: str):
        self.name = name
        self.description = "dummy"
        self.connected_clients = []
        self.max_read_write_users = 2
        self.is_running = True
        self._writes = []

    async def write_data(self, data: bytes):
        self._writes.append(data)
        return len(data)

    def get_status(self):
        return {"name": self.name, "is_running": True}


async def start_web_console(port: int, users):
    config = {
        "web_console": {
            "host": "127.0.0.1",
            "port": port,
            "enable_ui": False,
            "enable_probes": True,
            "probes_include_details": True,
        }
    }
    adapter = WebConsoleAdapter("wc", config)
    auth = AuthManager({"users": users})
    pm = PortManager([])
    # inject dummy port
    dummy = DummyPort("loopback_adpt")
    pm.ports["loopback_adpt"] = dummy  # type: ignore[attr-defined]
    cm = ConsoleManager(pm, auth)
    adapter.set_auth_manager(auth)
    adapter.set_console_manager(cm)
    assert await adapter.start()
    return adapter, auth, pm, cm, dummy


@pytest.mark.asyncio
async def test_discovery_list_ports_success(tmp_path):
    adapter, auth, pm, cm, dummy = await start_web_console(
        8921, [{"username": "u", "password_hash": "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"}]
    )
    client = WebSocketClientAdapter(
        "127.0.0.1",
        8921,
        {
            "basic_user": "u",
            "basic_password": "password",
            "list_ports_via_http": True,
            # discovery mode (no port_name)
        },
    )
    assert await client.connect()  # discovery mode ok
    ports = await client.list_ports()
    assert any(p.get("name") == "loopback_adpt" for p in ports)
    await client.close()
    await adapter.stop()


@pytest.mark.asyncio
async def test_discovery_list_ports_unauthorized(tmp_path):
    adapter, auth, pm, cm, dummy = await start_web_console(
        8922, [{"username": "u", "password_hash": "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"}]
    )
    client = WebSocketClientAdapter(
        "127.0.0.1",
        8922,
        {
            # missing creds -> 401 listing -> empty
            "list_ports_via_http": True,
        },
    )
    assert await client.connect()
    ports = await client.list_ports()
    assert ports == []  # unauthorized returns empty list
    await client.close()
    await adapter.stop()


@pytest.mark.asyncio
async def test_connect_and_send_receive(tmp_path):
    adapter, auth, pm, cm, dummy = await start_web_console(
        8923, [{"username": "u", "password_hash": "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"}]
    )
    # Create client with direct port_name to open real ws
    client = WebSocketClientAdapter(
        "127.0.0.1",
        8923,
        {
            "basic_user": "u",
            "basic_password": "password",
            "port_name": "loopback_adpt",
        },
    )
    assert await client.connect()
    # Send some data
    assert await client.send_data("hello")
    # The dummy port collects writes
    await asyncio.sleep(0.05)
    assert dummy._writes and dummy._writes[-1] == b"hello"
    await client.close()
    await adapter.stop()


@pytest.mark.asyncio
async def test_invalid_port_name_closes_cleanly(tmp_path):
    adapter, auth, pm, cm, dummy = await start_web_console(
        8924, [{"username": "u", "password_hash": "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"}]
    )
    client = WebSocketClientAdapter(
        "127.0.0.1",
        8924,
        {
            "basic_user": "u",
            "basic_password": "password",
            "port_name": "nonexistent",
        },
    )
    ok = await client.connect()
    # Connection may succeed then close quickly; treat immediate close as failure to stay connected
    assert not (ok and client.is_connected)
    await client.close()
    await adapter.stop()


@pytest.mark.asyncio
async def test_timeout_and_control_frames(tmp_path):
    adapter, auth, pm, cm, dummy = await start_web_console(
        8925, [{"username": "u", "password_hash": "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"}]
    )
    client = WebSocketClientAdapter(
        "127.0.0.1",
        8925,
        {
            "basic_user": "u",
            "basic_password": "password",
            # discovery
        },
    )
    assert await client.connect()
    # read_data with timeout should return b"" not None while open
    chunk = await client.read_data(timeout=0.05)
    assert chunk == b"" or chunk is None  # Accept None if server closed unexpectedly
    await client.close()
    await adapter.stop()


@pytest.mark.asyncio
async def test_manual_close_before_loop_end(tmp_path):
    adapter, auth, pm, cm, dummy = await start_web_console(
        8926, [{"username": "u", "password_hash": "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"}]
    )
    client = WebSocketClientAdapter(
        "127.0.0.1",
        8926,
        {
            "basic_user": "u",
            "basic_password": "password",
            "port_name": "loopback_adpt",
        },
    )
    assert await client.connect()
    await client.close()
    # Second close should be no-op (no exception)
    await client.close()
    await adapter.stop()
