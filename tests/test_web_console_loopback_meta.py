import asyncio
import json

import pytest

from openmux.server.adapters.loopback import LoopbackAdapter
from openmux.server.adapters.web_console import WebConsoleAdapter
from openmux.server.auth_manager import AuthManager
from openmux.server.console_manager import ConsoleManager
from openmux.server.port_manager import PortManager


@pytest.mark.asyncio
async def test_ports_snapshot_loopback_connected_and_id_local():
    # Setup a loopback adapter with one port and attach to PortManager
    la = LoopbackAdapter("loop", {"loopback_ports": [{"name": "L1"}]})
    pm = PortManager([])
    pm.set_unified_adapters([la])
    assert await la.start()

    # Setup minimal WebConsole without starting HTTP server
    wc = WebConsoleAdapter("wc", {"web_console": {"enable_ui": False}})
    auth = AuthManager({"users": []})
    cm = ConsoleManager(pm, auth)
    wc.set_auth_manager(auth)
    wc.set_console_manager(cm)

    snap = wc._get_ports_snapshot()
    entry = next(p for p in snap if p.get("name") == "L1")
    assert entry.get("connected") is True
    # id should be local::<name> when no muxcon server_id is available
    assert entry.get("id", "").endswith("local::L1")


@pytest.mark.asyncio
async def test_meta_broadcast_loopback_connected_true(monkeypatch):
    la = LoopbackAdapter("loop", {"loopback_ports": [{"name": "L2"}]})
    pm = PortManager([])
    pm.set_unified_adapters([la])
    assert await la.start()

    wc = WebConsoleAdapter("wc", {"web_console": {"enable_ui": False}})
    auth = AuthManager({"users": []})
    cm = ConsoleManager(pm, auth)
    wc.set_auth_manager(auth)
    wc.set_console_manager(cm)

    # Inject a fake websocket client subscribed to meta
    cid = "ws:test"
    class DummyWS:
        def __init__(self):
            self.sent = []
        async def send_str(self, s):
            self.sent.append(s)

    ws = DummyWS()
    wc._clients[cid] = ws
    wc._meta_subscribers.setdefault("L2", set()).add(cid)

    await wc._broadcast_meta("L2")

    assert ws.sent, "No meta payload sent"
    # Last sent payload should be an OMXCTRL meta with connected:true
    payload = ws.sent[-1]
    assert payload.startswith("OMXCTRL ")
    meta = json.loads(payload[len("OMXCTRL "):])
    assert meta.get("type") == "meta"
    assert meta.get("name") == "L2"
    assert meta.get("connected") is True
