# Adapter Configuration Guide

## Overview

OpenMux uses a modular adapter system. Adapters are configured under top-level sections in the YAML config, each providing one or more ports of a specific type. This section documents those adapter-specific sections, their options, and examples.

Top-level adapter sections supported by the server:
- `loopback_ports`: Loopback Adapter (testing)
- `command_ports`: Command Adapter (external processes)
- `tcp_initiator_ports`: TCP Initiator Adapter (outbound TCP/SSL; also connects to a remote OpenMux via `protocol: {type: openmux}`)
- `serial_ports`: Serial Adapter (physical serial devices)

Service adapters (not port lists):
- `client_listener`: Client access server (TCP listener)
- `muxcon`: Federation protocol (server/client)
- `web_status` / `web_console`: Status or console UI with HTTP API

Note: Binding is configured per-adapter; the `server` section is metadata-only (e.g., `id`, `description`).

## Port Access Control (group ACL and server-wide default)

Every port of every type supports the same access-control settings (issues #58 and #59):

- `read_write_groups: [str]` and `read_only_groups: [str]` (per port, alongside `max_read_write_users`): users whose `groups` in `authentication.yaml` intersect a list are admitted at that level; once either list is set, the lists are a **closed boundary** — a user in neither list is denied, even with a global `read-write` permission. `admin` always bypasses the lists.
- `max_read_write_users: none | one | multiple` (per port, write-slot capacity, issue #59): how many users may write at once. `one` (default) gives one driver: the first write-entitled user gets write, later ones attach read-only. `multiple` gives every write-entitled user write. `none` gives no driver at all: everyone attaches read-only, **including admin**. A slot is a resource, not a privilege: admin bypasses access control (groups, `access_default`), not capacity. Legacy integers still load (0 = none, 1 = one, >= 2 = multiple) with a one-time deprecation log line naming the port; any other value is a hard error.
- `access_default: allow | deny` (server-wide, top-level key in `security.yaml`, default `allow`): decides whether a port that declares *neither* list is open at all. Under `deny`, only `admin` can connect to no-list ports (reason `denied_by_access_default`) — a fail-closed posture for mis-created ports.

A full port never rejects: a write-entitled user on a full `one` port demotes to read-only; a `none` port demotes everyone to read-only. Note `none` decides who may *drive*; `access_default` decides who may be *present*. They are orthogonal.

Takeover transfers the one write slot from a current holder to another client. Only a write-entitled client may do it — entitlement is re-checked at the moment of the take, so an attached read-only seat can never take the slot, and a `none` port gives no one a slot to take. The operation demotes exactly one holder and promotes the taker; it is audit-logged (`WRITE-SLOT TAKEOVER` and a `write_slot_takeover` data-log event) and the demoted holder is restored if the taker's promotion fails. When no client holds the slot, a no-target takeover takes the empty slot: the taker is promoted directly, nothing is demoted or announced, and the same data-log event is recorded with an empty victim. A named target that matches no holder is refused. The target of the take is the named holder, or — when none is named — the most recently attached other read-write holder. On a federated port the origin node (not the requesting node) decides, because only the origin sees every holder; the requesting client's own entitlement is still checked on its own node first. Clients surface it as "Take control" (web console viewer menu, or the no-target fallback), the `f` menu command, or the CLI `force_promote` frame. Every console can target a specific holder by `client_id` (web console passes it per-holder; the CLI and the telnet/SSH `f` command prompt for it, with Enter keeping the no-target fallback). The holder lists show the id as `[<id>] username@ip (rw)` so a user can read it off and pass it back. The demoted client sees a notice that names the taker.

Loopback ports follow these same rules; they get no special treatment (the three modes included: a `none` loopback attaches read-only for everyone). Denial reasons surfaced to clients: `no_permissions` (unknown identity), `denied_by_group_acl`, `denied_by_access_default`. See `docs/ARCHITECTURE.md` §17 and `config/security.yaml`.

## Loopback Adapter (`loopback_ports`)

Virtual loopback devices for testing and development.

Supported options per port:
- `name` (required): Unique port name
- `description`: Human-readable description
- `echo_delay`: Seconds to delay echo (default: 0.0)
- `buffer_size`: Internal buffer size (default: 1024)
- `sanitize_control`: Replace control/escape sequences with safe tags (default: true)

Example:
```yaml
loopback_ports:
  - name: test_device
    description: "Test loopback for development"
    echo_delay: 0.1
    buffer_size: 1024
  sanitize_control: true
```

## Command Adapter (`command_ports`)

Runs external commands and exposes their stdin/stdout as a port.

Supported options per port:
- `name` (required): Unique port name
- `description`: Human-readable description
- `command` (required): Command to execute
- `shell`: Run via shell (default: false)
- `cwd`: Working directory
- `env`: Environment variables map
- `interactive`: Preset that enables a PTY plus `normalize_newlines` (default: false). It does not change the `command` string and never adds a shell. Set it for real terminals (shells, editors, TUIs). For scripted or byte-exact ports leave it off and set `normalize_newlines` if you need it (the process then runs on plain pipes).
- `normalize_newlines`: Normalize incoming newlines (default: `interactive`)
- `max_read_write_users`: Write-slot capacity — `one` (default), `multiple`, or `none` (see Port Access Control above)
- `scrollback_size`: Bytes of recent output to keep for replay (default: 0 = off). Local viewers replay on attach (`?scrollback=1`); a federation viewer that attaches later gets the same ring sent as the stream seed (issue #83).
- `read_write_groups` / `read_only_groups`: Console-group access lists (see above)
Lifecycle: on-demand spawn and idle teardown:

The process follows the connected-client count. This mirrors the serial adapter's presence-driven `dtr`/`rts` lines, which are driven by the same client-count event (issue #63):
- `spawn_on_demand`: When true, do not start the process at server startup; spawn only when the first client attaches. The next client after a stop respawns a fresh process. Default: false.
- `idle_timeout_sec`: When the last client disconnects, stop the process after this many seconds. A client that reconnects inside the window cancels the stop. The process is ready to respawn when the next client attaches. **`0` disables idle shutdown** — with `spawn_on_demand` and `idle_timeout_sec: 0` the process runs until server stop; set a positive value (for example `5`) to get teardown. Default: `0`.

```yaml
# Spawn on first client attach; stop 5s after the last client leaves.
command_ports:
  - name: shell_on_demand
    command: bash
    interactive: true
    spawn_on_demand: true
    idle_timeout_sec: 5
```

Note on shells and interactive flags:
- The server does not modify your `command` based on the binary name. If you need an interactive shell, include the appropriate flags yourself (e.g., `bash -i`, `zsh -i`, `fish -i`). This avoids making assumptions about which shell you use and keeps behavior explicit and predictable.

Behavior that is always on (issue #67 removed the config knobs):
- Sanitized environment: the process starts from a minimal environment (`PATH`, `HOME`, `SHELL`, `USER`, `LANG`, `LC_ALL`; `TERM` set to `xterm` if missing) with variables that trigger terminal feature probes stripped (e.g. `TERM_PROGRAM`, `ITERM_SESSION_ID`, kitty/VTE vars). Merge additional values via `env:`.
- Terminal capability queries: XTGETTCAP queries are intercepted and answered as unsupported, so editor probes do not stall the session.
- Newline normalization: output converts LF to CRLF (on a PTY) or normalizes to LF (on pipes); pipe input normalizes to LF when `normalize_newlines` is on.
- I/O batching: output flushes at 1024 bytes, 2 ms idle, or a 1.0 s cap; writes flush at 1024 bytes or 2 ms.
- No automatic restart: the process never restarts itself after it exits. The port shows the exit reason (non-zero exit) or rests (code 0); press Enter in the console to respawn. Supervised daemons belong under systemd; expose their socket on a TCP port instead.

Example:
```yaml
command_ports:
  - name: ssh_server
    description: "SSH via external client"
    command: ssh -i /path/to/key user@host
    shell: false
    interactive: true

  - name: shell_on_demand
    description: "Spawn bash only when a client connects; stop after 60s idle"
    command: bash -i
    interactive: true
    spawn_on_demand: true
    idle_timeout_sec: 60

  - name: telnet_device
    command: telnet 192.168.1.10 23

  - name: custom_script
    command: /opt/scripts/connect_device.sh
    cwd: /opt/scripts
    env:
      TERM: xterm
      LC_ALL: en_US.UTF-8

  - name: fast_tui
    description: "PTY for editors"
    command: bash
    interactive: true        # PTY + newline normalization
```

### Login prompts via Command Adapter

Because the command adapter allocates a real PTY in `interactive` mode, you can expose a system login prompt instead of launching a shell directly. This is useful for local access scenarios, jump boxes, or controlled service consoles.

Security note:
- Treat these like you would a console or SSH: restrict access (authz), use `max_read_write_users: one`, and prefer on-demand spawning with an idle timeout.

macOS (login(1)):
```yaml
command_ports:
  - name: local_login
    description: "macOS login prompt (on demand)"
    command: /usr/bin/login
    interactive: true           # PTY-backed
    spawn_on_demand: true       # start only when a client attaches
    idle_timeout_sec: 60        # stop 60s after last client disconnects
    max_read_write_users: 1     # exclusive session
```

Linux (agetty → login):
```yaml
command_ports:
  - name: local_login
    description: "Linux getty+login (on demand)"
    command: agetty -L - 9600 xterm  # local line, use stdin/stdout, required baud, TERM
    interactive: true
    spawn_on_demand: true
    idle_timeout_sec: 60
    max_read_write_users: 1
```

Linux (direct login(1)) – distro dependent:
```yaml
command_ports:
  - name: local_login
    description: "Direct login(1) on PTY"
    command: /bin/login
    interactive: true
    spawn_on_demand: true
    idle_timeout_sec: 60
    max_read_write_users: 1
```

Alternative via SSH to localhost (reuses SSH policies/keys):
```yaml
command_ports:
  - name: local_ssh_login
    command: ssh -o StrictHostKeyChecking=no localhost
    interactive: true
    spawn_on_demand: true
    idle_timeout_sec: 60
    max_read_write_users: 1
```

## TCP Initiator Adapter (`tcp_initiator_ports`)

Direct TCP or SSL/TLS connections to network services.

Supported options per port:
- `name` (required): Unique port name
- `host` (required): Hostname or IP
- `port` (required): TCP port number
- `use_tls`: Enable TLS (default: false)
- `ssl_verify`: Verify certificates when SSL is enabled (default: true)
- `timeout`: Connection timeout in seconds (default: 10.0)
- `auto_reconnect`: Auto-reconnect when disconnected (default: true)
- `reconnect_delay`: Delay between reconnect attempts (default: 5.0)
- `max_read_write_users`: Write-slot capacity — `one` (default), `multiple`, or `none` (see Port Access Control above)
- `protocol`: Selects the wire protocol for the connection (default: `plain`).
  - `protocol.type`: `plain` (default, raw TCP), `conserver`, or `openmux`.
  - `protocol.telnet_negotiation`: `none` (default) or `strip` (drop telnet IAC sequences), for `plain`.
  - `protocol.username` / `protocol.password`: credentials for `conserver`.
  - `protocol.remote_port`: required by the `openmux` type. Either `protocol.api_key` or
    `protocol.username` + `protocol.password` must be provided to authenticate.

To expose a port on a remote OpenMux server locally, use `protocol.type: openmux` (the
former `openmux_client_ports` section was removed in ticket #72 and converted to this form):

Examples:
```yaml
tcp_initiator_ports:
  - name: network_device
    host: 192.168.1.200
    port: 9999

  - name: secure_device
    host: example.com
    port: 443
    use_tls: true
    ssl_verify: true
    timeout: 15.0

  - name: remote_openmux
    host: remote-openmux.example.com
    port: 8023
    use_tls: true
    timeout: 10.0
    protocol:
      type: openmux
      remote_port: server_console
      api_key: your-api-key
```

## Serial Adapter (`serial_ports`)

Connect to physical serial devices (RS232, USB-to-serial, etc.).

The section is a list of port entries (array-only, like every other
`*_ports` section). Any other shape is a config error at load and hot
reload; no serial port is created for an adapter with a malformed section.

Supported options per port:
- `name` (required): Unique port name
- `description`: Human-readable description
- `device` (required): Device path (e.g., `/dev/ttyUSB0`). One port per device. A later duplicate entry stays listed but offline; the UI shows the reason. Give the later port another path to fix it.
- `baudrate`: Baud rate (default: 9600)
- `bytesize`: 5, 6, 7, 8 (default: 8)
- `parity`: N, E, O, M, S (default: N)
- `stopbits`: 1, 1.5, 2 (default: 1)
- `timeout`: Read timeout seconds (default: 1.0)
- `flow_control`: Flow control mode: `none` (default), `rtscts`, `dsrdtr`, `xonxoff`
- `dtr`: DTR signal-line policy (default: `none`, see Signal lines below)
- `rts`: RTS signal-line policy (default: `none`, see Signal lines below)
- `max_read_write_users`: Write-slot capacity — `one` (default), `multiple`, or `none` (see Port Access Control above); legacy `read_write_users` is still accepted with a warning

Adapter-level performance options:
- `read_coalesce` (default: true): Enable small, time-bounded coalescing of rapid serial read bursts before forwarding to clients. This reduces visual artifacts when devices emit very small chunks quickly (e.g., repeated CR/LF while holding Enter), without adding noticeable latency.
- `read_coalesce_max_delay_ms` (default: 4): Maximum coalescing window in milliseconds for a single flush. Incoming chunks that arrive within this tiny window may be grouped into one payload.
- `read_coalesce_max_bytes` (default: 8192): Upper bound on accumulated bytes per coalesced flush to prevent unbounded buffering.

Example:
```yaml
serial_ports:
  - name: server_console
  description: "Server console"
    device: /dev/ttyUSB0
    baudrate: 115200
    bytesize: 8
    parity: N
    stopbits: 1
    timeout: 1.0
    max_read_write_users: 1

  # Adapter-level knobs (applies to this adapter instance)
  read_coalesce: true                 # reduce small-chunk fragmentation
  read_coalesce_max_delay_ms: 4       # coalesce window (2–6ms typical)
  read_coalesce_max_bytes: 65536       # safety cap
```
#### Signal lines (`dtr` / `rts`)

Each of `dtr` and `rts` takes a signal-line policy. The policy applies on
connect and whenever the connected-client count crosses the threshold between
zero and one or more clients. A line without a policy stays untouched:
OpenMux never drives it.

| Value          | Behavior                                                                 |
| -------------- | ------------------------------------------------------------------------ |
| `none` (default) | Untouched. The line keeps the driver default from `open()` (pyserial asserts DTR at open; RTS stays off unless the driver changed it). |
| `on`           | Fixed high. Applied on connect and held.                                |
| `off`          | Fixed low. Applied on connect and held.                                 |
| `presence-on`  | High while one or more clients are connected, low while idle.           |
| `presence-off` | Low while one or more clients are connected, high while idle.           |

Legacy boolean values are a shorthand: `dtr: true` means `on` (the old
"set on open" behavior), `dtr: false` means `off`.

```yaml
serial_ports:
  - name: modem
    device: /dev/ttyUSB0
    baudrate: 9600
    dtr: presence-on          # DTR high while a user is connected
```

Constraints:
- `flow_control: rtscts` hands the RTS pin to the kernel handshake
  (termios CRTSCTS). A configured `rts` value conflicts with it, and the
  Config Manager rejects the combination at load, save, and reload. DTR is
  not affected by any flow-control mode.
- `dsrdtr` and `xonxoff` do not own either pin: pyserial has no kernel
  flag for DSR/DTR flow control (DSR is polled by applications), and
  XON/XOFF runs in-band. Either mode works with any `dtr` / `rts` policy.
- Note: YAML 1.1 parses unquoted `on` / `off` as booleans. Quote policy
  strings in config files to be safe; the boolean result is the same `on`
  / `off` shorthand.

## Client Listener (`client_listener`)

Accepts TCP connections from OpenMux clients. Handles auth, client sessions, and forwarding to ports. This is the primary way for users/tools to connect.

Supported keys:
- `host` (required): Bind address (e.g., `0.0.0.0` or `127.0.0.1`)
- `port` (required): TCP port to listen on (1-65535)
- `max_connections`: Maximum concurrent clients (default: 100)
- `connection_timeout`: Per-connection inactivity timeout seconds (default: 30)

Example:
```yaml
client_listener:
  host: "127.0.0.1"
  port: 8025
  max_connections: 100
  connection_timeout: 30
```

## MuxCon Federation (`muxcon`)

Federates multiple OpenMux servers. Can both listen for peers and initiate outbound connections. TLS is supported including optional TOFU and pinning.

Top-level keys:
- `heartbeat_interval`: Seconds between HB pings (0 to disable; default: 30)
- `auth_required`: Require the Ed25519 challenge/response from inbound peers (default: true). When false, each started listener logs a warning.
- `listeners`: List of listener configurations
- `initiators`: List of outbound peers
- `advertise_filters`: Adapter-level default filters over which local ports this node shares with peers (a `filter_set`). See below.
- `accept_filters`: Adapter-level default filters over which peer-advertised ports this node accepts (a `filter_set`). See below. `public_keys[]` entries may also carry a per-key `advertise_filters`/`accept_filters` that override these defaults for connections authenticating with that key.

A `filter_set` has six glob lists (case-sensitive, `*` wildcards): `include` / `exclude` (port name), `adapter_include` / `adapter_exclude` (adapter type), and `server_include` / `server_exclude` (origin server id; meaningfully the `accept` direction). An **empty or missing list passes anything for that dimension**, as long as at least one include list in the direction is set; exclude patterns always win over include. **A direction is deny-all (ticket #77) while all three of its include lists are empty:** then it matches no port, so nothing is shared or accepted by default. Set `include: ["*"]` to share or accept everything, or `exclude: ["*"]` to block a direction. Filter changes take effect on a Soft Reload. The server logs a warning for each direction still in deny mode, once per deny period (a warning does not repeat until the direction leaves deny mode and re-enters it).

Identity note:
- The node identity is derived from `server.id` at the top level

`listeners` item keys:
- `enabled`: Enable inbound listener (default: true)
- `host`: Bind address (default: `0.0.0.0`)
- `port`: TCP port (default: 7822)
- `use_tls`: Enable TLS (default: true)
- `ssl_cert`: Path to server certificate (PEM)
- `ssl_key`: Path to server private key (PEM)
- `ssl_ca_cert`: CA for client cert verification (optional)
- `require_client_cert`: Require client certificate (default: false)
- `tls_autogen`: Autogenerate self-signed cert if missing (default: true)
- Generated certs and the TOFU peers file store in `<state dir>/muxcon/`
  (see [../LOCATIONS.md](../LOCATIONS.md))
 - `interface` (alias: `bind_interface`): Bind the listening socket to a specific network interface. macOS uses interface index (IPv4/IPv6); Linux uses `SO_BINDTODEVICE` (requires privileges). If not supported, ignored with a warning.
 - `fwmark` (aliases: `so_mark`, `routing_mark`): Linux-only socket mark applied to the listener. Useful with policy routing; requires CAP_NET_ADMIN/root.

Each `initiators` entry (all keys are at the same level, alongside `host`/`port`):
- `host` (required): Peer host
- `port` (required): Peer port
- `use_tls`: Enable TLS to the peer (default: true)
- `ssl_verify`: Verify peer cert (default: true)
- `ssl_ca_cert`: CA bundle for verification
- `ssl_cert`/`ssl_key`: Client cert/key (mutual TLS)
- `server_hostname`: SNI/hostname for verification (defaults to host)
- `tls_pin_fingerprint`: Exact cert fingerprint to pin (format: `sha256:<hex>`)
- `tls_tofu`: Enable TOFU if no pin (default: true)
 - `bind_host` / `bind_port`: Optional local address/port to bind the outgoing socket to. Useful to influence routing via source IP. `bind_port` defaults to 0.
 - `interface` (alias: `bind_interface`): Prefer this network interface for the outgoing connection regardless of the current DHCP-assigned IP. Platform-specific behavior; see notes below.
 - `fwmark` (aliases: `so_mark`, `routing_mark`): Apply a routing mark on Linux to select policy routing rules. Integer value.

Platform notes for routing selection:
- Linux:
  - `interface`: Uses `SO_BINDTODEVICE`. Requires sufficient privileges (CAP_NET_RAW/CAP_NET_ADMIN or root). If unavailable, the option is ignored with a warning.
  - `fwmark`: Uses `SO_MARK`. Requires CAP_NET_ADMIN or root. Combine with policy routing rules (`ip rule`/`ip route`).
- macOS:
  - `interface`: Binds by interface index using `IP_BOUND_IF` (IPv4) or `IPV6_BOUND_IF` (IPv6). Typically does not require root. If the interface name cannot be resolved, the option is ignored with a warning.
- Other BSDs:
  - `interface`: Attempts `IP_BOUND_IF`/`IPV6_BOUND_IF` if supported; otherwise ignored.
- All platforms: `bind_host`/`bind_port` are portable and can be used when the local address is known and stable.

### Certificate verification (initiators)

Each initiator resolves one verification mode per peer. The first match wins:

1. `ssl_verify: false` — "off": no TLS-level check.
2. `ssl_ca_cert` — "ca": full chain and hostname check against this CA.
3. `tls_pin_fingerprint` — "pin": exact `sha256:<hex>` fingerprint check after the handshake.
4. `tls_tofu: true` (default) — "tofou": pin on first use (Trust-On-First-Use).
5. Otherwise — "system": strict check against the system trust store. This fails for a self-signed peer by design.

In the "pin", "tofou", and "off" modes the TLS-level check is relaxed (`CERT_NONE`). The post-handshake fingerprint gate protects the link. This is what lets a default initiator reach a default listener, whose autogen cert is self-signed.

Behavior:
- ToFU stores `host:port` to `sha256:<hex>` in `<state dir>/muxcon/known_peers.yaml`. The first connect is unverified and is logged at WARNING. After the first connect, a different certificate is rejected.
- A pin requires an exact fingerprint match (case-insensitive).
- A peer that presents no certificate is rejected while a pin or ToFU is active.
- `ssl_verify: false` disables only the TLS-level check. A configured pin or ToFU gate still protects the link.
- The autogen cert has a CN of the node's `server.id` and no SAN. For a strict "ca" or "system" check, set `server_hostname` to the peer's `server.id`.
- To graduate a ToFU peer to an explicit pin, copy the logged fingerprint into `tls_pin_fingerprint`.

The server logs the effective mode once per peer at connect time. An initiator with `use_tls: true` that cannot build its TLS context fails closed: it retries after backoff instead of dialing in plaintext.

Example:
```yaml
muxcon:
  heartbeat_interval: 30
  listeners:
    - enabled: true
      host: "0.0.0.0"
      port: 7822
      use_tls: true
      tls_autogen: true
      # Prefer a specific interface for inbound connections
      interface: "en0"  # macOS; use "eth1" on Linux
  initiators:
    - host: "hub.example.com"
      port: 7822
      use_tls: true
      ssl_verify: true
      tls_tofu: true
      # Explicit pin instead of ToFU (graduate from the logged fingerprint):
      # tls_pin_fingerprint: "sha256:..."
      # For strict checks the name must match the peer's server.id (autogen CN):
      # server_hostname: "hub"
      # Prefer the WAN interface even if IP is DHCP-assigned
      interface: "wan0"         # or `bind_interface`
      # Alternatively, influence routing via source IP
      # bind_host: "192.0.2.10"
      # Bind local port (optional)
      # bind_port: 0
      # On Linux: policy routing mark
      # fwmark: 100
```

## Web Status (`web_status`)

Minimal HTTP server exposing status JSON endpoints: `/api/status`, `/api/clients`, `/api/ports`, `/api/federation`.

Supported keys:
- `host`: Bind address (default: `0.0.0.0`)
- `port`: HTTP port (default: 8080)
- `enable_http_api`: Enable endpoints (default: true)
- `cors_enable`: Enable CORS `Access-Control-Allow-Origin: *` (default: true)

Example:
```yaml
web_status:
  host: "127.0.0.1"
  port: 8081
  enable_http_api: true
  cors_enable: true
```

## Web Console (`web_console`)

Integrated HTTP server for the xterm.js console UI and WebSocket streaming per port. Supports HTTP Basic Auth and optional HTTPS.

Reload behavior: a soft reload applies only `motd` and `logged_in_motd`. All
other `web_console` keys require a full reload. The display name shown on the
login page, About page, and the Basic-Auth dialog comes from the top-level
`server.description` (deriving to "OpenMux <id>" when absent) and re-reads on
each render, so it needs no reload at all.

Supported keys:
- `host`: Bind address (default: `0.0.0.0`)
- `port`: HTTP/HTTPS port (default: 8081)
- `enable_ui`: Serve HTML UI endpoints (default: true)
- `motd`: Public message of the day. Shown on the login page only. Multiline; hidden when blank (default: not set). Pick up changes with a soft reload.
- `logged_in_motd`: Message of the day for authenticated users. Shown at the top of the status page. May hold sensitive text; never shown before login. Multiline; hidden when blank (default: not set). Pick up changes with a soft reload.
- `base_path`: URL prefix for all routes (default: `/`)
- `respect_forwarded_prefix`: Honor `X-Forwarded-Prefix` headers from reverse proxies
- Static assets (xterm, css, js) and templates are inside the python
  package (`openmux/server/webui/`); there are no directory keys
- `session_ttl_seconds`: Browser session lifetime in seconds (default: 28800)
- `enable_probes`: Register health endpoints `/healthz`, `/livez`, `/readyz` (default: true)
- `probes_include_details`: Include extended JSON in probe responses (default: false)
- `plugins`: List of Python modules to load as web console plugins

Failed login lockout (per username + IP) is controlled by `rate_limits.authentication`
in `security.yaml`, shared with all other password-based login paths (TCP, telnet,
SSH listeners). See `config/security.yaml` for its `window_seconds`,
`failure_threshold`, and `base_lock_seconds` keys.

TLS/HTTPS keys:
- `use_tls`: Enable HTTPS and WSS (default: false)
- `ssl_cert`: Path to PEM-encoded server certificate (required if `use_tls` and `tls_autogen: false`)
- `ssl_key`: Path to PEM-encoded server private key (required if `use_tls` and `tls_autogen: false`)
- `tls_autogen`: Autogenerate a self-signed EC (P-256) cert + key on first run if missing (default: true)
- Generated cert/key store in `<state dir>/web_console/` (see [../LOCATIONS.md](../LOCATIONS.md); there is no `tls_dir` key)

Example (self-signed, autogen):
```yaml
web_console:
  use_tls: true
  tls_autogen: true
```

Example (bring-your-own cert/key):
```yaml
web_console:
  host: 0.0.0.0
  port: 8443
  use_tls: true
  tls_autogen: false
  ssl_cert: /etc/ssl/certs/openmux.crt
  ssl_key: /etc/ssl/private/openmux.key
```

## PDU Power (`power`)

Manages power distribution unit (PDU) outlets: on/off state plus watts, volts, and amps when the device reports them. The adapter is portless: outlets are not console ports. The console side maps a port to its feeds with the `power:` key on port entries (see below).

Drivers implement a common interface:
- `list_outlets()` returns the device's own outlet ids. Outlet ids are free strings as reported by the device (for example `1`, or `A1`, `B1`, `C2` on a 3-phase PDU). Config only holds optional per-outlet annotations; a PDU needs no outlet config to be fully listed.
- `read_states()` returns on/off plus watts/volts/amps per outlet. `on: null` = unknown (PDU down, not yet polled).
- `set_state(outlet_id, on)` switches one outlet.

The first (and only) driver in v1 is `dummy`, which simulates a device in memory. Real drivers (for example Raritan, APC) register new keys in the same interface with no other changes.

Supported keys:
- `enabled`: Disable the whole feature when false (default: true)
- `pdus`: List of PDU entries
- `pdus[].name`: PDU name. Unique; no dots or whitespace (it prefixes every outlet ref)
- `pdus[].description`: Free text (default: not set)
- `pdus[].driver`: Driver registry key. v1 provides `dummy`
- `pdus[].poll_interval`: PER-PDU refresh in seconds. `0` = no poll task (reads on demand only). Default: 10
- `pdus[].options`: Free-form object passed to the driver (dummy: `outlets` list and `watts_on`)
- `pdus[].outlets`: Optional per-outlet annotations; each item is `{id: "<outlet id>", description: "..."}`. `id` must match the device's id exactly (a string). Ids that the device does not report raise a warning at startup

Outlet ref = `<pdu_name>.<outlet_id>` (for example `rack1.3`, `phaseA.A1`). This is the single identity used by the CLI, the web API, the Power page, and the status page.

Console-side mapping: add `power: ["<ref>", ...]` to a port entry in any of the four port sections (`serial_ports`, `loopback_ports`, `command_ports`, `tcp_initiator_ports`). Multiple entries = A/B dual feed. A ref to an unknown PDU or outlet raises a warning at startup (not fatal). The web status page and the console header show a power dot (green all feeds on, yellow partial, red all off, grey unknown); the console header badge opens a per-outlet on/off menu (its switches travel as OMXCTRL power frames on the console WebSocket, the same core path as the `p` menu — not the power plugin's REST route).

Any power change (web toggle, CLI, or a poll that detects out-of-band drift) sends a message to every attached session of every console the outlet feeds, and updates the web badges live. A successful manual switch also writes two records: one `POWER CONTROL` audit line in the server log (user, outlet ref, new state, consoles that lose all power), and a `power_control_notice` meta event in each affected console's port data log (the `[POWER]` notice wording, so the event stays in the log with no client attached). Polls that detect out-of-band drift update the badges but do not audit-log; only a user's switch is a control event.

The `POWER` command is available on the client listener (command phase). The telnet, SSH, and OpenMux CLI clients get the escape-menu `p` command, which opens an interactive menu for the console you are attached to (see below). An outlet ref is always `<pdu-name>.<outlet-id>` (for example `rack1.3`; the PDU name is the `pdus[].name` you set, not a device number). Syntax:
```
POWER                       # list every PDU + outlet
POWER rack1                 # list one PDU's outlets
POWER rack1.3               # report one outlet
POWER rack1.3 off           # switch an outlet (needs read-write)
```
Switching an outlet off prints `WARNING:` lines naming the consoles that would lose ALL power, and `NOTE:` lines naming the consoles that stay up on other feeds.

Who may switch is scoped to console groups: switching needs `read-write` or `admin`, plus the entitlement to open every console the outlet feeds (the same console-access rules as attach time: `read_write_groups`/`read_only_groups` and `access_default`). A read-write user whose groups do not cover one of the outlet's consoles sees an `ERROR:POWER` line (CLI, telnet, SSH) or a 403 (web API) naming that console; switching an outlet that feeds any console outside the user's groups requires `admin`. An outlet that feeds no console stays switchable by any read-write user. The check runs on every switch path (web API, client-listener `POWER`, and the `p` power menu on the telnet, SSH, and CLI clients).

Reload behavior: a soft reload re-applies the `power:` section without a restart. Description or annotation edits apply in place. A material change (driver, `poll_interval`, `options`) re-creates that PDU and re-discovers its outlets. Console-side `power:` mapping is read live from the ports and needs no reload work at all.

Config Editor: the "Power" submenu of the Config menu (`/config-editor?view=power`) edits `power.enabled` and the PDU list (name, driver, `poll_interval`, description, `options` as JSON, and optional per-outlet descriptions). The per-port `power:` feed refs are edited as the "Power feeds" field on the Ports view. Apply, then use **Soft Reload** to reconcile the section. The web Power page plugin (`power_monitor`) that serves the standalone `/power` page and its REST endpoints autoloades as long as a PDU adapter is enabled; to keep it off, add `enabled: false` for the module under `web_console.plugins`. The in-session badge and power menu never depend on the plugin.

Example:
```yaml
power:
  enabled: true
  pdus:
    - name: "rack1"
      description: "Rack 1 PDU"
      driver: dummy
      poll_interval: 30
      options:
        outlets: ["1", "2", "3"]
      outlets:
        - id: "3"
          description: "Switch A"
    - name: "phaseA"
      driver: dummy
      poll_interval: 10
      options:
        outlets: ["A1", "B1", "C2"]
```

Example port mapping:
```yaml
serial_ports:
  - name: console1
    device: "/dev/ttyS0"
    power: ["rack1.3", "phaseA.A1"]   # dual feed
```

The telnet, SSH, and CLI-client escape menus have a `p` command for power. It shows an interactive menu of the feeds for the console you are attached to, numbered one per line with an on/off tag (for example ` 1  [on]   rack1.1`). Enter a number to toggle that feed. Enter `a` to toggle all feeds of the console. Press Enter to leave the menu without a change; the menu ends with an `[EXITING POWER]` marker. Switching needs the same access as the `POWER` command above. The CLI client sends the request and switch as OMXCTRL control frames, so it works over both the TCP client protocol and the WebSocket protocol; the unimplemented playback `p`/`P` placeholders it used are removed. Every attached session (CLI client, client listener, telnet, SSH, web console) also gets the live `[POWER]` / `[POWER WARNING]` notice when a feed changes under it. See [../GLOSSARY.md](../GLOSSARY.md) for the terms PDU, outlet, outlet id, outlet ref, and feed.

Federation: when a port federates across MuxCon, its declared power feeds and the origin node's last-reported outlet state travel with the port (a `POWER:STATE` control frame carries live changes). A peer renders the feed badge, the `/api/ports` power block, and the `p` power menu for a federated port as it does for a local one, and the state survives a peer restart via the federated cache. The peer shows the origin's state only (no origin PDU list, telemetry, or watts). Federated feeds are named with the **global ref** form `<origin_server_id>::<pdu>.<outlet>` (the same `::` origin convention as the federation display strings), so an outlet of the same name on two nodes never collides: a bare ref always means this node's own outlet, and a federated feed is switched by its global name (for example `POWER peerO::rack1.1 off`). Switching an origin-owned outlet is possible from a peer when the user holds an open read-write console session on a fed port that the outlet feeds — the switch relays to the origin node over `POWER:SWITCH` / `POWER:RESULT` and executes there (web in-session power menu, `POWER` command, telnet/SSH/CLI `p` menu), like a local outlet. The origin refuses when the user cannot open every console the outlet feeds that it knows of, and names those consoles in the refusal. The web Power REST page (no console session to anchor on) stays read-only for such refs, replying with one typed error naming the origin node. Feed-list config edits on the origin reach peers at the next re-advertise. See [../design/muxcon.md](../design/muxcon.md) section 4.5 (state) and section 4.6 (switch relay) for the wire format and the one-hop state limit.

Follow-on work beyond v1 (real drivers, more listener surfaces, metrics history, multi-hop live-power relay) is tracked in [../power-roadmap.md](../power-roadmap.md).

## Configuration Validation

At startup, each adapter validates its configuration and the server reports clear errors for:
- Missing required parameters
- Invalid parameter values
- Unreachable devices or hosts
- Permission issues

## Migration

This document describes only the current adapter configuration. Legacy formats (per-port `adapter:` entries and older field names) are not covered here and should be considered deprecated.

## Best Practices

1. Use descriptive `name` and `description` values for each port
2. Prefer secure transports: enable `ssl` and keep `ssl_verify` on where applicable
3. Right-size writer limits: set `max_read_write_users` appropriately for each port
4. Start simple: use `loopback_ports` to validate client flows
5. Document custom commands: include `cwd`/`env` and comments for complex setups

Debugging and profiling:
- PTY read profiling logs are emitted at debug level. Enable debug logging to diagnose latency; keep disabled in production to reduce log volume.
