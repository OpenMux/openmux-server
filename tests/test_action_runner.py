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
