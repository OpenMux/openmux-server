# CLI Interactive Command Mode (Issue #88)

Status: Implemented (agreed 2026-10-18).
Scope: OpenMux CLI client (`openmux/client/`), with two small server-side changes in
`openmux/server/adapters/client_listener.py` and one parser change in
`openmux/server/adapters/power_command.py`.

## Purpose

A user who runs `openmux -s <server>` (no port argument) today lands in the raw TCP
stream. The screen is silent, typing is blind, and a bare Enter ends the session. This
feature adds an interactive command mode to the CLI client. The user sees a prompt,
sees what they type, can discover the commands with `help`, and never loses the
session by pressing Enter.

The on-the-wire protocol stays stable. The server command phase stays prompt-free:
the prompt, the echo, and the help text are all client-side.

## When command mode is active

Command mode starts when all of these are true:

1. The session uses the TCP adapter (the WebSocket adapter attaches to one port only).
2. The client authenticated successfully.
3. No port is attached yet (the user started the client without a port argument).

Command mode ends when the user attaches to a port (`connect`). The session then runs
in stream mode: keystrokes go to the port, exactly as today. A detach returns the
session to command mode.

## Commands at the `mux> ` prompt

Typed forms are case-insensitive. The client also accepts the raw wire forms
(`LIST`, `CONNECT:<port>`, `DISCONNECT`, `QUIT`, `POWER ...`) unchanged, for users
who know the protocol.

| Command | Alias | Action | Output source |
|---|---|---|---|
| `help` | `h`, `?` | Print the command list below. | client |
| `list` | `l` | Send `LIST`. Render one port per line. | server |
| `connect <port>` | `c <port>` | Send `CONNECT:<port>`. On `CONNECTED:<port>:<mode>`, print `[Connected to <port> (<mode>)]` and switch to stream mode. On `ERROR:...`, print the server line and stay in command mode. | server |
| `connect` (no argument) | | Print `[Usage: connect <port>]` and `[Run "list" to see ports]`. | client |
| `disconnect` | | Not attached: print `[Not attached. Type "connect <port>", or "quit" to end the session]`. Never sent on the wire. | client |
| `power` | | Send the line verbatim. Forms: `power`, `power <pdu>`, `power <pdu>.<outlet>`, `power <pdu>.<outlet> on\|off`. Server answers (see `docs/configuration/adapters.md`). | server |
| `quit` | `exit`, `q` | End the session (same as the control menu `.`). | client + `QUIT` |
| Empty line (Enter) | | Re-prompt. Sends zero bytes. | client |
| Unknown line | | Print `[Unknown command: "<line>" — type "help"]`. Never sent on the wire. | client |

## Line-editing keys at the prompt

| Key | Behavior |
|---|---|
| Printable key | Echo locally, append to the line. |
| Backspace / Delete | Remove the last character; erase the echo with `backspace-space-backspace`. |
| Enter | Submit the line. A blank line only re-prompts. |
| Ctrl+C | Cancel the current line; start a new one. Does not end the session. |

The control menu still works in command mode: the escape introducer bytes are taken
out of the input before the line editor sees them.

## Help text

`help` prints exactly this text. A test pins the client output against this block; if
you change one, change both.

<!-- BEGIN HELP -->

```text
Commands:
  help, h, ?          Show this help
  list, l             List console ports
  connect <port>, c   Attach to a port
  disconnect          Leave a port (while attached: press Ctrl+E c d)
  power [args]        Power: power [<pdu>[.<outlet>] [on|off]]
  quit, exit, q       End the session
Blank line does nothing. While attached, keystrokes go to the port.
The control menu (shown at startup) controls the session.
```

<!-- END HELP -->

## While attached: stream mode and control menu

In stream mode, all keys go to the port. The control menu (default `Ctrl+E c`) controls
the session. One command is new:

| Key | Command | Status |
|---|---|---|
| `d` | Detach from the port; return to the `mux> ` prompt. | New |
| `.` | End the session. | Existing |
| `a` / `s` / `f` | Request / release / force-take read-write. | Existing |
| `i` / `w` / `p` | Info / viewers / power menu. | Existing |
| `o` / `e` / `R` / `\` | Reconnect / change escape / replay depth / octal byte. | Existing |
| `?` | Menu help (gains the `d` line). | Existing |

`d` sends the new `detach` control frame. The server detaches the session from the
port, replies `DISCONNECTED:<port>` plus a `detached` ack, and returns the session to
its command phase. The client prints `[Detached from <port>]` and shows the prompt
again.

### Notices

The client prints these bracketed notices:

| Notice | When |
|---|---|
| `[Connected to <port> (<mode>)]` | A `connect` succeeded. |
| `[Detached from <port>]` | A detach succeeded. |
| `[Detach refused by server]` | The server denied the detach. |
| `[Detach timed out]` | The server did not answer the detach within 5 seconds. |
| `[Detach not supported on this connection]` | The adapter cannot detach (WebSocket). |
| `[Unknown command: "<line>" — type "help"]` | Command mode got a line it does not know. |
| `[Usage: connect <port>]` + `[Run "list" to see ports]` | `connect` without a port. |
| `[Not attached. Type "connect <port>", or "quit" to end the session]` | `disconnect`/`detach` while not attached. |

## Protocol changes on the wire

The wire protocol stays compatible on purpose. Command mode is a client-side view of
the existing command phase. Two server-side changes ship with this feature:

1. **Blank line no-op (bug fix).** In the server command phase, a blank line ended the
   session. A blank line now does nothing. This only affects raw TCP clients (for
   example telnet) that type Enter at an empty line. The OpenMux CLI client never
   forwards a blank line in command mode, so it needs no server reply for it.
2. **Detach control frame (addition).** A new out-of-band control frame
   `{"type": "detach"}` (same `CTRL_MARKER` channel as `request_rw`) detaches the
   session from its port. The server replies with the existing `DISCONNECTED:<port>`
   line plus `{"type": "detached", "ok": true, "port": "<port>"}`. When no port is
   attached, it replies the existing `ERROR:DISCONNECT:No port connected` line plus
   `ok: false`. After a successful detach the server session returns to its command
   phase, so a later line command works on the same connection.

The server never sends a prompt and never echoes command-phase characters. A raw
session (telnet to the client-listener port) therefore sees no new bytes, except that
a blank line no longer closes it.

Control-frame framing rule (both directions): a frame starts at `CTRL_MARKER`
and ends at the next newline, and it may follow or precede other bytes in the
same TCP segment. Receivers scan for the marker inside each chunk, not only at
chunk start. A frame split across chunks is buffered once its full marker has
arrived; a split inside the 9-byte marker is treated as port data (receivers
never hold or lose bytes on a guess). TCP merges writes, so the `detach` ack
normally arrives glued to the `DISCONNECTED:<port>` line.

The client recognizes the port attach from the existing `CONNECTED:<port>:<mode>`
line and the detach from the `detached` ack. Both replies reach the client through
the background reader, so that reader stays the only reader of the connection (the
single-reader rule, the same one the power menu follows).

## Non-goals

- Command history (Up/Down arrows).
- Tab completion.
- `who` / `users` commands in command mode (the control menu `w` covers this while attached).
- WebSocket or browser-console detach (the browser console has no command phase; `d`
  reports "not supported" there).
- A server-side prompt, echo, or `HELP` command on the wire.
- Typing AUTH lines at the `mux> ` prompt. Authentication happens from CLI flags or
  config, before command mode starts.
- Telnet/SSH control-menu changes (their menu keeps `.` only).

## Design decisions

- The prompt is a client-side constant (`mux> `). It is not configurable.
- Command words are case-insensitive; the wire forms also work.
- `connect` uses the wire `CONNECT:<port>` line, so server ACLs, busy-port errors, and
  read-only grants behave exactly like any other client.
- `power` is forwarded verbatim; the server owns POWER wording. The structured POWER
  parser (part of issue #88, `power_command.py`) keeps every reply string.
- `disconnect` in command mode is informational only. Detach belongs to the session
  while attached, so it lives in the control menu (`d`), where a typed `DISCONNECT`
  cannot reach the server anyway (attached input goes to the port).
