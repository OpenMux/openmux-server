"""
Test-only OpenMux Protocol Handler shim

This provides ClientConnection and OpenMuxProtocolHandler for tests that
previously imported them from openmux.server.openmux_protocol_handler.

It mirrors the public behavior used by tests without registering a global
client manager.
"""

import asyncio
import logging
import secrets
from typing import Optional


class ClientConnection:
    """Represents a client connection (test shim)"""

    def __init__(self, reader, writer, client_id: str, username: Optional[str] = None):
        self.reader = reader
        self.writer = writer
        self.client_id: str = client_id
        self.username: Optional[str] = username
        self.connected_port: Optional[str] = None
        self.mode: str = "read-only"
        self.addr = writer.get_extra_info("peername") if hasattr(writer, "get_extra_info") else ("0.0.0.0", 0)
        # Provide deterministic loop time in tests via patched get_running_loop
        try:
            self.connected_at = asyncio.get_running_loop().time()
        except RuntimeError:
            self.connected_at = 0.0

    async def send(self, data: bytes) -> bool:
        try:
            processed = self._process_outgoing_data(data)
            self.writer.write(processed)
            if hasattr(self.writer, "drain"):
                await self.writer.drain()
            return True
        except Exception as e:
            logging.error(f"Error sending data to client {self.client_id}: {e}")
            return False

    def _process_outgoing_data(self, data: bytes) -> bytes:
        return data

    async def close(self):
        try:
            if hasattr(self.writer, "close"):
                self.writer.close()
            if hasattr(self.writer, "wait_closed"):
                await self.writer.wait_closed()
        except Exception as e:
            logging.error(f"Error closing client {self.client_id}: {e}")


class OpenMuxProtocolHandler:
    """Test-only protocol handler matching tests' expectations"""

    def __init__(self, console_manager, auth_manager):
        self.console_manager = console_manager
        self.auth_manager = auth_manager
        self.clients = []
        self.logger = logging.getLogger("openmux.client")
        # In tests we don't rely on console_manager to register a global reference
        if hasattr(self.console_manager, "register_client_manager"):
            self.console_manager.register_client_manager(self)
        self.console_manager.client_manager = self

    async def handle_new_connection(self, reader, writer):
        client_id = secrets.token_hex(8)
        addr = writer.get_extra_info("peername") if hasattr(writer, "get_extra_info") else ("0.0.0.0", 0)
        self.logger.info(f"New connection from {addr}")
        client = ClientConnection(reader, writer, client_id)
        self.clients.append(client)
        await self.handle_client(client)

    async def handle_client(self, client: ClientConnection):
        try:
            await client.send(b"\x1b[?25h")
            await client.send(b"\x1b[?7h")
            if not await self._authenticate_client(client):
                await client.send(b"Authentication failed\r\n")
                await client.close()
                if client in self.clients:
                    self.clients.remove(client)
                return
            await client.send(b"Welcome to OpenMux Server\r\n")
            await self._handle_client_commands(client)
        except Exception as e:
            self.logger.error(f"Error handling client {client.client_id}: {e}")
        finally:
            await self._disconnect_client(client.client_id)

    async def _authenticate_client(self, client: ClientConnection) -> bool:
        await client.send(b"Authentication required\r\n")
        auth_line = await client.reader.readline()
        if not auth_line:
            return False
        auth_data = auth_line.decode().strip()
        if auth_data.startswith("AUTH:KEY:"):
            _, _, api_key = auth_data.partition("AUTH:KEY:")
            if self.auth_manager.verify_api_key(api_key):
                client.username = f"api-{client.client_id[:6]}"
                return True
        elif auth_data.startswith("AUTH:USER:"):
            try:
                _, _, credentials = auth_data.partition("AUTH:USER:")
                username, password = credentials.split(":", 1)
                if self.auth_manager.authenticate(username, password):
                    client.username = username
                    return True
            except ValueError:
                pass
        return False

    async def _handle_client_commands(self, client: ClientConnection):
        while True:
            command_line = await client.reader.readline()
            if not command_line:
                break
            command = command_line.decode().strip()
            if command == "LIST":
                await self._handle_list_command(client)
            elif command.startswith("CONNECT:"):
                _, port_name = command.split(":", 1)
                await self._handle_connect_command(client, port_name)
            elif command in ("RELOAD",):
                await client.send(b"Reloading configuration...\r\n")
            elif command in ("QUIT", "EXIT"):
                await client.send(b"Goodbye\r\n")
                break
            elif client.connected_port:
                await self.console_manager.write_to_port(client.connected_port, command_line, client.client_id)
            else:
                await client.send(b"Unknown command\r\n")

    async def _handle_list_command(self, client: ClientConnection):
        port_list = await self.console_manager.get_port_list()
        response = "Available ports:\r\n"
        for i, port in enumerate(port_list):
            status = "Connected" if port.get("is_connected") else "Disconnected"
            clients_info = f"{port.get('clients')} clients, {port.get('read_write_clients')} read-write"
            response += (
                f"{i+1}. {port.get('name')} - {port.get('description')} ({port.get('device')}) - "
                f"{status} - {clients_info}\r\n"
            )
        await client.send(response.encode())

    async def _handle_connect_command(self, client: ClientConnection, port_name: str):
        if not await self.console_manager.port_exists(port_name):
            await client.send(f"Port {port_name} not found\r\n".encode())
            return
        success, mode = await self.console_manager.connect_client_to_port(client.client_id, port_name, client.username)
        if success:
            client.connected_port = port_name
            client.mode = mode
            status = "read-write" if mode == "read-write" else "read-only"
            await client.send(f"Connected to {port_name} in {status} mode\r\n".encode())
            await client.send(b"Use Ctrl+] to access control menu\r\n")
            await self._handle_console_mode(client)
        else:
            await client.send(f"Failed to connect to {port_name}\r\n".encode())

    async def _setup_console_mode(self, client: ClientConnection):
        await client.send(b"\x1b[?25h")
        await client.send(b"\x1b[?7h")

    async def _handle_console_mode(self, client: ClientConnection):
        await self._setup_console_mode(client)
        escape_sequence = b"\x1d"
        try:
            while client.connected_port:
                data = await client.reader.read(1024)
                if not data:
                    break
                if escape_sequence in data:
                    await self._handle_escape_sequence_data(client, data, escape_sequence)
                else:
                    await self._handle_regular_data(client, data)
        except Exception as e:
            self.logger.error(f"Error in console mode for client {client.client_id}: {e}")
        finally:
            await self._cleanup_console_mode(client)

    async def _handle_escape_sequence_data(self, client: ClientConnection, data: bytes, escape_sequence: bytes):
        before, after = data.split(escape_sequence, 1)
        if before:
            processed = self._process_input_data(before)
            await self.console_manager.write_to_port(client.connected_port, processed, client.client_id)
        await self._handle_control_menu(client)
        if client.connected_port and after:
            processed = self._process_input_data(after)
            await self.console_manager.write_to_port(client.connected_port, processed, client.client_id)

    async def _handle_regular_data(self, client: ClientConnection, data: bytes):
        processed = self._process_input_data(data)
        await self.console_manager.write_to_port(client.connected_port, processed, client.client_id)

    async def _cleanup_console_mode(self, client: ClientConnection):
        if client.connected_port:
            await self.console_manager.disconnect_client_from_port(client.client_id, client.connected_port)
            client.connected_port = None

    def _process_input_data(self, data: bytes) -> bytes:
        return data

    async def _handle_control_menu(self, client: ClientConnection):
        menu = (
            "\r\n--- OpenMux Control Menu ---\r\n"
            "r: Request read-write access\n"
            "d: Disconnect and reconnect port\n"
            "q: Disconnect from port\n"
            "?: Show this menu\r\n"
            "Enter your choice: "
        )
        await client.send(menu.encode())
        while True:
            choice_data = await client.reader.readline()
            if not choice_data:
                break
            choice = choice_data.decode().strip().lower()
            if choice == "r":
                if client.mode == "read-write":
                    await client.send(b"You already have read-write access\r\n")
                else:
                    success = await self.console_manager.promote_client_to_read_write(client.client_id, client.connected_port)
                    if success:
                        client.mode = "read-write"
                        await client.send(b"Promoted to read-write access\r\n")
                    else:
                        await client.send(b"Failed to get read-write access\r\n")
            elif choice == "d":
                port_name = client.connected_port
                await client.send(f"Disconnecting and reconnecting {port_name}...\r\n".encode())
                # Unified-only: simulate reconnect by detaching and re-attaching the client
                await self.console_manager.disconnect_client_from_port(client.client_id, client.connected_port)
                await asyncio.sleep(0.2)
                success, mode = await self.console_manager.connect_client_to_port(client.client_id, port_name, client.username)
                if success:
                    client.connected_port = port_name
                    client.mode = mode
                    await client.send(f"Reconnected to {port_name} in {mode} mode\r\n".encode())
                else:
                    await client.send(f"Failed to reconnect to {port_name}\r\n".encode())
                    client.connected_port = None
                    return
            elif choice == "q":
                port_name = client.connected_port
                await self.console_manager.disconnect_client_from_port(client.client_id, client.connected_port)
                client.connected_port = None
                await client.send(f"Disconnected from {port_name}\r\n".encode())
                return
            elif choice == "?":
                await client.send(menu.encode())
            else:
                await client.send(b"Returning to console mode\r\n")
                return

    async def _disconnect_client(self, client_id: str):
        target = None
        for c in self.clients:
            if c.client_id == client_id:
                target = c
                break
        if not target:
            return
        if target.connected_port:
            await self.console_manager.disconnect_client_from_port(client_id, target.connected_port)
        await target.close()
        if target in self.clients:
            self.clients.remove(target)

    async def close_all_connections(self):
        for c in list(self.clients):
            await c.close()
        self.clients.clear()

    async def send_data_to_client(self, client_id: str, data: bytes) -> bool:
        for c in self.clients:
            if c.client_id == client_id:
                return await c.send(data)
        return False
