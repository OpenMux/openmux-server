OpenMux Configuration Defaults

This document consolidates default values used across the server configuration. It combines defaults declared in the JSON Schema and implicit defaults enforced at runtime by adapters and managers. Where there is a discrepancy, the runtime behavior is authoritative, and the schema/docs should be aligned in subsequent revisions.

Notes
- “Schema default” applies when validating/merging config via the schema.
- “Runtime default” applies when code reads config and fills missing values.
- UI hints: The Config Editor shows many of these defaults as placeholders or badges to guide input, but it does not always auto-fill values.

Top-level sections

server
- server.id: no default (runtime may fall back to hostname in muxcon)
- server.description: no default
- server.control_socket: logs/openmux.sock (env override: OPENMUX_CTL_SOCK)
- server.pidfile: logs/openmux.pid (env override: OPENMUX_PIDFILE)

authentication
- At least one of users, api_keys, public_keys, or pam must be provided.
- PAM (Pluggable Authentication Modules):
  - authentication.pam.enabled: false
  - authentication.pam.service_name: login
  - authentication.pam.groups.admin_group: openmux_admin
  - authentication.pam.groups.write_group: openmux_write
  - authentication.pam.groups.read_group: openmux_read
  - authentication.pam.allow_root: false
  - authentication.pam.allowed_users: unset (optional allowlist; if set, only listed users can log in)

logging (runtime defaults from openmux/server/logging_manager.py)
- logging.log_level: INFO (key name is log_level in code)
- logging.log_dir: logs
- logging.max_log_size: 10485760 (10 MB)
- logging.log_backup_count: 5
- logging.console: no default (console logging is always configured)
- logging.file: no default file path; rotating file handlers are created in log_dir

client_listener (TCP console) (runtime defaults from openmux/server/adapters/client_listener.py)
- client_listener.host: 127.0.0.1
- client_listener.port: 8023
- client_listener.max_connections: 100 (schema default)
- client_listener.connection_timeout: 30 (schema default, seconds)

telnet_listener (per-listener entry; upcoming adapter defaults)
- bind_host: 0.0.0.0 (bind all interfaces unless overridden)
- bind_port: required (no implicit default)
- target: required (no implicit default)
- read_only: false
- acl: empty / missing list means allow all sources
- authentication: not performed (listener relies solely on ACL + network trust)

serial_ports (per-port) (schema defaults, plus adapter runtime behavior)
- baudrate: 9600
- bytesize: 8
- parity: N
- stopbits: 1
- timeout: 1.0 (seconds)
- flow_control: none
- dtr: true
- rts: true
- read_write_users: 1

serial adapter (runtime defaults from openmux/server/adapters/serial.py)
- read_coalesce: true
- read_coalesce_max_delay_ms: 4
- read_coalesce_max_bytes: 65536

loopback_ports (per-port) (schema + runtime)
- buffer_size: 1024 (schema)
- echo_delay: 0.0 (schema)
- max_read_write_users: 5 (schema)
- read_write_users: 1 (legacy, schema)
- sanitize_control: true

command_ports (per-port) (schema defaults)
- shell: false
- max_read_write_users: 1
- cwd, env, interactive, always_buffer: no defaults

muxcon (Unified Federation Adapter) (runtime defaults from openmux/server/adapters/muxcon.py)
- listeners[*]:
  - enabled: true
  - host: 0.0.0.0
  - port: 7822
  - use_tls: false (schema default)
  - require_client_cert: false (schema default)
  - tls_autogen: true (schema default)
  - tls_dir: ~/.openmux/muxcon
  - tls_known_peers_path: <tls_dir>/known_peers.yaml
  - interface, fwmark: unset by default
  - path_pref, path_group: unset by default
- initiators[*]:
  - host: localhost (if unspecified)
  - port: 7822 (if unspecified)
  - options.use_tls: false
  - options.ssl_verify: true
  - options.retry_backoff_initial: 2.0 (seconds)
  - options.retry_backoff_max: 30.0 (seconds)
  - options.retry_short_session_sec: 5.0 (seconds)
  - options.tls_tofu: true (Trust-On-First-Use)
  - options.server_hostname: defaults to host when verification is enabled
- heartbeats & timing:
  - heartbeat_interval: 30.0 (seconds)
  - shutdown_grace_timeout_sec: 5.0
  - context_idle_timeout_sec: 60.0
  - shutdown_ack_flush_ms: 75
- multipath:
  - mpath_primary_stale_sec: 10.0
  - mpath_failover_check_sec: 2.0
  - mpath_strategy: best_pref
  - mpath_preemptive_promote: true
  - mpath_neighbor_idle_drop_sec: 900.0
- retransmissions:
  - retx_initial_ms: 350
  - retx_max_ms: 2000
- federated_cache:
  - federated_cache_enabled: true
  - federated_cache_ttl_sec: 0.0 (disabled by time)
  - federated_cache_path: <tls_dir>/federated_cache.json

web_status (runtime defaults from openmux/server/adapters/web_status.py)
- host: 0.0.0.0
- port: 8080
- enable_http_api: true
- cors_enable: true
- enable_fault_injection: false

web_console (runtime defaults from openmux/server/adapters/web_console.py)
- host: 0.0.0.0
- port: 8081
- ssl_port: 8443
- base_path: /
- respect_forwarded_prefix: true
- enable_ui: true (schema default)
- realm: OpenMux (schema default)
- download_xterm_if_missing: true (schema default)
- enable_probes: true (schema default)
- probes_include_details: false (schema default)
- use_tls: false (schema default)
- tls_autogen: true (schema default)
- tls_dir: ~/.openmux/web_console
- session_ttl_seconds: 28800 (8 hours)
 - login_throttle_max_attempts: 10 (failed logins per IP before temporary lock)
 - login_throttle_window_seconds: 60 (seconds)
 - login_throttle_lock_seconds: 300 (5 minutes)
 - static_dir: ./static (relative to working directory if not set)
 - template_dir: ./templates/web_console (relative to working directory if not set)
- ssl_cert, ssl_key: required if use_tls true and tls_autogen false (runtime enforcement)
- plugins: array of module strings (no default; example includes openmux.server.web_plugins.config_editor)

Behavioral notes
- When use_tls is true and ssl_port differs from port, the server starts:
  - HTTPS on ssl_port (full UI/API/WSS)
  - HTTP on port as redirect-only (308 Permanent Redirect) to the corresponding https URL
  - If ssl_port equals port, single-port mode is used (no separate redirect-only listener)

Discrepancies and follow-ups
- loopback.sanitize_control default is true and is represented in both runtime and the UI. Ensure any future schema updates retain this default for consistency.
- muxcon advanced options (heartbeat_interval, multipath settings, retransmission timers) have runtime defaults but no schema defaults. This is acceptable; we surface them in the UI and defaults inventory. Adding descriptive defaults to the schema could improve validation UX.
- serial_ports UI defaults present 115200 8-N-1 for convenience when creating new entries. The schema’s formal default is 9600; the UI does not auto-apply 115200 unless the user saves that value explicitly.

Examples
- See examples/server_config.yaml for an example that includes the config editor plugin under web_console.plugins.
