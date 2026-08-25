"""Port Action runs against federated (muxcon) ports.

The origin server of a federated port arbitrates its shared read-write slot
(issue #52): a local read-write attach is not enough, the action must be
granted by the origin via FEDRW. These tests drive the real PortManager with
a fake RemotePortProxy + fake origin slot to pin down the arbitration
behavior: grant -> run works; deny -> fail fast before any keystroke;
self-demote frees the launcher's origin slot first; restore re-requests it
and leaves the launcher read-only when the slot moved on.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from openmux.server.actions.errors import PortBusyError
from openmux.server.actions.registry import load_action_from_file
from openmux.server.actions.runner import ActionRunner
from openmux.server.port_manager import PortManager

ECHO_PROBE_PATH = str(Path(__file__).resolve().parents[1] / "openmux" / "server" / "actions" / "examples" / "echo_probe.py")


class DummyDataLogger:
    def __init__(self):
        self.meta_events: List[Dict[str, Any]] = []

    def record(self, *args, **kwargs):
        pass

    def record_meta(self, port_name, event, client_id=None, meta=None, port_obj=None):
        self.meta_events.append({"port": port_name, "event": event, "client_id": client_id, "meta": meta})


@pytest.fixture
def dummy_logger(monkeypatch):
    from openmux.server import data_logger as dl_mod

    dummy = DummyDataLogger()
    monkeypatch.setattr(dl_mod.DataLogger, "get", classmethod(lambda cls: dummy))
    return dummy


class FakeOrigin:
    """Stands in for the origin server's read-write slot arbitration.

    `holders` is the set of client ids the origin currently grants the slot
    to (at most `max_rw`). `events` records every grant/release in order so
    tests can assert the FEDRW sequencing.
    """

    def __init__(self, max_rw: int = 1):
        self.max_rw = max_rw
        self.holders: List[str] = []
        self.events: List[str] = []

    def request(self, client_id: str) -> str:
        if client_id in self.holders:
            return "read-write"
        if len(self.holders) >= self.max_rw:
            return "read-only"
        self.holders.append(client_id)
        self.events.append(f"granted:{client_id}")
        return "read-write"

    def release(self, client_id: str) -> str:
        if client_id in self.holders:
            self.holders.remove(client_id)
            self.events.append(f"released:{client_id}")
        return "read-only"


class FakeRemotePort:
    """Minimal RemotePortProxy look-alike: wire behavior + FEDRW plumbing.

    `write_data` mimics the real topology: the bytes always leave this node,
    but the origin only forwards them to the device when the writing client
    holds the origin slot - otherwise the write is dropped at the origin and
    the device (here: the echo) never answers.
    """

    def __init__(self, name: str, origin: FakeOrigin):
        self.name = name
        self.remote_port_name = name
        self.metadata = SimpleNamespace(name=name, origin_server=SimpleNamespace(server_id="origin1"))
        self.is_connected = True
        self.max_read_write_users = 1
        self.connected_clients: List[Dict[str, Any]] = []
        self.client_queues: Dict[str, asyncio.Queue] = {}
        self.data_queue: asyncio.Queue = asyncio.Queue()
        self.origin = origin
        self.streams: Dict[str, int] = {}
        self.writes: List[tuple] = []
        self._cb = None
        self._pm = None
        self._stream_seq = 0

    def set_data_callback(self, cb):
        self._cb = cb

    def set_port_manager(self, pm):
        self._pm = pm

    async def open_stream_for_client(self, client_id: str) -> Optional[int]:
        self._stream_seq += 1
        self.streams[client_id] = self._stream_seq
        return self.streams[client_id]

    async def close_stream_for_client(self, client_id: str) -> bool:
        # The origin drops the fed client (and its slot) when the stream closes.
        self.streams.pop(client_id, None)
        self.origin.release(client_id)
        return True

    async def request_read_write_for_client(self, client_id: str, timeout: float = 3.0) -> str:
        return self.origin.request(client_id)

    async def release_read_write_for_client(self, client_id: str, timeout: float = 3.0) -> str:
        return self.origin.release(client_id)

    async def write_data(self, data: bytes, client_id: Optional[str] = None) -> int:
        self.writes.append((client_id, data))
        if client_id in self.origin.holders and self._cb is not None:
            asyncio.ensure_future(self._cb(data))
        return len(data)


class DummyConsoleManager:
    def __init__(self):
        self.frames: List[Dict[str, Any]] = []

    async def send_control_frame_to_client(self, client_id, payload):
        self.frames.append({"client_id": client_id, **payload})
        return True

    async def broadcast_control_frame_to_port(self, port_name, payload):
        return 1


async def _make_pm(port_name: str, origin: FakeOrigin, connected: bool = True) -> PortManager:
    pm = PortManager([])
    port = FakeRemotePort(port_name, origin)
    port.is_connected = connected
    await pm.register_federated_port(SimpleNamespace(name=port_name), port)
    return pm


async def _attach_human(pm: PortManager, port_name: str, origin: FakeOrigin, mode: str = "read-write") -> None:
    """Attach a browser-like client and mirror what ConsoleManager does on a federated port."""
    await pm.add_client_to_port(port_name, "human1", "alice", "read-only" if mode == "read-only" else "read-write")
    if mode == "read-write":
        assert await pm.ports[port_name].request_read_write_for_client("human1") == "read-write"


def _action_client_ids(pm: PortManager, port_name: str) -> List[str]:
    return [c["client_id"] for c in pm.ports[port_name].connected_clients if c["client_id"].startswith("action:")]


@pytest.mark.asyncio
async def test_run_on_federated_port_succeeds_when_origin_grants(dummy_logger):
    origin = FakeOrigin()
    pm = await _make_pm("rf1", origin)
    runner = ActionRunner(pm)
    action = load_action_from_file(ECHO_PROBE_PATH)

    run = await runner.start_run(action, "rf1", {"text": "hi"}, username="tester")

    assert run.status == "success"
    # The action client was granted by the origin, and the grant was released
    # on stream close when the run finished.
    assert f"granted:{run.client_id}" in origin.events
    assert "released:" + run.client_id in origin.events
    assert origin.holders == []
    # The device (echo) only answered because the origin slot was held: every
    # write went out and every write got an echo back.
    assert len(pm.ports["rf1"].writes) == 1
    # Action client is detached after the run.
    assert _action_client_ids(pm, "rf1") == []


@pytest.mark.asyncio
async def test_run_fails_fast_when_origin_slot_held_by_other(dummy_logger):
    origin = FakeOrigin()
    origin.request("other-fed-client")
    pm = await _make_pm("rf1", origin)
    runner = ActionRunner(pm)
    action = load_action_from_file(ECHO_PROBE_PATH)

    with pytest.raises(PortBusyError, match="did not grant the read-write slot"):
        await runner.start_run(action, "rf1", {"text": "hi"}, username="tester")

    # No keystroke left the node, and the other client's grant was untouched.
    assert pm.ports["rf1"].writes == []
    assert origin.holders == ["other-fed-client"]
    assert _action_client_ids(pm, "rf1") == []


@pytest.mark.asyncio
async def test_self_demote_releases_launcher_origin_slot_before_action_request(dummy_logger):
    origin = FakeOrigin()
    pm = await _make_pm("rf1", origin)
    await _attach_human(pm, "rf1", origin, mode="read-write")
    runner = ActionRunner(pm, console_manager=DummyConsoleManager())
    action = load_action_from_file(ECHO_PROBE_PATH)

    run = await runner.start_run(action, "rf1", {"text": "hi"}, username="tester", requesting_client_id="human1")

    assert run.status == "success"
    assert run.auto_demoted_client_id == "human1"
    seq = origin.events
    # The launcher's origin slot is released BEFORE the action requests its own.
    assert seq.index("released:human1") < seq.index(f"granted:{run.client_id}")
    # The launcher re-requests its origin slot only after the action's slot was
    # released on stream close (the second "granted:human1" is the restore).
    action_released = seq.index(f"released:{run.client_id}")
    assert seq.index("granted:human1", action_released) > action_released
    # Restore gives the launcher both slots back.
    assert pm.get_client_mode("human1", "rf1") == "read-write"
    assert origin.holders == ["human1"]


@pytest.mark.asyncio
async def test_restore_left_read_only_when_origin_slot_moved_on(dummy_logger):
    origin = FakeOrigin()
    pm = await _make_pm("rf1", origin)
    await _attach_human(pm, "rf1", origin, mode="read-write")
    console_manager = DummyConsoleManager()
    runner = ActionRunner(pm, console_manager=console_manager)
    action = load_action_from_file(ECHO_PROBE_PATH)

    async def _run_with_intrusion(session):
        # Mid-run, another client takes the origin slot out from under the action.
        # (No port I/O: the action has just lost the slot, so a write would time out.)
        action_holders = [h for h in origin.holders if h.startswith("action:")]
        origin.holders.remove(action_holders[0])
        origin.holders.append("intruder")
        session.log("intruded")

    action.run_func = _run_with_intrusion

    run = await runner.start_run(action, "rf1", {"text": "hi"}, username="tester", requesting_client_id="human1")

    assert run.status == "success"
    # The slot is held by the intruder now: the launcher is left read-only and
    # told so explicitly, instead of being silently marked read-write locally.
    assert pm.get_client_mode("human1", "rf1") == "read-only"
    assert origin.holders == ["intruder"]
    denied = [f for f in console_manager.frames if f.get("reason") == "action_restore_denied"]
    assert denied == [
        {"client_id": "human1", "type": "client_mode", "ok": False, "mode": "read-only", "reason": "action_restore_denied"}
    ]


@pytest.mark.asyncio
async def test_run_fails_fast_when_federated_link_is_down(dummy_logger):
    origin = FakeOrigin()
    pm = await _make_pm("rf1", origin, connected=False)
    runner = ActionRunner(pm)
    action = load_action_from_file(ECHO_PROBE_PATH)

    with pytest.raises(PortBusyError, match="Federated link.*is down"):
        await runner.start_run(action, "rf1", {"text": "hi"}, username="tester")

    assert pm.ports["rf1"].connected_clients == []
    assert origin.events == []
