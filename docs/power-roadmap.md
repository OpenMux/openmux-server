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
- [ ] **MuxCon outlet federation.** Make power state and per-port feed mappings
      visible across a federation. Today power is strictly per-node: a server
      only sees its own PDUs and its own local ports' feeds.
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
- [ ] **Power metrics history.** Keep watts/amps and on-state over time and
      make it queryable. Show it as a chart on the Power page. (Part of this is
      the audit log and port-log records from v2; this item adds retention and
      the view.)

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
