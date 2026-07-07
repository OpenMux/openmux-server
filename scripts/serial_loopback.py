#!/usr/bin/env python3
"""Standalone serial loopback utility for OpenMux testing.

Echoes all data received on a serial port back to the sender, applying the
same control-character sanitization and line-boundary annotation used by the
in-process LoopbackPort adapter.  Designed to sit on one end of a physical
or virtual (socat) null-modem pair while an OpenMux serial adapter connects
on the other.

Press the summary key (default Ctrl+T) from the remote side to receive a
status page showing port config, runtime counters, and current control line
states.

Usage
-----
    python scripts/serial_loopback.py --port /dev/ttyUSB0 --baud 115200
    python scripts/serial_loopback.py -p /dev/pts/3 --monitor-lines -v
    python scripts/serial_loopback.py --help

Null-modem pair with socat (for testing)
-----------------------------------------
    socat -d -d PTY,raw,echo=0 PTY,raw,echo=0
    # gives e.g. /dev/pts/4 and /dev/pts/5
    python scripts/serial_loopback.py -p /dev/pts/4
    # connect OpenMux serial adapter to /dev/pts/5
"""

import argparse
import asyncio
import logging
import os
import signal
import stat
import sys
import time
from typing import Optional


# ---------------------------------------------------------------------------
# ESC-sequence sanitizer (ported verbatim from loopback.py LoopbackPort)
# ---------------------------------------------------------------------------

class _EscSanitizer:
    """Stateful sanitizer: converts control/escape bytes to readable tokens.

    Maintains an internal ESC-sequence buffer so sequences split across
    consecutive read() chunks are handled correctly.
    """

    def __init__(self) -> None:
        self._esc_buf: bytearray = bytearray()

    def sanitize(self, data: bytes) -> bytes:
        """Return a safe-to-echo copy of *data*.

        - CR / LF preserved as-is.
        - Printable ASCII (0x20–0x7E) passed unchanged.
        - ESC cursor/navigation sequences → bracketed tags.
        - DEL (0x7F) → ``[DEL]``.
        - Other C0 controls → ``[CTRL-X]`` style tokens.
        - Bytes >= 0x80 passed through unchanged.
        """
        if not data:
            return data

        if self._esc_buf:
            data = bytes(self._esc_buf) + data
            self._esc_buf.clear()

        out = bytearray()
        i = 0
        n = len(data)
        while i < n:
            b = data[i]

            if b in (0x0A, 0x0D):           # CR / LF
                out.append(b); i += 1; continue

            if 0x20 <= b <= 0x7E:           # printable ASCII
                out.append(b); i += 1; continue

            if b == 0x7F:                   # DEL
                out.extend(b"[DEL]"); i += 1; continue

            if b == 0x1B:                   # ESC
                if i + 1 >= n:
                    self._esc_buf.extend(data[i:]); break
                second = data[i + 1]

                if second == ord("["):       # CSI  ESC [
                    if i + 2 >= n:
                        self._esc_buf.extend(data[i:]); break
                    third = data[i + 2]
                    if third == ord("A"): out.extend(b"[UP]");    i += 3; continue
                    if third == ord("B"): out.extend(b"[DOWN]");  i += 3; continue
                    if third == ord("C"): out.extend(b"[RIGHT]"); i += 3; continue
                    if third == ord("D"): out.extend(b"[LEFT]");  i += 3; continue
                    if third == ord("H"): out.extend(b"[HOME]");  i += 3; continue
                    if third == ord("F"): out.extend(b"[END]");   i += 3; continue
                    if ord("0") <= third <= ord("9"):
                        j = i + 2
                        digits = bytearray()
                        while j < n and ord("0") <= data[j] <= ord("9"):
                            digits.append(data[j]); j += 1
                        if j >= n:
                            self._esc_buf.extend(data[i:]); break
                        if data[j] == ord("~") and digits:
                            code = int(digits.decode("ascii"))
                            tag = {
                                1: b"[HOME]", 2: b"[INSERT]", 3: b"[DEL]",
                                4: b"[END]",  5: b"[PGUP]",   6: b"[PGDN]",
                            }.get(code, b"[CSI-" + digits + b"~]")
                            out.extend(tag); i = j + 1; continue
                    out.extend(b"[ESC]"); i += 1; continue

                if second == ord("O"):      # SS3  ESC O  (some terminals for arrows)
                    if i + 2 >= n:
                        self._esc_buf.extend(data[i:]); break
                    third = data[i + 2]
                    arrow = {
                        ord("A"): b"[UP]", ord("B"): b"[DOWN]",
                        ord("C"): b"[RIGHT]", ord("D"): b"[LEFT]",
                    }.get(third)
                    if arrow:
                        out.extend(arrow); i += 3; continue
                    out.extend(b"[ESC]"); i += 1; continue

                out.extend(b"[ESC]"); i += 1; continue  # bare ESC

            if b == 0x09:                   # TAB
                out.extend(b"[TAB]"); i += 1; continue

            if b < 0x20:                    # other C0 controls
                if b == 0x00:
                    out.extend(b"[NUL]")
                else:
                    try:
                        out.extend(b"[CTRL-" + bytes([b + 64]) + b"]")
                    except Exception:
                        out.extend(b"[CTRL]")
                i += 1; continue

            out.append(b); i += 1          # non-ASCII pass-through

        return bytes(out)


# ---------------------------------------------------------------------------
# Runtime statistics
# ---------------------------------------------------------------------------

class _Stats:
    """Mutable runtime counters, shared between coroutines (no lock needed:
    they are all updated by the single echo coroutine, read for display only
    from the same coroutine or the summary builder)."""

    def __init__(self) -> None:
        self.bytes_in: int = 0
        self.bytes_out: int = 0
        self.chars_in: int = 0       # printable ASCII characters received
        self.reconnect_count: int = 0
        self.start_time: float = time.monotonic()

    def uptime_str(self) -> str:
        elapsed = int(time.monotonic() - self.start_time)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Control line helpers
# ---------------------------------------------------------------------------

def _read_control_lines(serial_obj) -> dict:
    """Read all control line states from a pyserial Serial object.

    Returns a dict keyed by line name; value is True/False or None if the
    attribute is unavailable (e.g. the platform does not support it).
    """
    result: dict = {}
    for name in ("dtr", "rts", "cts", "dsr", "dcd", "ri"):
        try:
            result[name] = bool(getattr(serial_obj, name))
        except Exception:
            result[name] = None
    return result


def _on_off(val: Optional[bool]) -> str:
    """Format a boolean control line state as a fixed-width string."""
    if val is None:
        return "N/A"
    return "ON " if val else "OFF"


# ---------------------------------------------------------------------------
# Summary page builder
# ---------------------------------------------------------------------------

_BOX_WIDTH = 46  # inner content width (between the pipes)


def _box_row(text: str) -> str:
    return f"| {text:<{_BOX_WIDTH}} |\r\n"


def _box_div() -> str:
    return "+" + "-" * (_BOX_WIDTH + 2) + "+\r\n"


def _build_summary(args: argparse.Namespace, stats: _Stats, serial_obj) -> bytes:
    """Build a terminal-safe ASCII box status page and return it as bytes."""
    stopbits_s = str(args.stopbits).rstrip("0").rstrip(".")
    fmt = f"{args.bytesize}{args.parity}{stopbits_s}"

    k = ord(args.summary_key)
    key_name = f"Ctrl+{chr(k + 64)}" if k < 32 else repr(args.summary_key)

    page = (
        _box_div()
        + _box_row(f"OpenMux Serial Loopback -- {args.name}")
        + _box_div()
        + _box_row(f"Port      : {args.port}")
        + _box_row(f"Baud      : {args.baud}  Format : {fmt}")
        + _box_row(f"Flow ctrl : {args.flow_control}")
        + _box_div()
        + _box_row(f"Uptime    : {stats.uptime_str()}")
        + _box_row(f"Bytes in  : {stats.bytes_in:,}   Bytes out : {stats.bytes_out:,}")
        + _box_row(f"Chars in  : {stats.chars_in:,}   Reconnects: {stats.reconnect_count}")
        + _box_div()
    )

    if serial_obj is not None:
        try:
            cl = _read_control_lines(serial_obj)
            page += (
                _box_row(f"Lines (out): DTR={_on_off(cl['dtr'])}  RTS={_on_off(cl['rts'])}")
                + _box_row(f"Lines (in) : CTS={_on_off(cl['cts'])}  DSR={_on_off(cl['dsr'])}")
                + _box_row(f"             DCD={_on_off(cl['dcd'])}  RI ={_on_off(cl['ri'])}")
                + _box_div()
            )
        except Exception:
            pass

    page += _box_row(f"Press {key_name} for this summary") + _box_div()

    return page.encode("ascii", errors="replace")


# ---------------------------------------------------------------------------
# Shared write helper (serialises all writes via a lock)
# ---------------------------------------------------------------------------

async def _write(writer: asyncio.StreamWriter, lock: asyncio.Lock, data: bytes) -> int:
    """Write *data* under *lock* and drain.  Returns number of bytes written."""
    async with lock:
        writer.write(data)
        await writer.drain()
    return len(data)


# ---------------------------------------------------------------------------
# Control line monitor coroutine
# ---------------------------------------------------------------------------

async def _monitor_lines_loop(
    writer: asyncio.StreamWriter,
    lock: asyncio.Lock,
    interval: float,
    log: logging.Logger,
    args: argparse.Namespace,
) -> None:
    """Poll serial control lines; emit a notice on any change."""
    try:
        serial_obj = writer.transport.serial  # type: ignore[attr-defined]
    except AttributeError:
        log.warning(
            "Control line monitoring unavailable "
            "(transport.serial not accessible on this platform/driver)"
        )
        return

    prev = _read_control_lines(serial_obj)
    all_lines = ["dtr", "rts", "cts", "dsr", "dcd", "ri"]

    try:
        while True:
            await asyncio.sleep(interval)
            curr = _read_control_lines(serial_obj)
            changes = []
            for name in all_lines:
                pv, cv = prev.get(name), curr.get(name)
                if pv != cv and cv is not None:
                    changes.append(f"{name.upper()}: {_on_off(pv)}->{_on_off(cv)}")
            if changes:
                msg = (f"[LINE {args.name}: " + "  ".join(changes) + "]\r\n").encode("ascii")
                await _write(writer, lock, msg)
                log.debug("Control line change: %s", msg.decode().strip())
            prev = curr
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.error("Control line monitor error: %s", e, exc_info=True)


# ---------------------------------------------------------------------------
# Echo loop
# ---------------------------------------------------------------------------

async def _echo_loop(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    lock: asyncio.Lock,
    args: argparse.Namespace,
    stats: _Stats,
    sanitizer: _EscSanitizer,
    log: logging.Logger,
) -> None:
    """Read from *reader*, echo processed data back via *writer*."""
    trigger_byte: int = ord(args.summary_key)

    try:
        serial_obj = writer.transport.serial  # type: ignore[attr-defined]
    except AttributeError:
        serial_obj = None

    while True:
        try:
            data = await reader.read(1024)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("Read error: %s", e)
            break

        if not data:
            log.warning("Connection closed (empty read)")
            break

        stats.bytes_in += len(data)

        # Detect and strip the summary trigger key before echoing
        want_summary = trigger_byte in data
        if want_summary:
            data = data.replace(bytes([trigger_byte]), b"")

        if data:
            stats.chars_in += sum(1 for b in data if 0x20 <= b <= 0x7E)

            if args.echo_delay > 0:
                await asyncio.sleep(args.echo_delay)

            if args.no_sanitize:
                stats.bytes_out += await _write(writer, lock, data)
            else:
                safe = sanitizer.sanitize(data)
                for part in safe.splitlines(keepends=True):
                    if not part:
                        continue
                    trimmed = part.replace(b"\r", b"").replace(b"\n", b"")
                    if trimmed:
                        stats.bytes_out += await _write(writer, lock, trimmed)
                    if not args.no_banner and (part.endswith(b"\n") or part.endswith(b"\r")):
                        banner = f"[ENTER on {args.name}]\r\n".encode("ascii", errors="replace")
                        stats.bytes_out += await _write(writer, lock, banner)

        if want_summary:
            page = _build_summary(args, stats, serial_obj)
            stats.bytes_out += await _write(writer, lock, page)
            log.debug("Summary page sent (%d bytes)", len(page))


# ---------------------------------------------------------------------------
# Single connection lifecycle
# ---------------------------------------------------------------------------

async def _one_connection(
    args: argparse.Namespace,
    stats: _Stats,
    log: logging.Logger,
) -> bool:
    """Open the serial port and run until disconnect.

    Returns True if the port was successfully opened (even if it later
    disconnected), False if the connection could not be established.
    """
    try:
        import serial_asyncio  # type: ignore
    except ImportError:
        log.error(
            "pyserial-asyncio is not installed. "
            "Install it with:  pip install pyserial-asyncio"
        )
        return False

    if not os.path.exists(args.port):
        log.warning("Device %s not found", args.port)
        return False

    if os.name == "posix":
        try:
            st = os.stat(args.port)
            if not stat.S_ISCHR(st.st_mode):
                log.warning("Device %s is not a character device", args.port)
        except OSError:
            pass

    log.info(
        "Connecting: %s  %dbps  %s%s%s  flow=%s",
        args.port, args.baud, args.bytesize, args.parity, args.stopbits,
        args.flow_control,
    )

    try:
        reader, writer = await serial_asyncio.open_serial_connection(
            url=args.port,
            baudrate=args.baud,
            bytesize=args.bytesize,
            parity=args.parity,
            stopbits=float(args.stopbits),
            xonxoff=(args.flow_control == "xon-xoff"),
            rtscts=(args.flow_control == "rts-cts"),
        )
    except Exception as e:
        log.error("Connection failed: %s", e)
        return False

    log.info("Connected to %s", args.port)

    sanitizer = _EscSanitizer()
    lock = asyncio.Lock()

    echo_task = asyncio.create_task(
        _echo_loop(reader, writer, lock, args, stats, sanitizer, log)
    )
    monitor_task: Optional[asyncio.Task] = None
    if args.monitor_lines:
        monitor_task = asyncio.create_task(
            _monitor_lines_loop(writer, lock, args.monitor_interval, log, args)
        )

    try:
        await echo_task
    except asyncio.CancelledError:
        pass
    finally:
        for t in [echo_task, monitor_task]:
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        try:
            writer.close()
            if hasattr(writer, "wait_closed"):
                await writer.wait_closed()
        except Exception:
            pass
        log.info("Disconnected from %s", args.port)

    return True


# ---------------------------------------------------------------------------
# Supervisor (reconnect loop)
# ---------------------------------------------------------------------------

async def _supervisor(
    args: argparse.Namespace,
    stats: _Stats,
    log: logging.Logger,
) -> None:
    """Manage the connection lifecycle, retrying on disconnect."""
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows does not support add_signal_handler

    last_missing_warn: Optional[float] = None

    while not stop_event.is_set():
        # Wait for device to appear
        if not os.path.exists(args.port):
            now = time.monotonic()
            if last_missing_warn is None or (now - last_missing_warn) >= 3600:
                log.warning(
                    "Device %s not found; will retry every %.0fs",
                    args.port, args.reconnect_delay,
                )
                last_missing_warn = now
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=args.reconnect_delay)
            except asyncio.TimeoutError:
                pass
            continue

        last_missing_warn = None

        conn_task = asyncio.create_task(_one_connection(args, stats, log))
        stop_task = asyncio.create_task(stop_event.wait())

        done, pending = await asyncio.wait(
            [conn_task, stop_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        if stop_event.is_set():
            # Ensure the connection task is cancelled if still running
            if conn_task not in done:
                conn_task.cancel()
                try:
                    await conn_task
                except (asyncio.CancelledError, Exception):
                    pass
            break

        connected = False
        try:
            connected = conn_task.result()
        except Exception as e:
            log.error("Connection task raised: %s", e)

        if args.no_reconnect:
            log.info("Exiting (--no-reconnect)")
            break

        if connected:
            stats.reconnect_count += 1
            log.info("Reconnecting in %.1fs...", args.reconnect_delay)
        else:
            log.info("Will retry in %.1fs...", args.reconnect_delay)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=args.reconnect_delay)
        except asyncio.TimeoutError:
            pass

    log.info("Serial loopback stopped")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="serial_loopback",
        description=(
            "Standalone serial loopback for OpenMux. "
            "Echoes received data back to the sender with optional control-character "
            "sanitization and line-boundary annotation. "
            "Connect to an OpenMux serial adapter via a physical or virtual "
            "(socat) null-modem pair."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    grp = p.add_argument_group("Serial port")
    grp.add_argument("-p", "--port", required=True, metavar="DEVICE",
                     help="Serial device path, e.g. /dev/ttyUSB0 or /dev/pts/3")
    grp.add_argument("-n", "--name", default=None, metavar="NAME",
                     help="Human-friendly port name shown in the summary and event messages. "
                          "Defaults to the basename of the device path.")
    grp.add_argument("-b", "--baud", type=int, default=9600, metavar="RATE",
                     help="Baud rate")
    grp.add_argument("--bytesize", type=int, default=8, choices=[5, 6, 7, 8],
                     help="Number of data bits")
    grp.add_argument("--parity", default="N", choices=["N", "E", "O", "M", "S"],
                     help="Parity: N=None  E=Even  O=Odd  M=Mark  S=Space")
    grp.add_argument("--stopbits", type=float, default=1.0,
                     help="Stop bits: 1, 1.5, or 2")
    grp.add_argument("--timeout", type=float, default=1.0,
                     help="Serial read timeout in seconds")
    grp.add_argument("--flow-control", default="none",
                     choices=["none", "rts-cts", "xon-xoff"],
                     help="Flow control mode")

    grp = p.add_argument_group("Echo behaviour")
    grp.add_argument("--echo-delay", type=float, default=0.0, metavar="SECS",
                     help="Artificial delay before echoing each received chunk")
    grp.add_argument("--no-sanitize", action="store_true",
                     help="Echo raw bytes; skip control-character sanitization")
    grp.add_argument("--no-banner", action="store_true",
                     help="Suppress the [ENTER] annotation on newline bytes")

    grp = p.add_argument_group("Monitoring")
    grp.add_argument(
        "--summary-key", default="\x14", metavar="CHAR",
        help=(
            "Single character that triggers the status summary page when received "
            "from the remote end.  Default is Ctrl+T (\\x14).  The key is consumed "
            "and not echoed back."
        ),
    )
    grp.add_argument("--monitor-lines", action="store_true",
                     help="Print a notice on the serial link whenever a control line changes state")
    grp.add_argument("--monitor-interval", type=float, default=0.1, metavar="SECS",
                     help="Polling interval for control line state monitoring")

    grp = p.add_argument_group("Connection")
    grp.add_argument("--no-reconnect", action="store_true",
                     help="Exit on disconnect instead of attempting to reconnect")
    grp.add_argument("--reconnect-delay", type=float, default=5.0, metavar="SECS",
                     help="Seconds to wait between reconnection attempts")

    grp = p.add_argument_group("Logging")
    grp.add_argument("-v", "--verbose", action="store_true",
                     help="Enable DEBUG-level logging")
    grp.add_argument("-q", "--quiet", action="store_true",
                     help="Suppress all output except errors")

    args = p.parse_args()

    if args.stopbits not in (1.0, 1.5, 2.0):
        p.error("--stopbits must be 1, 1.5, or 2")

    if args.name is None:
        args.name = os.path.basename(args.port)

    if len(args.summary_key) != 1:
        p.error("--summary-key must resolve to exactly one character (e.g. \\x14)")

    return args


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()

    level = (
        logging.DEBUG if args.verbose else
        logging.ERROR if args.quiet else
        logging.INFO
    )
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
        level=level,
        stream=sys.stderr,
    )
    log = logging.getLogger("serial_loopback")

    k = ord(args.summary_key)
    key_name = f"Ctrl+{chr(k + 64)}" if k < 32 else repr(args.summary_key)

    log.info(
        "Serial loopback: name=%s  port=%s  baud=%d  format=%s%s%s  flow=%s",
        args.name, args.port, args.baud, args.bytesize, args.parity, args.stopbits,
        args.flow_control,
    )
    log.info(
        "Echo: sanitize=%s  banner=%s  delay=%.3fs  summary-key=%s",
        not args.no_sanitize, not args.no_banner, args.echo_delay, key_name,
    )
    if args.monitor_lines:
        log.info("Control line monitoring: enabled (interval=%.2fs)", args.monitor_interval)
    log.info("Press Ctrl+C to stop")

    stats = _Stats()
    try:
        asyncio.run(_supervisor(args, stats, log))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
