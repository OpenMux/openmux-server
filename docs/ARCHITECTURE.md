# OpenMux Architecture Overview

This document gives a precise, implementation‑oriented map of how the OpenMux server fits together so a newcomer can quickly become productive. It focuses on runtime flow, adapter/plugin model, required registrations, and typical extension points.

---
## 1. High‑Level Runtime Flow
1. Entry: `openmux/server/main.py` (`OpenMuxServer`).
2. Configuration loaded by `ConfigManager` (YAML provided via `-c/--config` or `--config-dir`).
3. Core managers instantiated:
   - `LoggingManager` – sets global logging.
   - `AuthManager` – user + API key auth data.
   - `PortManager` – port registry/aggregation layer.
   - `ConsoleManager` – interactive / management console integration.
4. Adapters created via `GenericAdapterFactory` ⇒ plugin discovery from per‑section keys like `loopback_ports`, `command_ports`, etc.
5. Legacy `ServerAdapterFactory` and connection adapters removed; plugin system only.
6. Adapters started; each may create ports (loopback, serial, command, muxcon federation, etc.). Ports are exposed through the central `PortManager`: some adapters register ports explicitly (e.g., `SerialAdapter` via `PortManager.register_unified_port(...)`), while others are wrapped lazily by `PortManager` from `adapter.ports`. A `DynamicPortManager` is attached to every adapter for dynamic lifecycle operations, though initial creation may be adapter‑specific.
7. Connection adapters listen for or initiate network connections and tie client sessions to ports.
8. Server loop blocks until shutdown signal → orchestrated graceful stop sequence (adapters → ports → managers).

---
## 2. Configuration Model (Strict Schema)
The authoritative schema is `docs/openmux_config_schema.yaml`. Minimal required top‑level keys:
- `server`: identifiers and metadata.
- `authentication`: at least one of `users` or `api_keys`.
- At least one runtime provider section present: one or more of (`client_listener`, `serial_ports`, `loopback_ports`, `command_ports`, `muxcon`, `web_status`).

Only per‑section configuration is supported. Each top‑level section maps directly to a specific adapter plugin (e.g., `loopback_ports`, `serial_ports`, `client_listener`, `muxcon`). The factory instantiates adapters from the sections present in the config.

Note:
- There is no separate "management" adapter section. Operator/console management rides on the `client_listener` text protocol via `TcpServerAdapter` in combination with `ConsoleManager`. For HTTP status, use the `web_status` adapter section.

---
## 3. Core Components & Responsibilities
| Component | Responsibility | Key Methods / Notes |
|-----------|---------------|---------------------|
| `OpenMuxServer` | Orchestration & lifecycle | `start()`, `_initialize_unified_adapters()`, `_start_all_adapters()`, `shutdown()` |
| `ConfigManager` | Load & expose YAML config | `load_config()`, getters (server host/port, auth, web) |
| `AuthManager` | User/API key auth state | `update_config()` (on reload) |
Note on configuration reloads: Hot-reload of port configuration is supported via a SIGHUP signal handler only; there is no HTTP reload endpoint. The server validates the on-disk config and reconciles affected adapters incrementally.
| `PortManager` | Aggregation of active ports | `set_unified_adapters()`, client lookup helpers, wrapper exposure |
| `ConsoleManager` | Interactive / mgmt console | Injected into connection adapters & adapters when supported |
| `GenericAdapterFactory` | Creates modern adapters from config | `create_adapters_from_config()` |
| `PluginRegistry` | Registers & exposes adapter plugin metadata | `_register_built_in_plugins()` auto‑adds built‑ins |
| `DynamicPortManager` | Per‑adapter dynamic port lifecycle | `create_port_dynamically()`, `destroy_port_dynamically()`, reconnection helpers |
| `BaseGenericAdapter` | Abstract base for all adapters | `start()`, `stop()`, `create_port()`, `destroy_port()`, `get_port_configurations()` |
| Built‑in Adapter Classes | Concrete behavior per type | Must subclass `BaseGenericAdapter` |

---
## 4. Adapter / Plugin Architecture
### 4.1 Plugin Registration
`PluginRegistry._register_built_in_plugins()` imports each built‑in adapter class and registers an `AdapterPlugin` keyed by its config section (e.g. `loopback_ports`). Built‑ins include: loopback, command, client_listener, tcp_initiator, serial, muxcon, web_status, and openmux_client. External plugins can be registered at runtime via `GenericAdapterFactory.register_external_plugin(plugin)` before `create_adapters_from_config()` is called.

### 4.2 Adapter Creation Path
For each registered section present in the config file, the factory resolves the corresponding plugin and instantiates an adapter with that section’s slice of configuration. Each adapter instance receives its slice of config. A `DynamicPortManager` is attached immediately, setting `adapter.port_manager`.

### 4.3 BaseGenericAdapter Contract
A custom adapter MUST implement:
- `get_capabilities() -> Set[AdapterCapability]`
- `start() -> bool` (async): initialize resources, call `load_configured_ports()` if providing ports.
- `stop()` (async): release resources; base class will destroy dynamic ports if `port_manager` exists.
- `validate_config(config: Dict) -> bool`: fast structural validation (factory will call this before instantiation).
- `create_port(port_name, config) -> object|None`: create and return a concrete port instance (store any handles on it).
- `destroy_port(port_name)`: inverse of `create_port`.
- `get_port_configurations() -> Dict[str, Dict]`: derive a mapping name→config from adapter's config segment (used by `load_configured_ports()`).

Optional overrides for event hooks:
- `_handle_port_created`, `_handle_port_connected`, `_handle_port_disconnected`, `_handle_port_removed` – invoked by lifecycle events mediated via `handle_lifecycle_event()`.

Optional data APIs (implement only if capabilities include `PROVIDES_PORTS`):
- `_read_from_port_instance(port_instance, timeout)`
- `_write_to_port_instance(port_instance, data)`

### 4.4 Capabilities
`AdapterCapability` flags declare what an adapter supports (e.g. `ACCEPTS_CONNECTIONS`, `PROVIDES_PORTS`, `FEDERATION_AWARE`). These are inspected by `OpenMuxServer` to determine whether port operations (read/write) are allowed.

### 4.5 Dynamic Ports
On `start()`, typical flow for port‑providing adapter:
1. Adapter calls `load_configured_ports()` (base helper) → uses `get_port_configurations()` map. Some built‑ins (e.g., Loopback) currently create ports directly and track them in `adapter.ports`; the factory still attaches a `DynamicPortManager` for future dynamic operations.
2. For each port: `DynamicPortManager.create_port_dynamically()` → adapter's `create_port()` executes.
3. Success path stores instance in `DynamicPortManager.active_ports` and sets state `ACTIVE`.
4. Port removal uses `destroy_port_dynamically()` → adapter's `destroy_port()` then removes state.

Future events (hotplug, federation availability) will trigger `handle_lifecycle_event()` which dispatches to overridable handlers.

---
## 5. Execution Sequencing & Dependencies
Order matters for correct startup:
1. Logging & config load (ensures consistent logging target early).
2. Auth and Port managers (so adapters can bind to them).
3. Adapter factory creates adapters (needs config + managers available).
4. Adapters started (ports become available; connection adapters may depend on them for port namespace completeness).
5. Connection adapters started last so that when clients connect, port space is already populated.

During shutdown the inverse order is applied implicitly: connection adapters stop; adapters stop (destroying ports); port manager closes remaining resources.

---
## 6. Function & Call Triggers (Key Paths)
| Trigger | Calls | Result |
|---------|-------|--------|
| `OpenMuxServer.start()` | `_initialize_server_components()` → `_initialize_unified_adapters()` → `GenericAdapterFactory.create_adapters_from_config()` | Builds + starts adapters |
| Adapter start | `adapter.start()` (impl) → optionally `load_configured_ports()` | Dynamic ports created |
| Dynamic port creation | `DynamicPortManager.create_port_dynamically()` → `adapter.create_port()` | Port instance stored + state ACTIVE |
| Client connection (TCP) | `TcpServerAdapter.handle_client_connection()` → `ConsoleManager`/`PortManager` attach | Session established |
| Port read/write | `BaseGenericAdapter.read_data()` / `write_data()` → adapter `_read_from_port_instance()` / `_write_to_port_instance()` | Data transfer |
| Lifecycle event | `adapter.handle_lifecycle_event()` | Dispatch to override handlers |
| Shutdown | `OpenMuxServer.shutdown()` → loop stopping (connection adapters) → unified adapters `stop()` (destroy ports) → `PortManager.close_all_ports()` | Graceful teardown |

---
## 7. Adding a New Adapter (Checklist)
1. Create new module under `openmux/server/adapters/` (e.g. `my_adapter.py`).
2. Subclass `BaseGenericAdapter` implementing required abstract methods.
3. Decide config surface: introduce a new top‑level section (e.g., `my_ports`) and document its schema; 
4. Register plugin: modify `_register_built_in_plugins()` or externally call `GenericAdapterFactory.register_external_plugin(AdapterPlugin("Pretty Name", "my_ports", MyAdapter))` before server start.
5. Implement `validate_config` to quickly fail invalid user config.
6. Define `get_capabilities()` (include `PROVIDES_PORTS` and/or `ACCEPTS_CONNECTIONS` as appropriate).
7. Implement port lifecycle methods if providing ports.
8. Ensure `start()` sets `is_running = True` after successful initialization; call `await self.load_configured_ports()` if ports are defined statically.
9. (Optional) Provide `get_status_info()` returning a dict with fields used in logging (e.g. endpoint, clients, ports, type) for better monitoring.

---
## 8. Error Handling & States
- Port states tracked in `DynamicPortManager.port_states` using `PortState` enum (CONFIGURED → CREATING → ACTIVE). Failures set DESTROYED.
- Adapter start failures are logged; server continues with remaining adapters.
- Validation errors (adapter factory) prevent instantiation.
- Reconnection workflow (future) will use `attempt_port_reconnection()` → `create_port_dynamically()`.

---
## 9. Extensibility Points
| Extension | How |
|-----------|-----|
| New adapter type | Add subclass + register plugin |
| External plugin package | Import on startup; call `register_external_plugin()` |
| Federation behaviors | Implement in adapter with `FEDERATION_AWARE` capability and custom lifecycle event handling |
| Custom lifecycle reactions | Override `_handle_port_*` methods |
| Hot‑plug dynamic port | Call `DynamicPortManager.create_port_dynamically()` at runtime |
| Management/console integration | Provide setter or use existing console manager injection pattern |

---
## 10. Minimal Skeleton Adapter Example
```python
from typing import Set, Dict, Any, Optional
from .base_adapter import BaseGenericAdapter, AdapterCapability

class MyAdapter(BaseGenericAdapter):
    def get_capabilities(self) -> Set[AdapterCapability]:
        return {AdapterCapability.PROVIDES_PORTS}

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> bool:
    # Expect config like {"my_ports": [{"name": "p1"}, ...]} OR entry
        return True

    def get_port_configurations(self) -> Dict[str, Dict[str, Any]]:
        entries = []
        if isinstance(self.config, dict):
            entries = self.config.get("my_ports", []) or self.config.get("ports", [])
        return {item["name"]: item for item in entries}

    async def start(self) -> bool:
        await self.load_configured_ports()
        self.is_running = True
        return True

    async def stop(self) -> None:
        await super().stop()
        self.is_running = False

    async def create_port(self, port_name: str, config: Dict[str, Any]) -> Optional[Any]:
        return object()  # Replace with real port instance

    async def destroy_port(self, port_name: str) -> None:
        pass
```

---
## 11. Order‑of‑Operations Summary (Condensed)
1. Parse CLI args → locate config.
2. Instantiate `OpenMuxServer` (logging, config, auth, port, console managers created).
3. Build unified adapters (plugin registry consulted) → attach `DynamicPortManager` to each.
4. Start unified adapters → static ports created.
5. Start connection adapters (unified, if any declare `ACCEPTS_CONNECTIONS`).
6. Run event loop until signal.
7. Shutdown: stop connection adapters → stop unified adapters (destroy ports) → close port manager.

---
## 12. Quick Glossary
- Adapter: A plugin implementing transport logic and/or virtual port provisioning.
- Port: Addressable endpoint representing a serial/loopback/command/federated channel.
- Unified Adapter: New style adapter built on `BaseGenericAdapter` + dynamic lifecycle.
- Plugin Registry: Lookup mapping config sections to adapter classes.
- Capability: Declared feature set driving orchestration decisions.

---
## 13. Fast Onboarding Tasks
1. Read `openmux/server/adapters/base_adapter.py` and `lifecycle.py`.
2. Inspect an existing simple adapter (e.g., `loopback.py`).
3. Run `make validate-config` on a sample config; study `docs/openmux_config_schema.yaml`.
4. Add a trivial adapter (copy skeleton) and register it; confirm it appears in server startup logs under "Available Plugin Types".

---
## 14. Future Evolution Notes
- Unification of legacy connection adapters into the unified adapter model (remove dual system).
- Implement reconnection scheduling + event bus for lifecycle events.
- Formal status API returning structured JSON for adapters & ports.

---
This document should give a newcomer enough precision to understand call chains, required interfaces, and extension workflow without diving blindly through the codebase.

---
## 15. Detailed Startup Procedure (End-to-End)

This chapter expands the earlier summaries (Sections 1, 5, 11) into a definitive, code‑mapped sequence from the moment a user invokes the server until it is fully operational.

### 15.1 Invocation
1. User runs (examples):
   - `python -m openmux.server.main -c config/loopback_test.yaml`
   - or wrapper script / Make target doing the same.
2. Python loads `openmux/server/main.py` and executes `main()`.

### 15.2 Argument Parsing & Config Path Resolution
3. `_parse_arguments()` reads `--config` (`-c`), `--config-dir`, `--auth-config` (`-a`), `--security-config` (`-s`), and `--verbose`, deriving defaults (e.g., `/etc/openmux/server.yaml`) when not provided.
4. `_find_config_file()` validates / locates the server YAML (tries provided path, then project `config/server.yaml`). Failure → `sys.exit(1)`.

### 15.3 Core Object Construction
5. `OpenMuxServer(config_path)` is instantiated.
6. Inside `__init__` (in order):
   a. `LoggingManager` created (baseline logging set; verbose flag may later raise level if desired).
   b. `ConfigManager` created and immediately `load_config()` invoked (YAML parsed into `self.config_manager.config`).
   c. `AuthManager` initialized with authentication slice from config.
   d. `PortManager` initialized (legacy aggregation + glue for unified adapters).
   e. `ConsoleManager` created (ties into `PortManager` and `AuthManager`).
    f. Legacy connection adapter factory removed; no backward compatibility path.
    g. Unified adapter factory: `GenericAdapterFactory()` → internally builds a `PluginRegistry` → `_register_built_in_plugins()` imports & registers built‑ins (loopback, command, tcp_initiator, serial, client_listener, muxcon, web_status, openmux_client). The factory maps per‑section config to the corresponding adapter types.
   h. Placeholders prepared: `self.adapters` (legacy connection adapters), `self.unified_adapters` (modern adapters), flags (`is_running`), and `shutdown_event` (None for now).

### 15.4 Event Loop & Signal Handlers
7. In `main()`: new asyncio loop created and set.
8. `_setup_shutdown_handlers(loop, server)`:
   - Creates `asyncio.Event` → assigned to `server.shutdown_event`.
   - Registers handlers for SIGINT/SIGTERM that set the event and schedule a graceful async shutdown.

### 15.5 Server Start Orchestration
9. `loop.run_until_complete(server.start())` begins asynchronous startup.
10. `OpenMuxServer.start()` logs banner and calls `_initialize_server_components()`.
11. `_initialize_server_components()`:
    - Spawns `_monitor_shutdown_event()` task (watches `shutdown_event`).
    - (removed) legacy pre‑unified hooks; PortManager no longer exposes initialize/close lifecycle.
    - Calls `_initialize_unified_adapters()`.
12. `_initialize_unified_adapters()`:
    - Re-loads config if necessary.
    - Uses `GenericAdapterFactory.create_adapters_from_config(config)`:
      * Config is sourced from per‑section keys
      * Else: discover active plugins by presence of per‑section keys (e.g. `loopback_ports`).
      * Each instantiation wraps list sections into dicts if needed and attaches a fresh `DynamicPortManager(adapter)` (setting `adapter.port_manager`).
    - After collection: `self.port_manager.set_unified_adapters(self.unified_adapters)` integrates them.
    - Sets `adapter.main_port_manager = self.port_manager` (back‑reference) where attribute exists.
    - Sequentially `await adapter.start()` for each unified adapter:
      * Adapter typically: validates internal config, calls `load_configured_ports()` (iterates `get_port_configurations()` map) → each port creation delegates to `DynamicPortManager.create_port_dynamically()` → invokes adapter `create_port()`.
      * On success sets `adapter.is_running = True`.
    - Failures logged; startup continues (partial availability is allowed).

### 15.6 Deciding Connection Adapter Path
13. `_create_and_configure_adapters()` determines connection endpoints purely from unified adapters:
    - Scans unified adapters for `AdapterCapability.ACCEPTS_CONNECTIONS`.
    - Injects dependencies: auth / console managers (port manager is already referenced via `main_port_manager`).

### 15.7 Starting Connection Endpoints
14. `_start_all_adapters()` orchestration:
    a. Partition unified adapters: connection vs port‑only.
    b. If connection‑capable unified adapters exist: ensure each is running (start if not already started in step 12).
15. Count of successfully started connection adapters logged; zero triggers error condition and aborts startup (returns False → overall start fails).

### 15.8 Readiness & Status Logging
16. `self.is_running = True` set after at least one connection adapter (unified) is active.
17. `_log_server_status()` builds a categorized view:
    - Unified connection adapters (if any) with endpoint + client counts.
    - Legacy connection adapters (else) with host:port.
    - Unified port adapters (non‑connection) with port list summary.
    - Plugin registry entries (available plugin types) for operator visibility.
18. User now sees listening endpoints (TCP, management, web, etc.) and can connect clients.

### 15.9 Steady State Loop
19. `_run_server_loop()` awaits an `asyncio.Event()` forever (effectively idle sentinel) while adapters process I/O on background tasks or internal loops.
20. System operational conditions:
    - At least one listening endpoint OR at least one active port adapter (depending on deployment mode).
    - All required authentication data loaded.
    - Signal handlers armed for graceful shutdown.

### 15.10 Shutdown Trigger Path
21. User sends SIGINT (Ctrl+C) or SIGTERM.
22. Signal handler sets `shutdown_event` and schedules `shutdown_coroutine()`.
23. `OpenMuxServer.shutdown()` sequence:
    - Iterates `self.unified_adapters` calling `adapter.stop()` (base class may tear down dynamic ports via `DynamicPortManager.destroy_port_dynamically()` where used).
    - Unified adapters stop (destroy ports); PortManager holds no independent lifecycle beyond adapter‑registered wrappers.
24. Remaining asyncio tasks cancelled; loop stopped.

### 15.11 Failure / Degradation Notes
| Phase | Typical Failure | Effect | Recovery Option |
|-------|-----------------|--------|-----------------|
| Config load | YAML parse or missing file | Immediate exit | Fix file & restart |
| Plugin import | Missing optional dependency | Adapter skipped | Install dependency, restart |
| Adapter start | Port binding failure (in use) | Adapter omitted | Free port, restart |
| Port creation | Invalid per‑port config | Port absent | Correct config, future hot‑add (planned) |
| Connection start | All endpoints fail | Startup aborts | Adjust config, restart |

### 15.12 Extension Hooks During Startup
You can inject custom behavior at deterministic points:
| Hook Point | How |
|------------|-----|
| Before unified adapter creation | Pre-import module that registers external plugin via `register_external_plugin()` | 
| After adapters instantiated, before start | Iterate `server.unified_adapters` (e.g. instrumentation) | 
| After ports created | Override `_handle_port_created()` in adapter | 
| After readiness log | Extend `_log_server_status()` or add management command to query state | 

### 15.13 Quick Visual (Condensed Sequence)
```
CLI → main() → parse args → resolve config → OpenMuxServer.__init__
  → (Logging, Config, Auth, Port, Console, Factories, Registry)
  → event loop + signals
  → start(): init components → initialize unified adapters
      → create adapters → attach DynamicPortManager → adapter.start() → ports
    → enable connection endpoints (if any unified adapters accept connections)
  → start connection adapters (servers then clients)
  → mark running + status log → steady event wait
```

This completes the authoritative startup narrative capturing order, decision branches, and recovery behaviors.

---
## 16. Federation Identity & Multipath Grouping

This section codifies how MuxCon federation connections establish and expose stable identities, how multiple transport paths between the same peer are grouped, and how process restarts are detected and rolled forward without manual intervention.

### 16.1 Terminology
- **server_id**: Stable configured identifier for a node in the federation. Defaults to the host name if not explicitly set. Intended to remain constant across process restarts.
- **instance_id**: Ephemeral UUID generated at process start. Changes on every restart; used to distinguish old vs new generations of connections from the same `server_id`.
- **node_name**: Optional user‑friendly alias (also used historically). When present it is treated the same as `server_id` for grouping precedence; both are surfaced using the unified `node:` key prefix for compatibility.

### 16.2 Handshake Fields
ASCII handshake lines (client → server):
```
HELLO MuxCon/1.0 TYPE=<client_type> ID=<server_id> INST=<instance_uuid>
```
Server replies:
```
OK MuxCon/1.0 ID=<server_id> INST=<instance_uuid> [optional capability echo]
```

### 16.3 Grouping Algorithm (Multipath)
Connections are grouped to allow failover / path preference decisions. A **peer key** is derived in the adapter:
1. If handshake supplies `node_name`: `node:<node_name>`
2. Else if handshake supplies `server_id`: `node:<server_id>` (note: still prefixed with `node:` – no `srv:` prefix is used)
3. Else (pre‑handshake inbound) collapse by source host: `host:<ip>` (ephemeral until identity known)
4. Fallback: `unknown:0`

After handshake completion the connection is re‑keyed if its provisional key changes (e.g. from `host:1.2.3.4` → `node:alpha`). If a group becomes empty the old placeholder group is removed.

### 16.4 Path Preference & Primary Selection
Each connection in a group tracks:
- `pref` (integer) – higher wins for preemptive promotion when strategy is `best_pref`.
- `opened_at`, `last_seen` timestamps.
- Staleness: a connection is considered stale if its `last_seen` is older than `mpath_primary_stale_sec`.

Primary selection logic:
- If current primary missing or stale and an alternative exists, promote the best candidate (highest `pref`, then newest `opened_at`).
- Preemptive promotion (configurable) can replace a lower preference primary with a higher one even if not stale.

### 16.5 Restart / Generation Rollover
When a new connection arrives with the same `server_id` but a different `instance_id`, the adapter compares `opened_at` times:
- Newer instance_id ⇒ retire (close) all older generation connections for that `server_id`.
- If an unexpected older connection appears after a newer one (rare race), the logic may retire the current (newer) connection instead—this avoids split brain; the peer will typically reconnect promptly restoring the latest generation.

This ensures only one active generation per `server_id` while allowing a restart to cleanly supersede existing paths without waiting for TCP timeouts.

### 16.6 Web Status & API Exposure
`/api/federation` (web_status adapter) includes per‑connection:
```
handshake.server_id
handshake.instance_id
```
`/api/multipath` returns per group:
```
peer_key
connections[...].server_id / instance_id
server_ids[]              # distinct server_ids observed (should normally be size 1)
instance_ids[]            # distinct instance_ids ( >1 briefly indicates rollover window )
distinct_instances        # count shortcut
```

### 16.7 Logging Conventions
- Handshake success (client & server sides) logs include: remote/local `server_id` and `instance_id`.
- Rollover events log the retirement of old generation connections with the surviving connection id.

### 16.8 Operational Guidance
- To verify a restart propagated: check `/api/multipath` for the group’s `instance_ids` shrinking back to one value shortly after new connections appear.
- If multiple `instance_ids` persist abnormally: investigate network freezes or fault injection states preventing closure of old connections.
- Automation can watch for `distinct_instances > 1` beyond a threshold and trigger an alert.

### 16.9 Future Enhancements (Planned)
- Explicit `generation` integer in handshake (monotonic) to avoid relying on `opened_at` ordering.
- Signed identity envelope (TLS client cert binding) for stronger peer authenticity.
- Optional replication of active stream state across paths (true multipath aggregation) instead of single-primary model.

This section formalizes the identity model so operators and integrators can rely on stable semantics for monitoring and tooling.
