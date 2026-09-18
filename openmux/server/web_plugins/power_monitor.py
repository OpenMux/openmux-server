"""Power monitoring web plugin.

Adds a "Power" entry to the left sidebar when a `power:` section is active,
plus:
- `GET /power`                - PDU list page
- `GET /power/{pdu_name}`     - per-PDU outlet page
- `GET /api/power`            - full power snapshot JSON
- `POST /api/power/outlets/{outlet_ref}`  - switch one outlet (read-write or admin, CSRF)
- `GET /ws/power`             - live outlet-change frames (snapshot first)

Enable in `web_console.plugins`:

    web_console:
      plugins:
        - module: openmux.server.web_plugins.power_monitor
          enabled: true

The plugin returns no nav entry when the PDU adapter is absent or disabled,
so servers without a `power:` section show no Power menu item.
"""

import asyncio
import contextlib
import json
from typing import Any, Dict, Optional

from aiohttp import WSMsgType, web

from . import get_web_adapter

_STATE_KEY = "power_monitor_state"


def _find_power_adapter(adapter):
    """Find the active PDU adapter, or None.

    Scans ``console_manager.port_manager.unified_adapters`` for
    ``get_adapter_type() == "power"`` (same pattern as the muxcon lookup in
    the web console).
    """
    pm = getattr(adapter, "console_manager", None)
    pm = getattr(pm, "port_manager", None) if pm else None
    if pm is None:
        return None
    for a in getattr(pm, "unified_adapters", []) or []:
        try:
            at = a.get_adapter_type()
        except Exception:
            continue
        if isinstance(at, str) and at.lower() == "power":
            return a
    return None


def _get_power_adapter(request: web.Request):
    """Resolve the power adapter for a request; 404 when absent."""
    adapter = get_web_adapter(request)
    pdu = _find_power_adapter(adapter)
    if pdu is None:
        raise web.NotFound(text="Power management is not configured\n")
    return adapter, pdu


async def _handle_power_list(request: web.Request) -> web.StreamResponse:
    """Render the PDU list page."""
    adapter, pdu = _get_power_adapter(request)
    username = request.get("username")
    if not username:
        raise web.HTTPUnauthorized()
    env = getattr(adapter, "_jinja_env", None)
    if env is None:
        raise web.HTTPInternalServerError(text="Templates are not initialized\n")
    user_permission = (
        adapter._get_effective_permission(username, request) if hasattr(adapter, "_get_effective_permission") else None
    )
    snapshot = pdu.get_power_snapshot()
    plugin_nav = (
        adapter._get_allowed_plugin_nav(username, request=request) if hasattr(adapter, "_get_allowed_plugin_nav") else []
    )
    ports = adapter._get_ports_snapshot() if hasattr(adapter, "_get_ports_snapshot") else []
    base_path = adapter._effective_base_path(request) if hasattr(adapter, "_effective_base_path") else ""
    tmpl = env.get_template("power_list.html.j2")
    html = tmpl.render(
        realm=adapter.realm,
        logo_url=adapter._get_logo_url() if hasattr(adapter, "_get_logo_url") else None,
        title="OpenMux Power",
        base_path=base_path,
        plugin_nav=plugin_nav,
        ports=ports,
        current_port=None,
        user_permission=user_permission,
        snapshot=snapshot,
        active_page="power",
        pdu_name=None,
    )
    return web.Response(body=html.encode("utf-8"), content_type="text/html")


async def _handle_power_detail(request: web.Request) -> web.StreamResponse:
    """Render one PDU's outlet page."""
    adapter, pdu = _get_power_adapter(request)
    username = request.get("username")
    if not username:
        raise web.HTTPUnauthorized()
    pdu_name = request.match_info.get("pdu_name", "")
    env = getattr(adapter, "_jinja_env", None)
    if env is None:
        raise web.HTTPInternalServerError(text="Templates are not initialized\n")
    user_permission = (
        adapter._get_effective_permission(username, request) if hasattr(adapter, "_get_effective_permission") else None
    )
    snapshot = pdu.get_power_snapshot()
    pdu_entry = next((p for p in snapshot.get("pdus", []) if p.get("name") == pdu_name), None)
    if pdu_entry is None:
        raise web.HTTPNotFound(text=f"PDU {pdu_name!r} is not configured\n")
    plugin_nav = (
        adapter._get_allowed_plugin_nav(username, request=request) if hasattr(adapter, "_get_allowed_plugin_nav") else []
    )
    ports = adapter._get_ports_snapshot() if hasattr(adapter, "_get_ports_snapshot") else []
    base_path = adapter._effective_base_path(request) if hasattr(adapter, "_effective_base_path") else ""
    tmpl = env.get_template("power_detail.html.j2")
    html = tmpl.render(
        realm=adapter.realm,
        logo_url=adapter._get_logo_url() if hasattr(adapter, "_get_logo_url") else None,
        title=f"OpenMux Power - {pdu_name}",
        base_path=base_path,
        plugin_nav=plugin_nav,
        ports=ports,
        current_port=None,
        user_permission=user_permission,
        snapshot=snapshot,
        pdu=pdu_entry,
        active_page="power",
        pdu_name=pdu_name,
    )
    return web.Response(body=html.encode("utf-8"), content_type="text/html")


async def _handle_api_power(request: web.Request) -> web.StreamResponse:
    """Return the full power snapshot as JSON."""
    adapter, pdu = _get_power_adapter(request)
    username = request.get("username")
    if not username:
        raise web.HTTPUnauthorized()
    return web.json_response(pdu.get_power_snapshot())


async def _handle_set_outlet(request: web.Request) -> web.StreamResponse:
    """Switch one outlet. Requires read-write or admin plus a valid CSRF token."""
    adapter, pdu = _get_power_adapter(request)
    username = request.get("username")
    if not username:
        raise web.HTTPUnauthorized()
    # Raises HTTP 401/403 when the user lacks read-write (or admin)
    adapter._require_permission(request, ("read-write", "admin"))
    if not adapter._check_csrf(request):
        return web.json_response({"error": True, "message": "CSRF check failed"}, status=403)
    ref = request.match_info.get("outlet_ref", "")
    try:
        body = await request.json()
    except Exception:
        body = {}
    on = None
    if isinstance(body, dict):
        on = body.get("on")
    if not isinstance(on, bool):
        return web.json_response({"error": True, "message": "`on` must be a boolean"}, status=400)
    try:
        result = await pdu.set_outlet(ref, on, user=username)
    except Exception as exc:
        return web.json_response({"error": True, "message": str(exc)}, status=500)
    if not result.get("ok"):
        # 404 for refs that do not exist (bad syntax, unknown PDU, unknown
        # outlet); 409 for PDU-level or driver failures
        msg = str(result.get("error", ""))
        status = 404 if ("not configured" in msg or "invalid outlet ref" in msg or "unknown outlet" in msg) else 409
        return web.json_response({"error": True, "message": msg, "impact": result.get("impact")}, status=status)
    return web.json_response({"ok": True, "reading": result.get("reading"), "impact": result.get("impact")})


async def _ws_power_send_snapshot(ws: web.WebSocketResponse, pdu) -> None:
    """Send one full snapshot frame (best-effort)."""
    try:
        await ws.send_str(json.dumps({"event": "power_snapshot", "snapshot": pdu.get_power_snapshot()}))
    except Exception:
        # justification: the client is gone; cleanup still runs
        pass


async def _ws_power_read_client(ws: web.WebSocketResponse, queue: "asyncio.Queue", flag: Dict[str, bool]) -> None:
    """Read client frames; flag a refresh request; sentinel when the socket ends."""
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except Exception:
                continue
            if isinstance(data, dict) and data.get("type") == "power_refresh_request":
                flag["refresh"] = True
    except Exception:
        # justification: reader-side errors end the stream; the sentinel below handles cleanup
        pass
    # Socket closed or errored: release the writer loop
    queue.put_nowait(None)


async def _ws_power_finish(ws: web.WebSocketResponse, pdu, reader_task: asyncio.Task, on_change) -> None:
    """Unsubscribe and close the socket (best-effort cleanup)."""
    reader_task.cancel()
    with contextlib.suppress(Exception):
        await reader_task
    pdu.unregister_state_listener(on_change)
    try:
        await ws.close()
    except Exception:
        # justification: shutdown cleanup; the transport may already be closed
        pass


async def _handle_ws_power(request: web.Request) -> web.WebSocketResponse:
    """Live outlet-change stream: snapshot first, then one frame per change.

    Frames are plain JSON (same style as the port-actions run stream):
    ``{"event": "power_snapshot", "snapshot": {...}}`` on connect, then
    ``{"event": "outlet_changed", "ref": ..., "on": ...}`` per change.
    Accepts ``{"type": "power_refresh_request"}`` for a fresh snapshot.
    """
    adapter, pdu = _get_power_adapter(request)
    username = request.get("username")
    if not username:
        raise web.HTTPUnauthorized()
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    queue: "asyncio.Queue[Optional[Dict[str, Any]]]" = asyncio.Queue()
    flag: Dict[str, bool] = {"refresh": False}

    def _on_change(ref: str, on: Optional[bool], affected: list) -> None:
        queue.put_nowait({"event": "outlet_changed", "ref": ref, "on": on})

    pdu.register_state_listener(_on_change)
    reader_task = asyncio.ensure_future(_ws_power_read_client(ws, queue, flag))
    try:
        await _ws_power_send_snapshot(ws, pdu)
        while True:
            frame = await queue.get()
            if frame is None:
                break
            if flag["refresh"]:
                flag["refresh"] = False
                await _ws_power_send_snapshot(ws, pdu)
            await ws.send_str(json.dumps(frame))
    except Exception:
        # justification: the client is gone; cleanup below still runs
        pass
    await _ws_power_finish(ws, pdu, reader_task, _on_change)
    return ws


def register_plugin(app: web.Application, adapter, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Register Power plugin routes. No nav when no PDU adapter is active."""
    pdu = _find_power_adapter(adapter)
    if pdu is None:
        adapter.logger.info("power_monitor plugin registered without a PDU adapter; no routes")
        return {}
    if getattr(pdu, "enabled", True) is False:
        return {}
    app.router.add_get("/power", _handle_power_list)
    app.router.add_get(r"/power/{pdu_name}", _handle_power_detail, name="power_detail")
    app.router.add_get("/api/power", _handle_api_power)
    app.router.add_post(r"/api/power/outlets/{outlet_ref:.+}", _handle_set_outlet)
    app.router.add_get("/ws/power", _handle_ws_power)
    # No "require": power state is visible to every permission level;
    # only the toggle button and the API write need read-write/admin.
    # The WebConsoleAdapter enriches this entry with a live "links" list of
    # PDU names on every page render (soft reloads change the PDU set).
    return {"nav": [{"title": "Power", "path": "/power"}]}
