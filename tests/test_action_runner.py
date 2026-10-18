import asyncio
from pathlib import Path
from typing import Any, Dict, List

import pytest

from openmux.server.actions.errors import ActionValidationError, PortBusyError
from openmux.server.actions.registry import load_action_from_file
from openmux.server.actions.runner import ActionRunner
from openmux.server.adapters.loopback import LoopbackAdapter
from openmux.server.port_manager import PortManager

ECHO_PROBE_PATH = str(Path(__file__).resolve().parents[1] / "openmux" / "server" / "actions" / "examples" / "echo_probe.py")


class DummyDataLogger:
    def __init__(self):
        self.meta_events: List[Dict[str, Any]] = []
        self.mirror_calls: List[Any] = []

    def record(self, *args, **kwargs):
        pass

    def record_meta(self, port_name, event, client_id=None, meta=None, port_obj=None):
        self.meta_events.append({"port": port_name, "event": event, "client_id": client_id, "meta": meta})

    def set_mirror(self, src_port, dst_port):
        self.mirror_calls.append(("set", src_port, dst_port))

    def clear_mirror(self, src_port, dst_port=None):
        self.mirror_calls.append(("clear", src_port, dst_port))


@pytest.fixture
def dummy_logger(monkeypatch):
    from openmux.server import data_logger as dl_mod

    dummy = DummyDataLogger()
    monkeypatch.setattr(dl_mod.DataLogger, "get", classmethod(lambda cls: dummy))
    return dummy


async def _make_pm(port_name="p1", max_rw=1):
    pm = PortManager([])
    adapter = LoopbackAdapter("loop", {"loopback_ports": [{"name": port_name, "max_read_write_users": max_rw}]})
    adapter.main_port_manager = pm
    pm.set_unified_adapters([adapter])
    assert await adapter.start() is True
    return pm, adapter


@pytest.mark.asyncio
async def test_successful_run_against_loopback_port(dummy_logger):
    pm, adapter = await _make_pm()
    action = load_action_from_file(ECHO_PROBE_PATH)
    runner = ActionRunner(pm)

    run = await runner.start_run(action, "p1", {"text": "hello"}, username="tester")

    assert run.status == "success"
    assert run.error is None
    # Action client is detached once the run finishes.
    assert pm.get_client_mode(run.client_id, "p1") is None
    assert runner.get_active_run("p1") is None
    # Structured events were recorded under a per-run synthetic log port name.
    events = [e for e in dummy_logger.meta_events if e["port"] == run.log_port_name]
    assert any(e["event"] == "done" for e in events)
    # Issue #94: the run installed a port->transcript log mirror and cleared it again.
    assert ("set", "p1", run.log_port_name) in dummy_logger.mirror_calls
    assert ("clear", "p1", run.log_port_name) in dummy_logger.mirror_calls
    await adapter.stop()


@pytest.mark.asyncio
async def test_run_transcript_mirrors_the_port_console_log(tmp_path):
    """Issue #94: while a run is active, the port's own log stream is duplicated into
    the run's transcript file, so the per-run file holds the device conversation too.
    Uses the real DataLogger (tmp_path base dir) instead of the dummy."""
    from openmux.server.data_logger import DataLogger

    dl = DataLogger.get()
    previous_base = dl.base_dir
    dl.set_base_dir(str(tmp_path))
    pm, adapter = await _make_pm()
    action = load_action_from_file(ECHO_PROBE_PATH)
    runner = ActionRunner(pm)
    try:
        run = await runner.start_run(action, "p1", {"text": "hello"}, username="tester")
        assert run.status == "success"
        # Wait for the async writer to flush every queued event, then close handles.
        if dl.queue is not None:
            await dl.queue.join()
        await dl.close()

        transcript = tmp_path / "ports" / f"{run.log_port_name}.log"
        port_log = tmp_path / "ports" / "p1.log"
        transcript_text = transcript.read_text(encoding="utf-8")
        port_text = port_log.read_text(encoding="utf-8")
        # The mirrored device output (loopback echo of the sent line) is in the run transcript...
        assert "hello" in transcript_text
        # Ordering: the `send:` debug line comes before the echoed device line - an
        # instant-echo port echoes inside the write, so logging the send only after
        # the write await queued the echo first (issue #94 report).
        send_pos = transcript_text.find("send: 'hello")
        echo_pos = transcript_text.find("hello[ENTER]")
        assert 0 <= send_pos < echo_pos
        # ...as are the run's own structured events and the mirrored lifecycle meta
        # (the action client's disconnect, which the port log records after the mirror is set).
        assert "action_started" in transcript_text
        assert "action_finished" in transcript_text
        assert "client_disconnected" in transcript_text
        # The port's own log keeps its copy of the data and never gets run-only events.
        assert "hello" in port_text
        assert "action_finished" not in port_text
        # The mirror is gone once the run ends.
        assert dl._mirror_map.get("p1") is None
    finally:
        await dl.close()
        dl.set_base_dir(previous_base)
        await adapter.stop()


@pytest.mark.asyncio
async def test_validation_error_prevents_port_attachment(dummy_logger):
    pm, adapter = await _make_pm()
    action = load_action_from_file(ECHO_PROBE_PATH)
    runner = ActionRunner(pm)

    with pytest.raises(ActionValidationError):
        await runner.start_run(action, "p1", {}, username="tester")

    assert pm.ports["p1"].connected_clients == []
    await adapter.stop()


@pytest.mark.asyncio
async def test_port_busy_fails_fast_when_rw_slot_taken(dummy_logger):
    pm, adapter = await _make_pm(max_rw=1)
    await pm.add_client_to_port("p1", client_id="human1", username="alice", mode="read-write")
    action = load_action_from_file(ECHO_PROBE_PATH)
    runner = ActionRunner(pm)

    with pytest.raises(PortBusyError):
        await runner.start_run(action, "p1", {"text": "hi"}, username="tester")

    await adapter.stop()


@pytest.mark.asyncio
async def test_self_demote_and_auto_restore(dummy_logger):
    pm, adapter = await _make_pm(max_rw=1)
    await pm.add_client_to_port("p1", client_id="human1", username="alice", mode="read-write")
    action = load_action_from_file(ECHO_PROBE_PATH)
    runner = ActionRunner(pm)

    run = await runner.start_run(action, "p1", {"text": "hi"}, username="tester", requesting_client_id="human1")

    assert run.status == "success"
    assert run.auto_demoted_client_id == "human1"
    assert pm.get_client_mode("human1", "p1") == "read-write"
    await adapter.stop()


class DummyConsoleManager:
    def __init__(self):
        self.frames: List[Dict[str, Any]] = []
        self.broadcasts: List[Dict[str, Any]] = []

    async def send_control_frame_to_client(self, client_id, payload):
        self.frames.append({"client_id": client_id, **payload})
        return True

    async def broadcast_control_frame_to_port(self, port_name, payload):
        self.broadcasts.append({"port_name": port_name, **payload})
        return 1


@pytest.mark.asyncio
async def test_self_demote_and_auto_restore_notifies_console_manager(dummy_logger):
    pm, adapter = await _make_pm(max_rw=1)
    await pm.add_client_to_port("p1", client_id="human1", username="alice", mode="read-write")
    action = load_action_from_file(ECHO_PROBE_PATH)
    console_manager = DummyConsoleManager()
    runner = ActionRunner(pm, console_manager=console_manager)

    run = await runner.start_run(action, "p1", {"text": "hi"}, username="tester", requesting_client_id="human1")

    assert run.status == "success"
    demote_frames = [f for f in console_manager.frames if f["reason"] == "action_self_demoted"]
    restore_frames = [f for f in console_manager.frames if f["reason"] == "action_restored"]
    assert demote_frames == [
        {"client_id": "human1", "type": "client_mode", "ok": False, "mode": "read-only", "reason": "action_self_demoted"}
    ]
    assert restore_frames == [
        {"client_id": "human1", "type": "client_mode", "ok": True, "mode": "read-write", "reason": "action_restored"}
    ]
    await adapter.stop()


@pytest.mark.asyncio
async def test_run_broadcasts_action_started_and_finished_to_port_viewers(dummy_logger):
    pm, adapter = await _make_pm()
    action = load_action_from_file(ECHO_PROBE_PATH)
    console_manager = DummyConsoleManager()
    runner = ActionRunner(pm, console_manager=console_manager)

    run = await runner.start_run(action, "p1", {"text": "hi"}, username="tester")

    assert run.status == "success"
    assert [b["event"] for b in console_manager.broadcasts] == ["action_started", "action_finished"]
    assert console_manager.broadcasts[0] == {
        "port_name": "p1",
        "type": "action_run",
        "event": "action_started",
        "run_id": run.run_id,
        "action_id": "echo_probe",
        "action_name": action.name,
        "operator_client_id": run.operator_client_id,
    }
    assert console_manager.broadcasts[1]["event"] == "action_finished"
    assert console_manager.broadcasts[1]["run_id"] == run.run_id
    await adapter.stop()


@pytest.mark.asyncio
async def test_run_broadcast_failure_is_swallowed_and_does_not_fail_run(dummy_logger):
    class BroadcastFailsConsoleManager:
        async def broadcast_control_frame_to_port(self, port_name, payload):
            raise RuntimeError("boom")

    pm, adapter = await _make_pm()
    action = load_action_from_file(ECHO_PROBE_PATH)
    runner = ActionRunner(pm, console_manager=BroadcastFailsConsoleManager())

    run = await runner.start_run(action, "p1", {"text": "hi"}, username="tester")

    assert run.status == "success"
    await adapter.stop()


@pytest.mark.asyncio
async def test_concurrent_action_on_same_port_fails_fast(dummy_logger):
    pm, adapter = await _make_pm(max_rw=2)

    async def _slow_run(session):
        await asyncio.sleep(5)

    action = load_action_from_file(ECHO_PROBE_PATH)
    action.run_func = _slow_run
    action.id = "slow"
    runner = ActionRunner(pm)

    task = asyncio.create_task(runner.start_run(action, "p1", {"text": "hi"}, username="tester"))
    await asyncio.sleep(0.05)  # let the first run attach
    assert runner.get_active_run("p1") is not None

    with pytest.raises(PortBusyError):
        await runner.start_run(action, "p1", {"text": "hi"}, username="tester2")

    # Cancelling the still-running first run is now a graceful, supported outcome (see
    # test_cancel_run_stops_a_running_action) rather than a raw CancelledError - _execute()
    # catches it and reports status="cancelled" instead of propagating.
    task.cancel()
    run = await task
    assert run.status == "cancelled"
    await adapter.stop()


@pytest.mark.asyncio
async def test_timeout_detaches_client_and_marks_run_timed_out(dummy_logger):
    pm, adapter = await _make_pm()

    async def _hang(session):
        await asyncio.sleep(5)

    action = load_action_from_file(ECHO_PROBE_PATH)
    action.run_func = _hang
    action.timeout = 0.05
    runner = ActionRunner(pm)

    with pytest.raises(Exception):
        await runner.start_run(action, "p1", {"text": "hi"}, username="tester")

    run = next(iter(runner.runs.values()))
    assert run.status == "timeout"
    assert pm.get_client_mode(run.client_id, "p1") is None
    assert runner.get_active_run("p1") is None
    await adapter.stop()


@pytest.mark.asyncio
async def test_script_crash_still_detaches_client(dummy_logger):
    pm, adapter = await _make_pm()

    async def _boom(session):
        raise RuntimeError("script bug")

    action = load_action_from_file(ECHO_PROBE_PATH)
    action.run_func = _boom
    runner = ActionRunner(pm)

    with pytest.raises(RuntimeError):
        await runner.start_run(action, "p1", {"text": "hi"}, username="tester")

    run = next(iter(runner.runs.values()))
    assert run.status == "failed"
    assert run.error == "script bug"
    assert pm.get_client_mode(run.client_id, "p1") is None
    await adapter.stop()


CONFIRM_PROBE_PATH = str(
    Path(__file__).resolve().parents[1] / "openmux" / "server" / "actions" / "examples" / "confirm_probe.py"
)


@pytest.mark.asyncio
async def test_operator_input_confirm_flow_end_to_end(dummy_logger):
    pm, adapter = await _make_pm()
    action = load_action_from_file(CONFIRM_PROBE_PATH)
    runner = ActionRunner(pm)

    run = runner.launch_run(action, "p1", {"text": "hello"}, username="tester", requesting_client_id="human1")
    queue = runner.subscribe(run.run_id)

    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    while event.get("event") != "waiting_for_operator":
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert "Send" in event["prompt"]

    assert runner.submit_operator_input(run.run_id, "yes", requesting_client_id="human1") is True

    task = runner._tasks.get(run.run_id)
    if task is not None:
        await task

    assert run.status == "success"
    assert pm.get_client_mode(run.client_id, "p1") is None
    await adapter.stop()


@pytest.mark.asyncio
async def test_operator_input_rejected_from_wrong_client(dummy_logger):
    pm, adapter = await _make_pm()
    action = load_action_from_file(CONFIRM_PROBE_PATH)
    runner = ActionRunner(pm)

    run = runner.launch_run(action, "p1", {"text": "hello"}, username="tester", requesting_client_id="human1")
    queue = runner.subscribe(run.run_id)

    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    while event.get("event") != "waiting_for_operator":
        event = await asyncio.wait_for(queue.get(), timeout=1.0)

    assert runner.submit_operator_input(run.run_id, "yes", requesting_client_id="someone_else") is False

    # The script is still waiting; the correct client can now unblock it.
    assert runner.submit_operator_input(run.run_id, "yes", requesting_client_id="human1") is True
    task = runner._tasks.get(run.run_id)
    if task is not None:
        await task
    assert run.status == "success"
    await adapter.stop()


@pytest.mark.asyncio
async def test_submit_operator_input_unknown_run_returns_false(dummy_logger):
    pm, adapter = await _make_pm()
    runner = ActionRunner(pm)

    assert runner.submit_operator_input("no-such-run", "yes") is False
    await adapter.stop()


@pytest.mark.asyncio
async def test_take_over_operator_reassigns_and_notifies_via_event(dummy_logger):
    pm, adapter = await _make_pm()
    action = load_action_from_file(CONFIRM_PROBE_PATH)
    runner = ActionRunner(pm)

    run = runner.launch_run(action, "p1", {"text": "hello"}, username="tester", requesting_client_id="human1")
    queue = runner.subscribe(run.run_id)

    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    while event.get("event") != "waiting_for_operator":
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert run.operator_client_id == "human1"

    assert runner.take_over_operator(run.run_id, "human2") is True
    assert run.operator_client_id == "human2"

    changed = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert changed == {
        "event": "operator_changed",
        "run_id": run.run_id,
        "operator_client_id": "human2",
        "previous_operator_client_id": "human1",
        "ts": changed["ts"],
    }

    # The old operator can no longer answer; the new one can.
    assert runner.submit_operator_input(run.run_id, "yes", requesting_client_id="human1") is False
    assert runner.submit_operator_input(run.run_id, "yes", requesting_client_id="human2") is True

    task = runner._tasks.get(run.run_id)
    if task is not None:
        await task
    assert run.status == "success"
    await adapter.stop()


@pytest.mark.asyncio
async def test_take_over_operator_unknown_run_returns_false(dummy_logger):
    pm, adapter = await _make_pm()
    runner = ActionRunner(pm)

    assert runner.take_over_operator("no-such-run", "human2") is False
    await adapter.stop()


@pytest.mark.asyncio
async def test_take_over_operator_requires_a_client_id(dummy_logger):
    pm, adapter = await _make_pm()
    action = load_action_from_file(CONFIRM_PROBE_PATH)
    runner = ActionRunner(pm)

    run = runner.launch_run(action, "p1", {"text": "hello"}, username="tester", requesting_client_id="human1")
    queue = runner.subscribe(run.run_id)

    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    while event.get("event") != "waiting_for_operator":
        event = await asyncio.wait_for(queue.get(), timeout=1.0)

    assert runner.take_over_operator(run.run_id, "") is False
    assert run.operator_client_id == "human1"

    assert runner.submit_operator_input(run.run_id, "yes", requesting_client_id="human1") is True
    task = runner._tasks.get(run.run_id)
    if task is not None:
        await task
    await adapter.stop()


@pytest.mark.asyncio
async def test_take_over_operator_moves_pending_read_write_restore(dummy_logger):
    """Issue #90: a pending auto-restore follows the operator role - the taker-overer
    gets the read-write slot at finish, not the original launcher."""
    pm, adapter = await _make_pm(max_rw=1)
    await pm.add_client_to_port("p1", client_id="human1", username="alice", mode="read-write")
    await pm.add_client_to_port("p1", client_id="human2", username="bob", mode="read-only")
    action = load_action_from_file(CONFIRM_PROBE_PATH)
    console_manager = DummyConsoleManager()
    runner = ActionRunner(pm, console_manager=console_manager)

    run = runner.launch_run(action, "p1", {"text": "hello"}, username="tester", requesting_client_id="human1")
    queue = runner.subscribe(run.run_id)

    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    while event.get("event") != "waiting_for_operator":
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
    # The launcher held the only read-write slot, so launching self-demoted it (issue #90).
    assert run.auto_demoted_client_id == "human1"

    assert runner.take_over_operator(run.run_id, "human2") is True
    assert run.auto_demoted_client_id == "human2"

    assert runner.submit_operator_input(run.run_id, "yes", requesting_client_id="human2") is True
    task = runner._tasks.get(run.run_id)
    if task is not None:
        await task

    assert run.status == "success"
    assert pm.get_client_mode("human2", "p1") == "read-write"
    assert pm.get_client_mode("human1", "p1") == "read-only"
    restore_frames = [f for f in console_manager.frames if f["reason"] == "action_restored"]
    assert restore_frames == [
        {"client_id": "human2", "type": "client_mode", "ok": True, "mode": "read-write", "reason": "action_restored"}
    ]
    await adapter.stop()


@pytest.mark.asyncio
async def test_take_over_operator_restore_skipped_when_taker_detaches(dummy_logger):
    """Issue #90: if the taker-overer disconnects before the run finishes, the slot is
    left unclaimed (first-come first-served) - the departed promise restores nobody."""
    pm, adapter = await _make_pm(max_rw=1)
    await pm.add_client_to_port("p1", client_id="human1", username="alice", mode="read-write")
    await pm.add_client_to_port("p1", client_id="human2", username="bob", mode="read-only")
    action = load_action_from_file(CONFIRM_PROBE_PATH)
    runner = ActionRunner(pm)

    run = runner.launch_run(action, "p1", {"text": "hello"}, username="tester", requesting_client_id="human1")
    queue = runner.subscribe(run.run_id)

    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    while event.get("event") != "waiting_for_operator":
        event = await asyncio.wait_for(queue.get(), timeout=1.0)

    assert runner.take_over_operator(run.run_id, "human2") is True
    await pm.remove_client_from_port("p1", "human2")

    assert runner.submit_operator_input(run.run_id, "yes", requesting_client_id="human2") is True
    task = runner._tasks.get(run.run_id)
    if task is not None:
        await task

    assert run.status == "success"
    assert pm.get_client_mode("human2", "p1") is None
    assert pm.get_client_mode("human1", "p1") == "read-only"
    await adapter.stop()


@pytest.mark.asyncio
async def test_take_over_operator_restore_survives_launcher_disconnect(dummy_logger):
    """Issue #90: the pending restore belongs to the operator role, not the launcher -
    when the launcher disconnects mid-run, the connected taker-overer still restores."""
    pm, adapter = await _make_pm(max_rw=1)
    await pm.add_client_to_port("p1", client_id="human1", username="alice", mode="read-write")
    await pm.add_client_to_port("p1", client_id="human2", username="bob", mode="read-only")
    action = load_action_from_file(CONFIRM_PROBE_PATH)
    runner = ActionRunner(pm)

    run = runner.launch_run(action, "p1", {"text": "hello"}, username="tester", requesting_client_id="human1")
    queue = runner.subscribe(run.run_id)

    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    while event.get("event") != "waiting_for_operator":
        event = await asyncio.wait_for(queue.get(), timeout=1.0)

    assert runner.take_over_operator(run.run_id, "human2") is True
    # The launcher (original operator) disconnects; the promise must follow the role.
    await pm.remove_client_from_port("p1", "human1")

    assert runner.submit_operator_input(run.run_id, "yes", requesting_client_id="human2") is True
    task = runner._tasks.get(run.run_id)
    if task is not None:
        await task

    assert run.status == "success"
    assert pm.get_client_mode("human2", "p1") == "read-write"
    assert pm.get_client_mode("human1", "p1") is None
    await adapter.stop()


SETUP_WIZARD_PATH = str(
    Path(__file__).resolve().parents[1] / "openmux" / "server" / "actions" / "examples" / "setup_wizard.py"
)


@pytest.mark.asyncio
async def test_setup_wizard_walks_through_every_operator_input_kind(dummy_logger):
    """End-to-end run of the `setup_wizard` demo script (confirm/wait_for_input/choose/
    select/radio, plus a final expect() on the loopback adapter's "[ENTER]" prompt)."""
    pm, adapter = await _make_pm()
    action = load_action_from_file(SETUP_WIZARD_PATH)
    runner = ActionRunner(pm)

    run = runner.launch_run(action, "p1", {"step_seconds": 0.01}, username="tester", requesting_client_id="human1")
    queue = runner.subscribe(run.run_id)
    answers = iter(["yes", "my-device", "quick", "115200", "verbose", ""])

    async def _wait_for_prompt():
        # Generous timeout: the script has a deliberate expect() timeout step (~5s) between
        # the "flash" and "reboot" phases, which sits between two of these prompts.
        event = await asyncio.wait_for(queue.get(), timeout=8.0)
        while event.get("event") != "waiting_for_operator":
            event = await asyncio.wait_for(queue.get(), timeout=8.0)
        return event

    seen_kinds = []
    for _ in range(6):
        event = await _wait_for_prompt()
        seen_kinds.append(event["kind"])
        answer = next(answers)
        assert runner.submit_operator_input(run.run_id, answer, requesting_client_id="human1") is True

    assert seen_kinds == ["buttons", "text", "buttons", "select", "radio", "text"]

    task = runner._tasks.get(run.run_id)
    if task is not None:
        await task

    assert run.status == "success"
    assert pm.get_client_mode(run.client_id, "p1") is None
    await adapter.stop()


SLOW_NOOP_PATH = str(Path(__file__).resolve().parents[1] / "openmux" / "server" / "actions" / "examples" / "slow_noop.py")


@pytest.mark.asyncio
async def test_cancel_run_stops_a_running_action(dummy_logger):
    pm, adapter = await _make_pm()
    action = load_action_from_file(SLOW_NOOP_PATH)
    runner = ActionRunner(pm)

    run = runner.launch_run(action, "p1", {"seconds": 30.0}, username="tester", requesting_client_id="human1")
    queue = runner.subscribe(run.run_id)
    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    while event.get("event") != "sleeping":
        event = await asyncio.wait_for(queue.get(), timeout=1.0)

    assert runner.cancel_run(run.run_id, requesting_client_id="human1") is True

    task = runner._tasks.get(run.run_id)
    if task is not None:
        await task

    assert run.status == "cancelled"
    # The port's read-write slot is released, same as any other run outcome.
    assert pm.get_client_mode(run.client_id, "p1") is None
    assert runner.get_active_run("p1") is None


# --- Issue #90 slice 2: a finished run stays open (visible to all) until its
# operator closes (acks) it; closing broadcasts `action_run: closed` to the port.


@pytest.mark.asyncio
async def test_finished_run_stays_open_until_closed(dummy_logger):
    pm, adapter = await _make_pm()
    action = load_action_from_file(ECHO_PROBE_PATH)
    runner = ActionRunner(pm)

    run = await runner.start_run(action, "p1", {"text": "hi"}, username="tester")

    # Finished, lock released, but the run is still the port's open run.
    assert run.status == "success"
    assert runner.get_active_run("p1") is None
    assert runner.get_open_finished_run("p1") is run
    assert run.summary()["closed"] is False
    assert run.summary()["closed_at"] is None

    assert runner.close_run("does-not-exist") == "unknown"
    assert runner.close_run(run.run_id) == "closed"
    assert runner.get_open_finished_run("p1") is None
    assert run.summary()["closed"] is True
    assert run.summary()["closed_at"] is not None
    assert runner.close_run(run.run_id) == "already_closed"
    await adapter.stop()


@pytest.mark.asyncio
async def test_finished_open_run_does_not_block_new_run_and_is_auto_closed(dummy_logger):
    pm, adapter = await _make_pm()
    action = load_action_from_file(ECHO_PROBE_PATH)
    console_manager = DummyConsoleManager()
    runner = ActionRunner(pm, console_manager=console_manager)

    first = await runner.start_run(action, "p1", {"text": "one"}, username="tester")
    assert runner.get_open_finished_run("p1") is first

    # Starting a new run beside a finished-unclosed one is allowed (the open run
    # never holds the lock) and auto-closes the previous run for everyone.
    second = runner.launch_run(action, "p1", {"text": "two"}, username="tester2")
    assert runner.get_active_run("p1") is second
    assert first.closed_at is not None
    assert runner.get_open_finished_run("p1") is None

    await runner._tasks[second.run_id]
    await asyncio.sleep(0)  # let the fire-and-forget closed broadcast deliver

    closed = [b for b in console_manager.broadcasts if b["event"] == "closed"]
    assert closed == [
        {
            "port_name": "p1",
            "type": "action_run",
            "event": "closed",
            "run_id": first.run_id,
            "action_id": "echo_probe",
            "closed_by": "",
        }
    ]
    # The finished broadcast now carries the outcome so a tab that never joined the
    # run can word its finished-state chip from the broadcast alone.
    finished = [b for b in console_manager.broadcasts if b["event"] == "action_finished"]
    assert finished[0]["status"] == "success"
    assert finished[0]["error"] is None
    await adapter.stop()


@pytest.mark.asyncio
async def test_close_run_operator_permission_matrix(dummy_logger):
    pm, adapter = await _make_pm()
    action = load_action_from_file(ECHO_PROBE_PATH)
    runner = ActionRunner(pm)

    run = await runner.start_run(action, "p1", {"text": "hi"}, username="tester", requesting_client_id="op1")
    assert run.operator_client_id == "op1"

    assert runner.close_run(run.run_id, requesting_client_id="other") == "denied"
    assert run.closed_at is None  # a denied close changes nothing
    assert runner.close_run(run.run_id, requesting_client_id="op1") == "closed"
    await adapter.stop()


@pytest.mark.asyncio
async def test_finished_run_without_operator_may_be_closed_by_anyone(dummy_logger):
    pm, adapter = await _make_pm()
    action = load_action_from_file(ECHO_PROBE_PATH)
    runner = ActionRunner(pm)

    run = await runner.start_run(action, "p1", {"text": "hi"}, username="tester")  # no client_id -> no operator
    assert run.operator_client_id is None

    assert runner.close_run(run.run_id, requesting_client_id="whoever") == "closed"
    await adapter.stop()


@pytest.mark.asyncio
async def test_close_run_rejects_a_still_running_run(dummy_logger):
    pm, adapter = await _make_pm()
    action = load_action_from_file(SLOW_NOOP_PATH)
    runner = ActionRunner(pm)

    run = runner.launch_run(action, "p1", {"seconds": 30.0}, username="tester", requesting_client_id="op1")
    # Wait until the script itself is running (same pattern as test_cancel_run_stops_a_
    # running_action): cancelling while still attaching would kill _execute before the
    # cancelled-outcome handling runs.
    queue = runner.subscribe(run.run_id)
    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    while event.get("event") != "sleeping":
        event = await asyncio.wait_for(queue.get(), timeout=1.0)

    assert runner.close_run(run.run_id, requesting_client_id="op1") == "running"
    assert runner.close_run(run.run_id, requesting_client_id="other") == "running"  # status checked before operator

    assert runner.cancel_run(run.run_id, requesting_client_id="op1") is True
    await runner._tasks[run.run_id]
    # A cancelled run is finished: it now waits open for its operator to close it.
    assert run.status == "cancelled"
    assert runner.get_open_finished_run("p1") is run
    assert runner.close_run(run.run_id, requesting_client_id="op1") == "closed"
    await adapter.stop()


@pytest.mark.asyncio
async def test_takeover_of_finished_open_run_allows_closing(dummy_logger):
    pm, adapter = await _make_pm()
    action = load_action_from_file(ECHO_PROBE_PATH)
    runner = ActionRunner(pm)

    run = await runner.start_run(action, "p1", {"text": "hi"}, username="tester", requesting_client_id="op1")
    assert runner.close_run(run.run_id, requesting_client_id="user2") == "denied"

    # The ticket's path: take over the finished-open run, then close it.
    assert runner.take_over_operator(run.run_id, "user2") is True
    assert run.operator_client_id == "user2"
    assert any(e.get("event") == "operator_changed" and e.get("operator_client_id") == "user2" for e in run.events)

    assert runner.close_run(run.run_id, requesting_client_id="user2") == "closed"
    # Closing is final: a closed run can no longer be taken over.
    assert runner.take_over_operator(run.run_id, "user3") is False
    await adapter.stop()


@pytest.mark.asyncio
async def test_cancel_run_rejected_from_wrong_client(dummy_logger):
    pm, adapter = await _make_pm()
    action = load_action_from_file(SLOW_NOOP_PATH)
    runner = ActionRunner(pm)

    run = runner.launch_run(action, "p1", {"seconds": 30.0}, username="tester", requesting_client_id="human1")
    queue = runner.subscribe(run.run_id)
    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    while event.get("event") != "sleeping":
        event = await asyncio.wait_for(queue.get(), timeout=1.0)

    assert runner.cancel_run(run.run_id, requesting_client_id="someone_else") is False
    assert run.status == "running"

    # The correct operator can still stop it afterwards.
    assert runner.cancel_run(run.run_id, requesting_client_id="human1") is True
    task = runner._tasks.get(run.run_id)
    if task is not None:
        await task
    assert run.status == "cancelled"
    await adapter.stop()


@pytest.mark.asyncio
async def test_cancel_run_unknown_run_returns_false(dummy_logger):
    pm, adapter = await _make_pm()
    runner = ActionRunner(pm)

    assert runner.cancel_run("no-such-run") is False
    await adapter.stop()


@pytest.mark.asyncio
async def test_cancel_run_on_finished_run_returns_false(dummy_logger):
    pm, adapter = await _make_pm()
    action = load_action_from_file(ECHO_PROBE_PATH)
    runner = ActionRunner(pm)

    run = await runner.start_run(action, "p1", {"text": "hi"}, username="tester")
    assert run.status == "success"

    assert runner.cancel_run(run.run_id) is False
    await adapter.stop()


# --- power context injection (issue #93) ----------------------------------------


class PowerAdapterStub:
    """A unified adapter advertising adapter_type "power" for find_power_adapter."""

    def __init__(self):
        self.calls = []
        self.feeds = {"p1": ["rack1.1"]}
        self.states = {"rack1.1": True}

    def get_adapter_type(self):
        return "power"

    def port_power_map(self, port_name):
        return list(self.feeds.get(port_name, []))

    def port_power_payload(self, port_name):
        refs = self.feeds.get(port_name)
        if not refs:
            return None
        return {
            "feeds": [{"ref": r, "on": self.states.get(r), "watts": None} for r in refs],
            "feeds_on": sum(1 for r in refs if self.states.get(r) is True),
            "feeds_total": len(refs),
            "state": "all",
            "all_power_lost": False,
        }

    def feed_states(self, port_name):
        return {r: self.states.get(r) for r in self.feeds.get(port_name, [])}

    def _power_blocked_ports(self, ref, username):
        return []

    async def set_outlet(self, ref, on, user=None, client_id=None):
        self.calls.append((ref, on, user, client_id))
        self.states[ref] = on
        return {"ok": True, "reading": {"ref": ref, "on": on}, "impact": {"losing_power": [], "staying_up": []}}


class PowerConsoleManager(DummyConsoleManager):
    def __init__(self, port_manager):
        super().__init__()
        self.port_manager = port_manager


async def _switcher_script(session):
    power = session.list_power()
    session.log(f"power_available={power.get('available')}")
    if power.get("available"):
        await session.power_off("rack1.1")


@pytest.mark.asyncio
async def test_run_injects_power_context_when_pdu_adapter_active(dummy_logger):
    """The session's power calls run against the found PDU adapter as the
    operator, audited under the run's action:<run-id> client id."""
    pm, adapter = await _make_pm()
    power = PowerAdapterStub()
    pm.set_unified_adapters(list(pm.unified_adapters) + [power])
    cm = PowerConsoleManager(pm)
    action = load_action_from_file(ECHO_PROBE_PATH)
    action.run_func = _switcher_script
    runner = ActionRunner(pm, console_manager=cm)

    run = await runner.start_run(action, "p1", {"text": "x"}, username="operator9")

    assert run.status == "success"
    assert any(e.get("event") == "power_available=True" for e in run.events)
    assert power.calls == [("rack1.1", False, "operator9", run.client_id)]
    assert run.client_id == f"action:{run.run_id}"
    await adapter.stop()


@pytest.mark.asyncio
async def test_run_without_pdu_adapter_reports_power_unavailable(dummy_logger):
    """No adapter_type "power" in the unified adapters: list_power() says
    unavailable and the run still succeeds."""
    pm, adapter = await _make_pm()
    cm = PowerConsoleManager(pm)
    action = load_action_from_file(ECHO_PROBE_PATH)
    action.run_func = _switcher_script
    runner = ActionRunner(pm, console_manager=cm)

    run = await runner.start_run(action, "p1", {"text": "x"}, username="operator9")

    assert run.status == "success"
    assert any(e.get("event") == "power_available=False" for e in run.events)
    await adapter.stop()


@pytest.mark.asyncio
async def test_run_without_console_manager_reports_power_unavailable(dummy_logger):
    pm, adapter = await _make_pm()
    action = load_action_from_file(ECHO_PROBE_PATH)
    action.run_func = _switcher_script
    runner = ActionRunner(pm)  # console_manager=None -> no adapter lookup possible

    run = await runner.start_run(action, "p1", {"text": "x"}, username="operator9")

    assert run.status == "success"
    assert any(e.get("event") == "power_available=False" for e in run.events)
    await adapter.stop()
