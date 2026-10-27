"""Listener session module: one Client session implementation, two transports.

This is the seam agreed in issue #100 and recorded in
``docs/adr/0002-listener-session-module-sits-above-a-byte-transport-seam.md``:
each transport adapter (``telnet_listener``, ``ssh_listener``) turns raw bytes into an
authenticated ``(username, port_name)`` pair, hands the byte pipes over at attach, and
owns nothing above that point. This module owns everything from attach to detach: the
session registry, the read-write gate, the Escape sequence scanner, the Control menu,
the power menu call, detach, and the read-write notices.

The module renders Access decisions (ADR-0001). It prints ``decision.text`` and relays
``decision.frame()`` lines; it never builds a sentence and never maps an Outcome code
to text. Holders and viewers lists are data, not decisions, so their sentences live
here next to the ``w`` and ``u`` commands that print them (see ``openmux.common.access_wire``).

``SessionTransport`` is the seam interface: ``read``, ``readexactly``, ``write``,
``drain``, ``close``. The Telnet adapter implements it with an ``asyncio`` stream pair,
the SSH adapter with an ``asyncssh`` process. The web console is the named future third
transport (ADR-0002): its frame-based message plane must not attach to this byte
interface; only its transport does.

Login policy, the Telnet/SSH auth and Port menu phase, and the reconcile ritual
deliberately stay below the seam, with the transports.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from openmux import __version__ as _OPENMUX_VERSION
from openmux.common.access_wire import holders_text, render_terminal_text

from .access_decision import FRAME_HOLDERS, AccessDecision, Code, decide, mode_or_read_only
from .adapters.listener_common import (
    AclEntry,
    _origin_entry_matches,
    compile_acl,
    read_take_target,
    render_port_list,
    resolve_server_label,
)
from .adapters.power_command import format_power_notice, run_power_menu

#: Sentinel 'target' value that puts a listener into interactive port-menu mode.
MENU_TARGET = "*"


# ---------------------------------------------------------------------------
# The seam: byte pipes for one Client session


class SessionTransport(Protocol):
    """The byte pipes of one Client session, with the wire protocol stripped away.

    Implementations: an ``asyncio`` stream pair (Telnet) and an ``asyncssh`` process
    (SSH). Everything a transport must answer is here, so a new transport adds no
    member to this module. Login-phase reads (IAC stripping, pty echo, line caps) are
    NOT part of the interface: they stay with the transport that needs them.
    """

    async def read(self, n: int) -> bytes:
        """Read up to ``n`` bytes; ``b""`` means the client is gone."""
        ...

    async def readexactly(self, n: int) -> bytes:
        """Read exactly ``n`` bytes (used by the ``e`` command to take two bytes)."""
        ...

    def write(self, data: bytes) -> None:
        """Queue bytes to the client."""
        ...

    async def drain(self) -> None:
        """Flush queued bytes."""
        ...

    async def close(self) -> None:
        """Close the session pipes (socket close, or process close + wait)."""
        ...


# ---------------------------------------------------------------------------
# Escape sequence state machine (moved here with the Control menu, issue #100)


@dataclass
class EscapeState:
    """Per-session state for detecting the in-band control-menu escape sequence.

    `escape_char1`/`escape_char2` are always exactly one byte each; the `e`
    control command lets a session change them at runtime (see
    `feed_escape_byte`'s state==2 handling in this module's pump).
    """

    escape_char1: bytes = b"\x05"  # Ctrl+E
    escape_char2: bytes = b"c"
    state: int = 0


def feed_escape_byte(state: EscapeState, b: bytes) -> Tuple[bytes, Optional[str]]:
    """Feed one byte through the control-menu escape-sequence state machine.

    Mirrors the CLI client's (`openmux/client/console.py`) escape handling:
    a lone first byte that isn't followed by the second escape byte is
    replayed as ordinary data rather than dropped.

    Args:
        state: Session-scoped state, mutated across calls to carry a partial
            sequence across chunk/read boundaries.
        b: Single input byte to evaluate.

    Returns:
        Tuple[bytes, Optional[str]]: bytes that should be forwarded to the
        port right now (empty if `b` was absorbed into pending escape
        state), and a completed single-character command mnemonic, or None.
    """
    if state.state == 0:
        if b == state.escape_char1:
            state.state = 1
            return b"", None
        return b, None
    if state.state == 1:
        state.state = 0
        if b == state.escape_char2:
            state.state = 2
            return b"", None
        return state.escape_char1 + b, None
    # state.state == 2: this byte is the command mnemonic.
    state.state = 0
    return b"", b.decode("latin1", errors="ignore")


CONTROL_MENU_HELP = (
    "\r\n--- OpenMux Control Menu ---\r\n"
    "a  Request read-write access\r\n"
    "f  Take the write slot (ask which holder, or latest)\r\n"
    "s  Release read-write access (switch to read-only)\r\n"
    "w  Show who holds read-write access\r\n"
    "u  Show who is viewing this port\r\n"
    "i  Show session info\r\n"
    "v  Show version\r\n"
    "p  Power (this console's feeds: number = toggle, a = all)\r\n"
    "e  Change escape sequence\r\n"
    ".  Disconnect\r\n"
    "?  Show this menu\r\n"
)


# ---------------------------------------------------------------------------
# Terminal rendering of access frames and data lists
#
# These render what the Console Manager relays; they never invent wording. The
# wording of an Access decision belongs to `access_decision` (ADR-0001); these
# functions only frame lines that already exist, or state data sentences.


def format_rw_notice(payload: Dict[str, Any]) -> str:
    """Render an access frame as CRLF-terminated human text for a raw terminal.

    Telnet and SSH sessions are raw terminals and cannot parse the JSON control
    frames the TCP/WebSocket adapters use, so this turns a frame into its lines.

    Since issue #98 the server owns the wording: an access decision arrives with
    its lines already rendered, and this function only frames them. It prints
    nothing for a frame that carries no line, which is how an attach grant stays
    silent on a terminal that already shows its connect banner.

    Args:
        payload: Control payload, e.g. `{"type": "client_mode", "code": "ok",
            "lines": [...]}` or `{"type": "rw_holders", "holders": [...]}`.

    Returns:
        str: CRLF-terminated message ready to write directly to the session.
    """
    if payload.get("type") == FRAME_HOLDERS:
        # A holders list is data, not a decision, so it keeps its own sentence here.
        return render_terminal_text([f"[{holders_text(payload.get('holders') or [], payload.get('max_rw_users'))}]"])

    if payload.get("type") == "presence":
        # Presence pushes are ambient/web-only (see the badge in console.js); CLI
        # sessions stay silent here and query the same data on demand via 'u'.
        return ""

    return render_terminal_text(payload.get("lines"))


def format_viewer_label(viewer: Dict[str, str]) -> str:
    """Render one viewer entry as `<muxcon-server>/username@<ip>` or `username@<ip>`.

    The `<muxcon-server>/` prefix is only shown for entries reporting a
    `server_id` (i.e. a viewer attached to a *different*, federated server -
    see `ConsoleManager.get_viewers_display`); a viewer local to this same
    server omits it, since it's implied.
    """
    who = f"{viewer.get('username', 'unknown')}@{viewer.get('ip', 'unknown')}"
    server_id = viewer.get("server_id")
    return f"{server_id}/{who}" if server_id else who


def format_viewers_notice(viewers: List[Dict[str, str]]) -> str:
    """Render the current per-port viewer list as CRLF-terminated human text.

    Used by the Ctrl+E control menu's "show viewers" ('u') command, the CLI
    counterpart to the web console's ambient viewer badge (see console.js).

    Args:
        viewers: Entries as returned by `ConsoleManager.get_viewers_display`,
            e.g. `[{"username": "alice", "mode": "read-write", "ip": "10.0.0.5"}, ...]`.

    Returns:
        str: CRLF-terminated message ready to write directly to the session.
    """
    if not viewers:
        return "\r\n[No one is currently viewing this port]\r\n"
    lines = [f"{format_viewer_label(v)} ({v.get('mode', 'read-only')})" for v in viewers]
    return "\r\n--- Viewers on this port ---\r\n" + "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# Listener config (shared by both transports; the reconcile ritual stays per-adapter)


@dataclass
class ListenerConfig:
    name: str
    bind_host: str
    bind_port: int
    target: str
    read_only: bool = False
    acl_raw: List[str] = field(default_factory=list)
    enabled: bool = True
    # Both transports require auth unless the config entry disables it (issue #100);
    # build_listener passes the per-transport default explicitly.
    require_auth: bool = True
    compiled_acl: List[AclEntry] = field(default_factory=list)
    effective_host: Optional[str] = None
    effective_port: Optional[int] = None


def build_listener(
    entry: Dict[str, Any],
    *,
    display_name: str,
    require_auth_default: bool,
    logger: logging.Logger,
) -> Optional[ListenerConfig]:
    """Build one listener spec from a config entry, or None when the entry is unusable.

    Shared by both transports (issue #100). Only two facts differ per transport and the
    caller passes both: the log display name ("Telnet"/"SSH") and the `require_auth`
    default applied to entries without the key. Warning strings keep their exact
    per-transport wording through ``display_name``.
    """
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
            require_auth=bool(entry.get("require_auth", require_auth_default)),
        )
        if not spec.name:
            return None
        if not spec.target:
            return None
        if spec.target == MENU_TARGET and not spec.require_auth:
            logger.warning(
                "%s listener '%s' uses menu mode (target: '*') without require_auth; "
                "any client can browse and attach to any port",
                display_name,
                spec.name,
            )
        spec.compiled_acl = compile_acl(
            spec.acl_raw,
            on_invalid=lambda rule: logger.warning("Ignoring invalid ACL '%s' for listener %s", rule, spec.name),
        )
        return spec
    except Exception:
        logger.error("Invalid %s listener entry: %s", display_name.lower(), entry, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# The session record


@dataclass
class ListenerSession:
    """One attached Client session. Merges the old TelnetSession/SshSession pair.

    ``transport`` is the seam: the byte pipes, not a socket and not a process.
    ``port_mode`` is the mode the session shows and gates writes with; it always
    follows the Access decision or frame the session just rendered (issue #98).
    """

    client_id: str
    listener: ListenerConfig
    transport: SessionTransport
    port_name: str
    read_only: bool
    remote_host: str
    username: str = ""
    port_mode: str = "read-only"
    bytes_in: int = 0
    bytes_out: int = 0
    escape: EscapeState = field(default_factory=EscapeState)


# ---------------------------------------------------------------------------
# The module interface: registry + attach/detach + Control menu + I/O pump


class ListenerSessionManager:
    """Everything from attach to detach, written once for both transports.

    A transport adapter constructs one manager per adapter instance and delegates
    its pinned ConsoleManager names to it (``send_data_to_client``,
    ``send_control_frame_to_client``, ``_resolve_client_meta``, channel
    registration). The Console Manager, Auth Manager, and Port Manager arrive as
    getters because adapters wire them after construction. ``display_name``
    ("Telnet"/"SSH") keeps every operator-visible log line's wording (issue #100).
    """

    def __init__(
        self,
        *,
        display_name: str,
        logger: logging.Logger,
        channel_owner: Any,
        get_console_manager: Callable[[], Any],
        get_auth_manager: Callable[[], Any],
        get_port_manager: Callable[[], Any],
    ):
        self.display_name = display_name
        self.logger = logger
        # ConsoleManager registers client channels on the object that answers its
        # pinned names (send_data_to_client, send_control_frame_to_client, probed
        # by hasattr at console_manager.py:1058). That object stays the adapter;
        # the adapter's one-line delegations land the work here.
        self._channel_owner = channel_owner
        self._get_console_manager = get_console_manager
        self._get_auth_manager = get_auth_manager
        self._get_port_manager = get_port_manager
        self.sessions: Dict[str, ListenerSession] = {}

    # --- registry -----------------------------------------------------------

    def create_session(
        self,
        listener: ListenerConfig,
        transport: SessionTransport,
        *,
        port_name: str,
        peer_ip: str,
        username: str,
    ) -> ListenerSession:
        """Build the session record for a transport that finished its auth/menu phase.

        The client id keeps its historical ``<kind>:<listener>:<uuid>`` shape; the
        kind is the lower-cased display name ("telnet", "ssh").
        """
        client_id = f"{self.display_name.lower()}:{listener.name}:{uuid.uuid4()}"
        return ListenerSession(
            client_id=client_id,
            listener=listener,
            transport=transport,
            port_name=port_name,
            read_only=listener.read_only,
            remote_host=peer_ip,
            username=username,
        )

    async def attach(self, session: ListenerSession) -> Optional[AccessDecision]:
        """Attach a session and return the access decision for it.

        Returns None when there is no console manager or the attach raised. The caller
        prints the refusal carried by a decision, so a listener refusal says what the
        access module says instead of what the transport guessed (issue #98). On a
        grant the session joins the registry and is registered as a client channel.
        """
        cm = self._get_console_manager()
        if not cm:
            return None
        try:
            attached = await cm.connect_client_to_port(
                session.client_id,
                session.port_name,
                session.username,
            )
        except Exception as exc:
            self.logger.error("Console manager attach failed for %s: %s", session.client_id, exc)
            return None
        if attached.ok:
            session.port_mode = attached.mode.value
        else:
            self.logger.warning(
                "Console manager rejected %s client %s for port %s (code=%s)",
                self.display_name,
                session.client_id,
                session.port_name,
                attached.code.value,
            )
        if attached.ok:
            self.sessions[session.client_id] = session
            if hasattr(cm, "register_client_channel"):
                try:
                    cm.register_client_channel(session.client_id, self._channel_owner)
                except Exception:
                    self.logger.error(
                        "register_client_channel failed for %s client %s", self.display_name, session.client_id, exc_info=True
                    )
        return attached

    async def serve(self, session: ListenerSession) -> None:
        """Run the session body: connected log, read-only warning, pump, detach.

        Byte-specific by construction (the pump needs ``read``). A frame-based
        transport (the future web console) uses ``attach`` plus the named command
        handlers and never calls this.
        """
        self.logger.info(
            "%s client %s connected (listener=%s, port=%s, ro=%s)",
            self.display_name,
            session.client_id,
            session.listener.name,
            session.port_name,
            session.read_only,
        )
        if session.listener.read_only or session.port_mode != "read-write":
            await self._write_session(session, "\r\n[WARNING: console is in read-only mode]\r\n")
        try:
            await self.pump(session)
        finally:
            await self.disconnect(session.client_id, reason="disconnect")

    # --- I/O pump + Control menu -------------------------------------------

    async def pump(self, session: ListenerSession) -> None:
        """Forward client bytes to the Port until EOF, servicing the Control menu."""
        while True:
            try:
                data = await session.transport.read(4096)
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

    async def _process_input_chunk(self, session: ListenerSession, data: bytes) -> bool:
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

    async def _forward_payload(self, session: ListenerSession, payload: bytes) -> bool:
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
            cm = self._get_console_manager()
            if cm:
                await cm.write_to_port(session.port_name, payload, session.client_id)
            return True
        except Exception as exc:
            self.logger.warning("Failed forwarding data from %s to port %s: %s", session.client_id, session.port_name, exc)
            return False

    async def _change_escape_sequence(self, session: ListenerSession, data: bytes, i: int) -> Tuple[bytes, int]:
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
                remaining += await session.transport.readexactly(2 - len(remaining))
            except Exception:
                # justification: a read failure ends the session; the reader loop sees the EOF
                pass
        if len(remaining) < 2:
            return b"", 0
        session.escape.escape_char1 = remaining[0:1]
        session.escape.escape_char2 = remaining[1:2]
        await self._write_session(session, "\r\n[Escape sequence changed]\r\n")
        return remaining[2:], 0

    async def _handle_control_command(self, session: ListenerSession, cmd: str) -> bool:
        """Execute one Control menu command. Returns True to request disconnect.

        Replaces the nine-argument ``_dispatch_control_command`` callback bag
        (issue #100): every action is a method on this manager now.
        """
        cm = self._get_console_manager()
        rw_command: Dict[str, Any] = {"a": self._cmd_request_rw, "f": self._cmd_force_rw, "s": self._cmd_release_rw}
        if cmd in rw_command:
            await rw_command[cmd](session, cm)
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
        elif cmd == "p":
            await run_power_menu(
                cm,
                session.port_name,
                session.transport,
                session.transport,
                lambda line: self._write_session(session, line + "\r\n"),
                session.username,
                self._get_auth_manager(),
                session.client_id,
            )
        elif cmd == ".":
            await self._write_session(session, "\r\n[Disconnecting...]\r\n")
            return True
        return False

    async def _cmd_request_rw(self, session: ListenerSession, cm: Any) -> None:
        if session.listener.read_only:
            await self._write_session(session, "\r\n[This listener is configured read-only]\r\n")
            return
        decision = await cm.promote_client_to_read_write(session.client_id, session.port_name) if cm else None
        await self._reply_access_decision(session, decision)

    async def _cmd_force_rw(self, session: ListenerSession, cm: Any) -> None:
        if session.listener.read_only:
            await self._write_session(session, "\r\n[This listener is configured read-only]\r\n")
            return
        # Targeted takeover (issue #61): ask which holder to take the slot
        # from; Enter keeps the no-target fallback (latest holder). A prompt
        # the client abandons (disconnect) stops the session pump.
        target, keep_going = await read_take_target(session.transport, session.transport)
        if not keep_going:
            return
        decision = await cm.take_write_slot(session.client_id, session.port_name, target) if cm else None
        await self._reply_access_decision(session, decision)

    async def _cmd_release_rw(self, session: ListenerSession, cm: Any) -> None:
        decision = await cm.demote_client_to_read_only(session.client_id, session.port_name) if cm else None
        await self._reply_access_decision(session, decision)

    async def _reply_access_decision(self, session: ListenerSession, decision: Optional[AccessDecision]) -> None:
        """Print one Access decision and record the mode it reports.

        The decision owns the wording, including a takeover's "taken from" line
        (issue #98). This module owns only the mode the session shows, and takes
        it from the decision, so the session, the printed line, and the Port agree
        - including after a release that failed, which leaves the session read-write.
        """
        if decision is None:
            # The collapse agreed in issue #100: the missing-manager path renders the
            # decision directly. ``decision.text`` is exactly what the old round trip
            # through a frame dict and format_rw_notice produced, so the
            # access_manager_missing_frame helper is gone.
            await self._write_session(session, decide(Code.INTERNAL, mode=mode_or_read_only(session.port_mode)).text)
            return
        session.port_mode = decision.mode.value
        await self._write_session(session, decision.text)

    def _format_session_info(self, session: ListenerSession) -> str:
        esc = session.escape
        esc_display = f"{esc.escape_char1!r}+{esc.escape_char2!r}"
        return (
            "\r\n--- Session Info ---\r\n"
            f"Port: {session.port_name}\r\n"
            f"Mode: {session.port_mode}\r\n"
            f"Remote host: {session.remote_host}\r\n"
            f"Escape sequence: {esc_display}\r\n"
        )

    async def _write_session(self, session: ListenerSession, text: str) -> None:
        try:
            session.transport.write(text.encode())
            await session.transport.drain()
            session.bytes_out += len(text)
        except Exception:
            self.logger.error(
                "%s session write failed (client=%s port=%s)",
                self.display_name,
                session.client_id,
                session.port_name,
                exc_info=True,
            )

    # --- cross-adapter channel (the names ConsoleManager pins on the adapter) -

    async def send_data(self, client_id: str, data: bytes) -> bool:
        session = self.sessions.get(client_id)
        if not session:
            return False
        try:
            session.transport.write(data)
            await session.transport.drain()
            session.bytes_out += len(data)
            return True
        except Exception as exc:
            self.logger.debug("Failed to send data to %s client %s: %s", self.display_name, client_id, exc)
            await self.disconnect(client_id, reason="write failure")
            return False

    async def send_frame(self, client_id: str, payload: Dict[str, Any]) -> bool:
        """Deliver a cross-adapter access notice as human text (raw terminal).

        Telnet and SSH clients cannot parse JSON control frames, so this renders
        `payload` via `format_rw_notice` instead of the JSON/OMXCTRL framing the
        TCP/WebSocket adapters use.
        """
        session = self.sessions.get(client_id)
        if session is None:
            return False
        # An access frame states the mode its Client holds afterwards, so the
        # session follows the Port instead of guessing from one reason value.
        if payload.get("mode"):
            session.port_mode = payload["mode"]
        await self._write_session(session, format_rw_notice(payload))
        return True

    def client_meta(self, client_id: str) -> Dict[str, Any]:
        """Return `{"type", "ip", "username"}` (ConsoleManager cross-adapter IP lookup)."""
        session = self.sessions.get(client_id)
        if session is None:
            return {}
        return {"type": self.display_name.lower(), "ip": session.remote_host, "username": session.username}

    def on_port_meta(self, port_name: str, changes: Optional[Dict[str, Any]] = None) -> None:
        """Push the live power notice to sessions attached to this port.

        A feed change (user switch or out-of-band drift) reaches each attached
        session as the ``[POWER]`` / ``[POWER WARNING]`` line, the same text the
        client listener writes.
        """
        try:
            if not isinstance(changes, dict) or changes.get("event") != "power_outlet_changed":
                return
            if not port_name:
                return
            notice = format_power_notice(changes)
            for session in list(self.sessions.values()):
                if session.port_name == port_name:
                    asyncio.ensure_future(self._write_session(session, notice))
        except Exception:
            # justification: best-effort notice; power state is unchanged
            pass

    # --- teardown ------------------------------------------------------------

    async def disconnect(self, client_id: str, *, reason: str) -> None:
        session = self.sessions.pop(client_id, None)
        if not session:
            return
        cm = self._get_console_manager()
        try:
            if cm:
                try:
                    if hasattr(cm, "unregister_client_channel"):
                        cm.unregister_client_channel(client_id)
                except Exception:
                    # justification: teardown routing cleanup; the port disconnect below is logged on failure
                    pass
                await cm.disconnect_client_from_port(client_id, session.port_name)
        except Exception:
            self.logger.debug("Failed disconnecting client %s from port", client_id, exc_info=True)
        try:
            await session.transport.close()
        except Exception:
            # justification: shutdown cleanup; the transport may already be closed
            pass
        self.logger.info(
            "%s client %s closed (%s) in=%d out=%d",
            self.display_name,
            client_id,
            reason,
            session.bytes_in,
            session.bytes_out,
        )

    async def drop_listener_sessions(self, listener_name: str, *, reason: str) -> None:
        """Disconnect only the sessions of one listener (the reconcile path's teardown)."""
        for client_id in [cid for cid, s in list(self.sessions.items()) if s.listener.name == listener_name]:
            await self.disconnect(client_id, reason=reason)

    async def disconnect_all(self, *, reason: str) -> None:
        for client_id in list(self.sessions.keys()):
            await self.disconnect(client_id, reason=reason)

    # --- target resolution (shared mechanism; both menu phases call it) ------

    async def resolve_target(self, descriptor: str) -> Optional[str]:
        pm = self._get_port_manager()
        if not pm:
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
            port = self._get_port_manager().get_port(port_name)
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
        """Resolve a port name to an unambiguous origin-matched entry.

        Delegates the per-entry filter to the shared ``_origin_entry_matches``
        (listener_common); returns the port name only when exactly one entry
        matches.
        """
        entries = []
        try:
            getter = getattr(self._get_port_manager(), "get_port_list_with_federation", None)
            if getter:
                entries = await asyncio.wait_for(getter(), timeout=1.0)
        except Exception:
            entries = []
        matches = [e for e in (entries or []) if _origin_entry_matches(e, port_name, server_id)]
        if len(matches) == 1:
            return matches[0].get("name")
        return None

    async def send_port_list(self, writer: Any) -> None:
        """Write the Port list to a session transport or a menu stream.

        ``writer`` needs only ``write(bytes)`` and ``await drain()``: both the
        post-attach transport and a raw menu stream satisfy it.
        """
        pm = self._get_port_manager()
        entries: List[Dict[str, Any]] = []
        try:
            getter = getattr(pm, "get_port_list_with_federation", None)
            if getter:
                entries = await asyncio.wait_for(getter(), timeout=1.0)
        except Exception:
            entries = []
        writer.write(render_port_list(entries, header=resolve_server_label(pm)))
        await writer.drain()


__all__ = [
    "CONTROL_MENU_HELP",
    "MENU_TARGET",
    "EscapeState",
    "ListenerConfig",
    "ListenerSession",
    "ListenerSessionManager",
    "SessionTransport",
    "build_listener",
    "feed_escape_byte",
    "format_rw_notice",
    "format_viewer_label",
    "format_viewers_notice",
]
