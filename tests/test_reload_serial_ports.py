import asyncio
import base64
import json

import pytest
from aiohttp import ClientSession, TCPConnector

from openmux.server.adapters.serial import SerialAdapter
from openmux.server.auth_manager import AuthManager
from openmux.server.console_manager import ConsoleManager
from openmux.server.port_manager import PortManager
from openmux.server.web_console import WebConsoleAdapter


@pytest.mark.asyncio
async def test_reload_serial_ports_incremental(tmp_path):
    # Start WebConsole + SerialAdapter in-process and wire them together
    wc_cfg = {
        "web_console": {
            "host": "127.0.0.1",
            "port": 8910,
            "enable_ui": False,
            "enable_probes": True,
            "probes_include_details": True,
        }
    }
    auth = AuthManager(
        {
            "users": [
                {
                    "username": "admin",
                    "password_hash": "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8",
                    "permissions": "admin",
                }
            ]
        }
    )

    # Prepare port manager and console manager
    pm = PortManager([])
    cm = ConsoleManager(pm, auth)

    # Create serial adapter with an initial config of one port
    ser_cfg = {"serial_ports": [{"name": "consoleA", "description": "A", "device": "/dev/null", "baudrate": 9600}]}
    serial = SerialAdapter("serial_ports", ser_cfg)
    # Wire dependencies (like main does)
    serial.main_port_manager = pm

    # Register adapter in pm.unified_adapters so web_console can find it
    pm.set_unified_adapters([serial])

    # Start serial adapter and web console
    assert await serial.start()

    wc = WebConsoleAdapter("wc", wc_cfg)
    wc.set_auth_manager(auth)
    wc.set_console_manager(cm)
    assert await wc.start()

    # Prepare auth header
    token = base64.b64encode(b"admin:password").decode()
    headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    async with ClientSession(connector=TCPConnector(ssl=False)) as session:
        # List ports before reload
        async with session.get("http://127.0.0.1:8910/api/ports", headers=headers) as resp:
            assert resp.status == 200
            data = json.loads(await resp.text())
            names = {p.get("name") for p in data.get("ports", [])}
            assert "consoleA" in names

        # Post reload via adapter reconcile: add consoleB, change consoleA baudrate, remove nothing
        new_serial_ports = [
            {"name": "consoleA", "description": "A2", "device": "/dev/null", "baudrate": 115200},
            # A different device path: two ports on the same unix device are
            # flagged offline (issue #57), which is not what tests here.
            {"name": "consoleB", "description": "B", "device": str(tmp_path / "ttyB"), "baudrate": 9600},
        ]
        summary = await serial.reconcile_ports({"serial_ports": new_serial_ports})
        # consoleB should be added, consoleA should be updated
        assert "consoleB" in (summary.get("added") or [])
        assert "consoleA" in (summary.get("updated") or [])

        # Verify /api/ports reflects new set
        async with session.get("http://127.0.0.1:8910/api/ports", headers=headers) as resp:
            assert resp.status == 200
            data2 = json.loads(await resp.text())
            names2 = {p.get("name") for p in data2.get("ports", [])}
            assert names2.issuperset({"consoleA", "consoleB"})

    # Cleanup
    await wc.stop()
    await serial.stop()


@pytest.mark.asyncio
async def test_duplicate_serial_device_flagged_in_api_ports(tmp_path):
    """Issue #57: two ports on the same unix device - only the first opens it.

    The duplicate stays listed in /api/ports but reports connected=False and
    a status_message naming the claimant.
    """
    wc_cfg = {
        "web_console": {
            "host": "127.0.0.1",
            "port": 8911,
            "enable_ui": False,
        }
    }
    auth = AuthManager(
        {
            "users": [
                {
                    "username": "admin",
                    "password_hash": "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8",
                    "permissions": "admin",
                }
            ]
        }
    )

    pm = PortManager([])
    cm = ConsoleManager(pm, auth)

    ser_cfg = {
        "serial_ports": [
            {"name": "consoleA", "description": "A", "device": "/dev/null", "baudrate": 9600},
            {"name": "consoleB", "description": "B", "device": "/dev/null", "baudrate": 9600},
        ]
    }
    serial = SerialAdapter("serial_ports", ser_cfg)
    serial.main_port_manager = pm
    pm.set_unified_adapters([serial])

    assert await serial.start()

    # The duplicate is registered (visible) but offline
    b = serial.serial_ports["consoleB"]
    assert b.status_message and "consoleA" in b.status_message
    assert b.connection_task is None

    wc = WebConsoleAdapter("wc", wc_cfg)
    wc.set_auth_manager(auth)
    wc.set_console_manager(cm)
    assert await wc.start()

    token = base64.b64encode(b"admin:password").decode()
    headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    async with ClientSession(connector=TCPConnector(ssl=False)) as session:
        async with session.get("http://127.0.0.1:8911/api/ports", headers=headers) as resp:
            assert resp.status == 200
            data = json.loads(await resp.text())
            by_name = {p.get("name"): p for p in data.get("ports", [])}
            assert set(by_name) >= {"consoleA", "consoleB"}
            assert by_name["consoleB"]["connected"] is False
            assert by_name["consoleB"]["status_message"]
            assert "consoleA" in by_name["consoleB"]["status_message"]
            assert "status_message" not in by_name["consoleA"]

    # Cleanup
    await wc.stop()
    await serial.stop()
    # Both connection supervisors (only consoleA has one) are stopped
    assert serial.serial_ports == {}
