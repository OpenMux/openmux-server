"""Tests for the PDU power web plugin (power_monitor.py).

Boots a real WebConsoleAdapter in-process on 127.0.0.1 with a dummy PDU
adapter and a loopback port, then exercises the HTTP + WS routes:
permission gates, CSRF, the outlet-toggle impact payload, and the
snapshot-then-frames live socket.
"""

import base64
import hashlib
import json

import pytest
from aiohttp import ClientSession, TCPConnector

from openmux.server.adapters.loopback import LoopbackAdapter
from openmux.server.adapters.pdu import PduAdapter
from openmux.server.auth_manager import AuthManager
from openmux.server.console_manager import ConsoleManager
from openmux.server.port_manager import PortManager
from openmux.server.web_console import WebConsoleAdapter


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


USER_PASSWORD = {
    "u": "password",  # admin
    "rw": "rwrite",  # read-write
    "ro": "ronly",  # read-only
    "grop": "grop",  # read-write, only in the "ops" console group
}
USERS = [
    {"username": "u", "password_hash": _sha(USER_PASSWORD["u"]), "permissions": "admin"},
    {"username": "rw", "password_hash": _sha(USER_PASSWORD["rw"]), "permissions": "read-write"},
    {"username": "ro", "password_hash": _sha(USER_PASSWORD["ro"]), "permissions": "read-only"},
    {"username": "grop", "password_hash": _sha(USER_PASSWORD["grop"]), "permissions": "read-write", "groups": ["ops"]},
]


def _hdr(user: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{USER_PASSWORD[user]}".encode()).decode()}


POWER_SECTION = {
    "power": {
        "enabled": True,
        "pdus": [
            {
                "name": "rack1",
                "driver": "dummy",
                "poll_interval": 0,
                "options": {"outlets": ["1", "2"]},
                "outlets": [{"id": "2", "description": "Switch A"}],
            },
            {
                "name": "rack2",
                "driver": "dummy",
                "poll_interval": 0,
                "options": {"outlets": ["1", "2"]},
            },
        ],
    },
}
# p2 is fed by rack2.1 and is group-restricted (ops only): the fixture for the
# group-scoped power control check.
LOOPBACK = {
    "loopback_ports": [
        {"name": "p1", "power": ["rack1.1", "rack1.2"]},
        {"name": "p2", "power": ["rack2.1"], "read_write_groups": ["ops"]},
    ]
}


async def _start(http_port: int):
    pm = PortManager([])
    loop_adapter = LoopbackAdapter("loop", LOOPBACK)
    loop_adapter.main_port_manager = pm
    pdu = PduAdapter("power", POWER_SECTION)
    pdu.main_port_manager = pm
    pm.set_unified_adapters([loop_adapter, pdu])
    assert await loop_adapter.start() is True
    assert await pdu.start() is True

    auth = AuthManager({"users": USERS})
    cm = ConsoleManager(pm, auth)
    pdu.set_console_manager(cm)
    pdu.set_auth_manager(auth)

    config = dict(POWER_SECTION)
    config.update(LOOPBACK)
    config["web_console"] = {
        "host": "127.0.0.1",
        "port": http_port,
        "enable_ui": True,
        "enable_probes": False,
        "plugins": [
            {"module": "openmux.server.web_plugins.power_monitor", "enabled": True},
        ],
    }
    web_adapter = WebConsoleAdapter("wc", dict(config["web_console"]))
    web_adapter.server_config = config
    web_adapter.set_auth_manager(auth)
    web_adapter.set_console_manager(cm)
    assert await web_adapter.start()
    bound_port = int(web_adapter._http_site._server.sockets[0].getsockname()[1])
    return web_adapter, pm, loop_adapter, pdu, bound_port


async def _stop(ctx):
    web_adapter, pm, loop_adapter, pdu, _ = ctx
    await web_adapter.stop()
    await pdu.stop()
    await loop_adapter.stop()


@pytest.mark.asyncio
async def test_api_power_snapshot_lists_pdu_outlets():
    ctx = await _start(0)
    try:
        _, _, _, pdu, port = ctx
        # Turn outlet 2 off so the mixed state is observable
        res = await pdu.set_outlet("rack1.2", False)
        assert res["ok"] is True
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.get(f"http://127.0.0.1:{port}/api/power", headers=_hdr("ro")) as resp:
                assert resp.status == 200
                snap = await resp.json()
                assert snap["enabled"] is True
                assert [p["name"] for p in snap["pdus"]] == ["rack1", "rack2"]
                entry = snap["pdus"][0]
                assert entry["outlet_count"] == 2
                assert entry["outlets_on"] == 1
                o1 = [o for o in entry["outlets"] if o["id"] == "1"][0]
                assert o1["on"] is True
                assert o1["description"] == ""
                assert o1["off_impact"]["losing_power"][0]["port"] == "p1"
                assert o1["mapped_ports"] == ["p1"]
    finally:
        await _stop(ctx)


@pytest.mark.asyncio
async def test_api_power_requires_auth():
    ctx = await _start(0)
    try:
        _, _, _, _, port = ctx
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.get(f"http://127.0.0.1:{port}/api/power") as resp:
                assert resp.status == 401
    finally:
        await _stop(ctx)


@pytest.mark.asyncio
async def test_pages_render_with_nav_and_permission():
    ctx = await _start(0)
    try:
        web_adapter, _, _, _, port = ctx
        # No "require": Power state is visible to every permission level
        assert web_adapter._plugin_nav == [{"title": "Power", "path": "/power"}]
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.get(f"http://127.0.0.1:{port}/power", headers=_hdr("u")) as resp:
                assert resp.status == 200
                body = await resp.text()
                assert "pdu-rows" in body
                assert "rack1" in body
                # sidebar menu item is rendered for the admin user
                assert 'href="/power"' in body
            # Detail page: the server-rendered outlet rows contain no toggle
            # button for read-only (the inline JS always references the
            # attribute textually, so scope the check to the rows block)
            async with session.get(f"http://127.0.0.1:{port}/power/rack1", headers=_hdr("ro")) as resp:
                assert resp.status == 200
                body = await resp.text()
                rows_block = body.split('id="outlet-rows"', 1)[1].split("</tbody>", 1)[0]
                assert "data-toggle=" not in rows_block
                assert "read-only" in rows_block
            # read-write sees toggle buttons in the rows
            async with session.get(f"http://127.0.0.1:{port}/power/rack1", headers=_hdr("rw")) as resp:
                body = await resp.text()
                rows_block = body.split('id="outlet-rows"', 1)[1].split("</tbody>", 1)[0]
                assert 'data-toggle="rack1.1"' in rows_block
            # Unknown PDU name -> 404
            async with session.get(f"http://127.0.0.1:{port}/power/nope", headers=_hdr("u")) as resp:
                assert resp.status == 404
            # Per-PDU detail links on the list page
            async with session.get(f"http://127.0.0.1:{port}/power", headers=_hdr("u")) as resp:
                body = await resp.text()
                assert "/power/rack1" in body
    finally:
        await _stop(ctx)


@pytest.mark.asyncio
async def test_power_pages_render_sidebar_with_pdus_and_ports():
    """The Power pages keep the full left sidebar.

    The Power section is expandable (like Console) and lists every PDU as a
    sub-item; the Console section shows the real port list instead of
    "No ports", and the port row highlights when a port is selected.
    """
    ctx = await _start(0)
    try:
        _, _, _, pdu, port = ctx
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            # List page: Power section with both PDUs, live from the adapter
            async with session.get(f"http://127.0.0.1:{port}/power", headers=_hdr("u")) as resp:
                body = await resp.text()
                assert 'id="nav-power-parent"' in body
                assert 'id="power-pdus"' in body
                assert 'data-pdu-name="rack1"' in body
                assert 'data-pdu-name="rack2"' in body
                assert "/power/rack2" in body
                assert "No ports" not in body
                assert "console-ports" in body
                assert 'href="/console?port=p1"' in body
                # The PDU links are rebuilt per request: adding a PDU at
                # runtime shows up without a restart or plugin re-registration.
                await pdu.reconcile_ports(
                    {
                        "pdus": [dict(d) for d in POWER_SECTION["power"]["pdus"]]
                        + [{"name": "rack3", "driver": "dummy", "poll_interval": 0, "options": {"outlets": ["1"]}}]
                    }
                )
                async with session.get(f"http://127.0.0.1:{port}/power", headers=_hdr("u")) as resp:
                    body = await resp.text()
                    assert 'data-pdu-name="rack3"' in body
                    assert 'href="/power/rack3"' in body
                # Restore the fixture state for later requests
                await pdu.reconcile_ports(POWER_SECTION)
            # Detail page: the active PDU is highlighted in the expanded list
            async with session.get(f"http://127.0.0.1:{port}/power/rack1", headers=_hdr("u")) as resp:
                body = await resp.text()
                assert 'class="nav-sub-item active"' in body and 'data-pdu-name="rack1"' in body
            # Console row highlight: active on the port-less landing, and on
            # port pages the port row (not the Console link) carries it
            async with session.get(f"http://127.0.0.1:{port}/console", headers=_hdr("u")) as resp:
                body = await resp.text()
                assert 'class="nav-item active" href="/console"' in body
                # The PDU sub-links are enriched on EVERY page, not just the
                # power pages (a prior bug showed "No PDUs configured"
                # elsewhere)
                assert 'data-pdu-name="rack1"' in body
                assert "No PDUs configured" not in body
            async with session.get(f"http://127.0.0.1:{port}/console?port=p1", headers=_hdr("u")) as resp:
                body = await resp.text()
                assert 'class="nav-item active" href="/console"' not in body
    finally:
        await _stop(ctx)


@pytest.mark.asyncio
async def test_set_outlet_permission_and_csrf(caplog):
    ctx = await _start(0)
    try:
        _, _, _, pdu, port = ctx
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            # Basic auth (no session cookie) skips CSRF, so these go through:
            # read-only is rejected with 403
            async with session.post(
                f"http://127.0.0.1:{port}/api/power/outlets/rack1.1", headers=_hdr("ro"), json={"on": False}
            ) as resp:
                assert resp.status == 403
            # read-write succeeds and the state + impact are returned, and the
            # web switch path writes the control audit line with the user
            with caplog.at_level("INFO", logger="openmux.adapter.power"):
                async with session.post(
                    f"http://127.0.0.1:{port}/api/power/outlets/rack1.1", headers=_hdr("rw"), json={"on": False}
                ) as resp:
                    assert resp.status == 200
                    data = await resp.json()
                    assert data["ok"] is True
                    assert data["reading"]["on"] is False
                    # p1 keeps rack1.2, so it stays up via the other feed
                    assert data["impact"]["staying_up"][0]["port"] == "p1"
                    assert data["impact"]["losing_power"] == []
            audit = [r for r in caplog.records if "POWER CONTROL" in r.getMessage()]
            assert any("user rw turned rack1.1 off" in r.getMessage() for r in audit)
            # Now p1 only has rack1.2; turning rack1.2 off would lose all power
            async with session.post(
                f"http://127.0.0.1:{port}/api/power/outlets/rack1.2", headers=_hdr("rw"), json={"on": False}
            ) as resp:
                data = await resp.json()
                assert data["impact"]["losing_power"][0]["port"] == "p1"
            # Bad ref -> 404
            async with session.post(
                f"http://127.0.0.1:{port}/api/power/outlets/rack1.99", headers=_hdr("rw"), json={"on": False}
            ) as resp:
                assert resp.status == 404
            # Missing `on` -> 400
            async with session.post(f"http://127.0.0.1:{port}/api/power/outlets/rack1.2", headers=_hdr("rw"), json={}) as resp:
                assert resp.status == 400
    finally:
        await _stop(ctx)


@pytest.mark.asyncio
async def test_set_outlet_group_scoped():
    """Switching is scoped to the consoles the user can open.

    p1 has no group lists (a global read-write may switch its feeds); p2 is
    ops-group-only, so it needs a user in that group or admin.
    """
    ctx = await _start(0)
    try:
        _, _, _, _, port = ctx
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            # read-write, no groups: p1's feed is allowed
            async with session.post(
                f"http://127.0.0.1:{port}/api/power/outlets/rack1.1", headers=_hdr("rw"), json={"on": False}
            ) as resp:
                assert resp.status == 200
            # ... but p2 feeds a console outside their groups -> 403 naming it
            async with session.post(
                f"http://127.0.0.1:{port}/api/power/outlets/rack2.1", headers=_hdr("rw"), json={"on": False}
            ) as resp:
                assert resp.status == 403
                data = await resp.json()
                assert data["error"] is True
                assert "p2" in data["message"]
                assert "needs admin" in data["message"]
            # a read-write in the ops group may switch p2's feed
            async with session.post(
                f"http://127.0.0.1:{port}/api/power/outlets/rack2.1", headers=_hdr("grop"), json={"on": False}
            ) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True
                assert data["reading"]["on"] is False
            # admin bypasses the group boundary
            async with session.post(
                f"http://127.0.0.1:{port}/api/power/outlets/rack2.1", headers=_hdr("u"), json={"on": True}
            ) as resp:
                assert resp.status == 200
    finally:
        await _stop(ctx)


@pytest.mark.asyncio
async def test_set_outlet_requires_csrf_with_session_cookie():
    """Session-authenticated requests must present the CSRF token.

    The CSRF token is the session cookie value, so a session is seeded
    directly on the adapter (the login flow is covered elsewhere).
    """
    ctx = await _start(0)
    try:
        import time as _time

        web_adapter, _, _, _, port = ctx
        sid = "test-session-sid"
        now = _time.time()
        web_adapter._sessions[sid] = {"username": "rw", "created": now, "last_seen": now, "ip": None}
        # Must be a real Cookie header (not a custom header of the cookie's name)
        cookie_header = {"Cookie": f"{web_adapter._session_cookie_name}={sid}"}
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            # CSRF endpoint returns the session id as the token
            async with session.get(f"http://127.0.0.1:{port}/api/csrf", headers=cookie_header) as resp:
                assert resp.status == 200
                assert (await resp.json())["csrf"] == sid
            # Without token -> 403
            async with session.post(
                f"http://127.0.0.1:{port}/api/power/outlets/rack1.1", headers=cookie_header, json={"on": True}
            ) as resp:
                assert resp.status == 403
            # Wrong token -> 403
            async with session.post(
                f"http://127.0.0.1:{port}/api/power/outlets/rack1.1",
                headers={**cookie_header, "X-OMX-CSRF": "nope"},
                json={"on": True},
            ) as resp:
                assert resp.status == 403
            # With token -> 200 and the state flips
            async with session.post(
                f"http://127.0.0.1:{port}/api/power/outlets/rack1.1",
                headers={**cookie_header, "X-OMX-CSRF": sid},
                json={"on": True},
            ) as resp:
                assert resp.status == 200
                assert (await resp.json())["ok"] is True
    finally:
        await _stop(ctx)


@pytest.mark.asyncio
async def test_ws_power_snapshot_then_change_frame():
    ctx = await _start(0)
    try:
        _, _, _, pdu, port = ctx
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.ws_connect(f"http://127.0.0.1:{port}/ws/power", headers=_hdr("ro")) as ws:
                first = json.loads((await ws.receive()).data)
                assert first["event"] == "power_snapshot"
                assert first["snapshot"]["pdus"][0]["name"] == "rack1"
                # Trigger a change while the socket is open
                await pdu.set_outlet("rack1.1", False)
                second = json.loads((await ws.receive(timeout=5)).data)
                assert second["event"] == "outlet_changed"
                assert second["ref"] == "rack1.1"
                assert second["on"] is False
    finally:
        await _stop(ctx)


@pytest.mark.asyncio
async def test_no_nav_when_no_power_adapter():
    """Without a `power:` section the plugin registers no routes or nav."""
    pm = PortManager([])
    loop_adapter = LoopbackAdapter("loop", {"loopback_ports": [{"name": "p1"}]})
    loop_adapter.main_port_manager = pm
    pm.set_unified_adapters([loop_adapter])
    assert await loop_adapter.start() is True
    auth = AuthManager({"users": USERS})
    cm = ConsoleManager(pm, auth)
    config = {
        "web_console": {
            "host": "127.0.0.1",
            "port": 0,
            "enable_ui": True,
            "enable_probes": False,
            "plugins": [
                {"module": "openmux.server.web_plugins.power_monitor", "enabled": True},
            ],
        }
    }
    web_adapter = WebConsoleAdapter("wc", dict(config["web_console"]))
    web_adapter.server_config = config
    web_adapter.set_auth_manager(auth)
    web_adapter.set_console_manager(cm)
    assert await web_adapter.start()
    try:
        bound_port = int(web_adapter._http_site._server.sockets[0].getsockname()[1])
        assert web_adapter._plugin_nav == []
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.get(f"http://127.0.0.1:{bound_port}/api/power", headers=_hdr("u")) as resp:
                assert resp.status == 404
    finally:
        await web_adapter.stop()
        await loop_adapter.stop()
