"""Console UI components for the OpenMux client.

This module implements an interactive terminal user interface that allows a
user to interact with an OpenMux server connection via a locally attached
``BaseClientAdapter`` implementation. Features include:

* Raw terminal mode management (with restoration on exit)
* Continuous asynchronous read loop with optional auto reconnect
* Local escape sequence handling (default ``Ctrl+E`` + ``c`` prefix)
* Read-only (spy) mode and read/write attachment switching
* Octal byte injection and dynamic escape sequence reconfiguration
* Exponential backoff based auto reconnect logic
* Graceful handling of server initiated shutdown messages

The UI is intentionally lightweight and keeps presentation simple (CRLF
normalization, bracketed status messages) while delegating connection and
authentication responsibilities to the provided adapter.
"""

import asyncio
import io
import json
import logging
import os
import sys
import termios
import tty
from typing import Any, Dict, List, Optional, Tuple

from .adapters import BaseClientAdapter

#: Interactive command-mode prompt (issue #88). Client-side by design: the
#: server command phase stays prompt-free, so raw TCP clients see no new bytes.
COMMAND_PROMPT = "mux> "

#: Notice printed for `disconnect` / detach while not attached to any port.
NOT_ATTACHED_NOTICE = '[Not attached. Type "connect <port>", or "quit" to end the session]'

#: Command-mode help text. The code block in docs/design/cli_command_mode.md
#: holds the identical lines; a test pins the two together so they cannot drift.
COMMAND_MODE_HELP_LINES = [
    "Commands:",
    "  help, h, ?          Show this help",
    "  list, l             List console ports",
    "  connect <port>, c   Attach to a port",
    "  disconnect          Leave a port (while attached: press Ctrl+E c d)",
    "  power [args]        Power: power [<pdu>[.<outlet>] [on|off]]",
    "  quit, exit, q       End the session",
    "Blank line does nothing. While attached, keystrokes go to the port.",
    "The control menu (shown at startup) controls the session.",
]


class ConsoleUI:
    """Interactive terminal user interface for an OpenMux client session.

    The UI manages terminal state, keyboard input, server output display, and
    a small command language reached through a two-character escape sequence.
    Reconnect logic (manual or automatic) is also coordinated here to keep the
    console session resilient to transient network issues.
    """

    def __init__(
        self,
        connection: BaseClientAdapter,
        reconnect_mode: str = "off",
        backoff_initial: float = 1.0,
        backoff_max: float = 30.0,
        command_mode: bool = False,
    ):
        """Initialize a new console UI instance.

        Args:
            connection: An already created client adapter that provides
                connectivity and authentication state plus ``send_data`` /
                ``read_data`` primitives.
            reconnect_mode: Reconnect strategy: ``'off'`` (terminate on
                disconnect), ``'manual'`` (stay running allowing user to issue
                an escape reconnect command), or ``'auto'`` (background retry
                with exponential backoff).
            backoff_initial: Initial delay in seconds before first retry in
                auto reconnect mode.
            backoff_max: Maximum delay in seconds between retries.
            command_mode: Offer the interactive ``mux> `` command mode for an
                unattached TCP session (issue #88). Only honored when the
                adapter reports ``supports_command_phase``; ignored entirely
                by WebSocket sessions and port-attached runs.
        """
        self.connection = connection
        self.is_running = False
        self.old_settings = None
        self.logger = logging.getLogger("openmux.client.console")
        # File descriptor for stdin (guarded for test environments without fileno)
        try:
            self._stdin_fd = sys.stdin.fileno()
        except (io.UnsupportedOperation, AttributeError):
            self._stdin_fd = None
        # Track if we've displayed any passthrough data yet (used to suppress
        # extra close notices for tests expecting a single write)
        self._received_any_data = False
        # Escape sequence handling
        self.escape_char1 = "\x05"  # Ctrl+E (octal 005)
        self.escape_char2 = "c"  # 'c' character
        # 0=normal, 1=got first escape char, 2=got second escape char
        self.escape_state = 0
        # Byte equivalents for escape handling
        self.escape_b1 = self.escape_char1.encode("latin1", errors="ignore")
        self.escape_b2 = self.escape_char2.encode("latin1", errors="ignore")
        self.read_only_mode = False
        # Sentinel: access mode not yet confirmed by the adapter
        self._last_synced_access_mode = None
        # True while an interactive escape submenu (the `p` power menu) is
        # open. The background read loop keeps reading and rendering server
        # data (the console is frozen, but stream data is never dropped) and
        # keyboard forwarding pauses so the menu owns stdin. The menu itself
        # never reads the stream - the background loop stays the sole reader
        # of the connection - it only waits for the reply the adapter
        # captures plus a brief settle of the stream. Cleared on exit.
        self._stream_exclusive = False
        # Payload chunks delivered by the background read loop since start.
        # The power menu compares this in _wait_stream_quiet so a live
        # [POWER] notice rendered just before a reply lands BEFORE the menu
        # re-renders the feed list.
        self._stream_activity = 0
        self.replay_lines = 20
        # Reconnect settings
        self.reconnect_mode = reconnect_mode  # 'off' | 'manual' | 'auto'
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self._current_backoff = backoff_initial
        self._auto_reconnect_task = None
        self._notified_disconnect = False
        # Debug toggle for input path
        self._debug_input = os.environ.get("OPENMUX_CLIENT_DEBUG_INPUT", "").lower() in ("1", "true", "yes")
        # Quiet mode support
        self.quiet_mode = os.environ.get("OPENMUX_CLIENT_QUIET", "").lower() in ("1", "true", "yes")
        # Normalize CR to LF on outgoing input (Enter often yields CR in raw mode)
        # Can be disabled with OPENMUX_CLIENT_NORMALIZE_CRLF=0
        self.normalize_crlf = os.environ.get("OPENMUX_CLIENT_NORMALIZE_CRLF", "1").lower() in ("1", "true", "yes")
        # Interactive command mode (issue #88): an unattached TCP server
        # session shows a prompt with local echo instead of dumping the user
        # into the raw stream. Switches off when a port attaches, and a
        # detach (escape `d`) switches it back on.
        self.command_mode = bool(command_mode and getattr(connection, "supports_command_phase", False))
        # Current command-mode input line; kept as raw bytes so echo and
        # backspace stay byte-correct for multi-byte input.
        self._cmd_line = bytearray()
        # Part of a server reply line received between reads in command mode.
        self._cmd_rx = b""
        # True while a forwarded command waits for its reply. The prompt is
        # written again once the reply stream has been quiet for a few polls:
        # command-phase replies have no completion framing, and the background
        # loop must stay the sole reader of the connection (single-reader rule).
        self._cmd_awaiting_reply = False
        self._cmd_quiet_polls = 0

    async def run(self) -> bool:
        """Run the interactive UI event loop.

        Sets raw terminal mode, spawns the background read loop, and processes
        keyboard input until termination (disconnect, user command, or error).

        Returns:
            bool: True if the loop exited cleanly (normal disconnect or user
            requested exit), False if an unrecoverable error occurred prior to
            normal initialization or while running.
        """
        if not self.connection.is_connected or not self.connection.is_authenticated:
            self.logger.error("Not connected or authenticated")
            return False

        try:
            self.is_running = True

            # Set terminal to raw mode
            self._set_raw_mode()

            # Show startup message
            self._show_startup_message()

            # Reflect any access mode already confirmed by the adapter (e.g. the
            # TCP adapter learns this synchronously during CONNECT) and warn
            # the user if the session starts out read-only.
            self._sync_access_mode()

            # Interactive command mode (issue #88): show the discovery hint
            # and the first prompt. Command replies re-prompt via the read loop.
            if self.command_mode:
                sys.stdout.write('\r\nType "help" for commands.\r\n' + COMMAND_PROMPT)
                sys.stdout.flush()

            # Start read task
            read_task = asyncio.create_task(self._read_from_server())

            # Handle keyboard input
            await self._handle_keyboard_input()

            # Cancel read task
            read_task.cancel()

            return True

        except Exception as e:
            self.logger.error("Error in console UI: %s", e, exc_info=True)
            return False

        finally:
            # Restore terminal settings
            self._restore_terminal()
            self.is_running = False

    def _set_raw_mode(self):
        """Switch the terminal into raw mode (POSIX only).

        Raw mode disables canonical input processing so that keystrokes are
        delivered immediately to the application without line buffering or
        local echo handling, allowing precise escape detection and forwarding
        of control characters to the remote side.
        """
        if os.name == "posix":
            # Save old settings
            self.old_settings = termios.tcgetattr(sys.stdin)
            # Set terminal to raw mode
            tty.setraw(sys.stdin)

    def _restore_terminal(self):
        """Restore the previously saved terminal configuration (POSIX only)."""
        if os.name == "posix" and self.old_settings:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.old_settings)

    def _show_startup_message(self):
        """Emit a startup banner including the active escape sequence mapping."""
        # Format escape sequence for display
        esc1_display = f"Ctrl+{chr(ord(self.escape_char1) + 64)}" if ord(self.escape_char1) < 32 else self.escape_char1
        esc2_display = self.escape_char2

        startup_msg = (
            f"Escape sequence: {esc1_display} {esc2_display}\r\n"
            + f"For help: {esc1_display} {esc2_display} ?\r\n"
            + f"To disconnect: {esc1_display} {esc2_display} .\r\n"
            + "Ctrl+C is forwarded to remote"
            + "\r\n[OpenMux Console Connected]\r\n"
            + "\r\n"
        )

        sys.stdout.write(startup_msg)
        sys.stdout.flush()

    def _sync_access_mode(self):
        """Refresh `read_only_mode` from the adapter's confirmed `access_mode`.

        The adapter (TCP or WebSocket) is the source of truth for the actual
        server-granted mode; this keeps local input gating and status displays
        aligned with it instead of an independent, purely local toggle. Prints
        a bracketed warning the first time (or whenever) the confirmed mode is
        read-only, since the server does not send its own "granted" style
        message for the initial/passive mode assigned at connect time.
        """
        mode = getattr(self.connection, "access_mode", None)
        if mode is None or mode == self._last_synced_access_mode:
            return
        self._last_synced_access_mode = mode
        self.read_only_mode = mode != "read-write"
        if self.read_only_mode:
            sys.stdout.write("\r\n[WARNING: console is in read-only mode]\r\n")
            sys.stdout.flush()

    async def _read_from_server(self):
        """Continuously read server data and render to the terminal.

        Handles CRLF normalization, shutdown message detection, reconnect
        orchestration, and quiet-mode suppression of status lines. The loop
        yields periodically to remain responsive to cancellation and state
        changes.
        """
        while self.is_running:
            try:
                if await self._read_step():
                    break
            except asyncio.CancelledError:
                # Task was cancelled
                break
            except Exception as e:
                self.logger.error("Error reading from server: %s", e, exc_info=True)
                # In reconnect modes, keep UI alive and retry
                if self.reconnect_mode in ("auto", "manual"):
                    await asyncio.sleep(0.2)
                    continue
                self.is_running = False
                break

    async def _read_step(self) -> bool:
        """Run one iteration of the read loop.

        Returns True when the caller should break and stop, False when it
        should loop again. The loop remains the SOLE reader of the connection:
        a second concurrent reader would desynchronize the transport (an
        asyncio stream reader allows only one pending read).
        """
        if self._read_handle_stream_exclusive():
            return True
        # If disconnected, ensure auto/manual handling is engaged and yield briefly
        if not self.connection.is_connected:
            await self._read_disconnected_poll()
            return False
        # Read data from server with a short timeout
        data = await self.connection.read_data(timeout=0.1)
        # None indicates connection closed (EOF)
        if data is None:
            return await self._read_eof()
        # Empty payload means timeout/no data; continue polling
        if data == b"" or data == "":
            self._command_reply_settle()
            return False
        # Record stream activity so the power menu can wait for the
        # stream to settle (a live [POWER] notice is payload that
        # renders here) before it re-renders the feed list.
        self._stream_activity += 1
        self._cmd_quiet_polls = 0
        # Reflect any access-mode change the adapter learned while
        # decoding a control-frame response (e.g. after a promote/demote).
        self._sync_access_mode()
        return self._read_render(data)

    def _read_handle_stream_exclusive(self) -> bool:
        """Handle a stream-exclusive (escape-submenu) open over a dead link.

        Returns True when the loop should break. While an interactive escape
        submenu is open (e.g. the `p` power menu), the read loop keeps running
        so the frozen console still receives everything, then hands back to the
        menu - it shows the close on its next poll - engaging reconnect
        handling if configured.
        """
        if not self._stream_exclusive or self.connection.is_connected:
            return False
        # Hand back to the menu, which shows the close on its next
        # poll; engage reconnect handling if configured.
        if self.reconnect_mode == "auto" and not self._auto_reconnect_task:
            self._auto_reconnect_task = asyncio.create_task(self._auto_reconnect_loop())
        elif self.reconnect_mode == "off":
            self.is_running = False
        return True

    async def _read_disconnected_poll(self) -> None:
        """Engage auto-reconnect for a disconnected link, then yield briefly.

        In auto mode this starts a background reconnection task (and, unless
        quiet, announces it). In manual/off modes it only yields, so the loop
        keeps polling the connection state.
        """
        if self.reconnect_mode == "auto" and not self._auto_reconnect_task:
            self._auto_reconnect_task = asyncio.create_task(self._auto_reconnect_loop())
            if not self.quiet_mode:
                sys.stdout.write("\r\n[Reconnecting... auto]\r\n")
                sys.stdout.flush()
        await asyncio.sleep(0.1)

    async def _read_eof(self) -> bool:
        """Process an EOF (read_data returned None). Returns True to break."""
        self._read_eof_notice()
        await self._read_eof_close()
        return await self._read_eof_reconnect()

    def _read_eof_notice(self) -> None:
        """Show the one-shot server-closed notice (suppressed after data shown)."""
        if not self._received_any_data and not self._notified_disconnect:
            # Only show user-facing notice if no prior data was displayed; this keeps
            # unit tests (which expect only the data bytes) satisfied after a single
            # data chunk followed by EOF.
            sys.stdout.write("\r\n")
            sys.stdout.flush()
            self.logger.info("Server connection closed")
            sys.stdout.write("\r\n[Server closed connection]\r\n")
            sys.stdout.flush()
            self._notified_disconnect = True

    async def _read_eof_close(self) -> None:
        """Proactively close adapter resources (e.g., aiohttp sessions) to avoid GC warnings."""
        try:
            await self.connection.close()
        except Exception:
            # justification: shutdown cleanup; the adapter close is idempotent
            pass

    async def _read_eof_reconnect(self) -> bool:
        """Decide post-EOF reconnect behavior. Returns True when to break.

        auto: start a background reconnection task and keep the UI running.
        manual: keep the UI running; allow a manual reconnect via the menu.
        off: stop the loop.
        """
        if self.reconnect_mode == "auto":
            if not self._auto_reconnect_task:
                self._auto_reconnect_task = asyncio.create_task(self._auto_reconnect_loop())
                if not self.quiet_mode:
                    sys.stdout.write("\r\n[Reconnecting... auto]\r\n")
                    sys.stdout.flush()
            await asyncio.sleep(0.1)
            return False
        if self.reconnect_mode == "manual":
            # Keep UI running; allow user to trigger manual reconnect via menu
            if not self._auto_reconnect_task and not self.read_only_mode:
                if not self.quiet_mode:
                    sys.stdout.write("\r\n[Disconnected. Use escape 'o' to reconnect]\r\n")
                    sys.stdout.flush()
            await asyncio.sleep(0.1)
            return False
        self.is_running = False
        return True

    def _read_render(self, data) -> bool:
        """Normalize and render one payload. Returns True on a SHUTDOWN (break)."""
        if self.command_mode:
            # Command mode renders server replies line-wise and watches for
            # the attach confirmation (issue #88).
            return self._read_render_command(data)
        if isinstance(data, str):
            # Check for shutdown message
            if "SERVER:SHUTDOWN" in data:
                sys.stdout.write("\r\n[Server shutdown]\r\n")
                sys.stdout.flush()
                self.is_running = False
                return True
            # Normalize \r\n/\n to exactly one \r\n per line end
            # (some payloads, e.g. the live [POWER] notice, are
            # already \r\n-terminated - a bare \n replacement would
            # double the CR).
            normalized_data = data.replace("\r\n", "\n").replace("\n", "\r\n")
            sys.stdout.write(normalized_data)
            sys.stdout.flush()
            if normalized_data:
                self._received_any_data = True
            return False
        # Handle bytes data - convert to bytes if memoryview, then normalize
        if isinstance(data, memoryview):
            data = data.tobytes()
        # Check for shutdown message
        if b"SERVER:SHUTDOWN" in data:
            sys.stdout.write("\r\n[Server shutdown]\r\n")
            sys.stdout.flush()
            self.is_running = False
            return True
        # Normalize \r\n/\n to exactly one \r\n per line end
        # (payloads that are already \r\n-terminated would
        # otherwise show a doubled CR).
        normalized_data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        sys.stdout.buffer.write(normalized_data)
        sys.stdout.buffer.flush()
        if normalized_data:
            self._received_any_data = True
        return False

    def _read_render_command(self, data) -> bool:
        """Render command-phase replies line-wise; switch mode on attach (issue #88).

        Server replies are line-framed in the command phase, so buffer to line
        ends and format each. A complete ``CONNECTED:<port>:<mode>`` line means
        the session attached: print the notice, record the port on the adapter,
        leave command mode, and render any bytes after the line through the
        normal path (they are port stream data).
        """
        if isinstance(data, str):
            data = data.encode("utf-8", errors="ignore")
        elif isinstance(data, memoryview):
            data = data.tobytes()
        if b"SERVER:SHUTDOWN" in data:
            sys.stdout.write("\r\n[Server shutdown]\r\n")
            sys.stdout.flush()
            self.is_running = False
            return True
        buf = self._cmd_rx + data
        lines = buf.split(b"\n")
        self._cmd_rx = lines.pop()  # possibly incomplete tail, kept for the next chunk
        out = bytearray()
        for idx, raw in enumerate(lines):
            line = raw.rstrip(b"\r")
            if line.startswith(b"CONNECTED:"):
                # Everything after this line is port stream data: collect the
                # raw bytes (the stored segments still carry their own CRs)
                # before _command_attached clears the pending buffer.
                rest = list(lines[idx + 1 :])
                if self._cmd_rx:
                    rest.append(self._cmd_rx)
                raw_tail = b"\n".join(rest)
                self._cmd_rx = b""
                if out:
                    sys.stdout.buffer.write(bytes(out))
                    sys.stdout.buffer.flush()
                    self._received_any_data = True
                    out = bytearray()
                self._command_attached(line)
                if raw_tail:
                    return self._read_render(bytes(raw_tail))
                return False
            out += self._format_command_reply_line(line) + b"\r\n"
        if out:
            sys.stdout.buffer.write(bytes(out))
            sys.stdout.buffer.flush()
            self._received_any_data = True
        return False

    def _format_command_reply_line(self, line: bytes) -> bytes:
        """Format one full command-phase reply line (currently only LIST)."""
        if line.startswith(b"LIST:{"):
            return self._format_list_line(line)
        return line

    def _format_list_line(self, line: bytes) -> bytes:
        """Render ``LIST:{json}`` as one line per port; keep the raw line when unparseable."""
        try:
            payload = json.loads(line[5:].decode("utf-8", errors="ignore"))
            ports = payload.get("ports") or []
            rows = []
            for p in ports:
                if not isinstance(p, dict):
                    rows.append(f"  {p}")
                    continue
                name = str(p.get("name") or "")
                desc = str(p.get("description") or "")
                if not name:
                    continue
                rows.append(f"  {name}  -  {desc}" if desc else f"  {name}")
            if not rows:
                return b"(no ports)"
            return "\r\n".join(rows).encode("utf-8")
        except Exception:
            return line

    def _command_attached(self, line: bytes) -> None:
        """Leave command mode after a ``CONNECTED:<port>:<mode>`` server line."""
        parts = line.decode("utf-8", errors="ignore").split(":")
        port_name = parts[1] if len(parts) > 1 else ""
        mode = "read-write" if len(parts) > 2 and parts[2] == "READ_WRITE" else "read-only"
        self.command_mode = False
        self._cmd_awaiting_reply = False
        self._cmd_rx = b""
        try:
            # Mirror what the adapter's own connect_to_port records, so info
            # dumps and manual reconnect behave the same after a typed connect.
            self.connection.current_port = port_name
            self.connection.access_mode = mode
        except Exception:
            # justification: adapter view mirror is best-effort; input forwarding works without it
            pass
        self._sync_access_mode()
        sys.stdout.write(f"\r\n[Connected to {port_name} ({mode})]\r\n")
        sys.stdout.flush()

    def _command_reply_settle(self) -> None:
        """Re-prompt when a forwarded command's reply stream has been quiet (issue #88).

        Command-phase replies are not completion-framed, so "answered" means
        this many consecutive idle read polls (~0.1 s each). Only the settle
        logic touches the prompt here; the background loop stays the reader.
        """
        if not self.command_mode or not self._cmd_awaiting_reply:
            return
        self._cmd_quiet_polls += 1
        if self._cmd_quiet_polls >= 3:
            self._cmd_awaiting_reply = False
            sys.stdout.write(COMMAND_PROMPT)
            sys.stdout.flush()

    async def _auto_reconnect_loop(self):
        """Attempt automatic reconnection with exponential backoff.

        Continues retrying until a connection is re-established or the UI is
        no longer running. Backoff doubles each failure up to ``backoff_max``.
        On success resets state and clears disconnect notifications.
        """
        try:
            while self.is_running and not self.connection.is_connected:
                # Immediate attempt
                try:
                    ok = await getattr(self.connection, "reconnect")()
                except Exception as e:
                    self.logger.error("Reconnect attempt failed: %s", e, exc_info=True)
                    ok = False
                if ok:
                    if not self.quiet_mode:
                        sys.stdout.write("\r\n[Reconnected]\r\n")
                        sys.stdout.flush()
                    self._current_backoff = self.backoff_initial
                    self._auto_reconnect_task = None
                    self._notified_disconnect = False
                    return
                # Not ok: wait and backoff
                wait = min(self._current_backoff, self.backoff_max)
                if not self.quiet_mode:
                    sys.stdout.write(f"\r\n[Reconnect attempt in {wait:.1f}s]\r\n")
                sys.stdout.flush()
                await asyncio.sleep(wait)
                self._current_backoff = min(self._current_backoff * 2, self.backoff_max)
        finally:
            self._auto_reconnect_task = None

    async def _handle_keyboard_input(self):
        """Capture and process local keyboard input.

        Polls stdin at a short interval (non-blocking) to interleave with the
        server read loop. Implements escape sequence state machine and forwards
        bytes to the connection unless suppressed (read-only mode or control
        flow consumed locally).
        """
        try:
            # Use asyncio to read from stdin
            buf = bytearray()
            last_send = 0.0
            flush_interval = 0.01  # seconds
            max_chunk = 4096
            while self.is_running:
                await asyncio.sleep(0.005)

                # While an interactive escape submenu (the `p` power menu)
                # owns stdin, pause forwarding so the two loops never read
                # the same keyboard buffer at once.
                if self._stream_exclusive:
                    continue

                sent_now = False
                if self._is_data_available():
                    # Read as many bytes as available up to max_chunk
                    if self._stdin_fd is not None:
                        import os as _os

                        # Read a small burst
                        piece = _os.read(self._stdin_fd, max_chunk)
                    else:
                        ch = sys.stdin.read(1)
                        piece = ch.encode("latin1", errors="ignore") if ch else b""
                    if piece:
                        buf.extend(piece)

                # Consume escape sequences in place; the remaining bytes are the payload
                await self._drain_escape_bytes(buf)

                # Command mode (issue #88): keystrokes feed the local line
                # editor with echo; nothing reaches the connection until Enter
                # submits a complete command line.
                if self.command_mode:
                    await self._drain_command_input(buf)
                    continue

                # Decide when to flush: when payload exists and enough time has elapsed
                now = asyncio.get_event_loop().time()
                payload = self._prepare_payload_for_send(bytes(buf))
                buf.clear()
                should_flush = bool(payload) and (
                    (now - last_send >= flush_interval) or (len(payload) >= max_chunk) or (b"\n" in payload)
                )

                if should_flush:
                    sent_now, last_send = await self._flush_payload(payload, now, last_send)

                # If nothing to send, keep looping
                if not sent_now:
                    continue

        except Exception as e:
            self.logger.error("Error handling keyboard input: %s", e, exc_info=True)
            self.is_running = False

    async def _drain_escape_bytes(self, buf: bytearray) -> None:
        """Remove escape-sequence bytes from `buf` in place.

        The byte is dropped when `_handle_escape_sequence` consumed it
        (statefully); the remaining bytes in order form the flushable payload.
        """
        i = 0
        while i < len(buf):
            b = buf[i : i + 1]
            # Try escape handler; it may consume statefully
            if await self._handle_escape_sequence(b):
                del buf[i : i + 1]
                # Do not include this byte in payload
                continue
            i += 1

    async def _drain_command_input(self, buf: bytearray) -> None:
        """Feed buffered keystrokes (escape bytes removed) to the command line editor."""
        while buf:
            b = bytes(buf[:1])
            del buf[:1]
            await self._command_key_byte(b)

    async def _command_key_byte(self, b: bytes) -> None:
        """Handle one command-mode keystroke: local echo, line editing, submit (issue #88).

        Ctrl+C cancels the current line (it never ends the session here);
        Backspace/Delete erase one echoed character; Enter submits (a blank
        line re-prompts and sends nothing); other control bytes are dropped.
        """
        if not b:
            return
        if b == b"\x03":  # Ctrl+C cancels the line
            self._cmd_line.clear()
            sys.stdout.write("\r\n" + COMMAND_PROMPT)
            sys.stdout.flush()
            return
        if b in (b"\x7f", b"\x08"):  # Backspace / Delete
            if self._cmd_line:
                self._cmd_line.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            return
        if b in (b"\r", b"\n"):
            sys.stdout.write("\r\n")
            sys.stdout.flush()
            line = bytes(self._cmd_line).decode("utf-8", errors="ignore").strip()
            self._cmd_line.clear()
            if not line:
                # Blank line: deliberate no-op, re-prompt, zero wire bytes.
                sys.stdout.write(COMMAND_PROMPT)
                sys.stdout.flush()
                return
            await self._handle_command_line(line)
            return
        if 0x20 <= b[0] < 0x7F or b[0] >= 0x80:  # printable (incl. UTF-8 lead/continuation bytes)
            self._cmd_line.extend(b)
            sys.stdout.buffer.write(b)
            sys.stdout.buffer.flush()
            return
        # Every other control byte is dropped in command mode.

    async def _handle_command_line(self, line: str) -> None:
        """Interpret one submitted command-mode line and act on it (issue #88).

        Wire forms (``LIST``, ``CONNECT:<port>``, ``DISCONNECT``, ``QUIT``,
        ``POWER ...``) pass through unchanged; word forms are case-insensitive
        sugar forwarded as the same wire lines. Unknown lines are refused
        locally and never reach the server. The full vocabulary lives in
        docs/design/cli_command_mode.md.
        """
        upper = line.upper()
        if upper in ("HELP", "H", "?"):
            self._write_command_help()
            return
        if upper in ("QUIT", "EXIT", "Q"):
            await self._esc_cmd_disconnect()
            return
        if upper in ("LIST", "L"):
            await self._send_command_line("LIST")
            return
        if upper == "DISCONNECT":
            self._write_notice(NOT_ATTACHED_NOTICE)
            return
        if upper in ("C", "CONNECT") or upper.startswith(("CONNECT:", "CONNECT ", "C ")):
            await self._command_connect(line)
            return
        if upper == "POWER" or upper.startswith("POWER "):
            await self._send_command_line("POWER" + line[5:])
            return
        self._write_notice(f'[Unknown command: "{line}" \u2014 type "help"]')

    async def _command_connect(self, line: str) -> None:
        """Normalize `connect <port>`, `c <port>` and `CONNECT:<port>` to the wire line."""
        if line.upper().startswith("CONNECT:"):
            target = line.split(":", 1)[1].strip()
        else:
            parts = line.split(maxsplit=1)
            target = parts[1].strip() if len(parts) > 1 else ""
        if not target:
            self._write_notice('[Usage: connect <port>]\r\n[Run "list" to see ports]')
            return
        await self._send_command_line(f"CONNECT:{target}")

    async def _send_command_line(self, text: str) -> None:
        """Forward one wire line from command mode and arm the quiet-stream re-prompt."""
        sent = False
        try:
            sent = bool(await self.connection.send_data(text + "\n"))
        except Exception:
            sent = False
        if not sent:
            self._write_notice("[Send failed - connection down]")
            return
        self._cmd_awaiting_reply = True
        self._cmd_quiet_polls = 0

    def _write_command_help(self) -> None:
        """Print the command-mode help text (issue #88) and re-prompt."""
        sys.stdout.write("\r\n" + "\r\n".join(COMMAND_MODE_HELP_LINES) + "\r\n" + COMMAND_PROMPT)
        sys.stdout.flush()

    def _write_notice(self, text: str) -> None:
        """Print bracketed local notice(s); a local command re-prompts immediately."""
        sys.stdout.write("\r\n" + text + "\r\n" + (COMMAND_PROMPT if self.command_mode else ""))
        sys.stdout.flush()

    def _prepare_payload_for_send(self, payload: bytes) -> bytes:
        """Normalize CR to LF if enabled, so newlines are consistent on the wire."""
        if self.normalize_crlf and b"\r" in payload:
            return payload.replace(b"\r", b"\n")
        return payload

    async def _flush_payload(self, payload: bytes, now: float, last_send: float) -> Tuple[bool, float]:
        """Send `payload` (or re-announce the read-only restriction); return (sent, last_send).

        `last_send` is updated only when something was sent or the read-only
        warning fired, so the flush-interval timer restarts on those events.
        """
        if not self.read_only_mode and self.connection.is_connected:
            if self._debug_input:
                self.logger.debug("send[chunk]: %s bytes", len(payload))
            await self.connection.send_data(bytes(payload))
            return True, now
        if self.read_only_mode and (b"\r" in payload or b"\n" in payload):
            # Re-announce the read-only restriction on every Enter press,
            # since the one-time startup/transition warning is easy to miss.
            sys.stdout.write("\r\n[WARNING: console is in read-only mode]\r\n")
            sys.stdout.flush()
            return False, now
        return False, last_send

    # Pending ESC completion removed

    def _is_data_available(self):
        """Return True if a byte is immediately readable from stdin.

        Uses ``select`` on POSIX systems; returns True unconditionally on other
        platforms where a non-blocking readiness probe is not implemented.
        """
        if os.name != "posix":
            return True  # Can't check on non-POSIX systems
        import select

        watch = self._stdin_fd if self._stdin_fd is not None else sys.stdin
        return select.select([watch], [], [], 0) == ([watch], [], [])

    async def _handle_escape_sequence(self, b: bytes) -> bool:
        """Interpret incremental escape sequence input.

        Args:
            b: Single input byte to evaluate.

        Returns:
            bool: True if the byte (and any buffered sequence) was fully
            consumed by escape handling logic and should not be forwarded to
            the remote; False if the caller should treat it as ordinary input.
        """
        if self.escape_state == 0:
            # First escape byte
            if b == self.escape_b1:
                self.escape_state = 1
                return True
            return False

        elif self.escape_state == 1:
            # Second escape byte
            if b == self.escape_b2:
                self.escape_state = 2
                return True
            else:
                # When disconnected, allow two-key escape: Ctrl+E + cmd
                if not self.connection.is_connected and b:
                    self.escape_state = 0
                    cmd = b.decode("latin1", errors="ignore")
                    await self._process_escape_command(cmd)
                    return True
                # Otherwise, treat as normal characters
                self.escape_state = 0
                if not self.read_only_mode:
                    await self.connection.send_data(self.escape_b1)
                    await self.connection.send_data(b)
                return True

        elif self.escape_state == 2:
            # Third byte is the command; decode single byte for command routing
            self.escape_state = 0
            cmd = b.decode("latin1", errors="ignore")
            await self._process_escape_command(cmd)
            return True

        return False

    async def _request_access_mode(self, method_name: str, unsupported_message: str):
        """Invoke an adapter access-mode control method and report failures.

        The actual grant/denial/holder confirmation is delivered asynchronously
        by the server and surfaced by `_read_from_server()` (via the adapter's
        `access_mode`/`rw_holders` state), so this only reports whether the
        request itself could be sent.

        Args:
            method_name: Name of the `BaseClientAdapter` method to call
                (`request_read_write`, `release_read_write`, `force_read_write`).
            unsupported_message: Message to show if the adapter lacks support
                or the request could not be sent.
        """
        method = getattr(self.connection, method_name, None)
        sent = False
        if method is not None:
            try:
                sent = bool(await method())
            except Exception:
                sent = False
        if not sent:
            sys.stdout.write(f"\r\n{unsupported_message}\r\n")
            sys.stdout.flush()

    async def _read_take_target_input(self) -> str:
        """Prompt for the client_id a write-slot takeover should target (issue #61).

        Mirrors the telnet/SSH `f` prompt. Each byte echoes as typed; the
        first CR/LF ends the input. The client_id is the id shown by `w`
        in its ``[id] username@ip (rw)`` holder label. Enter sends the
        no-target fallback (the most recently attached other holder).

        Returns:
            str: The target id, or "" for the no-target fallback.
        """
        sys.stdout.write("[Take from holder (client_id, or Enter for latest): ]")
        sys.stdout.flush()
        target = ""
        while True:
            await asyncio.sleep(0.01)
            if not self._is_data_available():
                continue
            if self._stdin_fd is not None:
                import os as _os

                ch = _os.read(self._stdin_fd, 1).decode("latin1", errors="ignore")
            else:
                ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                return target
            if ch in ("\x7f", "\b"):
                if target:
                    target = target[:-1]
                sys.stdout.write("\b \b")
                sys.stdout.flush()
                continue
            target += ch
            sys.stdout.write(ch)
            sys.stdout.flush()

    async def _power_menu(self):
        """Interactive per-console power (PDU) menu.

        Lists only this console's feeds with on/off tags; a number toggles
        that feed, ``a`` toggles every feed, and Enter leaves. Switches are a
        server round-trip; the menu waits for the reply and a settled stream.
        """
        port_name = await self._power_menu_feed_check()
        if port_name is None:
            return
        self._stream_exclusive = True
        try:
            feeds = await self._power_menu_load_feeds(port_name)
            if feeds is None:
                return
            while True:
                feeds = await self._power_menu_handle_entry(port_name, feeds)
                if feeds is None:
                    return
        finally:
            self._stream_exclusive = False

    async def _power_menu_feed_check(self) -> Optional[str]:
        """Check power control is available + a port is attached.

        Prints a notice and returns None when the connection lacks power
        methods or no port is attached (the caller bails silently). On
        success returns the attached port name so the menu knows what to
        address.
        """
        conn = self.connection
        if not (hasattr(conn, "request_power_feeds") and hasattr(conn, "switch_power_outlet")):
            sys.stdout.write("\r\n[Power control is not supported by this connection]\r\n")
            sys.stdout.flush()
            return None
        port_name = getattr(conn, "current_port", None) or getattr(conn, "port_name", None)
        if not port_name:
            sys.stdout.write("\r\n[Power needs an attached console (CONNECT first)]\r\n")
            sys.stdout.flush()
            return None
        return port_name

    async def _power_menu_load_feeds(self, port_name: str) -> Optional[List[Dict[str, Any]]]:
        """Send feed request, wait for reply, validate.

        Returns the feeds list on success; returns None on any failure
        (request failure, empty reply, wrong reply type, no feeds),
        printing the appropriate notice in each case.
        """
        conn = self.connection
        if not await conn.request_power_feeds():
            sys.stdout.write("\r\n[POWER: could not send the feed request]\r\n")
            sys.stdout.flush()
            return None
        reply = await self._await_power_reply()
        if not reply:
            msg = (
                "\r\n[POWER: server closed the connection]"
                if not conn.is_connected
                else "\r\n[POWER: power management is not configured]"
            )
            sys.stdout.write(msg + "\r\n")
            sys.stdout.flush()
            return None
        if reply.get("type") != "power_feeds" or not reply.get("ok", True):
            sys.stdout.write("\r\n[POWER: " + str(reply.get("error") or "power management is not configured") + "]\r\n")
            sys.stdout.flush()
            return None
        feeds = reply.get("feeds") or []
        if not feeds:
            sys.stdout.write(f"\r\n[POWER: {port_name} has no power feeds]\r\n")
            sys.stdout.flush()
            await self._read_power_line("> ")
            sys.stdout.write("\r\n[EXITING POWER]\r\n")
            sys.stdout.flush()
            return None
        return feeds

    async def _power_menu_handle_entry(self, port_name: str, feeds: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        """Render the menu + handle one user entry.

        Returns the updated feeds list to continue, or None to exit.
        """
        sys.stdout.write(f"\r\nPOWER: feeds for {port_name}  (a number = toggle, a = all, Enter = exit)\r\n")
        for i, f in enumerate(feeds, 1):
            sys.stdout.write("%2d  %s\r\n" % (i, self._power_menu_line(f.get("ref"), f.get("on"))))
        sys.stdout.flush()
        entry = await self._read_power_line("> ")
        if entry == "":
            sys.stdout.write("\r\n[EXITING POWER]\r\n")
            sys.stdout.flush()
            return None
        if entry in ("a", "all"):
            first = feeds[0].get("on")
            target = not bool(first)
            for f in feeds:
                await self._switch_power(feed=f, target=target, ref_after=f)
            return feeds
        if entry.isdigit():
            idx = int(entry)
            if 1 <= idx <= len(feeds):
                f = feeds[idx - 1]
                await self._switch_power(feed=f, target=not bool(f.get("on")), ref_after=f)
            else:
                sys.stdout.write(f"\r\nPOWER: number out of range (1-{len(feeds)})\r\n")
            return feeds
        sys.stdout.write("\r\nPOWER: enter a feed number, 'a' for all, or Enter to exit\r\n")
        sys.stdout.flush()
        return feeds

    async def _switch_power(self, feed: Dict[str, Any], target: bool, ref_after: Optional[Dict[str, Any]] = None) -> None:
        """Toggle one console feed to `target`; show the reply's state.

        Sends a `power_switch` frame and waits for the reply the background
        read loop captures (plain bytes such as the server's live `[POWER]`
        notice keep rendering in the meantime), then prints the
        `POWER <ref> -> <state>` confirmation. On a denial the reply's
        `error` line is shown and the feed state is left unchanged.
        """
        ref = feed.get("ref")
        since = self._stream_activity
        if not ref or not await self.connection.switch_power_outlet(ref, bool(target)):
            sys.stdout.write("\r\nPOWER: could not send the switch request\r\n")
            sys.stdout.flush()
            return
        reply = await self._await_power_reply(since=since)
        if not reply or reply.get("type") != "power_switch":
            sys.stdout.write(f"\r\nPOWER: no reply for {ref}\r\n")
            sys.stdout.flush()
            return
        if not reply.get("ok"):
            sys.stdout.write(f"\r\nERROR:POWER: {reply.get('error', 'switch failed')}\r\n")
            sys.stdout.flush()
            return
        state = "on" if reply.get("on") else "off"
        if ref_after is not None:
            ref_after["on"] = reply.get("on")
        sys.stdout.write(f"\r\nPOWER {ref} -> {state}\r\n")
        sys.stdout.flush()

    def _power_menu_line(self, ref: Any, on: Any) -> str:
        """Render one numbered feed row's state tag + ref (the number is the prefix)."""
        if on is True:
            return "[on]   " + str(ref)
        if on is False:
            return "[off]  " + str(ref)
        return "[unknown] " + str(ref)

    async def _await_power_reply(self, timeout: float = 6.0, since: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Wait for the next power OMXCTRL reply captured by the connection.

        The background read loop stays the sole reader of the stream, so this
        never reads the connection itself. It polls the reply object the
        adapter stores when it decodes a `power_feeds` / `power_switch` frame;
        plain bytes (such as the live `[POWER]` notice) keep flowing to the
        terminal through the background loop meanwhile. After the reply, the
        stream gets one short beat so a live notice sent next to it has
        rendered before the menu re-renders.

        Args:
            timeout: Maximum seconds to wait for the reply.
            since: ``_stream_activity`` value captured just before the
                matching request went out; a payload already rendered after
                it means the notice landed (and is on screen) already.

        Returns:
            The reply dict, or None if ``is_running`` stops, the connection
            drops, or the timeout elapses.
        """
        conn = self.connection
        conn.last_power_reply = None
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while self.is_running:
            reply = conn.last_power_reply
            if reply is not None:
                conn.last_power_reply = None
                await self._wait_stream_settle(since if since is not None else self._stream_activity)
                return reply
            if not conn.is_connected:
                return None
            if loop.time() > deadline:
                return None
            await asyncio.sleep(0.02)
        return None

    async def _wait_stream_settle(self, since: int) -> None:
        """Give the stream one short beat after a power reply.

        If the background render loop already delivered a payload since
        ``since`` (typically the live `[POWER]` notice the server sends next
        to the reply), it is on screen and we return at once. Otherwise wait
        up to ~0.35s for one to arrive. A chatty console that never goes
        quiet simply waits the beat and moves on.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 0.35
        while self.is_running:
            if self._stream_activity > since:
                return
            if loop.time() > deadline:
                return
            await asyncio.sleep(0.02)

    async def _read_power_line(self, prompt: str) -> str:
        """Prompt for one power-menu entry, echoing typed input (like telnet/SSH)."""
        sys.stdout.write(prompt)
        sys.stdout.flush()
        line = ""
        while True:
            await asyncio.sleep(0.01)
            if not self._is_data_available():
                continue
            if self._stdin_fd is not None:
                import os as _os

                ch = _os.read(self._stdin_fd, 1).decode("latin1", errors="ignore")
            else:
                ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                return line
            if ch in ("\x7f", "\b"):
                if line:
                    line = line[:-1]
                sys.stdout.write("\b \b")
                sys.stdout.flush()
                continue
            line += ch
            if line:
                sys.stdout.write(ch)
                sys.stdout.flush()

    async def _force_access_mode(self, target: Optional[str] = None):
        """Send a write-slot takeover request and report send failures.

        The grant/denial itself rides the asynchronous server response and is
        shown by `_read_from_server()` (via the adapter's `access_mode`
        state or its takeover notice), so this only reports whether the
        request itself could be sent.

        Args:
            target: Optional holder ``client_id`` to take the slot from
                (issue #61); ``None`` takes the most recently attached other
                holder. An adapter that predates the parameter falls back to
                the no-target form.
        """
        method = getattr(self.connection, "force_read_write", None)
        sent = False
        if method is not None:
            try:
                if target:
                    sent = bool(await method(target))
                else:
                    sent = bool(await method())
            except TypeError:
                try:
                    sent = bool(await method())
                except Exception:
                    sent = False
            except Exception:
                sent = False
        if not sent:
            sys.stdout.write("\r\n[Take control is not supported by this connection]\r\n")
            sys.stdout.flush()

    async def _process_escape_command(self, command: str):
        """Execute a parsed escape command.

        Args:
            command: Single-character command mnemonic following the two-byte
                escape introducer.
        """
        try:
            await self._esc_cmd_dispatch(command)
        except Exception as e:
            self.logger.error("Error processing escape command '%s': %s", command, e, exc_info=True)
            sys.stdout.write(f"\r\n[Error processing command: {e}]\r\n")
            sys.stdout.flush()

    async def _esc_cmd_dispatch(self, command: str) -> None:
        """Route one parsed escape command to its handler.

        Command mnemonics that only print a static notice (v, r, l, z, ^R)
        are looked up in the notices table first; the remaining commands map
        to handler methods. Anything unknown is reported and discarded.
        """
        notice = self._esc_cmd_notices().get(command)
        if notice is not None:
            await self._esc_cmd_static_notice(notice)
            return
        handler = self._esc_cmd_handlers().get(command)
        if handler is not None:
            await handler()
            return
        # Unknown command - discard
        sys.stdout.write(f"\r\n[Unknown command: {repr(command)}]\r\n")
        sys.stdout.flush()

    def _esc_cmd_notices(self) -> Dict[str, str]:
        """Static notice text per command mnemonic (computed per dispatch)."""
        return {
            # Show version
            "v": "\r\n[OpenMux Client v1.0]\r\n",
            # Replay lines
            "r": f"\r\n[Replay last {self.replay_lines} lines - not implemented]\r\n",
            # List break sequences or send break
            "l": "\r\n[Break sequences - not implemented]\r\n",
            # Suspend connection
            "z": "\r\n[Suspend not supported - use . to disconnect]\r\n",
            # Ctrl+R - Replay last line
            "\x12": "\r\n[Replay last line - not implemented]\r\n",
        }

    def _esc_cmd_handlers(self) -> Dict[str, Any]:
        """Mapping of command mnemonic to its async handler method."""
        return {
            ".": self._esc_cmd_disconnect,
            # Detach from the port and keep the session (issue #88): the server
            # returns the TCP session to its command phase (`mux> ` prompt).
            "d": self._esc_cmd_detach_port,
            "a": self._esc_cmd_request_write,
            "s": self._esc_cmd_release_write,
            "f": self._esc_cmd_take,
            # Information dump
            "i": self._show_info,
            # Who currently holds read-write access on this port
            "w": self._show_users,
            # PDU power control: interactive per-console feed menu. Same
            # behavior and wording as the telnet/SSH listeners' `p`
            # escape item (number = toggle, a = all, Enter = exit). The
            # old playback placeholders (p/P) are removed.
            "p": self._power_menu,
            # Set replay lines
            "R": self._esc_cmd_set_replay,
            # Manual reconnect
            "o": self._esc_cmd_reconnect,
            # Change escape sequence
            "e": self._esc_cmd_change_escape,
            # Continue - ignore the escape sequence
            "\r": self._esc_cmd_noop,
            "\n": self._esc_cmd_noop,
            # Show help
            "?": self._show_help,
            # Octal character input
            "\\": self._esc_cmd_octal,
        }

    async def _esc_cmd_static_notice(self, text: str) -> None:
        """Print one static escape-command notice."""
        sys.stdout.write(text)
        sys.stdout.flush()

    async def _esc_cmd_noop(self) -> None:
        """No-op handler: the continue commands ignore the escape sequence."""
        pass

    async def _esc_cmd_read_char(self) -> str:
        """Read one stdin character when data is available, else return "".

        Uses the raw _stdin_fd on POSIX when set, otherwise sys.stdin.
        """
        await asyncio.sleep(0.01)
        if self._is_data_available():
            if self._stdin_fd is not None:
                import os as _os

                return _os.read(self._stdin_fd, 1).decode("latin1", errors="ignore")
            return sys.stdin.read(1)
        return ""

    async def _esc_cmd_disconnect(self) -> None:
        """Disconnect: close the connection gracefully and stop the loop."""
        sys.stdout.write("\r\n[Disconnecting...]\r\n")
        sys.stdout.flush()
        # Gracefully close connection before stopping loop
        try:
            await self.connection.close()
        except Exception:
            # justification: shutdown cleanup; the adapter close is idempotent
            pass
        self.is_running = False

    async def _esc_cmd_detach_port(self) -> None:
        """Detach from the attached port and return to the `mux> ` prompt (issue #88).

        The server releases the port, answers the `detach` control frame with
        `detached`, and returns the session to its command phase. This handler
        never reads the stream itself: the adapter's read path stores the ack
        in `last_detach_reply` (single-reader rule, same shape as the power
        menu) and this handler only polls it.
        """
        if not getattr(self.connection, "current_port", None):
            sys.stdout.write(f"\r\n{NOT_ATTACHED_NOTICE}\r\n")
            sys.stdout.flush()
            return
        sent = False
        try:
            self.connection.last_detach_reply = None
            sent = bool(await self.connection.detach_port())
        except Exception:
            sent = False
        if not sent:
            sys.stdout.write("\r\n[Detach not supported on this connection]\r\n")
            sys.stdout.flush()
            return
        reply = await self._wait_detach_reply()
        if reply is None:
            sys.stdout.write("\r\n[Detach timed out]\r\n")
            sys.stdout.flush()
            return
        if not reply.get("ok"):
            sys.stdout.write("\r\n[Detach refused by server]\r\n")
            sys.stdout.flush()
            return
        port_name = str(reply.get("port") or getattr(self.connection, "current_port", "") or "port")
        try:
            # Clear the adapter's attach view so reconnect/info behave as unattached.
            self.connection.current_port = None
            self.connection.access_mode = None
        except Exception:
            # justification: adapter view mirror is best-effort; the detach already succeeded
            pass
        self._last_synced_access_mode = None
        self.read_only_mode = False
        sys.stdout.write(f"\r\n[Detached from {port_name}]\r\n")
        sys.stdout.flush()
        if getattr(self.connection, "supports_command_phase", False):
            # The server side is back in its command phase: offer the prompt
            # again so the user can pick another port without reconnecting.
            self.command_mode = True
            self._cmd_rx = b""
            self._cmd_awaiting_reply = False
            sys.stdout.write(COMMAND_PROMPT)
            sys.stdout.flush()

    async def _wait_detach_reply(self) -> Optional[Dict[str, Any]]:
        """Poll for the `detached` ack while the background loop stays the reader.

        Returns the ack dict, or None on timeout or disconnect. Mirrors the
        power menu's reply wait (20 ms poll, bounded) and never calls read_data.
        """
        deadline = 5.0
        waited = 0.0
        while waited < deadline:
            reply = getattr(self.connection, "last_detach_reply", None)
            if reply is not None:
                return reply
            if not self.connection.is_connected:
                return None
            await asyncio.sleep(0.02)
            waited += 0.02
        return None

    async def _esc_cmd_request_write(self) -> None:
        """Request read-write access from the server."""
        await self._request_access_mode("request_read_write", "[Read-write access is not supported by this connection]")

    async def _esc_cmd_release_write(self) -> None:
        """Release read-write access; switch to read-only (spy) mode."""
        await self._request_access_mode("release_read_write", "[Release read-write is not supported by this connection]")

    async def _esc_cmd_take(self) -> None:
        """Take the port's write slot (issue #59 Part 2, issue #61).

        Pick the holder by client_id, or Enter for the most recently
        attached holder. The server demotes that one holder and promotes
        this client, restoring the holder if the promotion fails.
        """
        target = await self._read_take_target_input()
        await self._force_access_mode(target or None)

    async def _esc_cmd_set_replay(self) -> None:
        """Prompt for and set the replay-lines count (digits only)."""
        sys.stdout.write("\r\n[Set replay lines: ")
        sys.stdout.flush()
        try:
            num_str = ""
            while True:
                ch = await self._esc_cmd_read_char()
                if not ch:
                    continue
                if ch == "\r" or ch == "\n":
                    break
                elif ch.isdigit():
                    num_str += ch
                    sys.stdout.write(ch)
                    sys.stdout.flush()

            if num_str:
                self.replay_lines = int(num_str)
                sys.stdout.write(f"]\r\n[Replay lines set to {self.replay_lines}]\r\n")
            else:
                sys.stdout.write("]\r\n[Cancelled]\r\n")
            sys.stdout.flush()
        except ValueError:
            sys.stdout.write("]\r\n[Invalid number]\r\n")
            sys.stdout.flush()

    async def _esc_cmd_reconnect(self) -> None:
        """Manual reconnect to the session."""
        if not self.quiet_mode:
            sys.stdout.write("\r\n[Reconnecting...]\r\n")
            sys.stdout.flush()
        try:
            ok = await getattr(self.connection, "reconnect")()
        except Exception as e:
            self.logger.error("Reconnect error: %s", e, exc_info=True)
            ok = False
        await self._esc_cmd_reconnect_notify(ok)

    async def _esc_cmd_reconnect_notify(self, ok: bool) -> None:
        """Report the reconnect outcome; reset the disconnect flag on success."""
        if ok:
            if not self.quiet_mode:
                sys.stdout.write("[Reconnected]\r\n")
                sys.stdout.flush()
            self._notified_disconnect = False
        else:
            if not self.quiet_mode:
                sys.stdout.write("[Reconnect failed]\r\n")
                sys.stdout.flush()

    async def _esc_cmd_change_escape(self) -> None:
        """Read two characters and set them as the new escape sequence."""
        sys.stdout.write("\r\n[Enter new escape sequence (2 chars): ")
        sys.stdout.flush()
        try:
            chars = ""
            for i in range(2):
                while True:
                    ch = await self._esc_cmd_read_char()
                    if not ch:
                        continue
                    chars += ch
                    if ord(ch) < 32:
                        sys.stdout.write(f"^{chr(ord(ch) + 64)}")
                    else:
                        sys.stdout.write(ch)
                    sys.stdout.flush()
                    break

            self.escape_char1 = chars[0]
            self.escape_char2 = chars[1]
            # Refresh byte equivalents used by the input handler
            self.escape_b1 = self.escape_char1.encode("latin1", errors="ignore")
            self.escape_b2 = self.escape_char2.encode("latin1", errors="ignore")
            sys.stdout.write("]\r\n[Escape sequence changed]\r\n")
            sys.stdout.flush()
        except Exception as e:
            # Real user-facing error; log with traceback for diagnostics.
            self.logger.error("Failed to change escape sequence: %s", e, exc_info=True)
            sys.stdout.write("]\r\n[Error changing escape sequence]\r\n")
            sys.stdout.flush()

    async def _esc_cmd_octal(self) -> None:
        """Read three octal digits and send the corresponding single byte."""
        sys.stdout.write("\r\n[Enter 3 octal digits: ")
        sys.stdout.flush()
        try:
            octal_str = ""
            for i in range(3):
                while True:
                    ch = await self._esc_cmd_read_char()
                    if not ch:
                        continue
                    if ch.isdigit() and ch in "01234567":
                        octal_str += ch
                        sys.stdout.write(ch)
                        sys.stdout.flush()
                        break

            octal_val = int(octal_str, 8)
            sys.stdout.write("]\r\n")
            sys.stdout.flush()

            if not self.read_only_mode:
                # Send exact single byte corresponding to octal value
                await self.connection.send_data(bytes([octal_val]))

        except (ValueError, OverflowError):
            sys.stdout.write("]\r\n[Invalid octal value]\r\n")
            sys.stdout.flush()

    async def _show_help(self):
        """Display the list of supported escape commands."""
        help_text = (
            "\r\n[OpenMux Console Commands]\r\n"
            + ".         disconnect\r\n"
            + "d         detach from the port (return to the mux> prompt)\r\n"
            + "a         request read-write access\r\n"
            + "f         take the write slot (prompt: holder id, or Enter for latest)\r\n"
            + "s         release read-write access / switch to read-only\r\n"
            + "i         information dump\r\n"
            + "w         show who holds read-write access on this port\r\n"
            + "v         show version\r\n"
            + "p         power (this console's feeds: number = toggle, a = all)\r\n"
            + "r         replay last N lines [not implemented]\r\n"
            + "R         set number of replay lines\r\n"
            + "l         list break sequences [not implemented]\r\n"
            + "o         reconnect to session\r\n"
            + "z         suspend (not supported)\r\n"
            + "e         change escape sequence\r\n"
            + "^M        continue (ignore escape)\r\n"
            + "^R        replay last line [not implemented]\r\n"
            + "\\ooo      send octal character\r\n"
            + "?         show this help\r\n"
        )
        sys.stdout.write(help_text)
        sys.stdout.flush()

    async def _show_info(self):
        """Display current connection and mode status summary."""
        access_mode = getattr(self.connection, "access_mode", None)
        if access_mode is not None:
            mode_text = "Read-Write" if access_mode == "read-write" else "Read-Only"
        else:
            mode_text = "Read-Only (assumed)" if self.read_only_mode else "Read-Write (assumed)"
        info = (
            "\r\n[Connection Information]\r\n"
            + f"Server: {self.connection.host}:{self.connection.port}\r\n"
            + f"Mode: {mode_text}\r\n"
            + f"Connected: {self.connection.is_connected}\r\n"
            + f"Authenticated: {self.connection.is_authenticated}\r\n"
            + f'Port: {getattr(self.connection, "current_port", "Unknown")}\r\n'
            + f"Escape Sequence: {repr(self.escape_char1 + self.escape_char2)}\r\n"
        )
        sys.stdout.write(info)
        sys.stdout.flush()

    async def _show_users(self):
        """Query and display the current read-write holder(s) of this port."""
        method = getattr(self.connection, "query_rw_holders", None)
        sent = False
        if method is not None:
            try:
                sent = bool(await method())
            except Exception:
                sent = False
        if not sent:
            sys.stdout.write("\r\n[User information not available from server]\r\n")
            sys.stdout.flush()
        # If sent, the server's asynchronous rw_holders response is displayed
        # by _read_from_server() when it arrives.
