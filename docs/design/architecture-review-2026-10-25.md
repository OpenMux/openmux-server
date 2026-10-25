# Architecture Review: Deepening Opportunities (2026-10-25)

This document lists seven refactors that turn shallow modules into deep ones. Each candidate names the files, the
problem, the proposed deepening, and the gains. It is the durable form of the 2026-10-25 review: the HTML report
with the before and after diagrams was a one-off preview and is not committed, so this file carries every fact.

Vocabulary follows `docs/GLOSSARY.md` and the deep-module terms: module, interface, implementation, depth, seam,
adapter, leverage, locality. Do not read these candidates as feature work. Each one removes a place where the
interface is as wide as the implementation.

## How to use this document in a new session

1. Pick one candidate. Open a branch from `main`; do not combine two candidates in one session.
2. File an issue for the candidate, label it `tech-debt`, and copy its section here as the issue body. Add the
   dependency row from the index table.
3. Pressure-test the choice before design. State the constraints, the dependencies, and where the seam sits.
   Do not write code until the seam is agreed.
4. Design the interface with the `codebase-design` vocabulary. Record the outcome: add new terms to
   `docs/GLOSSARY.md`, and record the first ADR under `docs/adr/` when a candidate fixes a trade-off.
5. Execute as behaviour-preserving moves with the `tdd` skill. Run `.venv/bin/pytest -q tests/test_<name>.py`
   after each move, and `make test` plus `make lint` before the pull request.
6. Update `CHANGELOG.md` only when a user sees a config or behavior change. Pure extraction needs no entry.

No `docs/adr/` exists at the time of writing, so no candidate contradicts a recorded decision.

## Scope and method

- Scan scope: `openmux/server/adapters/` (17 files, 22 555 lines), `openmux/server/web_console.py` (5 056 lines),
  and the reload, power, and listener code the last month of work touched hardest.
- Duplication is measured, not judged. Method bodies were compared per method with AST source segments after the
  transport names were normalised (`.venv/bin/python`, `ast` plus `difflib`).
- `grep` counts: 258 `hasattr` probes on collaborators across `openmux/server`, 13 `_api_reload_*` functions in
  `web_console.py`, and 170 Fake, Dummy, or Stub classes across 51 test files with one helper in `tests/support`.
- Strength badge: **Strong** means an interface shrinks and the deletion test shows complexity concentrates.
  **Worth exploring** means the seam is real but a product decision or a test gap must close first.
  **Speculative** means low churn, so the gain must be shown before code moves.

## Candidate index

| # | Candidate | Seam | Strength | Depends on |
|---|---|---|---|---|
| 1 | One Client-listener session module, two transport adapters | in-process | Strong | Candidate 2 |
| 2 | Give Takeover a result instead of `(bool, str)` | in-process | Strong | none |
| 3 | One deep Port-set reconcile module | in-process | Strong | none |
| 4 | One module for the Outlet switch decision | local-substitutable | Worth exploring | a decision on MuxCon |
| 5 | One login gate behind the Auth Manager's interface | mock | Worth exploring | none |
| 6 | One page-render interface for the web console and its plugins | in-process | Worth exploring | none |
| 7 | Fold the Command port spawn pipeline into one module | local-substitutable | Speculative | none |

## 1. One Client-listener session module, two transport adapters — Strong

Files: `openmux/server/adapters/telnet_listener.py` (984 lines), `openmux/server/adapters/ssh_listener.py`
(1 086 lines), `openmux/server/adapters/listener_common.py` (551 lines),
`tests/server/test_telnet_listener_adapter.py`, `tests/server/test_ssh_listener_adapter.py`.

Problem. Two adapters hold the same Client session. They share 36 member names. After only the transport names
change, 21 bodies are byte-identical (589 Telnet lines against 575 SSH lines) and 29 exceed 90 percent similarity.
`listener_common._dispatch_control_command` takes nine arguments, six of them callbacks that each caller must bind,
which makes it a helper bag and not a module. The two adapters also declare two `ListenerConfig` dataclasses and
two Session dataclasses.

Deepening. Place the seam at the only real difference: the transport. Each transport adapter exposes
`read`, `write`, `drain`, and `peer_ip`. One listener session module above it owns login, the Port menu, attach,
the input pump, the Escape sequence, the Control menu, the power menu, detach, and read-write notices. That module
carries 29 of the 36 names, written once.

Wins:

- Two transport adapters make the seam real instead of hypothetical.
- The interface drops from 36 mirrored names to one.
- One test suite replaces two mirror-image files and halves the fixtures.
- Control menu bugs get fixed once, because locality lands in one module.
- The nine-argument callback bag dies.
- One `ListenerConfig` replaces two.

Test note. The two suites are mirror images today: `test_force_rw_prompts_and_passes_target` appears in both, and
the power menu tests are duplicated. Several names say "escape menu", which `docs/GLOSSARY.md` forbids; the
approved term is **Control menu**. Retire that naming while the files move.

## 2. Give Takeover a result instead of `(bool, str)` — Strong

Files: `openmux/server/console_manager.py:1140` (`take_write_slot`) mints the reason string at line 1266;
`adapters/ssh_listener.py:768`, `adapters/telnet_listener.py:558`, `adapters/client_listener.py:864`, and
`web_console.py:1645` parse it; `adapters/listener_common.py:427-500` and
`openmux/client/adapters/rw_control.py:22-60` decode the denial vocabulary branch by branch.

Problem. The Write slot interface returns a bare `(bool, str)`. The string is an undocumented protocol of eight
values plus one embedded payload, `"takeover from <user> [<id>]"`. Four callers test the prefix, for example
`if ok and reason.startswith("takeover from "): payload["takeover"] = reason.removeprefix("takeover from ")`, and
two more decode it branch by branch. The reason values are `already_rw`, `promote_failed`, `invalid_target`,
`federation_denied`, and `victim_not_current`, plus the takeover payload. Test fakes hold the formatted string, for
example `_FakeSshCm((True, "takeover from alice [f4]"))`.

Deepening. Return a structured result: outcome, victim label, holders, and Write slot capacity. Each surface keeps
its own rendering: one CRLF line on Telnet and SSH, one control frame on the web console, one refusal line on the
CLI. Decoding exists nowhere.

Wins:

- The interface stops leaking a format.
- Four parse sites and two duplicated holder helpers (`_rw_holders_for_port`, `_max_rw_users_for_port`, which exist
  in both `client_listener.py` and `web_console.py`) join this interface for free.
- Denial wording lives in one place, and lockout wording stops drifting.
- Magic strings leave the fakes.
- The MuxCon relay matches the local shape.

Sequence. Do this candidate before candidate 1, in its own pull request. It is contained, and it removes the prefix
parsing that a moved `_cmd_force_rw` would otherwise inherit already wrong.

## 3. One deep Port-set reconcile module — Strong

Files: `reconcile_ports` in `adapters/serial.py`, `loopback.py`, `command.py`, `tcp_initiator.py`,
`telnet_listener.py`, `ssh_listener.py`, `muxcon.py`, `pdu.py`, and `client_listener.py`;
`web_console.py:1105-1310` (13 `_api_reload_*` functions); `main.py:921` and `main.py:1159`.

Problem. Nine adapters re-implement the same soft-reload ritual: index by name, diff the materialised config, stop
then start, build a summary, log one line. The helper names drifted into four vocabularies, for example
`_reconcile_normalize` with `_material_config` and `_material_keys` in `serial.py`, against `_reconcile_items` with
`_material_cfg` and `_port_old_cfg` in `loopback.py`. The rule that matters most, which Clients lose their
connection, escaped the adapters entirely into `web_console._api_reload_disconnect_affected`.

Evidence that the drift is live, not cosmetic: `serial._material_keys` lists 16 fields and `loopback._port_old_cfg`
reads 5. Two adapters decide "changed" differently today.

Deepening. One module owns the ritual. Each adapter supplies five hooks: `build`, `start`, `stop`, `material`, and
`apply_in_place`. The three reload drivers (`main.py` soft and full reload, and the Config Editor API) call it.

Wins:

- Deletion test: complexity concentrates in one module instead of nine.
- 13 reload functions collapse to one call, and `_api_reload_disconnect_affected` moves behind the interface.
- One ordering rule replaces nine restatements.
- The Config Editor path and SIGHUP become equal by construction.
- Drift becomes impossible, and per-adapter tests become hook tests.

## 4. One module for the Outlet switch decision — Worth exploring

Files: `adapters/pdu.py:441,449` (`handle_power_frame`), `adapters/power_command.py:160,170` (POWER command),
`web_plugins/power_monitor.py:166,176` (web console Power page), `actions/session.py:468,475` (Port Action script),
`adapters/muxcon.py:1364` (MuxCon relay).

Problem. The switch rule is: the operator must hold read-write or admin permission, then every console the Outlet
feeds must be openable by that operator, and admin bypasses. No module holds that rule, so five surfaces
re-assemble it. Three foreign modules reach the private `PduAdapter._power_blocked_ports`; the MuxCon relay reaches
`_port_objects()` instead and skips the console-group gate, so the relay path runs a different gate. The copy is
self-admitted in code: "Applies the same group gate as every other power surface" (`actions/session.py:464`).

Deepening. One switch decision on the PDU adapter:
`switch_as(username, client_id, ref, on) -> Outcome`. The Outcome carries permission, the console-group gate, the
global Outlet ref rejection, the off-impact pre-flight (`compute_off_impact`), and the audit line. Each surface
keeps its own transport guard and renders the result.

Open decision. The MuxCon relay switches on a session anchor and reports the actor as a `fed:*` id. Decide whether
the relay keeps a separate, narrower gate on purpose, or whether federation must satisfy the same console-group
gate. Do not move code before that answer, because the two choices produce different interfaces.

Wins:

- The security rule stops drifting across five surfaces.
- Private members go back inside the PDU adapter.
- One audit line covers the relay too.
- One gate test replaces five branches, with a dummy driver standing in.
- The Outcome shape stops varying per surface.

## 5. One login gate behind the Auth Manager's interface — Worth exploring

Files: `auth_manager.py` (`authenticate_user:478`, `register_auth_failure:290`, `clear_auth_failures:312`,
`is_user_locked:317`); `adapters/ssh_listener.py:59` (`_AUTH_MAX_ATTEMPTS = 3`);
`adapters/telnet_listener.py:700-745`; `adapters/client_listener.py:450-545`; `web_console.py:821-890`.

Problem. Four surfaces drive four shallow Auth Manager primitives through `hasattr` probes, and each keeps a slice
of the login policy. Only the SSH adapter holds the attempt cap. Only the web console holds its lockout read. The
client listener probes for HMAC support at the call site.

Evidence the seam is real today: the test
`test_pubkey_offers_do_not_count_against_password_attempt_cap` exists because two credential kinds shared one
close budget inside `_OpenMuxSshServer`. The fix belongs to a module that does not exist yet.

Deepening. One decision per credential kind: `check_password(user, secret, src_ip) -> Decision`. The module owns
the lock check, the attempt budget, the separate public-key budget, exponential lockout, the audit line, and the
default-credential warning. Callers only render the refusal. `verify_password_hmac`
(`auth_manager.py:245-289`) already has this shape internally, which makes the target interface concrete.

Wins:

- The attempt budget lives in one module, so policy is identical on all four surfaces.
- `hasattr` probing disappears here.
- One verifier fake replaces three variants.
- The public-key against password budget bug cannot recur.
- Lockout wording stops drifting.

## 6. One page-render interface for the web console and its plugins — Worth exploring

Files: `web_console.py` (`_render_console:2673`, `_render_console_error:2707`, `_render_logs:2732`,
`_render_status:2767`, `_render_about:2920`, `_render_login:4223`, plus two more);
`web_plugins/power_monitor.py:60-135`; `web_plugins/config_editor.py:290-345`.

Problem. Eight page renders re-assemble the same seven chrome keys. Because the core renders take chrome as
parameters, the plugins bypass them and read six private members of the web console instead: `_jinja_env`,
`_get_effective_permission`, `_get_allowed_plugin_nav`, `_get_ports_snapshot`, `_effective_base_path`, and
`_get_logo_url`. Those 24 fetches each sit behind a `hasattr` guard, so a renamed member fails silently.

Deepening. One render module: `render_page(request, template, payload) -> bytes`. It resolves chrome, permission,
plugin nav, the Port snapshot, base path, and logo. The plugin interface becomes a template, a payload, and a nav
id.

Wins:

- Six private members stop leaking and the 24 guarded fetches are deleted.
- `_render_logs` drops from nine parameters to five.
- Plugins become honest adapters against one interface.
- A new chrome key needs one edit instead of eight.
- Test fakes implement one render interface, and plugin fallback branches are deleted.

## 7. Fold the Command port spawn pipeline into one module — Speculative

Files: `adapters/command.py:138-285` (six free `_spawn_*` functions), `adapters/command.py:732-752` (their only
caller), `tests/test_command_adapter_spawn.py`.

Problem. Six free functions take the whole `CommandPort` and mutate its private state
(`_read_task`, `_pty_master_fd`, `_pty_reader_added`, `process_active`, `use_pty`, `_set_connected`). The
ordering, the pty-to-pipes fallback, and the fd cleanup exist only as an unwritten contract at one call site, where
the caller re-reads `use_pty` after `_spawn_pty_process` clears it. The test file exists to reach inside the port.

Deepening. One spawn module: `Spawn.spawn(spec) -> LiveProcess`. It resets the previous process, sanitises the
environment, falls back from pty to pipes, registers the loop reader, and marks the port up. The returned handle
owns the master fd and the loop reader. `CommandPort` keeps I/O, buffering, and lifecycle notices.

Wins:

- Locality: fd leaks are handled in one place.
- Ordering becomes the interface instead of a caller's memory.
- `CommandPort` sheds four private fields and the test fixtures stop poking them.
- Deletion test: complexity concentrates.

Why only Speculative. Churn here is low. Show the fd-leak or ordering risk in a real change before spending the
move.

## Recommended sequence

Start with candidate 1, one Client-listener session module. It buys the most locality per unit of risk. The
duplicated bodies are already identical, so the extraction is mechanical and provable by text diff rather than
argument: 21 of the 36 shared names match byte-for-byte once only Telnet and SSH change. It lands on the code the
last month of work touched hardest, the SSH listener, the power menu, and targeted Takeover. It also turns two
mirror-image test files into one suite that crosses the new seam, which retires the forbidden "escape menu" naming.

Run candidate 2 first, as its own pull request, for the reason given in candidate 2. Then candidate 3, which is
independent and removes the largest count of near-duplicates. Take candidate 4 only after the MuxCon decision.
Do candidates 5 and 6 when a related change would otherwise add another `hasattr` probe. Leave candidate 7 until a
spawn bug proves the need.

## Vocabulary side effects

- Candidate 1 adds the term for the new module (**Listener session module**) and the term for each side of the seam
  (**transport adapter**). Add both to `docs/GLOSSARY.md` with the seam contract, and fix the "escape menu" test
  names to **Control menu** in the same move.
- Candidate 2 changes the Takeover result contract. Update the **Takeover** entry to name the result instead of the
  reason string, and record the trade-off as the first ADR under `docs/adr/`.
- Candidate 4 needs one term for the shared decision (**Outlet switch decision**) and a decision recorded as an ADR
  about the MuxCon relay gate.
- Add any new term to `docs/GLOSSARY.md` in the same pull request as the code that introduces it.
