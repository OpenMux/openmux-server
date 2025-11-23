#!/usr/bin/env python3
"""
OpenMux Server - Main entry point (Unified Adapters)
"""
import argparse
import asyncio
import logging
import contextlib
import os
import signal
import sys
from typing import Any, Dict, List, Optional
import json
import stat

from .auth_manager import AuthManager
from .config_manager import ConfigManager
from .console_manager import ConsoleManager
from .port_manager import PortManager


class OpenMuxServer:
    """OpenMux Server using the unified adapter plugin system.

    Unified adapters provide both connection endpoints (e.g., TCP server,
    federation, status) and port adapters (e.g., loopback, serial, command).
    The legacy connection adapter framework has been removed.
    """

    def __init__(
        self,
        config_path: str,
        *,
        auth_config_path: Optional[str] = None,
        security_config_path: Optional[str] = None,
        log_level: Optional[str] = None,
    ):
        """Construct a server bound to a configuration file.

        Args:
            config_path: Path to the server configuration YAML file.
        """
        # Set up basic logging early; default overridden after config load
        _setup_basic_logging(level_name=log_level)
        self.logger = logging.getLogger("openmux.server")

        # Load configuration
        self.config_manager = ConfigManager(
            config_path,
            auth_config_path=auth_config_path,
            security_config_path=security_config_path,
        )
        self.security_policy = None
        self._reload_config_from_disk()

        # After loading config, re-evaluate logging level from config.logging.level (if present)
        try:
            cfg = getattr(self.config_manager, "config", {}) or {}
            logging_cfg = cfg.get("logging", {}) if isinstance(cfg, dict) else {}
            cfg_level = logging_cfg.get("level")
            # If CLI requested -v (DEBUG), keep DEBUG; else prefer config level when provided
            effective_level = None
            if log_level and str(log_level).upper() == "DEBUG":
                effective_level = "DEBUG"
            elif cfg_level:
                effective_level = str(cfg_level).upper()
            if effective_level:
                _setup_basic_logging(level_name=effective_level)
        except Exception:
            # Non-fatal if logging recompute fails; continue with prior setup
            pass

        # Initialize core components
        self.auth_manager = AuthManager(
            self.config_manager.get_authentication_config(),
            security_policy=self.security_policy,
        )
        self.port_manager = PortManager({})
        # Expose config manager to port manager so adapters can reach server id
        try:
            setattr(self.port_manager, "config_manager", self.config_manager)
        except Exception:  # justification: optional attribute injection; non-fatal if it fails
            pass
        self.console_manager = ConsoleManager(self.port_manager, self.auth_manager)
        # Provide a back-reference so web plugins can reach server APIs (e.g., full reload)
        try:
            setattr(self.console_manager, "server", self)
        except Exception:
            # Best-effort; some unit tests may stub ConsoleManager differently
            pass

        # Legacy connection adapters removed; keep empty structure for status API compatibility
        self.adapters = {}

        # Initialize unified adapters (for port adapters: loopback, serial, command, etc.)
        from .adapters import GenericAdapterFactory

        self.unified_adapter_factory = GenericAdapterFactory(security_policy=self.security_policy)
        self.unified_adapters = []

        # Server state
        self.is_running = False
        self.shutdown_event = asyncio.Event()

        # Control socket server (Unix domain) for local CLI control
        self._control_server = None
        self._control_socket_path = None

        # Legacy connection adapter configuration removed; unified adapters handle connections where applicable

    def _refresh_security_policy(self) -> None:
        try:
            policy = self.config_manager.get_security_policy()
        except Exception as exc:
            self.logger.warning("Failed to load security policy: %s", exc)
            return
        self.security_policy = policy
        auth = getattr(self, "auth_manager", None)
        if auth and hasattr(auth, "update_security_policy"):
            try:
                auth.update_security_policy(policy)
            except Exception:
                self.logger.warning("AuthManager failed to apply security policy", exc_info=True)
        factory = getattr(self, "unified_adapter_factory", None)
        if factory and hasattr(factory, "set_security_policy"):
            try:
                factory.set_security_policy(policy)
            except Exception:
                self.logger.warning("Adapter factory failed to accept security policy", exc_info=True)

    def _reload_config_from_disk(self) -> Dict[str, Any]:
        cfg = self.config_manager.load_config()
        self._refresh_security_policy()
        return cfg

    async def _initialize_server_components(self):
        """Initialize server components and monitor shutdown event.

        Sets up shutdown monitoring, initializes legacy port connections,
        and starts unified adapters per configuration.
        """
        # Monitor shutdown event if available
        if self.shutdown_event:
            asyncio.create_task(self._monitor_shutdown_event())

    # Legacy port initialization removed (unified-only)

        # Initialize unified adapters (new system)
        await self._initialize_unified_adapters()

        # Start control socket if configured/available
        try:
            ctl_path = self._resolve_control_socket_path()
            if ctl_path:
                await self._start_control_socket(ctl_path)
        except Exception:
            self.logger.warning("Control socket startup failed; CLI control disabled", exc_info=True)

    async def _initialize_unified_adapters(self):
        """Initialize the unified adapter system from configuration.

        Creates adapter instances via the plugin factory, wires dependencies,
        and starts them. Logs detailed errors per adapter on failure.
        """
        try:
            # Get the full configuration
            if not self.config_manager.config:
                self._reload_config_from_disk()

            full_config = self.config_manager.config
            if not full_config:
                self.logger.warning("No configuration loaded for unified adapters")
                return

            # Create unified adapters from configuration
            self.unified_adapters = self.unified_adapter_factory.create_adapters_from_config(full_config)

            if self.unified_adapters:
                self.logger.info(f"Created {len(self.unified_adapters)} unified adapters")

                # Connect unified adapters to the legacy PortManager for integration
                self.port_manager.set_unified_adapters(self.unified_adapters)

                # Set dependencies on all adapters before starting them to avoid races
                for adapter in self.unified_adapters:
                    if hasattr(adapter, "main_port_manager"):
                        adapter.main_port_manager = self.port_manager
                    set_auth = getattr(adapter, "set_auth_manager", None)
                    if callable(set_auth):
                        set_auth(self.auth_manager)
                    set_console = getattr(adapter, "set_console_manager", None)
                    if callable(set_console):
                        # Adapters that accept connections will register as client manager here (Option D)
                        set_console(self.console_manager)

                # Start all unified adapters
                for adapter in self.unified_adapters:
                    try:
                        success = await adapter.start()
                        if success:
                            adapter_type = getattr(
                                adapter,
                                "adapter_type",
                                adapter.__class__.__name__,
                            )
                            self.logger.info(f"Started unified adapter: {adapter.name} ({adapter_type})")
                        else:
                            self.logger.error(f"Failed to start unified adapter: {adapter.name}")
                    except Exception as e:
                        self.logger.error(f"Error starting unified adapter {adapter.name}: {e}", exc_info=True)
            else:
                self.logger.info("No unified adapters configured")

        except Exception as e:
            self.logger.error(f"Error initializing unified adapters: {e}", exc_info=True)
            import traceback

            traceback.print_exc()

    # ================= Control Socket (Unix domain) =================

    def _resolve_control_socket_path(self) -> Optional[str]:
        """Resolve the control socket path from env/config or default.

        Precedence:
          1) env OPENMUX_CTL_SOCK
          2) config.server.control_socket
          3) config.runtime.control_socket (deprecated; fallback)
          4) logs/openmux.sock (default)
        """
        try:
            path = os.environ.get("OPENMUX_CTL_SOCK")
            if not path:
                try:
                    cfg = getattr(self.config_manager, "config", {}) or {}
                    # Preferred location: server.control_socket
                    srv = (cfg.get("server", {}) or {})
                    path = srv.get("control_socket")
                    if not path:
                        # Back-compat (deprecated): runtime.control_socket
                        rt = (cfg.get("runtime", {}) or {})
                        path = rt.get("control_socket")
                        if path:
                            try:
                                self.logger.warning(
                                    "Using deprecated runtime.control_socket; please move to server.control_socket"
                                )
                            except Exception:
                                pass
                except Exception:
                    path = None
            if not path:
                path = os.path.join("logs", "openmux.sock")
            # Ensure parent exists
            os.makedirs(os.path.dirname(path), exist_ok=True)
            return path
        except Exception:
            return None

    async def _start_control_socket(self, path: str) -> None:
        """Start a Unix domain control socket for local CLI control."""
        try:
            # If an old socket exists, unlink it (best-effort)
            with contextlib.suppress(Exception):
                if os.path.exists(path) and stat.S_ISSOCK(os.stat(path).st_mode):
                    os.unlink(path)
            server = await asyncio.start_unix_server(self._handle_control_client, path=path)
            self._control_server = server
            self._control_socket_path = path
            # Restrict permissions to owner only
            with contextlib.suppress(Exception):
                os.chmod(path, 0o600)
            self.logger.info(f"Control socket listening at {path}")
        except Exception as e:
            self.logger.error(f"Failed to start control socket on {path}: {e}", exc_info=True)
            raise

    async def _stop_control_socket(self) -> None:
        """Stop the Unix domain control socket and unlink the file."""
        try:
            if self._control_server:
                self._control_server.close()
                with contextlib.suppress(Exception):
                    await self._control_server.wait_closed()
                self._control_server = None
            if self._control_socket_path and os.path.exists(self._control_socket_path):
                with contextlib.suppress(Exception):
                    os.unlink(self._control_socket_path)
            self._control_socket_path = None
        except Exception as e:
            self.logger.warning(f"Error stopping control socket: {e}")

    async def _handle_control_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle one control connection: read one JSON line, respond, close."""
        addr = "local-unix"
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not raw:
                return
            try:
                req = json.loads(raw.decode("utf-8").strip())
            except Exception as e:
                await self._write_control_response(writer, ok=False, error=f"invalid_json: {e}")
                return
            action = str(req.get("action") or "").lower()
            # Dispatch
            if action == "reload":
                scope = str(req.get("scope") or "soft").lower()
                if scope == "soft":
                    res = await self.reload_adapters_soft(context={"origin": "control-socket", "user": "local", "remote": addr, "req_id": "ctl-soft"})
                    await self._write_control_response(writer, ok=True, result=res)
                    return
                elif scope == "full":
                    res = await self.reload_adapters_full(context={"origin": "control-socket", "user": "local", "remote": addr, "req_id": "ctl-full"})
                    await self._write_control_response(writer, ok=True, result=res)
                    return
                else:
                    await self._write_control_response(writer, ok=False, error=f"unknown_scope: {scope}")
                    return
            elif action == "status":
                # Minimal status snapshot suitable for CLI
                adapters = getattr(self, "unified_adapters", []) or []
                started = sum(1 for a in adapters if getattr(a, "is_running", False))
                names = [getattr(a, "name", "?") for a in adapters]
                await self._write_control_response(writer, ok=True, result={"adapters": names, "started": started, "total": len(adapters)})
                return
            else:
                await self._write_control_response(writer, ok=False, error=f"unknown_action: {action}")
        except asyncio.TimeoutError:
            await self._write_control_response(writer, ok=False, error="timeout")
        except Exception as e:
            self.logger.error(f"Control socket error: {e}", exc_info=True)
            with contextlib.suppress(Exception):
                await self._write_control_response(writer, ok=False, error=str(e))
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _write_control_response(self, writer: asyncio.StreamWriter, ok: bool, error: Optional[str] = None, result: Optional[Dict[str, Any]] = None):
        resp: Dict[str, Any] = {"ok": bool(ok)}
        if error:
            resp["error"] = str(error)
        if result is not None:
            resp["result"] = result
        data = (json.dumps(resp) + "\n").encode("utf-8")
        writer.write(data)
        await writer.drain()

    def _create_and_configure_adapters(self):
        """Create and configure adapters (unified only).

        Returns:
            bool: True when configuration and dependency wiring completes.
        """
        # Check if we have unified adapters that can accept connections
        connection_unified_adapters = []
        if self.unified_adapters:
            from .adapters.base_adapter import AdapterCapability

            for adapter in self.unified_adapters:
                if AdapterCapability.ACCEPTS_CONNECTIONS in adapter.get_capabilities():
                    connection_unified_adapters.append(adapter)

        if connection_unified_adapters:
            self.logger.info(f"Using {len(connection_unified_adapters)} connection endpoints (unified)")
            # Set up unified adapters for connection handling
            for adapter in connection_unified_adapters:
                if hasattr(adapter, "set_auth_manager"):
                    adapter.set_auth_manager(self.auth_manager)
                if hasattr(adapter, "set_console_manager"):
                    adapter.set_console_manager(self.console_manager)
        else:
            self.logger.info("No connection endpoints (unified) configured")
        return True

    def _categorize_adapters(self):  # noqa: Vulture (kept for future runtime management)
        """Categorize adapters into server (listen) and client (connect) types.

        Returns:
            Tuple[dict, dict]: Mapping of server adapters and client adapters.
        """
        server_adapters = {}
        client_adapters = {}

        for name, adapter in self.adapters.items():
            # Client adapters are those that make outbound connections (by name only now)
            if "client" in name.lower():
                client_adapters[name] = adapter
            else:
                server_adapters[name] = adapter

        return server_adapters, client_adapters

    async def _start_server_adapters(self, adapters):  # noqa: Vulture (legacy pathway retained)
        """Start server adapters (those that listen for connections).

        Args:
            adapters: Mapping of adapter name to adapter instance.

        Returns:
            int: Number of adapters started successfully.
        """
        started_count = 0
        for name, adapter in adapters.items():
            try:
                if await adapter.start_server():
                    self.logger.info(
                        f"Started {name} adapter ({adapter.__class__.__name__}) " f"on {adapter.host}:{adapter.port}"
                    )
                    started_count += 1
                else:
                    self.logger.error(f"Failed to start {name} adapter")
            except Exception as e:
                self.logger.error(f"Error starting {name} adapter: {e}", exc_info=True)

        return started_count

    async def _start_client_adapters(self, adapters):  # noqa: Vulture (legacy pathway retained)
        """Start client adapters (those that make outbound connections).

        Args:
            adapters: Mapping of adapter name to adapter instance.

        Returns:
            int: Number of adapters started successfully.
        """
        started_count = 0
        for name, adapter in adapters.items():
            try:
                self.logger.info(f"Starting client adapter {name}...")
                if await adapter.start_server():
                    self.logger.info(
                        f"Started {name} adapter ({adapter.__class__.__name__}) "
                        f"connecting to {adapter.hub_host if hasattr(adapter, 'hub_host') else 'remote'}:"
                        f"{adapter.hub_port if hasattr(adapter, 'hub_port') else 'unknown'}"
                    )
                    started_count += 1
                else:
                    self.logger.error(f"Failed to start {name} adapter")
            except Exception as e:
                self.logger.error(f"Error starting {name} adapter: {e}", exc_info=True)

        return started_count

    async def _start_all_adapters(self):
        """Start all configured adapters with proper sequencing.

        Returns:
            int: Number of connection endpoints started.
        """
        # Determine unified adapters that expose connection endpoints
        from .adapters.base_adapter import AdapterCapability

        connection_unified_adapters = []
        port_unified_adapters = []

        if self.unified_adapters:
            for adapter in self.unified_adapters:
                if AdapterCapability.ACCEPTS_CONNECTIONS in adapter.get_capabilities():
                    connection_unified_adapters.append(adapter)
                else:
                    port_unified_adapters.append(adapter)

        total_started = 0

        # Start unified connection endpoints (if any)
        if connection_unified_adapters:
            self.logger.info(f"Starting {len(connection_unified_adapters)} connection endpoints...")
            for adapter in connection_unified_adapters:
                try:
                    if adapter.is_running:
                        self.logger.info(f"Connection endpoint already running: {adapter.name}")
                        total_started += 1
                    elif await adapter.start():
                        self.logger.info(f"Started connection endpoint: {adapter.name}")
                        total_started += 1
                    else:
                        self.logger.error(f"Failed to start connection endpoint: {adapter.name}")
                except Exception as e:
                    self.logger.error(f"Error starting connection endpoint {adapter.name}: {e}", exc_info=True)

        # Legacy connection adapters removed; nothing else to start here

        # Port unified adapters are already started in _initialize_unified_adapters

        if total_started == 0 and not connection_unified_adapters:
            self.logger.error("No connection endpoints started successfully")
            return 0

        self.logger.info(f"Started {total_started} connection endpoints")
        return total_started

    async def _run_server_loop(self):
        """Run the main server loop until shutdown."""
        try:
            await asyncio.Event().wait()  # Wait forever
        except asyncio.CancelledError:
            self.logger.info("Server task cancelled")
            # Don't call shutdown again - it's already being handled by the signal handler
            # Just exit gracefully without re-raising the exception

    async def start(self):
        """Start the OpenMux server (unified adapters).

        Returns:
            bool: True on successful startup; False on failure.
        """
        self.logger.info("Starting OpenMux server...")

        try:
            # Initialize server components
            await self._initialize_server_components()

            # Create and configure adapters
            if not self._create_and_configure_adapters():
                return False

            # Start all adapters
            started_count = await self._start_all_adapters()
            if started_count == 0:
                return False

            self.is_running = True

            # Display server information
            self._log_server_status()

            # Keep server running indefinitely
            await self._run_server_loop()

        except asyncio.CancelledError:
            # Shutdown is being handled by signal handler, exit gracefully
            self.logger.info("Server start cancelled during shutdown")
            return True
        except Exception as e:
            self.logger.error(f"Error starting server: {e}", exc_info=True)
            await self.shutdown()
            return False

    def _log_server_status(self):
        """Log current server status."""
        self.logger.info("=" * 60)
        self.logger.info("OpenMux Server Status")
        self.logger.info("=" * 60)
        # Check if we have unified connection endpoints
        from .adapters.base_adapter import AdapterCapability

        connection_unified_adapters = []
        port_unified_adapters = []

        if self.unified_adapters:
            for adapter in self.unified_adapters:
                if AdapterCapability.ACCEPTS_CONNECTIONS in adapter.get_capabilities():
                    connection_unified_adapters.append(adapter)
                else:
                    port_unified_adapters.append(adapter)

        # Show connection endpoints (unified)
        if connection_unified_adapters:
            self.logger.info("Connection Endpoints:")
            for adapter in connection_unified_adapters:
                status_info = adapter.get_status_info()
                status = "🟢 Running" if adapter.is_running else "🔴 Stopped"
                adapter_type = str(status_info.get("type", getattr(adapter, "adapter_type", adapter.__class__.__name__)))
                details = status_info.get("details", {}) or {}

                # Build per-line endpoints for readability
                endpoints_lines: list[str] = []
                atype_l = adapter_type.lower()

                if atype_l in ("webconsole", "web_console", "web-console"):
                    host = details.get("host")
                    port = details.get("port")
                    tls = bool(details.get("tls"))
                    ssl_port = details.get("ssl_port")
                    http_redirect = bool(details.get("http_redirect", False))
                    if tls and ssl_port and ssl_port != port:
                        # Dual-port: one line for HTTP (redirect) and one for HTTPS
                        try:
                            endpoints_lines.append(f"{host}:{port} (http{' redirect' if http_redirect else ''})")
                        except Exception:
                            endpoints_lines.append(f"{details.get('host')}:{details.get('port')}")
                        endpoints_lines.append(f"{host}:{ssl_port} (https)")
                    else:
                        scheme = "https" if tls else "http"
                        endpoints_lines.append(f"{host}:{port} ({scheme})")
                elif atype_l == "muxcon":
                    listeners = details.get("listeners") or []
                    for lst in listeners:
                        try:
                            if not lst.get("enabled", True):
                                continue
                            h = lst.get("host")
                            p = lst.get("port")
                            line = f"{h}:{p}" + (" (tls)" if lst.get("use_tls") else " (plain)")
                            endpoints_lines.append(line)
                        except Exception:
                            continue
                    # Fallback to comma-separated endpoint string if no structured listeners present
                    if not endpoints_lines:
                        ep = str(status_info.get("endpoint", "")).strip()
                        if ep:
                            endpoints_lines.extend([e.strip() for e in ep.split(",") if e.strip()])
                else:
                    # Generic adapters: prefer structured list in details.endpoints; else split endpoint string
                    eps = details.get("endpoints")
                    if isinstance(eps, list) and eps:
                        endpoints_lines.extend([str(e) for e in eps])
                    else:
                        ep = str(status_info.get("endpoint", "")).strip()
                        if ep:
                            endpoints_lines.extend([e.strip() for e in ep.split(",") if e.strip()])

                if not endpoints_lines:
                    endpoints_lines = [status_info.get("endpoint", "N/A")]

                for line in endpoints_lines:
                    self.logger.info(
                        f"  {adapter.name:15} ({adapter_type:15}) {line:25} {status}"
                    )

        # Show unified port adapters
        if port_unified_adapters:
            self.logger.info("Unified Port Adapters:")
            for adapter in port_unified_adapters:
                status_info = adapter.get_status_info()
                status = "🟢 Active" if adapter.is_running else "🔴 Stopped"
                ports = status_info.get("ports", "N/A")
                self.logger.info(f"  {adapter.name:15} ({status_info['type']:15}) " f"{ports:15} {status}")

        # Show all available plugins
        if hasattr(self, "unified_adapter_factory") and self.unified_adapter_factory:
            all_plugins = self.unified_adapter_factory.registry.get_all_plugins()
            if all_plugins:
                self.logger.info("Available Plugin Types:")
                for plugin in all_plugins:
                    self.logger.info(f"  🔌 {plugin.name:20} (config: {plugin.config_section})")

        self.logger.info("=" * 60)

    async def _monitor_shutdown_event(self):
        """Monitor the shutdown event and trigger shutdown when set."""
        if self.shutdown_event is None:
            return

        await self.shutdown_event.wait()
        self.logger.info("Shutdown event triggered")
        await self.shutdown()

    async def reload_config(self):  # noqa: Vulture (administrative API placeholder)
        """Reload configuration and restart adapters if needed.

        Returns:
            bool: True on successful reload; False otherwise.
        """
        self.logger.info("Reloading configuration...")

        try:
            # Load new configuration
            self._reload_config_from_disk()

            # Update core components
            await self.auth_manager.update_config(self.config_manager.get_authentication_config())
            # Config reload for unified adapters would require adapter restart
            # For now, just log that reload is not fully supported with unified adapters
            self.logger.info("Configuration reloaded successfully")
            self.logger.warning("Note: Adapter restart required for full config reload with unified adapters")

            return True

        except Exception as e:
            self.logger.error(f"Error reloading configuration: {e}", exc_info=True)
            return False

    async def reload_ports(self, partial: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:  # noqa: Vulture
        """Hot-reload port configurations without disconnecting unaffected users.

        Args:
            partial: Optional dict containing sections to update, e.g.,
                     {"serial_ports": [...]}.

        Returns:
            Summary mapping per adapter: { "serial": {added, removed, updated, unchanged} }.

        Notes:
            - Validates incoming structures using adapter validate_config before applying.
            - Only adapters with a 'reconcile_ports' method support online updates.
        """
        summary: Dict[str, Any] = {}
        try:
            # Determine target sections
            sections = partial or {}
            # Fallback to current config if partial missing
            cfg = getattr(self.config_manager, "config", None) or {}
            # Find unified adapters
            adapters = getattr(self, "unified_adapters", []) or []
            for a in adapters:
                atype = None
                try:
                    atype = a.get_adapter_type() if hasattr(a, "get_adapter_type") else getattr(a, "adapter_type", None)
                except Exception:
                    atype = getattr(a, "adapter_type", None)
                key = (atype or a.__class__.__name__).lower() if isinstance(atype, str) else str(atype)
                if not key:
                    continue
                # Serial adapter
                if key == "serial" and hasattr(a, "reconcile_ports"):
                    new_list = sections.get("serial_ports") if sections else cfg.get("serial_ports")
                    if new_list is None:
                        continue
                    # Validate shape
                    try:
                        validate_fn = getattr(a.__class__, "validate_config", None)
                        if callable(validate_fn) and not validate_fn({"serial_ports": new_list}):
                            self.logger.error("reload_ports: invalid serial_ports config; skipping")
                            continue
                    except Exception:
                        pass
                    try:
                        res = await a.reconcile_ports({"serial_ports": new_list})
                        summary["serial"] = res
                    except Exception as e:
                        self.logger.error(f"Serial reconcile failed: {e}", exc_info=True)
                        summary["serial"] = {"error": str(e)}
            return summary
        except Exception as e:
            self.logger.error(f"reload_ports failed: {e}", exc_info=True)
            return {"error": str(e)}

    async def reload_adapters_soft(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Perform a soft reload similar to the Web Console's soft reload.

        - Reload YAML from disk using ConfigManager
        - Update AuthManager configuration live
        - Reconcile ports for adapters that support online updates (serial, loopback, command, tcp initiator)
        - Do NOT restart connection endpoints (web console, client listener, muxcon)

        Returns a summary dict mirroring the web plugin for consistency.
        """
        req_id = (context or {}).get("req_id") or "sig"
        try:
            self.logger.info(f"[reload-soft:{req_id}] Initiating soft reload (origin={ (context or {}).get('origin', 'unknown') })")
        except Exception:
            pass

        summary: Dict[str, Any] = {"auth_updated": False, "adapters": {}}
        # Reload config
        try:
            cfg_path = getattr(self.config_manager, "config_path", None)
            self.logger.info(f"[reload-soft:{req_id}] Loading config from {cfg_path}")
            import time as _t
            _t0 = _t.time()
            new_cfg = self._reload_config_from_disk()
            self.logger.info(f"[reload-soft:{req_id}] Config loaded in {_t.time()-_t0:.3f}s")
        except Exception as e:
            self.logger.error(f"[reload-soft:{req_id}] Config load failed: {e}", exc_info=True)
            return {"error": str(e)}

        # Update AuthManager live
        try:
            if hasattr(self.auth_manager, "update_config"):
                await self.auth_manager.update_config(new_cfg.get("authentication", {}))
                summary["auth_updated"] = True
                self.logger.info(f"[reload-soft:{req_id}] AuthManager updated")
        except Exception as e:
            self.logger.error(f"[reload-soft:{req_id}] Auth update failed: {e}", exc_info=True)

        # Reconcile adapters that support in-place updates
        serial_section = new_cfg.get("serial_ports")
        loopback_section = new_cfg.get("loopback_ports")
        command_section = new_cfg.get("command_ports")
        tcp_init_section = new_cfg.get("tcp_initiator_ports") or new_cfg.get("openmux_client_ports")

        adapters = list(getattr(self, "unified_adapters", []) or [])
        for a in adapters:
            try:
                atype = None
                try:
                    atype = a.get_adapter_type() if hasattr(a, "get_adapter_type") else getattr(a, "adapter_type", None)
                except Exception:
                    atype = getattr(a, "adapter_type", None)
                key = (str(atype) if atype else "").lower()
                # Serial
                if key == "serial" and hasattr(a, "reconcile_ports") and serial_section is not None:
                    try:
                        res = await a.reconcile_ports(serial_section)
                        summary["adapters"].setdefault("serial", res)
                    except Exception as e:
                        self.logger.error(f"[reload-soft:{req_id}] Serial reconcile error: {e}", exc_info=True)
                        summary["adapters"]["serial"] = {"error": str(e)}
                # Loopback
                if key == "loopback" and hasattr(a, "reconcile_ports") and loopback_section is not None:
                    try:
                        res = await a.reconcile_ports(loopback_section)
                        summary["adapters"].setdefault("loopback", res)
                    except Exception as e:
                        self.logger.error(f"[reload-soft:{req_id}] Loopback reconcile error: {e}", exc_info=True)
                        summary["adapters"]["loopback"] = {"error": str(e)}
                # Command
                if key == "command" and hasattr(a, "reconcile_ports") and command_section is not None:
                    try:
                        res = await a.reconcile_ports(command_section)
                        summary["adapters"].setdefault("command", res)
                    except Exception as e:
                        self.logger.error(f"[reload-soft:{req_id}] Command reconcile error: {e}", exc_info=True)
                        summary["adapters"]["command"] = {"error": str(e)}
                # TCP initiator
                if key == "tcp_initiator" and hasattr(a, "reconcile_ports") and tcp_init_section is not None:
                    try:
                        res = await a.reconcile_ports(tcp_init_section)
                        summary["adapters"].setdefault("tcp_initiator", res)
                    except Exception as e:
                        self.logger.error(f"[reload-soft:{req_id}] TCP initiator reconcile error: {e}", exc_info=True)
                        summary["adapters"]["tcp_initiator"] = {"error": str(e)}
            except Exception:
                continue

        try:
            self.logger.info(f"[reload-soft:{req_id}] Completed with summary: {summary}")
        except Exception:
            pass
        return summary

    async def shutdown(self):
        """Gracefully shut down the server."""
        if not self.is_running:
            return

        self.logger.info("Shutting down OpenMux server...")
        self.is_running = False

        # Legacy connection adapters removed

        # Stop all unified adapters
        for adapter in self.unified_adapters:
            try:
                adapter_type = getattr(adapter, "adapter_type", adapter.__class__.__name__)
                self.logger.info(f"Stopping unified adapter {adapter.name} ({adapter_type})...")
                await adapter.stop()
                self.logger.info(f"Stopped unified adapter {adapter.name}")
            except Exception as e:
                self.logger.error(f"Error stopping unified adapter {adapter.name}: {e}", exc_info=True)

        # Legacy port close removed (unified-only)

        self.logger.info("Server shutdown complete")
        # Stop control socket and remove file
        try:
            await self._stop_control_socket()
        except Exception:
            pass

    async def reload_adapters_full(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Fully reload unified adapters from the current config.

        Stops all running unified adapters, reloads configuration from disk,
        recreates adapter instances, rewires dependencies, and starts them.

        Returns:
            Summary dict with counts and any per-adapter errors.
        """
        summary: Dict[str, Any] = {
            "stopped": 0,
            "started": 0,
            "errors": [],
            "stopped_adapters": [],  # list[dict]: {name, type}
            "created_adapters": [],  # list[dict]: {name, type}
            "started_adapters": [],  # list[dict]: {name, type}
            "web_console_restart_deferred": False,
        }
        try:
            # Correlation and origin context for logs
            import time as _time
            ctx = context or {}
            req_id = str(ctx.get("req_id") or "srv")
            origin = str(ctx.get("origin") or "unknown")
            remote = str(ctx.get("remote") or "?")
            user = str(ctx.get("user") or "?")

            # Phase timeouts (seconds)
            STOP_TIMEOUT_S = 10.0
            START_TIMEOUT_S = 10.0

            # Stop existing adapters
            old = list(self.unified_adapters or [])
            deferred_old_wc = None
            deferred_new_wc = None
            try:
                targets = []
                for a in old:
                    atype = getattr(a, "adapter_type", a.__class__.__name__)
                    targets.append(f"{getattr(a, 'name', '?')}({atype})")
                    self.logger.debug(f"[reload-full:{req_id}] Stop target: {getattr(a, 'name', '?')} ({atype})")
                self.logger.info(
                    f"[reload-full:{req_id}] Initiating STOP phase by {origin} user={user} remote={remote}; targets={len(targets)}: {', '.join(targets)}"
                )
            except Exception:
                pass
            for adapter in old:
                try:
                    aname = getattr(adapter, "name", "?")
                    atype = getattr(adapter, "adapter_type", adapter.__class__.__name__)
                    # Avoid self-stop deadlock: if the reload was triggered from the web console itself,
                    # defer stopping that web console instance until after we return the HTTP response.
                    atype_l = str(atype).lower()
                    if origin == "config-editor" and str(ctx.get("web_adapter_name") or "") == aname and atype_l in ("webconsole", "web_console", "web-console"):
                        self.logger.warning(f"[reload-full:{req_id}] Deferring stop of self-hosted WebConsole '{aname}' to avoid in-request shutdown")
                        deferred_old_wc = adapter
                        # Do not count as stopped here; will stop later
                        continue
                    self.logger.info(f"[reload-full:{req_id}] Stopping {aname} ({atype}) ...")
                    _t0 = _time.monotonic()
                    # Ensure a hung adapter.stop() won't block the reload forever
                    try:
                        await asyncio.wait_for(adapter.stop(), timeout=STOP_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        self.logger.error(
                            f"[reload-full:{req_id}] Timeout stopping {aname} ({atype}) after {STOP_TIMEOUT_S:.1f}s; continuing",
                            exc_info=False,
                        )
                        summary["errors"].append({
                            "adapter": aname,
                            "stop_timeout": STOP_TIMEOUT_S,
                        })
                    else:
                        self.logger.info(
                            f"[reload-full:{req_id}] Stopped {aname} ({atype}) in {_time.monotonic()-_t0:.3f}s"
                        )
                    summary["stopped"] += 1
                    try:
                        summary["stopped_adapters"].append({"name": aname, "type": atype})
                    except Exception:
                        pass
                except Exception as e:
                    self.logger.error(f"Error stopping adapter {adapter.name}: {e}", exc_info=True)
                    summary["errors"].append({"adapter": getattr(adapter, "name", "?"), "stop_error": str(e)})
            try:
                self.logger.info(f"[reload-full:{req_id}] Stop phase complete: {summary['stopped']} adapters processed")
            except Exception:
                pass
            # Clear adapter list and detach from port manager
            self.unified_adapters = []
            try:
                self.port_manager.set_unified_adapters([])
            except Exception:
                pass

            # Reload configuration from disk
            try:
                cfg_path = getattr(self.config_manager, "config_path", None)
                self.logger.info(f"[reload-full:{req_id}] Loading config from {cfg_path}")
                import time as _t
                _t0 = _t.time()
                self._reload_config_from_disk()
                self.logger.info(f"[reload-full:{req_id}] Config loaded in {_t.time()-_t0:.3f}s")
            except Exception as e:
                self.logger.error(f"Full reload: config load failed: {e}")
                summary["errors"].append({"phase": "load_config", "error": str(e)})
                return summary

            full_config = self.config_manager.config or {}

            # Recreate adapters
            try:
                self.unified_adapters = self.unified_adapter_factory.create_adapters_from_config(full_config)
                try:
                    self.logger.info(f"[reload-full:{req_id}] Created {len(self.unified_adapters)} unified adapters")
                    for a in self.unified_adapters:
                        atype = getattr(a, "adapter_type", a.__class__.__name__)
                        summary["created_adapters"].append({"name": getattr(a, "name", "?"), "type": atype})
                        self.logger.debug(f"[reload-full:{req_id}] Created: {getattr(a, 'name', '?')} ({atype})")
                        try:
                            if str(getattr(a, "adapter_type", "")).lower() in ("webconsole", "web_console", "web-console"):
                                deferred_new_wc = a
                        except Exception:
                            pass
                except Exception:
                    pass
            except Exception as e:
                self.logger.error(f"Full reload: adapter creation failed: {e}", exc_info=True)
                summary["errors"].append({"phase": "create_adapters", "error": str(e)})
                self.unified_adapters = []
                return summary

            # Reattach to port manager
            try:
                self.port_manager.set_unified_adapters(self.unified_adapters)
            except Exception:
                pass

            # Wire dependencies
            try:
                self.logger.info(f"[reload-full:{req_id}] Wiring adapter dependencies")
            except Exception:
                pass
            for adapter in self.unified_adapters:
                try:
                    if hasattr(adapter, "main_port_manager"):
                        adapter.main_port_manager = self.port_manager
                    set_auth = getattr(adapter, "set_auth_manager", None)
                    if callable(set_auth):
                        set_auth(self.auth_manager)
                    set_console = getattr(adapter, "set_console_manager", None)
                    if callable(set_console):
                        set_console(self.console_manager)
                    try:
                        atype = getattr(adapter, "adapter_type", adapter.__class__.__name__)
                        self.logger.debug(f"[reload-full:{req_id}] Wired dependencies for {getattr(adapter, 'name', '?')} ({atype})")
                    except Exception:
                        pass
                except Exception as e:
                    self.logger.error(f"Full reload: dependency wiring failed for {getattr(adapter, 'name', '?')}: {e}", exc_info=True)
                    summary["errors"].append({"adapter": getattr(adapter, "name", "?"), "wire_error": str(e)})

            # Start adapters
            try:
                self.logger.info(f"[reload-full:{req_id}] Starting {len(self.unified_adapters)} adapters")
            except Exception:
                pass
            for adapter in self.unified_adapters:
                try:
                    aname = getattr(adapter, "name", "?")
                    atype = getattr(adapter, "adapter_type", adapter.__class__.__name__)
                    # If we deferred stopping the current WebConsole, also defer starting the new WebConsole
                    if deferred_old_wc is not None and adapter is deferred_new_wc:
                        summary["web_console_restart_deferred"] = True
                        self.logger.warning(f"[reload-full:{req_id}] Deferring start of new WebConsole '{aname}' until after response")
                        continue
                    self.logger.info(f"[reload-full:{req_id}] Starting {aname} ({atype}) ...")
                    _s0 = _time.monotonic()
                    try:
                        ok = await asyncio.wait_for(adapter.start(), timeout=START_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        self.logger.error(
                            f"[reload-full:{req_id}] Timeout starting {aname} ({atype}) after {START_TIMEOUT_S:.1f}s",
                            exc_info=False,
                        )
                        summary["errors"].append({
                            "adapter": aname,
                            "start_timeout": START_TIMEOUT_S,
                        })
                        ok = False
                    if ok:
                        summary["started"] += 1
                        try:
                            entry = {"name": getattr(adapter, "name", "?"), "type": atype}
                            # Try to include a small endpoint hint, if available
                            try:
                                si = adapter.get_status_info()
                                det = (si or {}).get("details", {}) or {}
                                if isinstance(det, dict):
                                    if "endpoint" in si:
                                        entry["endpoint"] = si.get("endpoint")
                                    elif "host" in det and "port" in det:
                                        entry["endpoint"] = f"{det.get('host')}:{det.get('port')}"
                            except Exception:
                                pass
                            summary["started_adapters"].append(entry)
                            self.logger.info(
                                f"[reload-full:{req_id}] Started {entry.get('name')} ({entry.get('type')}) {entry.get('endpoint','')} in {_time.monotonic()-_s0:.3f}s"
                            )
                        except Exception:
                            pass
                    else:
                        summary["errors"].append({"adapter": getattr(adapter, "name", "?"), "start_error": "start returned False"})
                except Exception as e:
                    self.logger.error(f"Full reload: start failed for {getattr(adapter, 'name', '?')}: {e}", exc_info=True)
                    summary["errors"].append({"adapter": getattr(adapter, "name", "?"), "start_error": str(e)})

            try:
                self.logger.info(
                    f"[reload-full:{req_id}] Reload complete: stopped={summary['stopped']} created={len(summary['created_adapters'])} started={summary['started']} errors={len(summary['errors'])}"
                )
            except Exception:
                pass

            # If we deferred self WebConsole restart, schedule it in the background now
            if summary.get("web_console_restart_deferred") and deferred_old_wc is not None and deferred_new_wc is not None:
                try:
                    asyncio.create_task(self._deferred_restart_web_console(deferred_old_wc, deferred_new_wc, req_id))
                    self.logger.info(f"[reload-full:{req_id}] Scheduled deferred WebConsole restart task")
                except Exception as e:
                    self.logger.error(f"[reload-full:{req_id}] Scheduling deferred WebConsole restart failed: {e}")

            return summary
        except Exception as e:
            self.logger.error(f"reload_adapters_full failed: {e}", exc_info=True)
            return {"error": str(e)}

    async def _deferred_restart_web_console(self, old_adapter, new_adapter, req_id: str):
        """Stop the current WebConsole and start the newly created one after the HTTP response returns.

        This avoids self-stop deadlocks when full reload is triggered from within the WebConsole.
        """
        STOP_TIMEOUT_S = 10.0
        START_TIMEOUT_S = 10.0
        import time as _time
        an_old = getattr(old_adapter, "name", "?")
        an_new = getattr(new_adapter, "name", "?")
        try:
            self.logger.info(f"[reload-full:{req_id}] [deferred] Stopping WebConsole '{an_old}' ...")
            _t0 = _time.monotonic()
            try:
                await asyncio.wait_for(old_adapter.stop(), timeout=STOP_TIMEOUT_S)
                self.logger.info(f"[reload-full:{req_id}] [deferred] Stopped WebConsole '{an_old}' in {_time.monotonic()-_t0:.3f}s")
            except asyncio.TimeoutError:
                self.logger.error(f"[reload-full:{req_id}] [deferred] Timeout stopping WebConsole '{an_old}' after {STOP_TIMEOUT_S:.1f}s")
        except Exception as e:
            self.logger.error(f"[reload-full:{req_id}] [deferred] Error stopping WebConsole '{an_old}': {e}", exc_info=True)

        # Start the new WebConsole
        try:
            self.logger.info(f"[reload-full:{req_id}] [deferred] Starting WebConsole '{an_new}' ...")
            _s0 = _time.monotonic()
            try:
                ok = await asyncio.wait_for(new_adapter.start(), timeout=START_TIMEOUT_S)
            except asyncio.TimeoutError:
                ok = False
                self.logger.error(f"[reload-full:{req_id}] [deferred] Timeout starting WebConsole '{an_new}' after {START_TIMEOUT_S:.1f}s")
            if ok:
                self.logger.info(f"[reload-full:{req_id}] [deferred] Started WebConsole '{an_new}' in {_time.monotonic()-_s0:.3f}s")
            else:
                self.logger.error(f"[reload-full:{req_id}] [deferred] Failed to start WebConsole '{an_new}'")
        except Exception as e:
            self.logger.error(f"[reload-full:{req_id}] [deferred] Error starting WebConsole '{an_new}': {e}", exc_info=True)

    def get_server_status(self) -> Dict[str, Any]:
        """Get comprehensive server status.

        Returns:
            Dict[str, Any]: Aggregated status including adapters and ports.
        """
        status = {
            "adapters": {},
            "total_connections": 0,
            "ports": {},
            "summary": {
                "running_adapters": 0,
                "stopped_adapters": 0,
                "total_adapters": len(self.adapters),
            },
        }

        # Adapter status
        for name, adapter in self.adapters.items():
            adapter_info = adapter.get_server_info()
            connections = adapter.get_connection_count()

            status["adapters"][name] = {
                **adapter_info,
                "connections": connections,
                "active_clients": [
                    {
                        "client_id": client.client_id,
                        "address": client.address,
                        "username": client.username,
                        "protocol": client.protocol,
                        "connected_port": client.connected_port,
                        "mode": client.mode,
                    }
                    for client in adapter.get_active_connections()
                ],
            }

            status["total_connections"] += connections

            if adapter_info["running"]:
                status["summary"]["running_adapters"] += 1
            else:
                status["summary"]["stopped_adapters"] += 1

        return status


def _parse_arguments():
    """Parse command line arguments.

    Returns:
        argparse.Namespace: Parsed command-line options.
    """
    parser = argparse.ArgumentParser(description="OpenMux Serial Port Server")
    parser.add_argument(
        "-c",
        "--config",
        default=None,
        help="Path to the primary server configuration file (server.yaml)",
    )
    parser.add_argument(
        "--config-dir",
        help=(
            "Directory containing server.yaml, authentication.yaml, and security.yaml. "
            "Values derived from this directory are used only for files not explicitly set via"
            " --config/--auth-config/--security-config."
        ),
    )
    parser.add_argument(
        "-a",
        "--auth-config",
        help="Path to authentication configuration file (authentication.yaml)",
    )
    parser.add_argument(
        "-s",
        "--security-config",
        help="Path to security configuration file (security.yaml)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (-v=INFO, -vv or more=DEBUG)",
    )
    args = parser.parse_args()

    # When --config-dir is provided, derive any unspecified config paths from it
    if args.config_dir:
        base_dir = os.path.abspath(args.config_dir)
        if not args.config:
            args.config = os.path.join(base_dir, "server.yaml")
        if not args.auth_config:
            args.auth_config = os.path.join(base_dir, "authentication.yaml")
        if not args.security_config:
            args.security_config = os.path.join(base_dir, "security.yaml")

    # Preserve legacy default when nothing else was provided
    if not args.config:
        args.config = "/etc/openmux/server.yaml"

    return args


def _find_config_file(config_path: str) -> str:
    """Find and validate the configuration file path.

    Args:
        config_path: Preferred configuration path supplied by the user.

    Returns:
        str: Resolved configuration path.

    Notes:
        Exits the process with status 1 if no suitable config file is found.
    """
    if os.path.exists(config_path):
        return config_path

    # Check if the config file exists in the current directory
    local_config = os.path.join(os.path.dirname(__file__), "..", "..", "config", "server.yaml")
    if os.path.exists(local_config):
        return local_config
    else:
        logging.error(f"Config file not found: {config_path}")
        sys.exit(1)


def _setup_shutdown_handlers(loop, server):
    """Set up signal handlers and shutdown event.

    Args:
        loop: AsyncIO event loop to schedule shutdown on.
        server: OpenMuxServer instance to invoke shutdown for.
    """
    # Create shutdown event for graceful shutdown coordination
    shutdown_event = asyncio.Event()

    # Signal handler for graceful shutdown
    def handle_shutdown_signal():
        logging.info("Shutdown signal received")
        shutdown_event.set()
        asyncio.run_coroutine_threadsafe(shutdown_coroutine(), loop)

    async def shutdown_coroutine():
        try:
            # Run server shutdown with timeout
            await asyncio.wait_for(server.shutdown(), timeout=5.0)

            # Cancel remaining tasks
            tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task() and not t.done()]
            if tasks:
                logging.info(f"Cancelling {len(tasks)} remaining tasks")
                for task in tasks:
                    task.cancel()

                try:
                    await asyncio.wait(tasks, timeout=2.0)
                except asyncio.TimeoutError:
                    logging.warning("Some tasks did not cancel in time")

            # Stop the event loop
            loop.call_soon_threadsafe(loop.stop)

        except Exception as e:
            logging.error(f"Error during shutdown: {e}", exc_info=True)
            loop.call_soon_threadsafe(loop.stop)

    # Ensure control socket is closed promptly on TERM/INT
    async def close_control_socket():
        try:
            await server._stop_control_socket()
        except Exception:
            pass

    # Reload handler (SIGHUP): soft reload via server API and reconfigure logging
    def handle_reload_signal():
        logging.info("SIGHUP received: soft reload requested")
        asyncio.run_coroutine_threadsafe(soft_reload_coroutine(), loop)

    async def soft_reload_coroutine():
        try:
            # Reload config (for logging level and runtime settings)
            server._reload_config_from_disk()
            # Reconfigure logging level from config if provided
            try:
                cfg = getattr(server.config_manager, "config", {}) or {}
                lvl = (cfg.get("logging", {}) or {}).get("level")
                if isinstance(lvl, str) and lvl.strip():
                    _setup_basic_logging(level_name=lvl.strip().upper())
            except Exception:
                pass
            # Perform server soft reload
            ctx = {"origin": "signal", "user": "signal", "remote": "local", "req_id": "sighup"}
            res = await server.reload_adapters_soft(context=ctx)
            logging.info(f"Soft reload completed: {res}")
        except Exception as e:
            logging.error(f"Soft reload failed: {e}", exc_info=True)

    # Full reload handler (SIGUSR1)
    def handle_full_reload_signal():
        logging.info("SIGUSR1 received: full adapter reload requested")
        asyncio.run_coroutine_threadsafe(full_reload_coroutine(), loop)

    async def full_reload_coroutine():
        try:
            ctx = {"origin": "signal", "user": "signal", "remote": "local", "req_id": "sigusr1"}
            res = await server.reload_adapters_full(context=ctx)
            logging.info(f"Full reload completed: {res}")
        except Exception as e:
            logging.error(f"Full reload failed: {e}", exc_info=True)

    # Register signal handlers
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_shutdown_signal)
    # SIGHUP may not exist or be unsupported on some platforms; guard registration
    try:
        loop.add_signal_handler(signal.SIGHUP, handle_reload_signal)
    except Exception:
        logging.debug("SIGHUP not available; soft reload via signal disabled on this platform")
    try:
        loop.add_signal_handler(signal.SIGUSR1, handle_full_reload_signal)
    except Exception:
        logging.debug("SIGUSR1 not available; full reload via signal disabled on this platform")

    # Pass shutdown event to server
    server.shutdown_event = shutdown_event


def _cleanup_event_loop(loop):
    """Clean up event loop and cancel remaining tasks.

    Args:
        loop: Event loop to inspect and cancel tasks for.
    """
    if loop:
        # Cancel all remaining tasks
        for task in asyncio.all_tasks(loop):
            if not task.done():
                task.cancel()


def _setup_basic_logging(level_name: Optional[str] = None):
    """Set up basic logging configuration with flexible levels.

    - Level precedence: explicit arg > default WARNING
    - Handlers are set to NOTSET so they never filter; root logger controls level
    - Applies chosen level to all existing `openmux.*` loggers and their handlers
    """
    already_configured = getattr(_setup_basic_logging, "_configured", False)

    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger()
    # Resolve desired level name (may be re-invoked to adjust level)
    resolved_name = level_name or "WARNING"
    log_level = getattr(logging, str(resolved_name).upper(), logging.INFO)

    if not root.handlers and not already_configured:
        root.setLevel(log_level)

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.NOTSET)
        console_format = logging.Formatter(
            "%(asctime)s.%(msecs)03d %(name)s %(filename)s:%(lineno)d %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console_handler.setFormatter(console_format)
        root.addHandler(console_handler)

        # Main log file
        from logging.handlers import RotatingFileHandler

        main_file = os.path.join(log_dir, "openmux.log")
        file_handler = RotatingFileHandler(main_file, maxBytes=10 * 1024 * 1024, backupCount=5)
        file_format = logging.Formatter(
            "%(asctime)s.%(msecs)03d %(filename)s:%(lineno)d %(name)s %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(file_format)
        file_handler.setLevel(logging.NOTSET)
        root.addHandler(file_handler)

        # Component specific loggers (server, client, serial, auth, config, console)
        components = ["server", "client", "serial", "auth", "config", "console"]
        for comp in components:
            logger = logging.getLogger(f"openmux.{comp}")
            comp_file = os.path.join(log_dir, f"openmux_{comp}.log")
            comp_handler = RotatingFileHandler(comp_file, maxBytes=10 * 1024 * 1024, backupCount=5)
            comp_handler.setFormatter(file_format)
            comp_handler.setLevel(logging.NOTSET)
            logger.addHandler(comp_handler)

    # Update root and existing component loggers to the desired level
    try:
        root.setLevel(log_level)
        for name in list(logging.root.manager.loggerDict.keys()):
            if isinstance(name, str) and (name == "openmux" or name.startswith("openmux.")):
                lg = logging.getLogger(name)
                lg.setLevel(log_level)
                lg.propagate = True
                for h in list(lg.handlers):
                    h.setLevel(logging.NOTSET)
    except Exception:
        pass

    try:
        setattr(_setup_basic_logging, "_configured", True)
    except Exception:  # justification: best-effort idempotence flag; safe to ignore failure
        pass


def main():
    """Main entry point for the OpenMux server."""
    args = _parse_arguments()

    # Find and validate config file
    config_path = _find_config_file(args.config)
    auth_config = args.auth_config
    security_config = args.security_config

    # Determine initial log level from CLI or config.logging.level
    cli_level = None
    if args.verbose >= 2:
        cli_level = "DEBUG"
    elif args.verbose == 1:
        cli_level = "INFO"

    config_level = None
    try:
        cm = ConfigManager(
            config_path,
            auth_config_path=auth_config,
            security_config_path=security_config,
        )
        cfg = cm.load_config()
        log_cfg = (cfg or {}).get("logging", {})
        lvl = log_cfg.get("level")
        if isinstance(lvl, str) and lvl.strip():
            config_level = lvl.strip().upper()
    except Exception:
        config_level = None

    initial_level = cli_level or config_level or "WARNING"
    server = OpenMuxServer(
        config_path,
        auth_config_path=auth_config,
        security_config_path=security_config,
        log_level=initial_level,
    )
    loop = None

    try:
        # Create event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Set up shutdown handlers
        _setup_shutdown_handlers(loop, server)

        # Write PID file for CLI/signal control (default logs/openmux.pid; override with OPENMUX_PIDFILE)
        try:
            pidfile = os.environ.get("OPENMUX_PIDFILE")
            if not pidfile:
                # Try config runtime.pidfile, else fall back to logs/openmux.pid
                try:
                    cm2 = ConfigManager(
                        config_path,
                        auth_config_path=auth_config,
                        security_config_path=security_config,
                    )
                    cfg2 = cm2.load_config() or {}
                    # Preferred location: server.pidfile
                    server_cfg = (cfg2 or {}).get("server", {}) or {}
                    pidfile = server_cfg.get("pidfile")
                    if not pidfile:
                        # Back-compat (deprecated): runtime.pidfile
                        runtime_cfg = (cfg2 or {}).get("runtime", {}) or {}
                        pidfile = runtime_cfg.get("pidfile")
                        if pidfile:
                            logging.warning(
                                "Using deprecated runtime.pidfile; please move to server.pidfile"
                            )
                except Exception:
                    pidfile = None
            if not pidfile:
                pidfile = os.path.join("logs", "openmux.pid")
            # Ensure directory exists
            os.makedirs(os.path.dirname(pidfile), exist_ok=True)
            with open(pidfile, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            logging.info(f"PID file written: {pidfile}")
        except Exception as e:
            logging.warning(f"Failed to write PID file: {e}")

        # Start the server; exit with non-zero if startup failed
        started = loop.run_until_complete(server.start())
        if not started:
            # server.start() already logged detailed reason (including adapter fail-fast)
            sys.exit(2)

    except KeyboardInterrupt:
        logging.info("Server stopped by user")
    except Exception as e:
        logging.error(f"Unexpected error: {e}", exc_info=True)
    finally:
        # Clean up
        _cleanup_event_loop(loop)
        logging.info("Server shutdown complete")
        logging.shutdown()

        # Remove PID file (best-effort)
        try:
            # Try env first
            pidfile = os.environ.get("OPENMUX_PIDFILE")
            if not pidfile:
                # Try to mirror the same resolution as during write, but best-effort only
                try:
                    parsed_args = _parse_arguments()
                    cm3 = ConfigManager(
                        _find_config_file(parsed_args.config),
                        auth_config_path=parsed_args.auth_config,
                        security_config_path=parsed_args.security_config,
                    )
                    cfg3 = cm3.load_config() or {}
                    pidfile = ((cfg3.get("server", {}) or {}).get("pidfile")
                               or (cfg3.get("runtime", {}) or {}).get("pidfile"))
                except Exception:
                    pidfile = None
            if not pidfile:
                pidfile = os.path.join("logs", "openmux.pid")
            if os.path.exists(pidfile):
                os.remove(pidfile)
        except Exception:
            pass


if __name__ == "__main__":
    main()
