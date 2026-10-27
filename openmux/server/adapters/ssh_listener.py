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
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import asyncssh
from cryptography.hazmat.primitives import serialization

from ..access_decision import Code, Mode, decide
from ..listener_session import MENU_TARGET, ListenerConfig, ListenerSessionManager, build_listener
from ..locations import ssh_host_key_dir as _ssh_host_key_dir_default
from .base_adapter import AdapterCapability, BaseGenericAdapter
from .listener_common import ip_allowed, parse_login, validate_listener_entries

# Sentinel 'target' value that puts a listener into interactive port-menu mode.
_MENU_TARGET = MENU_TARGET

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
# Resolved once at import: the unit sets OPENMUX_STATE_DIR before exec, and the
# dev default is ~/.openmux/ssh_listener. Tests monkeypatch these module attrs.
_HOST_KEY_DIR = _ssh_host_key_dir_default()
_HOST_KEY_PATH = os.path.join(_HOST_KEY_DIR, "ssh_host_key")


class _ProcessSessionTransport:
    """The SessionTransport seam over an asyncssh process (issue #100).

    ``stdin``/``stdout`` are the raw (``encoding=None``) asyncssh streams, so
    the session module forwards bytes byte for byte, exactly as the old pump
    did - including the Control menu escape sequence over a pty.
    """

    def __init__(self, process: Any):
        self._process = process

    async def read(self, n: int) -> bytes:
        return await self._process.stdin.read(n)

    async def readexactly(self, n: int) -> bytes:
        data = await self._process.stdin.read(n)
        if len(data) < n:
            raise asyncio.IncompleteReadError(data, n)
        return data

    def write(self, data: bytes) -> None:
        self._process.stdout.write(data)

    async def drain(self) -> None:
        await self._process.stdout.drain()

    async def close(self) -> None:
        self._process.close()
        await self._process.wait_closed()


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
                # justification: optional rate-limit housekeeping; the session proceeds
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
                # justification: optional rate-limit bookkeeping; the login flow continues
                pass
        if self._pw_attempts >= _AUTH_MAX_ATTEMPTS and self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                # justification: shutdown cleanup; the attempt limit already fired
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
        self.console_manager = None
        self.auth_manager = None
        self._host_key: Optional[Any] = None
        # Everything from attach to detach lives in the listener session module
        # (issue #100); this adapter keeps only the SSH transport.
        # `channel_owner=self` keeps the ConsoleManager channel contract on this
        # adapter's pinned delegation names.
        self._sessions = ListenerSessionManager(
            display_name="SSH",
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
        entries = config.get("ssh_listener")
        if entries is None:
            return True
        if not isinstance(entries, list):
            return False
        return validate_listener_entries(entries, set())

    async def _do_start(self) -> bool:
        if not self.listeners:
            self.logger.info("SSH listener adapter has no entries; nothing to bind")
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
            # justification: best-effort permission tightening; the key file stays usable
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
                # justification: optional metadata; the bound value is authoritative
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
        await self._sessions.drop_listener_sessions(name, reason="listener removed")
        server = self.servers.pop(name, None)
        if server is not None:
            try:
                server.close()
                await server.wait_closed()
                self.logger.info("SSH listener '%s' stopped", name)
            except Exception:
                self.logger.warning("Error stopping SSH listener '%s'", name, exc_info=True)

    def _listener_material_diff(
        self, name: str, old_by_name: Dict[str, ListenerConfig], new_by_name: Dict[str, Dict[str, Any]]
    ) -> Tuple[Optional[str], bool, bool]:
        """Classify one common listener as (name, material_changed, enabled).

        Rebuilds the listener spec from its new config entry and compares the
        material fields (bind/host/target/rw/acl/enabled/auth) against the live
        spec. Returns (None, ...) when the entry fails to build so the caller
        skips it, else (name, True when any material field differs).
        """
        new_spec = self._build_listener(new_by_name[name])
        if new_spec is None:
            return None, False, False
        material_changed = self._material_cfg(old_by_name[name]) != self._material_cfg(new_spec)
        return name, material_changed, bool(new_spec.enabled)

    def _material_cfg(self, spec: ListenerConfig) -> Dict[str, Any]:
        """Material (restart-triggering) fields of a listener spec, as a dict."""
        return {
            "bind_host": spec.bind_host,
            "bind_port": spec.bind_port,
            "target": spec.target,
            "read_only": spec.read_only,
            "acl_raw": list(spec.acl_raw),
            "enabled": spec.enabled,
            "require_auth": spec.require_auth,
        }

    def _listeners_from_config(self, new_config: Any) -> Dict[str, Dict[str, Any]]:
        """Normalize a reconcile config into ``{name: raw entry}``.

        Accepts the wrapped ``{"ssh_listener": [...]}`` form, a bare list, or
        anything else (treated as empty). Entries without a usable name key
        are skipped silently.
        """
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
        return new_by_name

    async def _restart_added_listeners(
        self,
        specs_by_name: Dict[str, ListenerConfig],
        to_start: List[str],
        new_by_name: Dict[str, Dict[str, Any]],
    ) -> None:
        """Build + start listeners for the given added/updated names."""
        for name in to_start:
            spec = self._build_listener(new_by_name[name])
            if spec is None:
                continue
            specs_by_name[name] = spec
            if spec.enabled:
                await self._start_single_listener(spec)
            else:
                self.logger.info("SSH listener '%s' disabled via configuration", spec.name)

    async def reconcile_ports(self, new_config: Any) -> Dict[str, Any]:
        """Incrementally reconcile SSH listeners without disturbing unrelated sessions."""
        new_by_name = self._listeners_from_config(new_config)

        old_by_name = {spec.name: spec for spec in self.listeners}
        old_names = set(old_by_name.keys())
        new_names = set(new_by_name.keys())
        removed = sorted(old_names - new_names)
        added = sorted(new_names - old_names)
        common = sorted(old_names & new_names)

        updated: List[str] = []
        unchanged: List[str] = []
        for name in common:
            name, material, _enabled = self._listener_material_diff(name, old_by_name, new_by_name)
            if name is None:
                continue
            if material:
                updated.append(name)
            else:
                unchanged.append(name)

        for name in removed + updated:
            await self._stop_single_listener(name)

        if self._host_key is None:
            try:
                self._host_key = self._ensure_host_key()
            except Exception as exc:
                self.logger.error("Failed to prepare SSH host key during reconcile: %s", exc, exc_info=True)

        specs_by_name = {spec.name: spec for spec in self.listeners if spec.name not in (removed + updated)}
        await self._restart_added_listeners(specs_by_name, added + updated, new_by_name)

        self.listeners = [specs_by_name[n] for n in sorted(specs_by_name.keys())]
        self._set_running(True)

        summary = {"added": added, "removed": removed, "updated": updated, "unchanged": unchanged}
        self.logger.info(
            "SSH listener adapter %s reconcile: +%s ~%s -%s unchanged=%s",
            self.name,
            len(added),
            len(updated),
            len(removed),
            len(unchanged),
        )
        return summary

    async def stop(self) -> None:
        self.is_running = False
        await self._sessions.disconnect_all(reason="adapter stop")
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
        # Subscribe to port meta updates so attached SSH sessions get the live
        # power notice when a feed changes (mirrors the client listener).
        try:
            pm = getattr(console_manager, "port_manager", None)
            if pm and hasattr(pm, "register_meta_listener"):
                pm.register_meta_listener(self._on_port_meta_update)
        except Exception:
            # justification: optional meta subscription; power control still works without the notice
            pass

    def set_auth_manager(self, auth_manager):
        """Wire the shared AuthManager for password/public-key SSH auth."""
        self.auth_manager = auth_manager

    def _on_port_meta_update(self, port_name: str, changes: Optional[Dict[str, Any]] = None) -> None:
        """Live power notice for attached sessions; the session module owns the push."""
        self._sessions.on_port_meta(port_name, changes)

    async def send_data_to_client(self, client_id: str, data: bytes) -> bool:
        """ConsoleManager cross-adapter channel entry point; delegates to the session module."""
        return await self._sessions.send_data(client_id, data)

    # ------------------------------------------------------------------
    # Session handling

    async def _handle_process(self, listener: ListenerConfig, process: Any) -> None:
        if process.command or process.subsystem:
            await self._write_exit_notice(process.stderr, b"Only interactive sessions are supported.\r\n")
            process.exit(1)
            return
        if not self.main_port_manager:
            await self._write_exit_notice(process.stdout, b"Server unavailable\r\n")
            process.exit(1)
            return

        username = process.get_extra_info("openmux_username") or f"ssh_{listener.name}"
        embedded_descriptor = process.get_extra_info("openmux_embedded_descriptor")
        peer = process.get_extra_info("peername")
        peer_ip = peer[0] if isinstance(peer, tuple) and peer else "unknown"

        port_name = await self._resolve_process_target(listener, process, embedded_descriptor)
        if not port_name:
            return  # Already messaged + exited.

        session = self._sessions.create_session(
            listener,
            _ProcessSessionTransport(process),
            port_name=port_name,
            peer_ip=peer_ip,
            username=username,
        )
        attached = await self._sessions.attach(session)
        if attached is None:
            text = decide(Code.INTERNAL, mode=Mode.READ_ONLY).text
        elif attached.ok:
            text = ""
        else:
            text = attached.text
        if text:
            try:
                process.stdout.write(text.encode())
                await process.stdout.drain()
            except Exception:
                self.logger.debug("SSH: could not deliver attach refusal", exc_info=True)
            process.exit(1)
            return
        await self._sessions.serve(session)

    async def _write_exit_notice(self, stream: Any, message: bytes) -> None:
        """Best-effort delivery of a pre-exit notice to a process stream."""
        try:
            stream.write(message)
            await stream.drain()
        except Exception:
            self.logger.debug("SSH: could not deliver exit notice", exc_info=True)

    async def send_control_frame_to_client(self, client_id: str, payload: Dict[str, Any]) -> bool:
        """ConsoleManager frame entry point; the session module renders it for the raw terminal."""
        return await self._sessions.send_frame(client_id, payload)

    def _resolve_client_meta(self, client_id: str) -> Dict[str, Any]:
        """ConsoleManager cross-adapter IP lookup; the session module owns the registry."""
        return self._sessions.client_meta(client_id)

    # ------------------------------------------------------------------
    # Target resolution / port menu (menu phase only; the pump above has no
    # timeout, so once a port is attached sessions stay open indefinitely).

    async def _resolve_process_target(
        self, listener: ListenerConfig, process: Any, embedded_descriptor: Optional[str]
    ) -> Optional[str]:
        if embedded_descriptor and listener.target == _MENU_TARGET:
            port_name = await self._sessions.resolve_target(embedded_descriptor)
            if not port_name:
                await self._send_and_exit(process, f"Port {embedded_descriptor} unavailable\r\n".encode())
                return None
            return port_name

        if listener.target == _MENU_TARGET:
            return await self._run_port_menu(process)

        port_name = await self._sessions.resolve_target(listener.target)
        if not port_name:
            await self._send_and_exit(process, f"Port {listener.target} unavailable\r\n".encode())
            return None
        return port_name

    async def _run_port_menu(self, process: Any) -> Optional[str]:
        """Print the port list, prompt for a selection, and resolve it."""
        await self._sessions.send_port_list(process.stdout)
        for _ in range(_MENU_MAX_ATTEMPTS):
            process.stdout.write(b"Port: ")
            await process.stdout.drain()
            choice = await self._read_ssh_line(process)
            if choice is None:
                process.exit(0)
                return None
            choice = choice.strip()
            if not choice or choice in ("?", "list"):
                await self._sessions.send_port_list(process.stdout)
                continue
            if choice in ("quit", "exit"):
                process.exit(0)
                return None
            port_name = await self._sessions.resolve_target(choice)
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

    async def _send_and_exit(self, process: Any, payload: bytes) -> None:
        try:
            process.stdout.write(payload)
            await process.stdout.drain()
        except Exception:
            self.logger.error("SSH: failed to deliver port list before exit", exc_info=True)
        process.exit(1)

    # ------------------------------------------------------------------
    # Helpers

    def _build_listener(self, entry: Dict[str, Any]) -> Optional[ListenerConfig]:
        """Shared builder (issue #100); only the require_auth default differs per transport."""
        return build_listener(entry, display_name="SSH", require_auth_default=True, logger=self.logger)


__all__ = ["SshListenerAdapter"]
