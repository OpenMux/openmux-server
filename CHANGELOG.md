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
- **Federated takeover fixes.** The origin node arbitrates a takeover. The taker now writes immediately after a take on a federated port (previously: write-blocked until reconnect). The legacy `FORCE` wire action maps to `TAKE:latest`, so mixed-version peers keep working.
- **MuxCon federation relay is faster** (0a546ef). The local port pump now waits on the port queue instead of polling every 50 ms. The initial retransmit timeout starts at 0.35 s instead of up to one heartbeat interval. There is no wire-protocol change.

### Web console and observability

- The About page shows the logged-in user: username, global permission, and console groups.
- The login page and status page show the messages of the day.
- The Config Editor marks each field with its reload requirement (`live`, `soft`, `full`, `sighup`, `restart`) and shows the read-only `access_default` row.
- The Port Actions sub-view has a "Script health" panel that lists action-script load errors for the whole `actions_dir`.
- The selected port is centered in the sidebar port list after a port switch.
- Logs: repeated connect failures (serial adapter, client `connect_to_port`) no longer print a full stack trace. The error message keeps the detail. Unexpected faults still print tracebacks from the outer loops.

### Suggested upgrade checklist

1. Read "Behavior changes" above. Access resolution is stricter in four ways.
2. Check `authentication.yaml` user `permissions` values and the per-port `read_write_groups` / `read_only_groups` lists. Users who relied on the old shortcuts now need a group list entry or a matching `permissions` value.
3. Optional: set `access_default: deny` in `security.yaml` for a fail-closed posture on no-list ports.
4. Update legacy integer `max_read_write_users` values to `none`, `one`, or `multiple`.
5. Port-action setups: verify `action_ports` grant ids match the script filenames, then check the "Script health" panel (or `GET /api/port-actions/health`) after first start.
6. No data migration is required. Old configs load unchanged.
