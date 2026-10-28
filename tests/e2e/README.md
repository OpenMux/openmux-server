# End-to-end tests

This tier runs the real server as a child process and drives it the way an operator does: with the
CLI protocol, a Telnet client, the HTTP API, and a Chromium tab. A test here never imports
`OpenMuxServer` and never patches a product object. It starts a server, watches what a surface shows,
and stops the server.

Read [`docs/design/e2e_test_suite.md`](../../docs/design/e2e_test_suite.md) for why each scenario
exists and what it must prove. Read [`docs/E2E_TESTS.md`](../../docs/E2E_TESTS.md) for the list of
scenarios that exist today. This file explains how to run the tier and how to add to it.

## Run it

The tier is off by default. Plain `make test` deselects every file in this directory, so its results
and its run time stay out of the unit suite.

| Command | What it does |
| --- | --- |
| `make test-e2e` | Runs every scenario, including the browser ones when Chromium is present. |
| `make test-e2e-live` | Same, and prints the merged timeline to the terminal as the test runs (`-s`). |
| `make test-e2e-debug TEST=con01` | Runs one scenario with live output, and holds a failure open for a person. |
| `make e2e-install` | Installs Playwright and Chromium into `.venv`. Needed once per machine. |
| `make e2e-doc` | Regenerates [`docs/E2E_TESTS.md`](../../docs/E2E_TESTS.md) from the tests. |
| `make e2e-serve` | Starts the server on the `config-local/` world, for checking a page by hand. |

The same commands without the Makefile:

```bash
.venv/bin/python -m pytest --run-e2e -m e2e --no-cov -q tests/e2e
.venv/bin/python -m pytest --run-e2e -m e2e --no-cov -q tests/e2e/test_console.py
```

| Option | Effect |
| --- | --- |
| `--run-e2e` | Include this tier. Without it, pytest deselects every test here. |
| `--e2e-live` | Echo every timeline line as it is written. Needs `pytest -s` to be useful. |
| `--e2e-reveal` | On a failure, print the bundle and the live URLs, and wait for Enter when a terminal is attached. Also implies `--e2e-keep`. |
| `--e2e-headed` | Open a visible browser window. `--e2e-live` implies it. |
| `--e2e-slowmo N` | Delay each browser action by `N` ms, to follow it by eye. |
| `--e2e-keep` | Keep the artifact bundle even when the test passes. |
| `--e2e-artifacts DIR` | Where failure bundles go. Default `artifacts/e2e`. |
| `--e2e-list` | Write the scenario index and exit without running a test. |

`E2E_LIVE=1` and `E2E_REVEAL=1` do the same as the two matching options, for a shell where you cannot
add flags.

A browser scenario skips, with the install command in the reason, when Playwright or Chromium is
missing. It never fails for that reason, so a machine without a browser still runs the rest of the
tier.

## What is in this directory

| File | Job |
| --- | --- |
| `harness.py` | `ConfigBuilder` writes an isolated config, `E2EServer` starts and stops one server process, `DeviceStation` is a fake device a scenario can unplug, `Api` calls the HTTP API with Basic auth, `HttpSession` keeps a session cookie and never follows a redirect, `BasicSession` adds credentials to that session, `sso_v1_header` signs a trust header, `free_ports` reserves ports. |
| `textclients.py` | `CliSession` speaks the CLI protocol through the product's own `TcpClientAdapter`. `TelnetSession` drives the port menu over a raw socket. |
| `console.py` | `ConsoleSession` and `login()` drive a Chromium page and read it through the page hook. Playwright is imported lazily. |
| `story.py` | `Timeline` merges every surface into one log, `Story` numbers the steps of a scenario, `step()` marks a block, `surfaces()` names what a test observes, `concurrently()` runs two clients at once. |
| `index.py` | Builds the scenario table from the test files themselves, for [`docs/E2E_TESTS.md`](../../docs/E2E_TESTS.md). |
| `conftest.py` | The fixtures: `server`, `servers`, `devices`, `api`, `api_for`, `web`, `web_for`, `story`, `timeline`, `console`, `clients`, `browser`. |
| `test_*.py` | The scenarios. One file per suite. |

## The world a test gets

The `server` fixture starts one server with a small, fixed world:

- Users `admin` (admin), `rw` (read-write), `ro` (read-only). Every user uses the password in
  `harness.TEST_PASSWORD`. The password is synthetic and appears in logs on purpose.
- Ports `loop1` (a Loopback port, which echoes what it receives) and `cat1` (a command port running
  `/bin/cat`, so a teardown can prove a child process dies with the server).
- A web console, a client listener, and a Telnet listener, each on a free port of its own.
- Its own config directory, state directory, log directory, run directory, and PID file. Nothing here
  reads or writes `config/`, `config-local/`, or `~/.openmux`.

Build a different world with the `servers` factory, which takes the same knobs plus a `configure` hook:

```python
def test_example_a_port_with_no_write_slot(servers, story):
    server = servers(
        name="capacity-none",
        configure=lambda builder: builder.with_loopback("slot1", write_slot_capacity="none"),
    )
```

The `configure` hook also sets the policy a scenario needs: `with_login_lockout` for
`security.yaml` `rate_limits.authentication`, `with_sso` for the `web_console` trust header,
`with_dummy_pdu` for a power outlet, `with_actions` for Port Actions, `with_tcp_initiator_port` for a
port that dials a device of ours, and `scrollback_size=` on `with_command_port` when a scenario needs
a port that remembers. A Port takes its Write slot shape on the same call: `write_slot_capacity=`
(`one`, `multiple`, or `none`), `read_write_groups=`, and `read_only_groups=` on a Loopback port, and
`access_default=` for the `security.yaml` fallback. `with_user(name, permission, groups=...)` adds an
account with a console group membership. A scenario that needs a listener the default world does not
have passes `servers(telnet_kwargs={...})`, whose keys reach that listener: `require_auth=True` asks
for a login, `read_only=True` gives a read-only listener, and `target` picks a port or the menu. A
second server of its own is dialed with `clients.telnet(..., server=server)`, and `port=` dials a
listener the harness never allocated, which is how a scenario reaches one the Config Editor added to
`server.yaml`. A session that a reload ended carries no message, so `wait_for_close()` reads to the end
of its stream and reports whether the server closed it. `web_for` gives a
scenario an `HttpSession` for a named surface, which is how a scenario reads a redirect, a cookie, or a
refusal that a browser would hide. `devices.tcp()` starts one fake device: it answers the dial, and
`unplug()` closes its socket *and* stops listening, so the port really cannot reconnect until
`plug_back_in()`. `with_serial_port(name, device)` adds a Serial port; pass a device path no host has
and the reload starts that port with nothing behind it, which is the only adapter failure this tier can
produce on demand.

A Telnet session speaks the port menu the way an operator does: `open_control_menu()` sends the
escape prefix and waits for the prompt, `press("f")` answers one key, and `answer("c2")` answers one
line. `pause_reader()` stops the byte pump, which is the only honest way to model a client that stalls;
clear `reader_paused` to let it drain again. Both text sessions carry
`expect_no_text(needle, for_seconds=...)`, because "this prompt must not appear" is a guarantee. A CLI
holders request is `query_holders()`, whose `holders` list carries the holder labels the server built.
Attach a Telnet session with `choose_port("slot1")`. Do not wait for the Port name: the Port list printed at
connect already contains it, so the next keystroke can still land at the `Port:` prompt and be taken for a
choice. `choose_port` answers the prompt and then proves the attach with session info, which prints
`Port: <name>` only for a session that drives a Port. `press_until(key, needle)` retries a menu key the same
way, and `wait_for_echo("text")` types one line and returns when the Loopback sends it back, which proves a
menu kept its own keystrokes. An unattached CLI session reads commands with `command("POWER")`, and
`offset()` with `reply_after(mark)` returns only what arrived after a mark.

A Power page reads through `metric_cards()`, `outlet_rows()`, `power_badge()`, `power_badge_title()`,
`power_menu_items()`, `click_outlet_toggle(ref)`, and `click_power_toggle(ref)`. A confirm dialog arrives only
while a Playwright call is in flight, so a scenario arms `accept_dialogs(False)` before the click and then
calls `wait_for_dialogs(seen, 1)`; a plain poll of the recorded messages never sees the dialog. The
Config Editor page carries no `?e2e=1` hook, so a scenario reads it with `page_text(selector)`,
`page.evaluate(...)`, and `wait_for_selector(selector, state="visible")`, and reads which sub-page it is
on from the heading in `.page` — the browser tab title never changes. That page keeps every region in
the document and hides the others, so wait for the region the query asked for, not for any of them.
One page belongs to one surface: ask `console.user(surface, ...)` once per surface, because a second
call signs that page in again and the form is gone.
Never edit `config/` to make a test pass. It holds the shipped defaults. A test that needs a config
file writes it through `ConfigBuilder`, or replaces the generated `server.yaml` with
`servers(raw_server_yaml=...)`.

## Writing a scenario

1. Name the function `test_<suite><number>_<short title>`, for example
   `test_con01_a_signed_in_user_opens_the_console`. The name carries the scenario id (`CON-01`), which
   is how the index and [`docs/design/e2e_test_suite.md`](../../docs/design/e2e_test_suite.md) match a
   row to code.
2. Write a one-sentence docstring. The index uses it as the scenario title, so it must say what the
   test proves.
3. Add `@surfaces("web:rw", "cli:ro")`. Every surface a scenario observes is listed, so a reader can
   see what a skip costs. Names are `web:<role>`, `cli:<role>`, `telnet:<role>`, `http`,
   `server log`, and `collection`.
4. Split the body into `with step("...")` blocks. Each step is one line in the timeline, so a failure
   says where the story stopped.
5. Assert on what a user sees or reads: the terminal text, the mode, a banner, a frame's `code`, the
   server's own sentence. Do not assert on a private attribute, and do not assert on a sentence the
   product never wrote.
6. Wait on a condition, never on a fixed sleep. Use `wait_for_view`, `wait_for_text`,
   `wait_for_frame`, or `wait_for_log`.
7. Prove the negative case in the same way. `expect_no_text` and `expect_absent` watch for a moment
   and fail if the thing appears.
8. Run the scenario, then run `make e2e-doc`. The index test fails until the committed table matches
   the scenarios that exist. That command rewrites `docs/E2E_TESTS.md` whole, so write prose here or
   in the design doc, never there.

Before you settle on an assertion, read
["How a scenario gets a real answer"](../../docs/design/e2e_test_suite.md) in the design doc. It gives
the order to prefer: the frame the port sent, then a string the product wrote, then the page's own
DOM. A string typed into a test is a claim, not a proof.

Read [`CONTRIBUTING.md`](../../CONTRIBUTING.md) before you touch an `except Exception` block in this
tier: the same rule applies here, and a harness that swallows an error hides a product fault.

### How to extend a scenario

You do not need a new test to prove one more fact. Add a step, and put the assert on the artifact that
already records it: the `view` snapshot, the frame list, or `timeline.log`. `CON-01` grows this way.

When you need state the default world does not have — a second port, a policy such as
`rate_limits.authentication`, `power:`, or a device to unplug — build it with `servers`, the
`configure` hook above, and `devices`. Only reach for a `raw_server_yaml=` when the builder cannot
express the key at all.

If a fact has no readable place, that is a finding about the product, not permission to guess: the
state is not observable, and neither the operator nor a test can reach it. The page hook exists
because the state behind the menus was invisible from the DOM, and `CON-09` proves the scrollback ring
holds its first bytes because the ring has no indicator at all.

## When a scenario fails

A passing test deletes its bundle. A failing test keeps one under `artifacts/e2e/<file>/<test>/`:

| File | What it holds |
| --- | --- |
| `timeline.log` | One merged stream: the cast list, numbered steps, every client action, every frame with `OMXCTRL` decoded, every server log line, and one `FAIL` line. Read this first. |
| `raw-<surface>.bin` | The payloads too long for one timeline line, appended in full and pointed at by a `raw=<file>@<offset>` note. |
| `server-<name>.log` | Everything the server process wrote to stdout and stderr. |
| `portlog-<port>.log` | The data log the port adapter wrote for that port. |
| `config/` | The exact `server.yaml`, `authentication.yaml`, and `security.yaml` that run used. |
| `<surface>.failure.png` | A screenshot of each browser page at the moment of failure, named by its surface, for example `web-rw.failure.png`. |
| `<surface>.trace.zip` | A Playwright trace for that page. Open it with `playwright show-trace <file>`. |
| `meta.json` | Ports, ports' kinds, the config directory, and the run directory of each server. |

To investigate with the page and the server still alive, run `make test-e2e-debug TEST=<name>`. On a
failure the run prints the bundle path and each page URL, then waits for Enter. Without a terminal it
prints and carries on, so a batch run never blocks.

### How to read a timeline line

Every record is one line, and one line is one event:

```
+  0.543s cli:holder   -> lif03-ping
+  0.544s cli:holder   <- lif03-ping
+  0.544s server       -> 2026-10-27 19:41:25.772 openmux.console console_manager.py:469 INFO: ...
```

| Part | Meaning |
| --- | --- |
| `+0.543s` | Seconds since the test started, not a wall-clock time. Compare two lines by these. |
| `cli:holder` | The surface: `<transport>:<actor>`, for example `web:rw`, `telnet:viewer`, `cli:holder`. |
| `<-` | Server to client. `->` is client to server, and `.` is a server log line. |
| Payload | What moved. `OMXCTRL` frames arrive decoded as JSON, and a Telnet command arrives named, for example `IAC WILL ECHO`. |

Every record starts with its offset, so read the word after it: `STEP <n> <text>` is one numbered step of
the scenario, `FAIL` states what the test wanted against what it got, `cast` and `server` describe the
world the test built, and `snapshot` holds what a surface looked like at a moment. Read the `FAIL` line
and the lines above it before you read the pytest traceback.

Control bytes use caret notation (`^M` CR, `^J` LF, `^[` ESC), the same notation `cat -v` uses, so the
log reads the same in any terminal. A line ending that closes a payload is framing rather than content
and is left off: `hi\r\n` shows as `hi`. A line ending inside a payload stays, and a payload that is
only a line ending shows as `^M^J`. A payload longer than `MAX_PAYLOAD` characters is cut with `…`, and
the whole payload is appended to `raw-<surface>.bin`, pointed at by the `raw=<file>@<offset>` note on
that line.

## The page hook

A test cannot read the xterm buffer from outside, because
[`console.js`](../../openmux/server/webui/static/js/console.js) keeps the terminal in a file-scoped
`term`, and the `.xterm-rows` DOM shows the visible rows only. So that file exports
`window.__omxE2E.view()` when, and only when, the URL carries `?e2e=1`. `console.py` reads that one
function and nothing else.

`view()` returns one point-in-time picture:

| Key | Value |
| --- | --- |
| `mode` | `read-write` or `read-only`, as the server's `client_mode` frame said. |
| `connected`, `port`, `connected_port`, `port_up` | The session state of this page. |
| `lines` | The whole xterm buffer, scrollback first, trailing spaces stripped. The Loopback port answers Enter with `[ENTER]`, so match a round trip with a substring. |
| `notices` | The `lines` of every frame that carried a `code`, oldest first, capped at 200. |
| `banner` | `{text, class}` of the banner region, `{null, null}` when hidden. `text` is the page's first sentence; the reason the server sent follows it in the DOM, which `text_of("#banner")` reads. |
| `badge`, `badge_state` | The power badge label, and its tint: `all`, `some`, `none`, `unknown`, `hidden`. `ConsoleSession` exposes them as `power_badge()` and `power_state()`. |
| `viewers` | The viewer count shown in the header. |
| `frames` | Every control frame this page received, as `{type, code, ok, dir}`. |
| `body_class` | `document.body` classes, which carry `mode-readonly`, `ws-down`, and `port-down`. |
| `menus` | `ro` and `ctrl` as the visible items `{label, disabled}`; `viewers` as the text of each viewer row the page holds, shown or not, because that list carries no menu markup. |

Two rules keep that hook small enough to trust:

- It only reads. Nothing in the hook changes the page, and the object is undefined without `?e2e=1`.
  `test_con02` proves both.
- When you add a state region to the console, add it to `view()` and to this table in the same change.
  A test that reads the DOM instead will break on the next `innerHTML` update.

A stray comma in that file kills every console behaviour with no build error, and the first sign is
that sub-views stop switching. `ConsoleSession.open()` therefore fails with a message that names
`console.js` when the hook is missing, and `node --check
openmux/server/webui/static/js/console.js` catches the syntax error before a test run.

## Machine notes

- Chromium is not downloaded by `pip`. Run `make e2e-install` once; it needs network access.
- macOS caps a Unix socket path at 104 bytes, and a pytest temp path is close to that already. The
  run directory is therefore a short directory under `/tmp`, and the harness removes it when the
  server exits cleanly.
- Use `pgrep -P <pid>` to list a server's children. The `ps -o pid= -ppid` form is not portable and
  fails on macOS.
- Finish every `make` target before you start a tier. `make lint`, `make format`, `make test`, and
  `make e2e-doc` each run `pip install -e .` first, which rewrites the editable package tree under
  `build/__editable__*/`. A server subprocess that imports the package mid-rewrite stops with
  `ModuleNotFoundError` for a file that exists in the repo. The tier itself never installs anything.
- An interrupted tier can leave a server running. List them with `ps -ax -o pid,command | grep
  "[p]ytest-of-runar"`; each path names the run that started it. Stop them before the next run.
- The default world's Telnet listener is unauthenticated on purpose, so a scenario can walk the port
  menu. The server logs a warning about it at boot, which is expected and not a failure.
- One test server costs about 1 second to boot. The whole tier is a few tens of seconds, which is why
  it does not run inside `make test`.
- Browser scenarios ask for a page with the `console` fixture: `page = console(user="admin").open(
  "/healthz")` lands on a page, and `.open("/console", port="loop1")` opens the console. The fixture
  signs the user in once and reuses that context, so one scenario's pages share cookies and
  `localStorage`. `console.reset(user=...)` throws that away when a scenario must be a stranger.
- Scrolling back is a per-port setting, named `scrollback_size` in `server.yaml`. A ring starts empty,
  so a port with scrolling on must be *written to* before a page can read anything back — that is the
  whole of `CON-09`, and the reason a loopback port never replays on its own.
- A command port whose child exits does not go down. The port stays up, the server sends no notice, and
  the next Enter prints `[OpenMux:PROCESS_NOT_RUNNING <server>/<port> …]` into the terminal itself, so
  a scenario matches that line in `lines` and not in `notices`.
