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
from openmux.server.actions.session import ActionSession
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
    error: Optional[str] = None
    auto_demoted_client_id: Optional[str] = None
    exception: Optional[BaseException] = field(default=None, repr=False, compare=False)
    # Full structured-event history for the run's lifetime (see docs/design/port_actions.md
    # "Late join"), replayed once to WS subscribers that connect after the run started.
    events: List[Dict[str, Any]] = field(default_factory=list)
    # Who may answer a pending session.wait_for_input()/confirm() prompt (see "Operator
    # input" in the design doc) - the run's launcher by default, reassignable mid-run via
    # ActionRunner.take_over_operator() (mirrors the port's own "Force take read-write").
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

    def __init__(self, port_manager: Any, console_manager: Optional[Any] = None):
        self.port_manager = port_manager
        # Optional: lets self-demote/auto-restore notify the affected browser client
        # directly (see docs/design/port_actions.md "Locking"); runner still works
        # without it, just without that client-facing notification.
        self.console_manager = console_manager
        self.runs: Dict[str, ActionRun] = {}
        self._active_by_port: Dict[str, str] = {}
        self._tasks: Dict[str, "asyncio.Task[None]"] = {}
        self._subscribers: Dict[str, list] = {}
        self._sessions: Dict[str, ActionSession] = {}

    def get_active_run(self, port_name: str) -> Optional[ActionRun]:
        """Return the in-progress `ActionRun` for `port_name`, if any."""
        run_id = self._active_by_port.get(port_name)
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
        """
        run = self.runs.get(run_id)
        session = self._sessions.get(run_id)
        if run is None or session is None:
            return False
        if run.operator_client_id is not None and requesting_client_id != run.operator_client_id:
            return False
        session.submit_operator_input(text)
        self._log_debug(run, f"operator_answered: {_truncate(text)!r}")
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

    def take_over_operator(self, run_id: str, new_client_id: str) -> bool:
        """Reassign who may answer this run's prompts (mirrors "Force take read-write").

        Any connected viewer may take over - there is no separate permission check beyond
        being a WS client with an identified `client_id`, matching the allow-list-only
        security model used elsewhere in this feature. The previous operator is notified
        the same way every other viewer learns about it: an `operator_changed` structured
        event on the run's own live event stream. Returns False for an unknown/finished
        run or an empty `new_client_id`.
        """
        run = self.runs.get(run_id)
        if run is None or run.status != "running" or not new_client_id:
            return False
        previous = run.operator_client_id
        run.operator_client_id = new_client_id
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
            self._notify(port_name, "action_started", run)
            self._publish(run.run_id, {"event": "action_started", "run_id": run.run_id, "ts": time.time()})
            await self._broadcast_action_run_event(run, action, "action_started")
            session = ActionSession(
                self.port_manager,
                port_name,
                run.client_id,
                on_input_wait=lambda prompt, kind, choices, step, color: self._publish(
                    run.run_id,
                    {
                        "event": "waiting_for_operator",
                        "prompt": prompt,
                        "kind": kind,
                        "choices": choices,
                        "step": step,
                        "color": color,
                        "ts": time.time(),
                    },
                ),
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
            )
            self._sessions[run.run_id] = session
            run_timeout = action.timeout if timeout is None else timeout
            log = self._make_log_func(run)
            try:
                await asyncio.wait_for(action.run_func(session, validated, log), timeout=run_timeout)
                run.status = "success"
            except asyncio.TimeoutError:
                run.status = "timeout"
                run.error = f"Action timed out after {run_timeout}s"
                run.exception = ActionTimeoutError(run.error)
                logger.warning(
                    "Action %s run %s on port %s timed out after %ss", run.action_id, run.run_id, port_name, run_timeout
                )
                log(f"action_timeout: {run.error}")
            except asyncio.CancelledError:
                run.status = "cancelled"
                run.error = "Cancelled by user"
                logger.info("Action %s run %s on port %s was cancelled", run.action_id, run.run_id, port_name)
                log(f"action_cancelled: {run.error}")
            except Exception as exc:
                run.status = "failed"
                run.error = str(exc)
                run.exception = exc
                # exc_info=True so a genuine script/backend bug leaves a full traceback in
                # the server log, not just the short message surfaced to the UI/run history.
                logger.error(
                    "Action %s run %s on port %s failed: %s", run.action_id, run.run_id, port_name, exc, exc_info=True
                )
                log(f"action_error: {type(exc).__name__}: {exc}")
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
            self._active_by_port.pop(port_name, None)
            self._sessions.pop(run.run_id, None)
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
            self._subscribers.pop(run.run_id, None)

    async def _attach_read_write(self, run: ActionRun, requesting_client_id: Optional[str]) -> None:
        """Attach the action's client as read-write, self-demoting the launcher if needed."""
        port_name = run.port_name
        attached = await self.port_manager.add_client_to_port(
            port_name, run.client_id, username=f"action:{run.action_id}", mode="read-write"
        )
        if not attached and requesting_client_id:
            if self.port_manager.get_client_mode(requesting_client_id, port_name) == "read-write":
                await self.port_manager.demote_client(port_name, requesting_client_id)
                run.auto_demoted_client_id = requesting_client_id
                await self._notify_client_mode(requesting_client_id, mode="read-only", ok=False, reason="action_self_demoted")
                attached = await self.port_manager.add_client_to_port(
                    port_name, run.client_id, username=f"action:{run.action_id}", mode="read-write"
                )
        if not attached:
            raise PortBusyError(f"Port {port_name} read-write slot is unavailable for action {run.action_id}")

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
            await self.console_manager.broadcast_control_frame_to_port(
                run.port_name,
                {
                    "type": "action_run",
                    "event": event,
                    "run_id": run.run_id,
                    "action_id": run.action_id,
                    "action_name": action.name,
                    "operator_client_id": run.operator_client_id,
                },
            )
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
        """Detach the action's client and restore any self-demoted launcher (finally-safe)."""
        try:
            await self.port_manager.remove_client_from_port(run.port_name, run.client_id)
        except Exception:
            logger.error("Failed to detach action client %s from %s", run.client_id, run.port_name, exc_info=True)
        if run.auto_demoted_client_id:
            still_attached = self.port_manager.get_client_mode(run.auto_demoted_client_id, run.port_name) is not None
            if still_attached:
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

    def _make_log_func(self, run: ActionRun) -> Callable[[str], None]:
        """Build the `log(message)` callable passed into `action.run_func` (freetext, like `logging.info()`)."""

        def log(message: str) -> None:
            self._publish(run.run_id, {"event": message, "ts": time.time()})

        return log

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
