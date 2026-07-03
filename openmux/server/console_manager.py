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
        self.data_forwarding_tasks = {}  # Tasks for forwarding data from port to clients (one task per port)
        self.console_clients = {}  # Maps port_name to list of connected clients
        # Back-compat single manager (deprecated by client_managers)
        self.client_manager = None  # Will be set by the client manager
        # New: support multiple client managers and explicit client routing
        self.client_managers = []  # type: List[Any]
        self.client_to_manager = {}  # type: Dict[str, Any]

        # Set the global reference to this console manager
        set_console_manager(self)

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

    async def connect_client_to_port(self, client_id: str, port_name: str, username: str) -> Tuple[bool, str]:
        """Connect a client id to a port and determine access mode.

        Args:
            client_id: Unique identifier of the client.
            port_name: Name of the port to attach to.
            username: Authenticated username for permission lookup.

        Returns:
            Tuple[bool, str]: (success flag, access mode "read-only"|"read-write").
        """
        # Check if client is already connected to a port
        if client_id in self.client_port_map:
            old_port = self.client_port_map[client_id]
            # Disconnect from the old port first
            await self.disconnect_client_from_port(client_id, old_port)

        # Get user permissions
        permissions = self.auth_manager.get_user_permissions(username)

        # Check if this is a loopback port - always use read-write for loopback
        is_loopback = False
        has_write_slots = False
        try:
            port = None
            try:
                port = self.port_manager.get_port(port_name)
            except Exception:
                port = None
            if port is not None:
                # Check if it's a loopback port by adapter type or port name
                is_loopback = (
                    getattr(port, "loopback", False)
                    or getattr(port, "adapter_type", "") == "loopback"
                    or "loopback" in port_name.lower()
                )
                # Check if port has available write slots
                max_rw_users = getattr(port, "max_read_write_users", 1)
                current_rw_users = sum(
                    1 for client in getattr(port, "connected_clients", []) if client.get("mode") == "read-write"
                )
                has_write_slots = current_rw_users < max_rw_users
        except Exception:
            pass

        # Determine client mode based on permissions and port characteristics
        if is_loopback:
            mode = "read-write"
            self.logger.info(f"Auto-promoting client to read-write for loopback port {port_name}")
        elif permissions == "admin":
            mode = "read-write"
            self.logger.info(f"Granting read-write access to admin user for port {port_name}")
        elif has_write_slots:
            mode = "read-write"
            self.logger.info(f"Granting read-write access to user {username} for port {port_name} (slot available)")
        else:
            mode = "read-only"
            self.logger.info(f"Granting read-only access to user {username} for port {port_name}")

        # Add client to port; if read-write is full, fall back to read-only
        success = await self.port_manager.add_client_to_port(port_name, client_id, username, mode)
        if not success and mode == "read-write":
            mode = "read-only"
            self.logger.info(
                f"Read-write slot full for {username} on port {port_name}; falling back to read-only"
            )
            success = await self.port_manager.add_client_to_port(port_name, client_id, username, mode)

        if success:
            # Map client to port
            self.client_port_map[client_id] = port_name

            # Start or ensure data forwarding task for this port (not per client)
            self._ensure_port_data_forwarding(port_name)

            self.logger.info(f"Client {username} ({client_id}) connected to port {port_name} in {mode} mode")

        return success, mode

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

        # Check if this was the last client on the port, and stop forwarding if so
        self._check_and_stop_port_forwarding(port_name)

        # Remove from map
        del self.client_port_map[client_id]

        self.logger.info(f"Client {client_id} disconnected from port {port_name}")
        return True

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

        # Promote client
        success = await self.port_manager.promote_client(port_name, client_id)

        if success:
            self.logger.info(f"Client {client_id} promoted to read-write on port {port_name}")

        return success

    async def demote_client_to_read_only(self, client_id: str, port_name: str) -> bool:
        """Demote a client's access to read-only on a port.

        Args:
            client_id: Identifier of the client to demote.
            port_name: Port on which to demote the client.

        Returns:
            bool: True if demotion succeeded.
        """
        if client_id not in self.client_port_map or self.client_port_map[client_id] != port_name:
            return False

        success = await self.port_manager.demote_client(port_name, client_id)

        if success:
            self.logger.info(f"Client {client_id} demoted to read-only on port {port_name}")

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

    def _ensure_port_data_forwarding(self, port_name: str):
        """Ensure there's a data forwarding task for this port.

        Restarts the task to avoid stale client references.

        Args:
            port_name: Port to (re)establish data forwarding for.
        """
        # Always stop and recreate the task if it exists
        # This ensures we don't have stale client references
        if port_name in self.data_forwarding_tasks:
            old_task = self.data_forwarding_tasks[port_name]
            if not old_task.done() and not old_task.cancelled():
                self.logger.info(f"Stopping existing data forwarding task for port {port_name} to create fresh one")
                old_task.cancel()
            del self.data_forwarding_tasks[port_name]

        # Create new task for this port
        self.logger.info(f"Creating data forwarding task for port {port_name}")
        task = asyncio.create_task(self._forward_data_to_port_clients(port_name))
        self.data_forwarding_tasks[port_name] = task
        self.logger.info(f"Data forwarding task created for port {port_name}")

    def _stop_port_data_forwarding(self, port_name: str):
        """Stop data forwarding task for a port.

        Args:
            port_name: Port whose forwarding task should be stopped.
        """
        if port_name in self.data_forwarding_tasks:
            task = self.data_forwarding_tasks[port_name]
            self.logger.info(f"Stopping data forwarding task for port {port_name}")
            task.cancel()
            del self.data_forwarding_tasks[port_name]
            self.logger.info(f"Data forwarding task for port {port_name} cancelled and removed")

    def _check_and_stop_port_forwarding(self, port_name: str):
        """Stop port forwarding if no clients are connected to the port.

        Args:
            port_name: Port to check for active clients.
        """
        # Check if any clients are still connected to this port
        clients_on_port = [client_id for client_id, port in self.client_port_map.items() if port == port_name]

        if not clients_on_port:
            # No clients left on this port, stop the forwarding task
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
        self.logger.info(
            f"PORT->CLIENT FORWARD: port={port_name}, client={client_id}, "
            f"len={len(data)} bytes, hex={hex_data}, ascii='{ascii_data}'"
        )

        # Special logging for enter key
        if b"\r" in data or b"\n" in data:
            self.logger.info(f"ENTER KEY IN FORWARDED DATA: port={port_name}, client={client_id}")

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

    async def _forward_data_to_port_clients(self, port_name: str):
        """Forward data from a port to all currently connected clients.

        Args:
            port_name: Port whose data is broadcast to its clients.
        """
        try:
            port_count = 0  # Counter for debugging
            self.logger.info(f"Starting data forwarding for port {port_name}")

            while True:
                # Get current clients directly from the port manager (more reliable)
                try:
                    # Get clients from the actual port wrapper instead of our map
                    port_wrapper = None
                    try:
                        port_wrapper = self.port_manager.get_port(port_name)
                    except Exception:
                        port_wrapper = None
                    if port_wrapper and hasattr(port_wrapper, "connected_clients"):
                        clients_on_port = [client["client_id"] for client in port_wrapper.connected_clients]
                    else:
                        clients_on_port = [client_id for client_id, port in self.client_port_map.items() if port == port_name]

                    # This debug logging is to help diagnose issues with stale client references, but is very verbose
                    # self.logger.debug(
                    #    f"Data forwarding loop: found {len(clients_on_port)} clients on {port_name}: {clients_on_port}"
                    # )

                except Exception as e:
                    self.logger.warning(f"Error getting client list for {port_name}: {e}", exc_info=True)
                    # Fallback to our client map
                    clients_on_port = [client_id for client_id, port in self.client_port_map.items() if port == port_name]

                if not clients_on_port:
                    # No clients left on this port, stop the task
                    self.logger.debug(f"No clients left on port {port_name}, stopping data forwarding")
                    break

                try:
                    # Get data from port (now blocks until data available)
                    self._log_loopback_debug_info(port_name, port_count)
                    data = await self.port_manager.get_port_data(port_name)

                    port_count += 1
                    self._log_loopback_debug_info(port_name, port_count, data)

                    if data:
                        # Enhanced debugging for loopback ports
                        if "loopback" in port_name:
                            hex_data = " ".join(f"{b:02x}" for b in data)
                            ascii_data = "".join(chr(b) if 32 <= b <= 126 else "." for b in data)
                            self.logger.debug(
                                f"PORT->CLIENTS BROADCAST: port={port_name}, "
                                f"len={len(data)} bytes, hex={hex_data}, ascii='{ascii_data}'"
                            )

                        # Broadcast to all clients on this port via the client manager
                        # Always get fresh client list to avoid stale references
                        if clients_on_port:
                            # Instead of using console manager's client mapping, send to all current clients
                            success_count = 0
                            # Resolve port object once for logger filename resolution
                            port_obj = None
                            try:
                                port_obj = self.port_manager.get_port(port_name)
                            except Exception as e:
                                self.logger.exception(f"Failed to resolve port object for {port_name}: {e}")
                                port_obj = None

                            for client_id in clients_on_port:
                                try:
                                    success = await self._send_data_to_client(client_id, port_name, data)
                                    if success:
                                        success_count += 1
                                    else:
                                        self.logger.warning(f"Failed to send data to client {client_id}")
                                except Exception as e:
                                    self.logger.warning(f"Error sending data to client {client_id}: {e}", exc_info=True)

                            if success_count > 0:
                                self.logger.debug(
                                    f"Broadcasted data to {success_count}/{len(clients_on_port)} clients on port {port_name}"
                                )
                            else:
                                self.logger.warning(f"Failed to broadcast data to any clients on port {port_name}")
                        else:
                            self.logger.warning(f"No clients to broadcast to on port {port_name}")
                    else:
                        # No data available - small delay to prevent busy polling
                        await asyncio.sleep(0.1)

                    # No need for complex backoff - get_port_data now handles waiting

                except Exception as e:
                    self.logger.error(f"Error in data forwarding loop for port {port_name}: {e}", exc_info=True)
                    # On error, wait before retrying
                    await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            # Task was cancelled
            self.logger.info(f"Data forwarding task for port {port_name} was cancelled")
            pass
        except Exception as e:
            self.logger.error(f"Error forwarding data to clients on port {port_name}: {e}", exc_info=True)

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
