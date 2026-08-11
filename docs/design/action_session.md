# ActionSession

## Objective
Give a Port Action script (see [port_actions.md](port_actions.md)) a small,
expect-style API to drive a port — send bytes, wait for a pattern, and pause
for operator input — without touching `PortManager` directly.

`ActionSession` (`openmux/server/actions/session.py`) is the `session`
argument passed to a script's `async def run(session, params, log)`. One
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

### Operator input
One base primitive, `prompt()`, backs five convenience wrappers. All are
coroutines that block until the operator answers (or `timeout` elapses,
raising `ActionTimeoutError`).

#### `await session.prompt(text=None, *, kind="text", choices=None, timeout=None) -> str`
The base primitive; every wrapper below just calls this with fixed
arguments.
- `kind`: one of `"text"`, `"buttons"`, `"select"`, `"radio"`.
- `choices`: required (non-empty) for `"buttons"`/`"select"`/`"radio"` — a
  list of plain values, or `{"label": ..., "value": ...}` dicts. Normalized
  by `openmux.server.actions.choices.normalize_choices()`.
- **Raises**: `ValueError` if a choice-based `kind` gets an empty/missing
  `choices`.
- Invokes the session's `on_input_wait` callback (if set), synchronously,
  with `(text, kind, normalized_choices)` — this is how `ActionRunner` turns
  a pending prompt into a `step_waiting_for_operator` structured event (see
  port_actions.md, "Operator input"). Callback exceptions are swallowed;
  they never abort the run.

| Wrapper | `kind` | Returns |
|---|---|---|
| `await session.wait_for_input(prompt=None, timeout=None)` | `"text"` | free-form text |
| `await session.confirm(prompt, timeout=None)` | `"buttons"` (Yes/No) | `bool` (lenient parse: `y`/`yes`/`1`/`true`, case-insensitive) |
| `await session.choose(prompt, choices, timeout=None)` | `"buttons"` | the chosen value |
| `await session.select(prompt, choices, timeout=None)` | `"select"` | the chosen value |
| `await session.radio(prompt, choices, timeout=None)` | `"radio"` | the chosen value |

#### `session.submit_operator_input(text: str) -> None`
Feeds operator-supplied text to whichever `prompt()`/wrapper call is
currently awaiting one. Routing a submission to the right run/session, and
rejecting one from the wrong client, is `ActionRunner`'s job, not
`ActionSession`'s — the session itself has exactly one pending-input queue
and does not check identity.

## Example
```python
await session.sendline("show version")
banner = await session.expect(r"\$\s*$", timeout=10.0)

ok = await session.confirm("Reboot the device now?", timeout=60.0)
if ok:
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
