# End-to-End Test Suite

**Status:** Implemented through Phase 6. The harness, the `LIFECYCLE` suite, the scenario index, the browser proof,
the `AUTH` suite driven over HTTP, the fourteen `CON` console scenarios driven in Chromium, the twenty `RW`
write-slot scenarios, the nineteen `PWR` power scenarios, and the seventeen `CFG` Config Editor scenarios run green.
Phases 7-9 are not written; see **Build status** near the end.
**Date:** 2026-10-28
**Scope:** A test tier that runs the real server as a process and drives it through a browser, the CLI protocol,
and Telnet at the same time.

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

## Current state

| What exists | Coverage | Gap |
|---|---|---|
| `tests/` unit and component tests (about 3000 items) | Adapters, managers, ACL, power drivers, protocol edge cases | No test starts the server process. No browser. |
| `scripts/smoke_workflow.py` + `config/integration_test.yaml` | Starts the server, one CLI session, Loopback round trip | Manual only. No `make` target, no assertion depth. |
| `docs/SMOKE_TEST.md` | How to run that script by hand | Documents the manual path, not a pytest tier. |
| `tests/README.md` | Run `pytest`, pick a marker, add a docstring | Points at bare `pytest`, not the `make test-*` targets, and advertises `integration`, which no test uses. |

No test uses the registered `integration` marker. No file under `tests/` matches `*e2e*`, `*smoke*`, or
`*integration*`, so the `e2e` name and the `tests/e2e/` path are free.

`playwright` is not a dependency, and no browser binary is installed.

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
one freed seat, and one Port Action race (`ACT-06`). `concurrently([...])` runs actions in threads behind one
`threading.Barrier`, joins with a deadline, and returns every result or raises once with all of them. The freed-seat
race is not built: `RW-13` covers the seat-frees-then-requested sequence in order, and the simultaneous form is
available in the `RW` file when it is wanted.

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
   it does not. `AUTH-11` is named outstanding rather than skipped, because writing it needs an external helper script
   nobody would follow by accident.

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

The suite today runs 99 scenarios in 8 files: `LIFECYCLE` 11, `AUTH` 10, `CON` 14, `RW` 20, `PWR` 19, `CFG` 18, the
index checks 2, and the timeline recorder 5. The generated [docs/E2E_TESTS.md](../E2E_TESTS.md) is the list of what
runs, and its count is checked by `IDX-02`. Three further groups below are the plan of record: they have ids,
boundaries, and assertions written down, and they have no test file yet. Full wording for every built id lives in that
index.

### LIFECYCLE — process and listeners (`tests/e2e/test_lifecycle.py`, 11)

Boots, `/healthz` public plus `/api/ports` 401, Loopback and command round trip over the client listener, bad config
exits non-zero naming the key, SIGINT exits clean, SIGHUP soft reload keeps an attached client, SIGUSR1 full reload
drops then restores it, a reload with an attached client leaves no cleanup error in the log, control socket status with
mode 0600, and a Telnet login whose four lines arrive in one write reaches the Port those lines name.

### AUTH — login and permission matrix (`test_auth.py`, 10 of 11)

Built: the login form and the cookie attributes it hands out, the wrong-password sentence, lockout after failed
attempts and its release, `?next` round trip, logout and the cookie taken back, a wrong Basic header not falling
through to a valid cookie, `403` per permission level on the Config Editor, the CSRF token required for a session POST
and exempt for Basic, and the SSO trust header with every forgery refused. Open: a user who authenticates through an
external helper and is refused because that helper granted no permission.

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

### RW — write slot capacity and Takeover (`test_write_slot.py`, built: 20)

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

Three facts from the Access decision module shaped these ids, and all three are measured, not assumed.

- A plain attach grant and a `capacity_full` grant render **no** terminal line on any surface: `lines` is empty and the
  reason lives in the frame `message`. `RW-01` and `RW-02` read the mode and the frame code instead of a sentence.
- The sentence in the design draft — that a `promote_failed` answer is worded by the port capacity — is wrong. One
  place, `ConsoleManager._seat_refusal`, picks the Outcome **code**: `no_write_slot` on a `none` port,
  `take_without_slot` on a `multiple` port, and `promote_failed` only on a one-seat port whose promote failed. Each code
  has one wording, so capacity never restyles one code.
- The slot count is an invariant that `ConsoleManager._take_write_slot_local` restores on a half-completed transfer:
  when the promote of the taker fails after the demotion, the server promotes the victim again. No `RW` id drives that
  path at process level yet; it needs a port whose promote fails, which the harness cannot build from a config file.

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


### CFG — configure, then reload (`test_config_editor.py`, 17 scenarios)

Each scenario builds its own server, edits its own config, and proves its own save-then-reload-then-see order. Four
owners sit behind one page, so each scenario names the pair a person would otherwise reconcile by hand: access and
CSRF come from `web_plugins/config_editor.py`, what a save writes comes from that plugin's `/apply` and `/validate`,
what a save *starts* comes from `main.py` (`reload_adapters_soft()` reconciles in place, `reload_adapters_full()`
recreates every adapter and restarts the web console after the reply), and what an open page learns without a reload
comes from `layout.js` (`pdus_changed`) and `console.js` (`reload_applied`) over the socket that page already holds.

Two parts of these worlds are simulations. The `dummy` PDU driver answers at once and needs no device, so a PDU that
appears in the file always starts. The one adapter failure this tier can produce on demand is a Serial port whose
device path no host has: the reload starts it and the port reports itself down.

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
| `CFG-18` | A Serial entry the server cannot build is named in the log block, reported as `failed`, and never as `added`. |

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
(`CFG-06`).

Three defects in the product came out of measuring, and each is fixed with the scenario that proves it.

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
  [Issue #105](https://github.com/OpenMux/openmux-server/issues/105).

### ACT — Port Actions (`test_actions.py`, planned: 9)

Button visibility per port and permission, granted list, run output and status classes, progress bar, operator prompt
kinds, `operator_prompt_closed` reaching a second viewer, `?action=…&autorun=1` toast, stop and takeover and close
rules, both viewers see the finish.

### ST — status pages (`test_status_pages.py`, planned: 6)

Sort links and summary counts, detail row that stays open across a refresh, `#clientsTbody` across all surfaces,
federation empty state, power page driven by site events, `/api/status/fragments`.

### Story and timeline recorder (`test_story.py`, 5 built, 1 planned)

Built: the five recorder scenarios (`TIM-01` to `TIM-05`) prove what one timeline line looks like — caret notation for
control bytes, `IAC WILL ECHO` named, a truncated command marked rather than guessed, one record per line, and a long
payload kept whole beside the file. Planned: one sequence in one process, listed below. Its fixtures exist today, so it
is the cheapest of the planned groups to write.

One sequence in one process: configure a PDU through the browser, soft reload, web holds the write slot, CLI requests
and is refused, CLI takes the slot, both terminals show the wording, switch the last feed off, both warn, restore, and
the audit line present.

---

## Release 2

Named now so release 1 fixtures stay sufficient.

1. SSH listener: host key inside `OPENMUX_STATE_DIR`, one Takeover, one `p` menu run. The control menu is shared with
   Telnet and already covered by `RW-10`.
2. MuxCon with two live nodes: first attach read-only, `FEDRW` Takeover, `CONNECT:{server}::{port}`,
   `POWER:SWITCH` relay, federation area, multipath panel. See [muxcon.md](muxcon.md) §4.5 and 4.6.
3. Power `driver: command` driven by a checked-in fake device script. Only monkeypatched subprocesses are covered
   today.
4. Serial adapter over a `socat` pty pair.
5. Soak: many viewers, simultaneous requests, socket churn.

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
| 3 — AUTH and CON | Done | `AUTH-01` to `AUTH-10` over HTTP with a real cookie jar, and `CON-01` to `CON-14` in a real Chromium page. `AUTH-11` waits on a helper-based account, see the note below. |
| 4 — write slot | Done | `RW-01` to `RW-20` in `test_write_slot.py` over the three surfaces. The harness gained a per-port listener knob, a `groups` list per user, the two group lists per port, and `expect_no_text` on both text sessions. |
| 5 — power | Done | `PWR-01` to `PWR-19` in `test_power.py`: the REST shape, both PDU pages, the badge and its menu, the terminal notices on three surfaces, the `POWER` command, the Telnet `p` menu, the permission and group matrix, and the audit lines. Three product defects came out of it, listed under `PWR` above. The harness gained the page, dialog, and text-session helpers each one needed; see [`tests/e2e/README.md`](../../tests/e2e/README.md). |
| 6 — Config Editor | Done | `CFG-01` to `CFG-18` in `test_config_editor.py`: the gating and CSRF pair, what a save writes and refuses, the soft reconcile and the full reload seen from an open page, a Serial port with no device behind it, and a Serial entry the adapter refuses to build. Three product defects came out of building this group, listed under `CFG` above. The harness gained `ConfigBuilder.with_serial_port`, a Telnet `port` knob, `TelnetSession.wait_for_close`, and `ConsoleSession.wait_for_selector`; `CFG-18` edits `server.yaml` through `world.config_dir`, because a hand edit is the state under test and the Editor refuses it. |
| 7-9 | Not started | ACT, ST, and the story scenarios. |

- `AUTH-11` (an external helper that grants no permission): outstanding. The `command` authentication backend runs in
  every e2e server, so only the *no-permission* case differs from `AUTH-10`, and the helper would have to be a fixture
  script that the packaged `openmux` process can execute — an external path nobody would follow by accident.
  `CFG-01` covers who may open the Editor; the no-permission helper case stays open.
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
