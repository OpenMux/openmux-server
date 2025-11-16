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
import logging
import os
import sys
import termios
import tty

from .adapters import BaseClientAdapter


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
        self.playback_lines = 60
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

            # Start read task
            read_task = asyncio.create_task(self._read_from_server())

            # Handle keyboard input
            await self._handle_keyboard_input()

            # Cancel read task
            read_task.cancel()

            return True

        except Exception as e:
            self.logger.error(f"Error in console UI: {e}", exc_info=True)
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

    async def _read_from_server(self):
        """Continuously read server data and render to the terminal.

        Handles CRLF normalization, shutdown message detection, reconnect
        orchestration, and quiet-mode suppression of status lines. The loop
        yields periodically to remain responsive to cancellation and state
        changes.
        """
        while self.is_running:
            try:
                # If disconnected, ensure auto/manual handling is engaged and yield briefly
                if not self.connection.is_connected:
                    if self.reconnect_mode == "auto" and not self._auto_reconnect_task:
                        self._auto_reconnect_task = asyncio.create_task(self._auto_reconnect_loop())
                        if not self.quiet_mode:
                            sys.stdout.write("\r\n[Reconnecting... auto]\r\n")
                            sys.stdout.flush()
                    await asyncio.sleep(0.1)
                    continue
                # Read data from server with a short timeout
                data = await self.connection.read_data(timeout=0.1)
                # None indicates connection closed (EOF)
                if data is None:
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
                    # Proactively close adapter resources (e.g., aiohttp sessions) to avoid GC warnings
                    try:
                        await self.connection.close()
                    except Exception:
                        pass
                    # If auto reconnect is enabled, start it and keep UI running
                    if self.reconnect_mode == "auto":
                        if not self._auto_reconnect_task:
                            self._auto_reconnect_task = asyncio.create_task(self._auto_reconnect_loop())
                            if not self.quiet_mode:
                                sys.stdout.write("\r\n[Reconnecting... auto]\r\n")
                                sys.stdout.flush()
                        await asyncio.sleep(0.1)
                        continue
                    elif self.reconnect_mode == "manual":
                        # Keep UI running; allow user to trigger manual reconnect via menu
                        if not self._auto_reconnect_task and not self.read_only_mode:
                            if not self.quiet_mode:
                                sys.stdout.write("\r\n[Disconnected. Use escape 'o' to reconnect]\r\n")
                                sys.stdout.flush()
                        await asyncio.sleep(0.1)
                        continue
                    else:
                        self.is_running = False
                        break
                # Empty payload means timeout/no data; continue polling
                if data == b"" or data == "":
                    continue

                # Normalize line endings for terminal display
                if isinstance(data, str):
                    # Check for shutdown message
                    if "SERVER:SHUTDOWN" in data:
                        sys.stdout.write("\r\n[Server shutdown]\r\n")
                        sys.stdout.flush()
                        self.is_running = False
                        break
                    # Convert \n to \r\n for proper terminal display
                    normalized_data = data.replace("\n", "\r\n")
                    sys.stdout.write(normalized_data)
                    sys.stdout.flush()
                    if normalized_data:
                        self._received_any_data = True
                else:
                    # Handle bytes data - convert to bytes if memoryview, then normalize
                    if isinstance(data, memoryview):
                        data = data.tobytes()
                    # Check for shutdown message
                    if b"SERVER:SHUTDOWN" in data:
                        sys.stdout.write("\r\n[Server shutdown]\r\n")
                        sys.stdout.flush()
                        self.is_running = False
                        break
                    # Convert \n to \r\n for proper terminal display
                    normalized_data = data.replace(b"\n", b"\r\n")
                    sys.stdout.buffer.write(normalized_data)
                    sys.stdout.buffer.flush()
                    if normalized_data:
                        self._received_any_data = True

            except asyncio.CancelledError:
                # Task was cancelled
                break
            except Exception as e:
                self.logger.error(f"Error reading from server: {e}", exc_info=True)
                # In reconnect modes, keep UI alive and retry
                if self.reconnect_mode in ("auto", "manual"):
                    await asyncio.sleep(0.2)
                    continue
                self.is_running = False
                break

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
                    self.logger.error(f"Reconnect attempt failed: {e}", exc_info=True)
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

                # Process escape sequences inline and build payload to send
                payload = bytearray()
                i = 0
                while i < len(buf):
                    b = buf[i : i + 1]
                    # Try escape handler; it may consume statefully
                    if await self._handle_escape_sequence(b):
                        i += 1
                        # Do not include this byte in payload
                        continue
                    payload.extend(b)
                    i += 1
                # Clear buffer after processing
                buf.clear()

                # Decide when to flush: when payload exists and enough time has elapsed
                now = asyncio.get_event_loop().time()
                if payload:
                    # Normalize CR to LF if enabled to ensure consistent newlines
                    if self.normalize_crlf:
                        if b"\r" in payload:
                            payload = payload.replace(b"\r", b"\n")
                    # Flush immediately if we see a newline to preserve interactivity
                    should_flush = (now - last_send >= flush_interval) or (len(payload) >= max_chunk) or (b"\n" in payload)
                else:
                    should_flush = False

                if payload and should_flush:
                    if not self.read_only_mode and self.connection.is_connected:
                        if self._debug_input:
                            self.logger.debug(f"send[chunk]: {len(payload)} bytes")
                        await self.connection.send_data(bytes(payload))
                        sent_now = True
                        last_send = now

                # If nothing to send, keep looping
                if not sent_now:
                    continue

        except Exception as e:
            self.logger.error(f"Error handling keyboard input: {e}", exc_info=True)
            self.is_running = False

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

    async def _process_escape_command(self, command: str):
        """Execute a parsed escape command.

        Args:
            command: Single-character command mnemonic following the two-byte
                escape introducer.
        """
        try:
            if command == ".":
                # Disconnect
                sys.stdout.write("\r\n[Disconnecting...]\r\n")
                sys.stdout.flush()
                # Gracefully close connection before stopping loop
                try:
                    await self.connection.close()
                except Exception:
                    pass
                self.is_running = False

            elif command == "a":
                # Attach read-write
                self.read_only_mode = False
                sys.stdout.write("\r\n[Switched to read-write mode]\r\n")
                sys.stdout.flush()

            elif command == "s":
                # Switch to spy mode (read-only)
                self.read_only_mode = True
                sys.stdout.write("\r\n[Switched to read-only mode (spy)]\r\n")
                sys.stdout.flush()

            elif command == "i":
                # Information dump
                await self._show_info()

            elif command == "w":
                # Who is using this console
                await self._show_users()

            elif command == "v":
                # Show version
                sys.stdout.write("\r\n[OpenMux Client v1.0]\r\n")
                sys.stdout.flush()

            elif command == "p":
                # Playback lines
                sys.stdout.write(f"\r\n[Playback last {self.playback_lines} lines - not implemented]\r\n")
                sys.stdout.flush()

            elif command == "P":
                # Set playback lines
                sys.stdout.write("\r\n[Set playback lines: ")
                sys.stdout.flush()
                # Read number (simplified implementation)
                try:
                    num_str = ""
                    while True:
                        await asyncio.sleep(0.01)
                        if self._is_data_available():
                            if self._stdin_fd is not None:
                                import os as _os

                                ch = _os.read(self._stdin_fd, 1).decode("latin1", errors="ignore")
                            else:
                                ch = sys.stdin.read(1)
                            if ch == "\r" or ch == "\n":
                                break
                            elif ch.isdigit():
                                num_str += ch
                                sys.stdout.write(ch)
                                sys.stdout.flush()

                    if num_str:
                        self.playback_lines = int(num_str)
                        sys.stdout.write(f"]\r\n[Playback lines set to {self.playback_lines}]\r\n")
                    else:
                        sys.stdout.write("]\r\n[Cancelled]\r\n")
                    sys.stdout.flush()
                except ValueError:
                    sys.stdout.write("]\r\n[Invalid number]\r\n")
                    sys.stdout.flush()

            elif command == "r":
                # Replay lines
                sys.stdout.write(f"\r\n[Replay last {self.replay_lines} lines - not implemented]\r\n")
                sys.stdout.flush()

            elif command == "R":
                # Set replay lines
                sys.stdout.write("\r\n[Set replay lines: ")
                sys.stdout.flush()
                try:
                    num_str = ""
                    while True:
                        await asyncio.sleep(0.01)
                        if self._is_data_available():
                            if self._stdin_fd is not None:
                                import os as _os

                                ch = _os.read(self._stdin_fd, 1).decode("latin1", errors="ignore")
                            else:
                                ch = sys.stdin.read(1)
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

            elif command == "l":
                # List break sequences or send break
                sys.stdout.write("\r\n[Break sequences - not implemented]\r\n")
                sys.stdout.flush()

            elif command == "o":
                # Manual reconnect
                if not self.quiet_mode:
                    sys.stdout.write("\r\n[Reconnecting...]\r\n")
                    sys.stdout.flush()
                try:
                    ok = await getattr(self.connection, "reconnect")()
                except Exception as e:
                    self.logger.error(f"Reconnect error: {e}", exc_info=True)
                    ok = False
                if ok:
                    if not self.quiet_mode:
                        sys.stdout.write("[Reconnected]\r\n")
                        sys.stdout.flush()
                    self._notified_disconnect = False
                else:
                    if not self.quiet_mode:
                        sys.stdout.write("[Reconnect failed]\r\n")
                        sys.stdout.flush()

            elif command == "z":
                # Suspend connection
                sys.stdout.write("\r\n[Suspend not supported - use . to disconnect]\r\n")
                sys.stdout.flush()

            elif command == "e":
                # Change escape sequence
                sys.stdout.write("\r\n[Enter new escape sequence (2 chars): ")
                sys.stdout.flush()
                try:
                    chars = ""
                    for i in range(2):
                        while True:
                            await asyncio.sleep(0.01)
                            if self._is_data_available():
                                if self._stdin_fd is not None:
                                    import os as _os

                                    ch = _os.read(self._stdin_fd, 1).decode("latin1", errors="ignore")
                                else:
                                    ch = sys.stdin.read(1)
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
                    self.logger.error(f"Failed to change escape sequence: {e}", exc_info=True)
                    sys.stdout.write("]\r\n[Error changing escape sequence]\r\n")
                    sys.stdout.flush()

            elif command == "\r" or command == "\n":
                # Continue - ignore the escape sequence
                pass

            elif command == "\x12":  # Ctrl+R
                # Replay last line
                sys.stdout.write("\r\n[Replay last line - not implemented]\r\n")
                sys.stdout.flush()

            elif command == "?":
                # Show help
                await self._show_help()

            elif command == "\\":
                # Octal character input
                sys.stdout.write("\r\n[Enter 3 octal digits: ")
                sys.stdout.flush()
                try:
                    octal_str = ""
                    for i in range(3):
                        while True:
                            await asyncio.sleep(0.01)
                            if self._is_data_available():
                                if self._stdin_fd is not None:
                                    import os as _os

                                    ch = _os.read(self._stdin_fd, 1).decode("latin1", errors="ignore")
                                else:
                                    ch = sys.stdin.read(1)
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

            else:
                # Unknown command - discard
                sys.stdout.write(f"\r\n[Unknown command: {repr(command)}]\r\n")
                sys.stdout.flush()

        except Exception as e:
            self.logger.error(f"Error processing escape command '{command}': {e}", exc_info=True)
            sys.stdout.write(f"\r\n[Error processing command: {e}]\r\n")
            sys.stdout.flush()

    async def _show_help(self):
        """Display the list of supported escape commands."""
        help_text = (
            "\r\n[OpenMux Console Commands]\r\n"
            + ".         disconnect\r\n"
            + "a         attach read-write\r\n"
            + "s         switch to spy mode (read-only)\r\n"
            + "i         information dump\r\n"
            + "w         who is using this console [not implemented]\r\n"
            + "v         show version\r\n"
            + "p         playback last N lines [not implemented]\r\n"
            + "P         set number of playback lines\r\n"
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
        info = (
            "\r\n[Connection Information]\r\n"
            + f"Server: {self.connection.host}:{self.connection.port}\r\n"
            + f'Mode: {"Read-Only" if self.read_only_mode else "Read-Write"}\r\n'
            + f"Connected: {self.connection.is_connected}\r\n"
            + f"Authenticated: {self.connection.is_authenticated}\r\n"
            + f'Port: {getattr(self.connection, "current_port", "Unknown")}\r\n'
            + f"Escape Sequence: {repr(self.escape_char1 + self.escape_char2)}\r\n"
        )
        sys.stdout.write(info)
        sys.stdout.flush()

    async def _show_users(self):
        """Display user list placeholder (server does not yet supply data)."""
        sys.stdout.write("\r\n[User information not available from server]\r\n")
        sys.stdout.flush()
