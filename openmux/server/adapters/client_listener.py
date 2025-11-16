"""Client Listener Adapter (server-side TCP listener).

Accepts inbound OpenMux client connections over TCP (contrast with
the ClientInitiator adapter which initiates outbound connections). It performs a
three-phase interaction model with each client:

    1. Authentication (simple line-oriented, pluggable via auth manager).
    2. Command phase (LIST / CONNECT / DISCONNECT / QUIT semantics).
    3. Character streaming when attached to a port (raw byte forwarding).

The adapter multiplexes multiple simultaneously connected clients, tracks
their associated port attachments, and mediates broadcast of port output
back to all attached clients while supporting selective echo suppression.
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Set
from openmux.server.port_utils import safe_get_port

from .base_adapter import AdapterCapability, BaseGenericAdapter


class TcpServerAdapter(BaseGenericAdapter):
    """Server-side adapter handling multiple OpenMux TCP clients.

    High-level responsibilities:
        * Listen and accept new TCP connections (asyncio server).
        * Run an authentication handshake (pluggable auth manager).
        * Provide a small command vocabulary (LIST / CONNECT / DISCONNECT / QUIT).
        * Transition authenticated + connected clients into raw character mode.
        * Forward port output to all attached clients with optional echo rules.
        * Maintain per-port client membership for broadcast efficiency.

    Attributes:
        host: Bind host/IP string.
        port: Listening TCP port number.
        max_connections: Soft upper bound on concurrent sessions.
        connection_timeout: (Reserved) timeouts for connection ops.
        server: Asyncio server instance once started.
        clients: Mapping of client_id -> ``ClientSession``.
        port_clients: Mapping of port_name -> list of client_ids attached.
        console_manager: Console/port manager reference (injected externally).
        auth_manager: Authentication manager implementing ``authenticate``.
    """

    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)

        # TCP server configuration
        # Default bindpoint tightened to loopback for safety; override in config to expose externally
        # Enabled flag: when explicitly set to false, the adapter will no-op on start()
        try:
            self.enabled = bool(config.get("enabled", True))
        except Exception:
            self.enabled = True
        self.host = config.get("host", "127.0.0.1")
        self.port = config.get("port", 8023)
        self.max_connections = config.get("max_connections", 100)
        self.connection_timeout = config.get("connection_timeout", 30)

        # Server state
        self.server = None
        self.clients = {}  # client_id -> ClientSession
        self.port_clients = {}  # port_name -> [client_ids]

        # Components set by server
        self.console_manager = None
        self.auth_manager = None

        # Logger
        self.logger = logging.getLogger(f"openmux.adapter.client_listener.{self.name}")

    def get_capabilities(self) -> Set[AdapterCapability]:
        """Return capability flags implemented by this adapter.

        Returns:
            Set[AdapterCapability]: Declares connection acceptance, duplex
            data, multiplexed logical streams, and authentication support.
        """
        return {
            AdapterCapability.ACCEPTS_CONNECTIONS,
            AdapterCapability.BIDIRECTIONAL_DATA,
            AdapterCapability.MULTIPLEXED_STREAMS,
            AdapterCapability.AUTHENTICATION,
        }

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> bool:
        """Validate listener configuration structure.

        Args:
            config: Raw adapter configuration mapping.

        Returns:
            bool: True if the structure is valid. Host/port are optional and
            will default to safe values when omitted.
        """
        # Enforce wrapped legacy shape {"client_listener": {...}}; accept empty dict
        if not isinstance(config, dict):
            return False
        tcp_config = config.get("client_listener")
        if not isinstance(tcp_config, dict):
            return False

        # Host and port are optional; if omitted, defaults (127.0.0.1, 8023) are used.
        # If present, validate types and ranges.
        host = tcp_config.get("host", None)
        port = tcp_config.get("port", None)

        # Validate host only if provided
        if host is not None:
            if not isinstance(host, str) or host.strip() == "":
                return False

        # Validate port only if provided
        if port is not None:
            try:
                port_i = int(port)
            except Exception:
                return False
            if port_i < 1 or port_i > 65535:
                return False

        return True

    def get_adapter_type(self) -> str:
        """Return adapter type identifier."""
        return "client_listener"

    def get_port_configurations(self) -> Dict[str, Dict[str, Any]]:
        """Return empty mapping (server does not create or own ports)."""
        return {}

    def set_console_manager(self, console_manager):
        """Attach console/port manager used for port attach + data forwarding.

        Registers this adapter as a client manager if the console manager
        exposes a registration hook.

        Args:
            console_manager: Manager providing `register_client_manager` and
                a `port_manager` reference for port lifecycle + I/O.
        """
        self.console_manager = console_manager
        # Register as a client manager for data forwarding
        if hasattr(console_manager, "register_client_manager"):
            console_manager.register_client_manager(self)
        # Subscribe to PortManager meta updates to notify attached TCP clients about
        # federated port up/down transitions.
        try:
            pm = getattr(console_manager, "port_manager", None)
            if pm and hasattr(pm, "register_meta_listener"):
                pm.register_meta_listener(self._on_port_meta_update)  # type: ignore[arg-type]
        except Exception:
            pass

    def _emit_notice_to_port_clients(self, port_name: str, message: str) -> None:
        try:
            for cid in list(self.port_clients.get(port_name, []) or []):
                try:
                    if cid in self.clients:
                        # Fire and forget; best-effort
                        asyncio.create_task(self.clients[cid].send_raw_data(message.encode("utf-8")))
                except Exception:
                    continue
        except Exception:
            pass

    def _on_port_meta_update(self, port_name: str, changes: Optional[Dict[str, Any]] = None):
        """Relay federated port up/down notices to attached TCP clients.

        Expects PortManager to call this when muxcon reports disconnection or reconnection.
        """
        try:
            if not port_name or port_name not in self.port_clients:
                return
            # Down events from muxcon
            if isinstance(changes, dict) and changes.get("event") in ("federated_disconnected", "federated_cached_offline"):
                self._emit_notice_to_port_clients(port_name, "\r\n[Port disconnected on server]\r\n")
                return
            # On general meta update, check if port transitioned to connected
            pm = getattr(self.console_manager, "port_manager", None)
            if pm:
                port_obj = safe_get_port(pm, port_name)
                if port_obj is not None:
                    is_up = bool(getattr(port_obj, "is_connected", True))
                    # If reconnection occurred, inform clients
                    if is_up and isinstance(changes, dict) and changes.get("event") in (
                        "federated_port_registered", "client_connected", "port_registered"
                    ):
                        self._emit_notice_to_port_clients(port_name, "\r\n[Reconnected]\r\n")
        except Exception:
            # Non-fatal
            pass

    def set_auth_manager(self, auth_manager):
        """Attach authentication manager implementation.

        Args:
            auth_manager: Object exposing ``authenticate(username, password)``.
        """
        self.auth_manager = auth_manager

    async def start(self) -> bool:
        """Start listening server and begin accepting clients.

        Returns:
            bool: True on successful bind and serving; False otherwise.
        """
        # Respect explicit enable/disable flag. When disabled, skip binding but
        # report success so overall server startup isn't treated as a failure.
        if hasattr(self, "enabled") and not self.enabled:
            # Keep is_running False to reflect disabled/stopped state
            self.logger.info("Client listener '%s' is disabled via configuration; skipping bind", self.name)
            return True
        try:
            self.server = await asyncio.start_server(self.handle_client_connection, self.host, self.port)

            self.is_running = True
            self.logger.info(f"TCP server started on {self.host}:{self.port}")

            # Start serving
            await self.server.start_serving()
            return True

        except Exception as e:
            self.logger.error(f"Failed to start TCP server on {self.host}:{self.port}: {e}", exc_info=True)
            return False

    async def stop(self) -> None:
        """Stop server and disconnect all clients (best-effort)."""
        self.is_running = False

        # Proactively notify clients about shutdown before closing connections
        try:
            if self.clients:
                self.logger.info(f"Broadcasting server shutdown to {len(self.clients)} clients")
                await self._broadcast_shutdown_message()
        except Exception as e:
            self.logger.warning(f"Failed broadcasting shutdown message: {e}", exc_info=True)

        # Disconnect all clients
        for client_id in list(self.clients.keys()):
            await self.disconnect_client(client_id)

        # Stop server
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

        self.logger.info("TCP server stopped")

    async def handle_client_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Serve lifecycle of a newly accepted client connection.

        Creates a ``ClientSession`` wrapper, then delegates protocol flow to
        ``handle_client_protocol``. Ensures cleanup and disconnection on any
        exception during processing.

        Args:
            reader: StreamReader for client socket.
            writer: StreamWriter for client socket.
        """
        client_id = str(uuid.uuid4())
        address = writer.get_extra_info("peername")[0]

        self.logger.info(f"New TCP connection from {address}")

        try:
            # Create client session
            client_session = ClientSession(client_id, address, reader, writer, self.logger)
            self.clients[client_id] = client_session

            # Handle client protocol
            await self.handle_client_protocol(client_session)

        except Exception as e:
            self.logger.error(f"Error handling client {client_id}: {e}", exc_info=True)
        finally:
            await self.disconnect_client(client_id)

    async def handle_client_protocol(self, client: "ClientSession"):
        """Run full client protocol sequence.

        Performs authentication, executes the command phase loop, and if a
        port attachment succeeds transitions into raw character forwarding.

        Args:
            client: Active client session wrapper.
        """
        # Send authentication prompt
        await client.send_line("Authentication required")

        # Authentication phase (line-based mode)
        if not await self.authenticate_client_char_mode(client):
            await client.send_line("AUTH:FAILED:Authentication failed")
            return

        self.logger.info(f"Client {client.client_id} authenticated as {client.username}")
        await client.send_line(f"AUTH:SUCCESS:Welcome {client.username}")

        # Command handling phase (line-based mode)
        await self.handle_command_phase(client)

        # If connected to a port, switch to character mode
        if client.connected_port:
            await self.handle_character_mode(client)

    async def authenticate_client_char_mode(self, client: "ClientSession") -> bool:
        """Authenticate client using line emulation over char stream.

        Reads a single AUTH line of the form ``AUTH:USER:<username>:<password>``.
        If no auth manager is configured, anonymous access is granted.

        Args:
            client: Client session object.

        Returns:
            bool: True if authentication succeeded (or anonymous permitted).
        """
        if not self.auth_manager:
            self.logger.warning("No auth manager - allowing unauthenticated access")
            client.username = "anonymous"
            client.is_authenticated = True
            return True

        try:
            # Collect characters until we have a complete line
            auth_line = await self.collect_line_from_chars(client)
            if not auth_line:
                return False

            self.logger.debug(f"Received auth command: {auth_line}")

            # Parse authentication command
            if auth_line.startswith("AUTH:PK:INIT:"):
                # Public key auth initiation: AUTH:PK:INIT:<username>[:<key_id>]
                parts = auth_line.split(":")
                # Expected min parts: ['AUTH','PK','INIT','username']
                if len(parts) >= 4:
                    username = parts[3]
                    key_id = parts[4] if len(parts) >= 5 and parts[4] else None
                    if not hasattr(self.auth_manager, "start_pubkey_challenge"):
                        self.logger.warning("Public key auth not supported by auth manager")
                        return False
                    ch = self.auth_manager.start_pubkey_challenge(username, key_id)
                    if not ch:
                        await client.send_line("AUTH:FAILED:Authentication failed")
                        return False
                    await client.send_line(f"AUTH:PK:CHALLENGE:{ch['key_id']}:{ch['nonce']}")
                    # Wait for response line
                    resp_line = await self.collect_line_from_chars(client)
                    if not resp_line or not resp_line.startswith("AUTH:PK:RESPONSE:"):
                        return False
                    rparts = resp_line.split(":")
                    # AUTH:PK:RESPONSE:<key_id>:<signature_b64>
                    if len(rparts) < 5:
                        return False
                    resp_key_id = rparts[3]
                    signature_b64 = rparts[4]
                    ok = False
                    try:
                        ok = self.auth_manager.verify_pubkey_response(username, resp_key_id, signature_b64)
                    except Exception:
                        ok = False
                    if ok:
                        client.username = username
                        client.is_authenticated = True
                        return True
                    await client.send_line("AUTH:FAILED:Authentication failed")
                    return False
            elif auth_line.startswith("AUTH:USER:HMAC:"):
                # HMAC password challenge flow
                parts = auth_line.split(":")
                # AUTH:USER:HMAC:<username>
                if len(parts) >= 4:
                    username = parts[3]
                    src_ip = client.address[0] if hasattr(client, "address") and client.address else None
                    if hasattr(self.auth_manager, "is_user_locked") and self.auth_manager.is_user_locked(username, src_ip):
                        await client.send_line("AUTH:FAILED:Account temporarily locked due to failures")
                        return False
                    if not hasattr(self.auth_manager, "start_password_hmac_challenge"):
                        return False
                    nonce_b64 = self.auth_manager.start_password_hmac_challenge(username)
                    if not nonce_b64:
                        await client.send_line("AUTH:FAILED:Authentication failed")
                        return False
                    await client.send_line(f"AUTH:CHALLENGE:{nonce_b64}")
                    # Wait for response
                    resp = await self.collect_line_from_chars(client)
                    # AUTH:RESPONSE:<hmac_b64>
                    if not resp or not resp.startswith("AUTH:RESPONSE:"):
                        return False
                    rparts = resp.split(":")
                    if len(rparts) < 3:
                        return False
                    hmac_b64 = rparts[2]
                    ok = False
                    try:
                        ok = self.auth_manager.verify_password_hmac(username, hmac_b64, src_ip)
                    except Exception:
                        ok = False
                    if ok:
                        client.username = username
                        client.is_authenticated = True
                        return True
                    await client.send_line("AUTH:FAILED:Authentication failed")
                    return False
            elif auth_line.startswith("AUTH:USER:"):
                # Plaintext password auth deprecated
                parts = auth_line.split(":")
                if len(parts) >= 3:
                    username = parts[2]
                    src_ip = client.address[0] if hasattr(client, "address") and client.address else None
                    if hasattr(self.auth_manager, "is_user_locked") and self.auth_manager.is_user_locked(username, src_ip):
                        await client.send_line("AUTH:FAILED:Account temporarily locked due to failures")
                        return False
                await client.send_line("AUTH:FAILED:Plaintext password auth disabled; upgrade client (use HMAC or pubkey)")
                self.logger.warning("Rejected deprecated plaintext AUTH:USER attempt")
                return False

            return False

        except Exception as e:
            self.logger.error(f"Authentication error: {e}", exc_info=True)
            return False

    async def handle_command_phase(self, client: "ClientSession"):
        """Process command-phase requests until port attach or disconnect.

        Continuously collects line-mode commands and dispatches them to
        ``process_client_command`` until the client either attaches to a
        port (transition to character mode), disconnects, or an error occurs.

        Args:
            client: Active client session wrapper.
        """
        while client.connected and not client.connected_port:
            try:
                command_line = await self.collect_line_from_chars(client)
                if not command_line:
                    break

                await self.process_client_command(client, command_line.strip())

            except asyncio.TimeoutError:
                self.logger.warning(f"Client {client.client_id} timeout in command phase")
                break
            except Exception as e:
                self.logger.error(f"Command phase error for client {client.client_id}: {e}", exc_info=True)
                break

    async def handle_character_mode(self, client: "ClientSession"):
        """Relay raw character data once a port is attached.

        Reads buffered chunks and forwards them through
        ``forward_bytes_to_port`` until disconnection or detach.

        Args:
            client: Active client session in character mode.
        """
        self.logger.info(f"Client {client.client_id} entering character mode for port {client.connected_port}")

        # Use a moderate buffer size to balance latency and throughput
        bufsize = 4096
        while client.connected and client.connected_port:
            try:
                data = await client.receive_bytes(bufsize)
                if not data:
                    break

                # Forward the chunk to the port
                await self.forward_bytes_to_port(client, data)

            except asyncio.TimeoutError:
                self.logger.warning(f"Client {client.client_id} timeout in character mode")
                break
            except Exception as e:
                self.logger.error(f"Character mode error for client {client.client_id}: {e}", exc_info=True)
                break

    async def collect_line_from_chars(self, client: "ClientSession") -> Optional[str]:
        """Collect characters until newline to emulate line-based input.

        Args:
            client: Client session to read bytes from.

        Returns:
            Optional[str]: Decoded line without newline, or ``None`` on
            disconnect or buffer overflow.
        """
        line_buffer = bytearray()

        while client.connected:
            char_data = await client.receive_char()
            if not char_data:
                return None

            # Check for line endings
            if char_data == b"\n":
                # Complete line received
                line = line_buffer.decode("utf-8", errors="ignore").rstrip("\r\n")
                return line
            elif char_data == b"\r":
                # Handle CR - might be followed by LF
                continue
            else:
                # Add character to buffer
                line_buffer.extend(char_data)

                # Prevent buffer overflow
                if len(line_buffer) > 1024:
                    self.logger.warning(f"Line buffer overflow for client {client.client_id}")
                    return None

        return None

    async def forward_character_to_port(self, client: "ClientSession", char_data: bytes):
        """Forward one raw character to attached port (if any).

        Args:
            client: Originating client session.
            char_data: Single-byte payload to forward.
        """
        if not client.connected_port:
            return

        # Forward to port via console manager - let the port handle echo
        if self.console_manager and hasattr(self.console_manager, "port_manager"):
            try:
                await self.console_manager.port_manager.write_to_port(client.connected_port, char_data, client.client_id)
                self.logger.debug(f"Forwarded character {char_data.hex()} to port {client.connected_port}")
            except Exception as e:
                self.logger.error(f"Error writing character to port {client.connected_port}: {e}", exc_info=True)

    async def forward_bytes_to_port(self, client: "ClientSession", data: bytes):
        """Forward a raw byte chunk to the attached port (if any)."""
        if not client.connected_port:
            return

        if self.console_manager and hasattr(self.console_manager, "port_manager"):
            try:
                await self.console_manager.port_manager.write_to_port(client.connected_port, data, client.client_id)
                self.logger.debug(
                    f"Forwarded chunk {len(data)}B to port {client.connected_port} for client {client.client_id}"
                )
            except Exception as e:
                self.logger.error(f"Error writing chunk to port {client.connected_port}: {e}", exc_info=True)

    async def process_client_command(self, client: "ClientSession", command: str):
        """Parse and execute a client command.

        Supported commands: ``CONNECT:<port>``, ``LIST``, ``DISCONNECT``,
        ``QUIT``. If the client is already attached to a port, unrecognized
        commands are forwarded verbatim (with newline) as port data.

        Args:
            client: Client session issuing the command.
            command: Raw command string (without trailing newline).
        """
        self.logger.debug(f"Processing command from client {client.client_id}: {command}")

        if command.startswith("CONNECT:"):
            # Formats supported:
            #   CONNECT:<port_name>
            #   CONNECT:<server_id>::<port_name>
            parts = command.split(":", 1)
            if len(parts) >= 2:
                raw_target = parts[1].strip()
                server_id = None
                port_name = raw_target
                # Parse composite identifier if present
                if "::" in raw_target:
                    try:
                        sid, base = raw_target.split("::", 1)
                        if sid and base:
                            server_id, port_name = sid, base
                    except ValueError:
                        server_id = None
                # If a server_id was provided, resolve to a unique port entry first
                if server_id:
                    resolved = await self._resolve_port_by_origin(port_name, server_id)
                    if not resolved:
                        await client.send_line("ERROR:CONNECT:Port not found for given server_id")
                        return
                    port_name = resolved
                await self.handle_port_connection_request_text(client, port_name)
            else:
                await client.send_line("ERROR:Invalid CONNECT command format")

        elif command.startswith("LIST"):
            # List available ports
            await self.handle_list_ports_request(client)

        elif command.startswith("DISCONNECT"):
            # Disconnect from current port
            await self.handle_port_disconnection_request_text(client)

        elif command == "QUIT":
            # Client wants to disconnect
            client.connected = False

        else:
            # If connected to a port, forward the data
            if client.connected_port:
                # Forward raw data to the port
                data = command.encode("utf-8") + b"\n"  # Add newline for command
                await self.forward_data_to_port(client, data)
            else:
                await client.send_line(f"ERROR:Unknown command: {command}")

    async def handle_port_connection_request_text(self, client: "ClientSession", port_name: str):
        """Attempt port attachment for the client via text protocol.

        Informs the client of success with ``CONNECTED:<port>:<ACCESS_MODE>``
        or sends an error line on failure.

        Args:
            client: Client session requesting the connection.
            port_name: Target port name to attach to.
        """
        self.logger.info(f"Client {client.client_id} requesting connection to port {port_name}")

        try:
            # Use console manager to handle the connection
            if self.console_manager:
                success, mode = await self.console_manager.connect_client_to_port(client.client_id, port_name, client.username)

                if success:
                    client.connected_port = port_name

                    # Track client for this port
                    if port_name not in self.port_clients:
                        self.port_clients[port_name] = []
                    self.port_clients[port_name].append(client.client_id)

                    # Register explicit routing for this TCP client so console manager can
                    # deliver broadcasts directly via this adapter in multi-manager setups
                    try:
                        if hasattr(self.console_manager, "register_client_channel"):
                            self.console_manager.register_client_channel(client.client_id, self)
                    except Exception:
                        pass

                    # Get the actual access mode from console manager
                    # The console manager just added the client, so we should be able to get the mode
                    # Determine access mode from returned mode, fallback to querying if needed
                    access_mode = "READ_ONLY"
                    try:
                        effective_mode = mode
                        if not effective_mode and hasattr(self.console_manager, "get_client_mode"):
                            effective_mode = self.console_manager.get_client_mode(client.client_id, port_name)
                        if effective_mode == "read-write":
                            access_mode = "READ_WRITE"
                        elif effective_mode == "read-only":
                            access_mode = "READ_ONLY"
                    except Exception as e:
                        self.logger.error(f"Error determining client mode: {e}", exc_info=True)

                    await client.send_line(f"CONNECTED:{port_name}:{access_mode}")
                    # If the target port is a federated proxy and currently down,
                    # immediately inform the client so they understand why no data flows yet.
                    try:
                        pm = getattr(self.console_manager, "port_manager", None)
                        if pm:
                            port_obj = safe_get_port(pm, port_name)
                            if port_obj is not None:
                                is_fed = hasattr(port_obj, "remote_port_name")
                                is_up = bool(getattr(port_obj, "is_connected", True))
                                if is_fed and not is_up:
                                    await client.send_raw_data(b"\r\n[Port disconnected on server]\r\n")
                    except Exception:
                        pass
                    self.logger.info(f"Client {client.client_id} connected to port {port_name} in {access_mode} mode")
                else:
                    await client.send_line(f"ERROR:CONNECT:Cannot connect to port {port_name}")
            else:
                await client.send_line("ERROR:CONNECT:Console manager not available")

        except Exception as e:
            self.logger.error(f"Error connecting client to port {port_name}: {e}", exc_info=True)
            await client.send_line(f"ERROR:CONNECT:Connection error")

    async def _resolve_port_by_origin(self, port_name: str, server_id: str) -> Optional[str]:
        """Resolve port uniquely by (origin_server_id, name) using PortManager listing.

        Args:
            port_name: Target base name.
            server_id: Origin server identifier or 'local' for local ports.

        Returns:
            The canonical port name to use with PortManager, or None if not found/ambiguous.
        """
        try:
            if not self.console_manager or not hasattr(self.console_manager, "port_manager"):
                return None
            pm = self.console_manager.port_manager
            # Prefer federation-aware listing
            ports = []
            try:
                if hasattr(pm, "get_port_list_with_federation"):
                    ports = await asyncio.wait_for(pm.get_port_list_with_federation(), timeout=1.0)
            except Exception:
                ports = []
            if not ports:
                # Fallback to static snapshot
                raw_ports = getattr(pm, "ports", {})
                for n, p in list(raw_ports.items()):
                    try:
                        info = p.get_status() if hasattr(p, "get_status") else {"name": n}
                        info["name"] = info.get("name", n)
                        ports.append(info)
                    except Exception:
                        continue
            # Filter by name and origin
            cands = [p for p in ports if p.get("name") == port_name and (
                (server_id.lower() in ("local", "LOCAL") and not p.get("origin_server_id")) or
                (p.get("origin_server_id") == server_id)
            )]
            if len(cands) == 1:
                return cands[0].get("name")
            return None
        except Exception:
            return None

    async def handle_list_ports_request(self, client: "ClientSession"):
        """Send JSON list of available ports to client.

        Uses federation-aware listing when available and falls back to a
        basic snapshot if the call times out.

        Args:
            client: Requesting client session.
        """
        try:
            start_ts = time.time()
            self.logger.info(f"LIST: start for client {client.client_id}")
            ports = []
            timed_out = False
            if self.console_manager and hasattr(self.console_manager, "port_manager"):
                pm = self.console_manager.port_manager

                # Choose coroutine to call
                async def gather():
                    if hasattr(pm, "get_port_list_with_federation"):
                        return await pm.get_port_list_with_federation()
                    if hasattr(pm, "get_port_list"):
                        return await pm.get_port_list()
                    return []

                try:
                    ports = await asyncio.wait_for(gather(), timeout=1.0)
                except asyncio.TimeoutError:
                    timed_out = True
                    self.logger.warning("LIST: federation list timeout after 1.0s; falling back to basic snapshot")
                    try:
                        # Basic synchronous snapshot of current ports dict
                        raw_ports = getattr(pm, "ports", {})
                        for name, port in list(raw_ports.items()):
                            try:
                                info = port.get_status() if hasattr(port, "get_status") else {"name": name}
                                if "name" not in info:
                                    info["name"] = name
                                ports.append(info)
                            except Exception:
                                self.logger.error("LIST: port get_status error", exc_info=True)
                    except Exception as inner_e:
                        self.logger.error(f"LIST fallback snapshot error: {inner_e}", exc_info=True)
                except Exception as inner_e:
                    self.logger.error(f"PortManager listing error: {inner_e}", exc_info=True)
            elapsed_ms = int((time.time() - start_ts) * 1000)
            payload = {
                "type": "PORT_LIST",
                "count": len(ports),
                "ports": ports,
                "elapsed_ms": elapsed_ms,
                "timed_out": timed_out,
            }
            self.logger.info(
                f"LIST: done client={client.client_id} count={len(ports)} elapsed_ms={elapsed_ms} timeout={timed_out}"
            )
            await client.send_line("LIST:" + json.dumps(payload, separators=(",", ":")))
        except Exception as e:
            self.logger.error(f"Error listing ports: {e}", exc_info=True)
            await client.send_line("ERROR:LIST:Failed")

    async def handle_port_disconnection_request_text(self, client: "ClientSession"):
        """Detach client from currently attached port (if any)."""
        if client.connected_port:
            port_name = client.connected_port
            await self.disconnect_client_from_port(client)
            await client.send_line(f"DISCONNECTED:{port_name}")
        else:
            await client.send_line("ERROR:DISCONNECT:No port connected")

    async def forward_data_to_port(self, client: "ClientSession", data: bytes):
        """Forward multi-byte payload to attached port (line-mode payload)."""
        if not client.connected_port:
            return

        self.logger.debug(f"Forwarding {len(data)} bytes to port {client.connected_port} from client {client.client_id}")
        self.logger.debug(f"Data content: {data[:100]}...")  # Log first 100 bytes

        # For loopback ports, provide immediate character-by-character echo
        if "loop" in client.connected_port.lower():
            self.logger.debug(f"Providing immediate character echo for loopback port")
            # Echo each character individually for loopback ports
            for i in range(len(data)):
                char_byte = data[i : i + 1]
                if char_byte != b"\n":  # Don't echo newlines
                    try:
                        await client.send_raw_data(char_byte)
                        self.logger.debug(f"Echoed character: {char_byte} (hex: {char_byte.hex()})")
                    except Exception as e:
                        self.logger.error(f"Failed to send immediate echo: {e}", exc_info=True)

        # Also forward data to port via console manager
        if self.console_manager and hasattr(self.console_manager, "port_manager"):
            try:
                success = await self.console_manager.port_manager.write_to_port(client.connected_port, data, client.client_id)
                self.logger.debug(f"write_to_port returned: {success}")
                if not success:
                    await client.send_line("ERROR:Failed to write to port")
            except Exception as e:
                self.logger.error(f"Error writing to port {client.connected_port}: {e}", exc_info=True)
                await client.send_line("ERROR:Write error")

    async def disconnect_client_from_port(self, client: "ClientSession"):
        """Detach client session from its connected port (if attached).

        Args:
            client: Client session to detach from its port.
        """
        if not client.connected_port:
            return

        port_name = client.connected_port

        # Remove from port clients tracking
        if port_name in self.port_clients:
            if client.client_id in self.port_clients[port_name]:
                self.port_clients[port_name].remove(client.client_id)
            if not self.port_clients[port_name]:
                del self.port_clients[port_name]

        # Disconnect via console manager
        if self.console_manager:
            await self.console_manager.disconnect_client_from_port(client.client_id, port_name)
            # Remove routing association
            try:
                if hasattr(self.console_manager, "unregister_client_channel"):
                    self.console_manager.unregister_client_channel(client.client_id)
            except Exception:
                pass

        client.connected_port = None
        self.logger.debug(f"Client {client.client_id} disconnected from port {port_name}")

    async def disconnect_client(self, client_id: str):
        """Fully tear down a client session (port detach + socket close).

        Args:
            client_id: Identifier of the client to disconnect.
        """
        if client_id not in self.clients:
            return

        client = self.clients[client_id]

        # Disconnect from port if connected
        await self.disconnect_client_from_port(client)

        # Close connection
        await client.close()

        # Remove from tracking
        del self.clients[client_id]

        self.logger.info(f"Client {client_id} disconnected")

    async def _broadcast_shutdown_message(self):
        """Best-effort broadcast of impending shutdown to all clients."""
        line = "SERVER:SHUTDOWN:Server going down"
        tasks = []
        for client in list(self.clients.values()):
            if client.connected:
                tasks.append(client.send_line(line))
        if tasks:
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except Exception:
                self.logger.error("Shutdown broadcast gather error", exc_info=True)

    async def send_data_to_client(self, client_id: str, data: bytes) -> bool:
        """Send raw data to a specific client.

        Args:
            client_id: Target client identifier.
            data: Bytes payload to send.

        Returns:
            bool: True on success, False if client unknown or write failed.
        """
        if client_id not in self.clients:
            self.logger.warning(f"Tried to send data to unknown client {client_id}")
            return False

        client = self.clients[client_id]
        self.logger.debug(f"Sending {len(data)} bytes to client {client_id}")
        self.logger.debug(f"Echo data content: {data[:100]}...")  # Log first 100 bytes
        try:
            # Send raw data directly (not as a line)
            await client.send_raw_data(data)
            return True
        except Exception as e:
            self.logger.error(f"Failed to send data to client {client_id}: {e}", exc_info=True)
            return False

    # Port management methods (required by BaseGenericAdapter)
    async def create_port(self, port_name: str, config: Dict[str, Any]) -> Optional[Any]:
        """No-op for server (does not create ports)."""
        return None

    async def destroy_port(self, port_name: str) -> None:
        """Disconnect any clients referencing a port being removed."""
        if port_name in self.port_clients:
            # Disconnect all clients from this port
            for client_id in self.port_clients[port_name][:]:
                if client_id in self.clients:
                    await self.disconnect_client_from_port(self.clients[client_id])

    def get_status_info(self) -> Dict[str, Any]:
        """Return structured adapter status snapshot."""
        return {
            "type": self.get_adapter_type(),
            "status": "running" if self.is_running else "stopped",
            "endpoint": f"{self.host}:{self.port}",
            "clients": f"{len(self.clients)} connected",
            "details": {
                "adapter_name": self.name,
                "host": self.host,
                "port": self.port,
                "enabled": getattr(self, "enabled", True),
                "max_connections": self.max_connections,
                "connected_clients": len(self.clients),
                "active_ports": len(self.port_clients),
                "client_list": [
                    {
                        "id": client.client_id,
                        "address": client.address,
                        "username": client.username,
                        "connected_port": client.connected_port,
                        "connected_time": client.connected_time,
                    }
                    for client in self.clients.values()
                ],
            },
        }


class ClientSession:
    """Represents a connected client session.

    Maintains authentication state, current port attachment, and helpers for
    line-oriented and character-oriented I/O.
    """

    def __init__(
        self,
        client_id: str,
        address: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        logger: logging.Logger,
    ):
        self.client_id = client_id
        self.address = address
        self.reader = reader
        self.writer = writer
        self.logger = logger

        # Session state
        self.connected = True
        self.connected_time = time.time()
        self.username: Optional[str] = None
        self.is_authenticated = False
        self.connected_port: Optional[str] = None

    async def receive_char(self) -> Optional[bytes]:
        """Receive exactly one byte (unless disconnected).

        Returns:
            Optional[bytes]: A single-byte buffer, or ``None`` if disconnected.
        """
        try:
            data = await self.reader.read(1)
            if not data:
                self.connected = False
                return None
            return data
        except Exception as e:
            self.logger.error(f"Error receiving char from client {self.client_id}: {e}", exc_info=True)
            self.connected = False
            return None

    async def send_line(self, line: str):
        """Send UTF-8 encoded line with newline terminator.

        Args:
            line: String content to send (without trailing newline).
        """
        try:
            data = line.encode("utf-8") + b"\n"
            self.writer.write(data)
            await self.writer.drain()
        except Exception as e:
            self.logger.error(f"Failed to send line to client {self.client_id}: {e}", exc_info=True)
            self.connected = False

    async def send_raw_data(self, data: bytes):
        """Send raw bytes (no framing).

        Args:
            data: Raw bytes to write to the client transport.
        """
        try:
            self.writer.write(data)
            await self.writer.drain()
        except Exception as e:
            self.logger.error(f"Failed to send raw data to client {self.client_id}: {e}", exc_info=True)
            self.connected = False

    async def receive_bytes(self, n: int = 4096) -> Optional[bytes]:
        """Receive up to ``n`` bytes (unless disconnected).

        Returns:
            Optional[bytes]: Buffer with up to ``n`` bytes, or ``None`` on disconnect.
        """
        try:
            data = await self.reader.read(n)
            if not data:
                self.connected = False
                return None
            return data
        except Exception as e:
            self.logger.error(f"Error receiving bytes from client {self.client_id}: {e}", exc_info=True)
            self.connected = False
            return None

    async def close(self):
        """Close underlying connection (graceful best-effort).

        Attempts a half-close with EOF when supported, then closes the
        writer and waits briefly for completion.
        """
        self.connected = False
        if self.writer:
            try:
                # Attempt a graceful half-close first
                if not self.writer.is_closing():
                    try:
                        self.writer.write_eof()
                    except (AttributeError, RuntimeError):
                        pass
                self.writer.close()
                try:
                    await asyncio.wait_for(self.writer.wait_closed(), timeout=0.5)
                except Exception:
                    transport = getattr(self.writer, "transport", None)
                    if transport:
                        try:
                            transport.abort()
                        except Exception:
                            self.logger.error("Transport abort error", exc_info=True)
            except Exception:
                self.logger.error("Error during client close", exc_info=True)  # Ignore errors during close
