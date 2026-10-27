# End-to-End Test Suite

**Status:** Implemented through Phase 3. The harness, the `LIFECYCLE` suite, the scenario index, the browser proof,
the `AUTH` suite driven over HTTP, and the fourteen `CON` console scenarios driven in Chromium run green. Phases 4-9
are not written; see **Build status** near the end.
**Date:** 2026-10-27
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
one freed seat (`RW-15`), one Port Action race (`ACT-06`), and `RW-20` below, all three in groups that are not written
yet. `concurrently([...])` runs actions in threads behind one `threading.Barrier`, joins with a deadline, and returns
every result or raises once with all of them.

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

### RW-20: a stalled viewer

`RW-20` tests the control path: attach three viewers, stop one reader on purpose, then change the viewer set so the
server broadcasts presence, and assert the other two receive the frame inside one deadline. Nothing in the current
code gives that guarantee, so the test is expected to fail when it is written. If it does, the fix belongs in the server
— deliver in parallel with `asyncio.gather`, or skip a client whose write is not draining — and not in the test. The
scenario is written marked expected-to-fail, so a red first run reads as a product finding and not as a broken harness.

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

The suite today runs 41 scenarios in 5 files: `LIFECYCLE` 10, `AUTH` 10, `CON` 14, the index checks 2, and the timeline
recorder 5. The generated [docs/E2E_TESTS.md](../E2E_TESTS.md) is the list of what runs, and its count is checked by
`IDX-02`. Five further groups below are the plan of record: they have ids, boundaries, and assertions written down, and
they have no test file yet. Full wording for every built id lives in that index.

### LIFECYCLE — process and listeners (`tests/e2e/test_lifecycle.py`, 10)

Boots, `/healthz` public plus `/api/ports` 401, Loopback and command round trip over the client listener, bad config
exits non-zero naming the key, SIGINT exits clean, SIGHUP soft reload keeps an attached client, SIGUSR1 full reload
drops then restores it, a reload with an attached client leaves no cleanup error in the log, and control socket status
with mode 0600.

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

### RW — write slot capacity and Takeover (`test_write_slot.py`, planned: 20)

Ports at capacity `one`, `multiple`, `none`, and one with `read_write_groups`. First attach granted, second
`capacity_full`, holders label, request refused on a full seat, release then grant, Takeover words both terminals and
leaves the loser attached, viewers menu rules, cross-surface Takeover CLI→web and Telnet→CLI, menu parity, entitlement
refusals, two writers on `multiple`, `none` refused for every permission, read-only input blocked on each surface, two
simultaneous Takeover requests leave one holder, disconnect frees the seat, group ACL with close code 4003, audit
records, `read_only: true` listener, stalled viewer (`RW-20`, see
[Concurrency model](#concurrency-model)).

Two facts from the Access decision module shape these ids. A `promote_failed` answer is worded by the port capacity
first, so one Outcome code reads differently on a `one`, `multiple`, and `none` port. And the slot count is an
invariant that `ConsoleManager._take_slot_local` restores on a half-completed transfer: when the taker's promote fails
after the demotion, the server promotes the victim again, which is exactly what `RW-15` will assert at process level.

### PWR — power subsystem (`test_power.py`, planned: 16)

`/power` and `/power/<pdu>`, `/api/power` shape and `unresolved_refs`, buttons per permission, off-impact confirms,
power badge and `OMXCTRL power_switch`, notices on web and CLI and Telnet, all-feeds-off warning, audit lines in the
server log and the port log, no audit on a failed switch, REST permission and group matrix, CSRF, `POWER` command
forms, Telnet `p` menu, `power.enabled: false`, poll task reflection.

### CFG — configure, then reload (`test_config_editor.py`, planned: 14)

All views and the unknown-view fallback, nav, then add a Loopback port and soft reload with a client attached, config
file preserved, add a PDU and see the `pdus_changed` nudge, add `power:` feeds to an attached console, invalid edit
rejected with the file unchanged, non-writable section, read-write `403`, add a Telnet listener, full reload restarts
adapters, soft reload keeps attached clients, an adapter that cannot start is reported.

This suite depends on order: each test leaves state the next needs, and each docstring ends with `Depends on:`.

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
| 1 — lifecycle | Done | `LIF-01` to `LIF-10`, plus `IDX-01` and `IDX-02`. |
| 2 — browser proof | Done | `login()`, `ConsoleSession`, the page hook under `?e2e=1`, with Chromium installed by `make e2e-install`. |
| 3 — AUTH and CON | Done | `AUTH-01` to `AUTH-10` over HTTP with a real cookie jar, and `CON-01` to `CON-14` in a real Chromium page. `AUTH-11` waits on a helper-based account, see the note below. |
| 4-9 | Not started | RW, PWR, CFG, ACT, ST, and the story scenarios. |

- `AUTH-11` (an external helper that grants no permission): outstanding. The `command` authentication backend runs in
  every e2e server, so only the *no-permission* case differs from `AUTH-10`, and the helper would have to be a fixture
  script that the packaged `openmux` process can execute — an external path nobody would follow by accident. `CFG-01`
  is checked with `config/authentication.yaml.example`, which is the same file in a different place.
- The page hook (`console.js`), `ConsoleSession` state, and `ConsoleSession.menu_text()` (the page's own menu wording,
  read from the `ro` and `ctrl` menus, which is how `CON-05` avoids a typed-in string): built.
- `CON-11` to `CON-14`: built. A `DeviceStation` (a fake device behind a `tcp_initiator` port) is unplugged and plugged
  back in to drive the port-down banner, one `POST /api/reload` rebuilds the sidebar of an open page, the theme is read
  on its own key, and `embed=1` is measured against a page with the menu visible.
- `RW-20` a stalled viewer: not written. It needs `test_write_slot.py`, which does not exist yet; see
  [Concurrency model](#concurrency-model) for what it will assert and why it may report a product defect first.

Three product defects came out of these phases. A clean stop exited 2, which `Restart=on-failure` read as a crash;
`LIF-06` now proves a SIGINT stop exits 0 with no traceback. A client that left without closing its socket cleanly,
or left while the server was stopping, wrote a traceback into the log; `LIF-10` proves a stop with a client attached
and a client that dies mid-read leave no error, and that one session is torn down once. And the login lockout never
engaged, because asking whether an account was locked deleted the count of failures; `AUTH-03` proves the configured
threshold now locks and releases an account. `CHANGELOG.md` carries an entry for each.

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
