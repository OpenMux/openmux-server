# MuxCon Protocol Specification (Implemented)

**Status: current.** This document describes the wire protocol as implemented in
[openmux/server/muxcon_protocol.py](../openmux/server/muxcon_protocol.py) (`MuxConProtocolHandler`) and
[openmux/server/adapters/muxcon.py](../openmux/server/adapters/muxcon.py) (`UnifiedMuxConAdapter`). Every claim below is
verified against that code.

Related documents:

- [docs/design/muxcon.md](../docs/design/muxcon.md) — adapter design: federation, multipath, reliability, known gaps. This is
  the design source of truth; this file is the wire-protocol reference.
- [docs/configuration/adapters.md](../docs/configuration/adapters.md) — full config option reference.
  [docs/DEFAULTS.md](../docs/DEFAULTS.md) — all defaults.

## 1. Scope and roles

MuxCon is an ASCII, length-prefixed framing protocol between two OpenMux nodes. Each node runs one `muxcon` adapter section
with zero or more **listeners** (inbound) and zero or more **initiators** (outbound dials). A node's protocol peer is another
OpenMux node, not a generic client.

- Transport: one TCP connection, default port **7822**, optionally wrapped in TLS **before** the handshake.
- All connections stay in this ASCII frame mode for their whole lifetime. The module docstring mentions an optional binary
  framing mode; it is not implemented.
- Stream IDs start at 1 per peer group. Stream 0 is reserved for control, ACK, and heartbeat frames.

## 2. Connection setup

1. **TCP connect** (initiator to listener), optionally TLS.
2. **HELLO** line from the initiator, one line, ASCII:
   ```
   HELLO MuxCon/1.0 TYPE=regular_client ID=<server_id> INST=<instance_id>[ PKID=<key_id>]
   ```
   - `ID` is the node's `server.id` (fallback: system hostname).
   - `INST` is a fresh UUID-4 per process, so a restart is a new generation.
   - `PKID` appears only if the node configured an Ed25519 key (`muxcon.auth.private_key` + `key_id`).
3. **OK** line from the listener:
   ```
   OK MuxCon/1.0 [CAPS=<comma list>] ID=<server_id> INST=<instance_id>
   ```
   - `CAPS` is the listener's capability list restricted to a fixed whitelist (`validate_capabilities`). An initiator that
     receives a line not starting with `OK ` aborts the connection.
4. **Authentication** — Ed25519 public-key challenge/response, carried in stream-0 control frames. There is no
   username/password scheme.
   - `auth_required: true` (the default). The listener checks the HELLO for a `PKID` present in its `public_keys` list. Missing
     or unknown: it sends `AUTH:ERROR:missing_or_unknown_pkid` and closes. A known `PKID` gets:
     ```
     #0:C:<len>:<seq>:AUTH:PK:CHALLENGE:<pkid>:<nonce_b64>
     ```
     where `<nonce_b64>` is a 32-byte random nonce; the challenge expires after 30 s. The initiator signs the nonce with its
     private key and replies:
     ```
     #0:C:<len>:<seq>:AUTH:PK:RESPONSE:<pkid>:<sig_b64>
     ```
     If the initiator has no key configured to answer, it sends `AUTH:ERROR:no_client_key` instead. The listener verifies and
     replies `AUTH:OK`, or `AUTH:ERROR:bad_signature` / `AUTH:ERROR:expired` and closes.
   - `auth_required: false` skips steps 4 entirely (same-host testing only).
   - After a frame of type `O`, `D`, or `E` arrives on a connection that is not yet authenticated, the receiver sends one
     notice `#0:C:13:<seq>:AUTH:REQUIRED` and drops the frame.
5. **Port advertisement.** Once it considers the connection authenticated, each side **proactively** sends its local port
   catalog (section 5, `PORTS:FEDERATED`). It does not wait to be asked; the `PORTS:LIST:FEDERATED` request frame is parsed but
   is a no-op today.
6. **Heartbeats** start: `HB` request every `heartbeat_interval` seconds (default 30), section 4.4.

## 3. Frame format

Every frame is one ASCII header line, a raw payload, and one LF:

```
#<StreamID>:<type>:<len>:<seq>:<payload>\n
```

- `<StreamID>` — decimal. 0 is reserved; stream IDs are allocated per peer group starting at 1.
- `<type>` — a single character for stream frames, or a short command token (`HB`) for non-stream frames.
- `<len>` — decimal byte count of `<payload>`.
- `<seq>` — decimal sequence number (section 7).
- `<payload>` — exactly `<len>` bytes; any byte value, no escaping. For `C` frames the payload is ASCII command text (section
  5); for `D` frames it is raw data.
- The header is strictly ASCII. The parser scans for `#` (skipping stray whitespace/newlines), reads up to and including the
  fourth colon, then requires the three numeric fields to parse as integers.
- `<payload>` may contain `#`, `:`, LF, etc.; it is length-delimited, not line-delimited.

### Frame types

| type | name | stream | payload |
|---|---|---|---|
| `O` | stream open | per stream | target port name (the local port the peer opens against) |
| `D` | data | per stream | raw port bytes |
| `C` | control | stream 0 | command text (section 5) |
| `E` | stream close | per stream | optional reason text (often empty) |
| `A` | data ACK | stream 0 | decimal string of the DATA `<seq>` being acknowledged |
| `HB` | heartbeat | stream 0 | `REQ:<ts>` or `ACK:<ts>` (`<ts>` = whole-seconds epoch) |

`HB:REQ:<ts>` / `HB:ACK:<ts>` are also accepted as `C`-frame payloads (legacy form); the dedicated `HB` type is the current
one.

## 4. Stream lifecycle

All of this happens between two nodes. The node that owns the port is the **origin**; the node whose client opened it is the
**requester**.

### 4.1 OPEN

The requester opens a stream to a port name the origin advertised:

```
#3:O:9:1:console-a
```

On receipt the origin (if the connection is authenticated):

1. maps the session slot `(peer_key, stream_id) -> port_name`,
2. registers a tracked pseudo-client `fed:<peer_key>:<stream_id>` on that port — **read-only by default**; promotion requires a
   `FEDRW` request (section 5),
3. starts **one pump** task that drains the port's data queue and sends the bytes as `D` frames on that stream.

There is **no `OK` reply**. The origin either sets up the session silently or sends nothing. An unauthenticated OPEN gets the
`AUTH:REQUIRED` notice and is dropped.

One pump per slot: a duplicate OPEN for the same slot reuses the live pump. If the peer reuses a stream ID for a *different*
port, the origin stops the old session first.

### 4.2 DATA

```
#3:D:14:42:ls -la /var/log
```

The receiver buffers out-of-order frames per peer group (section 7), delivers them in order to the stream, and immediately
sends back a data ACK on stream 0:

```
#0:A:2:7:42
```

(`42` = the DATA sequence number being acknowledged.)

### 4.3 CLOSE

```
#3:E:17:8:client_disconnect
```

On receipt the origin stops the slot's pump, drops the slot mapping, and frees the `fed:` pseudo-client — releasing the port's
read/write slot and buffering. An unknown stream ID on CLOSE is ignored.


### 4.4 HEARTBEAT

Every `heartbeat_interval` seconds (default 30) each side sends:

```
#0:HB:14:3:REQ:1755000000
```

and the other echoes the timestamp:

```
#0:HB:14:4:ACK:1755000000
```

Heartbeats drive liveness/RTT for multipath (section 8). Heartbeats from an unauthenticated peer are ignored (no state is kept
for it).

## 5. Control commands (stream 0)

Every `C`-frame payload is a command handled by `_process_control_command`. The complete current set:

### Authentication

- `AUTH:PK:CHALLENGE:<pkid>:<nonce_b64>` — listener to initiator.
- `AUTH:PK:RESPONSE:<pkid>:<sig_b64>` — initiator to listener.
- `AUTH:OK` — listener: signature verified.
- `AUTH:ERROR:missing_or_unknown_pkid` — listener: HELLO had no usable PKID (connection closes).
- `AUTH:ERROR:bad_signature` / `AUTH:ERROR:expired` — listener: verification failure (connection closes).
- `AUTH:ERROR:no_client_key` — initiator: challenged but no key configured.
- `AUTH:REQUIRED` — one-off notice when `O`/`D`/`E` frames arrive before authentication; the frame is dropped.

### Port federation

- `PORTS:FEDERATED:<count>` — port catalog. The payload is the command line followed by one compact-JSON line per port,
  terminated by `END:PORTS`:
  ```
  #0:C:<len>:<seq>:PORTS:FEDERATED:2
  {"id":"console-a",...}
  {"id":"console-b",...}
  END:PORTS
  ```
  sent proactively on authentication (section 2, step 5).
- `PORTS:LIST:FEDERATED` — request form; parsed, then ignored.
- `PORT_STATUS:<port_name>` — live offline-reason push. The payload is the command line plus one JSON line (this example body
  is 81 bytes):
  ```
  #0:C:81:<seq>:PORT_STATUS:console-a
  {"status_message":"device link down","readiness":"offline"}
  ```
  Only the **origin** of a port pushes it (when that port's status changes). Intermediate relays apply it to their proxy and do
  **not** re-broadcast — no echo loop across a multi-hop chain.
- `VIEWERS:<port_name>` — viewer-presence snapshot. One JSON line per viewer (username/mode/ip), terminated by `END:VIEWERS`. A
  peer merges the entries into its local view and relays them one hop further upstream, adding its own local viewers — so the
  console viewers badge works across a multi-hop chain.

### Write-slot arbitration

- `FEDRW:<port_name>:<stream_id>:<action>` — the requester asks the origin to change the stream's access mode on a one-writer
  port:
  - `REQUEST` — promote to read-write (denied if the slot is held).
  - `RELEASE` — demote to read-only.
  - `TAKE:<spec>` — force a takeover; `<spec>` names the holder to demote (`latest`, `own:<sid>`, or a verbatim `fed:` id).
  - `FORCE` — legacy alias, routes to `TAKE:latest`. The origin always replies with the resulting mode:
  ```
  #0:C:<len>:<seq>:FEDRWACK:console-a:3:read-write
  ```

### Multipath teardown

- `MPATH:SHUTDOWN:BEGIN[:reason]` — a peer is closing a path gracefully. The receiver replies `MPATH:END` and closes the
  connection.
- `MPATH:END` — the receiver closes the connection.

**There is no `BROADCAST` command.** No control command carries a free-form text payload.

## 6. Error handling

- **Handshake**: an initiator that receives a line not starting with `OK ` raises and aborts the connection.
- **Framing errors**: a frame whose numeric fields do not parse, an incomplete header, or a short read on the payload/trailing
  LF makes `_read_frame` return `None`, which ends the receive loop and **closes the connection**. There is no per-stream
  recovery from a framing error.
- **Data drops**: a `D` frame for a stream with no route (peer restarted, stale stream ID) is dropped with a rate-limited
  WARNING. Every such drop is lost console output.
- **Ignored**: CLOSE of an unknown stream ID; unknown `FEDRW` actions; heartbeats from unauthenticated peers.

## 7. Sequence numbers and reliability

- DATA sequences number **per peer group** (the mpath group, `node:<server_id>`), not per TCP connection. A mid-stream failover
  between two paths to the same node does **not** restart numbering.
- The receiver reorders per peer and delivers in order. A missing sequence number the sender never refills wedges otherwise; a
  gap older than `gap_stuck_sec` (default 4 s) is dropped and the buffered tail is delivered in order, with an ERROR log,
  instead of wedging the peer.
- Unacked frames are retransmitted with an adaptive timeout: from `retx_initial_ms` (350 ms), tracking up toward 2.5× the
  observed heartbeat RTT, capped at `retx_max_ms` (2000 ms).
- Duplicates (`seq < expected`) are dropped with a rate-limited WARNING.
- **Peer restart**: same `server_id`, new `instance_id` → the first DATA frame from the new generation resets all per-peer
  sequence state (TX counter, reorder buffer, send buffer, retransmit counters). Frames from an older path of the same peer are
  dropped, so a dying old-generation path cannot roll the counters back.
- **Path loss keeps sequence state**: when all paths to a peer are gone the peer's group becomes empty, but the sequence state
  is kept until process exit (or a generation change), so a seconds-later re-dial does not silently desynchronize the two
  sides.

## 8. Multipath (mpath)

Multiple physical TCP connections to the same logical peer form one **mpath group** (keyed by `node:<server_id>` once
handshaked, else by address).

- One connection in the group is **primary**; only the primary carries DATA/control traffic for that peer.
- Preference comes from `path_pref` (per initiator, or matched listener).
- Strategy `best_pref` (the only one): highest `path_pref`, least stale.
- `mpath_preemptive_promote` (default on) swaps in a higher-preference connection immediately, without waiting for the current
  primary to fail.
- A connection goes stale after `max(mpath_primary_stale_sec, heartbeat_interval × 2.5)` (default 10 s / 75 s effective)
  without a heartbeat ACK or activity.
- A peer that reconnects with the same `server_id` but a new `instance_id` (a restart) has its older-generation connections
  closed.
- Idle paths are hard-dropped after `mpath_neighbor_idle_drop_sec` (default 900 s; `0` disables).

## 9. Identity and TLS

- A node identifies itself with `server.id` (fallback: system hostname) in both HELLO and OK, and with a per-process `INST`
  UUID (a restart is a new generation).
- TLS wraps the TCP connection **before** HELLO and is independent of the handshake:
  - listeners: `use_tls` defaults **true**. `tls_autogen: true` generates a self-signed cert/key under `<state dir>/muxcon/`
    (CN = `server.id`, no SAN). A listener that cannot build its TLS context refuses to start a plaintext listener
    (fail-closed).
  - initiators: `tls_tofu: true` pins the listener's certificate fingerprint into `<state dir>/muxcon/known_peers.yaml` on
    first connect and rejects later mismatches; `tls_pin_fingerprint: sha256:<hex>` pins an exact fingerprint up front.

## 10. Example session

Node `leaf-01` (initiator) connects to node `hub-01` (listener, port 7822, TLS, auth required). Stream 3 becomes a `console-a`
session.

```
# handshake
leaf→hub: HELLO MuxCon/1.0 TYPE=regular_client ID=leaf-01 INST=9f1c...0a PKID=leaf-a
hub→leaf: OK MuxCon/1.0 CAPS=port_federation ID=hub-01 INST=1b7e...d2
# authentication (hub challenges, leaf signs 32-byte nonce)
hub→leaf: #0:C:38:1:AUTH:PK:CHALLENGE:leaf-a:q8K...b64nonce
leaf→hub: #0:C:119:2:AUTH:PK:RESPONSE:leaf-a:<ed25519-sig b64>
hub→leaf: #0:C:7:3:AUTH:OK
# both sides advertise their port catalogs proactively
hub→leaf: #0:C:139:4:PORTS:FEDERATED:2
         {"id":"console-a","status":"healthy"}
         {"id":"console-b","status":"offline","status_message":"device link down"}
         END:PORTS
leaf→hub: #0:C:65:5:PORTS:FEDERATED:1
         {"id":"serial-p4","status":"healthy"}
         END:PORTS
# heartbeat ping/pong every 30 s
leaf→hub: #0:HB:14:6:REQ:1755000000
hub→leaf: #0:HB:14:7:ACK:1755000000
# leaf's client opens console-a on hub (no reply; the session just starts)
leaf→hub: #3:O:9:8:console-a
hub→leaf: #3:D:15:9:Console ready!
leaf→hub: #0:A:2:10:9
leaf→hub: #3:D:15:11:ls -la /var/log
hub→leaf: #3:D:31:12:total 40\ndrwxr-x--- 5 root root
leaf→hub: #0:A:2:13:12
# live offline-reason push from the origin of console-b
hub→leaf: #0:C:81:14:PORT_STATUS:console-b
         {"status_message":"device link down","readiness":"offline"}
# client disconnects; the reason is free text
leaf→hub: #3:E:17:15:client_disconnect
```

On a clean path close the sides coordinate with `MPATH:SHUTDOWN:BEGIN` → `MPATH:END` before the socket closes.
