"""Expect-style session wrapper for port action scripts.

An `ActionSession` is the `session` argument passed to a script's
`async def run(session)` (see docs/design/port_actions.md,
"Script format"). It reads from the same per-client delivery queue that
`PortManager.add_client_to_port` creates for the action's `client_id`, so it
sees exactly the bytes a normal read-write console client would see.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from openmux.server.actions.choices import Choice, normalize_choices
from openmux.server.actions.errors import ActionPowerError, ActionSessionError, ActionTimeoutError
from openmux.server.adapters.pdu import split_remote_ref

# Accent colors a script can request for the operator-input prompt border/flash (see
# `prompt()`'s `color` param and docs/design/port_actions.md, "Operator input").
# "none" (the default) keeps the console's built-in attention color.
VALID_PROMPT_COLORS = {"none", "red", "green", "blue", "pink", "yellow", "orange", "purple"}

logger = logging.getLogger("openmux.server.actions.session")


def _truncate(text: str, limit: int = 2000) -> str:
    """Cap `text` for a debug-log line; port output can be arbitrarily large/noisy."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"...(truncated, {len(text)} chars total)"


@dataclass
class _PowerContext:
    """PDU adapter + operator identity for one session's power calls (issue #93).

    The runner builds one per run (the active PDU adapter, the run's username,
    and the run's `action:<run-id>` client id for the POWER CONTROL audit line)
    and injects it into `ActionSession`. None (no active PDU adapter) makes
    `list_power()` report `{"available": False}` and the switch calls raise
    `ActionPowerError`.
    """

    pdu: Any
    username: str
    client_id: str


class ActionSession:
    """Send/expect interface bound to one port attachment (one `client_id`)."""

    def __init__(
        self,
        port_manager,
        port_name: str,
        client_id: str,
        params: Optional[Dict[str, Any]] = None,
        power_context: Optional[_PowerContext] = None,
        on_input_wait: Optional[
            Callable[[Optional[str], str, Optional[List[Dict[str, str]]], Optional[str], str], None]
        ] = None,
        on_input_end: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[str, Optional[int]], None]] = None,
        on_debug: Optional[Callable[[str], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ):
        self.port_manager = port_manager
        self.port_name = port_name
        self.client_id = client_id
        # The run's user-supplied inputs, validated against the ACTION metadata
        # before the script runs (see docs/design/port_actions.md, "Script format").
        self.params: Dict[str, Any] = params if params is not None else {}
        self._buffer = bytearray()
        self._operator_input: "asyncio.Queue[str]" = asyncio.Queue()
        # Called (sync) from log() with the script's operator-facing message.
        # The runner wires this to its _publish() so the line reaches both the
        # live console UI (run.events / WS subscribers) and the persisted log.
        self._on_log = on_log
        # Called (sync) each time wait_for_input()/confirm() starts waiting, so the
        # caller can surface a "waiting_for_operator" structured event (see
        # docs/design/port_actions.md "Operator input"). Receives the current step
        # (last set via progress(), if any) and the prompt's accent color, so the
        # event is self-contained.
        self._on_input_wait = on_input_wait
        # Called (sync) when a prompt stops waiting, with "answered" or "expired". Without it a
        # `waiting_for_operator` event is never superseded, so every viewer - and every history
        # replay - keeps a prompt box for a prompt nobody can answer any more.
        self._on_input_end = on_input_end
        # Called (sync) from progress(), so the caller can surface a "progress"
        # structured event. Separate from on_input_wait: progress is the script's own
        # step/percent reporting, waiting-for-operator is an orthogonal paused state
        # that does not advance (or reset) it.
        self._on_progress = on_progress
        # Called (sync) with a one-line debug message from send()/sendline()/expect() (matched
        # text, timeout buffer contents) - written straight to the persisted transcript (see
        # docs/design/port_actions.md "Persisted log"), never surfaced to WS subscribers/the
        # live console, so it's safe to be noisier than the operator-facing events above.
        self._on_debug = on_debug
        self._current_step: Optional[str] = None
        # PDU power control for this session's port (issue #93); None until the
        # runner injects it (see `_PowerContext`).
        self._power = power_context

    def _debug(self, message: str) -> None:
        if self._on_debug is not None:
            try:
                self._on_debug(message)
            except Exception:
                # justification: best-effort UI callback; the action flow continues
                pass

    def _end_input_wait(self, reason: str) -> None:
        """Report that the pending prompt is over: `reason` is "answered" or "expired"."""
        if self._on_input_end is None:
            return
        try:
            self._on_input_end(reason)
        except Exception:
            # justification: best-effort UI callback; the action flow continues
            pass

    def _finish_prompt(self, answer: str) -> str:
        """Close an answered prompt and hand `answer` back to the script.

        The waiting state ends first, then the answer is reported with `log()` - an ordinary
        run line, so it reaches the live view and the persisted transcript like any other
        milestone. Order matters: the answer must be the line a viewer sees last.
        """
        self._end_input_wait("answered")
        self.log(f"operator_answered: {answer!r}")
        return answer

    def _client_queue(self) -> Optional[asyncio.Queue]:
        port = self.port_manager.ports.get(self.port_name)
        if port is None:
            return None
        return getattr(port, "client_queues", {}).get(self.client_id)

    def _consume(self, decoded_text: str, end: int) -> None:
        """Drop bytes for `decoded_text[:end]` from `self._buffer` after a match."""
        consumed_len = len(decoded_text[:end].encode("utf-8", errors="replace"))
        del self._buffer[:consumed_len]

    def clear_buffer(self) -> None:
        """Discard any buffered inbound bytes, so the next `expect()` only sees new output."""
        self._buffer.clear()

    def progress(self, step: str, percent: Optional[int] = None) -> None:
        """Report the script's current step, and optionally how far through it is.

        Surfaces a "progress" structured event (see docs/design/port_actions.md,
        "Operator input" and the progress bar notes) - purely informational, separate
        from `prompt()`'s waiting-for-operator state, which pauses without advancing
        (or resetting) the last-reported step/percent.

        `percent`: 0-100, or omit/None for an indeterminate step (still running, no
        known fraction).

        Raises:
            ValueError: `percent` is given but outside 0-100.
        """
        if percent is not None and not (0 <= percent <= 100):
            raise ValueError(f"percent must be between 0 and 100, got {percent!r}")
        self._current_step = step
        if self._on_progress is not None:
            try:
                self._on_progress(step, percent)
            except Exception:
                # justification: best-effort UI callback; the action flow continues
                pass

    def log(self, message: str) -> None:
        """Report an operator-facing line for the run.

        Reaches both the live console UI (the run's event stream) and the
        persisted log file. Use it for the run's milestones - what the
        operator watching the run needs to see. For tracing detail the
        operator does not need, use `debug()` instead (file only).
        """
        if self._on_log is not None:
            try:
                self._on_log(message)
            except Exception:
                # justification: best-effort UI callback; the action flow continues
                pass

    def debug(self, message: str) -> None:
        """Write a debug-only line to the run's persisted log file.

        Unlike `session.log()` (which also reaches the live console UI), this
        goes only to the on-disk transcript (see docs/design/port_actions.md,
        "Persisted log"). Use it for tracing detail an operator watching the
        run does not need: per-line decisions, dialog traffic, buffer
        contents, raw API data.
        """
        self._debug(message)

    def read_buffer(self, *, consume: bool = False) -> str:
        """Return the inbound text seen so far, without waiting for a pattern match.

        Pulls any chunks already sitting in the client's delivery queue (non-blocking)
        into the buffer first, so this reflects output that arrived since the last
        `expect()`/`read_buffer()` call. Pass `consume=True` to also drop the returned
        text from the buffer (like a matched `expect()`), so a later `expect()`/
        `read_buffer()` only sees output that arrives after this call.
        """
        queue = self._client_queue()
        if queue is not None:
            while True:
                try:
                    chunk = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._buffer.extend(chunk)
        text = self._buffer.decode("utf-8", errors="replace")
        if consume:
            self._buffer.clear()
        return text

    async def send(self, text: str) -> None:
        """Write `text` to the port as-is (no newline added)."""
        # Log the send BEFORE issuing the write: an instant-echo port (loopback)
        # echoes inside `write_to_port`, so logging after the await would queue
        # the echo ahead of this line in the run's transcript (issue #94 report).
        # The line also survives a rejected write, recording what was attempted.
        self._debug(f"send: {_truncate(text)!r}")
        ok = await self.port_manager.write_to_port(self.port_name, text.encode("utf-8"), client_id=self.client_id)
        if not ok:
            raise ActionSessionError(f"Write to port {self.port_name} was rejected (client {self.client_id} not read-write?)")

    async def sendline(self, text: str) -> None:
        """Write `text` followed by a newline."""
        await self.send(text + "\n")

    async def expect(self, pattern: str, timeout: float = 10.0) -> str:
        """Wait until `pattern` (a regex) matches the accumulated inbound buffer.

        Checks already-buffered bytes first, then consumes new chunks from the
        client's delivery queue as they arrive. Returns the matched substring.
        Matched bytes (and anything before them) are dropped from the buffer, so a
        later `expect()` with the same pattern must see fresh output, not re-match
        the same stale bytes instantly.

        Raises:
            ActionTimeoutError: `timeout` elapsed before `pattern` matched.
            ActionSessionError: the action has no client queue on this port
                (not attached, or already detached).
        """
        regex = re.compile(pattern)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout

        text = self._buffer.decode("utf-8", errors="replace")
        match = regex.search(text)
        if match:
            self._consume(text, match.end())
            self._debug(f"expect_matched: pattern={pattern!r} matched={_truncate(match.group(0))!r}")
            return match.group(0)

        queue = self._client_queue()
        if queue is None:
            raise ActionSessionError(f"No client queue for {self.client_id} on port {self.port_name}")

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                self._debug(f"expect_timeout: pattern={pattern!r} buffer={_truncate(text)!r}")
                raise ActionTimeoutError(f"Timed out waiting for pattern {pattern!r} on port {self.port_name}")
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                self._debug(f"expect_timeout: pattern={pattern!r} buffer={_truncate(text)!r}")
                raise ActionTimeoutError(f"Timed out waiting for pattern {pattern!r} on port {self.port_name}")
            self._buffer.extend(chunk)
            text = self._buffer.decode("utf-8", errors="replace")
            match = regex.search(text)
            if match:
                self._consume(text, match.end())
                self._debug(f"expect_matched: pattern={pattern!r} matched={_truncate(match.group(0))!r}")
                return match.group(0)

    async def prompt(
        self,
        text: Optional[str] = None,
        *,
        kind: str = "text",
        choices: Optional[List[Choice]] = None,
        color: str = "none",
        timeout: Optional[float] = None,
    ) -> str:
        """Pause until the operator answers, via any of the console UI's input kinds.

        This is the operator channel described in docs/design/port_actions.md
        ("Operator input"), fed by `submit_operator_input()` via the web plugin's
        `/ws/actions/<run_id>` WebSocket (`{"type": "operator_input", "text": ...}`
        frames from the run's launcher).

        `kind`:
          "text"    - free-form single-line input (default).
          "buttons" - `choices` rendered as clickable buttons; picking one answers
                      immediately, no typing needed.
          "select"  - `choices` rendered as a dropdown; the operator picks one and
                      clicks Send.
          "radio"   - `choices` rendered as radio buttons; the operator picks one and
                      clicks Send.
        `choices`: required (non-empty) for "buttons"/"select"/"radio" - a list of plain
          values, or `{"label": ..., "value": ...}` dicts to show a different label
          than the value returned to the script.
        `color`: accent color for the prompt's border/flash - one of
          `VALID_PROMPT_COLORS` (`"none"` for the console's default). Purely visual,
          e.g. to make a destructive confirm() stand out in red.
        """
        if kind in ("buttons", "select", "radio") and not choices:
            raise ValueError(f"kind={kind!r} requires a non-empty choices list")
        if color not in VALID_PROMPT_COLORS:
            raise ValueError(f"color must be one of {sorted(VALID_PROMPT_COLORS)}, got {color!r}")
        normalized_choices = normalize_choices(choices) if choices else None
        if self._on_input_wait is not None:
            try:
                self._on_input_wait(text, kind, normalized_choices, self._current_step, color)
            except Exception:
                logger.error("action _on_input_wait callback failed; the user will not see prompt %r", text, exc_info=True)
        if timeout is None:
            return self._finish_prompt(await self._operator_input.get())
        try:
            return self._finish_prompt(await asyncio.wait_for(self._operator_input.get(), timeout=timeout))
        except asyncio.TimeoutError:
            # The script may catch this timeout and carry on, so the open prompt has to be
            # closed here: nothing else ever tells a viewer the prompt is gone.
            self._end_input_wait("expired")
            self.log(f"operator_input_timeout: {(text or 'no prompt')!r}")
            raise ActionTimeoutError(f"Timed out waiting for operator input ({text or 'no prompt'})")

    async def wait_for_input(self, prompt: Optional[str] = None, color: str = "none", timeout: Optional[float] = None) -> str:
        """Pause until the operator supplies a line of free-form text."""
        return await self.prompt(prompt, kind="text", color=color, timeout=timeout)

    async def confirm(self, prompt: str, color: str = "none", timeout: Optional[float] = None) -> bool:
        """Pause until the operator clicks Yes or No (rendered as buttons)."""
        value = await self.prompt(
            prompt,
            kind="buttons",
            choices=[{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}],
            color=color,
            timeout=timeout,
        )
        return value.strip().lower() in ("y", "yes", "1", "true")

    async def choose(self, prompt: str, choices: List[Choice], color: str = "none", timeout: Optional[float] = None) -> str:
        """Pause until the operator clicks one of `choices` (rendered as buttons)."""
        return await self.prompt(prompt, kind="buttons", choices=choices, color=color, timeout=timeout)

    async def select(self, prompt: str, choices: List[Choice], color: str = "none", timeout: Optional[float] = None) -> str:
        """Pause until the operator picks one of `choices` from a dropdown and sends it."""
        return await self.prompt(prompt, kind="select", choices=choices, color=color, timeout=timeout)

    async def radio(self, prompt: str, choices: List[Choice], color: str = "none", timeout: Optional[float] = None) -> str:
        """Pause until the operator picks one of `choices` from radio buttons and sends it."""
        return await self.prompt(prompt, kind="radio", choices=choices, color=color, timeout=timeout)

    def submit_operator_input(self, text: str) -> None:
        """Feed operator-supplied text to a pending `wait_for_input`/`confirm` call.

        Only delivers the text; `prompt()` reports the answer (and closes the waiting state)
        once the resumed script reads it, so a queued-but-never-read answer never looks answered.
        """
        self._operator_input.put_nowait(text)

    # --- power control (issue #93; docs/design/port_actions.md "Power control") ---------

    def list_power(self) -> Dict[str, Any]:
        """Return this port's power feeds (same payload as the web power menu).

        Returns ``{"available": False}`` - not an error - when no PDU adapter is
        active or the port declares no ``power:`` feeds. Otherwise returns
        ``{"available": True, "feeds": [{ref, on, watts}], "feeds_on",
        "feeds_total", "state", "all_power_lost"}``
        (`PduAdapter.port_power_payload`).
        """
        if self._power is None:
            return {"available": False}
        payload = self._power.pdu.port_power_payload(self.port_name)
        if payload is None:
            return {"available": False}
        return {"available": True, **payload}

    async def power_on(self, ref: Any = None) -> Any:
        """Power on one outlet, a list of outlets, or (ref omitted) all of this port's feeds."""
        return await self._power_switch(ref, True)

    async def power_off(self, ref: Any = None) -> Any:
        """Power off one outlet, a list, or (ref omitted) all of this port's feeds.

        There is no separate power-all call: omitting `ref` switches every feed
        this port declares.
        """
        return await self._power_switch(ref, False)

    async def _power_switch(self, ref: Any, on: bool) -> Any:
        """Shared switch path for `power_on()`/`power_off()`.

        Single ref (a plain string): returns the `set_outlet` result
        ``{ok, reading, impact}`` - ``impact["losing_power"]`` lists the consoles
        that lose ALL feeds, so a script can warn before a power-off. Raises
        `ActionPowerError` on refusal or failure.
        List or omitted ref: switches each of this port's feeds; an outlet
        already in the target state is skipped and one refusal does not stop the
        rest; returns ``{"changed": [...], "skipped": [...],
        "failed": [{"ref", "error"}]}``.
        Every ref - explicit or omitted - must be a feed THIS port declares.
        """
        ctx = self._power
        if ctx is None:
            raise ActionPowerError("power control is not available on this server (no active PDU adapter)")
        refs = self._power_target_refs(ref)
        if ref is not None and not isinstance(ref, (list, tuple)):
            return await self._power_switch_one(ctx, refs[0], on)
        return await self._power_switch_many(ctx, refs, on)

    def _power_target_refs(self, ref: Any) -> List[str]:
        """Normalize a `ref` argument to the ordered, deduped list of target refs.

        Raises `ActionPowerError` when the list form is empty, the omitted form
        matches no feed, a ref is not one of THIS port's declared feeds (a
        script on port A must not touch port B's outlet), or a ref is
        remote-owned (the ``origin::`` prefix; relaying needs a live anchored
        console session, which a script does not have).
        """
        ctx = self._power
        declared = ctx.pdu.port_power_map(self.port_name)
        if ref is None:
            wanted = list(declared)
            if not wanted:
                raise ActionPowerError(f"port '{self.port_name}' declares no power feeds")
        elif isinstance(ref, (list, tuple)):
            wanted = [str(r) for r in ref]
            if not wanted:
                raise ActionPowerError("ref list is empty")
        else:
            wanted = [str(ref)]
        out: List[str] = []
        for candidate in wanted:
            if split_remote_ref(candidate) is not None:
                raise ActionPowerError(
                    f"{candidate}: cannot switch a federated outlet from an action script "
                    "(the relay needs the console session that launched it)",
                    ref=candidate,
                )
            if candidate not in declared:
                raise ActionPowerError(
                    f"{candidate} is not a power feed of port '{self.port_name}'",
                    ref=candidate,
                )
            if candidate not in out:
                out.append(candidate)
        return out

    async def _power_switch_one(self, ctx: _PowerContext, ref: str, on: bool) -> Dict[str, Any]:
        """Switch one in-scope ref; raise `ActionPowerError` on refusal or failure.

        Applies the same group gate as every other power surface: the operator
        must be allowed to drive every console the outlet feeds (admin
        bypasses). The switch runs as the operator with the run's client id, so
        the POWER CONTROL audit line and the `[POWER]` notices work unchanged.
        """
        blocked = ctx.pdu._power_blocked_ports(ref, ctx.username)
        if blocked:
            raise ActionPowerError(
                f"{ref} feeds consoles outside your groups ({', '.join(blocked)}); switching it needs admin",
                ref=ref,
            )
        try:
            result = await ctx.pdu.set_outlet(ref, on, user=ctx.username, client_id=ctx.client_id)
        except Exception as exc:
            raise ActionPowerError(f"{ref}: {exc}", ref=ref) from exc
        if not result.get("ok"):
            raise ActionPowerError(f"{ref}: {result.get('error', 'switch failed')}", ref=ref)
        return result

    async def _power_switch_many(self, ctx: _PowerContext, refs: List[str], on: bool) -> Dict[str, Any]:
        """Switch a ref list, skipping outlets already in the target state.

        One refused outlet does not stop the rest; per-ref outcomes are
        collected into ``{"changed", "skipped", "failed"}``.
        """
        states = ctx.pdu.feed_states(self.port_name)
        changed: List[str] = []
        skipped: List[str] = []
        failed: List[Dict[str, str]] = []
        for ref in refs:
            current = states.get(ref)
            if current is not None and current == on:
                skipped.append(ref)
                continue
            try:
                await self._power_switch_one(ctx, ref, on)
            except ActionPowerError as exc:
                failed.append({"ref": ref, "error": str(exc)})
            else:
                changed.append(ref)
        return {"changed": changed, "skipped": skipped, "failed": failed}
