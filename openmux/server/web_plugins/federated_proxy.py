import re
import base64
import hmac
import hashlib
import json
import time
from aiohttp import web, ClientSession, ClientTimeout
from typing import Any, Dict, Optional, Tuple

from . import ADAPTER_APP_KEY
from urllib.parse import urlsplit

# Federated admin proxy plugin (skeleton). Proxies selected admin endpoints to
# other known nodes via existing federation. For now, provide a read-only list
# of known federated connections; no proxying implemented to avoid risks.


async def _handle_list(request: web.Request) -> web.StreamResponse:
    adapter = request.app[ADAPTER_APP_KEY]
    username = request.get("username")
    if not username:
        raise web.HTTPUnauthorized()
    muxcon = adapter._find_muxcon_adapter() if hasattr(adapter, "_find_muxcon_adapter") else None
    conns = []
    if muxcon is not None:
        try:
            for cid, c in (getattr(muxcon, "connections", {}) or {}).items():
                try:
                    role = c.get("role")
                    sid = c.get("server_id")
                    inst = c.get("instance_id")
                    peer = None
                    w = c.get("writer")
                    if w is not None:
                        peerinfo = w.get_extra_info("peername")
                        if peerinfo:
                            peer = {"host": peerinfo[0], "port": peerinfo[1]}
                except Exception:
                    role = None
                    sid = None
                    inst = None
                    peer = None
                conns.append({
                    "connection_id": cid,
                    "role": role,
                    "server_id": sid,
                    "instance_id": inst,
                    "peer": peer,
                })
        except Exception:
            pass
    import json
    return web.Response(body=json.dumps({"connections": conns}).encode("utf-8"), content_type="application/json")


def _norm_base_path(v: Optional[str]) -> str:
    try:
        if not v or v == "/":
            return ""
        s = str(v).strip()
        if not s:
            return ""
        if not s.startswith("/"):
            s = "/" + s
        if s.endswith("/") and s != "/":
            s = s[:-1]
        return s
    except Exception:
        return ""


_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}


def _is_ui_allowed_path(tail: str) -> bool:
    # Allow common UI paths: root, index, status, console, static assets, plugins (GET-only)
    t = (tail or "").lstrip("/")
    if not t:
        return True
    if t in {"index.html", "status", "console"}:
        return True
    if t.startswith("static/"):
        return True
    if t.startswith("plugins/"):
        return True
    # Block everything else by default in v1
    return False


def _inject_remote_banner(body: bytes, node: str) -> bytes:
    """Insert a small banner indicating remote view for clarity.

    Tries to place after <body> or before <main>; falls back to prepending.
    """
    try:
        text = body.decode("utf-8", errors="ignore")
        banner = (
            '<div class="proxy-remote-banner" style="background:#fff3cd;color:#664d03;border:1px solid #ffe69c;padding:8px 12px;margin:8px 0;">'
            + 'Viewing remote node: <strong>' + (node or '?') + '</strong>' +
            '</div>'
        )
        if "<body" in text:
            # insert right after the opening <body...>
            import re as _re
            def _ins(m):
                return m.group(0) + banner
            new = _re.sub(r"<body[^>]*>", _ins, text, count=1)
            return new.encode("utf-8")
        if "<main" in text:
            return text.replace("<main", banner + "<main", 1).encode("utf-8")
        # prepend as last resort
        return (banner + text).encode("utf-8")
    except Exception:
        return body


def _resolve_upstream(adapter, node: str, tail: str, request: web.Request) -> Optional[str]:
    """Resolve upstream URL based solely on active federation connection.

    Internal-only: we use the muxcon peer socket address as the host. No user-provided
    mapping is involved. Assumes remote UI serves at https://<peer-host>/.
    """
    try:
        muxcon = adapter._find_muxcon_adapter() if hasattr(adapter, "_find_muxcon_adapter") else None
        if not muxcon:
            return None
        conns = (getattr(muxcon, "connections", {}) or {})
        target = None
        for _cid, c in conns.items():
            try:
                if str(c.get("server_id")) == str(node):
                    target = c
                    break
            except Exception:
                continue
        if not target:
            return None
        w = target.get("writer")
        peer = None
        if w is not None:
            try:
                p = w.get_extra_info("peername")
                if p:
                    peer = p[0]
            except Exception:
                peer = None
        if not peer:
            return None
        scheme = "https"
        host = peer
        # Preserve query string correctly
        q = getattr(request, "query_string", None) or request.rel_url.query_string
        qs = ("?" + q) if q else ""
        path = "/" + (tail.lstrip("/"))
        return f"{scheme}://{host}{path}{qs}"
    except Exception:
        return None


def _rewrite_location(loc: str, upstream_base: str, proxied_prefix: str) -> str:
    try:
        if not loc:
            return loc
        # Absolute to upstream base -> replace prefix
        if upstream_base and loc.startswith(upstream_base):
            rest = loc[len(upstream_base):]
            if rest and not rest.startswith("/"):
                rest = "/" + rest
            return proxied_prefix + rest
        # Root-relative -> prefix with proxied prefix
        if loc.startswith("/"):
            return proxied_prefix + loc
    except Exception:
        pass
    return loc


_RE_PATH_ATTR = re.compile(r"(?i)(;\s*Path=)([^;]*)")


def _rewrite_set_cookie(cookie_value: str, proxied_prefix: str) -> str:
    try:
        if not cookie_value:
            return cookie_value
        # If Path attribute present and absolute, rewrite to proxied prefix + original
        def _sub(m: re.Match) -> str:
            path = m.group(2).strip()
            if path.startswith("/"):
                new_path = proxied_prefix + path
            else:
                new_path = proxied_prefix + "/" + path if path else proxied_prefix or "/"
            return m.group(1) + new_path

        return _RE_PATH_ATTR.sub(_sub, cookie_value)
    except Exception:
        return cookie_value


def _rewrite_html_root_links(body: bytes, proxied_prefix: str, node: str) -> bytes:
    """Prefix root-relative URLs (href/src/action) with the proxied prefix and inject remote banner.

    This keeps remote pages functional when they emit absolute paths like "/login" or "/static/...".
    """
    try:
        text = body.decode("utf-8", errors="ignore")
        # Avoid double-prefixing already proxied links
        pp = proxied_prefix.rstrip("/")
        import re as _re
        # Only rewrite attributes that begin with "/" and not "//"
        def _prefix_attr(m: _re.Match) -> str:
            attr = m.group(1)
            val = m.group(2)
            if val.startswith("//"):
                return m.group(0)  # protocol-relative; leave
            if val.startswith(pp + "/"):
                return m.group(0)  # already prefixed
            return f"{attr}=\"{pp}{val}\""

        text = _re.sub(r"(?i)\b(href|src|action)\s*=\s*\"(/[^\"]*)\"", _prefix_attr, text)
        text = _re.sub(r"(?i)\b(href|src|action)\s*=\s*'(/[^']*)'", _prefix_attr, text)

        # Inject remote banner for clarity
        out = _inject_remote_banner(text.encode("utf-8"), node)
        return out
    except Exception:
        return body


async def _handle_proxy(request: web.Request) -> web.StreamResponse:
    """Proxy placeholder for /proxy/{node}/{tail:.*}.

    V1 behavior: admin-only, GET-only, returns 501 Not Implemented (WS and HTTP pass-through to be added).
    Computes effective X-Forwarded-Prefix for upstream planning.
    """
    adapter = request.app.get(ADAPTER_APP_KEY)
    if adapter is None:
        raise web.HTTPInternalServerError(text="Adapter not available")
    # Admin-only guard
    username = request.get("username")
    if not username:
        # For browsers/tools, advertise Basic so non-session clients can auth
        return web.Response(status=401, text="Unauthorized\n", headers={"WWW-Authenticate": f'Basic realm="{getattr(adapter, "realm", "OpenMux")}"'})
    try:
        perms = adapter.auth_manager.get_user_permissions(username) if getattr(adapter, "auth_manager", None) else None
        if perms not in ("admin",):
            raise web.HTTPForbidden()
    except web.HTTPException:
        raise
    except Exception:
        raise web.HTTPForbidden()

    node = request.match_info.get("node")
    tail = request.match_info.get("tail") or ""
    method = request.method.upper()
    if method not in ("GET",):
        raise web.HTTPMethodNotAllowed(method, allowed_methods=["GET"])  # v1 read-only

    # Compute proxied prefix: base_path + /proxy/{node}
    # Try adapter base-path helper when available
    try:
        base_path = adapter._effective_base_path(request) if hasattr(adapter, "_effective_base_path") else ""
    except Exception:
        # Fall back to X-Forwarded-Prefix
        base_path = request.headers.get("X-Forwarded-Prefix", "")
    proxied_prefix = _norm_base_path(base_path) + f"/proxy/{node}"

    # Allowlist enforcement (UI-only for v1)
    if not _is_ui_allowed_path(tail):
        raise web.HTTPForbidden(text="Path not allowed")

    t = (tail or "").lstrip("/")
    if t == "console":
        # Redirect to local console with server hint; enhanced console supports FQPN
        raise web.HTTPFound(location=f"{_norm_base_path(base_path)}/console?server={node}")

    # Resolve upstream URL from plugin options
    if not node:
        raise web.HTTPBadRequest(text="Missing node")
    upstream_url = _resolve_upstream(adapter, str(node), tail, request)
    if not upstream_url:
        raise web.HTTPBadGateway(text="No upstream mapping for node")

    # Compute upstream_base to support Location rewrite
    # Take scheme://host[:port]/base from mapping
    # For rewrites, use scheme://host derived from upstream_url
    try:
        parts = upstream_url.split("/", 3)
        upstream_base = parts[0] + "//" + parts[2] if len(parts) >= 3 else ""
    except Exception:
        upstream_base = ""

    # Perform streaming GET with caps and timeout
    timeout = ClientTimeout(total=30)
    max_bytes = int((getattr(adapter, "proxy_max_bytes", None) or 8) * 1024 * 1024)  # 8 MiB default
    read = 0

    # Build SSO header
    def _make_sso_header() -> Optional[str]:
        """Return an SSO header string.

        Preference order:
        1) HMAC (legacy) when adapter.sso_secret is configured: "v1;<b64payload>;<hexmac>"
        2) Zero-config Ed25519 using MuxCon private key if available: "v1e;<kid>;<b64payload>;<b64sig>"
        """
        try:
            user = request.get("username") or "sso"
            now = int(time.time())
            claims = {
                "ver": 1,
                "user": user,
                "perm": "admin",
                "iat": now,
                "exp": now + 60,  # 1 minute
                "node": node,
            }
            payload = json.dumps(claims, separators=(",", ":")).encode("utf-8")
            payload_b64 = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

            # 1) Legacy shared secret (if explicitly configured)
            secret = getattr(adapter, "sso_secret", None)
            if secret:
                mac = hmac.new(str(secret).encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
                return f"v1;{payload_b64};{mac}"

            # 2) Zero-config: sign with muxcon Ed25519 private key
            try:
                muxcon = adapter._find_muxcon_adapter() if hasattr(adapter, "_find_muxcon_adapter") else None
                priv = getattr(muxcon, "_auth_priv", None) if muxcon else None
                kid = getattr(muxcon, "_auth_key_id", None) if muxcon else None
                if priv and kid:
                    sig = priv.sign(payload)  # bytes
                    sig_b64 = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
                    return f"v1e;{kid};{payload_b64};{sig_b64}"
            except Exception:
                # Fall through to None
                pass
            return None
        except Exception:
            return None

    sso_header = _make_sso_header()
    fwd_prefix = proxied_prefix

    # Try candidate upstream URLs with common scheme/port combos; disable TLS verify for internal peers
    parsed = urlsplit(upstream_url)
    host = parsed.hostname or ""
    path_qs = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
    candidates: list[tuple[str, str, Optional[int]]] = []
    if (parsed.scheme or "https") == "https":
        if parsed.port:
            candidates.append(("https", host, parsed.port))
        candidates.append(("https", host, 8443))
        candidates.append(("https", host, 443))
        # Fallback to http if https fails
        candidates.append(("http", host, 8081))
        candidates.append(("http", host, 80))
    else:
        if parsed.port:
            candidates.append(("http", host, parsed.port))
        candidates.append(("http", host, 8081))
        candidates.append(("http", host, 80))

    last_error = None
    async with ClientSession(timeout=timeout) as sess:
        for sch, hst, prt in candidates:
            base = f"{sch}://{hst}" + (f":{prt}" if prt else "")
            url = base + path_qs
            hdrs = {
                "Accept": request.headers.get("Accept", "*/*"),
                "Accept-Language": request.headers.get("Accept-Language", ""),
                "User-Agent": request.headers.get("User-Agent", "OpenMuxProxy/1.0"),
                "X-Forwarded-Prefix": fwd_prefix,
            }
            if sso_header:
                # Always include the standard header, plus any custom alias if configured locally
                std_name = "X-OMX-SSO"
                cfg_name = getattr(adapter, "sso_trust_header", std_name)
                hdrs[std_name] = sso_header
                if cfg_name and cfg_name != std_name:
                    hdrs[cfg_name] = sso_header
            try:
                ssl_opt = False if sch == "https" else None
                async with sess.get(url, headers=hdrs, ssl=ssl_opt) as upstream:
                    upstream_base = base
                    status = upstream.status
                    headers = {}
                    for k, v in upstream.headers.items():
                        lk = k.lower()
                        if lk in _HOP_BY_HOP or lk == "content-length":
                            continue
                        if lk == "location":
                            v = _rewrite_location(v, upstream_base, proxied_prefix)
                        if lk == "set-cookie":
                            v = _rewrite_set_cookie(v, proxied_prefix)
                        headers[k] = v

                    # If HTML, rewrite root-relative links and forms; else stream
                    ctype = upstream.headers.get("Content-Type", "").lower()
                    if "html" in ctype:
                        # Read with cap
                        buf = bytearray()
                        async for chunk in upstream.content.iter_chunked(64 * 1024):
                            read += len(chunk)
                            if read > max_bytes:
                                raise web.HTTPRequestEntityTooLarge(max_size=max_bytes, actual_size=read)
                            buf.extend(chunk)
                        body = _rewrite_html_root_links(bytes(buf), proxied_prefix, node)
                        headers["Content-Length"] = str(len(body))
                        return web.Response(status=status, headers=headers, body=body)
                    else:
                        resp = web.StreamResponse(status=status, headers=headers)
                        await resp.prepare(request)
                        async for chunk in upstream.content.iter_chunked(64 * 1024):
                            read += len(chunk)
                            if read > max_bytes:
                                await resp.write_eof()
                                raise web.HTTPRequestEntityTooLarge(max_size=max_bytes, actual_size=read)
                            await resp.write(chunk)
                        await resp.write_eof()
                        return resp
            except Exception as e:
                last_error = e
                continue
    raise web.HTTPBadGateway(text=f"Upstream connect failed: {last_error}")


def register_plugin(app: web.Application, adapter, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = "/plugins/federated-proxy"
    app.router.add_get(base + "/connections", _handle_list)
    # Proxy entrypoint; capture any tail
    app.router.add_route("GET", "/proxy/{node}", _handle_proxy)
    app.router.add_route("GET", "/proxy/{node}/{tail:.*}", _handle_proxy)
    return {
        "nav": [
            {"title": "Federation", "path": base + "/connections", "require": "admin"},
        ]
    }
