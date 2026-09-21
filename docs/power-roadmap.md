# PDU Power Roadmap

This file tracks the PDU power feature work that comes after v1. Use the
checkboxes to record progress. Keep each item scoped so it can be checked off
when the work is done and merged. Add a date and a short note beside a box when
you close it (for example `[x] control audit log — 2026-10-02`).

v1 (shipped) shipped: the power adapter, the `dummy` driver, the web Power
pages, the `/api/power` API, the Config Editor Power view, per-port `power:`
feeds and badges, the `POWER` CLI command, off-impact warnings, soft-reload
reconcile, and live notices to attached sessions.

## v2 — hardening and audit

### Scope note

v2 hardens the v1 control surface. It changes who may switch an outlet and what
gets recorded. It does not add new device drivers or federate power.

- [x] **Group-scoped power control** — 2026-09-18. `ConsoleManager` gains
      `blocked_ports_for_user(port_names, username)`, which reuses the
      attach-time access ladder (`_taker_entitled`) to answer "may this user
      drive (read-write open) each of these consoles" (admin never blocked).
      `PduAdapter._power_blocked_ports(ref, username)` maps the outlet's fed
      consoles through it, and both switch paths enforce it: the web API
      (`POST /api/power/outlets/{ref}`) returns 403, and the CLI
      `POWER <pdu>.<outlet> on|off` replies with an `ERROR:POWER` line — both
      naming the out-of-group consoles and that switching needs admin.
- [x] **Control audit log** — 2026-09-18. `PduAdapter.set_outlet` takes
      `user`/`client_id` (web passes the authenticated username, CLI passes the
      session username + client id) and on success logs one human-readable
      INFO line, e.g.
      `POWER CONTROL: user rw turned rack1.1 off; losing all power: c1;
      staying up: c2; client ab12cd34`. Failures are not audit-logged (each
      logs its own error).
- [x] **Record the notice in the port log** — 2026-09-18. Each successful
      switch records a `power_control_notice` meta event on every affected
      console's port data log (`DataLogger.record_meta`), carrying the
      `[POWER]` / `[POWER WARNING]` notice wording (single line, the outlet
      ref in the record's own `outlet=` field), plus the new state, user,
      and client id. The all-lost warning form is decided per-port (same
      rule as the session notice).
- [x] **Add a CHANGELOG entry** — 2026-09-18. One "Behavior changes" entry
      under Unreleased 1.0.4 for the access-control change, naming both the
      `POST /api/power/outlets/{ref}` and `POWER <pdu>.<outlet> on|off` paths.

### Verification when closing v2

- Add tests for the group-scoped check on both the web and CLI switch paths
      (allowed within groups, blocked across groups, admin allowed).
- Add a test that a successful switch logs the audit line and that the port log
      gains the notice.
- Run `make test`, `make lint`, `make format` and confirm they pass.

## v3 — scope expansion

### Scope note

v3 adds new capabilities beyond a single node. Each item is independent; they
can be delivered in any order.

- [ ] **Real PDU drivers.** Add at least one real vendor driver (for example
      Raritan or APC) via the `DRIVERS` and `DRIVER_INFO` registries in
      `openmux/server/adapters/pdu.py`. The Config Editor driver select and the
      per-driver options help update automatically from the registry.
- [x] **MuxCon outlet federation** (console ports only) — 2026-09-20. A
      federated console port carries the origin's declared power feeds and
      last-reported outlet state across the wire. The feed list and state
      ride the `PORTS:FEDERATED` advertisement, and live changes travel in a
      new `POWER:STATE:<port>` control frame (one JSON line
      `{"ref": ..., "on": true|false|null}`, per-ref not per-port). A peer
      renders the same feed badge, `/api/ports` power block, `p` power menu,
      and live `[POWER]` terminal notices for a federated port as for a local
      one; the state survives a peer restart via the federated cache. The
      full remote PDU catalog is intentionally NOT federated (the federation
      is mostly about the console ports): a peer never sees the origin's PDU
      list, telemetry, or watts.

      Refs are globally qualified (`<origin_server_id>::<pdu>.<outlet>`,
      added 2026-09-20) so two nodes with an outlet of the same name stay
      unambiguous: a bare ref always means the local node's own outlet.
      `POWER:STATE` frames carry the sender's local ref and are applied
      sender-scoped (a frame from one origin never touches another origin's
      same-named ref); `POWER:SWITCH` frames carry the origin's local ref
      plus plain port-name claims.

      Switching an origin-owned outlet is now possible from a peer, bound to
      an already-open console session (added 2026-09-20). A user with an open
      read-write session on a fed port that the ref feeds can switch the
      outlet from every in-session switch surface (web in-session power
      menu / `/ws/<port>`, `POWER` command, telnet/SSH/CLI `p` menu) — the
      same rule as a local outlet. The relay is anchored on that session (no
      username crosses the wire): the peer sends `POWER:SWITCH:<port>:<sid>`
      with the feed refs of every port on this node that declares the ref
      ("claims"), and the origin verifies (a) the stream is a real
      origin-side session for that port, (b) the session mirror
      (`fed:<peer>:<sid>`) is read-write, and (c) coverage — every console
      the origin knows is fed by the ref is either the anchored console or
      claimed. It then runs `set_outlet` itself (audited under the mirror id)
      and answers with one `POWER:RESULT` frame; its `POWER:STATE` broadcast
      carries the new state back. If the anchor is missing or read-only, or
      coverage fails (the refusal names the missing consoles), every switch
      surface gets that typed error verbatim. The web Power REST page (no
      console session to anchor on) stays read-only for such refs. A ref also
      declared on a local port still switches locally. Feeds resolve per-ref
      and state flows one hop (origin -> direct peers); a deeper multi-hop
      chain does not see live state today (documented in
      `docs/design/muxcon.md`, section 4.5). Wire format in section 4.6.
- [x] **Telnet and SSH POWER support** — 2026-09-18. The live `[POWER]` /
      `[POWER WARNING]` notice now works on the telnet and SSH listeners, and
      the escape menu has a `p` command for power. The shared interpreter
      (`openmux/server/adapters/power_command.py`) serves the client-listener
      `POWER` command and the telnet/SSH menu, so the wording, permission
      checks, and the v2 group-scoped access check are identical on all three
      surfaces. The telnet/SSH `p` command opens an interactive menu of the
      feeds for the console the user is attached to, numbered one per line
      with an on/off tag: enter a number to toggle that feed, `a` to toggle
      all of the console's feeds, or Enter to leave without a change. Both
      listeners now subscribe to port meta updates so an attached session gets
      the live notice when a feed changes, exactly like the client listener.
- [x] **CLI client POWER support** — 2026-09-18. The OpenMux CLI client
      (`openmux/client/`) gets the same `p` power menu on its escape menu,
      working over both the TCP client protocol and the WebSocket protocol.
      The adapters send `power_query` / `power_switch` OMXCTRL frames and
      store the server's `power_feeds` / `power_switch` reply on
      `last_power_reply`; the console `p` menu renders the numbered feed list
      (number = toggle, `a` = all, Enter = exit, ending with `[EXITING POWER]`)
      and waits for that reply while the background read loop keeps delivering
      stream data, so the live `[POWER]` notice renders before the list
      re-renders — the same order as the telnet/SSH menu.
      The server-side switch keeps the v2 access rules: read-write/admin plus
      the console-group check, run by `PduAdapter.handle_power_frame` on both
      listener wirings (client listener and web console). The client's old
      unimplemented playback `p`/`P` placeholders are removed.
- [ ] **Typed CLI POWER parsing.** Replace the free-text `command.split()`
      handler with structured parsing for the `POWER` command forms.
      Merged into [issue #88](https://github.com/OpenMux/openmux-server/issues/88)
      (client listener command-phase UX), where the structured parsing is one
      of the work items — close this box when that ticket closes.

Deferred (not scheduled): **Power metrics history** — keeping watts/amps and
on-state over time as queryable history with a chart on the Power page. It is
not needed at the moment. If saving statistics becomes wanted, add it back as
a v3 item (the v2 audit log and port-log records are already a partial basis).

### Verification when closing a v3 item

- Add a test for the item's behavior.
- Run `make test`, `make lint`, `make format` and confirm they pass.
- If the item is user-visible, add a CHANGELOG entry that names the config key
      or surface it touches.

## Related references

- PDU power configuration and CLI: [configuration/adapters.md](configuration/adapters.md)
  (section "PDU Power (`power`)").
- Terminology (PDU, outlet, outlet ref, feed): [GLOSSARY.md](GLOSSARY.md).
- Writable Config Editor sections: `security_policy.py`
  (`_KNOWN_CONFIG_SECTIONS`). The `power` section is a known writable section.
- Driver extension points: `DRIVERS` and `DRIVER_INFO` in
  `openmux/server/adapters/pdu.py`.
