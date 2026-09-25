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
| Server identity | The value that identifies this node: the `server.id` config key, else the system hostname. One shared resolver feeds every surface (federation handshake, cert CN, SSO node claim, banners). | node_name, server.name (removed), server.server_id (removed), muxcon.server_id (removed) |
| Dynamic Port Manager | The per-adapter helper that creates and destroys ports at runtime (`DynamicPortManager`). | — |

## Port Lifecycle

| Term | Meaning | Do not use |
|---|---|---|
| State | The current lifecycle stage of a port (see `PortState`). | status (for lifecycle stage) |
| Configured | Port is defined in config; `start()` did not run yet. | — |
| Creating | `start()` is running; the port is not ready yet. | — |
| Active | The port is ready and accepts data. | connected, online |
| Degraded | The port exists but works with reduced function (for example, a disconnected device that retries). | — |
| Offline | The UI tag shown for a port that is not connected and carries a reason (a failed start, a dropped connection, or a blocked start such as a duplicate serial device). The reason text is always shown next to it. | unstartable (use offline for every port type) |
| Idle | The UI tag shown for a port that is healthy but intentionally not running. The port resumes on the next client or Enter. The tag is yellow. It is derived from the port's liveliness and the absence of an offline reason, and it is distinct from `PortState`. | resting (use idle), inactive (use idle) |
| Destroying | `stop()` is running; cleanup is not done yet. | — |
| Destroyed | The port is stopped. Do not reuse the instance. | removed, dead |

## Adapter Types

| Term | Meaning | Do not use |
|---|---|---|
| Serial port | A port that connects to a physical or virtual serial device. | COM port (except in Windows-specific text) |
| Signal line | A RS-232 control line (DTR, RTS) that the serial adapter can drive on request, per the port's `dtr` / `rts` signal-line policy (issue #63). | control line (keep "control line" for the general RS-232 term) |
| Loopback port | A port that echoes data back to the client for testing. | test port |
| Command port | A port backed by a local shell command or process. | shell port |
| TCP initiator port | A port that makes an outbound TCP connection to a remote host, with a pluggable protocol handler (plain, conserver, openmux). | client initiator (legacy name), openmux client port (legacy name) |
| Client listener | The adapter that accepts inbound console-protocol client connections. | — |
| Telnet listener | The adapter that accepts inbound Telnet client connections to a port. | — |
| Web console | The adapter that gives the HTML5 web interface and its plugins (for example the Config Editor). | web UI, web interface (use "web console") |
| Web status | The adapter that gives a lightweight HTTP status/API endpoint. | — |
| PDU adapter | The portless adapter (config section `power`) that manages PDU outlets via a driver interface. It is not a console port adapter. | power adapter (use "PDU adapter") |

## PDU Power

| Term | Meaning | Do not use |
|---|---|---|
| PDU | A power distribution unit managed by the `power` adapter. | power supply, power unit (for a managed unit) |
| PDU adapter | The portless built-in adapter (config section `power`) that manages PDUs and their outlets. | power adapter (when meaning the PDU one), outlet adapter |
| Power driver | A backend module under `power_drivers/` that implements the PDU driver API for one device class. Selected per PDU entry with `driver:` (for example `dummy`, `command`). Not a port adapter: the "command adapter" is the console-port adapter for shell commands and is unrelated. | PDU backend, power plugin |
| Outlet | A single switchable circuit on a PDU. Not a console port; never listed in `LIST` output. | breaker slot, socket, plug |
| Outlet id | The device's own id for an outlet: a free string (for example `1` or `A1`), discovered from the device. Users annotate, not name, outlets. | outlet name (the id is the identity), outlet number (ids are strings) |
| Outlet ref | The single canonical outlet identifier used by the CLI, the web API, and port `power:` keys. A local ref is `<pdu_name>.<outlet_id>` (for example `rack1.3`); a federated feed of an origin node is globally qualified (`Global outlet ref`). The two forms never collide because a local pdu name cannot contain `::`. | outlet (when the pair is meant), feed ref |
| Global outlet ref | `<server_id>::<pdu_name>.<outlet_id>` (for example `peerO::rack1.3`): an outlet of a federated origin node, qualified with that node's server id so the same outlet name on two nodes stays unambiguous (MuxCon outlet federation). The `::` form is this convention's origin separator, shared with the federation display strings. A bare (unprefixed) ref always means this node's own outlet. | origin-qualified ref, remote ref (say "global ref") |
| Feed | One outlet power source declared on a console port's `power:` list. A port declares one or more feeds (dual feed = A/B). | power feed is fine; avoid "line" or "cable" |
| Power page | The web console "Power" menu and its PDU list + per-PDU pages (core web console routes, not a plugin). | power monitor UI, power tab |
| Power badge | The console session header chip showing this port's feed state (green all on, yellow partial, red all off, grey unknown). | power indicator, power dot (that is the status-page cell) |
| `POWER` command | The client-listener text command that lists or switches outlets. | power command (use the exact form) |
| MuxCon outlet federation | The feature that makes a federated console port's declared power feeds and the origin node's last-reported outlet state visible across a federation. A user with an open read-write console session on a fed port can also switch those outlets; the peer relays the switch, and the origin node runs and audits it. | federated PDU, power federation (say "outlet federation") |

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
| Permission | The access level of a user: `admin`, `read-write`, or `read-only`. | role (for these three levels) || Access default | The server-wide posture for console ports with no group lists: `allow` or `deny` from `security.yaml` `access_default` (issue #58). | default ACL, access policy || Write slot capacity | How many users may write to a port at once: `none` (0 writers), `one` (1), or `multiple` (unlimited), from the port's `max_read_write_users` (issue #59). It is a resource, not a privilege: `none` binds even admin. | write slot (for the mode), driver count, RW limit || Console group | A named group that controls read-write or read-only access to one console, via a port's `read_write_groups`/`read_only_groups` and a user's `groups`. | role (for a console-level group), team || Takeover | The explicit transfer of the one write slot from a current holder to another, write-entitled client on a `one` port. It demotes exactly one holder, is re-checked against access control, is audit-logged, and restores the holder if the transfer fails. When no client holds the slot, a no-target takeover takes the empty slot and the taker becomes the writer directly; a named target that matches no holder is refused. It works across a MuxCon link, where the origin node arbitrates. | force take, force promote, seize, grab |
| API key | A static credential used instead of a username and password. | token (use "API key" for this credential type) |
| Allow-list | The set of adapter/module names permitted by `security.yaml`. | whitelist |
| Control menu | The in-band Ctrl+E,c command menu on telnet/SSH sessions for read-write access control. | escape menu |
| Escape sequence | The two-byte prefix (default Ctrl+E then `c`) that opens the control menu. | escape code |

## Notes for Writers

- If you must introduce a new term, add it to this file in the same commit.
- When two terms in this file seem to overlap, prefer the one already used in
  [docs/ARCHITECTURE.md](ARCHITECTURE.md) or [docs/ADAPTER_PORT_CONTRACT.md](ADAPTER_PORT_CONTRACT.md).
