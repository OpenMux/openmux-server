"""Web Console Adapter (WebSocket per port + minimal xterm.js UI).

Provides a tiny integrated web server that serves:
- GET /                -> Minimal HTML page with xterm.js terminal
- WS  /ws/<port_name>  -> WebSocket stream bound to a specific port

Authentication: HTTP Basic Auth using usernames/passwords from the main
authentication config (via AuthManager). The landing HTML and the WebSocket
handshake both require Basic Auth. Browsers typically reuse the credentials
from the page load for same-origin WebSocket requests.

Notes:
- Uses the existing ConsoleManager and PortManager integration: the adapter
  registers itself as a client manager and participates in data forwarding.
- Keeps imports of optional dependencies inside methods to avoid import-time
  failures when the adapter isn't enabled.
"""

import asyncio
import base64
import html
import logging
import time
import os
import ssl
import hmac
import hashlib
import json
import re
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from typing import Callable

from aiohttp import web
from openmux.server.port_utils import safe_get_port
from openmux.server.web_plugins import ADAPTER_APP_KEY
from openmux.server.data_logger import DataLogger
import secrets
import urllib.parse
import importlib

try:  # Prefer importlib.metadata (std lib)
    from importlib.metadata import version as _dist_version  # type: ignore
except Exception:  # pragma: no cover
    _dist_version = None  # type: ignore

from .base_adapter import AdapterCapability, BaseGenericAdapter

# Default inline HTML is deprecated; we now render a Jinja2 template.
# Keep a minimal fallback in case template rendering fails catastrophically.
_HTML_FALLBACK = b"""<!doctype html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>OpenMux Web Console</title>
                                <link rel=\"stylesheet\" href=\"%BASE%/static/xterm/css/xterm.css\" />
                                <link rel=\"stylesheet\" href=\"%BASE%/static/web_console.css\" />
  </head>
  <body>
        <header class=\"toolbar\">
      Port: <input id=\"port\" placeholder=\"loopback1\"/>
      <button id=\"connect\">Connect</button>
      <span id=\"status\"></span>
        </header>
    <div id=\"term\"></div>
                        <script src=\"%BASE%/static/xterm/lib/xterm.js\"></script>
            <script>
                const term = new Terminal({ convertEol: true, theme: { background: '#111111' } });
                term.open(document.getElementById('term'));
                const qs = new URLSearchParams(window.location.search);
                const portInput = document.getElementById('port');
                const statusEl = document.getElementById('status');
                const defaultPort = qs.get('port') || 'loopback1';
                portInput.value = defaultPort;
                let ws;

                // Send keystrokes to server; ensure single handler
                term.onData((data) => { try { ws && ws.send(data); } catch (e) {} });

                function connect() {
                    const port = portInput.value.trim();
                    if (!port) { alert('Enter a port name'); return; }
                    if (ws && ws.readyState === WebSocket.OPEN) ws.close();
                    const proto = (location.protocol === 'https:') ? 'wss' : 'ws';
                        const basePathDefault = '%BASE%';
                        const m = document.querySelector('meta[name=\"omx-base-path\"]');
                        const basePath = ((m && m.getAttribute('content')) || basePathDefault || '');
                    const server = qs.get('server');
                    const wsPath = server ? `/ws/${encodeURIComponent(server)}/${encodeURIComponent(port)}` : `/ws/${encodeURIComponent(port)}`;
                    const url = `${proto}://${location.host}${basePath}${wsPath}`;
                    ws = new WebSocket(url);
                    ws.binaryType = 'arraybuffer';
                    ws.onopen = () => { statusEl.textContent = `Connected to ${(server||'local')}::${port}`; term.focus(); };
                    ws.onclose = () => { statusEl.textContent = 'Disconnected'; };
                    ws.onerror = (e) => { statusEl.textContent = 'Error (see console)'; console.error(e); };
                    ws.onmessage = (ev) => {
                        if (ev.data instanceof ArrayBuffer) {
                            const dec = new TextDecoder('utf-8');
                            term.write(dec.decode(new Uint8Array(ev.data)));
                        } else {
                            term.write(String(ev.data));
                        }
                    };
                }
                document.getElementById('connect').onclick = connect;
                // auto-connect on load
                connect();
            </script>
  </body>
  </html>"""

_ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-9;?]*[ -/]*[@-~]")

def _fallback_with_base(raw: bytes, base_path: str) -> bytes:
    try:
        bp = base_path or ""
        return raw.replace(b"%BASE%", bp.encode("utf-8"))
    except Exception:
        return raw


_PORT_SORT_KEYS = {"name", "description", "device", "origin", "status", "clients"}


def _assemble_status_payload(adapter, preloaded_ports: Optional[list[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Collect status/federation/multipath snapshots for UI rendering."""

    _log = getattr(adapter, "logger", None)
    data: Dict[str, Any] = {}
    try:
        data["status"] = adapter._build_status_adapter_snapshot()  # type: ignore[attr-defined]
    except Exception as exc:
        data["status"] = {}
        if _log:
            _log.warning("status snapshot failed: %s", exc)
    if preloaded_ports is None:
        try:
            data["ports"] = adapter._get_ports_snapshot()
        except Exception as exc:
            data["ports"] = []
            if _log:
                _log.warning("ports snapshot failed: %s", exc)
    else:
        data["ports"] = preloaded_ports
    try:
        data["federation"] = adapter._gather_federation_overview()  # type: ignore[attr-defined]
    except Exception as exc:
        data["federation"] = {}
        if _log:
            _log.warning("federation overview failed: %s", exc)
    try:
        data["multipath"] = adapter._gather_multipath_overview()  # type: ignore[attr-defined]
    except Exception as exc:
        data["multipath"] = {}
        if _log:
            _log.warning("multipath overview failed: %s", exc)
    try:
        data["web_clients"] = adapter._gather_web_clients()  # type: ignore[attr-defined]
    except Exception as exc:
        data["web_clients"] = []
        if _log:
            _log.warning("web clients listing failed: %s", exc)
    return data


def _port_device_value(port: Dict[str, Any]) -> str:
    try:
        sc = port.get("serial_config") or {}
        if not isinstance(sc, dict):
            sc = {}
        device = sc.get("device") or port.get("device") or port.get("physical_device")
        return str(device or "").lower()
    except Exception:
        return ""


def _port_clients_value(port: Dict[str, Any]) -> int:
    val = port.get("client_count")
    if val is not None:
        try:
            return int(val)
        except Exception:
            pass
    connected = port.get("connected_clients")
    if isinstance(connected, list):
        return len(connected)
    if connected is not None:
        try:
            return int(connected)
        except Exception:
            pass
    return 0


def _port_status_value(port: Dict[str, Any]) -> int:
    try:
        connected = port.get("connected")
        if connected is None:
            connected = port.get("is_running")
        return 0 if bool(connected) else 1
    except Exception:
        return 1


def _sort_ports_list(ports: List[Dict[str, Any]], sort_key: str, descending: bool) -> List[Dict[str, Any]]:
    if not ports:
        return ports

    def _key(port: Dict[str, Any]):
        name = str(port.get("name", "")).lower()
        if sort_key == "description":
            return ((port.get("description") or "").lower(), name)
        if sort_key == "device":
            return (_port_device_value(port), name)
        if sort_key == "origin":
            origin = port.get("origin_server_id") or ("remote" if port.get("remote") else "local")
            return (str(origin).lower(), name)
        if sort_key == "status":
            return (_port_status_value(port), name)
        if sort_key == "clients":
            return (_port_clients_value(port), name)
        return (name,)

    try:
        return sorted(ports, key=_key, reverse=descending)
    except Exception:
        return ports


def _extract_sort_params(request: web.Request) -> Tuple[str, str, str]:
    query = request.rel_url.query
    sort_key = str(query.get("sort", "name")).lower()
    if sort_key not in _PORT_SORT_KEYS:
        sort_key = "name"
    sort_dir = str(query.get("dir", "asc")).lower()
    if sort_dir not in {"asc", "desc"}:
        sort_dir = "asc"
    preserved = [(k, v) for k, v in query.items() if k not in ("sort", "dir")]
    base_query = urllib.parse.urlencode(preserved, doseq=True)
    return sort_key, sort_dir, base_query

def _tail_file(path: Path, limit: int) -> list[str]:
    """Return the last ``limit`` lines from ``path``.

    Reads the entire file via a bounded deque to keep implementation simple
    while avoiding excessive memory for large files.
    """

    limit = max(1, limit)
    lines: deque[str] = deque(maxlen=limit)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            lines.append(line.rstrip("\n"))
    return list(lines)


def _sanitize_log_line(line: str) -> str:
    """Strip carriage returns, ANSI escapes, and control chars for UI display."""

    if not line:
        return ""
    try:
        cleaned = line.replace("\r", "")
        cleaned = _ANSI_ESCAPE_RE.sub("", cleaned)
        cleaned = "".join(ch for ch in cleaned if ((ch >= " " and ch != "\x7f") or ch == "\t"))
        return cleaned
    except Exception:
        return line.replace("\r", "")


def _render_login_fallback(
    adapter,
    error: bool = False,
    next_url: Optional[str] = None,
    message: Optional[str] = None,
) -> bytes:
    """Module-level fallback login page renderer used when adapter method is unavailable.

    This is base-path aware and will scope static links and form action to the
    configured/effective base path when available.
    """
    try:
        realm = html.escape(str(getattr(adapter, "realm", "OpenMux")))
    except Exception:
        realm = "OpenMux"

    try:
        msg_text = str(message) if message is not None else None
    except Exception:
        msg_text = message
    if msg_text:
        msg = f"<p style='color:#f66'>{html.escape(msg_text)}</p>"
    elif error:
        msg = "<p style='color:#f66'>Invalid username or password</p>"
    else:
        msg = ""

    try:
        nxt = html.escape(str(next_url or "/"))
    except Exception:
        nxt = "/"

    # Logo abbreviation from realm (first two initials)
    try:
        parts = [p for p in realm.split() if p]
        abbr = (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper()
    except Exception:
        abbr = "OM"

    # Determine base path without request context (uses configured base_path)
    try:
        bp = str(getattr(adapter, "_effective_base_path", lambda _req=None: "")())
    except Exception:
        bp = ""

    body = f"""<!doctype html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>{realm} - Login</title>
    <link rel=\"stylesheet\" href=\"{bp}/static/web_console.css\" />
  </head>
  <body class=\"login\">
    <form class=\"card\" method=\"POST\" action=\"{bp}/login\"> 
      <div class=\"brand\"> 
        <div class=\"logo\">{abbr}</div> 
        <div> 
          <div class=\"title\">{realm}</div> 
          <div class=\"subtitle\">Web Console</div> 
        </div> 
      </div> 
      {msg}
      <h1>Sign in to {realm}</h1>
      <input type=\"hidden\" name=\"next\" value=\"{nxt}\" />
      <input type=\"text\" name=\"username\" placeholder=\"Username\" autocomplete=\"username\" required />
      <input type=\"password\" name=\"password\" placeholder=\"Password\" autocomplete=\"current-password\" required />
      <button class=\"btn\" type=\"submit\">Sign in</button>
      <div class=\"hint\">After login you'll be redirected back.</div>
    </form>
  </body>
</html>
"""
    return body.encode("utf-8")


# --- aiohttp middleware & route handlers (module-level) ---
@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Hybrid auth: accept Basic Auth for programmatic clients, else require session login.

    Public paths: /healthz, /livez, /readyz, /login, /static/*
    If not authenticated, redirect to /login?next=<original>.
    """
    adapter = request.app.get(ADAPTER_APP_KEY)
    if adapter is None:
        return web.Response(status=500, text="Adapter not initialized\n")

    path = request.path or "/"
    # Support base-path mounting: honor both absolute and base-prefixed public paths
    try:
        base_path = adapter._effective_base_path(request)
    except Exception:
        base_path = ""
    def _pref(p: str) -> str:
        try:
            if not base_path:
                return p
            return (base_path + p) if p.startswith("/") else (base_path + "/" + p)
        except Exception:
            return p

    # Helper: try to attach username from session cookie if valid; returns True if attached
    def _attach_session_user() -> bool:
        try:
            sid = request.cookies.get(adapter._session_cookie_name)
            if not sid:
                return False
            sess = adapter._sessions.get(sid)
            if not sess:
                return False
            now = time.time()
            if (now - sess.get("last_seen", now)) > adapter.session_ttl_seconds:
                # expired
                try:
                    del adapter._sessions[sid]
                except Exception:
                    pass
                return False
            sess["last_seen"] = now
            # Record/refresh client IP
            try:
                ip = adapter._get_client_ip(request) if hasattr(adapter, "_get_client_ip") else None
                if ip and not sess.get("ip"):
                    sess["ip"] = ip
            except Exception:
                pass
            request["username"] = sess.get("username")
            return True
        except Exception:
            return False

    # Public paths: process without enforcing login, but attach username if session is present
    if (
        path in ("/healthz", "/livez")
        or path == _pref("/healthz")
        or path == _pref("/livez")
        or path == "/logout"
        or path == _pref("/logout")
        or path == "/favicon.ico"
        or path == _pref("/favicon.ico")
        or path.startswith("/static/")
        or path.startswith(_pref("/static/"))
        or path == "/login"
        or path == _pref("/login")
    ):
        _attach_session_user()
        return await handler(request)

    # Semi-public: /proxy should not be redirected here; attach username if present and allow handler to enforce
    if (
        path == "/proxy"
        or path.startswith("/proxy/")
        or path == _pref("/proxy")
        or path.startswith(_pref("/proxy/"))
    ):
        _attach_session_user()
        return await handler(request)

    # 0) SSO trust header (for federated proxy)
    try:
        sso_header_name = getattr(adapter, "sso_trust_header", "X-OMX-SSO")
        sso_value = request.headers.get(sso_header_name)
        if sso_value:
            claims = adapter._verify_sso_header(sso_value, request=request)
            if isinstance(claims, dict):
                request["username"] = str(claims.get("user") or "sso")
                po = claims.get("perm")
                if isinstance(po, str) and po:
                    request["perm_override"] = po
                return await handler(request)
    except Exception:
        pass

    # 1) Try Basic Auth first (preserve backward compatibility for non-browser clients)
    auth = request.headers.get("Authorization")
    if auth and auth.lower().startswith("basic "):
        try:
            enc = auth.split(" ", 1)[1].strip()
            raw = base64.b64decode(enc).decode("utf-8", errors="ignore")
            if ":" in raw:
                username, password = raw.split(":", 1)
                if adapter.auth_manager and adapter.auth_manager.authenticate(username, password):
                    request["username"] = username
                    return await handler(request)
        except Exception:
            pass
        # Basic Auth attempted but failed -> 401 for programmatic clients
        return web.Response(status=401, text="Unauthorized\n", headers={"WWW-Authenticate": f'Basic realm="{adapter.realm}"'})

    # 2) Session cookie
    try:
        if _attach_session_user():
            return await handler(request)
    except Exception:
        pass

    # Not authenticated -> for API/probe endpoints, return 401 Basic challenge; else redirect to login
    if path == "/readyz" or path == _pref("/readyz") or path.startswith("/api/") or path.startswith(_pref("/api/")):
        return web.Response(status=401, text="Unauthorized\n", headers={"WWW-Authenticate": f'Basic realm="{adapter.realm}"'})
    next_url = urllib.parse.quote(str(request.rel_url))
    # Redirect to base-scoped login
    login_url = _pref("/login") + f"?next={next_url}"
    raise web.HTTPFound(location=login_url)

async def _render_status_page(
    request: web.Request,
    adapter,
    *,
    preloaded_ports: Optional[list] = None,
    default_status_path: str = "/",
) -> web.Response:
    """Shared status-page rendering used by handle_index and handle_status."""
    try:
        ports = adapter._get_ports_snapshot() if preloaded_ports is None else preloaded_ports
        username = request.get("username")
        try:
            plugin_nav = adapter._get_allowed_plugin_nav(username, request=request)
        except Exception:
            plugin_nav = []
        try:
            user_perm = adapter._get_effective_permission(username, request)
        except Exception:
            user_perm = None
        current_port = request.query.get("port") or request.query.get("console")
        sort_key, sort_dir, preserved_query = _extract_sort_params(request)
        status_payload = _assemble_status_payload(adapter, preloaded_ports=ports)
        status_payload["sidebar_ports"] = status_payload.get("ports", [])
        status_payload["ports"] = _sort_ports_list(status_payload.get("ports", []), sort_key, sort_dir == "desc")
        status_payload["sort_key"] = sort_key
        status_payload["sort_dir"] = sort_dir
        status_payload["sort_query"] = preserved_query
        status_payload["status_path"] = request.rel_url.path or default_status_path
        if hasattr(adapter, "_render_status"):
            body = adapter._render_status(status_payload, plugin_nav=plugin_nav, current_port=current_port, user_permission=user_perm)  # type: ignore[attr-defined]
        else:
            try:
                bp = adapter._effective_base_path(request)
            except Exception:
                bp = ""
            body = _fallback_with_base(_HTML_FALLBACK, bp)
    except Exception as exc:
        adapter.logger.error(f"Status page render failed, using fallback: {exc}")
        try:
            bp = adapter._effective_base_path(request)
        except Exception:
            bp = ""
        body = _fallback_with_base(_HTML_FALLBACK, bp)
    return web.Response(body=body, content_type="text/html")


async def handle_index(request: web.Request) -> web.Response:
    adapter = request.app[ADAPTER_APP_KEY]
    try:
        q = request.query_string or ""
        lq = q.lower()
        if ("console=" in lq) or ("port=" in lq):
            base_path = adapter._effective_base_path(request)
            bp = base_path or ""
            location = f"{bp}/console" + (f"?{q}" if q else "")
            raise web.HTTPFound(location)
    except web.HTTPException:
        raise
    except Exception:
        pass
    return await _render_status_page(request, adapter, default_status_path="/")


async def handle_console(request: web.Request) -> web.Response:
    adapter = request.app[ADAPTER_APP_KEY]
    try:
        if getattr(adapter, "_asset_error", None):
            try:
                await adapter._ensure_assets()
            except Exception as asset_exc:
                adapter._asset_error = str(asset_exc)
                try:
                    adapter.logger.debug(f"xterm assets still missing: {asset_exc}")
                except Exception:
                    pass
        username = request.get("username")
        try:
            plugin_nav = adapter._get_allowed_plugin_nav(username, request=request)
        except Exception:
            plugin_nav = []
        try:
            user_perm = adapter._get_effective_permission(username, request)
        except Exception:
            user_perm = None
        
        ports = adapter._get_ports_snapshot()
        current_port = request.query.get("port") or request.query.get("console")

        if hasattr(adapter, "_render_console"):
            body = adapter._render_console(plugin_nav=plugin_nav, ports=ports, current_port=current_port, user_permission=user_perm)  # type: ignore[attr-defined]
        else:
            try:
                bp = adapter._effective_base_path(request)
            except Exception:
                bp = ""
            body = _fallback_with_base(_HTML_FALLBACK, bp)
    except Exception as re:
        adapter.logger.error(f"Console template render failed, using fallback: {re}")
        try:
            bp = adapter._effective_base_path(request)
        except Exception:
            bp = ""
        body = _fallback_with_base(_HTML_FALLBACK, bp)
    return web.Response(body=body, content_type="text/html")


async def handle_logs(request: web.Request) -> web.Response:
    adapter = request.app[ADAPTER_APP_KEY]
    try:
        username = request.get("username")
        try:
            plugin_nav = adapter._get_allowed_plugin_nav(username, request=request)
        except Exception:
            plugin_nav = []
        try:
            user_perm = adapter._get_effective_permission(username, request)
        except Exception:
            user_perm = None

        ports = adapter._get_ports_snapshot()
        port_name = request.match_info.get("port_name")
        if not port_name:
            port_name = request.rel_url.query.get("port") or request.rel_url.query.get("console")
        port_name = (port_name or "").strip()
        tail_param = request.rel_url.query.get("tail")
        tail = 200
        try:
            if tail_param:
                tail = int(tail_param)
        except Exception:
            tail = 200
        tail = max(10, min(2000, tail))

        log_lines: list[str] = []
        log_error: Optional[str] = None
        log_path: Optional[Path] = None
        log_size: Optional[int] = None
        log_mtime: Optional[float] = None

        if port_name:
            try:
                pm = getattr(adapter.console_manager, "port_manager", None) if adapter.console_manager else None
                port_obj = safe_get_port(pm, port_name) if pm else None
            except Exception:
                port_obj = None
            try:
                logger = DataLogger.get()
                log_path = logger.get_log_path(port_name, port_obj)
                if log_path.exists():
                    try:
                        log_lines = [_sanitize_log_line(line) for line in _tail_file(log_path, tail)]
                    except Exception:
                        log_lines = []
                        log_error = "Failed to read log file."
                    stat = log_path.stat()
                    log_size = stat.st_size
                    log_mtime = stat.st_mtime
                else:
                    log_error = "No log file found for this port."
            except Exception as exc:
                adapter.logger.error(f"/logs read error for {port_name}: {exc}", exc_info=True)
                log_error = "Unable to resolve log path for this port."
        else:
            log_error = "Select a port to view logs."

        if hasattr(adapter, "_render_logs"):
            body = adapter._render_logs(
                ports=ports,
                plugin_nav=plugin_nav,
                current_port=port_name or None,
                user_permission=user_perm,
                log_lines=log_lines,
                log_error=log_error,
                log_path=str(log_path) if log_path else None,
                log_size=log_size,
                log_mtime=log_mtime,
                tail=tail,
            )  # type: ignore[attr-defined]
        else:
            raise AttributeError("Adapter missing _render_logs")
    except Exception as re:
        adapter.logger.error(f"Logs template render failed: {re}")
        body = b"<html><body><h1>Logs</h1><p>Unable to render logs page.</p></body></html>"
    return web.Response(body=body, content_type="text/html")


async def handle_status(request: web.Request) -> web.Response:
    adapter = request.app[ADAPTER_APP_KEY]
    return await _render_status_page(request, adapter, default_status_path="/status")


async def handle_login(request: web.Request) -> web.Response:
    """Render login page (GET) or process login (POST)."""
    adapter = request.app[ADAPTER_APP_KEY]
    if request.method == "POST":
        try:
            data = await request.post()
            username = str(data.get("username", ""))
            password = str(data.get("password", ""))
            next_val = data.get("next") or request.rel_url.query.get("next") or "/"
            next_url = str(next_val)
        except Exception:
            username = ""
            password = ""
            next_url = "/"
        safe_next = str(next_url or "/")
        try:
            client_ip = adapter._get_client_ip(request) if hasattr(adapter, "_get_client_ip") else None
        except Exception:
            client_ip = None
        renderer = getattr(adapter, "_render_login", None)

        def _render_login_response(error: bool, message: Optional[str] = None) -> web.Response:
            if callable(renderer):
                body = renderer(error=error, next_url=safe_next, message=message)
            else:
                body = _render_login_fallback(adapter, error, safe_next, message)
            return web.Response(body=body, content_type="text/html")

        throttle_message = adapter._check_login_throttle(client_ip)
        if throttle_message:
            return _render_login_response(True, throttle_message)
        ok = False
        if adapter.auth_manager and username:
            try:
                ok = bool(adapter.auth_manager.authenticate(username, password))
            except Exception:
                ok = False
        if ok:
            # Verify the user has at least some permissions assigned.
            # Authentication proves identity; a None permission means no access
            # has been granted (e.g. external-auth user not in any mapped group).
            try:
                perms = adapter.auth_manager.get_user_permissions(username) if adapter.auth_manager else None
            except Exception:
                perms = None
            if perms is None:
                adapter.logger.warning(
                    "Login denied for '%s': authenticated but no permissions assigned "
                    "(check group membership or external_auth.default_permission)",
                    username,
                )
                ok = False
        if ok:
            # Create session
            sid = secrets.token_urlsafe(32)
            now = time.time()
            adapter._clear_login_failures(client_ip)
            adapter._sessions[sid] = {"username": username, "created": now, "last_seen": now, "ip": client_ip}
            resp = web.HTTPFound(location=str(next_url))
            cookie_kwargs = {
                "httponly": True,
                "secure": bool(getattr(adapter, "use_tls", False)),
                "samesite": "Lax",
                "max_age": adapter.session_ttl_seconds,
                # Use root path for broad compatibility across base-path/proxy setups
                # (Some proxies/base-path combinations may fail to send path-scoped cookies consistently)
                "path": "/",
            }
            # Proactively clear any stale cookie variants to avoid the browser sending
            # multiple cookies with the same name (root vs base-path scoped)
            try:
                bp = adapter._effective_base_path(request) or "/"
            except Exception:
                bp = "/"
            for name in {adapter._session_cookie_name, "omx_session"}:
                for p in {"/", bp}:
                    try:
                        resp.del_cookie(name, path=p)
                    except Exception:
                        pass
            resp.set_cookie(adapter._session_cookie_name, sid, **cookie_kwargs)
            raise resp
        # Failure -> show login page with message
        adapter._record_login_failure(client_ip)
        return _render_login_response(True)

    # GET: if already authenticated, bounce to next
    try:
        sid = request.cookies.get(adapter._session_cookie_name)
        if sid and adapter._sessions.get(sid):
            nxt = request.rel_url.query.get("next") or "/"
            raise web.HTTPFound(location=str(nxt))
    except Exception:
        pass
    next_q = request.rel_url.query.get("next") or "/"
    renderer = getattr(adapter, "_render_login", None)
    body = renderer(error=False, next_url=next_q) if callable(renderer) else _render_login_fallback(adapter, False, next_q)
    return web.Response(body=body, content_type="text/html")


async def handle_logout(request: web.Request) -> web.Response:
    adapter = request.app[ADAPTER_APP_KEY]
    # Remove session and expire cookie
    try:
        sid = request.cookies.get(adapter._session_cookie_name)
        if sid and adapter._sessions.get(sid):
            del adapter._sessions[sid]
    except Exception:
        pass
    nxt = request.rel_url.query.get("next") or "/login"
    resp = web.HTTPFound(location=str(nxt))
    # Clear both current and legacy cookie variants across root and base-path scopes
    try:
        bp = adapter._effective_base_path(request) or "/"
    except Exception:
        bp = "/"
    for name in {adapter._session_cookie_name, "omx_session"}:
        for p in {"/", bp}:
            try:
                resp.del_cookie(name, path=p)
            except Exception:
                pass
    return resp


async def handle_api_ports(request: web.Request) -> web.Response:
    adapter = request.app[ADAPTER_APP_KEY]
    try:
        ports = adapter._get_ports_snapshot()
        payload = json.dumps({"ports": ports}).encode("utf-8")
        return web.Response(body=payload, content_type="application/json")
    except Exception as e:
        adapter.logger.error(f"/api/ports error: {e}", exc_info=True)
        return web.json_response({"ports": []})


async def handle_api_reload(request: web.Request) -> web.Response:
    """Reload selected configuration sections online (multi-adapter).

    Supported JSON keys (each must be a list of port definitions):
      - serial_ports
      - loopback_ports
      - command_ports
      - tcp_initiator_ports
    """
    adapter = request.app[ADAPTER_APP_KEY]
    # Require auth and permission
    username = request.get("username")
    if not username:
        return web.Response(status=401, text="Unauthorized\n")
    try:
        perms = adapter.auth_manager.get_user_permissions(username) if adapter.auth_manager else None
        if perms not in ("admin", "read-write"):
            return web.Response(status=403, text="Forbidden\n")
    except Exception:
        return web.Response(status=403, text="Forbidden\n")

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": True, "message": "Invalid JSON"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": True, "message": "Invalid body"}, status=400)

    wants: Dict[str, list] = {}
    for key in ("serial_ports", "loopback_ports", "command_ports", "tcp_initiator_ports"):
        if key in payload:
            if not isinstance(payload[key], list):
                return web.json_response({"error": True, "message": f"{key} must be a list"}, status=400)
            wants[key] = payload[key]
    if not wants:
        return web.json_response({"error": True, "message": "No supported sections to reload"}, status=400)

    # Access adapters
    pm = getattr(adapter.console_manager, "port_manager", None) if adapter.console_manager else None
    unified_adapters = getattr(pm, "unified_adapters", []) if pm else []

    def find_adapter(kind: str):
        for a in unified_adapters:
            try:
                at = a.get_adapter_type() if hasattr(a, "get_adapter_type") else getattr(a, "adapter_type", None)
            except Exception:
                at = getattr(a, "adapter_type", None)
            if isinstance(at, str) and at.lower() == kind:
                return a
        return None

    results: Dict[str, Any] = {}
    mapping = {
        "serial_ports": "serial",
        "loopback_ports": "loopback",
        "command_ports": "command",
        "tcp_initiator_ports": "tcp_initiator",
    }
    for key, lst in wants.items():
        kind = mapping[key]
        aobj = find_adapter(kind)
        if not aobj:
            results[kind] = {"error": f"{kind} adapter not active"}
            continue
        # Validate
        try:
            vfn = getattr(aobj.__class__, "validate_config", None)
            if callable(vfn) and not vfn({key: lst}):
                results[kind] = {"error": f"Invalid {key} configuration"}
                continue
        except Exception as e:
            adapter.logger.warning(f"{kind} validation error ignored: {e}")
        # Names
        names = [p.get("name") for p in lst if isinstance(p, dict)]
        if any(not isinstance(n, str) or not n.strip() for n in names) or len(set(names)) != len(names):
            results[kind] = {"error": f"Invalid or duplicate names in {key}"}
            continue
        # Apply
        try:
            if hasattr(aobj, "reconcile_ports"):
                results[kind] = await aobj.reconcile_ports({key: lst})
            else:
                results[kind] = {"error": "Adapter does not support live reconcile"}
        except Exception as e:
            adapter.logger.error(f"Hot-reload {kind} failed: {e}", exc_info=True)
            results[kind] = {"error": "Apply failed"}

    # Disconnect clients on affected ports
    try:
        if pm:
            affected = []
            for v in results.values():
                if isinstance(v, dict):
                    for t in ("removed", "updated"):
                        affected.extend(v.get(t, []) or [])
            for pname in affected:
                try:
                    pobj = safe_get_port(pm, pname)
                    clients = list(getattr(pobj, "connected_clients", []) or []) if pobj is not None else []
                    for c in clients:
                        cid = c.get("client_id") if isinstance(c, dict) else None
                        if cid and hasattr(adapter.console_manager, "disconnect_client_from_port"):
                            await adapter.console_manager.disconnect_client_from_port(cid, pname)
                except Exception:
                    pass
    except Exception as e:
        adapter.logger.warning(f"Post-reconcile disconnect failed: {e}")

    # Update in-memory config snapshot
    try:
        cm = getattr(pm, "config_manager", None) if pm else None
        if cm and getattr(cm, "config", None) is not None:
            for k, lst in wants.items():
                cm.config[k] = lst
    except Exception:
        pass

    # Build backward-compatible aggregated summary at top-level
    aggregate = {"added": [], "updated": [], "removed": [], "unchanged": []}
    for val in results.values():
        if isinstance(val, dict):
            for k in ("added", "updated", "removed", "unchanged"):
                items = val.get(k)
                if items:
                    aggregate[k].extend(items)

    return web.json_response({"ok": True, "summary": {**aggregate, **{k: v for k, v in results.items()}}})


async def handle_healthz(request: web.Request) -> web.Response:
    adapter = request.app[ADAPTER_APP_KEY]
    if not adapter.enable_probes:
        raise web.HTTPNotFound()
    if adapter.probes_include_details:
        details = adapter._probe_details()
        return web.Response(body=json.dumps(details).encode("utf-8"), content_type="application/json")
    return web.Response(text="ok\n", content_type="text/plain")


async def handle_livez(request: web.Request) -> web.Response:
    adapter = request.app[ADAPTER_APP_KEY]
    if not adapter.enable_probes:
        raise web.HTTPNotFound()
    if adapter.probes_include_details:
        details = adapter._probe_details(live_only=True)
        return web.Response(body=json.dumps(details).encode("utf-8"), content_type="application/json")
    return web.Response(text="live\n", content_type="text/plain")


async def handle_readyz(request: web.Request) -> web.Response:
    adapter = request.app[ADAPTER_APP_KEY]
    if not adapter.enable_probes:
        raise web.HTTPNotFound()
    ready = True
    reasons = []
    if adapter.console_manager is None:
        ready = False
        reasons.append("no_console_manager")
    else:
        if not getattr(adapter.console_manager, "port_manager", None):
            ready = False
            reasons.append("no_port_manager")
    if adapter.probes_include_details:
        details = adapter._probe_details()
        details["ready"] = ready and not reasons
        if reasons:
            details["reasons"] = reasons
        status = 200 if details["ready"] else 503
        return web.Response(status=status, body=json.dumps(details).encode("utf-8"), content_type="application/json")
    if ready:
        return web.Response(text="ready\n", content_type="text/plain")
    return web.Response(status=503, text="not_ready:" + ",".join(reasons) + "\n", content_type="text/plain")


def _rw_holders_for_port(adapter: Any, port_name: str) -> list:
    """Return list of 'username@ip' strings for all read-write clients on a port."""
    holders: list = []
    try:
        pm = getattr(getattr(adapter, "console_manager", None), "port_manager", None)
        port_obj = pm.ports.get(port_name) if (pm is not None and hasattr(pm, "ports")) else None
        if port_obj is not None:
            for c in getattr(port_obj, "connected_clients", []):
                if c.get("mode") == "read-write":
                    cid = c.get("client_id", "")
                    username = c.get("username", "unknown")
                    ip = "unknown"
                    try:
                        ip = getattr(adapter, "_client_meta", {}).get(cid, {}).get("ip", "unknown")
                    except Exception:
                        pass
                    holders.append(f"{username}@{ip}")
    except Exception:
        pass
    return holders


async def handle_ws(request: web.Request) -> web.StreamResponse:
    adapter = request.app[ADAPTER_APP_KEY]
    username = request.get("username") or "web"
    port_name = request.get("_fqpn_port") or request.match_info.get("port_name")
    if not port_name:
        raise web.HTTPBadRequest(text="Missing port name")
    port_name = html.unescape(port_name)

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    client_id = f"ws:{id(ws)}"
    adapter._clients[client_id] = ws
    adapter._ws_to_client[ws] = client_id
    # Capture client IP for this websocket
    try:
        ip = adapter._get_client_ip(request) if hasattr(adapter, "_get_client_ip") else None
        if ip:
            if not hasattr(adapter, "_client_meta"):
                adapter._client_meta = {}
            adapter._client_meta[client_id] = {"ip": ip, "username": username, "port": port_name, "type": "websocket"}
    except Exception:
        pass

    # Optional: push metadata over WS when requested by client (via query flag 'meta=1')
    want_meta = False
    try:
        qv = request.rel_url.query.get("meta")
        if isinstance(qv, str) and qv.lower() in ("1", "true", "yes", "on"):
            want_meta = True
    except Exception:
        want_meta = False

    meta_task = None

    attached = False
    meta_only = False

    def _cleanup_ws() -> None:
        """Remove this websocket from adapter tracking dicts on early exit."""
        adapter._clients.pop(client_id, None)
        adapter._ws_to_client.pop(ws, None)
        if hasattr(adapter, "_client_meta"):
            adapter._client_meta.pop(client_id, None)

    try:
        if not (adapter.console_manager and hasattr(adapter.console_manager, "connect_client_to_port")):
            await ws.close(code=1011, message=b"Console manager not available")
            _cleanup_ws()
            return ws
        ok, mode = await adapter.console_manager.connect_client_to_port(client_id, port_name, username)
        if ok:
            attached = True
        else:
            # Fallback: allow meta-only websocket when federated port is currently down
            try:
                pm = getattr(adapter.console_manager, "port_manager", None)
                port_obj = safe_get_port(pm, port_name)
                is_fed = bool(getattr(port_obj, "remote_port_name", None))
                is_up = bool(getattr(port_obj, "is_connected", True)) if port_obj is not None else True
                if is_fed and not is_up:
                    meta_only = True
                else:
                    await ws.close(code=4003, message=b"Port attach failed")
                    _cleanup_ws()
                    return ws
            except Exception:
                await ws.close(code=4003, message=b"Port attach failed")
                _cleanup_ws()
                return ws
        try:
            if hasattr(adapter.console_manager, "register_client_channel"):
                adapter.console_manager.register_client_channel(client_id, adapter)
        except Exception:
            pass
        adapter.logger.info(
            f"Web client {client_id} connected to port {port_name} "
            + (f"({mode})" if attached else "(meta-only)")
        )

        if attached:
            try:
                granted_mode = "read-write" if mode == "read-write" else "read-only"
                payload = {
                    "type": "client_mode",
                    "ok": True,
                    "mode": granted_mode,
                }
                if granted_mode == "read-only":
                    holders = _rw_holders_for_port(adapter, port_name)
                    if holders:
                        payload["rw_holders"] = holders
                await ws.send_str("OMXCTRL " + json.dumps(payload, separators=(",", ":")))
            except Exception:
                pass

        # Register this client for event-driven meta pushes on this port
        if want_meta:
            try:
                subs = adapter._meta_subscribers.setdefault(port_name, set())
                subs.add(client_id)
                # Send one immediate snapshot on subscribe
                try:
                    await adapter._broadcast_meta(port_name)
                except Exception:
                    pass
            except Exception:
                pass
    except Exception as e:
        adapter.logger.error(f"Error connecting web client to port {port_name}: {e}", exc_info=True)
        try:
            await ws.close(code=1011, message=b"Attach error")
        except Exception:
            pass
        _cleanup_ws()
        return ws

    try:
        async for msg in ws:
            try:
                if msg.type == web.WSMsgType.TEXT:
                    # Handle client control frames starting with 'OMXCTRL '
                    try:
                        if isinstance(msg.data, str) and msg.data.startswith("OMXCTRL "):
                            payload = msg.data[len("OMXCTRL "):]
                            req = json.loads(payload)
                            if isinstance(req, dict) and req.get("type") in ("request_rw", "promote"):
                                ok = False
                                try:
                                    ok = await adapter.console_manager.promote_client_to_read_write(client_id, port_name)
                                except Exception:
                                    ok = False
                                # Reply with a client_mode control frame so UI can update
                                resp = {"type": "client_mode", "ok": bool(ok), "mode": ("read-write" if ok else "read-only")}
                                if not ok:
                                    holders = _rw_holders_for_port(adapter, port_name)
                                    if holders:
                                        resp["rw_holders"] = holders
                                try:
                                    await ws.send_str("OMXCTRL " + json.dumps(resp, separators=(",", ":")))
                                except Exception:
                                    pass
                                continue  # handled control; do not forward
                            if isinstance(req, dict) and req.get("type") == "release_rw":
                                try:
                                    await adapter.console_manager.demote_client_to_read_only(client_id, port_name)
                                except Exception:
                                    pass
                                resp = {"type": "client_mode", "ok": True, "mode": "read-only"}
                                try:
                                    await ws.send_str("OMXCTRL " + json.dumps(resp, separators=(",", ":")))
                                except Exception:
                                    pass
                                continue  # handled control; do not forward
                            if isinstance(req, dict) and req.get("type") == "force_promote":
                                ok = False
                                try:
                                    # Collect other read-write holders before any changes
                                    pm = getattr(adapter.console_manager, "port_manager", None)
                                    port_obj = pm.ports.get(port_name) if (pm is not None and hasattr(pm, "ports")) else None
                                    other_rw_ids = []
                                    if port_obj is not None:
                                        for c in list(getattr(port_obj, "connected_clients", [])):
                                            if c.get("client_id") != client_id and c.get("mode") == "read-write":
                                                other_rw_ids.append(c["client_id"])
                                    # Demote others FIRST to free the slot, then promote self
                                    for other_id in other_rw_ids:
                                        try:
                                            await adapter.console_manager.demote_client_to_read_only(other_id, port_name)
                                        except Exception:
                                            pass
                                    ok = await adapter.console_manager.promote_client_to_read_write(client_id, port_name)
                                    if ok:
                                        for other_id in other_rw_ids:
                                            try:
                                                other_ws = adapter._clients.get(other_id)
                                                if other_ws is not None:
                                                    demotion = {"type": "client_mode", "ok": False, "mode": "read-only", "reason": "demoted"}
                                                    await other_ws.send_str("OMXCTRL " + json.dumps(demotion, separators=(",", ":")))
                                            except Exception:
                                                pass
                                except Exception:
                                    ok = False
                                resp = {"type": "client_mode", "ok": bool(ok), "mode": ("read-write" if ok else "read-only")}
                                try:
                                    await ws.send_str("OMXCTRL " + json.dumps(resp, separators=(",", ":")))
                                except Exception:
                                    pass
                                continue  # handled control; do not forward
                            if isinstance(req, dict) and req.get("type") == "query_rw_holders":
                                try:
                                    holders = _rw_holders_for_port(adapter, port_name)
                                    resp = {"type": "rw_holders", "holders": holders}
                                    await ws.send_str("OMXCTRL " + json.dumps(resp, separators=(",", ":")))
                                except Exception:
                                    pass
                                continue  # handled control; do not forward
                            if isinstance(req, dict) and req.get("type") == "request_scrollback":
                                try:
                                    scrollback = adapter.console_manager.port_manager.get_scrollback(port_name)
                                    if scrollback:
                                        await ws.send_bytes(scrollback)
                                    resp = {"type": "scrollback_done", "bytes": len(scrollback)}
                                    await ws.send_str("OMXCTRL " + json.dumps(resp, separators=(",", ":")))
                                except Exception:
                                    adapter.logger.debug(f"scrollback send error for {port_name}", exc_info=True)
                                continue  # handled control; do not forward
                    except Exception:
                        # Fall through to data path if control parsing fails
                        pass
                    data = msg.data.encode("utf-8", errors="ignore")
                elif msg.type == web.WSMsgType.BINARY:
                    data = bytes(msg.data)
                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSING, web.WSMsgType.CLOSED):
                    break
                else:
                    continue
                # In meta-only mode while the federated port is down, drop writes silently
                if not meta_only:
                    await adapter.console_manager.port_manager.write_to_port(port_name, data, client_id)
            except Exception as e:
                adapter.logger.error(f"Write to port error: {e}", exc_info=True)
                break
    except asyncio.CancelledError:
        # Shutdown path cancels pending receives; exit quietly
        try:
            await ws.close(code=1011, message=b"Server shutting down")
        except Exception:
            pass
    except Exception as e:
        adapter.logger.error(f"Websocket loop error for {port_name}: {e}", exc_info=True)
    finally:
        adapter.logger.info(
            "Web client %s websocket loop ending for port %s (closed=%s close_code=%s exception=%r)",
            client_id,
            port_name,
            getattr(ws, "closed", None),
            getattr(ws, "close_code", None),
            ws.exception() if hasattr(ws, "exception") else None,
        )
        # No background metadata task (event-driven only)
        # Remove subscription for event-driven meta
        try:
            subs = adapter._meta_subscribers.get(port_name)
            if isinstance(subs, set) and client_id in subs:
                subs.discard(client_id)
                if not subs:
                    adapter._meta_subscribers.pop(port_name, None)
        except Exception:
            pass
        try:
            if adapter.console_manager and hasattr(adapter.console_manager, "disconnect_client_from_port"):
                await adapter.console_manager.disconnect_client_from_port(client_id, port_name)
            try:
                if hasattr(adapter.console_manager, "unregister_client_channel"):
                    adapter.console_manager.unregister_client_channel(client_id)
            except Exception:
                pass
        except Exception:
            pass
        adapter._clients.pop(client_id, None)
        adapter._ws_to_client.pop(ws, None)
        try:
            if hasattr(adapter, "_client_meta"):
                adapter._client_meta.pop(client_id, None)
        except Exception:
            pass
        adapter.logger.info(f"Web client {client_id} disconnected from port {port_name}")

    return ws


async def handle_ws_fqpn(request: web.Request) -> web.StreamResponse:
    """WebSocket with disambiguated path: /ws/{server_id}/{port_name}.

    This allows selecting among duplicate port names coming from different origins.
    If no exact match exists, a 404 is returned.
    """
    adapter = request.app[ADAPTER_APP_KEY]
    username = request.get("username") or "web"
    server_id = request.match_info.get("server_id")
    port_name = request.match_info.get("port_name")
    if not server_id or not port_name:
        raise web.HTTPBadRequest(text="Missing server_id or port name")
    server_id = html.unescape(server_id)
    port_name = html.unescape(port_name)

    # Resolve the exact port by (origin_server_id, name)
    try:
        snapshot = adapter._get_ports_snapshot()
        candidates = [p for p in snapshot if p.get("name") == port_name and p.get("origin_server_id") == server_id]
        if not candidates:
            # If local requested
            if server_id in ("local", "LOCAL"):
                candidates = [p for p in snapshot if p.get("name") == port_name and not p.get("origin_server_id")]
        if len(candidates) != 1:
            raise web.HTTPNotFound(text="Port not found for given server_id")
    except web.HTTPException:
        raise
    except Exception:
        raise web.HTTPNotFound(text="Port not found for given server_id")

    # From here on, reuse the plain handler by passing port_name via request context
    # (avoids mutating the read-only UrlMappingMatchInfo)
    request["_fqpn_port"] = port_name
    return await handle_ws(request)


class WebConsoleAdapter(BaseGenericAdapter):
    """WebSocket per-port console with a tiny xterm.js UI.

    Config (section: web_console):
        host: 0.0.0.0 (str)
        port: 8081 (int)
        enable_ui: true (bool)  # serve landing page
        realm: "OpenMux" (str)  # Basic-Auth realm
        static_dir: <path> (str, optional)  # where /static/ is served from; created if missing
        template_dir: <path> (str, optional) # jinja2 templates directory (index.html.j2, console.html.j2, status.html.j2)
        enable_probes: true (bool)  # register /healthz, /livez, /readyz endpoints
        probes_include_details: false (bool)  # when true, probes return JSON with version/uptime/clients
    """

    # Expose adapter type for registry indexing (supports unified adapters list)
    adapter_type = "WebConsole"

    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        cfg = config.get("web_console", config)
        self.host = cfg.get("host", "0.0.0.0")
        self.port = int(cfg.get("port", 8081))  # Default port for the web console
        self.enable_ui = bool(cfg.get("enable_ui", True))
        self.realm = str(cfg.get("realm", "OpenMux"))
        # Base path configuration (UI may be served under a subpath)
        try:
            self.base_path = str(cfg.get("base_path", "/"))
        except Exception:
            self.base_path = "/"
        # Respect X-Forwarded-Prefix for URL generation (does not affect routing) 
        self.respect_forwarded_prefix = bool(cfg.get("respect_forwarded_prefix", True))
        # Optional template/static configuration
        self.template_dir = cfg.get("template_dir")  # directory with Jinja2 templates
        self.static_dir = cfg.get("static_dir")  # directory to serve /static from
        # Probe / health endpoint configuration
        self.enable_probes = bool(cfg.get("enable_probes", True))
        self.probes_include_details = bool(cfg.get("probes_include_details", False))
        # TLS configuration (server-side HTTPS for UI and WebSocket)
        self.use_tls = bool(cfg.get("use_tls", False))
        self.ssl_cert = cfg.get("ssl_cert")
        self.ssl_key = cfg.get("ssl_key")
        self.tls_autogen = bool(cfg.get("tls_autogen", True))
        # Optional separate HTTPS port; when set (or defaulted) and use_tls is True,
        # run HTTPS on ssl_port and HTTP on port as redirect-only.
        try:
            self.ssl_port = int(cfg.get("ssl_port", 8443))
        except Exception:
            self.ssl_port = 8443
        # Default directory for web_console certs separate from muxcon
        self.tls_dir = os.path.expanduser(cfg.get("tls_dir", "~/.openmux/web_console"))
        self.logger = logging.getLogger(f"openmux.adapter.web_console.{self.name}")

        # Will be set by server
        self.console_manager = None
        self.auth_manager = None

        # Runtime (aiohttp)
        # Runners/sites (support dual HTTP/HTTPS when ssl_port is used)
        self._http_runner = None  # legacy single-runner
        self._http_site = None  # legacy single-site
        self._http_runner_http = None
        self._http_site_http = None
        self._http_runner_https = None
        self._http_site_https = None
        self._clients = {}  # client_id -> aiohttp WebSocketResponse
        self._ws_to_client = {}  # websocket -> client_id
        self._client_meta = {}  # client_id -> {ip, username}
        # Event-driven meta push support
        self._meta_subscribers = {}  # port_name -> set(client_id)
        self._meta_debounce = {}     # port_name -> last_broadcast_ts
        self._meta_min_interval = 0.3
        # Template engine will be prepared on start
        self._jinja_env = None
        # Startup timestamps for uptime metrics
        self._started_monotonic = None
        self._started_wall = None
        # Simple in-memory session store for form-based auth
        self._sessions = {}
        # Cookie name unique per base-path to prevent collisions with legacy cookies
        try:
            _bp_norm = self._normalize_base_path(getattr(self, "base_path", "/"))
            if _bp_norm:
                _suffix = _bp_norm.strip("/").replace("/", "_")
                self._session_cookie_name = f"omx_session_{_suffix}"
            else:
                self._session_cookie_name = "omx_session"
        except Exception:
            self._session_cookie_name = "omx_session"
        self.session_ttl_seconds = int(cfg.get("session_ttl_seconds", 8 * 3600))
        self.login_throttle_max_attempts = int(cfg.get("login_throttle_max_attempts", 10))
        self.login_throttle_window_seconds = int(cfg.get("login_throttle_window_seconds", 60))
        self.login_throttle_lock_seconds = int(cfg.get("login_throttle_lock_seconds", 5 * 60))
        self.login_throttle_enabled = (
            self.login_throttle_max_attempts > 0 and self.login_throttle_lock_seconds > 0
        )
        self._login_failures: Dict[str, Dict[str, Any]] = {}
        # Plugins configuration
        self.plugins_cfg = cfg.get("plugins", [])
        # Collected plugin navigation items (if templates wish to render them)
        self._plugin_nav = []
        # Cached error message describing missing static assets
        self._asset_error: Optional[str] = None

        # SSO trust header (for federated proxy) – optional, disabled unless secret is set
        try:
            self.sso_trust_header = str(cfg.get("sso_trust_header", "X-OMX-SSO"))
        except Exception:
            self.sso_trust_header = "X-OMX-SSO"
        try:
            val = cfg.get("sso_secret")
            self.sso_secret = str(val) if val else None
        except Exception:
            self.sso_secret = None
        try:
            self.sso_max_skew_sec = int(cfg.get("sso_max_skew_sec", 120))
        except Exception:
            self.sso_max_skew_sec = 120

    # --- Base-path helpers ---
    @staticmethod
    def _normalize_base_path(value: Optional[str]) -> str:
        try:
            if not value or value == "/":
                return ""
            v = str(value).strip()
            if not v:
                return ""
            if not v.startswith("/"):
                v = "/" + v
            if v != "/" and v.endswith("/"):
                v = v[:-1]
            return v
        except Exception:
            return ""

    def _effective_base_path(self, request: Optional[web.Request] = None) -> str:
        """Determine the effective public base path for this request.

        Priority: explicit config base_path > X-Forwarded-Prefix when enabled > root.
        Returns empty string for root to simplify template joining.
        """
        cfg_bp = self._normalize_base_path(getattr(self, "base_path", "/"))
        if cfg_bp:
            return cfg_bp
        if self.respect_forwarded_prefix and request is not None:
            try:
                xfp = request.headers.get("X-Forwarded-Prefix")
                xbp = self._normalize_base_path(xfp)
                if xbp:
                    return xbp
            except Exception:
                pass
        return ""

    def _get_logo_url(self) -> Optional[str]:
        """Return a /static/ URL to a logo image if found in static_dir.

        Looks for: logo.svg, logo.png, logo.webp, logo.jpg, logo.jpeg
        """
        try:
            base = Path(self.static_dir) if self.static_dir else None
            if not base:
                return None
            for name in ("logo.svg", "logo.png", "logo.webp", "logo.jpg", "logo.jpeg"):
                p = base / name
                if p.is_file():
                    return f"/static/{name}"
        except Exception:
            return None
        return None

    def get_capabilities(self) -> Set[AdapterCapability]:
        return {AdapterCapability.ACCEPTS_CONNECTIONS}

    def get_adapter_type(self) -> str:
        return "WebConsole"

    # --- Plugin navigation filtering ---
    def _get_allowed_plugin_nav(self, username: Optional[str], request: Optional[web.Request] = None) -> list[Dict[str, Any]]:
        items: list[Dict[str, Any]] = []
        try:
            nav = list(self._plugin_nav or [])
        except Exception:
            nav = []
        if not nav:
            return items
        perm = self._get_effective_permission(username, request)
        for n in nav:
            try:
                req = n.get("require") if isinstance(n, dict) else None
                if req and perm != req:
                    continue
                items.append({"title": n.get("title"), "path": n.get("path"), "require": req})
            except Exception:
                continue
        return items

    # --- SSO helpers ---
    def _verify_sso_header(self, header_value: str, request: Optional[web.Request] = None) -> Optional[Dict[str, Any]]:
        try:
            if not header_value:
                return None
            parts = str(header_value).split(";")
            if len(parts) < 3:
                return None
            ver = parts[0]

            def _decode_payload(b64s: str) -> Optional[Dict[str, Any]]:
                try:
                    pad = "=" * (-len(b64s) % 4)
                    raw = base64.urlsafe_b64decode(b64s + pad)
                    obj = json.loads(raw.decode("utf-8", errors="ignore"))
                    return obj if isinstance(obj, dict) else None
                except Exception:
                    return None

            skew = int(getattr(self, "sso_max_skew_sec", 120))
            now = int(time.time())

            # Legacy HMAC format: v1;payload_b64;hexmac
            if ver == "v1" and len(parts) == 3:
                payload_b64 = parts[1]
                sig_hex = parts[2]
                claims = _decode_payload(payload_b64)
                if not isinstance(claims, dict):
                    return None
                iat = int(claims.get("iat", now))
                exp = int(claims.get("exp", now))
                if iat - skew > now or now > exp + skew:
                    return None
                secret = getattr(self, "sso_secret", None)
                if not secret:
                    return None
                mac = hmac.new(str(secret).encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(mac, sig_hex):
                    return None
                return claims

            # Zero-config Ed25519 format: v1e;kid;payload_b64;sig_b64
            if ver == "v1e" and len(parts) == 4:
                kid = parts[1]
                payload_b64 = parts[2]
                sig_b64 = parts[3]
                claims = _decode_payload(payload_b64)
                if not isinstance(claims, dict):
                    return None
                iat = int(claims.get("iat", now))
                exp = int(claims.get("exp", now))
                if iat - skew > now or now > exp + skew:
                    return None
                # Optional: ensure the claimed node is either this server or an active federation peer
                node = claims.get("node")
                try:
                    muxcon = self._find_muxcon_adapter()
                except Exception:
                    muxcon = None
                if node:
                    try:
                        local_id = getattr(muxcon, "server_id", None) if muxcon is not None else None
                        allowed = False
                        if str(node) == str(local_id):
                            allowed = True
                        elif muxcon is not None:
                            allowed = any(str(c.get("server_id")) == str(node) for c in (getattr(muxcon, "connections", {}) or {}).values())
                        if not allowed:
                            return None
                    except Exception:
                        return None
                # Lookup public key by kid from the MuxCon adapter's configured keys
                try:
                    pub = None
                    muxcon_local = self._find_muxcon_adapter()
                    if muxcon_local is not None:
                        try:
                            # MuxCon adapter stores parsed Ed25519 keys under _auth_pubkeys
                            pubs = getattr(muxcon_local, "_auth_pubkeys", {}) or {}
                            pub = pubs.get(str(kid))
                        except Exception:
                            pub = None
                    if not pub:
                        # Internal rollout fallback: if this request was forwarded by our proxy
                        # (indicated by X-Forwarded-Prefix starting with /proxy/) AND the claimed
                        # target node matches our own server_id, accept claims temporarily even
                        # without a published public key. This prevents a remote login wall while
                        # keys are being distributed across the fleet.
                        if node and request is not None:
                            try:
                                # Check forwarded prefix signal from the proxying peer
                                xfp = request.headers.get("X-Forwarded-Prefix", "")
                                # Also allow legacy path check in case of direct mounting
                                path = request.path or ""
                                # Compare claimed node with our local server_id when available
                                local_server_id = None
                                try:
                                    muxcon_local = self._find_muxcon_adapter()
                                    local_server_id = getattr(muxcon_local, "server_id", None)
                                except Exception:
                                    local_server_id = None
                                def _has_proxy_prefix(v: str) -> bool:
                                    try:
                                        return "/proxy/" in (v or "")
                                    except Exception:
                                        return False
                                if (str(node) and str(node) == str(local_server_id)) and (_has_proxy_prefix(xfp) or _has_proxy_prefix(path)):
                                    return claims
                            except Exception:
                                pass
                        return None
                    pad = "=" * (-len(payload_b64) % 4)
                    payload_bytes = base64.urlsafe_b64decode(payload_b64 + pad)
                    pad2 = "=" * (-len(sig_b64) % 4)
                    sig = base64.urlsafe_b64decode(sig_b64 + pad2)
                    # Verify signature (raises on failure)
                    pub.verify(sig, payload_bytes)
                    return claims
                except Exception:
                    return None

            return None
        except Exception:
            return None

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> bool:
        cfg = config.get("web_console", config)
        try:
            port = int(cfg.get("port", 8081))
        except Exception:
            return False
        try:
            if "ssl_port" in cfg:
                sp = int(cfg.get("ssl_port", 8443))
                if not (1 <= sp <= 65535):
                    return False
        except Exception:
            return False
        # Basic TLS validation: if use_tls and autogen disabled, require cert+key
        try:
            if bool(cfg.get("use_tls", False)) and not bool(cfg.get("tls_autogen", True)):
                if not cfg.get("ssl_cert") or not cfg.get("ssl_key"):
                    return False
        except Exception:
            pass
        return 1 <= port <= 65535

    def set_console_manager(self, console_manager):
        self.console_manager = console_manager
        if hasattr(console_manager, "register_client_manager"):
            console_manager.register_client_manager(self)
        # Also subscribe to PortManager meta events for event-driven WS pushes
        try:
            pm = getattr(console_manager, "port_manager", None)
            if pm and hasattr(pm, "register_meta_listener"):
                pm.register_meta_listener(self._on_port_meta_update)  # type: ignore[arg-type]
        except Exception:
            pass

    def set_auth_manager(self, auth_manager):
        self.auth_manager = auth_manager

    async def start(self) -> bool:
        try:
            # Prepare templates and assets
            try:
                self._prepare_templates()
                await self._ensure_assets()
            except Exception as prep_err:
                self.logger.warning(f"Template/static preparation warning: {prep_err}")

            app = web.Application(middlewares=[auth_middleware])
            app[ADAPTER_APP_KEY] = self

            # --- Routes ---
            # Static files at /static/
            try:
                # Ensure static dir exists
                Path(self.static_dir).mkdir(parents=True, exist_ok=True)
                app.router.add_static("/static/", self.static_dir, follow_symlinks=True)
            except Exception as e:
                self.logger.warning(f"Failed to add static route: {e}")

            app.router.add_get("/", handle_index)
            if self.enable_ui:
                app.router.add_get("/index.html", handle_index)
                app.router.add_get("/console", handle_console)
                app.router.add_get("/logs", handle_logs)
                app.router.add_get("/logs/{port_name}", handle_logs)
                app.router.add_get("/status", handle_status)
            # Login/logout
            app.router.add_get("/login", handle_login)
            app.router.add_post("/login", handle_login)
            app.router.add_get("/logout", handle_logout)
            app.router.add_get("/api/ports", handle_api_ports)
            app.router.add_post("/api/reload", handle_api_reload)
            app.router.add_get("/api/csrf", self._handle_api_csrf)
            app.router.add_get("/ws/{port_name}", handle_ws)
            app.router.add_get("/ws/{server_id}/{port_name}", handle_ws_fqpn)
            # Health/Probe endpoints
            if self.enable_probes:
                app.router.add_get("/healthz", handle_healthz)
                app.router.add_get("/livez", handle_livez)
                app.router.add_get("/readyz", handle_readyz)

            # Load optional web plugins
            try:
                self._load_plugins(app)
            except Exception as e:
                self.logger.error(f"Failed to load web plugins: {e}", exc_info=True)

            # Optionally mount under a configured base path using a parent app
            parent_app = None
            mount_prefix = self._normalize_base_path(self.base_path)
            if mount_prefix:
                parent_app = web.Application()
                parent_app.add_subapp(mount_prefix, app)
                serve_app = parent_app
            else:
                serve_app = app

            # Decide serving mode: single-port (legacy) or dual-port (HTTP redirect + HTTPS)
            if self.use_tls and self.ssl_port and self.ssl_port != self.port:
                # Dual-port: HTTPS main app on ssl_port; HTTP redirect-only app on port
                try:
                    ssl_ctx = await self._create_server_ssl_context()
                except Exception as e:
                    self.logger.error(f"Failed to initialize TLS context: {e}", exc_info=True)
                    return False

                # HTTPS main
                runner_tls = web.AppRunner(serve_app)
                await runner_tls.setup()
                site_tls = web.TCPSite(runner_tls, self.host, self.ssl_port, ssl_context=ssl_ctx)
                await site_tls.start()
                self._http_runner_https = runner_tls
                self._http_site_https = site_tls

                # HTTP redirect-only app
                redirect_app = self._build_redirect_app()
                runner_http = web.AppRunner(redirect_app)
                await runner_http.setup()
                site_http = web.TCPSite(runner_http, self.host, self.port, ssl_context=None)
                await site_http.start()
                self._http_runner_http = runner_http
                self._http_site_http = site_http

                self._started_monotonic = time.monotonic()
                self._started_wall = time.time()
                self.is_running = True
                self.logger.info(f"WebConsole HTTPS on https://{self.host}:{self.ssl_port} (primary); HTTP redirect on http://{self.host}:{self.port}")
                return True
            else:
                # single-site: honor use_tls on the configured port only
                runner = web.AppRunner(serve_app)
                await runner.setup()
                ssl_ctx = None
                if self.use_tls:
                    try:
                        ssl_ctx = await self._create_server_ssl_context()
                    except Exception as e:
                        self.logger.error(f"Failed to initialize TLS context: {e}", exc_info=True)
                        await runner.cleanup()
                        return False
                site = web.TCPSite(runner, self.host, self.port, ssl_context=ssl_ctx)
                await site.start()
                self._http_runner = runner
                self._http_site = site

                self._started_monotonic = time.monotonic()
                self._started_wall = time.time()
                self.is_running = True
                scheme = "https" if ssl_ctx else "http"
                self.logger.info(f"WebConsole listening on {scheme}://{self.host}:{self.port}")
                return True
        except Exception as e:
            self.logger.error(f"Failed to start WebConsole: {e}", exc_info=True)
            return False

    async def stop(self) -> None:
        self.is_running = False
        try:
            # Close HTTP server
            # New dual-server cleanup first
            if self._http_runner_http is not None:
                try:
                    await self._http_runner_http.cleanup()
                except Exception:
                    pass
                self._http_runner_http = None
                self._http_site_http = None
            if self._http_runner_https is not None:
                try:
                    await self._http_runner_https.cleanup()
                except Exception:
                    pass
                self._http_runner_https = None
                self._http_site_https = None
            # Legacy single-runner cleanup
            runner = self._http_runner
            self._http_runner = None
            self._http_site = None
            if runner is not None:
                await runner.cleanup()
        except Exception:
            pass
        # Close any open websockets
        for ws in list(self._ws_to_client.keys()):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()
        self._ws_to_client.clear()
        self.logger.info("WebConsole server stopped")

    def _build_redirect_app(self) -> web.Application:
        """Build a minimal aiohttp application that redirects all requests to HTTPS.

        When TLS is enabled and a distinct ssl_port is configured, we expose this
        HTTP server only to perform a strict redirect to the HTTPS endpoint and do
        not serve any content directly.
        """
        redirect_app = web.Application()

        async def _redirect(request: web.Request) -> web.StreamResponse:
            try:
                # Build target URL: https scheme, configured ssl_port, preserve path and query
                url = request.url.with_scheme("https")
                # yarl.URL.with_port returns new URL with provided port
                try:
                    target = url.with_port(self.ssl_port)
                except Exception:
                    target = url
                raise web.HTTPPermanentRedirect(location=str(target))
            except web.HTTPException:
                raise
            except Exception:
                # Fallback: best-effort Location header
                loc = f"https://{self.host}:{self.ssl_port}{request.rel_url}"
                raise web.HTTPPermanentRedirect(location=loc)

        # Catch-all for any method and path
        redirect_app.router.add_route("*", "/{tail:.*}", _redirect)
        return redirect_app

    # --- Template and HTML rendering helpers ---
    def _prepare_templates(self) -> None:
        """Initialize template environment and ensure default directories.

        This method is tolerant to missing jinja2 or templates; in that case
        we simply keep ``self._jinja_env`` as None and rely on inline HTML
        generators for the minimal UI.
        """
        # Default directories if not provided
        try:
            if not self.static_dir:
                # Prefer WorkingDirectory (systemd sets it) -> ./static
                self.static_dir = str((Path.cwd() / "static").resolve())
            if not self.template_dir:
                # Default templates for web_console live under ./templates/web_console
                self.template_dir = str((Path.cwd() / "templates" / "web_console").resolve())
        except Exception:
            # Fallback to module-relative dirs if cwd fails
            base = Path(__file__).resolve().parents[3] if len(Path(__file__).resolve().parents) >= 3 else Path(__file__).resolve().parent
            self.static_dir = self.static_dir or str((base / "static").resolve())
            self.template_dir = self.template_dir or str((base / "templates" / "web_console").resolve())

        # Attempt to set up Jinja2 if available and directory exists
        try:
            from jinja2 import Environment, FileSystemLoader, select_autoescape  # type: ignore

            tdir = Path(self.template_dir)
            if tdir.is_dir():
                self._jinja_env = Environment(
                    loader=FileSystemLoader(str(tdir)),
                    autoescape=select_autoescape(["html", "xml"]),
                    enable_async=False,
                )
                # Register handy filters
                def _fmt_ts(value):
                    try:
                        if value is None or value == "":
                            return ""
                        v = float(value)
                        # Accept values in ms as well
                        if v > 1_000_000_000_000:
                            v = v / 1000.0
                        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v))
                    except Exception:
                        return str(value)
                try:
                    self._jinja_env.filters["fmt_ts"] = _fmt_ts
                except Exception:
                    pass
                try:
                    self.logger.info(f"WebConsole templates enabled: {tdir}")
                except Exception:
                    pass
            else:
                self._jinja_env = None
                try:
                    self.logger.info("WebConsole templates disabled (template_dir missing); using inline UI")
                except Exception:
                    pass
        except Exception:
            # jinja2 not installed or couldn't initialize
            self._jinja_env = None
            try:
                self.logger.info("WebConsole templates unavailable (jinja2 not installed); using inline UI")
            except Exception:
                pass

    def _render_console(
        self,
        plugin_nav: Optional[list[Dict[str, Any]]] = None,
        ports: list = None,
        current_port: str = None,
        user_permission: Optional[str] = None,
    ) -> bytes:
        """Render the xterm-based console page.

        Tries Jinja2 'console.html.j2'; otherwise uses the built-in fallback
        HTML that embeds xterm.js and connects to /ws/{port}.
        """
        asset_error = getattr(self, "_asset_error", None)
        if asset_error:
            return self._render_console_error(
                asset_error,
                plugin_nav=plugin_nav,
                ports=ports,
                current_port=current_port,
                user_permission=user_permission,
            )
        try:
            if self._jinja_env:
                tmpl = self._jinja_env.get_template("console.html.j2")
                base_path = self._effective_base_path(None)
                html_text = tmpl.render(
                    realm=self.realm,
                    logo_url=self._get_logo_url(),
                    title="OpenMux Console",
                    base_path=base_path,
                    plugin_nav=plugin_nav,
                    ports=ports or [],
                    current_port=current_port,
                    user_permission=user_permission,
                )
                return html_text.encode("utf-8")
        except Exception as e:
            self.logger.debug(f"console template render failed: {e}")
        # Reuse the richer inline fallback defined at top of module
        try:
            if not self._jinja_env:
                self.logger.debug("console: using inline fallback (templates disabled)")
        except Exception:
            pass
        try:
            bp = self._effective_base_path(None) or ""
        except Exception:
            bp = ""
        return _fallback_with_base(_HTML_FALLBACK, bp)

    def _render_console_error(
        self,
        message: str,
        plugin_nav: Optional[list[Dict[str, Any]]] = None,
        ports: Optional[list] = None,
        current_port: Optional[str] = None,
        user_permission: Optional[str] = None,
    ) -> bytes:
        """Render a friendly error page when console assets are missing."""
        try:
            if self._jinja_env:
                tmpl = self._jinja_env.get_template("console_error.html.j2")
                base_path = self._effective_base_path(None)
                html_text = tmpl.render(
                    realm=self.realm,
                    logo_url=self._get_logo_url(),
                    title="OpenMux Console",
                    base_path=base_path,
                    plugin_nav=plugin_nav or [],
                    ports=ports or [],
                    current_port=current_port,
                    user_permission=user_permission,
                    error_message=message,
                )
                return html_text.encode("utf-8")
        except Exception as e:
            self.logger.debug(f"console error template render failed: {e}")
        try:
            bp = self._effective_base_path(None) or ""
        except Exception:
            bp = ""
        try:
            brand_logo = self._get_logo_url()
        except Exception:
            brand_logo = None
        if brand_logo:
            brand_html = f'<img class="logo-img" src="{html.escape(str(brand_logo))}" alt="logo">'
        else:
            brand_html = '<div class="logo">OM</div>'
        safe_message = html.escape(message or "Console unavailable.")
        body = f"""
<!doctype html>
<html>
    <head>
        <meta charset=\"utf-8\" />
        <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
        <title>OpenMux Console</title>
        <link rel=\"stylesheet\" href=\"{bp}/static/web_console.css\" />
    </head>
    <body>
        <header class=\"site\"><div class=\"brand\">{brand_html}<div class=\"title\">OpenMux</div></div><div class=\"page\">Console</div><div class=\"actions\"><a class=\"btn\" href=\"{bp}/\">Status</a><a class=\"btn\" href=\"{bp}/logout\">Logout</a></div></header>
        <main>
            <h1>Console Unavailable</h1>
            <div class=\"card warning\">
                <p>{safe_message}</p>
                <p>Install the missing files and reload this page once they are available.</p>
            </div>
        </main>
    </body>
</html>
"""
        return body.encode("utf-8")

    def _render_logs(
        self,
        plugin_nav: Optional[list[Dict[str, Any]]] = None,
        ports: Optional[list[dict[str, Any]]] = None,
        current_port: Optional[str] = None,
        user_permission: Optional[str] = None,
        log_lines: Optional[list[str]] = None,
        log_error: Optional[str] = None,
        log_path: Optional[str] = None,
        log_size: Optional[int] = None,
        log_mtime: Optional[float] = None,
        tail: int = 200,
    ) -> bytes:
        """Render the per-port log viewer page."""

        try:
            if self._jinja_env:
                tmpl = self._jinja_env.get_template("logs.html.j2")
                base_path = self._effective_base_path(None)
                html_text = tmpl.render(
                    realm=self.realm,
                    logo_url=self._get_logo_url(),
                    title="OpenMux Logs",
                    base_path=base_path,
                    plugin_nav=plugin_nav,
                    ports=ports or [],
                    current_port=current_port,
                    user_permission=user_permission,
                    log_lines=log_lines or [],
                    log_error=log_error,
                    log_path=log_path,
                    log_size=log_size,
                    log_mtime=log_mtime,
                    tail=tail,
                )
                return html_text.encode("utf-8")
        except Exception as e:
            self.logger.debug(f"logs template render failed: {e}")

        # Fallback minimal view if templates are unavailable
        try:
            lines = "<br/>".join(html.escape(line) for line in (log_lines or [])) or "<em>No log lines</em>"
        except Exception:
            lines = "<em>No log lines</em>"
        msg = html.escape(log_error) if log_error else ""
        msg_block = f"<p class='muted'>{msg}</p>" if msg else ""
        try:
            bp = self._effective_base_path(None) or ""
        except Exception:
            bp = ""
        body = f"""
<!doctype html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>OpenMux Logs</title>
    <link rel=\"stylesheet\" href=\"{bp}/static/web_console.css\" />
  </head>
  <body>
    <header class=\"site\"><div class=\"brand\">OpenMux</div><div class=\"page\">Logs</div><div class=\"actions\"><a class=\"btn\" href=\"{bp}/\">Status</a><a class=\"btn\" href=\"{bp}/console\">Console</a></div></header>
    <main>
      <h1>Port Logs</h1>
    {msg_block}
      <div class=\"card\" style=\"overflow:auto;\"><pre>{lines}</pre></div>
    </main>
  </body>
</html>
"""
        return body.encode("utf-8")

    def _render_status(
        self,
        data: Dict[str, Any],
        plugin_nav: Optional[list[Dict[str, Any]]] = None,
        current_port: str = None,
        user_permission: Optional[str] = None,
    ) -> bytes:
        """Render a comprehensive status page (server-side aggregated).

        Expects an aggregated mapping with keys similar to the WebStatus API:
        - status (dict)
        - ports (list)
        - federation (dict)
        - multipath (dict)
        - web_clients (list)

        Tries Jinja2 'status.html.j2'; otherwise emits a simple HTML fallback.
        """
        try:
            if self._jinja_env:
                tmpl = self._jinja_env.get_template("status.html.j2")
                # Compute a few top-level metrics for cards
                ports = data.get("ports", []) or []
                sidebar_ports = data.get("sidebar_ports") or ports
                total_ports = len(ports)
                connected_ports = sum(1 for p in ports if p.get("connected") or p.get("is_running"))
                fed = data.get("federation") or {}
                fed_tot = (fed.get("totals") or {}) if isinstance(fed, dict) else {}
                mpath = data.get("multipath") or {}
                m_tot = (mpath.get("totals") or {}) if isinstance(mpath, dict) else {}
                # Build quick index for template lookups
                conn_index = {
                    c.get("connection_id"): c
                    for c in (fed.get("connections") or [])
                    if isinstance(c, dict) and c.get("connection_id") is not None
                }
                # Build rport_map primarily from connections[].ports_registered
                # Helper: add ports into map with de-dup by name
                def _add_ports(target: Dict[str, list], key: str, ports_list: list) -> None:
                    if not key or not isinstance(ports_list, list):
                        return
                    bucket = target.setdefault(str(key), [])
                    seen = {rp.get("name") for rp in bucket if isinstance(rp, dict)}
                    for rp in ports_list:
                        if not isinstance(rp, dict):
                            continue
                        nm = rp.get("name")
                        if not nm or nm in seen:
                            continue
                        bucket.append(rp)
                        seen.add(nm)

                rport_map: Dict[str, list] = {}
                conns = (fed.get("connections") or []) if isinstance(fed, dict) else []
                groups = (mpath.get("groups") or []) if isinstance(mpath, dict) else []
                # Prefer the connection's own mpath_group key for grouping
                for c in conns:
                    if not isinstance(c, dict):
                        continue
                    gk = c.get("mpath_group") or f"_single:{c.get('connection_id')}"
                    plist = c.get("ports_registered") or []
                    if plist:
                        _add_ports(rport_map, str(gk), plist)
                # No fallback: require explicit mapping via connections[].ports_registered
                # Extract convenience vars
                hb_interval = None
                peers_cfg = []
                try:
                    hb_interval = fed.get("heartbeat_interval_sec")
                    peers_cfg = fed.get("peers_configured") or []
                except Exception:
                    pass
                base_path = self._effective_base_path(None)
                sort_key = data.get("sort_key") or "name"
                sort_dir = data.get("sort_dir") or "asc"
                sort_query = data.get("sort_query") or ""
                status_path = data.get("status_path") or "/"
                html_text = tmpl.render(
                    data=data,
                    ports=ports,
                    sidebar_ports=sidebar_ports,
                    ports_by_name={p.get("name"): p for p in ports if isinstance(p, dict) and p.get("name")},
                    total_ports=total_ports,
                    connected_ports=connected_ports,
                    federation=fed,
                    multipath=mpath,
                    realm=self.realm,
                    logo_url=self._get_logo_url(),
                    title="OpenMux Status",
                    fed_totals=fed_tot,
                    mpath_totals=m_tot,
                    conn_index=conn_index,
                    rport_map=rport_map,
                    hb_interval=hb_interval,
                    peers_cfg=peers_cfg,
                    adapter=self,
                    plugin_nav=plugin_nav or [],
                    base_path=base_path,
                    current_port=current_port,
                    user_permission=user_permission,
                    sort_key=sort_key,
                    sort_dir=sort_dir,
                    sort_query=sort_query,
                    status_path=status_path,
                )
                return html_text.encode("utf-8")
        except Exception as e:
            self.logger.debug(f"status template render failed: {e}")
        # Inline minimal status view fallback
        try:
            ports = data.get("ports", []) or []
        except Exception:
            ports = []
        def esc(x: Any) -> str:
            return html.escape(str(x))
        rows = []
        for p in ports:
            try:
                name = esc(p.get("name", ""))
                adapter = esc(p.get("adapter") or p.get("adapter_type") or "")
                desc = esc(p.get("description", ""))
                clients = esc(_port_clients_value(p))
                rows.append(f"<tr><td>{name}</td><td>{adapter}</td><td>{desc}</td><td>{clients}</td></tr>")
            except Exception:
                continue
        rows_html = ''.join(rows) if rows else '<tr><td colspan="4"><em>No ports</em></td></tr>'
        try:
            bp = self._effective_base_path(None) or ""
        except Exception:
            bp = ""
        body = f"""
<!doctype html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>OpenMux Status</title>
    <link rel=\"stylesheet\" href=\"{bp}/static/web_console.css\" />
  </head>
  <body>
    <header class=\"site\"><div class=\"brand\">OpenMux</div><div class=\"page\">Status</div><div class=\"actions\"><a class=\"btn\" href=\"{bp}/\">Status</a><a class=\"btn\" href=\"{bp}/console\">Console</a><a class=\"btn\" href=\"{bp}/logout\">Logout</a></div></header>
    <main>
      <h2>Ports</h2>
      <table>
        <thead><tr><th>Name</th><th>Adapter</th><th>Description</th><th>Clients</th></tr></thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
    </main>
  </body>
</html>
"""
        return body.encode("utf-8")

    def _build_status_adapter_snapshot(self) -> Dict[str, Any]:
        """Produce a status summary similar to WebStatus /api/status."""
        try:
            host = self.host
            port = self.port
        except Exception:
            host = "0.0.0.0"
            port = 0
        return {
            "adapter": self.get_adapter_type(),
            "name": self.name,
            "host": host,
            "port": port,
            "running": self.is_running,
            "features": {
                "http_api_enabled": True,
                "websocket_enabled": True,
                "web_ui_enabled": bool(self.enable_ui),
            },
            "connections": len(self._clients),
            "timestamp": time.time(),
        }

    def _find_muxcon_adapter(self):
        pm = getattr(self.console_manager, "port_manager", None) if self.console_manager else None
        try:
            unified = getattr(pm, "unified_adapters", []) if pm else []
            for ad in unified or []:
                try:
                    atype_fn = getattr(ad, "get_adapter_type", None)
                    atype = atype_fn() if callable(atype_fn) else getattr(ad, "adapter_type", "")
                    if str(atype).lower() == "muxcon":
                        return ad
                except Exception:
                    continue
        except Exception:
            pass
        return None

    # --- Event-driven meta push helpers ---
    def _on_port_meta_update(self, port_name: str, changes: Optional[Dict[str, Any]] = None):
        """PortManager meta listener: schedule a meta broadcast to WS subscribers.

        Uses a lightweight debounce to coalesce rapid updates. For connection
        state flips and key lifecycle events, bypass debounce and broadcast
        immediately to keep the UI responsive (e.g., unplug/replug scenarios).
        """
        try:
            subs = self._meta_subscribers.get(port_name)
            if not subs:
                return
            # Check if we should bypass debounce for connection state changes
            immediate = False
            try:
                if isinstance(changes, dict):
                    if "connected" in changes:
                        immediate = True
                    ev = changes.get("event")
                    if ev in ("serial_connected", "serial_disconnected", "federated_disconnected", "port_registered", "port_unregistered"):
                        immediate = True
            except Exception:
                immediate = False

            now = time.time()
            if immediate:
                # Force broadcast now and update debounce timestamp
                self._meta_debounce[port_name] = now
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._broadcast_meta(port_name, changes))
                except Exception:
                    pass
                return

            last = float(self._meta_debounce.get(port_name, 0) or 0)
            if (now - last) < float(self._meta_min_interval or 0):
                # too soon; schedule after remaining interval
                delay = max(0.0, float(self._meta_min_interval or 0) - (now - last))
                try:
                    loop = asyncio.get_running_loop()
                    loop.call_later(delay, lambda: asyncio.create_task(self._broadcast_meta(port_name)))
                except Exception:
                    pass
                return
            self._meta_debounce[port_name] = now
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._broadcast_meta(port_name))
            except Exception:
                pass
        except Exception:
            pass

    async def _broadcast_meta(self, port_name: str, changes: Optional[Dict[str, Any]] = None) -> None:
        """Build and send a compact OMXCTRL meta frame to all subscribers for a port."""
        try:
            subs = set(self._meta_subscribers.get(port_name) or [])
            if not subs:
                return
            # Build current meta snapshot for this port; prefer enriched unified entry
            info = None
            try:
                snapshot = self._get_ports_snapshot()
                # Merge all entries with same name, letting later entries (unified enumeration) override earlier ones
                combined: Dict[str, Any] = {}
                for p in snapshot:
                    if isinstance(p, dict) and p.get("name") == port_name:
                        try:
                            combined.update(p)
                        except Exception:
                            pass
                info = combined if combined else None
            except Exception:
                info = None
            if info is None:
                info = {"name": port_name, "connected": False}
            # Prefer connected state from the event itself to avoid racing the runtime flip
            event_connected = None
            try:
                if isinstance(changes, dict) and "connected" in changes:
                    event_connected = bool(changes.get("connected"))
            except Exception:
                event_connected = None

            # Prefer live connection state from PortManager's port object when available
            live_connected = None
            live_adapter = None
            live_serial_cfg = None
            try:
                pm = getattr(self.console_manager, "port_manager", None) if self.console_manager else None
                port_obj = safe_get_port(pm, port_name) if pm is not None else None
                if port_obj is not None:
                    # Unified wrapper exposes unified_port; federated proxies expose is_connected directly
                    if hasattr(port_obj, "unified_port") and hasattr(getattr(port_obj, "unified_port", None), "is_connected"):
                        live_connected = bool(getattr(getattr(port_obj, "unified_port", None), "is_connected", None))
                        live_adapter = getattr(port_obj, "adapter_type", None)
                        # Try serial config from unified_port if available
                        try:
                            cfg = getattr(getattr(port_obj, "unified_port", None), "config", None)
                            if cfg and hasattr(cfg, "__dict__"):
                                cd = cfg.__dict__
                                live_serial_cfg = {
                                    "device": cd.get("device"),
                                    "baudrate": cd.get("baudrate"),
                                    "bytesize": cd.get("bytesize"),
                                    "parity": cd.get("parity"),
                                    "stopbits": cd.get("stopbits"),
                                    "flow_control": cd.get("flow_control"),
                                }
                        except Exception:
                            pass
                    elif hasattr(port_obj, "is_connected"):
                        live_connected = bool(getattr(port_obj, "is_connected"))
                        live_adapter = getattr(port_obj, "adapter_type", None)
            except Exception:
                live_connected = None

            # Build meta payload with only defined keys to avoid clobbering UI with nulls
            meta = {
                "type": "meta",
                "name": info.get("name"),
                "adapter": live_adapter or info.get("adapter") or info.get("adapter_type"),
                # Choose authoritative connected flag: event > live_read > snapshot
                "connected": (
                    bool(event_connected)
                    if event_connected is not None
                    else (
                        bool(live_connected)
                        if live_connected is not None
                        else bool(info.get("connected", info.get("is_running", False)) )
                    )
                ),
            }
            desc = info.get("description")
            if desc is not None:
                meta["description"] = desc
            sc = live_serial_cfg or info.get("serial_config")
            if sc is not None:
                meta["serial_config"] = sc
            ls = info.get("line_status")
            if ls is not None:
                meta["line_status"] = ls
            chain = info.get("server_chain")
            if chain:
                meta["server_chain"] = chain
            ls_seen = info.get("last_seen")
            if ls_seen is not None:
                meta["last_seen"] = ls_seen
            payload = "OMXCTRL " + json.dumps(meta, separators=(",", ":"))
            for cid in list(subs):
                try:
                    ws = self._clients.get(cid)
                    if ws is None:
                        continue
                    await ws.send_str(payload)
                except Exception:
                    try:
                        self._meta_subscribers.get(port_name, set()).discard(cid)
                    except Exception:
                        pass
        except Exception:
            pass

    def _gather_federation_overview(self) -> Dict[str, Any]:
        """Build a federation overview payload mirroring WebStatus._api_get_federation."""
        try:
            muxcon = self._find_muxcon_adapter()
            node_name = getattr(muxcon, "server_id", None) if muxcon else None
            hb_interval = getattr(muxcon, "heartbeat_interval", None) if muxcon else None
            peers_cfg = []
            connections = []
            ports_summary = []
            # Global map of live metadata for remote ports: name -> details from _conn_proxies
            meta_by_port: Dict[str, Any] = {}
            if muxcon is not None:
                # peers configured
                try:
                    for p in getattr(muxcon, "peers", []) or []:
                        peers_cfg.append({
                            "host": getattr(p, "host", None),
                            "port": getattr(p, "port", None),
                            "options": getattr(p, "options", {}) or {},
                        })
                except Exception:
                    pass
                # active connections
                try:
                    hb_state = getattr(muxcon, "_hb_state", {}) or {}
                    for cid, c in (getattr(muxcon, "connections", {}) or {}).items():
                        role = c.get("role")
                        hs = c.get("handshake") or {}
                        conn_writer = c.get("writer")
                        peer = None
                        try:
                            if conn_writer is not None:
                                peerinfo = conn_writer.get_extra_info("peername")
                                if peerinfo:
                                    peer = {"host": peerinfo[0], "port": peerinfo[1]}
                        except Exception:
                            peer = None
                        opened_at = None
                        try:
                            parts = str(cid).split(":")
                            if len(parts) >= 4:
                                opened_at = int(parts[-1])
                        except Exception:
                            opened_at = None
                        if isinstance(hs, dict):
                            hs_version = hs.get("version")
                            ct = hs.get("type")
                            hs_caps = hs.get("capabilities", [])
                        else:
                            hs_version = getattr(hs, "version", None)
                            ct = getattr(hs, "client_type", None)
                            if ct is not None and hasattr(ct, "value"):
                                ct = ct.value
                            hs_caps = getattr(hs, "capabilities", [])
                        # Ports registered on this connection
                        ports_registered = []
                        try:
                            proxies_map = getattr(muxcon, "_conn_proxies", {}) or {}
                            for pname, proxy in (proxies_map.get(cid, {}) or {}).items():
                                meta = getattr(proxy, "metadata", None)
                                origin = getattr(meta, "origin_server", None) if meta else None
                                chain_objs = getattr(meta, "server_chain", []) if meta else []
                                ftype = getattr(meta, "federation_type", None) if meta else None
                                serial_cfg = getattr(meta, "serial_config", None) if meta else None
                                line_status = getattr(meta, "line_status", None) if meta else None
                                # Normalize origin/server_chain into dicts for UI
                                try:
                                    if origin is not None and callable(getattr(origin, "to_dict", None)):
                                        origin_info = origin.to_dict()  # type: ignore[attr-defined]
                                    elif origin is not None:
                                        origin_info = {
                                            "server_id": getattr(origin, "server_id", None),
                                            "hostname": getattr(origin, "hostname", None),
                                            "port": getattr(origin, "port", None),
                                            "server_type": getattr(getattr(origin, "server_type", None), "value", None),
                                        }
                                    else:
                                        origin_info = None
                                except Exception:
                                    origin_info = {"server_id": getattr(origin, "server_id", None)} if origin else None
                                try:
                                    chain_info = []
                                    for s in (chain_objs or []):
                                        if callable(getattr(s, "to_dict", None)):
                                            chain_info.append(s.to_dict())  # type: ignore[attr-defined]
                                        else:
                                            chain_info.append({"server_id": getattr(s, "server_id", str(s))})
                                except Exception:
                                    chain_info = [{"server_id": getattr(s, "server_id", str(s))} for s in (chain_objs or [])]
                                chain_ids = [getattr(s, "server_id", str(s)) for s in (chain_objs or [])]
                                prepped = {
                                    "name": pname,
                                    "adapter_type": "remote_muxcon",
                                    "connected": bool(getattr(proxy, "is_connected", True)),
                                    "origin_server_id": getattr(origin, "server_id", None),
                                    "server_chain": chain_ids,
                                    "origin_server": origin_info,
                                    "server_chain_info": chain_info,
                                    "federation_type": (getattr(ftype, "value", ftype) if ftype is not None else None),
                                    "max_rw_users": (getattr(meta, "max_rw_users", None) if meta else None),
                                    "serial_config": serial_cfg,
                                    "line_status": line_status,
                                }
                                ports_registered.append(prepped)
                                # Track per-name metadata for later enrichment of ports_summary
                                meta_by_port[pname] = {
                                    "origin_server": origin_info,
                                    "server_chain_info": chain_info,
                                    "federation_type": prepped.get("federation_type"),
                                    "max_rw_users": prepped.get("max_rw_users"),
                                    "serial_config": serial_cfg,
                                    "line_status": line_status,
                                }
                        except Exception:
                            pass
                        # Counts per connection
                        try:
                            derive_pk = getattr(muxcon, "_derive_peer_key_from_conn_id", None)
                            peer_key = derive_pk(cid) if callable(derive_pk) else None
                            smap = getattr(muxcon, "_session_map", {}) or {}
                            lmap = getattr(muxcon, "_local_session_map", {}) or {}
                            streams_count = len(smap.get(peer_key, {}) or {}) + len(lmap.get(peer_key, {}) or {})
                        except Exception:
                            streams_count = 0
                        now_ts = int(time.time())
                        eff_open = int(c.get("opened_at", opened_at) or 0)
                        uptime_seconds = now_ts - eff_open if eff_open else None
                        hb = hb_state.get(cid, {}) if isinstance(hb_state, dict) else {}
                        hb_view = {
                            "interval_sec": hb_interval,
                            "last_req_ts": hb.get("last_req_ts"),
                            "last_ack_ts": hb.get("last_ack_ts"),
                            "rtt_ms": hb.get("rtt_ms"),
                            "missed": hb.get("missed"),
                            "status": (("ok" if hb.get("missed", 0) == 0 else "degraded") if hb else None),
                        }
                        connections.append({
                            "connection_id": cid,
                            "role": role,
                            "opened_at": c.get("opened_at", opened_at),
                            "last_seen": c.get("last_seen", opened_at),
                            "uptime_seconds": uptime_seconds,
                            "remote_peer": peer,
                            "handshake": {
                                "version": hs_version,
                                "client_type": ct,
                                "capabilities": hs_caps,
                                "server_id": c.get("server_id"),
                                "instance_id": c.get("instance_id"),
                            },
                            "active": True,
                            "ports_registered": ports_registered,
                            "counts": {"streams": streams_count, "ports": len(ports_registered)},
                            "heartbeat": hb_view,
                            **(self._derive_mpath_info_for_muxcon(muxcon, cid) or {}),
                        })
                except Exception:
                    pass

            # In web_console context, we already expose port list at top via _get_ports_snapshot(); enrich from there
            try:
                data_ports = self._get_ports_snapshot()
            except Exception:
                data_ports = []
            for p in data_ports:
                adapter_type = p.get("adapter_type") or p.get("adapter")
                if str(adapter_type) == "remote_muxcon":
                    name = p.get("name")
                    live_meta = meta_by_port.get(name)
                    origin_obj = None
                    chain_info = None
                    if live_meta:
                        origin_obj = live_meta.get("origin_server")
                        chain_info = live_meta.get("server_chain_info")
                    entry = {
                        "name": p.get("name"),
                        "description": p.get("description"),
                        "connected": bool(p.get("connected", p.get("is_running", False))),
                        "adapter_type": "remote_muxcon",
                        "status": (
                            p.get("adapter_status", {}).get("status") if isinstance(p.get("adapter_status"), dict) else p.get("state", "connected")
                        ),
                        "origin_server": origin_obj,
                        "server_chain_info": chain_info,
                        "server_chain": p.get("server_chain", []),
                        "federation_type": p.get("federation_type"),
                        "max_rw_users": p.get("max_read_write_users", p.get("max_rw_users")),
                        "connected_clients": p.get("client_count", p.get("connected_clients", 0)),
                        "serial_config": (live_meta.get("serial_config") if live_meta else p.get("serial_config")),
                        "line_status": (live_meta.get("line_status") if live_meta else p.get("line_status")),
                    }
                    ports_summary.append(entry)

            total_retx = 0
            try:
                retx_map = getattr(muxcon, "_peer_retx_count", {}) or {}
                total_retx = sum(int(v) for v in retx_map.values())
            except Exception:
                total_retx = 0
            return {
                "node": {"server_id": node_name, "adapter": "muxcon"},
                "config_note": "heartbeat_interval controls HB REQ/ACK ping/pong and dead-peer detection; set to 0 to disable",
                "heartbeat_interval_sec": hb_interval,
                "peers_configured": peers_cfg,
                "connections": connections,
                "remote_ports": ports_summary,
                "totals": {
                    "peers_configured": len(peers_cfg),
                    "connections_active": sum(1 for c in connections if c.get("active")),
                    "connections_total": len(connections),
                    "remote_ports_total": len(ports_summary),
                    "remote_ports_connected": sum(1 for r in ports_summary if r.get("connected")),
                    "retransmissions": total_retx,
                },
            }
        except Exception as e:
            return {"error": True, "message": str(e)}

    def _derive_mpath_info_for_muxcon(self, muxcon, conn_id: str):
        try:
            groups = getattr(muxcon, "_mpath_groups", {}) or {}
            for key, grp in groups.items():
                if conn_id in grp.get("conns", {}):
                    return {
                        "mpath_group": key,
                        "mpath_primary": grp.get("primary") == conn_id,
                        "mpath_paths": len(grp.get("conns", {})),
                    }
        except Exception:
            return None
        return None

    def _gather_multipath_overview(self) -> Dict[str, Any]:
        """Summarize multipath groups in-process (like WebStatus._api_get_multipath)."""
        try:
            muxcon = self._find_muxcon_adapter()
            if not muxcon:
                now = time.time()
                return {"timestamp": now, "groups": [], "totals": {"groups": 0, "connections": 0, "primaries": 0, "stale": 0}}
            mpath_groups = getattr(muxcon, "_mpath_groups", {}) or {}
            groups_payload = []
            total_conns = 0
            primaries = 0
            stale_total = 0
            now = time.time()
            # Align stale computation with WebStatus._api_get_multipath (heartbeat-aware, no flicker)
            # 1) Determine effective stale window = max(muxcon.mpath_primary_stale_sec, heartbeat_interval*2.5),
            #    with a conservative fallback if neither is set.
            # 2) Use last_activity = max(conn.last_seen, heartbeat.last_ack_ts) to avoid classifying HB-idle as stale.
            # 3) Suppress transient stale when at most one heartbeat is missed and next request isn't overdue.
            hb_interval = 0.0
            try:
                hb_iv = getattr(muxcon, "heartbeat_interval", 0) or 0
                hb_interval = float(hb_iv) if isinstance(hb_iv, (int, float)) else 0.0
            except Exception:
                hb_interval = 0.0
            effective_stale_sec = None
            try:
                base_stale = getattr(muxcon, "mpath_primary_stale_sec", None)
                base_stale = float(base_stale) if isinstance(base_stale, (int, float)) else None
                hb_window = (hb_interval * 2.5) if hb_interval and hb_interval > 0 else None
                if base_stale and hb_window:
                    effective_stale_sec = max(base_stale, hb_window)
                elif base_stale:
                    effective_stale_sec = base_stale
                elif hb_window:
                    effective_stale_sec = hb_window
                else:
                    effective_stale_sec = 45.0  # conservative default
            except Exception:
                effective_stale_sec = 45.0
            stale_cut = now - effective_stale_sec if (effective_stale_sec and effective_stale_sec > 0) else None
            # Heartbeat per-connection state for gating
            try:
                hb_state = getattr(muxcon, "_heartbeat_state", {}) or {}
            except Exception:
                hb_state = {}
            total_retx = 0
            total_tx_bytes = 0
            total_rx_bytes = 0
            for peer_key, grp in mpath_groups.items():
                conns = []
                primary = grp.get("primary")
                if primary:
                    primaries += 1
                server_ids = set()
                instance_ids = set()
                sendbuf_sz = 0
                rxbuf_depth = 0
                retx_count = 0
                try:
                    sb = getattr(muxcon, "_peer_sendbuf", {}).get(peer_key)
                    if isinstance(sb, dict):
                        sendbuf_sz = len(sb)
                except Exception:
                    sendbuf_sz = 0
                try:
                    rxst = getattr(muxcon, "_peer_rx_state", {}).get(peer_key)
                    if isinstance(rxst, dict):
                        buf = rxst.get("buffer") or {}
                        rxbuf_depth = len(buf) if isinstance(buf, dict) else 0
                except Exception:
                    rxbuf_depth = 0
                try:
                    retx_map = getattr(muxcon, "_peer_retx_count", {}) or {}
                    if peer_key in retx_map:
                        retx_count = int(retx_map.get(peer_key) or 0)
                except Exception:
                    retx_count = 0
                try:
                    total_retx += int(retx_count or 0)
                except Exception:
                    pass
                tx_bytes = 0
                rx_bytes = 0
                try:
                    tx_bytes = int((getattr(muxcon, "_peer_bytes_tx", {}) or {}).get(peer_key, 0) or 0)
                except Exception:
                    tx_bytes = 0
                try:
                    rx_bytes = int((getattr(muxcon, "_peer_bytes_rx", {}) or {}).get(peer_key, 0) or 0)
                except Exception:
                    rx_bytes = 0
                try:
                    total_tx_bytes += tx_bytes
                    total_rx_bytes += rx_bytes
                except Exception:
                    pass
                for cid, meta in grp.get("conns", {}).items():
                    opened_at = meta.get("opened_at")
                    last_seen = meta.get("last_seen")
                    pref = meta.get("pref")
                    server_id = None
                    instance_id = None
                    try:
                        cinfo = getattr(muxcon, "connections", {}).get(cid, {})
                        server_id = cinfo.get("server_id")
                        instance_id = cinfo.get("instance_id")
                    except Exception:
                        pass
                    if server_id:
                        server_ids.add(server_id)
                    if instance_id:
                        instance_ids.add(instance_id)
                    # Heartbeat-aware stale calculation
                    is_stale = False
                    last_ack = None
                    last_req = None
                    missed = 0
                    try:
                        hb = hb_state.get(cid, {}) if isinstance(hb_state, dict) else {}
                        last_ack = hb.get("last_ack_ts")
                        last_req = hb.get("last_req_ts")
                        missed = int(hb.get("missed", 0) or 0)
                    except Exception:
                        last_ack = None
                        last_req = None
                        missed = 0
                    last_activity = max(v for v in [last_seen or 0, (last_ack or 0)] if isinstance(v, (int, float))) if (last_seen or last_ack) else 0
                    overdue = False
                    try:
                        if hb_interval and last_req:
                            overdue = (now - float(last_req)) > (hb_interval * 2.5)
                    except Exception:
                        overdue = False
                    if stale_cut is not None and (last_activity or 0) < stale_cut:
                        # Candidate stale based on activity window; apply heartbeat gating to suppress flicker
                        if missed > 1 or overdue:
                            is_stale = True
                            stale_total += 1
                    conns.append({
                        "conn_id": cid,
                        "pref": pref,
                        "opened_at": opened_at,
                        "last_seen": last_seen,
                        "age_sec": (now - opened_at) if opened_at else None,
                        "idle_sec": (now - last_activity) if last_activity else ((now - last_seen) if last_seen else None),
                        "stale": is_stale,
                        "is_primary": cid == primary,
                        "server_id": server_id,
                        "instance_id": instance_id,
                    })
                total_conns += len(conns)
                non_stale = sum(1 for c in conns if not c["stale"])
                groups_payload.append({
                    "peer_key": peer_key,
                    "primary": primary,
                    "primary_pref": next((c["pref"] for c in conns if c["conn_id"] == primary), None),
                    "connections": conns,
                    "non_stale": non_stale,
                    "stale": len(conns) - non_stale,
                    "server_ids": sorted(server_ids),
                    "instance_ids": sorted(instance_ids),
                    "distinct_instances": len(instance_ids),
                    "metrics": {
                        "sendbuf_size": sendbuf_sz,
                        "rx_buffer_depth": rxbuf_depth,
                        "retransmissions": retx_count,
                        "tx_bytes": tx_bytes,
                        "rx_bytes": rx_bytes,
                    },
                })
            return {
                "timestamp": now,
                "groups": groups_payload,
                "totals": {
                    "groups": len(groups_payload),
                    "connections": total_conns,
                    "primaries": primaries,
                    "stale": stale_total,
                    "retransmissions": total_retx,
                    "tx_bytes": total_tx_bytes,
                    "rx_bytes": total_rx_bytes,
                },
                "config": {
                    "mpath_primary_stale_sec": getattr(muxcon, "mpath_primary_stale_sec", None),
                    "mpath_failover_check_sec": getattr(muxcon, "mpath_failover_check_sec", None),
                    "mpath_strategy": getattr(muxcon, "mpath_strategy", None),
                    "mpath_preemptive_promote": getattr(muxcon, "mpath_preemptive_promote", None),
                },
            }
        except Exception as e:
            return {"error": True, "message": str(e)}

    def _gather_web_clients(self) -> list[Dict[str, Any]]:
        """List web console clients inferred from ports' connected_clients."""
        out: list[Dict[str, Any]] = []
        try:
            pm = getattr(self.console_manager, "port_manager", None) if self.console_manager else None
            if not pm:
                pm = None
            # Track which clients we have already included to avoid duplicates when combining with _client_meta
            seen_ids: set[str] = set()
            for pname, port in (getattr(pm, "ports", {}) or {}).items():
                try:
                    cc = getattr(port, "connected_clients", []) or []
                    for c in cc:
                        try:
                            cid = c.get("client_id") if isinstance(c, dict) else getattr(c, "client_id", None)
                            username = c.get("username") if isinstance(c, dict) else getattr(c, "username", None)
                            if cid:
                                seen_ids.add(str(cid))
                            meta = self._resolve_client_meta(cid)
                            out.append({
                                "client_id": cid,
                                "username": username,
                                "port": pname,
                                "type": meta.get("type"),
                                "ip": meta.get("ip"),
                            })
                        except Exception:
                            continue
                except Exception:
                    continue
            # Fallback: also include any known websocket clients from _client_meta that are bound to a port,
            # in case the Port.connected_clients list doesn't expose them.
            try:
                for cid, meta in (self._client_meta or {}).items():
                    try:
                        if not isinstance(meta, dict):
                            continue
                        if cid in seen_ids:
                            continue
                        p = meta.get("port")
                        if not p:
                            continue
                        out.append({
                            "client_id": cid,
                            "username": meta.get("username"),
                            "port": p,
                            "type": meta.get("type", "websocket"),
                            "ip": meta.get("ip"),
                        })
                    except Exception:
                        continue
            except Exception:
                pass
            # Also include active login sessions so a user "logged in" is visible even before attaching to a port
            try:
                for sid, sess in (self._sessions or {}).items():
                    try:
                        out.append(
                            {
                                "client_id": f"session:{str(sid)[:8]}",
                                "username": sess.get("username"),
                                "type": "session",
                                "created": sess.get("created"),
                                "last_seen": sess.get("last_seen"),
                                "ip": sess.get("ip"),
                            }
                        )
                    except Exception:
                        continue
            except Exception:
                pass
        except Exception:
            return out
        return out

    # --- Login throttling helpers ---
    def _check_login_throttle(self, ip: Optional[str]) -> Optional[str]:
        if not (self.login_throttle_enabled and ip):
            return None
        record = self._login_failures.get(ip)
        if not record:
            return None
        now = time.time()
        attempts = record.get("attempts")
        if not isinstance(attempts, deque):
            attempts = deque()
            record["attempts"] = attempts
        cutoff = now - float(self.login_throttle_window_seconds)
        while attempts and attempts[0] < cutoff:
            attempts.popleft()
        blocked_until = float(record.get("blocked_until") or 0.0)
        if blocked_until and now < blocked_until:
            remaining = max(1, int(blocked_until - now))
            minutes, seconds = divmod(remaining, 60)
            if minutes and seconds:
                wait = f"{minutes}m {seconds}s"
            elif minutes:
                wait = f"{minutes}m"
            else:
                wait = f"{seconds}s"
            return f"Too many failed attempts from this address. Try again in {wait}."
        if blocked_until and now >= blocked_until:
            record["blocked_until"] = 0.0
        if not attempts and not record.get("blocked_until"):
            self._login_failures.pop(ip, None)
        return None

    def _record_login_failure(self, ip: Optional[str]) -> None:
        if not (self.login_throttle_enabled and ip):
            return
        now = time.time()
        record = self._login_failures.setdefault(ip, {"attempts": deque(), "blocked_until": 0.0})
        attempts = record.get("attempts")
        if not isinstance(attempts, deque):
            attempts = deque()
            record["attempts"] = attempts
        cutoff = now - float(self.login_throttle_window_seconds)
        while attempts and attempts[0] < cutoff:
            attempts.popleft()
        if record.get("blocked_until"):
            # Already blocked; do not extend window to avoid indefinite locks
            return
        attempts.append(now)
        if len(attempts) >= self.login_throttle_max_attempts:
            record["blocked_until"] = now + float(self.login_throttle_lock_seconds)
            attempts.clear()
            try:
                self.logger.warning(
                    "Login throttling triggered for %s (locked %ss)", ip, self.login_throttle_lock_seconds
                )
            except Exception:
                pass

    def _clear_login_failures(self, ip: Optional[str]) -> None:
        if not (self.login_throttle_enabled and ip):
            return
        record = self._login_failures.get(ip)
        if not record:
            return
        attempts = record.get("attempts")
        if isinstance(attempts, deque):
            attempts.clear()
        record["blocked_until"] = 0.0
        self._login_failures.pop(ip, None)

    # --- Utility: IP extraction ---
    def _get_client_ip(self, request: web.Request) -> Optional[str]:
        """Best-effort client IP from headers or transport.

        Honors X-Forwarded-For and Forwarded headers; falls back to request.remote
        or transport peername. Does not validate public vs private ranges.
        """
        try:
            # X-Forwarded-For: comma-separated list; take first
            xff = request.headers.get("X-Forwarded-For")
            if xff:
                parts = [p.strip() for p in xff.split(",") if p.strip()]
                if parts:
                    return parts[0]
        except Exception:
            pass
        try:
            fwd = request.headers.get("Forwarded")
            if fwd:
                # Simple parse: look for for= value
                # Example: Forwarded: for=192.0.2.60; proto=http; by=203.0.113.43
                items = fwd.split(";")
                for it in items:
                    it = it.strip()
                    if it.lower().startswith("for="):
                        val = it[4:].strip().strip('"')
                        # Remove possible brackets
                        if val.startswith("[") and "]" in val:
                            val = val[1:val.find("]")]
                        # Remove possible port suffix
                        if ":" in val and val.count(":") == 1:
                            host, _port = val.split(":", 1)
                            return host
                        return val
        except Exception:
            pass
        try:
            if request.remote:
                return str(request.remote)
        except Exception:
            pass
        try:
            peer = request.transport.get_extra_info("peername") if request.transport else None
            if isinstance(peer, (list, tuple)) and peer:
                return str(peer[0])
        except Exception:
            pass
        return None

    def _resolve_client_meta(self, client_id: Optional[str]) -> Dict[str, Any]:
        """Resolve client metadata (type, ip) given a client_id.

        Resolution order:
          - self._client_meta (websocket manager)
          - console_manager.client_to_manager mapping: if manager is TcpServerAdapter, try to find the client session and its address
          - Fallback to websocket if client_id looks like 'ws:'
        """
        meta: Dict[str, Any] = {}
        if not client_id:
            return meta
        try:
            # If we have local meta (set on websocket connect), prefer that
            if isinstance(self._client_meta, dict) and client_id in self._client_meta:
                m = self._client_meta.get(client_id) or {}
                meta["type"] = m.get("type", "websocket")
                meta["ip"] = m.get("ip")
                meta["username"] = m.get("username")
                meta["port"] = m.get("port")
                return meta
        except Exception:
            pass
        # Try console manager mapping to identify manager and pull IPs from TCP listener
        try:
            cm = self.console_manager
            if cm and hasattr(cm, "client_to_manager"):
                mgr = cm.client_to_manager.get(client_id)
                if mgr is not None:
                    # If it looks like the TCP server adapter, try to get address
                    atype = getattr(mgr, "get_adapter_type", None)
                    atype = atype() if callable(atype) else getattr(mgr, "adapter_type", None)
                    if str(atype).lower() in ("client_listener", "tcp", "tcp_server"):
                        try:
                            # TcpServerAdapter maintains clients dict with sessions having .address
                            clients = getattr(mgr, "clients", {}) or {}
                            sess = clients.get(client_id)
                            if sess is not None:
                                addr = getattr(sess, "address", None)
                                if addr:
                                    meta["ip"] = addr
                                meta["type"] = "tcp"
                                return meta
                        except Exception:
                            pass
                    # Otherwise, assume manager is this (web_console) or another ws-capable manager
                    meta["type"] = meta.get("type") or ("websocket" if str(client_id).startswith("ws:") else None)
        except Exception:
            pass
        if "type" not in meta:
            meta["type"] = "websocket" if str(client_id).startswith("ws:") else None
        return meta

    def _render_login(
        self,
        error: bool = False,
        next_url: Optional[str] = None,
        message: Optional[str] = None,
    ) -> bytes:
        """Render a simple login form, with optional Jinja2 template support.

        Template name: 'login.html.j2' with variables: error (bool), next (str), realm (str)
        """
        # Template path
        try:
            if self._jinja_env:
                tmpl = self._jinja_env.get_template("login.html.j2")
                base_path = self._effective_base_path(None)
                html_text = tmpl.render(
                    error=bool(error),
                    next=next_url or "/",
                    realm=self.realm,
                    logo_url=self._get_logo_url(),
                    base_path=base_path,
                    message=message,
                )
                return html_text.encode("utf-8")
        except Exception as e:
            self.logger.debug(f"login template render failed: {e}")

        # Inline fallback
        try:
            msg_text = str(message) if message is not None else None
        except Exception:
            msg_text = message
        if msg_text:
            msg = f"<p style='color:#f66'>{html.escape(msg_text)}</p>"
        elif error:
            msg = "<p style='color:#f66'>Invalid username or password</p>"
        else:
            msg = ""
        nxt = html.escape(str(next_url or "/"))
        # Compute logo abbreviation from realm (first two initials)
        try:
            parts = [p for p in str(self.realm).split() if p]
            abbr = (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper()
        except Exception:
            abbr = "OM"
        # Prefer real logo image if present
        logo_url = None
        try:
            logo_url = self._get_logo_url()
        except Exception:
            logo_url = None
        brand_logo_html = (
            f'<img class="logo-img" src="{html.escape(str(logo_url))}" alt="logo">' if logo_url else f'<div class="logo">{abbr}</div>'
        )
        bp = self._effective_base_path(None)
        body = f"""
<!doctype html>
<html>
    <head>
        <meta charset=\"utf-8\" />
        <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
        <title>{html.escape(self.realm)} Login</title>
        <link rel=\"stylesheet\" href=\"{bp}/static/web_console.css\" />
    </head>
    <body class=\"login\">\n        <form class=\"card\" method=\"POST\" action=\"{bp}/login\">\n            <div class=\"brand\">\n                {brand_logo_html}\n                <div>\n                    <div class=\"title\">{html.escape(self.realm)}</div>\n                    <div class=\"subtitle\">Web Console</div>\n                </div>\n            </div>\n            {msg}
            <h1>Sign in to {html.escape(self.realm)}</h1>
            <input type=\"hidden\" name=\"next\" value=\"{nxt}\" />
            <input type=\"text\" name=\"username\" placeholder=\"Username\" autocomplete=\"username\" required />
            <input type=\"password\" name=\"password\" placeholder=\"Password\" autocomplete=\"current-password\" required />
            <button class=\"btn\" type=\"submit\">Sign in</button>
            <div class=\"hint\">After login you'll be redirected back.</div>
        </form>
    </body>
</html>
"""
        return body.encode("utf-8")

    # --- CSRF & RBAC helpers for plugins/APIs ---
    async def _handle_api_csrf(self, request: web.Request) -> web.Response:
        """Return a CSRF token for the current login session.

        For simplicity, we use the session cookie value as the token.
        Browser clients must echo it via the X-OMX-CSRF header for state-changing requests.
        """
        username = request.get("username")
        if not username:
            return web.Response(status=401, text="Unauthorized\n")
        sid = request.cookies.get(self._session_cookie_name)
        if not sid:
            return web.Response(status=401, text="Unauthorized\n")
        return web.Response(body=json.dumps({"csrf": sid}).encode("utf-8"), content_type="application/json")

    def _check_csrf(self, request: web.Request) -> bool:
        """Verify CSRF header for non-GET/HEAD/OPTIONS requests.

        Accepts header X-OMX-CSRF matching the session cookie value.
        Programmatic Basic Auth requests (no session cookie) are exempt.
        """
        try:
            if request.method in ("GET", "HEAD", "OPTIONS"):
                return True
            # If Basic Auth used (no session cookie), skip CSRF check to preserve API use
            if self._session_cookie_name not in request.cookies:
                return True
            expected = request.cookies.get(self._session_cookie_name)
            provided = request.headers.get("X-OMX-CSRF") or request.headers.get("X-CSRF-Token")
            return bool(expected and provided and provided == expected)
        except Exception:
            return False

    def _require_permission(self, request: web.Request, allowed: tuple[str, ...] = ("admin",)) -> Optional[str]:
        """Enforce that the authenticated user has one of the allowed permissions.

        Returns the username on success, else raises HTTP 401/403.
        """
        username = request.get("username")
        if not username:
            raise web.HTTPUnauthorized()
        perms = self._get_effective_permission(username, request)
        if perms not in allowed:
            raise web.HTTPForbidden()
        return username

    def _get_effective_permission(self, username: Optional[str], request: Optional[web.Request] = None) -> Optional[str]:
        """Resolve the caller's permission, honoring per-request overrides when present."""
        try:
            if request is not None:
                override = request.get("perm_override")
                if isinstance(override, str) and override:
                    return override
        except Exception:
            pass
        if username and self.auth_manager:
            try:
                return self.auth_manager.get_user_permissions(username)
            except Exception:
                return None
        return None

    # --- Plugin loader ---
    def _load_plugins(self, app: web.Application) -> None:
        """Load and initialize web plugins as configured.

        Config schema examples under web_console.plugins:
          - ["openmux.server.web_plugins.config_editor"]
          - [{"module": "openmux.server.web_plugins.os_customizer", "enabled": true}]
        Each module may expose register_plugin(app, adapter) -> Optional[dict]
        The returned mapping may include a "nav" list for UI integration.
        """
        cfg = self.plugins_cfg or []
        if not isinstance(cfg, list) or not cfg:
            return
        nav_items: list[Dict[str, Any]] = []
        for entry in cfg:
            try:
                if isinstance(entry, str):
                    mod_name = entry
                    enabled = True
                    opts = {}
                elif isinstance(entry, dict):
                    mod_name = entry.get("module") or entry.get("name")
                    enabled = entry.get("enabled", True)
                    opts = entry
                else:
                    continue
                if not enabled or not mod_name:
                    continue
                mod = importlib.import_module(str(mod_name))
                # Allow plugin to register its routes
                reg = getattr(mod, "register_plugin", None)
                info = None
                if callable(reg):
                    try:
                        info = reg(app, self, opts)
                    except TypeError:
                        # Backward-compat: register_plugin(app, adapter)
                        info = reg(app, self)
                if isinstance(info, dict):
                    nav = info.get("nav")
                    if isinstance(nav, list):
                        nav_items.extend([n for n in nav if isinstance(n, dict)])
                self.logger.info(f"Loaded web plugin: {mod_name}")
            except Exception as e:
                self.logger.error(f"Error loading plugin {entry}: {e}", exc_info=True)
        self._plugin_nav = nav_items

    def _get_ports_snapshot(self):
        ports = []
        seen_names: set[str] = set()
        pm = None
        try:
            pm = getattr(self.console_manager, "port_manager", None) if self.console_manager else None
        except Exception:
            pm = None
        if pm is not None:
            try:
                raw_ports = getattr(pm, "ports", {}) or {}
                for name, port in list(raw_ports.items()):
                    try:
                        info = port.get_status() if hasattr(port, "get_status") else {"name": name}
                        if "name" not in info:
                            info["name"] = name
                        # Compute a stable composite id: <server_id>::<port_name>
                        comp_id = None
                        try:
                            origin_id = info.get("origin_server_id")
                            if not origin_id:
                                # Try to derive a local server_id via muxcon adapter
                                mux = self._find_muxcon_adapter()
                                origin_id = getattr(mux, "server_id", None) if mux else None
                            if origin_id:
                                comp_id = f"{origin_id}::{info['name']}"
                            else:
                                comp_id = f"local::{info['name']}"
                        except Exception:
                            comp_id = f"local::{info['name']}"
                        info["id"] = comp_id
                        # Expose last_seen for federated RemotePortProxy when available
                        try:
                            if hasattr(port, "last_seen"):
                                ls_val = getattr(port, "last_seen")
                                if ls_val is not None:
                                    info["last_seen"] = float(ls_val)
                        except Exception:
                            pass
                        # If this is a loopback port, ensure dummy serial metadata so UI shows info badges
                        try:
                            atype = (info.get("adapter_type") or info.get("adapter") or "").lower()
                            if atype == "loopback":
                                # Populate default serial config if missing
                                if not info.get("serial_config"):
                                    info["serial_config"] = {
                                        "device": f"loopback:{name}",
                                        "baudrate": 9600,
                                        "bytesize": 8,
                                        "parity": "N",
                                        "stopbits": 1,
                                        "flow_control": "none",
                                    }
                                # Populate default line status if missing
                                if not info.get("line_status"):
                                    info["line_status"] = {"DCD": False, "DSR": True, "CTS": True, "RTS": True, "DTR": True}
                        except Exception:
                            pass
                        # If this is a federated remote proxy, enrich with serial/line-status from metadata
                        try:
                            meta = getattr(port, "metadata", None)
                            if meta is not None:
                                sc = getattr(meta, "serial_config", None)
                                if sc is not None:
                                    info["serial_config"] = sc
                                ls = getattr(meta, "line_status", None)
                                if ls is not None:
                                    info["line_status"] = ls
                                # Origin server identity and chain for federated ports
                                try:
                                    origin = getattr(meta, "origin_server", None)
                                    if origin is not None:
                                        # Basic server identity
                                        info["origin_server_id"] = getattr(origin, "server_id", None)
                                        info["origin_server_hostname"] = getattr(origin, "hostname", None)
                                        info["origin_server_port"] = getattr(origin, "port", None)
                                        st = getattr(origin, "server_type", None)
                                        info["origin_server_type"] = getattr(st, "value", st) if st is not None else None
                                        # Full origin object when possible
                                        try:
                                            to_dict = getattr(origin, "to_dict", None)
                                            if callable(to_dict):
                                                info["origin_server"] = to_dict()
                                            else:
                                                info["origin_server"] = {
                                                    "server_id": getattr(origin, "server_id", None),
                                                    "hostname": getattr(origin, "hostname", None),
                                                    "port": getattr(origin, "port", None),
                                                    "server_type": getattr(getattr(origin, "server_type", None), "value", None),
                                                    "description": getattr(origin, "description", None),
                                                }
                                        except Exception:
                                            info["origin_server"] = {"server_id": getattr(origin, "server_id", None)}
                                    # Chain
                                    chain = getattr(meta, "server_chain", []) or []
                                    info["server_chain"] = [getattr(s, "server_id", str(s)) for s in chain]
                                    # Detailed chain objects when possible
                                    try:
                                        chain_info = []
                                        for s in chain:
                                            to_dict = getattr(s, "to_dict", None)
                                            if callable(to_dict):
                                                chain_info.append(to_dict())
                                            else:
                                                chain_info.append({
                                                    "server_id": getattr(s, "server_id", str(s)),
                                                    "hostname": getattr(s, "hostname", None),
                                                    "port": getattr(s, "port", None),
                                                    "server_type": getattr(getattr(s, "server_type", None), "value", None),
                                                    "description": getattr(s, "description", None),
                                                })
                                        info["server_chain_info"] = chain_info
                                    except Exception:
                                        info["server_chain_info"] = [{"server_id": sid} for sid in info.get("server_chain", [])]
                                    # Federation type if present
                                    ftype = getattr(meta, "federation_type", None)
                                    info["federation_type"] = getattr(ftype, "value", ftype) if ftype is not None else None
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        # Attach clients info when available
                        try:
                            cc = getattr(port, "connected_clients", None)
                            if isinstance(cc, list):
                                info["client_count"] = len(cc)
                                # Best-effort usernames list
                                usernames = []
                                details = []
                                for c in cc:
                                    u = None
                                    cid = None
                                    if isinstance(c, dict):
                                        u = c.get("username") or c.get("client_id")
                                        cid = c.get("client_id")
                                    else:
                                        u = getattr(c, "username", None) or getattr(c, "client_id", None)
                                        cid = getattr(c, "client_id", None)
                                    if u:
                                        usernames.append(str(u))
                                    # Build enriched detail using _client_meta when available
                                    try:
                                        ip = None
                                        typ = None
                                        meta = self._resolve_client_meta(cid) if cid else {}
                                        if meta:
                                            ip = meta.get("ip")
                                            typ = meta.get("type")
                                        details.append({
                                            "client_id": cid,
                                            "username": u,
                                            "ip": ip,
                                            "type": typ or None,
                                        })
                                    except Exception:
                                        pass
                                if usernames:
                                    info["clients"] = usernames
                                # Merge in any clients we know from _client_meta for this port (avoid double count)
                                try:
                                    existing_ids = set()
                                    for c in cc:
                                        try:
                                            ecid = c.get("client_id") if isinstance(c, dict) else getattr(c, "client_id", None)
                                            if ecid:
                                                existing_ids.add(str(ecid))
                                        except Exception:
                                            continue
                                    extra = [cid for cid, meta in (self._client_meta or {}).items() if isinstance(meta, dict) and meta.get("port") == name and str(cid) not in existing_ids]
                                    if extra:
                                        info["client_count"] = int(info.get("client_count", 0)) + len(extra)
                                        try:
                                            ex_usernames = [str((self._client_meta.get(cid) or {}).get("username") or cid) for cid in extra]
                                            info.setdefault("clients", [])
                                            info["clients"].extend(ex_usernames)
                                            # Also extend details with enriched meta
                                            for cid in extra:
                                                try:
                                                    meta = self._resolve_client_meta(cid) or {}
                                                    details.append({
                                                        "client_id": cid,
                                                        "username": meta.get("username") or cid,
                                                        "ip": meta.get("ip"),
                                                        "type": meta.get("type", "websocket"),
                                                    })
                                                except Exception:
                                                    continue
                                        except Exception:
                                            pass
                                    if details:
                                        info["client_details"] = details
                                except Exception:
                                    pass
                            else:
                                # If the port doesn't expose connected_clients, approximate from our meta
                                try:
                                    extras = [cid for cid, meta in (self._client_meta or {}).items() if isinstance(meta, dict) and meta.get("port") == name]
                                    if extras:
                                        info["client_count"] = len(extras)
                                        try:
                                            info["clients"] = [str((self._client_meta.get(cid) or {}).get("username") or cid) for cid in extras]
                                            # Build details list as well
                                            det = []
                                            for cid in extras:
                                                try:
                                                    meta = self._resolve_client_meta(cid) or {}
                                                    det.append({
                                                        "client_id": cid,
                                                        "username": meta.get("username") or cid,
                                                        "ip": meta.get("ip"),
                                                        "type": meta.get("type", "websocket"),
                                                    })
                                                except Exception:
                                                    continue
                                            if det:
                                                info["client_details"] = det
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        ports.append(info)
                        try:
                            if info.get("name"):
                                seen_names.add(str(info["name"]))
                        except Exception:
                            pass
                    except Exception:
                        continue
                # Removed fallback enumeration of unified adapters to avoid duplicate paths.
                # Source of truth for listings is PortManager.ports above.
            except Exception:
                ports = []
        # Ensure a stable, user-friendly order: sort alphabetically by port name (case-insensitive)
        try:
            ports.sort(key=lambda p: str((p or {}).get("name", "")).lower())
        except Exception:
            pass
        return ports

    async def _ensure_assets(self) -> None:
        """Ensure required xterm assets exist locally before serving the UI."""
        xterm_css = Path(self.static_dir) / "xterm" / "css" / "xterm.css"
        xterm_js = Path(self.static_dir) / "xterm" / "lib" / "xterm.js"
        fit_js = Path(self.static_dir) / "xterm-addon-fit" / "lib" / "xterm-addon-fit.js"
        need_css = not xterm_css.is_file()
        need_js = not xterm_js.is_file()
        need_fit = not fit_js.is_file()
        if not (need_css or need_js or need_fit):
            self._asset_error = None
            return

        missing = []
        if need_css:
            missing.append(xterm_css)
        if need_js:
            missing.append(xterm_js)
        if need_fit:
            missing.append(fit_js)

        rel_missing = []
        for path in missing:
            try:
                rel_missing.append(str(path.relative_to(self.static_dir)))
            except Exception:
                rel_missing.append(str(path))

        script_hint = "scripts/install_xtermjs.py"
        msg = (
            "Missing xterm assets ({missing}) under static_dir='{static_dir}'. "
            "Install them with {script} or copy the files manually before starting the web console."
        ).format(missing=", ".join(rel_missing), static_dir=self.static_dir, script=script_hint)
        self._asset_error = msg
        raise RuntimeError(msg)

    async def _drop_client_channel(self, client_id: str, ws: Optional[Any] = None, detach_port: bool = True) -> None:
        """Remove websocket delivery state for a client, optionally detaching it from the port."""
        if ws is None:
            ws = self._clients.get(client_id)

        self._clients.pop(client_id, None)
        if ws is not None:
            self._ws_to_client.pop(ws, None)

        if isinstance(self._client_meta, dict):
            self._client_meta.pop(client_id, None)

        for port_name, subscribers in list(self._meta_subscribers.items()):
            if not isinstance(subscribers, set):
                continue
            subscribers.discard(client_id)
            if not subscribers:
                self._meta_subscribers.pop(port_name, None)

        manager = self.console_manager
        if manager is None or not detach_port:
            return

        try:
            port_map = getattr(manager, "client_port_map", {})
            port_name = port_map.get(client_id) if isinstance(port_map, dict) else None
            if port_name and hasattr(manager, "disconnect_client_from_port"):
                await manager.disconnect_client_from_port(client_id, port_name)
            if hasattr(manager, "unregister_client_channel"):
                manager.unregister_client_channel(client_id)
        except Exception:
            self.logger.exception("Client cleanup failed for %s", client_id)

    # Client manager API used by ConsoleManager to forward data to clients
    async def send_data_to_client(self, client_id: str, data: bytes) -> bool:
        ws = self._clients.get(client_id)
        if ws is None:
            await self._drop_client_channel(client_id, detach_port=False)
            return False
        try:
            # aiohttp WebSocketResponse expects send_bytes for binary
            if hasattr(ws, "send_bytes"):
                await ws.send_bytes(data)
            else:
                # Fallback (shouldn't happen with aiohttp)
                await ws.send_str(data.decode("utf-8", errors="ignore"))
            return True
        except (ConnectionResetError, OSError) as e:
            self.logger.warning(f"WebSocket transport failed for {client_id}: {e}")
            await self._drop_client_channel(client_id, ws, detach_port=False)
            return False
        except RuntimeError as e:
            self.logger.warning(
                "WebSocket send runtime error for %s: %s (closed=%s close_code=%s)",
                client_id,
                e,
                getattr(ws, "closed", None),
                getattr(ws, "close_code", None),
            )
            return False

    def get_status_info(self) -> Dict[str, Any]:
        # Build endpoint string and details, including dual-port HTTPS when enabled
        endpoints_list: list[str] = []
        endpoint_str: str
        http_redirect = False
        if self.use_tls and self.ssl_port and self.ssl_port != self.port:
            # Dual-port mode: HTTP on port (redirect-only), HTTPS on ssl_port
            endpoints_list = [f"{self.host}:{self.port}", f"{self.host}:{self.ssl_port}"]
            endpoint_str = ",".join(endpoints_list)
            http_redirect = True
        else:
            # Single-site mode (HTTP or HTTPS on self.port)
            endpoints_list = [f"{self.host}:{self.port}"]
            endpoint_str = endpoints_list[0]

        info = {
            "type": self.get_adapter_type(),
            "status": "running" if self.is_running else "stopped",
            "endpoint": endpoint_str,
            "clients": f"{len(self._clients)} connected",
            "details": {
                "adapter_name": self.name,
                "host": self.host,
                "port": self.port,
                "ui": self.enable_ui,
                "realm": self.realm,
                "tls": bool(self.use_tls),
                "endpoints": endpoints_list,
            },
        }
        if self.use_tls:
            # Provide additional TLS-related details
            try:
                info["details"]["ssl_port"] = self.ssl_port
            except Exception:
                pass
            try:
                info["details"]["http_redirect"] = http_redirect
            except Exception:
                pass
        if self._started_monotonic is not None:
            try:
                info["details"]["uptime_seconds"] = max(0.0, time.monotonic() - self._started_monotonic)
            except Exception:
                pass
        return info

    # Internal helper for probe detail payloads
    def _probe_details(self, live_only: bool = False) -> Dict[str, Any]:
        uptime = None
        if self._started_monotonic is not None:
            try:
                uptime = max(0.0, time.monotonic() - self._started_monotonic)
            except Exception:
                uptime = None
        # Version discovery
        version = "unknown"
        try:
            if _dist_version:
                version = _dist_version("openmux")  # type: ignore
        except Exception:
            pass
        data: Dict[str, Any] = {
            "component": "web_console",
            "status": "ok",
            "version": version,
            "uptime_seconds": uptime,
            "clients": len(self._clients),
        }
        if not live_only:
            data["console_manager"] = bool(self.console_manager is not None)
            data["port_manager"] = bool(getattr(self.console_manager, "port_manager", None) is not None)
        return data

    # --- Required abstract methods from BaseGenericAdapter (no ports created) ---
    def get_port_configurations(self) -> Dict[str, Dict[str, Any]]:
        """This adapter does not create or own ports."""
        return {}

    async def create_port(self, port_name: str, config: Dict[str, Any]) -> Optional[Any]:
        """Stub: Web console does not create ports."""
        return None

    # --- TLS helpers ---
    async def _create_server_ssl_context(self) -> Optional[ssl.SSLContext]:
        """Create an SSL context for the web console server.

        Honors configuration keys:
          - use_tls (bool)
          - ssl_cert / ssl_key
          - tls_autogen (bool)
          - tls_dir (path for generated files)

        Returns:
            ssl.SSLContext or None when TLS disabled.
        """
        if not self.use_tls:
            return None
        cert_file = self.ssl_cert
        key_file = self.ssl_key
        if (not cert_file or not key_file) and self.tls_autogen:
            cert_file, key_file = await self._ensure_autogen_cert()
        if not cert_file or not key_file:
            raise ValueError("use_tls enabled but missing ssl_cert/ssl_key and tls_autogen disabled")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
        # Reasonable defaults; we avoid requesting client certs for the web console
        ctx.options |= ssl.OP_NO_SSLv2 | ssl.OP_NO_SSLv3
        try:
            ctx.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20:@STRENGTH")
        except Exception:
            pass
        return ctx

    async def _ensure_autogen_cert(self) -> tuple[str, str]:
        """Generate (or reuse) a self-signed certificate and key for web_console.

        Uses EC P-256 by default. Files are placed under tls_dir as
        'server.crt' and 'server.key'. If they already exist, reuse them.
        """
        os.makedirs(self.tls_dir, exist_ok=True)
        cert_path = os.path.join(self.tls_dir, "server.crt")
        key_path = os.path.join(self.tls_dir, "server.key")
        if os.path.exists(cert_path) and os.path.exists(key_path):
            return cert_path, key_path
        # Lazy import cryptography to avoid hard dep if TLS not used
        try:
            from cryptography import x509  # type: ignore
            from cryptography.hazmat.primitives import hashes, serialization  # type: ignore
            from cryptography.hazmat.primitives.asymmetric import ec  # type: ignore
            from cryptography.hazmat.backends import default_backend  # type: ignore
            from cryptography.x509.oid import NameOID  # type: ignore
            from datetime import datetime, timedelta  # type: ignore
        except Exception as e:  # pragma: no cover - dependency missing
            raise RuntimeError(f"cryptography not available for TLS autogen: {e}")

        # Generate EC key and self-signed cert
        key = ec.generate_private_key(ec.SECP256R1(), backend=default_backend())
        cn = None
        try:
            # Prefer server id from config manager if present
            server_id = None
            cfg_mgr = getattr(getattr(self, "console_manager", None), "config_manager", None)
            if cfg_mgr and hasattr(cfg_mgr, "config"):
                server_cfg = getattr(cfg_mgr, "config", {}).get("server", {})
                server_id = server_cfg.get("id") or server_cfg.get("name")
            if not server_id:
                import socket

                server_id = socket.gethostname()
            cn = str(server_id)
        except Exception:
            cn = "OpenMux"

        subject = issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, cn),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "OpenMux"),
            ]
        )
        now = datetime.utcnow()
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=3650))
            .sign(private_key=key, algorithm=hashes.SHA256(), backend=default_backend())
        )
        with open(key_path, "wb") as f:
            f.write(
                key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )
        with open(cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        return cert_path, key_path

    async def destroy_port(self, port_name: str) -> None:
        """Stub: Web console does not create ports."""
        return None
