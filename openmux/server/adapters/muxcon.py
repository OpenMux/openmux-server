"""Unified MuxCon Adapter.

Overview:
        A high-level federation adapter that both accepts inbound federation
        sessions (listeners) and initiates outbound sessions (dialers) to
        configured peers. Each TCP/TLS connection performs an ASCII protocol
        handshake (HELLO + capability negotiation) with optional upgrade to a
        compact binary framing mode. Once established, connections participate in
        heartbeat monitoring, optional fault injection, and (experimental)
        multipath grouping for resilience and preference-based path selection.

Key Features:
        * Multi-listener support (TLS / non-TLS) plus outbound dialers.
        * HELLO handshake & capability negotiation (ASCII) with optional binary
            protocol upgrade watchdog.
        * Heartbeat REQ/ACK with RTT measurement, miss thresholds, and adaptive
            promotion/demotion of multipath primaries.
        * Experimental multipath: group multiple physical connections for a single
            logical peer (identified by node name / server id / host) and select a
            primary based on staleness + configured preference ("path_pref").
        * Federated port advertisement & dynamic remote proxy creation supporting
            logical stream multiplexing (DATA / STREAM OPEN / CLOSE frames).
        * Fault injection hooks (freeze, drop heartbeats, forced close/reset) to
            exercise failover logic and test path promotion.
        * Graceful reconnect semantics with generation rollover (retire older
            instance_id for same server_id).

Multipath Selection Strategy:
        Connections are bucketed by a derived key (node name > server id > host:port
        for outgoing or host-only for pre-handshake inbound). Each group tracks
        preference, activity timestamps, and a primary connection. Failover loop
        periodically evaluates staleness; optional preemptive promotion swaps in a
        higher-preference fresh path without waiting for failure.

Federated Ports:
        Remote peers may advertise federated ports via a multi-line PORTS:FEDERATED
        payload. Each entry is materialized as a ``_RemotePortProxy`` instance
        which integrates with the local PortManager, enabling local clients to
        open logical streams transparently. Reconnection attempts reuse existing
        proxies to preserve state and notify clients about link restoration or
        loss via injected informational messages.

Binary Protocol Upgrade:
        After the initial ASCII HELLO phase a background watchdog attempts a binary
        upgrade (if both peers declare support). The adapter tracks wire mode per
        connection and falls back cleanly if the upgrade does not complete within
        a timeout window.

Fault Injection:
        Testing utilities allow freezing a connection (suppressing reads and
        marking it stale), dropping heartbeats, force closing, or resetting the
        transport to validate multipath and client resilience logic under adverse
        conditions.

Concurrency Model:
        Fully asynchronous (``asyncio``) with per-connection read loops, periodic
        tasks (heartbeats, failover), and on-demand tasks for dialers and binary
        upgrade watchdogs. Shared mutable state (e.g., connection maps, multipath
        groups) is accessed single-threaded within the event loop; coarse-grained
        try/except guards isolate individual fault scenarios.

Public Entry Points (selected):
        * ``start()`` / ``stop()`` — lifecycle management.
        * ``freeze_connection()`` / ``unfreeze_connection()`` — fault injection.
        * ``force_close_connection()`` / ``force_reset_connection()`` — hard teardown.
        * Connection / port registration occurs internally during handshakes and
            federated advertisement processing.

Internal Helpers:
        Private methods (``_read_frame``, ``_send_protocol_frame``, multipath
        maintenance helpers, routing, remote port registration, etc.) are
        extensively documented inline for maintainability and future extension.

This module intentionally favors explicit logging for observability during
development of the evolving federation + multipath semantics.
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import hashlib
import json
import logging
import os
import secrets
import socket
import ssl
import sys
import time
import contextlib
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from openmux.server.port_utils import safe_get_port

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.x509.oid import NameOID

from ...common.federation_types import (
    ClientType,
    MuxConHandshake,
    ServerInfo,
    ServerType,
)
from ..muxcon_protocol import MuxConProtocolHandler
from .base_adapter import AdapterCapability, BaseGenericAdapter
from .lifecycle import PortLifecycleEvent, PortState


@dataclass
class FederationPeer:
    """Peer configuration for an outbound federation initiator.

    Args:
        host: Peer hostname or IP address to connect to.
        port: Peer listener port number.
        options: Optional feature flags and behavior overrides (e.g., TLS,
            path preferences). Keys are adapter-specific.
    """

    host: str
    port: int
    # Future: share_ports, accept_ports, request_ports, auth
    options: Dict[str, Any] = field(default_factory=dict)


class UnifiedMuxConAdapter(BaseGenericAdapter):  # noqa: Vulture
    """Federation adapter implementing MuxCon protocol (MVP phase).

    * Peer authentication / authorization
    """

    def __init__(self, name: str, config: Dict[str, Any]):
        # Support both direct dict and wrapped under top-level 'muxcon'
        effective_config = config.get("muxcon", config) if isinstance(config, dict) else config
        super().__init__(name, effective_config)
        self.logger = logging.getLogger(f"openmux.adapter.muxcon.{self.name}")

        # Unified-only listener configuration
        self._servers: List[asyncio.base_events.Server] = []
        listeners_list = effective_config.get("listeners") or []
        self.listeners_conf: List[Dict[str, Any]] = []
        for idx, lst in enumerate(listeners_list):
            if not isinstance(lst, dict):
                self.logger.warning(f"Ignoring non-dict listener entry at index {idx}: {lst}")
                continue
            lst_conf = {
                "enabled": bool(lst.get("enabled", True)),
                "host": lst.get("host", "0.0.0.0"),
                "port": int(lst.get("port", 7822)),
                # Default-safe: enable TLS for listeners unless explicitly disabled
                "use_tls": bool(lst.get("use_tls", True)),
                "ssl_cert": lst.get("ssl_cert"),
                "ssl_key": lst.get("ssl_key"),
                "ssl_ca_cert": lst.get("ssl_ca_cert"),
                "require_client_cert": bool(lst.get("require_client_cert", False)),
                "tls_autogen": bool(lst.get("tls_autogen", True)),
                "tls_dir": lst.get("tls_dir", "~/.openmux/muxcon"),
                "tls_known_peers_path": lst.get("tls_known_peers_path"),
                "path_pref": lst.get("path_pref"),
                "path_group": lst.get("path_group"),
                "tags": lst.get("tags", {}),
                # Listener routing/binding options (optional)
                "interface": lst.get("interface") or lst.get("bind_interface"),
                "fwmark": lst.get("fwmark") or lst.get("routing_mark") or lst.get("so_mark"),
            }
            self.listeners_conf.append(lst_conf)
        # Known peers / TLS dir defaults (use first listener's tls_dir when present)
        if self.listeners_conf:
            primary = self.listeners_conf[0]
            self._tls_dir = os.path.expanduser(primary.get("tls_dir", "~/.openmux/muxcon"))
            kp = primary.get("tls_known_peers_path") or os.path.join(self._tls_dir, "known_peers.yaml")
            self._known_peers_path = os.path.expanduser(kp)
        else:
            self._tls_dir = os.path.expanduser("~/.openmux/muxcon")
            self._known_peers_path = os.path.expanduser(os.path.join(self._tls_dir, "known_peers.yaml"))

        # Initiators configuration
        self.peers: List[FederationPeer] = []
        for p in effective_config.get("initiators", []) or []:
            try:
                self.peers.append(
                    FederationPeer(
                        host=p.get("host", "localhost"),
                        port=int(p.get("port", 7822)),
                        options={k: v for k, v in p.items() if k not in {"host", "port"}},
                    )
                )
            except Exception as e:
                self.logger.warning(f"Invalid initiator config {p}: {e}", exc_info=True)
        # Identity & protocol
        # Canonical identity comes from top-level server.id, with system hostname as fallback.
        # node_name is deprecated and no longer used.
        self.server_id = effective_config.get("server_id", socket.gethostname())
        # Optional human-friendly description (from server.description); populated during start() when ConfigManager is available
        self.server_description = ""
        import uuid as _uuid

        self.instance_id = str(_uuid.uuid4())
        self.proto = MuxConProtocolHandler()
        # Authentication (Ed25519) configuration
        auth_cfg = effective_config.get("auth", {}) if isinstance(effective_config.get("auth"), dict) else effective_config
        # Default-safe: require authentication unless explicitly disabled
        self._auth_required: bool = bool(effective_config.get("auth_required", True))
        # Allow alternate nested shape: { auth: { required: true, public_keys: [...], private_key: path, key_id: "peer1" } }
        if isinstance(auth_cfg, dict) and "auth" in effective_config:
            self._auth_required = bool(auth_cfg.get("required", self._auth_required))
        # Map of muxcon public keys configured under muxcon.public_keys
        self._auth_pubkeys: Dict[str, Ed25519PublicKey] = {}
        # Optional per-key muxcon filters loaded from muxcon.public_keys entries
        self._key_filters: Dict[str, Dict[str, Any]] = {}
        try:
            # New schema (no backward-compat): expect keys under muxcon.public_keys
            pk_list = effective_config.get("public_keys")
            if isinstance(pk_list, list):
                # Load Ed25519 keys and any per-key filter metadata
                for rec in pk_list:
                    try:
                        kid = str(rec.get("key_id")) if rec.get("key_id") is not None else None
                        pks = rec.get("public_key")
                        if not kid or not isinstance(pks, str):
                            continue
                        pub = self._load_ed25519_public_key(pks)
                        if pub:
                            self._auth_pubkeys[kid] = pub
                        # Extract optional per-key filter metadata (either nested under muxcon or flat)
                        mux = rec.get("muxcon") or {}
                        adv = mux.get("advertise_filters") or rec.get("advertise_filters") or {}
                        acc = mux.get("accept_filters") or rec.get("accept_filters") or {}
                        def _norm(d: Dict[str, Any]) -> Dict[str, List[str]]:
                            if not isinstance(d, dict):
                                return {}
                            return {
                                "include": list(d.get("include") or []),
                                "exclude": list(d.get("exclude") or []),
                                "adapter_include": list(d.get("adapter_include") or []),
                                "adapter_exclude": list(d.get("adapter_exclude") or []),
                                "server_include": list(d.get("server_include") or []),
                                "server_exclude": list(d.get("server_exclude") or []),
                            }
                        self._key_filters[str(kid)] = {
                            "advertise_filters": _norm(adv),
                            "accept_filters": _norm(acc),
                        }
                    except Exception:
                        continue
        except Exception:
            # Non-fatal; leave maps empty
            pass
        self._auth_priv: Optional[Ed25519PrivateKey] = None
        self._auth_key_id: Optional[str] = None
        try:
            priv_path = None
            key_id_cfg = None
            if isinstance(auth_cfg, dict):
                priv_path = auth_cfg.get("private_key") or auth_cfg.get("private_key_path")
                key_id_cfg = auth_cfg.get("key_id")
            if not priv_path:
                priv_path = effective_config.get("auth_private_key") or effective_config.get("auth_private_key_path")
            if not key_id_cfg:
                key_id_cfg = effective_config.get("auth_key_id")
            if priv_path:
                self._auth_priv = self._load_ed25519_private_key(os.path.expanduser(str(priv_path)))
            if key_id_cfg:
                self._auth_key_id = str(key_id_cfg)
        except Exception:
            self._auth_priv = None
            self._auth_key_id = None

        # Multipath config
        self.mpath_primary_stale_sec = float(effective_config.get("mpath_primary_stale_sec", 10.0))
        self.mpath_failover_check_sec = float(effective_config.get("mpath_failover_check_sec", 2.0))
        self.mpath_strategy = str(effective_config.get("mpath_strategy", "best_pref")).lower()
        self.mpath_preemptive_promote = bool(effective_config.get("mpath_preemptive_promote", True))
        # Drop completely idle neighbors after no heartbeat/activity for this TTL (seconds). 0/None disables.
        try:
            self.mpath_neighbor_idle_drop_sec = float(effective_config.get("mpath_neighbor_idle_drop_sec", 900.0))
        except Exception:
            self.mpath_neighbor_idle_drop_sec = 900.0
        # Sequences & state maps
        self._next_seq = 1
        self._wire_state: Dict[str, Dict[str, Any]] = {}
        self._mpath_groups: Dict[str, Dict[str, Any]] = {}
        self.connections: Dict[str, Dict[str, Any]] = {}
        # Receive ordering diagnostics keyed per connection.
        # Root cause of earlier noisy gap warnings: a single global transmit
        # sequence space was shared across all connections while diagnostics
        # compared consecutive numbers per connection, so gaps appeared when
        # frames for other connections advanced the global counter. We now use
        # per-connection sequence allocation; diagnostics can safely remain
        # per-connection without false gap noise.
        self._rx_last_seq: Dict[str, int] = {}
        self._rx_order_warned: Dict[str, bool] = {}
        # Active remote stream mapping keyed by peer_key -> stream_id -> proxy
        self._session_map: Dict[str, Dict[int, Any]] = {}
        # Local server-initiated stream mapping keyed by peer_key -> stream_id -> local_port_name
        self._local_session_map: Dict[str, Dict[int, str]] = {}
        # Proxies keyed by peer_key -> port_name -> proxy
        self._peer_proxies: Dict[str, Dict[str, Any]] = {}
        # Convenience mapping for UI: per-connection view of proxies (mirrors peer_proxies
        # for all connections within the same multipath group). This lets status pages
        # index federated ports by connection id without duplicating correlation logic.
        self._conn_proxies: Dict[str, Dict[str, Any]] = {}
        self._hb_state: Dict[str, Dict[str, Any]] = {}
        # Peer-scoped send buffers for retransmission: peer_key -> seq -> (conn_id, stream_id, data, ts)
        self._peer_sendbuf: Dict[str, Dict[int, Tuple[str, int, bytes, float]]] = {}
        # Retransmission counters per peer
        self._peer_retx_count: Dict[str, int] = {}
        # Per-peer payload byte counters
        self._peer_bytes_tx: Dict[str, int] = {}
        self._peer_bytes_rx: Dict[str, int] = {}
        # Retransmission timers (ms)
        try:
            self.retx_initial_ms = int(effective_config.get("retx_initial_ms", 350))
        except Exception:
            self.retx_initial_ms = 350
        try:
            self.retx_max_ms = int(effective_config.get("retx_max_ms", 2000))
        except Exception:
            self.retx_max_ms = 2000
        # Peer-level TX seq allocator and RX reorder state
        self._peer_tx_seq: Dict[str, int] = {}
        # peer_key -> { 'expected': int, 'buffer': Dict[int, Tuple[int, bytes]] }
        self._peer_rx_state: Dict[str, Dict[str, Any]] = {}
        # Tasks/shutdown
        self._tasks: List[asyncio.Task] = []
        self._stop_event = asyncio.Event()
        try:
            self.heartbeat_interval = float(effective_config.get("heartbeat_interval", 30))
        except Exception:  # justification: malformed heartbeat interval; fall back to safe default
            self.heartbeat_interval = 30.0
        try:
            self.shutdown_grace_timeout = float(effective_config.get("shutdown_grace_timeout_sec", 5.0))
        except Exception:  # justification: invalid shutdown grace timeout; default preserves graceful behavior
            self.shutdown_grace_timeout = 5.0
        try:
            self.context_idle_timeout = float(effective_config.get("context_idle_timeout_sec", 60.0))
        except Exception:  # justification: invalid idle timeout; default ensures cleanup still occurs
            self.context_idle_timeout = 60.0
        try:
            self.shutdown_ack_flush_ms = int(effective_config.get("shutdown_ack_flush_ms", 75))
        except Exception:  # justification: non-integer flush ms; default avoids tight loop or long stalls
            self.shutdown_ack_flush_ms = 75
        self._shutdown_state: Dict[str, Dict[str, Any]] = {}
        # Fault injection state (used by WebStatus /api/fault)
        self._fault_state = {}

        # --- Federated port filters (configurable include/exclude globs) ---
        try:
            advf = effective_config.get("advertise_filters", {}) or {}
            accf = effective_config.get("accept_filters", {}) or {}
        except Exception:
            advf, accf = {}, {}
        self._adv_name_inc = list((advf.get("include") or []))
        self._adv_name_exc = list((advf.get("exclude") or []))
        self._adv_adapter_inc = list((advf.get("adapter_include") or []))
        self._adv_adapter_exc = list((advf.get("adapter_exclude") or []))
        self._adv_server_inc = list((advf.get("server_include") or []))
        self._adv_server_exc = list((advf.get("server_exclude") or []))

        self._acc_name_inc = list((accf.get("include") or []))
        self._acc_name_exc = list((accf.get("exclude") or []))
        self._acc_adapter_inc = list((accf.get("adapter_include") or []))
        self._acc_adapter_exc = list((accf.get("adapter_exclude") or []))
        self._acc_server_inc = list((accf.get("server_include") or []))
        self._acc_server_exc = list((accf.get("server_exclude") or []))

        # Federated cache controls
        try:
            self.federated_cache_enabled = bool(effective_config.get("federated_cache_enabled", True))
        except Exception:
            self.federated_cache_enabled = True
        try:
            self.federated_cache_ttl_sec = float(effective_config.get("federated_cache_ttl_sec", 0.0))
        except Exception:
            self.federated_cache_ttl_sec = 0.0
        try:
            cache_path = effective_config.get("federated_cache_path")
            if not cache_path:
                cache_path = os.path.join(self._tls_dir, "federated_cache.json")
            self.federated_cache_path = os.path.expanduser(str(cache_path))
        except Exception:
            self.federated_cache_path = os.path.expanduser(os.path.join(self._tls_dir, "federated_cache.json"))
        # Internal: periodic task for TTL cleanup
        self._cache_cleanup_task: Optional[asyncio.Task] = None

        # Per-key filter metadata and per-connection overrides
        self._key_filters = {}
        self._conn_filters = {}

    # ======== Ed25519 helpers (MuxCon auth) ========
    def _load_ed25519_public_key(self, key_text: str) -> Optional[Ed25519PublicKey]:
        try:
            if not key_text or not isinstance(key_text, str):
                return None
            key_text = key_text.strip()
            if key_text.startswith("ssh-ed25519 "):
                pub = serialization.load_ssh_public_key(key_text.encode("utf-8"))
                if isinstance(pub, Ed25519PublicKey):
                    return pub
                return None
            # Accept base64 or base64:<data>
            if key_text.startswith("base64:"):
                key_text = key_text[len("base64:") :]
            raw = base64.b64decode(key_text)
            if len(raw) != 32:
                return None
            return Ed25519PublicKey.from_public_bytes(raw)
        except Exception:
            return None

    def _load_ed25519_private_key(self, path: str) -> Optional[Ed25519PrivateKey]:
        try:
            if not path:
                return None
            with open(path, "rb") as f:
                data = f.read()
            # PEM formats
            if data.startswith(b"-----BEGIN"):
                try:
                    priv = serialization.load_pem_private_key(data, password=None)
                    if isinstance(priv, Ed25519PrivateKey):
                        return priv
                    # Try OpenSSH private key loader on PEM-labeled but OpenSSH-formatted keys
                    try:
                        priv2 = serialization.load_ssh_private_key(data, password=None)
                        if isinstance(priv2, Ed25519PrivateKey):
                            return priv2
                    except Exception:
                        pass
                    return None
                except Exception:
                    # Try OpenSSH private key format
                    try:
                        priv = serialization.load_ssh_private_key(data, password=None)
                        if isinstance(priv, Ed25519PrivateKey):
                            return priv
                    except Exception:
                        pass
                    return None
            # Raw/base64 seed in file
            try:
                raw = base64.b64decode(data.strip())
                if len(raw) == 32:
                    return Ed25519PrivateKey.from_private_bytes(raw)
            except Exception:
                pass
            return None
        except Exception:
            return None

    def _is_conn_authenticated(self, conn_id: str) -> bool:
        conn = self.connections.get(conn_id)
        if not conn:
            return False
        role = conn.get("role")
        if role == "client":
            # Initiator side: if we present a private key (auth enabled), require that
            # the remote server has authenticated us (auth_ok). Otherwise, when no
            # private key is configured, treat as authenticated to allow plaintext
            # discovery/advertisement where permitted.
            if getattr(self, "_auth_priv", None) is not None or getattr(self, "_auth_key_id", None) is not None:
                return bool(conn.get("auth_ok"))
            return True
        # Server-side (acceptor): honor adapter-level auth requirement
        if not self._auth_required:
            return True
        return bool(conn.get("auth_ok"))

    # ===================== End __init__ =====================

    # BaseGenericAdapter overrides
    def get_capabilities(self) -> Set[AdapterCapability]:
        """Return capability flags implemented by this adapter.

        Returns:
            Set[AdapterCapability]: Implemented capability flags.
        """
        return {
            AdapterCapability.ACCEPTS_CONNECTIONS,
            AdapterCapability.MAKES_CONNECTIONS,
            AdapterCapability.BIDIRECTIONAL_DATA,
            AdapterCapability.FEDERATION_AWARE,
        }

    def get_adapter_type(self) -> str:
        """Return short adapter type identifier.

        Returns:
            str: The adapter type name ("muxcon").
        """
        return "muxcon"

    def get_status_info(self) -> Dict[str, Any]:
        """Return status snapshot for dashboards/logging.

        Includes endpoints, active connection IDs, listener configuration, and
        TLS indicators. Best-effort: exceptions yield minimal fallback.

        Returns:
            Dict[str, Any]: JSON-serializable status mapping.
        """
        try:
            endpoints = [f"{lc.get('host')}:{lc.get('port')}" for lc in self.listeners_conf if lc.get("enabled")]
            endpoint = ",".join(endpoints) if endpoints else "N/A"
            clients = len(self.connections)
            return {
                "type": self.get_adapter_type(),
                "endpoint": endpoint,
                "clients": clients,
                "details": {
                    "adapter_name": self.name,
                    "listener_enabled": any(l.get("enabled") for l in self.listeners_conf),
                    "listeners": self.listeners_conf,
                    "peers_configured": len(self.peers),
                    "listener_tls": any(l.get("use_tls") for l in self.listeners_conf),
                    "active_connections": list(self.connections.keys()),
                },
            }
        except Exception:  # justification: status snapshot is best-effort; errors reduce verbosity rather than raise
            return {
                "type": self.get_adapter_type(),
                "endpoint": "N/A",
                "clients": 0,
            }

    def set_auth_manager(self, auth_manager) -> None:
        """Wire in the global AuthManager (no longer used for muxcon keys).

        The MuxCon adapter now sources its public keys exclusively from the
        muxcon.public_keys section of its configuration. We retain a reference
        to the AuthManager for other potential integrations, but do not import
        any keys from it.
        """
        try:
            self._auth_manager = auth_manager
        except Exception:
            self._auth_manager = None
        # Prefer local muxcon.public_keys; if none configured, import from AuthManager for backward compatibility in tests
        try:
            if (not getattr(self, "_auth_pubkeys", None)) and auth_manager and hasattr(auth_manager, "get_ed25519_pubkeys_for_use"):
                imported = auth_manager.get_ed25519_pubkeys_for_use("muxcon") or {}
                if isinstance(imported, dict) and imported:
                    for kid, pub in imported.items():
                        if kid not in self._auth_pubkeys:
                            self._auth_pubkeys[kid] = pub
                    try:
                        self.logger.info(f"MuxCon adapter imported {len(imported)} public key(s) from AuthManager (compat)")
                    except Exception:
                        pass
            # Import per-key filter metadata similarly when not locally configured
            if (not getattr(self, "_key_filters", None)) and auth_manager and hasattr(auth_manager, "get_public_keys_for_use"):
                records = auth_manager.get_public_keys_for_use("muxcon") or []
                kf: Dict[str, Dict[str, Any]] = {}
                for rec in records:
                    try:
                        kid = str(rec.get("key_id")) if rec.get("key_id") is not None else None
                        if not kid:
                            continue
                        mux = rec.get("muxcon") or {}
                        adv = mux.get("advertise_filters") or rec.get("advertise_filters") or {}
                        acc = mux.get("accept_filters") or rec.get("accept_filters") or {}
                        def _norm(d: Dict[str, Any]) -> Dict[str, List[str]]:
                            if not isinstance(d, dict):
                                return {}
                            return {
                                "include": list(d.get("include") or []),
                                "exclude": list(d.get("exclude") or []),
                                "adapter_include": list(d.get("adapter_include") or []),
                                "adapter_exclude": list(d.get("adapter_exclude") or []),
                                "server_include": list(d.get("server_include") or []),
                                "server_exclude": list(d.get("server_exclude") or []),
                            }
                        kf[kid] = {"advertise_filters": _norm(adv), "accept_filters": _norm(acc)}
                    except Exception:
                        continue
                if kf:
                    self._key_filters = kf
        except Exception:
            pass

    def _apply_per_connection_filters(self, conn_id: str, key_id: Optional[str]) -> None:
        """Apply per-key muxcon filters to a specific connection if available.

        When a connection authenticates (server side) or when we identify
        our own PKID (client side), override this connection's filters with
        those attached to the key. If none are found, fall back to adapter-level.
        """
        try:
            if not key_id:
                return
            meta = self._key_filters.get(str(key_id)) or {}
            adv = meta.get("advertise_filters")
            acc = meta.get("accept_filters")
            if not adv and not acc:
                return
            self._conn_filters[conn_id] = {
                "advertise_filters": adv or {},
                "accept_filters": acc or {},
            }
            self.logger.info(f"Applied per-key muxcon filters for conn={conn_id} key_id={key_id}")
        except Exception:
            pass

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> bool:
        """Validate configuration structure & listener/initiator fields.

        Supports direct dict or wrapped {"muxcon": {...}} formats.

        Args:
            config: Raw configuration mapping.

        Returns:
            bool: True if structurally valid.

        Raises:
            ValueError: If fields are missing/invalid.
        """
        # Support both direct config and wrapped { 'muxcon': { ... } }
        if "listeners" not in config and "initiators" not in config and "muxcon" in config:
            cfg = config.get("muxcon", {}) or {}
        else:
            cfg = config

        # Unified listeners only
        listeners_list = cfg.get("listeners")
        if isinstance(listeners_list, list):
            for i, lst in enumerate(listeners_list):
                if not isinstance(lst, dict):
                    raise ValueError(f"listeners[{i}] must be a dict")
                if not lst.get("enabled", True):
                    continue
                port = lst.get("port", 7822)
                if not isinstance(port, int) or not (1 <= port <= 65535):
                    raise ValueError(f"listeners[{i}].port must be 1-65535")
                use_tls = bool(lst.get("use_tls", False))
                if use_tls:
                    if (not lst.get("ssl_cert") or not lst.get("ssl_key")) and not bool(lst.get("tls_autogen", True)):
                        raise ValueError(f"listeners[{i}] TLS enabled but missing cert/key and tls_autogen disabled")

        # Initiators
        for p in cfg.get("initiators", []) or []:
            if "host" not in p or "port" not in p:
                raise ValueError("Each initiator requires 'host' and 'port'")
            if not isinstance(p["port"], int) or not (1 <= p["port"] <= 65535):
                raise ValueError("initiator.port must be 1-65535")
        return True

    async def start(self) -> bool:
        """Start listeners, initiators, and background loops.

        Returns:
            bool: True if startup completed (even if no listeners active);
            False on fatal error.
        """
        try:
            # If ConfigManager is accessible via main_port_manager, prefer top-level server.id/description
            try:
                cfg_mgr = getattr(self, "main_port_manager", None)
                if cfg_mgr is not None:
                    cfg_mgr = getattr(cfg_mgr, "config_manager", None)
                srv = None
                if cfg_mgr is not None:
                    cfg = getattr(cfg_mgr, "config", None)
                    if not cfg:
                        # lazy load
                        getcm = getattr(cfg_mgr, "load_config", None)
                        if callable(getcm):
                            getcm()
                        cfg = getattr(cfg_mgr, "config", None)
                    if isinstance(cfg, dict):
                        srv = cfg.get("server") or {}
                if isinstance(srv, dict):
                    sid = srv.get("id") or srv.get("server_id")
                    if sid:
                        self.server_id = str(sid)
                    sdesc = srv.get("description") or srv.get("name")
                    if sdesc:
                        self.server_description = str(sdesc)
            except Exception:
                pass
            self._stop_event.clear()

            # Start listeners (multi or single)
            started_any = False
            for idx, lconf in enumerate(self.listeners_conf):
                if not lconf.get("enabled"):
                    continue
                host = lconf.get("host", "0.0.0.0")
                port = int(lconf.get("port", 7822))
                use_tls = bool(lconf.get("use_tls", False))
                ssl_cert = lconf.get("ssl_cert")
                ssl_key = lconf.get("ssl_key")
                # Autogenerate if requested and missing
                tls_autogen = bool(lconf.get("tls_autogen", True))
                if use_tls and tls_autogen and (not ssl_cert or not ssl_key):
                    try:
                        cert_path, key_path = await self._ensure_autogen_cert(lconf)
                        lconf["ssl_cert"], lconf["ssl_key"] = cert_path, key_path
                        ssl_cert, ssl_key = cert_path, key_path
                        self.logger.info(f"MuxCon listener[{idx}] TLS autogen cert ready at {cert_path}")
                    except Exception as e:
                        self.logger.error(f"TLS autogen failed for listener[{idx}] {host}:{port}: {e}", exc_info=True)
                        use_tls = False
                        lconf["use_tls"] = False
                server_ssl_ctx = None
                if use_tls:
                    server_ssl_ctx = await self._create_server_ssl_context(lconf)
                # Optional interface/fwmark binding for listeners
                l_iface = lconf.get("interface")
                l_fwmark = None
                try:
                    fm_val = lconf.get("fwmark")
                    if fm_val is not None:
                        l_fwmark = int(fm_val)
                except Exception:
                    l_fwmark = None

                if l_iface or l_fwmark is not None:
                    try:
                        lsock = self._make_listen_socket(host, port, interface=l_iface, fwmark=l_fwmark)
                        server = await asyncio.start_server(self._accept_client, sock=lsock, ssl=server_ssl_ctx)
                    except Exception as e:
                        self.logger.error(
                            f"Failed to bind listener {host}:{port} with interface/fwmark (iface={l_iface}, mark={l_fwmark}): {e}",
                            exc_info=True,
                        )
                        raise
                else:
                    server = await asyncio.start_server(self._accept_client, host, port, ssl=server_ssl_ctx)
                await server.start_serving()
                self._servers.append(server)
                started_any = True
                # Compose optional routing/bind suffix
                _suffix_parts = []
                if l_iface:
                    _suffix_parts.append(f"iface={l_iface}")
                if l_fwmark is not None:
                    _suffix_parts.append(f"fwmark={l_fwmark}")
                _suffix = (" " + " ".join(_suffix_parts)) if _suffix_parts else ""
                self.logger.info(
                    f"MuxCon listener[{idx}] started on {host}:{port}{' TLS' if use_tls else ''} path_pref={lconf.get('path_pref')} path_group={lconf.get('path_group')}{_suffix}"
                )
            if not started_any:
                self.logger.warning("No listeners actually started (check configuration)")

            # Start initiators
            for peer in self.peers:
                task = asyncio.create_task(self._initiator_loop(peer))
                self._tasks.append(task)

            # Start heartbeat loop if enabled
            if self.heartbeat_interval and self.heartbeat_interval > 0:
                hb_task = asyncio.create_task(self._heartbeat_loop())
                self._tasks.append(hb_task)
            # Multipath failover loop
            if getattr(self, "mpath_failover_check_sec", 0) > 0:
                mpath_task = asyncio.create_task(self._mpath_failover_loop())
                self._tasks.append(mpath_task)
            # Retransmission loop
            retx_task = asyncio.create_task(self._retx_loop())
            self._tasks.append(retx_task)

            # Start cache cleanup loop if enabled with TTL
            try:
                if self.federated_cache_enabled and self.federated_cache_ttl_sec > 0 and not self._cache_cleanup_task:
                    self._cache_cleanup_task = asyncio.create_task(self._cache_cleanup_loop())
            except Exception:
                pass

            # Load any persisted federated cache
            try:
                if self.federated_cache_enabled:
                    self._load_federated_cache()
            except Exception:
                pass

            self.is_running = True
            return True
        except Exception as e:
            self.logger.error(f"Failed to start Unified MuxCon adapter: {e}", exc_info=True)
            self.is_running = False
            return False

    async def stop(self) -> None:
        """Stop adapter: cancel tasks, close listeners, and active connections."""
        try:
            self._stop_event.set()

            # Cancel background tasks
            for t in self._tasks:
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

            # Close servers
            for srv in self._servers:
                try:
                    srv.close()
                    await srv.wait_closed()
                except Exception:  # justification: TOFU known_peers read optional; proceed with empty mapping
                    pass
            self._servers.clear()

            # Close connections
            for conn_id, conn in list(self.connections.items()):
                writer = conn.get("writer")
                if writer and not writer.is_closing():
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:  # justification: mpath group key registration optional for handshake; connection usable
                        pass
                self.connections.pop(conn_id, None)

            # Stop cache cleanup loop
            try:
                if self._cache_cleanup_task:
                    self._cache_cleanup_task.cancel()
                    with contextlib.suppress(Exception):
                        await self._cache_cleanup_task
                self._cache_cleanup_task = None
            except Exception:
                pass

            self.is_running = False
            self.logger.info("Unified MuxCon adapter stopped")
        except Exception as e:
            self.logger.error(f"Error stopping Unified MuxCon adapter: {e}", exc_info=True)

    async def _heartbeat_loop(self):
        """Send periodic heartbeats and detect timeouts.

        Applies fault injection semantics (freeze/drop), updates multipath
        last-seen metadata, and closes connections after consecutive misses.
        """
        try:
            # Small initial delay to avoid racing with startup
            await asyncio.sleep(0.5)
            while not self._stop_event.is_set():
                try:
                    if not self.connections:
                        await asyncio.sleep(self.heartbeat_interval)
                        continue
                    now_ts = time.time()
                    # Broadcast HB:REQ to all writers and check timeouts
                    for cid, conn in list(self.connections.items()):
                        # Treat frozen connections as fully suppressed (no heartbeats, allow stale aging)
                        if self._fault_state.get(cid, {}).get("frozen"):
                            # Do not update last_seen; let stale logic / failover handle promotion
                            # Optionally increment missed count to accelerate teardown if desired
                            st = self._hb_state.setdefault(
                                cid,
                                {
                                    "last_req_ts": 0.0,
                                    "last_ack_ts": 0.0,
                                    "missed": 0,
                                    "rtt_ms": None,
                                },
                            )
                            # Artificially advance missed if already waiting too long
                            if st.get("last_req_ts") and (now_ts - st["last_req_ts"]) > (self.heartbeat_interval * 2.5):
                                st["missed"] = st.get("missed", 0) + 1
                                if st["missed"] >= 3:
                                    self.logger.warning(f"Heartbeat timeout (frozen) on {cid}; closing connection")
                                    await self._close_connection(cid)
                            continue
                        # Fault: drop heartbeats
                        if self._fault_state.get(cid, {}).get("drop_heartbeats"):
                            # Behave as if heartbeat was missed by not sending; let timeout logic increment misses
                            st = self._hb_state.setdefault(
                                cid,
                                {
                                    "last_req_ts": 0.0,
                                    "last_ack_ts": 0.0,
                                    "missed": 0,
                                    "rtt_ms": None,
                                },
                            )
                            # Artificially advance last_req_ts to drive timeout/miss progression
                            if st.get("last_req_ts") and (now_ts - st["last_req_ts"]) > (self.heartbeat_interval * 2.5):
                                st["missed"] = st.get("missed", 0) + 1
                            continue
                        try:
                            w = conn.get("writer")
                            if not w or w.is_closing():
                                continue
                            # Initialize heartbeat state if missing
                            st = self._hb_state.setdefault(
                                cid,
                                {
                                    "last_req_ts": 0.0,
                                    "last_ack_ts": 0.0,
                                    "missed": 0,
                                    "rtt_ms": None,
                                },
                            )
                            # Send HB:REQ with timestamp
                            st["last_req_ts"] = now_ts
                            hb_seq = self._next_frame_seq(cid)
                            hb_req = self.proto.create_heartbeat_request(now_ts, hb_seq)
                            await self._send_protocol_frame(w, hb_req)
                            # Timeout detection (if previous REQ not acked within 2 intervals)
                            try:
                                if st["last_ack_ts"] < st["last_req_ts"] and (now_ts - st["last_req_ts"]) > (
                                    self.heartbeat_interval * 2.5
                                ):
                                    st["missed"] += 1
                                    # If too many misses, drop connection
                                    if st["missed"] >= 3:
                                        self.logger.warning(f"Heartbeat timeout on {cid}; closing connection")
                                        await self._close_connection(cid)
                                        continue
                            except Exception:  # justification: heartbeat state update for frozen connection optional
                                pass
                            # Update last_seen on outbound heartbeat (activity)
                            try:
                                self.connections[cid]["last_seen"] = now_ts
                                # Also update multipath group entry last_seen so stale logic sees activity even if only heartbeats occur
                                try:
                                    key = self._derive_peer_key_from_conn_id(cid)
                                    grp = self._mpath_groups.get(key)
                                    if grp and cid in grp.get("conns", {}):
                                        grp["conns"][cid]["last_seen"] = now_ts
                                except Exception:  # justification: mpath group last_seen refresh best-effort
                                    pass
                            except Exception:  # justification: hb miss escalation optional; ignore
                                pass
                        except Exception as e:
                            self.logger.debug(f"Heartbeat send failed for {cid}: {e}", exc_info=True)
                    # After processing all connections, recompute proxy live-state for peers
                    try:
                        self._update_peer_proxies_live_state()
                    except Exception:
                        pass
                    await asyncio.sleep(self.heartbeat_interval)
                except asyncio.CancelledError:
                    break
                except Exception:  # justification: failover loop scheduling optional; adapter still functions
                    await asyncio.sleep(self.heartbeat_interval)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.debug(f"Heartbeat loop exited with error: {e}", exc_info=True)

    async def _create_server_ssl_context(self, lconf: Dict[str, Any]) -> Optional[ssl.SSLContext]:
        """Build listener TLS context if configured.

        Returns:
            Optional[ssl.SSLContext]: SSL context if TLS enabled; otherwise None.
        """
        try:
            if not bool(lconf.get("use_tls", False)):
                return None
            ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            cert = lconf.get("ssl_cert")
            key = lconf.get("ssl_key")
            ca = lconf.get("ssl_ca_cert")
            if cert and key:
                ctx.load_cert_chain(cert, key)
            if ca:
                try:
                    ctx.load_verify_locations(cafile=ca)
                except Exception:  # justification: connection close writer wait best-effort during shutdown
                    pass
            if bool(lconf.get("require_client_cert", False)):
                ctx.verify_mode = ssl.CERT_REQUIRED
            else:
                ctx.verify_mode = ssl.CERT_OPTIONAL
            ctx.check_hostname = False
            return ctx
        except Exception as e:
            self.logger.error(f"Failed to create server SSL context: {e}", exc_info=True)
            return None

    async def _create_client_ssl_context(self, peer: FederationPeer) -> Optional[ssl.SSLContext]:
        """Build client TLS context for an outbound peer.

        Args:
            peer: Peer configuration.

        Returns:
            Optional[ssl.SSLContext]: SSL context if TLS enabled; otherwise None.
        """
        try:
            opts = peer.options or {}
            verify = opts.get("ssl_verify", True)
            ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            if opts.get("ssl_ca_cert"):
                try:
                    ctx.load_verify_locations(cafile=opts.get("ssl_ca_cert"))
                except Exception as e:
                    self.logger.warning(f"Failed to load CA cert for {peer.host}:{peer.port}: {e}", exc_info=True)
            cert_file = opts.get("ssl_cert")
            key_file = opts.get("ssl_key")
            if cert_file and key_file:
                try:
                    ctx.load_cert_chain(str(cert_file), str(key_file))
                except Exception as e:
                    self.logger.warning(f"Failed to load client cert/key for {peer.host}:{peer.port}: {e}", exc_info=True)
            if not verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            return ctx
        except Exception as e:
            self.logger.error(f"Failed to create client SSL context: {e}", exc_info=True)
            return None

    # --- TLS autogen & TOFU helpers ---
    async def _ensure_autogen_cert(self, lconf: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
        """Generate (or reuse) a self-signed certificate and key.

        Returns:
            Tuple[str, str]: Paths to the certificate and key files.

        Notes:
            Files are stored under the configured TLS directory. Existing files
            are reused when present.
        """
        tls_dir = os.path.expanduser((lconf or {}).get("tls_dir", self._tls_dir))
        os.makedirs(tls_dir, exist_ok=True)
        cert_path = os.path.join(tls_dir, "server.crt")
        key_path = os.path.join(tls_dir, "server.key")
        if os.path.exists(cert_path) and os.path.exists(key_path):
            return cert_path, key_path
        # Generate EC key and self-signed cert
        key = ec.generate_private_key(ec.SECP256R1(), backend=default_backend())
        subject = issuer = x509.Name(
            [
                # Use server_id as the certificate CN to align identity semantics
                x509.NameAttribute(NameOID.COMMON_NAME, (self.server_id or socket.gethostname())),
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
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=3650))
            .sign(
                private_key=key,
                algorithm=hashes.SHA256(),
                backend=default_backend(),
            )
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

    def _compute_fingerprint(self, der_bytes: bytes) -> str:
        """Compute SHA-256 fingerprint for certificate bytes.

        Args:
            der_bytes: Certificate in DER encoding.

        Returns:
            str: "sha256:<hex>" fingerprint string.
        """
        return f"sha256:{hashlib.sha256(der_bytes).hexdigest()}"

    def _load_known_peers(self) -> Dict[str, str]:
        """Load known peer fingerprints (TOFU) mapping from disk.

        Returns:
            Dict[str, str]: Mapping of "host:port" to fingerprint.
        """
        try:
            if os.path.exists(self._known_peers_path):
                path = self._known_peers_path
                ext = os.path.splitext(path)[1].lower()
                # Read file content once to allow multiple parse attempts
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                # Prefer YAML for .yaml/.yml
                if ext in (".yaml", ".yml"):
                    try:
                        import yaml  # type: ignore

                        data = yaml.safe_load(content) or {}
                        if isinstance(data, dict):
                            return {str(k): str(v) for k, v in data.items()}
                    except Exception:
                        # Fallback: content might be JSON-in-YAML; try JSON
                        try:
                            return json.loads(content)
                        except Exception:
                            return {}
                # JSON or unknown extension: try JSON, then YAML
                try:
                    return json.loads(content)
                except Exception:
                    try:
                        import yaml  # type: ignore

                        data = yaml.safe_load(content) or {}
                        if isinstance(data, dict):
                            return {str(k): str(v) for k, v in data.items()}
                    except Exception:
                        return {}
            # Backward-compat: if YAML file not present, try legacy JSON filename
            try:
                if self._known_peers_path.endswith(".yaml"):
                    legacy_json = self._known_peers_path[:-5] + ".json"
                    if os.path.exists(legacy_json):
                        with open(legacy_json, "r", encoding="utf-8") as f:
                            return json.load(f)
            except Exception:
                pass
        except Exception:  # justification: freeze bookkeeping best-effort; stale aging still proceeds
            pass
        return {}

    def _save_known_peers(self, mapping: Dict[str, str]) -> None:
        """Persist known peer fingerprints (TOFU) mapping to disk.

        Args:
            mapping: Map of "host:port" to fingerprint.
        """
        try:
            os.makedirs(os.path.dirname(self._known_peers_path), exist_ok=True)
            path = self._known_peers_path
            ext = os.path.splitext(path)[1].lower()
            if ext in (".yaml", ".yml"):
                try:
                    import yaml  # type: ignore

                    with open(path, "w", encoding="utf-8") as f:
                        yaml.safe_dump(mapping, f, sort_keys=True, default_flow_style=False)
                except Exception:
                    # Fallback: write JSON content into .yaml file if YAML unavailable
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(mapping, f, indent=2)
            else:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(mapping, f, indent=2)
        except Exception as e:
            self.logger.warning(f"Failed to save known_peers: {e}", exc_info=True)

    async def _verify_peer_fingerprint(self, peer: FederationPeer, writer: asyncio.StreamWriter) -> None:
        """Verify peer certificate via pin or TOFU policy.

        Policies:
            - tls_pin_fingerprint -> require exact fingerprint.
            - tls_tofu (default True) -> record on first sight, then enforce.

        Args:
            peer: Peer configuration containing TLS policy options.
            writer: Stream writer with active TLS transport.
        """
        sslobj = writer.get_extra_info("ssl_object")
        if not sslobj:
            return
        der = sslobj.getpeercert(True)
        if not der:
            return
        fp = self._compute_fingerprint(der)
        opts = peer.options or {}
        pin = opts.get("tls_pin_fingerprint")
        if pin and fp.lower() != str(pin).lower():
            raise ValueError(f"fingerprint mismatch (got {fp}, expected {pin})")
        if pin:
            return
        if not opts.get("tls_tofu", True):
            return
        key = f"{peer.host}:{peer.port}"
        mapping = self._load_known_peers()
        if key in mapping:
            if mapping[key].lower() != fp.lower():
                raise ValueError(f"TOFU fingerprint changed for {key}: {mapping[key]} -> {fp}")
            return
        mapping[key] = fp
        self._save_known_peers(mapping)
        self.logger.info(f"TOFU stored fingerprint for {key}: {fp}")

    # Dynamic ports (not used in MVP yet)
    async def create_port(self, port_name: str, config: Dict[str, Any]) -> Optional[Any]:
        """Create a federated port (not implemented).

        Args:
            port_name: Local logical port name.
            config: Per-port configuration mapping.

        Returns:
            None: Not implemented in the MVP phase.
        """
        self.logger.debug(f"create_port called for {port_name} (noop in MVP)")
        return None

    async def destroy_port(self, port_name: str) -> None:
        """Destroy a federated port (not implemented)."""
        self.logger.debug(f"destroy_port called for {port_name} (noop in MVP)")

    def get_port_configurations(self) -> Dict[str, Dict[str, Any]]:
        """Return configured port mappings.

        Returns:
            Dict[str, Dict[str, Any]]: Always empty; federated ports are
            discovered dynamically from peers.
        """
        return {}

    # Connection handling
    async def _accept_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Accept inbound connection and perform server-side handshake.

        Schedules the per-connection reader loop after successful registration.

        Args:
            reader: Stream reader for the accepted socket.
            writer: Stream writer for the accepted socket.
        """
        peer = writer.get_extra_info("peername")
        sockname = writer.get_extra_info("sockname")
        path_pref = None
        path_group = None
        if sockname:
            lhost, lport = sockname[0], sockname[1]
            try:
                for lconf in self.listeners_conf:
                    if not lconf.get("enabled"):
                        continue
                    lport_conf = lconf.get("port")
                    if lport_conf is None:
                        continue
                    if lconf.get("host") in ("0.0.0.0", "::", lhost) and int(lport_conf) == int(lport):
                        path_pref = lconf.get("path_pref")
                        path_group = lconf.get("path_group")
                        break
            except Exception:  # justification: listener metadata mapping best-effort; proceed without path tags
                pass
        conn_id = f"in:{peer[0]}:{peer[1]}:{int(time.time())}" if peer else f"in:unknown:{int(time.time())}"
        self.logger.info(
            f"Incoming MuxCon connection from {peer} -> {conn_id} local={sockname} path_pref={path_pref} path_group={path_group}"
        )
        try:
            await self._perform_server_handshake(reader, writer, conn_id)
            # Annotate connection with listener path metadata if available
            try:
                if conn_id in self.connections:
                    self.connections[conn_id]["listener_path_pref"] = path_pref
                    self.connections[conn_id]["listener_path_group"] = path_group
                    # Propagate inbound listener path_pref into mpath group if already registered
                    try:
                        if path_pref is not None:
                            key = self._derive_peer_key_from_conn_id(conn_id)
                            grp = self._mpath_groups.get(key)
                            if grp and conn_id in grp.get("conns", {}):
                                grp["conns"][conn_id]["pref"] = int(path_pref)
                                # Optional immediate preemptive promotion if better than current
                                if self.mpath_preemptive_promote and grp.get("primary") and grp.get("primary") != conn_id:
                                    cur_meta = grp["conns"].get(grp["primary"]) or {}
                                    new_pref = int(path_pref)
                                    cur_pref = int(cur_meta.get("pref", 0))
                                    if new_pref > cur_pref:
                                        grp["primary"] = conn_id
                                        self.logger.info(
                                            f"[MPATH] Inbound path_pref promoted {conn_id} over {cur_meta and grp.get('primary')} for {key}"
                                        )
                    except Exception:
                        pass
            except Exception:  # justification: annotate listener path metadata best-effort; non-critical
                pass
            # After handshake, if authenticated or auth not required, request remote ports
            try:
                if self._is_conn_authenticated(conn_id):
                    await self._request_remote_ports(conn_id)
            except Exception as e:
                self.logger.warning(f"Failed to request remote ports from inbound peer {conn_id}: {e}", exc_info=True)
            # Start reader loop
            task = asyncio.create_task(self._read_loop(conn_id))
            self._tasks.append(task)
        except Exception as e:
            self.logger.error(f"Handshake failed for {conn_id}: {e}", exc_info=True)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # justification: writer cleanup on handshake failure is best-effort
                pass

    async def _initiator_loop(self, peer: FederationPeer):
        """Maintain outbound connection with exponential backoff reconnect.

        Args:
            peer: Peer configuration to dial and monitor.
        """
        # Backoff parameters (per-peer configurable)
        try:
            base_backoff = float(peer.options.get("retry_backoff_initial", 2.0))
        except Exception:
            base_backoff = 2.0
        try:
            max_backoff = float(peer.options.get("retry_backoff_max", 30.0))
        except Exception:
            max_backoff = 30.0
        try:
            short_session_sec = float(peer.options.get("retry_short_session_sec", 5.0))
        except Exception:
            short_session_sec = 5.0
        backoff = base_backoff
        while not self._stop_event.is_set():
            try:
                self.logger.info(f"Connecting to MuxCon peer {peer.host}:{peer.port}")
                # Build client SSL context if requested
                # Default-safe: enable TLS for initiators unless explicitly disabled
                use_tls = bool(peer.options.get("use_tls", True))
                ssl_ctx = await self._create_client_ssl_context(peer) if use_tls else None
                # If verification is enabled, pass server_hostname for SNI/verification
                server_hostname = None
                try:
                    verify = peer.options.get("ssl_verify", True)
                    if ssl_ctx and verify:
                        server_hostname = peer.options.get("server_hostname", peer.host)
                except Exception:  # justification: derive SNI/hostname fallback; safe default to peer.host
                    server_hostname = peer.host
                # Optional local bind to influence routing via source address
                local_addr = None
                try:
                    bind_host = peer.options.get("bind_host") or peer.options.get("source_ip")
                    if bind_host:
                        bind_port = int(peer.options.get("bind_port", 0))
                        local_addr = (str(bind_host), bind_port)
                except Exception:
                    local_addr = None

                # Platform routing/interface selection
                iface = None
                fwmark = None
                try:
                    iface = peer.options.get("interface") or peer.options.get("bind_interface")
                except Exception:
                    iface = None
                try:
                    mark_opt = peer.options.get("fwmark") or peer.options.get("routing_mark") or peer.options.get("so_mark")
                    if mark_opt is not None:
                        fwmark = int(mark_opt)
                except Exception:
                    fwmark = None

                if iface or fwmark is not None:
                    reader, writer = await self._connect_with_routing_options(
                        peer.host,
                        peer.port,
                        ssl_ctx,
                        server_hostname,
                        local_addr,
                        interface=iface,
                        fwmark=fwmark,
                    )
                else:
                    reader, writer = await asyncio.open_connection(
                        peer.host,
                        peer.port,
                        ssl=ssl_ctx,
                        server_hostname=server_hostname,
                        local_addr=local_addr,
                    )
                # TOFU / Pin verification
                try:
                    await self._verify_peer_fingerprint(peer, writer)
                except Exception as e:
                    self.logger.warning(
                        f"Peer fingerprint verification failed for {peer.host}:{peer.port}: {e}", exc_info=True
                    )
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:  # justification: writer close best-effort after fingerprint failure
                        pass
                    raise
                conn_id = f"out:{peer.host}:{peer.port}:{int(time.time())}"
                session_start = time.time()
                await self._perform_client_handshake(reader, writer, conn_id)
                # Reader loop (blocks until error/EOF)
                await self._read_loop(conn_id)
                session_dur = time.time() - session_start
                # Reset backoff only if the session lived long enough
                if session_dur >= short_session_sec:
                    backoff = base_backoff
                else:
                    # Short-lived session: throttle reconnection attempts
                    self.logger.warning(
                        f"MuxCon session to {peer.host}:{peer.port} ended after {session_dur:.2f}s; throttling reconnect backoff={backoff:.1f}s"
                    )
                    try:
                        await asyncio.sleep(backoff)
                    except asyncio.CancelledError:
                        break
                    backoff = min(backoff * 2, max_backoff)
            except asyncio.CancelledError:
                break
            except ConnectionRefusedError:
                self.logger.warning(f"MuxCon initiator connection refused by {peer.host}:{peer.port}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
            except ConnectionResetError:
                self.logger.warning(f"MuxCon initiator connection reset by peer {peer.host}:{peer.port}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
            except TimeoutError:
                self.logger.warning(f"MuxCon initiator connection timeout to {peer.host}:{peer.port}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
            except OSError as e:
                self.logger.warning(f"MuxCon initiator connection error to {peer.host}:{peer.port}: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
            except Exception as e:  # justification: retryable failures logged at warning; backoff governs retries
                self.logger.warning(f"MuxCon initiator connection error to {peer.host}:{peer.port}: {e}", exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def _connect_with_routing_options(
        self,
        host: str,
        port: int,
        ssl_ctx: Optional[ssl.SSLContext],
        server_hostname: Optional[str],
        local_addr: Optional[Tuple[str, int]],
        *,
        interface: Optional[str] = None,
        fwmark: Optional[int] = None,
    ) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a TCP connection, applying interface/fwmark routing if requested.

        Notes:
        - Linux: uses SO_MARK (0x24) and SO_BINDTODEVICE to influence routing.
        - macOS: uses IP_BOUND_IF (for IPv4) via CMSG or socket option if available.
        - BSDs: similar IP_BOUND_IF. On unsupported platforms, falls back gracefully.
        """
        # Resolve target to determine address family
        infos = await asyncio.get_event_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not infos:
            raise OSError(f"Name resolution failed for {host}:{port}")
        af, socktype, proto, _, sockaddr = infos[0]

        sock = socket.socket(af, socktype, proto)
        try:
            # Optional local bind
            if local_addr:
                try:
                    sock.bind(local_addr)
                except Exception as e:
                    self.logger.warning(f"Local bind failed {local_addr}: {e}")

            # Apply platform-specific options before connect
            try:
                if interface:
                    if sys.platform.startswith("linux"):
                        SO_BINDTODEVICE = 25  # not in Python stdlib constants
                        sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, interface.encode() + b"\0")
                    elif sys.platform == "darwin":
                        # macOS: bind by interface index for IPv4/IPv6
                        try:
                            if_index = socket.if_nametoindex(interface)
                        except Exception:
                            if_index = 0
                        if if_index:
                            try:
                                if af == socket.AF_INET:
                                    IP_BOUND_IF = 25  # <netinet/in.h>
                                    sock.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, if_index)
                                elif af == socket.AF_INET6:
                                    IPV6_BOUND_IF = 125  # <netinet6/in6.h>
                                    sock.setsockopt(socket.IPPROTO_IPV6, IPV6_BOUND_IF, if_index)
                            except Exception:
                                pass
                    else:
                        # Try generic if_nametoindex + IP_BOUND_IF if present
                        try:
                            if_index = socket.if_nametoindex(interface)
                            IP_BOUND_IF = 25
                            sock.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, if_index)
                        except Exception:
                            pass
            except Exception as e:
                self.logger.warning(f"Failed to apply interface binding '{interface}': {e}")

            try:
                if fwmark is not None:
                    if sys.platform.startswith("linux"):
                        SO_MARK = 36  # from linux/include/uapi/linux/sol_socket.h
                        sock.setsockopt(socket.SOL_SOCKET, SO_MARK, fwmark)
                    else:
                        # Not supported on this platform; ignore
                        pass
            except Exception as e:
                self.logger.warning(f"Failed to apply fwmark {fwmark}: {e}")

            sock.setblocking(False)
            await asyncio.get_event_loop().sock_connect(sock, sockaddr)
            reader, writer = await asyncio.open_connection(
                ssl=ssl_ctx,
                server_hostname=server_hostname,
                sock=sock,
            )
            return reader, writer
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            raise

    async def _perform_server_handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        conn_id: str,
    ):
        """Execute server-side HELLO handshake and register connection state.

        Args:
            reader: Peer stream reader.
            writer: Peer stream writer.
            conn_id: Internal connection identifier.
        """
        # Expect HELLO line from client
        line_bytes = await reader.readline()
        line = line_bytes.decode("utf-8", errors="ignore").strip()
        hs: Optional[MuxConHandshake] = self.proto.parse_handshake(line)
        if not hs:
            raise ValueError("Invalid HELLO line")
        caps = self.proto.validate_capabilities(hs.capabilities)
        response = self.proto.create_handshake_response(caps, server_id=self.server_id, instance_id=self.instance_id)
        writer.write((response + "\n").encode("utf-8"))
        await writer.drain()
        self.connections[conn_id] = {
            "reader": reader,
            "writer": writer,
            "role": "server",
            "handshake": hs,
            "opened_at": time.time(),
            "last_seen": time.time(),
            "server_id": hs.server_id,
            "instance_id": hs.instance_id,
            "auth_ok": (not self._auth_required),
            "ports_advertised": False,
        }
        self._wire_state[conn_id] = {
            "mode": "ascii",
            "recv_next": 1,
            "gaps": set(),
            "unacked": {},
            "last_ack": 0,
            "send_next": 1,
        }
        self.logger.info(
            f"Handshake OK (server-side) conn={conn_id} remote_server_id={hs.server_id} remote_instance_id={hs.instance_id} local_server_id={self.server_id} local_instance_id={self.instance_id} caps={caps}"
        )
        # Ed25519 authentication challenge if required
        try:
            if self._auth_required:
                # Expect pk_id from client and have matching pubkey configured
                pkid = getattr(hs, "pk_id", None)
                if not pkid or pkid not in self._auth_pubkeys:
                    # Inform and close
                    seq = self._next_frame_seq(conn_id)
                    frame = self.proto.create_control_frame(0, seq, "AUTH:ERROR:missing_or_unknown_pkid")
                    await self._send_protocol_frame(writer, frame)
                    await self._close_connection(conn_id)
                    return
                # Issue challenge
                nonce = secrets.token_bytes(32)
                nonce_b64 = base64.b64encode(nonce).decode()
                # Store pending challenge in connection
                self.connections[conn_id]["auth_state"] = {
                    "type": "pk",
                    "key_id": pkid,
                    "nonce": nonce,
                    "expires_at": time.time() + 30,
                }
                seq = self._next_frame_seq(conn_id)
                frame = self.proto.create_control_frame(0, seq, f"AUTH:PK:CHALLENGE:{pkid}:{nonce_b64}")
                await self._send_protocol_frame(writer, frame)
        except Exception:
            pass
        try:
            self._register_mpath_connection(conn_id)
        except Exception:  # justification: mpath connection register best-effort; non-critical
            pass
        try:
            self._rekey_mpath_connection(conn_id)
        except Exception:  # justification: mpath rekey best-effort; failure does not compromise session
            pass
        try:
            self._retire_old_generation(conn_id)
        except Exception:  # justification: old generation retirement optional cleanup
            pass
        # If authentication is not required, advertise local ports immediately
        try:
            await self._maybe_advertise_local_ports(conn_id)
        except Exception:
            pass
        # Refresh per-connection proxy mapping once connection metadata/grouping is established
        try:
            self._refresh_conn_proxies()
        except Exception:
            pass

    def _make_listen_socket(
        self,
        host: str,
        port: int,
        *,
        interface: Optional[str] = None,
        fwmark: Optional[int] = None,
    ) -> socket.socket:
        """Create and bind a listening socket with optional interface/fwmark.

        On Linux, applies SO_BINDTODEVICE and SO_MARK.
        On macOS/BSD, binds by interface index via IP_BOUND_IF/IPV6_BOUND_IF when possible.
        """
        # Resolve to pick family; prefer exact family of host literal if given
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not infos:
            raise OSError(f"Name resolution failed for listen {host}:{port}")
        af, socktype, proto, _, sockaddr = infos[0]
        s = socket.socket(af, socktype, proto)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Apply interface binding before bind
            if interface:
                try:
                    if sys.platform.startswith("linux"):
                        SO_BINDTODEVICE = 25
                        s.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, interface.encode() + b"\0")
                    elif sys.platform == "darwin":
                        try:
                            if_index = socket.if_nametoindex(interface)
                        except Exception:
                            if_index = 0
                        if if_index:
                            try:
                                if af == socket.AF_INET:
                                    IP_BOUND_IF = 25
                                    s.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, if_index)
                                elif af == socket.AF_INET6:
                                    IPV6_BOUND_IF = 125
                                    s.setsockopt(socket.IPPROTO_IPV6, IPV6_BOUND_IF, if_index)
                            except Exception:
                                pass
                    else:
                        try:
                            if_index = socket.if_nametoindex(interface)
                            if if_index:
                                IP_BOUND_IF = 25
                                s.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, if_index)
                        except Exception:
                            pass
                except Exception as e:
                    self.logger.warning(f"Listener interface bind '{interface}' failed: {e}")

            # Apply routing mark (Linux only)
            if fwmark is not None and sys.platform.startswith("linux"):
                try:
                    SO_MARK = 36
                    s.setsockopt(socket.SOL_SOCKET, SO_MARK, fwmark)
                except Exception as e:
                    self.logger.warning(f"Listener fwmark {fwmark} failed: {e}")

            s.bind(sockaddr)
            s.listen()
            s.setblocking(False)
            return s
        except Exception:
            try:
                s.close()
            except Exception:
                pass
            raise

    async def _perform_client_handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        conn_id: str,
    ):
        """Execute client-side HELLO handshake and register connection state.

        Args:
            reader: Peer stream reader.
            writer: Peer stream writer.
            conn_id: Internal connection identifier.
        """
        # Send HELLO including identity
        # Include PKID if we have a private key configured
        pkid_part = f" PKID={self._auth_key_id}" if self._auth_priv and self._auth_key_id else ""
        hello = (
            f"HELLO MuxCon/1.0 TYPE={ClientType.REGULAR_CLIENT.value} "
            f"ID={self.server_id} INST={self.instance_id}{pkid_part}"
        )
        writer.write((hello + "\n").encode("utf-8"))
        await writer.drain()
        line = (await reader.readline()).decode("utf-8", errors="ignore").strip()
        if not line.startswith("OK "):
            raise ValueError(f"Unexpected handshake response: {line}")
        ok_server_id = None
        ok_instance_id = None
        for part in line.split()[2:]:  # skip OK MuxCon/1.0
            if part.startswith("ID="):
                ok_server_id = part[3:]
            elif part.startswith("INST="):
                ok_instance_id = part[5:]
        self.connections[conn_id] = {
            "reader": reader,
            "writer": writer,
            "role": "client",
            "handshake": {"type": ClientType.REGULAR_CLIENT.value},
            "opened_at": time.time(),
            "last_seen": time.time(),
            "server_id": ok_server_id,
            "instance_id": ok_instance_id,
            "auth_ok": False,
            "ports_advertised": False,
        }
        self._wire_state[conn_id] = {
            "mode": "ascii",
            "recv_next": 1,
            "gaps": set(),
            "unacked": {},
            "last_ack": 0,
            "send_next": 1,
        }
        self.logger.debug(
            f"Handshake OK (client-side) conn={conn_id} remote_server_id={ok_server_id} remote_instance_id={ok_instance_id} local_server_id={self.server_id} local_instance_id={self.instance_id} raw='{line}'"
        )
        try:
            self._register_mpath_connection(conn_id)
        except Exception:  # justification: mpath connection register best-effort; non-critical
            pass
        try:
            self._rekey_mpath_connection(conn_id)
        except Exception:  # justification: mpath rekey best-effort; failure does not compromise session
            pass
        try:
            self._retire_old_generation(conn_id)
        except Exception:  # justification: old generation retirement optional cleanup
            pass
        # Refresh per-connection proxy mapping after handshake establishes grouping
        try:
            self._refresh_conn_proxies()
        except Exception:
            pass
        # If peer doesn't require auth, advertise immediately after handshake
        try:
            await self._maybe_advertise_local_ports(conn_id)
        except Exception:
            pass

    async def _read_loop(self, conn_id: str):
        """Read frames for a connection and dispatch to handlers.

        Args:
            conn_id: Internal connection identifier to service.
        """
        conn = self.connections.get(conn_id)
        if not conn:
            return
        reader: asyncio.StreamReader = conn["reader"]
        writer_obj = conn.get("writer")
        if not isinstance(writer_obj, asyncio.StreamWriter):
            # Writer missing or invalid; close connection context
            try:
                self.logger.warning(f"Connection {conn_id} has no valid writer; closing")
            except Exception:  # justification: logging guard best-effort; ignore logger failure
                pass
            await self._close_connection(conn_id)
            return
        try:
            while not self._stop_event.is_set():
                # Fault: freeze connection (skip reading frames but keep connection open)
                if self._fault_state.get(conn_id, {}).get("frozen"):
                    try:
                        await asyncio.sleep(0.2)
                    except asyncio.CancelledError:
                        break
                    continue
                # Read next ASCII-framed protocol line
                frame = await self._read_frame(reader)
                if not frame:
                    break
                ftype = frame.get("frame_type")
                stream_id = frame.get("stream_id")
                payload = frame.get("payload") or b""
                self.logger.debug(
                    f"[{conn_id}] RX frame type={ftype} sid={stream_id} len={len(payload)} seq={frame.get('seq')}"
                )
                # Ordering diagnostics (lightweight): warn once if gap or reorder detected
                try:
                    rseq = int(frame.get("seq") or 0)
                    last = self._rx_last_seq.get(conn_id)
                    if last is not None:
                        if rseq <= last and not self._rx_order_warned.get(conn_id):
                            self.logger.warning(f"[{conn_id}] Out-of-order frame seq={rseq} last={last}")
                            self._rx_order_warned[conn_id] = True
                        elif rseq > last + 1 and not self._rx_order_warned.get(conn_id):
                            self.logger.warning(f"[{conn_id}] Sequence gap detected seq={rseq} last={last}")
                            self._rx_order_warned[conn_id] = True
                    self._rx_last_seq[conn_id] = rseq
                except Exception:  # justification: RX diagnostics best-effort; do not disrupt processing
                    pass
                if ftype == "C":
                    # Update last_seen on control activity
                    try:
                        if conn_id in self.connections:
                            now_ts = time.time()
                            self.connections[conn_id]["last_seen"] = now_ts
                            key = self._derive_peer_key_from_conn_id(conn_id)
                            grp = self._mpath_groups.get(key)
                            if grp and conn_id in grp["conns"]:
                                grp["conns"][conn_id]["last_seen"] = now_ts
                                grp["conns"][conn_id]["last_rx_seen"] = now_ts
                            # Recompute proxy live-state for this peer after activity
                            try:
                                self._update_peer_proxies_live_state(key)
                            except Exception:
                                pass
                    except Exception:  # justification: last_seen updates are advisory; ignore failures
                        pass
                    text = payload.decode("utf-8", errors="ignore")
                    await self._process_control_command(conn_id, writer_obj, text)
                elif ftype == "D":
                    # Require authentication before processing data frames
                    if not self._is_conn_authenticated(conn_id):
                        try:
                            seq = self._next_frame_seq(conn_id)
                            notice = self.proto.create_control_frame(0, seq, "AUTH:REQUIRED")
                            await self._send_protocol_frame(writer_obj, notice)
                        except Exception:
                            pass
                        continue
                    # Update last_seen on data activity
                    try:
                        if conn_id in self.connections:
                            now_ts = time.time()
                            self.connections[conn_id]["last_seen"] = now_ts
                            key = self._derive_peer_key_from_conn_id(conn_id)
                            grp = self._mpath_groups.get(key)
                            if grp and conn_id in grp["conns"]:
                                grp["conns"][conn_id]["last_seen"] = now_ts
                                grp["conns"][conn_id]["last_rx_seen"] = now_ts
                            # Recompute proxy live-state for this peer after activity
                            try:
                                self._update_peer_proxies_live_state(key)
                            except Exception:
                                pass
                    except Exception:  # justification: last_seen refresh is advisory; ignore errors to keep data path hot
                        pass
                    # MPH embedding removed: sequence numbers now in frame header universally
                    # Ensure stream_id is an int before routing
                    try:
                        sid = int(stream_id) if stream_id is not None else 0
                    except Exception:  # justification: invalid stream id; default to 0 for safe routing
                        sid = 0
                    # ACK immediately, then deliver in order per peer
                    await self._handle_inbound_data(conn_id, sid, payload, int(frame.get("seq") or 0))
                    # Send data ACK (peer-level) acknowledging this seq
                    try:
                        ack_seq = self._next_frame_seq(conn_id)
                        ack = self.proto.create_ack_frame(int(frame.get("seq") or 0), ack_seq)
                        await self._send_protocol_frame(writer_obj, ack)
                    except Exception:
                        pass
                elif ftype == "O":
                    # Require authentication before accepting stream opens
                    if not self._is_conn_authenticated(conn_id):
                        try:
                            seq = self._next_frame_seq(conn_id)
                            notice = self.proto.create_control_frame(0, seq, "AUTH:REQUIRED")
                            await self._send_protocol_frame(writer_obj, notice)
                        except Exception:
                            pass
                        continue
                    # Update last_seen on open stream
                    try:
                        if conn_id in self.connections:
                            now_ts = time.time()
                            self.connections[conn_id]["last_seen"] = now_ts
                            key = self._derive_peer_key_from_conn_id(conn_id)
                            grp = self._mpath_groups.get(key)
                            if grp and conn_id in grp["conns"]:
                                grp["conns"][conn_id]["last_seen"] = now_ts
                                grp["conns"][conn_id]["last_rx_seen"] = now_ts
                            try:
                                self._update_peer_proxies_live_state(key)
                            except Exception:
                                pass
                    except Exception:  # justification: best-effort telemetry update; do not perturb stream open
                        pass
                    # Remote requested opening a stream to a local port name
                    try:
                        port_name = payload.decode("utf-8", errors="ignore").strip()
                    except Exception:  # justification: decode failure; treat as empty port name and continue
                        port_name = ""
                    self.logger.info(f"[{conn_id}] OPEN stream {stream_id} -> {port_name}")
                    # Map server-initiated stream to local port (peer-scoped) and start pump back to remote
                    peer_key = self._derive_peer_key_from_conn_id(conn_id)
                    if peer_key not in self._local_session_map:
                        self._local_session_map[peer_key] = {}
                    try:
                        sid = int(stream_id) if stream_id is not None else 0
                    except Exception:  # justification: invalid stream id on OPEN; default to 0
                        sid = 0
                    self._local_session_map[peer_key][sid] = port_name
                    # Start background pump to send local port data back to remote stream
                    if port_name:
                        task = asyncio.create_task(self._pump_local_port_to_remote(peer_key, sid, port_name))
                        self._tasks.append(task)
                elif ftype == "E":
                    # Require authentication before accepting stream closes
                    if not self._is_conn_authenticated(conn_id):
                        try:
                            seq = self._next_frame_seq(conn_id)
                            notice = self.proto.create_control_frame(0, seq, "AUTH:REQUIRED")
                            await self._send_protocol_frame(writer_obj, notice)
                        except Exception:
                            pass
                        continue
                    # Update last_seen on close stream
                    try:
                        if conn_id in self.connections:
                            now_ts = time.time()
                            self.connections[conn_id]["last_seen"] = now_ts
                            key = self._derive_peer_key_from_conn_id(conn_id)
                            grp = self._mpath_groups.get(key)
                            if grp and conn_id in grp.get("conns", {}):
                                grp["conns"][conn_id]["last_seen"] = now_ts
                                grp["conns"][conn_id]["last_rx_seen"] = now_ts
                            try:
                                self._update_peer_proxies_live_state(key)
                            except Exception:
                                pass
                    except Exception:  # justification: best-effort telemetry update; safe to ignore
                        pass
                    # Close stream
                    self.logger.info(f"[{conn_id}] CLOSE stream {stream_id}")
                    try:
                        sid = int(stream_id) if stream_id is not None else 0
                    except Exception:  # justification: invalid stream id on CLOSE; default to 0
                        sid = 0
                    try:
                        peer_key = self._derive_peer_key_from_conn_id(conn_id)
                        if peer_key in self._session_map and sid in self._session_map[peer_key]:
                            self._session_map[peer_key].pop(sid, None)
                        if peer_key in self._local_session_map and sid in self._local_session_map[peer_key]:
                            self._local_session_map[peer_key].pop(sid, None)
                    except Exception:  # justification: session cleanup is best-effort during close
                        pass
                elif ftype == "HB":
                    # Heartbeat command channel; treat as control with HB payload
                    self.logger.debug(f"[{conn_id}] HEARTBEAT FRAME")
                    try:
                        if conn_id in self.connections:
                            now_ts = time.time()
                            self.connections[conn_id]["last_seen"] = now_ts
                            # Also refresh multipath group's last_seen so failover logic doesn't misclassify as stale
                            try:
                                key = self._derive_peer_key_from_conn_id(conn_id)
                                grp = self._mpath_groups.get(key)
                                if grp and conn_id in grp.get("conns", {}):
                                    grp["conns"][conn_id]["last_seen"] = now_ts
                                    grp["conns"][conn_id]["last_rx_seen"] = now_ts
                                # Heartbeat seen; update proxies live-state for this peer
                                try:
                                    self._update_peer_proxies_live_state(key)
                                except Exception:
                                    pass
                            except Exception:  # justification: mpath last_seen refresh is advisory
                                pass
                    except Exception:  # justification: best-effort heartbeat bookkeeping; non-critical
                        pass
                    # HB frames have payload like REQ:<ts> or ACK:<ts>
                    text = payload.decode("utf-8", errors="ignore")
                    await self._process_control_command(conn_id, writer_obj, text)
                elif ftype == "A":
                    # Data ACK: payload contains acked DATA seq number
                    try:
                        acked = int(payload.decode("utf-8", errors="ignore").strip() or 0)
                        peer_key = self._derive_peer_key_from_conn_id(conn_id)
                        buf = self._peer_sendbuf.get(peer_key)
                        if buf and acked in buf:
                            buf.pop(acked, None)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.warning(f"Read loop error on {conn_id}: {e}", exc_info=True)
        finally:
            # Cleanup
            await self._close_connection(conn_id)

    def _update_peer_proxies_live_state(self, peer_key: Optional[str] = None) -> None:
        """Update RemotePortProxy.is_connected based on current multipath liveness.

        If a peer group has no non-stale, non-frozen active paths, mark its
        proxies as disconnected (but do not destroy them). When liveness is
        restored, flip proxies back to connected. Emits PortManager meta
        notifications on state changes to drive UI updates promptly.

        Args:
            peer_key: Optional specific peer group key; when omitted, all peers
                      known to the adapter are evaluated.
        """
        try:
            now = time.time()
            # Effective stale window must account for heartbeat cadence
            try:
                # Use a shorter window than the hard timeout so UI reflects loss sooner
                hb_window = self.heartbeat_interval * 1.5
            except Exception:
                hb_window = 60.0
            effective_stale = 0.0
            try:
                effective_stale = float(self.mpath_primary_stale_sec)
            except Exception:
                effective_stale = 45.0
            if hb_window > effective_stale:
                stale_window = hb_window
            else:
                stale_window = effective_stale

            def _has_live_path(pk: str) -> bool:
                grp = self._mpath_groups.get(pk) or {}
                conns = grp.get("conns", {}) or {}
                for cid, meta in conns.items():
                    # Skip frozen connections entirely
                    if self._fault_state.get(cid, {}).get("frozen"):
                        continue
                    # Consider either last_rx_seen / last_seen or last ACK timestamp
                    last_rx = float(meta.get("last_rx_seen") or meta.get("last_seen") or 0)
                    hb = self._hb_state.get(cid) or {}
                    last_ack = float(hb.get("last_ack_ts") or 0)
                    last_activity = max(last_rx, last_ack)
                    if last_activity and (now - last_activity) <= stale_window:
                        return True
                return False

            # Determine which peers to check
            peer_keys = []
            if peer_key:
                peer_keys = [peer_key]
            else:
                try:
                    peer_keys = list((self._mpath_groups or {}).keys())
                except Exception:
                    peer_keys = []
                # Also include any peers that still have proxies but no group entry
                try:
                    for pk in (self._peer_proxies or {}).keys():
                        if pk not in peer_keys:
                            peer_keys.append(pk)
                except Exception:
                    pass

            for pk in peer_keys:
                proxies = (self._peer_proxies or {}).get(pk) or {}
                if not proxies:
                    continue
                live = _has_live_path(pk)
                for pname, proxy in list(proxies.items()):
                    try:
                        current = bool(getattr(proxy, "is_connected", True))
                        if current == live:
                            continue
                        # Flip state without destroying sessions on stale detection
                        try:
                            proxy.is_connected = live
                        except Exception:
                            pass
                        # When transitioning to disconnected, best-effort notify via data queue
                        if not live:
                            try:
                                src_server = None
                                meta = getattr(proxy, "metadata", None)
                                if meta is not None:
                                    origin = getattr(meta, "origin_server", None)
                                    src_server = getattr(origin, "server_id", None)
                                src_server = src_server or "unknown"
                                src_path = f"{src_server}::{pname}"
                                self._emit_link_notice_once(proxy, "stale", src_path)
                            except Exception:
                                pass
                        # Notify PortManager/meta listeners
                        try:
                            pm = getattr(self, "main_port_manager", None)
                            if pm and hasattr(pm, "notify_meta_updated"):
                                payload = {"event": "federated_live_state", "peer_key": pk, "connected": live}
                                try:
                                    if hasattr(proxy, "last_seen"):
                                        payload["last_seen"] = float(getattr(proxy, "last_seen"))
                                except Exception:
                                    pass
                                pm.notify_meta_updated(pname, payload)
                        except Exception:
                            pass
                    except Exception:
                        continue
        except Exception:
            # Non-fatal best-effort update
            pass

    # ================= Fault Injection Methods =================

    async def freeze_connection(self, conn_id: str) -> bool:  # pragma: no cover  # noqa: D401  # vulture: ignore
        """Prevent further reads from the connection without closing it."""
        if conn_id not in self.connections:
            return False
        st = self._fault_state.setdefault(conn_id, {})
        st["frozen"] = True
        # Age last_seen far into past so failover loop sees it as stale quickly
        try:
            meta_key = self._derive_peer_key_from_conn_id(conn_id)
            grp = self._mpath_groups.get(meta_key)
            if grp and conn_id in grp.get("conns", {}):
                grp["conns"][conn_id]["last_seen"] = 0  # force stale
            if conn_id in self.connections:
                self.connections[conn_id]["last_seen"] = 0
        except Exception:  # justification: heartbeat miss acceleration optional; safe to ignore
            pass
        # Clear any recent hb req to accelerate miss counting
        try:
            hb = self._hb_state.get(conn_id)
            if hb:
                hb["last_req_ts"] = hb.get("last_req_ts", 0) - (self.heartbeat_interval * 5)
        except Exception:  # justification: fallback to less stable key if derivation fails
            pass
        self.logger.warning(f"FaultInject: froze connection {conn_id}")
        return True

    async def unfreeze_connection(self, conn_id: str) -> bool:  # vulture: ignore
        """Resume reads on a previously frozen connection.

        Args:
            conn_id: Connection identifier to unfreeze.

        Returns:
            True if the operation completed (or the connection did not exist).
        """
        if conn_id not in self.connections:
            return False
        st = self._fault_state.setdefault(conn_id, {})
        if st.get("frozen"):
            st["frozen"] = False
            # Refresh last_seen so it is immediately eligible again
            try:
                now_ts = time.time()
                self.connections[conn_id]["last_seen"] = now_ts
                meta_key = self._derive_peer_key_from_conn_id(conn_id)
                grp = self._mpath_groups.get(meta_key)
                if grp and conn_id in grp.get("conns", {}):
                    grp["conns"][conn_id]["last_seen"] = now_ts
            except Exception:  # justification: unfreeze last_seen refresh best-effort; promotion logic still works
                pass
            self.logger.info(f"FaultInject: unfroze connection {conn_id}")
        return True

    async def set_drop_heartbeats(self, conn_id: str, drop: bool) -> bool:  # vulture: ignore
        """Enable/disable synthetic dropping of heartbeats for a connection.

        Args:
            conn_id: Target connection identifier.
            drop: True to drop heartbeats; False to restore normal behavior.

        Returns:
            True if the flag was set; False if the connection does not exist.
        """
        if conn_id not in self.connections:
            return False
        st = self._fault_state.setdefault(conn_id, {})
        st["drop_heartbeats"] = bool(drop)
        self.logger.warning(f"FaultInject: {'dropping' if drop else 'restoring'} heartbeats for {conn_id}")
        return True

    async def force_close_connection(self, conn_id: str, linger: int = 0) -> bool:  # vulture: ignore
        """Force-close a connection after an optional short linger.

        Args:
            conn_id: Connection identifier to close.
            linger: Optional seconds to wait before closing (max 5 seconds).

        Returns:
            True on success, False if an error occurred or connection missing.
        """
        if conn_id not in self.connections:
            return False
        try:
            if linger > 0:
                self.logger.info(f"FaultInject: closing connection {conn_id} after linger={linger}s")
                await asyncio.sleep(min(linger, 5))  # cap linger to 5s
            await self._close_connection(conn_id)
            return True
        except Exception as e:
            self.logger.error(f"FaultInject: force_close_connection error for {conn_id}: {e}", exc_info=True)
            return False

    async def force_reset_connection(self, conn_id: str) -> bool:  # vulture: ignore
        """Abruptly reset a connection by aborting its transport.

        Args:
            conn_id: Connection identifier to reset.

        Returns:
            True on success; False if reset failed or connection missing.
        """
        if conn_id not in self.connections:
            return False
        try:
            conn = self.connections.get(conn_id)
            writer = conn.get("writer") if conn else None
            if writer and hasattr(writer, "transport"):
                try:
                    transport = writer.transport  # type: ignore[attr-defined]
                    # Abort the transport if available
                    if hasattr(transport, "abort"):
                        transport.abort()
                except Exception:  # justification: abort is best-effort; transports may not support or be closed
                    pass
            # Ensure internal cleanup
            await self._close_connection(conn_id)
            self.logger.warning(f"FaultInject: reset connection {conn_id}")
            return True
        except Exception as e:
            self.logger.error(f"FaultInject: force_reset_connection error for {conn_id}: {e}", exc_info=True)
            return False

    async def _close_connection(self, conn_id: str):
        """Close a connection and cleanup all associated in-memory state.

        Best-effort orderly shutdown that:
        * Removes the connection from internal registries and multipath groups.
        * Closes the underlying transport (suppressing errors).
        * Marks federated port proxies linked to this connection as disconnected
            and injects a notification message into their queues so any clients
            see link loss.
        * Clears heartbeat / wire tracking structures.

        Args:
                conn_id: Internal connection identifier to close.
        """
        # Derive stable peer identity BEFORE tearing down connection record so
        # we can reliably look up proxies and unregister federated ports.
        try:
            stable_peer_key = self._derive_peer_key_from_conn_id(conn_id)
        except Exception:
            stable_peer_key = None
        # Best-effort origin server_id for unregister (prefer connection record)
        try:
            _conn_tmp = self.connections.get(conn_id) or {}
            stable_server_id = _conn_tmp.get("server_id")
        except Exception:
            stable_server_id = None

        # Attempt to unregister from multipath groups first using full scan
        try:
            self._unregister_mpath_connection(conn_id)
        except Exception:
            pass
        # Fetch connection record after unregister attempt (may be absent)
        conn = self.connections.pop(conn_id, None)
        if not conn:
            # Even if connection record is missing, clear any wire/hb state and log
            try:
                self._wire_state.pop(conn_id, None)
            except Exception:
                pass
            try:
                self._hb_state.pop(conn_id, None)
            except Exception:
                pass
            self.logger.info(f"Connection closed: {conn_id}")
            return
        # Drop wire state
        try:
            self._wire_state.pop(conn_id, None)
        except Exception:  # justification: best-effort wire-state cleanup
            pass
        writer: Optional[asyncio.StreamWriter] = conn.get("writer")
        if writer and not writer.is_closing():
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # justification: writer may already be closed or broken; ignore
                pass
        self.logger.info(f"Connection closed: {conn_id}")
        # Mark federated proxies for this peer based on current live-path state
        try:
            # Prefer the stable key determined before popping the connection
            peer_key = stable_peer_key or self._derive_peer_key_from_conn_id(conn_id)
            grp = self._mpath_groups.get(peer_key)
            any_paths = bool(grp and grp.get("conns"))
            # If no paths remain, mark proxies offline but retain them in cache
            if not any_paths:
                # Look up proxies by the stable key; if not found and the key was
                # host-based, attempt fallback to node:<server_id> derived earlier.
                proxies = self._peer_proxies.get(peer_key, {})
                if (not proxies) and stable_server_id:
                    try:
                        node_key = f"node:{stable_server_id}"
                        proxies = self._peer_proxies.get(node_key, proxies)
                        # Use node_key for subsequent operations if it exists
                        if proxies:
                            peer_key = node_key
                    except Exception:
                        pass
                # Attempt to determine server_id for unregister
                server_id = None
                try:
                    if peer_key.startswith("node:"):
                        server_id = peer_key.split(":", 1)[1]
                except Exception:
                    server_id = None
                if not server_id and stable_server_id:
                    server_id = stable_server_id
                for pname, proxy in list(proxies.items()):
                    try:
                        if hasattr(proxy, "is_connected"):
                            proxy.is_connected = False
                        if hasattr(proxy, "disconnect") and callable(proxy.disconnect):
                            if asyncio.iscoroutinefunction(proxy.disconnect):
                                await proxy.disconnect()
                            else:
                                proxy.disconnect()
                        try:
                            src_server = None
                            try:
                                meta = getattr(proxy, "metadata", None)
                                origin = getattr(meta, "origin_server", None)
                                src_server = getattr(origin, "server_id", None)
                            except Exception:
                                src_server = None
                            src_server = src_server or "unknown"
                            src_path = f"{src_server}::{pname}"
                            self._emit_link_notice_once(proxy, "disconnected", src_path)
                        except Exception:
                            pass
                        self.logger.info(
                            f"Marked federated port '{pname}' disconnected; no active paths remain for {peer_key}"
                        )
                        # Notify UI/consumers via PortManager meta update (if available)
                        try:
                            pm = getattr(self, "main_port_manager", None)
                            if pm and hasattr(pm, "notify_meta_updated"):
                                pm.notify_meta_updated(pname, {"event": "federated_disconnected", "peer_key": peer_key})
                        except Exception:
                            pass
                    except Exception as e:
                        self.logger.debug(f"Error marking proxy {pname} disconnected for peer {peer_key}: {e}", exc_info=True)
                # Retain proxies in cache; update last_seen and notify meta with last_seen
                try:
                    now_ts = time.time()
                    pm = getattr(self, "main_port_manager", None)
                    for pname, proxy in list(proxies.items()):
                        try:
                            setattr(proxy, "last_seen", getattr(proxy, "last_seen", now_ts))
                        except Exception:
                            pass
                        try:
                            if pm and hasattr(pm, "notify_meta_updated"):
                                pm.notify_meta_updated(pname, {"event": "federated_cached_offline", "peer_key": peer_key, "connected": False, "last_seen": getattr(proxy, "last_seen", None)})
                        except Exception:
                            pass
                except Exception:
                    pass
                # Persist cache after transition to offline
                try:
                    self._save_federated_cache()
                except Exception:
                    pass
            else:
                # Paths remain in group; recompute live-state (may be all stale)
                try:
                    self._update_peer_proxies_live_state(peer_key)
                except Exception:
                    pass
        except Exception:
            pass
        # Cleanup heartbeat state for this connection
        try:
            self._hb_state.pop(conn_id, None)
        except Exception:  # justification: heartbeat state may be absent; ignore
            pass
        # Topology/proxy state might have changed; refresh per-connection mapping
        try:
            self._refresh_conn_proxies()
        except Exception:
            pass

    # --- Federation helpers ---

    def _emit_link_notice_once(self, proxy: "RemotePortProxy", state: str, src_path: str):
        """Best-effort, de-duplicated emission of a federated-link notice.

        Args:
            proxy: RemotePortProxy to which the notice applies.
            state: One of {"stale", "disconnected", "restored"}.
            src_path: Human-readable "server_id::port_name" path.

        Notes:
            - Only emits when state changes from the last emitted state.
            - Adds a short 0.5s rate limit to guard against bursts.
        """
        try:
            last = getattr(proxy, "_last_link_notice", None)
            last_ts = float(getattr(proxy, "_last_link_notice_ts", 0.0) or 0.0)
            now_ts = time.time()
            if last == state and (now_ts - last_ts) < 0.5:
                return  # suppress burst duplicates
            if last == state:
                return  # already emitted for this state
            # Build message by state
            if state == "stale":
                text = f"\r\n[OpenMux:FEDERATED_LINK_STALE {src_path} paused until path recovers]\r\n"
            elif state == "disconnected":
                text = f"\r\n[OpenMux:FEDERATED_LINK_DISCONNECTED {src_path} paused until reconnected]\r\n"
            elif state == "restored":
                text = f"\r\n[OpenMux:FEDERATED_LINK_RESTORED {src_path} reconnected]\r\n"
            else:
                return
            msg = text.encode("utf-8")
            if hasattr(proxy, "data_queue") and proxy.data_queue:
                try:
                    proxy.data_queue.put_nowait(msg)
                except Exception:
                    pass
            try:
                setattr(proxy, "_last_link_notice", state)
                setattr(proxy, "_last_link_notice_ts", now_ts)
            except Exception:
                pass
        except Exception:
            # Best-effort only
            pass

    async def _cache_cleanup_loop(self):
        """Periodically purge cached offline proxies past TTL."""
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(max(5.0, min(self.federated_cache_ttl_sec or 60.0, 60.0)))
                try:
                    self._purge_offline_cached_ports()
                except Exception:
                    pass
        except asyncio.CancelledError:
            return

    def _purge_offline_cached_ports(self):
        """Remove cached remote proxies that have been offline longer than TTL."""
        if not self.federated_cache_enabled or not (self.federated_cache_ttl_sec and self.federated_cache_ttl_sec > 0):
            return
        now = time.time()
        for peer_key, proxies in list((self._peer_proxies or {}).items()):
            for pname, proxy in list((proxies or {}).items()):
                try:
                    if getattr(proxy, "is_connected", True):
                        continue
                    last_seen = float(getattr(proxy, "last_seen", 0) or 0)
                    if last_seen and (now - last_seen) > self.federated_cache_ttl_sec:
                        # Remove from PortManager and internal maps
                        try:
                            if hasattr(self, "main_port_manager") and self.main_port_manager:
                                pm = self.main_port_manager
                                try:
                                    if hasattr(pm, "unregister_federated_port"):
                                        import inspect, asyncio
                                        fn = getattr(pm, "unregister_federated_port")
                                        if inspect.iscoroutinefunction(fn):
                                            try:
                                                asyncio.create_task(fn(pname))
                                            except Exception:
                                                getattr(pm, "ports", {}).pop(pname, None)
                                        else:
                                            try:
                                                fn(pname)
                                            except Exception:
                                                getattr(pm, "ports", {}).pop(pname, None)
                                    else:
                                        getattr(pm, "ports", {}).pop(pname, None)
                                    try:
                                        pm.notify_meta_updated(pname, {"event": "federated_port_unregistered_ttl"})
                                    except Exception:
                                        pass
                                except Exception:
                                    getattr(pm, "ports", {}).pop(pname, None)
                        except Exception:
                            pass
                        proxies.pop(pname, None)
                        try:
                            self._save_federated_cache()
                        except Exception:
                            pass
                except Exception:
                    continue

    def _save_federated_cache(self) -> None:
        """Persist cached federated proxies (minimal fields) to JSON file."""
        if not self.federated_cache_enabled:
            return
        try:
            data = {"peers": {}}
            for peer_key, proxies in (self._peer_proxies or {}).items():
                ent = {}
                for pname, proxy in (proxies or {}).items():
                    try:
                        meta = getattr(proxy, "metadata", None)
                        origin_id = None
                        desc = None
                        max_rw = None
                        serial_cfg = None
                        line_status = None
                        if meta is not None:
                            try:
                                origin = getattr(meta, "origin_server", None)
                                origin_id = getattr(origin, "server_id", None)
                                desc = getattr(meta, "description", None)
                                max_rw = getattr(meta, "max_rw_users", None)
                                serial_cfg = getattr(meta, "serial_config", None)
                                line_status = getattr(meta, "line_status", None)
                            except Exception:
                                pass
                        ent[pname] = {
                            "connected": bool(getattr(proxy, "is_connected", False)),
                            "last_seen": float(getattr(proxy, "last_seen", 0) or 0),
                            "origin_server_id": origin_id,
                            "description": desc,
                            "max_rw_users": max_rw,
                            "serial_config": serial_cfg,
                            "line_status": line_status,
                        }
                    except Exception:
                        continue
                data["peers"][peer_key] = ent
            os.makedirs(os.path.dirname(self.federated_cache_path), exist_ok=True)
            with open(self.federated_cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass

    def _load_federated_cache(self) -> None:
        """Load cached federated proxies from JSON file and register placeholders."""
        try:
            if not os.path.exists(self.federated_cache_path):
                return
            with open(self.federated_cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            peers = data.get("peers", {}) or {}
            for peer_key, ports in peers.items():
                for pname, rec in (ports or {}).items():
                    try:
                        # Skip if already present
                        if peer_key not in self._peer_proxies:
                            self._peer_proxies[peer_key] = {}
                        if pname in self._peer_proxies[peer_key]:
                            continue
                        # Build minimal PortMetadata-like object
                        from ...common.federation_types import (
                            FederationType,
                            PortMetadata,
                            ServerInfo,
                            ServerType,
                        )
                        origin_id = rec.get("origin_server_id") or "remote"
                        server_info = ServerInfo(
                            server_id=str(origin_id),
                            hostname="remote",
                            port=0,
                            server_type=ServerType.LEAF,
                            description="",
                        )
                        metadata = PortMetadata(
                            name=pname,
                            original_name=pname,
                            description=rec.get("description", f"Remote port {pname}"),
                            adapter_type="remote_muxcon",
                            origin_server=server_info,
                            server_chain=[server_info],
                            status=("connected" if rec.get("connected") else "disconnected"),
                            max_rw_users=rec.get("max_rw_users", 1),
                            federation_type=FederationType.PULL,
                            serial_config=rec.get("serial_config"),
                            line_status=rec.get("line_status"),
                        )
                        proxy = self.RemotePortProxy(self, peer_key, pname, metadata)
                        proxy.is_connected = bool(rec.get("connected", False))
                        try:
                            proxy.last_seen = float(rec.get("last_seen", 0) or 0)
                        except Exception:
                            pass
                        # Register with PortManager if available
                        if hasattr(self, "main_port_manager") and self.main_port_manager:
                            pm = self.main_port_manager
                            try:
                                # Avoid duplicates by checking via API if present
                                existing = None
                                try:
                                    existing = pm.get_port(pname) if hasattr(pm, "get_port") else None
                                except Exception:
                                    existing = None
                                if existing is None:
                                    getattr(pm, "ports", {})[pname] = proxy
                            except Exception:
                                getattr(pm, "ports", {})[pname] = proxy
                        self._peer_proxies[peer_key][pname] = proxy
                    except Exception:
                        continue
        except Exception:
            pass

    async def _maybe_advertise_local_ports(self, conn_id: str) -> None:
        """Advertise local ports once when allowed.

        Sends PORTS:FEDERATED exactly once per connection after the session is
        authenticated (server-side when auth is required) or immediately after
        handshake (when auth is not required or for the client role where no
        auth is configured). Idempotent via a per-connection flag.
        """
        conn = self.connections.get(conn_id)
        if not conn:
            return
        if conn.get("ports_advertised"):
            return
        # Only proceed when session is considered authenticated for this side
        if not self._is_conn_authenticated(conn_id):
            return
        writer = conn.get("writer")
        if not isinstance(writer, asyncio.StreamWriter):
            return
        await self._send_local_port_list(conn_id, writer)
        conn["ports_advertised"] = True

    async def _request_remote_ports(self, conn_id: str):
        """Request federated port list from the peer with de-duplication.

        Sends a PORTS:LIST:FEDERATED control request unless one was sent very
        recently for the same connection.

        Args:
            conn_id: Target connection identifier.
        """
        conn = self.connections.get(conn_id)
        if not conn:
            return
        writer: asyncio.StreamWriter = conn["writer"]
        # De-duplicate rapid successive requests (within 2 seconds)
        now_ts = time.time()
        last_req = conn.get("last_ports_req_ts")
        if last_req and now_ts - last_req < 2.0:
            self.logger.debug(f"[{conn_id}] Suppressing duplicate PORTS:LIST:FEDERATED (last {now_ts - last_req:.2f}s ago)")
            return
        conn["last_ports_req_ts"] = now_ts
        try:
            seq = self._next_frame_seq(conn_id)
            frame = self.proto.create_port_list_request(seq)
            await self._send_protocol_frame(writer, frame)
            self.logger.debug(f"[{conn_id}] Requested federated port list (seq={seq})")
        except Exception as e:
            self.logger.info(f"[{conn_id}] Failed to request federated port list: {e}", exc_info=True)

    async def _send_local_port_list(self, conn_id: str, writer: asyncio.StreamWriter):
        """Send this node's local ports in a PORTS:FEDERATED response.

        Filters out already federated remote ports and encodes the remaining
        local ports as PortMetadata entries.

        Args:
            conn_id: Connection over which to send the response.
            writer: Stream writer bound to the connection.
        """
        try:
            if not hasattr(self, "main_port_manager") or not self.main_port_manager:
                self.logger.warning("No main_port_manager available to list local ports")
                return
            all_ports = await self.main_port_manager.get_port_list_with_federation()

            # Filter out already-federated remote ports if possible
            def is_remote(entry: Dict[str, Any]) -> bool:
                at = entry.get("adapter_type") or entry.get("adapter")
                return at == "remote_muxcon"

            # Apply advertise filters to local ports
            exposed = []
            for p in all_ports:
                if is_remote(p):
                    continue
                pname = p.get("name") or p.get("port") or ""
                atype = p.get("adapter_type") or p.get("adapter") or ""
                # server filter rarely applies on advertise; ignore unless configured
                sid = self.server_id
                if not self._allow_advertise_port_for_conn(conn_id, pname, atype, sid):
                    continue
                exposed.append(p)

            # Build PortMetadata list
            from ...common.federation_types import PortMetadata

            # Choose a representative listen port (first enabled listener)
            rep_port = 0
            for lc in self.listeners_conf:
                if lc.get("enabled"):
                    rep_port = int(lc.get("port", 0))
                    break
            server_info = ServerInfo(
                server_id=self.server_id,
                hostname=socket.gethostname(),
                port=rep_port,
                server_type=ServerType.LEAF,
                description=self.server_description or "",
            )
            metas: List[PortMetadata] = []
            for p in exposed:
                name = p.get("name") or p.get("port") or "unknown"
                desc = p.get("description", "")
                adapter_type = p.get("adapter_type") or p.get("adapter") or "unknown"
                status = "connected"
                if "connected" in p:
                    status = "connected" if p.get("connected") else "disconnected"
                elif "state" in p:
                    status = p.get("state")
                max_rw = p.get("max_read_write_users", p.get("max_rw_users", 1))
                # Optional serial details for local serial or loopback ports
                serial_cfg = None
                line_status = None
                try:
                    at_lower = str(adapter_type).lower()
                    if at_lower == "serial":
                        sc = p.get("serial_config") or {}
                        if sc:
                            serial_cfg = {
                                "device": sc.get("device"),
                                "baudrate": sc.get("baudrate"),
                                "bytesize": sc.get("bytesize"),
                                "parity": sc.get("parity"),
                                "stopbits": sc.get("stopbits"),
                                "flow_control": sc.get("flow_control"),
                            }
                        ls = p.get("line_status")
                        if isinstance(ls, dict) and ls:
                            line_status = ls
                    elif at_lower == "loopback":
                        # Emulate serial-like metadata for loopback ports so remote peers
                        # can render consistent details without UI-specific patches.
                        sc = p.get("serial_config") or {}
                        if sc:
                            serial_cfg = {
                                "device": sc.get("device"),
                                "baudrate": sc.get("baudrate"),
                                "bytesize": sc.get("bytesize"),
                                "parity": sc.get("parity"),
                                "stopbits": sc.get("stopbits"),
                                "flow_control": sc.get("flow_control"),
                            }
                        else:
                            serial_cfg = {
                                "device": f"loopback:{name}",
                                "baudrate": 9600,
                                "bytesize": 8,
                                "parity": "N",
                                "stopbits": 1,
                                "flow_control": "none",
                            }
                        ls = p.get("line_status")
                        if isinstance(ls, dict) and ls:
                            line_status = ls
                        else:
                            line_status = {"DCD": False, "DSR": True, "CTS": True, "RTS": True, "DTR": True}
                except Exception:
                    pass
                metas.append(
                    PortMetadata(
                        name=name,
                        original_name=name,
                        description=desc,
                        adapter_type=adapter_type,
                        origin_server=server_info,
                        server_chain=[server_info],
                        status=status,
                        max_rw_users=max_rw,
                        serial_config=serial_cfg,
                        line_status=line_status,
                    )
                )

            seq = self._next_frame_seq(conn_id)
            frame = self.proto.create_federated_port_list_response(metas, seq)
            await self._send_protocol_frame(writer, frame)
            self.logger.info(f"Sent {len(metas)} local ports in PORTS:FEDERATED response")
        except Exception as e:
            self.logger.error(f"Error sending local port list: {e}", exc_info=True)

    # --- Port filter helpers ---
    def _match_any(self, value: str, patterns: List[str]) -> bool:
        if not patterns:
            return False
        for pat in patterns:
            try:
                if fnmatch.fnmatchcase(value, str(pat)):
                    return True
            except Exception:
                continue
        return False

    def _first_match(self, value: str, patterns: List[str]) -> Optional[str]:
        try:
            for pat in patterns or []:
                try:
                    if fnmatch.fnmatchcase(value, str(pat)):
                        return str(pat)
                except Exception:
                    continue
        except Exception:
            return None
        return None

    def _get_filters_for_conn(self, conn_id: str) -> Dict[str, Dict[str, List[str]]]:
        try:
            overrides = self._conn_filters.get(conn_id) or {}
            adv = overrides.get("advertise_filters") or {}
            acc = overrides.get("accept_filters") or {}
            # Build effective advertise filters
            eff_adv = {
                "include": list(adv.get("include") or self._adv_name_inc),
                "exclude": list(adv.get("exclude") or self._adv_name_exc),
                "adapter_include": list(adv.get("adapter_include") or self._adv_adapter_inc),
                "adapter_exclude": list(adv.get("adapter_exclude") or self._adv_adapter_exc),
                "server_include": list(adv.get("server_include") or self._adv_server_inc),
                "server_exclude": list(adv.get("server_exclude") or self._adv_server_exc),
            }
            eff_acc = {
                "include": list(acc.get("include") or self._acc_name_inc),
                "exclude": list(acc.get("exclude") or self._acc_name_exc),
                "adapter_include": list(acc.get("adapter_include") or self._acc_adapter_inc),
                "adapter_exclude": list(acc.get("adapter_exclude") or self._acc_adapter_exc),
                "server_include": list(acc.get("server_include") or self._acc_server_inc),
                "server_exclude": list(acc.get("server_exclude") or self._acc_server_exc),
            }
            return {"advertise_filters": eff_adv, "accept_filters": eff_acc}
        except Exception:
            return {
                "advertise_filters": {
                    "include": self._adv_name_inc,
                    "exclude": self._adv_name_exc,
                    "adapter_include": self._adv_adapter_inc,
                    "adapter_exclude": self._adv_adapter_exc,
                    "server_include": self._adv_server_inc,
                    "server_exclude": self._adv_server_exc,
                },
                "accept_filters": {
                    "include": self._acc_name_inc,
                    "exclude": self._acc_name_exc,
                    "adapter_include": self._acc_adapter_inc,
                    "adapter_exclude": self._acc_adapter_exc,
                    "server_include": self._acc_server_inc,
                    "server_exclude": self._acc_server_exc,
                },
            }

    def _allow_advertise_port_for_conn(self, conn_id: str, name: str, adapter_type: str, server_id: Optional[str]) -> bool:
        f = self._get_filters_for_conn(conn_id).get("advertise_filters", {})
        name_exc = f.get("exclude") or []
        adapter_exc = f.get("adapter_exclude") or []
        server_exc = f.get("server_exclude") or []
        name_inc = f.get("include") or []
        adapter_inc = f.get("adapter_include") or []
        server_inc = f.get("server_include") or []
        if self._match_any(name, name_exc) or self._match_any(adapter_type, adapter_exc):
            if self.logger.isEnabledFor(logging.DEBUG):
                reason = None
                pat = self._first_match(name, name_exc)
                if pat:
                    reason = f"name excluded by '{pat}'"
                else:
                    pat = self._first_match(adapter_type, adapter_exc)
                    if pat:
                        reason = f"adapter excluded by '{pat}'"
                self.logger.debug(f"[{conn_id}] ADV DROP name='{name}' adapter='{adapter_type}' reason={reason}")
            return False
        if server_id and self._match_any(server_id, server_exc):
            if self.logger.isEnabledFor(logging.DEBUG):
                pat = self._first_match(server_id, server_exc)
                self.logger.debug(f"[{conn_id}] ADV DROP name='{name}' server='{server_id}' reason=server excluded by '{pat}'")
            return False
        inc_name = not name_inc or self._match_any(name, name_inc)
        inc_adapter = not adapter_inc or self._match_any(adapter_type, adapter_inc)
        inc_server = not server_inc or (server_id and self._match_any(server_id, server_inc))
        if not (inc_name and inc_adapter and inc_server) and self.logger.isEnabledFor(logging.DEBUG):
            parts = []
            if not inc_name:
                parts.append("name not in include")
            if not inc_adapter:
                parts.append("adapter not in include")
            if not inc_server:
                parts.append("server not in include")
            self.logger.debug(f"[{conn_id}] ADV DROP name='{name}' adapter='{adapter_type}' reason={'; '.join(parts)}")
        return bool(inc_name and inc_adapter and inc_server)

    def _allow_advertise_port(self, name: str, adapter_type: str, server_id: Optional[str]) -> bool:
        # Exclude takes precedence
        if self._match_any(name, self._adv_name_exc) or self._match_any(adapter_type, self._adv_adapter_exc):
            return False
        if server_id and self._match_any(server_id, self._adv_server_exc):
            return False
        # If includes are set, require a match
        inc_name = not self._adv_name_inc or self._match_any(name, self._adv_name_inc)
        inc_adapter = not self._adv_adapter_inc or self._match_any(adapter_type, self._adv_adapter_inc)
        inc_server = not self._adv_server_inc or (server_id and self._match_any(server_id, self._adv_server_inc))
        return bool(inc_name and inc_adapter and inc_server)

    def _allow_accept_port_for_conn(self, conn_id: str, pd: Dict[str, Any]) -> bool:
        try:
            name = str(pd.get("name") or pd.get("original_name") or "")
            atype = str(pd.get("adapter_type") or "")
            origin = pd.get("origin_server") or {}
            sid = str(origin.get("server_id") or "") if isinstance(origin, dict) else str(origin or "")
        except Exception:
            name = pd.get("name") or ""
            atype = pd.get("adapter_type") or ""
            sid = ""
        f = self._get_filters_for_conn(conn_id).get("accept_filters", {})
        name_exc = f.get("exclude") or []
        adapter_exc = f.get("adapter_exclude") or []
        server_exc = f.get("server_exclude") or []
        name_inc = f.get("include") or []
        adapter_inc = f.get("adapter_include") or []
        server_inc = f.get("server_include") or []
        # Exclude precedence
        if self._match_any(name, name_exc) or self._match_any(atype, adapter_exc):
            if self.logger.isEnabledFor(logging.DEBUG):
                reason = None
                pat = self._first_match(name, name_exc)
                if pat:
                    reason = f"name excluded by '{pat}'"
                else:
                    pat = self._first_match(atype, adapter_exc)
                    if pat:
                        reason = f"adapter excluded by '{pat}'"
                self.logger.debug(f"[{conn_id}] ACC DROP name='{name}' adapter='{atype}' reason={reason}")
            return False
        if sid and self._match_any(sid, server_exc):
            if self.logger.isEnabledFor(logging.DEBUG):
                pat = self._first_match(sid, server_exc)
                self.logger.debug(f"[{conn_id}] ACC DROP name='{name}' server='{sid}' reason=server excluded by '{pat}'")
            return False
        # Include constraints
        inc_name = not name_inc or self._match_any(name, name_inc)
        inc_adapter = not adapter_inc or self._match_any(atype, adapter_inc)
        inc_server = not server_inc or (sid and self._match_any(sid, server_inc))
        if not (inc_name and inc_adapter and inc_server) and self.logger.isEnabledFor(logging.DEBUG):
            parts = []
            if not inc_name:
                parts.append("name not in include")
            if not inc_adapter:
                parts.append("adapter not in include")
            if not inc_server:
                parts.append("server not in include")
            self.logger.debug(f"[{conn_id}] ACC DROP name='{name}' adapter='{atype}' reason={'; '.join(parts)}")
        return bool(inc_name and inc_adapter and inc_server)

    async def _process_control_command(self, conn_id: str, writer: asyncio.StreamWriter, payload: str):
        """Process a control payload (ASCII) for a connection.

        Handles shutdown markers, heartbeat REQ/ACK, and federated port listing
        commands. Unrecognized commands are ignored.

        Args:
            conn_id: Connection delivering the control message.
            writer: Writer for sending control responses.
            payload: Raw control payload string (already decoded).
        """
        try:
            payload = payload.strip()
            self.logger.debug(f"[{conn_id}] CONTROL: {payload[:200]}")
            # Handle authentication control messages first
            if payload.startswith("AUTH:"):
                parts = payload.split(":")
                if len(parts) >= 2 and parts[1] == "PK":
                    # AUTH:PK:CHALLENGE:<key_id>:<nonce_b64> (client side)
                    if len(parts) >= 5 and parts[2] == "CHALLENGE":
                        try:
                            kid = parts[3]
                            nonce_b64 = parts[4]
                            if self._auth_priv and self._auth_key_id and kid == self._auth_key_id:
                                nonce = base64.b64decode(nonce_b64)
                                sig = self._auth_priv.sign(nonce)
                                sig_b64 = base64.b64encode(sig).decode()
                                seq = self._next_frame_seq(conn_id)
                                frame = self.proto.create_control_frame(0, seq, f"AUTH:PK:RESPONSE:{kid}:{sig_b64}")
                                await self._send_protocol_frame(writer, frame)
                            else:
                                # No key to respond; send error
                                seq = self._next_frame_seq(conn_id)
                                frame = self.proto.create_control_frame(0, seq, "AUTH:ERROR:no_client_key")
                                await self._send_protocol_frame(writer, frame)
                        except Exception:
                            pass
                        return
                    # AUTH:PK:RESPONSE:<key_id>:<sig_b64> (server side)
                    if len(parts) >= 5 and parts[2] == "RESPONSE":
                        try:
                            kid = parts[3]
                            sig_b64 = parts[4]
                            conn = self.connections.get(conn_id) or {}
                            st = conn.get("auth_state") or {}
                            if not st or st.get("type") != "pk" or st.get("key_id") != kid:
                                return
                            if time.time() > st.get("expires_at", 0):
                                # expired
                                seq = self._next_frame_seq(conn_id)
                                frame = self.proto.create_control_frame(0, seq, "AUTH:ERROR:expired")
                                await self._send_protocol_frame(writer, frame)
                                await self._close_connection(conn_id)
                                return
                            pub = self._auth_pubkeys.get(kid)
                            ok = False
                            if pub:
                                try:
                                    sig = base64.b64decode(sig_b64)
                                    pub.verify(sig, st.get("nonce") or b"")
                                    ok = True
                                except Exception:
                                    ok = False
                            if ok:
                                conn["auth_ok"] = True
                                conn.pop("auth_state", None)
                                # Apply per-key muxcon filters for this authenticated peer
                                try:
                                    self._apply_per_connection_filters(conn_id, kid)
                                except Exception:
                                    pass
                                seq = self._next_frame_seq(conn_id)
                                frame = self.proto.create_control_frame(0, seq, "AUTH:OK")
                                await self._send_protocol_frame(writer, frame)
                                # Now that auth is OK, advertise our ports
                                try:
                                    await self._maybe_advertise_local_ports(conn_id)
                                except Exception:
                                    pass
                            else:
                                seq = self._next_frame_seq(conn_id)
                                frame = self.proto.create_control_frame(0, seq, "AUTH:ERROR:bad_signature")
                                await self._send_protocol_frame(writer, frame)
                                await self._close_connection(conn_id)
                        except Exception:
                            pass
                        return
                # AUTH:OK / AUTH:ERROR (client side updates)
                if payload == "AUTH:OK":
                    try:
                        conn = self.connections.get(conn_id)
                        if conn:
                            conn["auth_ok"] = True
                            # Apply our own per-key filters if we have a client key id
                            try:
                                if self._auth_key_id:
                                    self._apply_per_connection_filters(conn_id, self._auth_key_id)
                            except Exception:
                                pass
                            # After AUTH:OK, advertise our ports
                            try:
                                await self._maybe_advertise_local_ports(conn_id)
                            except Exception:
                                pass
                    except Exception:
                        pass
                    return
                if payload.startswith("AUTH:ERROR"):
                    # Server reported auth failure; log actionable hints and close
                    try:
                        parts = payload.split(":")
                        code = parts[2] if len(parts) >= 3 else "unknown"
                        # Provide detailed guidance for common cases
                        if code == "missing_or_unknown_pkid":
                            if self._auth_key_id and self._auth_priv:
                                self.logger.error(
                                    f"MuxCon auth failed: server does not recognize PKID '{self._auth_key_id}'. "
                                    f"Ensure listener muxcon.auth.public_keys includes key_id='{self._auth_key_id}' with the matching public key."
                                )
                            else:
                                self.logger.error(
                                    "MuxCon auth failed: server requires a known PKID but client did not advertise one. "
                                    "Set muxcon.auth.key_id and muxcon.auth.private_key on the initiator."
                                )
                        elif code == "bad_signature":
                            self.logger.error(
                                "MuxCon auth failed: bad signature. Verify the initiator private key matches the server's configured public key for this key_id."
                            )
                        elif code == "expired":
                            self.logger.warning(
                                "MuxCon auth challenge expired before response. Check clock skew and network latency; reconnect will retry."
                            )
                        else:
                            self.logger.error(f"MuxCon auth failed with error: {payload}")
                    except Exception:
                        self.logger.error(f"MuxCon auth failed with error: {payload}")
                    await self._close_connection(conn_id)
                    return
            # Fast shutdown (no drain). Any BEGIN triggers immediate END + close.
            if payload.startswith("MPATH:SHUTDOWN:BEGIN"):
                st = self._shutdown_state.setdefault(conn_id, {"state": "BEGIN_SEEN"})
                if st.get("state") != "CLOSED":
                    st["state"] = "BEGIN_SEEN"
                    try:
                        seq = self._next_frame_seq(conn_id)
                        frame = self.proto.create_control_frame(0, seq, "MPATH:END")
                        await self._send_protocol_frame(writer, frame)
                    except Exception:  # justification: shutdown END send is best-effort; proceed to close
                        pass
                    st["state"] = "CLOSED"
                    await self._close_connection(conn_id)
                return
            if payload == "MPATH:END":
                st = self._shutdown_state.setdefault(conn_id, {"state": "CLOSED"})
                if st.get("state") != "CLOSED":
                    st["state"] = "CLOSED"
                await self._close_connection(conn_id)
                return
            # Heartbeat request/ack handling (new HB payloads or legacy HB:... in C-frames)
            if payload.startswith("REQ:") or payload.startswith("HB:REQ:"):
                # Echo back ACK with same timestamp
                try:
                    raw = payload.split(":", 1)[1] if payload.startswith("REQ:") else payload.split(":", 2)[2]
                    ts = float(raw)
                except Exception:  # justification: malformed heartbeat timestamp; fall back to current time
                    ts = time.time()
                ack_seq = self._next_frame_seq(conn_id)
                ack = self.proto.create_heartbeat_ack(ts, ack_seq)
                await self._send_protocol_frame(writer, ack)
                # Update last_seen on inbound heartbeat
                try:
                    if conn_id in self.connections:
                        ts_now = time.time()
                        self.connections[conn_id]["last_seen"] = ts_now
                        key = self._derive_peer_key_from_conn_id(conn_id)
                        grp = self._mpath_groups.get(key)
                        if grp and conn_id in grp.get("conns", {}):
                            grp["conns"][conn_id]["last_seen"] = ts_now
                            grp["conns"][conn_id]["last_rx_seen"] = ts_now
                except Exception:  # justification: heartbeat metrics computation is best-effort
                    pass
                return

            if payload.startswith("ACK:") or payload.startswith("HB:ACK:"):
                # Update heartbeat state
                try:
                    raw = payload.split(":", 1)[1] if payload.startswith("ACK:") else payload.split(":", 2)[2]
                    ts = float(raw)
                except Exception:  # justification: malformed ACK timestamp; use current time as fallback
                    ts = time.time()
                st = self._hb_state.setdefault(
                    conn_id,
                    {
                        "last_req_ts": 0.0,
                        "last_ack_ts": 0.0,
                        "missed": 0,
                        "rtt_ms": None,
                    },
                )
                st["last_ack_ts"] = time.time()
                # Compute RTT if matches our last req
                try:
                    if st["last_req_ts"]:
                        st["rtt_ms"] = max(
                            0,
                            int((st["last_ack_ts"] - st["last_req_ts"]) * 1000),
                        )
                    st["missed"] = 0
                except Exception:  # justification: last_seen update is advisory; ignore failures
                    pass
                # Update last_seen on ACK
                try:
                    if conn_id in self.connections:
                        ts_now = time.time()
                        self.connections[conn_id]["last_seen"] = ts_now
                        key = self._derive_peer_key_from_conn_id(conn_id)
                        grp = self._mpath_groups.get(key)
                        if grp and conn_id in grp.get("conns", {}):
                            grp["conns"][conn_id]["last_seen"] = ts_now
                            grp["conns"][conn_id]["last_rx_seen"] = ts_now
                except Exception:  # justification: last_seen update is advisory; ignore failures
                    pass
                return
            if payload == "PORTS:LIST:FEDERATED":
                # Ignore request-based discovery; we proactively advertise on auth/handshake
                return
            if payload.startswith("PORTS:FEDERATED:"):
                if not self._is_conn_authenticated(conn_id):
                    return
                await self._handle_ports_federated(conn_id, payload)
                return
            # Other commands can be added here
        except Exception as e:
            self.logger.error(f"Error processing control command from {conn_id}: {e}", exc_info=True)

    async def initiate_graceful_shutdown(self, conn_id: str, writer: asyncio.StreamWriter, reason: Optional[str] = None):
        """Initiate fast shutdown: send BEGIN then END and close immediately."""
        st = self._shutdown_state.setdefault(conn_id, {"state": "ACTIVE"})
        if st.get("state") == "CLOSED":
            return
        try:
            seq = self._next_frame_seq(conn_id)
            frame = self.proto.create_control_frame(0, seq, f"MPATH:SHUTDOWN:BEGIN{':' + reason if reason else ''}")
            await self._send_protocol_frame(writer, frame)
            try:
                end_seq = self._next_frame_seq(conn_id)
                end_frame = self.proto.create_control_frame(0, end_seq, "MPATH:END")
                await self._send_protocol_frame(writer, end_frame)
            except Exception:  # justification: END send during graceful shutdown is best-effort
                pass
        except Exception as e:
            self.logger.debug(f"[{conn_id}] Failed during graceful shutdown control frames: {e}", exc_info=True)
        st["state"] = "CLOSED"
        await self._close_connection(conn_id)

    async def _shutdown_grace_timeout_task(self, conn_id: str, writer: asyncio.StreamWriter):
        """Compatibility no-op for legacy graceful shutdown timer.

        Fast shutdown is immediate; this task is retained for API compatibility.

        Args:
            conn_id: Connection identifier.
            writer: Connection writer.
        """
        pass

    async def _schedule_shutdown_end(self, conn_id: str, writer: asyncio.StreamWriter):
        """Compatibility no-op: END is sent immediately in fast shutdown.

        Args:
            conn_id: Connection identifier.
            writer: Connection writer.
        """
        pass

    # ===================== Binary Protocol Integration (Experimental) =====================

    # ===================== Multipath Helper Methods (ASCII protocol) =====================

    def _derive_peer_key_from_conn_id(self, conn_id: str) -> str:
        """Derive a stable multipath peer grouping key for a connection.

        The grouping key determines which physical TCP connections are considered
        alternate paths ("multipath") to the same logical peer so that
        promotion / failover logic can operate over an aggregate. The method
        attempts to use the most stable identity information available in the
        following priority order:

          1. ``node:<server_id>`` if a stable server identifier is available.
          2. For outgoing connections (prefix ``out:``) use ``<host>:<peer_listen_port>``.
          3. For inbound pre‑handshake connections collapse to ``host:<ip>`` so
           simultaneous inbound attempts from the same peer group together even
           if they arrive on different ephemeral ports.
          4. Fallback: ``unknown:0`` if nothing else can be derived.

        Args:
            conn_id: Internal connection identifier (e.g. ``out:1.2.3.4:4000:12345`` or
                ``in:1.2.3.4:54321``) containing direction, host and ports.

        Returns:
            A stable peer grouping key string.
        """
        try:
            conn = self.connections.get(conn_id) or {}
            hs = conn.get("handshake")
            # hs could be MuxConHandshake or dict
            server_id = None
            if hs:
                try:
                    # Direct server_id attribute from handshake (new)
                    if server_id is None and not isinstance(hs, dict):
                        server_id = getattr(hs, "server_id", None)
                except Exception:  # justification: handshake server_id retrieval is optional; safe fallback
                    pass
                try:
                    if server_id is None and hasattr(hs, "server_info"):
                        si = getattr(hs, "server_info")
                        server_id = getattr(si, "server_id", None)
                    elif isinstance(hs, dict):
                        server_id = hs.get("server_id")
                except Exception:  # justification: server_info access is best-effort; grouping tolerates unknown
                    pass
            # Prefer stable server_id for grouping
            if server_id:
                return f"node:{server_id}"
            # Fallback: if handshake lacked identity, use connection record's server_id when present
            try:
                rec_sid = conn.get("server_id")
                if rec_sid:
                    return f"node:{rec_sid}"
            except Exception:  # justification: advisory fallback; ignore errors
                pass
            parts = conn_id.split(":")
            if len(parts) >= 4:
                direction = parts[0]
                host = parts[1]
                port = parts[2]
                if direction == "out":
                    return f"{host}:{port}"  # peer's listen port
                # inbound: avoid ephemeral port specificity until handshake gives identity
                return f"host:{host}"  # collapse all inbound pre-handshake
        except Exception:  # justification: server_chain population is advisory metadata; ignore errors
            pass
        return "unknown:0"

    def _retire_old_generation(self, conn_id: str) -> None:
        """Retire older generation connections for the same logical peer.

        When a peer restarts it will typically present a new ``instance_id`` in
        the handshake while retaining the same stable ``server_id``. Multiple
        live connections with different generations are not desirable; this
        method asynchronously closes the older ones keeping the most recently
        opened generation. A race where the *current* connection is older than
        one already established is also handled by scheduling the current one
        for closure.

        Args:
            conn_id: Identifier of the (presumably newest) connection to compare
                against other connections from the same ``server_id``.
        """
        try:
            current = self.connections.get(conn_id)
            if not current:
                return
            cur_server_id = current.get("server_id")
            cur_instance_id = current.get("instance_id")
            if not cur_server_id or not cur_instance_id:
                return  # need both to compare generations
            # Gather other connections with same server_id but different instance_id
            to_close = []
            for cid, info in self.connections.items():
                if cid == conn_id:
                    continue
                if (
                    info.get("server_id") == cur_server_id
                    and info.get("instance_id")
                    and info.get("instance_id") != cur_instance_id
                ):
                    # Prefer keeping the one with later opened_at
                    opened_cur = float(current.get("opened_at", 0))
                    opened_other = float(info.get("opened_at", 0))
                    # If current is newer, retire other; if other is newer, schedule current (rare race) and break
                    if opened_cur >= opened_other:
                        to_close.append(cid)
                    else:
                        # Other is newer; mark current instead
                        to_close = [conn_id]
                        break
            for old_cid in to_close:
                if old_cid == conn_id:
                    # Close self (newer connection lost race) - schedule async
                    asyncio.create_task(self._close_connection(old_cid))
                else:
                    self.logger.info(
                        f"[MPATH] Retiring old-generation connection {old_cid} (server_id={cur_server_id}) in favor of {conn_id}"
                    )
                    asyncio.create_task(self._close_connection(old_cid))
        except Exception as e:
            try:
                self.logger.debug(f"Rollover check failed for {conn_id}: {e}", exc_info=True)
            except Exception:  # justification: logging failure should not interrupt cleanup
                pass

    def _register_mpath_connection(self, conn_id: str) -> None:
        """Register a connection in its multipath group and choose / update primary.

        Called whenever a new connection is accepted or established. The
        connection is inserted into a group keyed by
        :meth:`_derive_peer_key_from_conn_id`. The group's *primary* path is
        selected if none exists, or possibly preemptively promoted if
        ``mpath_preemptive_promote`` is enabled and the new connection has a
        higher preference value.

        Preference (``pref``) is currently only derived for outgoing
        connections via peer configuration (``path_pref`` option).

        Args:
            conn_id: Internal connection identifier.
        """
        key = self._derive_peer_key_from_conn_id(conn_id)
        grp = self._mpath_groups.setdefault(key, {"conns": OrderedDict(), "primary": None, "rr_index": 0})
        opened_at = 0.0
        try:
            opened_at = float(self.connections.get(conn_id, {}).get("opened_at", 0.0))
        except Exception:  # justification: opened_at conversion is advisory; treat as 0.0
            pass
        # Derive path preference (outbound via peer config, inbound via listener)
        pref = 0
        if conn_id.startswith("out:"):
            try:
                parts = conn_id.split(":")
                if len(parts) >= 4:
                    host = parts[1]
                    port = int(parts[2])
                    for peer in self.peers:
                        if peer.host == host and peer.port == port:
                            pref = int(peer.options.get("path_pref", 0))
                            break
            except Exception:  # justification: preemptive promotion evaluation is advisory; ignore failures
                pass
        elif conn_id.startswith("in:"):
            try:
                lpp = self.connections.get(conn_id, {}).get("listener_path_pref")
                if lpp is not None:
                    pref = int(lpp)
            except Exception:
                pass
        now_ts = time.time()
        grp["conns"][conn_id] = {"opened_at": opened_at, "pref": pref, "last_seen": now_ts, "last_rx_seen": now_ts}
        # Choose primary if none set
        if not grp.get("primary"):
            grp["primary"] = conn_id
            try:
                self.logger.debug(
                    f"[MPATH] Registered {conn_id} in group {key}; primary={grp.get('primary')} members={list(grp['conns'].keys())}"
                )
            except Exception:
                self.logger.debug(f"Registered {conn_id} in multipath group {key} primary={grp.get('primary')}")
            return
        # Preemptive promotion check: promote immediately if higher preference than current
        if self.mpath_preemptive_promote:
            current = grp.get("primary")
            if current and current != conn_id:
                cur_meta = grp["conns"].get(current)
                new_meta = grp["conns"].get(conn_id)
                try:
                    cur_pref = int(cur_meta.get("pref", 0)) if cur_meta else 0
                    new_pref = int(new_meta.get("pref", 0)) if new_meta else 0
                    if new_pref > cur_pref:
                        grp["primary"] = conn_id
                        self.logger.info(
                            f"[MPATH] Preemptive promote {conn_id} over {current} (pref {new_pref}>{cur_pref}) for {key}"
                        )
                except Exception:  # justification: per-iteration evaluation should not abort failover loop
                    pass
        try:
            self.logger.debug(
                f"[MPATH] Registered {conn_id} in group {key}; primary={grp.get('primary')} members={list(grp['conns'].keys())}"
            )
        except Exception:
            self.logger.debug(f"Registered {conn_id} in multipath group {key} primary={grp.get('primary')}")
        # Keep per-connection proxy view in sync
        try:
            self._refresh_conn_proxies()
        except Exception:
            pass

    def _rekey_mpath_connection(self, conn_id: str) -> None:
        """Re-evaluate the grouping key after handshake completes.

        During early connection lifetime grouping may be based on the remote
        host/port (pre‑handshake). Once a handshake provides stable identity
        (node name or server id) the connection may need to migrate to a new
        group. If the destination group already exists metadata is merged and
        primary selection rules are applied.

        Args:
            conn_id: Internal connection identifier to rekey.
        """
        try:
            new_key = self._derive_peer_key_from_conn_id(conn_id)
            # Find current key containing conn_id
            current_key = None
            for k, grp in self._mpath_groups.items():
                if conn_id in grp.get("conns", {}):
                    current_key = k
                    break
            if current_key == new_key or current_key is None:
                return
            # Move metadata
            meta = self._mpath_groups[current_key]["conns"].pop(conn_id, None)
            if meta is None:
                return
            # Cleanup old group if empty
            old_grp = self._mpath_groups.get(current_key)
            if old_grp:
                if old_grp.get("primary") == conn_id:
                    # choose next primary
                    old_grp["primary"] = next(iter(old_grp["conns"].keys()), None)
                if not old_grp["conns"]:
                    self._mpath_groups.pop(current_key, None)
                    # Also migrate or clear peer-level state from old key
                    try:
                        # If new key already has sendbuf, merge; else move
                        if current_key in self._peer_sendbuf:
                            old_buf = self._peer_sendbuf.pop(current_key, {})
                            if old_buf:
                                self._peer_sendbuf.setdefault(new_key, {}).update(old_buf)
                        # RX state: prefer existing new_key else move
                        if current_key in self._peer_rx_state:
                            rx_old = self._peer_rx_state.pop(current_key, None)
                            if rx_old is not None and new_key not in self._peer_rx_state:
                                self._peer_rx_state[new_key] = rx_old
                        # TX seq: keep max to avoid reuse
                        if current_key in self._peer_tx_seq:
                            old_seq = self._peer_tx_seq.pop(current_key, 1)
                            self._peer_tx_seq[new_key] = max(old_seq, self._peer_tx_seq.get(new_key, 1))
                        # RETX count: accumulate into new key
                        if current_key in self._peer_retx_count:
                            prev = self._peer_retx_count.pop(current_key, 0)
                            self._peer_retx_count[new_key] = prev + self._peer_retx_count.get(new_key, 0)
                    except Exception:
                        pass
            # Insert into new group (merge if exists)
            new_grp = self._mpath_groups.setdefault(new_key, {"conns": OrderedDict(), "primary": None, "rr_index": 0})
            new_grp["conns"][conn_id] = meta
            if not new_grp.get("primary"):
                new_grp["primary"] = conn_id
            try:
                self.logger.info(
                    f"[MPATH] Rekey {conn_id} {current_key} -> {new_key} primary={new_grp.get('primary')} members={list(new_grp['conns'].keys())}"
                )
            except Exception:
                self.logger.info(f"[MPATH] Rekey {conn_id} {current_key} -> {new_key} primary={new_grp.get('primary')}")
            # Update per-connection proxies mapping due to group change
            try:
                self._refresh_conn_proxies()
            except Exception:
                pass
        except Exception as e:
            self.logger.debug(f"Rekey failed for {conn_id}: {e}", exc_info=True)

    def _unregister_mpath_connection(self, conn_id: str) -> None:
        """Remove a connection from its multipath group and cleanup.

        If the connection was the group's primary a new primary is chosen.
        Empty groups are removed entirely.

        Args:
            conn_id: Internal connection identifier.
        """
        key = self._derive_peer_key_from_conn_id(conn_id)
        grp = self._mpath_groups.get(key)
        if not grp:
            return
        if conn_id in grp["conns"]:
            grp["conns"].pop(conn_id, None)
        if grp.get("primary") == conn_id:
            # Pick next available as primary
            new_primary = next(iter(grp["conns"].keys()), None)
            grp["primary"] = new_primary
        # Cleanup empty group
        if not grp["conns"]:
            self._mpath_groups.pop(key, None)
            try:
                self._peer_sendbuf.pop(key, None)
            except Exception:
                pass
            try:
                self._peer_rx_state.pop(key, None)
            except Exception:
                pass
            try:
                self._peer_tx_seq.pop(key, None)
            except Exception:
                pass
            try:
                self._peer_retx_count.pop(key, None)
            except Exception:
                pass
        self.logger.debug(f"Unregistered {conn_id} from multipath group {key}")
        # Refresh mapping after topology change
        try:
            self._refresh_conn_proxies()
        except Exception:
            pass

    def _refresh_conn_proxies(self) -> None:
        """Rebuild per-connection proxy map from peer-level proxies and multipath groups.

        For each multipath group, assign the same proxy dict to all connection ids
        in that group so UI layers can look up ports per connection easily. Use the
        underlying dict (not a deep copy) so updates to _peer_proxies reflect here.
        """
        try:
            mapping: Dict[str, Dict[str, Any]] = {}
            # Iterate groups and apply their peer_key proxies to each connection id
            for peer_key, grp in (self._mpath_groups or {}).items():
                proxies = (self._peer_proxies or {}).get(peer_key, {})
                for cid in (grp.get("conns", {}) or {}).keys():
                    mapping[cid] = proxies
            # Also include any standalone connections that may not be in groups yet
            for cid in (self.connections or {}).keys():
                if cid not in mapping:
                    pk = self._derive_peer_key_from_conn_id(cid)
                    mapping[cid] = (self._peer_proxies or {}).get(pk, {})
            self._conn_proxies = mapping
        except Exception:
            # Best-effort; leave previous mapping
            pass

    def _select_mpath_connection(self, peer_key: str) -> Optional[str]:
        """Select (and possibly promote) the active connection for a peer group.

        Implements both failover and (optionally) preemptive promotion logic. A
        connection becomes ineligible if it has not been seen within the stale
        window or has been *frozen* via fault injection. If the primary is
        missing or stale the best successor is promoted based on preference and
        open time. When ``mpath_preemptive_promote`` + ``best_pref`` strategy are
        enabled a higher preference non‑stale path may replace the current
        primary.

        Args:
            peer_key: Multipath group key returned by
                :meth:`_derive_peer_key_from_conn_id`.

        Returns:
            The selected (possibly newly promoted) connection id or ``None`` if
            no eligible connections exist.
        """
        grp = self._mpath_groups.get(peer_key)
        if not grp:
            return None
        now = time.time()
        stale_cutoff = now - self.mpath_primary_stale_sec if self.mpath_primary_stale_sec > 0 else None
        # Filter available (non-stale) connections
        candidates = []
        for cid, meta in grp["conns"].items():
            ls = meta.get("last_rx_seen") or 0
            stale = False
            if stale_cutoff is not None and ls < stale_cutoff:
                stale = True
            # Exclude frozen connections from selection entirely (treat as stale)
            if self._fault_state.get(cid, {}).get("frozen"):
                stale = True
            candidates.append((cid, meta, stale))
        # Promote if current primary missing or stale
        current = grp.get("primary")
        if current:
            meta = grp["conns"].get(current)
            if not meta:
                current = None
            else:
                ls = meta.get("last_rx_seen") or 0
                if stale_cutoff is not None and ls < stale_cutoff:
                    current = None
                # Also demote if current is frozen
                elif self._fault_state.get(current, {}).get("frozen"):
                    current = None
        if not current:
            # Choose highest preference non-stale first
            non_stale = [c for c in candidates if not c[2]] or candidates
            non_stale.sort(key=lambda x: (x[1].get("pref", 0), x[1].get("opened_at", 0)), reverse=True)
            new_primary = non_stale[0][0] if non_stale else None
            prev = grp.get("primary")
            grp["primary"] = new_primary
            if new_primary and new_primary != prev:
                self.logger.info(f"[MPATH] Promoted {new_primary} as primary for {peer_key}")
            return new_primary
        # If current exists and preemptive enabled, consider higher preference non-stale candidate
        if self.mpath_preemptive_promote and self.mpath_strategy == "best_pref":
            try:
                cur_meta = grp["conns"].get(current)
                cur_pref = int(cur_meta.get("pref", 0)) if cur_meta else 0
                better = [c for c in candidates if not c[2] and int(c[1].get("pref", 0)) > cur_pref]
                if better:
                    # Pick best among better
                    better.sort(key=lambda x: (int(x[1].get("pref", 0)), x[1].get("opened_at", 0)), reverse=True)
                    new_best = better[0]
                    new_id = new_best[0]
                    new_pref = int(new_best[1].get("pref", 0))
                    if new_id != current:
                        grp["primary"] = new_id
                        self.logger.info(
                            f"[MPATH] Preemptive promote {new_id} over {current} (pref {new_pref}>{cur_pref}) for {peer_key}"
                        )
                        return new_id
            except Exception:  # justification: local session map cleanup is best-effort on pump exit
                pass
        return grp.get("primary")

    async def _mpath_failover_loop(self):
        """Background task periodically evaluating multipath primary health.

        Runs every ``mpath_failover_check_sec`` seconds (if configured) and
        checks each group for a stale or missing primary. Promotion is attempted
        only when an alternative non‑stale candidate exists to avoid churn. The
        loop also applies optional preemptive preference based promotion.
        """
        if self.mpath_failover_check_sec <= 0:
            return
        try:
            await asyncio.sleep(1.0)
            while not self._stop_event.is_set():
                try:
                    now = time.time()
                    # Use an effective stale window that respects heartbeat cadence to avoid false stale flags
                    effective_stale_sec = self.mpath_primary_stale_sec
                    try:
                        hb_window = self.heartbeat_interval * 2.5
                        if hb_window > effective_stale_sec:
                            effective_stale_sec = hb_window
                    except Exception:
                        pass
                    stale_cutoff = now - effective_stale_sec if effective_stale_sec > 0 else None
                    for key, grp in list(self._mpath_groups.items()):
                        # Hard idle pruning first: close any connection idle beyond configured TTL
                        try:
                            ttl_sec = getattr(self, "mpath_neighbor_idle_drop_sec", 0) or 0
                        except Exception:
                            ttl_sec = 0
                        if ttl_sec > 0:
                            # Per-group throttle map for TTL logs
                            if "_ttl_pruned" not in grp:
                                grp["_ttl_pruned"] = {}
                            for cid, m in list(grp.get("conns", {}).items()):
                                try:
                                    hb_c = self._hb_state.get(cid) or {}
                                    last_ack_c = hb_c.get("last_ack_ts") or 0
                                    last_activity_c = max(m.get("last_rx_seen") or m.get("last_seen") or 0, last_ack_c)
                                    if last_activity_c and (now - last_activity_c) >= ttl_sec:
                                        # Log once per connection
                                        last_logged = grp["_ttl_pruned"].get(cid, 0)
                                        if now - last_logged >= ttl_sec:
                                            self.logger.warning(
                                                f"[MPATH] Idle TTL exceeded ({ttl_sec:.0f}s) for {cid}; dropping neighbor"
                                            )
                                            grp["_ttl_pruned"][cid] = now
                                        # Immediately remove from group to prevent further selection/log spam
                                        try:
                                            if cid in grp["conns"]:
                                                grp["conns"].pop(cid, None)
                                            if grp.get("primary") == cid:
                                                grp["primary"] = next(iter(grp["conns"].keys()), None)
                                            if not grp["conns"]:
                                                self._mpath_groups.pop(key, None)
                                        except Exception:
                                            pass
                                        # Close asynchronously to clean up transport and state
                                        asyncio.create_task(self._close_connection(cid))
                                        # Skip further processing for this cid in this iteration
                                except Exception:
                                    # Best-effort pruning; ignore per-connection errors
                                    pass
                        primary = grp.get("primary")
                        if not primary:
                            self._select_mpath_connection(key)
                            continue
                        meta = grp["conns"].get(primary)
                        if not meta:
                            self._select_mpath_connection(key)
                            continue
                        # Initialize last change time metadata
                        if "last_primary_change" not in grp:
                            grp["last_primary_change"] = meta.get("opened_at") or now
                        # Determine last activity using both generic activity and heartbeat ACKs
                        hb = self._hb_state.get(primary) or {}
                        last_ack = hb.get("last_ack_ts") or 0
                        last_activity = max(meta.get("last_rx_seen") or meta.get("last_seen") or 0, last_ack)
                        if stale_cutoff is not None and last_activity < stale_cutoff:
                            # Before logging/promotion attempt, ensure there exists at least one non-stale alternative
                            alternatives = []
                            for cid, m in grp["conns"].items():
                                if cid == primary:
                                    continue
                                hb_c = self._hb_state.get(cid) or {}
                                last_ack_c = hb_c.get("last_ack_ts") or 0
                                ls = max(m.get("last_rx_seen") or m.get("last_seen") or 0, last_ack_c)
                                if stale_cutoff is not None and ls < stale_cutoff:
                                    continue
                                alternatives.append(cid)
                            if not alternatives:
                                # Throttle repeated stale warnings if no alternative (only every 3 * check interval)
                                last_warn = grp.get("_last_stale_log_ts") or 0
                                if now - last_warn > (self.mpath_failover_check_sec * 3):
                                    self.logger.debug(
                                        f"[MPATH] Primary {primary} appears stale for {key} but no alternative path available"
                                    )
                                    grp["_last_stale_log_ts"] = now
                                # Do not demote; keep primary until alternative appears
                            else:
                                last_change = grp.get("last_primary_change", 0)
                                # Throttle primary churn: avoid re-promoting if last change very recent (< failover interval)
                                if now - last_change < (self.mpath_failover_check_sec * 1.5):
                                    # Skip rapid churn
                                    continue
                                self.logger.warning(f"[MPATH] Primary {primary} stale for {key}; attempting promotion")
                                prev = primary
                                new_primary = self._select_mpath_connection(key)
                                if new_primary and new_primary != prev:
                                    grp["last_primary_change"] = now
                        elif self.mpath_preemptive_promote and self.mpath_strategy == "best_pref":
                            # Scan for better non-stale preference
                            try:
                                cur_pref = int(meta.get("pref", 0))
                                better = []
                                for cid, m in grp["conns"].items():
                                    if cid == primary:
                                        continue
                                    hb_c = self._hb_state.get(cid) or {}
                                    last_ack_c = hb_c.get("last_ack_ts") or 0
                                    ls = max(m.get("last_rx_seen") or m.get("last_seen") or 0, last_ack_c)
                                    if stale_cutoff is not None and ls < stale_cutoff:
                                        continue
                                    p = int(m.get("pref", 0))
                                    if p > cur_pref:
                                        better.append((cid, p, m.get("opened_at", 0)))
                                if better:
                                    better.sort(key=lambda x: (x[1], x[2]), reverse=True)
                                    new_id, new_pref, _ = better[0]
                                    if new_id != primary:
                                        # Throttle preemptive churn as well
                                        last_change = grp.get("last_primary_change", 0)
                                        if now - last_change >= (self.mpath_failover_check_sec * 1.0):
                                            grp["primary"] = new_id
                                            grp["last_primary_change"] = now
                                            self.logger.info(
                                                f"[MPATH] Preemptive promote {new_id} over {primary} (pref {new_pref}>{cur_pref}) for {key}"
                                            )
                            except Exception:  # justification: preemptive scan is advisory; avoid loop disruption
                                pass
                except Exception:  # justification: group evaluation best-effort; continue scanning
                    pass
                await asyncio.sleep(self.mpath_failover_check_sec)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.debug(f"mpath failover loop exited with error: {e}", exc_info=True)

    async def _send_control_mpath(self, peer_key: str, payload: str) -> bool:
        """Send a control frame over the currently selected path for a peer.

        Selects (and possibly promotes) an active connection for the given
        multipath group and transmits a control frame with an auto‑incremented
        sequence number.

        Args:
            peer_key: Multipath group key identifying the logical peer.
            payload: ASCII control payload (without framing characters).

        Returns:
            True if a frame was successfully queued to the OS, else False.
        """
        conn_id = self._select_mpath_connection(peer_key)
        if not conn_id:
            return False
        conn = self.connections.get(conn_id)
        if not conn:
            return False
        writer = conn.get("writer")
        if not isinstance(writer, asyncio.StreamWriter):
            return False
        try:
            seq = self._next_frame_seq(conn_id)
            frame = self.proto.create_control_frame(0, seq, payload)
            await self._send_protocol_frame(writer, frame)
            return True
        except Exception as e:
            self.logger.debug(f"Multipath control send failed via {conn_id}: {e}", exc_info=True)
            return False

    async def _send_stream_open_mpath(self, peer_key: str, stream_id: int, port_name: str) -> bool:
        """Open a stream over the selected path for the peer group."""
        conn_id = self._select_mpath_connection(peer_key)
        if not conn_id:
            return False
        conn = self.connections.get(conn_id)
        if not conn:
            return False
        writer = conn.get("writer")
        if not isinstance(writer, asyncio.StreamWriter):
            return False
        try:
            seq = self._next_frame_seq(conn_id)
            frame = self.proto.create_stream_open_frame(stream_id, seq, port_name)
            await self._send_protocol_frame(writer, frame)
            return True
        except Exception as e:
            self.logger.debug(f"Multipath OPEN send failed via {conn_id}: {e}", exc_info=True)
            return False

    async def _send_stream_close_mpath(self, peer_key: str, stream_id: int, reason: str = "") -> bool:
        """Close a stream over the selected path for the peer group."""
        conn_id = self._select_mpath_connection(peer_key)
        if not conn_id:
            return False
        conn = self.connections.get(conn_id)
        if not conn:
            return False
        writer = conn.get("writer")
        if not isinstance(writer, asyncio.StreamWriter):
            return False
        try:
            seq = self._next_frame_seq(conn_id)
            frame = self.proto.create_stream_close_frame(stream_id, seq, reason or "client_disconnect")
            await self._send_protocol_frame(writer, frame)
            return True
        except Exception as e:
            self.logger.debug(f"Multipath CLOSE send failed via {conn_id}: {e}", exc_info=True)
            return False

    async def _send_data_mpath(self, peer_key: str, stream_id: int, data: bytes) -> bool:
        """Send a data frame for a stream via the selected multipath connection.

        Args:
            peer_key: Multipath group key identifying the logical peer.
            stream_id: Logical stream identifier within the protocol session.
            data: Raw payload bytes to encapsulate in a DATA frame.

        Returns:
            True on success, False if no eligible path or an error occurred.
        """
        conn_id = self._select_mpath_connection(peer_key)
        if not conn_id:
            return False
        conn = self.connections.get(conn_id)
        if not conn:
            return False
        writer = conn.get("writer")
        if not isinstance(writer, asyncio.StreamWriter):
            return False
        try:
            # Allocate peer-level sequence for DATA frames
            seq = self._next_peer_seq(peer_key)
            frame = self.proto.create_data_frame(stream_id, seq, data)
            try:
                self._peer_bytes_tx[peer_key] = self._peer_bytes_tx.get(peer_key, 0) + len(data)
            except Exception:
                pass
            await self._send_protocol_frame(writer, frame)
            # Track for retransmission under peer_key
            try:
                if peer_key not in self._peer_sendbuf:
                    self._peer_sendbuf[peer_key] = {}
                self._peer_sendbuf[peer_key][seq] = (conn_id, stream_id, data, time.time())
            except Exception:
                pass
            return True
        except Exception as e:
            self.logger.debug(f"Multipath data send failed via {conn_id}: {e}", exc_info=True)
            return False

    def _next_peer_seq(self, peer_key: str) -> int:
        seq = self._peer_tx_seq.get(peer_key, 1)
        self._peer_tx_seq[peer_key] = seq + 1
        if self._peer_tx_seq[peer_key] >= (1 << 63):
            self._peer_tx_seq[peer_key] = 1
        return seq

    async def _handle_inbound_data(self, conn_id: str, stream_id: int, data: bytes, seq: int) -> None:
        """Enforce in-order delivery per peer by buffering out-of-order frames.

        Args:
            conn_id: Source connection id
            stream_id: Stream id for the frame
            data: Payload
            seq: Sender-assigned DATA sequence number
        """
        try:
            peer_key = self._derive_peer_key_from_conn_id(conn_id)
            st = self._peer_rx_state.setdefault(peer_key, {"expected": 1, "buffer": {}})
            expected = int(st.get("expected", 1))
            buf: Dict[int, Tuple[int, bytes]] = st["buffer"]
            # Duplicate or already delivered
            if seq < expected:
                return
            if seq == expected:
                # deliver now
                try:
                    self._peer_bytes_rx[peer_key] = self._peer_bytes_rx.get(peer_key, 0) + len(data)
                except Exception:
                    pass
                await self._route_data_frame(conn_id, stream_id, data, seq)
                expected += 1
                # drain contiguous buffered
                while expected in buf:
                    sid2, data2 = buf.pop(expected)
                    try:
                        self._peer_bytes_rx[peer_key] = self._peer_bytes_rx.get(peer_key, 0) + len(data2)
                    except Exception:
                        pass
                    await self._route_data_frame(conn_id, sid2, data2, expected)
                    expected += 1
                st["expected"] = expected
                return
            # seq > expected: buffer and wait
            try:
                self._peer_bytes_rx[peer_key] = self._peer_bytes_rx.get(peer_key, 0) + len(data)
            except Exception:
                pass
            buf[seq] = (stream_id, data)
        except Exception as e:
            self.logger.debug(f"Inbound order handler error on {conn_id}:{stream_id} seq={seq}: {e}", exc_info=True)

    async def _retx_loop(self):
        """Background retransmission loop for unacked DATA frames (peer-level).

        Scans the peer send buffers and resends frames that have not been
        acknowledged within the current RTO. Uses a simple capped backoff.
        """
        try:
            await asyncio.sleep(1.0)
            rto = max(0.15, (self.heartbeat_interval or 0.3) / 4)
            while not self._stop_event.is_set():
                now = time.time()
                for peer_key, buf in list(self._peer_sendbuf.items()):
                    for seq, (orig_cid, stream_id, data, ts) in list(buf.items()):
                        if now - ts < rto:
                            continue
                        # resend via current primary
                        cid = self._select_mpath_connection(peer_key)
                        if not cid:
                            continue
                        conn = self.connections.get(cid)
                        if not conn:
                            continue
                        writer = conn.get("writer")
                        if not isinstance(writer, asyncio.StreamWriter):
                            continue
                        try:
                            # reuse same sequence number for idempotency of ACK tracking
                            frame = self.proto.create_data_frame(stream_id, seq, data)
                            await self._send_protocol_frame(writer, frame)
                            buf[seq] = (cid, stream_id, data, now)
                            try:
                                self._peer_retx_count[peer_key] = self._peer_retx_count.get(peer_key, 0) + 1
                                self._peer_bytes_tx[peer_key] = self._peer_bytes_tx.get(peer_key, 0) + len(data)
                            except Exception:
                                pass
                            self.logger.debug(f"[RETX] Resent seq={seq} sid={stream_id} via {cid} for {peer_key}")
                        except Exception:
                            pass
                # increase/decrease rto mildly based on HB
                try:
                    rtt_candidates = []
                    for st in self._hb_state.values():
                        if st.get("rtt_ms"):
                            rtt_candidates.append(st["rtt_ms"])
                    if rtt_candidates:
                        avg_rtt = max(1, sum(rtt_candidates) // max(1, len(rtt_candidates))) / 1000.0
                        rto = min(self.retx_max_ms / 1000.0, max(self.retx_initial_ms / 1000.0, avg_rtt * 2.5))
                except Exception:
                    pass
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.debug(f"Retransmission loop error: {e}", exc_info=True)

    async def _handle_ports_federated(self, conn_id: str, payload: str):
        """Handle a PORTS:FEDERATED control payload and register remote ports.

        The payload is a multi‑line block beginning with ``PORTS:FEDERATED:``
        followed by one JSON object per line describing a federated port and
        terminated by ``END:PORTS``. Each JSON object is deserialized and passed
        to :meth:`_register_remote_port_from_dict`.

        Args:
            conn_id: Connection delivering the federated port list.
            payload: Raw ASCII control payload body (already decoded).
        """
        try:
            lines = payload.split("\n")
            if not lines or not lines[0].startswith("PORTS:FEDERATED:"):
                return
            # Collect JSON lines until END:PORTS
            port_lines: List[str] = []
            for line in lines[1:]:
                if line.strip() == "END:PORTS":
                    break
                if line.strip():
                    port_lines.append(line.strip())
            import json

            ports: List[Dict[str, Any]] = [json.loads(s) for s in port_lines]

            # Create and register proxies; collect seen names for removal diff
            peer_key = self._derive_peer_key_from_conn_id(conn_id)
            seen_names: Set[str] = set()
            for pd in ports:
                try:
                    # Apply accept filters (per-connection overrides honored)
                    if not self._allow_accept_port_for_conn(conn_id, pd):
                        continue
                    name = pd.get("name") or pd.get("original_name")
                    if isinstance(name, str):
                        seen_names.add(name)
                    await self._register_remote_port_from_dict(conn_id, pd)
                except Exception as e:
                    # Log with traceback; malformed entries should not abort whole batch
                    self.logger.warning(f"Failed to register federated port {pd.get('name')}: {e}", exc_info=True)
            self.logger.info(f"[{conn_id}] Registered {len(ports)} federated ports")

            # Remove proxies for this peer that are no longer advertised
            try:
                peer_proxies = self._peer_proxies.get(peer_key, {})
                stale = [pname for pname in list(peer_proxies.keys()) if pname not in seen_names]
                for pname in stale:
                    proxy = peer_proxies.pop(pname, None)
                    if proxy is None:
                        continue
                    try:
                        # Unregister from PortManager if this is the canonical registered port
                        if hasattr(self, "main_port_manager") and self.main_port_manager:
                            pm = self.main_port_manager
                            pobj = safe_get_port(pm, pname)
                            # Best-effort: ensure it's our proxy before removal
                            if pobj is proxy:
                                try:
                                    # Use PortManager API if it exposes an unregister; else direct pop
                                    if hasattr(pm, "unregister_federated_port"):
                                        await pm.unregister_federated_port(pname)  # type: ignore[attr-defined]
                                    else:
                                        getattr(pm, "ports", {}).pop(pname, None)
                                    self.logger.info(f"Unregistered stale federated port: {pname}")
                                except Exception:
                                    getattr(pm, "ports", {}).pop(pname, None)
                                    self.logger.info(f"Unregistered stale federated port: {pname}")
                    except Exception:
                        pass
                    try:
                        if hasattr(proxy, "disconnect"):
                            await proxy.disconnect()
                    except Exception:
                        pass
                # Proxies changed for this peer group; refresh per-connection mapping
                try:
                    self._refresh_conn_proxies()
                except Exception:
                    pass
            except Exception as e:
                self.logger.debug(f"Stale proxy purge failed for {peer_key}: {e}", exc_info=True)
        except Exception as e:
            self.logger.error(f"Error handling PORTS:FEDERATED: {e}", exc_info=True)

    async def _route_data_frame(self, conn_id: str, stream_id: int, data: bytes, seq: Optional[int] = None):
        """Route inbound DATA frame payload to a remote proxy or local port.

        Resolution order:
        1. Active remote proxy stream mapping (federated port).
        2. Local session mapping (server initiated stream to a local port).
        3. Drop with debug logging if unresolved.

        Args:
            conn_id: Connection identifier delivering the frame.
            stream_id: Logical stream id within the connection.
            data: Raw payload bytes.
            seq: Optional sequence number (for diagnostics only).
        """
        try:
            peer_key = self._derive_peer_key_from_conn_id(conn_id)
            proxy = self._session_map.get(peer_key, {}).get(stream_id)
            if proxy and hasattr(proxy, "trigger_data_received"):
                self.logger.info(f"[{conn_id}] ROUTE D->proxy sid={stream_id} bytes={len(data)}")
                await proxy.trigger_data_received(data)
                return
            # Check for local session mapping (server-initiated stream targeting local port)
            port_name = self._local_session_map.get(peer_key, {}).get(stream_id)
            if port_name and hasattr(self, "main_port_manager") and self.main_port_manager:
                try:
                    # Write to local port via port manager (unified path supports generic writes)
                    if seq is not None:
                        self.logger.debug(
                            f"[{conn_id}] ROUTE D->local port={port_name} sid={stream_id} bytes={len(data)} seq={seq}"
                        )
                    else:
                        self.logger.debug(f"[{conn_id}] ROUTE D->local port={port_name} sid={stream_id} bytes={len(data)}")
                    await self.main_port_manager.write_to_port(port_name, data, client_id=f"fed:{peer_key}:{stream_id}")
                except Exception as e:
                    self.logger.error(
                        f"Error writing to local port {port_name} from {conn_id}:{stream_id}: {e}",
                        exc_info=True,
                    )
            else:
                self.logger.debug(f"[{conn_id}] No mapping for stream {stream_id}; dropping {len(data)} bytes")
        except Exception as e:
            self.logger.error(f"Error routing data frame on {conn_id}:{stream_id}: {e}", exc_info=True)

    async def _pump_local_port_to_remote(self, peer_key: str, stream_id: int, port_name: str):
        """Continuously forward data from a local port to the remote stream.

        This coroutine runs while the session mapping remains intact and the
        adapter is not stopping. It polls the ``main_port_manager`` for new data
        (non‑blocking) and sends DATA frames upstream. On termination the local
        mapping for the stream is removed.

        Args:
            conn_id: Connection identifier carrying the federated session.
            stream_id: Logical stream id targeting a local port consumer.
            port_name: Name of the local port to read from.
        """
        try:
            # Send via currently selected path dynamically
            # Simple loop until mapping is removed or connection closes
            while (
                peer_key in self._local_session_map
                and self._local_session_map[peer_key].get(stream_id) == port_name
                and not self._stop_event.is_set()
            ):
                try:
                    if not hasattr(self, "main_port_manager") or not self.main_port_manager:
                        await asyncio.sleep(0.2)
                        continue
                    data = await self.main_port_manager.get_port_data(port_name)
                    if data:
                        self.logger.debug(f"[{peer_key}] PUMP local->{port_name} sid={stream_id} bytes={len(data)}")
                        await self._send_data_mpath(peer_key, stream_id, data)
                    else:
                        # No data available right now; avoid busy loop
                        await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    # Log and continue
                    self.logger.debug(f"Pump error for {port_name} on {peer_key}:{stream_id}: {e}", exc_info=True)
                    await asyncio.sleep(0.1)
        finally:
            # Cleanup mapping on exit
            try:
                if peer_key in self._local_session_map:
                    self._local_session_map[peer_key].pop(stream_id, None)
            except Exception:  # justification: local session map cleanup is best-effort on pump exit
                pass

    async def _register_remote_port_from_dict(self, conn_id: str, pd: Dict[str, Any]):
        """Create/register a RemotePortProxy for a federated port definition.

        Attempts to reuse an existing proxy with the same name + origin server
        (allowing seamless reconnection) before creating a new proxy. Reused
        proxies have their client streams re‑opened where possible.

        Args:
            conn_id: Connection providing the federated port description.
            pd: Parsed JSON dict describing the port (adapter_type, name, limits, etc.).
        """
        from ...common.federation_types import (
            FederationType,
            PortMetadata,
            ServerInfo,
        )

        # Build origin server info (V2 expects full object)
        origin_field = pd.get("origin_server")
        if isinstance(origin_field, dict):
            try:
                server_info = ServerInfo(
                    server_id=str(origin_field.get("server_id")),
                    hostname=str(origin_field.get("hostname", "remote")),
                    port=int(origin_field.get("port", 0) or 0),
                    server_type=ServerType(str(origin_field.get("server_type", "leaf"))),
                    description=str(origin_field.get("description", "")),
                )
            except Exception:
                # Fallback if enum parsing fails
                server_info = ServerInfo(
                    server_id=str(origin_field.get("server_id", "remote")),
                    hostname=str(origin_field.get("hostname", "remote")),
                    port=int(origin_field.get("port", 0) or 0),
                    server_type=ServerType.LEAF,
                    description=str(origin_field.get("description", "")),
                )
        else:
            # Legacy id string (not required per request, but allows resilience)
            origin_id = str(origin_field or "remote")
            server_info = ServerInfo(
                server_id=origin_id,
                hostname="remote",
                port=0,
                server_type=ServerType.LEAF,
            )
        name = pd.get("name") or pd.get("original_name") or "remote_port"
        desc = pd.get("description", f"Remote port {name}")
        adapter_type = pd.get("adapter_type", "remote_muxcon")
        max_rw = int(pd.get("max_rw_users", 1) or 1)
        status = pd.get("status", "connected")
        # Server chain if provided (prefer V2 detailed objects)
        server_chain = [server_info]
        sc_info = pd.get("server_chain_info")
        if isinstance(sc_info, list) and sc_info:
            try:
                # Build from detailed objects, ensure origin first
                for hop in sc_info:
                    if not isinstance(hop, dict):
                        continue
                    sid = str(hop.get("server_id"))
                    if sid == server_info.server_id:
                        # origin already at index 0
                        continue
                    try:
                        stype = ServerType(str(hop.get("server_type", "relay")))
                    except Exception:
                        stype = ServerType.RELAY
                    server_chain.append(
                        ServerInfo(
                            server_id=sid,
                            hostname=str(hop.get("hostname", "remote")),
                            port=int(hop.get("port", 0) or 0),
                            server_type=stype,
                            description=str(hop.get("description", "")),
                        )
                    )
            except Exception:
                pass
        else:
            # Optional legacy string server ids (not required per request)
            try:
                chain_ids = pd.get("server_chain") or []
                origin_id = server_info.server_id
                others = [sid for sid in chain_ids if str(sid) != str(origin_id)]
                for sid in others:
                    server_chain.append(
                        ServerInfo(
                            server_id=str(sid),
                            hostname="remote",
                            port=0,
                            server_type=ServerType.RELAY,
                        )
                    )
            except Exception:
                pass
        # Optional serial/line-status details if peer provides them
        serial_cfg = None
        try:
            sc = pd.get("serial_config")
            if isinstance(sc, dict):
                serial_cfg = {
                    "device": sc.get("device"),
                    "baudrate": sc.get("baudrate"),
                    "bytesize": sc.get("bytesize"),
                    "parity": sc.get("parity"),
                    "stopbits": sc.get("stopbits"),
                    "flow_control": sc.get("flow_control"),
                }
        except Exception:
            serial_cfg = None
        line_status = None
        try:
            ls = pd.get("line_status")
            if isinstance(ls, dict):
                line_status = ls
        except Exception:
            line_status = None

        metadata = PortMetadata(
            name=name,
            original_name=name,
            description=desc,
            adapter_type=adapter_type,
            origin_server=server_info,
            server_chain=server_chain,
            status=status,
            max_rw_users=max_rw,
            federation_type=FederationType.PULL,
            serial_config=serial_cfg,
            line_status=line_status,
        )

        # Reuse existing proxy if present to preserve clients and sessions
        reused = False
        try:
            if hasattr(self, "main_port_manager") and self.main_port_manager:
                existing = None
                try:
                    existing = self.main_port_manager.get_port(name)
                except Exception:
                    try:
                        existing = getattr(self.main_port_manager, "ports", {}).get(name)
                    except Exception:
                        existing = None
                if existing is not None and hasattr(existing, "remote_port_name"):
                    # Update existing proxy in-place
                    prev_conn = getattr(existing, "connection_id", None)
                    peer_key = self._derive_peer_key_from_conn_id(conn_id)
                    # Re-bind proxy to this new adapter instance so writes go over live connections
                    try:
                        setattr(existing, "adapter", self)
                    except Exception:
                        pass
                    setattr(existing, "connection_id", peer_key)
                    setattr(existing, "metadata", metadata)
                    setattr(existing, "server_adapter", self)
                    if hasattr(existing, "is_connected"):
                        existing.is_connected = True
                    # Clear old client sessions and reopen on new connection
                    try:
                        if hasattr(existing, "close_all_streams"):
                            await existing.close_all_streams()
                    except Exception:  # justification: stream cleanup on reuse is best-effort
                        pass
                    # Notify clients that link is restored (de-duplicated)
                    try:
                        # Build machine-readable source path server/port
                        src_server = None
                        try:
                            origin = getattr(metadata, "origin_server", None)
                            src_server = getattr(origin, "server_id", None)
                        except Exception:  # justification: metadata introspection is optional
                            src_server = None
                        src_server = src_server or "unknown"
                        src_path = f"{src_server}::{name}"
                        self._emit_link_notice_once(existing, "restored", src_path)
                    except Exception:  # justification: notification is advisory; ignore queueing failures
                        pass
                    # Attempt to reopen streams for currently connected clients
                    try:
                        for c in list(getattr(existing, "connected_clients", []) or []):
                            cid = c.get("client_id") if isinstance(c, dict) else None
                            if cid and hasattr(existing, "open_stream_for_client"):
                                await existing.open_stream_for_client(cid)
                    except Exception as e:
                        self.logger.debug(f"Failed to reopen streams for {name} on reconnect: {e}", exc_info=True)

                    # Update peer->proxy mapping
                    try:
                        peer_key = self._derive_peer_key_from_conn_id(conn_id)
                        if peer_key not in self._peer_proxies:
                            self._peer_proxies[peer_key] = {}
                        self._peer_proxies[peer_key][name] = existing
                        # Keep per-connection view updated when proxies change
                        try:
                            self._refresh_conn_proxies()
                        except Exception:
                            pass
                    except Exception:
                        pass
                    reused = True
        except Exception as e:
            self.logger.debug(f"Proxy reuse check failed for {name} on {conn_id}: {e}", exc_info=True)

        if reused:
            try:
                self._save_federated_cache()
            except Exception:
                pass
            return

        # Create remote proxy and register with port manager
        peer_key = self._derive_peer_key_from_conn_id(conn_id)
        proxy = self.RemotePortProxy(self, peer_key, name, metadata)
        # Provide back-reference for management/cleanup
        setattr(proxy, "server_adapter", self)
        if hasattr(self, "main_port_manager") and self.main_port_manager:
            # Idempotent: skip if a port with identical name & origin already present
            # Duplicate check (non-blocking): avoid re-entrant await of port manager list
            try:
                origin_id = None
                try:
                    origin = getattr(metadata, "origin_server", None)
                    origin_id = getattr(origin, "server_id", None)
                except Exception:  # justification: metadata introspection optional; default to origin_id=None
                    origin_id = None
                # Directly scan existing registered ports dictionary
                for existing_name, existing_port in list(self.main_port_manager.ports.items()):
                    if existing_name != name:
                        continue
                    try:
                        meta2 = getattr(existing_port, "metadata", None)
                        if meta2:
                            o2 = getattr(meta2, "origin_server", None)
                            sid2 = getattr(o2, "server_id", None)
                            if sid2 == origin_id:
                                self.logger.info(
                                    f"Duplicate federated port '{name}' from {origin_id}; skipping re-registration"
                                )
                                return
                    except Exception:  # justification: existing-port metadata introspection is optional
                        pass
            except Exception:  # justification: duplicate check is advisory; proceed to registration
                pass
            registered = await self.main_port_manager.register_federated_port(metadata, proxy)
            if registered:
                self.logger.info(f"Registered federated port: {registered}")
                try:
                    if peer_key not in self._peer_proxies:
                        self._peer_proxies[peer_key] = {}
                    self._peer_proxies[peer_key][name] = proxy
                    # Update per-connection mapping after new proxy registration
                    try:
                        self._refresh_conn_proxies()
                    except Exception:
                        pass
                    try:
                        self._save_federated_cache()
                    except Exception:
                        pass
                except Exception:
                    pass
        else:
            self.logger.warning("No main_port_manager set; cannot register federated ports")

    # --- Wire helpers: frame send/read ---

    async def _send_protocol_frame(self, writer: asyncio.StreamWriter, frame: bytes):
        """Write a fully formatted protocol frame to the stream with logging.

        Args:
            writer: Destination stream writer.
            frame: Pre-encoded frame bytes including trailing newline.
        """
        try:
            # Frame is already formatted as single chunk: #stream:TYPE:len:seq:payload\n
            # Build a safe header preview and extract the sequence number by
            # scanning for the 4th ':' to avoid including payload bytes.
            header_end = None
            colon_seen = 0
            for i, b in enumerate(frame):
                if b == ord(":"):
                    colon_seen += 1
                    if colon_seen == 4:
                        header_end = i
                        break
            if header_end is not None:
                header_bytes = frame[: header_end + 1]
                hdr_preview = header_bytes.decode("utf-8", errors="ignore")
                seq_val = None
                try:
                    parsed = self.proto.parse_frame_header(header_bytes)
                    if parsed:
                        _, _, _, seq_val = parsed
                except Exception:
                    pass
                if seq_val is not None:
                    self.logger.debug(f"TX frame: {hdr_preview} seq={seq_val}")
                else:
                    self.logger.debug(f"TX frame: {hdr_preview}")
            else:
                # Fallback preview if header couldn't be isolated
                frame_str = frame.decode("utf-8", errors="ignore")
                hdr_preview = frame_str.split("\n", 1)[0] if "\n" in frame_str else frame_str[:64]
                self.logger.debug(f"TX frame: {hdr_preview}")
            writer.write(frame)
            await writer.drain()
        except Exception as e:
            self.logger.error(f"Failed to send protocol frame: {e}", exc_info=True)

    async def _read_frame(self, reader: asyncio.StreamReader) -> Optional[Dict[str, Any]]:
        """Read one ASCII frame (header + payload + newline) from the stream.

        Returns a dict with parsed components or None on EOF / parse error.

        Args:
            reader: StreamReader tied to the TCP connection.

        Returns:
            Parsed frame dictionary or None.
        """
        try:
            header_bytes = b""
            colon_count = 0
            # Seek start '#'
            while True:
                bch = await reader.readexactly(1)
                if bch == b"#":
                    header_bytes = bch
                    break
                # Skip whitespace/newlines
                if bch in (b"\n", b"\r", b" ", b"\t"):
                    continue
                # Unexpected byte; continue searching
            # Need 4 colons for header format: #<sid>:<type>:<len>:<seq>:
            while colon_count < 4:
                bch = await reader.readexactly(1)
                header_bytes += bch
                if bch == b":":
                    colon_count += 1
            header = header_bytes.decode("ascii", errors="ignore")
            parsed = self.proto.parse_frame_header(header_bytes)
            if not parsed:
                return None
            stream_id, frame_type, payload_len, seq_val = parsed
            payload = await reader.readexactly(payload_len) if payload_len > 0 else b""
            # trailing newline
            _ = await reader.readexactly(1)
            frame_obj = {
                "stream_id": stream_id,
                "frame_type": frame_type,
                "payload_length": payload_len,
                "payload": payload,
                "seq": seq_val,
            }
            self.logger.debug(f"RX header parsed: sid={stream_id} type={frame_type} len={payload_len}")
            return frame_obj
        except asyncio.IncompleteReadError:
            return None
        except Exception as e:
            self.logger.error(f"Error reading frame: {e}", exc_info=True)
            return None

    def _map_session(self, peer_key: str, stream_id: int, proxy: Any):
        """Map a stream id for a peer group to a proxy object for routing."""
        if peer_key not in self._session_map:
            self._session_map[peer_key] = {}
        self._session_map[peer_key][stream_id] = proxy

    # --- Global frame sequencing ---
    def _next_frame_seq(self, conn_id: Optional[str] = None) -> int:
        """Return next sequence number.

        If a conn_id is provided and wire state has per-connection counter,
        allocate from that. Otherwise fall back to legacy global sequencing.
        Both wrap after 2^63.
        """
        if conn_id:
            st = self._wire_state.get(conn_id)
            if st is not None:
                seq = st.get("send_next", 1)
                st["send_next"] = seq + 1
                if st["send_next"] >= (1 << 63):
                    st["send_next"] = 1
                return seq
        seq = self._next_seq
        self._next_seq += 1
        if self._next_seq >= (1 << 63):
            self._next_seq = 1
        return seq

    # --- Contract-compliant RemotePortProxy (federated remote port) ---
    class RemotePortProxy:
        """Proxy for a federated remote port exposed by a peer server.

        This object represents a single remote port that lives on a federated
        OpenMux peer. It manages client-to-remote stream mapping, data sending,
        and delivery of inbound data to a registered callback or queue.

        Attributes:
            adapter: The parent adapter coordinating federation connections.
            connection_id: Identifier for the peer (peer_key across mpath).
            remote_port_name: The name of the remote port on the peer.
            metadata: Optional metadata describing the remote port.
            is_connected: Whether the underlying federation link is up.
            data_queue: Queue used when no callback is registered.
            data_callback: Optional callable/coroutine to receive inbound data.
            port_manager: Local PortManager reference, if attached.
            name: Public name of the remote port (same as `remote_port_name`).
            description: Human-readable port description.
            connected_clients: List of connected client records.
            max_read_write_users: Max number of concurrent RW clients allowed.
            state: Current `PortState` of the proxy.
        """

        state: PortState  # contract annotation
        is_connected: bool  # contract annotation

        def __init__(
            self,
            adapter: "UnifiedMuxConAdapter",
            conn_id: str,
            remote_port_name: str,
            metadata: Any,
        ):
            """Initialize a RemotePortProxy.

            Args:
                adapter: Parent `UnifiedMuxConAdapter` instance.
                conn_id: Federation connection identifier.
                remote_port_name: Name of the remote port on the peer.
                metadata: Optional port metadata from the peer.
            """
            self.adapter = adapter
            self.connection_id = conn_id
            self.remote_port_name = remote_port_name
            self.metadata = metadata
            self.is_connected = True
            self.data_queue: asyncio.Queue = asyncio.Queue()
            self.data_callback = None  # set by PortManager
            self.port_manager = None
            self._client_sessions: Dict[str, int] = {}
            self._next_session_id = 1
            self.logger = logging.getLogger(f"openmux.unified.remote_proxy.{remote_port_name}")

            # Surface required / expected attributes
            self.name = remote_port_name
            self.description = getattr(metadata, "description", f"Remote port {remote_port_name}")
            self.connected_clients: List[Dict[str, Any]] = []
            self.max_read_write_users: int = int(getattr(metadata, "max_rw_users", 5) or 5)
            self.state = PortState.ACTIVE
            # Offline cache support
            self.last_seen: float = time.time()
            # Track last emitted federated-link notice to prevent duplicates
            self._last_link_notice: Optional[str] = None  # one of {"stale","disconnected","restored"}
            self._last_link_notice_ts: float = 0.0

        def set_data_callback(self, callback):
            """Register a callback to receive inbound data.

            The callback may be a regular function or an async coroutine
            accepting a single `bytes` argument.

            Args:
                callback: Callable or coroutine function taking `bytes`.
            """
            self.data_callback = callback

        def set_port_manager(self, pm):
            """Attach the local PortManager for lifecycle integration.

            Args:
                pm: PortManager instance managing this proxy.
            """
            self.port_manager = pm

        def get_status(self) -> Dict[str, Any]:
            """Return a status snapshot of the remote port.

            Returns:
                Dict with basic fields such as name, description, connected
                state, client counts, adapter type, connection id and port name.
            """
            try:
                return {
                    "name": self.name,
                    "description": self.description,
                    "connected": self.is_connected,
                    "client_count": len(self.connected_clients),
                    "connected_clients": len(self.connected_clients),
                    "adapter_type": "remote_muxcon",
                    "remote_connection_id": self.connection_id,
                    "remote_port_name": self.remote_port_name,
                }
            except Exception:  # justification: status synthesis best-effort for UI; return minimal info
                return {
                    "name": getattr(self, "name", self.remote_port_name),
                    "connected": bool(getattr(self, "is_connected", True)),
                    "adapter_type": "remote_muxcon",
                }

        async def trigger_data_received(self, data: bytes):
            """Deliver inbound bytes to the callback or enqueue them.

            If a data callback is registered, it is invoked (awaited if
            coroutine) with the payload. Otherwise, the data is put on
            `data_queue` for later consumption.

            Args:
                data: Payload received for this remote port.
            """
            # Update last_seen on inbound activity
            try:
                self.last_seen = time.time()
            except Exception:
                pass
            if self.data_callback:
                try:
                    if asyncio.iscoroutinefunction(self.data_callback):
                        await self.data_callback(data)
                    else:
                        self.data_callback(data)
                except Exception as e:
                    self.logger.error(f"data_callback error: {e}", exc_info=True)
            else:
                try:
                    self.data_queue.put_nowait(data)
                except Exception:  # justification: advisory duplicate scan; ignore errors
                    pass

        async def write_data(self, data: bytes, client_id: Optional[str] = None) -> int:
            """Send data to the remote port, opening a stream if necessary.

            Args:
                data: Bytes to send to the remote port.
                client_id: Optional logical client identifier to use for
                    per-client stream mapping.

            Returns:
                Number of bytes accepted for sending.

            Raises:
                Exception: If the federation connection or writer is missing.
            """
            session_id = await self._ensure_session(client_id)
            self.logger.info(
                f"REMOTE PROXY WRITE port={self.remote_port_name} sid={session_id} bytes={len(data)} client={client_id}"
            )
            try:
                self.last_seen = time.time()
            except Exception:
                pass
            await self.adapter._send_data_mpath(self.connection_id, session_id, data)
            return len(data)

        async def _ensure_session(self, client_id: Optional[str]) -> int:
            """Ensure a remote stream exists for the given client.

            Creates and registers a new remote stream if no mapping exists,
            opens it on the wire, and returns the session id.

            Args:
                client_id: Optional logical client id; if omitted, a default
                    per-port key is used.

            Returns:
                The remote session id for this client.
            """
            key = client_id or f"default:{self.remote_port_name}"
            if key in self._client_sessions:
                return self._client_sessions[key]
            session_id = self._next_session_id
            self._next_session_id += 1
            self.logger.info(f"REMOTE PROXY OPEN stream sid={session_id} port={self.remote_port_name}")
            await self.adapter._send_stream_open_mpath(self.connection_id, session_id, self.remote_port_name)
            self.adapter._map_session(self.connection_id, session_id, self)
            self._client_sessions[key] = session_id
            return session_id

        async def open_stream_for_client(self, client_id: str) -> Optional[int]:
            """Open a remote stream for a specific client.

            Args:
                client_id: Logical client identifier.

            Returns:
                The session id if opened or already present, otherwise None on
                failure.
            """
            try:
                sid = await self._ensure_session(client_id)
                self.logger.info(f"Opened remote stream sid={sid} for client={client_id} on port={self.remote_port_name}")
                return sid
            except Exception as e:
                self.logger.error(f"Failed to open stream for client {client_id}: {e}", exc_info=True)
                return None

        async def close_stream_for_client(self, client_id: str) -> bool:
            """Close the remote stream associated with a specific client.

            Best-effort close; returns True if no stream existed or after
            successfully sending a close on the wire.

            Args:
                client_id: Logical client identifier.

            Returns:
                True if the stream no longer exists for the client; False only
                if an error prevented sending the close frame.
            """
            try:
                key = client_id or f"default:{self.remote_port_name}"
                if key not in self._client_sessions:
                    return True
                sid = self._client_sessions.pop(key)
                try:
                    if (
                        self.connection_id in self.adapter._session_map
                        and sid in self.adapter._session_map[self.connection_id]
                    ):
                        self.adapter._session_map[self.connection_id].pop(sid, None)
                except Exception:
                    pass
                await self.adapter._send_stream_close_mpath(self.connection_id, sid, "client_disconnect")
                self.logger.info(f"Closed remote stream sid={sid} for client={client_id} on port={self.remote_port_name}")
                return True
            except Exception as e:
                self.logger.error(f"Failed to close stream for client {client_id}: {e}", exc_info=True)
                return False

        async def close_all_streams(self) -> None:
            """Close all client-associated remote streams for this proxy.

            Sends best-effort close frames for each active remote session and
            clears local session mappings.
            """
            try:
                for key, sid in list(self._client_sessions.items()):
                    await self.adapter._send_stream_close_mpath(self.connection_id, sid, "proxy_disconnect")
                    try:
                        if (
                            self.connection_id in self.adapter._session_map
                            and sid in self.adapter._session_map[self.connection_id]
                        ):
                            self.adapter._session_map[self.connection_id].pop(sid, None)
                    except Exception:
                        pass
                self._client_sessions.clear()
            except Exception as e:
                self.logger.debug(f"close_all_streams error: {e}", exc_info=True)

        async def connect(self) -> bool:
            """Mark the proxy as connected and active.

            Returns:
                True when state is set successfully.
            """
            self.is_connected = True
            self.state = PortState.ACTIVE
            try:
                self.last_seen = time.time()
            except Exception:
                pass
            return True

        async def disconnect(self) -> None:
            """Tear down the proxy and close all remote streams.

            Sets state to DESTROYING, closes streams, then DESTROYED. Errors
            during cleanup are ignored.
            """
            try:
                self.is_connected = False
                self.state = PortState.DESTROYING
                await self.close_all_streams()
                self.state = PortState.DESTROYED
                # keep last_seen as-is to reflect last activity
            except Exception:  # justification: disconnect cleanup is best-effort; ignore
                pass

        async def start(self) -> bool:
            """Transition the proxy to an operational state.

            Returns:
                True after updating the state based on connectivity.
            """
            if self.is_connected:
                self.state = PortState.ACTIVE
            else:
                self.state = PortState.CREATING
            return True

        async def stop(self) -> None:
            """Stop the proxy by disconnecting and closing streams."""
            await self.disconnect()
