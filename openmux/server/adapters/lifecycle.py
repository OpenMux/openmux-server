"""Port Lifecycle Management.

Provides dynamic creation, destruction, and reconnection orchestration for
adapter-managed ports. All ports use a unified path for creation whether
instantiated at startup or during runtime events (e.g. hot-plug hardware,
federation reconnect sequences, or administrative actions).
"""

import logging
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from .base_adapter import BaseGenericAdapter

logger = logging.getLogger(__name__)


class PortLifecycleEvent(Enum):
    """Generic lifecycle events for managed ports.

    These events represent transitions or notable milestones that adapters
    and observers may respond to (e.g. metrics, logging, supervision).
    """

    PORT_CREATED = "port_created"  # Port instance created and ready
    PORT_CONNECTED = "port_connected"  # Port connection established (data flow possible)
    PORT_DISCONNECTED = "port_disconnected"  # Port connection lost (temporary)
    PORT_REMOVED = "port_removed"  # Port permanently removed/destroyed


class PortState(Enum):
    """Enumerated runtime states of a managed port."""

    CONFIGURED = "configured"  # Defined in config, not yet created
    CREATING = "creating"  # In process of being created
    ACTIVE = "active"  # Port is available and functional
    DEGRADED = "degraded"  # Port exists but with limited functionality
    # RECONNECTING state removed (unused)
    DESTROYING = "destroying"  # In process of being removed
    DESTROYED = "destroyed"  # Port removed and cleaned up


#: The three readiness values, in display order (issue #68). Readiness is a
#: derived *view* of a port's liveliness plus its offline reason; it is not a
#: stored state and is intentionally distinct from :class:`PortState` (the
#: create/destroy/active lifecycle axis).
READINESS_ACTIVE = "active"  # running and serving (green "connected")
READINESS_IDLE = "idle"  # healthy, intentionally not running (yellow "idle")
READINESS_OFFLINE = "offline"  # failed; needs attention (red "offline")


def port_is_alive(port: Any, /) -> bool:
    """Return whether a port's underlying resource is currently up (issue #68).

    This is the "alive" input to :func:`derive_port_readiness`. It reads the
    first available liveness signal on ``port`` in a deliberate order so it
    works for every local adapter without an adapter-type branch:

    - ``process_active`` (command): a healthy-but-resting command port keeps
      ``is_connected = True`` while ``process_active`` is ``False``, so the
      process flag must win or a resting shell would read "active".
    - ``is_connected`` (serial, tcp_initiator, loopback, federated proxy).
    - ``is_running`` (fallback for ports that expose only that).

    Args:
        port: A port object exposing one or more of the attributes above.

    Returns:
        bool: ``True`` when the underlying resource is up/running.
    """
    for attr in ("process_active", "is_connected", "is_running"):
        value = getattr(port, attr, None)
        if isinstance(value, bool):
            return value
    return False


def derive_port_readiness(alive: bool, status_message: Optional[str]) -> str:
    """Derive a port's readiness from liveliness and its offline reason.

    Single source of truth for the three readiness values (issue #68). The
    values are derived at the status-snapshot point and are never stored:

    - ``offline`` when ``status_message`` is set (the reason is shown);
    - ``idle`` when there is no reason and the port is not currently running;
    - ``active`` otherwise.

    A non-empty ``status_message`` always wins: it is the discriminator that
    keeps a genuinely-failing port red. "No reason + not running" is exactly
    the healthy-but-idle case.

    Args:
        alive: Whether the underlying process/connection is up now.
        status_message: The port's offline reason, or empty/None when healthy.

    Returns:
        str: One of READINESS_ACTIVE, READINESS_IDLE, READINESS_OFFLINE.
    """
    if status_message:
        return READINESS_OFFLINE
    if not alive:
        return READINESS_IDLE
    return READINESS_ACTIVE


class DynamicPortManager:
    """Manage dynamic port lifecycle for a single adapter.

    Tracks active ports, their configurations, and state transitions. Offers
    reconnection helpers and notification hooks for future integration with
    monitoring or status reporting subsystems.

    Args:
        adapter: The adapter instance owning the managed ports.
    """

    def __init__(self, adapter: "BaseGenericAdapter"):
        self.adapter = adapter
        self.active_ports = {}  # port_name -> PortInstance
        self.port_configs = {}  # port_name -> config dict
        self.port_states = {}  # port_name -> PortState

        # Set the port manager reference in the adapter
        adapter.port_manager = self

    async def create_port_dynamically(self, port_name: str, config: Dict[str, Any], event: PortLifecycleEvent) -> bool:
        """Create and register a port dynamically.

        Reuses the adapter's ``create_port`` path so startup and runtime
        creation behave identically.

        Args:
            port_name: Logical unique name of the port.
            config: Per-port configuration mapping.
            event: Lifecycle event context prompting creation.

        Returns:
            True if creation succeeded; False otherwise.
        """
        try:
            logger.debug(f"Creating port {port_name} for adapter {self.adapter.name}")
            self.port_states[port_name] = PortState.CREATING

            # Use the same port creation function as load-time
            # Cast for static type checkers: BaseGenericAdapter defines abstract create_port
            port_instance = await self.adapter.create_port(port_name, config)  # type: ignore[attr-defined]

            if port_instance:
                self.active_ports[port_name] = port_instance
                self.port_configs[port_name] = config
                self.port_states[port_name] = PortState.ACTIVE

                logger.info(f"Port {port_name} created successfully for adapter {self.adapter.name}")

                # Notify clients that new port is available
                await self._notify_port_created(port_name, port_instance)
                return True
            else:
                self.port_states[port_name] = PortState.DESTROYED
                logger.warning(f"Failed to create port {port_name} for adapter {self.adapter.name}")
                return False

        except Exception as e:
            self.port_states[port_name] = PortState.DESTROYED
            logger.error(f"Error creating port {port_name} for adapter {self.adapter.name}: {e}", exc_info=True)
            await self._handle_port_creation_error(port_name, e)
            return False

    async def destroy_port_dynamically(self, port_name: str, event: PortLifecycleEvent) -> bool:
        """Destroy a managed port instance.

        Ensures graceful client disconnect before invoking adapter cleanup.

        Args:
            port_name: Name of the port to destroy.
            event: Lifecycle event context prompting destruction.

        Returns:
            True if destruction succeeded (or port absent); False on error.
        """
        if port_name not in self.active_ports:
            return True

        try:
            logger.debug(f"Destroying port {port_name} for adapter {self.adapter.name}")
            self.port_states[port_name] = PortState.DESTROYING

            # Gracefully handle active connections
            await self._disconnect_port_clients(port_name)

            # Use the same cleanup function as normal shutdown
            await self.adapter.destroy_port(port_name)  # type: ignore[attr-defined]

            # Remove from tracking
            del self.active_ports[port_name]
            self.port_states[port_name] = PortState.DESTROYED

            logger.info(f"Port {port_name} destroyed successfully for adapter {self.adapter.name}")

            # Notify clients that port is no longer available
            await self._notify_port_destroyed(port_name)
            return True

        except Exception as e:
            logger.error(f"Error destroying port {port_name} for adapter {self.adapter.name}: {e}", exc_info=True)
            await self._handle_port_destruction_error(port_name, e)
            return False

    # Internal helper methods
    async def _notify_port_created(self, port_name: str, port_instance: Any) -> None:
        """Internal hook: port creation notification.

        Placeholder for future event bus / observer integration.
        """
        # TODO: Integrate with existing port notification system
        logger.debug(f"Port {port_name} is now available")

    async def _notify_port_destroyed(self, port_name: str) -> None:
        """Internal hook: port destruction notification."""
        # TODO: Integrate with existing port notification system
        logger.debug(f"Port {port_name} is no longer available")

    async def _disconnect_port_clients(self, port_name: str) -> None:
        """Internal hook: gracefully detach any active client sessions."""
        # TODO: Integrate with client manager to disconnect active sessions
        logger.debug(f"Disconnecting clients from port {port_name}")

    async def _handle_port_creation_error(self, port_name: str, error: Exception) -> None:
        """Internal error handler for port creation failures."""
        logger.error(f"Port creation error for {port_name}: {error}", exc_info=True)
        # TODO: Could trigger alerts or retry logic here

    async def _handle_port_destruction_error(self, port_name: str, error: Exception) -> None:
        """Internal error handler for port destruction failures."""
        logger.error(f"Port destruction error for {port_name}: {error}", exc_info=True)
        # TODO: Could trigger cleanup alerts here
