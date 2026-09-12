"""Backward-compatibility shim for the OpenMux client adapter.

The full implementation has been merged into :class:`TcpInitiatorAdapter`.
Use ``tcp_initiator_ports`` with ``protocol: {type: openmux, ...}`` in your
configs. The deprecated ``openmux_client_ports`` section was removed (ticket
#72); configs that still use it fail schema validation.
"""

from .tcp_initiator import TcpInitiatorAdapter as OpenMuxClientAdapter
from .tcp_initiator import TcpInitiatorPort as OpenMuxClientPort

__all__ = ["OpenMuxClientAdapter", "OpenMuxClientPort"]
