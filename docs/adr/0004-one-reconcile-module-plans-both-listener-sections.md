# 4. One reconcile module plans both listener sections

Date: 2026-11-02. Status: accepted. Relates to GitHub issues #107 and #105, and supersedes the
"`reconcile_ports` stays two implementations" consequence in
`docs/adr/0002-listener-session-module-sits-above-a-byte-transport-seam.md`.

## Context

The Telnet listener and the SSH listener each applied their own config section. Each held its own plan:
Telnet through a material-config closure, SSH through `_listener_material_diff()` and `_material_cfg()`.
Both read the same entry rule in `listener_common.py`, and both threw its answer away. `build_listener()`
in `listener_session.py` logged one line naming no entry, then returned `None`.

Issue #105 gave the port adapters one contract for this: `prepare_port()` raises the reason an entry is
unusable, `record_config_gap()` keeps it, `prune_config_gaps()` drops it when the file stops asking, and
`reconcile_summary()` moves a refused name out of `added` and into `failed`. The listener adapters joined
none of that. Measured on the code before this ADR, with one entry missing `bind_port`:

- at boot, the factory skipped the entire section. The adapters never implemented `prepare_port()`, so the
  boot rescue in `factory.py` could not tell a bad entry from a bad adapter, found nothing usable, and
  returned no adapter at all. Every listener of that transport stayed closed with no line naming any entry.
- on reload, the summary reported `added: ['bad1']` for a listener that never came up, and `config_gaps`
  stayed empty. The `CONFIG_GAP` block printed nothing, so the server looked healthy while the listener was
  absent.
- on a reload that broke a running listener's entry, the old socket kept serving and the summary said
  `unchanged=1`. That part was already correct, by accident.

ADR 0002 left this open on purpose and named it "candidate 3". The reason it was open then — the session
module had just landed, and folding a second refactor into it would have mixed two changes — no longer holds.

## Decision

Build the module ADR 0002 named. `openmux/server/adapters/listener_reconcile.py` holds
`ListenerReconcileMixin` plus the three helpers it needs: `listener_section_items()` (file order, nameless
entries kept), `entry_label()` (a name, or `<unnamed #N>` by place in the file), and `listener_material()`
(the fields whose change means a listener must be rebuilt). Both adapters inherit the mixin and keep their
own transport work: `_build_listener()`, `_start_single_listener()`, `_stop_single_listener()`,
`validate_config()`, and the sessions.

Five rules follow:

1. A refused entry names itself and names its reason. `build_listener()` raises `ValueError` with the
   validator's sentence instead of logging and returning `None`. `prepare_port()` delegates to it, so the
   factory's boot rescue keeps the entries that are usable and the section still builds.
2. A socket that will not bind is a gap too. The operator cannot tell a mis-typed entry from a port another
   process holds, and the port adapters already report both. The reason reads
   `the listener socket could not be bound (is the port already in use?)`.
3. A spec that failed to bind is dropped from `listeners`. The next reload then treats it as an add and
   binds it. Keeping the dead spec would classify it as `unchanged`, because the file's entry did not
   change, and it would never bind again.
4. A refused change keeps its running listener. That listener appears in `kept_previous`, in no name list,
   and keeps its gap with `kept_previous: true` — the same shape a refused port has.
5. The listener names an adapter reports come from the specs it holds, not from the entries it was
   constructed with. The held specs are the only record of what the last reload left running.

The entry rule returns its reason. `listener_common.py` keeps one owner, `listener_entry_problem()`, and
`validate_listener_entries()` loops over it. A caller that must act on one entry reads the sentence instead
of a bool.

Prepare before destroying, as the port adapters do: build every entry of the new section first, stop only
the listeners a real add or change replaces, then bind. An entry the adapter refuses therefore never costs
a running listener. The SSH transport prepares its host key before the first bind through
`_prepare_listener_start()`; when that fails, each listener of the transport gets one gap naming the reason
rather than raising out of the reload.

## Consequences

- One place owns the plan for listener sections, so a rule fixed once holds on both transports. Issue #107
  existed precisely because the reporting lived twice and landed nowhere.
- A bad entry now costs one listener. At boot the rest of the section comes up, and the `CONFIG_GAP` block,
  the reload answer, and `adapters.<kind>.failed_detail` name the entry and the reason.
- `build_listener()` now raises where it returned `None`, and enforces the 1-65535 port range that
  `validate_config()` already checked at boot. Callers and tests that passed `bind_port: 0` to mean "any
  free port" must name a real port. That range is the documented contract in
  `openmux/config_schema/openmux_config_schema.yaml`, and the Config Editor already refuses 0, so teaching
  the builder to accept it would have widened the contract to save a test line.
- The mixin is a plain mixin, not a base class: it reads `listeners`, `listener_section`, and
  `listener_display` from the adapter that inherits it. A third listener transport must set those three and
  supply the four transport methods. It gains the reporting without copying the plan.
- `listener_common.py` loses its private per-field validators' callers-facing shape: `listener_entry_problem()`
  is the exported rule. Nothing outside it needs the bool form.
- `tests/server/test_listener_config_gaps.py` guards the contract, parametrized over both transports, and one
  of its tests asserts the `CONFIG_GAP` line the operator reads: `telnet_listener/bad1: 'bind_port' is
  required and must be a port number between 1 and 65535`. With the gap record and the `added` stripping
  removed from the module, 20 of its 26 tests fail; with only the stripping removed, the refused entry
  reappears under `added`, exactly as issue #107 reported it.
