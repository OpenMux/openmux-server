"""Backward-compatibility shim for the OpenMux client adapter.

The full implementation has been merged into :class:`TcpInitiatorAdapter`.
Use ``tcp_initiator_ports`` with ``protocol: {type: openmux, ...}`` in new
configs.  Existing ``openmux_client_ports`` configs continue to work
unchanged via the compat alias in :class:`TcpInitiatorAdapter`.
"""

from .tcp_initiator import TcpInitiatorAdapter as OpenMuxClientAdapter
from .tcp_initiator import TcpInitiatorPort as OpenMuxClientPort

__all__ = ["OpenMuxClientAdapter", "OpenMuxClientPort"]
