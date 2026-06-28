# Adapter Port Contract

This document defines the interface and behavioral invariants that every port
implementation in OpenMux must satisfy. Adapters (serial, loopback, command,
tcp\_initiator, client\_initiator, etc.) expose individual **port instances**
that plug into the PortManager. This contract is the authoritative reference
for what PortManager expects from a port and what a port may rely on from
PortManager.

---

## Data Flow — Two Directions, Two Methods

Port I/O is symmetric. Both directions go through PortManager method calls:

```
Resource → port (read loop) → data_callback(name, data)
                                      ↓
                            pm.send_data()
                                      ↓
                            wrapper.data_queue  ← owned by PM/wrapper
                                      ↓
                            pm.get_port_data() → clients

Client → pm.write_to_port() → port.write_data(data) → device/process/socket
```

- **Outbound (resource → client):** the port calls `self.data_callback(name, data)`. PM sets this callback to `pm.send_data` during `register_unified_port`. PM owns the delivery queue; the port has no queue of its own.
- **Inbound (client → resource):** PM calls `port.write_data(data)`. The port writes to the underlying device/process/socket.

---

## Required Attributes

Every port instance **must** expose the following attributes:

| Attribute | Type | Description |
|---|---|---|
| `name` | `str` | Unique logical name within the adapter. |
| `state` | `PortState` | Current lifecycle state (see [Lifecycle States](#lifecycle-states)). |
| `is_connected` | `bool` | `True` when the underlying resource (device, socket, process) is ready for I/O. Ports without a physical resource (e.g. loopback) should be `True` while `ACTIVE`. |
| `data_callback` | `Callable \| None` | Set by `PortManager.register_unified_port()` to `pm.send_data`. The port must call `await self.data_callback(self.name, data)` for all outbound data. Initialized to `None`; absent PM is an error (see below). |
| `always_buffer` | `bool` | When `True`, PM enqueues data even when no clients are connected. Defaults to `False`. |
| `drop_oldest_on_full` | `bool` | When `True`, PM evicts the oldest item when the delivery queue is full. Defaults to `False`. |
| `max_read_write_users` | `int` | Maximum number of simultaneous read-write clients. Used by PM for access control. |

> **Note:** Ports do **not** own or allocate a `data_queue`. The `UnifiedPortWrapper` created by PM owns the delivery queue. Any `data_queue` attribute on a port is a test-only staging buffer and is not part of this contract.

---

## Required Methods

### `async start() -> bool`

Initialize internal state, open the underlying resource (if any), and
transition `state` to `ACTIVE`. Returns `True` on success, `False` on
failure. Must **not** raise; log errors internally.

### `async stop() -> None`

Close the underlying resource and transition `state` to `DESTROYED`. Must
**not** raise; log errors internally.

### `async write_data(data: bytes) -> int`

Accept inbound bytes from a connected client (**client → resource** direction)
and deliver them to the underlying resource (device, process stdin, socket).
Returns the number of bytes accepted. Must raise `RuntimeError` if the port is
not in `ACTIVE` state. Does **not** touch `data_callback`.

---

## Lifecycle States

States are defined in `openmux.server.adapters.lifecycle.PortState`:

```
CONFIGURED → CREATING → ACTIVE → DESTROYING → DESTROYED
                              ↓
                           DEGRADED
```

| State | Meaning |
|---|---|
| `CONFIGURED` | Port defined in config; `start()` not yet called. |
| `CREATING` | `start()` is executing; resource acquisition in progress. |
| `ACTIVE` | Port is fully operational; I/O is accepted. |
| `DEGRADED` | Port exists but with reduced functionality (e.g. device disconnected, reconnecting). |
| `DESTROYING` | `stop()` is executing; cleanup in progress. |
| `DESTROYED` | Port is fully stopped; instance must not be reused. |

---

## Data Routing — PortManager-Only Invariant

**All outbound data (resource → client) must flow exclusively through
`data_callback`.**

```python
cb = self.data_callback
if cb:
    await cb(self.name, data)
else:
    self.logger.error("Port %s: data_callback not set; dropping data", self.name)
```

`data_callback` is `pm.send_data`, which applies per-port
policies (client presence, `always_buffer`, `drop_oldest_on_full`, DataLogger
recording) before placing data in the wrapper's internal queue for client
delivery.

### What a port must NOT do

- Call `pm.send_data()` directly — use `data_callback` instead.
- Allocate or write to a local `data_queue` as a fallback output buffer.
- Silently drop data without logging when `data_callback` is not set.

### Absence of `data_callback` is an error

If `data_callback` is `None` when data arrives from the resource, the port
must log an **error** and **drop** the chunk. This surfaces misconfiguration
rather than hiding it behind silent buffering.

### When `data_callback` is set

`data_callback` is wired **automatically** at two points — whichever comes
first:

1. **At construction** (`__init__`): if `adapter.main_port_manager` is already
   set, `data_callback` is wired immediately.
2. **At registration** (`pm.register_unified_port`): PM always sets
   `port.data_callback = pm.send_data`.

---

## Registration

After `start()` succeeds, the port must be registered with PortManager:

```python
await self.main_port_manager.register_unified_port(port_name, port, adapter)
```

Registration:
1. Sets `port.data_callback = pm.send_data` (idempotent if already set at construction).
2. Creates a `UnifiedPortWrapper` that owns its own `asyncio.Queue` for client delivery.
3. Exposes the port through PM's port registry.

On teardown, unregister **before** calling `stop()`:

```python
await self.main_port_manager.unregister_unified_port(port_name)
await port.stop()
```

---

## Optional Attributes

| Attribute | Used for |
|---|---|
| `description` | Human-readable label shown in the UI and status pages. |
| `on_client_count_changed(count)` | Called by PM when the number of connected read-write clients changes. |
| `adapter_type` | String identifying the adapter class. Surfaced in port listings. |

---

## Test Guidance

Unit tests that create port instances without a running PortManager must
provide a stub that captures outbound data in its own queue:

```python
class _PortManagerStub:
    def __init__(self, ports: dict = None):
        self._ports = ports or {}
        self.output_queue = asyncio.Queue()
        for p in self._ports.values():
            if hasattr(p, "data_callback"):
                p.data_callback = self.send_data

    async def send_data(self, name: str, data: bytes, **kwargs) -> bool:
        await self.output_queue.put(data)
        return True

    async def read(self, timeout: float = 0.1) -> bytes:
        try:
            return await asyncio.wait_for(self.output_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return b""

stub = _PortManagerStub({"port_name": port})
adapter.main_port_manager = stub
```

Tests read output from `stub.output_queue` (or `stub.read()`), not from any
attribute on the port. If the stub is set on the adapter before port
construction, `data_callback` is wired automatically via `__init__`.

