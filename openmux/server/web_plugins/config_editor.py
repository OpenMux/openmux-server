from aiohttp import web
from typing import Any, Dict, List, Optional, Set, Tuple
import time
import uuid

from openmux.server.config_manager import ConfigManager
from . import ADAPTER_APP_KEY


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


def _get_writable_metadata(cm: Optional[ConfigManager]) -> Tuple[List[str], bool]:
    if not cm:
        return [], False
    try:
        policy = cm.get_security_policy()
        return sorted(policy.get_writable_sections()), policy.is_config_editor_enforced()
    except Exception:
        return [], False


def _normalize_section_value(value: Any) -> Any:
    if isinstance(value, dict):
        normalized_dict = {}
        for key, val in value.items():
            normalized_val = _normalize_section_value(val)
            if normalized_val is not None:
                normalized_dict[key] = normalized_val
        return normalized_dict or None
    if isinstance(value, list):
        normalized_list = []
        for item in value:
            normalized_item = _normalize_section_value(item)
            if normalized_item is not None:
                normalized_list.append(normalized_item)
        return normalized_list or None
    if value is None:
        return None
    if isinstance(value, str):
        return value if value != "" else None
    return value


def _detect_modified_sections(current: Optional[Dict[str, Any]], new_cfg: Dict[str, Any]) -> Set[str]:
    modified: Set[str] = set()
    current = current or {}
    keys = set(current.keys()) | set(new_cfg.keys())
    for key in keys:
        if _normalize_section_value(current.get(key)) != _normalize_section_value(new_cfg.get(key)):
            modified.add(key)
    return modified


def _enforce_writable_sections(cm: ConfigManager, payload: Dict[str, Any]) -> Set[str]:
    try:
        policy = cm.get_security_policy()
    except Exception:
        return set()
    if not policy.is_config_editor_enforced():
        return set()
    current_cfg = cm.config or cm.load_config() or {}
    modified = _detect_modified_sections(current_cfg, payload)
    writable = policy.get_writable_sections()
    if not modified:
        return set()
    if not writable:
        return modified
    return {section for section in modified if section not in writable}

# Minimal config editor plugin with read-only GET and guarded POST apply.
# Requires admin permissions for write operations and CSRF token for session-auth requests.


async def _handle_view(request: web.Request) -> web.StreamResponse:
    adapter = request.app[ADAPTER_APP_KEY]
    username = request.get("username")
    if not username:
        raise web.HTTPUnauthorized()
    # Enforce admin permission for the UI
    adapter._require_permission(request, ("admin",))
    # Render a Jinja2 template if available; fall back to JSON only if template missing
    user_permission: Optional[str] = None
    if hasattr(adapter, "_get_effective_permission"):
        try:
            user_permission = adapter._get_effective_permission(username, request)  # type: ignore[attr-defined]
        except Exception:
            user_permission = None
    try:
        env = getattr(adapter, "_jinja_env", None)
        if env:
            tmpl = env.get_template("config_editor.html.j2")
            plugin_nav = adapter._get_allowed_plugin_nav(username, request=request) if hasattr(adapter, "_get_allowed_plugin_nav") else []
            
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
            cm = _find_config_manager(adapter)
            writable_sections, writable_enforced = _get_writable_metadata(cm)
            html_text = tmpl.render(
                realm=adapter.realm,
                logo_url=adapter._get_logo_url() if hasattr(adapter, "_get_logo_url") else None,
                title="OpenMux Config Editor",
                plugin_nav=plugin_nav,
                defaults_doc_json=_json.dumps(defaults_doc),
                base_path=base_path,
                ports=ports,
                current_port=current_port,
                user_permission=user_permission,
                writable_sections=writable_sections,
                writable_enforced=writable_enforced,
            )
            return web.Response(body=html_text.encode("utf-8"), content_type="text/html")
    except Exception:
        pass
    # Fallback JSON (if templates not available)
    try:
        cm = _find_config_manager(adapter)
        config = cm.config if cm and getattr(cm, "config", None) is not None else {}
        writable_sections, writable_enforced = _get_writable_metadata(cm)
    except Exception:
        config = {}
        writable_sections, writable_enforced = [], False
    import json
    return web.Response(
        body=json.dumps(
            {
                "config": config,
                "writable_sections": writable_sections,
                "writable_enforced": writable_enforced,
            }
        ).encode("utf-8"),
        content_type="application/json",
    )


async def _handle_data(request: web.Request) -> web.StreamResponse:
    """Return current effective config as JSON for the editor UI."""
    adapter = request.app[ADAPTER_APP_KEY]
    username = request.get("username")
    if not username:
        raise web.HTTPUnauthorized()
    # Enforce admin permission for data access
    adapter._require_permission(request, ("admin",))
    try:
        cm = _find_config_manager(adapter)
        config = cm.config if cm and getattr(cm, "config", None) is not None else {}
        writable_sections, writable_enforced = _get_writable_metadata(cm)
    except Exception:
        config = {}
        writable_sections, writable_enforced = [], False
    import json
    return web.Response(
        body=json.dumps(
            {
                "config": config,
                "writable_sections": writable_sections,
                "writable_enforced": writable_enforced,
            }
        ).encode("utf-8"),
        content_type="application/json",
    )


async def _handle_apply(request: web.Request) -> web.StreamResponse:
    adapter = request.app[ADAPTER_APP_KEY]
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

        disallowed = _enforce_writable_sections(cm, payload)
        if disallowed:
            detail = ", ".join(sorted(disallowed))
            return web.json_response(
                {
                    "error": True,
                    "message": f"Changes to {detail} are blocked by the security policy",
                },
                status=403,
            )

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
    """Perform a soft reload by delegating to the server's reload_adapters_soft().

    Mirrors _handle_reload_full which delegates to server.reload_adapters_full().
    All reload logic (config load, auth update, bootstrap, reconcile) lives in
    the server method so CLI and web paths share a single implementation.
    """
    adapter = request.app[ADAPTER_APP_KEY]
    req_id = uuid.uuid4().hex[:8]
    username = request.get("username")
    try:
        adapter.logger.info(f"[reload-soft:{req_id}] request from {request.remote or '?'} user={username or '?'}")
    except Exception:
        pass
    adapter._require_permission(request, ("admin",))
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

    server = getattr(getattr(adapter, "console_manager", None), "server", None)
    if not server or not hasattr(server, "reload_adapters_soft"):
        try:
            adapter.logger.error(f"[reload-soft:{req_id}] Server reload API unavailable (server={bool(server)})")
        except Exception:
            pass
        return web.json_response({"error": True, "message": "Server reload API unavailable"}, status=500)
    try:
        ctx = {
            "req_id": req_id,
            "origin": "config-editor",
            "remote": request.remote or "?",
            "user": username or "?",
            "web_adapter_name": getattr(adapter, "name", None) or "web_console",
        }
        summary = await server.reload_adapters_soft(context=ctx)
        return web.json_response({"ok": True, "summary": summary})
    except Exception as e:
        try:
            adapter.logger.error(f"[reload-soft:{req_id}] Soft reload failed: {e}", exc_info=True)
        except Exception:
            pass
        return web.json_response({"error": True, "message": str(e)}, status=500)


async def _handle_reload_full(request: web.Request) -> web.StreamResponse:
    """Trigger a full adapter reload (stop/recreate/start) using server API.

    This will interrupt listeners and reconnect paths; clients may be dropped.
    Requires admin + CSRF. Returns a summary of stopped/started adapters and errors.
    """
    adapter = request.app[ADAPTER_APP_KEY]
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
        temp_cm = ConfigManager(
            cm.config_path,
            auth_config_path=getattr(cm, "auth_config_path", None),
            security_config_path=getattr(cm, "security_config_path", None),
        )
        # Directly assign and validate
        temp_cm.config = payload
        # Use internal validation routine; it raises on error
        temp_cm._validate_config(allow_inline_authentication=True)  # type: ignore[attr-defined]
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
    adapter = request.app[ADAPTER_APP_KEY]
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
    adapter = request.app[ADAPTER_APP_KEY]
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
            "telnet_listener": ("telnet_listener", True),
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
