"""Base Generic Adapter Interface.

Provides a unified abstract base class for all OpenMux server-side adapters.
Adapters may accept inbound client connections, initiate outbound
connections, provide virtual ports, consume other adapters' ports, and/or
support multiplexed streams and federation. This base class standardizes
capability discovery, lifecycle management, and dynamic port operations.
"""

import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

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
    MANAGES_POWER = "manages_power"  # Controls PDU outlets (power on/off, readings)


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
        # Running state. The base start() sets this from the start result
        # (via _set_running); stop() implementations clear it. Subclass start
        # bodies must NOT assign it themselves, so the flag always matches the
        # return value callers observe.
        self.is_running = False
        # Set by DynamicPortManager when adapter is registered for dynamic ports.
        self.port_manager: Optional["DynamicPortManager"] = None
        # Reference to the unified/global port manager; provided after construction.
        self.main_port_manager: Optional[Any] = None
        # Config entries this adapter refused to apply (issue #105): port name -> the reason the
        # adapter gave. A refused port does not exist, so it has no console and a reboot does not
        # create it. ``config_gaps_kept`` lists the refused updates whose running port was kept,
        # so those users still have their session. The server prints both at boot and on reload.
        self.config_gaps: Dict[str, str] = {}
        self.config_gaps_kept: List[str] = []

    def record_config_gap(self, port_name: str, error: Any, kept_previous: bool = False) -> None:
        """Record one config entry that did not become a running port.

        Args:
            port_name: Name the config file gave the port.
            error: Reason the adapter refused it; an exception is rendered as its message.
            kept_previous: True when the previously running port was kept instead.
        """
        reason = str(error).strip() if error is not None else ""
        self.config_gaps[port_name] = reason or "no reason recorded"
        if kept_previous and port_name not in self.config_gaps_kept:
            self.config_gaps_kept.append(port_name)

    def clear_config_gap(self, port_name: str) -> None:
        """Drop recorded gaps for a port that now exists (issue #105).

        Args:
            port_name: Port name that applied successfully.
        """
        self.config_gaps.pop(port_name, None)
        if port_name in self.config_gaps_kept:
            self.config_gaps_kept.remove(port_name)

    def prepare_port(self, port_name: str, config: Dict[str, Any]) -> Any:
        """Build a port object from a config entry without starting or registering it.

        Reconcile calls this before it destroys anything, so a config the port rejects is caught
        while the running port is still in place (issue #105). Adapters whose port constructor can
        reject a config override this. The default says the adapter cannot pre-validate, which
        leaves reconcile behaving as it did before.

        Args:
            port_name: Name the port is created under.
            config: Raw per-port configuration.

        Returns:
            An unstarted port object, or None when this adapter has nothing to pre-validate.

        Raises:
            NotImplementedError: The adapter does not support pre-validation.
            ValueError: The config holds a value the port rejects.
        """
        raise NotImplementedError(f"{self.__class__.__name__} does not support port pre-validation")

    def reject_unusable_entries(self, config_section: str) -> List[str]:
        """Drop the port entries this adapter cannot build, and record why each one goes (issue #105).

        The factory calls this when ``validate_config`` rejects the section. That check covers the
        whole section, so one unusable entry would otherwise cost every port of it: an adapter
        answers per entry, and only the refused ones are dropped. The kept entries replace the
        refused list under ``config_section``, which is how ``get_port_configurations`` and the
        startup port load see the trimmed section. Nothing is touched when this adapter cannot
        pre-validate, so a config the adapter cannot explain behaves exactly as it did before.

        Args:
            config_section: Config key holding this adapter's list of port entries.

        Returns:
            Names of the dropped entries, each labelled by position when the entry has no name.
            Empty when nothing was dropped, the section is not a list, or the adapter cannot
            pre-validate. Each dropped entry is also recorded as a config gap, so the load-time
            warning block names the reason and no port of the kept entries is lost.
        """
        entries = self.config.get(config_section) if isinstance(self.config, dict) else None
        if not isinstance(entries, list):
            return []
        logger = getattr(self, "logger", None) or logging.getLogger(__name__)
        kept: List[Any] = []
        dropped: List[str] = []
        for index, entry in enumerate(entries):
            # No name means no port name to report, so the position in the file names it: #1 is the
            # first entry the reader sees under the section.
            where = f"#{index + 1}"
            if not isinstance(entry, dict):
                label, reason = f"<entry {where}>", "the entry is not a mapping of port options"
            elif not entry.get("name"):
                label, reason = f"<unnamed {where}>", "the entry has no 'name', so no port can be built from it"
            else:
                label = str(entry["name"])
                try:
                    self.prepare_port(label, entry)
                    kept.append(entry)
                    continue
                except NotImplementedError:
                    logger.debug(
                        "%s adapter %s cannot pre-validate entries, keeping the section as it is",
                        self.get_adapter_type(),
                        self.name,
                    )
                    return []
                except Exception as e:
                    reason = str(e).strip() or "the port rejected its own configuration"
            if label not in self.config_gaps:
                # An adapter that reports a bad entry while it parses its own section (the serial
                # adapter does) already logged this reason and recorded it; repeating it here would
                # print the same line twice for one entry.
                logger.error(
                    "%s adapter %s rejects port %s in the config file: %s", self.get_adapter_type(), self.name, label, reason
                )
            dropped.append(label)
            self.record_config_gap(label, reason)
        if dropped and isinstance(self.config, dict):
            # Rebind instead of writing into the caller's list: the config dict it came from may be
            # the server's live config, and the refused entry must not survive in it.
            self.config[config_section] = kept
        return dropped

    def prepare_reconcile_candidates(
        self, names: List[str], new_by_name: Dict[str, Dict[str, Any]], running: Any
    ) -> Dict[str, str]:
        """Pre-validate the ports a reload wants to create (issue #105).

        Args:
            names: Port names the reload wants to create (added plus re-created).
            new_by_name: New port configs indexed by name.
            running: Current port registry (used to say whether a running port is kept).

        Returns:
            Dict of port name -> reason, for each entry the adapter refuses. Empty when every
            entry is buildable or the adapter cannot pre-validate. Each refusal is also recorded
            as a config gap, so the boot or reload warning block can name it.
        """
        refused: Dict[str, str] = {}
        running_names = set(running.keys()) if isinstance(running, dict) else set(running or [])
        for name in names:
            cfg = new_by_name.get(name)
            if not cfg:
                continue
            try:
                self.prepare_port(name, cfg)
            except NotImplementedError:
                return {}
            except Exception as e:
                reason = str(e).strip() or "the port rejected its own configuration"
                self.logger.error("%s adapter %s rejects port %s: %s", self.get_adapter_type(), self.name, name, reason)
                refused[name] = reason
                self.record_config_gap(name, reason, kept_previous=name in running_names)
        return refused

    def reconcile_summary(
        self,
        added: List[str],
        removed: List[str],
        updated: List[str],
        unchanged: List[str],
        refused: Optional[Dict[str, str]] = None,
        kept_previous: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Build a reconcile summary that lists only ports the reload applied (issue #105).

        Args:
            added: Names the reload created.
            removed: Names the reload deleted.
            updated: Names the reload re-created.
            unchanged: Names the reload left alone.
            refused: Name -> reason for entries that did not apply.
            kept_previous: Refused names whose running port was kept.

        Returns:
            The four name lists, plus ``failed``, ``failed_detail`` and ``kept_previous`` only when
            something refused. A refused name never appears in ``added`` or ``updated``: reporting
            an unusable entry as new is what made it reappear on every later reload.
        """
        summary: Dict[str, Any] = {
            "added": list(added),
            "removed": list(removed),
            "updated": list(updated),
            "unchanged": list(unchanged),
        }
        refused = refused or {}
        if not refused:
            return summary
        kept = set(kept_previous or [])
        summary["added"] = [name for name in summary["added"] if name not in refused]
        summary["updated"] = [name for name in summary["updated"] if name not in refused]
        summary["failed"] = sorted(refused)
        summary["failed_detail"] = {name: {"error": refused[name], "kept_previous": name in kept} for name in sorted(refused)}
        if kept:
            summary["kept_previous"] = [name for name in (kept_previous or []) if name in refused]
        return summary

    def _set_running(self, running: bool) -> None:
        """Record a start result in ``is_running``.

        Called by the base ``start()`` with the return value of
        ``_do_start()``. This keeps the running state in sync with what every
        start caller observes. ``stop()`` implementations set ``is_running``
        to ``False`` directly.
        """
        self.is_running = bool(running)

    def get_adapter_type(self) -> str:
        """Return adapter type identifier.

        Override in subclasses with a stable string key used for configuration
        lookup and status reporting. Falls back to the ``adapter_type`` attribute
        when present (supports legacy class-attribute style and test mocks).
        """
        at = getattr(self, "adapter_type", None)
        if isinstance(at, str) and at:
            return at
        return self.__class__.__name__

    @abstractmethod
    def get_capabilities(self) -> Set[AdapterCapability]:
        """Return supported adapter capabilities.

        Returns:
            Set of capability enum values describing features implemented.
        """
        raise NotImplementedError

    async def start(self) -> bool:
        """Start adapter resources and set ``is_running`` from the result.

        Subclasses implement the actual startup in ``_do_start``. This wrapper
        is the single place where ``is_running`` is set after a start attempt,
        so the flag always matches the result every caller observes.

        Returns:
            True if startup succeeded, otherwise False.
        """
        try:
            ok = await self._do_start()
        except Exception:
            self._set_running(False)
            raise
        self._set_running(bool(ok))
        return bool(ok)

    @abstractmethod
    async def _do_start(self) -> bool:
        """Adapter-specific startup implementation.

        Subclass method that used to be ``start``. The base ``start`` wrapper
        is the only place ``is_running`` is set after a start attempt, so
        subclasses must NOT override ``start`` itself -- they implement
        ``_do_start`` and return the success flag only. On a disabled (not
        configured) adapter return True so the server treats "disabled on
        purpose" as a healthy state; ``get_status_info`` then reports the
        adapter as ``disabled`` rather than ``running``.
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

        # Use same dynamic creation function for load-time ports
        from .lifecycle import PortLifecycleEvent

        for port_name, port_config in port_configs.items():
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
        adapter_type = self.get_adapter_type()
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
