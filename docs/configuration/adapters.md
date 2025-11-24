# Adapter Configuration Guide

## Overview

OpenMux uses a modular adapter system. Adapters are configured under top-level sections in the YAML config, each providing one or more ports of a specific type. This section documents those adapter-specific sections, their options, and examples.

Top-level adapter sections supported by the server:
- `loopback_ports`: Loopback Adapter (testing)
- `command_ports`: Command Adapter (external processes)
- `tcp_initiator_ports`: TCP Initiator Adapter (outbound TCP/SSL)
- `serial_ports`: Serial Adapter (physical serial devices)
- `openmux_client_ports`: OpenMux Client Adapter (connect to another OpenMux)

Service adapters (not port lists):
- `client_listener`: Client access server (TCP listener)
- `muxcon`: Federation protocol (server/client)
- `web_status` / `web_console`: Status or console UI with HTTP API

Note: Binding is configured per-adapter; the `server` section is metadata-only (e.g., `id`, `description`).

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
- `interactive`: Enable interactive mode; implies PTY by default (default: false)
- `use_pty`: Allocate a PTY for the process (default: same as `interactive`)
- `always_buffer`: Buffer output even with no clients (default: `interactive`)
- `normalize_newlines`: Normalize incoming newlines (default: `interactive`)
- `local_echo`: Echo written data back to clients (default: false)
- `output_crlf`: Convert LF to CRLF on output (default: true)
- `clean_env`: Start with a minimal sanitized environment (default: true). Keeps `PATH`, `HOME`, `SHELL`, `USER`, `LANG`, `LC_ALL`; sets `TERM` to `xterm` if missing; strips variables that trigger terminal feature probes (e.g., `TERM_PROGRAM`, `ITERM_SESSION_ID`, kitty/VTE vars). Merge additional values via `env:`.
- `intercept_term_queries`: Intercept XTGETTCAP-style terminal capability queries and respond with “unsupported” to avoid editor probe timeouts (default: true).
- `auto_restart`: Restart the process if it exits (default: false)
- `restart_delay`: Initial delay before restart in seconds (default: 1.0)
- `max_restarts`: Max restart attempts (0 = unlimited, default: 0)
- `restart_backoff`: Exponential backoff factor (default: 1.0)
- `max_read_write_users`: Max concurrent writers (default: 1)
Lifecycle and on-demand options:
- `spawn_on_demand`: When true, do not start the process at server startup; spawn only when the first client attaches. Default: false.
- `spawn_mode`: Alternative to `spawn_on_demand`. Supported values: `shared_eager` (default) or `shared_on_demand` (equivalent to `spawn_on_demand: true`).
- `idle_timeout_sec`: When the last client disconnects, stop the process after this many seconds. `0` disables idle shutdown. Default: `0`.

Note on shells and interactive flags:
- The server does not modify your `command` based on the binary name. If you need an interactive shell, include the appropriate flags yourself (e.g., `bash -i`, `zsh -i`, `fish -i`). This avoids making assumptions about which shell you use and keeps behavior explicit and predictable.

Performance and latency tuning:
- `enable_output_batching`: Batch PTY output before forwarding to clients (default: true).
- `output_batch_size`: Max batched bytes before an immediate flush (default: 1024).
- `output_batch_timeout`: Inactivity timeout in seconds to flush partial batches (default: 0.002 = 2ms).
- `output_force_flush_timeout`: Absolute cap in seconds to force-flush long-running batches (default: 1.0).
- `enable_batching`: Batch writes to the subprocess stdin (default: true).
- `batch_size`: Max write buffer size before flush (default: 1024).
- `batch_timeout`: Inactivity timeout in seconds for write flushes (default: 0.002 = 2ms).

Example:
```yaml
command_ports:
  - name: ssh_server
    description: "SSH via external client"
    command: ssh -i /path/to/key user@host
    shell: false
    interactive: true
    always_buffer: true

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
    description: "Low-latency PTY for editors"
    command: bash
    interactive: true        # implies PTY
    clean_env: true          # minimal env + TERM=xterm
    intercept_term_queries: true
    # Output batching (PTY -> clients)
    enable_output_batching: true
    output_batch_size: 2048
    output_batch_timeout: 0.002   # 2ms
    output_force_flush_timeout: 1.0
    # Write batching (clients -> PTY)
    enable_batching: true
    batch_size: 1024
    batch_timeout: 0.002
```

### Login prompts via Command Adapter

Because the command adapter allocates a real PTY in `interactive` mode, you can expose a system login prompt instead of launching a shell directly. This is useful for local access scenarios, jump boxes, or controlled service consoles.

Security note:
- Treat these like you would a console or SSH: restrict access (authz), use `max_read_write_users: 1`, and prefer on-demand spawning with an idle timeout.

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
    clean_env: true             # TERM=xterm default
    output_crlf: true
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
    clean_env: true
    output_crlf: true
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
```

## Serial Adapter (`serial_ports`)

Connect to physical serial devices (RS232, USB-to-serial, etc.).

Supported options per port:
- `name` (required): Unique port name
- `description`: Human-readable description
- `device` (required): Device path (e.g., `/dev/ttyUSB0`)
- `baudrate`: Baud rate (default: 9600)
- `bytesize`: 5, 6, 7, 8 (default: 8)
- `parity`: N, E, O, M, S (default: N)
- `stopbits`: 1, 1.5, 2 (default: 1)
- `timeout`: Read timeout seconds (default: 1.0)
- `flow_control`: Flow control mode (default: "none")
- `dtr`: Set DTR on open (default: true)
- `rts`: Set RTS on open (default: true)
- `max_read_write_users`: Max concurrent writers (default: 1; legacy `read_write_users` is still accepted with a warning)

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

## OpenMux Client Adapter (`openmux_client_ports`)

Connect to a remote OpenMux server and expose a remote port locally.

Authentication: either `api_key` or `username` + `password` is required.

Supported options per port:
- `name` (required): Unique port name
- `host` (required): Remote OpenMux host
- `port` (required): Remote OpenMux TCP port
- `remote_port` (required): Port name on the remote OpenMux server
- `api_key`: API key for authentication
- `username`: Username for password auth
- `password`: Password for password auth
- `use_tls`: Enable TLS encryption (default: false)
- `timeout`: Connect/auth timeout in seconds (default: 10.0)
- `auto_reconnect`: Auto-reconnect when disconnected (default: true)
- `reconnect_delay`: Delay between reconnect attempts (default: 5.0)

Example:
```yaml
openmux_client_ports:
  - name: remote_openmux
    host: remote-openmux.example.com
    port: 8080
    remote_port: server_console
    api_key: your-api-key
    use_tls: true
    timeout: 10.0
```

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
- `listeners`: List of listener configurations
- `initiators`: List of outbound peers

Identity note:
- The node identity is derived from `server.id` at the top level

`listeners` item keys:
- `enabled`: Enable inbound listener (default: false)
- `host`: Bind address (default: `0.0.0.0`)
- `port`: TCP port (default: 7822)
- `use_tls`: Enable TLS (default: false)
- `ssl_cert`: Path to server certificate (PEM)
- `ssl_key`: Path to server private key (PEM)
- `ssl_ca_cert`: CA for client cert verification (optional)
- `require_client_cert`: Require client certificate (default: false)
- `tls_autogen`: Autogenerate self-signed cert if missing (default: true)
- `tls_dir`: Directory for generated certs and TOFU file (default: `~/.openmux/muxcon`)
- `tls_known_peers_path`: Override path to known_peers file
 - `interface` (alias: `bind_interface`): Bind the listening socket to a specific network interface. macOS uses interface index (IPv4/IPv6); Linux uses `SO_BINDTODEVICE` (requires privileges). If not supported, ignored with a warning.
 - `fwmark` (aliases: `so_mark`, `routing_mark`): Linux-only socket mark applied to the listener. Useful with policy routing; requires CAP_NET_ADMIN/root.

Each `initiators` entry:
- `host` (required): Peer host
- `port` (required): Peer port
- `options`: TLS and verification options

`initiators.options` keys (selected):
- `use_tls`: Enable TLS to peer
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
      options:
        use_tls: true
        ssl_verify: true
        tls_tofu: true
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

Supported keys:
- `host`: Bind address (default: `0.0.0.0`)
- `port`: HTTP/HTTPS port (default: 8081)
- `enable_ui`: Serve HTML UI endpoints (default: true)
- `realm`: HTTP Basic-Auth realm name (default: "OpenMux")
- `base_path`: URL prefix for all routes (default: `/`)
- `respect_forwarded_prefix`: Honor `X-Forwarded-Prefix` headers from reverse proxies
- `static_dir`: Directory for static assets (xterm, css, js)
- `template_dir`: Directory for Jinja2 templates (optional)
- `session_ttl_seconds`: Browser session lifetime in seconds (default: 28800)
- `login_throttle_max_attempts`: Failed login attempts allowed per IP within the window
- `login_throttle_window_seconds`: Rolling window for login throttling (seconds)
- `login_throttle_lock_seconds`: Duration of lockout once the attempt limit is exceeded (seconds)
- `enable_probes`: Register health endpoints `/healthz`, `/livez`, `/readyz` (default: true)
- `probes_include_details`: Include extended JSON in probe responses (default: false)
- `plugins`: List of Python modules to load as web console plugins

TLS/HTTPS keys:
- `use_tls`: Enable HTTPS and WSS (default: false)
- `ssl_cert`: Path to PEM-encoded server certificate (required if `use_tls` and `tls_autogen: false`)
- `ssl_key`: Path to PEM-encoded server private key (required if `use_tls` and `tls_autogen: false`)
- `tls_autogen`: Autogenerate a self-signed EC (P-256) cert + key on first run if missing (default: true)
- `tls_dir`: Directory for generated cert/key (default: `~/.openmux/web_console`)

Example (self-signed, autogen):
```yaml
web_console:
  use_tls: true
  tls_autogen: true
  # Optional custom location for generated files
  tls_dir: ~/.openmux/web_console
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
