from aiohttp import web
from typing import Any, Dict, Optional

from . import ADAPTER_APP_KEY

# OS customizer plugin (skeleton). Provides read-only view and a stub endpoint
# to apply changes via a privileged helper (not implemented here).


async def _handle_view(request: web.Request) -> web.StreamResponse:
    adapter = request.app[ADAPTER_APP_KEY]
    username = request.get("username")
    if not username:
        raise web.HTTPUnauthorized()
    # Expose minimal host facts; avoid reading sensitive files.
    import platform
    facts = {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
    }
    import json
    return web.Response(body=json.dumps({"facts": facts}).encode("utf-8"), content_type="application/json")


async def _handle_apply_network(request: web.Request) -> web.StreamResponse:
    adapter = request.app[ADAPTER_APP_KEY]
    adapter._require_permission(request, ("admin",))
    if not adapter._check_csrf(request):
        raise web.HTTPForbidden(text="CSRF")
    # Stub: integration point for a privileged helper; we only validate payload shape.
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": True, "message": "Invalid JSON"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": True, "message": "Body must be a JSON object"}, status=400)

    # Not implemented; respond with accepted=false to indicate stub
    return web.json_response({"ok": False, "message": "Not implemented: requires privileged helper"}, status=501)


def register_plugin(app: web.Application, adapter, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = "/plugins/os-customizer"
    app.router.add_get(base, _handle_view)
    app.router.add_post(base + "/network", _handle_apply_network)
    return {
        "nav": [
            {"title": "OS", "path": base, "require": "admin"},
        ]
    }
