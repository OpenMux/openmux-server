"""Telnet Listener Adapter.

Expose existing OpenMux ports over simple Telnet-compatible TCP sockets. Each
listener entry binds to a host/port pair and attaches clients directly to a
configured OpenMux port (local or federated). Access controls rely on per-
listener ACLs and optional read-only enforcement (client input dropped).
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any, Dict, List, Optional, Tuple

from ..access_decision import Code, Mode, decide
from ..listener_session import MENU_TARGET, ListenerConfig, ListenerSessionManager, build_listener
from .base_adapter import AdapterCapability, BaseGenericAdapter
from .listener_common import ip_allowed, parse_login, validate_listener_entries
from .protocols.plain import TelnetIacStripper

# Sentinel 'target' value that puts a listener into interactive port-menu mode.
_MENU_TARGET = MENU_TARGET

# Auth/menu phase tuning (only applies before a port is attached; the
# post-attach data pump has no timeout so connected sessions stay open).
_AUTH_LOGIN_MAX_ATTEMPTS = 3
_AUTH_PORT_MAX_ATTEMPTS = 5
_AUTH_IDLE_TIMEOUT = 60.0
_AUTH_LINE_MAX_LEN = 256
_IAC_WILL_ECHO = bytes([255, 251, 1])  # IAC WILL ECHO
_IAC_WONT_ECHO = bytes([255, 252, 1])  # IAC WONT ECHO


class _StreamSessionTransport:
    """The SessionTransport seam over an asyncio reader/writer pair (issue #100).

    The streams deliver raw wire bytes to the session module. IAC stripping
    happens only in the auth/menu phase (`_read_auth_line` builds a
    `TelnetIacStripper` there); the data pump has always seen raw bytes, and
    moving the pump preserved that exactly.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        # Set by `drop_line_feed` when a menu prompt ended a line at a CR: the LF that a Telnet client
        # sends with that CR is part of the line already read, not the next one.
        self._skip_line_feed = False
        # Bytes read while dropping that LF, handed back before the stream is read again.
        self._pushback = b""

    async def read(self, n: int) -> bytes:
        """Read up to ``n`` bytes, skipping the LF tail of a CR LF a menu prompt already consumed.

        The flag is cleared by the first byte returned, whatever that byte is: a client that sends CR
        alone keeps working, because its next keystroke then starts the next line as ordinary data.
        Anything read along with the dropped LF is kept and returned next, so no byte is lost.
        """
        if self._pushback:
            take, self._pushback = self._pushback[:n], self._pushback[n:]
            return take
        while True:
            chunk = await self._reader.read(n)
            if not (self._skip_line_feed and chunk[:1] == b"\n"):
                self._skip_line_feed = False
                return chunk
            self._skip_line_feed = False
            if len(chunk) > 1:
                self._pushback = chunk[1:]

    async def readexactly(self, n: int) -> bytes:
        """Read exactly ``n`` bytes, serving any byte that `read` held back first."""
        data = bytearray()
        while len(data) < n:
            chunk = await self.read(n - len(data))
            if not chunk:
                raise asyncio.IncompleteReadError(bytes(data), expected=n)
            data.extend(chunk)
        return bytes(data)

    def drop_line_feed(self) -> None:
        """Mark the CR LF boundary so the paired LF is not read as the next line.

        Optional seam member (issue #100 transport): `listener_common.read_prompt_line` calls it after a
        CR. A Telnet client may deliver the LF in the same segment as the CR or in the next one, so this
        records the boundary instead of looking for a byte that may not have arrived yet: the next
        `read` discards that LF, and the keystroke the operator typed next is read as the next line.
        """
        self._skip_line_feed = True

    def write(self, data: bytes) -> None:
        self._writer.write(data)

    async def drain(self) -> None:
        await self._writer.drain()

    async def close(self) -> None:
        self._writer.close()
        await self._writer.wait_closed()


class TelnetListenerAdapter(BaseGenericAdapter):
    """Adapter exposing OpenMux ports via raw TCP/Telnet sockets."""

    def get_adapter_type(self) -> str:
        """Return adapter type for security policy and factory lookup."""
        return "telnet_listener"

    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        self.logger = logging.getLogger(f"openmux.adapter.telnet_listener.{name}")
        raw_entries = config.get("telnet_listener") if isinstance(config, dict) else []
        self.listeners: List[ListenerConfig] = []
        if isinstance(raw_entries, list):
            for entry in raw_entries:
                spec = self._build_listener(entry)
                if spec:
                    self.listeners.append(spec)
        self.servers: Dict[str, asyncio.AbstractServer] = {}
        self.console_manager = None
        self.auth_manager = None
        # Everything from attach to detach lives in the listener session module
        # (issue #100); this adapter keeps only the Telnet transport.
        # `channel_owner=self` keeps the ConsoleManager channel contract on this
        # adapter's pinned delegation names.
        self._sessions = ListenerSessionManager(
            display_name="Telnet",
            logger=self.logger,
            channel_owner=self,
            get_console_manager=lambda: self.console_manager,
            get_auth_manager=lambda: self.auth_manager,
            get_port_manager=lambda: self.main_port_manager,
        )

    @property
    def sessions(self) -> Dict[str, Any]:
        """Attached sessions, owned by the listener session module (issue #100)."""
        return self._sessions.sessions

    # ------------------------------------------------------------------
    # Adapter contract

    def get_capabilities(self) -> set:
        return {
            AdapterCapability.ACCEPTS_CONNECTIONS,
            AdapterCapability.BIDIRECTIONAL_DATA,
        }

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> bool:
        if not isinstance(config, dict):
            return False
        entries = config.get("telnet_listener")
        if entries is None:
            return True
        if not isinstance(entries, list):
            return False
        return validate_listener_entries(entries, set())

    async def _do_start(self) -> bool:
        if not self.listeners:
            self.logger.info("Telnet listener adapter has no entries; nothing to bind")
            return True
        if not self.console_manager:
            self.logger.error("Telnet listener adapter requires a console manager reference")
            return False
        success = True
        for spec in self.listeners:
            if not spec.enabled:
                self.logger.info("Telnet listener '%s' disabled via configuration", spec.name)
                continue
            if not await self._start_single_listener(spec):
                success = False
        return success

    async def _start_single_listener(self, spec: ListenerConfig) -> bool:
        """Bind a single listener's server socket. Returns True on success."""
        try:

            async def _connection_entry(reader, writer, listener_spec=spec):
                await self._handle_connection(listener_spec, reader, writer)

            server = await asyncio.start_server(
                _connection_entry,
                spec.bind_host,
                spec.bind_port,
            )
            self.servers[spec.name] = server
            self.logger.info("Telnet listener '%s' bound to %s", spec.name, self._format_sockname(server.sockets))
            try:
                sock = server.sockets[0]
                if sock:
                    sockname = sock.getsockname()
                    spec.effective_host = sockname[0]
                    spec.effective_port = sockname[1]
            except Exception:
                # justification: optional metadata; the bound value is authoritative
                pass
            return True
        except Exception as exc:
            self.logger.error(
                "Failed to start telnet listener '%s' on %s:%s: %s",
                spec.name,
                spec.bind_host,
                spec.bind_port,
                exc,
                exc_info=True,
            )
            return False

    async def _stop_single_listener(self, name: str) -> None:
        """Close one listener's server and disconnect only its sessions."""
        await self._sessions.drop_listener_sessions(name, reason="listener removed")
        server = self.servers.pop(name, None)
        if server is not None:
            try:
                server.close()
                await server.wait_closed()
                self.logger.info("Telnet listener '%s' stopped", name)
            except Exception:
                self.logger.warning("Error stopping telnet listener '%s'", name, exc_info=True)

    async def reconcile_ports(self, new_config: Any) -> Dict[str, Any]:
        """Incrementally reconcile telnet listeners without disturbing unrelated sessions.

        Args:
            new_config: Dict with key 'telnet_listener' as list, or direct list.

        Returns:
            Summary dict: {added, removed, updated, unchanged}.
        """
        if isinstance(new_config, dict) and isinstance(new_config.get("telnet_listener"), list):
            items = list(new_config["telnet_listener"])
        elif isinstance(new_config, list):
            items = list(new_config)
        else:
            items = []

        new_by_name: Dict[str, Dict[str, Any]] = {}
        for entry in items:
            if isinstance(entry, dict) and entry.get("name"):
                new_by_name[str(entry["name"])] = entry

        old_by_name = {spec.name: spec for spec in self.listeners}
        old_names = set(old_by_name.keys())
        new_names = set(new_by_name.keys())
        removed = sorted(old_names - new_names)
        added = sorted(new_names - old_names)
        common = sorted(old_names & new_names)

        def _material_cfg(spec: ListenerConfig) -> Dict[str, Any]:
            return {
                "bind_host": spec.bind_host,
                "bind_port": spec.bind_port,
                "target": spec.target,
                "read_only": spec.read_only,
                "acl_raw": list(spec.acl_raw),
                "enabled": spec.enabled,
                "require_auth": spec.require_auth,
            }

        updated: List[str] = []
        unchanged: List[str] = []
        for name in common:
            new_spec = self._build_listener(new_by_name[name])
            if new_spec is None:
                continue
            if _material_cfg(old_by_name[name]) == _material_cfg(new_spec):
                unchanged.append(name)
            else:
                updated.append(name)

        await self._rebuild_listeners(removed, updated, added, new_by_name)

        summary = {"added": added, "removed": removed, "updated": updated, "unchanged": unchanged}
        self.logger.info(
            "Telnet listener adapter %s reconcile: +%s ~%s -%s unchanged=%s",
            self.name,
            len(added),
            len(updated),
            len(removed),
            len(unchanged),
        )
        return summary

    async def _rebuild_listeners(
        self, removed: List[str], updated: List[str], added: List[str], new_by_name: Dict[str, Dict[str, Any]]
    ) -> None:
        """Stop removed/updated listeners, start added/updated, refresh state.

        Stale listeners (removed + updated) stop first; the kept, added and
        updated specs then rebuild ``self.listeners``. A spec that fails to
        build on re-apply is skipped (the old listener already stopped).
        """
        for name in removed + updated:
            await self._stop_single_listener(name)

        specs_by_name = {spec.name: spec for spec in self.listeners if spec.name not in (removed + updated)}
        for name in added + updated:
            spec = self._build_listener(new_by_name[name])
            if spec is None:
                continue
            specs_by_name[name] = spec
            if spec.enabled:
                await self._start_single_listener(spec)
            else:
                self.logger.info("Telnet listener '%s' disabled via configuration", spec.name)

        self.listeners = [specs_by_name[n] for n in sorted(specs_by_name.keys())]
        self._set_running(True)

    async def stop(self) -> None:
        self.is_running = False
        # Close sessions
        await self._sessions.disconnect_all(reason="adapter stop")
        # Stop servers
        for name, server in list(self.servers.items()):
            try:
                server.close()
                await server.wait_closed()
                self.logger.info("Telnet listener '%s' stopped", name)
            except Exception:
                self.logger.warning("Error stopping telnet listener '%s'", name, exc_info=True)
        self.servers.clear()

    async def create_port(self, port_name: str, config: Dict[str, Any]) -> Optional[Any]:
        return None

    async def destroy_port(self, port_name: str) -> None:
        return None

    def get_port_configurations(self) -> Dict[str, Dict[str, Any]]:
        return {}

    # ------------------------------------------------------------------
    # Console manager hooks

    def set_console_manager(self, console_manager):
        self.console_manager = console_manager
        try:
            if hasattr(console_manager, "register_client_manager"):
                console_manager.register_client_manager(self)
        except Exception:
            self.logger.warning("Failed to register telnet adapter as client manager", exc_info=True)
        # Subscribe to port meta updates so attached telnet sessions get the
        # live power notice when a feed changes (mirrors the client listener).
        try:
            pm = getattr(console_manager, "port_manager", None)
            if pm and hasattr(pm, "register_meta_listener"):
                pm.register_meta_listener(self._on_port_meta_update)
        except Exception:
            # justification: optional meta subscription; power control still works without the notice
            pass

    def set_auth_manager(self, auth_manager):
        """Wire the shared AuthManager for listeners with require_auth enabled."""
        self.auth_manager = auth_manager

    def _on_port_meta_update(self, port_name: str, changes: Optional[Dict[str, Any]] = None) -> None:
        """Live power notice for attached sessions; the session module owns the push."""
        self._sessions.on_port_meta(port_name, changes)

    async def send_data_to_client(self, client_id: str, data: bytes) -> bool:
        """ConsoleManager cross-adapter channel entry point; delegates to the session module."""
        return await self._sessions.send_data(client_id, data)

    # ------------------------------------------------------------------
    # Connection handling

    async def _handle_connection(
        self,
        listener: ListenerConfig,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer_ip = self._peer_ip(writer)
        if not self._client_allowed(listener, peer_ip):
            await self._send_and_close(writer, b"Access denied\r\n")
            return
        if not self.main_port_manager:
            await self._send_and_close(writer, b"Server unavailable\r\n")
            return
        resolved = await self._resolve_session_target(listener, reader, writer, peer_ip)
        if resolved is None:
            return  # Auth/menu phase already sent an error and closed the connection.
        username, port_name = resolved
        session = self._sessions.create_session(
            listener,
            _StreamSessionTransport(reader, writer),
            port_name=port_name,
            peer_ip=peer_ip,
            username=username,
        )
        attached = await self._sessions.attach(session)
        if attached is None:
            await self._send_and_close(writer, decide(Code.INTERNAL, mode=Mode.READ_ONLY).text.encode())
            return
        if not attached.ok:
            # The listener refuses with what the access module says, not with what this
            # transport guessed (issue #98); the session module renders both paths.
            await self._send_and_close(writer, attached.text.encode())
            return
        await self._sessions.serve(session)

    async def send_control_frame_to_client(self, client_id: str, payload: Dict[str, Any]) -> bool:
        """ConsoleManager frame entry point; the session module renders it for the raw terminal."""
        return await self._sessions.send_frame(client_id, payload)

    def _resolve_client_meta(self, client_id: str) -> Dict[str, Any]:
        """ConsoleManager cross-adapter IP lookup; the session module owns the registry."""
        return self._sessions.client_meta(client_id)

    # ------------------------------------------------------------------
    # Authentication and multi-port menu (auth/menu phase only; the pump
    # above has no timeout, so once a port is attached sessions stay open
    # indefinitely).

    async def _resolve_session_target(
        self,
        listener: ListenerConfig,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer_ip: str,
    ) -> Optional[Tuple[str, str]]:
        """Run the login prompt (if required) and resolve the target port.

        Returns (username, port_name) on success, or None if the connection
        was already closed (auth failure, bad selection, or client quit).
        """
        username = f"telnet_{listener.name}"
        embedded_descriptor: Optional[str] = None

        if listener.require_auth:
            auth_result = await self._run_login(listener, reader, writer, peer_ip)
            if auth_result is None:
                return None
            username, embedded_descriptor = auth_result

        if embedded_descriptor and listener.target == _MENU_TARGET:
            port_name = await self._sessions.resolve_target(embedded_descriptor)
            if not port_name:
                await self._send_and_close(writer, f"Port {embedded_descriptor} unavailable\r\n".encode())
                return None
            return username, port_name

        if listener.target == _MENU_TARGET:
            port_name = await self._run_port_menu(writer, reader)
            if not port_name:
                return None
            return username, port_name

        port_name = await self._sessions.resolve_target(listener.target)
        if not port_name:
            await self._send_and_close(writer, f"Port {listener.target} unavailable\r\n".encode())
            return None
        return username, port_name

    @staticmethod
    def _parse_login(line: str) -> Tuple[str, Optional[str]]:
        """Split a login line into (username, embedded_port_descriptor).

        Supports ``<username>+<port>`` and ``<username>:<port>``. See
        `listener_common.parse_login` for the delimiter rules (shared with
        `ssh_listener`).
        """
        return parse_login(line)

    async def _read_auth_line(
        self,
        reader: asyncio.StreamReader,
        stripper: TelnetIacStripper,
        max_len: int = _AUTH_LINE_MAX_LEN,
        timeout: float = _AUTH_IDLE_TIMEOUT,
    ) -> Optional[str]:
        """Read one line from the client during the auth/menu phase.

        Strips telnet IAC negotiation bytes so a real telnet client's
        automatic option replies don't corrupt the buffered line.
        """
        buf = bytearray()
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(256), timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                return None
            if not chunk:
                return None
            buf.extend(stripper.strip(chunk))
            nl = buf.find(b"\n")
            if nl != -1:
                line = bytes(buf[:nl]).decode("utf-8", errors="ignore").rstrip("\r")
                return line
            if len(buf) > max_len:
                return None

    async def _run_login(
        self,
        listener: ListenerConfig,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer_ip: str,
    ) -> Optional[Tuple[str, Optional[str]]]:
        """Prompt for username/password. Returns (username, embedded_port) or None."""
        if not self.auth_manager:
            self.logger.error("Telnet listener '%s' has require_auth set but no auth manager is configured", listener.name)
            await self._send_and_close(writer, b"Server misconfigured: authentication unavailable\r\n")
            return None

        stripper = TelnetIacStripper()
        for _ in range(_AUTH_LOGIN_MAX_ATTEMPTS):
            writer.write(b"login: ")
            await writer.drain()
            login_line = await self._read_auth_line(reader, stripper)
            if login_line is None:
                await self._close_quiet(writer)
                return None
            username, embedded_descriptor = self._parse_login(login_line)
            if not username:
                continue

            if self.auth_manager.is_user_locked(username, peer_ip):
                await self._send_and_close(writer, b"Login incorrect\r\n")
                return None

            writer.write(_IAC_WILL_ECHO + b"Password: ")
            await writer.drain()
            password = await self._read_auth_line(reader, stripper)
            writer.write(_IAC_WONT_ECHO + b"\r\n")
            await writer.drain()
            if password is None:
                await self._close_quiet(writer)
                return None

            if self.auth_manager.authenticate_user(username, password):
                if hasattr(self.auth_manager, "clear_auth_failures"):
                    self.auth_manager.clear_auth_failures(username, peer_ip)
                return username, embedded_descriptor

            if hasattr(self.auth_manager, "register_auth_failure"):
                self.auth_manager.register_auth_failure(username, peer_ip)
            writer.write(b"Login incorrect\r\n")
            await writer.drain()

        await self._close_quiet(writer)
        return None

    async def _run_port_menu(self, writer: asyncio.StreamWriter, reader: asyncio.StreamReader) -> Optional[str]:
        """Print the port list, prompt for a selection, and resolve it.

        Returns the resolved port name, or None if the client quit, gave up
        after too many bad attempts, or disconnected.
        """
        stripper = TelnetIacStripper()
        await self._sessions.send_port_list(writer)
        for _ in range(_AUTH_PORT_MAX_ATTEMPTS):
            writer.write(b"Port: ")
            await writer.drain()
            choice = await self._read_auth_line(reader, stripper)
            if choice is None:
                await self._close_quiet(writer)
                return None
            choice = choice.strip()
            if not choice or choice in ("?", "list"):
                await self._sessions.send_port_list(writer)
                continue
            if choice in ("quit", "exit"):
                await self._close_quiet(writer)
                return None
            port_name = await self._sessions.resolve_target(choice)
            if port_name:
                return port_name
            writer.write(f"Unknown port: {choice}\r\n".encode())
            await writer.drain()

        await self._close_quiet(writer)
        return None

    async def _close_quiet(self, writer: asyncio.StreamWriter) -> None:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            # justification: shutdown cleanup; the transport may already be closed
            pass

    # ------------------------------------------------------------------
    # Helpers

    def _build_listener(self, entry: Dict[str, Any]) -> Optional[ListenerConfig]:
        """Shared builder; both transports require auth unless the entry disables it (issue #100)."""
        return build_listener(entry, display_name="Telnet", require_auth_default=True, logger=self.logger)

    def _client_allowed(self, listener: ListenerConfig, peer_ip: str) -> bool:
        return ip_allowed(peer_ip, listener.compiled_acl)

    def _peer_ip(self, writer: asyncio.StreamWriter) -> str:
        try:
            peer = writer.get_extra_info("peername")
            if isinstance(peer, tuple) and peer:
                return peer[0]
            if isinstance(peer, str):
                return peer
        except Exception:
            # justification: heuristic peer lookup; "unknown" is the safe fallback
            pass
        return "unknown"

    async def _send_and_close(self, writer: asyncio.StreamWriter, payload: bytes) -> None:
        try:
            writer.write(payload)
            await writer.drain()
        except Exception:
            self.logger.error("Telnet: failed to deliver final message before close", exc_info=True)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            # justification: shutdown cleanup; the transport may already be closed
            pass

    def _format_sockname(self, sockets: Optional[List[socket.socket]]) -> str:
        if not sockets:
            return "unknown"
        parts = []
        for sock in sockets:
            try:
                host, port = sock.getsockname()[:2]
                parts.append(f"{host}:{port}")
            except Exception:
                continue
        return ",".join(parts) or "unknown"


__all__ = ["TelnetListenerAdapter"]
