# SSH Listener Adapter

## Objective
Expose selected OpenMux ports over real SSH connections, using the same
auth/menu/embedded-login model as `telnet_listener`, but with SSH transport
security (encryption, host key verification, password or public-key auth)
instead of a plaintext socket.

## Requirements
- **Binding**: Each listener entry specifies `bind_host` and `bind_port`.
- **Target Resolution**: `target` accepts the same three formats as
  `telnet_listener` (`local::name`, `<server_id>::name`, bare `name`).
- **ACL**: Per-listener allow list of host/subnet strings, same rules as
  `telnet_listener`. Enforced when the TCP connection is accepted, before
  the SSH handshake completes.
- **Authentication**: `require_auth` defaults to `true` (opposite of
  `telnet_listener`, which defaults to `false`) because SSH exposes a
  standard password/public-key prompt with no extra client tooling needed.
  When `require_auth: false`, any client is accepted without a password or
  key (SSH "none" authentication).
  - Password auth reuses `AuthManager.authenticate_user` / `is_user_locked`
    / `register_auth_failure` / `clear_auth_failures`, identical to
    `telnet_listener`.
  - Public-key auth reuses the shared `AuthManager.public_keys` store.
    Records must include `"ssh"` in `allowed_uses` to be eligible (an
    Ed25519 key registered only for `"client"` or `"muxcon"` will not
    authenticate an SSH session). Only Ed25519 keys are supported.
- **Embedded Login**: The client's SSH username is parsed with the same
  `<username>+<port>` / `<username>:<port>` rules as `telnet_listener`'s
  `login:` prompt (there is no separate login line in SSH — the username
  field itself carries the descriptor).
- **Port Menu**: A listener with `target: '*'` prints a port list and
  prompts `Port: ` after authentication, identical in behavior to
  `telnet_listener`.
- **Read-Only Toggle**: Same `read_only: true` semantics as
  `telnet_listener`, including the one-time in-band warning banner on
  attach.
- **Control Menu (Ctrl+E,c)**: Same in-band Ctrl+E,c control menu as
  `telnet_listener` (`a`/`f`/`s`/`w`/`u`/`i`/`v`/`e`/`.`/`?`; `f` prompts for
  the holder's `client_id` or takes the most recently attached holder on Enter,
  see GitHub issue #61; `u` shows every attached client and its
  read-write/read-only mode, see GitHub issue #48), shared via
  `listener_common.py`'s `EscapeState`/`feed_escape_byte`/`format_rw_notice`.
  A takeover from any adapter type notifies this session in-band.
- **Session Model**: Raw pass-through only. `exec` and subsystem requests
  (e.g. `ssh host command`, SFTP, git-over-ssh) are rejected with an error
  and the channel is closed; only an interactive shell session is served.
- **Host Key**: All `ssh_listener` entries in one server share a single
  auto-generated Ed25519 host key, persisted at
  `~/.openmux/ssh_listener/ssh_host_key` (mirrors `web_console`'s
  self-signed TLS cert autogeneration). Generated once on first start and
  reused afterward.
- **Config Editor Integration**: `ssh_listener` is a first-class section
  editable in the Config Editor UI, subject to the resolved `config_editor`
  writable-section set.

### Example Configuration
```yaml
ssh_listener:
  - name: lab_console_entry
    bind_host: 0.0.0.0
    bind_port: 2022
    target: local::console1
    read_only: false
    require_auth: true
    acl:
      - 192.0.2.10
      - 198.51.100.0/24
  - name: shared_menu
    bind_host: 0.0.0.0
    bind_port: 2023
    target: '*'
    require_auth: true
    acl:
      - 198.51.100.0/24
```
A client picks a port either interactively (`ssh alice@host -p 2023`, then
`Port: console1`) or in one step via the embedded descriptor
(`ssh 'alice+console1'@host -p 2023` or `ssh 'alice:console1'@host -p 2023`).

## Implementation
- `openmux/server/adapters/ssh_listener.py` — `SshListenerAdapter`, built on
  `asyncssh.create_server`. Shares `listener_common.py` (parse_login,
  compile_acl, ip_allowed, render_port_list) with `telnet_listener.py`.
- `openmux/server/auth_manager.py` — adds
  `get_ed25519_pubkeys_for_user_and_use(username, use)` for username-scoped
  public-key lookups (SSH-specific; `telnet_listener` doesn't need this
  since it only does password auth).
- Wired into `factory.py` (`built_ins`), `main.py` (soft-reload bootstrap +
  reconcile), and the known-name lists in `security_policy.py` (adapter type
  `sshlistener`, config_editor section `ssh_listener`), same pattern as
  `telnet_listener`.

## Testing
- `tests/server/test_ssh_listener_adapter.py` covers `validate_config`,
  password auth (success/failure/lockout), public-key auth (matching key,
  non-matching key, wrong username), `require_auth: false` anonymous mode,
  embedded login, menu mode, ACL denial, exec/subsystem rejection, the
  read-only warning banner, and the full Ctrl+E,c control menu (help,
  version/info, request/release read-write and take the write slot, show holders,
  cross-session takeover notification, changing the escape sequence, and
  read-only-listener rejection of `a`/`f`), using real `asyncssh` client
  connections against a loopback server.
- `tests/server/test_listener_common.py` unit-tests the shared
  `EscapeState`/`feed_escape_byte`/`format_rw_notice` helpers (also used by
  `telnet_listener.py`) without any network I/O.
- `tests/server/test_console_manager.py` covers
  `ConsoleManager.take_write_slot`/`get_rw_holders_display`.

## Future Extensions
- Per-listener host key override (currently one shared key for all
  entries).
- Idle timeout and rate limiting per listener.
- Metrics export via `web_status` or Prometheus adapters.

---
*Last updated: 2026-07-30*
