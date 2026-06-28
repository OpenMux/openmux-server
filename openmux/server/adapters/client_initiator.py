"""OpenMux Client Adapter.

Creates outbound connections to remote OpenMux servers using the official
OpenMux client protocol (authenticate + ``connect_to_port``) and exposes
them as local ports within the unified adapter subsystem.

Configuration (list-of-dicts) expected in top-level section ``openmux_client_ports``::

        openmux_client_ports:
            - name: <local_port_name>
                host: <remote host>            # required
                port: <remote tcp port>        # required
                remote_port: <remote portname> # required (on the remote server)
                # authentication (one of):
                api_key: <key>
                # or
                username: <user>
                password: <pass>
                use_tls: false        # optional TLS
                timeout: 10.0         # optional seconds
                auto_reconnect: true  # optional
                reconnect_delay: 5.0  # optional seconds

Following the unified adapter config rules, ``adapter_type`` is not
required in this section since the context is explicit.
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from .base_adapter import AdapterCapability, BaseGenericAdapter
from .lifecycle import PortState


class OpenMuxClientPort:
    """Represents a single remote OpenMux server port connection.

    Manages connection lifecycle (connect / authenticate / reconnect), async
    read loop, and data forwarding into the unified port manager.

    Contract: See docs/ADAPTER_PORT_CONTRACT.md for required semantics of
    write_data, lifecycle, and data_callback behavior.
    """

    state: PortState  # enforced contract annotation
    is_connected: bool  # enforced contract annotation (network readiness flag)

    def __init__(
        self,
        name: str,
        config: Dict[str, Any],
        adapter: "OpenMuxClientAdapter",
    ):
        self.name = name
        self.config = config
        self.adapter = adapter
        self.logger = logging.getLogger(f"openmux.adapter.client_initiator.{name}")
        self.state = PortState.CONFIGURED

        # Connection configuration
        self.host: str = config.get("host", "")
        self.port: int = int(config.get("port", 0))
        self.remote_port: str = config.get("remote_port", "")
        self.use_tls: bool = bool(config.get("use_tls", False))
        self.timeout: float = float(config.get("timeout", 10.0))
        self.auto_reconnect: bool = bool(config.get("auto_reconnect", True))
        self.reconnect_delay: float = float(config.get("reconnect_delay", 5.0))

        # Auth config
        self.api_key: Optional[str] = config.get("api_key")
        self.username: Optional[str] = config.get("username")
        self.password: Optional[str] = config.get("password")

        # State
        self.conn = None  # type: ignore[attr-defined]
        self.is_connected: bool = False
        self.monitor_task: Optional[asyncio.Task] = None
        self.read_task: Optional[asyncio.Task] = None

        # Data callback injected by adapter -> port manager
        self.data_callback: Optional[Callable[[str, bytes], Awaitable[None]]] = None

        # Validate required configuration
        if not self.host:
            raise ValueError(f"OpenMux client port {name} requires 'host' configuration")
        if not self.port:
            raise ValueError(f"OpenMux client port {name} requires 'port' configuration")
        if not self.remote_port:
            raise ValueError(f"OpenMux client port {name} requires 'remote_port' configuration")
        if not ((self.username and self.password) or self.api_key):
            raise ValueError(f"OpenMux client port {name} requires either api_key or username/password")

    async def start(self) -> bool:
        """Start the port (non-blocking connect manager).

        Returns:
            True once monitoring tasks are scheduled.
        """
        self.logger.info(f"Starting OpenMux client port {self.name} (will connect in background)")
        self.state = PortState.CREATING
        self.monitor_task = asyncio.create_task(self._connection_manager())
        self.state = PortState.ACTIVE
        return True

    async def stop(self) -> None:
        """Stop monitoring and disconnect from remote server."""
        self.logger.info(f"Stopping OpenMux client port {self.name}")

        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await asyncio.wait_for(self.monitor_task, timeout=0.5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self.monitor_task = None

        if self.read_task:
            self.read_task.cancel()
            try:
                await asyncio.wait_for(self.read_task, timeout=0.5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self.read_task = None

        await self._disconnect()

    async def _connect(self, log_connect_info: bool = True) -> bool:
        """Establish TCP connection, authenticate, and select remote port.

        Returns:
            True if fully connected (including port selection); False otherwise.
        """
        if self.is_connected:
            return True

        try:
            if log_connect_info:
                self.logger.info(f"Connecting to OpenMux {self.host}:{self.port} (TLS: {self.use_tls})")

            # Use production adapter abstraction instead of deprecated ServerConnection
            from openmux.client.adapters import TcpClientAdapter

            self.conn = TcpClientAdapter(host=self.host, port=self.port, config={"use_tls": self.use_tls})

            # Connect with timeout and validate result before proceeding to auth
            connect_ok = await asyncio.wait_for(self.conn.connect(), timeout=self.timeout)
            if not connect_ok:
                self.logger.warning(f"Connection failed to {self.host}:{self.port} (no TCP session or missing banner)")
                return False

            # Authenticate (only after a confirmed TCP connection + banner)
            if self.api_key:
                ok = await self.conn.authenticate_with_key(self.api_key)
            else:
                ok = await self.conn.authenticate_with_password(self.username or "", self.password or "")
            if not ok:
                self.logger.error(
                    f"Authentication failed against {self.host}:{self.port} using "
                    f"{'api_key' if self.api_key else 'username/password'}"
                )
                await self._disconnect()
                return False

            # Connect to remote port
            ok = await self.conn.connect_to_port(self.remote_port)
            if not ok:
                self.logger.error(f"Failed to connect to remote port '{self.remote_port}'")
                await self._disconnect()
                return False

            self.is_connected = True
            # Start read loop
            self.read_task = asyncio.create_task(self._read_loop())
            self.logger.info(f"Connected to OpenMux {self.host}:{self.port} remote_port='{self.remote_port}'")
            return True

        except asyncio.TimeoutError:
            self.logger.warning(f"Connection timeout to {self.host}:{self.port} (after {self.timeout}s)")
            return False
        except Exception as e:
            self.logger.warning(f"Connection failed to {self.host}:{self.port}: {e}", exc_info=True)
            return False

    async def _disconnect(self) -> None:
        """Close underlying connection if present and reset flags.

        Best-effort; suppresses and logs exceptions.
        """
        try:
            if self.conn:
                await self.conn.close()
        except Exception as e:
            self.logger.error(f"Error closing OpenMux connection: {e}", exc_info=True)
        finally:
            self.is_connected = False
            self.conn = None

    async def _read_loop(self) -> None:
        """Continuously read data frames until connection closes or errors.

        Forwards each payload to the injected ``data_callback`` (port manager)
        after normalizing to bytes. Terminates on remote close, cancellation,
        or read error.
        """
        try:
            while self.is_connected and self.conn:
                try:
                    data = await self.conn.read_data()
                    if not data:
                        # remote closed
                        self.is_connected = False
                        break
                    if self.data_callback:
                        # Adapter may return str or bytes; normalize to bytes
                        payload = data.encode() if isinstance(data, str) else data
                        await self.data_callback(self.name, payload)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.logger.error(f"Error reading from OpenMux {self.host}:{self.port}: {e}", exc_info=True)
                    self.is_connected = False
                    break
        except asyncio.CancelledError:
            pass

    async def _connection_manager(self) -> None:
        """Background task supervising connection (and reconnection).

        Initiates the initial connect, then if ``auto_reconnect`` is enabled
        polls connection health and attempts reconnects with a configurable
        delay. Suppresses cancellation cleanly.
        """
        try:
            await self._connect(log_connect_info=True)
            if self.auto_reconnect:
                while True:
                    await asyncio.sleep(1.0)
                    if not self.is_connected:
                        self.logger.info(f"Attempting to reconnect to {self.host}:{self.port}")
                        ok = await self._connect(log_connect_info=False)
                        if not ok:
                            await asyncio.sleep(self.reconnect_delay)
        except asyncio.CancelledError:
            self.logger.info(f"Connection manager for {self.name} cancelled")
        except Exception as e:
            self.logger.error(f"Connection manager error for {self.name}: {e}", exc_info=True)

    async def write_data(self, data: bytes) -> int:
        """Write raw bytes to remote port.

        Normalized: returns number of bytes accepted (len(data)) or 0 on
        failure. (Previous versions returned a bool; integer preserves
        truthiness for legacy callers performing "if await write_data(...)".)
        """
        if not self.is_connected or not self.conn:
            self.logger.warning(f"Cannot write to {self.name}: not connected")
            return 0
        try:
            ok = await self.conn.send_data(data)
            return len(data) if ok else 0
        except Exception as e:
            self.logger.error(f"Error writing to {self.name}: {e}", exc_info=True)
            self.is_connected = False
            return 0

    def __del__(self):  # best-effort cleanup to avoid pending task warnings
        try:
            for t in (self.monitor_task, self.read_task):
                if t and not t.done():
                    t.cancel()
        except Exception:
            self.logger.error("Destructor cleanup error", exc_info=True)


class OpenMuxClientAdapter(BaseGenericAdapter):
    """Unified OpenMux Client Adapter.

    Provides local virtual ports backed by remote OpenMux server ports.
    Each configured entry creates a background-managed connection object
    that handles reconnection and data forwarding.
    """

    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        self.ports: Dict[str, OpenMuxClientPort] = {}
        self.logger = logging.getLogger(f"openmux.adapter.client_initiator.{name}")

    def get_capabilities(self) -> Set[AdapterCapability]:
        """Return adapter capability flags."""
        return {
            AdapterCapability.MAKES_CONNECTIONS,
            AdapterCapability.PROVIDES_PORTS,
            AdapterCapability.BIDIRECTIONAL_DATA,
        }

    def get_adapter_type(self) -> str:
        """Return human-friendly adapter type string."""
        return "OpenMux Client"

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> bool:
        """Validate adapter configuration structure.

        Ensures the provided mapping or list conforms to the expected
        list-of-dicts schema describing client port definitions.

        Args:
            config: Raw adapter config mapping or list.

        Returns:
            True if valid, otherwise False.
        """
        if not isinstance(config, dict):
            return False

        cfg = config.get("openmux_client_ports")
        if cfg is None:
            # When invoked via unified adapters, the adapter may receive just the list
            cfg = config

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
            if not item.get("remote_port"):
                return False
            if not (item.get("api_key") or (item.get("username") and item.get("password"))):
                return False
        return True

    async def create_port(self, port_name: str, config: Dict[str, Any]) -> Optional[Any]:
        """Instantiate and start a client port.

        On success registers the port with the main port manager (if present)
        so that unified data routing is enabled.

        Args:
            port_name: Local logical port name.
            config: Per-port configuration mapping.

        Returns:
            Port instance or None on failure.
        """
        try:
            port = OpenMuxClientPort(port_name, config, self)
            self.wire_port_data_callback(port, self._handle_port_data)
            if await port.start():
                self.ports[port_name] = port
                self.logger.info(f"OpenMux client port {port_name} created successfully")
                if hasattr(self, "main_port_manager") and self.main_port_manager:
                    await self.main_port_manager.register_unified_port(port_name, port, self)
                return port
            else:
                self.logger.error(f"Failed to start OpenMux client port {port_name}")
                return None
        except Exception as e:
            self.logger.error(f"Error creating OpenMux client port {port_name}: {e}", exc_info=True)
            return None

    async def destroy_port(self, port_name: str) -> None:
        """Tear down and unregister a client port if it exists."""
        port = self.ports.get(port_name)
        if port:
            try:
                await port.stop()
                del self.ports[port_name]
                if hasattr(self, "main_port_manager") and self.main_port_manager:
                    await self.main_port_manager.unregister_unified_port(port_name)
                self.logger.info(f"OpenMux client port {port_name} destroyed")
            except Exception as e:
                self.logger.error(f"Error destroying OpenMux client port {port_name}: {e}", exc_info=True)

    def get_port_configurations(self) -> Dict[str, Dict[str, Any]]:
        """Return mapping of port names to config dictionaries.

        Returns:
            Dict keyed by local port name, each a shallow copy of original
            per-port config.
        """
        root = self.config
        items: List[Dict[str, Any]]
        if isinstance(root, dict) and isinstance(root.get("openmux_client_ports"), list):
            items = root["openmux_client_ports"]
        elif isinstance(root, list):
            # If the adapter was constructed directly with the list
            items = root
        else:
            return {}

        result: Dict[str, Dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            result[name] = dict(item)
        return result

    @property
    def adapter_type(self) -> str:
        """Internal adapter type identifier.

        Returns:
            String key used in status outputs and internal routing.
        """
        return "openmux_client"

    async def start(self) -> bool:
        """Start adapter by creating all configured client ports.

        Returns:
            True if at least one port initiated (even if still connecting).
        """
        self.logger.info(f"Starting OpenMux client adapter {self.name}")
        ports_config = self.get_port_configurations()
        if not ports_config:
            self.logger.warning(f"No ports configured for OpenMux client adapter {self.name}")
            return True

        success = 0
        for port_name, port_cfg in ports_config.items():
            try:
                port = await self.create_port(port_name, port_cfg)
                if port:
                    success += 1
                    self.logger.info(f"OpenMux client port {port_name} started (connecting in background)")
                else:
                    self.logger.error(f"Failed to start OpenMux client port {port_name}")
            except Exception as e:
                self.logger.error(f"Error creating OpenMux client port {port_name}: {e}", exc_info=True)

        if success > 0:
            self.is_running = True
        self.logger.info(f"OpenMux client adapter {self.name} started with {success}/{len(ports_config)} ports")
        return success > 0

    async def stop(self) -> None:
        """Stop adapter and all managed client ports."""
        self.logger.info(f"Stopping OpenMux client adapter {self.name} with {len(self.ports)} ports")
        for port_name, port in list(self.ports.items()):
            try:
                await port.stop()
            except Exception as e:
                self.logger.error(f"Error stopping OpenMux client port {port_name}: {e}", exc_info=True)
        self.ports.clear()
        self.is_running = False
        self.logger.info(f"OpenMux client adapter {self.name} stopped")

    async def write_to_port(self, port_name: str, data: bytes) -> int:
        """Write bytes to a named port.

        Args:
            port_name: Logical port to send to.
            data: Bytes payload.

        Returns:
            Number of bytes written (0 on failure or port missing).
        """
        port = self.ports.get(port_name)
        if not port:
            self.logger.error(f"OpenMux client port {port_name} not found")

            return 0
        bytes_written = await port.write_data(data)
        return bytes_written

    async def get_port_status(self, port_name: str) -> Dict[str, Any]:
        """Return status snapshot for a single port.

        Args:
            port_name: Port to inspect.

        Returns:
            Mapping with connection and configuration fields.
        """
        port = self.ports.get(port_name)
        if not port:
            return {"error": f"Port {port_name} not found"}
        return {
            "name": port_name,
            "adapter_type": self.adapter_type,
            "connected": port.is_connected,
            "host": port.host,
            "port": port.port,
            "remote_port": port.remote_port,
            "use_tls": port.use_tls,
            "auto_reconnect": port.auto_reconnect,
        }

    async def list_ports(self) -> list:
        """Return list of configured port names."""
        return list(self.ports.keys())

    async def _handle_port_data(self, port_name: str, data: bytes) -> None:
        """Forward inbound data from remote server to port manager.

        Drops data with debug log if the main port manager is not yet
        attached.
        """
        if hasattr(self, "main_port_manager") and self.main_port_manager:
            await self.main_port_manager.send_data(port_name, data)
        else:
            self.logger.debug(f"No main port manager available, dropping {len(data)} bytes from port {port_name}")

    def get_status_info(self) -> Dict[str, Any]:
        """Return adapter status summary.

        Collates per-port connection state plus aggregate metrics suitable
        for inclusion in server-wide status outputs.
        """
        # Summarize unique remote endpoints for readability
        endpoints = set()
        for p in self.ports.values():
            try:
                endpoints.add(f"{p.host}:{p.port}")
            except Exception:  # justification: best-effort endpoint summary; failures don't affect core status
                continue

        # Build detailed per-port info safely
        port_list: list = []
        for p in self.ports.values():
            state_obj = getattr(p, "state", None)
            if hasattr(state_obj, "value"):
                state_str = getattr(state_obj, "value")
            elif hasattr(state_obj, "name"):
                state_str = getattr(state_obj, "name")
            else:
                state_str = str(state_obj) if state_obj is not None else None

            port_list.append(
                {
                    "name": getattr(p, "name", None),
                    "state": state_str,
                    "connected": getattr(p, "is_connected", False),
                    "host": getattr(p, "host", None),
                    "port": getattr(p, "port", None),
                    "remote_port": getattr(p, "remote_port", None),
                    "use_tls": getattr(p, "use_tls", None),
                    "auto_reconnect": getattr(p, "auto_reconnect", None),
                }
            )

        return {
            "type": self.get_adapter_type(),
            "status": "running" if self.is_running else "stopped",
            "ports": f"{len(self.ports)} configured",
            "endpoint": ", ".join(sorted(endpoints)) if endpoints else None,
            "details": {
                "adapter_name": self.name,
                "total_ports": len(self.ports),
                "connected_ports": sum(1 for p in self.ports.values() if getattr(p, "is_connected", False)),
                "port_list": port_list,
            },
        }
