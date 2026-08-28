"""Console Manager for OpenMux server.

Coordinates clients and ports: connects clients to ports, manages access
mode (read-only/read-write), and forwards data from ports to all connected
clients via the registered client manager.
"""

import asyncio
import inspect
import logging
from typing import Any, Dict, List, Optional, Tuple

# Import the helper module to set the console manager reference
from openmux.server.console_manager_helper import set_console_manager
from openmux.server.data_logger import DataLogger


class ConsoleManager:
    """Manages console sessions and client↔port connections.

    Attributes:
        port_manager: Port manager providing port lifecycle and I/O APIs.
        auth_manager: Authentication/authorization provider.
        logger: Module logger instance.
        client_port_map: Map of `client_id` -> `port_name`.
        data_forwarding_tasks: Map of `port_name` -> asyncio.Task that forwards data.
        console_clients: Map of `port_name` -> list of connected client objects.
        client_manager: Registered client manager used to send data to clients.
    """

    def __init__(self, port_manager, auth_manager):
        self.port_manager = port_manager
        self.auth_manager = auth_manager
        self.logger = logging.getLogger("openmux.console")
        self.client_port_map = {}  # Maps client_id to port_name
        self.data_forwarding_tasks = {}  # Tasks for forwarding data to clients (keyed by client_id, one task per client)
        self.console_clients = {}  # Maps port_name to list of connected clients
        # Back-compat single manager (deprecated by client_managers)
        self.client_manager = None  # Will be set by the client manager
        # New: support multiple client managers and explicit client routing
        self.client_managers = []  # type: List[Any]
        self.client_to_manager = {}  # type: Dict[str, Any]

        # Set the global reference to this console manager
        set_console_manager(self)

        # Re-broadcast presence to our own local viewers whenever muxcon learns
        # of a remote viewer change on one of our ports (see issue: a federated
        # viewer opening a console on the far side never reached local viewers'
        # badges here, since only a *local* attach/detach triggered a push).
        try:
            if hasattr(self.port_manager, "register_meta_listener"):
                self.port_manager.register_meta_listener(self._on_federated_viewers_updated)
        except Exception:
            self.logger.debug("Failed to register federated-viewers meta listener", exc_info=True)

    async def _on_federated_viewers_updated(self, port_name: str, changes: Optional[Dict[str, Any]]) -> None:
        """PortManager meta listener: push a fresh presence snapshot on federation viewer changes.

        Triggered by muxcon's `_handle_viewers_frame` after it records an updated
        remote-viewer list on a `RemotePortProxy` or on one of our own local
        ports. Ignores our own `presence_changed` events (already broadcast by
        `broadcast_presence` itself) to avoid redundant frames.
        """
        if not isinstance(changes, dict) or changes.get("event") != "federated_viewers_updated":
            return
        try:
            # notify_federation=False: this data just arrived FROM federation; echoing
            # it straight back out would only feed an unnecessary A<->B broadcast loop.
            await self.broadcast_presence(port_name, notify_federation=False)
        except Exception:
            self.logger.debug(f"Failed to re-broadcast presence for {port_name}", exc_info=True)

    async def port_exists(self, port_name: str) -> bool:
        """Return whether a port exists.

        Args:
            port_name: Name of the port to check.

        Returns:
            bool: True if the port exists in the port manager; else False.
        """
        return self.port_manager.port_exists(port_name)

    async def get_port_list(self) -> List[Dict[str, Any]]:
        """Return list of all ports with status (federation-aware).

        Returns:
            List[Dict[str, Any]]: Port descriptions with status fields.
        """
        return await self.port_manager.get_port_list_with_federation()

    async def list_consoles(self) -> List[Dict[str, Any]]:
        """List all available consoles with their status.

        Returns:
            List[Dict[str, Any]]: Console entries including name, description,
            connection state, and current client list.
        """
        # For tests, directly use the ports from the port manager
        if hasattr(self.port_manager, "ports") and self.port_manager.ports:
            console_list = []
            for port_name, port in self.port_manager.ports.items():
                console = {
                    "name": port.name,
                    "description": getattr(port, "description", ""),
                    "is_connected": getattr(port.adapter, "is_connected", False),
                    "clients": [],
                }

                # Get connected clients from port
                console["clients"] = [
                    {
                        "client_id": client["client_id"],
                        "username": client["username"],
                        "mode": client["mode"],
                    }
                    for client in port.connected_clients
                ]

                console_list.append(console)

            return console_list

        # Actual implementation for production
        try:
            # Get list of all ports with their status
            port_list = await self.port_manager.get_port_list_with_federation()

            # Format the list for client consumption
            console_list = []
            for port in port_list:
                console = {
                    "name": port["name"],
                    "description": port.get("description", ""),
                    "is_connected": port["is_connected"],
                    "clients": port.get("client_list", []),
                }
                console_list.append(console)

            return console_list
        except Exception as e:
            self.logger.error(f"Error listing consoles: {e}", exc_info=True)
            return []

    async def connect_client(self, client, port_name: str) -> bool:
        """Connect a client object to a console/port.

        Args:
            client: Client object (session) to connect.
            port_name: Target port name.

        Returns:
            bool: True on success; False if port missing or operation failed.
        """

        # Check if port exists
        if not self.port_manager.port_exists(port_name):
            return False

        # Get permissions for the client
        permissions = "read-write"  # Default to read-write for tests
        if hasattr(client, "permissions"):
            permissions = client.permissions
        elif hasattr(self.auth_manager, "get_user_permissions") and hasattr(client, "username"):
            permissions = self.auth_manager.get_user_permissions(client.username)

        # Initialize the console_clients dictionary for this port if needed
        if port_name not in self.console_clients:
            self.console_clients[port_name] = []

        # Add client to the console
        self.console_clients[port_name].append(client)

        # Add client to port via unified path if available
        try:
            port = None
            try:
                port = self.port_manager.get_port(port_name)
            except Exception:
                port = None
            if port and hasattr(port, "connect_client"):
                await port.connect_client(client, permissions)
        except Exception:
            pass

        self.logger.info(f"Client connected to {port_name}")
        return True

    async def disconnect_client(self, client) -> None:
        """Disconnect a client from all consoles.

        Args:
            client: Client object (session) to disconnect.
        """

        # Find which console the client is connected to
        for port_name, clients in list(self.console_clients.items()):
            if client in clients:
                # Remove client from port via unified path
                try:
                    port = None
                    try:
                        port = self.port_manager.get_port(port_name)
                    except Exception:
                        port = None
                    if port and hasattr(port, "disconnect_client"):
                        await port.disconnect_client(client)
                except Exception:
                    pass

                # Remove client from console
                self.console_clients[port_name].remove(client)

                self.logger.debug(f"Client disconnected from {port_name}")

                # If no clients are left on this console, clean up
                if not self.console_clients[port_name]:
                    self.console_clients.pop(port_name, None)

    def _has_write_slots(self, port: Optional[Any]) -> bool:
        """Return whether a port has an available read-write slot."""
        if port is None:
            return False
        max_rw_users = getattr(port, "max_read_write_users", 1)
        current_rw_users = sum(1 for client in getattr(port, "connected_clients", []) if client.get("mode") == "read-write")
        return current_rw_users < max_rw_users

    def _rw_mode_or_demote(self, port: Optional[Any], port_name: str, username: str, context: str) -> str:
        """Return "read-write" if a slot is free, else "read-only" (demote, never reject)."""
        if self._has_write_slots(port):
            self.logger.info(f"Granting read-write access to user {username} for port {port_name} ({context})")
            return "read-write"
        self.logger.info(
            f"Read-write slots full; granting read-only access to user {username} for port {port_name} ({context})"
        )
        return "read-only"

    def _resolve_access_mode(
        self, port: Optional[Any], port_name: str, permissions: Optional[str], username: str
    ) -> Tuple[Optional[str], Optional[str]]:
        """Resolve the console access mode for a connecting user (issue #58 ladder).

        Single ladder, first match wins:

        1. admin                         -> read-write, done
        2. port declares group lists:
             in read_write_groups        -> read-write while a slot is free, else read-only
             in read_only_groups         -> read-only
             in neither                  -> denied ("denied_by_group_acl")
        3. port declares no group lists (server-wide allow; the `deny` value
           of `access_default` arrives with Part 2 of issue #58):
             global read-write           -> read-write while a slot is free, else read-only
             global read-only            -> read-only

        Loopback ports get no special treatment: they follow the same ladder
        and slot rules as any other port.

        A full port demotes read-write -> read-only; it never rejects. An
        explicit per-port grant beats the global permission in both
        directions. Unknown identities cannot reach this method: the caller
        rejects ``permissions is None`` first.

        Returns:
            Tuple[Optional[str], Optional[str]]: (mode, deny_reason). ``mode``
            is "read-write"/"read-only" when access is granted (deny_reason is
            then None); ``mode`` is None with a deny_reason when denied.
        """
        if permissions == "admin":
            self.logger.info(f"Granting read-write access to admin user for port {port_name}")
            return "read-write", None

        rw_groups = set(getattr(port, "read_write_groups", None) or [])
        ro_groups = set(getattr(port, "read_only_groups", None) or [])
        if rw_groups or ro_groups:
            user_groups = self.auth_manager.get_user_groups(username)
            if user_groups & rw_groups:
                # Group grants respect the slot cap like global grants do.
                return self._rw_mode_or_demote(port, port_name, username, "group ACL"), None
            if user_groups & ro_groups:
                self.logger.info(f"Granting read-only access to user {username} for port {port_name} (group ACL)")
                return "read-only", None
            # Group lists are a closed boundary: anyone not listed is denied,
            # regardless of the global permission or the server-wide default.
            return None, "denied_by_group_acl"

        # No group lists: the global permission decides.
        if permissions == "read-write":
            return self._rw_mode_or_demote(port, port_name, username, "global permission"), None
        self.logger.info(f"Granting read-only access to user {username} for port {port_name}")
        return "read-only", None

    async def connect_client_to_port(
        self, client_id: str, port_name: str, username: str
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Connect a client id to a port and determine access mode.

        Args:
            client_id: Unique identifier of the client.
            port_name: Name of the port to attach to.
            username: Authenticated username for permission lookup.

        Returns:
            Tuple[bool, Optional[str], Optional[str]]: (success flag, access
            mode "read-only"|"read-write" or None, deny reason code
            ("no_permissions"|"denied_by_group_acl"|"port_full") when access
            was not granted, else None.
        """
        # Check if client is already connected to a port
        if client_id in self.client_port_map:
            old_port = self.client_port_map[client_id]
            # Disconnect from the old port first
            await self.disconnect_client_from_port(client_id, old_port)

        # Get user permissions; an unrecognized identity (no role assigned) gets no access at all.
        permissions = self.auth_manager.get_user_permissions(username)
        if permissions is None:
            self.logger.warning(f"Denying connection for '{username}' to port {port_name}: no permissions assigned")
            return False, None, "no_permissions"

        port = None
        try:
            port = self.port_manager.get_port(port_name)
        except Exception:
            port = None

        mode, reason = self._resolve_access_mode(port, port_name, permissions, username)
        if mode is None:
            self.logger.warning(f"Denying connection for '{username}' to port {port_name}: {reason}")
            return False, None, reason

        # A federated (remote_muxcon) port's origin server is the sole authority on
        # its shared read-write slot (issue #52) - this proxy's own connected_clients
        # only reflects clients attached HERE, not on the origin or any other peer.
        # Always attach read-only first; "read-write" here only means this user is
        # entitled to try for the slot, not that one is actually free right now.
        is_federated = hasattr(port, "remote_port_name")
        entitled_mode = mode
        if is_federated and mode == "read-write":
            mode = "read-only"

        # Add client to port; if read-write is full, fall back to read-only
        success = await self.port_manager.add_client_to_port(port_name, client_id, username, mode)
        if not success and mode == "read-write":
            mode = "read-only"
            self.logger.info(f"Read-write slot full for {username} on port {port_name}; falling back to read-only")
            success = await self.port_manager.add_client_to_port(port_name, client_id, username, mode)

        if not success:
            return False, None, "port_full"

        # Map client to port
        self.client_port_map[client_id] = port_name

        # Start per-client data forwarding task
        self._ensure_client_forwarding_task(port_name, client_id)

        # Federated port: now that a stream is open to the origin, ask it to
        # arbitrate promotion to the slot this user is entitled to. Best-effort -
        # falls back to read-only on any failure/timeout/older-origin, never
        # optimistically grants read-write across a federation link.
        if is_federated and entitled_mode == "read-write":
            if await self._request_federated_promotion(port, port_name, client_id):
                mode = "read-write"

        self.logger.info(f"Client {username} ({client_id}) connected to port {port_name} in {mode} mode")

        # Update every already-attached viewer's presence badge; the new client's own
        # channel isn't registered yet, so the caller sends it an initial snapshot itself
        # (mirrors the existing initial client_mode frame sent on connect).
        try:
            await self.broadcast_presence(port_name)
        except Exception:
            self.logger.debug(f"broadcast_presence failed after connect for {port_name}", exc_info=True)

        return True, mode, None

    async def disconnect_client_from_port(self, client_id: str, port_name: str) -> bool:
        """Disconnect a client from a specific port.

        Args:
            client_id: Identifier of the client to remove.
            port_name: Target port name.

        Returns:
            bool: True if the client was disconnected from the port.
        """
        # Check if client is connected to this port
        if client_id not in self.client_port_map or self.client_port_map[client_id] != port_name:
            return False

        # Remove client from port
        await self.port_manager.remove_client_from_port(port_name, client_id)

        # Cancel this client's forwarding task
        self._stop_client_forwarding_task(client_id)

        # Remove from map
        del self.client_port_map[client_id]

        self.logger.info(f"Client {client_id} disconnected from port {port_name}")
        try:
            await self.broadcast_presence(port_name)
        except Exception:
            self.logger.debug(f"broadcast_presence failed after disconnect for {port_name}", exc_info=True)
        return True

    async def _request_federated_promotion(self, port: Any, port_name: str, client_id: str) -> bool:
        """Ask a federated port's origin to arbitrate read-write promotion (issue #52).

        Args:
            port: The RemotePortProxy instance for this federated port.
            port_name: Local name of the port, for logging.
            client_id: Client requesting promotion.

        Returns:
            True if the origin granted read-write and the local promote_client
            call succeeded; False on denial, timeout, or any error.
        """
        try:
            origin_mode = await port.request_read_write_for_client(client_id)
        except Exception:
            origin_mode = "read-only"
            self.logger.debug(f"FEDRW promotion request failed for {client_id} on {port_name}", exc_info=True)
        if origin_mode != "read-write":
            return False
        return await self.port_manager.promote_client(port_name, client_id)

    async def promote_client_to_read_write(self, client_id: str, port_name: str) -> bool:
        """Promote a client's access to read-write on a port.

        Args:
            client_id: Identifier of the client to promote.
            port_name: Port on which to promote the client.

        Returns:
            bool: True if promotion succeeded.
        """
        # Check if client is connected to this port
        if client_id not in self.client_port_map or self.client_port_map[client_id] != port_name:
            return False

        # A federated (remote_muxcon) port's origin server is the sole authority on
        # its shared read-write slot (issue #52) - ask it before granting locally.
        try:
            port = self.port_manager.get_port(port_name)
        except Exception:
            port = None
        if port is not None and hasattr(port, "request_read_write_for_client"):
            if not await self._request_federated_promotion(port, port_name, client_id):
                self.logger.info(f"Origin denied read-write promotion for {client_id} on federated port {port_name}")
                return False

        # Promote client
        success = await self.port_manager.promote_client(port_name, client_id)

        if success:
            self.logger.info(f"Client {client_id} promoted to read-write on port {port_name}")
            try:
                await self.broadcast_presence(port_name)
            except Exception:
                self.logger.debug(f"broadcast_presence failed after promote for {port_name}", exc_info=True)

        return success

    async def demote_client_to_read_only(self, client_id: str, port_name: str, notify_origin: bool = True) -> bool:
        """Demote a client's access to read-only on a port.

        Args:
            client_id: Identifier of the client to demote.
            port_name: Port on which to demote the client.
            notify_origin: Whether to also ask a federated port's origin to
                release the client's shared read-write slot. Must be False when
                the caller already knows the origin performed this demotion
                itself (e.g. `UnifiedMuxConAdapter._notify_local_client_of_federated_demotion`
                reacting to an unsolicited FEDRWACK) - otherwise the resulting
                FEDRW RELEASE round-trip blocks on the very same connection
                whose read loop is currently awaiting it, self-deadlocking for
                the full request timeout before this method can return.

        Returns:
            bool: True if demotion succeeded.
        """
        if client_id not in self.client_port_map or self.client_port_map[client_id] != port_name:
            return False

        success = await self.port_manager.demote_client(port_name, client_id)

        if success:
            # Release the shared slot on the origin too (issue #52), so another
            # local or federated writer can be promoted. Best-effort: the local
            # demotion above already applies regardless of this call's outcome.
            if notify_origin:
                try:
                    port = self.port_manager.get_port(port_name)
                except Exception:
                    port = None
                if port is not None and hasattr(port, "release_read_write_for_client"):
                    try:
                        await port.release_read_write_for_client(client_id)
                    except Exception:
                        self.logger.debug(f"FEDRW release failed for {client_id} on {port_name}", exc_info=True)
            self.logger.info(f"Client {client_id} demoted to read-only on port {port_name}")
            try:
                await self.broadcast_presence(port_name)
            except Exception:
                self.logger.debug(f"broadcast_presence failed after demote for {port_name}", exc_info=True)

        return success

    # Note: legacy connect_port/disconnect_port removed (unified adapters own lifecycle)

    async def write_to_port(self, port_name: str, data: bytes, client_id: str) -> bool:
        """Write data to a port on behalf of a client.

        Args:
            port_name: Target port name.
            data: Bytes to write.
            client_id: Originating client identifier.

        Returns:
            bool: True if write succeeded.
        """
        return await self.port_manager.write_to_port(port_name, data, client_id)

    def _ensure_client_forwarding_task(self, port_name: str, client_id: str):
        """Create (or recreate) a per-client data forwarding task.

        Args:
            port_name: Port the client is connected to.
            client_id: Client whose dedicated task should be (re)started.
        """
        if client_id in self.data_forwarding_tasks:
            old_task = self.data_forwarding_tasks[client_id]
            if not old_task.done() and not old_task.cancelled():
                old_task.cancel()
            del self.data_forwarding_tasks[client_id]
        task = asyncio.create_task(self._forward_data_to_client(port_name, client_id))
        self.data_forwarding_tasks[client_id] = task
        self.logger.info(f"Created forwarding task for {port_name} → {client_id}")

    def _stop_client_forwarding_task(self, client_id: str):
        """Cancel the forwarding task for a specific client.

        Args:
            client_id: Client whose task should be cancelled.
        """
        if client_id in self.data_forwarding_tasks:
            task = self.data_forwarding_tasks[client_id]
            task.cancel()
            del self.data_forwarding_tasks[client_id]
            self.logger.info(f"Stopped forwarding task for client {client_id}")

    def _stop_port_data_forwarding(self, port_name: str):
        """Cancel all client forwarding tasks associated with a port.

        Args:
            port_name: Port whose client tasks should all be stopped.
        """
        client_ids = [cid for cid, p in self.client_port_map.items() if p == port_name]
        for client_id in client_ids:
            self._stop_client_forwarding_task(client_id)

    def _check_and_stop_port_forwarding(self, port_name: str):
        """Stop port forwarding if no clients are connected to the port.

        Args:
            port_name: Port to check for active clients.
        """
        clients_on_port = [cid for cid, p in self.client_port_map.items() if p == port_name]
        if not clients_on_port:
            self._stop_port_data_forwarding(port_name)

    def _log_loopback_debug_info(self, port_name: str, port_count: int, data: Optional[bytes] = None):
        """Log debug information for loopback ports.

        Args:
            port_name: Port name being logged.
            port_count: Poll iteration counter.
            data: Optional payload read from port.
        """
        if "loopback" not in port_name:
            return

        if data is None:
            # Only log every 50 polls to reduce spam
            if port_count % 50 == 0:
                self.logger.debug(f"Data forwarder: About to call get_port_data for {port_name} (poll #{port_count})")
        elif data:
            self.logger.debug(f"Data forwarder: get_port_data returned {len(data)} bytes (poll #{port_count})")
        else:
            # Only log every 20 empty reads to reduce spam
            if port_count % 20 == 0:
                self.logger.debug(f"Data forwarder: No data from port (poll #{port_count})")

    def _log_forwarded_data_details(self, port_name: str, client_id: str, data: bytes):
        """Log forwarded data details for loopback ports.

        Args:
            port_name: Source port.
            client_id: Destination client identifier.
            data: Bytes forwarded.
        """
        if "loopback" not in port_name:
            return

        hex_data = " ".join(f"{b:02x}" for b in data)
        ascii_data = "".join(chr(b) if 32 <= b <= 126 else "." for b in data)
        self.logger.debug(
            f"PORT->CLIENT FORWARD: port={port_name}, client={client_id}, "
            f"len={len(data)} bytes, hex={hex_data}, ascii='{ascii_data}'"
        )

    async def _send_data_to_client(self, client_id: str, port_name: str, data: bytes) -> bool:
        """Send data to a client through the registered client manager.

        Args:
            client_id: Target client identifier.
            port_name: Source port name (used for logging only).
            data: Payload to send.

        Returns:
            bool: True on success, False if no manager or send failed.
        """
        # Prefer explicit routing if we know the manager for this client id
        mgr = self.client_to_manager.get(client_id)
        if mgr is not None:
            try:
                if "loopback" in port_name:
                    self.logger.debug("Data forwarder: About to call send_data_to_client (mapped)")
                ok = await mgr.send_data_to_client(client_id, data)
                if "loopback" in port_name:
                    self.logger.debug(f"FORWARD TO CLIENT RESULT (mapped): {'Success' if ok else 'Failed'}")
                return ok
            except Exception as e:
                self.logger.warning(f"Client mapped manager send failed for {client_id}: {e}", exc_info=True)
                # fall through to try other managers

        # Next, try all known client managers (multi-manager support)
        any_success = False
        if getattr(self, "client_managers", None):
            for m in list(self.client_managers):
                try:
                    ok = await m.send_data_to_client(client_id, data)
                    if ok:
                        any_success = True
                        # Cache mapping for future fast routing
                        self.client_to_manager[client_id] = m
                        break
                except Exception as e:
                    self.logger.debug(f"Manager {getattr(m, 'name', type(m).__name__)} send error: {e}")
            if any_success:
                return True

        # Back-compat: fall back to single manager if set
        if not (hasattr(self, "client_manager") and self.client_manager):
            self.logger.warning(f"No client manager available to forward data to client {client_id}")
            return False

        if "loopback" in port_name:
            self.logger.debug("Data forwarder: About to call send_data_to_client (legacy)")
        success = await self.client_manager.send_data_to_client(client_id, data)
        if "loopback" in port_name:
            self.logger.debug(f"FORWARD TO CLIENT RESULT (legacy): {'Success' if success else 'Failed'}")
        # Cache mapping even for legacy path to reduce future lookups
        if success:
            try:
                self.client_to_manager[client_id] = self.client_manager
            except Exception:
                pass
        return success

    async def _forward_data_to_client(self, port_name: str, client_id: str):
        """Forward data from a port to a single client via its dedicated queue.

        Args:
            port_name: Source port name.
            client_id: Target client identifier.
        """
        try:
            self.logger.info(f"Starting data forwarding for {port_name} \u2192 {client_id}")

            # Capture the queue reference once; task is recreated on reconnect.
            port_wrapper = self.port_manager.get_port(port_name)
            if port_wrapper is None or not hasattr(port_wrapper, "client_queues"):
                self.logger.error(f"Port {port_name} has no client_queues; forwarding for {client_id} cannot start")
                return
            q = port_wrapper.client_queues.get(client_id)
            if q is None:
                self.logger.error(f"No delivery queue for {client_id} on {port_name}")
                return

            chunk_count = 0
            while True:
                try:
                    data = await q.get()
                    chunk_count += 1

                    self._log_loopback_debug_info(port_name, chunk_count, data)
                    self._log_forwarded_data_details(port_name, client_id, data)

                    success = await self._send_data_to_client(client_id, port_name, data)
                    if not success:
                        self.logger.warning(f"Failed to send data to client {client_id} on {port_name}")
                    else:
                        self.logger.debug(f"Forwarded {len(data)} bytes from {port_name} to {client_id}")

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.logger.error(f"Error forwarding data to {client_id} on {port_name}: {e}", exc_info=True)
                    await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            self.logger.info(f"Data forwarding for {port_name} \u2192 {client_id} was cancelled")
        except Exception as e:
            self.logger.error(f"Unexpected error in forwarding task for {client_id}: {e}", exc_info=True)

    def register_client_manager(self, client_manager):
        """Register the client manager for callbacks.

        Args:
            client_manager: Manager exposing `send_data_to_client`.
        """
        # Keep legacy field for backward compatibility
        self.client_manager = client_manager
        # New behavior: accumulate managers for multi-adapter broadcasts
        try:
            if client_manager not in self.client_managers:
                self.client_managers.append(client_manager)
                self.logger.info(
                    f"Registered client manager: {getattr(client_manager, 'name', type(client_manager).__name__)}"
                )
        except Exception:
            # Non-fatal if this fails; legacy path remains
            pass

    def register_client_channel(self, client_id: str, client_manager: Any) -> None:
        """Associate a specific client id with a client manager for routing.

        Adapters should call this after a client successfully attaches to a port.

        Args:
            client_id: Unique identifier of the client.
            client_manager: Manager instance that can deliver data to the client.
        """
        try:
            self.client_to_manager[client_id] = client_manager
            # Ensure the manager is in our list as well
            if getattr(self, "client_managers", None) is not None and client_manager not in self.client_managers:
                self.client_managers.append(client_manager)
        except Exception:
            pass

    def unregister_client_channel(self, client_id: str) -> None:
        """Remove routing association for a client id.

        Should be called when a client detaches from a port or disconnects.
        """
        try:
            if client_id in self.client_to_manager:
                del self.client_to_manager[client_id]
        except Exception:
            pass

    def register_client_port(self, client_id: str, port_name: str) -> None:
        """Map a client id to a port, bypassing the full `connect_client_to_port` flow.

        For federated pseudo-clients (muxcon's "fed:<peer_key>:<stream_id>" ids,
        added directly to PortManager by UnifiedMuxConAdapter, never through
        `connect_client_to_port`) that still need to be reachable by the same
        generic `demote_client_to_read_only`/`force_promote_client` machinery
        every other adapter's real clients go through - otherwise a locally
        initiated force-take against a federated read-write holder silently
        fails to actually demote it (see `force_promote_client`).
        """
        self.client_port_map[client_id] = port_name

    def unregister_client_port(self, client_id: str) -> None:
        """Remove a mapping registered via `register_client_port`."""
        self.client_port_map.pop(client_id, None)

    async def send_control_frame_to_client(self, client_id: str, payload: Dict[str, Any]) -> bool:
        """Deliver an access-mode control frame to a client via its owning adapter.

        Routes through the adapter registered for `client_id` (TCP, telnet, or
        web console), regardless of which adapter originated the request, so
        e.g. a CLI force-take can notify a web console client and vice versa.

        Args:
            client_id: Target client identifier.
            payload: JSON-serializable control message (e.g. a demotion notice).

        Returns:
            bool: True if the owning adapter accepted and sent the frame.
        """
        mgr = self.client_to_manager.get(client_id)
        if mgr is None or not hasattr(mgr, "send_control_frame_to_client"):
            return False
        try:
            return bool(await mgr.send_control_frame_to_client(client_id, payload))
        except Exception:
            self.logger.debug(f"send_control_frame_to_client failed for {client_id}", exc_info=True)
            return False

    async def broadcast_control_frame_to_port(self, port_name: str, payload: Dict[str, Any]) -> int:
        """Deliver a control frame to every client currently attached to `port_name`.

        Mirrors `send_control_frame_to_client` but fans out to all viewers (e.g. a
        "Port Action started" live notice, see docs/design/port_actions.md "Live view"),
        regardless of which adapter each client is on.

        Args:
            port_name: Port whose current viewers should receive the frame.
            payload: JSON-serializable control message.

        Returns:
            int: Number of clients the frame was successfully delivered to.
        """
        client_ids = [cid for cid, p in self.client_port_map.items() if p == port_name]
        delivered = 0
        for client_id in client_ids:
            try:
                if await self.send_control_frame_to_client(client_id, payload):
                    delivered += 1
            except Exception:
                self.logger.debug(f"broadcast_control_frame_to_port failed for {client_id}", exc_info=True)
        return delivered

    async def force_promote_client(self, client_id: str, port_name: str) -> Tuple[bool, List[str]]:
        """Demote existing read-write holders on a port, then promote a client.

        Shared by every adapter (TCP, web console, telnet, SSH) so the
        demote-others/promote-self/cross-notify sequence lives in one place.

        Args:
            client_id: Client requesting the forced takeover.
            port_name: Port on which to force the takeover.

        Returns:
            Tuple[bool, List[str]]: (success, ids of demoted clients that
            could NOT be cross-notified, for the caller's own same-adapter
            fallback delivery).
        """
        other_ids = [c["client_id"] for c in self._read_write_holders(port_name) if c["client_id"] != client_id]
        for other_id in other_ids:
            try:
                await self.demote_client_to_read_only(other_id, port_name)
            except Exception:
                pass

        # A federated port's origin may have OTHER read-write holders this
        # server can't see (a client local to the origin, or attached via a
        # different federated peer) - a plain promote request would be denied
        # once the slot looks full to the origin. Ask it to force-demote
        # everyone and promote this client in one arbitration (issue #52
        # follow-up) instead of going through the non-forcing request path.
        try:
            port = self.port_manager.get_port(port_name)
        except Exception:
            port = None
        if port is not None and hasattr(port, "force_read_write_for_client"):
            try:
                origin_mode = await port.force_read_write_for_client(client_id)
            except Exception:
                origin_mode = "read-only"
                self.logger.debug(f"FEDRW FORCE request failed for {client_id} on {port_name}", exc_info=True)
            ok = origin_mode == "read-write" and await self.port_manager.promote_client(port_name, client_id)
        else:
            ok = await self.promote_client_to_read_write(client_id, port_name)
        undelivered: List[str] = []
        if ok:
            demotion = {"type": "client_mode", "ok": False, "mode": "read-only", "reason": "demoted"}
            for other_id in other_ids:
                try:
                    delivered = await self.send_control_frame_to_client(other_id, demotion)
                except Exception:
                    delivered = False
                if not delivered:
                    undelivered.append(other_id)
        return ok, undelivered

    def _read_write_holders(self, port_name: str) -> List[Dict[str, Any]]:
        """Return the raw connected-client records currently in read-write mode."""
        try:
            port = self.port_manager.ports.get(port_name) if hasattr(self.port_manager, "ports") else None
        except Exception:
            port = None
        if port is None:
            return []
        return [c for c in getattr(port, "connected_clients", []) if c.get("mode") == "read-write"]

    def get_rw_holders_display(self, port_name: str) -> List[str]:
        """Return 'username@ip' strings for all read-write clients on a port.

        Resolves IPs across adapter types via each owning adapter's
        `_resolve_client_meta`, so e.g. a telnet client can see that a web
        console user currently holds read-write access.
        """
        holders: List[str] = []
        for c in self._read_write_holders(port_name):
            cid = c.get("client_id", "")
            username = c.get("username", "unknown")
            holders.append(f"{username}@{self._resolve_client_ip(cid)}")
        return holders

    def _resolve_client_ip(self, client_id: str) -> str:
        """Resolve a client's source IP via its owning adapter's `_resolve_client_meta`."""
        mgr = self.client_to_manager.get(client_id)
        resolver = getattr(mgr, "_resolve_client_meta", None)
        if callable(resolver):
            try:
                meta = resolver(client_id) or {}
                ip = meta.get("ip")
                if ip:
                    return str(ip)
            except Exception:
                pass
        return "unknown"

    def get_viewers_display(self, port_name: str) -> List[Dict[str, str]]:
        """Return viewer entries for every client attached to a port, local and federated.

        Unlike `get_rw_holders_display`, this includes read-only viewers too (see
        GitHub issue #48: presence must be visible to every viewer, not just the
        read-write holder). Local entries carry `{"username", "mode", "client_id",
        "ip"}` (`client_id` lets a viewer's own UI mark itself as "(me)"); when the
        port is a federated `remote_muxcon` proxy, entries reported by the remote
        side (via muxcon's VIEWERS presence relay) are appended too, each carrying
        an extra `"server_id"` naming the server the viewer is actually attached to.

        `connected_clients` entries whose `client_id` starts with `"fed:"` are
        skipped here: those are internal pseudo-clients muxcon registers on the
        origin port purely so ConsoleManager's RW-arbitration/notify machinery
        can reach a federated writer (see issue #52 / commit `818e213`); the
        SAME remote viewer is already reported properly (real username, ip,
        `server_id`) via `remote_viewers`. Counting both double-counts that
        viewer and shows a bogus `federation:<peer_key>@unknown` entry.
        """
        try:
            port = self.port_manager.ports.get(port_name) if hasattr(self.port_manager, "ports") else None
        except Exception:
            port = None
        if port is None:
            return []
        entries = [
            {
                "username": c.get("username", "unknown"),
                "mode": c.get("mode", "read-only"),
                "client_id": c.get("client_id", ""),
                "ip": self._resolve_client_ip(c.get("client_id", "")),
            }
            for c in getattr(port, "connected_clients", [])
            if not str(c.get("client_id", "")).startswith("fed:")
        ]
        entries.extend(getattr(port, "remote_viewers", None) or [])
        return entries

    async def broadcast_presence(self, port_name: str, notify_federation: bool = True) -> int:
        """Broadcast the current viewer list to every client attached to a port.

        Called from the client-attach/detach/promote/demote call sites so the web
        console's ambient viewer badge (see console.js) stays live with no separate
        poll loop. CLI adapters (telnet/SSH) render this frame as a no-op and expose
        the same data on demand instead, via the Ctrl+E "show viewers" command.

        Args:
            port_name: Port whose current viewers should be broadcast.
            notify_federation: Whether to also notify PortManager's meta-listener
                bus (muxcon relays this to peers). Pass False when the viewers
                changed *because* a peer just told us about them, so we don't
                immediately echo the same data back out over federation.

        Returns:
            int: Number of clients the frame was successfully delivered to.
        """
        viewers = self.get_viewers_display(port_name)
        if notify_federation:
            # Let PortManager's generic meta-listener bus fan this out to interested
            # adapters too (e.g. muxcon relays it to peers so a federated view of this
            # port shows our local viewers - see UnifiedMuxConAdapter's VIEWERS frame).
            try:
                self.port_manager.notify_meta_updated(port_name, {"event": "presence_changed", "viewers": viewers})
            except Exception:
                self.logger.debug(f"notify_meta_updated(presence_changed) failed for {port_name}", exc_info=True)
        return await self.broadcast_control_frame_to_port(port_name, {"type": "presence", "viewers": viewers})

    def get_client_mode(self, client_id: str, port_name: str) -> Optional[str]:
        """Return access mode for a client on a specific port.

        Args:
            client_id: Client identifier to query.
            port_name: Port name.

        Returns:
            Optional[str]: "read-only" or "read-write"; defaults to "read-only".
        """
        if hasattr(self.port_manager, "get_client_mode"):
            return self.port_manager.get_client_mode(client_id, port_name)
        # Fallback: query the port directly for client info via unified path
        try:
            port = None
            try:
                port = self.port_manager.get_port(port_name)
            except Exception:
                port = None
            if port and hasattr(port, "connected_clients"):
                for client in port.connected_clients:
                    if client.get("client_id") == client_id:
                        return client.get("mode", "read-only")
        except Exception:
            pass
        return "read-only"

        return "read-only"  # Default fallback
