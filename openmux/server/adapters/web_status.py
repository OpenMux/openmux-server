"""Unified Web Status Adapter (HTTP-only).

Provides a minimal read-only JSON HTTP API replacing the legacy web server.

Endpoints:
    GET /            : Landing page enumerating endpoints
    GET /api/status  : Adapter/server status summary
    GET /api/clients : Connected HTTP status clients
    GET /api/ports   : Port inventory (requires main_port_manager)
    GET /api/federation : Federation + remote port view (via muxcon)
    GET /api/multipath  : Multipath connection groups/statistics (muxcon)
    POST /api/fault  : Fault injection controls (when enabled)

Fault injection (optional) lets tests manipulate muxcon behavior (freeze, drop
heartbeats, close/reset connections) for resilience testing.
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, Optional, Set

from .base_adapter import AdapterCapability, BaseGenericAdapter


class WebStatusAdapter(BaseGenericAdapter):  # noqa: Vulture
    """HTTP status / inspection adapter.

    Provides a minimal JSON-over-HTTP interface (no WebSocket / UI) for
    retrieving operational information about OpenMux, and—optionally—injecting
    test faults into the muxcon federation adapter. It does not create or own
    ports. Each inbound TCP connection handles exactly one HTTP request before
    closing (no keep‑alive / pipelining support).

    Main groups of endpoints:
        status      : Adapter/server status snapshot
        clients     : Connected status-client sessions
        ports       : Local + remote port inventory (via main_port_manager)
        federation  : Federation peer + connection overview (via muxcon)
        multipath   : Multipath group health/statistics (muxcon)
        fault (POST): Fault injection controls (if enabled)

    Configuration keys (subset):
        host (str): Listen address (default "0.0.0.0").
        port (int): Listen port (default 8080).
        enable_http_api (bool): Master on/off switch for all endpoints.
        cors_enable (bool): If true, adds permissive CORS header.
        enable_fault_injection (bool): Allow POST /api/fault operations.
    """

    def __init__(self, name: str, config: Dict[str, Any]):
        """Initialize adapter instance.

        Args:
            name: Logical adapter name.
            config: Adapter configuration mapping.
        """
        super().__init__(name, config)
        self.host = config.get("host", "0.0.0.0")
        self.port = config.get("port", 8080)
        self.enable_http_api = bool(config.get("enable_http_api", True))
        # Use only 'cors_enable' going forward
        self.cors_enabled = bool(config.get("cors_enable", True))
        self.enable_fault_injection = bool(config.get("enable_fault_injection", False))
        self.server = None
        self.logger = logging.getLogger(f"openmux.adapter.web_status.{self.name}")

        # Track simple connections for /api/clients reporting (lightweight)
        self.clients = {}  # type: Dict[str, Dict[str, Any]]

        # Dependencies that OpenMux main will set if available
        self.console_manager = None
        self.auth_manager = None

    def get_capabilities(self) -> Set[AdapterCapability]:
        """Return capability flags.

        Returns:
            Set with ``ACCEPTS_CONNECTIONS`` only.
        """
        return {AdapterCapability.ACCEPTS_CONNECTIONS}

    def get_adapter_type(self) -> str:
        """Return human-readable adapter type."""
        return "WebStatus"

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> bool:
        """Validate configuration mapping.

        Supports both nested (``web_status`` key) and flat config styles.

        Args:
            config: Raw adapter config.

        Returns:
            True if port is an integer within 1..65535.
        """
        cfg = config.get("web_status", config)
        port = cfg.get("port", 8080)
        try:
            port_int = int(port)
        except Exception:  # justification: invalid port config; treat as invalid without logging
            return False
        return 1 <= port_int <= 65535

    def get_port_configurations(self) -> Dict[str, Dict[str, Any]]:
        """Return empty mapping (adapter does not provide ports)."""
        return {}

    # Optional setters for dependencies to mirror TcpServerAdapter
    def set_console_manager(self, console_manager):
        """Inject optional console manager dependency.

        Provided for symmetry with other adapters; may be unused.
        """
        self.console_manager = console_manager

    def set_auth_manager(self, auth_manager):
        """Inject optional auth manager dependency."""
        self.auth_manager = auth_manager

    async def start(self) -> bool:
        """Start HTTP server listener.

        Returns:
            True if server bound and serving; False on failure.
        """
        try:
            self.server = await asyncio.start_server(self._handle_client_connection, self.host, self.port)
            self.is_running = True
            await self.server.start_serving()
            self.logger.info(f"WebStatus HTTP server listening on {self.host}:{self.port}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to start WebStatus server: {e}", exc_info=True)
            return False

    async def stop(self) -> None:
        """Stop HTTP server and release socket."""
        self.is_running = False
        if self.server:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:  # justification: shutdown/teardown best-effort; ignore wait_closed errors
                pass
            self.server = None
        self.logger.info("WebStatus server stopped")

    async def _handle_client_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Process a single inbound TCP client connection.

        Reads one HTTP request, responds, and closes the connection (no
        keep‑alive). Captures lightweight client metadata for /api/clients.

        Args:
            reader: Stream reader for inbound data.
            writer: Stream writer for response output.
        """
        client_addr = writer.get_extra_info("peername")
        client_id = str(uuid.uuid4())
        self.clients[client_id] = {
            "address": (f"{client_addr[0]}:{client_addr[1]}" if client_addr else "unknown"),
            "connected_time": time.time(),
            "protocol": "http",
        }
        try:
            await self._handle_http_session(reader, writer)
        except Exception as e:
            self.logger.debug(f"HTTP session error: {e}", exc_info=True)
        finally:
            self.clients.pop(client_id, None)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # justification: client may already be closed; ignore
                pass

    async def _handle_http_session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Parse a basic HTTP/1.1 request and dispatch to an endpoint.

        Limitations: one request per connection, ignores most headers, no
        transfer‑encoding support, and treats unknown methods as 404.

        Args:
            reader: Stream reader.
            writer: Stream writer.
        """
        # Parse request line
        request_line = await reader.readline()
        if not request_line:
            return
        try:
            request_line = request_line.decode("utf-8", errors="ignore").strip()
            method, path, *_ = request_line.split(" ")
        except Exception:  # justification: malformed request line is a client error; 400 without logging
            await self._send_http_error(writer, 400, "Bad Request")
            return

        # Collect headers, detect content-length
        headers = {}
        content_length = 0
        while True:
            line = await reader.readline()
            if not line or line == b"\r\n":
                break
            try:
                line_str = line.decode("utf-8", errors="ignore").strip()
                if not line_str:
                    continue
                if ":" in line_str:
                    h, v = line_str.split(":", 1)
                    headers[h.lower()] = v.strip()
            except Exception:  # justification: header parsing is best-effort; ignore malformed lines
                pass
        try:
            content_length = int(headers.get("content-length", 0))
        except Exception:  # justification: non-integer content-length; treat as 0
            content_length = 0

        body_bytes = b""
        if content_length > 0:
            try:
                body_bytes = await reader.readexactly(content_length)
            except Exception:  # justification: short read/disconnect; treat as empty body
                body_bytes = b""
        if not self.enable_http_api:
            await self._send_http_error(writer, 404, "Not Found")
            return

        # Basic CORS preflight handling (when enabled)
        if method == "OPTIONS":
            await self._send_cors_preflight_ok(writer, headers)
            return

        if method == "GET" and path == "/":
            # Simple landing listing endpoints
            await self._send_json(
                writer,
                200,
                {
                    "endpoints": [
                        "/api/status",
                        "/api/clients",
                        "/api/ports",
                        "/api/federation",
                        "/api/multipath",
                        "/api/fault (POST)",
                    ],
                    "note": "See docs/examples/fault_injection.md for fault injection POST schema",
                },
            )
        elif method == "GET" and path == "/api/status":
            await self._api_get_status(writer)
        elif method == "GET" and path == "/api/clients":
            await self._api_get_clients(writer)
        elif method == "GET" and path == "/api/ports":
            await self._api_get_ports(writer)
        elif method == "GET" and path == "/api/federation":
            await self._api_get_federation(writer)
        elif method == "GET" and path == "/api/multipath":
            await self._api_get_multipath(writer)
        elif method == "POST" and path == "/api/fault":
            await self._api_post_fault(writer, body_bytes)
        else:
            await self._send_http_error(writer, 404, "Not Found")

    async def _api_get_status(self, writer: asyncio.StreamWriter) -> None:
        """Send adapter/server status payload.

        Args:
            writer: Stream writer to send JSON response.
        """
        client_count = len(self.clients)
        status = {
            "adapter": self.get_adapter_type(),
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "running": self.is_running,
            "features": {
                "http_api_enabled": self.enable_http_api,
                "websocket_enabled": False,
                "web_ui_enabled": False,
            },
            "connections": client_count,
            "timestamp": time.time(),
        }
        await self._send_json(writer, 200, status)

    async def _api_get_clients(self, writer: asyncio.StreamWriter) -> None:
        """Send list of currently connected HTTP status clients.

        Args:
            writer: Stream writer for JSON response.
        """
        clients = []
        # If a TCP server adapter is also present, clients list might be richer.
        # Here we expose only HTTP status clients tracked by this adapter.
        for cid, info in self.clients.items():
            clients.append(
                {
                    "client_id": cid,
                    "address": info.get("address"),
                    "connected_time": info.get("connected_time"),
                    "protocol": info.get("protocol", "http"),
                }
            )
        await self._send_json(writer, 200, {"clients": clients})

    async def _api_get_ports(self, writer: asyncio.StreamWriter) -> None:
        """Send list of ports gathered from the main port manager.

        Args:
            writer: Stream writer.
        """
        # Use main_port_manager from unified environment if available
        if self.main_port_manager and hasattr(self.main_port_manager, "get_port_list_with_federation"):
            try:
                ports = await self.main_port_manager.get_port_list_with_federation()
                await self._send_json(writer, 200, {"ports": ports})
                return
            except Exception as e:  # justification: surface error to client; avoid duplicate server log
                await self._send_json(writer, 500, {"error": True, "message": str(e)})
                return
        await self._send_json(
            writer,
            503,
            {"error": True, "message": "Port manager not available"},
        )

    async def _api_get_federation(self, writer: asyncio.StreamWriter) -> None:
        """Send federation overview including remote ports and connections.

        Aggregates data from the muxcon adapter plus port manager to expose
        peer configuration, live connections, remote port summaries, and
        derived totals.

        Args:
            writer: Stream writer.
        """
        # Build federation overview from muxcon adapter (if present) and port manager
        try:
            node_name = None
            hb_interval = None
            peers_cfg = []
            connections = []
            ports_summary = []

            # Discover muxcon adapter among unified adapters
            muxcon = None
            if self.main_port_manager and hasattr(self.main_port_manager, "unified_adapters"):
                for ad in self.main_port_manager.unified_adapters:
                    atype = getattr(ad, "get_adapter_type", None)
                    at = atype() if callable(atype) else getattr(ad, "adapter_type", "")
                    if str(at).lower() == "muxcon":
                        muxcon = ad
                        break

            if muxcon is not None:
                # Prefer server_id as canonical identity
                node_name = getattr(muxcon, "server_id", None)
                hb_interval = getattr(muxcon, "heartbeat_interval", None)
                # peers configured
                try:
                    for p in getattr(muxcon, "peers", []) or []:
                        peers_cfg.append(
                            {
                                # node_name deprecated; no display name for peers beyond host/port
                                "host": getattr(p, "host", None),
                                "port": getattr(p, "port", None),
                                "options": getattr(p, "options", {}) or {},
                            }
                        )
                except Exception:  # justification: peers config enumeration best-effort; ignore
                    pass
                # active connections
                try:
                    hb_state = getattr(muxcon, "_hb_state", {}) or {}
                    # Collect proxy metadata to enrich remote_ports later
                    meta_by_port = {}
                    for cid, c in (getattr(muxcon, "connections", {}) or {}).items():
                        role = c.get("role")
                        hs = c.get("handshake") or {}
                        # reader object unused here (writer provides peer info)
                        conn_writer = c.get("writer")
                        peer = None
                        try:
                            # For incoming connections, peername is from writer
                            if conn_writer is not None:
                                peerinfo = conn_writer.get_extra_info("peername")
                                if peerinfo:
                                    peer = {
                                        "host": peerinfo[0],
                                        "port": peerinfo[1],
                                    }
                        except Exception:  # justification: peername introspection best-effort
                            peer = None
                        # For outgoing connections, derive peer from conn_id if needed
                        try:
                            if not peer and isinstance(cid, str) and cid.startswith("out:"):
                                parts = cid.split(":")
                                if len(parts) >= 4:
                                    peer = {
                                        "host": parts[1],
                                        "port": int(parts[2]),
                                    }
                        except Exception:  # justification: derive peer from conn_id best-effort
                            pass
                        # Derive opened_at from conn_id suffix timestamp if available
                        opened_at = None
                        try:
                            parts = str(cid).split(":")
                            if len(parts) >= 4:
                                opened_at = int(parts[-1])
                        except Exception:  # justification: opened_at parse best-effort
                            opened_at = None
                        # Safe extraction of handshake fields
                        if isinstance(hs, dict):
                            hs_version = hs.get("version")
                            ct = hs.get("type")
                            hs_caps = hs.get("capabilities", [])
                        else:
                            hs_version = getattr(hs, "version", None)
                            ct = getattr(hs, "client_type", None)
                            # If enum-like with value, unwrap
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
                                # Build V2 origin/server_chain info while preserving legacy fields
                                origin_info = None
                                try:
                                    if origin is not None:
                                        to_dict = getattr(origin, "to_dict", None)
                                        if callable(to_dict):
                                            origin_info = to_dict()
                                        else:
                                            origin_info = {
                                                "server_id": getattr(origin, "server_id", None),
                                                "hostname": getattr(origin, "hostname", None),
                                                "port": getattr(origin, "port", None),
                                                "server_type": getattr(getattr(origin, "server_type", None), "value", None),
                                            }
                                except Exception:
                                    origin_info = {"server_id": getattr(origin, "server_id", None)} if origin else None

                                chain_ids = [getattr(s, "server_id", str(s)) for s in (chain_objs or [])]
                                chain_info = []
                                try:
                                    for s in chain_objs or []:
                                        to_dict = getattr(s, "to_dict", None)
                                        if callable(to_dict):
                                            chain_info.append(to_dict())
                                        else:
                                            chain_info.append({"server_id": getattr(s, "server_id", str(s))})
                                except Exception:
                                    chain_info = [{"server_id": sid} for sid in chain_ids]

                                prepped = {
                                    "name": pname,
                                    "adapter_type": "remote_muxcon",
                                    "connected": bool(getattr(proxy, "is_connected", True)),
                                    # V1 compatibility
                                    "origin_server_id": getattr(origin, "server_id", None),
                                    "server_chain": chain_ids,
                                    # V2 enriched fields
                                    "origin_server": origin_info,
                                    "server_chain_info": chain_info,
                                    "federation_type": (getattr(ftype, "value", ftype) if ftype is not None else None),
                                    "max_rw_users": (getattr(meta, "max_rw_users", None) if meta else None),
                                }
                                ports_registered.append(prepped)
                                # Cache for later remote_ports enrichment
                                try:
                                    meta_by_port[pname] = {
                                        "origin_server": origin_info,
                                        "server_chain_info": chain_info,
                                        "federation_type": prepped["federation_type"],
                                        "max_rw_users": prepped["max_rw_users"],
                                    }
                                except Exception:
                                    pass
                        except Exception:  # justification: proxies listing best-effort; continue
                            pass
                        # Counts per connection (peer-scoped streams; all paths for a peer share sessions)
                        try:
                            derive_pk = getattr(muxcon, "_derive_peer_key_from_conn_id", None)
                            peer_key = derive_pk(cid) if callable(derive_pk) else None
                            smap = getattr(muxcon, "_session_map", {}) or {}
                            lmap = getattr(muxcon, "_local_session_map", {}) or {}
                            streams_count = len(smap.get(peer_key, {}) or {}) + len(lmap.get(peer_key, {}) or {})
                        except Exception:  # justification: session count lookup best-effort; default 0
                            streams_count = 0
                        # Compute uptime in seconds if possible
                        now_ts = int(time.time())
                        eff_open = int(c.get("opened_at", opened_at) or 0)
                        uptime_seconds = now_ts - eff_open if eff_open else None
                        # Heartbeat health view
                        hb = hb_state.get(cid, {}) if isinstance(hb_state, dict) else {}
                        hb_view = {
                            "interval_sec": hb_interval,
                            "last_req_ts": hb.get("last_req_ts"),
                            "last_ack_ts": hb.get("last_ack_ts"),
                            "rtt_ms": hb.get("rtt_ms"),
                            "missed": hb.get("missed"),
                            "status": (("ok" if hb.get("missed", 0) == 0 else "degraded") if hb else None),
                        }

                        connections.append(
                            {
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
                                "counts": {
                                    "streams": streams_count,
                                    "ports": len(ports_registered),
                                },
                                "heartbeat": hb_view,
                                # Multipath augmentation (best-effort)
                                **(self._derive_mpath_info(muxcon, cid) or {}),
                            }
                        )
                except Exception:  # justification: connection synthesis best-effort; continue
                    pass

            # Collect remote ports from port manager listing
            ports = []
            if self.main_port_manager and hasattr(self.main_port_manager, "get_port_list_with_federation"):
                try:
                    ports = await self.main_port_manager.get_port_list_with_federation()
                except Exception:  # justification: port listing best-effort; default empty
                    ports = []
            for p in ports:
                adapter_type = p.get("adapter_type") or p.get("adapter")
                if str(adapter_type) == "remote_muxcon":
                    # Enrich with live metadata gathered from muxcon if available
                    name = p.get("name")
                    live_meta = locals().get("meta_by_port", {}).get(name) if name else None
                    origin_obj = None
                    chain_info = None
                    if live_meta:
                        origin_obj = live_meta.get("origin_server")
                        chain_info = live_meta.get("server_chain_info")
                    else:
                        # Fallback to legacy fields if live meta not present
                        origin_obj = {
                            "server_id": p.get("origin_server_id", p.get("origin_server")),
                            "hostname": p.get("origin_server_hostname"),
                            "port": p.get("origin_server_port"),
                            "server_type": p.get("origin_server_type"),
                        }
                        chain_info = None

                    entry = {
                        "name": p.get("name"),
                        "description": p.get("description"),
                        "connected": bool(p.get("connected", p.get("is_running", False))),
                        "adapter_type": "remote_muxcon",
                        "status": (
                            p.get("adapter_status", {}).get("status")
                            if isinstance(p.get("adapter_status"), dict)
                            else p.get("state", "connected")
                        ),
                        # V2 enriched
                        "origin_server": origin_obj,
                        "server_chain_info": chain_info,
                        # V1 compatibility
                        "server_chain": p.get("server_chain", []),
                        "federation_type": p.get("federation_type"),
                        "max_rw_users": p.get("max_read_write_users", p.get("max_rw_users")),
                        "connected_clients": p.get("client_count", p.get("connected_clients", 0)),
                    }
                    ports_summary.append(entry)

            # Compute simple totals and muxcon-level metrics
            total_retx = 0
            try:
                retx_map = getattr(muxcon, "_peer_retx_count", {}) or {}
                total_retx = sum(int(v) for v in retx_map.values())
            except Exception:
                total_retx = 0

            payload = {
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
            await self._send_json(writer, 200, payload)
        except Exception as e:  # justification: API surfaces error to client; avoid extra server log
            await self._send_json(writer, 500, {"error": True, "message": str(e)})

    async def _api_get_multipath(self, writer: asyncio.StreamWriter) -> None:
        """Send multipath group statistics.

        Inspects muxcon internal multipath tables to summarize path health,
        staleness, identity diversity, and primary selection state.

        Args:
            writer: Stream writer.
        """
        try:
            muxcon = None
            if self.main_port_manager and hasattr(self.main_port_manager, "unified_adapters"):
                for ad in getattr(self.main_port_manager, "unified_adapters", []) or []:
                    try:
                        atype_fn = getattr(ad, "get_adapter_type", None)
                        atype = atype_fn() if callable(atype_fn) else getattr(ad, "adapter_type", "")
                        if str(atype).lower() == "muxcon":
                            muxcon = ad
                            break
                    except Exception:  # justification: adapter type detection best-effort
                        pass
            if not muxcon:
                await self._send_json(
                    writer,
                    200,
                    {
                        "timestamp": time.time(),
                        "groups": [],
                        "totals": {"groups": 0, "connections": 0, "primaries": 0, "stale": 0},
                    },
                )
                return
            mpath_groups = getattr(muxcon, "_mpath_groups", {}) or {}
            # Heartbeat state (best-effort) to align UI staleness with adapter logic
            hb_state = getattr(muxcon, "_hb_state", {}) or {}
            groups_payload = []
            total_conns = 0
            primaries = 0
            stale_total = 0
            now = time.time()
            # Derive an effective staleness window similar to muxcon failover logic.
            # Use max(mpath_primary_stale_sec, heartbeat_interval * 2.5) to avoid
            # false UI "stale" during normal heartbeat gaps.
            stale_cut = None
            effective_stale_sec = None
            try:
                stale_sec = getattr(muxcon, "mpath_primary_stale_sec", None)
                if not isinstance(stale_sec, (int, float)) or stale_sec <= 0:
                    # Fallback default if unset; keep generous to avoid flicker
                    stale_sec = 45.0
                eff = float(stale_sec)
                try:
                    hb_int = float(getattr(muxcon, "heartbeat_interval", 0) or 0)
                except Exception:
                    hb_int = 0.0
                if hb_int > 0:
                    hb_window = hb_int * 2.5
                    if hb_window > eff:
                        eff = hb_window
                effective_stale_sec = eff
                stale_cut = now - eff
            except Exception:  # justification: stale cutoff compute best-effort
                stale_cut = None
                effective_stale_sec = None
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
                # Peer-level metrics (best-effort): sendbuf size, rx buffer depth, retx count
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
                # Bytes TX/RX per peer
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
                    # Consider both tx/rx activity and last ACK; align with adapter
                    last_seen = meta.get("last_seen")
                    last_rx_seen = meta.get("last_rx_seen")
                    try:
                        hb_c = hb_state.get(cid, {}) if isinstance(hb_state, dict) else {}
                        last_ack = hb_c.get("last_ack_ts")
                        last_req = hb_c.get("last_req_ts")
                    except Exception:
                        last_ack = None
                        last_req = None
                    pref = meta.get("pref")
                    # Pull server/instance identity from live connection table
                    server_id = None
                    instance_id = None
                    try:
                        cinfo = getattr(muxcon, "connections", {}).get(cid, {})
                        server_id = cinfo.get("server_id")
                        instance_id = cinfo.get("instance_id")
                    except Exception:  # justification: identity extraction best-effort
                        pass
                    if server_id:
                        server_ids.add(server_id)
                    if instance_id:
                        instance_ids.add(instance_id)
                    # Compute last activity using RX, TX, and heartbeat ACK timestamps
                    last_activity = None
                    try:
                        vals = [v for v in (last_rx_seen, last_seen, last_ack) if isinstance(v, (int, float))]
                        last_activity = max(vals) if vals else None
                    except Exception:
                        last_activity = last_seen
                    is_stale = False
                    if stale_cut is not None:
                        la = float(last_activity or 0)
                        if la < stale_cut:
                            # Candidate stale; apply heartbeat-aware guardrails to avoid flicker
                            is_stale = True
                            try:
                                hb_missed = int((hb_state.get(cid, {}) or {}).get("missed", 0) or 0)
                            except Exception:
                                hb_missed = 0
                            # If only a single heartbeat is outstanding (missed <= 1), treat as not stale.
                            # Be stricter only when multiple cycles have been missed or the last ACK is far overdue.
                            try:
                                hb_int = float(getattr(muxcon, "heartbeat_interval", 0) or 0)
                            except Exception:
                                hb_int = 0.0
                            overdue = False
                            try:
                                # Consider ACK overdue if last_req exists and now exceeds 2.5 * hb interval since request
                                if hb_int > 0 and last_req:
                                    overdue = (now - float(last_req)) > (hb_int * 2.5)
                            except Exception:
                                overdue = False
                            if hb_missed <= 1 and not overdue:
                                is_stale = False
                    if is_stale:
                        stale_total += 1
                    conns.append(
                        {
                            "conn_id": cid,
                            "pref": pref,
                            "opened_at": opened_at,
                            "last_seen": last_seen,
                            "age_sec": (now - opened_at) if opened_at else None,
                            "idle_sec": (now - (last_activity or last_seen) ) if (last_activity or last_seen) else None,
                            "stale": is_stale,
                            "is_primary": cid == primary,
                            "server_id": server_id,
                            "instance_id": instance_id,
                        }
                    )
                total_conns += len(conns)
                non_stale = sum(1 for c in conns if not c["stale"])
                groups_payload.append(
                    {
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
                    }
                )
            payload = {
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
            await self._send_json(writer, 200, payload)
        except Exception as e:  # justification: API surfaces error to client; avoids duplicate logging
            await self._send_json(writer, 500, {"error": True, "message": str(e)})

    def _derive_mpath_info(self, muxcon, conn_id: str):
        """Derive multipath augmentation fields for a single connection.

        Args:
            muxcon: Muxcon adapter instance.
            conn_id: Connection identifier.

        Returns:
            Mapping with group id, primary flag, and path count, or None if
            not part of any group.
        """
        try:
            groups = getattr(muxcon, "_mpath_groups", {}) or {}
            for key, grp in groups.items():
                if conn_id in grp.get("conns", {}):
                    return {
                        "mpath_group": key,
                        "mpath_primary": grp.get("primary") == conn_id,
                        "mpath_paths": len(grp.get("conns", {})),
                    }
        except Exception:  # justification: best-effort; return None when groups not available
            return None
        return None

    async def _api_post_fault(self, writer: asyncio.StreamWriter, body: bytes) -> None:
        """Handle fault injection POST request.

        Args:
            writer: Stream writer for response.
            body: Raw request body bytes (JSON expected).
        """
        if not self.enable_fault_injection:
            await self._send_json(writer, 403, {"error": True, "message": "Fault injection disabled"})
            return
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:  # justification: invalid JSON is a client error; 400 without logging
            await self._send_json(writer, 400, {"error": True, "message": "Invalid JSON"})
            return
        action = payload.get("action")
        conn_id = payload.get("connection_id")
        params = payload.get("params", {})
        if not action:
            await self._send_json(writer, 400, {"error": True, "message": "Missing action"})
            return
        muxcon = None
        if self.main_port_manager and hasattr(self.main_port_manager, "unified_adapters"):
            for ad in getattr(self.main_port_manager, "unified_adapters", []) or []:
                try:
                    atype_fn = getattr(ad, "get_adapter_type", None)
                    atype = atype_fn() if callable(atype_fn) else getattr(ad, "adapter_type", "")
                    if str(atype).lower() == "muxcon":
                        muxcon = ad
                        break
                except Exception:  # justification: adapter type detection best-effort
                    pass
        if muxcon is None:
            await self._send_json(writer, 503, {"error": True, "message": "Muxcon adapter not available"})
            return
        # Map action -> muxcon method names or inline effects
        result: Dict[str, Any] = {"action": action, "connection_id": conn_id}
        try:
            if action == "list":
                result["fault_states"] = getattr(muxcon, "_fault_state", {})
            elif action == "freeze":  # stop reading from connection
                ok = await self._invoke_muxcon_fault(muxcon, "freeze_connection", conn_id)
                result["applied"] = ok
            elif action == "unfreeze":
                ok = await self._invoke_muxcon_fault(muxcon, "unfreeze_connection", conn_id)
                result["applied"] = ok
            elif action == "drop_heartbeats":
                ok = await self._invoke_muxcon_fault(muxcon, "set_drop_heartbeats", conn_id, True)
                result["applied"] = ok
            elif action == "restore_heartbeats":
                ok = await self._invoke_muxcon_fault(muxcon, "set_drop_heartbeats", conn_id, False)
                result["applied"] = ok
            elif action == "close_conn":
                ok = await self._invoke_muxcon_fault(muxcon, "force_close_connection", conn_id, params.get("linger", 0))
                result["applied"] = ok
            elif action == "reset_conn":
                ok = await self._invoke_muxcon_fault(muxcon, "force_reset_connection", conn_id)
                result["applied"] = ok
            else:
                await self._send_json(writer, 400, {"error": True, "message": f"Unknown action {action}"})
                return
            await self._send_json(writer, 200, result)
        except Exception as e:  # justification: API surfaces error to client; avoid duplicate server log
            await self._send_json(writer, 500, {"error": True, "message": str(e)})

    async def _invoke_muxcon_fault(self, muxcon, method: str, conn_id: Optional[str], *args) -> bool:
        """Invoke a muxcon fault-injection helper safely.

        Args:
            muxcon: Muxcon adapter instance.
            method: Method name to call.
            conn_id: Target connection id (may be None for global actions).
            *args: Extra positional arguments.

        Returns:
            True if invocation succeeded / applied; otherwise False.
        """
        if not method:
            return False
        fn = getattr(muxcon, method, None)
        if not callable(fn):
            return False
        if conn_id is None and "connection" in method:
            return False
        try:
            if conn_id is not None:
                res = fn(conn_id, *args)
            else:
                res = fn(*args)
            if asyncio.iscoroutine(res):
                res = await res
            return bool(res is None or res is True)
        except Exception:  # justification: fault invocation best-effort; return False to API
            return False

    # The following are required abstract methods from BaseGenericAdapter
    async def create_port(self, port_name: str, config: Dict[str, Any]) -> Optional[Any]:
        """Stub (adapter does not create ports)."""
        return None

    async def destroy_port(self, port_name: str) -> None:
        """Stub (adapter does not create ports)."""
        return None

    def get_status_info(self) -> Dict[str, Any]:
        """Return status snapshot for banner/logging.

        Mirrors TcpServerAdapter shape for consistency.
        """
        info = {
            "type": self.get_adapter_type(),
            "status": "running" if self.is_running else "stopped",
            "endpoint": f"{self.host}:{self.port}",
            "clients": f"{len(self.clients)} connected",
            "details": {
                "adapter_name": self.name,
                "host": self.host,
                "port": self.port,
                "http_api_enabled": self.enable_http_api,
                "cors_enabled": self.cors_enabled,
                "fault_injection_enabled": self.enable_fault_injection,
            },
        }
        return info

    async def _send_json(
        self,
        writer: asyncio.StreamWriter,
        status_code: int,
        payload: Dict[str, Any],
    ) -> None:
        """Serialize payload as JSON and write HTTP response.

        Args:
            writer: Stream writer.
            status_code: HTTP status code.
            payload: JSON-serializable dict.
        """
        body = json.dumps(payload)
        status_text = "OK" if status_code == 200 else "ERROR"
        headers = [
            f"HTTP/1.1 {status_code} {status_text}",
            "Content-Type: application/json",
            f"Content-Length: {len(body)}",
            "Connection: close",
        ]
        if self.cors_enabled:
            headers.append("Access-Control-Allow-Origin: *")
        headers.append("\r\n")
        try:
            writer.write(("\r\n".join(headers)).encode("utf-8"))
            writer.write(body.encode("utf-8"))
            await writer.drain()
        except Exception:  # justification: client may disconnect mid-write; ignore
            pass

    async def _send_http_error(self, writer: asyncio.StreamWriter, status_code: int, message: str) -> None:
        """Send plain-text error response.

        Args:
            writer: Stream writer.
            status_code: HTTP status code to return.
            message: Human-friendly error message.
        """
        body = message + "\n"
        headers = [
            f"HTTP/1.1 {status_code} {message}",
            "Content-Type: text/plain",
            f"Content-Length: {len(body)}",
            "Connection: close",
        ]
        if self.cors_enabled:
            headers.append("Access-Control-Allow-Origin: *")
        headers.append("\r\n")
        try:
            writer.write(("\r\n".join(headers)).encode("utf-8"))
            writer.write(body.encode("utf-8"))
            await writer.drain()
        except Exception:  # justification: client may disconnect mid-write; ignore
            pass

    async def _send_cors_preflight_ok(self, writer: asyncio.StreamWriter, req_headers: Dict[str, str]) -> None:
        """Reply to an HTTP CORS preflight (OPTIONS) request.

        Emits permissive headers when CORS is enabled; otherwise returns 404.

        Args:
            writer: Stream writer to respond on.
            req_headers: Parsed request headers (lower-cased keys).
        """
        if not self.cors_enabled:
            await self._send_http_error(writer, 404, "Not Found")
            return
        allow_headers = req_headers.get("access-control-request-headers", "content-type, authorization")
        headers = [
            "HTTP/1.1 204 No Content",
            "Content-Length: 0",
            "Connection: close",
            "Access-Control-Allow-Origin: *",
            "Access-Control-Allow-Methods: GET, POST, OPTIONS",
            f"Access-Control-Allow-Headers: {allow_headers}",
            "Access-Control-Max-Age: 86400",
            "\r\n",
        ]
        try:
            writer.write("\r\n".join(headers).encode("utf-8"))
            await writer.drain()
        except Exception:
            pass
