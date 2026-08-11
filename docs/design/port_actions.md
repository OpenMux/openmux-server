# Port Actions (Scripted Automation)

**Status: rollout phases 1-2 implemented, phase 3's client-notification/self-
demote wiring implemented (script format, expect-style session, in-process
runner, read-write-slot locking with client-facing mode notifications, CLI
trigger, web plugin with run/monitor HTTP+WS API, console-page Actions panel
with a persistent "action running" strip). The phase 3 split-pane
raw+structured terminal view IS implemented, as a vertical (left/right) split
rather than the top/bottom stacking originally sketched below — a draggable
`#actionTermSplitter` divider resizes an xterm.js-based `#actionTermPane`
docked to the right of the main `#term` console (see "UI surface" and "Live
view" notes below). Phase 4's structured-event late-join
is implemented (a port's actions catalog response includes `active_run` when
one is in progress, the console page shows a "Script running — click to join"
strip, and `/ws/actions/<run_id>` replays a run's full structured-event history
before switching to live streaming) — the raw-byte scrollback ring buffer for
a late joiner is NOT implemented, since the raw port terminal already
broadcasts live to every attached client regardless of mode (see "Live view"
below), so a late joiner only misses structured events, not raw I/O. Phase 5's
operator-input channel is implemented (`session.wait_for_input()`/`confirm()`
feed from a `waiting_for_operator` structured event through to the
console page's run panel, gated by a `client_id` query param on
/ws/actions/<run_id>` matching the run's operator) — "Take over as operator"
force-take reassignment IS also implemented: any connected viewer can send
`{"type": "operator_take_over"}` to become the new operator, and every viewer
(including the previous operator) learns about it via an `operator_changed`
structured event on the run's own live event stream. A run's operator can also stop it
mid-execution (`{"type": "cancel_run"}` on the same WebSocket, a `#actionTermStop`
button next to the pane's close button — see "Stopping a run" below). Phase 6's
deep-linking is implemented: `?action=<id>` (+ bare `&<param_name>=<value>` per
declared param, + `&autorun=1`) pre-fills and optionally auto-launches an action's run
form on page load, going through the exact same `launchCurrentAction()`/run-API call a
manual click uses (so auth/permission/param validation is never bypassed); sensitive
params are never read from the URL, and autorun is skipped (falls back to a pre-filled
but not-yet-submitted form) if the form isn't already valid, e.g. a required sensitive
param that couldn't be pre-filled. Phase 7's action-centric assignment config is
implemented: the sole allow-list config key is `action_ports: {action_id:
[port_name, ...]}`, plus a `"*"` wildcard entry granting an action to every port —
Config Editor web-UI support for managing these assignments without hand-editing YAML
is NOT implemented (see "Action-to-port assignment config" below). The plugin's config
(`actions_dir`/`action_ports`) lives in its own top-level `port_actions` section (a
sibling of `web_console`, not nested inside it) — `web_console.plugins` only enables
its routes.**

Implementation: `openmux/server/actions/` (`session.py`, `registry.py`,
`runner.py`), example scripts in `openmux/server/actions/examples/` (`echo_probe.py`,
`slow_noop.py`, `confirm_probe.py`, `select_probe.py`, `setup_wizard.py`), CLI entry
point in `scripts/run_action.py`, web plugin in
`openmux/server/web_plugins/port_actions.py`, console-page UI in
`templates/web_console/console.html.j2` (Actions button/overlay) and
`static/js/console.js` (catalog fetch, run form, live WS event log, run
history), tests in `tests/test_action_*.py` and `tests/test_port_actions_plugin.py`.

The web plugin exposes, per port, `GET .../actions` (catalog filtered by config),
`POST .../actions/{action_id}/run` (launches a run, returns immediately with
`run_id`/`status: "running"`), `GET .../actions/{action_id}/runs` (run history
summaries), and `GET /ws/actions/{run_id}` (live structured event stream until
`action_finished`). Runs execute as background asyncio tasks (`ActionRunner.launch_run`),
so a port-busy failure (e.g. read-write slot already held) is only observable
asynchronously via the runs endpoint or WS stream, not as a synchronous HTTP error
— only the "another action already running on this port" check is synchronous
and returns 400 immediately.

The console page's Actions button (hidden when no actions are configured for the
current port, or the user lacks permission) opens a small overlay listing the
allowed actions; selecting one shows a generated param form and a Run button.
Starting a run opens the split-pane `#actionTermPane` docked to the right of
the main console (an xterm.js transcript of the run's structured events, plus
an operator-input prompt when the script is waiting on `wait_for_input()`), and
a small run-history table stays in the overlay itself. Closing the `#actionTermPane`
(✕ button) only hides it — the run's WS stream keeps updating in the
background, and a small persistent "Action running: ..." strip stays visible
(click to reopen the pane) so it stays clear the port's read-write slot is
still held. The pane's width is draggable via `#actionTermSplitter` and
persisted across reloads (`localStorage` key `omx_action_term_width`).


## Objective
Let an operator trigger a scripted automation ("action") against a port — for example
"factory reset device" or "add config to a device" — instead of typing commands by hand.
An action is a Python script that drives the port using an expect-style
send/wait-for-pattern loop, with support for user-supplied input parameters.

## Why reuse the port/adapter stack
An action needs the same thing a human console session needs: a read-write attach to a
port, a stream of inbound bytes, and a way to send bytes. The command adapter already
shows this pattern (get a shell, send input, read output). Rather than a new I/O layer,
an action attaches to `PortManager` exactly like a console client does, through
`add_client_to_port()` / `write_to_port()` / its own entry in `client_queues`.

## Script format
- A script is a plain Python module with `ACTION` metadata (name, description, declared
  input parameters) and an `async def run(session, params, log)` entry point.
- `session` is a small expect-style wrapper: `await session.send(text)`,
  `await session.expect(pattern, timeout=...)`, `await session.sendline(text)`.
  A matched `expect()` (and anything before it) is dropped from the buffer, so a
  later `expect()` sees only new output. `session.clear_buffer()` discards buffered
  bytes without waiting for a match. `session.read_buffer(consume=False)` returns the
  inbound text seen so far without waiting for a pattern match — it drains any
  already-queued chunks first, so a script can inspect the current output (e.g. to
  branch on it) without needing an `expect()` call; pass `consume=True` to also drop
  the returned text from the buffer. See [action_session.md](action_session.md) for
  the full `ActionSession` API reference.
- `params` is a dict of the user-supplied inputs, validated against the `ACTION`
  metadata's declared parameter types before the script runs.
- Each declared param (`ActionParam` in `registry.py`) has a `widget`, defaulting to
  `"text"` (a plain text/number/password input, per its `type`/`sensitive`). Setting
  `widget: "select"` or `widget: "radio"` plus a non-empty `choices` list (plain values,
  or `{"label": ..., "value": ...}` dicts, normalized by `choices.normalize_choices()`)
  renders the start-run form field as a dropdown or a set of radio buttons instead — e.g.
  a `device_type` param with `widget: "select"` and `choices: ["router", "switch"]`. The
  same `widget`/`choices` fields are included in the `GET .../actions` catalog response
  so the console page (`renderActionParamField()` in `console.js`) can render them.
- `log` is a callable for structured progress events (step name, status, matched text) —
  distinct from raw port I/O, see "Live view" below.

## Execution model
- Run the script in a separate process (subprocess isolation) by default, not in-process
  in the server's event loop — a script bug or infinite loop must not be able to block or
  crash the server. In-process execution can be a later, explicitly-opt-in mode for
  trusted, lightweight scripts.
  - **Phase-1 note**: `ActionRunner` currently runs `action.run_func` in-process (an
    `asyncio.Task` in the server's own event loop), not in a subprocess. Timeout
    enforcement and read-write-slot locking/cleanup already work as designed; subprocess
    isolation is deferred to a later phase since it needs a proxy protocol to carry the
    session's `send`/`expect` calls across a process boundary.
- Enforce a timeout per run; the process is killed and the port's client attachment is
  released if it's exceeded.
- Concurrency: only one action per port at a time (see "Locking" below).

## Locking (read-write slot)
- The action attaches to the port with `mode="read-write"`, so it participates in the
  existing `max_read_write_users` capacity check in `add_client_to_port()` — no separate
  mutual-exclusion mechanism is needed.
- If the read-write slot is already held by a different client, the action attach fails
  fast with a clear "port busy" error. It must not silently preempt another session.
- If the *same* browser session that is about to launch the action currently holds the
  read-write slot, the console demotes itself to read-only first (`demote_client()`),
  then the action attaches as read-write. This is the user's own explicit choice playing
  out, not preemption of someone else.
- The action's `client_info` entry should carry a marker (e.g. `username="action:<id>"`)
  so the UI can render "Locked by: Action `factory_reset` (run #1234)" instead of a
  generic username.
- The action's `remove_client_from_port()` call must run in a `finally` block covering
  the whole run (success, failure, timeout, crash) so a crashed script can never
  permanently wedge the port's only read-write slot.
- **Auto-restore on finish**: if the run's self-demote path fired (see above), record
  which `client_id` was demoted on the `ActionRun`. When the run ends (success, failure,
  timeout, or crash — same `finally` block), if that `client_id` is still attached to the
  port, call `promote_client()` to give it read-write back automatically. This only
  applies to the client that was demoted to make room for its *own* launched action —
  any other read-only viewer is left untouched, and if that client already disconnected
  or someone else has since taken the slot, no restore happens (normal capacity rules
  decide the outcome, first-come first-served).
- **Client-facing notification (phase 3)**: `demote_client()`/`promote_client()` only
  update `PortManager`'s internal state; they do not by themselves tell the affected
  browser tab its mode changed. `ActionRunner` accepts an optional `console_manager` and,
  when set, pushes a `{"type": "client_mode", ...}` control frame (via
  `console_manager.send_control_frame_to_client()`) to the self-demoted client on both
  the demote (`reason: "action_self_demoted"`) and the auto-restore
  (`reason: "action_restored"`) — the same delivery mechanism already used for
  force-take demotion notices. The console page's own WebSocket connection learns its
  `client_id` from the initial `client_mode` frame the server sends on connect, and
  passes it back as `client_id` in the action run request so the runner can target it.

## Live view: one raw terminal, one structured panel — not two separate terminals
`handle_incoming_port_data()` already broadcasts every inbound chunk to *all* attached
`client_queues`, regardless of mode. This means a read-only console watching the port
already sees the action's sends and the device's replies live, with no extra plumbing —
the "port terminal" and the action's raw I/O were never separate streams.

What's worth adding is a second, additional view for *structured* progress information
(step name, elapsed time, matched pattern, pass/fail) — this is different information
than a byte stream and doesn't belong inside a terminal's character grid.

**Implemented UI** (see
[console.html.j2](../../templates/web_console/console.html.j2) and
[static/js/console.js](../../static/js/console.js)): `#term-container` holds
two panes side by side, split vertically (left/right, not top/bottom):
- The existing `#term` xterm pane, unchanged — still shows raw port I/O live via the
  normal broadcast.
- A new `#actionTermPane` (containing `#actionTerm`), created lazily on first use so
  `FitAddon.fit()` never runs against a `display:none` container, with its own
  `Terminal`/`FitAddon` pair (same pattern as the main console) fed by a dedicated
  WebSocket (`/ws/actions/<run_id>`) carrying the action's transcript plus structured
  events (`step_started`, `step_matched`, `action_finished`, `waiting_for_operator`,
  `progress`, ...) rendered as one text line per event (timestamp, event/message text) —
  not squeezed into the same grid as raw port bytes. `progress` (from
  `session.progress(step, percent=None)`, see action_session.md) drives an optional
  progress bar shown in the run panel, in the same slot the finished-run outcome banner
  uses — hidden entirely for scripts that never call it. `waiting_for_operator`
  carries the script's last-reported step too, so the console page can show which step
  a pending prompt belongs to, but is otherwise a separate, paused overlay state, not
  a step of the progress bar's own sequence.

The split is a draggable divider (`#actionTermSplitter`, `cursor: col-resize`), clamped to
`[220px, container width - 220px]` and persisted via `localStorage`. The pane is
toggleable via its own ✕ button so it can be hidden without killing its WebSocket/Terminal
instance — output keeps buffering while hidden, and a thin persistent strip ("Action
running: `factory_reset` — step 3/8") stays visible so it's clear something is still
active and holding the lock.

## Late join: viewing a run already in progress
If a user opens (or already has open) a port's console while an action is running there,
they need a way in without starting a second run:
- **Implemented**: `GET /api/ports/<name>/actions` (the same catalog call the console page
  already makes on load) includes an `active_run` field (`run.summary()`, i.e. `run_id`,
  `action_id`, `started_at`, `status`, ...) whenever `ActionRunner.get_active_run(port_name)`
  returns a run — no separate poll/endpoint was added, and no `get_status()`/`notify_meta_updated`
  wiring was needed for this since the existing per-port actions fetch already happens at the
  right time (page load). The console page shows a "Script running: `<name>` — click to join"
  strip (reusing the same persistent `#actionRunStrip` chip from phase 3) when `active_run` is
  present and this tab hasn't itself started a run; clicking it opens the actions overlay's run
  panel and connects to that run's WS stream (`joinActiveRun()` in `console.js`) instead of
  starting a new run.
- Joining opens the same split-pane `#actionTermPane` described in phase 3 above,
  connecting to the *same* `/ws/actions/<run_id>` the run is already using — it does not
  start a new run.
- A late joiner has missed earlier output, so the action-run keeps two different buffers, since
  a joiner is often specifically interested in the debug/step history, not just the tail:
  - A capped raw-byte ring buffer for the `#term`-style transcript — **not implemented**, since
    the raw port terminal already broadcasts every inbound chunk live to all attached clients
    regardless of mode (see "Live view" above), so a late joiner already sees raw device I/O from
    the moment they open the console; only the structured event history needed replaying.
  - **Implemented**: the *full*, unbounded-for-the-run list of structured events (`ActionRun.events`
    in `runner.py`, appended to from `_publish()`) is kept for the run's lifetime. `GET
    /ws/actions/<run_id>` (`_handle_ws_run_events` in `port_actions.py`) replays this full history
    to every new subscriber before switching to live streaming (or just the history, if the run
    already finished) — this also replaced the old "send one synthetic final-status event" fallback
    for connecting to an already-finished run, since replaying the real recorded event history now
    covers that case too.
  - Both are replayed once on connect, then the connection switches to live streaming.
- **Implemented**: a console that hasn't opened the Actions overlay at all (so never
  fetched the catalog / `active_run`, and isn't connected to `/ws/actions/<run_id>`)
  still learns "live", with no refresh needed, that a run just started or finished on
  the port it's viewing. `ConsoleManager.broadcast_control_frame_to_port(port_name,
  payload)` fans a control frame out to every client currently attached to that port
  (any adapter, looked up via `client_port_map`), and `ActionRunner` calls it right after
  the run's own `action_started`/`action_finished` structured events are published,
  sending `{"type": "action_run", "event": ..., "run_id":..., "action_id":...,
  "action_name":..., "operator_client_id":...}` over the client's *existing* main port
  WebSocket (the same `OMXCTRL `-prefixed control-frame channel used for `client_mode`/
  `meta`, not a new connection). `console.js`'s `ws.onmessage` handler shows/hides the
  same persistent `#actionRunStrip` "click to join" chip used for late-join (above) —
  from this viewer's perspective there's no difference between "a run was already active
  when I opened the page" and "a run just started while I was already looking", both
  surface the same strip. A client that already has its own `/ws/actions/<run_id>`
  stream open (the launcher, or someone who already joined) ignores this broadcast, since
  it's already seeing the run's real event stream directly.

## Operator input (human-in-the-loop scripts)
A script should be able to pause and wait on a human, not just on device output — e.g.
"press Enter once the cable is connected" or "enter the serial number printed on the
label". This is a second, separate channel from the device I/O already covered above.

**Implemented** (`session.py`, `runner.py`, `web_plugins/port_actions.py`,
`console.html.j2`, `static/js/console.js`):
- **Device channel** (already covered): script ⇄ port, via the action's own read-write
  client attachment (`write_to_port` / the action's `client_queues[client_id]`).
- **Operator channel**: browser ⇄ script, carried over the *same*
  `/ws/actions/<run_id>` WebSocket, but in the upstream direction — a JSON text frame
  `{"type": "operator_input", "text": ...}` sent by the console page is routed to the
  running script's pending `session.prompt(...)` call (an `asyncio.Queue` inside
  `ActionSession`), not to the device. `wait_for_input()`, `confirm()`, `choose()`,
  `select()`, and `radio()` are convenience wrappers around `session.prompt(text, *,
  kind, choices, color, timeout)`:
  - `wait_for_input(prompt)` — `kind="text"`: a free-form single-line input + Send
    button (the original/default behavior).
  - `confirm(prompt)` — `kind="buttons"` with fixed `Yes`/`No` choices; returns `bool`.
  - `choose(prompt, choices)` — `kind="buttons"` with script-supplied choices (e.g.
    `["continue", "cancel"]`), rendered as one button per choice; clicking answers
    immediately, no typing needed.
  - `select(prompt, choices)` — `kind="select"`: choices rendered as a `<select>`
    dropdown plus a Send button; the operator picks one, then sends.
  - `radio(prompt, choices)` — `kind="radio"`: choices rendered as radio buttons plus a
    Send button; the operator picks one, then sends.
  `choices` is a list of plain values, or `{"label": ..., "value": ...}` dicts to show a
  different label than the value returned to the script (normalized by
  `choices.normalize_choices()`, shared with the start-run param `widget`s above).
  `color` (all five wrappers accept it) picks the prompt box's accent color — `"none"`
  (default) or one of `session.VALID_PROMPT_COLORS` (`red`, `green`, `blue`, `pink`,
  `yellow`, `orange`, `purple`), purely visual (e.g. red for a destructive `confirm()`).
  The `#actionTermPane`'s `#actionsOperatorPrompt` box swaps between a text input, a row
  of buttons, a `<select>`, or a column of radio buttons based on the event's `kind`
  field, and colors its border/flash based on the event's `color` field
  (`showOperatorPrompt(prompt, kind, choices, color)` in `console.js`, styled via
  `#actionsOperatorPrompt[data-color="..."]` in `web_console.css`).
- Permission for the operator channel is separate from the port's read-write slot: only
  the client identified as `ActionRun.operator_client_id` (the run's launcher by
  default, reassignable mid-run — see "Taking over as operator" below) may answer via
  its `?client_id=<id>` query param on `/ws/actions/<run_id>`, checked by
  `ActionRunner.submit_operator_input()`; other callers' `operator_input` frames are
  silently ignored (no error surfaced, matching the allow-list-only security model used
  elsewhere in this design).
- A pending `prompt()` call (via any of the five wrappers above) is reflected as a
  `waiting_for_operator` structured event carrying `prompt`, `kind`, `color`, and (for
  `buttons`/`select`/`radio`) the normalized `choices` list, published the same way as
  any other action event (history-replayed for late joiners, per phase 4), so it's
  obvious the run is paused and needs a human, not just idle.

**Implemented**: "Taking over as operator" — `ActionRunner.take_over_operator(run_id,
new_client_id)` reassigns `ActionRun.operator_client_id` mid-run (mirroring the port's
own "Force take read-write") and publishes an `operator_changed` structured event
(`operator_client_id`, `previous_operator_client_id`) on the run's normal live event
stream — every subscriber, including the previous operator, sees the change the same
way they see any other event, no separate notification channel needed. A viewer sends
`{"type": "operator_take_over"}` upstream on `/ws/actions/<run_id>?client_id=<id>` to
become the operator; there is no extra permission check beyond having a `client_id`
(any connected, identified viewer may take over), matching this design's
allow-list-only security model. `console.js`'s `#actionTermTakeOver` button (shown
only when the current client isn't the run's operator) sends this frame and shows a
brief toast to a previous operator who just lost that role.- **Implemented**: non-operators cannot interact with a pending prompt at all, not just
  "answers are ignored server-side" (the pre-existing security behavior) — the operator-
  input controls themselves (`#actionsOperatorInput`/Send, every button, the `<select>`/
  its Send button, every radio input/its Send button) are visually and functionally
  `disabled` for anyone who isn't the run's current `operator_client_id`, with a small
  `#actionsOperatorReadonlyNote` explaining why ("Only the current operator can answer
  this prompt — use 'Take over as operator' above."). `console.js`'s
  `applyOperatorInputDisabledState()` (called from `showOperatorPrompt()` and
  `updateOperatorTakeOverUI()`, so it re-evaluates on both a new prompt and an
  `operator_changed` event) drives this; `sendOperatorInput()` also keeps a
  server-adjacent client-side guard (`isCurrentOperator()`) as defense-in-depth in case
  a control is somehow triggered anyway.

## Stopping a run
An operator needs a way to abort a stuck or mistaken run without waiting for its
timeout — e.g. a script sending the wrong command to a live device.

**Implemented**: `ActionRunner.cancel_run(run_id, requesting_client_id=None)` cancels
the run's background `asyncio.Task`; `_execute()` catches the resulting
`asyncio.CancelledError`, sets `status = "cancelled"`, and runs its normal cleanup
(detach the action's client, auto-restore a self-demoted launcher, publish
`action_finished`) exactly like any other run outcome — a stopped run is never left
holding the port's read-write slot. Same permission model as `submit_operator_input()`:
only the run's current `operator_client_id` may cancel it once one is assigned; other
callers are silently ignored. A viewer sends `{"type": "cancel_run"}` upstream on
`/ws/actions/<run_id>?client_id=<id>` (the same channel used for `operator_input`/
`operator_take_over`). The console page's `#actionTermStop` button (next to the pane's
✕ close button, hidden for non-operators the same way the operator-input controls are)
sends this frame after a confirmation prompt.

## Persisted log
`DataLogger` resolves a port's log file from either a `port_obj.config["log_file"]`
override or the default `logs/ports/{port_name}.log`, keyed by whatever `port_name`
string is passed to `record()`. No DataLogger change is needed: route action traffic
through `DataLogger.get().record(port_name=f"{port_name}__action_{run_id}", ...)` to get
a fully separate, self-contained transcript file per run
(`logs/ports/{port_name}__action_{run_id}.log`), independent of the port's own log.

## Run registry
Keep a lightweight in-memory `ActionRun` record per run: run_id, port_name, action_id,
user, redacted params, start/end timestamps, status, the transcript log path, and the
`auto_demoted_client_id` (if any) to restore on finish. This backs both the live WS view
while running and a "past runs" list per port for later review/audit, without wading
through the port's general traffic log.

## UI surface
- **Actions list**: visible directly in the console page, not a separate page — a new
  toolbar button (next to `Connect`/`Menu`/`Info`/`Show Logs`) plus a section in the
  existing `ctrlMenu` overlay, listing the actions available for the current port
  (fetched from `GET /api/ports/<name>/actions`). Each entry has a "Run" button that
  opens a small params form.
- **API**: `POST /api/ports/<name>/actions/<id>/run` starts a run; `GET
  /api/ports/<name>/actions/<id>/runs` lists history.
- **Web plugin**: implemented as its own `openmux.server.web_plugins.port_actions` module
  (`register_plugin(app, adapter, options=None)`, same pattern as
  [config_editor.py](../../openmux/server/web_plugins/config_editor.py#L639)) adding its
  own routes rather than modifying `web_console.py` core.

## Deep-linking an action
Another system (a ticketing tool, an inventory system, a runbook link) should be able to
open a console straight into a specific action with its parameters pre-filled, instead of
an operator navigating and typing them by hand:
**Implemented** (`console.js`'s `applyActionDeepLink()`/`launchCurrentAction()`,
`console.html.j2`'s `#actionToast`):
- The console URL accepts optional query params: `?action=<action_id>` opens the actions
  panel with that action pre-selected, and `&<param_name>=<value>` (one per declared
  `ACTION` parameter) pre-fills the run form (including `select`/`radio` widget fields,
  matched by value).
- The user still sees the filled-in form and clicks "Run" themselves by default — an
  optional `&autorun=1` skips that click, but still requires the normal auth/permission
  checks (existing login-gated console session, read-write eligibility, port-busy/self-
  demote handling) exactly as a manually-triggered run would, since it calls the exact
  same `launchCurrentAction()` function (and thus the same `POST .../actions/<id>/run`
  API call) as the Run button; a short confirmation toast (`#actionToast`, a
  few seconds) is still shown even on autorun, so an operator watching the screen isn't
  surprised by a script starting with no visible cause.
- `&autorun=1` only fires if `actionsRunForm.reportValidity()` passes first (all
  `required` fields filled) — a required param that couldn't be pre-filled (e.g. a
  sensitive one, see below) silently falls back to leaving the pre-filled form open for
  the operator to finish and submit by hand, rather than submitting an incomplete run.
- Params arriving via the URL still go through the same `ACTION` metadata validation as
  manually-typed ones (server-side, on the run API call) before a run is allowed to
  start; invalid/missing values surface the same `actionsRunStatus` error text a manual
  run would, rather than failing silently.
- Parameters marked `sensitive: true` in `ACTION` metadata (passwords, tokens) are never
  deep-linkable — `applyActionDeepLink()` skips reading a URL param for any `sensitive`
  param entirely, since URLs end up in browser history, server access logs, and referrer
  headers. A linking system can still open the form for a sensitive-param action; the
  operator types that value in by hand.

## Security / permissions
- Actions are configured per-port (or per-adapter-type) in server config, not arbitrary
  user-supplied scripts — an operator can't upload and run new scripts through the web
  UI without an explicit allow-list entry, mirroring the Config Editor's writable-section
  model in `config/security.yaml`.
- Parameters are validated (type, allowed values) against the `ACTION` metadata before
  the script runs; redact anything marked sensitive (e.g. passwords) before it's stored
  in the run registry or logs.

## Action-to-port assignment config
The plugin's own settings (`actions_dir`, `action_ports`) live in a dedicated top-level
`port_actions` config section - a sibling of `web_console`, `loopback_ports`, etc., not
nested inside `web_console.plugins` - since it's the plugin's own configuration, not a
web-console-specific detail. `web_console.plugins` only carries `module`/`enabled` to
turn its routes on. The allow-list config key itself is action-centric: `action_ports:
{action_id: [port_name, ...]}` (see the `config/loopback_test.yaml` example, or the
module docstring in
[port_actions.py](../../openmux/server/web_plugins/port_actions.py)) — for when it's more natural to think "which ports get this action" than "which
actions does this port get", and to avoid repeating the same action id under every port
that should get it. **Implemented** (`openmux/server/web_plugins/port_actions.py`):
- **Top-level `port_actions` section**: `register_plugin()` reads `actions_dir`/
  `action_ports` from `adapter.server_config["port_actions"]` (the full raw server
  config, wired onto the `WebConsoleAdapter` by `main.py` right after adapter creation -
  see `WebConsoleAdapter.server_config` in `openmux/server/web_console.py`), not from
  its own `web_console.plugins` entry. `openmux/server/adapters/factory.py`'s
  fail-fast startup check treats `port_actions` as a recognized non-adapter top-level
  section (alongside `server`/`authentication`/`logging`), so a bare `port_actions:`
  section with no matching adapter plugin doesn't abort startup.
- **Action-centric layout**: `_allowed_actions()` computes a port's effective allow-list
  from `action_ports`, matching each entry's port list against the requested port name.
  A port-centric `port_actions: {port_name: [action_id, ...]}` layout was considered and
  briefly supported for backward compatibility, but was removed — it had no other
  callers, and `action_ports` alone (with the wildcard below) covers every case it did.
- **"All ports" wildcard**: `action_ports: {factory_reset: ["*"]}` grants the action to
  every port name requested against `_allowed_actions()`, without listing each one by
  name. Since the allow-list is computed per-request rather than precomputed once at
  startup, a port added later is covered immediately without a config reload — the
  wildcard is still an explicit, visible administrative choice per action, not a default,
  matching the "Security / permissions" model above.
- **Config Editor web-UI support**: **implemented** — `port_actions` is a recognized
  `config_editor.allowed` section name in `config/security.yaml`, and there is a
  dedicated "Actions" sub-view (mirroring the existing `ports`/`listeners`/`muxcon`/
  `auth` sub-pages in `templates/web_console/config_editor.html.j2`, `?view=actions`)
  for editing `actions_dir` and the `action_ports` allow-list table without hand-editing
  YAML. Each `action_ports` entry is edited as one row (action id + comma-separated
  port list, `*` for all ports) in `static/js/config_editor.js`.

## Rollout phases
1. Script format + `session` expect wrapper + subprocess runner, no UI (CLI-triggerable
   only), single-port lock via existing read-write slot mechanism.
2. Web plugin: actions list in console page, run API, structured live WS view.
3. Split-pane UI (raw xterm + structured status panel), show/hide toggle, self-demote on
   launch with auto-restore on finish.
4. Late-join ("Script running — Join") via active-action meta + action-run scrollback
   replay; per-run persisted log + run history/audit list. **Implemented**: `active_run`
   in the actions catalog response + structured-event history replay on `/ws/actions/<id>`;
   the persisted per-run log (`logs/ports/{port}__action_{run_id}.log`, phase 1) and run
   history list (`GET .../runs`, phase 2) already existed from earlier phases.
5. Operator-input channel (`session.wait_for_input()`/`confirm()`), force-take parity
   with existing console controls. **Implemented**: `waiting_for_operator` event +
   `client_id`-gated `{"type": "operator_input", ...}` frames on `/ws/actions/<run_id>` +
   `#actionsOperatorPrompt` UI in the existing flat run panel (not a separate writable
   `#actionTerm` pane, per the phase-3 UI-surface decision above). **Implemented**:
   "Take over as operator" force-take reassignment — `ActionRunner.take_over_operator()`
   plus the `operator_changed` event and `#actionTermTakeOver` button; see "Taking over
   as operator" above.
6. Deep-linking: `?action=`/param query-string support, pre-filled/auto-run launches from
   external systems, sensitive-param URL restrictions. **Implemented**: see "Deep-linking
   an action" above.
7. Action-centric assignment config (`action_ports: {action_id: [port_name, ...]}`,
   including an "all ports" wildcard) — see "Action-to-port assignment config" above.
   **Implemented**: the config key, the `"*"` wildcard, its own top-level
   `port_actions` config section (sibling of `web_console`), and Config Editor
   web-UI support for managing action/port assignments without hand-editing YAML.
