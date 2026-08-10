"""Expect-style session wrapper for port action scripts.

An `ActionSession` is the `session` argument passed to a script's
`async def run(session, params, log)` (see docs/design/port_actions.md,
"Script format"). It reads from the same per-client delivery queue that
`PortManager.add_client_to_port` creates for the action's `client_id`, so it
sees exactly the bytes a normal read-write console client would see.
"""

import asyncio
import re
from typing import Callable, Dict, List, Optional

from openmux.server.actions.choices import Choice, normalize_choices
from openmux.server.actions.errors import ActionSessionError, ActionTimeoutError


class ActionSession:
    """Send/expect interface bound to one port attachment (one `client_id`)."""

    def __init__(
        self,
        port_manager,
        port_name: str,
        client_id: str,
        on_input_wait: Optional[Callable[[Optional[str], str, Optional[List[Dict[str, str]]]], None]] = None,
    ):
        self.port_manager = port_manager
        self.port_name = port_name
        self.client_id = client_id
        self._buffer = bytearray()
        self._operator_input: "asyncio.Queue[str]" = asyncio.Queue()
        # Called (sync) each time wait_for_input()/confirm() starts waiting, so the
        # caller can surface a "step_waiting_for_operator" structured event (see
        # docs/design/port_actions.md "Operator input").
        self._on_input_wait = on_input_wait

    def _client_queue(self) -> Optional[asyncio.Queue]:
        port = self.port_manager.ports.get(self.port_name)
        if port is None:
            return None
        return getattr(port, "client_queues", {}).get(self.client_id)

    async def send(self, text: str) -> None:
        """Write `text` to the port as-is (no newline added)."""
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

        Raises:
            ActionTimeoutError: `timeout` elapsed before `pattern` matched.
            ActionSessionError: the action has no client queue on this port
                (not attached, or already detached).
        """
        regex = re.compile(pattern)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout

        match = regex.search(self._buffer.decode("utf-8", errors="replace"))
        if match:
            return match.group(0)

        queue = self._client_queue()
        if queue is None:
            raise ActionSessionError(f"No client queue for {self.client_id} on port {self.port_name}")

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ActionTimeoutError(f"Timed out waiting for pattern {pattern!r} on port {self.port_name}")
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                raise ActionTimeoutError(f"Timed out waiting for pattern {pattern!r} on port {self.port_name}")
            self._buffer.extend(chunk)
            match = regex.search(self._buffer.decode("utf-8", errors="replace"))
            if match:
                return match.group(0)

    async def prompt(
        self,
        text: Optional[str] = None,
        *,
        kind: str = "text",
        choices: Optional[List[Choice]] = None,
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
        """
        if kind in ("buttons", "select", "radio") and not choices:
            raise ValueError(f"kind={kind!r} requires a non-empty choices list")
        normalized_choices = normalize_choices(choices) if choices else None
        if self._on_input_wait is not None:
            try:
                self._on_input_wait(text, kind, normalized_choices)
            except Exception:
                pass
        if timeout is None:
            return await self._operator_input.get()
        try:
            return await asyncio.wait_for(self._operator_input.get(), timeout=timeout)
        except asyncio.TimeoutError:
            raise ActionTimeoutError(f"Timed out waiting for operator input ({text or 'no prompt'})")

    async def wait_for_input(self, prompt: Optional[str] = None, timeout: Optional[float] = None) -> str:
        """Pause until the operator supplies a line of free-form text."""
        return await self.prompt(prompt, kind="text", timeout=timeout)

    async def confirm(self, prompt: str, timeout: Optional[float] = None) -> bool:
        """Pause until the operator clicks Yes or No (rendered as buttons)."""
        value = await self.prompt(
            prompt,
            kind="buttons",
            choices=[{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}],
            timeout=timeout,
        )
        return value.strip().lower() in ("y", "yes", "1", "true")

    async def choose(self, prompt: str, choices: List[Choice], timeout: Optional[float] = None) -> str:
        """Pause until the operator clicks one of `choices` (rendered as buttons)."""
        return await self.prompt(prompt, kind="buttons", choices=choices, timeout=timeout)

    async def select(self, prompt: str, choices: List[Choice], timeout: Optional[float] = None) -> str:
        """Pause until the operator picks one of `choices` from a dropdown and sends it."""
        return await self.prompt(prompt, kind="select", choices=choices, timeout=timeout)

    async def radio(self, prompt: str, choices: List[Choice], timeout: Optional[float] = None) -> str:
        """Pause until the operator picks one of `choices` from radio buttons and sends it."""
        return await self.prompt(prompt, kind="radio", choices=choices, timeout=timeout)

    def submit_operator_input(self, text: str) -> None:
        """Feed operator-supplied text to a pending `wait_for_input`/`confirm` call."""
        self._operator_input.put_nowait(text)
