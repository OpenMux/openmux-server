"""
Port and port-manager facilities for OpenMux.

This module provides:

- `Port`: A lightweight, legacy-compatible port abstraction that wraps an
    underlying adapter and exposes a consistent API used by the console layer.
- `PortManager`: The central coordinator that creates, tracks, and mediates
    access to both legacy-style ports and unified-adapter ports (via wrappers),
    including federation-aware behaviors.

Docstrings follow Google style and call out noteworthy behavior, including
how unified adapters are wrapped to present a legacy interface surface.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Union, Callable
import inspect

from openmux.server.data_logger import DataLogger
class PortManager:
    """Manages ports across legacy and unified adapters."""

    def __init__(self, config: Union[List[Dict[str, Any]], Dict[str, Any]]):
        """Initialize the port manager (unified-only).

        The manager now tracks only unified adapter ports (and federated
        proxies). Legacy flat "ports" configurations and fallback adapters
        have been removed.
        """
        self.config = config
        self.ports = {}  # Port or UnifiedPortWrapper (single source of truth)
        self.unified_adapters = []
        self.logger = logging.getLogger("openmux.server.port_manager")
        # Subscribers receiving metadata/port updates (event-driven UI, etc.)
        # Listener signature: callable(port_name: str, changes: Optional[dict]) -> None | awaitable
        self._meta_listeners: List[Callable[[str, Optional[Dict[str, Any]]], Any]] = []

    # --- Metadata event subscription API ---
    def register_meta_listener(self, listener: Callable[[str, Optional[Dict[str, Any]]], Any]) -> None:
        """Register a listener for port metadata updates.

        The listener will be called with (port_name, changes) where changes is an
        optional dict describing the reason for the update (best-effort).
        """
        try:
            if listener not in self._meta_listeners:
                self._meta_listeners.append(listener)
        except Exception:
            # Non-fatal; listeners are best-effort
            pass

    def unregister_meta_listener(self, listener: Callable[[str, Optional[Dict[str, Any]]], Any]) -> None:
        """Unregister a previously registered meta listener."""
        try:
            if listener in self._meta_listeners:
                self._meta_listeners.remove(listener)
        except Exception:
            pass

    def notify_meta_updated(self, port_name: str, changes: Optional[Dict[str, Any]] = None) -> None:
        """Notify listeners that metadata for a port changed.

        Tolerates listener failures and supports async listeners via create_task.
        """
        try:
            loop = None
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop; fallback to get_event_loop (may raise in 3.11 if no loop set)
                try:
                    loop = asyncio.get_event_loop()
                except Exception:
                    loop = None
            for listener in list(self._meta_listeners):
                try:
                    if inspect.iscoroutinefunction(listener):
                        if loop:
                            loop.create_task(listener(port_name, changes))
                    else:
                        res = listener(port_name, changes)
                        # If sync returns awaitable, schedule it
                        if inspect.isawaitable(res) and loop:
                            loop.create_task(res)  # type: ignore[arg-type]
                except Exception:
                    # Ignore misbehaving listeners
                    continue
        except Exception:
            # Notification failures are non-fatal
            pass

    def set_unified_adapters(self, unified_adapters):
        """Install unified adapters and set back-references.

        Args:
            unified_adapters: Iterable of unified adapter instances. Each adapter
                will receive `main_port_manager` as a back-reference to report
                events and register ports at runtime.
        """
        self.unified_adapters = unified_adapters

        # Set a reference to the main port manager in each adapter for data callbacks
        for adapter in unified_adapters:
            adapter.main_port_manager = self


    def get_port(self, name: str) -> Optional[Any]:
        """Return a port by name, creating a unified wrapper on-demand.

        Args:
            name: Logical port name.

        Returns:
            A unified-wrapper instance if the name maps to a unified adapter
            port, or a previously cached port-like object. Returns None when not found.
        """
        # First check legacy ports
        if name in self.ports:
            return self.ports[name]

        # Then check unified adapter ports and create wrapper if needed
        for adapter in self.unified_adapters:
            if hasattr(adapter, "ports") and name in adapter.ports:
                unified_port = adapter.ports[name]
                wrapper = self._create_unified_port_wrapper(unified_port, adapter)
                # Cache in the single ports map for consistent lookup
                self.ports[name] = wrapper
                return wrapper

        return None

    def _ensure_unified_wrapper(self, port_name: str) -> bool:
        """Ensure a unified wrapper exists for `port_name`.

        This scans registered unified adapters for the named port and, if found,
        constructs a lightweight wrapper that mirrors the legacy `Port` shape.
        The wrapper is cached in `self.ports` so that existing code paths such as
        `port_exists()` and `add_client_to_port()` work unchanged.

        Args:
            port_name: The unified port to surface through a legacy-compatible
                wrapper.

        Returns:
            True if a wrapper or legacy port is present after the call; otherwise False.
        """
        # Already present (legacy port or previously created wrapper)
        if port_name in self.ports:
            return True

        for adapter in self.unified_adapters:
            try:
                if not hasattr(adapter, "ports") or not adapter.ports:
                    continue
                if port_name not in adapter.ports:
                    continue
                unified_port = adapter.ports[port_name]

                # Reuse the canonical wrapper factory and cache in ports
                wrapper = self._create_unified_port_wrapper(unified_port, adapter)
                self.ports[port_name] = wrapper
                self.logger.debug(f"Created unified port wrapper for {port_name} (adapter={adapter.name})")
                return True
            except Exception as e:
                # Justification: wrapper creation is best-effort; continue scanning other adapters
                self.logger.debug(
                    f"Wrapper creation failed for {port_name} on adapter {getattr(adapter, 'name', '?')}: {e}",
                    exc_info=True,
                )
                continue
        return False

    def _create_unified_port_wrapper(self, unified_port, adapter):
        """Create a legacy-compatible wrapper for a unified adapter port.

        Args:
            unified_port: The adapter-specific port object to expose.
            adapter: The unified adapter instance that owns the port.

        Returns:
            An object that mimics the legacy `Port` interface sufficiently for
            the console and manager layers.
        """

        # This is a simple compatibility wrapper that implements the Port interface
        # for unified adapter ports
        class UnifiedPortWrapper:
            def __init__(self, unified_port, adapter):
                self.name = unified_port.name
                self.unified_port = unified_port
                self.adapter = adapter
                self.logger = logging.getLogger(f"openmux.server.port_manager.unified_wrapper.{self.name}")

                _get_type = getattr(adapter, "get_adapter_type", None)
                adapter_type = _get_type() if callable(_get_type) else getattr(adapter, "adapter_type", "unknown")

                self.description = getattr(unified_port, "description", f"Unified {adapter_type} port")
                self.is_running = getattr(unified_port, "is_running", True)
                self.connected_clients = []
                self.max_read_write_users = getattr(unified_port, "max_read_write_users", 5)
                self.adapter_type = adapter_type
                self.loopback = str(adapter_type).lower() == "loopback"

                # Data queue: if underlying port already has one (loopback), reuse; else create
                existing_q = getattr(unified_port, "data_queue", None)
                if existing_q is not None:
                    self.data_queue = existing_q
                else:
                    self.data_queue = asyncio.Queue(maxsize=100)
                # Surface adapter-specific buffering hints so the manager can honor them
                self.always_buffer = bool(getattr(unified_port, "always_buffer", False))
                self.drop_oldest_on_full = bool(getattr(unified_port, "drop_oldest_on_full", False))

            def get_status(self):
                """Return a status snapshot for this unified wrapper."""
                status = {
                    "name": self.name,
                    "description": self.description,
                    "adapter": self.adapter_type,
                    "state": (self.unified_port.state.value if hasattr(self.unified_port, "state") else "active"),
                    "is_running": self.is_running,
                    # When unified port exposes connection state (e.g., Serial), surface it explicitly
                    "connected": bool(getattr(self.unified_port, "is_connected", self.is_running)),
                    "connected_clients": len(self.connected_clients),
                    "max_read_write_users": self.max_read_write_users,
                }
                # Include adapter-provided snapshot details when available
                snapshot = getattr(self.unified_port, "get_status_snapshot", None)
                if callable(snapshot):
                    try:
                        extra = snapshot() or {}
                        if isinstance(extra, dict):
                            status.update(extra)
                    except Exception:
                        self.logger.debug(
                            f"Unified port {self.name} get_status_snapshot failed",
                            exc_info=True,
                        )
                elif hasattr(self.unified_port, "status_snapshot"):
                    extra = getattr(self.unified_port, "status_snapshot")
                    if isinstance(extra, dict):
                        status.update(extra)
                return status

            def add_client(self, client: Any) -> None:
                """Register a client and trigger the adapter hook if present."""
                if client not in self.connected_clients:
                    was_empty = len(self.connected_clients) == 0
                    self.connected_clients.append(client)
                    if hasattr(self.unified_port, "on_client_count_changed"):
                        try:
                            self.unified_port.on_client_count_changed(len(self.connected_clients))
                        except Exception:
                            # justification: hook failure should not block port add; log and continue
                            self.logger.error(
                                f"on_client_count_changed hook error (add_client) for unified port {self.name}",
                                exc_info=True,
                            )
                    # On first client, clear any buffered data to ensure empty start
                    if was_empty:
                        try:
                            while True:
                                self.data_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass

            def remove_client(self, client: Any) -> None:
                """Unregister a client and trigger the adapter hook if present."""
                if client in self.connected_clients:
                    self.connected_clients.remove(client)
                    if hasattr(self.unified_port, "on_client_count_changed"):
                        try:
                            self.unified_port.on_client_count_changed(len(self.connected_clients))
                        except Exception:
                            # justification: hook failure should not block client removal; log and continue
                            self.logger.error(
                                f"on_client_count_changed hook error (remove_client) for unified port {self.name}",
                                exc_info=True,
                            )
                    # When last client disconnects, clear any remaining buffered data
                    if len(self.connected_clients) == 0:
                        try:
                            while True:
                                self.data_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass

            async def write_data(self, data: bytes) -> bool:
                """Write to the underlying unified port using best available path."""
                # Prefer adapter unified write_to_port if available
                if hasattr(self.adapter, "write_to_port"):
                    try:
                        res = await self.adapter.write_to_port(self.name, data)
                        if isinstance(res, bool):
                            return res
                        if isinstance(res, int):
                            return res > 0
                        return True
                    except Exception:
                        # justification: unified write error already contained; log traceback for diagnosis
                        self.logger.error(f"Unified write_to_port error for {self.name}", exc_info=True)
                        return False
                # Fallback: underlying loopback port writer
                writer = getattr(self.unified_port, "_writer", None)
                if writer:
                    try:
                        if hasattr(writer, "write") and callable(getattr(writer, "write")):
                            written = await writer.write(data)
                            return written > 0
                        if hasattr(writer, "async_write") and callable(getattr(writer, "async_write")):
                            written = await writer.async_write(data)
                            return written > 0
                    except Exception:
                        # justification: fallback writer failure non-fatal; log traceback
                        self.logger.error(f"Unified underlying writer error for {self.name}", exc_info=True)
                        return False
                return False

            async def disconnect(self) -> None:
                """Invoke the unified port's stop/disconnect hooks if available."""
                if hasattr(self.unified_port, "stop"):
                    try:
                        await self.unified_port.stop()
                    except Exception:
                        # justification: stop hook failure should not prevent broader shutdown; log
                        self.logger.error(f"Unified port stop() error for {self.name}", exc_info=True)
                elif hasattr(self.unified_port, "disconnect"):
                    try:
                        await self.unified_port.disconnect()
                    except Exception:
                        # justification: disconnect hook failure should not block cleanup; log
                        self.logger.error(f"Unified port disconnect() error for {self.name}", exc_info=True)

        return UnifiedPortWrapper(unified_port, adapter)

    def list_ports(self) -> List[Dict[str, Any]]:
        """List legacy ports and their status snapshots.

        Returns:
            A list of dictionaries, each representing `get_status()` output for
            a legacy-style port (unified adapters are not enumerated here).
        """
        return [port.get_status() for port in self.ports.values()]


    # Methods for compatibility with ConsoleManager interface

    def port_exists(self, port_name: str) -> bool:
        """Return whether a port exists (legacy or unified).

        Args:
            port_name: Logical name of the port.

        Returns:
            True if the port exists either as a legacy port or as a unified
            adapter port for which a wrapper can be created.
        """
        # Check legacy ports first
        if port_name in self.ports:
            return True

        # Try to dynamically ensure unified wrapper exists (on-demand)
        if self._ensure_unified_wrapper(port_name):
            return True

        return False

    async def get_port_list(self) -> List[Dict[str, Any]]:
        """Return status for all legacy-style ports."""
        port_list = []
        for name, port in self.ports.items():
            port_list.append(port.get_status())
        return port_list

    async def get_port_list_with_federation(self) -> List[Dict[str, Any]]:
        """Return status for legacy, federated, and unified-adapter ports.

        The resulting entries may include additional federation metadata
        (origin server identity, chain, and type) when available.
        """
        port_list = []

        # Add ports from the single source of truth (self.ports),
        # which now includes unified wrappers and federated proxies.
        for name, port in self.ports.items():
            port_info = port.get_status()
            # Add the port name to the status info if not already present
            if "name" not in port_info:
                port_info["name"] = name
            # Enrich with federation metadata if available (remote muxcon proxies)
            try:
                if hasattr(port, "metadata"):
                    meta = getattr(port, "metadata")
                    origin = getattr(meta, "origin_server", None)
                    if origin is not None:
                        # Basic server identity
                        port_info["origin_server_id"] = getattr(origin, "server_id", None)
                        port_info["origin_server_hostname"] = getattr(origin, "hostname", None)
                        port_info["origin_server_port"] = getattr(origin, "port", None)
                        st = getattr(origin, "server_type", None)
                        port_info["origin_server_type"] = getattr(st, "value", st) if st is not None else None
                        # V2: full origin object
                        try:
                            to_dict = getattr(origin, "to_dict", None)
                            if callable(to_dict):
                                port_info["origin_server"] = to_dict()
                            else:
                                port_info["origin_server"] = {
                                    "server_id": getattr(origin, "server_id", None),
                                    "hostname": getattr(origin, "hostname", None),
                                    "port": getattr(origin, "port", None),
                                    "server_type": getattr(getattr(origin, "server_type", None), "value", None),
                                    "description": getattr(origin, "description", None),
                                }
                        except Exception:
                            port_info["origin_server"] = {"server_id": getattr(origin, "server_id", None)}
                    # Chain and federation type
                    chain = getattr(meta, "server_chain", []) or []
                    port_info["server_chain"] = [getattr(s, "server_id", str(s)) for s in chain]
                    # V2: chain detailed objects
                    chain_info = []
                    try:
                        for s in chain:
                            to_dict = getattr(s, "to_dict", None)
                            if callable(to_dict):
                                chain_info.append(to_dict())
                            else:
                                chain_info.append(
                                    {
                                        "server_id": getattr(s, "server_id", str(s)),
                                        "hostname": getattr(s, "hostname", None),
                                        "port": getattr(s, "port", None),
                                        "server_type": getattr(getattr(s, "server_type", None), "value", None),
                                        "description": getattr(s, "description", None),
                                    }
                                )
                    except Exception:
                        chain_info = [{"server_id": sid} for sid in port_info["server_chain"]]
                    port_info["server_chain_info"] = chain_info
                    ftype = getattr(meta, "federation_type", None)
                    port_info["federation_type"] = getattr(ftype, "value", ftype) if ftype is not None else None
                    # Mark as remote/federated
                    port_info["remote"] = True
                    # Optional: expose last_seen when present on RemotePortProxy
                    try:
                        if hasattr(port, "last_seen"):
                            port_info["last_seen"] = float(getattr(port, "last_seen"))
                    except Exception:
                        pass
                    # Optional serial configuration and live line status from remote metadata
                    try:
                        sc = getattr(meta, "serial_config", None)
                        if sc is not None:
                            port_info["serial_config"] = sc
                    except Exception:
                        pass
                    try:
                        ls = getattr(meta, "line_status", None)
                        if ls is not None:
                            port_info["line_status"] = ls
                    except Exception:
                        pass
            except Exception:
                # Justification: Non-federated ports or unexpected structure; ignore enrichment
                pass

            # Enrich unified-wrapper ports (e.g., Serial) with config/line-status when available
            try:
                unified = getattr(port, "unified_port", None)
                adapter_type = getattr(port, "adapter_type", None)
                if unified is not None and isinstance(adapter_type, str) and adapter_type.lower() == "serial":
                    cfg = getattr(getattr(unified, "config", None), "__dict__", None)
                    if cfg:
                        port_info.setdefault("serial_config", {
                            "device": cfg.get("device"),
                            "baudrate": cfg.get("baudrate"),
                            "bytesize": cfg.get("bytesize"),
                            "parity": cfg.get("parity"),
                            "stopbits": cfg.get("stopbits"),
                            "flow_control": cfg.get("flow_control"),
                        })
                    get_ls = getattr(unified, "get_line_status", None)
                    if callable(get_ls):
                        try:
                            ls = get_ls()
                            if ls:
                                port_info.setdefault("line_status", ls)
                        except Exception:
                            pass
            except Exception:
                pass
            port_list.append(port_info)

        # Ensure a stable, user-friendly order across all consumers
        try:
            port_list.sort(key=lambda p: str((p or {}).get("name", "")).lower())
        except Exception:
            pass
        return port_list

    async def add_client_to_port(
        self,
        port_name: str,
        client_id: str,
        username: str,
        mode: str = "read-only",
    ) -> bool:
        """Add a client to a port (legacy or unified wrapper).

        Args:
            port_name: Target port name.
            client_id: Unique identifier for the client.
            username: Human-readable username for logs.
            mode: Desired access level, defaults to "read-only".

        Returns:
            True if the client was added; otherwise False (for example when
            capacity is reached or the port cannot be found).
        """
        # On-demand unify wrapper creation before legacy lookup
        if port_name not in self.ports:
            self._ensure_unified_wrapper(port_name)

        # First try legacy ports (may now include wrapper)
        if port_name in self.ports:
            port = self.ports[port_name]
            # If federated proxy is disconnected, allow clients to attach in a
            # meta-only mode so UIs/CLI can remain open and display a warning
            # while waiting for the port to come back. Writes will still be
            # blocked by write_to_port() until the connection is re-established.
            try:
                if hasattr(port, "remote_port_name") and hasattr(port, "is_connected"):
                    is_up = bool(getattr(port, "is_connected"))
                    if not is_up:
                        self.logger.info(
                            f"Allowing client {username} ({client_id}) on {port_name}: federated connection down (meta-only attach)"
                        )
            except Exception:
                self.logger.error(
                    f"Error checking federated connection status for client {username} on {port_name}",
                    exc_info=True,
                )

            # Check if port has room for more clients
            if len(port.connected_clients) >= port.max_read_write_users:
                self.logger.warning(f"Port {port_name} is at maximum capacity")
                return False

            # Add client (the client object will be provided by the console manager)
            client_info = {
                "client_id": client_id,
                "username": username,
                "mode": mode,
                "connected_at": time.time(),
            }
            port.connected_clients.append(client_info)

            self.logger.info(f"Added client {username} ({client_id}) to port {port_name} in {mode} mode")
            # Lifecycle event: client connected
            try:
                DataLogger.get().record_meta(
                    port_name=port_name,
                    event="client_connected",
                    client_id=str(client_id),
                    meta={"username": username, "mode": mode},
                    port_obj=port,
                )
            except Exception:
                self.logger.debug(
                    f"DataLogger lifecycle record failed for {port_name} (client_connected)",
                    exc_info=True,
                )
            # Notify meta listeners (clients count, status, etc.)
            try:
                self.notify_meta_updated(port_name, {"event": "client_connected", "client_id": str(client_id)})
            except Exception:
                pass
            # If this is a federated port (RemotePortProxy), proactively open stream when connected
            try:
                if hasattr(port, "remote_port_name") and hasattr(port, "open_stream_for_client"):
                    if bool(getattr(port, "is_connected", False)):
                        open_fn = getattr(port, "open_stream_for_client", None)
                        if open_fn:
                            await open_fn(client_id)
                    else:
                        # Defer opening the remote stream until federation reconnects
                        self.logger.info(
                            f"Deferring remote stream open for {port_name} (client {client_id}): federated connection down"
                        )
            except Exception as e:
                self.logger.warning(f"Failed to proactively open remote stream for {port_name}: {e}", exc_info=True)
            return True
        # If not handled above and still not found, fail
        return False

    async def remove_client_from_port(self, port_name: str, client_id: str) -> bool:
        """Remove a client from a port (legacy or unified wrapper).

        Args:
            port_name: Port to remove the client from.
            client_id: Identifier used when the client was added.

        Returns:
            True if the client entry was removed; otherwise False.
        """
        # First try legacy ports
        if port_name in self.ports:
            port = self.ports[port_name]

            # Find and remove client
            for i, client in enumerate(port.connected_clients):
                if client["client_id"] == client_id:
                    port.connected_clients.pop(i)
                    self.logger.debug(f"Removed client {client_id} from port {port_name}")
                    # Lifecycle event: client disconnected
                    try:
                        DataLogger.get().record_meta(
                            port_name=port_name,
                            event="client_disconnected",
                            client_id=str(client_id),
                            meta=None,
                            port_obj=port,
                        )
                    except Exception:
                        self.logger.debug(
                            f"DataLogger lifecycle record failed for {port_name} (client_disconnected)",
                            exc_info=True,
                        )
                    # Close remote stream for federated ports
                    try:
                        if hasattr(port, "remote_port_name") and hasattr(port, "close_stream_for_client"):
                            close_fn = getattr(port, "close_stream_for_client", None)
                            if close_fn:
                                await close_fn(client_id)
                    except Exception as e:
                        self.logger.warning(f"Failed to close remote stream for {port_name}: {e}", exc_info=True)
                    # Notify meta listeners (client disconnected)
                    try:
                        self.notify_meta_updated(port_name, {"event": "client_disconnected", "client_id": str(client_id)})
                    except Exception:
                        pass

                    # If this is a federated port and no more clients are connected,
                    # send a :C: command to close the federation session
                    try:
                        if (
                            hasattr(port, "metadata")
                            and hasattr(
                                getattr(port, "metadata", None),
                                "origin_server",
                            )
                            and len(port.connected_clients) == 0
                        ):
                            self.logger.info(f"No more clients connected to federated port {port_name}, closing session")
                            await self._close_federation_session(port_name, port)
                    except AttributeError:
                        # Not a federated port, skip session cleanup
                        self.logger.error(
                            f"Federated session cleanup attribute error for {port_name}",
                            exc_info=True,
                        )

                    return True
            return False

        # Then try unified adapter ports - use the wrapper from the ports dict
        wrapper_port = self.get_port(port_name)
        if wrapper_port and hasattr(wrapper_port, "unified_port"):
            # This is a unified port wrapper
            self.logger.info(f"Attempting to remove client {client_id} from unified port {port_name}")
            self.logger.info(
                f"Current connected_clients: {[c.get('client_id', 'unknown') for c in wrapper_port.connected_clients]}"
            )

            # Find and remove client using wrapper's method
            for client in wrapper_port.connected_clients:
                if client["client_id"] == client_id:
                    wrapper_port.remove_client(client)
                    self.logger.debug(f"Removed client {client_id} from unified port {port_name}")
                    return True

            self.logger.warning(f"Client {client_id} not found in unified port {port_name}")
            return False

        return False

    async def _close_federation_session(self, port_name: str, port):
        """Notify federation adapter(s) to close a session for a port.

        Args:
            port_name: Federated port name.
            port: Port/proxy object that may reference a federation server adapter.
        """
        try:
            # For now, just log that we would close the session
            # In a proper implementation, we'd need access to the federation adapter
            # that manages this specific port
            self.logger.info(f"Would close federation session for port {port_name}")

            # If the port has a write_data method (it's a RemotePortProxy),
            # we can try to access its server adapter to notify about port closure
            if hasattr(port, "server_adapter"):
                server_adapter = port.server_adapter
                if hasattr(server_adapter, "handle_port_session_close"):
                    await server_adapter.handle_port_session_close(port_name)
                    self.logger.info(f"Notified server adapter about port {port_name} session close")

        except Exception as e:
            self.logger.error(f"Error closing federation session for port {port_name}: {e}", exc_info=True)

    async def promote_client(self, port_name: str, client: Any) -> bool:
        """Promote a client's mode to read-write (ConsoleManager compatibility).

        Args:
            port_name: Port on which to promote the client.
            client: Client object whose identity will be derived.

        Returns:
            True if the client's mode was updated to read-write; otherwise False.
        """
        if port_name not in self.ports:
            return False

        # Accept either a client object or a client_id string
        if isinstance(client, str):
            client_id = client
        else:
            client_id = getattr(client, "username", str(id(client)))
        port = self.ports[port_name]

        # Find client and promote
        for client_info in port.connected_clients:
            if client_info["client_id"] == client_id:
                client_info["mode"] = "read-write"
                self.logger.info(f"Promoted client {client_id} to read-write mode on port {port_name}")
                # Lifecycle event: client promoted
                try:
                    DataLogger.get().record_meta(
                        port_name=port_name,
                        event="client_promoted",
                        client_id=str(client_id),
                        meta={"mode": "read-write"},
                        port_obj=port,
                    )
                except Exception:
                    self.logger.debug(
                        f"DataLogger lifecycle record failed for {port_name} (client_promoted)",
                        exc_info=True,
                    )
                return True

        return False

    # Legacy connect/disconnect for built-in ports removed (unified adapters own lifecycle).

    async def write_to_port(self, port_name: str, data: bytes, client_id: str) -> bool:
        """Write data to a port (legacy or unified adapter).

        Args:
            port_name: Target port name.
            data: Bytes to transmit.
            client_id: Client identity used for permission checks and auditing.

        Returns:
            True when the write path reports success; otherwise False.
        """
        # Ensure unified wrapper is present if not a legacy port
        if port_name not in self.ports:
            self._ensure_unified_wrapper(port_name)

        # Unified or legacy path now share the same logic via ports map
        if port_name in self.ports:
            port = self.ports[port_name]

            # Check if client has write permissions
            client_has_write = False
            client_mode = None
            if isinstance(client_id, str) and client_id.startswith("fed:"):
                client_has_write = True
                client_mode = "federation"
            else:
                for client in getattr(port, "connected_clients", []):
                    if client.get("client_id") == client_id and client.get("mode") == "read-write":
                        client_has_write = True
                        client_mode = client.get("mode")
                        break

            if not client_has_write:
                self.logger.warning(f"WRITE BLOCKED: client={client_id} mode={client_mode or 'unknown'} port={port_name}")
                return False

            try:
                self.logger.debug(f"WRITE ALLOW: client={client_id} -> port={port_name} bytes={len(data)} type={type(port)}")
                # Log inbound client->port write
                try:
                    DataLogger.get().record(
                        port_name=port_name,
                        data=data,
                        direction="out",
                        client_id=str(client_id),
                        meta=None,
                        port_obj=port,
                    )
                except (
                    Exception
                ):  # justification: DataLogger failure shouldn't block primary write path; logging for diagnosis
                    self.logger.error("DataLogger record failed (legacy inbound write)", exc_info=True)
                # Pass client_id to write_data if it's a RemotePortProxy
                if hasattr(port, "remote_port_name"):
                    if hasattr(port, "is_connected") and not bool(getattr(port, "is_connected")):
                        self.logger.error(f"WRITE BLOCKED: federated connection not found for {port_name}")
                        return False
                    await port.write_data(data, client_id=client_id)  # type: ignore
                else:
                    await port.write_data(data)
                return True
            except Exception as e:
                self.logger.error(f"Failed to write to port {port_name}: {e}", exc_info=True)
                return False

        return False

    async def get_port_data(self, port_name: str) -> Optional[bytes]:
        """Read one item of port data without blocking.

        Uses non-blocking queue operations so multiple consumers can safely poll
        the same port without creating multiple waiters on the same queue.

        Args:
            port_name: Port to read from.

        Returns:
            The next bytes item if available; otherwise None.
        """
        # Ensure wrapper is present if not a legacy port
        if port_name not in self.ports:
            self._ensure_unified_wrapper(port_name)

        if port_name in self.ports:
            port = self.ports[port_name]
            try:
                data = port.data_queue.get_nowait()
                if data:
                    try:
                        self.logger.debug(f"READ FROM PORT: port={port_name} bytes={len(data)}")
                    except Exception:
                        pass
                return data
            except asyncio.QueueEmpty:
                return None
            except Exception as e:
                self.logger.error(f"Error getting data from port {port_name}: {e}", exc_info=True)
                return None

        return None

    def handle_incoming_port_data(
        self,
        port_name: str,
        data: bytes,
        *,
        require_clients: Optional[bool] = None,
        drop_oldest: Optional[bool] = None,
    ) -> bool:
        """Synchronous handler: log and optionally enqueue incoming port data.

        Args:
            port_name: Logical port name.
            data: Payload bytes to log/forward.
            require_clients: When True (default) data is enqueued only when at
                least one client is connected. When False the queue is used even
                with zero clients. When None, adapter-level hints (e.g.
                ``always_buffer``) control the behavior.
            drop_oldest: When True the oldest queued item is dropped on
                overflow. When False (default) a full queue drops the new data.
                When None, adapter-level preferences (``drop_oldest_on_full``)
                are applied.

        Returns:
            True if accepted/logged; False on queue or unexpected errors.
        """
        if port_name in self.ports:
            port = self.ports[port_name]
            try:
                # Record once per chunk
                try:
                    DataLogger.get().record(
                        port_name=port_name,
                        data=data,
                        direction="in",
                        client_id=None,
                        meta=None,
                        port_obj=getattr(port, "unified_port", None) or port,
                    )
                except Exception:
                    self.logger.debug(f"DataLogger record failed for {port_name}", exc_info=True)

                # Only enqueue when clients are connected (unless told otherwise)
                if hasattr(port, "data_queue") and hasattr(port, "connected_clients"):
                    client_list = getattr(port, "connected_clients", []) or []
                    always_buffer = bool(getattr(port, "always_buffer", False))
                    require_clients_flag = True if require_clients is None else bool(require_clients)
                    should_enqueue = False
                    if not require_clients_flag:
                        should_enqueue = True
                    elif len(client_list) > 0:
                        should_enqueue = True
                    elif always_buffer:
                        should_enqueue = True

                    if should_enqueue and port.data_queue is not None:
                        drop_oldest_flag = (
                            bool(getattr(port, "drop_oldest_on_full", False))
                            if drop_oldest is None
                            else bool(drop_oldest)
                        )
                        try:
                            port.data_queue.put_nowait(data)
                            self.logger.debug(
                                f"Queued {len(data)} bytes for port {port_name} to {len(client_list)} clients"
                            )
                        except asyncio.QueueFull:
                            if drop_oldest_flag:
                                try:
                                    port.data_queue.get_nowait()
                                    port.data_queue.put_nowait(data)
                                    self.logger.debug(
                                        f"Queue full for {port_name}; dropped oldest chunk to enqueue {len(data)} bytes"
                                    )
                                except Exception:
                                    self.logger.warning(
                                        f"Data queue contention for port {port_name}; dropping data after retry",
                                        exc_info=True,
                                    )
                                    return False
                            else:
                                self.logger.warning(f"Data queue full for port {port_name}, dropping data")
                                return False
                    else:
                        self.logger.debug(f"No clients connected to port {port_name}; logged and not queued")
                return True
            except Exception as e:
                self.logger.error(f"Error handling data for port {port_name}: {e}", exc_info=True)
                return False
        self.logger.error(f"Port {port_name} not found for data handling")
        return False

    async def send_data_from_unified_port(
        self,
        port_name: str,
        data: bytes,
        *,
        require_clients: Optional[bool] = None,
        drop_oldest: Optional[bool] = None,
    ) -> bool:
        """Centralized enqueue/log for incoming port data (legacy or unified).

        Always records a single port-level outbound log entry, then enqueues
        for delivery only if there are connected clients.

        Args:
            port_name: Port name.
            data: Bytes from the adapter/port.

        Args:
            port_name: Port name.
            data: Bytes from the adapter/port.
            require_clients: See :meth:`handle_incoming_port_data`.
            drop_oldest: See :meth:`handle_incoming_port_data`.

        Returns:
            True if logged/accepted (even if not enqueued due to no clients);
            False only on queue or unexpected errors.
        """
        return self.handle_incoming_port_data(
            port_name,
            data,
            require_clients=require_clients,
            drop_oldest=drop_oldest,
        )

    def get_client_mode(self, client_id: str, port_name: str) -> Optional[str]:
        """Return a client's mode (if known) for a specific port.

        Args:
            client_id: Identity used to associate the client with the port.
            port_name: Port name to query.

        Returns:
            The client's mode string (e.g. "read-only", "read-write"), or None.
        """
        try:
            # Check legacy/federated ports
            if port_name in self.ports:
                port = self.ports[port_name]
                if hasattr(port, "connected_clients"):
                    for c in port.connected_clients:
                        if c.get("client_id") == client_id:
                            return c.get("mode")
            # Check unified wrapper now stored in ports map
            if port_name in self.ports:
                port2 = self.ports[port_name]
                if hasattr(port2, "connected_clients"):
                    for c in port2.connected_clients:
                        if c.get("client_id") == client_id:
                            return c.get("mode")
        except Exception:
            self.logger.error(
                f"Error determining client mode for client_id={client_id} port_name={port_name}",
                exc_info=True,
            )
        return None

    async def register_unified_port(self, port_name: str, unified_port, adapter) -> bool:
        """Register a unified port and expose it through a wrapper.

        Args:
            port_name: Logical name to assign to the unified port.
            unified_port: Adapter-provided port object.
            adapter: Unified adapter that owns the port.

        Returns:
            True if the port was registered; otherwise False.
        """
        try:
            # Create the unified port wrapper
            wrapper = self._create_unified_port_wrapper(unified_port, adapter)

            # Add to the main ports dictionary
            self.ports[port_name] = wrapper

            self.logger.info(f"Registered unified port {port_name} from adapter {adapter.name}")
            try:
                self.notify_meta_updated(port_name, {"event": "port_registered", "adapter": getattr(adapter, "name", None)})
            except Exception:
                pass
            return True

        except Exception as e:
            self.logger.error(f"Failed to register unified port {port_name}: {e}", exc_info=True)
            return False

    async def unregister_unified_port(self, port_name: str) -> bool:
        """Unregister a unified port and remove its wrapper.

        Args:
            port_name: Name of the unified port to remove.

        Returns:
            True if a port was removed; otherwise False.
        """
        try:
            if port_name in self.ports:
                del self.ports[port_name]
                self.logger.info(f"Unregistered unified port {port_name}")
                try:
                    self.notify_meta_updated(port_name, {"event": "port_unregistered"})
                except Exception:
                    pass
                return True
            else:
                self.logger.warning(f"Unified port {port_name} not found for unregistration")
                return False

        except Exception as e:
            self.logger.error(f"Failed to unregister unified port {port_name}: {e}", exc_info=True)
            return False  # Compatibility methods for ConsoleManager

    async def connect_client(self, port_name: str, client: Any, permissions: str) -> bool:
        """Connect a client to a port (ConsoleManager compatibility).

        Args:
            port_name: Port to connect to.
            client: Client object whose `username` is used as the ID when present.
            permissions: Permission string (e.g. "read-only", "read-write", "admin").

        Returns:
            True if the client was added; otherwise False.
        """
        client_id = getattr(client, "username", str(id(client)))
        username = getattr(client, "username", "unknown")
        mode = "read-write" if permissions in ["read-write", "admin"] else "read-only"
        return await self.add_client_to_port(port_name, client_id, username, mode)

    async def disconnect_client(self, port_name: str, client: Any) -> bool:
        """Disconnect a client from a port (ConsoleManager compatibility).

        Args:
            port_name: Port to disconnect from.
            client: Client object whose identity will be derived.

        Returns:
            True if the client was removed; otherwise False.
        """
        client_id = getattr(client, "username", str(id(client)))
        return await self.remove_client_from_port(port_name, client_id)

    async def register_federated_port(self, metadata, remote_proxy) -> Optional[str]:
        """Register a federated port (RemotePortProxy) with this manager.

        Args:
            metadata: Object describing the federated port and its origin.
            remote_proxy: Proxy object that exposes the remote port locally.

        Returns:
            The registered port name on success; otherwise None.
        """
        try:
            port_name = metadata.name
            self.logger.debug(f"Registering federated port: {port_name}")

            # Add the RemotePortProxy to the ports dictionary
            self.ports[port_name] = remote_proxy

            # Set port manager reference on the proxy
            remote_proxy.set_port_manager(self)

            # Set up data callback for proper integration with console manager
            # Create a callback that will handle incoming data from the federated port
            async def federated_data_callback(data: bytes):
                """Callback for federated port data - integrates with console manager"""
                try:
                    self.logger.debug(f"🚀 FEDERATED CALLBACK: Received {len(data)} bytes for port {port_name}")
                    ok = await self.send_data_from_unified_port(port_name, data, require_clients=False)
                    if not ok and hasattr(remote_proxy, "data_queue"):
                        try:
                            remote_proxy.data_queue.put_nowait(data)
                        except Exception:
                            self.logger.warning(
                                f"🚀 FEDERATED CALLBACK: Failed fallback queue enqueue for {port_name}",
                                exc_info=True,
                            )
                    self.logger.debug(f"Processed {len(data)} bytes for federated port {port_name}")
                except Exception as e:
                    self.logger.error(f"Error in federated port data callback for {port_name}: {e}", exc_info=True)

            # Set the data callback on the remote proxy
            remote_proxy.set_data_callback(federated_data_callback)

            self.logger.debug(f"Registered runtime port {port_name} with data callback")
            try:
                self.notify_meta_updated(port_name, {"event": "federated_port_registered"})
            except Exception:
                pass
            return port_name

        except Exception as e:
            self.logger.error(
                f"Failed to register federated port {getattr(metadata, 'name', 'unknown')}: {e}",
                exc_info=True,
            )
            return None

    async def unregister_federated_ports(self, server_id: str) -> List[str]:
        """Unregister all federated ports originating from `server_id`.

        Args:
            server_id: Identity of the remote server whose ports should be removed.

        Returns:
            A list of port names that were unregistered.
        """
        removed_ports = []

        try:
            # Find and remove all ports that belong to this server
            ports_to_remove = []
            for port_name, port in self.ports.items():
                # Check if this is a RemotePortProxy from the specified server
                try:
                    if hasattr(port, "server_adapter") and hasattr(port, "metadata"):
                        metadata = getattr(port, "metadata", None)
                        if metadata and hasattr(metadata, "origin_server") and metadata.origin_server.server_id == server_id:
                            ports_to_remove.append(port_name)
                except AttributeError:
                    # Skip non-federated ports
                    continue

            # Remove the ports
            for port_name in ports_to_remove:
                del self.ports[port_name]
                removed_ports.append(port_name)
                self.logger.info(f"Unregistered federated port: {port_name}")
                try:
                    self.notify_meta_updated(port_name, {"event": "federated_port_unregistered"})
                except Exception:
                    pass

        except Exception as e:
            self.logger.error(f"Error unregistering federated ports for server {server_id}: {e}", exc_info=True)

        return removed_ports
