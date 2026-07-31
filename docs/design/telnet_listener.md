# Telnet Listener Adapter

## Objective
Provide a lightweight listener that exposes selected OpenMux ports over simple Telnet-compatible TCP sockets. This enables traditional console-server tooling (plain telnet, netcat, scripts) to attach without running the OpenMux client stack, while remaining compatible with both local and muxcon-hosted ports.

## Requirements
- **Binding**: Each listener entry specifies `bind_host` and `bind_port`.
- **Target Resolution**: `target` accepts three formats:
  - `local::name` – resolve only against the local PortManager.
  - `<server_id>::name` – resolve via muxcon federation to a specific remote server.
  - `name` – attempt local first, then muxcon inventory; if multiple muxcon matches exist, reject with an informative error.
- **Port Compatibility**: Works for local adapters and muxcon federated ports. Reuses existing attach APIs; no special-case logic per adapter type.
- **ACL**: Per-listener allow list of host/subnet strings (exact IP or CIDR). Missing ACL means "allow all". Enforcement happens before any port interaction.
- **Authentication**: Access control relies on the network ACL plus the optional read-only flag. A listener can also set `require_auth: true` to prompt for an OpenMux username and password (via `AuthManager`) before it attaches a session.
- **Port Menu**: A listener with `target: '*'` does not attach to a fixed port. Instead, it prints a port list and prompts `Port: ` so the user can pick a target. Pair `target: '*'` with `require_auth: true`.
- **Embedded Login**: When `require_auth` is set, the `login:` prompt accepts `<username>+<port>` or `<username>:<port>` (a literal `+` always wins over `:`; a doubled `::` is treated as part of the target, not a delimiter). On a fixed-target listener the embedded port is ignored, because the target is fixed. On a `target: '*'` listener, a valid embedded port skips the port menu and attaches directly.
- **Read-Only Toggle**: Each listener may declare `read_only: true` to forward data from the OpenMux port to the Telnet client while ignoring client input. On attach, a read-only session (config-forced, or granted read-only because no read-write slot was free) gets a one-time in-band warning: `[WARNING: console is in read-only mode]`.
- **Control Menu (Ctrl+E,c)**: Any session can press Ctrl+E then `c` to open an in-band control menu, matching the CLI client's own escape sequence. Commands: `a` request read-write, `f` force-take read-write (demotes other holders), `s` release read-write (switch to read-only), `w` show who holds read-write, `i` show session info, `v` show server version, `e` change this session's escape sequence, `.` disconnect, `?` show the menu. On a `read_only: true` listener, `a` and `f` are rejected with a clear message; `w`/`i`/`v`/`?` still work. A force-take on another adapter type (TCP CLI, web console) also notifies this session with `[Your read-write access was taken by another user]`.
- **Minimal Telnet Handling**: Treat the socket as raw TCP. Do not emit Telnet negotiations; echoed data is controlled by the underlying port.
- **Failure Messaging**: On ACL failure, missing target, or ambiguity, send a short banner (e.g., `Port console1 unavailable`) and close the socket.
- **Config Editor Integration**: `telnet_listener` is a first-class section that can be modified in the Config Editor UI, subject to the resolved `config_editor` writable-section set.

### Example Configuration
```yaml
telnet_listener:
  - name: lab_console_entry
    bind_host: 0.0.0.0
    bind_port: 20023
    target: local::console1
    read_only: false
    acl:
      - 192.0.2.10
      - 198.51.100.0/24
  - name: remote_pass_through
    bind_host: 127.0.0.1
    bind_port: 20024
    target: labmux::core_router
    read_only: true
    acl:
      - 127.0.0.1/32
  - name: shared_menu
    bind_host: 0.0.0.0
    bind_port: 20025
    target: '*'
    require_auth: true
    acl:
      - 198.51.100.0/24
```

## Implementation Plan

### 1. Adapter Skeleton
- Create `openmux/server/adapters/telnet_listener.py` deriving from `BaseGenericAdapter`.
- Adapter capabilities: `AdapterCapability.ACCEPTS_CLIENTS` (no ports).
- Adapter config schema: list of listener dicts with fields described above.
- Start-up: spawn one asyncio TCP server per entry; keep handles for graceful shutdown.

### 2. Connection Flow
1. Accept TCP client; fetch peer IP.
2. Evaluate ACL list (support exact IP and CIDR). Use `ipaddress` module for parsing; cache compiled networks per listener.
3. On acceptance, resolve `target`:
   - If `local::`, query PortManager for the port name.
   - If `<server>::`, ask muxcon/federation proxy for that server/port pair.
   - If bare name, attempt local; on failure query muxcon for all matches. If >1 remote match, send error and disconnect.
4. If resolution fails, send banner and close.
5. Acquire session/reader-writer streams using existing port attach APIs (same path the client listener uses). Ensure cleanup on disconnect.
6. Bridge bytes:
   - From Telnet client → port writer unless `read_only` is true.
   - From port reader → Telnet socket until EOF.
   - Apply backpressure via flow control (pause reading when writer is busy).
7. On either side closing, send summary banner (optional) and close the other side.

### 3. Config & Security Wiring
- Update `security.yaml`'s known-name lists in `security_policy.py`: add `telnetlistener` to the known adapter types, and `telnet_listener` to the known config_editor sections (both already covered by the default `allowed: ["*"]`).
- Extend `ConfigManager` parser and schema checks to accept `telnet_listener` alongside other listener sections.
- Add `telnet_listener` to the Config Editor metadata so fields can be edited (with validation for IP/CIDR, port range, etc.).
- Document that security enforcement is ACL-only; highlight lack of OpenMux auth in README/docs.

### 4. UI: Read-Only Button/Flag
- Config Editor form for each listener includes a toggle labeled "Read-only session" which sets `read_only: true`.
- Server side enforces by dropping client input silently (log at DEBUG the discard events).

### 5. Observability & Logging
- Emit structured logs on:
  - Listener started/stopped.
  - ACL denies (with remote IP, listener name).
  - Target resolution failures or ambiguities.
  - Sessions established/terminated (duration, bytes transferred).
- Expose metrics counters via existing instrumentation hook if available (optional for first pass but keep stubs).

### 6. Testing
- Unit tests for ACL parsing and enforcement (IPv4, IPv6, CIDR, malformed entries).
- Tests for target resolution logic (local only, muxcon only, ambiguous remote).
- Integration-style async tests using loopback sockets plus a fake port implementing simple echo behavior.
- Config validation tests verifying read-only default, required fields, and Config Editor serialization.

### 7. Documentation Updates
- README: add `telnet_listener` section to security policy example and configuration overview. Explicitly state that no authentication occurs and that ACL/read-only controls exist.
- `docs/CONFIG_INVARIANTS.md`: include `telnet_listener` in the canonical section list and describe sidecar/security implications.
- New `docs/design/telnet_listener.md` (this file) referenced from architecture index.

## Future Extensions
- Global ACL defaults inherited by listeners unless overridden.
- Idle timeout and rate limiting per listener.
- Metrics export via web_status or Prometheus adapters.

## Open Questions / Follow-Ups
- Confirm muxcon API surface for targeted attachments (ensure we can request a remote port session without user auth).
- Decide whether connection banners should be configurable (message of the day, disclaimers).
- Evaluate IPv6 binding needs (include in first pass if quick, otherwise document as a limitation).

---
*Last updated: 2026-07-30*
