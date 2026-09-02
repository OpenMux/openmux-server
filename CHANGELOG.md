# OpenMux Changelog

All notable changes to OpenMux are documented in this file. Each release section lists what a user must know before upgrading.
Keep entries short. Use the same documentation style as the rest of the repository (short sentences, active voice).
Update rule: after committing a user-visible change, add one entry to the current version section. At release, rename `Unreleased` to the version number. See `AGENTS.md` for the workflow.

## [Unreleased — 1.0.3]

Changes since v1.0.2 (2026-08-27).

### Config changes to check before upgrading

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
- **`web_console.motd` and `web_console.logged_in_motd`** are new optional keys. Free-form multiline text; a blank value hides the notice.
  - `motd` shows on the login page (public).
  - `logged_in_motd` shows at the top of the status page for authenticated users.
  - Both apply on soft reload, together with `realm`.

### Behavior changes (no config change required)

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
    - `command`: process spawn failure (binary not found, generic error), non-zero exit code, max restarts reached, or process exited with auto_restart off.
  - The reason clears automatically when the port recovers (reconnect, new process spawn, intentional stop). An intentional `stop()` is a resting state and does not set a reason.
  - Federated peers now see the same reason text: the value travels inside the `PORTS:FEDERATED` catalog and is pushed live over a new lightweight `PORT_STATUS:` control channel (no full re-advertise needed). The value survives a peer restart via the existing federated cache.
  - A federated port also reports when the muxcon link to its origin is down, as a local reason of its own ("MuxCon link to <server_id> is down"). It is set when the last link path dies, shown as long as no path is live, and cleared when the link recovers. The link reason takes precedence over the origin's last reason, because the link outage is the freshest fact. It is local to the node that sees the outage and is never published over the wire.
  - Mixed-version peers: an older peer that does not know the field simply ignores it. No wire-protocol version bump, no config change.
- **RW/RO access-group lists now apply on Soft Reload.** A change to `read_write_groups` / `read_only_groups` on a serial, loopback, command, or TCP-initiator port no longer needs a Full Reload.
  - A Soft Reload (SIGHUP, or the Config Editor "Soft Reload") updates the lists in place on the running port. It does not recreate the port and does not drop connected sessions.
  - The new lists take effect from the next connection. A session already attached keeps the mode it was granted.
  - The Config Editor now tags these fields `soft`, not `full`.
  - No config change.

### Web console and observability

- The About page shows the logged-in user: username, global permission, and console groups.
- The login page and status page show the messages of the day.
- The Config Editor marks each field with its reload requirement (`live`, `soft`, `full`, `sighup`, `restart`) and shows the read-only `access_default` row.
- The serial port editor no longer offers an empty choice for Data bits, Parity, or Stop bits. The selects previously carried a blank, meaningless leading option; they now always show a valid value (defaulting to 8-N-1 when unset).
- Generic Config Editor dropdowns no longer show a pointless blank option when the field has a documented default. The common renderer used to put an empty leading option in every dropdown. This was redundant for fields whose "unset" value means the same as the default (serial Flow, Write slots, TCP-initiator Protocol and Telnet negotiation). Those now preselect the default instead. A dropdown that has no documented default (user and API-key Permissions) keeps its blank option, because leaving that field unset is a different choice.
- The Port Actions sub-view has a "Script health" panel that lists action-script load errors for the whole `actions_dir`.
- Ports show their offline reason: a red "offline" tag on the status page, a Status row in the console info panel, and a "Device health" panel on the Config Editor ports view (checks on load, after save, and on every table edit).
- The reason now covers serial disconnect and failed-connect reasons, not just duplicate-device reasons (issue #62). For serial ports that were connected and then dropped (e.g. device yanked, read error), the status page shows the reason text under the port. Every port type with a reason shows the red "offline" tag with the reason text. The tag updates live when the reason changes; federated ports show the reason their origin advertised. The centered "Port is disconnected on server" banner in the web console also shows the reason on a muted second line when one is available.
- The selected port is centered in the sidebar port list after a port switch.
- Logs: repeated connect failures (serial adapter, client `connect_to_port`) no longer print a full stack trace. The error message keeps the detail. Unexpected faults still print tracebacks from the outer loops.

### Suggested upgrade checklist

1. Read "Behavior changes" above. Access resolution is stricter in four ways.
2. Check `authentication.yaml` user `permissions` values and the per-port `read_write_groups` / `read_only_groups` lists. Users who relied on the old shortcuts now need a group list entry or a matching `permissions` value.
3. Optional: set `access_default: deny` in `security.yaml` for a fail-closed posture on no-list ports.
4. Update legacy integer `max_read_write_users` values to `none`, `one`, or `multiple`.
5. Port-action setups: verify `action_ports` grant ids match the script filenames, then check the "Script health" panel (or `GET /api/port-actions/health`) after first start.
6. Serial setups: check that no two `serial_ports` entries name the same `device`. The later entry is offline until the duplicate is fixed.
7. No data migration is required. Old configs load unchanged.
