import asyncio
import base64
import json
import os
from unittest.mock import MagicMock

import pytest
from aiohttp import ClientSession, TCPConnector, WSMsgType

from openmux.server.adapters.web_console import WebConsoleAdapter
from openmux.server.auth_manager import AuthManager
from openmux.server.console_manager import ConsoleManager
from openmux.server.port_manager import PortManager


@pytest.mark.asyncio
async def test_probes_plain_text(tmp_path):
    # Minimal config
    config = {
        "web_console": {
            "host": "127.0.0.1",
            "port": 8901,
            "enable_ui": False,
            "enable_probes": True,
            "probes_include_details": False,
        }
    }
    adapter = WebConsoleAdapter("wc", config)
    # Fake managers
    auth = AuthManager({"users": []})
    pm = PortManager([])
    cm = ConsoleManager(pm, auth)
    adapter.set_auth_manager(auth)
    adapter.set_console_manager(cm)
    started = await adapter.start()
    assert started

    async with ClientSession(connector=TCPConnector(ssl=False)) as session:
        async with session.get("http://127.0.0.1:8901/healthz") as resp:
            assert resp.status == 200
            text = await resp.text()
            assert text.strip() == "ok"
        async with session.get("http://127.0.0.1:8901/livez") as resp:
            assert resp.status == 200
            assert (await resp.text()).strip() == "live"
        # readyz requires auth
        async with session.get("http://127.0.0.1:8901/readyz") as resp:
            assert resp.status in (401, 403)

    await adapter.stop()


@pytest.mark.asyncio
async def test_probes_detailed_json(tmp_path):
    config = {
        "web_console": {
            "host": "127.0.0.1",
            "port": 8902,
            "enable_ui": False,
            "enable_probes": True,
            "probes_include_details": True,
        }
    }
    adapter = WebConsoleAdapter("wc", config)
    auth = AuthManager(
        {"users": [{"username": "u", "password_hash": "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"}]}
    )
    pm = PortManager([])
    cm = ConsoleManager(pm, auth)
    adapter.set_auth_manager(auth)
    adapter.set_console_manager(cm)
    assert await adapter.start()

    async with ClientSession(connector=TCPConnector(ssl=False)) as session:
        async with session.get("http://127.0.0.1:8902/healthz") as resp:
            assert resp.status == 200
            data = json.loads(await resp.text())
            assert data["component"] == "web_console"
            assert "uptime_seconds" in data
        async with session.get("http://127.0.0.1:8902/livez") as resp:
            assert resp.status == 200
            ldata = json.loads(await resp.text())
            assert ldata["status"] == "ok"
        # readyz with auth header
        token = base64.b64encode(b"u:password").decode()
        headers = {"Authorization": f"Basic {token}"}
        async with session.get("http://127.0.0.1:8902/readyz", headers=headers) as resp:
            body = await resp.text()
            if resp.status == 200:
                rdata = json.loads(body)
                assert rdata.get("ready") in (True, False)
            else:
                # If auth mismatch, should be 401
                assert resp.status == 401

    await adapter.stop()


@pytest.mark.asyncio
async def test_probes_disabled(tmp_path):
    config = {
        "web_console": {
            "host": "127.0.0.1",
            "port": 8903,
            "enable_ui": False,
            "enable_probes": False,
        }
    }
    adapter = WebConsoleAdapter("wc", config)
    auth = AuthManager({"users": []})
    pm = PortManager([])
    cm = ConsoleManager(pm, auth)
    adapter.set_auth_manager(auth)
    adapter.set_console_manager(cm)
    assert await adapter.start()

    async with ClientSession(connector=TCPConnector(ssl=False)) as session:
        for path in ("healthz", "livez"):
            async with session.get(f"http://127.0.0.1:8903/{path}") as resp:
                # Should be 404 because probes disabled (no route registered, middleware bypasses only if probes enabled)
                assert resp.status == 404
        # readyz path still requires auth (middleware not bypassed) so expect 401 when probes disabled
        async with session.get("http://127.0.0.1:8903/readyz") as resp:
            assert resp.status in (401, 404)

    await adapter.stop()


@pytest.mark.asyncio
async def test_websocket_connect_and_send(tmp_path):
    """Verify a WebSocket session can connect and send data while probes work."""

    # Create a dummy port in port manager with minimal interface
    class DummyPort:
        def __init__(self, name):
            self.name = name
            self.description = "dummy"
            self.connected_clients = []
            self.max_read_write_users = 5
            self.is_running = True

        async def write_data(self, data):
            # store last write for assertion
            self.last_write = data
            return len(data)

        def get_status(self):
            return {"name": self.name, "is_running": True}

    dummy = DummyPort("loopback_ws1")
    pm = PortManager([])
    # Inject dummy port into manager (bypassing creation path for test)
    pm.ports["loopback_ws1"] = dummy  # type: ignore[attr-defined]

    config = {
        "web_console": {
            "host": "127.0.0.1",
            "port": 8904,
            "enable_ui": False,
            "enable_probes": True,
            "probes_include_details": True,
        }
    }
    auth = AuthManager(
        {"users": [{"username": "u", "password_hash": "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"}]}
    )
    cm = ConsoleManager(pm, auth)
    adapter = WebConsoleAdapter("wc", config)
    adapter.set_auth_manager(auth)
    adapter.set_console_manager(cm)
    assert await adapter.start()

    token = base64.b64encode(b"u:password").decode()
    headers = {"Authorization": f"Basic {token}"}

    async with ClientSession(connector=TCPConnector(ssl=False)) as session:
        # Ready should reflect console_manager / port_manager presence
        async with session.get("http://127.0.0.1:8904/readyz", headers=headers) as resp:
            assert resp.status == 200
            data = json.loads(await resp.text())
            assert data.get("port_manager") is True
        # Connect WS
        async with session.ws_connect("http://127.0.0.1:8904/ws/loopback_ws1", headers=headers) as ws:
            await ws.send_str("hello")
            # Allow server to process write
            await asyncio.sleep(0.05)
            assert getattr(dummy, "last_write", None) == b"hello"

    await adapter.stop()
