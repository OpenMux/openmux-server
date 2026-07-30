"""Telnet Listener Adapter.

Expose existing OpenMux ports over simple Telnet-compatible TCP sockets. Each
listener entry binds to a host/port pair and attaches clients directly to a
configured OpenMux port (local or federated). Access controls rely on per-
listener ACLs and optional read-only enforcement (client input dropped).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from openmux import __version__ as _OPENMUX_VERSION

from .base_adapter import AdapterCapability, BaseGenericAdapter
from .listener_common import (
    AclEntry,
    CONTROL_MENU_HELP,
    EscapeState,
    compile_acl,
    feed_escape_byte,
    format_rw_notice,
    ip_allowed,
    parse_login,
    render_port_list,
)
from .protocols.plain import TelnetIacStripper

# Sentinel 'target' value that puts a listener into interactive port-menu mode.
_MENU_TARGET = "*"

# Auth/menu phase tuning (only applies before a port is attached; the
# post-attach data pump has no timeout so connected sessions stay open).
_AUTH_LOGIN_MAX_ATTEMPTS = 3
_AUTH_PORT_MAX_ATTEMPTS = 5
_AUTH_IDLE_TIMEOUT = 60.0
_AUTH_LINE_MAX_LEN = 256
_IAC_WILL_ECHO = bytes([255, 251, 1])  # IAC WILL ECHO
_IAC_WONT_ECHO = bytes([255, 252, 1])  # IAC WONT ECHO


@dataclass
class ListenerConfig:
    name: str
    bind_host: str
    bind_port: int
    target: str
    read_only: bool = False
    acl_raw: List[str] = field(default_factory=list)
    enabled: bool = True
    require_auth: bool = False
    compiled_acl: List[AclEntry] = field(default_factory=list)
    effective_host: Optional[str] = None
    effective_port: Optional[int] = None


@dataclass
class TelnetSession:
    client_id: str
    listener: ListenerConfig
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    port_name: str
    read_only: bool
    remote_host: str
    username: str = ""
    port_mode: str = "read-only"
    bytes_in: int = 0
    bytes_out: int = 0
    task: Optional[asyncio.Task] = None
    escape: EscapeState = field(default_factory=EscapeState)


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
        self.sessions: Dict[str, TelnetSession] = {}
        self.console_manager = None
        self.auth_manager = None

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
        seen_names = set()
        for entry in entries:
            if not isinstance(entry, dict):
                return False
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                return False
            if name in seen_names:
                return False
            seen_names.add(name)
            target = entry.get("target")
            if not isinstance(target, str) or not target.strip():
                return False
            bind_port = entry.get("bind_port")
            try:
                port = int(bind_port)
            except (TypeError, ValueError):
                return False
            if port < 1 or port > 65535:
                return False
            bind_host = entry.get("bind_host")
            if bind_host is not None and (not isinstance(bind_host, str) or not bind_host.strip()):
                return False
            if "read_only" in entry and not isinstance(entry.get("read_only"), bool):
                return False
            if "enabled" in entry and not isinstance(entry.get("enabled"), bool):
                return False
            if "require_auth" in entry and not isinstance(entry.get("require_auth"), bool):
                return False
            if "acl" in entry:
                acl = entry.get("acl")
                if acl is not None and not isinstance(acl, list):
                    return False
                if isinstance(acl, list):
                    for rule in acl:
                        if not isinstance(rule, str) or not rule.strip():
                            return False
                        try:
                            if "/" in rule:
                                ipaddress.ip_network(rule, strict=False)
                            else:
                                ipaddress.ip_address(rule)
                        except ValueError:
                            return False
        return True

    async def start(self) -> bool:
        if not self.listeners:
            self.logger.info("Telnet listener adapter has no entries; nothing to bind")
            self.is_running = True
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
        self.is_running = success
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
            self.logger.info(
                "Telnet listener '%s' bound to %s", spec.name, self._format_sockname(server.sockets)
            )
            try:
                sock = server.sockets[0]
                if sock:
                    sockname = sock.getsockname()
                    spec.effective_host = sockname[0]
                    spec.effective_port = sockname[1]
            except Exception:
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
        for client_id in [cid for cid, s in list(self.sessions.items()) if s.listener.name == name]:
            await self._disconnect_session(client_id, reason="listener removed")
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
        self.is_running = True

        summary = {"added": added, "removed": removed, "updated": updated, "unchanged": unchanged}
        self.logger.info(
            f"Telnet listener adapter {self.name} reconcile: +{len(added)} ~{len(updated)} -{len(removed)} unchanged={len(unchanged)}"
        )
        return summary

    async def stop(self) -> None:
        self.is_running = False
        # Close sessions
        for client_id in list(self.sessions.keys()):
            await self._disconnect_session(client_id, reason="adapter stop")
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

    def set_auth_manager(self, auth_manager):
        """Wire the shared AuthManager for listeners with require_auth enabled."""
        self.auth_manager = auth_manager

    async def send_data_to_client(self, client_id: str, data: bytes) -> bool:
        session = self.sessions.get(client_id)
        if not session:
            return False
        try:
            session.writer.write(data)
            await session.writer.drain()
            session.bytes_out += len(data)
            return True
        except Exception as exc:
            self.logger.debug("Failed to send data to telnet client %s: %s", client_id, exc)
            await self._disconnect_session(client_id, reason="write failure")
            return False

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
        client_id = f"telnet:{listener.name}:{uuid.uuid4()}"
        session = TelnetSession(
            client_id=client_id,
            listener=listener,
            reader=reader,
            writer=writer,
            port_name=port_name,
            read_only=listener.read_only,
            remote_host=peer_ip,
            username=username,
        )
        attach_ok, attach_reason = await self._attach_session(session)
        if not attach_ok:
            if attach_reason == "denied_by_group_acl":
                await self._send_and_close(writer, b"Access denied to this port\r\n")
            elif attach_reason == "no_permissions":
                await self._send_and_close(writer, b"No permissions assigned to this account\r\n")
            else:
                await self._send_and_close(writer, b"Failed to attach to port\r\n")
            return
        self.sessions[client_id] = session
        self.logger.info(
            "Telnet client %s connected (listener=%s, port=%s, ro=%s)",
            client_id,
            listener.name,
            port_name,
            session.read_only,
        )
        if session.listener.read_only or session.port_mode != "read-write":
            await self._write_session(session, "\r\n[WARNING: console is in read-only mode]\r\n")
        try:
            await self._pump_client_input(session)
        finally:
            await self._disconnect_session(client_id, reason="disconnect")

    async def _pump_client_input(self, session: TelnetSession) -> None:
        reader = session.reader
        while True:
            try:
                data = await reader.read(4096)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.debug("Read error for %s: %s", session.client_id, exc)
                break
            if not data:
                break
            session.bytes_in += len(data)
            if await self._process_input_chunk(session, data):
                return

    async def _process_input_chunk(self, session: TelnetSession, data: bytes) -> bool:
        """Scan a chunk for the control-menu escape sequence, forwarding the rest.

        Returns:
            bool: True if the '.' command requested disconnect (or a forward
            failed); the caller should stop pumping.
        """
        i = 0
        payload = bytearray()
        while i < len(data):
            extra, cmd = feed_escape_byte(session.escape, data[i : i + 1])
            payload.extend(extra)
            i += 1
            if not cmd:
                continue
            if payload:
                if not await self._forward_payload(session, bytes(payload)):
                    return True
                payload.clear()
            if cmd == "e":
                data, i = await self._change_escape_sequence(session, data, i)
                continue
            if await self._handle_control_command(session, cmd):
                return True
        if payload and not await self._forward_payload(session, bytes(payload)):
            return True
        return False

    async def _forward_payload(self, session: TelnetSession, payload: bytes) -> bool:
        """Write `payload` to the attached port, respecting the read-only gate.

        Returns False only on an actual write failure (session should close);
        a read-only gate silently drops data and still returns True.
        """
        if session.listener.read_only or session.port_mode != "read-write":
            if b"\r" in payload or b"\n" in payload:
                # Re-announce on every Enter press; the one-time attach warning is easy to miss.
                await self._write_session(session, "\r\n[WARNING: console is in read-only mode]\r\n")
            return True
        try:
            if self.console_manager:
                await self.console_manager.write_to_port(session.port_name, payload, session.client_id)
            return True
        except Exception as exc:
            self.logger.warning(
                "Failed forwarding data from %s to port %s: %s", session.client_id, session.port_name, exc
            )
            return False

    async def _change_escape_sequence(self, session: TelnetSession, data: bytes, i: int) -> Tuple[bytes, int]:
        """Consume the two bytes following an 'e' command as the new escape sequence.

        Takes them from the remaining buffered `data` when available;
        otherwise reads the remainder directly from the session's transport.

        Returns:
            Tuple[bytes, int]: the (possibly replaced) buffer and index to
            resume scanning from.
        """
        remaining = data[i:]
        if len(remaining) < 2:
            try:
                remaining += await session.reader.readexactly(2 - len(remaining))
            except Exception:
                pass
        if len(remaining) < 2:
            return b"", 0
        session.escape.escape_char1 = remaining[0:1]
        session.escape.escape_char2 = remaining[1:2]
        await self._write_session(session, "\r\n[Escape sequence changed]\r\n")
        return remaining[2:], 0

    async def _handle_control_command(self, session: TelnetSession, cmd: str) -> bool:
        """Execute one control-menu command. Returns True to request disconnect."""
        cm = self.console_manager
        if cmd == "a":
            await self._cmd_request_rw(session, cm)
        elif cmd == "f":
            await self._cmd_force_rw(session, cm)
        elif cmd == "s":
            await self._cmd_release_rw(session, cm)
        elif cmd == "w":
            holders = cm.get_rw_holders_display(session.port_name) if cm else []
            await self._write_session(session, format_rw_notice({"type": "rw_holders", "holders": holders}))
        elif cmd == "?":
            await self._write_session(session, CONTROL_MENU_HELP)
        elif cmd == "i":
            await self._write_session(session, self._format_session_info(session))
        elif cmd == "v":
            await self._write_session(session, f"\r\n[OpenMux Server v{_OPENMUX_VERSION}]\r\n")
        elif cmd == ".":
            await self._write_session(session, "\r\n[Disconnecting...]\r\n")
            return True
        return False

    async def _cmd_request_rw(self, session: TelnetSession, cm: Any) -> None:
        if session.listener.read_only:
            await self._write_session(session, "\r\n[This listener is configured read-only]\r\n")
            return
        ok = bool(cm and await cm.promote_client_to_read_write(session.client_id, session.port_name))
        if ok:
            session.port_mode = "read-write"
        payload: Dict[str, Any] = {"type": "client_mode", "ok": ok, "mode": session.port_mode}
        if not ok and cm:
            holders = cm.get_rw_holders_display(session.port_name)
            if holders:
                payload["rw_holders"] = holders
        await self._write_session(session, format_rw_notice(payload))

    async def _cmd_force_rw(self, session: TelnetSession, cm: Any) -> None:
        if session.listener.read_only:
            await self._write_session(session, "\r\n[This listener is configured read-only]\r\n")
            return
        ok = False
        if cm:
            ok, _undelivered = await cm.force_promote_client(session.client_id, session.port_name)
        if ok:
            session.port_mode = "read-write"
        await self._write_session(session, format_rw_notice({"type": "client_mode", "ok": ok, "mode": session.port_mode}))

    async def _cmd_release_rw(self, session: TelnetSession, cm: Any) -> None:
        ok = bool(cm and await cm.demote_client_to_read_only(session.client_id, session.port_name))
        if ok:
            session.port_mode = "read-only"
        await self._write_session(session, format_rw_notice({"type": "client_mode", "ok": ok, "mode": session.port_mode}))

    def _format_session_info(self, session: TelnetSession) -> str:
        esc = session.escape
        esc_display = f"{esc.escape_char1!r}+{esc.escape_char2!r}"
        return (
            "\r\n--- Session Info ---\r\n"
            f"Port: {session.port_name}\r\n"
            f"Mode: {session.port_mode}\r\n"
            f"Remote host: {session.remote_host}\r\n"
            f"Escape sequence: {esc_display}\r\n"
        )

    async def _write_session(self, session: TelnetSession, text: str) -> None:
        try:
            session.writer.write(text.encode())
            await session.writer.drain()
            session.bytes_out += len(text)
        except Exception:
            pass

    async def send_control_frame_to_client(self, client_id: str, payload: Dict[str, Any]) -> bool:
        """Deliver a cross-adapter access-mode notice as human text.

        Telnet clients are raw terminals and cannot parse JSON control
        frames, so this renders `payload` via `format_rw_notice` instead of
        the JSON/OMXCTRL framing used by the TCP/WebSocket adapters.
        """
        session = self.sessions.get(client_id)
        if session is None:
            return False
        if payload.get("reason") == "demoted":
            session.port_mode = "read-only"
        await self._write_session(session, format_rw_notice(payload))
        return True

    def _resolve_client_meta(self, client_id: str) -> Dict[str, Any]:
        """Return `{"type", "ip", "username"}` for a telnet session (used by
        `ConsoleManager.get_rw_holders_display` for cross-adapter IP lookup)."""
        session = self.sessions.get(client_id)
        if session is None:
            return {}
        return {"type": "telnet", "ip": session.remote_host, "username": session.username}

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
            port_name = await self._resolve_target(embedded_descriptor)
            if not port_name:
                await self._send_and_close(writer, f"Port {embedded_descriptor} unavailable\r\n".encode())
                return None
            return username, port_name

        if listener.target == _MENU_TARGET:
            port_name = await self._run_port_menu(writer, reader)
            if not port_name:
                return None
            return username, port_name

        port_name = await self._resolve_target(listener.target)
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
            self.logger.error(
                "Telnet listener '%s' has require_auth set but no auth manager is configured", listener.name
            )
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
        await self._send_port_list(writer)
        for _ in range(_AUTH_PORT_MAX_ATTEMPTS):
            writer.write(b"Port: ")
            await writer.drain()
            choice = await self._read_auth_line(reader, stripper)
            if choice is None:
                await self._close_quiet(writer)
                return None
            choice = choice.strip()
            if not choice or choice in ("?", "list"):
                await self._send_port_list(writer)
                continue
            if choice in ("quit", "exit"):
                await self._close_quiet(writer)
                return None
            port_name = await self._resolve_target(choice)
            if port_name:
                return port_name
            writer.write(f"Unknown port: {choice}\r\n".encode())
            await writer.drain()

        await self._close_quiet(writer)
        return None

    async def _send_port_list(self, writer: asyncio.StreamWriter) -> None:
        entries: List[Dict[str, Any]] = []
        try:
            getter = getattr(self.main_port_manager, "get_port_list_with_federation", None)
            if getter:
                entries = await asyncio.wait_for(getter(), timeout=1.0)
        except Exception:
            entries = []
        writer.write(render_port_list(entries))
        await writer.drain()

    async def _close_quiet(self, writer: asyncio.StreamWriter) -> None:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    async def _attach_session(self, session: TelnetSession) -> Tuple[bool, Optional[str]]:
        if not self.console_manager:
            return False, None
        try:
            ok, mode, reason = await self.console_manager.connect_client_to_port(
                session.client_id,
                session.port_name,
                session.username,
            )
        except Exception as exc:
            self.logger.error("Console manager attach failed for %s: %s", session.client_id, exc)
            return False, None
        if ok:
            session.port_mode = mode
        else:
            self.logger.warning(
                "Console manager rejected telnet client %s for port %s (reason=%s)",
                session.client_id,
                session.port_name,
                reason,
            )
        if ok and hasattr(self.console_manager, "register_client_channel"):
            try:
                self.console_manager.register_client_channel(session.client_id, self)
            except Exception:
                pass
        return bool(ok), reason

    async def _disconnect_session(self, client_id: str, *, reason: str) -> None:
        session = self.sessions.pop(client_id, None)
        if not session:
            return
        try:
            if self.console_manager:
                try:
                    if hasattr(self.console_manager, "unregister_client_channel"):
                        self.console_manager.unregister_client_channel(client_id)
                except Exception:
                    pass
                await self.console_manager.disconnect_client_from_port(client_id, session.port_name)
        except Exception:
            self.logger.debug("Failed disconnecting client %s from port", client_id, exc_info=True)
        try:
            writer = session.writer
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        self.logger.info(
            "Telnet client %s closed (%s) in=%d out=%d",
            client_id,
            reason,
            session.bytes_in,
            session.bytes_out,
        )

    # ------------------------------------------------------------------
    # Helpers

    def _build_listener(self, entry: Dict[str, Any]) -> Optional[ListenerConfig]:
        if not isinstance(entry, dict):
            return None
        try:
            bind_host = entry.get("bind_host", "0.0.0.0")
            if not isinstance(bind_host, str) or not bind_host.strip():
                bind_host = "0.0.0.0"
            bind_port = int(entry.get("bind_port"))
            spec = ListenerConfig(
                name=str(entry.get("name")),
                bind_host=bind_host,
                bind_port=bind_port,
                target=str(entry.get("target")),
                read_only=bool(entry.get("read_only", False)),
                acl_raw=[str(a).strip() for a in entry.get("acl", []) if str(a).strip()],
                enabled=bool(entry.get("enabled", True)),
                require_auth=bool(entry.get("require_auth", False)),
            )
            if not spec.name:
                return None
            if not spec.target:
                return None
            if spec.target == _MENU_TARGET and not spec.require_auth:
                self.logger.warning(
                    "Telnet listener '%s' uses menu mode (target: '*') without require_auth; "
                    "any client can browse and attach to any port",
                    spec.name,
                )
            compiled = compile_acl(
                spec.acl_raw,
                on_invalid=lambda rule: self.logger.warning("Ignoring invalid ACL '%s' for listener %s", rule, spec.name),
            )
            spec.compiled_acl = compiled
            return spec
        except Exception:
            self.logger.error("Invalid telnet listener entry: %s", entry, exc_info=True)
            return None

    def _client_allowed(self, listener: ListenerConfig, peer_ip: str) -> bool:
        return ip_allowed(peer_ip, listener.compiled_acl)

    async def _resolve_target(self, descriptor: str) -> Optional[str]:
        if not self.main_port_manager:
            return None
        descriptor = descriptor.strip()
        if not descriptor:
            return None
        if "::" in descriptor:
            prefix, base = descriptor.split("::", 1)
            if prefix.lower() == "local":
                return base if self._is_local_port(base) else None
            return await self._resolve_remote_by_origin(base, prefix)
        if self._is_local_port(descriptor):
            return descriptor
        return await self._resolve_remote_by_origin(descriptor, None)

    def _is_local_port(self, port_name: str) -> bool:
        try:
            port = self.main_port_manager.get_port(port_name)
        except Exception:
            port = None
        if port is None:
            return False
        metadata = getattr(port, "metadata", None)
        if metadata is not None:
            origin = getattr(metadata, "origin_server", None)
            if origin is not None and getattr(origin, "server_id", None):
                return False
        return True

    async def _resolve_remote_by_origin(self, port_name: str, server_id: Optional[str]) -> Optional[str]:
        entries = []
        try:
            getter = getattr(self.main_port_manager, "get_port_list_with_federation", None)
            if getter:
                entries = await asyncio.wait_for(getter(), timeout=1.0)
        except Exception:
            entries = []
        matches = []
        for entry in entries or []:
            if entry.get("name") != port_name:
                continue
            origin = entry.get("origin_server_id")
            if server_id is None:
                if origin:
                    matches.append(entry)
                continue
            if server_id.lower() in {"local", "localhost"}:
                if origin:
                    continue
                matches.append(entry)
            elif origin == server_id:
                matches.append(entry)
        if len(matches) == 1:
            return matches[0].get("name")
        return None

    def _peer_ip(self, writer: asyncio.StreamWriter) -> str:
        try:
            peer = writer.get_extra_info("peername")
            if isinstance(peer, tuple) and peer:
                return peer[0]
            if isinstance(peer, str):
                return peer
        except Exception:
            pass
        return "unknown"

    async def _send_and_close(self, writer: asyncio.StreamWriter, payload: bytes) -> None:
        try:
            writer.write(payload)
            await writer.drain()
        except Exception:
            pass
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
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
