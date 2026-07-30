"""Protocol handlers for the TCP initiator adapter.

Each handler encapsulates the connection-setup (handshake) and optional
byte-transform (encode/decode) logic for a specific application protocol
layered on top of a raw TCP connection.

Registered handlers
-------------------
plain     -- Raw TCP bytes, optional telnet command stripping
openmux   -- OpenMux client auth + port-selection handshake
conserver -- Conserver 2-phase master/group handshake (RFC PROTOCOL)
"""

from .base import TcpProtocolHandler
from .conserver import ConserverHandler
from .openmux_handler import OpenMuxHandler
from .plain import PlainHandler

PROTOCOL_HANDLERS: dict = {
    "plain": PlainHandler,
    "openmux": OpenMuxHandler,
    "conserver": ConserverHandler,
}


def get_handler(protocol_type: str, config: dict) -> TcpProtocolHandler:
    """Return an instantiated protocol handler for *protocol_type*.

    Falls back to :class:`PlainHandler` for unknown or empty type strings so
    that existing configs without a ``protocol:`` key continue to work.

    Args:
        protocol_type: One of ``"plain"``, ``"openmux"``, ``"conserver"``.
        config: Full per-port configuration dict (handler reads its own sub-key).

    Returns:
        Instantiated handler ready for :meth:`~TcpProtocolHandler.establish`.
    """
    cls = PROTOCOL_HANDLERS.get((protocol_type or "plain").lower(), PlainHandler)
    return cls(config)


__all__ = [
    "TcpProtocolHandler",
    "PlainHandler",
    "OpenMuxHandler",
    "ConserverHandler",
    "PROTOCOL_HANDLERS",
    "get_handler",
]
