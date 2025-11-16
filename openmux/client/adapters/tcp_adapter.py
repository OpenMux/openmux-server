"""TCP transport implementation of the OpenMux client adapter interface.

This module provides a concrete client adapter that speaks the standard
line‑oriented OpenMux console/port protocol over a raw (optionally TLS wrapped)
TCP stream. It encapsulates connection lifecycle management, authentication
flows (password or API key), port discovery/attachment, bidirectional data
transfer, and reconnection using cached credentials.

Responsibilities:
    * Establish and optionally secure a TCP connection.
    * Perform authentication handshakes (password or API key).
    * Enumerate available ports and attach to one for data exchange.
    * Provide simple read/write primitives for port traffic.
    * Gracefully close or transparently reconnect using cached auth state.

The class avoids raising exceptions for routine network/protocol failures;
instead it returns False/None/empty values and logs at debug/error levels to
keep higher level orchestration straightforward.
"""

import asyncio
import json
import ssl
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

from .base_adapter import BaseClientAdapter

if TYPE_CHECKING:
    from asyncio import StreamReader, StreamWriter


class TcpClientAdapter(BaseClientAdapter):
    """Standard TCP adapter implementation.

    Implements the abstract interface defined by ``BaseClientAdapter`` using
    asyncio streams. Protocol messages are newline terminated UTF‑8 sequences
    following the server console/port semantics.
    """

    def __init__(self, host: str, port: int, config: Optional[Dict[str, Any]] = None):
        """Initialize adapter state (no network I/O performed).

        Args:
            host: Remote server hostname or IPv4/IPv6 address.
            port: Remote TCP port number exposed by the OpenMux server.
            config: Optional configuration dictionary. Supported keys:
                use_tls (bool): If True wrap the connection in TLS (default False).
                ssl_context (ssl.SSLContext): Custom context; created automatically
                    if omitted and ``use_tls`` is True.
        """
        super().__init__(host, port, config)

        # TCP-specific configuration
        self.use_tls = bool(self.config.get("use_tls", False))
        self.ssl_context = self.config.get("ssl_context")

        # TCP connection objects
        self.reader: Optional["StreamReader"] = None
        self.writer: Optional["StreamWriter"] = None

        # Reconnect state
        self.current_port = None
        self._last_auth = None

    async def connect(self) -> bool:
        """Open the underlying TCP (optionally TLS) connection.

        Performs the initial read to obtain the authentication challenge banner.
        The connection is considered established only if the expected banner is
        detected.

        Returns:
            bool: True if the socket connected and an authentication prompt
            containing the substring ``"Authentication required"`` was received;
            False if connection or banner validation failed.
        """
        if self.is_connected:
            return True

        try:
            self.logger.debug(f"Connecting to {self.host}:{self.port}")

            # Use SSL if encryption is enabled
            if self.use_tls:
                # Create default SSL context if none provided
                ssl_context = self.ssl_context or ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
                self.reader, self.writer = await asyncio.open_connection(self.host, self.port, ssl=ssl_context)
                self.logger.debug("Using encrypted connection (TLS)")
            else:
                self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
                self.logger.debug("Using unencrypted connection")

            # Wait for authentication prompt
            data = await self.reader.readline()
            if b"Authentication required" not in data:
                self.logger.error(f"Unexpected banner from {self.host}:{self.port}: {data.decode(errors='ignore').strip()}")
                await self.close()
                return False

            self.is_connected = True
            return True

        except Exception as e:
            self.logger.debug(f"Socket connect failed to {self.host}:{self.port}: {e}", exc_info=True)
            return False

    async def authenticate_with_password(self, username: str, password: str) -> bool:
        """Authenticate using a username/password sequence.

        On success, credentials are cached for later reconnect attempts.

        Args:
            username: The account name to authenticate as.
            password: The associated plaintext password (sent over TLS if enabled).

        Returns:
            bool: True if authentication succeeded, False otherwise (including
            network/protocol errors or explicit server rejection).
        """
        if not self.is_connected:
            return False

        try:
            ok = await self._authenticate_standard_password(username, password)

            # Cache last auth on success for reconnects
            if ok:
                self._last_auth = {
                    "method": "password",
                    "username": username,
                    "password": password,
                }
            return ok

        except Exception as e:
            self.logger.debug(f"Authentication error: {e}", exc_info=True)
            return False

    async def authenticate_with_key(self, api_key: str) -> bool:
        """Authenticate using an API key token.

        Successful authentication will cache the key for reconnect cycles.

        Args:
            api_key: The API key presented to the server.

        Returns:
            bool: True if authentication was accepted; False otherwise.
        """
        if not self.is_connected:
            return False

        try:
            ok = await self._authenticate_standard_key(api_key)

            if ok:
                self._last_auth = {"method": "api_key", "api_key": api_key}
            return ok

        except Exception as e:
            self.logger.debug(f"Authentication error: {e}", exc_info=True)
            return False

    async def authenticate_with_pubkey(self, username: str, private_key_path: str, key_id: Optional[str] = None) -> bool:
        """Authenticate using Ed25519 public key challenge/response.

        Args:
            username: Identity to claim.
            private_key_path: Path to Ed25519 private key (PEM or OpenSSH format).
            key_id: Optional key identifier if multiple keys registered.

        Returns:
            bool: True if auth succeeded, False otherwise.
        """
        if not self.writer or not self.reader:
            self.logger.error("Connection not established")
            return False
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            # Load private key
            with open(private_key_path, "rb") as f:
                key_data = f.read()
            priv = None
            passphrase = None
            # Support environment variable passphrase if encrypted
            import os

            pw_env = os.environ.get("OPENMUX_PUBKEY_PASSPHRASE")
            if pw_env:
                passphrase = pw_env.encode()
            try_formats = [
                lambda: serialization.load_pem_private_key(key_data, password=passphrase),
                lambda: serialization.load_ssh_private_key(key_data, password=passphrase),
            ]
            for loader in try_formats:
                try:
                    obj = loader()
                    if isinstance(obj, Ed25519PrivateKey):
                        priv = obj
                        break
                except Exception:
                    continue
            if priv is None:
                self.logger.error("Failed to load Ed25519 private key")
                return False
            init_cmd = f"AUTH:PK:INIT:{username}:{key_id}\n" if key_id else f"AUTH:PK:INIT:{username}\n"
            self.writer.write(init_cmd.encode())
            await self.writer.drain()
            line = await self.reader.readline()
            if not line.startswith(b"AUTH:PK:CHALLENGE:"):
                self.logger.error(f"Unexpected challenge line: {line.decode(errors='ignore').strip()}")
                return False
            parts = line.decode(errors="ignore").strip().split(":")
            # AUTH:PK:CHALLENGE:<key_id>:<nonce>
            if len(parts) < 5:
                self.logger.error("Malformed challenge response")
                return False
            chal_key_id = parts[3]
            nonce_b64 = parts[4]
            import base64

            try:
                nonce_raw = base64.b64decode(nonce_b64)
            except Exception:
                self.logger.error("Invalid nonce encoding")
                return False
            signature = priv.sign(nonce_raw)
            sig_b64 = base64.b64encode(signature).decode()
            resp = f"AUTH:PK:RESPONSE:{chal_key_id}:{sig_b64}\n"
            self.writer.write(resp.encode())
            await self.writer.drain()
            # Read potentially multiple lines (some server variants may send a banner after success)
            auth_ok = False
            last_line: bytes = b""
            for _ in range(3):  # read up to 3 lines defensively
                final_line = await self.reader.readline()
                if not final_line:
                    break
                last_line = final_line
                if final_line.startswith(b"AUTH:SUCCESS"):
                    auth_ok = True
                    break
                # Skip empty / banner lines
                if final_line.strip() == b"":
                    continue
                # If we see an auth failure indicator, break early
                if b"AUTH:FAILED" in final_line or b"Authentication failed" in final_line:
                    break
            if auth_ok:
                self.is_authenticated = True
                self.username = username
                self._last_auth = {
                    "method": "pubkey",
                    "username": username,
                    "key_id": chal_key_id,
                    "private_key_path": private_key_path,
                }
                self.logger.debug(f"Authenticated via public key {chal_key_id}")
                return True
            self.logger.error(
                f"Public key authentication failed: {last_line.decode(errors='ignore').strip() if last_line else 'no response'}"
            )
            return False
        except Exception as e:
            self.logger.error(f"Public key authentication error: {e}")
            return False

    async def list_ports(self) -> List[Any]:
        """Retrieve the list of available port or resource identifiers.

        Returns:
            list: Sequence of port name strings. Empty list if not authenticated
            or if a protocol/network error occurs.
        """
        if not self.is_ready():
            return []

        try:
            return await self._list_ports_standard()

        except Exception as e:
            self.logger.error(f"Failed to list ports: {e}", exc_info=True)
            return []

    async def connect_to_port(self, port_name: str) -> bool:
        """Attach (bind) the session to a chosen server port/resource.

        Args:
            port_name: The symbolic port/resource identifier previously returned
                by ``list_ports``.

        Returns:
            bool: True if the server confirmed attachment, False otherwise.
        """
        if not self.is_ready():
            return False

        try:
            return await self._connect_to_port_standard(port_name)

        except Exception as e:
            self.logger.error(f"Failed to connect to port: {e}", exc_info=True)
            return False

    async def send_data(self, data: Union[str, bytes]) -> bool:
        """Transmit a raw payload to the currently attached port.

        Args:
            data: UTF‑8 text (``str``) or arbitrary ``bytes`` buffer to send
                verbatim.

        Returns:
            bool: True if the payload was queued and flushed to the stream;
            False if not connected or a write failure occurred.
        """
        if not self.is_connected or not self.writer:
            return False

        try:
            if isinstance(data, str):
                data = data.encode()
            if not self.writer:
                return False
            self.writer.write(data)
            await self.writer.drain()
            return True

        except Exception as e:
            self.logger.error(f"Failed to send data: {e}", exc_info=True)
            self.is_connected = False
            return False

    async def read_data(self, timeout: Optional[float] = None) -> Optional[Union[str, bytes]]:
        """Read a chunk of bytes from the active port stream.

        A timeout returns an empty bytes object ``b""``. A remote close (EOF)
        returns ``None`` and transitions the adapter to a disconnected state.

        Args:
            timeout: Optional maximum seconds to wait; None blocks until data or EOF.

        Returns:
            bytes | None: Payload bytes (possibly empty), ``b""`` on timeout,
            or ``None`` if the remote closed the connection. Returns ``b""`` when
            not connected.
        """
        if not self.is_connected or not self.reader:
            return b""

        try:
            if timeout:
                # Read with timeout
                data = await asyncio.wait_for(self.reader.read(4096), timeout=timeout)
            else:
                # Read without timeout
                data = await self.reader.read(4096)
            # Detect EOF (server closed connection)
            if data == b"":
                # Mark disconnected and signal caller with None
                self.is_connected = False
                self.is_authenticated = False
                return None  # Caller interprets as closed
            return data

        except asyncio.TimeoutError:
            return b""
        except Exception as e:
            self.logger.debug(f"Failed to read data: {e}", exc_info=True)
            self.is_connected = False
            return b""

    async def close(self):
        """Gracefully terminate the TCP session and release resources.

        Sends a QUIT command (best-effort) then closes the underlying writer.
        Idempotent; may be invoked multiple times safely.
        """
        if not self.is_connected:
            return

        try:
            # Store writer reference temporarily
            writer = self.writer

            # Send appropriate quit command
            if writer:
                try:
                    writer.write(b"QUIT\n")
                    await writer.drain()

                    # Close writer
                    writer.close()
                    if hasattr(writer, "wait_closed"):
                        await writer.wait_closed()
                except Exception as e:
                    self.logger.error(f"Error during writer operations: {e}", exc_info=True)

            self.logger.debug("Connection closed")

        except Exception as e:
            self.logger.debug(f"Error closing connection: {e}", exc_info=True)

        finally:
            self.is_connected = False
            self.is_authenticated = False
            self.reader = None
            self.writer = None

    # Protocol-specific implementation methods

    async def _authenticate_standard_password(self, username: str, password: str) -> bool:
        """Execute the low-level password authentication exchange.

        Args:
            username: Login identity.
            password: Plain text password.

        Returns:
            bool: True on success, False on rejection or error.
        """
        if not self.writer or not self.reader:
            self.logger.error("Connection not established")
            return False

        try:
            # Attempt HMAC challenge first
            import base64
            import hashlib
            import hmac

            initiate = f"AUTH:USER:HMAC:{username}\n".encode()
            self.writer.write(initiate)
            await self.writer.drain()
            line = await self.reader.readline()
            # Expect AUTH:CHALLENGE:<nonce_b64>
            if line.startswith(b"AUTH:CHALLENGE:"):
                parts = line.decode(errors="ignore").strip().split(":")
                if len(parts) >= 3:
                    nonce_b64 = parts[2]
                    try:
                        nonce_raw = base64.b64decode(nonce_b64)
                        pw_hash = hashlib.sha256(password.encode()).digest()
                        sig = hmac.new(pw_hash, nonce_raw, hashlib.sha256).digest()
                        sig_b64 = base64.b64encode(sig).decode()
                        resp = f"AUTH:RESPONSE:{sig_b64}\n".encode()
                        self.writer.write(resp)
                        await self.writer.drain()
                        final = await self.reader.readline()
                        if final.startswith(b"AUTH:SUCCESS"):
                            self.is_authenticated = True
                            self.username = username
                            self.logger.debug(f"Authenticated (HMAC) as {username}")
                            return True
                        # Fall through to legacy if not success
                    except Exception:
                        pass
            # Plaintext legacy disabled
            self.logger.error("Plaintext password authentication disabled; server requires HMAC or another method")
            return False
        except Exception as e:
            self.logger.error(f"Authentication error: {e}", exc_info=True)
            return False

    async def _authenticate_standard_key(self, api_key: str) -> bool:
        """Execute the low-level API key authentication exchange.

        Args:
            api_key: The API key token.

        Returns:
            bool: True on success, False on failure or protocol error.
        """
        if not self.writer or not self.reader:
            self.logger.error("Connection not established")
            return False
        try:
            # Send authentication command
            auth_cmd = f"AUTH:KEY:{api_key}\n"
            self.writer.write(auth_cmd.encode())
            await self.writer.drain()

            # Wait for response
            data = await self.reader.readline()
            if b"Authentication failed" in data:
                self.logger.error("Authentication failed")
                return False

            self.is_authenticated = True
            self.username = "api-user"
            self.logger.debug("Authenticated with API key")
            return True
        except Exception as e:
            self.logger.error(f"Authentication error: {e}", exc_info=True)
            return False

    async def _list_ports_standard(self) -> List[str]:
        """Issue LIST command and parse multi-line response.

            Handles both the modern LIST:SUCCESS ... END:LIST framing and a legacy
            blob format containing the line 'Port List:'.
        Also supports JSON payload style: LIST:{"type":"PORT_LIST",...}.

            Returns:
                list: Port names; empty on error.
        """
        # Send list command
        if not self.writer:
            self.logger.error("Writer not available for LIST")
            return []
        self.writer.write(b"LIST\n")
        await self.writer.drain()

        # Read response line by line
        ports = []

        # Read the first non-empty line (should be LIST:...)
        try:
            if not self.reader:
                self.logger.error("Reader not available for LIST response")
                return []
            first_line = await self.reader.readline()
            # Skip any leading blank lines gracefully
            skip_guard = 0
            while isinstance(first_line, (bytes, bytearray)) and first_line.strip() == b"" and skip_guard < 3:
                first_line = await self.reader.readline()
                skip_guard += 1
        except Exception as e:
            # Some callers/tests may not provide readline; fall back to legacy format
            self.logger.debug(f"readline failed, falling back to read(): {e}", exc_info=True)
            first_line = b""

        # Interpret first line
        line_text = first_line.decode(errors="ignore").strip() if isinstance(first_line, (bytes, bytearray)) else ""

        # Case 1: JSON payload style (newer server)
        if line_text.startswith("LIST:{"):
            json_part = line_text[5:]
            try:
                import json as _json

                payload = _json.loads(json_part)
                raw_ports = payload.get("ports", []) if isinstance(payload, dict) else []
                # Deduplicate by name (last occurrence wins) and collect names
                dedup: dict = {}
                for entry in raw_ports:
                    if isinstance(entry, dict):
                        nm = entry.get("name") or entry.get("port")
                        if nm:
                            dedup[nm] = entry
                # Store raw metadata for callers if attribute available
                try:
                    self.last_port_metadata = list(dedup.values())  # type: ignore[attr-defined]
                except Exception:
                    # Justification: optional metadata assignment failure should not impact
                    # functional port listing; ignore to maintain backward compatibility
                    # with older adapter instances lacking the attribute.
                    pass
                return list(dedup.keys())
            except Exception as e:
                self.logger.error(f"Failed to parse JSON LIST payload: {e}: {line_text[:120]}", exc_info=True)
                return []

        # Case 2: Expected framed SUCCESS format
        if not line_text.startswith("LIST:SUCCESS"):
            # Legacy fallback reading blob
            try:
                if not self.reader:
                    return ports
                blob = await self.reader.read(4096)
                if isinstance(blob, (bytes, bytearray)):
                    text = blob.decode(errors="ignore")
                    if "Port List:" in text:
                        for line in text.splitlines():
                            if line and not line.startswith("Port List:"):
                                ports.append(line.strip())
                        return ports
            except Exception:
                # Justification: legacy LIST blob parsing best-effort; failure falls through
                # to standard error path with explicit log below.
                self.logger.debug("Legacy LIST blob parsing failed", exc_info=True)
            self.logger.error(f"Unexpected LIST response: {line_text if line_text else str(first_line)}")
            return []

        # Read port lines until END:LIST
        while True:
            if not self.reader:
                break
            line = await self.reader.readline()
            if not line:
                break

            line_str = line.decode().strip()
            if line_str == "END:LIST":
                break
            elif line_str.startswith("ERROR:"):
                self.logger.error(f"Server error: {line_str}")
                break
            elif line_str:
                ports.append(line_str)

        return ports

    async def _connect_to_port_standard(self, port_name: str) -> bool:
        """Issue CONNECT command and interpret response lines.

        Args:
            port_name: Target port identifier.

        Returns:
            bool: True if CONNECTED response received, False otherwise.
        """
        # Send connect command
        if not self.writer or not self.reader:
            self.logger.error("Connection not established for CONNECT")
            return False
        connect_cmd = f"CONNECT:{port_name}\n"
        self.writer.write(connect_cmd.encode())
        await self.writer.drain()

        # Wait for response
        data = await self.reader.readline()
        response = data.decode().strip()

        # Check for successful connection (format: CONNECTED:port_name:mode)
        if response.startswith("CONNECTED:"):
            parts = response.split(":", 2)
            if len(parts) >= 2:
                connected_port = parts[1]
                mode = parts[2] if len(parts) > 2 else "READ_ONLY"
                self.current_port = connected_port
                self.logger.debug(f"Connected to port {connected_port} in {mode} mode")
                return True

        # Check for error response
        if response.startswith("ERROR:"):
            self.logger.error(f"Failed to connect to port: {response}")
            return False

        # Unexpected response
        self.logger.error(f"Unexpected response from server: {response}")
        return False

    # All management-protocol specific methods removed

    async def reconnect(self) -> bool:
        """Reconnect using cached credentials and reattach previous port.

        Performs: connect -> authenticate (cached method) -> reattach last port
        if present.

        Returns:
            bool: True if all stages succeeded; False otherwise.
        """
        try:
            # Ensure we have auth info
            if not self._last_auth:
                self.logger.error("No cached credentials available for reconnect")
                return False

            # Establish TCP connection
            if not await self.connect():
                return False

            # Authenticate
            method = self._last_auth.get("method")
            if method == "password":
                if not await self.authenticate_with_password(
                    self._last_auth.get("username", ""),
                    self._last_auth.get("password", ""),
                ):
                    return False
            elif method == "api_key":
                if not await self.authenticate_with_key(self._last_auth.get("api_key", "")):
                    return False
            else:
                self.logger.error(f"Unsupported auth method for reconnect: {method}")
                return False

            # Reattach to previous port if any
            if self.current_port:
                return await self.connect_to_port(self.current_port)
            return True

        except Exception as e:
            self.logger.debug(f"Reconnect failed: {e}", exc_info=True)
            return False
