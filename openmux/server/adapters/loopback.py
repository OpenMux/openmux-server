"""Unified Loopback Adapter.

Provides echo-style loopback ports for testing and diagnostics.
Each loopback port sanitizes control/escape sequences (optional) and can
inject an echo delay for timing tests. Implements unified adapter/port
contracts without legacy-specific interfaces.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Set

from .base_adapter import AdapterCapability, BaseGenericAdapter
from .lifecycle import PortState


class LoopbackPort:
    """Loopback port implementation.

    Echoes back any data written (after optional sanitation and delay) using
    a single async write entrypoint and an internal `asyncio.Queue` for data.

    Attributes:
        name: Logical port name.
        echo_delay: Optional artificial delay before echoing bytes.
        buffer_size: Max queued bytes (queue depth expressed in messages).
        sanitize_control: Whether to convert control sequences to tokens.
    """

    state: PortState  # enforced contract annotation

    def __init__(self, name: str, config: Dict[str, Any], adapter: "LoopbackAdapter"):
        """Initialize a loopback port instance.

        Args:
            name: Logical port name.
            config: Per-port configuration (echo delay, buffer size, sanitation).
            adapter: Owning loopback adapter.
        """
        self.name = name
        self.config = config
        self.adapter = adapter
        self.state = PortState.CONFIGURED
        self.logger = logging.getLogger(f"openmux.adapter.loopback.{name}")
        # Loopback ports are virtual; consider them connected while active
        self.is_connected = False

        # Loopback-specific configuration (backward compatible)
        self.echo_delay = config.get("echo_delay", 0.0)
        self.buffer_size = config.get("buffer_size", 1024)
        # Sanitize control/escape input to avoid impacting terminals
        self.sanitize_control = bool(config.get("sanitize_control", True))

        # Buffer to assemble incomplete ESC/CSI sequences across writes
        self._esc_buf = bytearray()

        # Internal queue for data
        self.data_queue = None

        # Client capacity hint used by wrappers/manager
        self.max_read_write_users = int(config.get("max_read_write_users", 5))
        # Unified interface placeholders
        self.data_callback = None  # Set by adapter/manager if used

    async def start(self) -> bool:
        """Initialize internal queues and mark port active.

        Returns:
            True on successful startup; False on error.
        """
        try:
            self.state = PortState.CREATING

            # Create the data queue
            self.data_queue = asyncio.Queue(maxsize=self.buffer_size)

            self.state = PortState.ACTIVE
            self.is_connected = True

            self.logger.info(f"Loopback port {self.name} started successfully")
            return True

        except Exception as e:
            self.logger.error(f"Failed to start loopback port {self.name}: {e}", exc_info=True)
            self.state = PortState.DEGRADED  # Use existing state
            return False

    async def stop(self) -> None:
        """Tear down queue and mark port destroyed."""
        try:
            self.state = PortState.DESTROYING

            # Clear the queue
            if self.data_queue:
                while not self.data_queue.empty():
                    try:
                        self.data_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                self.data_queue = None

            self.state = PortState.DESTROYED
            self.is_connected = False

            self.logger.info(f"Loopback port {self.name} stopped")

        except Exception as e:
            self.logger.error(f"Error stopping loopback port {self.name}: {e}", exc_info=True)

    async def write_data(self, data: bytes) -> int:
        """Unified write entrypoint.

        Args:
            data: Bytes to transmit.

        Returns:
            Number of input bytes processed.
        """
        if self.state != PortState.ACTIVE or not self.data_queue:
            raise RuntimeError(f"Loopback port {self.name} not active")
        if not data:
            return 0

        # Apply echo delay if configured
        if self.echo_delay > 0:
            await asyncio.sleep(self.echo_delay)

        # Interleave feedback immediately after each newline inside the chunk
        # Split on universal line boundaries while preserving the newline bytes
        parts = data.splitlines(keepends=True)
        if not parts:
            return 0
        for part in parts:
            if not part:
                continue
            safe_part = self.sanitize_data(part)
            if safe_part:
                await self.data_queue.put(safe_part.replace(b"\r", b"").replace(b"\n", b""))
            # If this part ends with a newline (CR or LF or CRLF), emit banner now
            if safe_part and (safe_part.endswith(b"\n") or safe_part.endswith(b"\r")):
                feedback_msg = b"[ENTER]\r\n"
                await self.data_queue.put(feedback_msg)

        self.logger.debug(f"Loopback write: {len(data)} bytes")
        return len(data)

    # Unified read API for tests and adapter helpers
    async def read_data(self, timeout: float = 1.0) -> bytes:
        """Read one chunk from the loopback queue.

        Args:
            timeout: Seconds to wait for data before timing out.

        Returns:
            Bytes from the data queue, or b"" on timeout.
        """
        if self.state != PortState.ACTIVE or not self.data_queue:
            raise RuntimeError(f"Loopback port {self.name} not active")
        try:
            return await asyncio.wait_for(self.data_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return b""

    async def read(self, timeout: float = 1.0) -> bytes:  # backward-compat alias used by some tests
        return await self.read_data(timeout)

    def sanitize_data(self, data: bytes) -> bytes:
        """Sanitize control / escape sequences for safe echo.

        - CR / LF preserved.
        - Printable ASCII bytes (0x20-0x7E) passed unchanged.
        - ESC-based cursor/navigation sequences converted to bracketed tags.
        - DEL -> ``[DEL]``; other C0 controls -> ``[CTRL-X]`` style tokens.
        - Bytes >= 0x80 passed through.

        Args:
            data: Raw incoming bytes.

        Returns:
            Sanitized bytes (or original if disabled).
        """
        if not self.sanitize_control or not data:
            return data

        # Prepend any previously buffered incomplete ESC sequence
        if self._esc_buf:
            data = bytes(self._esc_buf) + data
            self._esc_buf.clear()

        out = bytearray()
        i = 0
        n = len(data)
        while i < n:
            b = data[i]
            # Keep CR/LF as-is
            if b in (0x0A, 0x0D):
                out.append(b)
                i += 1
                continue
            # Printable ASCII
            if 0x20 <= b <= 0x7E:
                out.append(b)
                i += 1
                continue
            # DEL
            if b == 0x7F:
                out.extend(b"[DEL]")
                i += 1
                continue
            # ESC sequences
            if b == 0x1B:
                # Need at least one more byte to decide
                if i + 1 >= n:
                    # Buffer incomplete ESC and wait for next data
                    self._esc_buf.extend(data[i:])
                    break

                second = data[i + 1]
                # CSI sequence ESC [ ...
                if second == ord("["):
                    # Need at least ESC [ X
                    if i + 2 >= n:
                        self._esc_buf.extend(data[i:])
                        break
                    third = data[i + 2]
                    # Arrow/home/end single-letter forms
                    if third == ord("A"):
                        out.extend(b"[UP]")
                        i += 3
                        continue
                    if third == ord("B"):
                        out.extend(b"[DOWN]")
                        i += 3
                        continue
                    if third == ord("C"):
                        out.extend(b"[RIGHT]")
                        i += 3
                        continue
                    if third == ord("D"):
                        out.extend(b"[LEFT]")
                        i += 3
                        continue
                    if third == ord("H"):
                        out.extend(b"[HOME]")
                        i += 3
                        continue
                    if third == ord("F"):
                        out.extend(b"[END]")
                        i += 3
                        continue

                    # ESC [ <digits> ~ sequences (Insert/Del/PgUp/PgDn, etc.)
                    if third >= ord("0") and third <= ord("9"):
                        j = i + 2
                        digits = bytearray()
                        while j < n and data[j] >= ord("0") and data[j] <= ord("9"):
                            digits.append(data[j])
                            j += 1
                        # Incomplete at buffer end: keep for next call
                        if j >= n:
                            self._esc_buf.extend(data[i:])
                            break
                        if data[j] == ord("~") and digits:
                            code = int(digits.decode("ascii"))
                            if code == 1:
                                out.extend(b"[HOME]")
                            elif code == 2:
                                out.extend(b"[INSERT]")
                            elif code == 3:
                                out.extend(b"[DEL]")
                            elif code == 4:
                                out.extend(b"[END]")
                            elif code == 5:
                                out.extend(b"[PGUP]")
                            elif code == 6:
                                out.extend(b"[PGDN]")
                            else:
                                out.extend(b"[CSI-" + digits + b"~]")
                            i = j + 1
                            continue
                        # Not a ~ terminator; treat as bare ESC and continue
                        out.extend(b"[ESC]")
                        i += 1
                        continue

                    # Unrecognized ESC [ sequence; treat as bare ESC
                    out.extend(b"[ESC]")
                    i += 1
                    continue

                # SS3 sequence ESC O A/B/C/D (some terminals for arrows)
                if second == ord("O"):
                    if i + 2 >= n:
                        self._esc_buf.extend(data[i:])
                        break
                    third = data[i + 2]
                    if third == ord("A"):
                        out.extend(b"[UP]")
                        i += 3
                        continue
                    if third == ord("B"):
                        out.extend(b"[DOWN]")
                        i += 3
                        continue
                    if third == ord("C"):
                        out.extend(b"[RIGHT]")
                        i += 3
                        continue
                    if third == ord("D"):
                        out.extend(b"[LEFT]")
                        i += 3
                        continue
                    # Unknown ESC O sequence; output [ESC]
                    out.extend(b"[ESC]")
                    i += 1
                    continue

                # Bare ESC (not followed by recognized prefix)
                out.extend(b"[ESC]")
                i += 1
                continue
            # Horizontal tab
            if b == 0x09:
                out.extend(b"[TAB]")
                i += 1
                continue
            # Other C0 controls -> caret/tag style
            if b < 0x20:
                # Map 0x00 to [NUL], others to [CTRL-X]
                if b == 0x00:
                    out.extend(b"[NUL]")
                else:
                    try:
                        out.extend(b"[CTRL-" + bytes([b + 64]) + b"]")
                    except Exception:  # justification: rare formatting edge; fallback token adequate, avoid noisy logs
                        out.extend(b"[CTRL]")
                i += 1
                continue
            # Non-ASCII bytes (>= 0x80): pass through unchanged
            out.append(b)
            i += 1

        return bytes(out)


class LoopbackAdapter(BaseGenericAdapter):  # noqa: Vulture
    """Loopback adapter providing backward-compatible echo ports.

    Creates one or more loopback ports that echo inbound data, optionally
    sanitizing control characters and adding echo delays for test purposes.
    """

    def __init__(self, plugin_name: str, config: Dict[str, Any]):
        """Initialize the loopback adapter.

        Args:
            plugin_name: Adapter plugin name (used for logging identity).
            config: Adapter configuration containing a `loopback_ports` list.
        """
        # The config contains the list of ports under the plugin-specific key
        # For loopback, this will be config["loopback_ports"]
        super().__init__(plugin_name, config)
        self.ports: Dict[str, LoopbackPort] = {}
        self.logger = logging.getLogger(f"openmux.adapter.{plugin_name}")

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> bool:  # pragma: no cover
        """Validate unified loopback adapter configuration strictly.

        Expected shape (unified only):
            {
              "loopback_ports": [
                 {
                   "name": str (non-empty),
                   "echo_delay": float >= 0 (optional),
                   "buffer_size": int >= 1 (optional),
                   "sanitize_control": bool (optional),
                   "max_read_write_users": int >= 1 (optional)
                 }, ...
              ]
            }
        """
        try:
            if not isinstance(config, dict):
                return False
            ports = config.get("loopback_ports")
            if not isinstance(ports, list):
                return False
            for i, entry in enumerate(ports):
                if not isinstance(entry, dict):
                    return False
                name = entry.get("name")
                if not isinstance(name, str) or not name.strip():
                    return False
                if "echo_delay" in entry:
                    ed = entry["echo_delay"]
                    if not isinstance(ed, (int, float)) or ed < 0:
                        return False
                if "buffer_size" in entry:
                    bs = entry["buffer_size"]
                    if not isinstance(bs, int) or bs <= 0:
                        return False
                if "sanitize_control" in entry:
                    sc = entry["sanitize_control"]
                    if not isinstance(sc, bool):
                        return False
                # Unified-only: reject legacy synonyms
                if "read_write_users" in entry or "read_write_users_max" in entry:
                    return False
                if "max_read_write_users" in entry:
                    mru = entry["max_read_write_users"]
                    if not isinstance(mru, int) or mru < 1:
                        return False
            return True
        except Exception:  # justification: malformed config structure; simple False result suffices for validator contract
            return False

    def get_adapter_type(self) -> str:
        """Return adapter type identifier."""
        return "loopback"

    def get_capabilities(self) -> Set[AdapterCapability]:
        """Return capability flags for loopback adapter.

        Returns:
            Set[AdapterCapability]: Provides virtual ports with bidirectional data.
        """
        return {AdapterCapability.PROVIDES_PORTS, AdapterCapability.BIDIRECTIONAL_DATA}

    def get_port_configurations(self) -> Dict[str, Dict[str, Any]]:
        """Return mapping of port names to configuration dicts."""
        port_configs = {}

        self.logger.debug(f"Getting port configurations from config: {self.config}")

        # Get the loopback_ports list from the config
        loopback_ports = self.config.get("loopback_ports", [])
        if isinstance(loopback_ports, list):
            for port_config in loopback_ports:
                if isinstance(port_config, dict) and "name" in port_config:
                    port_name = port_config.get("name", "")
                    if port_name:
                        port_configs[str(port_name)] = port_config
                        self.logger.debug(f"Added port config: {port_name} -> {port_config}")

        self.logger.debug(f"Final port configurations: {port_configs}")
        return port_configs

    async def create_port(self, port_name: str, config: Dict[str, Any]) -> Optional[LoopbackPort]:
        """Create and start a loopback port instance."""
        try:
            port = LoopbackPort(port_name, config, self)
            # No intrinsic callback needed because LoopbackPort uses an internal queue.
            # Unified routing is achieved via manager wrapper polling; if we later
            # add push notifications, wire a callback to call
            # self.main_port_manager.send_data_from_unified_port(port_name, data).
            if await port.start():
                self.ports[port_name] = port
                self.logger.info(f"Created loopback port: {port_name}")
                # Register with PortManager so unified wrapper and routing are active
                try:
                    if hasattr(self, "main_port_manager") and self.main_port_manager:
                        await self.main_port_manager.register_unified_port(port_name, port, self)
                        self.logger.info(f"Registered loopback port {port_name} with port manager")
                except Exception:
                    self.logger.warning(f"Failed to register loopback port {port_name} with port manager", exc_info=True)
                return port
            else:
                self.logger.error(f"Failed to start loopback port: {port_name}")
                return None

        except Exception as e:
            self.logger.error(f"Error creating loopback port {port_name}: {e}", exc_info=True)
            return None

    async def destroy_port(self, port_name: str) -> None:
        """Stop and remove a managed loopback port."""
        port = self.ports.get(port_name)
        if port:
            # Unregister from port manager first to stop broadcasts
            try:
                if hasattr(self, "main_port_manager") and self.main_port_manager:
                    await self.main_port_manager.unregister_unified_port(port_name)
            except Exception:
                self.logger.warning(f"Failed to unregister loopback port {port_name}")
            await port.stop()
            del self.ports[port_name]
            self.logger.info(f"Destroyed loopback port: {port_name}")

    async def start(self) -> bool:
        """Create and start configured loopback ports."""
        # Create loopback ports directly from configuration
        success = await self._create_loopback_ports_from_config()

        if success:
            port_count = len(self.ports)
            self.logger.info(f"Loopback adapter {self.name} started with {port_count} ports")
        else:
            self.logger.error(f"Failed to start loopback adapter {self.name}")

        self.is_running = success
        return success

    async def _create_loopback_ports_from_config(self) -> bool:
        """Internal helper to instantiate configured ports.

        Returns:
            bool: True if at least one port created (or none configured).
        """
        try:
            # Get port configurations from adapter-specific config
            port_configs = self.get_port_configurations()

            self.logger.debug(f"Creating {len(port_configs)} loopback ports from config")

            success_count = 0
            for port_name, port_config in port_configs.items():
                port = await self.create_port(port_name, port_config)
                if port:
                    success_count += 1

            self.logger.info(f"Created {success_count}/{len(port_configs)} loopback ports")
            return success_count > 0 or len(port_configs) == 0

        except Exception as e:
            self.logger.error(f"Error creating loopback ports from config: {e}", exc_info=True)
            return False

    async def stop(self) -> None:
        """Stop adapter and destroy loopback ports.

        Destroys all managed ports explicitly (not relying on base class
        abstract stop implementation to keep static analysis simple).
        """
        # Destroy ports we created
        for port_name in list(self.ports.keys()):
            try:
                await self.destroy_port(port_name)
            except Exception as e:
                self.logger.error(f"Error destroying loopback port {port_name}: {e}", exc_info=True)

        self.ports.clear()
        self.is_running = False
        self.logger.info(f"Loopback adapter {self.name} stopped")

    def get_status_info(self) -> Dict[str, Any]:  # pragma: no cover - simple aggregation
        """Return summary info used by server status logger.

        Matches keys accessed in main._log_server_status(): type, ports.
        """
        return {
            "type": "loopback",
            "status": "running" if self.is_running else "stopped",
            "ports": f"{len(self.ports)} configured",
            "details": {
                "adapter_name": self.name,
                "total_ports": len(self.ports),
                "port_list": [name for name in self.ports.keys()],
            },
        }

    async def write_to_port(self, port_name: str, data: bytes) -> int:
        """Unified adapter write path for loopback ports.

        Args:
            port_name: Target loopback port name.
            data: Bytes to echo.

        Returns:
            Number of bytes accepted (0 on failure).
        """
        port = self.ports.get(port_name)
        if not port:
            self.logger.error(f"Loopback port {port_name} not found")
            return 0
        try:
            return await port.write_data(data)
        except Exception as e:
            self.logger.error(f"Error writing to loopback port {port_name}: {e}", exc_info=True)
            return 0

    # --- Live configuration reconciliation ---
    async def reconcile_ports(self, new_config: Any) -> Dict[str, Any]:
        """Incrementally update loopback ports configuration.

        Args:
            new_config: Dict with key 'loopback_ports' as list, or direct list.

        Returns:
            Summary dict: {added, removed, updated, unchanged}.
        """
        # Normalize new config to list of dicts
        items: List[Dict[str, Any]] = []
        if isinstance(new_config, dict) and isinstance(new_config.get("loopback_ports"), list):
            items = list(new_config["loopback_ports"])  # shallow copy
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
            # All fields except 'name' and 'description' are material
            out = dict(cfg)
            out.pop("name", None)
            out.pop("description", None)
            return out

        updated: List[str] = []
        unchanged: List[str] = []
        for n in common:
            try:
                port = self.ports[n]
                # Build current cfg snapshot from port internals (best-effort)
                old_cfg = {
                    "echo_delay": getattr(port, "echo_delay", None),
                    "buffer_size": getattr(port, "buffer_size", None),
                    "sanitize_control": getattr(port, "sanitize_control", None),
                    "max_read_write_users": getattr(port, "max_read_write_users", None),
                }
            except Exception:
                old_cfg = {}
            if old_cfg == _material_cfg(new_by_name[n]):
                # Optionally update description in-place
                try:
                    new_desc = new_by_name[n].get("description")
                    if isinstance(new_desc, str) and new_desc:
                        setattr(self.ports[n], "description", new_desc)
                except Exception:
                    pass
                unchanged.append(n)
            else:
                updated.append(n)

        # Apply removals/updates
        for n in removed + updated:
            try:
                await self.destroy_port(n)
            except Exception as e:
                self.logger.error(f"Failed to destroy loopback port {n}: {e}", exc_info=True)

        # Apply additions and re-creations
        for n in added + updated:
            cfg = new_by_name.get(n)
            if not cfg:
                continue
            try:
                await self.create_port(n, cfg)
            except Exception as e:
                self.logger.error(f"Failed to create loopback port {n}: {e}", exc_info=True)

        # Update internal config snapshot
        try:
            self.config["loopback_ports"] = [new_by_name[k] for k in sorted(new_by_name.keys())]
        except Exception:
            pass

        summary = {"added": added, "removed": removed, "updated": updated, "unchanged": unchanged}
        self.logger.info(
            f"Loopback adapter {self.name} reconcile: +{len(added)} ~{len(updated)} -{len(removed)} unchanged={len(unchanged)}"
        )
        return summary
