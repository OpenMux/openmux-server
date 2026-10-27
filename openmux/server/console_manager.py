"""Console Manager for OpenMux server.

Coordinates clients and ports: connects clients to ports, manages access
mode (read-only/read-write), and forwards data from ports to all connected
clients via the registered client manager.
"""

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

# Import the helper module to set the console manager reference
from openmux.server.access_control import holder_id_short, wire_to_mode, write_capacity
from openmux.server.access_decision import (
    AccessDecision,
    Code,
    Entitlement,
    Holder,
    Mode,
    decide,
    holders_frame,
    holders_of,
    notice,
    presence_frame,
)
from openmux.server.console_manager_helper import set_console_manager
from openmux.server.data_logger import DataLogger


def _port_capacity(port: Optional[Any]) -> Optional[Any]:
    """The port's own write-capacity setting, passed through un-normalised.

    Every Access decision about a Port carries it, so a console can show the
    limit next to the refusal without asking the Port again. ``None`` when the
    Port could not be resolved, which leaves the key off the frame.
    """
    return getattr(port, "max_read_write_users", None) if port is not None else None


def _match_take_target(holders: List[Dict[str, Any]], target: str) -> Optional[Dict[str, Any]]:
    """Resolve the target a console named to exactly one read-write holder record.

    A console shows each holder with ``holder_id_short`` - the id inside the label in
    its holders list - and that is the id an operator copies and types back. So accept
    the exact ``client_id`` first, which is what the web console and the CLI protocol
    send, then the short form when exactly one holder shows it. Two holders printing
    the same short form is a collision: refuse rather than demote the wrong writer.
    """
    for record in holders:
        if record.get("client_id") == target:
            return record
    printed = [record for record in holders if holder_id_short(str(record.get("client_id") or "")) == target]
    return printed[0] if len(printed) == 1 else None


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
        # Server-wide security policy (issue #58 `access_default` for no-list
        # ports). Set by OpenMuxServer at construction and on every soft/full
        # reload via _refresh_security_policy; resolution falls back to
        # "allow" when unset, so a missing policy is backward compatible.
        self.security_policy = None

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
            self.logger.debug("Failed to re-broadcast presence for %s", port_name, exc_info=True)

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
            self.logger.error("Error listing consoles: %s", e, exc_info=True)
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
            self.logger.error("connect_client: unified attach failed for port %s", port_name, exc_info=True)

        self.logger.info("Client connected to %s", port_name)
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
                    self.logger.error("disconnect_client: port-side detach failed for port %s", port_name, exc_info=True)

                # Remove client from console
                self.console_clients[port_name].remove(client)

                self.logger.debug("Client disconnected from %s", port_name)

                # If no clients are left on this console, clean up
                if not self.console_clients[port_name]:
                    self.console_clients.pop(port_name, None)

    def _has_write_slots(self, port: Optional[Any]) -> bool:
        """Return whether a port can grant a read-write slot right now.

        Capacity comes from the port's ``max_read_write_users`` mode (issue #59):
        ``multiple`` means unlimited (always True), ``one`` means one concurrent
        writer (True while none is attached), ``none`` means the port is never
        drivable (always False, for everyone including admin).
        """
        if port is None:
            return False
        capacity = write_capacity(getattr(port, "max_read_write_users", 1))
        if capacity == 0.0:
            return False
        if capacity == float("inf"):
            return True
        current_rw_users = sum(1 for client in getattr(port, "connected_clients", []) if client.get("mode") == "read-write")
        return current_rw_users < capacity

    def _holders_for(self, port: Optional[Any]) -> Tuple[Holder, ...]:
        """The Write slot holders of one port, labelled the one way (issue #98).

        ``_resolve_client_ip`` resolves through the owning adapter, so a telnet
        client sees a web console holder with the same label the web shows.
        """
        return holders_of(getattr(port, "connected_clients", None) or [], ip_of=self._resolve_client_ip)

    def _rw_mode_or_demote(self, port: Optional[Any], port_name: str, username: str, context: str) -> Tuple[Mode, Code]:
        """Return the mode a write-entitled user gets on this port, with its code.

        A full port demotes; a ``none`` port (0 writers, issue #59) always
        demotes — that includes admin, because a slot is a resource, not a
        privilege. Both demotions are still grants: the Client attaches and
        watches, so the codes say which seat it got rather than reporting a
        failure.
        """
        if self._has_write_slots(port):
            self.logger.info("Granting read-write access to user %s for port %s (%s)", username, port_name, context)
            return Mode.READ_WRITE, Code.ATTACHED
        no_slots = write_capacity(getattr(port, "max_read_write_users", 1)) == 0.0
        if no_slots:
            self.logger.info(
                f"Port {port_name} has no write slots (max_read_write_users=none); "
                f"granting read-only access to user {username} ({context})"
            )
        else:
            self.logger.info(
                "Read-write slots full; granting read-only access to user %s for port %s (%s)", username, port_name, context
            )
        return Mode.READ_ONLY, Code.CAPACITY_NONE if no_slots else Code.CAPACITY_FULL

    def _taker_entitled(self, port: Optional[Any], port_name: str, permissions: Optional[str], username: str) -> Entitlement:
        """Decide whether this identity may drive this port (issue #59 Part 2).

        This is the #58 access ladder with the slot-occupancy question removed.
        Attach time combines this with the slot check; write-slot takeover
        re-checks it so a read-only seat can never take the slot even though
        it is attached.

        Returns:
            Entitlement: drive, watch, or denied. A denial carries the code of
            the rule that refused it, which is also the code the attach reports.
        """
        if permissions == "admin":
            return Entitlement.write()
        rw_groups = set(getattr(port, "read_write_groups", None) or [])
        ro_groups = set(getattr(port, "read_only_groups", None) or [])
        if rw_groups or ro_groups:
            user_groups = self.auth_manager.get_user_groups(username)
            if user_groups & rw_groups:
                return Entitlement.write()
            if user_groups & ro_groups:
                return Entitlement.read_only()
            # Group lists are a closed boundary: anyone not listed is denied,
            # regardless of the global permission or the server-wide default.
            return Entitlement.denied(Code.DENIED_BY_GROUP_ACL)

        # No group lists: the server-wide access_default decides whether the
        # port is drivable at all (security.yaml, issue #58).
        access_default = "allow"
        if self.security_policy is not None and hasattr(self.security_policy, "get_access_default"):
            try:
                access_default = self.security_policy.get_access_default() or "allow"
            except Exception:
                access_default = "allow"  # justification: policy lookup must not break attach; stay fail-open on this path
        if access_default == "deny":
            return Entitlement.denied(Code.DENIED_BY_ACCESS_DEFAULT)

        # access_default allow: the global permission decides.
        return Entitlement.write() if permissions == "read-write" else Entitlement.read_only()

    def blocked_ports_for_user(self, port_names: List[str], username: str) -> List[str]:
        """Return the subset of ``port_names`` this user may not drive.

        Reuses the attach-time access ladder (``_taker_entitled``), so "may
        drive" means "would get read-write access to this console". ``admin``
        is never blocked. For the others, a port whose group lists exclude the
        user, a read-only-only grant, a denied ``access_default``, or a global
        permission below read-write all count as blocked.

        Used by the PDU power adapter to scope outlet switching to consoles the
        user can open: turning an outlet off may only cut power to consoles the
        user has read-write access to, otherwise admin is required.
        """
        permissions = self.auth_manager.get_user_permissions(username) if self.auth_manager else None
        if permissions == "admin":
            return []
        blocked: List[str] = []
        for name in port_names:
            try:
                port = self.port_manager.get_port(name)
            except Exception:
                port = None
            if not self._taker_entitled(port, name, permissions, username).may_drive:
                blocked.append(name)
        return blocked

    def _resolve_access_mode(
        self, port: Optional[Any], port_name: str, permissions: Optional[str], username: str
    ) -> AccessDecision:
        """Resolve the console access mode for a connecting user (issue #58 ladder).

        Single ladder, first match wins ("a slot is free" reads the port's
        write-capacity mode, issue #59: ``one`` = free while 0 writers are
        attached, ``multiple`` = always free, ``none`` = never free). The
        entitlement decision itself lives in `_taker_entitled` (shared with
        write-slot takeover); this method adds the slot check on top.

        1. admin                         -> read-write while a slot is free, else read-only
        2. port declares group lists:
             in read_write_groups        -> read-write while a slot is free, else read-only
             in read_only_groups         -> read-only
             in neither                  -> denied ("denied_by_group_acl")
        3. port declares no group lists: apply the server-wide access_default
           (security.yaml, "allow" by default when unset):
             deny                        -> denied ("denied_by_access_default")
             allow:
               global read-write         -> read-write while a slot is free, else read-only
               global read-only          -> read-only

        Loopback ports get no special treatment: they follow the same ladder
        and slot rules as any other port.

        Admin bypasses access control (the group boundary, the server-wide
        default), not capacity: under ``none`` admin attaches read-only like
        everyone else (issue #59, explicit behavior change). A full port
        demotes read-write -> read-only; it never rejects. An explicit
        per-port grant beats the global permission in both directions. Unknown
        identities cannot reach this method: the caller rejects
        ``permissions is None`` first.

        Returns:
            AccessDecision: ``mode`` is the mode the ladder gives (read-only for
            a refusal, which grants nothing) and ``code`` is why: ``attached``
            with the seat it got, or the rule that refused it.
        """
        entitlement = self._taker_entitled(port, port_name, permissions, username)
        if not entitlement.may_drive:
            if entitlement.code is not None:
                self.logger.info("Denying user %s for port %s: %s", username, port_name, entitlement.code.value)
                return decide(entitlement.code, mode=Mode.READ_ONLY, port_name=port_name, capacity=_port_capacity(port))
            self.logger.info("Granting read-only access to user %s for port %s", username, port_name)
            return decide(Code.READ_ONLY_STANDING, mode=Mode.READ_ONLY, port_name=port_name, capacity=_port_capacity(port))
        # Admin bypasses access control but not capacity (issue #59): under a
        # ``none`` (0-writer) port admin gets read-only like everyone else.
        mode, code = self._rw_mode_or_demote(port, port_name, username, "write entitlement")
        return decide(code, mode=mode, port_name=port_name, capacity=_port_capacity(port))

    async def connect_client_to_port(self, client_id: str, port_name: str, username: str) -> AccessDecision:
        """Connect a client id to a port and determine its access mode.

        Args:
            client_id: Unique identifier of the client.
            port_name: Name of the port to attach to.
            username: Authenticated username for permission lookup.

        Returns:
            AccessDecision: ``ok`` is True for every attach that left the Client
            on the Port, including a read-only one; ``mode`` is the mode it holds.
            A refusal carries ``no_permissions``, a group/policy refusal, or
            ``port_unavailable`` when even the read-only seat could not be taken.
        """
        # Check if client is already connected to a port
        if client_id in self.client_port_map:
            old_port = self.client_port_map[client_id]
            # Disconnect from the old port first
            await self.disconnect_client_from_port(client_id, old_port)

        # Get user permissions; an unrecognized identity (no role assigned) gets no access at all.
        permissions = self.auth_manager.get_user_permissions(username)
        if permissions is None:
            self.logger.warning("Denying connection for '%s' to port %s: no permissions assigned", username, port_name)
            return self._attach_refusal(Code.NO_PERMISSIONS, client_id, port_name, None)

        port = None
        try:
            port = self.port_manager.get_port(port_name)
        except Exception:
            port = None

        ladder = self._resolve_access_mode(port, port_name, permissions, username)
        if not ladder.ok:
            self.logger.warning("Denying connection for '%s' to port %s: %s", username, port_name, ladder.code.value)
            return replace(ladder, client_id=client_id)

        # A federated (remote_muxcon) port's origin server is the sole authority on
        # its shared read-write slot (issue #52) - this proxy's own connected_clients
        # only reflects clients attached HERE, not on the origin or any other peer.
        # Always attach read-only first; "read-write" here only means this user is
        # entitled to try for the slot, not that one is actually free right now.
        is_federated = hasattr(port, "remote_port_name")
        entitled_mode = ladder.mode
        mode = Mode.READ_ONLY if (is_federated and entitled_mode is Mode.READ_WRITE) else entitled_mode

        attached, code = await self._attach_client_to_port(
            port, port_name, client_id, username, mode, is_federated, entitled_mode, ladder.code
        )
        if attached is None:
            return self._attach_refusal(Code.PORT_UNAVAILABLE, client_id, port_name, port)

        self.logger.info("Client %s (%s) attached to port %s in %s mode", username, client_id, port_name, attached.value)

        # Update every already-attached viewer's presence badge; the new client's own
        # channel isn't registered yet, so the caller sends it an initial snapshot itself
        # (mirrors the existing initial client_mode frame sent on connect).
        try:
            await self.broadcast_presence(port_name)
        except Exception:
            self.logger.debug("broadcast_presence failed after connect for %s", port_name, exc_info=True)

        return decide(
            code,
            mode=attached,
            client_id=client_id,
            port_name=port_name,
            holders=self._holders_for(port),
            capacity=_port_capacity(port),
        )

    def _attach_refusal(self, code: Code, client_id: str, port_name: str, port: Optional[Any]) -> AccessDecision:
        """Refuse an attach: the Client holds nothing, so the mode is read-only."""
        return decide(
            code,
            mode=Mode.READ_ONLY,
            client_id=client_id,
            port_name=port_name,
            holders=self._holders_for(port),
            capacity=_port_capacity(port),
        )

    async def _attach_client_to_port(
        self,
        port: Any,
        port_name: str,
        client_id: str,
        username: str,
        mode: Mode,
        is_federated: bool,
        entitled_mode: Mode,
        code: Code = Code.ATTACHED,
    ) -> Tuple[Optional[Mode], Code]:
        """Attach a client to a port, falling back from read-write to read-only.

        A denied read-write attach (slot full) retries once as read-only.
        On a federated port (issue #52) the origin is the sole authority on
        the shared read-write slot: a client entitled to the slot attaches
        read-only first and is promoted only if the origin grants it.

        Args:
            port: Port object, used for the federated promotion request.
            port_name: Target port name.
            client_id: Client id to attach.
            username: Authenticated username recorded with the attachment.
            mode: Mode to attach in.
            is_federated: True when the port is a federated (remote_muxcon) proxy.
            entitled_mode: Mode this user is entitled to (before demotion).
            code: The code matching ``mode``, from ``_resolve_access_mode``.

        Returns:
            Tuple[Optional[Mode], Code]: (mode attached in, the code matching
            it), or (None, ``port_unavailable``) when the port is full.
        """
        # The Port keeps its plain mode strings; a Mode is this module's vocabulary.
        success = await self.port_manager.add_client_to_port(port_name, client_id, username, mode.value)
        if not success and mode is Mode.READ_WRITE:
            mode = Mode.READ_ONLY
            code = Code.CAPACITY_FULL
            self.logger.info("Read-write slot full for %s on port %s; falling back to read-only", username, port_name)
            success = await self.port_manager.add_client_to_port(port_name, client_id, username, mode.value)

        if not success:
            return None, Code.PORT_UNAVAILABLE

        # Map client to port
        self.client_port_map[client_id] = port_name

        # Start per-client data forwarding task
        self._ensure_client_forwarding_task(port_name, client_id)

        # Federated port: now that a stream is open to the origin, ask it to
        # arbitrate promotion to the slot this user is entitled to. Best-effort -
        # falls back to read-only on any failure/timeout/older-origin, never
        # optimistically grants read-write across a federation link.
        if is_federated and entitled_mode is Mode.READ_WRITE:
            if await self._request_federated_promotion(port, port_name, client_id):
                mode, code = Mode.READ_WRITE, Code.ATTACHED
            else:
                # The origin holds the one shared slot; the reason this Client
                # watches instead of drives is capacity, not access control.
                code = Code.CAPACITY_FULL

        return mode, code

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

        self.logger.info("Client %s disconnected from port %s", client_id, port_name)
        try:
            await self.broadcast_presence(port_name)
        except Exception:
            self.logger.debug("broadcast_presence failed after disconnect for %s", port_name, exc_info=True)
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
            self.logger.debug("FEDRW promotion request failed for %s on %s", client_id, port_name, exc_info=True)
        if origin_mode != "read-write":
            return False
        return await self.port_manager.promote_client(port_name, client_id)

    async def promote_client_to_read_write(self, client_id: str, port_name: str) -> AccessDecision:
        """Promote a client's access to read-write on a port.

        Args:
            client_id: Identifier of the client to promote.
            port_name: Port on which to promote the client.

        Returns:
            AccessDecision: ``granted`` when the Client now drives; otherwise
            ``not_attached``, ``not_entitled``, ``denied_by_group_acl``,
            ``denied_by_access_default``, ``federation_denied``, or
            ``promote_failed`` with the holders that took the seat and the Port's
            capacity. ``mode`` is the mode the Client holds afterwards, so a
            refusal still reads read-only here and the Client keeps its read-only
            seat.
        """
        # Check if client is connected to this port
        if client_id not in self.client_port_map or self.client_port_map[client_id] != port_name:
            return decide(Code.NOT_ATTACHED, mode=Mode.READ_ONLY, client_id=client_id, port_name=port_name)

        try:
            port = self.port_manager.get_port(port_name)
        except Exception:
            port = None

        # Re-check the access ladder before granting, as an attach and a
        # take-over both do (SEC-07). ``port_manager.promote_client`` counts the
        # write slot and nothing else, so without this a Client that attached
        # read-only could drive the Port by asking: a read-only account, a name in
        # ``read_only_groups``, or an ``access_default: deny`` port. The ladder is
        # ``_taker_entitled``, which is the attach ladder without the slot
        # question; ``take_write_slot`` refuses the same identity with
        # ``not_entitled``, so a request must not be easier than a take.
        if port is not None:
            creds = self._take_write_slot_permissions(port, client_id, port_name)
            entitlement = (
                self._taker_entitled(port, port_name, creds[1], creds[0] or client_id)
                if creds is not None
                else Entitlement.denied(Code.NOT_ENTITLED)
            )
            if not entitlement.may_drive:
                refusal = entitlement.code or Code.NOT_ENTITLED
                self.logger.info("Refusing read-write for %s on port %s: %s", client_id, port_name, refusal.value)
                return decide(
                    refusal,
                    mode=Mode.READ_ONLY,
                    client_id=client_id,
                    port_name=port_name,
                    holders=self._holders_for(port),
                    capacity=_port_capacity(port),
                )

        # A federated (remote_muxcon) port's origin server is the sole authority on
        # its shared read-write slot (issue #52) - ask it before granting locally.
        if port is not None and hasattr(port, "request_read_write_for_client"):
            if not await self._request_federated_promotion(port, port_name, client_id):
                self.logger.info("Origin denied read-write promotion for %s on federated port %s", client_id, port_name)
                return decide(
                    Code.FEDERATION_DENIED,
                    mode=Mode.READ_ONLY,
                    client_id=client_id,
                    port_name=port_name,
                    holders=self._holders_for(port),
                    capacity=_port_capacity(port),
                )

        # Promote client
        success = await self.port_manager.promote_client(port_name, client_id)

        if not success:
            # Snapshot the holders after the attempt: whoever holds the seat now is
            # who the console names in the refusal.
            return self._seat_refusal(port, port_name, client_id)

        self.logger.info("Client %s promoted to read-write on port %s", client_id, port_name)
        try:
            await self.broadcast_presence(port_name)
        except Exception:
            self.logger.debug("broadcast_presence failed after promote for %s", port_name, exc_info=True)

        return decide(
            Code.GRANTED,
            mode=Mode.READ_WRITE,
            client_id=client_id,
            port_name=port_name,
            capacity=_port_capacity(port),
        )

    async def demote_client_to_read_only(self, client_id: str, port_name: str, notify_origin: bool = True) -> AccessDecision:
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
            AccessDecision: ``released`` when the Client gave the slot up. A
            failure keeps ``mode`` at read-write, because that is what the
            Client still holds: its console would otherwise drop to read-only
            and stop forwarding keystrokes while the Port still lists it as the
            writer (issue #98).
        """
        if client_id not in self.client_port_map or self.client_port_map[client_id] != port_name:
            return decide(Code.NOT_ATTACHED, mode=Mode.READ_WRITE, client_id=client_id, port_name=port_name)

        success = await self.port_manager.demote_client(port_name, client_id)
        try:
            port = self.port_manager.get_port(port_name)
        except Exception:
            port = None
        if not success:
            return decide(
                Code.DEMOTE_FAILED,
                mode=Mode.READ_WRITE,
                client_id=client_id,
                port_name=port_name,
                holders=self._holders_for(port),
                capacity=_port_capacity(port),
            )

        # Release the shared slot on the origin too (issue #52), so another
        # local or federated writer can be promoted. Best-effort: the local
        # demotion above already applies regardless of this call's outcome.
        if notify_origin and port is not None and hasattr(port, "release_read_write_for_client"):
            try:
                await port.release_read_write_for_client(client_id)
            except Exception:
                self.logger.debug("FEDRW release failed for %s on %s", client_id, port_name, exc_info=True)
        self.logger.info("Client %s demoted to read-only on port %s", client_id, port_name)
        try:
            await self.broadcast_presence(port_name)
        except Exception:
            self.logger.debug("broadcast_presence failed after demote for %s", port_name, exc_info=True)

        return decide(
            Code.RELEASED,
            mode=Mode.READ_ONLY,
            client_id=client_id,
            port_name=port_name,
            capacity=_port_capacity(port),
        )

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
        self.logger.info("Created forwarding task for %s → %s", port_name, client_id)

    def _stop_client_forwarding_task(self, client_id: str):
        """Cancel the forwarding task for a specific client.

        Args:
            client_id: Client whose task should be cancelled.
        """
        if client_id in self.data_forwarding_tasks:
            task = self.data_forwarding_tasks[client_id]
            task.cancel()
            del self.data_forwarding_tasks[client_id]
            self.logger.info("Stopped forwarding task for client %s", client_id)

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
                self.logger.debug("Data forwarder: About to call get_port_data for %s (poll #%s)", port_name, port_count)
        elif data:
            self.logger.debug("Data forwarder: get_port_data returned %s bytes (poll #%s)", len(data), port_count)
        else:
            # Only log every 20 empty reads to reduce spam
            if port_count % 20 == 0:
                self.logger.debug("Data forwarder: No data from port (poll #%s)", port_count)

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
        """Try mapped → multi-manager → legacy in order, returning on first success.

        The mapped step returns ``None`` to signal "no routing info or the
        cached manager threw" so the caller falls through; an explicit
        ``True``/``False`` is returned as-is even on a failed mapped send.
        """
        ok = await self._send_via_mapped(client_id, port_name, data)
        if ok is not None:
            return ok
        if await self._send_via_multi(client_id, data):
            return True
        return await self._send_via_legacy(client_id, port_name, data)

    async def _send_via_mapped(self, client_id: str, port_name: str, data: bytes) -> Optional[bool]:
        """Send through the per-client cached manager. ``None`` on no mapping or send throw."""
        mgr = self.client_to_manager.get(client_id)
        if mgr is None:
            return None
        try:
            if "loopback" in port_name:
                self.logger.debug("Data forwarder: About to call send_data_to_client (mapped)")
            ok = await mgr.send_data_to_client(client_id, data)
            if "loopback" in port_name:
                self.logger.debug("FORWARD TO CLIENT RESULT (mapped): %s", "Success" if ok else "Failed")
            return ok
        except Exception as e:
            self.logger.warning("Client mapped manager send failed for %s: %s", client_id, e, exc_info=True)
            return None

    async def _send_via_multi(self, client_id: str, data: bytes) -> bool:
        """Try each known client manager; return True on first success."""
        any_success = False
        if not getattr(self, "client_managers", None):
            return False
        for m in list(self.client_managers):
            try:
                ok = await m.send_data_to_client(client_id, data)
                if ok:
                    any_success = True
                    self.client_to_manager[client_id] = m
                    break
            except Exception as e:
                self.logger.debug("Manager %s send error: %s", getattr(m, "name", type(m).__name__), e)
        return any_success

    async def _send_via_legacy(self, client_id: str, port_name: str, data: bytes) -> bool:
        """Fall back to the single legacy single-manager (if set) and cache the mapping."""
        if not hasattr(self, "client_manager") or not self.client_manager:
            self.logger.warning("No client manager available to forward data to client %s", client_id)
            return False
        if "loopback" in port_name:
            self.logger.debug("Data forwarder: About to call send_data_to_client (legacy)")
        success = await self.client_manager.send_data_to_client(client_id, data)
        if "loopback" in port_name:
            self.logger.debug("FORWARD TO CLIENT RESULT (legacy): %s", "Success" if success else "Failed")
        if success:
            try:
                self.client_to_manager[client_id] = self.client_manager
            except Exception:
                # justification: idempotent cache write; failure re-enters fallback on next send
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
                self.logger.error("Port %s has no client_queues; forwarding for %s cannot start", port_name, client_id)
                return
            q = port_wrapper.client_queues.get(client_id)
            if q is None:
                self.logger.error("No delivery queue for %s on %s", client_id, port_name)
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
                        self.logger.warning("Failed to send data to client %s on %s", client_id, port_name)
                    else:
                        self.logger.debug("Forwarded %s bytes from %s to %s", len(data), port_name, client_id)

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.logger.error("Error forwarding data to %s on %s: %s", client_id, port_name, e, exc_info=True)
                    await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            self.logger.info(f"Data forwarding for {port_name} \u2192 {client_id} was cancelled")
        except Exception as e:
            self.logger.error("Unexpected error in forwarding task for %s: %s", client_id, e, exc_info=True)

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
                    "Registered client manager: %s", getattr(client_manager, "name", type(client_manager).__name__)
                )
        except Exception:
            # justification: optional bookkeeping; the single-manager field was already set above
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
            # justification: idempotent routing cache; delivery falls back to the manager broadcast loop
            pass

    def unregister_client_channel(self, client_id: str) -> None:
        """Remove routing association for a client id.

        Should be called when a client detaches from a port or disconnects.
        """
        try:
            if client_id in self.client_to_manager:
                del self.client_to_manager[client_id]
        except Exception:
            # justification: idempotent routing cleanup; a stale entry is harmless and re-resolved on re-registration
            pass

    def register_client_port(self, client_id: str, port_name: str) -> None:
        """Map a client id to a port, bypassing the full `connect_client_to_port` flow.

        For federated pseudo-clients (muxcon's "fed:<peer_key>:<stream_id>" ids,
        added directly to PortManager by UnifiedMuxConAdapter, never through
        `connect_client_to_port`) that still need to be reachable by the same
        generic `demote_client_to_read_only`/`take_write_slot` machinery every
        other adapter's real clients go through - otherwise a locally initiated
        write-slot takeover against a federated read-write holder silently
        fails to actually demote it (see `take_write_slot`).
        """
        self.client_port_map[client_id] = port_name

    def unregister_client_port(self, client_id: str) -> None:
        """Remove a mapping registered via `register_client_port`."""
        self.client_port_map.pop(client_id, None)

    def _taker_display(self, taker_id: str, taker_username: str) -> str:
        """The name a demoted Client sees in "taken by ...": the taker's Holder Display Name.

        The same ``username@ip`` a holders list shows, because two people can share one
        account name and this notice is the victim's only clue about who displaced it.
        Without a source IP, the name alone is better than a made-up ``@unknown``.
        """
        name = taker_username or "unknown"
        if not taker_id:
            return name
        meta = self._client_meta_for(taker_id)
        ip = str(meta.get("ip") or "")
        return f"{name}@{ip}" if ip and ip != "unknown" else name

    def _client_meta_for(self, client_id: str) -> Dict[str, Any]:
        """The owning adapter's metadata for one Client, or an empty dict."""
        mgr = self.client_to_manager.get(client_id)
        resolver = getattr(mgr, "_resolve_client_meta", None)
        if callable(resolver):
            try:
                return dict(resolver(client_id) or {})
            except Exception:
                # justification: optional metadata; the notice falls back to the username
                pass
        return {}

    async def send_control_frame_to_client(self, client_id: str, payload: Any) -> bool:
        """Deliver an access-mode control frame to a client via its owning adapter.

        Routes through the adapter registered for `client_id` (TCP, telnet, or
        web console), regardless of which adapter originated the request, so
        e.g. a CLI force-take can notify a web console client and vice versa.
        Each adapter renders the frame its own surface uses.

        Args:
            client_id: Target client identifier.
            payload: an ``AccessDecision`` (rendered with ``frame()``), or a
                JSON-serializable control message from a plugin.

        Returns:
            bool: True if the owning adapter accepted and sent the frame.
        """
        if isinstance(payload, AccessDecision):
            payload = payload.frame()
        mgr = self.client_to_manager.get(client_id)
        if mgr is None or not hasattr(mgr, "send_control_frame_to_client"):
            return False
        try:
            return bool(await mgr.send_control_frame_to_client(client_id, payload))
        except Exception:
            self.logger.debug("send_control_frame_to_client failed for %s", client_id, exc_info=True)
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
                self.logger.debug("broadcast_control_frame_to_port failed for %s", client_id, exc_info=True)
        return delivered

    def _resolve_take_target(
        self, port: Any, port_name: str, taker_id: str, target: Optional[str] = None
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Code]]:
        """Pick the holder a write-slot takeover demotes (issue #59 Part 2).

        With an explicit ``target`` that record must be a current read-write
        holder (the taker's own seat can never be taken). The name may be the
        full ``client_id`` or the short form this server prints for it in the
        holders list, because that label is what an operator reads (see
        ``_match_take_target``). Without a target, fall back to the most recently
        attached read-write holder that is not the taker itself (max
        ``connected_at``; records lacking the field compare as 0).

        Local ``fed:`` pseudo-clients (muxcon's mirrored holders on a local
        port) are legitimate targets - their demotion reaches the owning
        peer via the normal channel routing.

        Returns:
            Tuple[Optional[dict], Optional[Code]]: (the chosen record, None)
            on success, else (None, ``no_holder`` or ``invalid_target``).
        """
        holders = [
            c
            for c in getattr(port, "connected_clients", [])
            if c.get("mode") == Mode.READ_WRITE.value and c.get("client_id") != taker_id
        ]
        if not holders:
            return None, Code.NO_HOLDER
        if target is not None:
            chosen = _match_take_target(holders, target)
            if chosen is None:
                return None, Code.INVALID_TARGET
            return chosen, None
        chosen = max(holders, key=lambda c: (c.get("connected_at") or 0.0))
        return chosen, None

    async def _take_slot_local(
        self, port: Any, port_name: str, taker_id: str, taker_username: str, victim: Dict[str, Any]
    ) -> AccessDecision:
        """Perform a take on a LOCAL port: demote the victim, promote the taker.

        The slot count is invariant: a takeover transfers the one writer; it
        never briefly creates a second. If the taker's promote fails after
        the victim was demoted (for example the seat vanished in between),
        the victim is restored to read-write so the port is never left with
        zero writers when one was held before.

        Returns:
            AccessDecision: ``granted`` with the demoted holder as its victim, or
            the reason the transfer did not happen.
        """
        victim_id = victim.get("client_id")
        victim_username = victim.get("username", "unknown")
        victim_holder = Holder(
            client_id=str(victim_id or ""),
            username=str(victim_username or "unknown"),
            ip=self._resolve_client_ip(str(victim_id or "")),
        )
        if not (await self.demote_client_to_read_only(victim_id, port_name)).ok:
            return decide(Code.VICTIM_NOT_CURRENT, mode=Mode.READ_ONLY, client_id=taker_id, port_name=port_name)
        if not (await self.promote_client_to_read_write(taker_id, port_name)).ok:
            # Restore the victim: the transfer failed, the port keeps its writer.
            await self.port_manager.promote_client(port_name, victim_id)
            return self._seat_refusal(port, port_name, taker_id)
        # Push the victim its takeover notice. The producer decides whom to notify;
        # the notice carries the wording every surface shows, including who took it.
        await self.send_control_frame_to_client(
            victim_id,
            notice(
                Code.DEMOTED,
                client_id=str(victim_id or ""),
                port_name=port_name,
                actor=self._taker_display(taker_id, taker_username),
            ),
        )
        self.logger.info(
            f"WRITE-SLOT TAKEOVER port={port_name} "
            f"taker={taker_username}:{taker_id} victim={victim_username}:{victim_id} time={time.time():.3f}"
        )
        try:
            DataLogger.get().record_meta(
                port_name=port_name,
                event="write_slot_takeover",
                client_id=str(taker_id),
                meta={
                    "taker": taker_id,
                    "taker_username": taker_username,
                    "victim": victim_id,
                    "victim_username": victim_username,
                },
                port_obj=port,
            )
        except Exception:
            self.logger.debug("DataLogger takeover record failed for %s", port_name, exc_info=True)
        return decide(
            Code.GRANTED,
            mode=Mode.READ_WRITE,
            client_id=taker_id,
            port_name=port_name,
            victim=victim_holder,
            capacity=_port_capacity(port),
        )

    def _log_empty_slot_grant(self, port: Any, port_name: str, taker_id: str, taker_username: str) -> None:
        """Audit a take that grabbed an EMPTY write slot directly.

        Same DataLogger event as a demoting takeover, with ``victim`` empty:
        one grep covers every slot acquisition through the force verb.
        """
        self.logger.info(
            f"WRITE-SLOT TAKEN (empty slot) port={port_name} " f"taker={taker_username}:{taker_id} time={time.time():.3f}"
        )
        try:
            DataLogger.get().record_meta(
                port_name=port_name,
                event="write_slot_takeover",
                client_id=str(taker_id),
                meta={
                    "taker": taker_id,
                    "taker_username": taker_username,
                    "victim": None,
                },
                port_obj=port,
            )
        except Exception:
            self.logger.debug("DataLogger takeover record failed for %s", port_name, exc_info=True)

    def _take_write_slot_port(self, taker_id: str, port_name: str) -> Tuple[Optional[Any], Optional[Code]]:
        """(port, None): proceed when attached + port exists, else (None, reason code)."""
        if self.client_port_map.get(taker_id) != port_name:
            return None, Code.NOT_ATTACHED
        try:
            port = self.port_manager.get_port(port_name)
        except Exception:
            port = None
        if port is None:
            return None, Code.PORT_MISSING
        return port, None

    def _take_write_slot_permissions(self, port: Any, taker_id: str, port_name: str) -> Optional[Tuple[str, str]]:
        """(taker_username, permissions) for one takeover, or None if not entitled."""
        taker_username = ""
        for c in getattr(port, "connected_clients", []) or []:
            if c.get("client_id") == taker_id:
                taker_username = str(c.get("username") or "")
                break
        permissions: Optional[str] = None
        if taker_username:
            try:
                permissions = self.auth_manager.get_user_permissions(taker_username)
            except Exception:
                permissions = None
        if permissions is None:
            permissions = self._client_permissions(port_name, taker_id)
        if permissions is None:
            return None
        return taker_username, permissions

    async def _take_write_slot_federated(
        self, port: Any, taker_id: str, port_name: str, taker_username: str, target: Optional[str]
    ) -> AccessDecision:
        """Arbitrate a takeover on a federated port: the origin is the authority."""
        try:
            origin_mode = await port.take_write_slot_for_client(taker_id, target)
        except Exception:
            self.logger.debug("FEDRW TAKE request failed for %s on %s", taker_id, port_name, exc_info=True)
            origin_mode = "read-only"
        if origin_mode != Mode.READ_WRITE.value:
            return decide(
                Code.FEDERATION_DENIED,
                mode=Mode.READ_ONLY,
                client_id=taker_id,
                port_name=port_name,
                holders=self._holders_for(port),
                capacity=_port_capacity(port),
            )
        # The origin granted the takeover. Mirror it onto our LOCAL view of the
        # taker: the origin's promote only reached the origin's
        # "fed:<peer>:<sid>" pseudo-client, not this node's client record - and
        # the local write path gates on that record, so without this line the
        # taker would show read-write but stay blocked (WRITE BLOCKED) until
        # reconnect. Safe order: the origin relays the victim's demotion
        # (FEDRWACK, demoting the local mirror) BEFORE the taker's take-ack, in
        # order on the same connection, so when this node promotes the taker
        # the slot already looks free.
        if not (await self.promote_client_to_read_write(taker_id, port_name)).ok:
            # The origin held the transfer, but the local record could not
            # follow (the taker has no local seat, or the victim's demotion
            # relay did not land here yet). Report the take as FAILED: a
            # success the local write gate cannot honor would just reproduce
            # the read-write-but-blocked state (the taker sees RW, every
            # keystroke is WRITE BLOCKED until reconnect).
            self.logger.warning(
                f"Federated takeover granted by the origin, but mirroring it locally " f"failed for {taker_id} on {port_name}"
            )
            return decide(
                Code.PROMOTE_FAILED,
                mode=Mode.READ_ONLY,
                client_id=taker_id,
                port_name=port_name,
                holders=self._holders_for(port),
                capacity=_port_capacity(port),
            )
        self._log_takeover_audit(port, port_name, taker_id, taker_username, target or "(latest)")
        return decide(
            Code.GRANTED,
            mode=Mode.READ_WRITE,
            client_id=taker_id,
            port_name=port_name,
            victim=self._victim_holder(port, target),
            capacity=_port_capacity(port),
        )

    async def take_write_slot(self, taker_id: str, port_name: str, target: Optional[str] = None) -> AccessDecision:
        """Take the port's write slot from another holder (issue #59 Part 2).

        The single write-slot takeover for every client (TCP control frame,
        web console, telnet/SSH Control menu, CLI). Replaces the old
        `force_promote_client` (which demoted ALL holders, checked no
        entitlement, and carried no audit line).

        Rules:
        - ``one``-mode ports only: ``multiple`` gives everyone write (return
          ``already_rw`` when the taker has it), ``none`` has no writer to
          take from.
        - The taker must be attached here and write-ENTITLED (the #58 ladder
          without the slot check, `_taker_entitled`) - an attached read-only
          seat can never take the slot.
        - The victim is the chosen target, else the most recently attached
          other read-write holder (see `_resolve_take_target`). When nobody
          holds the slot, a no-target take takes the EMPTY slot and promotes
          the taker directly; a named target that matches no holder is
          refused.
        - Federated (`remote_muxcon`) ports: the origin is the capacity
          authority and sees every holder this server cannot (a client local
          to the origin, another peer's writer). The origin re-checks that the
          taker's own mirror is connected there and arbitrates the transfer
          via a FEDRW TAKE frame; the origin also audits the transfer. When
          the origin grants it, the TAKER'S OWN local record is mirrored to
          read-write here too - the local data path gates writes on that
          record, so without the mirror the taker's console shows read-write
          but every keystroke is `WRITE BLOCKED` until reconnect. (The
          victim's mirror is demoted in order: the origin relays the
          victim's demotion before the taker's ack reaches this node.)

        Args:
            taker_id: Client session requesting the takeover.
            port_name: Port to take the slot on.
            target: Optional client_id of the holder to demote. When None the
                most recently attached other read-write holder is used.

        Returns:
            AccessDecision: the outcome of the take. Successes are ``granted``
                (a holder was displaced, or the empty slot was taken) and
                ``already_rw`` (the taker already had the slot; nobody was
                demoted); ``granted`` names the displaced holder so the taker's
                console shows its "taken from" line (issue #61, issue #98).
                Refusals: ``not_attached``, ``port_missing``, ``not_entitled``,
                ``denied_by_group_acl``, ``denied_by_access_default``,
                ``no_holder``, ``invalid_target``, ``victim_not_current``,
                ``promote_failed``, and ``federation_denied``. A ``none`` Port and
                a named target that matches no holder both refuse: the first
                with ``promote_failed`` and no write slot, the second with
                ``invalid_target``.
        """
        port, reason = self._take_write_slot_port(taker_id, port_name)
        if port is None:
            return decide(reason or Code.NOT_ATTACHED, mode=Mode.READ_ONLY, client_id=taker_id, port_name=port_name)

        # The taker's username + entitlement are resolved before the port mode
        # gate and federation: the origin only sees a "federation:<peer>"
        # pseudo-client there and cannot adjudicate our local user's
        # group/permission standing (SEC-07: a read-only seat must not take the
        # slot, local or not).
        creds = self._take_write_slot_permissions(port, taker_id, port_name)
        if creds is None:
            return decide(Code.NOT_ENTITLED, mode=Mode.READ_ONLY, client_id=taker_id, port_name=port_name)
        taker_username, permissions = creds
        entitlement = self._taker_entitled(port, port_name, permissions, taker_username or taker_id)
        if not entitlement.may_drive:
            return decide(
                entitlement.code or Code.NOT_ENTITLED,
                mode=Mode.READ_ONLY,
                client_id=taker_id,
                port_name=port_name,
                holders=self._holders_for(port),
                capacity=_port_capacity(port),
            )

        # Federated port: the origin arbitrates (it sees every holder).
        if hasattr(port, "take_write_slot_for_client"):
            return await self._take_write_slot_federated(port, taker_id, port_name, taker_username, target)
        return await self._take_write_slot_local(port, port_name, taker_id, taker_username, target)

    async def _take_write_slot_local(
        self, port: Any, port_name: str, taker_id: str, taker_username: str, target: Optional[str]
    ) -> AccessDecision:
        """Local (non-federated) takeover: the mode gate, then the transfer.

        ``multiple`` gives everyone write (``already_rw`` when the taker has
        it), ``none`` has no writer to take. For ``one`` ports, the targeted
        transfer runs when a victim resolves; otherwise a no-target take
        promotes into the EMPTY slot (a named target matching no holder is
        refused - the named victim does not (or no longer) exist).
        """
        capacity = _port_capacity(port)
        mode = wire_to_mode(capacity)
        if mode == "multiple":
            if self._is_client_rw(port, taker_id):
                return decide(
                    Code.ALREADY_RW, mode=Mode.READ_WRITE, client_id=taker_id, port_name=port_name, capacity=capacity
                )
            # Every holder may write at once here, so there is no seat to take.
            return self._no_seat_refusal(port, port_name, taker_id)
        if mode == "none":
            return self._no_seat_refusal(port, port_name, taker_id)
        victim, reason = self._resolve_take_target(port, port_name, taker_id, target)
        if victim is not None:
            return await self._take_slot_local(port, port_name, taker_id, taker_username, victim)
        # Only an unresolved NAMED target stops the take here. An empty slot with no
        # target is the legacy force behaviour: the taker takes the empty slot.
        if target is not None or reason is not Code.NO_HOLDER:
            return decide(
                reason or Code.NO_HOLDER,
                mode=Mode.READ_ONLY,
                client_id=taker_id,
                port_name=port_name,
                holders=self._holders_for(port),
                capacity=capacity,
            )
        if self._is_client_rw(port, taker_id):
            return decide(Code.ALREADY_RW, mode=Mode.READ_WRITE, client_id=taker_id, port_name=port_name, capacity=capacity)
        if not (await self.promote_client_to_read_write(taker_id, port_name)).ok:
            return self._seat_refusal(port, port_name, taker_id)
        self._log_empty_slot_grant(port, port_name, taker_id, taker_username)
        return decide(Code.GRANTED, mode=Mode.READ_WRITE, client_id=taker_id, port_name=port_name, capacity=capacity)

    def _seat_refusal(self, port: Any, port_name: str, client_id: str) -> AccessDecision:
        """Refuse a request to drive because no free Write slot was there.

        A ``none`` Port has no slot at all and a ``multiple`` Port has none to take. A
        one-slot Port that refused the promote did have a seat, so the server own
        failure is the honest answer there: ``promote_failed``. The wording lives in the
        rows; this is the one place that picks between them, so the same facts read the
        same everywhere.
        """
        capacity = write_capacity(getattr(port, "max_read_write_users", 1))
        if capacity == 0:
            code = Code.NO_WRITE_SLOT
        elif capacity > 1:
            code = Code.TAKE_WITHOUT_SLOT
        else:
            code = Code.PROMOTE_FAILED
        return decide(
            code,
            mode=Mode.READ_ONLY,
            client_id=client_id,
            port_name=port_name,
            holders=self._holders_for(port),
            capacity=_port_capacity(port),
        )

    def _no_seat_refusal(self, port: Any, port_name: str, taker_id: str) -> AccessDecision:
        """Refuse a take for want of a seat, naming who holds it and the limit.

        One refusal shape for every "there was nothing to take" outcome, so a
        console renders it once: a ``none`` Port, and a ``multiple`` Port where
        this verb is meaningless because everyone may hold write at once.
        """
        return self._seat_refusal(port, port_name, taker_id)

    def _victim_holder(self, port: Any, target: Optional[str]) -> Optional[Holder]:
        """The holder a takeover displaced, labelled, for the taker's "Taken from" line.

        ``None`` when ``target`` is empty or names no local record: on a
        federated Port the demoted holder lives on the origin and is not in the
        local ``connected_clients``, so the taker simply sees "granted". Matched
        by id regardless of mode, because the victim is demoted by the time this
        runs.
        """
        if not target:
            return None
        for c in getattr(port, "connected_clients", []) or []:
            if c.get("client_id") == target:
                victim_id = str(c.get("client_id") or "")
                return Holder(
                    client_id=victim_id,
                    username=str(c.get("username") or "unknown"),
                    ip=self._resolve_client_ip(victim_id),
                )
        return None

    def _client_permissions(self, port_name: str, client_id: str) -> Optional[str]:
        """Recover the global permission for an attached client whose adapter
        did not expose it via AuthManager name resolution (best-effort)."""
        mgr = self.client_to_manager.get(client_id)
        resolver = getattr(mgr, "_resolve_client_meta", None)
        if callable(resolver):
            try:
                meta = resolver(client_id) or {}
                username = meta.get("username")
                if username:
                    return self.auth_manager.get_user_permissions(username)
            except Exception:
                # justification: optional metadata; the caller default applies
                pass
        return None

    def _is_client_rw(self, port: Any, client_id: str) -> bool:
        """True when `client_id` currently holds read-write on `port`."""
        return any(
            c.get("client_id") == client_id and c.get("mode") == "read-write" for c in getattr(port, "connected_clients", [])
        )

    def _log_takeover_audit(self, port: Any, port_name: str, taker_id: str, taker_username: str, victim_id: str) -> None:
        """One audit line per takeover (taker, victim, port, time + DataLogger event)."""
        self.logger.info(
            f"WRITE-SLOT TAKEOVER (federation) port={port_name} "
            f"taker={taker_username}:{taker_id} victim={victim_id} time={time.time():.3f}"
        )
        try:
            DataLogger.get().record_meta(
                port_name=port_name,
                event="write_slot_takeover",
                client_id=str(taker_id),
                meta={"taker": taker_id, "taker_username": taker_username, "victim": victim_id},
                port_obj=port,
            )
        except Exception:
            self.logger.debug("DataLogger takeover record failed for %s", port_name, exc_info=True)

    def get_rw_holders_display(self, port_name: str) -> List[str]:
        """Return one entry per read-write client on a port, labeled
        ``[id] username@ip (rw)`` (see `holder_id_short` for the id form).

        The label comes from ``Holder.label`` in the Access decision module, so
        every console shows the same text for the same Client (issue #98). It
        resolves IPs across adapter types via each owning adapter's
        `_resolve_client_meta`, so e.g. a telnet client can see that a web
        console user currently holds read-write access. The label doubles as the
        target for a takeover: the `client_id` in brackets is exactly the value
        the `force_promote` target field matches on.
        """
        try:
            port = self.port_manager.ports.get(port_name) if hasattr(self.port_manager, "ports") else None
        except Exception:
            port = None
        return [h.label for h in self._holders_for(port)]

    def rw_holders_frame(self, port_name: Optional[str]) -> Dict[str, Any]:
        """The one answer to "who holds the Write slot on this Port?".

        Serves the ``query_rw_holders`` control frame and the Control menu ``w``
        command, on every surface, from this one builder (issue #98).
        """
        try:
            port = self.port_manager.ports.get(port_name) if hasattr(self.port_manager, "ports") else None
        except Exception:
            port = None
        return holders_frame(self._holders_for(port), capacity=_port_capacity(port))

    def _resolve_client_ip(self, client_id: str) -> str:
        """Resolve a client's source IP via its owning adapter's `_resolve_client_meta`."""
        return str(self._client_meta_for(client_id).get("ip") or "unknown")

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
                self.logger.debug("notify_meta_updated(presence_changed) failed for %s", port_name, exc_info=True)
        return await self.broadcast_control_frame_to_port(port_name, presence_frame(viewers))

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
            # justification: heuristic lookup; defaulting to read-only is safe
            pass
        return "read-only"

        return "read-only"  # Default fallback
