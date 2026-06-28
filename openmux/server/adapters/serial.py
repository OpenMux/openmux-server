"""Unified Serial Device Adapter.

Provides managed access to one or more system serial devices (e.g. USB
serial, RS-232) within the OpenMux adapter framework. Handles connection
establishment, automatic reconnection, queued asynchronous reads, and
forwarding into the unified port manager abstraction. Each configured
device becomes a logical port with independent buffering and lifecycle.
"""

import asyncio
import inspect
import logging
import os
import stat
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from ..data_logger import DataLogger
from .base_adapter import AdapterCapability, BaseGenericAdapter


@dataclass
class SerialPortConfig:
    """Configuration parameters for a single serial port.

    Args:
        name: Logical unique port name.
        description: Human-friendly description.
        device: Path to serial device (e.g. /dev/ttyUSB0).
        baudrate: Baud rate (positive integer).
        bytesize: Number of data bits (5..8).
        parity: Parity spec (N,E,O,M,S).
        stopbits: Stop bits (1, 1.5, or 2).
        timeout: Read timeout in seconds.
        flow_control: Flow control mode string.
        dtr: Initial DTR state.
        rts: Initial RTS state.
        max_read_write_users: Max simultaneous read-write users.
    """

    name: str
    description: str
    device: str
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    timeout: float = 1.0
    flow_control: str = "none"
    dtr: bool = True  # vulture: ignore (configured via runtime, accessed through config)
    rts: bool = True  # vulture: ignore (configured via runtime, accessed through config)
    max_read_write_users: int = 1  # Maximum number of users with write access
    log_file: Optional[str] = None  # Optional per-port data log file path
    log_format: Optional[str] = None  # 'line' or 'jsonl'
    log_line_template: Optional[str] = None  # For 'line' format

    def __post_init__(self):
        """Validate configuration values after initialization.

        Raises:
            ValueError: If any field contains an invalid value.
        """
        if not self.name:
            raise ValueError("Serial port name cannot be empty")
        if not self.device:
            raise ValueError("Serial device path cannot be empty")
        if self.baudrate <= 0:
            raise ValueError("Baudrate must be positive")
        if self.bytesize not in [5, 6, 7, 8]:
            raise ValueError("Bytesize must be 5, 6, 7, or 8")
        if self.parity not in ["N", "E", "O", "M", "S"]:
            raise ValueError("Parity must be N, E, O, M, or S")
        if self.stopbits not in [1, 1.5, 2]:
            raise ValueError("Stopbits must be 1, 1.5, or 2")


class SerialPortWrapper:
    """Runtime connection manager for a single serial port.

    Encapsulates connection attempts, read loop, graceful shutdown, and
    optional infinite auto-reconnect with back-off style delay. Data read
    from the underlying serial stream is forwarded via data_callback, which
    is set by PortManager.register_unified_port().

    Attributes:
        config: Immutable ``SerialPortConfig`` describing the port.
        name: Convenience alias of ``config.name``.
        description: Human friendly description string.
        max_read_write_users: Maximum concurrent read/write users allowed.
        is_connected: True while active connection is established.
        data_callback: Callback set by PortManager; called with (port_name, data).
    """

    def __init__(self, config: SerialPortConfig, logger: logging.Logger, meta_notify: Optional[Callable[[str, Dict[str, Any]], None]] = None):
        self.config = config
        self.name = config.name  # Add name attribute for port manager compatibility
        self.description = config.description
        self.max_read_write_users = config.max_read_write_users  # For port manager compatibility
        self.logger = logger.getChild(f"serial.{config.name}")
        # Best-effort callback into PortManager listeners via adapter
        self._meta_notify = meta_notify

        # Connection state
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.is_connected = False
        # Track last notified connection state to avoid duplicate events
        self._last_notified_connected: Optional[bool] = None
        self.connection_task: Optional[asyncio.Task] = None
        self.reconnect_task: Optional[asyncio.Task] = None

        # Data handling
        self.read_task: Optional[asyncio.Task] = None
        # Callback set by PortManager.register_unified_port(); called as (port_name, data)
        self.data_callback: Optional[Callable[[str, bytes], Optional[Awaitable[None]]]] = None
        self._callback_missing_logged = False
        # Reconnection settings
        self.auto_reconnect = True
        self.reconnect_delay = 5.0
        self.max_reconnect_attempts = 0  # 0 = infinite
        self.reconnect_attempts = 0
        # Timestamp (monotonic) of the last "device does not exist" warning;
        # used to rate-limit the message to at most once per hour.
        self._last_missing_warn_ts: Optional[float] = None

    async def start(self) -> bool:
        """Start (or schedule) serial port connection management.

        Returns:
            bool: True once connection supervision loop is active.
        """
        if self.is_connected:
            return True

        self.logger.info(f"Starting serial port {self.config.name} on {self.config.device}")

        # Start connection supervisor in background
        self.connection_task = asyncio.create_task(self._connect_loop())
        return True

    async def stop(self) -> None:
        """Stop connection supervision and close the port.

        Idempotent; safe to call multiple times.
        """
        self.logger.info(f"Stopping serial port {self.config.name}")

        # Stop auto-reconnection
        self.auto_reconnect = False

        # Cancel tasks
        if self.reconnect_task and not self.reconnect_task.done():
            self.reconnect_task.cancel()
            try:
                await self.reconnect_task
            except asyncio.CancelledError:
                pass

        if self.connection_task and not self.connection_task.done():
            self.connection_task.cancel()
            try:
                await self.connection_task
            except asyncio.CancelledError:
                pass

        # Close connection
        await self._disconnect()

    async def _connect_loop(self) -> None:
        """Background supervisor implementing optional auto-reconnect.

        Repeatedly attempts to connect and run a read loop until cancelled
        or reconnection policy disallows further attempts.
        """
        while self.auto_reconnect:
            try:
                success = await self._connect()
                if success:
                    self.reconnect_attempts = 0
                    # Reset so a future disconnect+missing-device logs immediately
                    self._last_missing_warn_ts = None

                    # Start reading data
                    if self.read_task and not self.read_task.done():
                        self.read_task.cancel()
                    self.read_task = asyncio.create_task(self._read_loop())

                    # Wait for disconnection
                    try:
                        await self.read_task
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        self.logger.error(f"Read loop error: {e}", exc_info=True)

                    # Connection lost, clean up
                    await self._disconnect()

                if self.auto_reconnect and (
                    self.max_reconnect_attempts == 0 or self.reconnect_attempts < self.max_reconnect_attempts
                ):
                    self.reconnect_attempts += 1
                    self.logger.debug(
                        f"Attempting to reconnect to {self.config.device} "
                        f"(attempt {self.reconnect_attempts})"
                    )
                    await asyncio.sleep(self.reconnect_delay)
                else:
                    break

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Connection loop error: {e}", exc_info=True)
                if self.auto_reconnect:
                    await asyncio.sleep(self.reconnect_delay)

    async def _connect(self) -> bool:
        """Establish a new connection to the configured device.

        Returns:
            bool: True if connection established; False otherwise.
        """
        try:
            # Check if device exists
            if not os.path.exists(self.config.device):
                import time as _time
                now = _time.monotonic()
                if self._last_missing_warn_ts is None or (now - self._last_missing_warn_ts) >= 3600:
                    self.logger.warning(f"Serial device {self.config.device} does not exist")
                    self._last_missing_warn_ts = now
                else:
                    self.logger.debug(f"Serial device {self.config.device} still not found")
                return False

            # Check device type on POSIX systems
            if os.name == "posix":
                try:
                    st = os.stat(self.config.device)
                    if stat.S_ISCHR(st.st_mode):
                        self.logger.debug(f"Device {self.config.device} is a character device")
                    else:
                        self.logger.warning(f"Device {self.config.device} is not a character device")
                except Exception as e:
                    self.logger.warning(f"Could not check device type: {e}", exc_info=True)

            # Import serial_asyncio
            try:
                import serial_asyncio
            except ImportError:
                self.logger.error(
                    "pyserial-asyncio not installed (required for serial adapter). Install with: pip install pyserial-asyncio"
                )
                return False

            # Create serial connection
            self.logger.info(
                f"Connecting to {self.config.device} "
                f"({self.config.baudrate}bps, {self.config.bytesize}"
                f"{self.config.parity}{self.config.stopbits})"
            )

            self.reader, self.writer = await serial_asyncio.open_serial_connection(
                url=self.config.device,
                baudrate=self.config.baudrate,
                bytesize=self.config.bytesize,
                parity=self.config.parity,
                stopbits=self.config.stopbits,
                timeout=self.config.timeout,
            )

            self.is_connected = True
            self.logger.info(f"Successfully connected to {self.config.device}")
            try:
                if self._meta_notify and self._last_notified_connected is not True:
                    self._meta_notify(self.config.name, {"event": "serial_connected", "connected": True})
                    self._last_notified_connected = True
            except Exception:
                self.logger.debug("Meta notify failed on serial connect", exc_info=True)
            return True

        except Exception as e:
            self.logger.error(f"Failed to connect to {self.config.device}: {e}", exc_info=True)
            return False

    async def _disconnect(self) -> None:
        """Close serial connection and cancel pending read tasks."""
        if not self.is_connected:
            return

        try:
            if self.read_task and not self.read_task.done():
                self.read_task.cancel()
                try:
                    await self.read_task
                except asyncio.CancelledError:
                    pass

            if self.writer:
                self.writer.close()
                if hasattr(self.writer, "wait_closed"):
                    await self.writer.wait_closed()

            self.logger.info(f"Disconnected from {self.config.device}")

        except Exception as e:
            self.logger.error(f"Error disconnecting from {self.config.device}: {e}", exc_info=True)
        finally:
            self.is_connected = False
            self.reader = None
            self.writer = None
            try:
                if self._meta_notify and self._last_notified_connected is not False:
                    self._meta_notify(self.config.name, {"event": "serial_disconnected", "connected": False})
                    self._last_notified_connected = False
            except Exception:
                self.logger.debug("Meta notify failed on serial disconnect", exc_info=True)

    # (health watchdog removed; rely on read loop exceptions and connection supervisor)

    async def _read_loop(self) -> None:
        """Continuously read from device and enqueue data chunks."""
        while self.is_connected and self.reader:
            try:
                data = await self.reader.read(1024)
                if not data:
                    # Empty read typically means connection closed
                    self.logger.warning("Serial connection closed (empty read)")
                    # Proactively mark down and notify before breaking, to update UI immediately
                    try:
                        # Mark state false immediately to reduce race window
                        self.is_connected = False
                        if self._meta_notify and self._last_notified_connected is not False:
                            self._meta_notify(self.config.name, {"event": "serial_disconnected", "connected": False})
                            self._last_notified_connected = False
                    except Exception:
                        self.logger.debug("Meta notify failed on empty read disconnect", exc_info=True)
                    break

                # Forward data via callback (set by PortManager at registration)
                if self.data_callback:
                    try:
                        cb = self.data_callback
                        result: Any = cb(self.name, data)  # type: ignore[misc]
                        if inspect.isawaitable(result):
                            await result
                    except Exception:
                        self.logger.error("Serial read callback error", exc_info=True)
                else:
                    if not self._callback_missing_logged:
                        self.logger.error(
                            "Serial port %s: data_callback not set; dropping data",
                            self.name,
                        )
                        self._callback_missing_logged = True
                # Per-chunk data logs are very chatty; keep at debug level
                self.logger.debug(
                    f"Read {len(data)} bytes from {self.config.device}: {data.decode('utf-8', errors='replace')}"
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error reading from {self.config.device}: {e}", exc_info=True)
                # Proactively notify disconnect on read error
                try:
                    # Mark state false immediately to reduce race window
                    self.is_connected = False
                    if self._meta_notify and self._last_notified_connected is not False:
                        self._meta_notify(self.config.name, {"event": "serial_disconnected", "connected": False})
                        self._last_notified_connected = False
                except Exception:
                    self.logger.debug("Meta notify failed on read error", exc_info=True)
                break

    async def write_data(self, data: bytes) -> int:
        """Write a bytes payload to the device.

        Args:
            data: Bytes to transmit.

        Returns:
            int: Number of bytes written.

        Raises:
            RuntimeError: If port not connected.
        """
        if not self.is_connected or not self.writer:
            raise RuntimeError(f"Serial port {self.config.name} not connected")

        try:
            self.writer.write(data)
            await self.writer.drain()
            self.logger.debug(f"Wrote {len(data)} bytes to {self.config.device}")
            return len(data)
        except Exception as e:
            self.logger.error(f"Error writing to {self.config.device}: {e}", exc_info=True)
            raise

    def get_status_snapshot(self) -> Dict[str, Any]:
        """Return static config details for port listings."""
        return {
            "serial_config": {
                "device": self.config.device,
                "baudrate": self.config.baudrate,
                "bytesize": self.config.bytesize,
                "parity": self.config.parity,
                "stopbits": self.config.stopbits,
                "flow_control": self.config.flow_control,
                "timeout": self.config.timeout,
                "dtr": self.config.dtr,
                "rts": self.config.rts,
            }
        }


class SerialAdapter(BaseGenericAdapter):
    """Adapter orchestrating multiple serial port wrappers.

    Converts each configured serial device into an OpenMux unified port,
    supervising connection state, relaying inbound data, and offering a
    write API that respects per-port connection state.
    """

    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)

        self.serial_ports: Dict[str, SerialPortWrapper] = {}
        self.logger = logging.getLogger(f"openmux.adapter.serial.{name}")

        # Parse port configurations
        self._parse_port_configs()

        # Read coalescing settings (helps reduce fragmentation during bursts)
        self._coalesce_reads: bool = bool(self.config.get("read_coalesce", True))
        # Maximum time window to accumulate consecutive chunks (seconds)
        self._coalesce_max_delay: float = float(self.config.get("read_coalesce_max_delay_ms", 4)) / 1000.0
        # Maximum total size to accumulate before forwarding (bytes)
        self._coalesce_max_bytes: int = int(self.config.get("read_coalesce_max_bytes", 65536))

    def get_capabilities(self) -> Set[AdapterCapability]:
        """Return capability flags implemented by this adapter."""
        return {
            AdapterCapability.PROVIDES_PORTS,
            AdapterCapability.BIDIRECTIONAL_DATA,
            AdapterCapability.MAKES_CONNECTIONS,
        }

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> bool:
        """Validate serial adapter configuration.

        Args:
            config: Raw configuration mapping (possibly wrapped).

        Returns:
            True if structurally valid; False otherwise.
        """
        # Handle wrapped config from factory
        serial_config = config
        if "serial_ports" in config:
            serial_config = config["serial_ports"]

        # serial_config should be a list of port definitions
        if not isinstance(serial_config, list):
            return False

        # Validate each port configuration
        for port_config in serial_config:
            if not isinstance(port_config, dict):
                return False
            # Each port must have name and device
            if "name" not in port_config or "device" not in port_config:
                return False

        return True

    def _resolve_max_rw_users(self, port_config: Dict[str, Any], *, port_name: Optional[str] = None, default: int = 1) -> int:
        """Resolve the max_read_write_users value with legacy fallback."""

        value = port_config.get("max_read_write_users")
        legacy = False
        if value is None and "read_write_users" in port_config:
            value = port_config.get("read_write_users")
            legacy = True
        if value is None:
            return max(1, int(default))
        try:
            resolved = int(value)
        except (TypeError, ValueError):
            self.logger.warning(
                "Port %s has invalid max_read_write_users=%r; falling back to %s",
                port_name or port_config.get("name") or "unknown",
                value,
                default,
            )
            return max(1, int(default))
        if resolved < 1:
            self.logger.warning(
                "Port %s has max_read_write_users=%s < 1; clamping to 1",
                port_name or port_config.get("name") or "unknown",
                resolved,
            )
            return 1
        if legacy:
            self.logger.info(
                "Port %s uses deprecated read_write_users; rename to max_read_write_users",
                port_name or port_config.get("name") or "unknown",
            )
        return resolved

    def _parse_port_configs(self) -> None:
        """Parse configuration and build ``SerialPortWrapper`` objects.

        Raises:
            ValueError: If no ports configured.
        """
        # Handle factory-wrapped config format
        ports_config = []
        if "serial_ports" in self.config:
            # Factory wrapped format: {'serial_ports': [port_configs]}
            ports_config = self.config["serial_ports"]
        elif "ports" in self.config:
            # Direct format: {'ports': [port_configs]}
            ports_config = self.config["ports"]
        else:
            # Check if config itself is the ports list
            if isinstance(self.config, list):
                ports_config = self.config

        if not ports_config:
            raise ValueError(f"Serial adapter {self.name} has no ports configured")

        for port_config in ports_config:
            if not isinstance(port_config, dict):
                self.logger.error(f"Invalid port config (not a dict): {port_config}")
                continue

            try:
                max_rw_users = self._resolve_max_rw_users(port_config, port_name=port_config.get("name"))
                # Create SerialPortConfig with validation
                serial_config = SerialPortConfig(
                    name=port_config["name"],
                    description=port_config.get("description", f"Serial port {port_config['name']}"),
                    device=port_config["device"],
                    baudrate=port_config.get("baudrate", 9600),
                    bytesize=port_config.get("bytesize", 8),
                    parity=port_config.get("parity", "N"),
                    stopbits=port_config.get("stopbits", 1),
                    timeout=port_config.get("timeout", 1.0),
                    flow_control=port_config.get("flow_control", "none"),
                    dtr=port_config.get("dtr", True),
                    rts=port_config.get("rts", True),
                    max_read_write_users=max_rw_users,
                    log_file=port_config.get("log_file"),
                    log_format=port_config.get("log_format"),
                    log_line_template=port_config.get("log_line_template"),
                )

                # Create wrapper
                notifier = None
                try:
                    def _notif(pname: str, payload: Dict[str, Any], _self=self):
                        try:
                            mpm = getattr(_self, "main_port_manager", None)
                            if mpm and hasattr(mpm, "notify_meta_updated"):
                                mpm.notify_meta_updated(pname, payload)  # type: ignore[attr-defined]
                        except Exception:
                            pass
                    notifier = _notif
                except Exception:
                    notifier = None
                port_wrapper = SerialPortWrapper(serial_config, self.logger, meta_notify=notifier)
                self.serial_ports[serial_config.name] = port_wrapper

                self.logger.info(f"Configured serial port {serial_config.name} -> {serial_config.device}")

            except Exception as e:
                port_name = port_config.get("name", "unknown") if isinstance(port_config, dict) else "unknown"
                self.logger.error(f"Failed to configure serial port {port_name}: {e}", exc_info=True)
                raise

    async def start(self) -> bool:
        """Start all configured serial ports.

        Returns:
            bool: True if at least one port started.
        """
        if self.is_running:
            return True

        self.logger.info(f"Starting serial adapter {self.name}")

        success_count = 0
        for port_name, port_wrapper in self.serial_ports.items():
            try:
                await port_wrapper.start()

                # Register with port manager
                if self.main_port_manager:
                    await self.main_port_manager.register_unified_port(
                        port_name=port_name,
                        unified_port=port_wrapper,
                        adapter=self,
                    )
                    self.logger.info(f"Registered serial port {port_name} with port manager")

                success_count += 1

            except Exception as e:
                self.logger.error(f"Failed to start serial port {port_name}: {e}", exc_info=True)

        if success_count > 0:
            self.is_running = True
            self.logger.info(f"Serial adapter {self.name} started with {success_count}/{len(self.serial_ports)} ports")
            return True
        else:
            self.logger.error(f"Failed to start any serial ports in adapter {self.name}")
            return False

    async def stop(self) -> None:
        """Stop all managed serial ports and cancel background tasks."""
        if not self.is_running:
            return

        self.logger.info(f"Stopping serial adapter {self.name}")

        for port_name, port_wrapper in self.serial_ports.items():
            try:
                await port_wrapper.stop()
                self.logger.info(f"Stopped serial port {port_name}")
            except Exception as e:
                self.logger.error(f"Error stopping serial port {port_name}: {e}", exc_info=True)

        self.is_running = False
        self.logger.info(f"Serial adapter {self.name} stopped")

    async def get_port_status(self, port_name: str) -> Dict[str, Any]:
        """Return structured status snapshot for a specific port."""
        if port_name not in self.serial_ports:
            return {"error": f"Port {port_name} not found"}

        port_wrapper = self.serial_ports[port_name]
        return {
            "name": port_name,
            "device": port_wrapper.config.device,
            "connected": port_wrapper.is_connected,
            "baudrate": port_wrapper.config.baudrate,
            "config": {
                "bytesize": port_wrapper.config.bytesize,
                "parity": port_wrapper.config.parity,
                "stopbits": port_wrapper.config.stopbits,
                "flow_control": port_wrapper.config.flow_control,
            },
            "reconnect_attempts": port_wrapper.reconnect_attempts,
        }

    async def run(self) -> None:
        """Legacy compatibility shim (no-op: data now flows via data_callback)."""
        pass

    # Required abstract methods from BaseGenericAdapter
    async def create_port(self, port_name: str, config: Dict[str, Any]) -> Optional[Any]:
        """Dynamic creation placeholder (not yet implemented).

        Returns:
            None: Dynamic addition currently unsupported.
        """
        # For now, serial ports are only created at startup
        # This could be extended for dynamic port creation
        return None

    async def destroy_port(self, port_name: str) -> None:
        """Destroy a managed port instance if present."""
        if port_name in self.serial_ports:
            port_wrapper = self.serial_ports[port_name]
            await port_wrapper.stop()
            del self.serial_ports[port_name]

    def get_port_configurations(self) -> Dict[str, Dict[str, Any]]:
        """Return mapping of port names to raw configuration dictionaries."""
        port_configs = {}
        for port_config in self.config.get("ports", []):
            port_name = port_config["name"]
            port_configs[port_name] = port_config
        return port_configs

    def get_adapter_type(self) -> str:
        """Return human-friendly adapter type string."""
        return "Serial"

    async def write_to_port(self, port_name: str, data: bytes) -> bool:
        """Write bytes to a specific serial port.

        Returns:
            bool: True if write succeeded; False otherwise.
        """
        if port_name not in self.serial_ports:
            self.logger.warning(f"Port {port_name} not found in serial ports")
            return False

        port_wrapper = self.serial_ports[port_name]
        if not port_wrapper.is_connected:
            self.logger.warning(f"Port {port_name} is not connected")
            return False

        try:
            self.logger.info(f"Writing {len(data)} bytes to serial port {port_name}: {data.decode('utf-8', errors='replace')}")
            bytes_written = await port_wrapper.write_data(data)
            self.logger.info(f"Successfully wrote {bytes_written} bytes to serial port {port_name}")
            return bytes_written > 0
        except Exception as e:
            self.logger.error(f"Failed to write to serial port {port_name}: {e}", exc_info=True)
            return False

    def get_status_info(self) -> Dict[str, Any]:
        """Return structured adapter status snapshot."""
        connected_ports = sum(1 for p in self.serial_ports.values() if p.is_connected)
        total_ports = len(self.serial_ports)

        return {
            "type": self.get_adapter_type(),
            "status": "running" if self.is_running else "stopped",
            "ports": f"{total_ports} ports",
            "connected": f"{connected_ports}/{total_ports}",
            "details": {
                "adapter_name": self.name,
                "port_count": total_ports,
                "connected_count": connected_ports,
                "ports": {name: wrapper.is_connected for name, wrapper in self.serial_ports.items()},
            },
        }

    # --- Live configuration reconciliation ---
    async def reconcile_ports(self, new_config: Any) -> Dict[str, Any]:
        """Incrementally reconcile serial ports with a new configuration.

        This preserves unchanged ports and their client connections, while
        cleanly removing or recreating only changed/removed ports.

        Args:
            new_config: Either a list of port dicts, or a dict containing
                        {'serial_ports': [...]} or {'ports': [...]}.

        Returns:
            Summary dict with counts and details: {added, removed, updated, unchanged}.
        """
        # Normalize incoming configuration to a list of dicts
        if isinstance(new_config, dict):
            if "serial_ports" in new_config and isinstance(new_config["serial_ports"], list):
                new_ports_list = list(new_config["serial_ports"])  # shallow copy
            elif "ports" in new_config and isinstance(new_config["ports"], list):
                new_ports_list = list(new_config["ports"])  # shallow copy
            else:
                # If dict but not expected shape, assume it's already a list-like config
                new_ports_list = []
        elif isinstance(new_config, list):
            new_ports_list = list(new_config)
        else:
            new_ports_list = []

        # Build index of new configs by name
        new_by_name: Dict[str, Dict[str, Any]] = {}
        for p in new_ports_list:
            if isinstance(p, dict) and p.get("name"):
                new_by_name[str(p["name"])] = p

        old_names = set(self.serial_ports.keys())
        new_names = set(new_by_name.keys())

        removed = sorted(old_names - new_names)
        added = sorted(new_names - old_names)
        common = sorted(old_names & new_names)

        # Determine which common ports have materially changed (require recreate)
        def _material_config(port_cfg: Dict[str, Any]) -> Dict[str, Any]:
            keys = [
                "device",
                "baudrate",
                "bytesize",
                "parity",
                "stopbits",
                "timeout",
                "flow_control",
                "dtr",
                "rts",
                "max_read_write_users",
            ]
            mat = {k: port_cfg.get(k) for k in keys}
            if mat.get("max_read_write_users") is None and "read_write_users" in port_cfg:
                mat["max_read_write_users"] = port_cfg.get("read_write_users")
            return mat

        updated: list[str] = []
        unchanged: list[str] = []
        for name in common:
            old_cfg = {}
            spw = None
            try:
                spw = self.serial_ports[name]
                # Extract material fields from existing dataclass
                old_cfg = {
                    "device": spw.config.device,
                    "baudrate": spw.config.baudrate,
                    "bytesize": spw.config.bytesize,
                    "parity": spw.config.parity,
                    "stopbits": spw.config.stopbits,
                    "timeout": spw.config.timeout,
                    "flow_control": spw.config.flow_control,
                    "dtr": spw.config.dtr,
                    "rts": spw.config.rts,
                    "max_read_write_users": spw.config.max_read_write_users,
                }
            except Exception:
                old_cfg = {}
            new_cfg = _material_config(new_by_name[name])
            if old_cfg == new_cfg:
                # Optionally update non-material attributes like description/logging without restart
                if spw is not None:
                    try:
                        desc = new_by_name[name].get("description")
                        if isinstance(desc, str) and desc and desc != getattr(spw, "description", None):
                            spw.description = desc
                    except Exception:
                        pass
                unchanged.append(name)
            else:
                updated.append(name)

        # Apply removals and updates (stop/unregister existing)
        async def _remove_port(name: str) -> None:
            try:
                spw = self.serial_ports.get(name)
                if not spw:
                    return
                # Unregister from main port manager first to stop broadcasts
                if self.main_port_manager:
                    try:
                        await self.main_port_manager.unregister_unified_port(name)
                    except Exception:
                        self.logger.warning(f"Failed to unregister unified port {name}")
                # Stop runtime wrapper
                try:
                    await spw.stop()
                except Exception as e:
                    self.logger.error(f"Error stopping serial port {name}: {e}", exc_info=True)
                # Remove from local map
                self.serial_ports.pop(name, None)
            except Exception as e:
                self.logger.error(f"Removal failure for {name}: {e}", exc_info=True)

        for name in removed + updated:
            await _remove_port(name)

        # Apply additions and re-creations
        async def _add_port_from_config(pcfg: Dict[str, Any]) -> None:
            try:
                max_rw = self._resolve_max_rw_users(pcfg, port_name=pcfg.get("name"))
                # Build validated dataclass; will raise on invalid values
                serial_cfg = SerialPortConfig(
                    name=pcfg["name"],
                    description=pcfg.get("description", f"Serial port {pcfg['name']}"),
                    device=pcfg["device"],
                    baudrate=pcfg.get("baudrate", 9600),
                    bytesize=pcfg.get("bytesize", 8),
                    parity=pcfg.get("parity", "N"),
                    stopbits=pcfg.get("stopbits", 1),
                    timeout=pcfg.get("timeout", 1.0),
                    flow_control=pcfg.get("flow_control", "none"),
                    dtr=pcfg.get("dtr", True),
                    rts=pcfg.get("rts", True),
                    max_read_write_users=max_rw,
                    log_file=pcfg.get("log_file"),
                    log_format=pcfg.get("log_format"),
                    log_line_template=pcfg.get("log_line_template"),
                )
                notifier = None
                try:
                    def _notif(pname: str, payload: Dict[str, Any], _self=self):
                        try:
                            mpm2 = getattr(_self, "main_port_manager", None)
                            if mpm2 and hasattr(mpm2, "notify_meta_updated"):
                                mpm2.notify_meta_updated(pname, payload)  # type: ignore[attr-defined]
                        except Exception:
                            pass
                    notifier = _notif
                except Exception:
                    notifier = None
                wrapper = SerialPortWrapper(serial_cfg, self.logger, meta_notify=notifier)
                self.serial_ports[serial_cfg.name] = wrapper
                await wrapper.start()
                if self.main_port_manager:
                    await self.main_port_manager.register_unified_port(serial_cfg.name, wrapper, self)
            except Exception as e:
                self.logger.error(f"Failed to (re)create serial port {pcfg.get('name','?')}: {e}", exc_info=True)

        for name in added:
            await _add_port_from_config(new_by_name[name])
        for name in updated:
            await _add_port_from_config(new_by_name[name])

        # Update adapter's config snapshot to reflect new state
        try:
            # Store under canonical 'ports' key for internal use
            self.config["ports"] = [new_by_name[n] for n in sorted(new_by_name.keys())]
        except Exception:
            pass

        summary = {
            "added": added,
            "removed": removed,
            "updated": updated,
            "unchanged": unchanged,
        }
        self.logger.info(
            f"Serial adapter {self.name} reconcile summary: +{len(added)} ~{len(updated)} -{len(removed)} =unchanged {len(unchanged)}"
        )
        return summary
