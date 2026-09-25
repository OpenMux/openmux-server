import asyncio
import base64
import json
import os
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import ClientSession, TCPConnector, WSMsgType

from openmux.server.auth_manager import AuthManager
from openmux.server.console_manager import ConsoleManager
from openmux.server.port_manager import PortManager
from openmux.server.web_console import WebConsoleAdapter, handle_ws
from openmux.server.web_plugins import ADAPTER_APP_KEY


def _bound_port(adapter) -> int:
    """Read back the port a started single-site web console actually bound.

    Lets tests start on an ephemeral port (``"port": 0``) instead of a hard-
    coded one, so full-suite runs never collide on a fixed port (issue #69).
    """
    return int(adapter._http_site._server.sockets[0].getsockname()[1])


@pytest.mark.asyncio
async def test_probes_plain_text(tmp_path):
    # Minimal config
    config = {
        "web_console": {
            "host": "127.0.0.1",
            "port": 0,
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
    port = _bound_port(adapter)

    async with ClientSession(connector=TCPConnector(ssl=False)) as session:
        async with session.get(f"http://127.0.0.1:{port}/healthz") as resp:
            assert resp.status == 200
            text = await resp.text()
            assert text.strip() == "ok"
        async with session.get(f"http://127.0.0.1:{port}/livez") as resp:
            assert resp.status == 200
            assert (await resp.text()).strip() == "live"
        # readyz requires auth
        async with session.get(f"http://127.0.0.1:{port}/readyz") as resp:
            assert resp.status in (401, 403)

    await adapter.stop()


@pytest.mark.asyncio
async def test_probes_detailed_json(tmp_path):
    config = {
        "web_console": {
            "host": "127.0.0.1",
            "port": 0,
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
    port = _bound_port(adapter)

    async with ClientSession(connector=TCPConnector(ssl=False)) as session:
        async with session.get(f"http://127.0.0.1:{port}/healthz") as resp:
            assert resp.status == 200
            data = json.loads(await resp.text())
            assert data["component"] == "web_console"
            assert "uptime_seconds" in data
        async with session.get(f"http://127.0.0.1:{port}/livez") as resp:
            assert resp.status == 200
            ldata = json.loads(await resp.text())
            assert ldata["status"] == "ok"
        # readyz with auth header
        token = base64.b64encode(b"u:password").decode()
        headers = {"Authorization": f"Basic {token}"}
        async with session.get(f"http://127.0.0.1:{port}/readyz", headers=headers) as resp:
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
            "port": 0,
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
    port = _bound_port(adapter)

    async with ClientSession(connector=TCPConnector(ssl=False)) as session:
        for path in ("healthz", "livez"):
            async with session.get(f"http://127.0.0.1:{port}/{path}") as resp:
                # Should be 404 because probes disabled (no route registered, middleware bypasses only if probes enabled)
                assert resp.status == 404
        # readyz path still requires auth (middleware not bypassed) so expect 401 when probes disabled
        async with session.get(f"http://127.0.0.1:{port}/readyz") as resp:
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
            "port": 0,
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
    port = _bound_port(adapter)

    token = base64.b64encode(b"u:password").decode()
    headers = {"Authorization": f"Basic {token}"}

    async with ClientSession(connector=TCPConnector(ssl=False)) as session:
        # Ready should reflect console_manager / port_manager presence
        async with session.get(f"http://127.0.0.1:{port}/readyz", headers=headers) as resp:
            assert resp.status == 200
            data = json.loads(await resp.text())
            assert data.get("port_manager") is True
        # Connect WS
        async with session.ws_connect(f"http://127.0.0.1:{port}/ws/loopback_ws1", headers=headers) as ws:
            await ws.send_str("hello")
            # Allow server to process write
            await asyncio.sleep(0.05)
            assert getattr(dummy, "last_write", None) == b"hello"

    await adapter.stop()


@pytest.mark.asyncio
async def test_websocket_transport_failure_prunes_delivery_channel_only():
    class DummyPort:
        def __init__(self, name):
            self.name = name
            self.description = "dummy"
            self.connected_clients = []
            self.max_read_write_users = 5
            self.is_running = True

        async def write_data(self, data):
            return len(data)

        def get_status(self):
            return {"name": self.name, "is_running": True}

    class FailingWS:
        closed = False

        async def send_bytes(self, data):
            raise ConnectionResetError("socket reset")

    pm = PortManager([])
    pm.ports["console1"] = DummyPort("console1")  # type: ignore[attr-defined]
    auth = AuthManager(
        {"users": [{"username": "u", "password_hash": "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"}]}
    )
    cm = ConsoleManager(pm, auth)
    adapter = WebConsoleAdapter("wc", {"web_console": {"enable_ui": False}})
    adapter.set_auth_manager(auth)
    adapter.set_console_manager(cm)

    ok, _mode, _reason = await cm.connect_client_to_port("ws:test", "console1", "u")
    assert ok is True
    cm.register_client_channel("ws:test", adapter)
    failing_ws = FailingWS()
    adapter._clients["ws:test"] = failing_ws

    result = await adapter.send_data_to_client("ws:test", b"abc")

    assert result is False
    assert "ws:test" not in adapter._clients
    assert "ws:test" in cm.client_port_map
    assert pm.ports["console1"].connected_clients[0]["client_id"] == "ws:test"


@pytest.mark.asyncio
async def test_websocket_send_uses_falsy_socket_object():
    class DummyPort:
        def __init__(self, name):
            self.name = name
            self.description = "dummy"
            self.connected_clients = []
            self.max_read_write_users = 5
            self.is_running = True

        async def write_data(self, data):
            return len(data)

        def get_status(self):
            return {"name": self.name, "is_running": True}

    class FalsyWS:
        closed = False

        def __init__(self):
            self.sent = []

        def __bool__(self):
            return False

        async def send_bytes(self, data):
            self.sent.append(data)

    pm = PortManager([])
    pm.ports["console1"] = DummyPort("console1")  # type: ignore[attr-defined]
    auth = AuthManager(
        {"users": [{"username": "u", "password_hash": "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"}]}
    )
    cm = ConsoleManager(pm, auth)
    adapter = WebConsoleAdapter("wc", {"web_console": {"enable_ui": False}})
    adapter.set_auth_manager(auth)
    adapter.set_console_manager(cm)

    ok, _mode, _reason = await cm.connect_client_to_port("ws:test", "console1", "u")
    assert ok is True
    cm.register_client_channel("ws:test", adapter)
    falsy_ws = FalsyWS()
    adapter._clients["ws:test"] = falsy_ws

    result = await adapter.send_data_to_client("ws:test", b"abc")

    assert result is True
    assert falsy_ws.sent == [b"abc"]
    assert "ws:test" in adapter._clients


# --- OMXCTRL power frames + in-terminal [POWER] notice ------------------------

PDU_SECTION = {
    "power": {"pdus": [{"name": "rack1", "driver": "dummy", "poll_interval": 0, "options": {"outlets": ["1", "2"]}}]}
}

_U_HASH = "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"


class _PowerPort:
    """Real-port stand-in: writable, carries the console's PDU feed refs."""

    def __init__(self, name, power=()):
        self.name = name
        self.description = "dummy"
        self.power = list(power)
        self.connected_clients = []
        self.max_read_write_users = 5
        self.is_running = True
        self.unified_port = self

    async def write_data(self, data):
        return len(data)

    def get_status(self):
        return {"name": self.name, "is_running": True}


class _ScriptedWS:
    """WebSocket stand-in: replays scripted incoming messages, records sends.

    Driven as `async for msg in ws` by `handle_ws`; the script ends so the
    loop exits cleanly through the normal teardown path.
    """

    def __init__(self, incoming=()):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False
        self.close_code = None

    async def prepare(self, request):
        return None

    async def send_str(self, text):
        self.sent.append(text)

    async def send_bytes(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.incoming:
            raise StopAsyncIteration
        return self.incoming.pop(0)

    def exception(self):
        return None

    async def close(self, code=None, message=None):
        self.closed = True
        self.close_code = code


def _power_frame(text):
    from aiohttp import WSMessage, WSMsgType

    return WSMessage(WSMsgType.TEXT, text, None)


async def _power_ws_adapter(username="u"):
    """WebConsoleAdapter wired to a real console manager + one dummy PDU.

    The PDU gets the SAME auth manager (its switches check
    `get_user_permissions`), so `u` (no explicit role) is read-write and a
    `permissions: read-only` user is denied.
    """
    from openmux.server.adapters.pdu import PduAdapter

    user = {"username": username, "password_hash": _U_HASH}
    if username != "u":
        user["permissions"] = "read-only"
    pm = PortManager([])
    pm.ports["console1"] = _PowerPort("console1", ["rack1.1", "rack1.2"])
    auth = AuthManager({"users": [user]})
    cm = ConsoleManager(pm, auth)
    adapter = WebConsoleAdapter("wc", {"web_console": {"enable_ui": False}})
    adapter.set_auth_manager(auth)
    adapter.set_console_manager(cm)
    pdu = PduAdapter("power", PDU_SECTION)
    pdu.main_port_manager = pm
    pdu.set_auth_manager(auth)
    assert await pdu.start() is True
    pm.unified_adapters = [pdu]
    return adapter, pdu


def _fake_ws_request(adapter, username):
    request = MagicMock()
    request.app = {ADAPTER_APP_KEY: adapter}
    request.get = lambda key, default=None: username if key == "username" else default
    request.match_info = {"port_name": "console1"}
    request._fqpn_port = None
    rel_url = MagicMock()
    rel_url.query = {"meta": "1"}
    request.rel_url = rel_url
    request.headers = {}
    request.transport = None
    return request


def _ws_strs(ws):
    return [m for m in ws.sent if isinstance(m, str)]


def _ws_ctrl_frames(ws):
    return [json.loads(t[len("OMXCTRL ") :]) for t in _ws_strs(ws) if t.startswith("OMXCTRL ")]


async def _await_ws_pred(ws, pred, tries=200):
    for _ in range(tries):
        if any(pred(t) for t in _ws_strs(ws)):
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.mark.asyncio
async def test_ws_power_frames_answered_and_switched():
    adapter, pdu = await _power_ws_adapter()
    ws = _ScriptedWS(
        incoming=[
            _power_frame("OMXCTRL " + json.dumps({"type": "power_query"})),
            _power_frame("OMXCTRL " + json.dumps({"type": "power_switch", "ref": "rack1.1", "on": False})),
        ]
    )
    with patch("openmux.server.web_console.web.WebSocketResponse") as mock_ws_cls:
        mock_ws_cls.return_value = ws
        res = await handle_ws(_fake_ws_request(adapter, "u"))
    assert res is ws
    feeds = [c for c in _ws_ctrl_frames(ws) if c["type"] == "power_feeds"][0]
    assert feeds["feeds_total"] == 2
    assert [f["ref"] for f in feeds["feeds"]] == ["rack1.1", "rack1.2"]
    sw = [c for c in _ws_ctrl_frames(ws) if c["type"] == "power_switch"][0]
    assert sw["ok"] is True and sw["ref"] == "rack1.1" and sw["on"] is False and sw["state"] == "off"
    assert pdu.pdus["rack1"].readings["1"].on is False
    await pdu.stop()


@pytest.mark.asyncio
async def test_ws_power_switch_denied_for_read_only():
    adapter, pdu = await _power_ws_adapter(username="ro")
    ws = _ScriptedWS(incoming=[_power_frame("OMXCTRL " + json.dumps({"type": "power_switch", "ref": "rack1.1", "on": False}))])
    with patch("openmux.server.web_console.web.WebSocketResponse") as mock_ws_cls:
        mock_ws_cls.return_value = ws
        await handle_ws(_fake_ws_request(adapter, "ro"))
    sw = [c for c in _ws_ctrl_frames(ws) if c["type"] == "power_switch"][0]
    assert sw["ok"] is False
    assert "insufficient permission" in sw["error"]
    assert pdu.pdus["rack1"].readings["1"].on is True
    await pdu.stop()


@pytest.mark.asyncio
async def test_ws_power_notice_pushed_on_meta_update():
    adapter, pdu = await _power_ws_adapter()
    ws = _ScriptedWS()
    cid = "ws:test"
    adapter._clients[cid] = ws
    adapter._meta_subscribers["console1"] = {cid}
    adapter._meta_debounce["console1"] = 0.0

    adapter._on_port_meta_update(
        "console1",
        {
            "event": "power_outlet_changed",
            "outlet": "rack1.1",
            "on": False,
            "all_power_lost": False,
            "other_outlets_on": ["rack1.2"],
        },
    )

    assert await _await_ws_pred(ws, lambda t: "feed rack1.1 is now off" in t)
    # The meta frame still rides along for the web badge (fire-and-forget)
    assert await _await_ws_pred(ws, lambda t: t.startswith("OMXCTRL ") and '"power"' in t)
    await pdu.stop()


@pytest.mark.asyncio
async def test_ws_power_notice_all_power_lost_variant():
    adapter, pdu = await _power_ws_adapter()
    ws = _ScriptedWS()
    cid = "ws:test"
    adapter._clients[cid] = ws
    adapter._meta_subscribers["console1"] = {cid}
    adapter._meta_debounce["console1"] = 0.0

    adapter._on_port_meta_update(
        "console1",
        {"event": "power_outlet_changed", "outlet": "rack1.2", "on": False, "all_power_lost": True, "other_outlets_on": []},
    )

    assert await _await_ws_pred(ws, lambda t: "all power feeds are now OFF" in t and "rack1.2" in t)
    await pdu.stop()
