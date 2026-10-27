"""CLI and Telnet sessions: real protocol code, one reader thread each, a sync face for tests.

Every attached surface needs a live reader for its whole life. The server delivers data through a
per-client queue that drops the oldest chunk when it fills, and delivers control frames one client
after another, so a session whose bytes nobody reads can delay another terminal and lose its own
bytes. The reader thread here is what prevents both.

These classes are safe to call from sync test code: the async CLI adapter runs on its own loop in a
daemon thread, and the Telnet socket is read by a daemon thread. A test never sees either.
"""

from __future__ import annotations

import asyncio
import json
import queue
import socket
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from openmux.client.adapters.tcp_adapter import TcpClientAdapter

from .harness import TEST_PASSWORD, E2EServer
from .story import READER_QUEUE_CAP, Timeline

#: Bytes the client listener sends before a session may issue a command.
AUTH_BANNER = b"Authentication required"

IAC = 255
WILL = 252
WONT = 251
DO = 253
DONT = 254
SB = 250
SE = 240

#: Default two-byte prefix that opens the Control menu: Ctrl+E then ``c``.
DEFAULT_ESCAPE_SEQUENCE = b"\x05c"

CONTROL_MENU_HELP_MARKER = "OpenMux Control Menu"


class _WireTee:
    """Wraps an ``asyncio.StreamReader`` and records every byte read through it.

    The real adapter keeps all protocol logic; this only copies what it reads, so a test can assert
    on an exact wire line such as ``CONNECTED:loop1:READ_WRITE`` instead of on a parsed bool.
    """

    def __init__(self, reader: Any, sink: Deque[bytes]):
        self._reader = reader
        self._sink = sink

    def __getattr__(self, name: str) -> Any:
        return getattr(self._reader, name)

    def _record(self, data: Any) -> Any:
        if isinstance(data, (bytes, bytearray)) and data:
            self._sink.append(bytes(data))
        return data

    async def read(self, n: int = -1) -> bytes:
        return self._record(await self._reader.read(n))

    async def readline(self) -> bytes:
        return self._record(await self._reader.readline())

    async def readexactly(self, n: int) -> bytes:
        return self._record(await self._reader.readexactly(n))

    async def readuntil(self, separator: bytes = b"\n") -> bytes:
        return self._record(await self._reader.readuntil(separator))


class _SessionLoop:
    """One asyncio loop on a daemon thread, so sync tests can drive an async adapter.

    This keeps the event loop out of the pytest loop: the harness never shares a loop with the
    per-test loop that ``tests/conftest.py`` manages.
    """

    def __init__(self, name: str):
        self.name = name
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=10)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()

    def call(self, coro: Any, timeout: float = 30.0) -> Any:
        """Run one coroutine on this loop and wait for its result, from the test thread."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)

    def submit(self, coro_factory: Any) -> None:
        """Start a long-running coroutine on this loop without waiting."""
        self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(coro_factory(), loop=self.loop))

    def close(self) -> None:
        """Stop the loop and join the thread, so a leaked task cannot outlive the test."""
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(timeout=5.0)
        except RuntimeError:
            pass  # justification: the loop may already be closed by an earlier teardown
        finally:
            try:
                self.loop.close()
            except RuntimeError:
                pass  # justification: already closed


class CliSession:
    """A CLI session over the client listener, driven by the real ``TcpClientAdapter``.

    Using the product's client means a protocol change fails a test instead of being hidden by a
    second implementation of the same protocol in the harness.

    Args:
        server: The running server whose ``cli`` address to connect to.
        username: Configured user to authenticate as.
        surface: Timeline surface key, for example ``cli:holder``.
        timeline: Optional recorder; every byte and frame lands on it.
    """

    def __init__(
        self,
        server: E2EServer,
        username: str = "admin",
        password: str = TEST_PASSWORD,
        surface: str = "cli",
        timeline: Optional[Timeline] = None,
    ):
        self.server = server
        self.username = username
        self.password = password
        self.surface = surface
        self.timeline = timeline
        self.port_name: Optional[str] = None
        self.attached = False
        self.closed = False
        self.reader_paused = False
        self.reader_overflow: Optional[str] = None
        self._reader_started = False
        self._loop = _SessionLoop(f"{surface}-loop")
        self._adapter = _RecordingAdapter("127.0.0.1", server.port_of("cli"))
        self._chunks: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._seen = bytearray()
        self._wire_lock = threading.Lock()

    # --- connect and attach -------------------------------------------------------------

    def connect(self, timeout: float = 20.0) -> "CliSession":
        """Open the socket and authenticate with the password method.

        The background reader starts in ``attach``, not here. ``list_ports`` and ``connect_to_port``
        read their own replies from the same ``StreamReader``, so a reader started earlier would
        steal those bytes and the command would report an empty answer.

        Raises:
            AssertionError: when the banner or authentication fails, with the wire bytes attached.
        """
        if not self._loop.call(self._adapter.connect(), timeout=timeout):
            raise AssertionError(
                f"{self.surface}: no {AUTH_BANNER.decode()!r} banner from 127.0.0.1:{self.server.port_of('cli')}"
            )
        if not self._loop.call(self._adapter.authenticate_with_password(self.username, self.password), timeout=timeout):
            raise AssertionError(f"{self.surface}: authentication failed for user {self.username!r}\n{self.wire_text()}")
        return self

    def attach(self, *, port: str, timeout: float = 20.0) -> "CliSession":
        """Attach to one port, and fail when the server refuses.

        The granted mode is whatever the server decided; read it back with ``mode()`` rather than
        assuming the first seat is free. Attaching starts the reader, because from here on the
        server sends unsolicited data and frames.
        """
        if not self._loop.call(self._adapter.connect_to_port(port), timeout=timeout):
            raise AssertionError(f"{self.surface}: refused to attach to {port!r}\n{self.wire_text()}")
        self.port_name = port
        self.attached = True
        if not self._reader_started:
            self._reader_started = True
            self._loop.submit(self._read_forever)
        return self

    def list_ports(self, timeout: float = 20.0) -> List[str]:
        """Ask the command phase for the port list, on a session that is not attached."""
        ports = self._loop.call(self._adapter.list_ports(), timeout=timeout)
        return [str(entry) for entry in ports or []]

    # --- state ---------------------------------------------------------------------------

    def mode(self) -> Optional[str]:
        """The mode the server granted on this session's port: ``read-write`` or ``read-only``."""
        return self._adapter.access_mode

    def holders(self) -> List[str]:
        """The read-write holder labels the server last reported to this session."""
        return [str(label) for label in (self._adapter.rw_holders or [])]

    def frames(self, frame_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Decoded ``OMXCTRL`` frames this session received, oldest first."""
        frames = list(self._adapter.captured_frames)
        if frame_type is None:
            return frames
        return [frame for frame in frames if frame.get("type") == frame_type]

    def last_frame(self, frame_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The newest matching frame, or None when none arrived yet."""
        frames = self.frames(frame_type)
        return frames[-1] if frames else None

    def wait_for_frame(self, frame_type: str, code: Optional[str] = None, timeout: float = 10.0) -> Dict[str, Any]:
        """Wait for one frame of ``frame_type`` (and one Outcome code) and return it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for frame in self.frames(frame_type):
                if code is None or frame.get("code") == code:
                    return frame
            self._raise_reader_overflow()
            time.sleep(0.05)
        seen = [f"{f.get('type')}:{f.get('code')}" for f in self.frames()][-8:]
        raise AssertionError(f"{self.surface}: frame type={frame_type!r} code={code!r} never arrived; recent={seen}")

    def wire_text(self) -> str:
        """Everything read off the socket, as text. This is the wire evidence in a failure message."""
        with self._wire_lock:
            return b"".join(self._adapter.wire).decode("utf-8", errors="replace")

    # --- data ----------------------------------------------------------------------------

    def write(self, text: str, timeout: float = 20.0) -> None:
        """Send bytes to the attached port, verbatim. No newline is added unless the test writes one."""
        if not self.attached:
            raise AssertionError(f"{self.surface}: not attached to a port; call attach(port=...) first")
        payload = text.encode("utf-8")
        self._note(self.surface, "->", payload)
        if not self._loop.call(self._adapter.send_data(payload), timeout=timeout):
            raise AssertionError(f"{self.surface}: send failed for {text!r}")

    def write_line(self, text: str, timeout: float = 20.0) -> None:
        """Send one line with a trailing CR LF, which is what a terminal sends."""
        self.write(text + "\r\n", timeout=timeout)

    def read_text(self) -> str:
        """Everything received so far, as text. Reading does not consume: the text keeps growing."""
        self._raise_reader_overflow()
        while True:
            try:
                chunk = self._chunks.get_nowait()
            except queue.Empty:
                break
            if chunk is None:
                break
            self._seen.extend(chunk)
        return self._seen.decode("utf-8", errors="replace")

    def wait_for(self, needle: str, timeout: float = 10.0) -> str:
        """Wait until the received bytes contain ``needle``, and return all bytes received so far.

        Waits on a predicate with a deadline, never on a fixed sleep, so a slow adapter slows the
        run instead of breaking it.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.read_text()
            if needle in text:
                return text
            self._raise_reader_overflow()
            time.sleep(0.05)
        raise AssertionError(f"{self.surface}: {needle!r} never arrived\nreceived: {self.read_text()[-1200:]!r}")

    def echo_of(self, marker: str, timeout: float = 10.0) -> str:
        """Send ``marker`` and wait for the Loopback adapter to send it back."""
        self.write(marker)
        return self.wait_for(marker, timeout=timeout)

    def is_eof(self) -> bool:
        """True when the server closed this session."""
        return bool(self._adapter._e2e_eof)

    def wait_for_eof(self, timeout: float = 10.0) -> None:
        """Wait until the server closes this session, and fail when it stays open.

        Used where the product must drop a connection, for example the stop phase of a full reload.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._raise_reader_overflow()
            if self.is_eof():
                return
            time.sleep(0.05)
        raise AssertionError(
            f"{self.surface}: the server never closed the session in {timeout}s; received: {self.read_text()[-400:]!r}"
        )

    # --- control requests ---------------------------------------------------------------

    def request_read_write(self, timeout: float = 20.0) -> Dict[str, Any]:
        """Send ``request_rw`` and return the Access decision frame that answers it."""
        before = len(self._adapter.captured_frames)
        self._send(self._adapter.request_read_write(), timeout=timeout, note="request read-write access")
        return self._await_frame(("client_mode",), after=before, action="request read-write access")

    def release(self, timeout: float = 20.0) -> Dict[str, Any]:
        """Send ``release_rw``: give the Write slot up and stay attached."""
        before = len(self._adapter.captured_frames)
        self._send(self._adapter.release_read_write(), timeout=timeout, note="release write slot")
        return self._await_frame(("client_mode", "detached"), after=before, action="release the write slot")

    def takeover(self, target: Optional[str] = None, timeout: float = 20.0) -> Dict[str, Any]:
        """Send a Takeover (``force_promote``), optionally naming the holder to demote."""
        before = len(self._adapter.captured_frames)
        self._send(
            self._adapter.force_read_write(target=target), timeout=timeout, note=f"takeover target={target or 'latest'}"
        )
        return self._await_frame(("client_mode",), after=before, action="take the write slot")

    def query_holders(self, timeout: float = 20.0) -> Dict[str, Any]:
        """Send ``query_rw_holders`` and return the holders frame."""
        before = len(self._adapter.captured_frames)
        self._send(self._adapter.query_rw_holders(), timeout=timeout, note="query write slot holders")
        return self._await_frame(("rw_holders", "client_mode"), after=before, action="query the write slot holders")

    def request_power_feeds(self, timeout: float = 20.0) -> Dict[str, Any]:
        """Ask for this console's power feeds, as the ``p`` menu does."""
        self._send(self._adapter.request_power_feeds(), timeout=timeout, note="query power feeds")
        reply = self._wait_attr("last_power_reply", timeout=timeout)
        self._adapter.last_power_reply = None
        return reply

    def switch_power(self, ref: str, on: bool, timeout: float = 20.0) -> Dict[str, Any]:
        """Switch one feed of this console, through the control frame — not through REST."""
        self._adapter.last_power_reply = None
        self._send(
            self._adapter.switch_power_outlet(ref=ref, on=on), timeout=timeout, note=f"power {ref} -> {'on' if on else 'off'}"
        )
        reply = self._wait_attr("last_power_reply", timeout=timeout)
        return reply

    def detach(self, timeout: float = 20.0) -> Dict[str, Any]:
        """Detach from the port and return to the command phase; the session stays connected."""
        self._adapter.last_detach_reply = None
        self._send(self._adapter.detach_port(), timeout=timeout, note="detach from port")
        reply = self._wait_attr("last_detach_reply", timeout=timeout)
        self.attached = False
        self.port_name = None
        return reply

    # --- teardown and troubleshooting ---------------------------------------------------

    def close(self) -> None:
        """Close the session and stop its loop, so no task outlives the test."""
        if self.closed:
            return
        self.closed = True
        try:
            self._loop.call(self._adapter.close(), timeout=10.0)
        except Exception as exc:  # justification: teardown must not mask the test's own failure
            self._note(self.surface, "log", f"close failed: {type(exc).__name__}: {exc}")
        self._loop.close()

    def _send(self, coro: Any, timeout: float, note: str) -> None:
        self._note(self.surface, "->", note)
        if not self._loop.call(coro, timeout=timeout):
            raise AssertionError(f"{self.surface}: could not {note} (send failed)")

    def _await_frame(self, types: Tuple[str, ...], after: int, action: str, timeout: float = 10.0) -> Dict[str, Any]:
        """Wait for a frame that arrived after index ``after``, of one of ``types``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frames = list(self._adapter.captured_frames)
            for frame in frames[after:]:
                if frame.get("type") in types:
                    return frame
            self._raise_reader_overflow()
            time.sleep(0.05)
        raise AssertionError(f"{self.surface}: no reply frame when asked to {action}; frames={self._recent()}")

    def _wait_attr(self, attribute: str, timeout: float) -> Dict[str, Any]:
        """Wait for the adapter to store a structured reply in ``attribute``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = getattr(self._adapter, attribute, None)
            if value:
                return dict(value)
            self._raise_reader_overflow()
            time.sleep(0.05)
        raise AssertionError(f"{self.surface}: no reply in {attribute!r}; frames={self._recent()}")

    def _recent(self) -> List[str]:
        return [f"{frame.get('type')}:{frame.get('code')}" for frame in self.frames()][-8:]

    # --- reader thread -------------------------------------------------------------------

    async def _read_forever(self) -> None:
        """Drain the adapter's read path into a queue, for the whole life of the session."""
        while True:
            if self.reader_paused:
                await asyncio.sleep(0.2)
                continue
            try:
                chunk = await self._adapter.read_data(timeout=0.2)
            except Exception as exc:  # justification: recorded and surfaced on the test thread instead
                self.reader_overflow = f"read failed: {type(exc).__name__}: {exc}"
                return
            if chunk is None:
                self._adapter._e2e_eof = True
                self._chunks.put(None)
                self._note(self.surface, "log", "server closed this session")
                return
            if chunk:
                data = chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
                self._note(self.surface, "<-", data)
                if self._chunks.qsize() >= READER_QUEUE_CAP:
                    self.reader_overflow = (
                        f"{self.surface} received more than {READER_QUEUE_CAP} chunks that the test never read"
                    )
                    return
                self._chunks.put(data)

    def _raise_reader_overflow(self) -> None:
        """Turn a reader fault into a test failure that names the session at fault."""
        if self.reader_overflow:
            raise AssertionError(self.reader_overflow)

    def _note(self, surface: str, direction: str, payload: Any) -> None:
        if self.timeline is not None:
            self.timeline.record(surface, direction, payload)


class _RecordingAdapter(TcpClientAdapter):
    """The product's TCP client adapter, with the frames and wire bytes it consumes recorded.

    Only ``_process_control_frame`` and the reader are observed. All protocol decisions stay in the
    product's code, which is the point of using it.
    """

    def __init__(self, host: str, port: int):
        super().__init__(host, port)
        self.captured_frames: Deque[Dict[str, Any]] = deque(maxlen=500)
        self.wire: Deque[bytes] = deque(maxlen=2000)

    async def connect(self) -> bool:
        """Connect, then wrap the reader so every later read is copied into ``wire``."""
        ok = await super().connect()
        if ok and not isinstance(self.reader, _WireTee) and self.reader is not None:
            self.reader = _WireTee(self.reader, self.wire)
        return ok

    def _process_control_frame(self, data: bytes) -> bytes:
        try:
            body = data[len(b"\x00OMXCTRL ") :].split(b"\n", 1)[0]
            payload = json.loads(body.decode("utf-8", errors="replace"))
            if isinstance(payload, dict):
                self.captured_frames.append(payload)
        except Exception:  # justification: a frame the product cannot parse is the product's problem, not the recorder's
            pass
        return super()._process_control_frame(data)

    #: Set by the session's reader when the server closes the connection.
    _e2e_eof = False


class TelnetSession:
    """A Telnet listener session: a raw socket plus a minimal negotiation responder.

    This is deliberately not product code. Telnet is a line protocol, and a product change there
    must not break the test that proves the product behaviour, so the socket helper is written
    against the documented wire format.
    """

    def __init__(
        self,
        host: str,
        port: int,
        username: str = "admin",
        password: str = TEST_PASSWORD,
        surface: str = "telnet",
        timeline: Optional[Timeline] = None,
        require_auth: bool = False,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.surface = surface
        self.timeline = timeline
        self.require_auth = require_auth
        self.escape_sequence = DEFAULT_ESCAPE_SEQUENCE
        self.reader_paused = False
        self.reader_overflow: Optional[str] = None
        self.closed = False
        self._socket = socket.create_connection((host, port), timeout=10)
        self._chunks: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._seen = bytearray()
        self._thread = threading.Thread(target=self._read_forever, name=f"{surface}-reader", daemon=True)
        self._thread.start()
        if require_auth:
            self._authenticate()

    # --- auth and menu -------------------------------------------------------------------

    def _authenticate(self) -> None:
        """Answer the ``login:`` and ``Password:`` prompts, when the listener requires auth."""
        self.wait_for_prompt("login:", timeout=10.0)
        self.send_line(self.username)
        self.wait_for_prompt("assword:", timeout=10.0)
        self.send_line(self.password)

    def wait_for_prompt(self, *needles: str, timeout: float = 10.0) -> str:
        """Wait until the received text contains any of ``needles`` (case-insensitive)."""
        lowered = [needle.lower() for needle in needles]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.text().lower()
            if any(needle in text for needle in lowered):
                return self.text()
            self._raise_reader_overflow()
            time.sleep(0.05)
        raise AssertionError(f"{self.surface}: none of {needles} appeared\nreceived: {self.text()[-800:]!r}")

    def open_control_menu(self) -> str:
        """Send the escape sequence, which opens the Control menu on this session."""
        self.send_raw(self.escape_sequence, note=f"escape {self.escape_sequence!r}")
        return self.wait_for_prompt(CONTROL_MENU_HELP_MARKER, timeout=10.0)

    # --- bytes ---------------------------------------------------------------------------

    def send_raw(self, data: bytes, note: str = "") -> None:
        """Send bytes verbatim."""
        self._note("->", data if not note else note)
        self._socket.sendall(data)

    def send_line(self, text: str) -> None:
        """Send one CRLF-terminated line, which is what a Telnet client sends."""
        self.send_raw(text.encode("utf-8") + b"\r\n", note=f"line {text!r}")

    def press(self, key: str) -> str:
        """Send one Control menu key and return the text received so far."""
        self.send_raw(key.encode("utf-8"), note=f"menu key {key!r}")
        time.sleep(0.05)
        return self.text()

    def text(self) -> str:
        """Everything received so far, as text. Reading does not consume."""
        self._raise_reader_overflow()
        while True:
            try:
                chunk = self._chunks.get_nowait()
            except queue.Empty:
                break
            if chunk is None:
                break
            self._seen.extend(chunk)
        return self._seen.decode("utf-8", errors="replace")

    def wait_for(self, needle: str, timeout: float = 10.0) -> str:
        """Wait until the received bytes contain ``needle``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.text()
            if needle in text:
                return text
            self._raise_reader_overflow()
            time.sleep(0.05)
        raise AssertionError(f"{self.surface}: {needle!r} never arrived\nreceived: {self.text()[-800:]!r}")

    def text_since(self, mark: str) -> str:
        """The text after the last occurrence of ``mark``, for a reply that repeats earlier output."""
        text = self.text()
        return text.rsplit(mark, 1)[-1]

    # --- teardown ------------------------------------------------------------------------

    def close(self) -> None:
        """Close the socket and stop the reader thread."""
        if self.closed:
            return
        self.closed = True
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # justification: the peer may already have closed
        try:
            self._socket.close()
        except OSError:
            pass  # justification: closing an artifact is best effort
        self._thread.join(timeout=2.0)

    def kill_reader(self) -> None:
        """Stop reading on purpose, for the stalled-viewer test. The server keeps sending."""
        self.reader_paused = True

    # --- internals -----------------------------------------------------------------------

    def _read_forever(self) -> None:
        """Read until the socket closes, answering Telnet negotiation as it arrives."""
        while not self.closed:
            if self.reader_paused:
                time.sleep(0.2)
                continue
            try:
                data = self._socket.recv(4096)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                self._chunks.put(None)
                return
            if not data:
                self._chunks.put(None)
                self._note("log", "server closed this session")
                return
            self._note("<-", data)
            self._answer_negotiation(data)
            if self._chunks.qsize() >= READER_QUEUE_CAP:
                self.reader_overflow = f"{self.surface} received more than {READER_QUEUE_CAP} chunks that the test never read"
                return
            self._chunks.put(data)

    def _answer_negotiation(self, data: bytes) -> None:
        """Refuse every option the peer offers, so the session stays a plain byte stream.

        The Telnet listener does not negotiate on its own; this responder only keeps a third-party
        client from stalling if a proxy in front of the test does.
        """
        index = 0
        replies = bytearray()
        while index < len(data):
            if data[index] != IAC or index + 1 >= len(data):
                break
            command = data[index + 1]
            if command in (WILL, WONT) and index + 2 < len(data):
                replies += bytes([IAC, DONT if command == WILL else WILL, data[index + 2]])
                index += 3
                continue
            if command in (DO, DONT) and index + 2 < len(data):
                replies += bytes([IAC, WONT if command == DO else DO, data[index + 2]])
                index += 3
                continue
            if command == SB:
                end = data.find(bytes([IAC, SE]), index)
                if end == -1:
                    break
                index = end + 2
                continue
            index += 2
        if replies:
            try:
                self._socket.sendall(bytes(replies))
                self._note("->", bytes(replies))
            except OSError:
                pass  # justification: a negotiation reply to a dead socket carries no information

    def _raise_reader_overflow(self) -> None:
        if self.reader_overflow:
            raise AssertionError(self.reader_overflow)

    def _note(self, direction: str, payload: Any) -> None:
        if self.timeline is not None:
            self.timeline.record(self.surface, direction, payload)


def open_cli_client(
    server: E2EServer,
    *,
    user: str = "admin",
    password: str = TEST_PASSWORD,
    surface: str = "cli",
    attach_to: Optional[str] = None,
    timeline: Optional[Timeline] = None,
) -> CliSession:
    """Open and authenticate one CLI session, optionally attached to a port.

    Keeps one line in the test body, so the scenario reads as the sequence of events.
    """
    session = CliSession(server, username=user, password=password, surface=surface, timeline=timeline)
    session.connect()
    if attach_to:
        session.attach(port=attach_to)
    return session


def open_telnet_client(
    server: E2EServer,
    *,
    user: str = "admin",
    password: str = TEST_PASSWORD,
    surface: str = "telnet",
    require_auth: bool = False,
    timeline: Optional[Timeline] = None,
) -> TelnetSession:
    """Open one Telnet session on the server's Telnet listener."""
    return TelnetSession(
        "127.0.0.1",
        server.port_of("telnet"),
        username=user,
        password=password,
        surface=surface,
        timeline=timeline,
        require_auth=require_auth,
    )
