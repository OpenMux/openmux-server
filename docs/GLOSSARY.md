# OpenMux Glossary

This is the approved word list for OpenMux docs, code comments, and UI text. It
supports the Simplified Technical English (ASD-STE100) rule "one term, one
concept". Use the term in the **Use** column. Do not use the words in the
**Do not use** column for the same concept.

## Core Runtime Concepts

| Term | Meaning | Do not use |
|---|---|---|
| Port | An addressable serial, loopback, command, or TCP-initiator endpoint. | channel, line, socket (for the logical endpoint) |
| Adapter | A plugin that gives one or more ports, or accepts connections, for one config section. | driver, module, provider |
| Plugin Registry | The lookup table that maps a config section name to an adapter class. | registry (alone), plugin map |
| Capability | A flag that says what an adapter can do (for example `PROVIDES_PORTS`). | feature, ability |
| Client | A user session connected to one or more ports through the CLI, WebSocket, or web console. | user (for the connection object), session (for the client itself) |
| Session | A single active connection between a client and a port. | — |
| Server | The OpenMux daemon process (`openmux/server/main.py`, class `OpenMuxServer`). | daemon, service |
| Config Manager | The component that loads and gives access to the YAML config (`ConfigManager`). | config loader |
| Auth Manager | The component that holds user and API key auth data (`AuthManager`). | authenticator |
| Port Manager | The component that tracks all active ports and routes data to clients (`PortManager`). | — |
| Console Manager | The component that connects the interactive console protocol to `PortManager` (`ConsoleManager`). | management console (use "console" alone) |
| Dynamic Port Manager | The per-adapter helper that creates and destroys ports at runtime (`DynamicPortManager`). | — |

## Port Lifecycle

| Term | Meaning | Do not use |
|---|---|---|
| State | The current lifecycle stage of a port (see `PortState`). | status (for lifecycle stage) |
| Configured | Port is defined in config; `start()` did not run yet. | — |
| Creating | `start()` is running; the port is not ready yet. | — |
| Active | The port is ready and accepts data. | connected, online |
| Degraded | The port exists but works with reduced function (for example, a disconnected device that retries). | — |
| Destroying | `stop()` is running; cleanup is not done yet. | — |
| Destroyed | The port is stopped. Do not reuse the instance. | removed, dead |

## Adapter Types

| Term | Meaning | Do not use |
|---|---|---|
| Serial port | A port that connects to a physical or virtual serial device. | COM port (except in Windows-specific text) |
| Loopback port | A port that echoes data back to the client for testing. | test port |
| Command port | A port backed by a local shell command or process. | shell port |
| TCP initiator port | A port that makes an outbound TCP connection to a remote host, with a pluggable protocol handler (plain, conserver, openmux). | client initiator (legacy name), openmux client port (legacy name) |
| Client listener | The adapter that accepts inbound console-protocol client connections. | — |
| Telnet listener | The adapter that accepts inbound Telnet client connections to a port. | — |
| Web console | The adapter that gives the HTML5 web interface and its plugins (for example the Config Editor). | web UI, web interface (use "web console") |
| Web status | The adapter that gives a lightweight HTTP status/API endpoint. | — |

## MuxCon Federation

| Term | Meaning | Do not use |
|---|---|---|
| MuxCon | The federation protocol that links two or more OpenMux nodes so they can share ports. | federation protocol (alone) |
| Node | One OpenMux server instance that takes part in a MuxCon federation. | peer, host (for a MuxCon participant) |
| Listener (MuxCon) | A MuxCon endpoint that accepts inbound federation connections. | — |
| Initiator (MuxCon) | A MuxCon endpoint that makes an outbound federation connection. | — |
| Heartbeat | A periodic control message that confirms a MuxCon link is alive. | keepalive, ping |
| Mpath | The multi-path logic that picks and fails over between redundant MuxCon links. | — |
| Public key | An Ed25519 key used to verify a MuxCon peer during authentication. | — |

## Config Editor and Web Console

| Term | Meaning | Do not use |
|---|---|---|
| Config Editor | The web console plugin at `/config-editor` that edits `server.yaml` sections. | config UI, settings page |
| View | One sub-page of the Config Editor, selected with the `?view=` query parameter (for example `ports`, `muxcon`). | tab, panel (for the top-level sub-page) |
| Writable section | A config section the current user is allowed to save, per `security.yaml`. | editable section |
| Soft reload | A reload that updates authentication, web console UI settings, adapters, and ports without a full restart (SIGHUP). | hot reload |
| Full reload | A reload that stops and re-creates all adapters (SIGUSR1). | hard reload |
| Reload hint | The badge in the Config Editor that marks what applies a field change: LIVE (no reload), SOFT, FULL, SIGHUP, or RESTART (process restart). | reload requirement mark |
| Login MOTD | The public message of the day from `web_console.motd`, shown on the login page. | notice, banner (for this text) |
| Logged-in MOTD | The message of the day from `web_console.logged_in_motd`, shown at the top of the status page. May hold sensitive text. | notice, banner (for this text) |

## Authentication and Security

| Term | Meaning | Do not use |
|---|---|---|
| Permission | The access level of a user: `admin`, `read-write`, or `read-only`. | role (for these three levels) || Access default | The server-wide posture for console ports with no group lists: `allow` or `deny` from `security.yaml` `access_default` (issue #58). | default ACL, access policy || Console group | A named group that controls read-write or read-only access to one console, via a port's `read_write_groups`/`read_only_groups` and a user's `groups`. | role (for a console-level group), team |
| API key | A static credential used instead of a username and password. | token (use "API key" for this credential type) |
| Allow-list | The set of adapter/module names permitted by `security.yaml`. | whitelist |
| Control menu | The in-band Ctrl+E,c command menu on telnet/SSH sessions for read-write access control. | escape menu |
| Escape sequence | The two-byte prefix (default Ctrl+E then `c`) that opens the control menu. | escape code |

## Notes for Writers

- If you must introduce a new term, add it to this file in the same commit.
- When two terms in this file seem to overlap, prefer the one already used in
  [docs/ARCHITECTURE.md](ARCHITECTURE.md) or [docs/ADAPTER_PORT_CONTRACT.md](ADAPTER_PORT_CONTRACT.md).
