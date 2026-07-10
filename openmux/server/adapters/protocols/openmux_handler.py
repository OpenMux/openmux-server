"""OpenMux client protocol handler.

Wraps the existing :class:`~openmux.client.adapters.TcpClientAdapter` to
perform the OpenMux authentication + port-selection handshake, then exposes
the resulting raw asyncio streams for use by :class:`TcpInitiatorPort`.
"""

import asyncio
from typing import List, Tuple

from .base import TcpProtocolHandler


class OpenMuxHandler(TcpProtocolHandler):
    """Handles OpenMux client auth + port-selection handshake.

    Configuration (read from ``config["protocol"]``):

    .. code-block:: yaml

        protocol:
          type: openmux
          remote_port: prod-serial0   # required
          api_key: "secret"           # one of: api_key  OR  username + password
          # username: admin
          # password: secret
    """

    def __init__(self, config: dict) -> None:
        prot = config.get("protocol", {})
        self._remote_port: str = prot.get("remote_port", "")
        self._api_key: str = prot.get("api_key", "")
        self._username: str = prot.get("username", "")
        self._password: str = prot.get("password", "")

    @classmethod
    def validate_config(cls, config: dict) -> List[str]:
        prot = config.get("protocol", {})
        problems: List[str] = []
        if not prot.get("remote_port"):
            problems.append("protocol.remote_port")
        has_key = bool(prot.get("api_key"))
        has_up = bool(prot.get("username")) and bool(prot.get("password"))
        if not has_key and not has_up:
            problems.append("protocol.api_key (or protocol.username + protocol.password)")
        return problems

    async def establish(
        self,
        host: str,
        port: int,
        config: dict,
    ) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Connect, authenticate, and select the remote port.

        Returns the underlying asyncio streams from the TcpClientAdapter so
        that TcpInitiatorPort can read/write directly after the handshake.
        """
        from openmux.client.adapters import TcpClientAdapter

        use_tls = bool(config.get("use_tls", False))
        timeout = float(config.get("timeout", 10.0))

        conn = TcpClientAdapter(
            host=host,
            port=port,
            config={"use_tls": use_tls},
        )

        connect_ok = await asyncio.wait_for(conn.connect(), timeout=timeout)
        if not connect_ok:
            raise ConnectionError(f"Failed to connect to OpenMux server {host}:{port}")

        if self._api_key:
            auth_ok = await conn.authenticate_with_key(self._api_key)
        else:
            auth_ok = await conn.authenticate_with_password(
                self._username, self._password
            )
        if not auth_ok:
            await conn.close()
            method = "api_key" if self._api_key else "username/password"
            raise ConnectionError(
                f"Authentication failed to OpenMux {host}:{port} using {method}"
            )

        port_ok = await conn.connect_to_port(self._remote_port)
        if not port_ok:
            await conn.close()
            raise ConnectionError(
                f"Failed to connect to remote port '{self._remote_port}' on {host}:{port}"
            )

        # Handshake complete — return the raw streams for direct use.
        # TcpClientAdapter.reader/writer are plain asyncio streams at this point.
        if conn.reader is None or conn.writer is None:
            raise ConnectionError(
                f"OpenMux handshake succeeded but streams are unavailable for {host}:{port}"
            )
        return conn.reader, conn.writer
