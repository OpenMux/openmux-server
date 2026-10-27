# 2. The listener session module sits above a byte-transport seam

Date: 2026-10-31. Status: accepted. Relates to GitHub issue #100 and
`docs/design/architecture-review-2026-10-25.md` section 1.

## Context

The Telnet and SSH listeners held the same Client session twice. `scripts/audit_duplication.py` measures the pair
at 37 shared member names, 21 byte-identical bodies, and 30 bodies at or above 0.90 similarity once transport
words are normalised. The shared half is the session: the registry, the read-write gate, the Escape sequence
scanner, the Control menu, the power menu call, the detach, and the read-write notices. The unshared half is the
transport: Telnet speaks raw TCP with IAC negotiation and in-band echo, SSH speaks through `asyncssh` with a pty,
protocol-level authentication, and `process.exit()` as its refusal verb.

An earlier review draft proposed a session module that also owns login and the Port menu. That proposal fails on
the two transports. Login differs below the byte level: Telnet must send `IAC WILL ECHO` around the password
prompt and strip IAC sequences from the line, while SSH authenticates inside the SSH protocol and, at the Port
menu, must echo typed bytes itself because `encoding=None` disables the pty line editor. The refusal mechanics
differ too: Telnet writes a sentence and closes the socket; SSH writes to the process stdout and calls
`process.exit(1)`, and its menu paths split between `exit(0)` (client quit) and `exit(1)` (give-up). A module
above both would need a callback per difference, which is the nine-argument `_dispatch_control_command` bag the
review asked us to delete, rebuilt under another name.

## Decision

Place the seam where the transports genuinely differ: at attach. Each transport adapter keeps everything before
attach — the IP allow-list, the login phase, the Port menu phase, and its own refusal mechanics — and hands the
session module the byte pipes by implementing one small interface:

```python
class SessionTransport(Protocol):
    async def read(self, n: int) -> bytes: ...
    async def readexactly(self, n: int) -> bytes: ...
    def write(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...
    async def close(self) -> None: ...
```

`openmux/server/listener_session.py` owns everything from attach to detach: `ListenerSessionManager` holds the
session registry, the attach and detach, the input pump, the Escape sequence scanner, the Control menu, the
read-write commands, the power menu call, and the read-write notices. It renders the `AccessDecision` text and
frames that `access_decision.py` authors (ADR-0001); it never builds a sentence and never maps a code to text.
`peer_ip`, `username`, and `port_name` enter as construction parameters, not as interface members. A per-transport
`display_name` ("Telnet", "SSH") keeps every operator-visible log line's wording unchanged.

`adapter.sessions` stays the same dict, reachable through a property, because the ConsoleManager and the tests
read it. The ConsoleManager channel contract also stays on the adapter: `register_client_channel` registers the
adapter object (the module registers it as `channel_owner`), and the adapter's pinned one-line delegations
(`send_data_to_client`, `send_control_frame_to_client`, `_resolve_client_meta`) route into the manager.

## Consequences

- The web console is the named third transport, and its shape is now explicit. Its console session can attach
  through the same manager and drive the Control menu commands through the named handlers, but it must implement
  a frame-based variant, not `SessionTransport`: it has no byte pump, and its message plane (modal dialogs, Port
  Actions, operator prompts) is wider than this seam and never attaches to it. `serve`, the pump entry, stays
  byte-only by construction; a frame-based transport uses `attach` plus the command handlers.
- Login policy (attempt caps, lockouts, the HMAC probe) stays per transport. It is the Auth Manager's ground
  (issue #92) and a later candidate; folding it in here would have mixed two refactors.
- `reconcile_ports` stays two implementations on purpose (candidate 3, not yet built): Telnet reconciles through
  a material-config closure, SSH through `_listener_material_diff` and `_material_cfg`. The session module takes
  no part in it beyond `drop_listener_sessions`.
- `listener_common.py` shrinks to a pure leaf: login-line parsing, ACL compile/match, entry validation, port-list
  rendering, and the echoing prompt primitives. `read_prompt_line` must stay there and not move into the session
  module, because `power_command.run_power_menu` imports it and the session module imports `power_command`.
- Two mirrored test files became one shared-behaviour suite (`tests/server/test_listener_session.py`) plus thin
  transport suites. A Control menu bug can now be fixed once.
- The honest cost: the auth/menu phase stays duplicated, about 150 lines per transport at 0.30 similarity. It is
  genuinely different code, and the audit profile keeps its similarity out of the shared score instead of the
  refactor faking a seam it cannot honour.
