# OpenMux Changelog

All notable changes to OpenMux are documented in this file. Each release section lists what a user must know before upgrading.
Keep entries short. Use the same documentation style as the rest of the repository (short sentences, active voice).
Update rule: after committing a user-visible change, add one entry to the current version section. At release, rename `Unreleased` to the version number. See `AGENTS.md` for the workflow.

## [Unreleased — 1.0.4]

Changes since v1.0.3 (2026-09-17).

### Behavior changes (no config change required)

- **Power outlet switching is scoped to console groups.** A `read-write` user
  may now switch an outlet only if the user can open every console the outlet
  feeds, using the same console-access rules as attach time
  (`read_write_groups` / `read_only_groups` and `access_default`). Switching an
  outlet that feeds a console outside the user's groups now requires `admin`.
  Affected surfaces: the web API `POST /api/power/outlets/{ref}` (now returns
  403) and the client-listener command `POWER <pdu>.<outlet> on|off` (now
  replies with an `ERROR:POWER` line naming the out-of-group consoles). No
  config change is required. An outlet that feeds no console stays switchable
  by any read-write user.

- **PDU power state federates across MuxCon (console ports only).** A federated
  console port now carries the origin node's declared power feeds and its
  last-reported outlet state. A peer renders the same feed badge,
  `/api/ports` power block, `p` power menu, and live `[POWER]` /
  `[POWER WARNING]` terminal notices for a federated port as for a local one;
  the state also survives a peer restart (federated cache). Live outlet
  changes travel in a new `POWER:STATE:<port>` MuxCon control frame (one JSON
  line per change, per outlet ref). The origin's PDU catalog, telemetry, and
  watts are not federated. No config change is required for nodes that
  already run the `power:` section and MuxCon federation. State flows one
  hop (origin to direct peers); a deeper chain does not see live state
  today. See `docs/design/muxcon.md` section 4.5 and
  `docs/configuration/adapters.md` (PDU Power, "Federation").

- **Peers can now switch origin-owned outlets from a session (outlet
  federation, MuxCon only).** A ref declared only on a federated port no
  longer always refuses on a peer. A user who holds an open read-write
  console session on a fed port that the outlet feeds can now switch that
  outlet from every in-session switch surface (web in-session power menu and
  browser `/ws/<port>`, client-listener `POWER <ref> on|off`, telnet/SSH/CLI
  `p` menu) — the same rule as on a local outlet, extended over the
  federation. The switch is anchored on that already-open console session
  (no username crosses the wire), sent in a new `POWER:SWITCH` control
  frame, and executed on the origin node under the session's federated
  mirror client id (audited there). The origin re-checks that the user can
  open every console the outlet feeds *that it knows of* (including
  consoles not federated to the requesting node) and answers with a typed
  `POWER:RESULT` refusal naming any console the requester cannot see. The
  web Power REST page (`POST /api/power/outlets/{ref}`) has no console
  session to anchor on, so it stays read-only for such refs (same typed
  refusal as before). A ref also declared on a local port still switches
  locally on this node. No config change is required; the peer waits on new
  optional `muxcon.power_switch_timeout_sec` (default 5.0) for the origin's
  reply. See `docs/design/muxcon.md` sections 4.5/4.6 and
  `docs/configuration/adapters.md` (PDU Power, "Federation").

### Web console and observability

- **New: PDU power control** (a new user-facing feature). Add a `power:` section to `server.yaml` to manage one or more PDUs (v1 ships the `dummy` driver). A "Power" item appears in the web console sidebar (below the Console section, expandable to list every PDU) when the section is present, with a PDU list and per-PDU outlet pages (on/off, watts/volts/amps, and which consoles each outlet feeds). Console ports declare their power feeds with a new optional `power: ["<pdu>.<outlet>", ...]` key (`serial_ports`, `loopback_ports`, `command_ports`, `tcp_initiator_ports`); the web console header and status page show a power dot per console (green all feeds on, yellow partial, red all off, grey unknown), the console header badge has a per-outlet on/off menu, and any power change messages every attached session of every affected console. A new `POWER` command on the client listener lists or switches outlets (switching needs read-write). The Config Editor gains a "Power" view (`/config-editor?view=power`) that edits the section, and a per-port "Power feeds" field on the Ports view; a Soft Reload reconciles the section. See `docs/configuration/adapters.md` (PDU Power) and `docs/GLOSSARY.md`.

- **POWER control over telnet and SSH.** The telnet and SSH listeners now
  support power (not only the client listener and the web console). Open the
  escape menu (default `Ctrl-E` then `c`) and press `p`: an interactive menu
  lists the feeds of the console you are attached to, numbered one per line
  with an on/off tag. Enter a number to toggle that feed, `a` to toggle all
  feeds of the console, or Enter to leave without a change. The live
  `[POWER]` / `[POWER WARNING]` notice reaches telnet and SSH sessions, and
  the same `read-write`/console-group access rules apply as on every other
  surface. The client-listener `POWER` command keeps its text forms
  (`POWER`, `POWER <pdu>`, `POWER <ref>`, `POWER <ref> on|off`). No config
  change is required.

- **POWER control on the OpenMux CLI client.** The CLI client
  (`openmux client`) now has the same `p` power menu in its escape menu, and
  it works over both the TCP client protocol and the WebSocket protocol. Press
  `p` after attaching: the numbered feed list (a number toggles a feed, `a`
  toggles all, Enter leaves with an `[EXITING POWER]` marker) behaves exactly
  like the telnet/SSH menu, including the live `[POWER]` / `[POWER WARNING]`
  notice. Switching enforces the same `read-write`/console-group access rules
  on the server. The CLI client's old unimplemented playback `p`/`P` escape
  commands are removed. No config change is required.

## [1.0.3]

Changes since v1.0.2 (2026-08-27).

### Config changes to check before upgrading

- **Command adapter ports: 20 per-port keys are removed** (issue #67,
  completed by #83). The `command_ports` surface is reduced to 14 keys; the
  removed keys either became unconditional behavior or are dropped features.
  Remove them from your `server.yaml`:
  - Became unconditional (the behavior is always on; the key is ignored):
    `clean_env`, `intercept_term_queries`, `output_crlf`,
    `enable_output_batching`, `output_batch_size`, `output_batch_timeout`,
    `output_force_flush_timeout`, `enable_batching`, `batch_size`,
    `batch_timeout`, `always_buffer` (see "Federation streams are seeded from
    the origin scrollback" below).
  - Dropped features (no longer possible at all): `auto_restart`,
    `restart_delay`, `max_restarts`, `restart_backoff` (a process never
    restarts itself; press Enter in the console to respawn), `local_echo`,
    `pty_force_raw`, `pty_enter_mode`, `use_pty` (a PTY is enabled by
    `interactive: true`), `spawn_mode` (use `spawn_on_demand: true`).
  For the `use_pty` override case (`interactive: true` plus
  `use_pty: false`), set `normalize_newlines: true` on the pipe instead
  The schema rejects every removed key (`--check-config` and the Config
  Editor name each one and refuse the save); at live load the ConfigManager
  strips the keys with one warning per stale port for this one release, so a
  running server keeps booting until you clean the config.

- **`server.name`, `server.server_id`, and `muxcon.server_id` are no longer
  identity keys** (ticket #74). `server.id` is now the single identity key:
  the MuxCon federation handshake `ID=`, the MuxCon and web-console autogen
  cert CN, the SSO node claim, and the command port banner all use it, and
  the system hostname is the only fallback. Configs that set identity only
  via one of the removed keys (and had no `server.id`) drop to the hostname
  identity on upgrade — set `server.id`. The removed keys are rejected by
  the schema and `--check-config`; at live load the ConfigManager strips
  them with a warning for one release. `server.description` is now the only
  free-form label (it no longer doubles as a muxcon description fallback
  read from `server.name`).

- **`openmux_client_ports` is removed** (ticket #72). The section is no
  longer recognized: the server will not create any ports from it, and a
  config that still carries it fails schema validation (`--check-config`
  exits non-zero). Convert each entry to `tcp_initiator_ports` with an
  `openmux` protocol sub-key, lifting `remote_port`/`api_key`/`username`/
  `password` into `protocol:`:

  ```yaml
  # before
  openmux_client_ports:
    - name: uplink
      host: 10.0.0.9
      port: 8023
      remote_port: "8023"
      api_key: "..."

  # after
  tcp_initiator_ports:
    - name: uplink
      host: 10.0.0.9
      port: 8023
      protocol:
        type: openmux
        remote_port: "8023"
        api_key: "..."
  ```

  The older legacy key `client_initiator_ports` (the section's original
  name; already rejected by the schema and unread by the factory) is no
  longer read by the adapter either. Its remaining fallback branches were
  removed.

- **`serial_ports` is array-only** (ticket #71). The unified adapter dict
  form (`{adapter_type: serial, ports: [...]}`) is no longer accepted.
  It fails schema validation, the server refuses to start with it, and
  a hot reload refuses to apply it. Convert the section in `server.yaml`
  from the dict form:

  ```yaml
  serial_ports:
    adapter_type: serial
    ports:
      - name: console1
        device: "/dev/ttyS0"
  ```

  to the array form used by every other `*_ports` section:

  ```yaml
  serial_ports:
    - name: console1
      device: "/dev/ttyS0"
  ```

- **Fresh Debian installs now generate a random admin password.** The
  packaged `authentication.yaml` template carries only the admin user with a
  `CHANGE_ME_ON_FIRST_BOOT` sentinel; postinst replaces it with a random
  password (24 characters) and stores the plaintext once in
  `/etc/openmux/.initial_credentials` (0600, root:openmux). The password is
  printed on the console and in the postinst message. Log in, change the
  password in the Config Editor, then delete the credentials file.
- **Demo users and default API keys are no longer shipped in the default
  authentication files** (`config/authentication.yaml` and the Debian
  package template). The `user1`/`viewer` users and the `automation-key` /
  `monitoring-key` API keys are gone; only the admin user remains. If you
  relied on the shipped demo users or API keys, define your own in
  `authentication.yaml`.
- **The dev default admin password changed from `password` to `admin`** in
  `config/authentication.yaml` (local development only; existing `config-local/`
  copies keep their old value until re-seeded).
- **The server logs a startup warning while a known default credential is in
  use** (the old `admin`/`password`, the old Debian `admin`/`openmux` hash, or
  the old default API keys), naming the affected user or key.
- **The web console now binds `127.0.0.1` by default** (`web_console.host` in
  `server.yaml`; the packaged Debian config inherits this). Fresh installs no
  longer serve the console on all interfaces. To reach it from other hosts,
  set `web_console.host: 0.0.0.0` in your `server.yaml` (or via the web
  console Config Editor > Server view) and do a full reload; see
  `docs/QUICKSTART.md` section "Reach the web console from other machines".

- **Default configs are generated for the Debian package; `config/` is the single source.** The packaged `server.yaml` is generated at build time from `config/server.yaml` with only the FHS overrides (`logging.file` → `/var/log/openmux/...`, `port_actions.actions_dir` → `/etc/openmux/actions`), and the packaged `security.yaml` now ships verbatim from `config/`. The hand-maintained copies in `debian/package-config/` for those two files are gone, so the packaged defaults can no longer drift from the repo defaults (the old packaged `security.yaml` was missing the `access_default` key). The packaged `authentication.yaml` still differs intentionally (no local automation API keys) and keeps living in `debian/package-config/`. No change to a running install: `/etc/openmux` is seeded only on first install and is never overwritten.

- **Location keys are removed from `server.yaml`** (dirs move to packaging).
  These keys no longer exist and their values are ignored, with one
  deprecation warning per key. Remove them from your config:
  - `server.control_socket` and `server.pidfile`. Runtime files come from
    `OPENMUX_RUN_DIR` (packaged: `/run/openmux`).
  - `logging.log_dir`. Logs come from `OPENMUX_LOG_DIR` (packaged:
    `/var/log/openmux`). `logging.file` stays.
  - `muxcon.federated_cache_path`, `muxcon.listeners[].tls_dir`, and
    `muxcon.listeners[].tls_known_peers_path`. State comes from
    `OPENMUX_STATE_DIR` (packaged: `/var/lib/openmux/muxcon`).
  - `web_console.static_dir` and `web_console.template_dir`. UI assets ship
    inside the package; the server finds them on its own.
- **`OPENMUX_PIDFILE` is deprecated.** `OPENMUX_RUN_DIR` replaces it; the old
  variable still works while a warning is issued. The new variables for
  packaged installs are `OPENMUX_LOG_DIR`, `OPENMUX_RUN_DIR`, and
  `OPENMUX_STATE_DIR`. `OPENMUX_CTL_SOCK` stays as an override. A packaged
  install can set them in `/etc/defaults/openmux` (one `KEY=VALUE` per
  line, no secrets in the file).
  - On the Debian package the unit's `RuntimeDirectory=`/`StateDirectory=`/
    `LogsDirectory=` both create the dirs and act as the server defaults
    (`$RUNTIME_DIRECTORY`/`$STATE_DIRECTORY`/`$LOGS_DIRECTORY`); files land
    in `/run/openmux`, `/var/log/openmux`, and `/var/lib/openmux` without
    touching the conffile.
  - Resolution is `OPENMUX_*` env > `/etc/defaults/openmux` > the systemd
    dir variables > the dev defaults. `/etc/defaults/openmux` ships with
    its three values commented out: it is the override point. Relocate
    files there, not in the unit. `openmuxctl` resolves `OPENMUX_RUN_DIR`
    from the same file and probes `/run/openmux` when no override is set.
  - Dev defaults (no env, no file, no unit) are unchanged: `logs/` next to
    the start dir and `~/.openmux` for per-user state.
- **Web UI templates and static assets now ship with the Python package.**
  A running server serves the console from the installed package; no
  `template_dir`/`static_dir` config and no repository-root `templates/` or
  `static/` directories are needed anymore. The `Dockerfile` no longer
  copies those trees, and the Debian package no longer copies them to
  `/usr/share/openmux`.

- **`security.yaml` gains `access_default`** (issue #58). Value: `allow` (default) or `deny`. It sets the default posture for console ports that declare no group lists.
  - `deny`: a no-list port admits only admin. A mis-created port is locked, not open.
  - `allow`: every authenticated user connects. Mode comes from the user `permissions` value and the write slots.
  - Ports with group lists are unaffected by this key.
  - New denial reason: `denied_by_access_default`.
  - Hot-applies on SIGHUP / soft reload, from the next connection. A bad value stops startup; on reload, the last-known-good policy stays.
  - The Config Editor shows this key as a read-only row. The editor never writes `security.yaml`.
- **`max_read_write_users` is now a tri-value mode** (issue #59, part 1). Serial, loopback, and command ports take `none`, `one` (default), or `multiple`.
  - `multiple` means unlimited concurrent writers.
  - `none` binds everyone to read-only, including admin. Admin bypasses access control, not capacity.
  - Legacy integers still load, with a one-time deprecation warning per port: `0` maps to `none`, `1` to `one`, `>= 2` to `multiple`. Any other value is a hard error at port creation.
  - Update your configs to the mode strings to remove the warning.
- **`tcp_initiator_ports` gains `max_read_write_users`** (issue #60, #59 part 2). Same tri-value as the other local adapters: `none`, `one` (default), or `multiple`.
  - Previously the key was rejected by the config schema, and every TCP initiator port silently behaved as `one`. The default is unchanged; the knob now works.
  - Legacy integers map the same way (`0` → `none`, `1` → `one`, `>= 2` → `multiple`); any other value is a hard error.
  - The Config Editor shows the "Write slots" column on the TCP-initiator table. Changing the value on a running port recreates the port's connection.
- **`logging.file` and `logging.log_dir` are honored** (issue #47). Both keys were previously ignored: the server always wrote logs to `logs/` relative to the working directory.
  - `logging.log_dir` is the base directory for all server logs; `logging.file` is the main aggregate log (default `{log_dir}/openmux.log`).
  - Per-port logs and action-run transcripts move with it: `{log_dir}/ports/*.log`.
  - On the Debian package, logs now go to `/var/log/openmux/` as the packaged config already asked for, instead of `/var/lib/openmux/logs/`.
  - If `logging.file`'s name matches a component log name (`openmux_server.log`, etc.) in the same directory, both write to that one file (written once, rotated once). This is what the packaged default does.
  - Log level, path, rotation and console changes apply on SIGHUP/soft reload or full reload — not live, not on restart.
  - `logging.max_log_size` (bytes) and `logging.log_backup_count` are now honored for all rotating log files (defaults 10 MB / 5, same as before). `logging.console` (default true) now actually disables the stdout handler when false.
- **Port-action scripts load by grant scope** (issue #43). A script file is imported only when its filename (without `.py`) matches a grant id in `action_ports`.
  - The `ACTION` id must equal the filename. A mismatch is reported.
  - Ungranted files (`test_*` scripts, helper modules) never run and appear nowhere.
  - Grant ids that resolve to no file on disk are reported with the ports they are assigned to.
- **`web_console.realm` is removed.** The `realm` key no longer exists in `server.yaml` (it fails schema validation now that the key is gone). The name shown on the web console login/About pages, in the web Basic-Auth dialog, and in the telnet/SSH port menus now derives from the top-level `server` section: `server.description` when set, else `OpenMux <server.id | hostname>`. To change the displayed server name, set `server.description` in `server.yaml` (no reload needed — it re-reads on each render). The CLI client also now shows the server identity in its port listing, and the web Basic-Auth `WWW-Authenticate` header is now correctly quoted/escaped. Remove any `realm:` line from your `web_console:` block.

- **`web_console.motd` and `web_console.logged_in_motd`** are new optional keys. Free-form multiline text; a blank value hides the notice.
  - `motd` shows on the login page (public).
  - `logged_in_motd` shows at the top of the status page for authenticated users.
  - Both apply on soft reload.
- **Serial `dtr` and `rts` now take signal-line policies** (`server.yaml`, issue #63). Values: `none` (default: untouched), `on`, `off`, `presence-on`, `presence-off`.
  - Legacy booleans still load: `true` means `on`, `false` means `off`.
  - Previously these flags were parsed but never applied to the device: the line state was left at the `open()` default. Omitted lines continue to be untouched, so configs without these keys keep their old behavior exactly.
  - `flow_control: rtscts` with a managed `rts` value is now a config error at load, save, and reload (the kernel owns the RTS pin in that mode). Set `rts` to `none` or omit it, or use another flow mode. DTR works under every flow mode.
- **MuxCon federation filters default to deny-all** (ticket #77). A node shares no local port with peers, and accepts no port any peer advertises, until an include list is named. Before this change an empty `include` list meant "share/accept everything", so upgrading silences federation by default. To share or accept anything, set the adapter-level filter in `server.yaml`:
  - `muxcon.advertise_filters.include`: the local ports this node shares with peers.
  - `muxcon.accept_filters`: the peer-advertised ports this node accepts (set an `include`, `adapter_include`, or `server_include` list).
  - `include: ["*"]` is the explicit allow-all and matches the old default. Per-key `public_keys[].advertise_filters` / `public_keys[].accept_filters` still override the adapter-level default for the peers that authenticate with that key.
  - A filter change takes effect on a Soft Reload (or Full Reload). The adapter re-reads the filter keys on reconcile; no restart is needed.
  - The server logs a warning for each direction still in deny mode. Each direction warns once per deny period: setting an include removes the warning, and clearing it again warns once.
  - Fresh installs are unaffected: they configure federation explicitly, and the default configs carry no `muxcon` filters.

### Behavior changes (no config change required)

- **Federation streams are seeded from the origin scrollback; the `always_buffer` key is removed** (issue #83). A peer that opens a federated port now first receives the origin port's `scrollback_size` ring (sent as ordinary data frames, so older peers tolerate it), then the live output follows with no gap or duplication. Local console clients are unaffected: they already replay the ring on attach via `?scrollback=1`; the shared relay queue is now fed by the federation hold reference count instead of the removed `always_buffer` config flag. The queue is drained when the last relay closes, and a client leaving can no longer clear in-flight relay bytes (the old drain ignored active relays, which could gap a federated stream). Set `scrollback_size` on a port to give late federation viewers history; with the default 0 a late viewer simply starts from the live tail. No config change is required; removing the key from `server.yaml` is the only action named above, and it follows the same one-release strip-and-warn as the #67 keys.

- **The server no longer silently falls back to a bundled config.** When `-c`/`--config-dir` name a missing file, the server exits with a hint instead of loading `config/server.yaml` from the source tree. Dev workflow: `make init-config` seeds the gitignored `config-local/` from the pristine `config/` defaults; `make run-server` runs `--config-dir config-local`. The bare `openmux-server` also uses `config-local/` when it exists. Packaged installs are unchanged (`/etc/openmux`, `--config-dir`).
- **Config files are validated against the JSON schemas at startup.** The
  authoritative schemas now ship inside the Python package
  (`openmux/config_schema/`). On every config load (startup and reload),
  `server.yaml`, `authentication.yaml`, and `security.yaml` are checked
  against their schemas. Each violation is logged as ERROR; the server
  still starts (a working deployment never breaks on a newly caught typo).
  Validate offline with
  `openmux-server --check-config -c <dir>/server.yaml` (exit 0 valid, 1
  schema violations, 2 missing or unparseable file). The Config Editor now
  rejects edits that violate the schema. The runtime imports `jsonschema`;
  the Debian package declares the new `python3-jsonschema` dependency.
- **Schema tightenings (both `serial_ports` and `openmux_client_ports` items
  are resolved; see the config changes above).**
  - `serial_ports` is an array of port mappings only. The unified adapter
    dict form (`{adapter_type: serial, ports: [...]}`) no longer passes
    schema validation, and the code now rejects it too (ticket #71).
  - The deprecated `openmux_client_ports` section no longer passes schema
    validation, and the code no longer reads it either (ticket #72).
  - Per-key MuxCon federation filters use the flat
    `public_keys[].advertise_filters` / `public_keys[].accept_filters`
    keys only. The nested `public_keys[].muxcon` wrapper no longer passes
    schema validation. The code still reads the nested form as a fallback
    until it is removed (ticket #73).
  - Write-slot capacity uses `max_read_write_users` only (all port
    types). The legacy `read_write_users` alias no longer passes schema
    validation. The serial code still reads it as a fallback until it is
    removed (ticket #75). Configs that used the alias keep loading but now
    log a schema ERROR at load.

- **Console access resolves with one predictable ladder** (issue #58, part 1). Order: admin bypass, then group lists (a closed boundary), then the user `permissions` value on no-list ports.
  - Review your configs if you relied on the old shortcuts.
  - A user with global `read-write` no longer gets read-write on a list-bearing port where the user is not listed.
  - A user with global `read-only` can no longer attach read-write via the slot-contention path.
  - Group grants now respect `max_read_write_users`. A full port demotes the new writer to read-only instead of rejecting the attach.
  - Loopback ports lose their auto-promotion. They follow the same ladder as any other port.
- **Take control is one audited operation** (issue #59, part 2). All console clients share `take_write_slot`.
  - Web console: a "Take control" button in the viewers menu. Telnet/SSH escape menus and the CLI use the same operation.
  - The taker's entitlement is re-checked at takeover time. A read-only seat can never take.
  - Exactly one holder is demoted: the named target, or the most recently attached other read-write holder. If the taker's promotion fails, the victim is restored. The port is never left with zero writers.
  - The victim sees `taken by <user>` (or `taken by another user`).
  - "Take control" on an empty slot promotes the taker. A named target that matches no holder is refused.
  - Takeovers are audit-logged (`write_slot_takeover`).
- **Targeted write-slot takeover** (issue #61, #59 part 3). Non-web clients can name which holder to take the slot from.
  - CLI `f` and the telnet/SSH `f` menu command now prompt for the holder's `client_id`; Enter keeps the no-target (most-recently-attached) fallback.
  - The `w` holder list and the web "Held by:" lines show the `client_id` as `[<id>] username@ip (rw)`. The id in brackets is the exact value to pass to `f`. Long local ids show their last 8 characters; federated `fed:` ids stay verbatim.
  - A successful targeted take shows `Taken from: <holder>`. A refused take (bad id, no slot, or the origin declining) shows the reason.
  - No config change. The `force_promote` wire frame already carried an optional `client_id`; the CLI, telnet, and SSH consoles now send it.
- **Federated takeover fixes.** The origin node arbitrates a takeover. The taker now writes immediately after a take on a federated port (previously: write-blocked until reconnect). The legacy `FORCE` wire action maps to `TAKE:latest`, so mixed-version peers keep working.
- **MuxCon federation relay is faster** (0a546ef). The local port pump now waits on the port queue instead of polling every 50 ms. The initial retransmit timeout starts at 0.35 s instead of up to one heartbeat interval. There is no wire-protocol change.
- **A serial device is opened by only one port** (issue #57). Two `serial_ports` entries no longer point at the same `device`.
  - The first entry claims the device. Later entries stay listed but offline. The startup log prints the reason.
  - The flag re-checks after every port create, destroy, and soft reload. The port starts again when the duplicate goes away.
  - Fix it by removing the extra port or giving it a different `device`.
- **An uncreatable log directory stops the logging spam** (issue #42).
  - If the log directory (default `logs/`, or `logging.log_dir`) cannot be created, the server emits one warning and continues with console-only logging. It no longer reprints a `PermissionError` traceback on every startup, every config reload, and every port log write.
  - The client behaves the same: it keeps console output and attaches no file handler instead of raising.
  - No config change. A directory that still cannot be created warns once more on the next process start.
- **Offline reason is shown for all port types** (issue #62).
  - Serial ports now report the offline reason in every case, not only for a duplicate device. A serial port reports:
    - `serial`: device not found, pyserial-asyncio missing, device open failure, connection closed (empty read), or read error.
  - And now `tcp_initiator` and `command` ports report the reason as well:
    - `tcp_initiator`: connection refused, timeout, protocol handshake failure, connection closed by remote, read error, or manual disconnect.
    - `command`: process spawn failure (binary not found, generic error), non-zero exit code, or max restarts reached.
  - The reason clears automatically when the port recovers (reconnect, new process spawn, intentional stop). An intentional `stop()` is a resting state and does not set a reason.
  - Federated peers now see the same reason text: the value travels inside the `PORTS:FEDERATED` catalog and is pushed live over a new lightweight `PORT_STATUS:` control channel (no full re-advertise needed). The value survives a peer restart via the existing federated cache.
  - A federated port also reports when the muxcon link to its origin is down, as a local reason of its own ("MuxCon link to <server_id> is down"). It is set when the last link path dies, shown as long as no path is live, and cleared when the link recovers. The link reason takes precedence over the origin's last reason, because the link outage is the freshest fact. It is local to the node that sees the outage and is never published over the wire.
  - Mixed-version peers: an older peer that does not know the field simply ignores it. No wire-protocol version bump, no config change.
- **RW/RO access-group lists now apply on Soft Reload.** A change to `read_write_groups` / `read_only_groups` on a serial, loopback, command, or TCP-initiator port no longer needs a Full Reload.
  - A Soft Reload (SIGHUP, or the Config Editor "Soft Reload") updates the lists in place on the running port. It does not recreate the port and does not drop connected sessions.
  - The new lists take effect from the next connection. A session already attached keeps the mode it was granted.
  - The Config Editor now tags these fields `soft`, not `full`.
  - No config change.
- **Configured serial signal lines are now applied** (issue #63). A `dtr` / `rts` policy drives the pin on connect, and `presence-on` / `presence-off` track the connected-client count (high or low while one or more clients are attached, the opposite level while idle).
- **Client attach/detach now notifies the adapter.** `on_client_count_changed` fires on the main client path for every port type. This makes command `idle_timeout_sec` and tcp_initiator `disconnect_when_idle` actually trigger, in addition to the serial signal lines above. Ports without the hook (loopback, federated) are unaffected.
- **Command ports print a bracketed status line when the process lifecycle changes.** Attached clients now see `[OpenMux:PROCESS_STARTED …]` on spawn (e.g. on-demand start or auto-restart), `[OpenMux:PROCESS_EXITED …]` when the process dies — "restarting in Ns" under `auto_restart`, "press Enter to spawn/respawn" otherwise — and `[OpenMux:PROCESS_STOPPED …]` on an intentional stop (idle timeout, manual stop). With no client attached, nothing is printed — same convention as the existing `PROCESS_NOT_RUNNING` notice.
  - Related fix: a process's final output (e.g. its last line) is now always delivered to clients before the exit notice, even with output batching enabled. Previously the trailing chunk could sit un-flushed until the next input.
  - Related fix: the command port no longer leaks a PTY file descriptor per die→Enter respawn cycle.
- **A clean command-port exit keeps the port online.** When a process finishes with exit code 0 (a normal termination, for example a closing login shell) and `auto_restart` is off, the port now rests in a configured, connected state instead of showing as offline. It resumes from the same Enter-to-respawn as before.
  - A non-zero exit (or a signal) still marks the port offline with the exit reason, as before.
  - No config change.
- **Client listener changes now apply on Soft Reload.** The `client_listener` section is no longer Full-Reload-only.
  - `max_connections` and `connection_timeout` update in place. A rebind is skipped.
  - A change to `host`, `port`, or `enabled` rebinds the socket. Active TCP console sessions are disconnected; clients reconnect.
  - A Full Reload still rebinds. The Config Editor tags the capacity and timeout fields `soft` and the rest `full`.
  - No config change.
- **Serial port configuration is unified internally (issue #65).** A serial port no longer nests its settings in a separate `SerialPortConfig` object next to the port. The port object keeps one flat set of settings, in the same shape as the loopback, command, and TCP-initiator ports. There is one place to read and write each value, so a soft-reload update and the access ladder can no longer see two copies of the same field. No config change. No user-visible behavior change.
- **Ports show a derived readiness (issue #68).** The status page and web console now show three readiness values: green `connected` (running), yellow `idle` (healthy, intentionally not running), and red `offline` (a reason is set). `idle` is derived at the snapshot point from the port's liveliness and the absence of an offline reason; it is not a `PortState` value and is not stored.
  - A command port after a clean code-0 exit with `auto_restart` off shows `idle`, not `offline` and not green. It resumes from the same Enter-to-respawn. A fresh `spawn_on_demand` port before its first spawn also shows `idle`.
  - A TCP-initiator on-demand rest (`connect_on_demand` + `disconnect_when_idle` + last client left) now shows `idle`, not `offline` (regression fix from #62). The intentional disconnect no longer sets `status_message = "Disconnected from host:port"`. A remote-closed or read-error disconnect still sets the reason and stays red.
  - A command port that has exhausted `max_restarts` still shows `offline` with the reason. A command port mid-`auto_restart` (the restart delay before a respawn) shows `offline` with `Restarting in Ns (exit code C)`.
  - A federated port shows the origin's derived readiness, forwarded over the existing #62 `PORT_STATUS:` channel and inside `PORTS:FEDERATED`. A down muxcon link still takes precedence and shows red, because the freshest fact is the link outage.
  - Mixed-version peers and clients ignore the new field. No wire-protocol version bump, no config change.
- **Enter respawn works after a stopped command port.** A port stopped by `idle_timeout_sec` or a manual stop could not be respawned: the "press Enter to respawn" notice showed, but every keystroke was a no-op write (log: `WRITE FAILED`) until the server restarted. A lone Enter (CR) now restarts the process on that path too, and other input on a stopped port re-emits the one-shot `PROCESS_NOT_RUNNING` notice. No config change.
### Web console and observability

- `GET /config-editor` no longer serves a raw JSON dump of the server config when the template cannot be rendered (ticket #80). Both failure modes now answer a 500 text error page: a missing template engine says the server templates are missing (install problem), and a render failure says the editor could not be rendered (template problem; the full traceback goes to the log). The `GET /config-editor/data` API route still returns JSON.
- The Config Editor Validate button no longer always fails on the `password_hash` field. It validated the payload straight from the browser, where every stored secret sits as the `********` sentinel, so the schema pattern `^[0-9a-fA-F]{64}$` could never match. Save already restored the stored values before validating; Validate now runs the same preparation (secret restore and the unmodelled-key merge from ticket #78), so it judges the exact mapping a save would write.
- The About page shows the logged-in user: username, global permission, and console groups.
- The login page and status page show the messages of the day.
- The Config Editor marks each field with its reload requirement (`live`, `soft`, `full`, `sighup`, `restart`) and shows the read-only `access_default` row.
- The Config Editor TCP-initiator port table is narrower. Six advanced columns (Verify TLS, Timeout, Reconnect delay, Batch writes, Batch size, Batch timeout) are no longer shown in the list. They remain available in the per-port Edit dialog and keep their value when you save.
- The serial port editor row gains Flow control, DTR, and RTS selects for the signal-line policies, and shows an inline conflict warning when Flow control is `rtscts` and RTS is not untouched. Saving such a config fails validation with the same error the server reports.
- The Config Editor now accepts the full documented command-port key set. Previously the editor's strict schema rejected the documented process-lifecycle keys (`spawn_on_demand`, `spawn_mode`, `idle_timeout_sec`), the restart keys (`auto_restart`, `restart_delay`, `max_restarts`, `restart_backoff`), and the I/O tuning keys (`clean_env`, `local_echo`, `output_crlf`, `intercept_term_queries`, `pty_force_raw`, `pty_enter_mode`, and the six batch-size/timeout/flags keys). YAML configs carrying these keys load correctly on the server; the editor now lets you see and set them too. No runtime behavior change.
- The serial port editor no longer offers an empty choice for Data bits, Parity, or Stop bits. The selects previously carried a blank, meaningless leading option; they now always show a valid value (defaulting to 8-N-1 when unset).
- **The Config Editor preserves unmodeled schema-valid keys on save (ticket #78).** Previously a save rebuilt each section from the UI's fixed field list and the server wrote the payload verbatim, so any schema-valid key the UI did not render was dropped from `server.yaml` on the first save. A server-side merge (`_merge_preserve_unmodelled`) now runs before `cm.save_config()`. It re-adds a small explicit allowlist of known-unmodelled keys:
  - `muxcon.auth_private_key_path`
  - `web_console.hardware_info_file`, `web_console.sso_trust_header`, `web_console.sso_secret`, `web_console.sso_max_skew_sec`
  The allowlist is deliberately explicit, not a blanket "preserve any key the payload lacks": the editor sends an absent key both when a field is missing *and* when the user clears a modelled field, so the two look identical in the payload. Restricting the preserve list to keys the UI genuinely does not render keeps a deliberate clear a deletion. A new key that the UI does not render must be added to `_PRESERVE_UNMODELLED_KEYS` in `openmux/server/web_plugins/config_editor.py` (a guard test pins the list against the shipped schema).
- **The Config Editor muxcon view now exposes the adapter-level federation filters.** Two JSON-text fields (Advertise filters / Accept filters) appear on the MuxCon section. Empty means deny-all (ticket #77); `{"include":["*"]}` means allow-all. The fields are populated and built by the same code path as the per-key filter fields on `public_keys[]`, so invalid JSON is silently dropped (same behavior as before).
- **The Config Editor initiators table no longer writes `share_ports` / `accept_ports` / `request_ports` (ticket #78).** These three per-initiator keys are dead: read by no code (only a `# Future:` comment in `openmux/server/adapters/muxcon.py`) and documented as "does not exist" in `docs/design/muxcon.md`. The columns were removed from the table; rows that carried them still load, they just no longer render or save.
- Generic Config Editor dropdowns no longer show a pointless blank option when the field has a documented default. The common renderer used to put an empty leading option in every dropdown. This was redundant for fields whose "unset" value means the same as the default (serial Flow, Write slots, TCP-initiator Protocol and Telnet negotiation). Those now preselect the default instead. A dropdown that has no documented default (user and API-key Permissions) keeps its blank option, because leaving that field unset is a different choice.
- The Port Actions sub-view has a "Script health" panel that lists action-script load errors for the whole `actions_dir`.
- **Command ports now report live connected state, and the status snapshot is never frozen.** Previously a newly created command port (e.g. an on-demand port registered before its first spawn) permanently showed `Connected: no` in the Port info panel and a `Port is disconnected on server` banner in the web console even though data flowed fine. The port now exposes `is_connected` (resting or not-yet-spawned = connected; offline only when the process has exited or a spawn failed, with the reason shown as before). The banner and the info-panel Connected row update live on spawn and exit, and the port listing reports the current state instead of a copy captured at registration. No config change.
- Ports show their offline reason: a red "offline" tag on the status page, a Status row in the console info panel, and a "Device health" panel on the Config Editor ports view (checks on load, after save, and on every table edit).
- The reason now covers serial disconnect and failed-connect reasons, not just duplicate-device reasons (issue #62). For serial ports that were connected and then dropped (e.g. device yanked, read error), the status page shows the reason text under the port. Every port type with a reason shows the red "offline" tag with the reason text. The tag updates live when the reason changes; federated ports show the reason their origin advertised. The centered "Port is disconnected on server" banner in the web console also shows the reason on a muted second line when one is available.
- The selected port is centered in the sidebar port list after a port switch.
- The sidebar width is user-resizable: drag the thin strip on its right edge. The width is remembered per browser and restored on the next visit.
- The Console port list supports two optional label parts, set from the port list options menu (the gear on the Console row) and remembered per browser: **Show server** prefixes every port with its server id (`server::port`; local ports use `local`) and sorts the list by server, then by port name; **Show description** appends the port description in a dimmed font. Both default to off; with both on a port reads `server::port (description)`. Display only - which console a click opens is unchanged.
- Logs: repeated connect failures (serial adapter, client `connect_to_port`) no longer print a full stack trace. The error message keeps the detail. Unexpected faults still print tracebacks from the outer loops.

### Suggested upgrade checklist

1. Read "Behavior changes" above. Access resolution is stricter in four ways.
2. Check `authentication.yaml` user `permissions` values and the per-port `read_write_groups` / `read_only_groups` lists. Users who relied on the old shortcuts now need a group list entry or a matching `permissions` value.
3. Optional: set `access_default: deny` in `security.yaml` for a fail-closed posture on no-list ports.
4. Update legacy integer `max_read_write_users` values to `none`, `one`, or `multiple`.
5. Port-action setups: verify `action_ports` grant ids match the script filenames, then check the "Script health" panel (or `GET /api/port-actions/health`) after first start.
6. Serial setups: check that no two `serial_ports` entries name the same `device`. The later entry is offline until the duplicate is fixed.
7. No data migration is required. Old configs load unchanged.
