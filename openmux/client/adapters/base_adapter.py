"""Abstract base class for OpenMux client connection adapters.

Defines the interface expected by higher-level UI and orchestration code for
establishing a session, authenticating, selecting a port/resource, and
exchanging raw data. Concrete subclasses encapsulate protocol specifics
(plain TCP, WebSocket, etc.) while presenting a uniform async API.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Union


class BaseClientAdapter(ABC):
    """Common interface and shared helpers for client adapters.

    Responsibilities of an adapter implementation:

    * Establish and maintain a transport connection to an OpenMux server
    * Perform one of the supported authentication flows
    * Enumerate available ports/resources
    * Attach to a selected port for subsequent bidirectional data exchange
    * Provide non-blocking send/receive primitives with optional timeouts
    * Surface identifying/protocol metadata (session id, negotiated version)
    """

    def __init__(self, host: str, port: int, config: Optional[Dict[str, Any]] = None):
        """Construct a new adapter instance (not yet connected).

        Args:
            host: Server hostname or IP address.
            port: TCP port number exposed by the server.
            config: Optional implementation-specific configuration values.
        """
        self.host = host
        self.port = port
        self.config = config or {}
        self.logger = logging.getLogger(f"openmux.client.adapter.{self.__class__.__name__.lower()}")

        # Connection state
        self.is_connected = False
        self.is_authenticated = False
        self.username = None

        # Access-mode state for the currently attached port, kept in sync by
        # adapters that support out-of-band mode control frames. `None` means
        # the adapter does not know (or does not support) the granted mode.
        self.access_mode: Optional[str] = None
        self.rw_holders: list = []
        self.max_rw_users: Optional[int] = None

        # Protocol-specific attributes (to be set by subclasses)
        self.protocol_version = None
        self.session_id = None

    @abstractmethod
    async def connect(self) -> bool:
        """Open the underlying transport connection.

        Returns:
            bool: True if the connection was successfully established; False
            if an error occurred or the server was unreachable.
        """
        pass

    @abstractmethod
    async def authenticate_with_password(self, username: str, password: str) -> bool:
        """Authenticate using username/password credentials.

        Args:
            username: User identifier.
            password: Plain text password (transport security may vary by
                adapter implementation).

        Returns:
            bool: True if credentials were accepted; False otherwise.
        """
        pass

    @abstractmethod
    async def authenticate_with_key(self, api_key: str) -> bool:
        """Authenticate using an API key token.

        Args:
            api_key: Provisioned API key string.

        Returns:
            bool: True if the key was accepted; False if rejected or an error
            occurred.
        """
        pass

    @abstractmethod
    async def list_ports(self) -> list:
        """Retrieve the collection of available port/resource names.

        Returns:
            list: Port identifiers (string elements). Exact semantics may vary
            by adapter (e.g., filtering or metadata shaping).
        """
        pass

    @abstractmethod
    async def connect_to_port(self, port_name: str) -> bool:
        """Attach the session to a specific server port/resource.

        Args:
            port_name: Canonical port identifier returned by ``list_ports``.

        Returns:
            bool: True if attachment succeeded; False otherwise.
        """
        pass

    @abstractmethod
    async def send_data(self, data: Union[str, bytes]) -> bool:
        """Transmit raw data to the currently attached port.

        Args:
            data: Text (str) or binary (bytes) payload. Implementations should
                accept either and encode/forward appropriately.

        Returns:
            bool: True if the payload was queued/flushed successfully; False
            if the connection is not writable or an error occurred.
        """
        pass

    @abstractmethod
    async def read_data(self, timeout: Optional[float] = None) -> Optional[Union[str, bytes]]:
        """Receive the next chunk of data from the server.

        Args:
            timeout: Optional maximum seconds to wait; if exceeded an empty
                value (implementation defined) or None may be returned.

        Returns:
            Optional[Union[str, bytes]]: Payload read; may be ``None`` to
            indicate EOF or no data depending on adapter semantics.
        """
        pass

    @abstractmethod
    async def close(self):
        """Terminate the connection and release resources."""
        pass

    # Access-mode control (read-only/read-write). Base implementations are
    # no-ops so adapters that don't support the underlying control channel
    # degrade gracefully instead of raising. Concrete adapters that support
    # out-of-band mode control frames (TCP, WebSocket) should override these.
    async def request_read_write(self) -> bool:
        """Ask the server to promote this client to read-write.

        Returns:
            bool: True if the request was sent; the actual grant/denial is
            reported asynchronously via `read_data()` and reflected in
            `access_mode`. False if unsupported or the request could not be sent.
        """
        return False

    async def release_read_write(self) -> bool:
        """Ask the server to voluntarily demote this client to read-only.

        Returns:
            bool: True if the request was sent; False if unsupported or the
            request could not be sent.
        """
        return False

    async def force_read_write(self) -> bool:
        """Ask the server to demote other read-write holders and promote this client.

        Returns:
            bool: True if the request was sent; False if unsupported or the
            request could not be sent.
        """
        return False

    async def query_rw_holders(self) -> bool:
        """Ask the server for the current read-write holder(s) of the attached port.

        Returns:
            bool: True if the request was sent; False if unsupported or the
            request could not be sent.
        """
        return False

    # Common utility methods
    def get_connection_info(self) -> Dict[str, Any]:
        """Return a snapshot of notable connection and session attributes.

        Returns:
            Dict[str, Any]: Serializable connection metadata including address,
            authentication state, and protocol/session identifiers.
        """
        return {
            "host": self.host,
            "port": self.port,
            "connected": self.is_connected,
            "authenticated": self.is_authenticated,
            "username": self.username,
            "protocol_version": self.protocol_version,
            "session_id": self.session_id,
            "adapter_type": self.__class__.__name__,
        }

    def is_ready(self) -> bool:
        """Return True if the adapter is both connected and authenticated."""
        return self.is_connected and self.is_authenticated
