"""Abstract base class for TCP protocol handlers."""

import asyncio
from abc import ABC, abstractmethod
from typing import List, Tuple


class TcpProtocolHandler(ABC):
    """Base class for protocol handlers used by :class:`TcpInitiatorPort`.

    A handler is responsible for:

    * **Connection setup** — :meth:`establish` opens the TCP connection (and
      any additional connections required by multi-phase protocols) and runs
      any required handshake before returning the final data streams.
    * **Byte transforms** — :meth:`encode` / :meth:`decode` transform outgoing
      and incoming bytes respectively.  The default implementations are
      identity pass-throughs; override only when needed.
    * **Config validation** — :meth:`validate_config` is a class method that
      returns a list of missing or invalid field names for the protocol's
      ``protocol:`` sub-key.  Used by the adapter's ``validate_config``.
    """

    @classmethod
    def validate_config(cls, config: dict) -> List[str]:
        """Return a list of missing or invalid field names.

        Args:
            config: Full per-port config dict (read ``config.get("protocol", {})``.

        Returns:
            Empty list when the config is valid; list of problem field paths
            otherwise (e.g. ``["protocol.console_name", "protocol.username"]``).
        """
        return []

    @abstractmethod
    async def establish(
        self,
        host: str,
        port: int,
        config: dict,
    ) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open connection(s), run handshake, and return the final data streams.

        Args:
            host: Remote hostname or IP address.
            port: Remote TCP port.
            config: Full per-port config dict (TLS settings, timeout, protocol
                    sub-key, etc.).

        Returns:
            ``(reader, writer)`` pair representing the live data stream.

        Raises:
            ConnectionError: When the handshake fails for protocol-level reasons.
            asyncio.TimeoutError: When a network operation exceeds ``timeout``.
            OSError: When the TCP connection itself fails.
        """

    def encode(self, data: bytes) -> bytes:
        """Transform *data* before writing to the wire.  Default: pass-through."""
        return data

    def decode(self, data: bytes) -> bytes:
        """Transform *data* after reading from the wire.  Default: pass-through."""
        return data
