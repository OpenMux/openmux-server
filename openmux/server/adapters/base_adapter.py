"""Base Generic Adapter Interface.

Provides a unified abstract base class for all OpenMux server-side adapters.
Adapters may accept inbound client connections, initiate outbound
connections, provide virtual ports, consume other adapters' ports, and/or
support multiplexed streams and federation. This base class standardizes
capability discovery, lifecycle management, and dynamic port operations.
"""

from abc import ABC, abstractmethod
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, Optional, Set

if TYPE_CHECKING:
    from .lifecycle import DynamicPortManager, PortLifecycleEvent


class AdapterCapability(Enum):
    """Adapter capability flags.

    Enumerates distinct feature sets an adapter can implement. These flags
    influence which optional methods are expected to be supported.
    """

    ACCEPTS_CONNECTIONS = "accepts_connections"  # Can listen for client connections
    MAKES_CONNECTIONS = "makes_connections"  # Can connect to remote services
    PROVIDES_PORTS = "provides_ports"  # Exposes virtual ports
    BIDIRECTIONAL_DATA = "bidirectional_data"  # Full duplex communication
    MULTIPLEXED_STREAMS = "multiplexed_streams"  # Multiple sessions per connection
    FEDERATION_AWARE = "federation_aware"  # Supports federation protocols
    AUTHENTICATION = "authentication"  # Handles user authentication


class BaseGenericAdapter(ABC):
    """Unified abstract base class for OpenMux adapters.

    Responsibilities:
        * Declare adapter capabilities.
        * Start/stop underlying resources (listeners, connections, ports).
        * Support dynamic creation/destruction of ports during runtime.
        * Provide optional data I/O helpers for adapters that manage ports.
        * Surface lifecycle events to subclasses for customization.

    Subclasses must implement abstract methods marked with ``@abstractmethod``.
    """

    def __init__(self, name: str, config: Dict[str, Any]):
        """Initialize the adapter.

        Args:
            name: Logical adapter name (unique within the server instance).
            config: Adapter-specific configuration mapping.
        """
        self.name = name
        self.config = config
        self.capabilities = self.get_capabilities()
        self.is_running = False
        # Set by DynamicPortManager when adapter is registered for dynamic ports.
        self.port_manager: Optional["DynamicPortManager"] = None
        # Reference to the unified/global port manager; provided after construction.
        self.main_port_manager: Optional[Any] = None

    @abstractmethod
    def get_capabilities(self) -> Set[AdapterCapability]:
        """Return supported adapter capabilities.

        Returns:
            Set of capability enum values describing features implemented.
        """
        raise NotImplementedError

    @abstractmethod
    async def start(self) -> bool:
        """Start adapter resources.

        For inbound adapters this may open listening sockets; for outbound
        adapters it may initiate connections; for port-providing adapters it
        may create initial ports.

        Returns:
            True if startup succeeded, otherwise False.
        """
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        """Stop adapter and clean up all resources.

        Default implementation destroys all active dynamic ports via the
        attached ``DynamicPortManager`` before returning.
        """
        # Default implementation will stop all active ports using port_manager
        if self.port_manager:
            from .lifecycle import PortLifecycleEvent

            for port_name in list(self.port_manager.active_ports.keys()):
                await self.port_manager.destroy_port_dynamically(port_name, PortLifecycleEvent.PORT_REMOVED)

    @classmethod
    @abstractmethod
    def validate_config(cls, config: Dict[str, Any]) -> bool:
        """Validate adapter-specific configuration structure.

        Args:
            config: Raw configuration mapping provided to the adapter.

        Returns:
            True if configuration is valid; otherwise False (or raise).
        """
        raise NotImplementedError

    # Dynamic Port Management - Core Functions Used by Both Load-time and Runtime
    @abstractmethod
    async def create_port(self, port_name: str, config: Dict[str, Any]) -> Optional[Any]:
        """Create a port instance.

        Used both during initial adapter startup and subsequent dynamic
        creation requests.

        Args:
            port_name: Logical unique port identifier.
            config: Per-port configuration mapping.

        Returns:
            The created port object or None on failure.
        """
        raise NotImplementedError

    @abstractmethod
    async def destroy_port(self, port_name: str) -> None:
        """Destroy a port instance.

        Used during adapter shutdown or dynamic removal operations.

        Args:
            port_name: Name of the port to remove.
        """
        pass

    async def load_configured_ports(self) -> bool:
        """Instantiate ports defined in static configuration.

        Reuses the dynamic creation path to ensure behavioral parity.

        Returns:
            True if all configured ports were created successfully (logical
            AND of individual create results).
        """
        if not self.port_manager:
            raise RuntimeError("Port manager not initialized")

        success = True

        # Get port configurations from adapter-specific config
        port_configs = self.get_port_configurations()

        for port_name, port_config in port_configs.items():
            # Use same dynamic creation function for load-time ports
            from .lifecycle import PortLifecycleEvent

            created = await self.port_manager.create_port_dynamically(port_name, port_config, PortLifecycleEvent.PORT_CREATED)
            success &= created

        return success

    @abstractmethod
    def get_port_configurations(self) -> Dict[str, Dict[str, Any]]:
        """Return mapping of port names to configuration dictionaries.

        Returns:
            Dict keyed by port name containing shallow copies of per-port
            configuration.
        """
        pass

    # Event-Driven Port Management
    async def handle_lifecycle_event(self, event: "PortLifecycleEvent", event_data: Dict[str, Any]) -> None:  # vulture: ignore
        """Handle a port lifecycle event.

        Dispatches to internal hook methods; referenced indirectly by the
        dynamic port manager which triggers lifecycle propagation.

        Args:
            event: Lifecycle event enumerator indicating the transition.
            event_data: Context payload for the event (port name, metadata).
        """
        from .lifecycle import PortLifecycleEvent

        if event == PortLifecycleEvent.PORT_CREATED:
            await self._handle_port_created(event_data)
        elif event == PortLifecycleEvent.PORT_CONNECTED:
            await self._handle_port_connected(event_data)
        elif event == PortLifecycleEvent.PORT_DISCONNECTED:
            await self._handle_port_disconnected(event_data)
        elif event == PortLifecycleEvent.PORT_REMOVED:
            await self._handle_port_removed(event_data)

    # Default implementations - adapters can override for specific behavior
    async def _handle_port_created(self, event_data: Dict[str, Any]) -> None:
        """Handle port creation event.

        Subclasses may override to inject adapter-specific logic.
        """
        pass

    async def _handle_port_connected(self, event_data: Dict[str, Any]) -> None:
        """Handle port connection event.

        Subclasses may override to inject adapter-specific logic.
        """
        pass

    async def _handle_port_disconnected(self, event_data: Dict[str, Any]) -> None:
        """Handle port disconnection event.

        Subclasses may override to inject adapter-specific logic.
        """
        pass

    async def _handle_port_removed(self, event_data: Dict[str, Any]) -> None:
        """Handle port removal event.

        Subclasses may override to inject adapter-specific logic.
        """
        pass

    # Connection-oriented methods (optional implementation)
    async def handle_client_connection(self, reader, writer) -> None:
        """Handle an inbound client connection.

        Args:
            reader: AsyncIO StreamReader for the client.
            writer: AsyncIO StreamWriter for the client.

        Raises:
            NotImplementedError: If adapter does not support accepting connections.
        """
        if AdapterCapability.ACCEPTS_CONNECTIONS not in self.capabilities:
            raise NotImplementedError(f"{self.name} doesn't accept connections")

    # Port-oriented methods (optional implementation)
    async def read_data(self, port_name: str, timeout: float = 1.0) -> bytes:
        """Read data from a managed port.

        Args:
            port_name: Target port name.
            timeout: Max seconds to wait for data.

        Returns:
            Bytes payload read from the underlying port instance.

        Raises:
            NotImplementedError: If adapter cannot provide port data.
            ValueError: If the port is not active.
        """
        if AdapterCapability.PROVIDES_PORTS not in self.capabilities:
            raise NotImplementedError(f"{self.name} doesn't provide port data")

        # Check if port is active before reading
        if not self.port_manager or port_name not in self.port_manager.active_ports:
            raise ValueError(f"Port {port_name} is not active")

        port_instance = self.port_manager.active_ports[port_name]
        return await self._read_from_port_instance(port_instance, timeout)

    async def write_data(self, port_name: str, data: bytes) -> int:
        """Write data to a managed port.

        Args:
            port_name: Target port name.
            data: Bytes to write.

        Returns:
            Number of bytes accepted by the port (0 on failure).

        Raises:
            NotImplementedError: If adapter cannot accept port data.
            ValueError: If the port is not active.
        """
        if AdapterCapability.PROVIDES_PORTS not in self.capabilities:
            raise NotImplementedError(f"{self.name} doesn't accept port data")

        # Check if port is active before writing
        if not self.port_manager or port_name not in self.port_manager.active_ports:
            raise ValueError(f"Port {port_name} is not active")

        port_instance = self.port_manager.active_ports[port_name]
        return await self._write_to_port_instance(port_instance, data)

    # Port instance helpers (adapters should implement these)
    async def _read_from_port_instance(self, port_instance: Any, timeout: float) -> bytes:
        """Adapter-specific port instance read operation.

        Args:
            port_instance: Concrete port object.
            timeout: Max seconds to wait for data.

        Returns:
            Bytes read from the port instance.

        Raises:
            NotImplementedError: If the port instance does not support reading.
        """
        if hasattr(port_instance, "read_data"):
            return await port_instance.read_data(timeout)
        else:
            raise NotImplementedError("Port instance doesn't support reading")

    async def _write_to_port_instance(self, port_instance: Any, data: bytes) -> int:
        """Adapter-specific port instance write operation.

        Args:
            port_instance: Concrete port object.
            data: Bytes to write.

        Returns:
            Number of bytes written.

        Raises:
            NotImplementedError: If the port instance does not support writing.
        """
        if hasattr(port_instance, "write_data"):
            try:
                result = await port_instance.write_data(data)  # type: ignore[call-arg]
            except TypeError:
                # Some legacy definitions may not be awaitable / or signature mismatch
                maybe = port_instance.write_data(data)  # type: ignore[call-arg]
                return int(maybe) if isinstance(maybe, (int, bool)) else 0
            return int(result) if isinstance(result, (int, bool)) else 0
        raise NotImplementedError("Port instance doesn't support writing (expected write_data)")

    def is_port_ready(self, port_name: str) -> bool:
        """Return True if the named port is logically ready for I/O.

        Readiness requires ACTIVE state and (if present) an ``is_connected``
        attribute evaluating truthy. Ports without ``is_connected`` are
        treated as ready once ACTIVE.

        Args:
            port_name: Logical port identifier to check.

        Returns:
            True if the port is ACTIVE and, when present, ``is_connected`` is truthy.
        """
        if not self.port_manager or port_name not in self.port_manager.active_ports:
            return False
        port_obj = self.port_manager.active_ports[port_name]
        state = getattr(self.port_manager, "port_states", {}).get(port_name)
        if state is None or getattr(state, "value", None) != "active":
            return False
        if hasattr(port_obj, "is_connected"):
            return bool(getattr(port_obj, "is_connected"))
        return True

    # Standard status reporting (adapters can override for richer detail)
    def get_status_info(self) -> Dict[str, Any]:  # pragma: no cover - simple aggregation
        """Return a standardized adapter status dictionary.

        Keys:
            type: Adapter type string (override via get_adapter_type() if present)
            status: running|stopped based on is_running flag
            ports: human summary (if port_manager attached) else "n/a"
            details: adapter-specific structure placeholder
        """
        adapter_type = getattr(self, "get_adapter_type", lambda: self.__class__.__name__)()
        port_count = 0
        pending_connect = 0
        if self.port_manager and hasattr(self.port_manager, "active_ports"):
            for pname, pobj in getattr(self.port_manager, "active_ports", {}).items():
                port_count += 1
                if hasattr(pobj, "is_connected") and not getattr(pobj, "is_connected"):
                    pending_connect += 1
        return {
            "type": adapter_type,
            "status": "running" if self.is_running else "stopped",
            "ports": f"{port_count} active" if port_count else "0 active",
            "pending_connect": pending_connect if pending_connect else 0,
            "details": {
                "adapter_name": self.name,
                "capabilities": sorted([c.value for c in self.capabilities]),
            },
        }

    # Unified helper to register a data callback on a port object if supported
    def wire_port_data_callback(self, port_obj: Any, callback) -> None:
        """Attach a data callback to a port instance if supported.

        Adapters previously used varying attribute names; we standardize on
        ``data_callback``.

        Args:
            port_obj: Concrete port instance that may expose ``data_callback``.
            callback: Callable accepting ``(port_name: str, data: bytes)``.
        """
        try:
            if hasattr(port_obj, "data_callback"):
                setattr(port_obj, "data_callback", callback)
        except (
            Exception
        ):  # justification: best-effort optional callback wiring; missing/unsafe attribute shouldn't break adapter lifecycle
            pass

    # Federation/multiplexing methods (optional implementation)
