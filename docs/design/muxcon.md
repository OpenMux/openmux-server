# MuxCon Federation: Current Implementation

**Status: implemented.** This document describes the MuxCon federation
adapter as it actually exists in
[openmux/server/adapters/muxcon.py](../../openmux/server/adapters/muxcon.py)
(`UnifiedMuxConAdapter`), its wire protocol
([openmux/server/muxcon_protocol.py](../../openmux/server/muxcon_protocol.py)),
and its data types
([openmux/common/federation_types.py](../../openmux/common/federation_types.py)).

The original design proposal,
[specifications/Unified_MuxCon_Adapter_Specification.md](../../specifications/Unified_MuxCon_Adapter_Specification.md),
is a pre-implementation draft. Its config keys (`federation_policies`,
`node_pattern`, per-initiator `share_ports`/`accept_ports`/`request_ports`)
and protocol frames (`CAPABILITIES:DECLARE`/`CONFIRMED`) were never built as
written. This document replaces it as the source of truth for what MuxCon
does today. For the full config option reference (defaults, types), see
[configuration/adapters.md](../configuration/adapters.md#muxcon-federation-muxcon)
and [DEFAULTS.md](../DEFAULTS.md).

## 1. Overview

One `muxcon` adapter section per server config gives that node (see
Glossary: **Node**) both:

- zero or more **listeners** (`muxcon.listeners`), which accept inbound
  connections from other nodes, and
- zero or more **initiators** (`muxcon.initiators`), which dial out to one
  peer each.

A node identifies itself with `server.id` (fallback: system hostname).
`server.node_name` from the original design doc does not exist — it was
replaced by `server.id` before this shipped.

Each TCP (optionally TLS) connection between two nodes carries one ASCII
framed protocol session. There is no separate "hub"/"leaf" adapter and no
`federation_policies`/`node_pattern` matching engine — every listener and
every initiator uses the same adapter code and the same
`advertise_filters`/`accept_filters` mechanism (section 4).

## 2. Handshake and authentication

Sequence for one connection (`_perform_client_handshake`/
`_perform_server_handshake` in `muxcon.py`):

1. The initiator sends one line:
   `HELLO MuxCon/1.0 TYPE=regular_client ID=<server_id> INST=<instance_id>[ PKID=<key_id>]`.
   `PKID` is included only if `muxcon.auth.private_key`/`key_id` are set.
2. The listener replies one line:
   `OK MuxCon/1.0 CAPS=<comma list> ID=<server_id> INST=<instance_id>`.
3. If the listener has `auth_required: true` (the default), it requires a
   known `PKID` in the HELLO line. If missing/unknown, it sends a control
   frame `AUTH:PK:CHALLENGE:<key_id>:<nonce_b64>`.
4. The initiator signs the nonce with its Ed25519 private key
   (`muxcon.auth.private_key`) and replies
   `AUTH:PK:RESPONSE:<key_id>:<sig_b64>`.
5. The listener verifies the signature against the matching entry in
   `muxcon.public_keys` and replies `AUTH:OK`, or
   `AUTH:ERROR:<missing_or_unknown_pkid|bad_signature|expired>` and closes
   the connection.
6. Once a side considers the connection authenticated (`AUTH:OK` seen, or
   `auth_required: false`), it proactively sends its own port catalog as a
   `PORTS:FEDERATED:<count>` control frame (section 4) — it does **not**
   wait to be asked. A `PORTS:LIST:FEDERATED` request frame exists in the
   protocol handler but the receiving side currently ignores it
   (`_process_control_command`'s handler for `"PORTS:LIST:FEDERATED"` is a
   no-op), since both sides already advertise on their own.
7. Both sides begin exchanging `HB:REQ`/`HB:ACK` heartbeat frames every
   `heartbeat_interval` seconds (default 30s) for liveness/RTT and staleness
   detection used by multipath (section 5).

There is no `CAPABILITIES:DECLARE`/`CONFIRMED` negotiation frame pair, and
no `AUTH api <key>` scheme — auth is Ed25519 public-key challenge/response
only. Setting `auth_required: false` skips steps 3-5 entirely (useful for
same-host loopback testing; not recommended over an untrusted network).

TLS is independent of this handshake: `use_tls: true` (the default for
listeners) wraps the TCP connection before HELLO. `tls_autogen: true`
generates a self-signed cert/key under `tls_dir` on first start; the cert
CN is the node's `server.id` and it has no SAN. TLS failure is fail-closed
on both sides: a listener that cannot build its TLS context refuses to
start a plaintext listener, and an initiator that cannot build its TLS
context retries after backoff instead of dialing in plaintext.
`tls_tofu: true` on an initiator (Trust-On-First-Use) pins the listener's
certificate fingerprint into `<tls_dir>/known_peers.yaml` on first connect
and rejects a different certificate later; `tls_pin_fingerprint` pins an
exact `sha256:<hex>` up front instead.

## 3. Wire protocol (frames)

Every frame (`MuxConProtocolHandler` in `muxcon_protocol.py`) is one ASCII
header line followed by a raw payload and `\n`:

```
#<stream_id>:<TYPE>:<payload_length>:<seq>:<payload bytes>\n
```

`TYPE` is a single letter or short command token:

| Type | Meaning | Created by |
|---|---|---|
| `C` | Control frame (ASCII command text as payload) | `create_control_frame` |
| `D` | Data frame for one logical stream | `create_data_frame` |
| `O` | Stream open (payload = target port name) | `create_stream_open_frame` |
| `E` | Stream close (payload = reason text) | `create_stream_close_frame` |
| `A` | ACK for a specific DATA sequence number | `create_ack_frame` |
| `HB` | Heartbeat request/ack (`REQ:<ts>`/`ACK:<ts>` payload) | `create_heartbeat_request`/`_ack` |

Control-frame payload commands actually handled by
`_process_control_command` include: `AUTH:PK:CHALLENGE:...`,
`AUTH:PK:RESPONSE:...`, `AUTH:OK`, `AUTH:ERROR:...`,
`MPATH:SHUTDOWN:BEGIN[:reason]`, `MPATH:END`, `HB:REQ:<ts>`/`HB:ACK:<ts>`
(and bare `REQ:`/`ACK:` for the `HB` command type), `PORTS:LIST:FEDERATED`
(ignored, see above), `PORTS:FEDERATED:<count>` (section 4), and
`VIEWERS:<port_name>` (viewer-presence relay, one JSON line per viewer,
terminated by `END:VIEWERS` — see section 7).

A separate, experimental "binary framing mode" is mentioned in the module's
own docstring ("optional upgrade to a compact binary framing mode") but
**no such upgrade path is implemented** — there is no code that ever
switches a connection's wire mode away from ASCII. Every connection stays
ASCII-framed for its whole lifetime today.

## 4. Federated ports

A federated port is one entry in a peer's `PORTS:FEDERATED:<count>` block
(one JSON object per line, terminated by `END:PORTS`), built from
`PortMetadata.to_federation_dict()` (`federation_types.py`). Local ports
are advertised via `_send_local_port_list`, which pulls the local
`PortManager`'s port list and applies `advertise_filters` (section 4.1)
before sending.

On receipt, `_handle_ports_federated` applies `accept_filters` per entry,
then creates or reuses one `_RemotePortProxy` per accepted port
(`_register_remote_port_from_dict`). The proxy is registered directly into
the local `PortManager`'s port map, so any client (CLI, web console,
telnet/SSH) opens a federated port exactly like a local one — no special
client-side code path exists for remote ports.

Reconnection reuses the existing proxy for the same port name + origin
`server_id` instead of creating a duplicate, re-opens any client streams
that were active, and emits a one-time
`[OpenMux:FEDERATED_LINK_RESTORED ...]`/`..._STALE ...`/`..._DISCONNECTED ...`
notice into the port's data stream so attached clients see the link state
change. Ports no longer present in a peer's latest advertisement are
unregistered (`_handle_ports_federated`'s stale-proxy diff).

### 4.1 Advertise/accept filters

`advertise_filters`/`accept_filters` (adapter-level, under `muxcon:`) and
optional per-key overrides (nested under a `muxcon.public_keys[].muxcon`
entry, applied once a connection authenticates with that key) each take:

```yaml
include: []            # glob patterns on port name; empty = allow all
exclude: []             # glob patterns on port name; exclude wins over include
adapter_include: []     # glob patterns on adapter_type
adapter_exclude: []
server_include: []      # glob patterns on the origin server_id (accept_filters only, meaningfully)
server_exclude: []
```

Matching uses `fnmatch.fnmatchcase` (shell-style globs, e.g. `console_*`).
Exclude always wins over include. There is no regex or prefix pattern
syntax (the original design draft's `node_pattern: {regex: ...}` /
`{prefix: ...}` forms do not exist) — glob only.

### 4.2 Federated cache (offline ports survive a restart)

If `federated_cache_enabled: true` (default), a JSON snapshot of every
remote proxy's minimal metadata is written to `federated_cache_path`
(default `<tls_dir>/federated_cache.json`) on every change, and reloaded on
`start()` — so a federated port a peer previously advertised still shows up
(marked disconnected) after this node restarts, even before the peer
reconnects. If `federated_cache_ttl_sec > 0`, a background loop purges
proxies that have been disconnected longer than that TTL (default `0.0`:
never expires by time). Cached proxies register through
`PortManager.register_federated_port` on load (and re-register in the reuse
path if `data_callback is None`) — without that callback inbound data just
fills the proxy's own `data_queue`, which nothing consumes here (issue
#56), so the port black-holes.

### 4.3 Origin-side stream sessions (pumps)

When a peer's client opens a federated port, the origin maps the received
`O` (stream open) to a session slot `(peer_key, stream_id) -> local port`
(`_local_session_map`), registers a tracked `fed:<peer_key>:<stream_id>`
pseudo-client on that port (read-only by default, promotable via FEDRW
arbitration), and starts one **pump** task
(`_pump_local_port_to_remote`) that drains the local port's shared
`data_queue` and sends the data as DATA frames on the peer's stream. The
pump also takes a ref-counted buffering hold so port output is buffered even
when no local console is attached.

Invariants (issue #54):

- One pump per slot. A duplicate `O` for the same slot reuses the live pump
  instead of stacking a second one on the same `data_queue` (two pumps would
  split the stream and roughly half the output would be lost). A peer may
  reuse a stream id for a *different* port; the OPEN handler then stops the
  old session first (`_stop_local_session`) and maps the slot to the new
  port.
- An `E` (stream close) stops the slot's pump, drops the mapping, and frees
  the `fed:` pseudo-client, so the port's read-only/read-write slot and
  buffering hold come back.
- When a peer's last mpath path closes (or the adapter stops), every session
  slot that peer owned is torn down (`_maybe_cleanup_empty_peer` ->
  `_cleanup_peer_local_sessions`), even if the peer never sent `E` frames
  (crash, network drop, process death). Without this, a left-over ("zombie")
  pump keeps draining the local port's `data_queue`: every chunk it dequeues
  while no path is connected is consumed and lost, and after the peer
  reconnects with fresh stream ids its old-id frames are dropped on the far
  side — the "half the console output disappears after a reset" symptom.
  Proxies themselves survive the tear-down in the federated cache (section
  4.2); only the live stream sessions go.

A pump cleans up its own slot on exit only if it still owns the
pump-registry entry (`_pump_tasks`), so a replaced pump or an in-flight
teardown can never wipe a newer session's mapping. Inbound DATA for a stream
that has no route (stale stream id after a peer restart, session not yet
mapped) is dropped and logged at WARNING, rate-limited to one line per
stream per second (`_log_no_mapping_drop`) — every such drop is lost console
output.

On the `PortManager` side, `add_client_to_port` dedupes by client id:
re-adding an id that already has a record updates that record in place
(mode/username/timestamp) instead of appending a duplicate, keeps the
existing delivery queue of the re-added client, and does not count the
re-adding client against its own read-write seat.

## 5. Multipath (mpath)

Multiple physical connections to the *same logical peer* (grouped by
`_derive_peer_key_from_conn_id`: preferably `node:<server_id>` once a
handshake completes, else `<host>:<port>` for outbound or `host:<host>` for
not-yet-handshaked inbound) form one **mpath group**. One connection in the
group is the **primary**; only the primary carries DATA/control traffic for
that peer (`_select_mpath_connection`, used by every `_send_*_mpath` call).

- A connection's preference (`pref`) comes from an initiator's
  `path_pref` option, or a listener's `path_pref` (matched by which
  listener accepted the socket).
- `mpath_strategy: best_pref` (default, only implemented strategy) picks
  the highest-`pref`, least-stale connection as primary.
- `mpath_preemptive_promote: true` (default) swaps in a newly-registered or
  newly-eligible higher-preference connection immediately, without waiting
  for the current primary to actually fail.
- A connection is considered stale once its last heartbeat ACK / activity
  is older than `mpath_primary_stale_sec` (default 10s; the actual cutoff
  used is `max(mpath_primary_stale_sec, heartbeat_interval * 2.5)` so a slow
  heartbeat cadence doesn't cause false failover).
- `_mpath_failover_loop` re-evaluates every `mpath_failover_check_sec`
  (default 2s), and also hard-drops (closes) any connection idle longer
  than `mpath_neighbor_idle_drop_sec` (default 900s; `0` disables).
- When a peer reconnects with the same `server_id` but a new
  `instance_id` (a restart), `_retire_old_generation` closes the older
  connection(s) for that peer, keeping only the newest generation live.

There is no configuration for topology-wide policies (tree/mesh/hub) as in
the original draft — multipath only groups *redundant paths to one peer*,
it does not do multi-node routing/relay policy selection.

## 6. Reliability: sequencing and retransmission

DATA frames are numbered per multipath group (`_peer_tx_seq`), not per
physical connection, so a mid-stream failover doesn't restart numbering.
The receiving side buffers out-of-order frames per peer
(`_peer_rx_state`) and delivers them in order once gaps fill in
(`_handle_inbound_data`). Unacked frames are tracked in `_peer_sendbuf` and
resent by `_retx_loop` using a retransmit timeout that starts at
`retx_initial_ms` (default 350ms), adapts toward `2.5x` the observed
heartbeat RTT, and is capped at `retx_max_ms` (default 2000ms).

### 6.1 Peer generation change (restart) resync

Numbering is per peer, so a peer **restart** is the one case where numbering
really does restart: the peer's TX counter comes back at 1 while keeping the
same stable `server_id` (same `peer_key`), and our per-peer RX `expected`
counter, reorder buffer, TX counter, unacked send buffer and retransmission
counters all survive the restart. Left alone, every frame from the
restarted peer has `seq < expected` and is dropped as a "duplicate" while
still being ACKed — permanent one-direction data loss. Both handshake roles
record the peer's `instance_id` on the connection; the first DATA frame from
a different generation resets all peer-scoped sequence state
(`_maybe_resync_peer_generation`). Two guards keep the reset precise: a
failover between two paths of the *same* peer process never resets, and a
frame from an **older path** (a connection opened before the generation
currently adopted) is dropped, not buffered, so the dying old-generation
connection cannot roll the counters back or wedge the new generation's
reorder window.

### 6.2 Sequence state survives path loss

The peer-scoped sequence state (`_peer_tx_seq`, `_peer_rx_state`,
`_peer_sendbuf`, `_peer_retx_count`) is tied to the peer *identity*
(`node:<server_id>`), not to live paths. When a peer's mpath group becomes
empty, `_unregister_mpath_connection` removes the group but does NOT clear
this state: an empty group only means "no live path right now", and the
peer often re-dials seconds later with the same identity. Clearing it here
used to reset the counter on whichever side's group emptied first, while
the other side (whose replacement path joined before its last path was
reaped) kept its old counter. Every frame from the reset side then read
`seq < expected`, was dropped as stale, and was ACKed anyway, so it was
lost for good: permanent one-direction data loss after a path loss +
reconnect with no process restart on either side. The state now lives
until process exit. The only in-process reset is a peer generation change
(same `server_id`, new `instance_id` — a restart): the first DATA frame
from the new generation resyncs all state for that peer
(`_maybe_resync_peer_generation`).

### 6.3 Stuck reorder gap flush

A missing seq that the sender never refills would otherwise wedge in-order
delivery forever: every later frame for the peer sits in the reorder buffer.
Since the sender's retransmission window is bounded by `retx_max_ms`, a gap
older than `gap_stuck_sec` (`2 x retx_max_ms`, default 4s, minimum 1s) is
treated as permanent — most likely a frame lost without a RETX request.
`_flush_stuck_gap` drops the missing seq and delivers the buffered tail in
order, logging an ERROR, instead of wedging the whole peer. A gap that fills
in time is never flushed.

### 6.4 Duplicate drop warning

A `seq < expected` frame is dropped (already delivered, or the peer
restarted mid-stream and the generation resync has not seen a newer frame
yet). This is surfaced as a rate-limited WARNING (at most one per peer per
second) via `_warn_stale_data_drop`, reusing the same rate-limit table as
`_log_no_mapping_drop` under a `"dup"` slot, so a silent one-direction loss
is diagnosable instead of invisible.

### 6.5 Reconnect recovery: stale stream CLOSE and read-write re-grant

Stream sessions are per path. When the last path to a peer dies, the
initiator's proxy cannot deliver CLOSE frames for the streams it opened
(the path is dead). It remembers those stream ids in
`RemotePortProxy._stale_sessions` instead of forgetting them. When a path
is back and the peer re-advertises its ports (the proxy reuse path in
`_register_remote_port_from_dict`), the adapter resends those CLOSEs on
the live path before it reopens fresh streams
(`_close_stale_proxy_sessions`). That makes the origin tear down the
orphaned sessions — their pumps, their `fed:<peer>:<sid>` clients, and any
read-write slot they hold — instead of leaving them alive until the
origin's whole peer group empties. Unknown stream ids are ignored by the
origin's CLOSE handler, so a peer restart in between is harmless.

The same reuse path then re-requests the origin's read-write grant for
every local client that was `read-write` before the outage
(`_regrant_proxy_read_write`, a plain `FEDRW ... REQUEST`): the reopened
streams register read-only on the origin, and nothing else re-sends the
grant. A denied grant (the slot is held by another user) demotes the
client locally so its mode matches the origin; a human can still
force-take.

## 7. Viewer presence relay

A `VIEWERS:<port_name>` control frame (JSON lines, `END:VIEWERS`
terminated) carries the list of local viewers (username/mode/ip) for a
port to every authenticated peer, triggered by
`ConsoleManager.broadcast_presence()` locally. On receipt, the reporting
peer's entries are merged into that port's `_RemotePortProxy.remote_viewers`
(surfaced by `ConsoleManager.get_viewers_display()` for the federated port)
and relayed one hop further upstream, adding any genuinely-local viewers at
this hop — this is what makes the console's viewers badge show
`<server_id>/<username>@<ip>` correctly across a multi-hop federation
chain. `get_viewers_display()` skips `connected_clients` entries whose
`client_id` starts with `fed:` — internal pseudo-clients, not real viewers:
the same remote viewer already appears via `remote_viewers`, and counting
both would double-count it and show a malformed
`federation:<peer_key>@unknown` entry.

## 8. Status, monitoring, and fault injection (`web_status` adapter)

These HTTP endpoints are served by the `web_status` adapter (not
`web_console`), and read the muxcon adapter's live state; there is no
muxcon-specific `openmuxctl` subcommand today (only the adapter-agnostic
`openmuxctl status`/`reload`).

- `GET /api/federation` — peers configured, active connections (role,
  handshake info, peer address, ports registered per connection), and a
  ports summary aggregated from `PortManager`.
- `GET /api/multipath` — mpath groups, their primary, and per-connection
  preference/staleness.
- `POST /api/fault` (only if `web_status.enable_fault_injection: true`,
  default `false`) — body `{"action": "...", "connection_id": "...",
  "params": {...}}`. Supported `action` values map 1:1 to adapter methods:
  `list` (dump `_fault_state`), `freeze`/`unfreeze`
  (`freeze_connection`/`unfreeze_connection` — suppress reads, mark stale
  for mpath purposes), `drop_heartbeats`/`restore_heartbeats`
  (`set_drop_heartbeats`), `close_conn` (`force_close_connection`, honors
  `params.linger`), `reset_conn` (`force_reset_connection`, hard RST).
  This is a testing/ops tool for exercising failover, not something a
  normal deployment needs to call.

## 9. Known gaps vs. the original design draft

Documented here so nobody re-discovers these by reading 5000 lines of
`muxcon.py`:

- No `federation_policies`/`node_pattern` (glob/regex/prefix) matching
  engine — only the flat `advertise_filters`/`accept_filters` glob lists
  in section 4.1.
- No per-initiator `share_ports`/`accept_ports`/`request_ports` capability
  declarations — filtering is adapter/connection-level, not declared per
  initiator entry.
- No `CAPABILITIES:DECLARE`/`CONFIRMED` bidirectional negotiation frames.
- No binary wire-mode upgrade, despite the module docstring describing one
  (section 3).
- No CLI (`openmuxctl`) command surfaces muxcon status or fault injection;
  only the `web_status` HTTP API does.
- `server.node_name` does not exist; use `server.id`.

## See also

- [configuration/adapters.md](../configuration/adapters.md#muxcon-federation-muxcon) — full config option reference with defaults.
- [DEFAULTS.md](../DEFAULTS.md) — every muxcon runtime default in one place.
- [QUICKSTART.md](../QUICKSTART.md) — step-by-step two-node setup walkthrough.
- [config/loopback_test.yaml](../../config/loopback_test.yaml) and
  [config/remote_leaf_server.yaml](../../config/remote_leaf_server.yaml) — a
  working listener + initiator pair.
