"""Power monitoring web routes (core web console feature).

Route handlers for the standalone Power surface:
- `GET /power`                - PDU list page
- `GET /power/{pdu_name}`     - per-PDU outlet page
- `GET /api/power`            - full power snapshot JSON
- `POST /api/power/outlets/{outlet_ref}`  - switch one outlet (read-write or admin, CSRF)

Live outlet updates do not use a dedicated socket: the web console publishes
a coalesced ``{"type": "power_changed"}`` nudge on the shared `/ws/events`
site socket, and the Power pages re-fetch `/api/power` on it (see
``openmux/server/site_events.py``).

The routes are CORE: the web console registers them unconditionally
(`WebConsoleAdapter.start`), so they follow a `power:` section added or
removed on a soft reload without a restart. Each route replies 404 -
"Power management is not configured" - when no PDU adapter is active, the
same as every other feature-gated core path. The sidebar "Power" item is
equally core: the web console emits it per page when a PDU adapter is
enabled (``_get_allowed_plugin_nav`` adds it in addition to real plugin
nav).

The in-session console power badge/menu is served by the core web console
itself (OMXCTRL power frames) and does not use these handlers.
"""

from aiohttp import web

from . import get_web_adapter


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
        raise web.HTTPNotFound(text="Power management is not configured\n")
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
    blocked = pdu._power_blocked_ports(ref, username)
    if blocked:
        return web.json_response(
            {
                "error": True,
                "message": f"{ref} feeds consoles outside your groups ({', '.join(blocked)}); switching it needs admin",
            },
            status=403,
        )
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
    # The Power pages re-fetch /api/power on a "power_changed" nudge. That nudge
    # rides the per-port meta event, so an outlet that feeds no console carries
    # no port to emit it and an open Power page would keep showing the position
    # this call just changed. Kick the nudge for every switch: the port path may
    # kick it too, and the coalescer collapses the pair into one frame.
    kicker = getattr(adapter, "_kick_site_nudge", None)
    if callable(kicker):
        kicker("power_changed")
    return web.json_response({"ok": True, "reading": result.get("reading"), "impact": result.get("impact")})
