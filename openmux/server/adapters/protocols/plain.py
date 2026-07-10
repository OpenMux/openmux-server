"""Plain TCP protocol handler with optional telnet command stripping."""

import asyncio
import ssl
from typing import List, Tuple

from .base import TcpProtocolHandler

# Telnet special byte values (RFC 854)
_IAC = 0xFF   # Interpret As Command
_SB  = 0xFA   # Start of subnegotiation (250)
_SE  = 0xF0   # End of subnegotiation   (240)
# Option-command bytes that consume one further option byte
_WILL = 0xFB
_WONT = 0xFC
_DO   = 0xFD
_DONT = 0xFE

_OPTION_CMDS = {_WILL, _WONT, _DO, _DONT}


class PlainHandler(TcpProtocolHandler):
    """Raw TCP handler with optional telnet negotiation stripping.

    Configuration (read from ``config["protocol"]``):

    .. code-block:: yaml

        protocol:
          type: plain              # (default when 'protocol:' is omitted)
          telnet_negotiation: strip  # absorb 0xFF command sequences silently
                                     # default: none (pass bytes through unchanged)
    """

    def __init__(self, config: dict) -> None:
        prot = config.get("protocol", {})
        self._telnet_negotiation: str = prot.get("telnet_negotiation", "none")

        # Stateful parser for multi-chunk telnet command stripping
        # States: "data" | "iac" | "iac_option" | "subneg" | "subneg_iac"
        self._iac_state = "data"

    @classmethod
    def validate_config(cls, config: dict) -> List[str]:
        prot = config.get("protocol", {})
        negotiation = prot.get("telnet_negotiation", "none")
        if negotiation not in ("none", "strip"):
            return ["protocol.telnet_negotiation (must be 'none' or 'strip')"]
        return []

    async def establish(
        self,
        host: str,
        port: int,
        config: dict,
    ) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a plain TCP (optionally TLS) connection."""
        use_tls = bool(config.get("use_tls", False))
        ssl_verify = config.get("ssl_verify", True)
        timeout = float(config.get("timeout", 10.0))

        ssl_ctx = None
        if use_tls:
            ssl_ctx = ssl.create_default_context()
            if not ssl_verify:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_ctx),
            timeout=timeout,
        )
        return reader, writer

    def decode(self, data: bytes) -> bytes:
        """Pass data through, stripping telnet IAC sequences when configured."""
        if self._telnet_negotiation != "strip":
            return data
        return self._strip_telnet(data)

    def _strip_telnet(self, data: bytes) -> bytes:
        """Remove telnet IAC command sequences from *data*.

        Maintains parser state across calls so that sequences split across
        multiple ``read()`` chunks are handled correctly.

        Mapping:
        * ``IAC IAC``                 → emit literal ``0xFF``
        * ``IAC WILL/WONT/DO/DONT X`` → discard 3 bytes
        * ``IAC SB … IAC SE``         → discard entire subnegotiation
        * ``IAC <other>``             → discard 2 bytes
        """
        out = bytearray()
        for b in data:
            if self._iac_state == "data":
                if b == _IAC:
                    self._iac_state = "iac"
                else:
                    out.append(b)

            elif self._iac_state == "iac":
                if b == _IAC:
                    out.append(_IAC)          # IAC IAC → literal 0xFF
                    self._iac_state = "data"
                elif b == _SB:
                    self._iac_state = "subneg"
                elif b in _OPTION_CMDS:
                    self._iac_state = "iac_option"  # consume one more byte
                else:
                    self._iac_state = "data"  # single-byte command, consumed

            elif self._iac_state == "iac_option":
                # Option byte after WILL/WONT/DO/DONT — discard
                self._iac_state = "data"

            elif self._iac_state == "subneg":
                if b == _IAC:
                    self._iac_state = "subneg_iac"
                # else: subneg payload byte, discard

            elif self._iac_state == "subneg_iac":
                if b == _SE:
                    self._iac_state = "data"  # end of subnegotiation
                else:
                    self._iac_state = "subneg"  # IAC within subneg data

        return bytes(out)
