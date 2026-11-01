"""Stress tier: many viewers on one port, requests arriving together, and sockets that come and go.

The other functional scenarios ask what the server does to one console. These ask what happens when a
port is busy: a dozen viewers watch one stream, the web page is polled while a writer streams, and
clients open a session and then leave without saying so.

Each scenario asserts on what a user sees: the lines a viewer received, the seat that came free, the
socket count that returned to its baseline. None asserts on log text, because a log line reports a
fault instead of being one. The unit tier holds that half, in
tests/server/test_client_listener_peer_gone.py: a peer that vanished must cost one INFO line and no
traceback.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Tuple

import pytest

from .harness import LSOF, LSOF_MISSING, Api, E2EServer
from .story import surfaces

pytestmark = [pytest.mark.e2e, pytest.mark.feature]

#: Viewers on one port in these scenarios.
VIEWERS = 12

#: Sessions that log in on one listener and then do nothing.
IDLE_SESSIONS = 20

#: Descriptor kinds compared before and after a burst, so an unrelated kind cannot look like a leak.
_SOCKET_KINDS = ("total", "IPv4", "IPv6", "unix", "KQUEUE", "NPOLLED")

#: Bytes of padding on a line in the stalled-viewer scenario. A viewer that stops reading loses bytes
#: only once its own socket buffer is full, which takes more than the few kilobytes one line each is.
_STALL_PADDING = "x" * 200

#: One big line in the stalled-console flood. A loopback connection absorbs only a few MiB
#: (``net.inet.tcp.autorcvbufmax`` and its send-side twin are 4 MiB each here), so the scenario pushes more
#: than that to find out where the surplus goes.
_FLOOD_LINE = "x" * 65000
_FLOOD_COUNT = 240

#: Seconds a healthy console may wait for a viewer-set notice while another console reads nothing. The
#: same attach with nobody stalled costs the same measurement, so the bound is the gap and not the wait.
_PRESENCE_LIMIT = 5.0


def _swarm_world(
    servers,
    *,
    port: str = "swarm",
    users: Tuple[Tuple[str, str], ...] = (("admin", "admin"), ("ro", "read-only")),
) -> E2EServer:
    """Start a running world with one loopback port and a Telnet listener that asks who you are.

    A loopback Port has no per-port viewer limit: any number of sessions may attach and watch.
    ``max_read_write_users`` (default ``one``) says only how many may drive it at once, so the viewers
    are the ``ro`` user, which is read-only and never competes for the write slot, and the writer is
    ``admin``. The Telnet listener requires a login so each viewer is a named user. The world needs its
    own ``name``, because the ``servers`` fixture already built the default one in this directory.
    """

    def configure(builder: Any) -> Any:
        builder._loopback = []
        builder.with_loopback(port)
        builder._loopback[-1]["description"] = "port shared by every viewer"
        return builder

    return servers(
        name=port,
        users=users,
        configure=configure,
        telnet=True,
        telnet_kwargs={"require_auth": True},
        ssh=False,
        start=True,
    )


def _get(url: str, *, timeout: float = 30.0) -> Tuple[int, str]:
    """Fetch one URL with no session at all and return its status and body.

    No cookies are sent, so every call is a first request: a new session, a login page or a redirect,
    and nothing reused. That is what makes a burst of them a burst.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(response.status), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return int(error.code), error.read().decode("utf-8", "replace")


def _port_entry(api: Any, name: str) -> Dict[str, Any]:
    """Return one port's entry from ``/api/status``, so a case can name the counter it checks.

    The payload carries the port list both at the top level and under ``status``, so this accepts
    either rather than depending on which one the server happened to build.
    """
    payload = api.json("/api/status")
    sections: List[Any] = [payload]
    sections.extend(value for value in payload.values() if isinstance(value, dict))
    for section in sections:
        for key in ("ports", "port_list", "port_summary"):
            entries = section.get(key)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and entry.get("name") == name:
                    return entry
    raise AssertionError(f"{name!r} is not in the /api/status port list")


def _markers(writer: Any, label: str, count: int, *, pad: str = "") -> List[str]:
    """Send numbered lines through an attached session and return the markers in order.

    Every marker is unique, so a scenario can name the segment a viewer missed. ``pad`` makes each line
    long enough to fill a socket, which a stalled-viewer scenario needs.
    """
    markers = []
    for index in range(1, count + 1):
        marker = f"{label}-{index:04d}"
        writer.write_line(marker + pad)
        # The port echoes to the writer too, so this confirms the line reached the port. A write the
        # port never took fails here, at the writer, instead of surfacing later as a viewer gap.
        writer.wait_for(marker, timeout=30.0)
        markers.append(marker)
    return markers


def _missing(markers: List[str], text: str) -> List[str]:
    """Return the markers absent from ``text``, in the order they were sent."""
    return [marker for marker in markers if marker not in text]


def _viewers(clients, world: E2EServer, count: int, port_name: str, *, surface: str = "telnet:viewer") -> List[Any]:
    """Open ``count`` Telnet sessions on the listener and attach each one to one port.

    Each session keeps its own reader thread, which every attached surface must do. A viewer whose
    bytes nobody reads is the subject of its own scenario, not an accident here.
    """
    viewers = []
    try:
        for index in range(count):
            viewer = clients.telnet(user="ro", require_auth=True, server=world, surface=f"{surface}{index + 1}")
            viewer.choose_port(port_name)
            viewers.append(viewer)
    except Exception:
        for viewer in viewers:
            viewer.close()
        raise
    return viewers


def _descriptors(world: E2EServer) -> Dict[str, int]:
    """Count the server's descriptors, keeping only the kinds a leak would move."""
    if LSOF is None:
        return {}
    full = world.descriptor_kinds()
    return {kind: full.get(kind, 0) for kind in _SOCKET_KINDS}


def _wait_for_descriptors(world: E2EServer, baseline: Dict[str, int], *, seconds: float = 30.0) -> Dict[str, int]:
    """Wait until the process holds no more sockets than it did at the start, and return the last count.

    A closed socket is released some milliseconds after the peer left, so waiting is the check. The
    comparison allows one IPv4 and five KQUEUE entries above the baseline, because the queue of a
    listener the scenario still holds open moves on its own.
    """
    deadline = time.monotonic() + seconds
    current = _descriptors(world)
    while time.monotonic() < deadline:
        sockets_back = current.get("IPv4", 0) <= baseline.get("IPv4", 0) + 1
        queues_back = current.get("KQUEUE", 0) <= baseline.get("KQUEUE", 0) + 5
        if sockets_back and queues_back:
            return current
        time.sleep(1.0)
        current = _descriptors(world)
    return current


def _assert_descriptors_back(world: E2EServer, baseline: Dict[str, int], after: Dict[str, int]) -> None:
    """Fail with both counts when the process still holds what the burst opened."""
    if LSOF is None:
        return
    assert after.get("IPv4", 0) <= baseline.get("IPv4", 0) + 1, f"descriptors did not return: {baseline} then {after}"
    assert after.get("KQUEUE", 0) <= baseline.get("KQUEUE", 0) + 5, f"queues did not return: {baseline} then {after}"


# --- SOK-01, SOK-02 -------------------------------------------------------------------------------


@pytest.mark.e2e
@surfaces("telnet:viewer", "cli:writer")
def test_sok01_twelve_viewers_receive_one_stream(servers, clients, story) -> None:
    """A dozen viewers watch one port while a writer sends two hundred lines."""

    world = _swarm_world(servers)
    viewers = _viewers(clients, world, VIEWERS, "swarm")
    writer = clients.cli(server=world, attach_to="swarm", surface="cli:writer")

    markers = _markers(writer, "stream", 200)
    for viewer in viewers:
        viewer.wait_for(markers[-1], timeout=30.0)
    gaps = [_missing(markers, viewer.text()) for viewer in viewers]
    sizes = [len(viewer.text()) for viewer in viewers]
    # The writer sees the port too, so its own echo says whether a missing line ever reached the port.
    writer_gap = _missing(markers, writer.read_text())
    story.note(
        f"12 viewers received the stream: missing counts {[len(gap) for gap in gaps]}, sizes {sizes}, "
        f"writer missing {len(writer_gap)}",
    )

    assert [len(gap) for gap in gaps] == [0] * VIEWERS, (
        f"writer echo missing {len(writer_gap)} of {len(markers)}; viewer missing counts "
        f"{[len(gap) for gap in gaps]}; first gaps "
        f"{[(viewer.surface, gap[:2]) for viewer, gap in zip(viewers, gaps) if gap][:3]}"
    )
    assert max(sizes) - min(sizes) < 400, f"a viewer fell behind the rest: sizes {sizes}"


@pytest.mark.e2e
@surfaces("telnet:viewer", "telnet:stalled", "cli:writer", "api:http")
def test_sok02_a_viewer_that_stops_reading_loses_only_its_own_bytes(servers, clients, story, api_for_server) -> None:
    """One viewer stops reading while the others keep up."""

    world = _swarm_world(servers)
    api = api_for_server(world)
    viewers = _viewers(clients, world, 3, "swarm")
    stalled = clients.telnet(user="ro", require_auth=True, server=world, surface="telnet:stalled")
    stalled.choose_port("swarm")
    writer = clients.cli(server=world, attach_to="swarm", surface="cli:writer")

    first = _markers(writer, "paced", 20)
    stalled.wait_for(first[-1], timeout=30.0)
    stalled.kill_reader()
    story.note("one viewer stopped reading, as a wedged tab or a laptop with no network does")

    beyond = _markers(writer, "beyond", 1200, pad=_STALL_PADDING)
    sizes_before = [len(viewer.text()) for viewer in viewers]
    for viewer in viewers:
        viewer.wait_for(beyond[-1], timeout=60.0)
    lost = [marker for marker in beyond if any(marker not in viewer.text() for viewer in viewers)]
    entry = _port_entry(api, "swarm")
    story.note(
        f"{len(beyond)} padded lines written after the stall: {len(lost)} missing for a viewer that read, "
        f"port counters {sorted((key, value) for key, value in entry.items() if not isinstance(value, (list, dict)))}, "
        f"viewer sizes {sizes_before} then {[len(viewer.text()) for viewer in viewers]}",
    )

    assert lost == [], f"a stalled viewer cost another viewer its bytes: {lost[:5]}"
    assert entry.get("dropped_chunks"), f"the server must shed the stalled viewer's bytes, not block: {entry}"

    writer.write_line("port-still-delivers")
    viewers[0].wait_for("port-still-delivers", timeout=30.0)
    story.note("the port still delivers to a viewer that kept up")


# --- SOK-03 ---------------------------------------------------------------------------------------


@pytest.mark.e2e
@surfaces("telnet:viewer", "cli:writer", "api:http", "http:burst")
def test_sok03_two_hundred_requests_answer_every_one_while_a_port_streams(servers, clients, story) -> None:
    """Four logged-in web sessions, a status poller, and two hundred requests from sixteen threads."""

    world = _swarm_world(servers)
    viewers = _viewers(clients, world, 4, "swarm")
    writer = clients.cli(server=world, attach_to="swarm", surface="cli:writer")
    sessions = [Api(world.web_url) for _ in range(4)]
    paths = ("/login", "/", "/api/ports", "/api/status", "/healthz")
    urls = [f"{world.web_url}{path}" for path in paths for _ in range(40)]
    expected = {url: _get(url) for url in dict.fromkeys(urls)}

    stop_polling = threading.Event()

    def poll_status() -> None:
        """Ask the status endpoint on its own thread until told to stop."""
        while not stop_polling.is_set():
            _get(f"{world.web_url}/api/status", timeout=10.0)
            time.sleep(0.05)

    poller = threading.Thread(target=poll_status, name="soak-status-poll", daemon=True)
    poller.start()

    def fetch(url: str) -> Tuple[str, int, int]:
        status, body = _get(url)
        return url, status, len(body)

    def fetch_signed(session: Api) -> Tuple[int, int]:
        status, _, body = session.get("/api/status")
        return status, len(body)

    with ThreadPoolExecutor(max_workers=16) as pool:
        started = time.monotonic()
        anonymous_tasks = [pool.submit(fetch, url) for url in urls]
        signed_tasks = [pool.submit(fetch_signed, session) for session in sessions for _ in range(5)]
        results = [task.result() for task in anonymous_tasks]
        signed = [task.result() for task in signed_tasks]
        elapsed = time.monotonic() - started
    stop_polling.set()
    poller.join(timeout=15.0)
    story.note(f"{len(urls)} unanswered requests and 20 signed ones from 16 threads in {elapsed:.2f} s")

    wrong = [
        (url, status, size, expected[url][0], len(expected[url][1]))
        for url, status, size in results
        if status != expected[url][0] or size != len(expected[url][1])
    ]
    assert wrong == [], f"answers changed under load: {wrong[:5]}"

    not_answered = [entry for entry in signed if entry[0] != 200]
    assert not_answered == [], f"a logged-in session went unanswered: {not_answered[:5]}"

    writer.write_line("still-streaming")
    for viewer in viewers:
        viewer.wait_for("still-streaming", timeout=30.0)
    story.note("the four logged-in sessions answered, four viewers read the line, and the poller survived")


# --- SOK-04 ---------------------------------------------------------------------------------------


@pytest.mark.e2e
@surfaces("telnet:idle", "raw:silent", "telnet:keeper", "cli:writer")
def test_sok04_idle_and_silent_sessions_hold_the_listener_then_release_it(servers, clients, story) -> None:
    """Twenty sessions log in and do nothing, and twelve connections send not one byte."""

    world = _swarm_world(servers)
    # The scenario keeps these two open to the end, so the baseline counts them too. A baseline taken
    # before them would score their own two sockets as a leak.
    keeper = clients.telnet(user="ro", require_auth=True, server=world, surface="telnet:keeper")
    keeper.choose_port("swarm")
    writer = clients.cli(server=world, attach_to="swarm", surface="cli:writer")
    baseline = _descriptors(world)
    if LSOF is None:
        story.note(f"descriptor counts skipped: {LSOF_MISSING}")

    holders = [
        clients.telnet(user="ro", require_auth=True, server=world, surface=f"telnet:idle{index + 1}")
        for index in range(IDLE_SESSIONS)
    ]
    silent = [clients.silent(surface=f"raw:silent{index + 1}", server=world) for index in range(12)]
    story.note(f"{IDLE_SESSIONS} logged-in sessions and 12 silent connections are open and idle")

    writer.write_line("idle-check-1")
    keeper.wait_for("idle-check-1", timeout=30.0)
    story.note("a fresh session still gets a seat and the stream while thirty others wait")

    for holder in holders:
        holder.close()
    for socket_holder in silent:
        socket_holder.abort()
    story.note("the idle sessions closed and the silent ones were abandoned")

    writer.write_line("idle-check-2")
    keeper.wait_for("idle-check-2", timeout=30.0)
    _assert_descriptors_back(world, baseline, _wait_for_descriptors(world, baseline))


# --- SOK-05, SOK-06 -------------------------------------------------------------------------------


@pytest.mark.e2e
@surfaces("telnet:passing", "raw:silent", "telnet:keeper", "cli:writer", "api:http")
def test_sok05_abandoned_sockets_leave_the_seat_and_the_socket_behind(servers, clients, story, api_for_server) -> None:
    """Nine sessions in a row: one closes cleanly, one resets mid-stream, one never sends a byte."""

    world = _swarm_world(servers)
    api = api_for_server(world)
    keeper = clients.telnet(user="ro", require_auth=True, server=world, surface="telnet:keeper")
    keeper.choose_port("swarm")
    writer = clients.cli(server=world, attach_to="swarm", surface="cli:writer")
    # Counted after the two sessions that outlive every round, so their sockets are not read as a leak.
    baseline = _descriptors(world)
    if LSOF is None:
        story.note(f"descriptor counts skipped: {LSOF_MISSING}")

    outcomes: List[str] = []
    for round_index in range(9):
        leaving = round_index % 3
        marker = f"churn-{round_index}"
        if leaving == 2:
            writer.write_line(marker)
            keeper.wait_for(marker, timeout=20.0)
            silent = clients.silent(surface=f"raw:silent{round_index}", server=world)
            silent.abort()
            outcomes.append("silent connection abandoned")
            continue
        passing = clients.telnet(user="ro", require_auth=True, server=world, surface=f"telnet:passing{round_index}")
        passing.choose_port("swarm")
        # Written after the attach: a viewer receives only what the Port writes from then on.
        writer.write_line(marker)
        passing.wait_for(marker, timeout=20.0)
        if leaving == 0:
            passing.close()
            outcomes.append("closed cleanly")
        else:
            passing.abort()
            outcomes.append("reset mid-stream")

    final = _markers(writer, "last", 10)
    keeper.wait_for(final[-1], timeout=30.0)
    entry = _port_entry(api, "swarm")
    story.note("churn outcomes: " + ", ".join(outcomes))
    story.note(
        "port after churn: "
        + ", ".join(f"{key}={entry.get(key)}" for key in ("connected_clients", "dropped_chunks", "client_count")),
    )

    assert _missing(final, keeper.text()) == [], "the stream must survive nine sessions arriving and leaving"
    assert entry["connected_clients"] == 2, f"a session that left still holds its seat: {entry}"
    _assert_descriptors_back(world, baseline, _wait_for_descriptors(world, baseline))


@pytest.mark.e2e
@surfaces("telnet:passing", "ws:passing", "cli:heartbeat", "telnet:late", "api:http")
def test_sok06_a_sustained_stream_outlives_viewers_coming_and_going(servers, clients, story, api_for_server) -> None:
    """Twenty seconds of marked output, with viewers arriving, leaving, and status asked between."""

    world = _swarm_world(servers)
    api = api_for_server(world)
    writer = clients.cli(server=world, attach_to="swarm", surface="cli:heartbeat")
    stop = threading.Event()
    sent: List[str] = []
    sent_lock = threading.Lock()

    def stream() -> None:
        """Write one marked line every 0.2 s until told to stop, on its own thread."""
        index = 0
        while not stop.is_set():
            index += 1
            marker = f"beat-{index:04d}"
            with sent_lock:
                sent.append(marker)
            writer.write_line(marker)
            time.sleep(0.2)

    thread = threading.Thread(target=stream, name="soak-heartbeat", daemon=True)
    thread.start()
    seen: List[str] = []
    try:
        for round_index in range(3):
            passing = clients.telnet(user="ro", require_auth=True, server=world, surface=f"telnet:passing{round_index}")
            passing.choose_port("swarm")
            browser = clients.websocket("swarm", user="ro", surface=f"ws:passing{round_index}", server=world)
            marker = f"watch-{round_index}"
            writer.write_line(marker)
            passing.wait_for(marker, timeout=20.0)
            browser.wait_for_text(marker, timeout=20.0)
            seen.append(f"viewer {round_index + 1} read {marker} on Telnet and on the console socket")
            if round_index % 2:
                passing.abort()
                browser.abort()
            else:
                passing.close()
                browser.close()
            _get(f"{world.web_url}/api/status")
            time.sleep(6.0)
    finally:
        stop.set()
        thread.join(timeout=15.0)
    with sent_lock:
        markers = list(sent)
    story.note(f"{'; '.join(seen) or 'no passing viewer completed'}; {len(markers)} marked lines written")

    assert len(markers) >= 40, f"the sustained writer produced only {len(markers)} lines in 20 s"

    entry = _port_entry(api, "swarm")
    scalars = sorted((key, value) for key, value in entry.items() if not isinstance(value, (list, dict)))
    story.note(f"the port after the run: {scalars}")
    assert entry["connected_clients"] == 1, f"a viewer that left still holds its seat: {entry}"

    watched = [f"watch-{index}" for index in range(3)]
    assert _missing(watched, writer.read_text()) == [], "the stream stopped for the writer at some round"
    story.note("the writer's own transcript holds every round marker beside the beats")

    late = clients.telnet(user="ro", require_auth=True, server=world, surface="telnet:late")
    late.choose_port("swarm")
    writer.write_line("late-check")
    late.wait_for("late-check", timeout=30.0)
    story.note("a viewer that arrives after the run still gets a seat and the stream")


def _drain_console(viewer: Any, seconds: float) -> None:
    """Read every frame one console hands over within ``seconds``.

    A viewer set changes on every attach and detach, so a scenario that times the next notice must first
    clear the notices the setup already caused.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            viewer.read_frame(timeout=0.05)
        except (TimeoutError, OSError):
            return


def _presence_gap(clients, world: E2EServer, observer: Any, port_name: str) -> Tuple[float, float]:
    """Attach one console, close it again, and time what the observing console waits for the notice.

    Returns the attach round trip and the wait for the viewer-set frame, in seconds. The login happens
    outside the timed window, so the second number is the notice and not the handshake.
    """
    _drain_console(observer, 0.4)
    arriving = clients.telnet(user="ro", require_auth=True, server=world, surface="telnet:arriving")
    started = time.monotonic()
    arriving.choose_port(port_name)
    attached = time.monotonic()
    deadline = attached + 30.0
    while time.monotonic() < deadline:
        try:
            opcode, payload = observer.read_frame(timeout=0.5)
        except (TimeoutError, OSError):
            continue
        if opcode == 0x1 and b'"presence"' in payload:
            gap = time.monotonic() - started
            arriving.close()
            return attached - started, gap
    raise AssertionError(f"no viewer-set notice reached the observing console within 30s of choosing {port_name}")


@pytest.mark.e2e
@surfaces("ws:stalled", "ws:observer", "cli:writer", "telnet:arriving")
def test_sok07_a_console_that_reads_nothing_holds_back_no_other_console(servers, clients, story, api_for_server) -> None:
    """A viewer set changes while one console reads nothing, and the healthy console must not wait for it.

    ``ConsoleManager.broadcast_control_frame_to_port`` delivers to one viewer at a time, so a viewer whose
    socket is full could make every viewer behind it wait. This measures what that costs. The stalled
    console is attached first, because fan-out order is attach order.
    """

    world = _swarm_world(servers, port="swarm")
    api = api_for_server(world)
    stalled = clients.websocket("swarm", user="ro", surface="ws:stalled", server=world)
    stalled_port = stalled.socket().getsockname()[1]
    observer = clients.websocket("swarm", user="ro", surface="ws:observer", server=world)
    writer = clients.cli(server=world, attach_to="swarm", surface="cli:writer")

    writer.write_line("sok07-prime")
    stalled.wait_for_text("sok07-prime", timeout=30.0)
    observer.wait_for_text("sok07-prime", timeout=30.0)

    _, before = _presence_gap(clients, world, observer, "swarm")
    story.note(f"a viewer-set notice reached the watching console in {before:.3f}s with nobody stalled")

    drops_before = _port_entry(api, "swarm").get("dropped_chunks")
    stalled.socket().settimeout(0.01)
    for index in range(_FLOOD_COUNT):
        writer.write_line(f"sok07-flood-{index:04d} {_FLOOD_LINE}", timeout=30.0)
    try:
        queued = world.tcp_queue("web", stalled_port)
    except RuntimeError as exc:  # justification: the queue is evidence, and the assertion does not need it
        queued = {"send_q": -1, "recv_q": -1, "note": str(exc)}
    _, gap = _presence_gap(clients, world, observer, "swarm")
    entry = _port_entry(api, "swarm")
    story.note(
        f"{_FLOOD_COUNT * len(_FLOOD_LINE) / 1e6:.1f} MB pushed at one console that read nothing: "
        f"the kernel held send_q={queued.get('send_q')} for it, dropped_chunks moved {drops_before} -> "
        f"{entry.get('dropped_chunks')}, and a viewer-set notice reached the watching console in {gap:.3f}s",
    )

    writer.write_line("sok07-after-flood")
    observer.wait_for_text("sok07-after-flood", timeout=30.0)
    buffered = 0
    stalled.socket().settimeout(20)
    while True:
        try:
            _, payload = stalled.read_frame(timeout=1.0)
            buffered += len(payload)
        except (TimeoutError, OSError):
            break
    story.note(
        f"the stalled console was still attached and caught up on {buffered / 1e6:.1f} MB when it read again, "
        f"of the {_FLOOD_COUNT * len(_FLOOD_LINE) / 1e6:.1f} MB written while it read nothing",
    )

    assert gap < _PRESENCE_LIMIT, (
        f"a console that reads nothing held the viewer-set notice of a healthy console: {gap:.3f}s "
        f"against {before:.3f}s with nobody stalled"
    )
    assert (
        _port_entry(api, "swarm")["connected_clients"] == 3
    ), f"a console that stopped reading, or one that caught up, is no longer on the Port: {_port_entry(api, 'swarm')}"
