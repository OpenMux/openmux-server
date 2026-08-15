"""SSH Listener Adapter.

Expose existing OpenMux ports over real SSH connections (via `asyncssh`). Each
listener entry binds to a host/port pair and, after authentication, attaches
the client to a configured OpenMux port (local or federated) or an
interactive port-selection menu (`target: '*'`). Sessions are raw
pass-through only: `exec`/subsystem requests are rejected, only an
interactive "shell" request is honored.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import asyncssh
from cryptography.hazmat.primitives import serialization

from openmux import __version__ as _OPENMUX_VERSION

from .base_adapter import AdapterCapability, BaseGenericAdapter
from .listener_common import (
    AclEntry,
    CONTROL_MENU_HELP,
    EscapeState,
    compile_acl,
    feed_escape_byte,
    format_rw_notice,
    format_viewers_notice,
    ip_allowed,
    parse_login,
    render_port_list,
)

# Sentinel 'target' value that puts a listener into interactive port-menu mode.
_MENU_TARGET = "*"

# Menu-phase tuning (only applies to the port-selection prompt, after SSH auth
# has already completed at the protocol level; the post-attach data pump has
# no timeout so connected sessions stay open indefinitely).
_MENU_MAX_ATTEMPTS = 5
_MENU_IDLE_TIMEOUT = 60.0

# Per-connection PASSWORD attempt cap (auth_manager's own lockout is the real
# security control; this just mirrors telnet_listener's behavior of
# disconnecting a misbehaving client instead of waiting for the client to
# give up on its own). Public-key offers are excluded: an SSH client
# routinely probes several identities (often twice each - once unsigned to
# query, once signed) before ever reaching a password prompt, so counting
# those against the same budget could disconnect - or even account-lock via
# auth_manager - a legitimate user before they type a single password.
_AUTH_MAX_ATTEMPTS = 3

# Host key is shared by all ssh_listener entries in this adapter instance and
# auto-generated on first start, mirroring web_console's TLS cert autogen.
_HOST_KEY_DIR = os.path.expanduser("~/.openmux/ssh_listener")
_HOST_KEY_PATH = os.path.join(_HOST_KEY_DIR, "ssh_host_key")


@dataclass
class ListenerConfig:
    name: str
    bind_host: str
    bind_port: int
    target: str
    read_only: bool = False
    acl_raw: List[str] = field(default_factory=list)
    enabled: bool = True
    require_auth: bool = True
    compiled_acl: List[AclEntry] = field(default_factory=list)
    effective_host: Optional[str] = None
    effective_port: Optional[int] = None


@dataclass
class SshSession:
    client_id: str
    listener: ListenerConfig
    process: Any
    port_name: str
    read_only: bool
    remote_host: str
    username: str
    port_mode: str = "read-only"
    bytes_in: int = 0
    bytes_out: int = 0
    escape: EscapeState = field(default_factory=EscapeState)


def _match_ssh_pubkey(auth_manager: Any, username: str, presented_key: "asyncssh.SSHKey") -> bool:
    """Compare a presented SSH public key against a user's registered keys.

    Both sides are normalized to OpenSSH text (`ssh-ed25519 <base64>`) so the
    comparison doesn't depend on which library produced the encoding.
    """
    try:
        presented_prefix = " ".join(presented_key.export_public_key("openssh").decode().split()[:2])
    except Exception:
        return False
    try:
        candidates = auth_manager.get_ed25519_pubkeys_for_user_and_use(username, "ssh")
    except Exception:
        return False
    for pub in candidates.values():
        try:
            stored = pub.public_bytes(
                encoding=serialization.Encoding.OpenSSH,
                format=serialization.PublicFormat.OpenSSH,
            ).decode()
            stored_prefix = " ".join(stored.split()[:2])
        except Exception:
            continue
        if stored_prefix == presented_prefix:
            return True
    return False


class _OpenMuxSshServer(asyncssh.SSHServer):
    """Per-connection auth callback bridge into AuthManager and listener ACL."""

    def __init__(self, adapter: "SshListenerAdapter", listener: ListenerConfig):
        self._adapter = adapter
        self._listener = listener
        self._conn: Optional[Any] = None
        self._peer_ip = "unknown"
        self._pending_username = ""
        self._embedded_descriptor: Optional[str] = None
        self._pw_attempts = 0

    def connection_made(self, conn) -> None:
        self._conn = conn
        peer = conn.get_extra_info("peername")
        self._peer_ip = peer[0] if isinstance(peer, tuple) and peer else "unknown"
        if not ip_allowed(self._peer_ip, self._listener.compiled_acl):
            self._adapter.logger.info("SSH listener '%s' denied %s (ACL)", self._listener.name, self._peer_ip)
            conn.close()

    def begin_auth(self, username: str) -> bool:
        real_username, embedded = parse_login(username)
        self._pending_username = real_username
        self._embedded_descriptor = embedded
        if not self._listener.require_auth:
            if self._conn is not None:
                self._conn.set_extra_info(
                    openmux_username=f"ssh_{self._listener.name}",
                    openmux_embedded_descriptor=embedded,
                )
            return False
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        auth_manager = self._adapter.auth_manager
        if not auth_manager or self._auth_blocked():
            return False
        self._pw_attempts += 1
        try:
            ok = bool(auth_manager.authenticate_user(self._pending_username, password))
        except Exception:
            ok = False
        if ok:
            self._on_auth_success(auth_manager)
        else:
            self._on_password_failure(auth_manager)
        return ok

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: "asyncssh.SSHKey") -> bool:
        # Not gated by the password attempt cap: a single client typically
        # offers several identities (and each may be probed twice, once
        # unsigned then once signed), which is routine and not a failed
        # login attempt - see the _AUTH_MAX_ATTEMPTS comment above.
        auth_manager = self._adapter.auth_manager
        if not auth_manager or self._is_locked(auth_manager):
            return False
        ok = _match_ssh_pubkey(auth_manager, self._pending_username, key)
        if ok:
            self._on_auth_success(auth_manager)
        return ok

    def _is_locked(self, auth_manager: Any) -> bool:
        try:
            return bool(auth_manager.is_user_locked(self._pending_username, self._peer_ip))
        except Exception:
            return False

    def _auth_blocked(self) -> bool:
        if self._pw_attempts >= _AUTH_MAX_ATTEMPTS:
            return True
        return self._is_locked(self._adapter.auth_manager)

    def _on_auth_success(self, auth_manager: Any) -> None:
        if hasattr(auth_manager, "clear_auth_failures"):
            try:
                auth_manager.clear_auth_failures(self._pending_username, self._peer_ip)
            except Exception:
                pass
        if self._conn is not None:
            self._conn.set_extra_info(
                openmux_username=self._pending_username,
                openmux_embedded_descriptor=self._embedded_descriptor,
            )

    def _on_password_failure(self, auth_manager: Any) -> None:
        if hasattr(auth_manager, "register_auth_failure"):
            try:
                auth_manager.register_auth_failure(self._pending_username, self._peer_ip)
            except Exception:
                pass
        if self._pw_attempts >= _AUTH_MAX_ATTEMPTS and self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass


class SshListenerAdapter(BaseGenericAdapter):
    """Adapter exposing OpenMux ports via SSH sessions."""

    def get_adapter_type(self) -> str:
        """Return adapter type for security policy and factory lookup."""
        return "ssh_listener"

    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        self.logger = logging.getLogger(f"openmux.adapter.ssh_listener.{name}")
        raw_entries = config.get("ssh_listener") if isinstance(config, dict) else []
        self.listeners: List[ListenerConfig] = []
        if isinstance(raw_entries, list):
            for entry in raw_entries:
                spec = self._build_listener(entry)
                if spec:
                    self.listeners.append(spec)
        self.servers: Dict[str, Any] = {}
        self.sessions: Dict[str, SshSession] = {}
        self.console_manager = None
        self.auth_manager = None
        self._host_key: Optional[Any] = None

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
        entries = config.get("ssh_listener")
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
            self.logger.info("SSH listener adapter has no entries; nothing to bind")
            self.is_running = True
            return True
        if not self.console_manager:
            self.logger.error("SSH listener adapter requires a console manager reference")
            return False
        try:
            self._host_key = self._ensure_host_key()
        except Exception as exc:
            self.logger.error("Failed to prepare SSH host key: %s", exc, exc_info=True)
            return False
        success = True
        for spec in self.listeners:
            if not spec.enabled:
                self.logger.info("SSH listener '%s' disabled via configuration", spec.name)
                continue
            if not await self._start_single_listener(spec):
                success = False
        self.is_running = success
        return success

    def _ensure_host_key(self) -> Any:
        """Load the shared SSH host key, generating+persisting one if missing."""
        os.makedirs(_HOST_KEY_DIR, exist_ok=True)
        if os.path.exists(_HOST_KEY_PATH):
            return asyncssh.read_private_key(_HOST_KEY_PATH)
        key = asyncssh.generate_private_key("ssh-ed25519")
        key.write_private_key(_HOST_KEY_PATH)
        try:
            os.chmod(_HOST_KEY_PATH, 0o600)
        except OSError:
            pass
        self.logger.info("Generated new SSH host key at %s", _HOST_KEY_PATH)
        return key

    async def _start_single_listener(self, spec: ListenerConfig) -> bool:
        """Bind a single listener's SSH server. Returns True on success."""
        try:

            def _server_factory(listener_spec=spec):
                return _OpenMuxSshServer(self, listener_spec)

            async def _process_entry(process, listener_spec=spec):
                await self._handle_process(listener_spec, process)

            server = await asyncssh.create_server(
                _server_factory,
                spec.bind_host,
                spec.bind_port,
                server_host_keys=[self._host_key],
                process_factory=_process_entry,
                encoding=None,
            )
            self.servers[spec.name] = server
            try:
                spec.effective_host = spec.bind_host
                spec.effective_port = server.get_port()
            except Exception:
                pass
            self.logger.info("SSH listener '%s' bound to %s:%s", spec.name, spec.bind_host, spec.effective_port)
            return True
        except Exception as exc:
            self.logger.error(
                "Failed to start SSH listener '%s' on %s:%s: %s",
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
                self.logger.info("SSH listener '%s' stopped", name)
            except Exception:
                self.logger.warning("Error stopping SSH listener '%s'", name, exc_info=True)

    async def reconcile_ports(self, new_config: Any) -> Dict[str, Any]:
        """Incrementally reconcile SSH listeners without disturbing unrelated sessions."""
        if isinstance(new_config, dict) and isinstance(new_config.get("ssh_listener"), list):
            items = list(new_config["ssh_listener"])
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

        if self._host_key is None:
            try:
                self._host_key = self._ensure_host_key()
            except Exception as exc:
                self.logger.error("Failed to prepare SSH host key during reconcile: %s", exc, exc_info=True)

        specs_by_name = {spec.name: spec for spec in self.listeners if spec.name not in (removed + updated)}
        for name in added + updated:
            spec = self._build_listener(new_by_name[name])
            if spec is None:
                continue
            specs_by_name[name] = spec
            if spec.enabled:
                await self._start_single_listener(spec)
            else:
                self.logger.info("SSH listener '%s' disabled via configuration", spec.name)

        self.listeners = [specs_by_name[n] for n in sorted(specs_by_name.keys())]
        self.is_running = True

        summary = {"added": added, "removed": removed, "updated": updated, "unchanged": unchanged}
        self.logger.info(
            f"SSH listener adapter {self.name} reconcile: +{len(added)} ~{len(updated)} -{len(removed)} unchanged={len(unchanged)}"
        )
        return summary

    async def stop(self) -> None:
        self.is_running = False
        for client_id in list(self.sessions.keys()):
            await self._disconnect_session(client_id, reason="adapter stop")
        for name, server in list(self.servers.items()):
            try:
                server.close()
                await server.wait_closed()
                self.logger.info("SSH listener '%s' stopped", name)
            except Exception:
                self.logger.warning("Error stopping SSH listener '%s'", name, exc_info=True)
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
            self.logger.warning("Failed to register SSH adapter as client manager", exc_info=True)

    def set_auth_manager(self, auth_manager):
        """Wire the shared AuthManager for password/public-key SSH auth."""
        self.auth_manager = auth_manager

    async def send_data_to_client(self, client_id: str, data: bytes) -> bool:
        session = self.sessions.get(client_id)
        if not session:
            return False
        try:
            session.process.stdout.write(data)
            await session.process.stdout.drain()
            session.bytes_out += len(data)
            return True
        except Exception as exc:
            self.logger.debug("Failed to send data to SSH client %s: %s", client_id, exc)
            await self._disconnect_session(client_id, reason="write failure")
            return False

    # ------------------------------------------------------------------
    # Session handling

    async def _handle_process(self, listener: ListenerConfig, process: Any) -> None:
        if process.command or process.subsystem:
            try:
                process.stderr.write(b"Only interactive sessions are supported.\r\n")
                await process.stderr.drain()
            except Exception:
                pass
            process.exit(1)
            return
        if not self.main_port_manager:
            try:
                process.stdout.write(b"Server unavailable\r\n")
                await process.stdout.drain()
            except Exception:
                pass
            process.exit(1)
            return

        username = process.get_extra_info("openmux_username") or f"ssh_{listener.name}"
        embedded_descriptor = process.get_extra_info("openmux_embedded_descriptor")
        peer = process.get_extra_info("peername")
        peer_ip = peer[0] if isinstance(peer, tuple) and peer else "unknown"

        port_name = await self._resolve_process_target(listener, process, embedded_descriptor)
        if not port_name:
            return  # Already messaged + exited.

        client_id = f"ssh:{listener.name}:{uuid.uuid4()}"
        session = SshSession(
            client_id=client_id,
            listener=listener,
            process=process,
            port_name=port_name,
            read_only=listener.read_only,
            remote_host=peer_ip,
            username=username,
        )
        attach_ok, attach_reason = await self._attach_session(session)
        if not attach_ok:
            try:
                if attach_reason == "denied_by_group_acl":
                    process.stdout.write(b"Access denied to this port\r\n")
                elif attach_reason == "no_permissions":
                    process.stdout.write(b"No permissions assigned to this account\r\n")
                else:
                    process.stdout.write(b"Failed to attach to port\r\n")
                await process.stdout.drain()
            except Exception:
                pass
            process.exit(1)
            return
        self.sessions[client_id] = session
        self.logger.info(
            "SSH client %s connected (listener=%s, port=%s, ro=%s)",
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

    async def _pump_client_input(self, session: SshSession) -> None:
        stdin = session.process.stdin
        while True:
            try:
                data = await stdin.read(4096)
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

    async def _process_input_chunk(self, session: SshSession, data: bytes) -> bool:
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

    async def _forward_payload(self, session: SshSession, payload: bytes) -> bool:
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
            self.logger.warning("Failed forwarding data from %s to port %s: %s", session.client_id, session.port_name, exc)
            return False

    async def _change_escape_sequence(self, session: SshSession, data: bytes, i: int) -> Tuple[bytes, int]:
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
                remaining += await session.process.stdin.readexactly(2 - len(remaining))
            except Exception:
                pass
        if len(remaining) < 2:
            return b"", 0
        session.escape.escape_char1 = remaining[0:1]
        session.escape.escape_char2 = remaining[1:2]
        await self._write_session(session, "\r\n[Escape sequence changed]\r\n")
        return remaining[2:], 0

    async def _handle_control_command(self, session: SshSession, cmd: str) -> bool:
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
        elif cmd == "u":
            viewers = cm.get_viewers_display(session.port_name) if cm else []
            await self._write_session(session, format_viewers_notice(viewers))
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

    async def _cmd_request_rw(self, session: SshSession, cm: Any) -> None:
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

    async def _cmd_force_rw(self, session: SshSession, cm: Any) -> None:
        if session.listener.read_only:
            await self._write_session(session, "\r\n[This listener is configured read-only]\r\n")
            return
        ok = False
        if cm:
            ok, _undelivered = await cm.force_promote_client(session.client_id, session.port_name)
        if ok:
            session.port_mode = "read-write"
        await self._write_session(session, format_rw_notice({"type": "client_mode", "ok": ok, "mode": session.port_mode}))

    async def _cmd_release_rw(self, session: SshSession, cm: Any) -> None:
        ok = bool(cm and await cm.demote_client_to_read_only(session.client_id, session.port_name))
        if ok:
            session.port_mode = "read-only"
        await self._write_session(session, format_rw_notice({"type": "client_mode", "ok": ok, "mode": session.port_mode}))

    def _format_session_info(self, session: SshSession) -> str:
        esc = session.escape
        esc_display = f"{esc.escape_char1!r}+{esc.escape_char2!r}"
        return (
            "\r\n--- Session Info ---\r\n"
            f"Port: {session.port_name}\r\n"
            f"Mode: {session.port_mode}\r\n"
            f"Remote host: {session.remote_host}\r\n"
            f"Escape sequence: {esc_display}\r\n"
        )

    async def _write_session(self, session: SshSession, text: str) -> None:
        try:
            session.process.stdout.write(text.encode())
            await session.process.stdout.drain()
            session.bytes_out += len(text)
        except Exception:
            pass

    async def send_control_frame_to_client(self, client_id: str, payload: Dict[str, Any]) -> bool:
        """Deliver a cross-adapter access-mode notice as human text.

        SSH clients are raw terminals and cannot parse JSON control frames,
        so this renders `payload` via `format_rw_notice` instead of the
        JSON/OMXCTRL framing used by the TCP/WebSocket adapters.
        """
        session = self.sessions.get(client_id)
        if session is None:
            return False
        if payload.get("reason") == "demoted":
            session.port_mode = "read-only"
        await self._write_session(session, format_rw_notice(payload))
        return True

    def _resolve_client_meta(self, client_id: str) -> Dict[str, Any]:
        """Return `{"type", "ip", "username"}` for an SSH session (used by
        `ConsoleManager.get_rw_holders_display` for cross-adapter IP lookup)."""
        session = self.sessions.get(client_id)
        if session is None:
            return {}
        return {"type": "ssh", "ip": session.remote_host, "username": session.username}

    # ------------------------------------------------------------------
    # Target resolution / port menu (menu phase only; the pump above has no
    # timeout, so once a port is attached sessions stay open indefinitely).

    async def _resolve_process_target(
        self, listener: ListenerConfig, process: Any, embedded_descriptor: Optional[str]
    ) -> Optional[str]:
        if embedded_descriptor and listener.target == _MENU_TARGET:
            port_name = await self._resolve_target(embedded_descriptor)
            if not port_name:
                await self._send_and_exit(process, f"Port {embedded_descriptor} unavailable\r\n".encode())
                return None
            return port_name

        if listener.target == _MENU_TARGET:
            return await self._run_port_menu(process)

        port_name = await self._resolve_target(listener.target)
        if not port_name:
            await self._send_and_exit(process, f"Port {listener.target} unavailable\r\n".encode())
            return None
        return port_name

    async def _run_port_menu(self, process: Any) -> Optional[str]:
        """Print the port list, prompt for a selection, and resolve it."""
        await self._send_port_list(process)
        for _ in range(_MENU_MAX_ATTEMPTS):
            process.stdout.write(b"Port: ")
            await process.stdout.drain()
            choice = await self._read_ssh_line(process)
            if choice is None:
                process.exit(0)
                return None
            choice = choice.strip()
            if not choice or choice in ("?", "list"):
                await self._send_port_list(process)
                continue
            if choice in ("quit", "exit"):
                process.exit(0)
                return None
            port_name = await self._resolve_target(choice)
            if port_name:
                return port_name
            process.stdout.write(f"Unknown port: {choice}\r\n".encode())
            await process.stdout.drain()

        process.exit(1)
        return None

    async def _read_ssh_line(self, process: Any, timeout: float = _MENU_IDLE_TIMEOUT) -> Optional[str]:
        """Read one line of menu input from the raw SSH channel.

        This connection is opened with `encoding=None` (needed so the
        post-attach data pump can forward raw bytes, including the
        Ctrl+E control-menu escape sequence, byte for byte). That also
        disables asyncssh's own pty line editor, so a real interactive
        client's Enter key (which sends a bare `\r`, not `\n`) is never
        translated to a newline. `process.stdin.readline()` only
        recognizes `\n` and would hang forever waiting for one, so line
        endings are detected by hand instead. Typed input is only
        echoed back (with basic backspace handling) when a pty was
        actually requested; non-interactive/scripted clients (no pty)
        get the previous silent, non-editing behavior.
        """
        echo = process.get_terminal_type() is not None
        buf = bytearray()
        try:
            while True:
                b = await asyncio.wait_for(process.stdin.read(1), timeout=timeout)
                if not b:
                    return None
                if b in (b"\r", b"\n"):
                    if echo:
                        await self._echo_ssh_bytes(process, b"\r\n")
                    break
                if b in (b"\x08", b"\x7f"):  # Backspace / DEL
                    await self._handle_ssh_backspace(process, buf, echo)
                    continue
                buf.extend(b)
                if echo:
                    await self._echo_ssh_bytes(process, b)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            return None
        return buf.decode("utf-8", errors="ignore")

    async def _handle_ssh_backspace(self, process: Any, buf: bytearray, echo: bool) -> None:
        """Erase the last buffered byte for a menu-input backspace/DEL keypress."""
        if not buf:
            return
        buf.pop()
        if echo:
            await self._echo_ssh_bytes(process, b"\x08 \x08")

    async def _echo_ssh_bytes(self, process: Any, data: bytes) -> None:
        """Write raw bytes back to an interactive menu session's terminal."""
        process.stdout.write(data)
        await process.stdout.drain()

    async def _send_port_list(self, process: Any) -> None:
        entries: List[Dict[str, Any]] = []
        try:
            getter = getattr(self.main_port_manager, "get_port_list_with_federation", None)
            if getter:
                entries = await asyncio.wait_for(getter(), timeout=1.0)
        except Exception:
            entries = []
        process.stdout.write(render_port_list(entries))
        await process.stdout.drain()

    async def _send_and_exit(self, process: Any, payload: bytes) -> None:
        try:
            process.stdout.write(payload)
            await process.stdout.drain()
        except Exception:
            pass
        process.exit(1)

    async def _attach_session(self, session: SshSession) -> Tuple[bool, Optional[str]]:
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
                "Console manager rejected SSH client %s for port %s (reason=%s)",
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
            session.process.close()
            await session.process.wait_closed()
        except Exception:
            pass
        self.logger.info(
            "SSH client %s closed (%s) in=%d out=%d",
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
                require_auth=bool(entry.get("require_auth", True)),
            )
            if not spec.name:
                return None
            if not spec.target:
                return None
            if spec.target == _MENU_TARGET and not spec.require_auth:
                self.logger.warning(
                    "SSH listener '%s' uses menu mode (target: '*') without require_auth; "
                    "any client can browse and attach to any port",
                    spec.name,
                )
            spec.compiled_acl = compile_acl(
                spec.acl_raw,
                on_invalid=lambda rule: self.logger.warning("Ignoring invalid ACL '%s' for listener %s", rule, spec.name),
            )
            return spec
        except Exception:
            self.logger.error("Invalid SSH listener entry: %s", entry, exc_info=True)
            return None

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


__all__ = ["SshListenerAdapter"]
