# MuxCon Binary Framing: Design

**Status: design. Nothing in this document is implemented.** The shipped wire
protocol is ASCII. See
[specifications/MuxCon Protocol Specification (Implemented).md](../../specifications/MuxCon%20Protocol%20Specification%20(Implemented).md)
for the current protocol and
[muxcon.md](muxcon.md) for the current federation behavior (handlers are
`MuxConProtocolHandler` in
[openmux/server/muxcon_protocol.py](../../openmux/server/muxcon_protocol.py)
and `UnifiedMuxConAdapter` in
[openmux/server/adapters/muxcon.py](../../openmux/server/adapters/muxcon.py),
whose module docstring mentions this upgrade).

## 1. Goals

- Keep the semantic model: stream open, data, close, control, heartbeat.
- Use a compact fixed-width frame header instead of the ASCII header text.
- Share one sequence space across all paths to one peer, so a receiver can
  re-order frames that arrive over different paths.
- Detect lost frames and resend them over an available path.

## 2. Non-goals (v1)

- Per-stream ordering metadata. Global sequencing only.
- Compression and encryption. TLS handles both on the transport.
- Flow control windows. The application layer limits data flow.

## 3. Frame types

The design keeps the existing ASCII type characters for the active types and
adds one. `O` (open), `E` (close), and `N` (gap report) disappear: the
stream lifecycle and gap reports become TLVs inside `C`, `H`, and `A` frames.

| Type | Meaning |
|------|---------|
| `D` | Data for one stream |
| `C` | Control: stream open/close, federation commands, errors |
| `H` | Heartbeat. Always carries a `CUM_ACK` TLV. |
| `A` | Standalone ACK with a `CUM_ACK` TLV, plus an optional `GAP_LIST` TLV |

## 4. Base header

The header is fixed at 24 bytes, on a 4-byte boundary:

```
Offset  Size  Field      Description
0       2     Magic      0x4D 0x43 ('M','C')
2       1     Version    0x01
3       1     Type       ASCII 'D','C','H','A'
4       2     Flags      16-bit bitfield (section 5)
6       8     GlobalSeq  Sequence number, 64-bit, per peer
14      4     StreamID   Stream number, 32-bit (0 = control)
18      4     Length     Payload length in bytes (header and TLVs excluded)
22      1     ExtCount   Number of extension TLVs
23      1     Reserved   0
```

Extension TLVs (section 6) follow the header. The payload follows the TLVs.

## 5. Flags (16 bits)

| Bit | Name | Meaning |
|-----|------|---------|
| 0   | R    | Resent frame. `GlobalSeq` keeps the original value. |
| 1   | A    | A `CUM_ACK` TLV follows. |
| 2   | S    | A `GAP_LIST` TLV follows. |
| 3   | P    | Frame is latency-sensitive. |
| 4   | T    | A `TIMESTAMP` TLV follows. |
| 5   | X    | Reserved for fragmentation. |
| 6-15 |    | Reserved. Set to 0. |

`ExtCount` shows the TLV count. A separate extension flag is not needed.

## 6. Extension TLVs

Format: `Type (1 byte) | Len (1 byte) | Value (Len bytes)`. `Len` is 0-255,
so a value is at most 255 bytes. That is enough for v1 port names and
metadata.

| Code | Name | Value |
|------|------|-------|
| 0x01 | CUM_ACK | `uint64`. The highest contiguous sequence the sender has received. |
| 0x02 | GAP_LIST | `Count (1)`, then `Count` entries of `StartSeq (8) | RunLen (2)` |
| 0x03 | TIMESTAMP | `uint64` Unix time in ms. Use to measure RTT. |
| 0x30 | STREAM_OPEN | `NameLen (1) | PortName | MetaLen (2) | MetadataJSON` (`MetaLen` may be 0) |
| 0x31 | STREAM_CLOSE | `ReasonCode (1) | ReasonLen (1) | ReasonText` (both may be 0) |
| 0x20 | CONTROL_SUBTYPE | Reserved for a structured control mapping (not used in v1) |

TLV order does not matter, except that ACK TLVs come first.

A `GAP_LIST` entry names the missing range
`[StartSeq, StartSeq + RunLen - 1]`. Keep `Count` at 8 or fewer.

## 7. Payload by type

- `D`: raw data for `StreamID`.
- `C`:
  - `STREAM_OPEN` TLV: open a stream (`StreamID` above 0).
  - `STREAM_CLOSE` TLV: close a stream (`StreamID` above 0).
  - Control text (`StreamID` 0): federation commands, in the same ASCII
    format the current protocol uses (for example `SERVER:REGISTER:...`,
    `PORTS:REGISTER:...`).
  - Keep control text and stream TLVs in separate frames.
- `H`: usually an empty payload. The `CUM_ACK` TLV is required.
- `A`: no payload. A `CUM_ACK` TLV is required. `GAP_LIST` is optional.

## 8. Sequencing

- Every first-sent frame takes the next `GlobalSeq` (mod 2^64). The sequence
  is per peer, not per path. This mirrors the current per-peer-group
  numbering (`_peer_tx_seq` in `muxcon.md`, section 6), so the mpath failover
  model keeps working.
- A resent frame keeps its original number and sets flag R.
- Sequence comparison is modulo 2^64. Two numbers are close when their
  difference is below 2^63.
- The sender holds unacked frames in a resend buffer, keyed by `GlobalSeq`,
  until a cumulative ACK covers them.

## 9. ACKs and loss detection

- The receiver keeps `next_contiguous`.
- A frame with `seq == next_contiguous`: advance and close any gap range that
  it fills.
- A frame with `seq > next_contiguous`: record the gap range and buffer the
  frame.

ACK emission:

- Send ACKs in the `CUM_ACK`/`GAP_LIST` TLVs of any outbound frame
  when at least `ack_interval_ms` has passed or a new gap appeared.
- Send a standalone `A` frame when the link stayed quiet for
  `ack_silence_ms` or when gaps are still open.

Retransmission:

- On an ACK that reports gaps, resend the earliest missing ranges at once.
  Limit the number of concurrent retransmissions.
- On a timer: resend any unacked frame older than `base_rto`.

The current ASCII implementation already does retransmission with an adaptive
timeout (`muxcon.md`, section 6). The binary design keeps that behavior and
adds explicit gap lists.

## 10. Heartbeats

- Send a heartbeat at every `heartbeat_interval` when no other outbound frame
  already carries ACKs.
- Every heartbeat carries a `CUM_ACK` TLV, so liveness and progress share one
  frame.
- A `TIMESTAMP` TLV is optional, for RTT measurement.

## 11. Stream lifecycle and control text

- Open: a `C` frame with a `StreamID` above 0 and a `STREAM_OPEN` TLV.
- Data: `D` frames with the same `StreamID`.
- Close: a `C` frame with a `StreamID` above 0 and a `STREAM_CLOSE` TLV.
- Control lines (`StreamID` 0) keep their current ASCII payloads and
  semantics unchanged in v1. The binary design changes framing only; it does
  not re-structure the control protocol.
- Multipath: the mpath grouping, primary selection, and failover loops
  (`muxcon.md`, section 5) stay as they are. Binary frames replace the ASCII
  DATA frames as the transport, and the shared per-peer `GlobalSeq` is what
  lets a receiver re-order frames that arrive over different paths.
- Fast shutdown: the current protocol already ships a fast, drain-free
  shutdown (`MPATH:SHUTDOWN:BEGIN[:reason]`, `MPATH:END`; see `multipath`
  handling in [muxcon.md](muxcon.md) sections 5 and 9). The binary framing
  does not change it: these control payloads travel inside binary `C` frames.

## 12. Capability negotiation (proposed)

The current capability whitelist
(`validate_capabilities` in `muxcon_protocol.py`) is:
`multi_hop`, `conflict_resolution`, `metadata`, `topology_discovery`,
`port_federation`, `remote_registration`, `chain_tracking`. This design
would add one entry, `binary1`. The current code does not define it.

Steps:

1. Both nodes list `binary1` in their handshake `CAPS=`.
2. On mutual agreement, the initiator sends an ASCII control line
   `UPGRADE:BINARY`.
3. After the peer acknowledges:
   1. Both sides flush their send buffers.
   2. Both sides switch to the binary parser.
   3. The first binary frame is a `H` (with a `CUM_ACK`) or an `A`, and the
      `CUM_ACK` value is the last ASCII sequence seen (0 if the sender had
      no sequence state yet).
4. Fallback: if no binary frame arrives within `upgrade_timeout`, the link
   stays ASCII.

## 13. Error handling

- A malformed header or an unknown type: close the path. If other paths exist,
  report the failure with a control frame on a surviving path.
- Unknown TLVs: skip them. This keeps forward compatibility.

## 14. Open items

- Graceful shutdown across multiple paths.
- A TLV for flow-control window advertisement.
- Fragmentation TLVs for frames larger than the MTU.
- A mapped binary control subtype (TLV `0x20`).
- Retained context after a path closes. The config key
  `context_idle_timeout_sec` is parsed and stored as
  `self.context_idle_timeout` (`UnifiedMuxConAdapter.__init__`) but the
  current code does not read it, so no peer state is retained from a closed
  path today. If this design adds a resend buffer, it needs a retention rule
  and a defined behavior when a path closes mid-buffer.

## 15. Suggested reference constants

```python
MAGIC = b"MC"
VERSION = 1
HEADER_SIZE = 24
MAX_GAP_RANGES = 8
ACK_INTERVAL_MS = 10
ACK_SILENCE_MS = 25
BASE_RTO_MS = 200
HEARTBEAT_INTERVAL_MS = 5000
```

These are starting values for tuning. The defaults in
[DEFAULTS.md](../DEFAULTS.md) apply to every config key that this design
reuses.

## 16. Sender loop (sketch)

1. `build_packet`: take `next_seq`, store the frame in the resend buffer keyed
   by `GlobalSeq`.
2. Send the frame on the chosen path.
3. Resend: set flag R and re-send the same `GlobalSeq`.
4. On ACKs: drop every buffered frame the cumulative ACK covers, and drop
   selectively recovered sequences.

The receiver runs the mirror image: keep a gap map, deliver frames once they
become contiguous.
