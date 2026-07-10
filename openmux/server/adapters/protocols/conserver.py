"""Conserver protocol handler.

Implements the two-phase conserver handshake described in
https://github.com/bstansell/conserver/blob/master/PROTOCOL

Phase 1 — Master connection (line-based):
  connect → read "ok" → login → (passwd? →) call <console> → get group port

Phase 2 — Group connection (line-based until console mode):
  connect → read "ok" → login → (passwd? →) call <console> → read "[attached]"
  → console mode (raw bytes with 0xFF escape sequences)

Console-mode byte encoding (same escape byte as telnet IAC):
  0xFF 0xFF → literal 0xFF in data
  0xFF 'E'/'G'/'Z'/'.' → out-of-band control signals (stripped on decode)
  Outgoing 0xFF bytes must be doubled: 0xFF → 0xFF 0xFF
"""

import asyncio
import ssl
from typing import List, Optional, Tuple

from .base import TcpProtocolHandler

_IAC = 0xFF  # conserver escape byte (same value as telnet IAC)


class ConserverHandler(TcpProtocolHandler):
    """Handles the conserver 2-phase handshake and console-mode byte escaping.

    Configuration (read from ``config["protocol"]``):

    .. code-block:: yaml

        protocol:
          type: conserver
          console_name: "blade1"   # required
          username: "admin"        # required
          password: "secret"       # optional (omit for passwordless/PAM auth)
    """

    def __init__(self, config: dict) -> None:
        prot = config.get("protocol", {})
        self._console_name: str = prot.get("console_name", "")
        self._username: str = prot.get("username", "")
        self._password: str = prot.get("password", "")

        # Stateful decode: True when the previous chunk ended with a 0xFF byte
        self._pending_iac: bool = False

    @classmethod
    def validate_config(cls, config: dict) -> List[str]:
        prot = config.get("protocol", {})
        problems: List[str] = []
        if not prot.get("console_name"):
            problems.append("protocol.console_name")
        if not prot.get("username"):
            problems.append("protocol.username")
        return problems

    async def establish(
        self,
        host: str,
        port: int,
        config: dict,
    ) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Run the full 2-phase conserver handshake and return console streams.

        Phase 1 connects to the master port (``host``:``port``), logs in, and
        obtains the group port number via ``call <console_name>``.

        Phase 2 connects to the group port (same host, dynamic port), repeats
        the login, and attaches to the console.

        Raises:
            ConnectionError: On any protocol-level failure (bad greeting,
                             auth failure, unknown console, remote redirect).
            asyncio.TimeoutError: When a network operation exceeds the timeout.
            OSError: When a TCP connection attempt fails.
        """
        timeout = float(config.get("timeout", 10.0))
        ssl_ctx: Optional[ssl.SSLContext] = None
        if config.get("use_tls"):
            ssl_ctx = ssl.create_default_context()
            if not config.get("ssl_verify", True):
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE

        # ── Phase 1: master ───────────────────────────────────────────────
        r1, w1 = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_ctx),
            timeout=timeout,
        )
        try:
            group_port = await self._master_handshake(r1, w1, timeout)
        finally:
            w1.close()
            try:
                await w1.wait_closed()
            except Exception:
                pass

        # ── Phase 2: group ────────────────────────────────────────────────
        r2, w2 = await asyncio.wait_for(
            asyncio.open_connection(host, group_port, ssl=ssl_ctx),
            timeout=timeout,
        )
        await self._group_handshake(r2, w2, timeout)
        return r2, w2

    # ── Handshake helpers ──────────────────────────────────────────────────

    async def _master_handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        timeout: float,
    ) -> int:
        """Complete master-mode login + call, return the group port number."""
        greeting = await self._read_line(reader, timeout)
        if not greeting.startswith("ok"):
            raise ConnectionError(f"Conserver master not ready: {greeting!r}")

        await self._login(reader, writer, timeout)

        await self._send_line(writer, f"call {self._console_name}")
        response = await self._read_line(reader, timeout)

        if response.startswith("@"):
            raise ConnectionError(
                f"Console '{self._console_name}' is managed by remote conserver "
                f"{response!r}; remote redirect is not supported"
            )
        try:
            return int(response.strip())
        except ValueError:
            raise ConnectionError(
                f"Unexpected response to 'call {self._console_name}': {response!r}"
            )

    async def _group_handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        timeout: float,
    ) -> None:
        """Complete group-mode login + call, leave connection in console mode."""
        greeting = await self._read_line(reader, timeout)
        if not greeting.startswith("ok"):
            raise ConnectionError(f"Conserver group port not ready: {greeting!r}")

        await self._login(reader, writer, timeout)

        await self._send_line(writer, f"call {self._console_name}")
        response = await self._read_line(reader, timeout)

        if not response.startswith("["):
            raise ConnectionError(
                f"Conserver attach failed for '{self._console_name}': {response!r}"
            )
        # response is e.g. "[attached]", "[spy]", "[read-only -- initializing]"

    async def _login(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        timeout: float,
    ) -> None:
        """Send ``login <username>``, handle optional password challenge."""
        await self._send_line(writer, f"login {self._username}")
        response = await self._read_line(reader, timeout)

        if response.startswith("passwd?"):
            if not self._password:
                raise ConnectionError(
                    "Conserver requires a password but none is configured"
                )
            await self._send_line(writer, self._password)
            response = await self._read_line(reader, timeout)

        if not response.startswith("ok"):
            raise ConnectionError(f"Conserver login failed: {response!r}")

    @staticmethod
    async def _read_line(reader: asyncio.StreamReader, timeout: float) -> str:
        data = await asyncio.wait_for(reader.readline(), timeout=timeout)
        return data.decode(errors="replace").strip()

    @staticmethod
    async def _send_line(writer: asyncio.StreamWriter, text: str) -> None:
        writer.write((text + "\n").encode())
        await writer.drain()

    # ── Byte transforms ───────────────────────────────────────────────────

    def decode(self, data: bytes) -> bytes:
        """Strip conserver 0xFF escape sequences from incoming data.

        * ``0xFF 0xFF`` → emit literal ``0xFF``
        * ``0xFF <cmd>`` → discard both bytes (out-of-band control)

        Maintains state across calls to handle sequences split across chunks.
        """
        out = bytearray()
        for b in data:
            if self._pending_iac:
                self._pending_iac = False
                if b == _IAC:
                    out.append(_IAC)  # 0xFF 0xFF → literal 0xFF
                # else: out-of-band command byte ('E','G','Z','.') — discard
            else:
                if b == _IAC:
                    self._pending_iac = True
                else:
                    out.append(b)
        return bytes(out)

    def encode(self, data: bytes) -> bytes:
        """Escape literal 0xFF bytes in outgoing data as ``0xFF 0xFF``."""
        if _IAC not in data:
            return data
        out = bytearray()
        for b in data:
            out.append(b)
            if b == _IAC:
                out.append(_IAC)
        return bytes(out)
