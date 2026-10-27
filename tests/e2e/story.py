"""One merged timeline for every surface, plus the step, cast list, and race helper.

A reader must be able to open ``timeline.log`` at line 1 and follow who does what, in what order,
without opening the test file. Everything here serves that reader: the header lists the cast and the
legend, ``step`` numbers the actions, and the byte renderers make a control frame and a Telnet
negotiation readable instead of showing escapes.

Recording never changes behaviour: no sleep, no blocking write, and a fault disables the recorder
once instead of failing the test.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: One record shows at most this many payload bytes; the full bytes go to ``raw-<surface>.bin``.
MAX_PAYLOAD = 400

#: A session that reads this much data without the test consuming it is a harness bug.
READER_QUEUE_CAP = 20000

_ARROWS = {"in": "<-", "out": "->", "<-": "<-", "->": "->", "log": "."}

_TELNET_NAMES = {
    255: "IAC",
    254: "DONT",
    253: "DO",
    252: "WILL",
    251: "WONT",
    250: "SB",
    240: "SE",
    249: "GA",
    248: "EL",
    247: "EC",
    244: "IP",
    243: "AYT",
    242: "AO",
    241: "ACP",
    245: "BRK",
    239: "EOR",
}

_CONTROL_GLYPHS = {i: chr(0x2400 + i) for i in range(32)}
_CONTROL_GLYPHS[127] = "␡"

_LEGEND = (
    "legend  <- server to client   -> client to server   . server log line\n"
    "        OMXCTRL = control frame, decoded   IAC/DO/WILL/... = Telnet negotiation\n"
    "        ␀-␡ = control bytes (␍ CR, ␊ LF)"
)

#: The story for the running test, so ``step`` in a test body needs no fixture argument.
_ACTIVE: "contextvars.ContextVar[Optional[Story]]" = contextvars.ContextVar("openmux_e2e_story", default=None)


def render_bytes(data: bytes) -> str:
    """Render raw bytes for the timeline: Telnet negotiation named, control bytes shown.

    Telnet options are printed by name because a hand-rolled negotiation responder hides exactly
    the bytes that break a session.
    """
    out: List[str] = []
    index = 0
    length = len(data)
    while index < length:
        byte = data[index]
        if byte == 255 and index + 2 < length:
            command = data[index + 2]
            out.append(f"IAC {_TELNET_NAMES.get(command, command)}")
            index += 3
            continue
        if byte in _TELNET_NAMES and byte != 255 and _inside_negotiation(data, index):
            out.append(_TELNET_NAMES[byte])
            index += 1
            continue
        out.append(_CONTROL_GLYPHS.get(byte, chr(byte) if byte >= 32 else "?"))
        index += 1
    return "".join(out)


def _inside_negotiation(data: bytes, index: int) -> bool:
    """True when this option byte follows an IAC in the three bytes before it."""
    for back in (1, 2):
        if index - back >= 0 and data[index - back] == 255:
            return True
    return False


def render_omxctrl(data: bytes) -> Optional[str]:
    """Decode one ``OMXCTRL`` control frame, in either framing, or return None.

    Raw TCP prefixes the frame with NUL; the web console sends the text form. A record shows the
    decoded JSON, because the payload is the assertion.
    """
    raw = data
    if raw.startswith(b"\x00"):
        raw = raw[1:]
    prefix = b"OMXCTRL "
    if not raw.startswith(prefix):
        return None
    body = raw[len(prefix) :].strip().rstrip(b"\r\n")
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return f"OMXCTRL <unparsable: {body[:60]!r}>"
    return "OMXCTRL " + json.dumps(payload, separators=(",", ":"), sort_keys=False)


def render_payload(payload: Any) -> str:
    """Render any recorded payload as one readable line."""
    if isinstance(payload, (bytes, bytearray)):
        data = bytes(payload)
        if data.startswith(b"\x00OMXCTRL ") or data.startswith(b"OMXCTRL "):
            head, _, _rest = data.partition(b"\n")
            decoded = render_omxctrl(head)
            return decoded or render_bytes(data)
        return render_bytes(data)
    if isinstance(payload, dict):
        return json.dumps(payload, separators=(",", ":"))
    text = str(payload)
    return " ".join(text.split())


class Timeline:
    """One merged stream of records for all surfaces of one test.

    Writers come from several threads (server reader, per-session readers, the test body), so the
    lock plus a whole-line write is what keeps two records from interleaving into garbage.
    """

    def __init__(self, path: Optional[Path] = None, echo: bool = False):
        self.path = Path(path) if path is not None else None
        self.echo = echo
        self._start = time.monotonic()
        self._lock = threading.Lock()
        self._lines: List[str] = []
        self._raw_files: Dict[str, Path] = {}
        self._disabled: Optional[str] = None
        self._sink = None
        #: True once a failure line is written, so a second summary line is not added on top of it.
        self.recorded_failure = False

    # --- public --------------------------------------------------------------------------

    def record(self, surface: str, direction: str, payload: Any, note: Optional[str] = None) -> None:
        """Append one record. Never raises: a broken recorder must not fail or slow a test."""
        offset = time.monotonic() - self._start
        text = render_payload(payload)
        shown, raw_note = self._cap(surface, text)
        arrow = _ARROWS.get(direction, direction)
        line = f"+{offset:7.3f}s {surface:<12} {arrow:<2} {shown}"
        if note:
            line += f"   # {note}"
        if raw_note:
            line += f"   {raw_note}"
        self._emit(line)

    def note(self, text: str) -> None:
        """Append one plain line: a step marker, the cast list, or a failure summary."""
        self._emit(f"+{time.monotonic() - self._start:7.3f}s {text}")

    def step(self, number: int, text: str) -> None:
        """Append one numbered step marker, so cause precedes effect."""
        self.note(f"STEP {number} {text}")

    def failure(self, expected: str, actual: str) -> None:
        """Append what the test wanted against what happened, which is the useful half of a failure."""
        self.recorded_failure = True
        self.note(f"FAIL  EXPECTED {expected} / ACTUAL {actual}")

    def lines(self) -> List[str]:
        """Every line recorded so far, for a test that asserts on the log itself."""
        with self._lock:
            return list(self._lines)

    def close(self) -> None:
        """Close the file handle, so an artifact bundle is complete before anyone reads it."""
        with self._lock:
            if self._sink is not None:
                try:
                    self._sink.close()
                except OSError:
                    pass  # justification: the artifact is already flushed line by line
                self._sink = None

    # --- internals -----------------------------------------------------------------------

    def _cap(self, surface: str, text: str) -> Tuple[str, str]:
        """Shorten a long payload for the stream and keep the full bytes in a raw file."""
        if len(text) <= MAX_PAYLOAD:
            return text, ""
        raw_path = self._raw_for(surface)
        offset = -1
        if raw_path is not None:
            try:
                offset = raw_path.stat().st_size if raw_path.exists() else 0
                with raw_path.open("a", encoding="utf-8") as handle:
                    handle.write(text + "\n")
            except OSError:
                raw_path = None
        shown = text[:MAX_PAYLOAD] + "…"
        note = f"raw={raw_path.name}@{offset}" if raw_path is not None else "raw=unavailable"
        return shown, note

    def _raw_for(self, surface: str) -> Optional[Path]:
        """The raw-bytes file for one surface, inside the artifact dir."""
        if self.path is None:
            return None
        safe = surface.replace(":", "-").replace("/", "-")
        if safe not in self._raw_files:
            self._raw_files[safe] = self.path / f"raw-{safe}.bin"
        return self._raw_files[safe]

    def _emit(self, line: str) -> None:
        if self._disabled is not None:
            return
        try:
            with self._lock:
                self._lines.append(line)
                if self.path is not None:
                    if self._sink is None:
                        self.path.parent.mkdir(parents=True, exist_ok=True)
                        self._sink = self.path.open("a", encoding="utf-8")
                    self._sink.write(line + "\n")
                    self._sink.flush()
                if self.echo:
                    print(line, flush=True)
        except Exception as exc:  # justification: a debugging aid must never break the run it serves
            self._disabled = f"{type(exc).__name__}: {exc}"
            print(f"[timeline] disabled: {self._disabled}", flush=True)


class Story:
    """The narrative layer of one test: cast list, numbered steps, and the timeline it writes."""

    def __init__(self, timeline: Timeline, test_id: str, title: str = ""):
        self.timeline = timeline
        self.test_id = test_id
        self.title = title
        self._counter = 0
        self._token = None
        self.steps: List[str] = []

    # --- header --------------------------------------------------------------------------

    def header(self, cast: Sequence[str] = (), ports: Sequence[str] = (), server: Sequence[str] = ()) -> None:
        """Write the banner, the cast list, the ports, and the legend.

        The header is deliberately first, so a reader can understand ``web:rw`` before the first
        record uses it.
        """
        self.timeline.note(f"=== {self.test_id}  {self.title} ".ljust(74, "="))
        for line in server:
            self.timeline.note(f"  server  {line}")
        for index, line in enumerate(cast):
            self.timeline.note(("  cast    " if index == 0 else "            ") + line)
        for index, line in enumerate(ports):
            self.timeline.note(("  port    " if index == 0 else "            ") + line)
        for line in _LEGEND.splitlines():
            self.timeline.note(line)

    def cast(self, line: str) -> None:
        """Announce one participant on the timeline.

        A surface is announced when it is created rather than in a header block, because a page or a
        socket opened after step 3 must not appear to have existed before step 1.
        """
        self.timeline.note(f"          cast    {line}")

    # --- steps ---------------------------------------------------------------------------

    def begin(self) -> "Story":
        """Make this story the active one, so ``step()`` in a test body finds it."""
        self._token = _ACTIVE.set(self)
        return self

    def end(self) -> None:
        """Release the active story and close the timeline."""
        if self._token is not None:
            _ACTIVE.reset(self._token)
            self._token = None
        self.timeline.close()

    @contextlib.contextmanager
    def step(self, text: str):
        """Run one narrative step: record it, run the block, record a failure with its number."""
        self._counter += 1
        number = self._counter
        self.steps.append(text)
        self.timeline.step(number, text)
        try:
            yield self
        except AssertionError as exc:
            self.timeline.failure(text, _one_line(exc))
            raise
        except Exception as exc:
            self.timeline.failure(text, f"{type(exc).__name__}: {_one_line(exc)}")
            raise

    def expect(self, expected: str, actual: str) -> None:
        """Record an unmet expectation from inside a helper, without raising here."""
        self.timeline.failure(expected, actual)

    def snapshot(self, surface: str, summary: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Record what one browser showed at this moment, and keep the JSON when there is one."""
        self.timeline.note(f"          {surface} snapshot {summary}")
        if payload is None or self.timeline.path is None:
            return
        try:
            snapshots = self.timeline.path.parent / "snapshots"
            snapshots.mkdir(parents=True, exist_ok=True)
            index = len(self.timeline.lines())
            (snapshots / f"{index:04d}-{surface.replace(':', '-')}.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError:
            pass  # justification: a snapshot is a convenience, the timeline line is the record


def _one_line(text: object, limit: int = 300) -> str:
    """Collapse any message or exception to one line for the timeline."""
    flattened = " ".join(str(text).split())
    return flattened[:limit] + ("…" if len(flattened) > limit else "")


def step(text: str):
    """The active story's step, so a test body writes ``with step("..."):``.

    Raises:
        RuntimeError: when no story is active, which means the test forgot the ``story`` fixture.
    """
    active = _ACTIVE.get()
    if active is None:
        raise RuntimeError("step() needs the story fixture: add `story` to the test signature")
    return active.step(text)


def surfaces(*names: str):
    """Declare the surfaces one scenario proves, for the generated index and a reader."""
    import pytest

    return pytest.mark.e2e_surfaces(*names)


def concurrently(actions: Sequence[Callable[[], Any]], deadline: float = 15.0, story: Optional[Story] = None) -> List[Any]:
    """Run every action at the same moment and return their results in the order given.

    The actions start behind one barrier, so a race such as two Takeover requests on one freed seat
    is genuine rather than an artefact of the test order. Each result lands on the timeline in
    completion order. When an action raises, this raises once with every failure.

    Args:
        actions: Zero-argument callables, run in their own threads.
        deadline: Seconds to wait for all of them.
        story: Optional story used to record each action's result.
    """
    if not actions:
        return []
    barrier = threading.Barrier(len(actions) + 1)
    results: Dict[int, Any] = {}
    errors: List[Tuple[int, BaseException]] = []
    done: "queue.Queue[Tuple[int, Optional[float]]]" = queue.Queue()

    def run(index: int, action: Callable[[], Any]) -> None:
        try:
            barrier.wait(timeout=deadline)
        except threading.BrokenBarrierError as exc:  # justification: reported once by the caller below
            errors.append((index, exc))
            done.put((index, None))
            return
        started = time.monotonic()
        try:
            results[index] = action()
        except BaseException as exc:  # justification: collected and re-raised together below
            errors.append((index, exc))
        done.put((index, time.monotonic() - started))

    threads = [
        threading.Thread(target=run, args=(index, action), name=f"e2e-race-{index}", daemon=True)
        for index, action in enumerate(actions)
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=deadline)  # release the actions together

    finished: List[int] = []
    started_all = time.monotonic()
    while len(finished) < len(actions):
        remaining = deadline - (time.monotonic() - started_all)
        if remaining <= 0:
            break
        try:
            index, elapsed = done.get(timeout=remaining)
        except queue.Empty:
            break
        finished.append(index)
        if story is not None and elapsed is not None:
            story.timeline.note(f"          race action {index} finished in {elapsed:.3f}s")
    for thread in threads:
        thread.join(timeout=max(0.1, deadline - (time.monotonic() - started_all)))

    if errors:
        detail = "; ".join(f"action {index}: {type(exc).__name__}: {exc}" for index, exc in sorted(errors))
        raise AssertionError(f"concurrently: {len(errors)} of {len(actions)} actions failed: {detail}")
    if len(results) != len(actions):
        raise AssertionError(f"concurrently: only {len(results)} of {len(actions)} actions returned inside {deadline}s")
    return [results[index] for index in range(len(actions))]


def artifact_dir_for(root: Path, nodeid: str) -> Path:
    """The artifact directory for one test id, with the separators made path-safe."""
    safe = nodeid.replace("::", "/").replace("tests/e2e/", "").replace(".py", "")
    return Path(root) / safe


def environment_flags(environ: Optional[Dict[str, str]] = None) -> Dict[str, bool]:
    """Which opt-in observability knobs are on, read from the environment.

    Every flag also reads an env var, following the ``PYTEST_ANNOUNCE_TEST`` precedent in
    ``tests/conftest.py``, so a VS Code task or ``launch.json`` can turn on detail without editing
    the command line.
    """
    env = environ if environ is not None else os.environ
    return {
        "live": env.get("E2E_LIVE", "") == "1",
        "reveal": env.get("E2E_REVEAL", "") == "1",
    }
