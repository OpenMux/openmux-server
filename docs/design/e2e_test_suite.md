# End-to-End Test Suite

**Status:** Implemented through Phase 9, and through Release 2 except the Config Editor's `muxcon` writes: 160
scenarios in fifteen suites run green. [`docs/E2E_TESTS.md`](../E2E_TESTS.md) lists them and is the only place a group's
count is written down, because restating those numbers here let two of them go stale. See **Build status** near the end.
**Date:** 2026-11-01
**Scope:** A test tier that runs the real server as a process and drives it through a browser, the CLI protocol,
Telnet, and SSH at the same time.

Related: [ADR-0001 Access wording belongs to the server](../adr/0001-access-wording-belongs-to-the-server.md),
[ADR-0002 Listener session module sits above a byte transport seam](../adr/0002-listener-session-module-sits-above-a-byte-transport-seam.md),
[Port Actions](port_actions.md), [MuxCon](muxcon.md), [Telnet listener](telnet_listener.md),
[SSH listener](ssh_listener.md), [tests/README.md](../../tests/README.md).

---

## Purpose

Most tests in `tests/` build one component and pass it fake objects. They are fast and they are useful, and they
cannot answer one question: does the running product work? This tier answers it.

Each test starts a real `openmux.server.main` process with a generated config, then acts as several users at once
through a real browser, the client listener protocol, and Telnet. Tests assert on the bytes each surface shows.

The suite targets the parts a unit test cannot reach:

- The HTTP and WebSocket protocol the browser uses.
- The Access decision wording on every surface (see [ADR-0001](../adr/0001-access-wording-belongs-to-the-server.md)).
- Reload of a live server that holds attached clients.
- More than one permission level on one port at the same moment.

---

## Starting state

What existed before Phase 0, and the gap each part left. For the scenarios that exist today, read
[`docs/E2E_TESTS.md`](../E2E_TESTS.md): `make e2e-doc` rewrites that file, and `IDX-02` fails when the committed copy
is older than the tests.

| What existed then | Coverage | Gap then open |
|---|---|---|
| `tests/` unit and component tests (about 3000 items) | Adapters, managers, ACL, power drivers, protocol edge cases | No test starts the server process. No browser. |
| `scripts/smoke_workflow.py` + `config/integration_test.yaml` | Starts the server, one CLI session, Loopback round trip | Manual only. No `make` target, no assertion depth. |
| `docs/SMOKE_TEST.md` | How to run that script by hand | Documents the manual path, not a pytest tier. |
| `tests/README.md` | Run `pytest`, pick a marker, add a docstring | Points at bare `pytest`, not the `make test-*` targets, and advertises `integration`, which no test uses. |

The `e2e` name and the `tests/e2e/` path were free: no file under `tests/` matched `*e2e*`, `*smoke*`, or
`*integration*`, and no test carried the registered `integration` marker. `playwright` was not a dependency, and no
browser binary was installed.

Three claims above are history: this tier exists, `playwright` is a dev dependency (`requirements-dev.txt`) with the
Chromium binary installed by `make e2e-install`, and `tests/README.md` names the `make test` and `make test-e2e`
targets. Two stay open on purpose: `scripts/smoke_workflow.py` keeps its manual path with no `make` target, recorded
under item 3 of [Open decisions](#open-decisions), and its optional serial peer through `socat` waits as item 4 of
[Release 2](#release-2). No test carries the `integration` marker, and `tests/README.md` now says it is declared but
unused.

---

## Design decisions

| Decision | Choice | Reason |
|---|---|---|
| Server | Real subprocess (`python -m openmux.server.main --config-dir <tmp>`) | Tests startup, config load, signal handling, and shutdown, which an in-process test cannot reach. |
| Scope of release 1 | Loopback, command, PDU (`driver: dummy`), web console, client listener, Telnet listener | Covers the surfaces without hardware. SSH and MuxCon wait for release 2. |
| Browser | Playwright, sync API, Chromium | The console needs a real browser: xterm, WebSocket, and the live event socket. |
| Config | Generator writes synthetic files in `tmp_path` | Tests never write to `config/`, which holds pristine defaults (see [AGENTS.md](../../AGENTS.md)). |
| Default run | `make test` unchanged, no browser needed | A developer without a browser must not see a failure. |
| First config | Minimal, then configured through the Config Editor | The same tests then prove reload works on a live server. |
| Concurrency | Sync test code, one reader thread per socket | Several clients act at once, and the server delivers control frames one after another. An async test runner buys nothing here; see [Concurrency model](#concurrency-model). |

---

## Architecture

Three modules, one per concern:

- `tests/e2e/harness.py` — `free_ports()`, `ConfigBuilder`, `E2EServer`.
- `tests/e2e/console.py` — `login()`, `ConsoleSession`, `expect_notice()`, `expect_banner()`.
- `tests/e2e/textclients.py` — `open_cli_client()`, `open_telnet_client()`, `port_log_lines()`.

```
E2EServer           openmux.server.main subprocess, its own config dir and ports
  ConfigSession     a browser context: one signed-in web console user
  CliSession        a client listener socket: the real CLI protocol
  TelnetSession     a Telnet listener socket
  Api               aiohttp with Basic auth, for setup and for reading server state
  Story             numbered steps and surfaces, written to one timeline
```

Design rules:

- **Real client code.** `CliSession` wraps `TcpClientAdapter`, so a protocol change fails a test. The Telnet helper
  is deliberately not product code: Telnet is a line protocol, and a product-logic change there must not break the
  test that proves the product behaviour.
- **Nothing shared.** One server per suite, its own config dir and its own ports. The suite mutates config, so no
  state may cross a suite boundary.
- **Env pinned.** The child gets `OPENMUX_STATE_DIR`, `OPENMUX_RUN_DIR`, and `OPENMUX_LOG_DIR`. Without them a
  server writes `~/.openmux`, and port logs land in the developer's `logs/`. Port logs are
  `{OPENMUX_LOG_DIR}/ports/<port>.log` (`locations.ENV_LOG_DIR` → `DataLogger.set_base_dir` in
  [`main.py`](../../openmux/server/main.py)). There is no config key for it.
- **Config guard.** `ConfigBuilder` refuses an existing directory, so no test can boot `config/` or `config-local/`.
  `config-local/server.yaml` holds a real PDU host and community string, and those would reach a shareable log.
- **Assertions on content, never on timing.** The ws-down banner debounces 1000 ms, so a fixed sleep both flakes and
  misses. Wait for a predicate with a deadline.

`E2EServer` stops with SIGINT (same path `make run-server` uses) and fails the test if the exit code is non-zero, so
a shutdown crash surfaces in the test that caused it.

### Test hook in the console

[`console.js`](../../openmux/server/webui/static/js/console.js) keeps its terminal in a module-scoped `term`.
Playwright cannot read it, and `.xterm-rows` shows the viewport only, not the scrollback. Phase 0 adds a read-only
hook gated on `?e2e=1`:

```js
window.__omxE2E = { clientMode: () => clientMode, lines: terminalLines, notices: recentNotices };
```

The gate matters: a production page must not expose the object, and the check must not change console behaviour.

---

## Concurrency model

Most scenarios have several clients live at once, so the harness needs concurrency. It does not need an async test
runner.

**Sync test code, one reader thread per socket.** Playwright runs in its sync API. Each CLI and Telnet session owns a
daemon thread that reads for the whole life of the session and pushes bytes into a queue. An author writes
`cli.until("[POWER]", timeout=5)` and never sees the thread. A browser needs no thread at all: Chromium is a separate
process, so the page keeps receiving frames while Python waits on a socket.

| Model | Cost |
|---|---|
| `async_playwright`, async everywhere | `async def` in every test body, and one event loop holding a subprocess, a browser, and log tailers. `page.pause()` and `pdb` get awkward. |
| Sync Playwright plus one reader thread per socket (chosen) | Threads inside the harness only. Test bodies stay linear, which is what the readability rules ask for. |

Most scenarios that read as simultaneous are propagation, not concurrency: one surface acts, then the test waits for a
predicate on another surface. Sequential code does that. Only a few need real simultaneity — two Takeover requests on
one freed seat. `concurrently([...])` runs actions in threads behind one `threading.Barrier`, joins with a deadline, and
returns every result or raises once with all of them. The freed-seat race is not built: `RW-13` covers the
seat-frees-then-requested sequence in order, and the simultaneous form is available in the `RW` file when it is wanted.
No Port Action scenario needs it: the server refuses a second run on a port that holds one (`ACT-04`), so the outcome
does not depend on which of two requests landed first.

### Every attached surface keeps a reader

This rule comes from the code, not from taste.

The **data** path is decoupled per client. `PortManager._fan_out_to_client_queues`
([`port_manager.py`](../../openmux/server/port_manager.py)) is synchronous: one `put_nowait` per client queue, and a
full queue drops its own oldest chunk and counts it in `dropped_chunks`. A task per client drains that queue
(`ConsoleManager._forward_data_to_client` in
[`console_manager.py`](../../openmux/server/console_manager.py)), so a slow client stalls only its own queue.

The **control** path is not decoupled. `ConsoleManager.broadcast_control_frame_to_port` walks the attached clients and
awaits each delivery in turn, and each delivery ends in a blocking write: `await ws.send_str(...)` for a web console
client ([`web_console.py`](../../openmux/server/web_console.py)) and `await session.transport.drain()` for Telnet
([`listener_session.py`](../../openmux/server/listener_session.py)). A client that stops reading blocks every client
later in that loop. Presence rides this path on every attach, Detach, promote, and demote
(`ConsoleManager.broadcast_presence`). Two rules follow:

- **Never attach a session and ignore its bytes.** A CLI socket that nobody reads can delay an Access decision
  reaching another terminal, which reads as a flaky console.
- **A harness reader must not push back on the product.** The harness queue grows and counts its drops; past a
  per-session cap it raises an error naming the session. A reader with a bounded blocking queue would rebuild the
  stall inside the harness.

Because the server queue drops the oldest chunk, a test that stalls a reader on purpose must assert on the newest
bytes, never on every byte the port ever sent.

A control frame shares one socket with the bytes already written to it, so it cannot arrive before them. Measured
2026-10-28: with a 512 KiB burst in flight, a viewer-set change broadcast one presence frame, every delivery in the
fan-out took 0 ms, and the frame still reached the two healthy readers 0.25 s later on an idle machine and 7.9 s later
on a machine held at 2.5x CPU oversubscription. The wait is the bytes in front of the frame, and machine load scales it
with no ceiling. A scenario that times a control frame must drain that burst from that reader first.

### RW-20: a stalled viewer

`RW-20` tests the control path: attach three viewers, stop one reader on purpose, then change the viewer set so the
server broadcasts presence, and assert the other two receive the frame inside one deadline. The scenario was written
marked expected-to-fail, on the reasoning in the section above that one blocking write holds up every client later in
the loop.

**Measured 2026-10-26: the scenario passed, so that expectation was wrong for this shape.** The 512 KiB backlog of the
stalled viewer travels through the data-forwarding task of that client, and its `drain()` waits there, in that one task.
`broadcast_control_frame_to_port` ran on its own and delivered the presence frame to both readers that keep up. One limit
on that result, so it is not read as a general guarantee: because the port data queue drops its oldest chunk, the backlog
of the stalled viewer ends where the queue dropped, so the test asserts on the newest bytes only.

**Measured 2026-10-28, the other shape also does not block.** The first result left open the clients whose session write
happens inside the read of a request: the Telnet and SSH shell (`await session.transport.drain()`,
`listener_session._write_session`) and the web console (`ws.send_str()`). A viewer on that path was stalled for real by
stopping its reader thread and shrinking its receive buffer to 8 KiB, proven with `getsockopt`. That viewer took 4335
bytes while the two CLI readers that kept up drained 9.4 MB and 8.7 MB. The control write to the stalled viewer returned
in 17 ms and the writes to the readers behind it in 0 ms, and both healthy readers had the frame 0.05 s after the viewer
set changed. A local client never gets far enough behind to hold a write: its data queue drops its oldest chunk past 100
chunks (`PortManager._fan_out_to_client_queues`), which keeps its socket below the 64 KiB level where `drain()` waits.

What remains true is the ordering, and it is not reachable over a local socket: the loop in
`broadcast_control_frame_to_port` awaits one client at a time, so a client that keeps a `drain()` waiting for seconds
delays every client after it for those seconds. A test with a fake adapter that holds one client for 2 s gives the other
two a delivery 2 s late, and issue #106 carries that result. A real slow peer, such as a link with a small bandwidth
delay product, is the only way to show it through the API.

**Measured 2026-10-28: the scenario then failed in a full run, and the broadcast was again not the cause.** It asked for
a presence frame 512 KiB into the burst it had written itself, and gave that frame 10 s. The fan-out took 0 ms per
delivery, and the frame arrived when each reader's socket had drained: 0.25 s idle, 5.06 s with a 16 MiB burst idle, and
7.9 s at 2.5x CPU oversubscription. Nothing in the product sets those numbers, so the deadline measured the machine. The
same run showed why the stall is not proof of backpressure either: the delivery to the viewer that stopped reading
finished in 0 ms at 512 KiB, 4 ms at 16 MiB, and 6 ms under a slow 15 MiB feed, because the socket of a local client
absorbs up to `kern.ipc.maxsockbuf` (8 MiB measured) and the port queue drops the rest.

**The scenario now measures the fan-out only.** It waits until both healthy readers stop receiving bytes
(`_wait_until_quiet`), it proves the stall happened by comparing what each viewer took (34 bytes for the stalled one
against the whole burst for the readers that kept up), and it changes the viewer set by closing one client instead of
attaching one: an attach waits for the broadcast it triggers, so a held-up fan-out hides inside the attach deadline. The
final report names the position each reader holds in the fan-out, ahead of or behind the stalled client, because the
shape of the failure is the diagnosis. The reported failure had both readers missing, which a block at the stalled
client cannot produce: holding that delivery for 15 s moves only the readers behind it, and was measured to make
`RW-20` fail naming `third` while `first` receives the frame. `RW-20` therefore asserts what the product delivers —
presence reaches every reader that keeps up — and carries no `xfail`.

---

## Observability (off by default)

A quiet run stays quiet. Two flags turn on detail, and one artifact bundle is always kept for a failure.

`Story` records every step and every byte on every surface into one merged `timeline.log`, so a reader sees the
order across a browser, a CLI socket, a Telnet socket, the server log, and the port logs. The log opens with a cast
list and a legend:

```
cast    web:rw     user 'rw'    read-write   browser tab 1
        cli:holder user 'admin' read-write   CLI over the client listener
port    loop1  write slot capacity=one  feeds=rack1.1,rack1.2
legend  <- server to client   -> client to server   ^M ^J control bytes   OMXCTRL = control frame
```

| Flag | Effect |
|---|---|
| `--e2e-live` | Timeline and server log to stdout, and each browser gets a visible window. |
| `--e2e-reveal` | On failure, pause instead of discarding, so a human can inspect and drive the page. |

`--e2e-reveal` is the troubleshooting path a plan cannot verify on its own. Test it before release: fail one test on
purpose and confirm a human can see what each surface held and reach the page by hand.

---

## Readability rules

The harness is for a person who has never opened this code.

1. **One narrative string, three readers.** `with story.step("the CLI client takes the write slot"):` prints the same
   text in the test, in `timeline.log`, and in the failure banner.
2. **Handles named by surface:** `web_rw`, `web_ro`, `cli_holder`, `telnet_viewer` — never `page2`. The same key names the
   timeline surface, the browser context, and the artifact file. `docs/GLOSSARY.md` defines **Surface**; do not call it a role.
3. **Keyword-only arguments:** `attach(client, port="loop1")`, never a positional mode string.
4. **Every test names the surfaces it drives** in a marker, for example `@surfaces("web:rw", "telnet:viewer")`, so a reader
   sees at a glance which transports a scenario proves and in which permission.
5. **About 25 lines** per test body. A longer body means a helper is missing.
6. **Plan id in the method name:** `test_rw06_web_takeover_words_both_terminals`. So `--collect-only` reads as a
   specification.
7. **Vocabulary from [docs/GLOSSARY.md](../GLOSSARY.md).** Takeover, not steal. Soft and full reload, not hot
   reload. Write slot capacity, Access decision, Outcome code, Feed, Outlet ref, port.

A generated index (`--e2e-list`) writes the scenario table into [docs/E2E_TESTS.md](../E2E_TESTS.md), and one test
fails when the committed file is stale. That catches a new test with no docstring or no named surface. That file is
regenerated whole, so prose never belongs in it: `make e2e-doc` deletes anything written there.

### How a scenario gets a real answer

Write down what the scenario claims, then give each part the strongest proof that exists for it.

1. **A port state or an Outcome code**: the server's own answer, read from the frame the port sent, or from
   `/api/ports`. The harness never decides what the code is.
2. **Words a person reads**: the product's own string. Import it from `openmux` when the product holds it as a
   constant; read it off the surface when the page or the server wrote it. A test string typed by hand is a claim, not
   a proof — `AUTH-02` asserts the sentence `login.html.j2` renders, and the scenario fails if the page changes without
   the test agreeing.
3. **A browser claim**: a real Chromium page, and the state from the hook or the visible DOM. `CON-06` shows why this
   tier exists: the hook reported zero viewers while the page showed two. The rows are plain divs and `e2eMenuItems()`
   selected menu-item elements only, so the read was wrong and the page was right. A scenario that asserted on the read
   alone would have reported a product defect that never existed.
4. **Anything the suite cannot prove**: an honest skip or a named outstanding id. A skip names what it proves and what
   it does not. Release 1 has no outstanding id left. `ConfigBuilder.with_muxcon` now writes a `muxcon:` section, so a
   scenario can link two real nodes; the federation surfaces still unproven are named in **Release 2** item 2.

Three traps in this tier cost a debugging session each, so they are rules now.

- **A confirm dialog arrives only while a Playwright call is in flight.** Sync Playwright hands a modal dialog to its
  handler when a call is running, so polling a Python list of captured messages never sees one. Arm
  `accept_dialogs(False)` before the click, then call `wait_for_dialogs(seen, 1)`, which keeps a call in flight.
- **A Port name proves nothing.** The Port list printed at connect contains every Port name, so waiting for the name you
  typed returns before the listener has attached anything, and the next keystroke is taken for a Port choice.
  `choose_port` proves the attach with session info, which prints `Port: <name>` only for a session that drives a Port.
- **One typed line does not mean one read.** A client that answers a prompt while the server still writes it sends the
  next line in the same segment. The auth/menu phase keeps what it does not ask for and hands it to the next reader
  (`LIF-11`, issue #103), which is what lets `send_lines_at_once` ask for that shape. `choose_port` still proves the
  attach with session info, because a Port name in the received text proves nothing, as the note above says.

### How to fix a failure that touches shared world state

When a scenario changes the world other scenarios read — a `server.yaml`, the login lockout counter, a session cookie —
the failure usually looks like a product bug in a *different* suite. Do not fix the other suite.

1. Restore the state inside the scenario that broke it, not in a later fixture: `servers.raw_server_yaml(post_restart)`
   restores what `LIF-05` replaced.
2. Prove the shared subject clean before you conclude the product regressed. The lockout counts per account across
   processes, so a probe first signs the account out with wrong passwords and waits it clear; a `401` then means the
   product, and a `200` means this probe.
3. Say in one line why the change is safe for the tests that follow.

---

## Test inventory

The suite today runs 160 scenarios in 15 files: `LIFECYCLE` 11, `AUTH` 11, `CON` 14, `RW` 21, `PWR` 19, `CMD` 6,
`SER` 6, `CFG` 20, `ACT` 11, `ST` 6, `SSH` 10, `SOK` 7, `MXC` 9, the index checks 3, and the story recorder 6. The generated
[docs/E2E_TESTS.md](../E2E_TESTS.md) is the list of what runs, and its count is checked by `IDX-02`. Every group below now
maps to a file that runs. What is still unwritten sits in the **Release 2** list further down, where item 2, MuxCon with
two live nodes, now has its base suite (`MXC`) and names the surfaces that stay unproven. Full wording for every built id
lives in that index.

### LIFECYCLE — process and listeners (`tests/e2e/test_lifecycle.py`, 11)

Boots, `/healthz` public plus `/api/ports` 401, Loopback and command round trip over the client listener, bad config
exits non-zero naming the key, SIGINT exits clean, SIGHUP soft reload keeps an attached client, SIGUSR1 full reload
drops then restores it, a reload with an attached client leaves no cleanup error in the log, control socket status with
mode 0600, and a Telnet login whose four lines arrive in one write reaches the Port those lines name.

### AUTH — login and permission matrix (`test_auth.py`, 11)

Built: the login form and the cookie attributes it hands out, the wrong-password sentence, lockout after failed
attempts and its release, `?next` round trip, logout and the cookie taken back, a wrong Basic header not falling
through to a valid cookie, `403` per permission level on the Config Editor, the CSRF token required for a session POST
and exempt for Basic, the SSO trust header with every forgery refused, and the external helper.

`AUTH-11` drives the second credential authority, `external_auth` in `authentication.yaml`. A helper script answers for
a name this file does not hold. Its answer opens a session, and grants no role. The `groups` mapping decides the level.
The same helper answer is read-only once the group is mapped, and refused once it is not. `default_permission` is the
only key that turns that refusal into a session, and it grants the level its own value names. The page answers the
permissionless case with the sentence `AUTH-02` owns for a wrong password, so the page cannot say which half failed.
Only the server log names group membership and `external_auth.default_permission`, so the log is the oracle for that
step. A static entry answers for its own name: the helper's password is refused for it. The client listener refuses the
same identity outright with `AUTH:FAILED:Authentication failed`, because its password HMAC needs a stored hash that no
helper name has.

### CON — console page (`test_console.py`, 14)

A real Chromium page with a seeded `rw` user signs in and gets her own text back (`CON-01`), which also smoke-checks
`/logs/loop1` and `/about` and their nav. The page hook answers under `?e2e=1`, and a page without the flag has no hook
at all (`CON-02`). A console opened with no port says to choose one and its button asks for one (`CON-03`); the sidebar
links each port by the address the page reports (`CON-04`). The same button is the only way on and off the port, and its
label follows the state it will leave behind (`CON-05`). A second viewer on the port tells the first who she is and
keeps her own mode (`CON-06`). A page switched to read-only shows that mode and swallows its own keystrokes (`CON-07`),
and the info overlay reports the port and seat behind the menu icons (`CON-08`). A reader who arrives mid-line receives
what the port already carried (`CON-09`). Two different silences look alike to a reader and are told apart here: a
socket the harness kills is announced as a lost connection and the next connect clears it (`CON-10`), while a device
that stops answering closes the port and, after the page's debounce, the console names the peer address, marks the port
down, and clears all of it when the device answers again (`CON-11`). `POST /api/reload` rebuilds the sidebar with one
POST and no reload, leaving one active port (`CON-12`), the theme survives a reload on its own key (`CON-13`), and
`embed=1` keeps the console full window while the port still echoes (`CON-14`).

Two measurements corrected the plan. The plan asks for `#liveDataBadge` to go red on this page: that badge belongs to
the overview and the power page, which no console view draws, and the console's equivalent is the banner a dead socket
raises (`CON-10`) and the stale pill the port-down banner explains (`CON-11`). The plan also expects `scrollback_done`
to be visible in the terminal: the server sends it as a frame mark before the live stream, and the page uses it to order
the feed rather than to print a note, so `CON-09` reads it from the frame log. What that scenario proves is the useful
half — a page that arrives mid-line still gets the bytes behind the mark — with a page that asks for no scrollback as
the control, because that page must not replay.

### RW — write slot capacity and Takeover (`test_write_slot.py`, built: 21)

Ports at capacity `one`, `multiple`, and `none`, one port with `read_write_groups`, one `read_only: true` listener, and
one viewer stalled on purpose. Every assertion that quotes a sentence takes it from the Access decision module
(`decide(...).lines`, `notice(...).message`), because a sentence typed into a test is one sentence that lets a surface
drift.

| Id | Proof |
| --- | --- |
| `RW-01` | The first write-entitled client attaches read-write and drives, and the holders list names it in the label shape the module builds. |
| `RW-02` | A page that opens a held port comes up read-only, answers `capacity_full` in its attach frame, prints no refusal line, and still reads what the holder types. |
| `RW-03` | One holder label appears in the CLI holders reply and after `Held by` on the page, and a second viewer reads the same label. |
| `RW-04` | A request on a held one-seat port answers `promote_failed` in the module words, and the frame names the holder that has the seat. |
| `RW-05` | The same refusal renders the same lines on the CLI protocol, on Telnet, and on the page. |
| `RW-06` | Release gives the seat up without leaving the port, and the next request is granted. |
| `RW-07` | A closed socket frees the seat, so a departed client never blocks a port. |
| `RW-08` | A Takeover grants the taker, names the victim on both terminals, and leaves the loser attached as a viewer. |
| `RW-09` | A Takeover that names the id the holders list prints takes from that holder, and an id nobody holds is refused by name. |
| `RW-10` | The Telnet menu holders key prints the same label, and its request key answers with the same refusal lines. |
| `RW-11` | Takeover from the Telnet menu grants the seat on the empty-target answer, and one line lands in the server log and in the port log. |
| `RW-12` | A `multiple` port grants both entitled clients at once, shows two holders, and refuses a Takeover there as pointless. |
| `RW-13` | A read-only account watches, is refused `not_entitled` when it asks to drive, and its keystrokes never reach the port. |
| `RW-14` | A `none` port keeps admin and a read-write account read-only, answers `no_write_slot` with its capacity, and names no holder. |
| `RW-15` | A `read_only: true` listener answers both write keys with its own reason, never shows the Takeover prompt, and drops what its session types. |
| `RW-16` | `read_write_groups` decides who drives, `read_only_groups` decides who watches, an unlisted account is refused at attach, and admin passes the boundary but not a held seat. |
| `RW-17` | A page whose attach a group list refuses shows the refusal sentence, reports that it is not connected, and the listed member still reaches that port. |
| `RW-18` | The `Take control` item on the page is the same Takeover: the page goes read-write and the CLI holder is told who took the seat. |
| `RW-19` | A demoted page changes its mode, its pill, and its `mode-readonly` class at once, prints the module sentence naming the taker, and stops sending keystrokes to the port. |
| `RW-20` | One viewer stalled on purpose still leaves the presence frame reaching the two readers that keep up. See [Concurrency model](#concurrency-model). |
| `RW-21` | A console that may not drive reads why, on Telnet and on the CLI protocol, and no notice names the holder. |

Four facts from the Access decision module shaped these ids, and all four are measured, not assumed.

- A plain attach grant and a `capacity_full` grant carry **no** `lines`: the reason lives in the frame `message`, which is
  what the web console and the CLI protocol read. A Telnet or SSH session gets no frame at all, so its attach line now
  carries the decision `sentence` instead, and a refused write carries `write_blocked_notice` (`RW-21`). `RW-01` and
  `RW-02` read the mode and the frame code instead of a sentence.
- The sentence in the design draft — that a `promote_failed` answer is worded by the port capacity — is wrong. One
  place, `ConsoleManager._seat_refusal`, picks the Outcome **code**: `no_write_slot` on a `none` port,
  `take_without_slot` on a `multiple` port, and `promote_failed` only on a one-seat port whose promote failed. Each code
  has one wording, so capacity never restyles one code.
- The slot count is an invariant that `ConsoleManager._take_write_slot_local` restores on a half-completed transfer:
  when the promote of the taker fails after the demotion, the server promotes the victim again. No `RW` id drives that
  path at process level yet; it needs a port whose promote fails, which the harness cannot build from a config file.
- A write the session may not make is answered in words the server owns: `write_blocked_notice(code)` for the refusal
  and `read_only_grant_notice(code)` for the read-only grant (`RW-21`). The refusal reads the Port on every Enter,
  because the write slot this session waited for may be free by then, and neither line names the Client in front of it.

One measured behavior is recorded here because it surprises a reader: a listener with `read_only: true` gates its own
write keys and its own data path, but not the mode the access ladder grants at Attach. Such a session can therefore hold
the Write slot of a one-seat port while it cannot drive the port. Measured 2026-10-26: one session attaches over a
read-only Telnet listener, the holders list of that Port then names it `admin@127.0.0.1 (rw)`, and a second
write-entitled client attaches read-only because the seat is gone. That session also answers both write keys with
`[This listener is configured read-only]`, so no operator can drive the Port until the seat is released. `RW-15` asserts
the guarantees the knob does give — the reason, the missing Takeover prompt, and the dropped keystrokes — and stays clear
of the slot, because no document says that knob is meant to release a seat. Whether `read_only: true` should attach
read-only is an open product question, tracked in [issue #102](https://github.com/OpenMux/openmux-server/issues/102).

### PWR — power subsystem (`test_power.py`, 19 scenarios)

One world serves the group: a `driver: dummy` PDU `rack1` with three outlets, and three Loopback consoles.
`rack1.1` feeds `solo` and `dual`, `rack1.2` feeds `dual` alone, and `rack1.3` feeds no console. A scenario that needs a
feed nobody provides declares `rack1.9` on a fourth console, as `PWR-02` does. The dummy driver adds three fictions,
listed in the module docstring: an outlet reports 120 W and 230 V whether it is on or off, PDU load is
`120 W x outlets that are on`, and a switch answers at once with no poll delay. Assertions on watts therefore check
shape, never a real load.

| Id | What one scenario proves |
| --- | --- |
| `PWR-01` | `/api/power` mirrors the configured PDU, and one switch moves that outlet and the totals only. |
| `PWR-02` | A feed no outlet provides is named by the API, by the landing page, and by the badge of its console. |
| `PWR-03` | Power nobody configured is refused one way, and power switched off another way, on every surface. |
| `PWR-04` | The landing page counts the estate, its row opens that PDU, and the detail page repaints after a switch someone else made. |
| `PWR-05` | A read-write page carries one switch per outlet; a read-only page carries none and says it is read-only. |
| `PWR-06` | The first confirm names the console that would lose all power, and dismissing it switches nothing. |
| `PWR-07` | The second confirm names the console that stays up through another feed, and accepting it switches. |
| `PWR-08` | The console badge counts the feeds that are on, changes tint as they fall, and warns when none are left. |
| `PWR-09` | The badge menu switches one feed and states its wattage, and the switch travels over the console socket. |
| `PWR-10` | The same badge menu on a read-only console lists the feeds and their wattage with no switch at all. |
| `PWR-11` | The console terminal warns when the last feed falls, and reports the count when power returns. |
| `PWR-12` | An attached watcher on CLI and on Telnet is told about one switch in the same words. |
| `PWR-13` | `POWER` lists every PDU, `POWER <pdu>` lists its outlets, and `POWER <ref>` answers one line. |
| `PWR-14` | A malformed line, an unknown PDU, an unknown outlet and a bad state are each refused in one sentence. |
| `PWR-15` | `POWER <ref> off` warns which consoles lose everything and which hold on, before it switches. |
| `PWR-16` | The Telnet `p` menu toggles one feed by number, refuses a bad number, and gives the console back on Enter. |
| `PWR-17` | A read-only identity is refused on REST, on the command line, and on the page, in that surface's words. |
| `PWR-18` | `read_write_groups` reaches power: an outsider is told which console protects itself, a member is not. |
| `PWR-19` | One switch writes one control line naming who acted and which consoles fell; a refusal writes none. |

Four measured behaviours are recorded here, because a reader would otherwise assume the tidier version.

- No `power:` section and `enabled: false` are **not** the same state. Without the section there is no power adapter,
  so all four power routes answer 404 and the `POWER` command says "Power management is not configured". With
  `enabled: false` the adapter exists: `GET /api/power` answers 200 with `enabled` false, a switch answers 409 and says
  "power management is disabled", the page carries a banner — and the `POWER` command still says "not configured".
  `PWR-03` asserts both, including that divergence.
- A declared feed that no outlet provides still counts. The badge of that console reads `0/1` with state `unknown` and
  the tooltip `PDU power: 0 of 1 feeds on`; it is never hidden, because silence would read as "no power wired". The ref
  also appears in `unresolved_refs` and on the landing page, and in no outlet's `mapped_ports`.
- `mapped_ports` and `off_impact` keep the order the ports are configured in, not sorted order, so `POWER rack1.1`
  prints `-> solo, dual` because `solo` is configured first. A group refusal names the consoles in that same order.
- The all-lost notice differs by audience on purpose: a Port's own data log records
  `[POWER WARNING] all power feeds to this console are now off`, while a terminal attached to it reads
  `[POWER WARNING] all power feeds are now OFF for this console (<ref>)`. One is an audit line, one is a warning with
  the ref an operator acts on. `PWR-19` and `PWR-12` assert each where it belongs.

Three defects in the product came out of measuring, and each is fixed with the scenario that proves it.

- The terminal never printed the restore sentence. `console.js` compared `powerState.all_power_lost` **after**
  `updatePowerBadge` had replaced `powerState`, so the branch was dead code. `PWR-11` prints both sentences, and fails
  against the previous file.
- An open Power page kept the old switch position when someone switched an outlet that feeds no console. A change
  reached the page only through the port meta event of a console that carries that feed, so `rack1.3` never arrived.
  `power_monitor` now nudges `power_changed` after a successful switch. `PWR-04` repaints both rows, and fails against
  the previous file. A change a poller detects on an unmapped outlet is still not nudged, because that is the poller's
  own path and no scenario drives a poller.
- A Telnet session ended a menu line at the CR and read the paired LF as the next line. That phantom
  line closed the `p` menu by itself and ate the keystroke after it. `read_prompt_line` now tells the
  transport about a CR, and the Telnet transport drops the LF that follows it. `PWR-16` exits the menu,
  waits for the next echo, and asserts no phantom line sits between the two (issue #100). The reader in
  front of that pair, `_read_auth_line`, which reads the login, the password and the Port choice, kept
  only the text before the first LF of its 256-byte read and discarded the rest, so a line typed while a
  prompt was still written never reached anything. It now hands the rest to the next reader, and after
  the last prompt that reader is the session on the same transport (issue #103). `LIF-11` sends the
  login, the password, the Port choice and one Port line in one write, and asserts the Port echoes that
  last line. A Port choice that ends at CR LF is the one case where the phase, not the session, decides
  where that LF goes, so
  `tests/server/test_telnet_listener_adapter.py` covers it: the LF ends the choice and never reaches the
  Port as an empty line.

One notice defect came out of the story run and stays open. The page writes its own all-power-lost line again on every
status frame while power stays lost, so a Port with traffic repeats that warning in each open page of that Port. In one
run a second console joining wrote two more copies, a grant one more, and a leave one more, while the restore line
stayed at one copy. [Issue #112](https://github.com/OpenMux/openmux-server/issues/112) carries it.


### CFG — configure, then reload (`test_config_editor.py`, 20 scenarios)

Each scenario builds its own server, edits its own config, and proves its own save-then-reload-then-see order. Four
owners sit behind one page, so each scenario names the pair a person would otherwise reconcile by hand: access and
CSRF come from `web_plugins/config_editor.py`, what a save writes comes from that plugin's `/apply` and `/validate`,
what a save *starts* comes from `main.py` (`reload_adapters_soft()` reconciles in place, `reload_adapters_full()`
recreates every adapter and restarts the web console after the reply), and what an open page learns without a reload
comes from `layout.js` (`pdus_changed`) and `console.js` (`reload_applied`) over the socket that page already holds.

Two parts of these worlds are simulations. The `dummy` PDU driver answers at once and needs no device, so a PDU that
appears in the file always starts. The two adapter failures this tier can produce on demand are a Serial port whose
device path no host has, which the reload starts offline and the port reports itself down, and one port entry holding a
value its own port refuses, which the server reports instead of starting: for one Serial entry at soft reload
(`CFG-18`), for one entry of any port section at boot (`CFG-19`), and for that entry again when a full reload rebuilds
every adapter from the file (`CFG-20`).

| Id | What one scenario proves |
| --- | --- |
| `CFG-01` | Only an administrator opens the Editor; an operator and a reader are refused each route and see no menu entry. |
| `CFG-02` | Every sub-page heads itself with its own name, and an unknown or mis-cased view falls back to the Server page. |
| `CFG-03` | The page reports the config the process runs on with every stored secret masked, and the file keeps a hash that still logs in. |
| `CFG-04` | A new row that omits a required field is refused inside the panel, names the field, and never reaches the file. |
| `CFG-05` | A saved port starts on the soft reconcile, an attached console keeps its Port, and the next reconcile calls both unchanged. |
| `CFG-06` | One field the schema rejects stops the whole save and names itself, and the check button answers the same way without writing. |
| `CFG-07` | A section the security policy closed is refused by name and the file does not move; opening that section writes the same edit. |
| `CFG-08` | A section the policy closed arrives on the page disabled, with the reason beside it. |
| `CFG-09` | A save keeps the stored hash behind the mask and keeps a key the page never renders. |
| `CFG-10` | A save that carries a session cookie must also carry the CSRF token; the same body with the token is written. |
| `CFG-11` | A PDU the editor saved appears in the Power menu of a page that was already open, with no reload. |
| `CFG-12` | Feeds given to a running console light the badge of that console's open page, and its terminal is never dropped. |
| `CFG-13` | A Telnet listener the editor saved answers after a soft reload, and the console on the first listener keeps its Port. |
| `CFG-14` | The Reload view leaves the summary a soft reload gave on the page, and asks in words before a full one. |
| `CFG-15` | A full reload recreates every adapter, restarts the web console, and keeps an open browser page signed in. |
| `CFG-16` | A full reload ends an attached console with no message, and the next client that signs in is served. |
| `CFG-17` | A Serial port whose device is missing is listed, named once in the log, and still takes a console — read-only. |
| `CFG-18` | A Serial entry the server cannot build is named in the log block, reported as `failed`, never as `added`, and forgotten once the file drops it. |
| `CFG-19` | One unusable port entry at boot costs that port alone: the port beside it runs and the block names both. |
| `CFG-20` | A full reload answers ok, keeps every port the file can build, and names the entry it refused in its answer and its log. |

Unlike the plan for this suite, **no scenario depends on another**. Each builds its server and makes its own edit, so
a failure names the rule it broke instead of the scenario that ran before it. The save-then-reload order is a product
rule, so it lives inside the scenario that claims it rather than in the order of the file.

Six measured behaviours are recorded here, because a reader would otherwise assume the tidier version.

- A save alone starts nothing, and the soft reconcile reports what it started under the **adapter kind**: `loopback`,
  `serial`, `telnet_listener`, `client_listener`, `web_console`. A soft answer carries no `errors` key at all; only a
  full answer does. `CFG-05`, `CFG-17` and `CFG-18` read those keys.
- A feeds-only edit is reported as `unchanged` for that port, not `updated`: the reconcile diffs adapters, and a console's
  `power:` feeds are rewired outside that diff. `CFG-12` therefore pins the measured verdict and proves the feed through
  `/api/power` (`outlets[].mapped_ports`) and the live badge, which is what a person sees.
- A save that changes nothing still rewrites the file, with normalised layout. `CFG-07` therefore compares the two
  readings by value, because a byte comparison would report a change that is not one.
  [Issue #82](https://github.com/OpenMux/openmux-server/issues/82) tracks that layout churn, and the Editor writing
  each column's default into every entry it saves; nothing here depends on how that is answered.
- The Editor page and its JSON routes ask for different proofs. The routes accept a password; the page is written for a
  browser and sends a sessionless visitor to the login page. One early measurement also showed a bare request for the
  page answered differently before and after another authenticated request on the same client, so the suite asserts the
  stable pair — session cookie in, password on the routes — and states nothing about a bare first request.
- A full reload keeps the browsers that were signed in. The reload carries the login sessions to the console that
  replaces the one it stopped, so the operator who pressed **Full reload** is not sent to a login form. `CFG-15` reads
  the pair that proves both halves of that sentence: the open page's own `GET /api/status` answers 200, while a visitor
  with no session answers 401. A carry that also admitted a stranger would look exactly like the first half.
- A port with one Write seat hands it to the first console that asks. A second console on that Port attaches read-only,
  which is why `CFG-13` drives the new listener's console on the Port the same save added rather than on the busy one.
  A Serial port whose device is missing keeps its console page in `read-only` and reports `port_up: false` on the page's
  own state; no page banner calls it offline. `CFG-17` asserts the state it can see.

One product question recorded here has since been answered, and `CFG-18` asserts the answer (issue #105). A Serial entry
the operator wrote by hand with a `parity` the schema rejects leaves no port object at all: plugging a device in cannot
bring that port up, so a status page slot for it would only offer a console that cannot open. The soft reload answers
`200` and names that port under `failed`, with the adapter's own sentence in `failed_detail` and `kept_previous: false`
to say no older config stands in its place. The name never appears under `added`, on that reload or on the next one, and
`GET /api/status` lists no such port: earlier builds claimed the add on every reload and never created the port. The
refused name is dropped from every list that would claim it, and the adapter snapshots what is running, so the next
reload compares against the running config and repeats the same verdict. The server prints one warning block naming the
file and each unapplied item with its reason, and every line of that block starts with `CONFIG_GAP`, so a person who
greps the log gets the whole block. The tolerated schema problem reaches the caller as well: the answer carries
`config_violations`. `security.yaml` keeps `serial_ports` writable, so hand editing still reaches that state without the
page doing anything wrong: `POST /config-editor/apply` runs the same schema and refuses the entry with `400`
(`CFG-06`). While the file keeps that entry listed, the verdict repeats on every reload. Once the file drops that entry,
the next reload reports nothing, because nothing in the file is then unapplied.

Five defects in the product came out of measuring, and each is fixed with the scenario that proves it.

- A full reload while any browser page was connected left the web console down for good. `WebConsoleAdapter.stop()`
  asked each runner to `cleanup()`, which waits for the handlers that runner started, and the console and `/ws/events`
  handlers return only when their socket closes. With a page connected the wait never ended, so the old listener kept
  the port and the restarted console could not bind: the last line in the log was `Stopping WebConsole`, and nothing
  answered on that port again — measured four times in a row, 39 s and longer, never recovered. `stop()` now closes
  the sockets it holds, through `_close_live_sockets()` and `SiteEventHub.close_all()`, before it waits for anything.
  Measured after the fix: `Stopped WebConsole 'web_console' in 1.502s`, `Started WebConsole in 0.002s`, and
  `GET /healthz` answers 200 about 2.5 s after the reload. No `asyncio.wait_for` bound was added around the cleanup,
  because aiohttp's cleanup swallows `CancelledError` and the existing 10 s timeout never fired. `CFG-15` keeps a page
  open across a full reload, and it fails against the previous file: run with the old `stop()`, it reports that the
  log line `[deferred] Stopped WebConsole` never appeared, and it passes again with the fix in place.
- A full reload signed out every browser that was open, including the one that asked for it. The session store lives in
  the adapter instance (`WebConsoleAdapter.__init__` sets `self._sessions = {}`) and the recreated adapter starts with
  an empty one, so every cookie on the server named a session that no longer existed. Measured before the fix: the open
  page's own `GET /api/status/fragments` answered 401 while `GET /healthz` answered 200, `layout.js` stopped
  reconnecting and wrote `↻ Signed out – click to sign in again` over content that only looked live, and that badge's own
  click landed on `/login?next=/`. The reload now hands the sessions over: `login_sessions_snapshot()` and
  `adopt_login_sessions()` on the adapter, read before the stop phase by `OpenMuxServer._capture_login_sessions()` and
  passed while dependencies are wired in `_reload_full_wire()`. Only the identity moves, because every request asks
  `auth_manager` for its permission (`_get_effective_permission`), so an account the same reload removed or demoted is
  refused on its next click. An entry is not carried when its cookie name came from another `base_path`, when it lacks
  a username, or when `session_ttl_seconds` already passed. Measured after the fix: `Carried 1 login session(s) across
  the reload`, the page's own request answers 200, and the badge stays `● Live data`. `CFG-15` asserts the carry and the
  refusal of a sessionless visitor; `tests/test_session_carry_on_full_reload.py` holds the unit half, including the
  removed account and a reload that follows a reload. [Issue #104](https://github.com/OpenMux/openmux-server/issues/104).
- A port entry the adapter refused was claimed as started, and one such entry disabled every port of its kind. Measured
  before the fix: `GET /config-editor/reload/soft` answered `200` with that port under `serial.added` and no `errors`
  key while `GET /api/status` listed no such port, the same claim repeated on every later reload, and at startup one bad
  entry ended `SerialAdapter.__init__`, so the log said `No ports configured for serial adapter serial_ports` and the
  ports that were valid in the same file never started. The reconcile now builds each candidate before it destroys
  anything (`prepare_reconcile_candidates()` on the base adapter), records the refusal (`record_config_gap()`), and
  leaves the refused name out of `added` and `updated` and out of the config snapshot (`reconcile_summary()`). Measured
  after the fix: `Serial adapter serial_ports rejects port typo1: Parity must be N, E, O, M, or S`, then
  `reconcile summary: +0 ~0 -0 =unchanged 1 refused 1`, and the answer carries
  `failed: ['typo1']` with `failed_detail` naming the error and `kept_previous: false`. `OpenMuxServer.report_config_gaps()`
  turns the same facts into one `CONFIG_GAP` block in the log at boot, soft reload, and full reload. `CFG-18` asserts all
  of it against a running server, and `tests/test_config_gaps.py` plus `tests/server/test_serial_config_gaps.py`,
  `tests/server/test_tcp_config_gaps.py`, `tests/server/test_command_loopback_config_gaps.py` and
  `tests/server/test_main_config_gaps.py` hold the unit half.
  Boot kept the same gap for the other three port sections, and was measured after that fix: one entry the adapter's
  whole-section `validate_config()` refused made the factory's section gate raise, so no adapter was created and no
  object existed to record a reason. Measured then, for `command_ports` with one entry missing `command` and
  `loopback_ports` with one entry holding `max_read_write_users: two`: `Failed to create adapter for plugin Command Ports:
  Invalid configuration for Command Ports` with its traceback, and no port of that section at all. The factory now builds
  that adapter anyway when the section is a list and drops only the entries the adapter's own `prepare_port()` refuses
  (`reject_unusable_entries()` on the base adapter); it re-raises the old skip when no entry explains the refusal, so a
  section error no entry caused still skips whole. Measured after that fix, same file: `command adapter command_ports
  rejects port badcap1 in the config file: Invalid max_read_write_users for port badcap1: 'two'`, `Created adapter for
  Command Ports without 1 unusable port entry(ies): badcap1`, `Command adapter command_ports started with 1 ports`, and
  one `CONFIG_GAP command/badcap1:` line inside the boot block beside the schema violation. `CFG-19` asserts that pair
  against a running server, and `tests/server/test_factory_config_gaps.py` holds the unit half, including the shapes that
  must keep skipping whole.
  A full reload is the third path a person takes with such a file, and `CFG-20` measures it end to end: the operator runs
  it when a soft reload did not apply what the file says. Measured on a live server whose `serial_ports` holds `live1`
  plus one entry with `parity: Q`: `[reload-full:52834dcc] Reload complete: stopped=4 created=5 started=4 errors=0`, and
  `POST /config-editor/reload/full` answers `ok: true` with that empty `errors` list, because the reported gap is what
  the operator asked for and must not read as a failed reload. `keep` and `live1` run after it and `typo1` does not, and
  the block carries `CONFIG_GAP   schema: ... @ serial_ports[1].parity: 'Q' is not one of ['N', 'E', 'O', 'M', 'S']`
  beside `CONFIG_GAP   serial/typo1: Parity must be N, E, O, M, or S`. That scenario guards the fix rather than describing
  it: run against the `serial.py` from before the fix above, it fails with `assert {'keep', 'live1'} <= {'keep'}`, because
  one entry with `parity: Q` took the good Serial port `live1` with it.
  Measured while that was written, the answer of the same reload carried no gap: it held no `adapters` key and no
  `config_violations` key, so a caller that reads the answer and not the log learned nothing about `typo1`. The soft answer
  already carried both. `OpenMuxServer.add_gap_keys_to_summary()` now writes them on both paths, merging into what the
  reconcile stored rather than overwriting it, and only when something stayed unapplied. Measured after that change:
  `CFG-20` reads `failed: ['typo1']` and the parity reason out of the answer of the full reload.
  [Issue #105](https://github.com/OpenMux/openmux-server/issues/105).
- A gap stayed reported after the operator deleted the entry that caused it. Measured with `parity: Q` in hand: with
  `typo1` removed from `serial_ports`, the next soft reload still logged
  `CONFIG_GAP   serial/typo1: Parity must be N, E, O, M, or S`, and the same name stayed in the adapter's `config_gaps`,
  because only a port that applied cleared a gap and nothing cleared a name the new section no longer listed. The block
  claims to list config file entries that did not fully apply, so a deleted entry made that claim wrong, and the answer keys
  above would have repeated the mistake to a caller. `prune_config_gaps()` on the base adapter drops each gap whose label
  the new section no longer lists, and every port adapter calls it with the entries it is about to reconcile. An entry the
  file keeps listing keeps its gap, because the refused verdict must repeat until the file is fixed. Measured after that
  change: the reload that follows the deletion answers with no `failed` key and logs no `CONFIG_GAP` line at all, and
  without those `prune_config_gaps()` calls `CFG-18` fails on that step. The unit half sits in
  `tests/server/test_command_loopback_config_gaps.py` and `tests/server/test_main_config_gaps.py`.
  [Issue #105](https://github.com/OpenMux/openmux-server/issues/105).
- A full reload that could not start an adapter printed its own bookkeeping instead of the failure. The reporter read
  every entry of a full reload summary's `errors` list as plain text, while `reload_adapters_full()` appends one dict per
  failure, named by the adapter it was stopping, wiring, or starting, or by the phase that failed. Measured before the
  fix, fed the dict `main.py` appends: `CONFIG_GAP   adapter: {'adapter': 'serial_ports', 'start_error': 'start returned
  False'}` — the line names no adapter and its reason is a Python dict. A timeout entry holds a number and no sentence,
  so it lost its reason the same way. The reporter now names the adapter or the phase and keeps the reason that code
  wrote, and words a timeout itself: measured after the fix, from a real `reload_adapters_full()` whose adapter raises,
  `CONFIG_GAP   telnet_listener: listener port in use`. Two unit tests had asserted shapes no code produces — one result
  per port under `{"status": "failed"}`, and an `errors` list of sentences — which is why this stayed visible only in
  green tests: a search over the tree for a producer of the per-port shape found the test that fed it and nothing else.
  Both now read the payloads that method builds, and `tests/server/test_main_config_gaps.py` drives the real method, so
  the reporter is checked against a summary the product made. No world in this tier can force an adapter start failure,
  which needs a second process holding the listener port, so that half stays a unit test: it asserts the summary as the
  producer writes it and the block line as the operator reads it. All three tests fail against the previous reporter —
  `('adapter', "{'adapter': 'serial_ports', 'start_timeout': 10.0}") != ('serial_ports', 'start timed out after 10.0s')`.

### ACT — Port Actions (`test_actions.py`, built: 11)

One world: the Loopback port `loop1` holds every grant and the command port `cat1` holds none, so a scenario can compare a
granted Port with an un-granted one in the same page. Each scenario grants the scripts it needs, read from the shipped examples
(`echo_probe`, `slow_noop`) and from fixtures under `tests/e2e/action_fixtures` — percent progress (`meter_probe`), one
operator prompt of each kind (`ask_probe`), and one prompt that expires unanswered (`ask_timeout`).
A Port Action spreads one run over nine regions of the page, and each region is hidden with an inline `display` instead of
being removed, so every scenario reads `page.action_state()`, which pairs a region's text with whether it shows, and never
asserts that a node exists. Where two pages watch one run, each rule is asserted on both sides of it: an answer, an expiry,
or a Close run reaches every page, and a ✕ dismissal reaches one page only.

| Id | Proof |
| --- | --- |
| `ACT-01` | The Actions button shows on a granted port and hides on an un-granted one, a read-only account is answered 403 and gets no button, and the roster matches the API in the declared order and wording. |
| `ACT-02` | The run panel's own form, the answer typed into it, the finished tag, the outcome banner, the transcript lines, and the history row a finished run leaves: one run, its username, `success`. |
| `ACT-03` | A run a page launches takes the port's read-write seat from that page, names itself as the holder in the holders list, and gives the seat back in the server's own sentence when it ends. |
| `ACT-04` | A run launched against a port a human holds fails instead of displacing them, a second launch while one runs is refused by run id, a second page shows the `(running)` marker, and the refusal clears when the first run retires. |
| `ACT-05` | The bar shows each step the script reports, with its percent and its width, and one step whose label stands alone with no width. The bar is gone once the outcome banner arrives. |
| `ACT-06` | One run moves the operator box through text, buttons, dropdown, and radio, and each answer reaches the port and the run's transcript. |
| `ACT-07` | A second viewer takes over as operator and the launcher is told in the page's own words; each answer closes the box on the page that did not answer it, and both pages end with four closings and one finish. |
| `ACT-08` | `?action=…&autorun=1` names what it starts and starts it, and the parameters the link carries are the values the run uses. |
| `ACT-09` | An `autorun` link whose required answer is missing opens the prefilled form, says nothing, starts nothing, and starts the run once the operator types that answer. |
| `ACT-10` | Stop asks first and then records `cancelled`; the ✕ retires the pane on its own page and its own strip brings the run back; Close run drops the run on every page and in the catalog. |
| `ACT-11` | An operator prompt its own timeout closes leaves no box on any open page and none for a viewer that arrives after it, and the run nobody answered still finishes by itself. |

Two page defects came out of these ids and are fixed in `console.js`, each with the entry `CHANGELOG.md` carries. An
`autorun` deep link sent its run request as soon as its two action requests came back, which is often before the page's
console WebSocket reports the page's own `client_id`; the run request carries that id so the server can take the
read-write seat from that page, so the launch was refused with "read-write slot is unavailable". And the catalog read that
the same overlay open starts rebuilt the run form a moment after the link filled it, so the run started on the parameter
defaults instead of the values the link carried.

### ST — status pages (`test_status_pages.py`, 6 built)

The status page renders once and then keeps itself current. Four **Status fragments** (`summary`, `ports`, `federation`,
`clients`) are swapped in place from `GET /api/status/fragments` when the **Site event hub** carries a **Status poke**.
`status.js` owns the swap and which detail rows stay open, `web_console.py` owns the sort links, the fragment markers,
and the client list, and `layout.js` owns the socket and the **Live-data badge**. Each scenario moves real state — a
console attaches, an outlet falls — and reads the page after the swap, so one read proves the state and the swap
together. Where two pages watch one change, both are read, and what belongs to one page stays with that page.

| Id | What it proves |
| --- | --- |
| `ST-01` | The four cards carry the counts `/api/status` reports. Six headers are sort links whose link names the next direction and whose arrow names the current one. Following one reorders the table by the Clients count. The chosen order survives a swap that replaces the whole table. A column name the server does not know is answered as `name` ascending. |
| `ST-02` | A detail row one operator opened is still open afterwards, and its control still reads `-`, after a swap that rebuilt the table under them. A second page keeps its own row open instead of borrowing the first page's. A row an operator closed stays closed across the next swap. |
| `ST-03` | Three consoles on one port over three transports appear as three rows, each with the user, the port, the peer address, and the transport that console really used, agreeing with the port's own holders list and with `/api/status`. Each signed-in page adds its own session row with no port. A read-only operator reads the same rows. The port row labels each console with its transport. |
| `ST-04` | With no multipath peer, `/api/status` reports no group and no connection, the region says `No federation connections` and shows no peer card, and both federation cards read 0. The `federation` fragment carries the same sentence, and still says it after a console joins a port. |
| `ST-05` | Both pages start with a green **Live-data badge** carrying its exact sentence. The fed port's **Power dot** reads healthy and its tooltip names the feed; the unfed port shows `—`. One outlet switched through REST tints that dot on both pages to `0/1 - ALL POWER LOST` while both sockets stay live, and switching back restores it. |
| `ST-06` | The endpoint answers with exactly the four regions under one `fragments` key. No fragment carries its own container id, and the `ports` fragment carries no sort header. The sort query is honoured in both directions. A signed-in cookie answers for the fetch the page itself makes, while an anonymous caller gets `401` with a challenge and no port name. After a swap the open page shows the rows and the clients the endpoint serves for the same query. |

Three boundaries keep this group from wider claims. Federation proves itself here only as the empty state, because this
suite runs one node; the two-node behaviour lives in the `MXC` suite. `ConsoleSession.set_offline()` blocks
fetches but leaves an open WebSocket running, so the stale red badge is out of reach for a page scenario and stays with
`tests/test_web_console_live_badge.py`; `ST-05` proves the green half instead, and a full reload cannot catch the red
either because the one-second debounce races the first reconnect. And `AUTH-01` owns the anonymous refusal of
`/status`, so `ST-06` covers only the fragment endpoint the page calls.

One product defect came out of building this group. The Clients table named every Telnet console a websocket and left
its address blank, because `web_console.py` matched a client id against its own rules instead of asking the adapter that
accepted the connection, and `TelnetListenerAdapter._resolve_client_meta()` already answered with the transport and the
peer address. `ST-03` proves a Telnet console is now listed as `telnet` with its address on an administrator's page and a
read-only operator's page, and that the port row labels it the same way. The template no longer writes a transport it was
never given: a client whose transport the server could not name reads `unknown`. Each half carries a unit guard in
`tests/server/test_web_console_gather_clients.py` that fails without the fix.

### SSH — encrypted console (`test_ssh_listener.py`, 10)

`asyncssh` terminates this surface, and `openmux/server/adapters/ssh_listener.py` decides who gets in, which Port a
session lands on, and what that session may type. The unit tests for that adapter pass a fake console manager and a
fake process object, so no unit test completes a handshake, offers a host key, or answers a `Port: ` prompt. Every
scenario here dials a running server with the library a person uses, and reads the bytes back.

Five measured facts shape these bodies, and each is used where it applies:

- The adapter generates one Ed25519 host key per state directory, logs `Generated new SSH host key at <path>` once,
  and reuses that file on a restart of the same state directory.
- A refused **login** shows up on the wire only: the client reads `Permission denied for user <name>`, and a wrong
  password or an unknown account added no product log line of its own.
- A refused **attach** is the opposite: the listener writes the line `access_decision` renders onto the channel,
  closes it with status 1, and leaves two warnings naming the code it used.
- A command request is answered with `Only interactive sessions are supported.` on stderr and status 1, and the shell
  on the same connection keeps working.
- The escape prefix here is two bytes, which the session-info block reports as `Escape sequence: b'\x05'+b'c'`.

| Id | What it proves |
| --- | --- |
| `SSH-01` | The host key is generated inside the state dir with mode 0600, the handshake offers exactly that key, and the home directory's own key file is untouched. A restart of the same state dir offers the same bytes with no second generation line, and the client still gets in. |
| `SSH-02` | A password the config accepts opens a shell, and the account behind it decides the seat: `rw` comes up read-write, `ro` comes up read-only and reads what the writer types on the same Port. A wrong password and an unknown account are both refused before a prompt. |
| `SSH-03` | A key registered with `allowed_uses: [ssh]` opens a session for its own account. A key the config never names, a key registered for `client` use only, and a valid key offered for another account are each refused. |
| `SSH-04` | With `require_auth: false` the listener asks for no credential and prints the start-up warning that promises an attach. Every attach is refused, on the channel and in the log, for `rw` and for a made-up name alike. |
| `SSH-05` | A `acl` that omits the client's address closes the socket before any prompt, and the log names the listener, the address, and `(ACL)`. The listener on the next port, which lists no `acl`, admits the same client to a read-write seat. |
| `SSH-06` | The shell is the only thing this listener serves. `echo hi` on the same connection comes back with status 1, no stdout, and `Only interactive sessions are supported.`, and the shell still takes what the session types next. |
| `SSH-07` | A CLI Takeover takes a seat an SSH session holds. The holder is told in band, in the sentence the shared module owns, drops to read-only, and both logs carry the event: `WRITE-SLOT TAKEOVER port=slot1 taker=admin: ... victim=rw: ...` and the Port's `write_slot_takeover` record. |
| `SSH-08` | The Control menu's `p` key lists this console's two feeds over SSH, one answer switches one outlet and says the Port stays up via the other, the attached session reads `[POWER] feed rack1.1 is now off`, and the audit line names `user admin`, `staying up: feed1`, and `client ssh:ssh1:`. |
| `SSH-09` | `user+port` and `user:port` land on that Port with no `Port: ` prompt in between, an unknown Port is named back as `Port nosuch unavailable` and the channel closes with status 1, and a plain username still gets the Port menu. |
| `SSH-10` | A Port whose own group lists exclude the account is still listed in the Port menu, then answers the name on the channel with the refusal `access_decision` renders, closes with status 1, and logs `denied_by_group_acl` twice: once from the ladder, once from the listener. |

Three boundaries keep this group from wider claims. The harness dials with `known_hosts=None`, because the server's key
is generated per run: `SSH-01` therefore reads the offered key out of the handshake instead of proving that a client
rejects a key it has not seen. A subsystem request shares one branch with a command request in
`ssh_listener.py:534`, and `asyncssh` offers no call for a subsystem shell on this path, so `SSH-06` proves the exec
half only. And no scenario here proves a serial or `tcp_initiator` Port over SSH, because the Port behind the session
is the same object every transport reaches.

Issue [#110](https://github.com/OpenMux/openmux-server/issues/110) came out of `SSH-04` and is open: a listener that
offers the Port menu with `require_auth: false` warns that any client can attach, then refuses every attach. With no
login the adapter has
no account to carry the session, so it names the client after the listener (`ssh_<name>`), and the access ladder grants
that identity nothing. The username a client offers is never consulted. `SSH-04` asserts that measured behaviour, so
the scenario fails when the fix lands and the claim can be rewritten.

The harness gained the SSH half of itself for this group: `ConfigBuilder.with_ssh_listener` and
`ConfigBuilder.with_public_key`, the module function `ssh_ed25519_keypair`, `E2EServer.restart`, the `ssh` and
`ssh_kwargs` knobs on `servers()` with `telnet=False` to leave one listener answering, and `clients.ssh()` over the new
`SshSession` in `textclients.py`. `SshSession` runs its own event loop on a daemon thread, like the other text
sessions, and pumps the channel into a queue. One harness defect surfaced while building `SSH-10`: `text()` returned a
cached transcript once the channel end arrived, so the refusal the listener had already sent stayed in the queue and the
scenario read a transcript that ended at `Port: `. That read looked exactly like a listener that refuses in silence,
and one wrong claim reached a scenario body before the queue was drained by hand. `text()` now always drains.
`E2EServer.restart()` waits for the world's ports to become bindable before it starts the second process: a politely
closed SSH connection leaves a socket in TIME_WAIT, macOS then refuses the rebind, and a listener that cannot bind is
logged dead and left stopped while the rest of the server keeps answering `/healthz` with 200 — issue
[#115](https://github.com/OpenMux/openmux-server/issues/115) carries that, and no scenario asserts it yet.

### CMD — command-driver PDU (`test_power_command.py`, 6)

`driver: command` reads and switches a power device by running a program: one batch read per PDU and one command per
switch. `tests/test_power_command_driver.py` covers that code with `asyncio.create_subprocess_exec` monkeypatched, so no
unit test starts a process. Each scenario here starts one: the device is the checked-in outlet box
`tests/e2e/power_fixtures/outlet_box.py`, a script that keeps its outlet positions in a JSON file. That file is what
proves the argv the driver built, because the script writes what it was told to move.

Six measured facts shape these bodies, and each is used where it applies:

- The device decides what the server reports. A fresh box seeds every outlet off, so the first read reports `on: false`
  and leaves `watts`, `amps`, `volts` and `total_watts` null. The `dummy` driver starts every outlet on and reports
  120 W at 230 V, so `test_power.py` cannot show either half of that pair.
- A listed feed carries no wattage when the device reports none: `POWER box.1 off  -> con1`, where the dummy row reads
  `POWER rack1.1 on 120W 230V  -> solo, dual`.
- A successful switch logs nothing of its own. The only server-side trace is the audit line `POWER CONTROL: user <name>
  turned <ref> off; losing all power: <port>`, so a scenario waits for that line and never for a driver line.
- A notice reaches a console only when its feed changes. Switching an outlet that already reports that state changes
  nothing and says nothing, so a scenario sets the starting position first.
- A refused switch reports the device's exit code and its own words on every surface: `power switch failed (exit 3)
  (stderr: outlet box: relay refused to move)` in the CLI answer, the same text inside `set_state failed for <ref>` in
  the log, and the same text in a REST 409 that still carries the impact.
- A device-wide read failure is silent by contract (`power_drivers/readbackoff.py`): no further device IO, the last
  readings kept, `online` still true, and no power log line. A switch is never backed off, so one switch still reaches
  the device and the report follows it.

| Id | What it proves |
| --- | --- |
| `CMD-01` | The config runs the checked-in device, the first read reports the device's own positions, no wattage is invented for it, and the device file agrees. |
| `CMD-02` | One `POWER` line spawns the configured command with the substituted outlet id, the device records it, the console is told, and the audit line names the console that lost power. |
| `CMD-03` | A change thrown on the device itself reaches the console and `/api/power` on the next poll, while the device records no switch because none was asked for. |
| `CMD-04` | A relay that refuses to move answers with its exit code and stderr in the CLI, in the product log, and in a REST 409 that keeps the impact, and the device keeps its position. |
| `CMD-05` | A command past its timeout answers `power switch timed out after 1s` on both surfaces, the helper it started is stopped, and the device wrote nothing. |
| `CMD-06` | A device that stops answering leaves the last reading in place while the device holds another, says nothing in the log or on the console, and still accepts a switch. |

The world knob is `ConfigBuilder.with_command_pdu(name, outlets, poll_interval=…, box_args=…, timeout=…)`, and the
scenario reads the device through `world.power_box(name)`, `world.power_box_journal(name)`, `world.power_box_child(name)`,
`world.power_box_flip(name, outlet)` and `world.power_box_fault(name, *modes)`. `box_args` bakes a fault into the argv at
start-up; `power_box_fault` moves the device while the server runs, which is the case a poll or a backoff window is about.

### SER — serial device over a pty pair (`test_serial_device.py`, 6)

The Serial adapter reads and drives a character device. Its unit tests under `tests/server/` replace
`serial_asyncio.open_serial_connection` or the port object, so no unit test opens a device and none can say what
the device sees. Each scenario here opens one: `SerialStation` runs `socat` over a pty pair, the server opens one
link as its `device`, and the test holds the other. The tier therefore proves the bytes on the wire, the termios on
the device, and what an operator is told when the cable comes out. The tier needs `socat`; each scenario of the
file states that reason and skips when the host has none.

Six measured facts shape these bodies, and each is used where it applies:

- Nothing is added and nothing is rewritten. A console that types `boot\x1b[A\x03 … end\r` is read by the device as
  those same bytes, UTF-8 included, and the console is shown nothing of its own typing: a Serial Port forwards, it
  does not echo.
- The line settings land on the device. Before the server opens the link, the pty carries `socat`'s own line: 9600
  baud, no parity, one stop bit, `ONLCR` set. After the server opens it with `baudrate: 57600`, `parity: "E"` and
  `stopbits: 2`, the device carries that, with `CLOCAL` raised and `ONLCR` cleared.
- An absent device never stops the server. The Port is listed with `state` still `active` and `connected` false, and
  its `status_message` reads `Serial device <path> not found` beside the one logged warning `Serial device <path>
  does not exist`. A console may still attach, and its typing is answered with `[Write blocked: port is disconnected
  on server]`.
- The Port recovers by itself. It retries every five seconds (`SerialPortWrapper.reconnect_delay`), so a device that
  arrives, or a cable that goes back in, needs no reload. A seat granted while the device was down carries across
  the connect, and the console is told `[Reconnected]`.
- A device pulled mid-session says so on every surface: the console gets `[Port disconnected on server]`, the API
  carries `Read error on <path>: device reports readiness to read but returned no data`, and the log adds `Port
  <name> is not connected` plus `WRITE FAILED` for the next write.
- Bytes are neither lost nor reordered. 4,096 bytes of every value arrive at the console whole and in order, and
  2,000 bytes typed in one action arrive at the device whole, where the reads come up as three chunks.

| Id | What it proves |
| --- | --- |
| `SER-01` | The device reads the control characters and the non-ASCII bytes exactly as typed, the console is shown none of its own typing, and what the device sends arrives unchanged. |
| `SER-02` | The speed, parity, and stop bits of `server.yaml` are on the device's own termios, and opening the line clears output post-processing. |
| `SER-03` | A Port whose device no host has states the reason in `/api/ports` and the log, lets a console attach and tells it where its typing went, then connects on its own when the device arrives. |
| `SER-04` | Pulling a live device is reported to the attached console, in `status_message`, and in the log, the refused write is recorded, and the same console works again after the cable goes back in. |
| `SER-05` | One block of every byte value crosses the server whole and in order in both directions, in either direction's own chunking. |
| `SER-06` | `flow_control` reaches the device: the RTS flag or the in-band flags are set in its termios, the default sets neither, and a line under the hardware handshake still carries characters. |

The world knob is `ConfigBuilder.with_serial_port(name, device, baudrate=…, parity=…, stopbits=…, flow_control=…)`,
and `devices.serial(tag, plugged_in=True)` hands a scenario its device: `path` is what the config names, `send` and
`received` are the wire, `line_settings` reads the device's own termios, and `unplug` plus `plug_back_in` are the
cable. `SER-06` found the one product defect of this group and it is fixed on its own: the adapter used to leave
`flow_control` out of the call that opened the device, so no handshake was ever applied.

### Story and timeline recorder (`test_story.py`, 6 built)

Built: `TIM-01` to `TIM-05` prove what one timeline line looks like: caret notation for control bytes, `IAC WILL ECHO`
named, a truncated command marked rather than guessed, one record per line, and a long payload kept whole beside the
file. Those five read the recorder only, start no server, and open no browser. `TIM-06` is the sequence the recorder
exists for, and the only scenario in this file that carries `e2e_browser`.

| Id | Claim |
| --- | --- |
| `TIM-06` | One operator's shift in one process: a PDU added in the Config Editor, the reload that page runs by itself, an open console gaining the feed it waited for, a Write slot taken by a second console, the last feed lost and restored while both terminals watch, and the audit lines the shift leaves behind. |

Measured boundaries of `TIM-06`:

- `Save item` changes the editor's own page only: the PDU reaches the server when the page's Save button posts the
  draft. Step 3 asserts that a draft is page state and not server state. Pressed straight after `Save item`, that
  button still posts the draft: the page sent `POST /config-editor/apply` and reported
  `Saved; Soft reload completed` inside 6 s.
- The PDU is switched through REST, because the same switch made from a console session adds `; client <id>` to the
  audit line, and `PWR-19` covers that half.
- A refusal to promote names its holder on the reply frame (`rw_holders`) and only its own sentence on the terminal, so
  the scenario reads the holder from the frame and quotes the sentence from `access_decision`.
- One switch writes two sentences in the web terminal: the server's notice, which every attached surface reads alike,
  and the page's own all-power-lost line. The page re-writes that second line on every status frame while power stays
  lost -- a second console joining the same Port wrote two more copies of it, a grant one more, and a leave one more,
  in one run. The restore line stays at one copy. [Issue #112](https://github.com/OpenMux/openmux-server/issues/112)
  carries that defect. The scenario reads each sentence once, and `RW-04` records this refusal wording as intended
  until [Issue #113](https://github.com/OpenMux/openmux-server/issues/113) settles it.
- The holders are labelled `user@peer`, so two sessions of one account from one address cannot be told apart there.
  `RW-17` reports that label.

### SOK — soak: many viewers, simultaneous requests, and socket churn (`test_soak.py`, 7)

One Port, many sessions, and the server measured from outside. The unit tests under `tests/server/` hand the fan-out a
client that never blocks, so they cannot say what a slow reader, an abandoned socket, or two hundred parallel requests do
to a running process. Each scenario here opens real sessions, then reads numbers the server itself gives: the Port entry in
`/api/status`, and the descriptors `lsof -n -P` reports for the server process. No dependency is added for the second one.

| Id | Claim |
| --- | --- |
| `SOK-01` | Twelve read-only Telnet viewers watch one Port while a writer sends two hundred marked lines. Every viewer holds every marker, and none falls more than 400 bytes behind the busiest. |
| `SOK-02` | One of four viewers stops reading, as a wedged tab does. Twelve hundred padded lines follow the stall. No marker is missing for a viewer that kept reading, and the run prints the Port's `dropped_chunks` beside each reader's missing count. |
| `SOK-03` | Two hundred unanswered requests over five paths arrive from sixteen threads while twenty signed `/api/status` calls run in the same pool and a poller asks the status endpoint on its own thread. Every answer carries the status and length it had before the load, and a line written during the burst reaches every viewer. |
| `SOK-04` | Twenty sessions log in on Telnet and do nothing, and twelve bare connections send not one byte. A fresh session still gets a seat and the stream beside them. After the closes and the resets, the process holds what it held before. |
| `SOK-05` | Nine sessions arrive and leave in a row: one closes cleanly, one resets mid-stream, one never sends a byte. The stream loses no marker, and the Port lists only the two sessions that outlive the rounds. |
| `SOK-06` | A heartbeat writes a marked line every 0.2 s while three viewers arrive and go, on Telnet and on the console websocket, abandoned on the odd round and closed on the even one. The writer's own transcript holds every round marker, no left session keeps a seat, and a viewer that arrives after the run is served. |
| `SOK-07` | One console websocket attaches and then reads nothing while a writer pushes 15.6 MB through its Port. The kernel holds 143 KiB of that against the server, the Port drops the rest for that one console, and a viewer-set notice for a console arriving during the flood still reaches a watching console in 0.110 s against 0.107 s with nobody stalled. The stalled console keeps its seat and catches up on the 1.0 MB that survived when it reads again. |

Seven measured boundaries shape these bodies:

- The Write slot is one seat, not a viewer limit. A console websocket that asks for read-write takes it: with such a
  viewer attached, the writer's line never reached the Port, and no data frame arrived within two seconds. The same viewer
  as `ro` takes no seat and receives the writer's line, and the writer keeps the seat.
- A viewer receives only what the Port writes after it attaches. Two bodies waited for a marker the writer had sent before
  the attach, and reported a loss that never happened. Each now writes its marker after the attach.
- Every write is confirmed against the Port echo before the next one. The first `SOK-01` measured 57 of two hundred markers
  missing for all twelve viewers *and* for the writer's own echo, which places that loss before the per-client fan-out.
  Confirmation paces the writer and fails at the writer. It does not fix that batch loss, so the tier no longer exercises
  an un-paced bulk write and would not catch a regression in one.
  [Issue #116](https://github.com/OpenMux/openmux-server/issues/116) carries it, unproven on current `main`.
- `dropped_chunks` names the Port, not the reader. One counter covers two queues: the port-wide `data_queue` (100 chunks,
  `port_manager.py:59`), which feeds federation and `get_port_data` and nobody else drains, and one client's queue
  (100 chunks, `port_manager.py:682`). Each drop adds to the same number (`port_manager.py:1296`, `port_manager.py:1331`).
  Measured with one reader and no other viewer on the Port, a 300-line burst lost nothing for that reader and still
  reported 500, then 1102, then 1704 after three bursts. So a non-zero count says bytes were shed somewhere, and cannot
  say a named viewer lost bytes.
- A stalled reader was seen not to starve the others. With one viewer stalled and three reading, 1 200 marked lines and
  1 200 markers each, none of the three missed a marker and the stalled one missed them all. That is the Loopback Port
  with a small line rate; a slow real device is not tested here, and the product still has no per-viewer pacing.
- A console that reads nothing does not hold back a *control* delivery either, because the Port sheds its bytes before its
  socket can fill. `broadcast_control_frame_to_port` (`console_manager.py:1111`) delivers to one viewer at a time in attach
  order, so `SOK-07` stalls the console it attaches first and then floods that Port with 65 000-byte lines. One run measured
  15.6 MB written, `send_q=146988` on the server's side of the stalled socket (`netstat -an -p tcp`), `dropped_chunks`
  0 to 14745, and a viewer-set notice reaching a healthy console in 0.110 s against 0.107 s with nobody stalled. The queue
  stopped near 143 KiB, far under the 4 MiB the system allows, because `_process_data_queue` (`port.py:584`) discards that
  console's chunks once its queue is full. [Issue #106](https://github.com/OpenMux/openmux-server/issues/106) keeps the two
  questions this shape cannot answer: a peer that closes its socket mid-burst leaves no queue to shed into, and a stream
  slow enough to stay under the queue limit needs a device that really reads slowly, which no fixture provides yet — the
  Serial fixture is a `socat` pty pair that never throttles.
- Awaiting sessions hold no seat, and the descriptor check must count them fairly. The first baseline was taken before the
  scenario's own two surviving sessions opened, so those two sockets were read as a leak (measured 3 before, 5 after). The
  baseline now includes them, and the check allows one IPv4 and five KQUEUE entries of slack.

No product defect in this group is proven. Two measurements stayed unexplained, and both are on
[Issue #116](https://github.com/OpenMux/openmux-server/issues/116): the batch loss above, and a `dropped_chunks` count that
moves on a busy Port whose only reader received every line. The third open question, answered as far as this machine lets
it and kept open for the two shapes above, is [issue #106](https://github.com/OpenMux/openmux-server/issues/106).

### MXC — two linked nodes (`test_muxcon.py`, built: 9)

The only suite in the tier that starts two servers. One answers on a federation port the harness allocated and owns no
Port (`MXC-08` alone gives it a local Port too); the other owns `loop1` and dials it. Both state their filters, because
both directions of a MuxCon link default to deny-all
(issue #77) and a Port a filter drops is reported only at `DEBUG`. Four ids run that link in plaintext, which each side
declares in its config. `MXC-05` and `MXC-06` run it over TLS: the answering side autogenerates a self-signed certificate in
its own state dir, and the dialling side records the peer's fingerprint on first sight — and refuses that peer forever after
under a different one. `MXC-07` runs the key-authenticated handshake: one hub, one configured key, three diallers — the
key's true holder links (`AUTH:OK`, then its Port in the hub's registry), a different key carried under the configured
`key_id` is refused at the signature, and a keyless dialler never reaches the challenge, offering its Port list and being
dropped by the acceptor's auth gate. `MXC-08` gives a Port its node's name: the menu lists a federated Port as
`<origin>::<port>` and answering with that printed label attaches it, the same qualified line resolves on the CLI
wire, `local::` attaches this node's own Port, and both wrong claims — a node that does not exist, and a real node
that does not own a Port standing on this node — are refused on both surfaces. `MXC-09` crosses the link in the other
direction, with a switch typed into the viewing node's `p` menu: the ref names an outlet on the far node, so the request
travels anchored on the console that typed it, and the origin confirms that its own mirror of that console really drives
the Port the outlet powers before running the switch locally there; the owner's audit trail names a `fed:` pseudo-client
and its own local ref, and the state update travelling back powers the typing console out beneath its own
`[POWER WARNING]`, qualified ref included. The Config Editor's `muxcon` writes stay in Release 2 item 2.

`MXC-02` is the first scenario in the tier to read a peer Port from `api/ports`, and it found a defect on the way: the
composite id of a federated Port carried this node's id rather than its origin's, because the id was computed before the
origin was filled in. No node resolves such an id, and the interactive menu already printed the other label.
`MXC-05` found the second: the autogen certificate puts `server.id` in its X.509 CommonName, which RFC 5280 caps at 64
characters, so a node with a longer id logged `TLS autogen failed`, refused to start a plaintext listener, and simply had no
federation listener at all. The CommonName now ends at that ceiling.

| Id | What one scenario proves |
| --- | --- |
| `MXC-01` | Two nodes complete a handshake and each names the other in its own status API, on the federation and multipath pages. |
| `MXC-02` | A peer Port keeps its origin's name and identity, drives for a console the origin entitles, and stays a view for one it does not. |
| `MXC-03` | Every console on every node competes for the ONE Write slot the origin arbitrates, and a released slot belongs to nobody until a console asks for it. |
| `MXC-04` | A console refused across the link is told the reason in words on a listener surface, on a line that names no node and no holder. |
| `MXC-05` | A TLS link needs no shared CA: the answering side serves an autogenerated certificate, and the dialling side records the peer's fingerprint as the unverified first sight it is. |
| `MXC-06` | A peer that returns with a different certificate is refused, completes no handshake, and does not rewrite the fingerprint the node trusts. |
| `MXC-07` | One hub's configured key admits its true holder, refuses a different key carried under its `key_id` at the signature, and drops a keyless peer's Port list unheard. |
| `MXC-08` | The qualified Port name the menu prints reaches the node it names on menu and wire alike, and a qualifier naming a node that lacks the Port is refused though the bare name stands here. |
| `MXC-09` | An outlet switch typed on the viewing node moves the outlet on the owner, anchored on the typing console and audited there as its own relayed session. |

---

## Release 2

Named now so release 1 fixtures stay sufficient. All five have a suite. Item 2 is built as far as one link and one Port
carry, and its remaining surfaces are named in its entry.

1. SSH listener: **built** as `SSH-01` to `SSH-10`, ahead of the rest of this list. The host key inside
   `OPENMUX_STATE_DIR`, one Takeover, and one `p` menu run are each proven there, beside the login forms, the `acl`
   fence, and the command request. The control menu is shared with Telnet and already covered by `RW-10`.
2. MuxCon with two live nodes: **built as far as one link and one Port carry** as `MXC-01` to `MXC-09` — the link and what
   each side reports about it, the origin's identity on a peer Port, the ONE Write slot the origin arbitrates for every node,
   the refusal a blocked console reads, TLS from the first stored fingerprint to a refused re-key, the `PKID` handshake
   that admits only the peer holding the configured key, the qualified `CONNECT:<server>::<port>` menu syntax, and the
   `POWER:SWITCH` relay anchored on the console that types it.
   Still unproven there: the Config Editor's `muxcon` writes.
   See [muxcon.md](muxcon.md) §4.5 and 4.6.
3. Power `driver: command` driven by a checked-in fake device script: **built** as `CMD-01` to `CMD-06`. The device is
   `tests/e2e/power_fixtures/outlet_box.py`, which keeps its outlet positions in a file, so the tier proves the argv the
   driver builds and the exit code a user reads. The monkeypatched unit tests keep covering the parse and quoting rules.
4. Serial adapter over a `socat` pty pair: **built** as `SER-01` to `SER-06`. The device is a `socat` pty pair held by
   `SerialStation`, so the tier reads the bytes the device reads, reads the termios the server applied, and pulls the
   cable in the middle of a session. It found one product defect, and that defect is fixed: `flow_control` never
   reached the opened device.
5. Soak: many viewers, simultaneous requests, socket churn: **built** as `SOK-01` to `SOK-07` in `tests/e2e/test_soak.py`. The
   viewers are real sessions on the Telnet listener and the client listener, because a mock holds no socket and reads no
   stream. A churn scenario counts what the server process still holds with `lsof -n -P`, so the tier gains no dependency.
   Two facts the tier had to learn now stand where they bite: a Loopback Port limits only the Write seat, so a viewer that
   asks for read-write takes the one seat and leaves the writer without it; and a viewer receives only what the Port writes
   after it attaches, so a scenario waits for a marker it sent after the attach, never for one the Port wrote before. A
   third fact is an open question rather than a lesson: an un-paced bulk write lost the same markers for every reader in
   one run and not in another, so each scenario confirms its writes against the Port echo and
   [issue #116](https://github.com/OpenMux/openmux-server/issues/116) carries the rest. `SOK-07` adds the console that keeps
   its socket and reads nothing: one run measured a viewer-set notice reaching a healthy console in 0.110 s while 15.6 MB
   piled up behind the stalled one, and [issue #106](https://github.com/OpenMux/openmux-server/issues/106) keeps the two
   shapes a machine like this cannot produce — a peer that closes its socket mid-burst, and a device that reads slowly on
   purpose. A console that disappears mid-write, without holding a socket open, is still untested on purpose: the server
   fails silently there, so nothing can observe it.

---

## Open decisions

1. **`console.js` hook.** Resolved: shipped. The page exports `window.__omxE2E.view()` only when the URL carries `?e2e=1`,
   and `CON-02` fails if the gate is removed. The contract is the table in
   [`tests/e2e/README.md`](../../tests/e2e/README.md). A browser assertion can now prove the browser prints the server's
   own sentence, which is the point of ADR-0001.
2. **Scenario index in git.** Resolved: committed. `make e2e-doc` writes [`docs/E2E_TESTS.md`](../E2E_TESTS.md) and
   `IDX-02` fails when the committed file is older than the scenarios that exist.
3. **`scripts/smoke_workflow.py`.** Resolved: kept, with the duplication noted. [`docs/SMOKE_TEST.md`](../SMOKE_TEST.md)
   says which flows the tier now covers and what the script still adds: a by-hand check on a new machine, and an optional
   serial peer through `socat`.

---

## Build status

| Phase | State | What exists |
| --- | --- | --- |
| 0 — harness | Done | `harness.py`, `story.py`, `textclients.py`, `console.py`, `index.py`, `conftest.py`; markers and `--run-e2e`; `make test-e2e`, `test-e2e-live`, `test-e2e-debug`, `e2e-install`, `e2e-doc`. |
| 1 — lifecycle | Done | `LIF-01` to `LIF-11`, plus `IDX-01` and `IDX-02`. |
| 2 — browser proof | Done | `login()`, `ConsoleSession`, the page hook under `?e2e=1`, with Chromium installed by `make e2e-install`. |
| 3 — AUTH and CON | Done | `AUTH-01` to `AUTH-10` over HTTP with a real cookie jar, and `CON-01` to `CON-14` in a real Chromium page. `AUTH-11` came after Phase 9, once a helper fixture existed; see row 9a. |
| 4 — write slot | Done | `RW-01` to `RW-21` in `test_write_slot.py` over the three surfaces. The harness gained a per-port listener knob, a `groups` list per user, the two group lists per port, and `expect_no_text` on both text sessions. |
| 5 — power | Done | `PWR-01` to `PWR-19` in `test_power.py`: the REST shape, both PDU pages, the badge and its menu, the terminal notices on three surfaces, the `POWER` command, the Telnet `p` menu, the permission and group matrix, and the audit lines. Three product defects came out of it, listed under `PWR` above. The harness gained the page, dialog, and text-session helpers each one needed; see [`tests/e2e/README.md`](../../tests/e2e/README.md). |
| 6 — Config Editor | Done | `CFG-01` to `CFG-20` in `test_config_editor.py`: the gating and CSRF pair, what a save writes and refuses, the soft reconcile and the full reload seen from an open page, a Serial port with no device behind it, a Serial entry the adapter refuses to build at soft reload, one entry of any port section the adapter refuses at boot, and that entry again under a full reload. Five product defects came out of building this group, listed under `CFG` above; the third turned out to have a second half found after it was fixed, the fourth was found while measuring that half, and the fifth came from making a full reload answer carry the keys a soft reload answer already carried. The harness gained `ConfigBuilder.with_serial_port`, a Telnet `port` knob, `TelnetSession.wait_for_close`, and `ConsoleSession.wait_for_selector`; `CFG-18` edits `server.yaml` through `world.config_dir`, because a hand edit is the state under test and the Editor refuses it; `CFG-18` then deletes that entry with `_drop_port_named()` and counts the block lines after `_wait_for_log_count()`, because a plain log wait is satisfied by an earlier reload; `CFG-19` needs no harness change at all, because an unusable entry is written by `ConfigBuilder.with_command_port` with `write_slot_capacity="two"`, and `CFG-20` reuses the hand-edit helper `CFG-18` added. |
| 6a — uncovered-run notice | Done | A run that cannot start a browser skips every page scenario and, under the heading `E2E browser coverage`, prints how many scenarios asserted nothing, one line per reason, and the fix command. `IDX-03` drives the notice with a fake reason and a fake skip list; whether this machine has a browser is not what it checks. |
| 7 — Port Actions | Done | Eleven scenarios (`ACT-01` to `ACT-11`) in `tests/e2e/test_actions.py`, with three fixture scripts under `tests/e2e/action_fixtures` and an `action` block on the page hook. Two page defects came out of them: see the ACT section above. |
| 8 — Status pages | Done | Six scenarios (`ST-01` to `ST-06`) in `tests/e2e/test_status_pages.py`: the cards and the sort links against `/api/status`, a detail row that survives the swap that rebuilt it, one Clients table naming each transport across three surfaces, the federation empty state, one port's power dot following an outlet on two open pages, and the shape of `/api/status/fragments`. One product defect came out of them: the Clients table called every Telnet console a websocket. See the ST section above. The harness gained `ConsoleSession.is_visible()` and `ConsoleSession.live_badge()`; `set_offline()` is documented for what it really does. |
| 9 — Story | Done | Six scenarios in `tests/e2e/test_story.py`: the five recorder scenarios and `TIM-06`, one operator's shift in one process. It adds no harness method: the editor steps run through `ConsoleSession` helpers other suites already added, and the file gained four local helpers of its own for the inline editor row. `TIM-06` marks `e2e_browser` inside a file whose other scenarios need no browser; `AUTH-11` is the other scenario that does. |
| 9a — AUTH-11 | Done | `AUTH-11` in `tests/e2e/test_auth.py`: an `external_auth` helper decides who may sign in, and only `authentication.yaml` hands out the role. The harness gained `ConfigBuilder.with_external_auth`, and `tests/e2e/auth_fixtures/helper_stub.py` is the fixture — one script, four names, in the protocol `auth_manager.py` speaks. One product defect came out of building it: a helper that answers slowly stops the server for every other visitor, which issue #114 carries. The scenario asserts nothing about time, because a timing claim would read as flaky while that defect is open. |
| 10 — SSH listener | Done | Ten scenarios (`SSH-01` to `SSH-10`) in `tests/e2e/test_ssh_listener.py`, each dialing a running server with `asyncssh`: the host key generated in the state dir and reused across a restart, the account behind a password, the `ssh` use of a registered key, the listener that offers the Port menu without a login, the `acl` fence beside a listener without one, the command request that never reaches a Port, one Takeover taken from an SSH holder, the Control menu's `p` key over SSH, a username that carries a Port name, and a Port that excludes the account. Two product defects came out of this group: issue #110 from `SSH-04`, and issue #115 from the restart in `SSH-01`, where a listener refused a bind is left stopped while the server goes on answering `/healthz` with 200. The harness gained the SSH half of itself: `with_ssh_listener`, `with_public_key`, `ssh_ed25519_keypair`, `E2EServer.restart` (which waits for the ports to be bindable again), the `ssh` knobs on `servers()`, and `clients.ssh()` over the new `SshSession`. See the SSH section above. |
| 11 — Command-driver PDU | Done | Six scenarios (`CMD-01` to `CMD-06`) in `tests/e2e/test_power_command.py`, each driving the checked-in outlet box `tests/e2e/power_fixtures/outlet_box.py` through `driver: command`, so a real process answers every read and switch: the positions and missing wattages a real device reports, one CLI switch proven by the outlet id the box recorded, a position changed on the device found by the next poll, a refused switch quoting exit 3 and the box's own words on the CLI, in the log, and in a REST 409 that keeps its impact, a command past its timeout answered on two surfaces with the helper it started stopped, and a device that stops answering kept silent and stale while a switch still reaches it. No product defect came out of this group. The two surprises were documented behaviour, and each is now asserted where it applies: a switch that changes nothing sends no notice, and a device-wide read failure is silent by the contract in `power_drivers/readbackoff.py`. The harness gained `ConfigBuilder.with_command_pdu` plus `E2EServer.power_box`, `power_box_journal`, `power_box_child`, `power_box_flip`, `power_box_fault`, and `wait_until`; the builder owns the device's state path, so no scenario rebuilds it. See the CMD section above. |
| 12 — Serial device | Done | Six scenarios (`SER-01` to `SER-06`) in `tests/e2e/test_serial_device.py`, each wiring a Serial Port to a `socat` pty pair whose far end `SerialStation` holds, so the tier reads what the device reads: the characters an operator typed arriving unchanged with nothing echoed back, the configured speed, parity and stop bits on the device's own termios, a device no host has stating its reason in `/api/ports` and the log while a console attached to that Port is told where its typing went, a cable pulled mid-session reported on the console, in `status_message` and in the log and healed by the Port's own five-second retry, one block of every byte value crossing whole in both directions, and the handshake a config asks for present on the line. One product defect came out of this group and is fixed on its own: `flow_control` was left out of the call that opened the device, so `rtscts` and `xonxoff` started no handshake at all. `tests/server/test_serial_flow_control.py` binds the mapping and `SER-06` proves the termios. The harness gained `SerialStation` (`send`, `received`, `line_settings`, `unplug`, `plug_back_in`), `devices.serial(...)`, the `stopbits` and `flow_control` knobs on `with_serial_port`, and `CliSession.byte_offset` with `reply_bytes_after` for a byte-exact read of a binary payload. See the SER section above. |
| 13 — Soak | Done | Seven scenarios (`SOK-01` to `SOK-07`) in `tests/e2e/test_soak.py`, each with real sessions on one Port: twelve Telnet viewers over one two-hundred-line stream, one viewer that stops reading while twelve hundred padded lines follow, two hundred unanswered requests from sixteen threads beside twenty signed ones and a live poller, twenty idle logins plus twelve abandoned sockets, nine sessions that close cleanly or reset mid-stream or never speak, and a 0.2 s heartbeat that outlives viewers arriving on Telnet and on the console websocket. `lsof -n -P` gives the descriptor counts and `netstat -an -p tcp` the bytes the kernel holds for one stalled console, so the tier adds no dependency. Two measurements stayed unexplained and are on issue #116. The harness gained `clients.silent(...)`, `clients.websocket(...)`, `SilentSocket`, `WebSocketViewer` (`read_frame`, `wait_for_text`, `ping`, `wait_for_pong`, `abort`), and `E2EServer.descriptor_kinds()`. See the SOK section above. |
| 14 — MuxCon federation | Done (base) | Nine scenarios (`MXC-01` to `MXC-09`) in `tests/e2e/test_muxcon.py`, each starting two `openmux.server.main` processes in one test: the handshake and what each node reports about the other, a peer Port carrying its origin's identity, the ONE Write slot the origin arbitrates for every console on every node, a released slot freed to nobody and taken only by the next console that asks, the refusal a blocked console reads across the link, a TLS link whose self-signed certificate is trusted on first sight and refused forever after under a different fingerprint, a peering key whose true holder links while a wrong key under its name dies at the signature and a keyless peer's Port list is dropped unheard, a qualified `<node>::<port>` name honoured on the menu and the wire and refused for any node that lacks the Port, and an outlet switch anchored on the console that types it, audited on the owner's log as its own relayed session and flowing back as the typing console's own power-loss notice. `ConfigBuilder.with_muxcon` writes the section (listener, initiator, filters, authentication), `servers()` gained the `muxcon` and `muxcon_kwargs` knobs, and `E2EServer.server_id` names a peer. It found two product defects: `api/ports` labelled a peer Port with this node's own id, because the composite id was computed before the origin was known, and a `server.id` longer than the 64-character X.509 CommonName ceiling made the autogen certificate fail, so the federation listener never started. The Config Editor's `muxcon` writes stay in Release 2 item 2. See the MXC section above. |

- `IDX-03` closes a gap the phase-1 index checks left: a machine without Chromium reported
  `60 passed, 41 skipped, 3106 deselected in 143.92s`, and the skip reason stayed hidden because `make test-e2e`
  runs with `-q`. The pass count alone read like a covered run. See [`tests/e2e/README.md`](../../tests/e2e/README.md)
  for the block it now prints.

- `AUTH-11` (an external helper that grants no permission): built, and it is why the tier now ships a helper fixture.
  `ConfigBuilder.with_external_auth` writes the `external_auth` block into `authentication.yaml`, and
  `tests/e2e/auth_fixtures/helper_stub.py` answers one protocol on stdin and stdout. It holds four names, so one script
  covers the states a helper can leave a person in, and `ext_slow` exists only so a probe can time a slow answer.
  `AUTH-11` asserts nothing about a duration; issue #114 carries the stall it measures.
- The page hook (`console.js`), `ConsoleSession` state, and `ConsoleSession.menu_text()` (the page's own menu wording,
  read from the `ro` and `ctrl` menus, which is how `CON-05` avoids a typed-in string): built.
- `CON-11` to `CON-14`: built. A `DeviceStation` (a fake device behind a `tcp_initiator` port) is unplugged and plugged
  back in to drive the port-down banner, one `POST /api/reload` rebuilds the sidebar of an open page, the theme is read
  on its own key, and `embed=1` is measured against a page with the menu visible.
- `RW-20` a stalled viewer: built, and it passes. It was planned as an expected failure, and it later failed once in a
  full run for a reason inside the scenario; see [Concurrency model](#concurrency-model) for what one stalled reader
  does and does not block.

Five product defects came out of these phases. A clean stop exited 2, which `Restart=on-failure` read as a crash;
`LIF-06` now proves a SIGINT stop exits 0 with no traceback. A client that left without closing its socket cleanly,
or left while the server was stopping, wrote a traceback into the log; `LIF-10` proves a stop with a client attached
and a client that dies mid-read leave no error, and that one session is torn down once. And the login lockout never
engaged, because asking whether an account was locked deleted the count of failures; `AUTH-03` proves the configured
threshold now locks and releases an account. The write-slot suite found two more. A client attached read-only could ask
its way into the Write slot, because the request path counted the seat and skipped the access ladder that attach and
Takeover both run; `RW-13` and `RW-16` prove the refusal now. And a targeted Takeover refused the holder id the holders
list prints, so an operator could not name the holder the console showed them; `RW-09` proves the round trip. Each defect
carries a unit guard that fails without the fix, and `CHANGELOG.md` carries an entry for each.

---

## Build order

1. **Phase 0 — harness.** `harness.py`, `console.py`, `textclients.py`, `story.py`, marker, `--no-cov` target,
   artifacts ignored. Stop and review here; every test depends on these choices.
2. **Phase 1 — `test_lifecycle.py`.** No browser. Proves the server boots, stops, and round-trips over the client
   listener.
3. **Phase 2 — browser proof.** `login()`, `ConsoleSession`, one console test. Proves Playwright and the hook, the
   two risky pieces.
4. Phases 3-6 — AUTH and CON, then RW, then PWR, then CFG.
5. Phases 7-9 — ACT, ST, story.
6. **Phase 10 — `test_ssh_listener.py`.** Release 2 item 1, built early: no browser, one more listener per world.
7. **Phase 11 — `test_power_command.py`.** Release 2 item 3: no browser, one fixture script that plays the device, and
   one server per scenario.
8. **Phase 12 — `test_serial_device.py`.** Release 2 item 4: no browser, one `socat` pty pair per device, and the test
   itself holding the far end as the device.
9. **Phase 13 — `test_soak.py`.** Release 2 item 5: no browser, many real sessions on one Port, and the server process
   counted from outside with `lsof`.

---

## Verification

1. `make e2e-install`, then the lifecycle suite alone. Socket binds need network permission; a blocked bind looks
   like a product failure.
2. `.venv/bin/python -m pytest -q -m "not e2e" --no-cov` collects the same count as before this work. `make test`
   passes with no browser installed.
3. Run the tier twice: no leftover server process, no orphaned command child, no `~/.openmux` writes.
4. **Prove each suite can fail.** Break one product behaviour per suite and confirm a readable assertion: stop a
   listener, rename a DOM id, change the `demoted` wording, drop the CSRF header. A suite that survives these
   changes adds nothing.
5. Prove the observability layer: run one suite with `--e2e-live` and confirm all surfaces interleave with monotonic
   offsets, then fail a test and confirm the bundle shows what each terminal held and decodes the frames.
6. `make lint`, then `black` and `isort` on `tests/e2e/`.
7. Docs: new harness terms in [docs/GLOSSARY.md](../GLOSSARY.md) in the same commit (AGENTS.md requires this),
   [`tests/README.md`](../../tests/README.md) (add the tier, fix the `integration` claim),
   [`docs/SMOKE_TEST.md`](../SMOKE_TEST.md) (note what the tier now covers), and a `CHANGELOG.md` entry. ASD-STE100,
   wrapped at 127 columns.

Result of the Phase 0-2 run (2026-10-27). Steps 1, 2, 3, 5, 6, and 7 are done: 13 scenarios pass in about 10 s, plain
collection is unchanged at 3004 collected and 9 deselected (3013 with `--run-e2e`), no server process or command child
survives a run, nothing is written to `~/.openmux`, `--e2e-live` interleaves every surface with monotonic offsets, and a
failure keeps a bundle with the timeline, the config, the server output, a screenshot, and a trace. Step 4 is partial by
nature: `LIFECYCLE` and `CONSOLE` hold no wording or element-id assertion to break yet, so that step completes with the
suites that read them. What was proved now is that the harness reports a failure readably: a wrong assertion produced one
`FAIL` line naming the step, and a failing fixture produced a bundle with the crash line.
