from aiohttp import web
from typing import Any, Dict, Optional, Tuple
import time
import uuid

from openmux.server.config_manager import ConfigManager


def _find_config_manager(adapter) -> Optional[ConfigManager]:
    """Best-effort resolution of ConfigManager from the running server.

    Tries common attachment points to be resilient to wiring differences:
      1) adapter.console_manager.server.config_manager
      2) adapter.console_manager.port_manager.config_manager
      3) adapter.console_manager.config_manager
      4) adapter.config_manager
    Returns None if not found.
    """
    try:
        cm = None
        # 1) server.config_manager
        try:
            cm = getattr(getattr(getattr(adapter, "console_manager", None), "server", None), "config_manager", None)
            if cm:
                return cm  # type: ignore[return-value]
        except Exception:
            pass
        # 2) port_manager.config_manager
        try:
            cm = getattr(getattr(getattr(adapter, "console_manager", None), "port_manager", None), "config_manager", None)
            if cm:
                return cm  # type: ignore[return-value]
        except Exception:
            pass
        # 3) console_manager.config_manager (direct)
        try:
            cm = getattr(getattr(adapter, "console_manager", None), "config_manager", None)
            if cm:
                return cm  # type: ignore[return-value]
        except Exception:
            pass
        # 4) adapter.config_manager (unlikely, but cheap)
        try:
            cm = getattr(adapter, "config_manager", None)
            if cm:
                return cm  # type: ignore[return-value]
        except Exception:
            pass
        return None
    except Exception:
        return None

# Minimal config editor plugin with read-only GET and guarded POST apply.
# Requires admin permissions for write operations and CSRF token for session-auth requests.


async def _handle_view(request: web.Request) -> web.StreamResponse:
    adapter = request.app["adapter"]
    username = request.get("username")
    if not username:
        raise web.HTTPUnauthorized()
    # Render a Jinja2 template if available; fall back to JSON only if template missing
    try:
        env = getattr(adapter, "_jinja_env", None)
        if env:
            tmpl = env.get_template("config_editor.html.j2")
            plugin_nav = adapter._get_allowed_plugin_nav(username) if hasattr(adapter, "_get_allowed_plugin_nav") else []
            
            ports = adapter._get_ports_snapshot() if hasattr(adapter, "_get_ports_snapshot") else []
            current_port = request.query.get("port") or request.query.get("console")

            # Attempt to load defaults from docs/DEFAULTS.md for UI hinting
            import json as _json
            defaults_doc = _read_defaults_doc()
            # Compute effective base path for links/assets in the template
            try:
                base_path = adapter._effective_base_path(request) if hasattr(adapter, "_effective_base_path") else ""
            except Exception:
                base_path = ""
            html_text = tmpl.render(
                realm=adapter.realm,
                logo_url=adapter._get_logo_url() if hasattr(adapter, "_get_logo_url") else None,
                title="OpenMux Config Editor",
                plugin_nav=plugin_nav,
                defaults_doc_json=_json.dumps(defaults_doc),
                base_path=base_path,
                ports=ports,
                current_port=current_port,
            )
            return web.Response(body=html_text.encode("utf-8"), content_type="text/html")
    except Exception:
        pass
    # Fallback JSON (if templates not available)
    try:
        cm = _find_config_manager(adapter)
        config = cm.config if cm and getattr(cm, "config", None) is not None else {}
    except Exception:
        config = {}
    import json
    return web.Response(body=json.dumps({"config": config}).encode("utf-8"), content_type="application/json")


async def _handle_data(request: web.Request) -> web.StreamResponse:
    """Return current effective config as JSON for the editor UI."""
    adapter = request.app["adapter"]
    username = request.get("username")
    if not username:
        raise web.HTTPUnauthorized()
    try:
        cm = _find_config_manager(adapter)
        config = cm.config if cm and getattr(cm, "config", None) is not None else {}
    except Exception:
        config = {}
    import json
    return web.Response(body=json.dumps({"config": config}).encode("utf-8"), content_type="application/json")


async def _handle_apply(request: web.Request) -> web.StreamResponse:
    adapter = request.app["adapter"]
    try:
        # Enforce admin role and CSRF
        adapter._require_permission(request, ("admin",))
        if not adapter._check_csrf(request):
            raise web.HTTPForbidden(text="CSRF")

        # Load incoming config and validate structure as dict
        try:
            payload = await request.json()
        except Exception as e:
            adapter.logger.warning("Invalid JSON body for config apply", exc_info=True)
            return web.json_response({"error": True, "message": "Invalid JSON"}, status=400)
        if not isinstance(payload, dict):
            adapter.logger.warning("Config apply: body must be a JSON object (got %s)", type(payload).__name__)
            return web.json_response({"error": True, "message": "Body must be a JSON object"}, status=400)

        # Access ConfigManager
        cm = _find_config_manager(adapter)
        if not cm:
            adapter.logger.error("ConfigManager unavailable for apply()")
            return web.json_response({"error": True, "message": "ConfigManager unavailable"}, status=500)

        # Validate before saving
        ok, err, exc = _validate_payload(payload, cm)
        if not ok:
            try:
                if exc is not None:
                    adapter.logger.exception("Config validation failed: %s", err or "<no message>")
                else:
                    adapter.logger.error("Config validation failed: %s", err or "<no message>")
            except Exception:
                pass
            return web.json_response({"error": True, "message": err or "Validation failed"}, status=400)

        # Persist config
        try:
            saved = bool(cm.save_config(payload))
        except Exception:
            # Log full traceback for debugging; return 500 to UI with message
            adapter.logger.exception("Config save failed")
            return web.json_response({"error": True, "message": "Config save failed"}, status=500)

        if not saved:
            adapter.logger.error("Config save returned False (no changes written)")
            return web.json_response({"error": True, "message": "Config save failed (no changes written)"}, status=500)

        return web.json_response({"ok": True})

    except web.HTTPException:
        # Let HTTP errors propagate (403/401 etc.). These are not internal failures.
        raise
    except Exception:
        # Catch-all for unexpected errors; log full traceback for diagnostics
        adapter.logger.exception("Unhandled error during config apply")
        return web.json_response({"error": True, "message": "Internal server error"}, status=500)


async def _handle_reload_soft(request: web.Request) -> web.StreamResponse:
    """Perform a soft reload of configuration without restarting the server.

    Steps:
      - Require admin + CSRF
      - Reload YAML from disk using ConfigManager.load_config()
      - Update AuthManager live config
      - Reconcile ports for adapters that support online updates (serial, loopback, command, tcp initiator)
      - Do NOT restart connection endpoints (web console, client listener, muxcon). Those require full restart.

    Returns a JSON summary of applied changes per adapter or an error JSON.
    """
    adapter = request.app["adapter"]
    req_id = uuid.uuid4().hex[:8]
    username = request.get("username")
    try:
        adapter.logger.info(f"[reload-soft:{req_id}] request from {request.remote or '?'} user={username or '?'}")
    except Exception:
        pass
    # AuthN/Z
    adapter._require_permission(request, ("admin",))
    # Log presence of CSRF header to aid debugging (not the value)
    try:
        has_csrf = bool(request.headers.get("X-OMX-CSRF"))
        adapter.logger.debug(f"[reload-soft:{req_id}] CSRF header present={has_csrf}")
    except Exception:
        pass
    if not adapter._check_csrf(request):
        try:
            adapter.logger.warning(f"[reload-soft:{req_id}] CSRF check failed")
        except Exception:
            pass
        raise web.HTTPForbidden(text="CSRF")

    # Resolve managers
    cm = _find_config_manager(adapter)
    if not cm:
        try:
            adapter.logger.error(f"[reload-soft:{req_id}] ConfigManager unavailable")
        except Exception:
            pass
        return web.json_response({"error": True, "message": "ConfigManager unavailable"}, status=500)

    # Best-effort access to the running server instance for auth/ports
    server = getattr(getattr(adapter, "console_manager", None), "server", None)
    auth = getattr(adapter, "auth_manager", None)
    port_mgr = getattr(adapter, "main_port_manager", None) or getattr(getattr(adapter, "console_manager", None), "port_manager", None)

    summary: Dict[str, Any] = {"auth_updated": False, "adapters": {}}
    try:
        # Reload YAML from disk and validate
        cfg_path = getattr(cm, "config_path", None)
        adapter.logger.info(f"[reload-soft:{req_id}] Loading config from {cfg_path}")
        start = time.time()
        new_cfg = cm.load_config()
        adapter.logger.info(f"[reload-soft:{req_id}] Config loaded OK in {time.time()-start:.3f}s")
    except Exception as e:
        try:
            adapter.logger.exception(f"[reload-soft:{req_id}] Config load failed: {e}")
        except Exception:
            pass
        return web.json_response({"error": True, "message": f"Config load failed: {e}"}, status=400)

    # Update authentication live
    try:
        if auth and hasattr(auth, "update_config"):
            adapter.logger.debug(f"[reload-soft:{req_id}] Updating AuthManager config")
            await auth.update_config(new_cfg.get("authentication", {}))
            summary["auth_updated"] = True
            adapter.logger.info(f"[reload-soft:{req_id}] AuthManager updated")
    except Exception as e:
        # Non-fatal; continue with port reconciliation
        try:
            adapter.logger.error(f"[reload-soft:{req_id}] Auth update failed: {e}", exc_info=True)
        except Exception:
            pass

    # Reconcile ports for adapters that support it
    adapters = []
    try:
        if server and hasattr(server, "unified_adapters"):
            adapters = list(getattr(server, "unified_adapters") or [])
        elif port_mgr and hasattr(port_mgr, "unified_adapters"):
            adapters = list(getattr(port_mgr, "unified_adapters") or [])
        else:
            adapters = []
        adapter.logger.debug(f"[reload-soft:{req_id}] Found {len(adapters)} unified adapters for reconciliation")
    except Exception as e:
        try:
            adapter.logger.error(f"[reload-soft:{req_id}] Adapter discovery failed: {e}", exc_info=True)
        except Exception:
            pass
        adapters = []

    # Pull sections from config
    serial_section = new_cfg.get("serial_ports")
    loopback_section = new_cfg.get("loopback_ports")
    command_section = new_cfg.get("command_ports")
    tcp_init_section = new_cfg.get("tcp_initiator_ports") or new_cfg.get("openmux_client_ports")
    try:
        def count_items(x):
            if x is None:
                return 0
            if isinstance(x, dict):
                # section may be {"serial_ports": [...]}
                for v in x.values():
                    if isinstance(v, list):
                        return len(v)
                return 0
            if isinstance(x, list):
                return len(x)
            return 1
        adapter.logger.info(
            f"[reload-soft:{req_id}] sections: serial={count_items(serial_section)} loopback={count_items(loopback_section)} command={count_items(command_section)} tcp_initiator={count_items(tcp_init_section)}"
        )
    except Exception:
        pass

    for a in adapters or []:
        try:
            atype = None
            try:
                atype = a.get_adapter_type() if hasattr(a, "get_adapter_type") else getattr(a, "adapter_type", None)
            except Exception:
                atype = getattr(a, "adapter_type", None)
            key = (str(atype) if atype else "").lower()
            # Serial
            if key == "serial" and hasattr(a, "reconcile_ports") and serial_section is not None:
                try:
                    adapter.logger.debug(f"[reload-soft:{req_id}] Reconciling serial ports")
                    res = await a.reconcile_ports(serial_section)
                    summary["adapters"].setdefault("serial", res)
                except Exception as e:
                    adapter.logger.error(f"[reload-soft:{req_id}] Serial reconcile error: {e}", exc_info=True)
                    summary["adapters"]["serial"] = {"error": str(e)}
            elif key == "serial" and serial_section is None:
                adapter.logger.debug(f"[reload-soft:{req_id}] Skipping serial: no section in config")
            # Loopback
            if key == "loopback" and hasattr(a, "reconcile_ports") and loopback_section is not None:
                try:
                    adapter.logger.debug(f"[reload-soft:{req_id}] Reconciling loopback ports")
                    res = await a.reconcile_ports(loopback_section)
                    summary["adapters"].setdefault("loopback", res)
                except Exception as e:
                    adapter.logger.error(f"[reload-soft:{req_id}] Loopback reconcile error: {e}", exc_info=True)
                    summary["adapters"]["loopback"] = {"error": str(e)}
            elif key == "loopback" and loopback_section is None:
                adapter.logger.debug(f"[reload-soft:{req_id}] Skipping loopback: no section in config")
            # Command
            if key == "command" and hasattr(a, "reconcile_ports") and command_section is not None:
                try:
                    adapter.logger.debug(f"[reload-soft:{req_id}] Reconciling command ports")
                    res = await a.reconcile_ports(command_section)
                    summary["adapters"].setdefault("command", res)
                except Exception as e:
                    adapter.logger.error(f"[reload-soft:{req_id}] Command reconcile error: {e}", exc_info=True)
                    summary["adapters"]["command"] = {"error": str(e)}
            elif key == "command" and command_section is None:
                adapter.logger.debug(f"[reload-soft:{req_id}] Skipping command: no section in config")
            # TCP initiator (client side ports)
            if key == "tcp_initiator" and hasattr(a, "reconcile_ports") and tcp_init_section is not None:
                try:
                    adapter.logger.debug(f"[reload-soft:{req_id}] Reconciling tcp_initiator ports")
                    res = await a.reconcile_ports(tcp_init_section)
                    summary["adapters"].setdefault("tcp_initiator", res)
                except Exception as e:
                    adapter.logger.error(f"[reload-soft:{req_id}] TCP initiator reconcile error: {e}", exc_info=True)
                    summary["adapters"]["tcp_initiator"] = {"error": str(e)}
            elif key == "tcp_initiator" and tcp_init_section is None:
                adapter.logger.debug(f"[reload-soft:{req_id}] Skipping tcp_initiator: no section in config")
        except Exception:
            # Continue with others
            continue
    try:
        adapter.logger.info(f"[reload-soft:{req_id}] Completed with summary: {summary}")
    except Exception:
        pass
    return web.json_response({"ok": True, "summary": summary})


async def _handle_reload_full(request: web.Request) -> web.StreamResponse:
    """Trigger a full adapter reload (stop/recreate/start) using server API.

    This will interrupt listeners and reconnect paths; clients may be dropped.
    Requires admin + CSRF. Returns a summary of stopped/started adapters and errors.
    """
    adapter = request.app["adapter"]
    req_id = uuid.uuid4().hex[:8]
    username = request.get("username")
    try:
        adapter.logger.info(f"[reload-full:{req_id}] request from {request.remote or '?'} user={username or '?'}")
    except Exception:
        pass
    adapter._require_permission(request, ("admin",))
    # Log presence of CSRF header to aid debugging (not the value)
    try:
        has_csrf = bool(request.headers.get("X-OMX-CSRF"))
        adapter.logger.debug(f"[reload-full:{req_id}] CSRF header present={has_csrf}")
    except Exception:
        pass
    if not adapter._check_csrf(request):
        try:
            adapter.logger.warning(f"[reload-full:{req_id}] CSRF check failed")
        except Exception:
            pass
        raise web.HTTPForbidden(text="CSRF")

    server = getattr(getattr(adapter, "console_manager", None), "server", None)
    if not server or not hasattr(server, "reload_adapters_full"):
        try:
            adapter.logger.error(f"[reload-full:{req_id}] Server reload API unavailable (server={bool(server)})")
        except Exception:
            pass
        return web.json_response({"error": True, "message": "Server reload API unavailable"}, status=500)
    try:
        adapter.logger.info(f"[reload-full:{req_id}] Invoking server.reload_adapters_full()")
        start = time.time()
        ctx = {
            "req_id": req_id,
            "origin": "config-editor",
            "remote": request.remote or "?",
            "user": username or "?",
            "web_adapter_name": getattr(adapter, 'name', None) or 'web_console',
        }
        summary = await server.reload_adapters_full(context=ctx)
        adapter.logger.info(f"[reload-full:{req_id}] Completed in {time.time()-start:.3f}s summary={summary}")
        return web.json_response({"ok": True, "summary": summary})
    except Exception as e:
        try:
            adapter.logger.error(f"[reload-full:{req_id}] Full reload failed: {e}", exc_info=True)
        except Exception:
            pass
        return web.json_response({"error": True, "message": str(e)}, status=500)


def _validate_payload(payload: Dict[str, Any], cm: ConfigManager) -> Tuple[bool, Optional[str], Optional[BaseException]]:
    """Validate a config payload using ConfigManager's validation logic.

    Does not persist any changes. Returns (ok, error_message).
    """
    try:
        # Create a throwaway manager pointing at the same path to reuse behavior
        temp_cm = ConfigManager(cm.config_path)
        # Directly assign and validate
        temp_cm.config = payload
        # Use internal validation routine; it raises on error
        temp_cm._validate_config()  # type: ignore[attr-defined]
        return True, None, None
    except Exception as e:
        # Log full traceback to aid debugging of 400 validation errors
        try:
            logger = getattr(cm, "logger", None)
            if logger is not None:
                logger.exception("Config validation failed")
        except Exception:
            pass
        # Ensure non-empty, useful error messages are returned to the UI
        msg = str(e).strip()
        if not msg:
            msg = f"{e.__class__.__name__} during validation"
        return False, msg, e


async def _handle_validate(request: web.Request) -> web.StreamResponse:
    adapter = request.app["adapter"]
    # Require admin permission (no CSRF needed as no state change occurs)
    adapter._require_permission(request, ("admin",))
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": True, "message": "Invalid JSON"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": True, "message": "Body must be a JSON object"}, status=400)

    cm = _find_config_manager(adapter)
    if not cm:
        return web.json_response({"error": True, "message": "ConfigManager unavailable"}, status=500)

    ok, err, exc = _validate_payload(payload, cm)
    if ok:
        return web.json_response({"ok": True})
    try:
        if exc is not None:
            adapter.logger.exception("Config validation failed: %s", err or "<no message>")
        else:
            adapter.logger.error("Config validation failed: %s", err or "<no message>")
    except Exception:
        pass
    return web.json_response({"ok": False, "error": True, "message": err or "Validation failed"}, status=400)


async def _handle_schema(request: web.Request) -> web.StreamResponse:
    """Return a JSON Schema describing the config shape for UI form building.

    Attempts to load the authoritative YAML-formatted JSON Schema from the
    repository (docs/to_check/openmux_config_schema.yaml). If unavailable or
    invalid, falls back to a permissive minimal schema. Server-side validation
    via ConfigManager remains the source of truth.
    """
    # Admin-only visibility for the schema endpoint
    adapter = request.app["adapter"]
    adapter._require_permission(request, ("admin",))
    # Try to load YAML schema from well-known locations
    schema: Dict[str, Any] = {}
    loaded = False
    try:
        import os
        from pathlib import Path
        import yaml  # type: ignore

        # Optional override via environment variable
        env_path = os.environ.get("OPENMUX_CONFIG_SCHEMA")
        candidates = []
        if env_path:
            candidates.append(Path(env_path))
        # Repo root relative to this file: openmux/server/web_plugins/ -> repo_root/docs/to_check/...
        try:
            repo_root = Path(__file__).resolve().parents[3]
            candidates.append(repo_root / "docs" / "to_check" / "openmux_config_schema.yaml")
        except Exception:
            pass
        # Also check current working directory mirror path (when running from repo root)
        try:
            candidates.append(Path.cwd() / "docs" / "to_check" / "openmux_config_schema.yaml")
        except Exception:
            pass

        for p in candidates:
            try:
                if not p:
                    continue
                if p.is_file():
                    with p.open("r", encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                    if isinstance(data, dict) and data:
                        schema = data  # type: ignore[assignment]
                        loaded = True
                        break
            except Exception:
                continue
    except Exception:
        loaded = False

    if not loaded:
        # Permissive fallback schema
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "OpenMux Server Configuration",
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "server": {"type": "object"},
                "authentication": {"type": "object"},
                "logging": {"type": "object"},
                "client_listener": {"type": "object"},
                "serial_ports": {"type": ["array", "object"]},
                "loopback_ports": {"type": ["array", "object"]},
                "command_ports": {"type": ["array", "object"]},
                "muxcon": {"type": "object"},
                "web_status": {"type": "object"},
                "web_console": {"type": "object"},
            },
        }

    import json
    return web.Response(body=json.dumps({"schema": schema, "authoritative": loaded}).encode("utf-8"), content_type="application/json")


def register_plugin(app: web.Application, adapter, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = "/plugins/config-editor"
    app.router.add_get(base, _handle_view)
    app.router.add_get(base + "/data", _handle_data)
    app.router.add_get(base + "/schema", _handle_schema)
    app.router.add_post(base + "/apply", _handle_apply)
    app.router.add_post(base + "/validate", _handle_validate)
    app.router.add_post(base + "/reload/soft", _handle_reload_soft)
    app.router.add_post(base + "/reload/full", _handle_reload_full)
    return {
        "nav": [
            {"title": "Config Editor", "path": base, "require": "admin"},
        ]
    }


def _read_defaults_doc() -> Dict[str, Any]:
    """Parse docs/DEFAULTS.md into a simple defaults mapping for the UI.

    Returns a dict with keys:
      - dot: { 'section.key': value, ... } for simple fields
      - sections: { 'sectionId': { 'key': value, ... }, ... } for table sections

    Parsing is best-effort and based on known headings in the DEFAULTS.md file.
    """
    try:
        from pathlib import Path
        import re

        # Locate docs/DEFAULTS.md relative to repo root
        # repo_root: openmux/server/web_plugins/ -> repo_root
        repo_root = Path(__file__).resolve().parents[3]
        md_path = repo_root / "docs" / "DEFAULTS.md"
        if not md_path.is_file():
            # Try current working directory as a fallback
            alt = Path.cwd() / "docs" / "DEFAULTS.md"
            if alt.is_file():
                md_path = alt
            else:
                return {"dot": {}, "sections": {}}

        text = md_path.read_text(encoding="utf-8")

        # Known top-level section names mapping to config prefixes or table IDs
        # For table sections, we use the same IDs as our template buildTable() roots
        top_sections = {
            "server": ("server", False),
            "client_listener": ("client_listener", False),  # (prefix, is_table)
            "web_status": ("web_status", False),
            "web_console": ("web_console", False),
            "logging": ("logging", False),
            "serial_ports": ("serial_ports", True),
            "loopback_ports": ("loopback_ports", True),
            "command_ports": ("command_ports", True),
            "muxcon": ("muxcon", False),
        }

        # Subsections within muxcon that map to sections or dot-paths
        muxcon_subsections = {
            "listeners[*]": ("muxcon.listeners", True),
            "initiators[*]": ("muxcon.initiators", True),
            "heartbeats & timing": ("muxcon", False),
            "multipath": ("muxcon", False),
            "retransmissions": ("muxcon", False),
            "federated_cache": ("muxcon", False),
        }

        dot: Dict[str, Any] = {}
        sections: Dict[str, Dict[str, Any]] = {}

        cur_top: Optional[str] = None
        cur_top_key: Optional[str] = None
        cur_sub: Optional[str] = None

        # Regexes
        bullet_kv = re.compile(r"^\s*-\s*([a-zA-Z0-9_\.\[\]\* ]+):\s*(.+?)\s*$")
        heading = re.compile(r"^([a-zA-Z0-9_ ]+)(?:\s*\(.*\))?\s*$")

        def parse_value(val: str):
            v = val.strip()
            # Booleans
            if v.lower() in ("true", "false"):
                return v.lower() == "true"
            # Numbers (int or float)
            try:
                if v.startswith("0") and v != "0" and not "." in v:
                    # keep as string (paths like 0.0.0.0 handled below)
                    pass
                else:
                    if "." in v:
                        return float(v)
                    return int(v)
            except Exception:
                pass
            # IP-like values or paths remain strings
            return v

        for raw_line in text.splitlines():
            line = raw_line.rstrip("\n")
            if not line.strip():
                continue
            if not line.startswith(" ") and not line.startswith("-"):
                # New top-level heading candidate
                m = heading.match(line.strip())
                if m:
                    name = m.group(1).strip()
                    key = None
                    # Normalize known headings that include annotation text
                    # e.g., "serial_ports (per-port) (schema defaults, plus adapter runtime behavior)"
                    for k in list(top_sections.keys()):
                        if name.startswith(k):
                            key = k
                            break
                    if key:
                        cur_top = key
                        cur_top_key = top_sections[key][0]
                        cur_sub = None
                        # Ensure table dict exists when needed
                        if top_sections[key][1] and cur_top_key not in sections:
                            sections[cur_top_key] = {}
                        continue
                # If not a recognized heading, skip
                continue

            # Subsection under muxcon recognized via bullet style marker ending with ':'
            if line.strip().startswith("- ") and line.strip().endswith(":"):
                label = line.strip()[2:-1].strip()
                if cur_top == "muxcon" and label in muxcon_subsections:
                    cur_sub = label
                else:
                    cur_sub = None
                continue

            # Key-value bullets
            m = bullet_kv.match(line)
            if not m:
                continue
            key, value = m.group(1).strip(), m.group(2).strip()
            val = parse_value(value)

            # Decide where to store
            if cur_top == "muxcon" and cur_sub:
                sub_target, is_table = muxcon_subsections[cur_sub]
                if cur_sub == "initiators[*]" and key.startswith("options."):
                    # options.* defaults
                    base = f"{sub_target}"
                    # table section defaults: only store the option key after 'options.'
                    opt_key = key  # keep full 'options.xxx' for UI mapping
                    sections.setdefault(base, {})
                    sections[base][opt_key] = val
                elif cur_sub in ("listeners[*]",):
                    base = f"{sub_target}"
                    sections.setdefault(base, {})
                    sections[base][key] = val
                elif cur_sub in ("heartbeats & timing", "multipath", "retransmissions", "federated_cache"):
                    # Map directly under muxcon.* in dot map
                    dot[f"muxcon.{key}"] = val
                continue

            # Generic handling for top-level simple sections
            if cur_top and cur_top_key and cur_top in top_sections:
                prefix, is_table = top_sections[cur_top]
                if is_table:
                    sections.setdefault(prefix, {})
                    sections[prefix][key] = val
                else:
                    dot[f"{prefix}.{key}"] = val

        return {"dot": dot, "sections": sections}
    except Exception:
        return {"dot": {}, "sections": {}}
