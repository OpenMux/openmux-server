"""
TCP Initiator Adapter for OpenMux - Outbound raw TCP/TLS connections

This adapter initiates outbound TCP/SSL connections to remote services like:
- Network devices (switches, routers, firewalls)
- Telnet servers
- Custom TCP-based console services

Configuration (list-of-dicts under tcp_initiator_ports):
        tcp_initiator_ports:
                - name: network_switch
                    host: 192.168.1.100
                    port: 23
                    description: "Main network switch console"

                - name: firewall_mgmt
                    host: firewall.example.com
                    port: 443
                    use_tls: true
                    description: "Firewall management interface"

Legacy compatibility:
- The legacy section key "client_initiator_ports" is still accepted (deprecated).
"""

import asyncio
import logging
import ssl
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from .base_adapter import AdapterCapability, BaseGenericAdapter
from .lifecycle import PortLifecycleEvent, PortState
from .protocols import get_handler
from .protocols.base import TcpProtocolHandler


class TcpInitiatorPort:
    """Individual TCP initiator port connection.

    Contract reference: docs/ADAPTER_PORT_CONTRACT.md
    """

    state: PortState  # enforced contract annotation
    is_connected: bool  # enforced contract annotation (network readiness flag)

    def __init__(self, name: str, config: Dict[str, Any], adapter: "TcpInitiatorAdapter"):
        """Initialize a TCP initiator port instance.

        Args:
            name: Logical port name (unique within the adapter).
            config: Per-port configuration (host, port, TLS, timeouts, batching).
            adapter: Owning adapter instance.
        """
        self.name = name
        self.config = config
        self.adapter = adapter
        self.logger = logging.getLogger(f"tcp_initiator.{name}")
        self.state = PortState.CONFIGURED

        # Connection configuration
        self.host = config.get("host", "")
        self.port = config.get("port", 0)
        self.use_tls = bool(config.get("use_tls", False))
        self.ssl_verify = config.get("ssl_verify", True)
        self.timeout = config.get("timeout", 10.0)
        self.auto_reconnect = config.get("auto_reconnect", True)
        self.reconnect_delay = config.get("reconnect_delay", 5.0)
        self.enabled = bool(config.get("enabled", True))

        # Connect-on-demand: stay disconnected until a user actively opens the port
        self.connect_on_demand: bool = bool(config.get("connect_on_demand", False))
        self.disconnect_when_idle: bool = bool(config.get("disconnect_when_idle", False))
        self.idle_disconnect_delay: float = float(config.get("idle_disconnect_delay", 30.0))
        self._active_clients: int = 0
        self._idle_disconnect_task: Optional[asyncio.Task] = None
        protocol_cfg = config.get("protocol", {})
        protocol_type: str = (protocol_cfg.get("type", "") or "plain").lower()
        self._protocol_handler: TcpProtocolHandler = get_handler(protocol_type, config)

        # Connection state
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None

        # Batching buffer for outgoing data
        # Default batching ON for plain, OFF for protocols that have their own framing
        _default_batching = protocol_type == "plain"
        self._write_buffer = bytearray()
        self._write_buffer_lock = asyncio.Lock()
        self._flush_task = None
        self._flush_event = asyncio.Event()
        self._batch_size = config.get("batch_size", 1024)  # bytes
        self._batch_timeout = config.get("batch_timeout", 0.015)  # seconds (5ms)
        self._batching_enabled = config.get("enable_batching", _default_batching)

        # Connection state
        self.is_connected = False
        self.reconnect_task = None
        self.read_task = None

        # Data callback: callable(port_name: str, data: bytes) -> Optional[Awaitable]
        self.data_callback: Optional[Callable[[str, bytes], Any]] = None

        # Timestamp (monotonic) of the last connection-failure warning;
        # used to rate-limit the message to at most once per hour.
        self._last_failed_warn_ts: Optional[float] = None

        # Validate required configuration
        if not self.host:
            raise ValueError(f"TCP initiator port {self.name} requires 'host' configuration")
        if not self.port:
            raise ValueError(f"TCP initiator port {self.name} requires 'port' configuration")

    async def start(self) -> bool:
        """Start the TCP initiator port (non-blocking)."""
        if not self.enabled:
            self.logger.info(f"TCP initiator port {self.name} is disabled, skipping connection")
            self.state = PortState.ACTIVE
            return True
        if self.connect_on_demand:
            self.logger.info(f"TCP initiator port {self.name} is connect-on-demand, waiting for users")
            self.state = PortState.ACTIVE
            return True
        self.logger.info(f"Starting TCP initiator port {self.name} (will connect in background)")
        self.state = PortState.CREATING
        self.reconnect_task = asyncio.create_task(self._connection_manager())
        self.state = PortState.ACTIVE
        return True

    async def stop(self) -> None:
        """Stop the TCP initiator port and cancel background tasks."""
        self.logger.info(f"Stopping TCP initiator port {self.name}")
        if self._idle_disconnect_task and not self._idle_disconnect_task.done():
            self._idle_disconnect_task.cancel()
            try:
                await self._idle_disconnect_task
            except asyncio.CancelledError:
                pass
            self._idle_disconnect_task = None
        if self.reconnect_task:
            self.reconnect_task.cancel()
            try:
                await self.reconnect_task
            except asyncio.CancelledError:
                pass
            self.reconnect_task = None
        if self.read_task:
            self.read_task.cancel()
            try:
                await self.read_task
            except asyncio.CancelledError:
                pass
            self.read_task = None
        await self._disconnect()

    async def _connect(self) -> bool:
        """Establish the outbound connection via the configured protocol handler."""
        if self.is_connected:
            return True
        try:
            self._log_connect_attempt(
                f"Connecting to {self.host}:{self.port} "
                f"(protocol: {self.config.get('protocol', {}).get('type', 'plain')}, "
                f"TLS: {self.use_tls})"
            )
            self.reader, self.writer = await self._protocol_handler.establish(
                self.host, self.port, self.config
            )
            self.is_connected = True
            self.logger.info(f"Successfully connected to {self.host}:{self.port}")
            # Reset so the next disconnect logs immediately
            self._last_failed_warn_ts = None
            self.read_task = asyncio.create_task(self._read_loop())
            return True
        except asyncio.TimeoutError:
            self._log_connect_failure(f"Connection timeout to {self.host}:{self.port} (after {self.timeout}s)")
            return False
        except ConnectionRefusedError as e:
            self._log_connect_failure(f"Connection refused to {self.host}:{self.port}: {e}")
            return False
        except ConnectionError as e:
            self._log_connect_failure(f"Protocol handshake failed for {self.host}:{self.port}: {e}")
            return False
        except Exception as e:
            self._log_connect_failure(f"Connection failed to {self.host}:{self.port}: {e}")
            return False

    def _log_connect_attempt(self, message: str) -> None:
        """Log a connection-attempt message at INFO first time, DEBUG while in known-bad state."""
        import time as _time
        now = _time.monotonic()
        if self._last_failed_warn_ts is None or (now - self._last_failed_warn_ts) >= 3600:
            self.logger.info(message)
        else:
            self.logger.debug(message)

    def _log_connect_failure(self, message: str) -> None:
        """Log a connection-failure message, rate-limited to once per hour."""
        import time as _time
        now = _time.monotonic()
        if self._last_failed_warn_ts is None or (now - self._last_failed_warn_ts) >= 3600:
            self.logger.warning(message)
            self._last_failed_warn_ts = now
        else:
            self.logger.debug(message)

    async def _disconnect(self) -> None:
        """Close the active TCP connection and reset stream state."""
        if not self.is_connected:
            return
        try:
            if self.writer:
                self.writer.close()
                if hasattr(self.writer, "wait_closed"):
                    await self.writer.wait_closed()
            self.logger.info(f"Disconnected from {self.host}:{self.port}")
        except Exception as e:
            self.logger.error(f"Error disconnecting from {self.host}:{self.port}: {e}", exc_info=True)
        finally:
            self.is_connected = False
            self.reader = None
            self.writer = None

    async def _read_loop(self) -> None:
        """Continuously read inbound data until connection closes or cancelled."""
        try:
            while self.is_connected and self.reader:
                try:
                    data = await self.reader.read(4096)
                    if not data:
                        self.logger.info(f"Connection to {self.host}:{self.port} closed by remote")
                        self.is_connected = False
                        break
                    decoded = self._protocol_handler.decode(data)
                    if decoded:
                        await self._handle_received_data(decoded)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.logger.error(f"Error reading from {self.host}:{self.port}: {e}", exc_info=True)
                    self.is_connected = False
                    break
        except asyncio.CancelledError:
            pass

    async def _handle_received_data(self, data: bytes) -> None:
        """Forward received data to the registered port manager callback."""
        if self.data_callback:
            try:
                if asyncio.iscoroutinefunction(self.data_callback):
                    await self.data_callback(self.name, data)  # type: ignore[arg-type]
                else:
                    self.data_callback(self.name, data)  # type: ignore[arg-type]
            except Exception as e:
                self.logger.error(f"Data callback error on {self.name}: {e}", exc_info=True)

    async def _monitor_connection(self) -> None:
        """Monitor connection and attempt reconnection when disconnected."""
        try:
            while True:
                await asyncio.sleep(1.0)
                if not self.is_connected and self.auto_reconnect:
                    if self.connect_on_demand and self._active_clients == 0:
                        # No users present — stop reconnecting; on_client_count_changed will restart
                        break
                    success = await self._connect()
                    if not success:
                        await asyncio.sleep(self.reconnect_delay)
        except asyncio.CancelledError:
            pass

    async def _connection_manager(self) -> None:
        """Drive initial connect attempt then (optionally) monitor reconnects."""
        try:
            await self._connect()
            if self.auto_reconnect:
                await self._monitor_connection()
        except asyncio.CancelledError:
            self.logger.info(f"Connection manager for {self.name} cancelled")
        except Exception as e:
            self.logger.error(f"Connection manager error for {self.name}: {e}", exc_info=True)

    def on_client_count_changed(self, count: int) -> None:
        """Called by the port manager when the number of connected clients changes.

        When ``connect_on_demand`` is enabled, the first arriving user triggers
        the outbound connection; the last departing user may schedule an idle
        disconnect if ``disconnect_when_idle`` is also set.
        """
        self._active_clients = count
        if not self.connect_on_demand:
            return

        # Cancel any pending idle-disconnect timer whenever client count changes
        if self._idle_disconnect_task and not self._idle_disconnect_task.done():
            self._idle_disconnect_task.cancel()
            self._idle_disconnect_task = None

        if count > 0:
            # Trigger connection if not already running
            if not self.is_connected and (
                self.reconnect_task is None or self.reconnect_task.done()
            ):
                self.logger.info(f"Port {self.name}: user connected, starting on-demand connection")
                self.reconnect_task = asyncio.create_task(self._connection_manager())
        else:
            # Last user left
            if self.disconnect_when_idle and self.is_connected:
                self._idle_disconnect_task = asyncio.create_task(self._idle_disconnect())

    async def _idle_disconnect(self) -> None:
        """Disconnect after ``idle_disconnect_delay`` seconds with no active users."""
        try:
            await asyncio.sleep(self.idle_disconnect_delay)
            if self._active_clients == 0:
                self.logger.info(
                    f"Port {self.name}: disconnecting after {self.idle_disconnect_delay}s idle"
                )
                if self.reconnect_task and not self.reconnect_task.done():
                    self.reconnect_task.cancel()
                    try:
                        await self.reconnect_task
                    except asyncio.CancelledError:
                        pass
                    self.reconnect_task = None
                await self._disconnect()
        except asyncio.CancelledError:
            pass

    async def write_data(self, data: bytes) -> int:
        """Write data to the remote endpoint (optionally batched)."""
        if not self.is_connected or not self.writer:
            self.logger.warning(f"Cannot write to {self.name}: not connected")
            return 0
        if not self._batching_enabled:
            try:
                self.writer.write(self._protocol_handler.encode(data))
                await self.writer.drain()
                return len(data)
            except Exception as e:
                self.logger.error(f"Error writing to {self.name}: {e}", exc_info=True)
                self.is_connected = False
                return 0
        async with self._write_buffer_lock:
            self._write_buffer += data
            queued = len(data)
            if len(self._write_buffer) >= self._batch_size:
                self._flush_event.set()
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_buffer_loop())
        return queued

    async def _flush_buffer_loop(self):
        """Flush buffered outbound data based on size threshold or timeout."""
        import time

        while True:
            try:
                await asyncio.wait_for(self._flush_event.wait(), timeout=self._batch_timeout)
            except asyncio.TimeoutError:
                pass
            self._flush_event.clear()
            async with self._write_buffer_lock:
                if not self._write_buffer:
                    break
                to_send = bytes(self._write_buffer)
                self._write_buffer.clear()
            try:
                if not self.writer:
                    self.logger.error(f"Writer is None while flushing batched data to {self.name}")
                    self.is_connected = False
                    break
                start = time.perf_counter()
                self.writer.write(self._protocol_handler.encode(to_send))
                await self.writer.drain()
                elapsed = time.perf_counter() - start
                self.logger.info(
                    f"TCP BATCH PROFILE: Flushed {len(to_send)} bytes to {self.host}:{self.port} in {elapsed:.6f}s (batch_size={self._batch_size}, batch_timeout={self._batch_timeout})"
                )
            except Exception as e:
                self.logger.error(f"Error flushing batched data to {self.name}: {e}", exc_info=True)
                self.is_connected = False
                break
            async with self._write_buffer_lock:
                if not self._write_buffer:
                    break


class TcpInitiatorAdapter(BaseGenericAdapter):
    """Unified TCP Initiator Adapter

    Creates outbound TCP/SSL connections to remote services.
    """

    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        self.ports: Dict[str, TcpInitiatorPort] = {}
        self.logger = logging.getLogger(f"openmux.adapter.tcp_initiator.{name}")

    def get_capabilities(self) -> Set[AdapterCapability]:
        return {
            AdapterCapability.MAKES_CONNECTIONS,
            AdapterCapability.PROVIDES_PORTS,
            AdapterCapability.BIDIRECTIONAL_DATA,
        }

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> bool:
        """Validate adapter configuration structure.

        Supports two forms:
        1. Dict containing key ``tcp_initiator_ports`` or ``openmux_client_ports`` (compat alias)
           with list of port dicts.
        2. Top-level list of port dicts (legacy style) or legacy key ``client_initiator_ports``.
        """
        from .protocols import PROTOCOL_HANDLERS

        cfg = config.get("tcp_initiator_ports", config)
        is_openmux_compat = False
        if cfg is config:
            cfg = config.get("openmux_client_ports", config)
            if cfg is not config:
                is_openmux_compat = True
        if cfg is config:
            cfg = config.get("client_initiator_ports", config)
            # client_initiator_ports is a legacy plain-TCP alias, not openmux
        if not isinstance(cfg, list):
            return False
        for item in cfg:
            if not isinstance(item, dict):
                return False
            if not item.get("name"):
                return False
            if not item.get("host"):
                return False
            if not item.get("port"):
                return False
            # For compat sections (openmux_client_ports), inject protocol sub-key
            # before delegating to the handler's validate_config
            validate_item = (
                cls._inject_openmux_protocol(item)
                if is_openmux_compat and "protocol" not in item
                else item
            )
            prot = validate_item.get("protocol", {})
            ptype = (prot.get("type", "") or "plain").lower()
            handler_cls = PROTOCOL_HANDLERS.get(ptype)
            if handler_cls is not None:
                problems = handler_cls.validate_config(validate_item)
                if problems:
                    return False
        return True

    async def create_port(self, port_name: str, config: Dict[str, Any]) -> Optional[Any]:
        try:
            tcp_port = TcpInitiatorPort(port_name, config, self)
            self.wire_port_data_callback(tcp_port, self._handle_port_data)
            if await tcp_port.start():
                self.ports[port_name] = tcp_port
                self.logger.info(f"TCP initiator port {port_name} created successfully")
                if hasattr(self, "main_port_manager") and self.main_port_manager:
                    await self.main_port_manager.register_unified_port(port_name, tcp_port, self)
                return tcp_port
            else:
                self.logger.error(f"Failed to start TCP initiator port {port_name}")
                return None
        except Exception as e:
            self.logger.error(f"Error creating TCP initiator port {port_name}: {e}", exc_info=True)
            return None

    async def destroy_port(self, port_name: str) -> None:
        tcp_port = self.ports.get(port_name)
        if tcp_port:
            try:
                if hasattr(self, "main_port_manager") and self.main_port_manager:
                    await self.main_port_manager.unregister_unified_port(port_name)
                await tcp_port.stop()
                del self.ports[port_name]
                self.logger.info(f"TCP initiator port {port_name} destroyed")
            except Exception as e:
                self.logger.error(f"Error destroying TCP initiator port {port_name}: {e}", exc_info=True)

    def get_port_configurations(self) -> Dict[str, Dict[str, Any]]:
        root = self.config
        items: List[Dict[str, Any]]
        if isinstance(root, dict) and isinstance(root.get("tcp_initiator_ports"), list):
            items = root["tcp_initiator_ports"]
        elif isinstance(root, dict) and isinstance(root.get("openmux_client_ports"), list):
            # Compat alias: inject protocol sub-key so TcpInitiatorPort uses OpenMuxHandler
            items = [self._inject_openmux_protocol(i) for i in root["openmux_client_ports"]]
        elif isinstance(root, dict) and isinstance(root.get("client_initiator_ports"), list):  # legacy
            items = root["client_initiator_ports"]
        elif isinstance(root, list):
            items = root
        else:
            return {}
        result: Dict[str, Dict[str, Any]] = {}
        for item in items:
            if isinstance(item, dict) and item.get("name"):
                result[item["name"]] = dict(item)
        return result

    @staticmethod
    def _inject_openmux_protocol(item: Dict[str, Any]) -> Dict[str, Any]:
        """Translate a legacy openmux_client_ports entry to tcp_initiator format.

        Lifts ``remote_port``, ``api_key``, ``username``, ``password`` into a
        ``protocol:`` sub-key so the unified handler can pick them up.
        """
        if "protocol" in item:
            return item  # already in new format
        merged = dict(item)
        merged["protocol"] = {
            "type": "openmux",
            "remote_port": item.get("remote_port", ""),
            "api_key": item.get("api_key", ""),
            "username": item.get("username", ""),
            "password": item.get("password", ""),
        }
        return merged

    def get_adapter_type(self) -> str:
        """Return adapter type identifier."""
        return "tcp_initiator"

    async def start(self) -> bool:
        self.logger.info(f"Starting TCP initiator adapter {self.name}")
        ports_config = self.get_port_configurations()
        if not ports_config:
            self.logger.warning(f"No ports configured for TCP initiator adapter {self.name}")
            return True
        success_count = 0
        for port_name, port_config in ports_config.items():
            try:
                tcp_port = await self.create_port(port_name, port_config)
                if tcp_port:
                    success_count += 1
                    self.logger.info(f"TCP initiator port {port_name} started (connecting in background)")
                else:
                    self.logger.error(f"Failed to start TCP initiator port {port_name}")
            except Exception as e:
                self.logger.error(f"Error creating TCP initiator port {port_name}: {e}", exc_info=True)
        self.logger.info(
            f"TCP initiator adapter {self.name} started with {success_count}/{len(ports_config)} ports (connections in progress)"
        )
        if success_count > 0:
            self.is_running = True
        return success_count > 0

    async def stop(self) -> None:
        self.logger.info(f"Stopping TCP initiator adapter {self.name} with {len(self.ports)} ports")
        for port_name in list(self.ports.keys()):
            try:
                self.logger.info(f"Stopping TCP initiator port {port_name}")
                await self.destroy_port(port_name)
            except Exception as e:
                self.logger.error(f"Error stopping TCP initiator port {port_name}: {e}", exc_info=True)
        self.is_running = False
        self.logger.info(f"TCP initiator adapter {self.name} stopped")

    async def write_to_port(self, port_name: str, data: bytes) -> int:
        tcp_port = self.ports.get(port_name)
        if not tcp_port:
            self.logger.error(f"TCP initiator port {port_name} not found")
            return 0
        success = await tcp_port.write_data(data)
        return len(data) if success else 0

    def get_status_info(self) -> Dict[str, Any]:
        total = len(self.ports)
        connected = sum(1 for p in self.ports.values() if p.is_connected)
        return {
            "type": "TCPInitiator",
            "status": "running" if self.is_running else "stopped",
            "ports": f"{total} ports",
            "connected": f"{connected}/{total}",
            "details": {
                "adapter_name": self.name,
                "connected_ports": [name for name, p in self.ports.items() if p.is_connected],
            },
        }

    async def _handle_port_data(self, port_name: str, data: bytes) -> None:
        if hasattr(self, "main_port_manager") and self.main_port_manager:
            await self.main_port_manager.send_data(port_name, data)
        else:
            self.logger.debug(f"No main port manager available, dropping {len(data)} bytes from port {port_name}")

    # --- Live configuration reconciliation ---
    async def reconcile_ports(self, new_config: Any) -> Dict[str, Any]:
        """Incrementally reconcile tcp_initiator ports.

        Args:
            new_config: Dict with key 'tcp_initiator_ports' (or legacy 'client_initiator_ports') as list,
                        or a direct list of port dicts.

        Returns:
            Summary: {added, removed, updated, unchanged}.
        """
        # Normalize
        if isinstance(new_config, dict):
            if isinstance(new_config.get("tcp_initiator_ports"), list):
                items = list(new_config["tcp_initiator_ports"])  # shallow copy
            elif isinstance(new_config.get("openmux_client_ports"), list):
                items = [self._inject_openmux_protocol(i) for i in new_config["openmux_client_ports"]]
            elif isinstance(new_config.get("client_initiator_ports"), list):
                items = list(new_config["client_initiator_ports"])  # legacy
            else:
                items = []
        elif isinstance(new_config, list):
            items = list(new_config)
        else:
            items = []

        new_by_name: Dict[str, Dict[str, Any]] = {}
        for p in items:
            if isinstance(p, dict) and p.get("name"):
                new_by_name[str(p["name"])] = p

        old_names = set(self.ports.keys())
        new_names = set(new_by_name.keys())
        removed = sorted(old_names - new_names)
        added = sorted(new_names - old_names)
        common = sorted(old_names & new_names)

        def _material_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
            # Apply the same defaults as TcpInitiatorPort.__init__ so comparison is apples-to-apples
            protocol_cfg = cfg.get("protocol", {})
            protocol_type = (protocol_cfg.get("type", "") or "plain").lower()
            _default_batching = protocol_type == "plain"
            return {
                "host": cfg.get("host", ""),
                "port": cfg.get("port", 0),
                "use_tls": bool(cfg.get("use_tls", False)),
                "ssl_verify": cfg.get("ssl_verify", True),
                "timeout": cfg.get("timeout", 10.0),
                "auto_reconnect": cfg.get("auto_reconnect", True),
                "reconnect_delay": cfg.get("reconnect_delay", 5.0),
                "enable_batching": cfg.get("enable_batching", _default_batching),
                "batch_size": cfg.get("batch_size", 1024),
                "batch_timeout": cfg.get("batch_timeout", 0.015),
                "enabled": bool(cfg.get("enabled", True)),
                "connect_on_demand": bool(cfg.get("connect_on_demand", False)),
                "disconnect_when_idle": bool(cfg.get("disconnect_when_idle", False)),
                "idle_disconnect_delay": float(cfg.get("idle_disconnect_delay", 30.0)),
                "protocol": protocol_cfg,
            }

        updated: List[str] = []
        unchanged: List[str] = []
        for n in common:
            port = self.ports.get(n)
            old_cfg: Dict[str, Any] = {}
            if port is not None:
                try:
                    old_cfg = {
                        "host": getattr(port, "host", None),
                        "port": getattr(port, "port", None),
                        "use_tls": getattr(port, "use_tls", None),
                        "ssl_verify": getattr(port, "ssl_verify", None),
                        "timeout": getattr(port, "timeout", None),
                        "auto_reconnect": getattr(port, "auto_reconnect", None),
                        "reconnect_delay": getattr(port, "reconnect_delay", None),
                        # Note: these fields are stored under private names in TcpInitiatorPort
                        "enable_batching": getattr(port, "_batching_enabled", None),
                        "batch_size": getattr(port, "_batch_size", None),
                        "batch_timeout": getattr(port, "_batch_timeout", None),
                        "enabled": getattr(port, "enabled", True),
                        "connect_on_demand": getattr(port, "connect_on_demand", False),
                        "disconnect_when_idle": getattr(port, "disconnect_when_idle", False),
                        "idle_disconnect_delay": getattr(port, "idle_disconnect_delay", 30.0),
                        "protocol": getattr(port, "config", {}).get("protocol", {}),
                    }
                except Exception:
                    old_cfg = {}
            _new_mat = _material_cfg(new_by_name[n])
            _untracked = set(_new_mat.keys()) - set(old_cfg.keys())
            if _untracked:
                self.logger.error(
                    f"[BUG] reconcile_ports: _material_cfg has keys not tracked in old_cfg: "
                    f"{sorted(_untracked)} — add them to old_cfg to ensure changes are detected."
                )
            if old_cfg == _new_mat:
                try:
                    desc = new_by_name[n].get("description")
                    if isinstance(desc, str) and desc:
                        setattr(port, "description", desc)
                except Exception:
                    pass
                unchanged.append(n)
            else:
                updated.append(n)

        # Remove then recreate updated; remove deleted
        for n in removed + updated:
            try:
                await self.destroy_port(n)
            except Exception as e:
                self.logger.error(f"Failed to destroy TCP initiator port {n}: {e}", exc_info=True)

        for n in added + updated:
            cfg = new_by_name.get(n)
            if not cfg:
                continue
            try:
                await self.create_port(n, cfg)
            except Exception as e:
                self.logger.error(f"Failed to create TCP initiator port {n}: {e}", exc_info=True)

        # Update config snapshot
        try:
            self.config["tcp_initiator_ports"] = [new_by_name[k] for k in sorted(new_by_name.keys())]
        except Exception:
            pass

        return {"added": added, "removed": removed, "updated": updated, "unchanged": unchanged}
