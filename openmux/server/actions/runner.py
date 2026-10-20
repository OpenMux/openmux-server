"""Runs Port Action scripts against a port, reusing the port's read-write slot.

See docs/design/port_actions.md ("Locking (read-write slot)", "Persisted log",
"Run registry") for the design this implements.
"""

import asyncio
import logging
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from openmux.server.actions.errors import ActionTimeoutError, PortBusyError
from openmux.server.actions.registry import ActionScript, redact_params, validate_params
from openmux.server.actions.session import ActionSession, _PowerContext
from openmux.server.adapters.power_command import find_power_adapter
from openmux.server.data_logger import DataLogger

logger = logging.getLogger("openmux.server.actions.runner")


def _truncate(text: str, limit: int = 4000) -> str:
    """Cap `text` for a debug-log line; a crash buffer/traceback can be arbitrarily large."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"...(truncated, {len(text)} chars total)"


@dataclass
class ActionRun:
    """A single execution record for an action, kept for the run's lifetime.

    Attributes mirror the "Run registry" section of the design doc.
    """

    run_id: str
    port_name: str
    action_id: str
    username: str
    params: Dict[str, Any]
    client_id: str
    status: str = "running"  # running | success | failed | timeout | cancelled
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    # Set when the run's operator closes (acks) a finished run (issue #90 slice 2).
    # A finished run with closed_at == None stays open: visible to every viewer of the
    # port until it is closed, but it never holds the port's one-run-at-a-time lock.
    closed_at: Optional[float] = None
    error: Optional[str] = None
    auto_demoted_client_id: Optional[str] = None
    exception: Optional[BaseException] = field(default=None, repr=False, compare=False)
    # Full structured-event history for the run's lifetime (see docs/design/port_actions.md
    # "Late join"), replayed once to WS subscribers that connect after the run started.
    events: List[Dict[str, Any]] = field(default_factory=list)
    # Who may answer a pending session.wait_for_input()/confirm() prompt (see "Operator
    # input" in the design doc) - the run's launcher by default, reassignable mid-run via
    # ActionRunner.take_over_operator() (mirrors the port's own "Take control").
    operator_client_id: Optional[str] = None

    @property
    def log_port_name(self) -> str:
        """Synthetic port name used to key this run's own log file (see docs/design/
        port_actions.md, "Persisted log"):
        `<port>_action_<action_id>_<started YYYYMMDDHHMMSS UTC>_<run_id>`.
        """
        started = time.strftime("%Y%m%d%H%M%S", time.gmtime(self.started_at))
        return f"{self.port_name}_action_{self.action_id}_{started}_{self.run_id}"

    def summary(self) -> Dict[str, Any]:
        """JSON-serializable snapshot for API/UI consumption (excludes the raw exception)."""
        return {
            "run_id": self.run_id,
            "port_name": self.port_name,
            "action_id": self.action_id,
            "username": self.username,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "closed_at": self.closed_at,
            "closed": self.closed_at is not None,
            "error": self.error,
            "operator_client_id": self.operator_client_id,
        }


def _format_event_for_log(event: Dict[str, Any]) -> str:
    """Render a structured event as one persisted-transcript line (mirrors the terminal
    "detail" text `console.js`'s `sock.onmessage` builds for the same event types, so the
    on-disk transcript reads the same as the live view - keep both in sync if either changes.
    """
    name = event.get("event", "")
    if name == "progress":
        step = event.get("step") or ""
        percent = event.get("percent")
        return f"progress {step}{f' ({percent}%)' if percent is not None else ''}"
    if name == "waiting_for_operator":
        step = event.get("step")
        prompt = event.get("prompt") or ""
        return f"waiting_for_operator {f'{step}: ' if step else ''}{prompt}"
    if name == "operator_prompt_closed":
        return f"operator_prompt_closed ({event.get('reason') or ''})"
    if name == "action_finished":
        status = event.get("status") or ""
        error = event.get("error")
        return f"action_finished {status}{f': {error}' if error else ''}"
    if name == "operator_changed":
        return f"operator_changed operator={event.get('operator_client_id') or ''}"
    # action_started and freetext log() messages: the event name/message is self-contained.
    return name


class ActionRunner:
    """Coordinates action-script execution: locking, timeout, and the run registry.

    Only one action may run per port at a time (tracked here), on top of the
    port's own read-write-slot capacity check enforced by `PortManager`.
    """

    def __init__(
        self,
        port_manager: Any,
        console_manager: Optional[Any] = None,
        site_event_publisher: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.port_manager = port_manager
        # Optional: lets self-demote/auto-restore notify the affected browser client
        # directly (see docs/design/port_actions.md "Locking"); runner still works
        # without it, just without that client-facing notification.
        self.console_manager = console_manager
        # Optional: forwards `action_status` frames to the web console's site event
        # hub (/ws/events) so the sidebar console list can annotate run outcomes
        # (see docs/design/port_actions.md "Sidebar status annotations"). The CLI
        # runner (scripts/run_action.py) passes nothing and stays silent.
        self.site_event_publisher = site_event_publisher
        self.runs: Dict[str, ActionRun] = {}
        self._active_by_port: Dict[str, str] = {}
        # The port's finished-but-not-yet-closed run (issue #90 slice 2), kept separate
        # from _active_by_port so it never participates in the one-run-per-port lock check.
        self._open_finished_by_port: Dict[str, str] = {}
        self._tasks: Dict[str, "asyncio.Task[None]"] = {}
        self._subscribers: Dict[str, list] = {}
        self._sessions: Dict[str, ActionSession] = {}
        # The prompt id each run is currently blocked on (see `_next_prompt_id()`). A viewer needs
        # it to tell that an `operator_prompt_closed` frame retires the prompt box it shows,
        # rather than an older one from the replayed history.
        self._pending_prompt_id: Dict[str, str] = {}
        self._prompt_seq = 0

    def get_active_run(self, port_name: str) -> Optional[ActionRun]:
        """Return the in-progress `ActionRun` for `port_name`, if any."""
        run_id = self._active_by_port.get(port_name)
        return self.runs.get(run_id) if run_id else None

    def get_open_finished_run(self, port_name: str) -> Optional[ActionRun]:
        """Return the port's finished-but-not-closed run (issue #90 slice 2), if any.

        Surfaced to the console as the catalog's `open_finished_run` so a viewer who
        arrives after the run ended still sees its transcript and outcome until the
        operator closes it. Never blocks a new launch (see `launch_run`).
        """
        run_id = self._open_finished_by_port.get(port_name)
        return self.runs.get(run_id) if run_id else None

    def launch_run(
        self,
        action: ActionScript,
        port_name: str,
        params: Dict[str, Any],
        username: str,
        *,
        requesting_client_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> ActionRun:
        """Validate, register, and start `action` running in the background; returns immediately.

        The returned `ActionRun` has `status == "running"`. Use `get_active_run()`/`self.runs`
        to poll, or `subscribe()` for a live structured-event stream. Raises synchronously
        (before any background work starts) for the same reasons as `start_run`.
        """
        if self.get_active_run(port_name) is not None:
            raise PortBusyError(f"An action is already running on port {port_name}")

        # A new run settles the port's still-open finished run for everyone (issue #90
        # slice 2): it is auto-closed the same way an operator close would be, but the
        # broadcast must not re-open anyone's pane - the client's 'closed' handler only
        # stamps existing state. Fire-and-forget: launch_run stays synchronous.
        prior_id = self._open_finished_by_port.pop(port_name, None)
        if prior_id is not None:
            prior = self.runs.get(prior_id)
            if prior is not None and prior.closed_at is None:
                prior.closed_at = time.time()
                asyncio.ensure_future(self.broadcast_run_closed(prior, ""))

        validated = validate_params(action, params)

        run_id = uuid.uuid4().hex[:12]
        run = ActionRun(
            run_id=run_id,
            port_name=port_name,
            action_id=action.id,
            username=username,
            params=redact_params(action, validated),
            client_id=f"action:{run_id}",
            operator_client_id=requesting_client_id,
        )
        self.runs[run_id] = run
        self._active_by_port[port_name] = run_id
        self._subscribers[run_id] = []
        self._tasks[run_id] = asyncio.ensure_future(self._execute(run, action, validated, requesting_client_id, timeout))
        return run

    async def start_run(
        self,
        action: ActionScript,
        port_name: str,
        params: Dict[str, Any],
        username: str,
        *,
        requesting_client_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> ActionRun:
        """Validate, lock, run `action` against `port_name`, and return the completed `ActionRun`.

        Args:
            action: The loaded action script to execute.
            port_name: Target port.
            params: Raw (unvalidated) user-supplied parameters.
            username: Identity to attribute the run to.
            requesting_client_id: If set and this client currently holds the
                port's read-write slot, it is demoted to make room for the
                action and auto-restored to read-write when the run ends.
            timeout: Overrides `action.timeout` when set.

        Raises:
            PortBusyError: another action is already running on the port, or
                the read-write slot could not be obtained.
            ActionValidationError: `params` fail `action`'s declared metadata.
        """
        run = self.launch_run(action, port_name, params, username, requesting_client_id=requesting_client_id, timeout=timeout)
        task = self._tasks.pop(run.run_id, None)
        if task is not None:
            await task
        if run.exception is not None:
            raise run.exception
        return run

    def subscribe(self, run_id: str) -> "asyncio.Queue[Dict[str, Any]]":
        """Return a new queue receiving this run's structured events until it finishes.

        The final event delivered has `event == "action_finished"`; callers should stop
        reading after that. Raises KeyError if `run_id` is unknown.
        """
        if run_id not in self.runs:
            raise KeyError(f"Unknown run {run_id}")
        queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=200)
        self._subscribers.setdefault(run_id, []).append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: "asyncio.Queue[Dict[str, Any]]") -> None:
        """Stop delivering events for `run_id` to `queue` (safe to call more than once)."""
        subs = self._subscribers.get(run_id)
        if subs and queue in subs:
            subs.remove(queue)

    def submit_operator_input(self, run_id: str, text: str, *, requesting_client_id: Optional[str] = None) -> bool:
        """Feed operator-supplied text to a pending `wait_for_input`/`confirm` call.

        Only the run's `operator_client_id` (the client that launched it, see
        docs/design/port_actions.md "Operator input") may do this once `operator_client_id`
        is set; other callers are silently ignored. Returns False if the run/session is
        unknown or the caller isn't authorized, True once the text has been delivered.

        Nothing is announced here: the resumed script reports the answer and closes the waiting
        state itself (see `ActionSession._finish_prompt()`), so a rejected or unread answer never
        looks answered to any viewer.
        """
        run = self.runs.get(run_id)
        session = self._sessions.get(run_id)
        if run is None or session is None:
            return False
        if run.operator_client_id is not None and requesting_client_id != run.operator_client_id:
            return False
        session.submit_operator_input(text)
        return True

    def cancel_run(self, run_id: str, *, requesting_client_id: Optional[str] = None) -> bool:
        """Stop a running action mid-execution (see docs/design/port_actions.md "Stopping a run").

        Cancels the run's background `asyncio.Task`; `_execute()` catches the resulting
        `CancelledError`, sets `status = "cancelled"`, and runs its normal cleanup
        (detach client, restore auto-demoted launcher, publish `action_finished`) exactly
        like any other run outcome. Same permission model as `submit_operator_input()`:
        only the run's `operator_client_id` may cancel it once one is assigned. Returns
        False for an unknown run, one that isn't running, or an unauthorized caller.
        """
        run = self.runs.get(run_id)
        task = self._tasks.get(run_id)
        if run is None or run.status != "running" or task is None:
            return False
        if run.operator_client_id is not None and requesting_client_id != run.operator_client_id:
            return False
        task.cancel()
        return True

    def iter_active_runs(self):
        """Yield each in-progress `ActionRun`, one per busy port (registry view)."""
        for run_id in self._active_by_port.values():
            run = self.runs.get(run_id)
            if run is not None:
                yield run

    def iter_open_finished_runs(self):
        """Yield each port's finished-but-not-closed `ActionRun` (issue #90 slice 2).

        These are the runs still awaiting their operator's close; the sidebar
        status endpoint annotates ports from exactly this registry, so closing
        or superseding a run removes its dot everywhere.
        """
        for run_id in self._open_finished_by_port.values():
            run = self.runs.get(run_id)
            if run is not None:
                yield run

    def close_run(self, run_id: str, *, requesting_client_id: Optional[str] = None) -> str:
        """Close (ack) a finished run for everyone (issue #90 slice 2).

        Only the run's operator may close it; a run with no operator assigned (e.g. a
        CLI launch) may be closed by any caller - same rule as `cancel_run`. Returns a
        result code the web handler maps to a status: "closed" | "denied" | "running"
        | "already_closed" | "unknown". A still-running run cannot be closed (stop it
        first); closing is final - a closed run is never reported again and starts
        nothing.
        """
        run = self.runs.get(run_id)
        if run is None:
            return "unknown"
        if run.status == "running":
            return "running"
        if run.closed_at is not None:
            return "already_closed"
        if run.operator_client_id is not None and run.operator_client_id != requesting_client_id:
            return "denied"
        run.closed_at = time.time()
        if self._open_finished_by_port.get(run.port_name) == run_id:
            del self._open_finished_by_port[run.port_name]
        # Sidebar annotations (docs/design/port_actions.md "Sidebar status
        # annotations"): an outcome dot stays until its run is closed, so every
        # site viewer must learn about the close. The client answers a `closed`
        # frame with a status re-fetch, like `cancelled`.
        self._emit_site_status(run, "closed")
        return "closed"

    async def broadcast_run_closed(self, run: ActionRun, closed_by: Optional[str]) -> None:
        """Push an `action_run: closed` notice to every viewer of the run's port.

        Tells attached consoles the execution is finished and closed (acknowledged),
        not merely hidden from one pane (issue #90 slice 2). `closed_by` is the
        closing client id, or "" for the auto-close that starts-with-a-new-run does.
        """
        if self.console_manager is None or not hasattr(self.console_manager, "broadcast_control_frame_to_port"):
            return
        try:
            await self.console_manager.broadcast_control_frame_to_port(
                run.port_name,
                {
                    "type": "action_run",
                    "event": "closed",
                    "run_id": run.run_id,
                    "action_id": run.action_id,
                    "closed_by": closed_by or "",
                },
            )
        except Exception:
            logger.debug("Failed to broadcast action_run closed for run %s", run.run_id, exc_info=True)

    def take_over_operator(self, run_id: str, new_client_id: str) -> bool:
        """Reassign who owns this run (mirrors "Take control").

        Any connected viewer may take over - there is no separate permission check beyond
        being a WS client with an identified `client_id`, matching the allow-list-only
        security model used elsewhere in this feature. The previous operator is notified
        the same way every other viewer learns about it: an `operator_changed` structured
        event on the run's own live event stream.

        Also works on a finished-but-not-closed run (issue #90 slice 2): ownership of
        an open run is what lets another user close it after it ended - the ticket's
        "take over as operator, then close" path. A *closed* run can no longer be taken
        over.

        A pending read-write restore (issue #90: the launcher was self-demoted, see
        `_attach_read_write`) follows the role: the operator at finish is the client
        `_detach_and_restore` promotes, so `auto_demoted_client_id` moves to
        `new_client_id`. If the new operator disconnects before the run ends, the
        `still_attached` check in `_detach_and_restore` leaves the slot unclaimed
        (first-come first-served), same as before. The restore has already run for a
        finished run, so the transfer only applies while the run is still running.

        Returns False for an unknown/closed run or an empty `new_client_id`.
        """
        run = self.runs.get(run_id)
        if run is None or run.closed_at is not None or not new_client_id:
            return False
        previous = run.operator_client_id
        run.operator_client_id = new_client_id
        if run.status == "running" and run.auto_demoted_client_id and run.auto_demoted_client_id != new_client_id:
            run.auto_demoted_client_id = new_client_id
        self._publish(
            run_id,
            {
                "event": "operator_changed",
                "run_id": run_id,
                "operator_client_id": new_client_id,
                "previous_operator_client_id": previous,
                "ts": time.time(),
            },
        )
        return True

    def _next_prompt_id(self, run_id: str) -> str:
        """Return a fresh id for the prompt `run_id` is about to block on.

        Ids are unique within a run only, which is all the client needs: it compares an
        `operator_prompt_closed` id against the box it currently shows.
        """
        self._prompt_seq += 1
        prompt_id = f"{run_id}-{self._prompt_seq}"
        self._pending_prompt_id[run_id] = prompt_id
        return prompt_id

    def _close_prompt(self, run_id: str, reason: str) -> None:
        """Publish the close of the prompt `run_id` was blocked on (`reason`: answered | expired).

        This frame is a pure state change and carries no answer text: the answer itself travels as
        an ordinary `session.log()` line (see `ActionSession._finish_prompt()`), so every viewer
        reads it like any other milestone. `_publish()` appends to `run.events`, so a late
        joiner's replay pairs each `waiting_for_operator` with its close instead of showing a
        prompt box nobody can answer.
        """
        self._publish(
            run_id,
            {
                "event": "operator_prompt_closed",
                "run_id": run_id,
                "prompt_id": self._pending_prompt_id.pop(run_id, None),
                "reason": reason,
                "ts": time.time(),
            },
        )

    def _publish(self, run_id: str, event: Dict[str, Any]) -> None:
        run = self.runs.get(run_id)
        if run is not None:
            run.events.append(event)
            try:
                DataLogger.get().record_meta(
                    port_name=run.log_port_name,
                    event=_format_event_for_log(event),
                    client_id=run.client_id,
                )
            except Exception:
                logger.error("Failed to record action event %r for run %s", event.get("event"), run_id, exc_info=True)
        for queue in list(self._subscribers.get(run_id, [])):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Dropping action event for run %s: subscriber queue full", run_id)

    def _log_debug(self, run: ActionRun, text: str) -> None:
        """Write a debug-only line straight to the run's transcript (see docs/design/
        port_actions.md "Persisted log") - unlike `_publish()`, never appended to
        `run.events` or pushed to WS subscribers, so it never reaches the live console.
        """
        try:
            DataLogger.get().record_meta(port_name=run.log_port_name, event=text, client_id=run.client_id)
        except Exception:
            logger.error("Failed to record debug info for run %s", run.run_id, exc_info=True)

    def _build_power_context(self, run: ActionRun) -> Optional[_PowerContext]:
        """Return the run's power context, or None when power control is unavailable.

        The active PDU adapter (issue #93); None when none is configured or the
        runner has no console manager to search. A configured-but-disabled PDU
        section still returns a context: `set_outlet` refuses with its own
        "power management is disabled" error, which the session reports as
        `ActionPowerError`.
        """
        if self.console_manager is None:
            return None
        try:
            pdu = find_power_adapter(self.console_manager)
        except Exception:
            # justification: optional capability; scripts get a typed error instead
            logger.debug("Power adapter lookup failed for run %s", run.run_id, exc_info=True)
            return None
        if pdu is None:
            return None
        return _PowerContext(pdu=pdu, username=run.username, client_id=run.client_id)

    async def _execute(
        self,
        run: ActionRun,
        action: ActionScript,
        validated: Dict[str, Any],
        requesting_client_id: Optional[str],
        timeout: Optional[float],
    ) -> None:
        """Attach, run the script, detach. Never raises; outcome is reflected on `run`."""
        port_name = run.port_name
        try:
            await self._attach_read_write(run, requesting_client_id)
            # Issue #94: while the run holds the port, mirror the port's own log
            # stream (device data and lifecycle meta, subject to the port's
            # log_direction filters) into the run's transcript file, so the
            # per-run file is a self-contained record of the device conversation.
            try:
                DataLogger.get().set_mirror(port_name, run.log_port_name)
            except Exception:
                logger.error("Failed to enable log mirroring for run %s", run.run_id, exc_info=True)
            self._notify(port_name, "action_started", run)
            self._publish(run.run_id, {"event": "action_started", "run_id": run.run_id, "ts": time.time()})
            await self._broadcast_action_run_event(run, action, "action_started")
            self._emit_site_status(run, "running")
            session = ActionSession(
                self.port_manager,
                port_name,
                run.client_id,
                params=validated,
                # Issue #93: power control for session.power_*(). Looked up once
                # per run (never cached) so a soft reload of `power:` applies to
                # the next run. None when no PDU adapter is active: list_power()
                # then reports unavailable and the switch calls raise.
                power_context=self._build_power_context(run),
                on_input_wait=lambda prompt, kind, choices, step, color: self._publish(
                    run.run_id,
                    {
                        "event": "waiting_for_operator",
                        "prompt": prompt,
                        "kind": kind,
                        "choices": choices,
                        "step": step,
                        "color": color,
                        "prompt_id": self._next_prompt_id(run.run_id),
                        "ts": time.time(),
                    },
                ),
                on_input_end=lambda reason: self._close_prompt(run.run_id, reason),
                on_progress=lambda step, percent: self._publish(
                    run.run_id,
                    {
                        "event": "progress",
                        "step": step,
                        "percent": percent,
                        "ts": time.time(),
                    },
                ),
                on_debug=lambda message: self._log_debug(run, message),
                on_log=lambda message: self._publish(run.run_id, {"event": message, "ts": time.time()}),
            )
            self._sessions[run.run_id] = session
            run_timeout = action.timeout if timeout is None else timeout
            try:
                await asyncio.wait_for(action.run_func(session), timeout=run_timeout)
                run.status = "success"
            except asyncio.TimeoutError:
                run.status = "timeout"
                run.error = f"Action timed out after {run_timeout}s"
                run.exception = ActionTimeoutError(run.error)
                logger.warning(
                    "Action %s run %s on port %s timed out after %ss", run.action_id, run.run_id, port_name, run_timeout
                )
                session.log(f"action_timeout: {run.error}")
            except asyncio.CancelledError:
                run.status = "cancelled"
                run.error = "Cancelled by user"
                logger.info("Action %s run %s on port %s was cancelled", run.action_id, run.run_id, port_name)
                session.log(f"action_cancelled: {run.error}")
            except Exception as exc:
                run.status = "failed"
                run.error = str(exc)
                run.exception = exc
                # exc_info=True so a genuine script/backend bug leaves a full traceback in
                # the server log, not just the short message surfaced to the UI/run history.
                logger.error(
                    "Action %s run %s on port %s failed: %s", run.action_id, run.run_id, port_name, exc, exc_info=True
                )
                session.log(f"action_error: {type(exc).__name__}: {exc}")
                self._log_debug(run, f"buffer_at_crash: {_truncate(session.read_buffer())!r}")
                self._log_debug(run, f"traceback:\n{_truncate(traceback.format_exc())}")
        except PortBusyError as exc:
            run.status = "failed"
            run.error = str(exc)
            run.exception = exc
            logger.warning("Action %s run %s on port %s could not start: %s", run.action_id, run.run_id, port_name, exc)
        finally:
            run.ended_at = time.time()
            await self._detach_and_restore(run)
            # Release the one-run lock and register the finished-but-not-closed state in
            # one synchronous step (issue #90 slice 2): a launch that interleaves here
            # either sees the lock held (before the pop) or sees the open-finished entry
            # (after) and auto-closes it. No await may sit between the two lines.
            self._active_by_port.pop(port_name, None)
            self._open_finished_by_port[port_name] = run.run_id
            self._sessions.pop(run.run_id, None)
            self._pending_prompt_id.pop(run.run_id, None)
            self._notify(port_name, "action_finished", run)
            self._publish(
                run.run_id,
                {
                    "event": "action_finished",
                    "run_id": run.run_id,
                    "status": run.status,
                    "error": run.error,
                    "ts": time.time(),
                },
            )
            await self._broadcast_action_run_event(run, action, "action_finished")
            # Emit for every outcome, `cancelled` included: a cancelled run ends its own
            # "running" pulse but is not an outcome for the port, so the client answers a
            # `cancelled` frame with a status re-fetch (the previous outcome shows again).
            self._emit_site_status(run, run.status)
            self._subscribers.pop(run.run_id, None)
            # Last, so the action client's own detach meta still reaches the run
            # transcript. The `log_port_name` guard leaves a newer run's mirror
            # on the same port untouched.
            try:
                DataLogger.get().clear_mirror(port_name, run.log_port_name)
            except Exception:
                # justification: best-effort mirror teardown; the run already finished
                logger.debug("Failed to clear log mirror for run %s", run.run_id, exc_info=True)

    def _emit_site_status(self, run: ActionRun, state: str) -> None:
        """Publish one `action_status` frame for `run` to the site event hub.

        Small frame on purpose (the client only re-fetches on it); best-effort:
        annotations re-sync from `GET /api/port-actions/status` whenever a
        browser (re)connects, so a lost frame never strands the UI.
        """
        if self.site_event_publisher is None:
            return
        try:
            self.site_event_publisher(
                {
                    "type": "action_status",
                    "port": run.port_name,
                    "action_id": run.action_id,
                    "run_id": run.run_id,
                    "state": state,
                    "ts": time.time(),
                    "error": run.error,
                }
            )
        except Exception:
            # justification: site annotation fan-out is advisory; the UI re-syncs on reload
            logger.debug("Site event publish failed for run %s", run.run_id, exc_info=True)

    def _federated_port(self, port_name: str) -> Optional[Any]:
        """Return the port object if `port_name` is a federated (muxcon) port, else None.

        A federated port's origin server is authoritative for the shared
        read-write slot (issue #52): the proxy here only mirrors clients
        attached at this hop, so a local read-write attach is necessary but not
        sufficient - the origin must grant the slot via a FEDRW request.
        """
        ports = getattr(self.port_manager, "ports", None)
        port = ports.get(port_name) if isinstance(ports, dict) else None
        if port is not None and hasattr(port, "remote_port_name"):
            return port
        return None

    async def _request_origin_grant(self, port: Any, client_id: str, port_name: str) -> bool:
        """Ask the port's origin to grant `client_id` the shared read-write slot.

        Returns True when the origin reports the client read-write, or when the
        port is not federated (nothing to ask). Fails closed - False on denial,
        timeout, link failure, or an origin that cannot arbitrate - never
        optimistically grants across a federation link.
        """
        request = getattr(port, "request_read_write_for_client", None)
        if request is None:
            return True
        try:
            mode = await request(client_id)
        except Exception:
            logger.warning("FEDRW request failed for %s on federated port %s", client_id, port_name, exc_info=True)
            return False
        if mode != "read-write":
            logger.info("Origin denied read-write slot for %s on federated port %s (mode=%s)", client_id, port_name, mode)
            return False
        return True

    async def _release_origin_slot(self, port: Any, client_id: str, port_name: str) -> None:
        """Tell the port's origin that `client_id` gives up the read-write slot.

        Best-effort: the local demotion that follows proceeds regardless, and a
        stale origin-side slot frees itself when the client's stream closes.
        """
        release = getattr(port, "release_read_write_for_client", None)
        if release is None:
            return
        try:
            await release(client_id)
        except Exception:  # justification: best-effort origin release; local demotion still proceeds
            logger.debug("FEDRW release failed for %s on federated port %s", client_id, port_name, exc_info=True)

    async def _attach_read_write(self, run: ActionRun, requesting_client_id: Optional[str]) -> None:
        """Attach the action's client as read-write, self-demoting the launcher if needed.

        On a federated (muxcon) port the origin server arbitrates the shared
        read-write slot (issue #52). The launcher's origin slot (if any) is
        released before the action requests its own, and a denied grant fails
        the run fast - before any keystroke is sent - so a script can never
        run against a port it has no write access to.
        """
        port_name = run.port_name
        port = self._federated_port(port_name)
        if port is not None and not bool(getattr(port, "is_connected", False)):
            raise PortBusyError(f"Federated link for port {port_name} is down; cannot run action {run.action_id}")
        attached = await self.port_manager.add_client_to_port(
            port_name, run.client_id, username=f"action:{run.action_id}", mode="read-write"
        )
        if not attached and requesting_client_id:
            if self.port_manager.get_client_mode(requesting_client_id, port_name) == "read-write":
                await self._release_origin_slot(port, requesting_client_id, port_name)
                await self.port_manager.demote_client(port_name, requesting_client_id)
                run.auto_demoted_client_id = requesting_client_id
                await self._notify_client_mode(requesting_client_id, mode="read-only", ok=False, reason="action_self_demoted")
                attached = await self.port_manager.add_client_to_port(
                    port_name, run.client_id, username=f"action:{run.action_id}", mode="read-write"
                )
        if not attached:
            raise PortBusyError(f"Port {port_name} read-write slot is unavailable for action {run.action_id}")
        if port is not None and not await self._request_origin_grant(port, run.client_id, port_name):
            # The origin keeps the slot for whoever holds it (local or another
            # federated peer); never preempt - detach and fail with a clear error.
            await self.port_manager.remove_client_from_port(port_name, run.client_id)
            raise PortBusyError(
                f"Origin server did not grant the read-write slot for port {port_name}; "
                f"action {run.action_id} cannot run while another client holds it"
            )

    async def _broadcast_action_run_event(self, run: ActionRun, action: ActionScript, event: str) -> None:
        """Push a live "script started/finished" notice to every viewer of this run's port.

        Lets a console that hasn't joined the run (and isn't the launcher) show the
        "Script running" strip immediately (see docs/design/port_actions.md "Live view"),
        instead of only learning about it from the next full page load's catalog fetch.
        A client that already has this run's own WS stream open ignores the notice
        client-side, so this doesn't affect the launcher or anyone who already joined.
        """
        if self.console_manager is None or not hasattr(self.console_manager, "broadcast_control_frame_to_port"):
            return
        try:
            payload = {
                "type": "action_run",
                "event": event,
                "run_id": run.run_id,
                "action_id": run.action_id,
                "action_name": action.name,
                "operator_client_id": run.operator_client_id,
            }
            if event == "action_finished":
                # Issue #90 slice 2: a viewer who never joined the run words its
                # finished-state chip from this broadcast (the run stays open for
                # everyone until its operator closes it).
                payload["status"] = run.status
                payload["error"] = run.error
            await self.console_manager.broadcast_control_frame_to_port(run.port_name, payload)
        except Exception:
            logger.debug("Failed to broadcast action_run event %s for run %s", event, run.run_id, exc_info=True)

    async def _notify_client_mode(self, client_id: str, *, mode: str, ok: bool, reason: str) -> None:
        """Push a `client_mode` control frame to `client_id`, if a console_manager is set."""
        if self.console_manager is None:
            return
        try:
            await self.console_manager.send_control_frame_to_client(
                client_id, {"type": "client_mode", "ok": ok, "mode": mode, "reason": reason}
            )
        except Exception:
            logger.debug("Failed to notify client %s of mode change (%s)", client_id, reason, exc_info=True)

    async def _detach_and_restore(self, run: ActionRun) -> None:
        """Detach the action's client and restore any self-demoted launcher (finally-safe).

        On a federated (muxcon) port the launcher's read-write slot lives on
        the origin (issue #52): the restore re-requests it from the origin and
        promotes locally only when granted, so a local read-write marker can
        never outlive the origin's grant. If the slot moved on in the meantime,
        the launcher stays read-only (first-come first-served, per design).
        """
        try:
            await self.port_manager.remove_client_from_port(run.port_name, run.client_id)
        except Exception:
            logger.error("Failed to detach action client %s from %s", run.client_id, run.port_name, exc_info=True)
        if run.auto_demoted_client_id:
            still_attached = self.port_manager.get_client_mode(run.auto_demoted_client_id, run.port_name) is not None
            if still_attached:
                port = self._federated_port(run.port_name)
                if port is not None and not await self._request_origin_grant(port, run.auto_demoted_client_id, run.port_name):
                    logger.info(
                        "Origin did not re-grant read-write for %s on %s after run %s; leaving read-only",
                        run.auto_demoted_client_id,
                        run.port_name,
                        run.run_id,
                    )
                    await self._notify_client_mode(
                        run.auto_demoted_client_id, mode="read-only", ok=False, reason="action_restore_denied"
                    )
                    return
                try:
                    await self.port_manager.promote_client(run.port_name, run.auto_demoted_client_id)
                    await self._notify_client_mode(
                        run.auto_demoted_client_id, mode="read-write", ok=True, reason="action_restored"
                    )
                except Exception:
                    logger.error(
                        "Failed to auto-restore read-write for %s on %s",
                        run.auto_demoted_client_id,
                        run.port_name,
                        exc_info=True,
                    )

    def _notify(self, port_name: str, event: str, run: ActionRun) -> None:
        """Push an `action_started`/`action_finished` meta event (see get_status().active_action)."""
        try:
            self.port_manager.notify_meta_updated(
                port_name,
                {
                    "event": event,
                    "run_id": run.run_id,
                    "action_id": run.action_id,
                    "status": run.status,
                },
            )
        except Exception:
            logger.error("Failed to notify meta listeners for %s (%s)", port_name, event, exc_info=True)
