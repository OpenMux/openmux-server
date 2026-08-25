# ActionSession

## Objective
Give a Port Action script (see [port_actions.md](port_actions.md)) a small,
expect-style API to drive a port — send bytes, wait for a pattern, and pause
for operator input — without touching `PortManager` directly.

`ActionSession` (`openmux/server/actions/session.py`) is the `session`
argument passed to a script's `async def run(session, params)`. One
instance is bound to one port attachment (one `client_id`), created and torn
down by `ActionRunner._execute()` around a single run. It reads from the same
per-client delivery queue that `PortManager.add_client_to_port()` creates for
the action's `client_id`, so it sees exactly the bytes a normal read-write
console client would see — no separate I/O path.

## API reference

### Sending

#### `await session.send(text: str) -> None`
Writes `text` to the port as-is (no newline added).
- **Raises**: `ActionSessionError` if `PortManager.write_to_port()` rejects
  the write (e.g. the action's `client_id` isn't the read-write holder).

#### `await session.sendline(text: str) -> None`
Writes `text + "\n"`. Same failure mode as `send()`.

### Reading the inbound buffer
Inbound bytes accumulate in an internal `bytearray`, decoded as UTF-8
(`errors="replace"`) whenever read.

#### `await session.expect(pattern: str, timeout: float = 10.0) -> str`
Waits until regex `pattern` matches the buffer.
- Checks already-buffered bytes first, then pulls new chunks off the queue
  as they arrive.
- **Returns**: the matched substring.
- **Side effect**: drops the match and everything before it from the
  buffer — a later `expect()` only sees new output, never re-matches stale
  bytes.
- **Raises**: `ActionTimeoutError` on timeout; `ActionSessionError` if the
  action has no client queue on the port (not attached, or already
  detached).

#### `session.clear_buffer() -> None`
Discards buffered bytes without waiting for a match.

#### `session.read_buffer(*, consume: bool = False) -> str`
Returns the buffer's current text without waiting for a pattern match.
- First drains any chunks already sitting in the queue (non-blocking, no
  timeout), then decodes and returns the buffer as-is.
- `consume=True` also clears the buffer after reading — mirrors
  `expect()`'s consume-on-match behavior.
- Use this to branch on recent output ad hoc, without committing to a
  regex and a wait.

### Progress reporting

#### `session.progress(step: str, percent: Optional[int] = None) -> None`
Reports what the script is doing, for the console page's progress bar (shown
in the run panel, in the same slot the finished-run outcome banner uses).
Purely informational — `session.log(message)` is for operator-facing
freetext messages, like `logging.info()`; `progress()` is the one dedicated
channel for "what step, how far along".
- `percent`: `0`-`100`, or omit/`None` for an indeterminate step (still
  running, no known fraction) — the console page then shows an animated
  bar with no fill level instead of a specific percentage.
- Sets the session's current step, which `prompt()` (below) reads to label
  a pending wait — but waiting for operator input is a separate, paused
  overlay state, not a step of its own; it never advances or resets the
  last-reported step/percent.
- **Raises**: `ValueError` if `percent` is given but outside `0`-`100`.

### Logging

#### `session.log(message: str) -> None`
Reports an operator-facing line for the run. Reaches both the live console
UI (the run's event stream) and the persisted log file. Use it for the run's
milestones — what the operator watching the run needs to see. For tracing
detail the operator does not need, use `debug()` instead (file only).

### Debug logging

#### `session.debug(message: str) -> None`
Writes a debug-only line to the run's persisted log file (see
[port_actions.md](port_actions.md), "Persisted log"). Unlike
`session.log()` — which also reaches the live console UI — `debug()` never
reaches the UI. Use it for tracing detail an operator watching the run does
not need: per-line decisions, dialog traffic, buffer contents, raw API data.
The message is written verbatim (not truncated).

### Operator input
One base primitive, `prompt()`, backs five convenience wrappers. All are
coroutines that block until the operator answers (or `timeout` elapses,
raising `ActionTimeoutError`).

#### `await session.prompt(text=None, *, kind="text", choices=None, color="none", timeout=None) -> str`
The base primitive; every wrapper below just calls this with fixed
arguments.
- `kind`: one of `"text"`, `"buttons"`, `"select"`, `"radio"`.
- `choices`: required (non-empty) for `"buttons"`/`"select"`/`"radio"` — a
  list of plain values, or `{"label": ..., "value": ...}` dicts. Normalized
  by `openmux.server.actions.choices.normalize_choices()`.
- `color`: accent color for the prompt's border/flash in the console — one
  of `"none"` (default), `"red"`, `"green"`, `"blue"`, `"pink"`, `"yellow"`,
  `"orange"`, `"purple"` (`session.VALID_PROMPT_COLORS`). Purely visual, e.g.
  to make a destructive `confirm()` stand out in red.
- **Raises**: `ValueError` if a choice-based `kind` gets an empty/missing
  `choices`, or if `color` isn't one of `VALID_PROMPT_COLORS`.
- Invokes the session's `on_input_wait` callback (if set), synchronously,
  with `(text, kind, normalized_choices, current_step, color)` — this is how
  `ActionRunner` turns a pending prompt into a `waiting_for_operator`
  structured event carrying the script's last-reported step (see
  `progress()` above, and port_actions.md, "Operator input"). Callback
  exceptions are swallowed; they never abort the run.

| Wrapper | `kind` | Returns |
|---|---|---|
| `await session.wait_for_input(prompt=None, color="none", timeout=None)` | `"text"` | free-form text |
| `await session.confirm(prompt, color="none", timeout=None)` | `"buttons"` (Yes/No) | `bool` (lenient parse: `y`/`yes`/`1`/`true`, case-insensitive) |
| `await session.choose(prompt, choices, color="none", timeout=None)` | `"buttons"` | the chosen value |
| `await session.select(prompt, choices, color="none", timeout=None)` | `"select"` | the chosen value |
| `await session.radio(prompt, choices, color="none", timeout=None)` | `"radio"` | the chosen value |

#### `session.submit_operator_input(text: str) -> None`
Feeds operator-supplied text to whichever `prompt()`/wrapper call is
currently awaiting one. Routing a submission to the right run/session, and
rejecting one from the wrong client, is `ActionRunner`'s job, not
`ActionSession`'s — the session itself has exactly one pending-input queue
and does not check identity.

## Example
```python
session.progress("connecting", 10)
await session.sendline("show version")
banner = await session.expect(r"\$\s*$", timeout=10.0)
session.progress("connecting", 100)

session.progress("confirm reboot")  # no percent - indeterminate
ok = await session.confirm("Reboot the device now?", timeout=60.0)
if ok:
    session.progress("rebooting")
    await session.sendline("reboot")
    await session.expect(r"\[ENTER\]", timeout=15.0)

tail = session.read_buffer()  # inspect recent output without an expect()
```
See `openmux/server/actions/examples/setup_wizard.py` for a full script
exercising every operator-input kind.

## Non-goals
- Subprocess/process isolation for the script itself (tracked separately in
  port_actions.md's "Execution model").
- Reassigning which client's queue a session reads from mid-run — a session
  is bound to one `client_id` for its whole lifetime.
- Retry/reconnect logic — if the underlying client queue disappears (port
  removed, client detached), `expect()`/`read_buffer()` simply stop seeing
  new data or raise `ActionSessionError`.
