#!/usr/bin/env python3
"""
OpenMux Client - Main entry point
"""
import argparse
import asyncio
import getpass
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from .adapters import BaseClientAdapter, ClientAdapterFactory
from .console import ConsoleUI
from .logging_manager import ClientLoggingManager, print_client_info


class OpenMuxClient:
    """Main client class for OpenMux"""

    def __init__(self, config_path: Optional[str] = None):
        """Initialize an OpenMux client instance.

        Args:
            config_path: Optional path to a YAML configuration file. If present
                and readable it may define:
                * servers: list[dict] with host/port entries
                * default_server: identifier (currently not enforced)
                * use_tls: default TLS flag
                * logging: logging configuration block

        Side Effects:
            Loads configuration, creates logging manager, initializes connection map & cache.
        """
        # Setup basic logging first
        self.logger = logging.getLogger("openmux.client")

        # Load configuration
        self.config_path = config_path
        self.config = self._load_config()

        # Setup full logging manager with config
        self.logging_manager = ClientLoggingManager(self.config.get("logging", {}))

        # Initialize connection manager
        self.connections = {}

    # Client-side cache removed: always fetch live from server

    def _discover_default_config_path(self) -> Optional[str]:
        """Locate a default client config file using common search paths.

        Search order (first hit wins):
            1. Environment `OPENMUX_CLIENT_CONFIG`
            2. Current directory: `client.(yaml|yml|json)`, `openmux_client.*`, `openmux-client.*`, and dotfile variants
            3. XDG config: `$XDG_CONFIG_HOME/openmux/client.*` or `~/.config/openmux/client.*`
            4. macOS: `~/Library/Application Support/OpenMux/client.*`
            5. Home dotfiles: `~/.openmux_client.*`, `~/.openmux-client.*`, `~/.openmux/client.*`
            6. System: `/etc/openmux/client.*`, `/usr/local/etc/openmux/client.*`

        Returns:
            Path string if found and readable else None.
        """
        env_path = os.environ.get("OPENMUX_CLIENT_CONFIG")
        if env_path and os.path.exists(env_path):
            return env_path

        def candidates_in_dir(d: str, names: List[str]) -> List[str]:
            paths: List[str] = []
            for base in names:
                for ext in ("yaml", "yml", "json"):
                    paths.append(os.path.join(d, f"{base}.{ext}"))
            return paths

        names = [
            "client",
            "openmux_client",
            "openmux-client",
            ".client",
            ".openmux_client",
            ".openmux-client",
        ]

        # 2) Current directory
        for p in candidates_in_dir(os.getcwd(), names):
            if os.path.exists(p):
                return p

        # 3) XDG config
        xdg_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
        xdg_dir = os.path.join(xdg_home, "openmux")
        for p in candidates_in_dir(xdg_dir, ["client"]):
            if os.path.exists(p):
                return p

        # 4) macOS Application Support
        mac_dir = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "OpenMux")
        for p in candidates_in_dir(mac_dir, ["client"]):
            if os.path.exists(p):
                return p

        # 5) Home dotfiles
        home = os.path.expanduser("~")
        for p in candidates_in_dir(home, [".openmux_client", ".openmux-client"]):
            if os.path.exists(p):
                return p
        # Nested folder in home
        for p in candidates_in_dir(os.path.join(home, ".openmux"), ["client"]):
            if os.path.exists(p):
                return p

        # 6) System locations
        for sysdir in ("/etc/openmux", "/usr/local/etc/openmux"):
            for p in candidates_in_dir(sysdir, ["client"]):
                if os.path.exists(p):
                    return p

        return None

    def _read_config_file(self, path: str) -> Dict[str, Any]:
        """Read a YAML or JSON config file safely.

        Args:
            path: File path with extension .yaml/.yml/.json

        Returns:
            Parsed dict or empty dict on error.
        """
        try:
            _, ext = os.path.splitext(path.lower())
            if ext in (".yaml", ".yml"):
                try:
                    import yaml  # type: ignore
                except Exception:
                    self.logger.error("PyYAML not installed; cannot read YAML config")
                    return {}
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                    if isinstance(data, dict):
                        return data
                    return {}
            if ext == ".json":
                import json

                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
                    return {}
            # Unknown extension; try YAML then JSON best-effort
            try:
                import yaml  # type: ignore

                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                    return data if isinstance(data, dict) else {}
            except Exception:
                pass
            try:
                import json

                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, dict) else {}
            except Exception:
                return {}
        except Exception as e:
            self.logger.error(f"Error reading config file {path}: {e}", exc_info=True)
            return {}

    def _load_config(self):
        """Load client configuration from disk if a path was supplied.

        Merge Rules:
            * Start with defaults.
            * If file exists and parses, shallow‑merge keys.
            * Keep defaults for any missing keys.

        Returns:
            dict: Effective configuration (always contains servers, default_server, use_tls).

        Notes:
            Any load/parse error is logged and defaults are used.
        """
        config = {
            "servers": [],
            "default_server": None,
            # Unified TLS flag.
            "use_tls": False,
        }

        path = None
        # 1) CLI-specified path wins
        if self.config_path:
            path = self.config_path if os.path.exists(self.config_path) else None
            if not path:
                self.logger.error(f"Config path not found: {self.config_path}")
        # 2) Env / default discovery
        if not path:
            path = self._discover_default_config_path()

        if path:
            loaded_config = self._read_config_file(path)
            if loaded_config:
                config.update(loaded_config)
                self.logger.info(f"Loaded client configuration from {path}")

        return config

    def resolve_default_server(self, server_arg: Optional[str], port_arg: Optional[int]) -> Optional[Tuple[str, int]]:
        """Resolve server host/port from CLI args or config defaults.

        Resolution:
            - If `server_arg` provided: return `(server_arg, port_arg or 8023)`
            - Else if config.default_server is set: match by `name` or `host` in `servers` list
            - Else take first entry in `servers`

        Returns:
            (host, port) tuple or None if no candidates.
        """
        if server_arg:
            return server_arg, int(port_arg or 8023)

        servers: List[Dict[str, Any]] = self.config.get("servers", []) or []
        if not servers:
            return None

        default_id = self.config.get("default_server")
        chosen = None
        if default_id:
            # Match by name or host
            for s in servers:
                if s.get("name") == default_id or s.get("host") == default_id:
                    chosen = s
                    break
        if not chosen:
            chosen = servers[0]

        host = chosen.get("host")
        port = int(chosen.get("port", port_arg or 8023))
        return (host, port) if host else None

    def _get_server_entry(self, ident: Optional[str]) -> Optional[Dict[str, Any]]:
        """Return the server entry matching by name or host.

        Args:
            ident: Name or host string to match.

        Returns:
            Matching server dict or None.
        """
        if not ident:
            return None
        servers: List[Dict[str, Any]] = self.config.get("servers", []) or []
        for s in servers:
            if s.get("name") == ident or s.get("host") == ident:
                return s
        return None

    def get_credentials_for(self, host_or_name: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Lookup credentials for a server by host or name.

        Args:
            host_or_name: Host or name used to find the server entry.

        Returns:
            (username, password, api_key) tuple; elements may be None.
        """
        s = self._get_server_entry(host_or_name)
        if not s:
            return None, None, None
        return s.get("username"), s.get("password"), s.get("api_key")

    def get_pubkey_for(self, host_or_name: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        """Lookup pubkey auth parameters for a server if present.

        Supports per-server entries:
            pubkey_path: path to private key
            pubkey_id: key identifier
        """
        s = self._get_server_entry(host_or_name)
        if not s:
            return None, None
        return s.get("pubkey_path"), s.get("pubkey_id")

    async def connect_to_server(
        self,
        host: str,
        port: int,
        username: Optional[str] = None,
        password: Optional[str] = None,
        api_key: Optional[str] = None,
        pubkey_path: Optional[str] = None,
        pubkey_id: Optional[str] = None,
        use_tls: Optional[bool] = None,
        adapter_type: str = "tcp",
        port_name: Optional[str] = None,
        ws_basic_user: Optional[str] = None,
        ws_basic_password: Optional[str] = None,
    ):
        """Create, connect, and authenticate a client adapter to a server.

        Auth Order:
            1. API key (if provided)
            2. Username/password (if both provided)
            3. Interactive username + password prompts

        Args:
            host: Server hostname or IP.
            port: TCP port.
            username: Username for password auth (optional).
            password: Password for auth (optional).
            api_key: API key for key-based auth (skips password).
            use_tls: Override TLS flag; falls back to config if None.
            adapter_type: Connection adapter type (default "tcp").

        Additional Args (websocket adapter only):
            port_name: Target server port name (establishes immediate data channel). If omitted, a discovery-only
                connection is created allowing --list to function without choosing a port first.
            ws_basic_user/ws_basic_password: Basic Auth credentials passed during WebSocket handshake and HTTP
                listing calls.

        Returns:
            BaseClientAdapter | None: Connected and (if applicable) authenticated adapter, else None.

        Notes:
            Caller must close the returned connection when finished.
        """
        # Determine TLS usage flag
        if use_tls is None:
            use_tls = self.config.get("use_tls", False)

        # Build adapter config explicitly (new primary pattern)
        from typing import Any as _Any  # local import to avoid top-level noise
        from typing import Dict as _Dict

        adapter_config: _Dict[str, _Any] = {"use_tls": bool(use_tls)}
        if adapter_type == "websocket":
            # Provide discovery/connection parameters for raw streaming WS adapter
            if port_name:
                adapter_config["port_name"] = port_name
            if ws_basic_user:
                adapter_config["basic_user"] = ws_basic_user
            if ws_basic_password:
                adapter_config["basic_password"] = ws_basic_password
            # Enable HTTP port listing by default
            adapter_config["list_ports_via_http"] = True

        try:
            connection = ClientAdapterFactory.create_adapter(
                host=host,
                port=port,
                adapter_type=adapter_type,
                config=adapter_config,
            )
        except Exception as e:
            self.logger.error(f"Failed to create adapter: {e}", exc_info=True)
            return None

        # Connect to server
        if not await connection.connect():
            self.logger.error(f"Failed to connect to {host}:{port}")
            return None

        # Authenticate (priority: api key > pubkey > username/password)
        if adapter_type == "websocket":
            # Websocket raw adapter authenticates during handshake (Basic Auth); skip explicit auth
            auth_success = True
        else:
            if api_key:
                auth_success = await connection.authenticate_with_key(api_key)
            elif pubkey_path and username and hasattr(connection, "authenticate_with_pubkey"):
                auth_success = await getattr(connection, "authenticate_with_pubkey")(username, pubkey_path, pubkey_id)
            elif username and password:
                auth_success = await connection.authenticate_with_password(username, password)
            else:
                if not username:
                    username = input("Username: ")
                if pubkey_path and hasattr(connection, "authenticate_with_pubkey"):
                    auth_success = await getattr(connection, "authenticate_with_pubkey")(username, pubkey_path, pubkey_id)
                else:
                    password = getpass.getpass("Password: ")
                    auth_success = await connection.authenticate_with_password(username, password)

        if not auth_success:
            self.logger.error("Authentication failed")
            await connection.close()
            return None

        # Store connection
        connection_id = f"{host}:{port}"
        self.connections[connection_id] = connection

        return connection

    async def list_ports(self, connection: BaseClientAdapter):
        """Retrieve port list live and display rich metadata when available.

        Prefers structured entries (dicts) from the adapter or falls back to
        adapter-provided metadata captured during LIST. If neither is present,
        prints plain port names.

        Args:
            connection: Active authenticated adapter.

        Returns:
            list: Raw entries or names as returned by the adapter.
        """
        ports = await connection.list_ports()
        if not ports:
            print_client_info("No ports available", "WARNING")
            return []

        # Determine if we have structured metadata to show
        meta_entries = None
        try:
            if ports and isinstance(ports[0], dict):
                meta_entries = ports
            else:
                meta_entries = getattr(connection, "last_port_metadata", None)
                if meta_entries and not isinstance(meta_entries, list):
                    meta_entries = None
        except Exception:
            meta_entries = None

        print_client_info(f"Ports on {connection.host}:{connection.port}:")
        if meta_entries:
            # Optional: detect duplicate names to hint users about composite ids
            try:
                name_counts = {}
                for entry in meta_entries:
                    nm = entry.get("name") or entry.get("port")
                    if nm:
                        name_counts[nm] = name_counts.get(nm, 0) + 1
                dup_names = {n for n, c in name_counts.items() if c > 1}
            except Exception:
                dup_names = set()
            for i, entry in enumerate(meta_entries):
                name = entry.get("name") or entry.get("port") or "<unknown>"
                comp_id = entry.get("id") or None
                desc = entry.get("description") or ""
                device = entry.get("device") or entry.get("phy_device") or None
                # Status/clients (support both legacy and unified fields)
                is_connected = bool(entry.get("connected")) or bool(entry.get("is_running"))
                status = "Connected" if is_connected else "Disconnected"
                clients = entry.get("client_count")
                if clients is None:
                    clients = entry.get("connected_clients")
                rw_cap = entry.get("max_read_write_users")

                # Federation info (muxcon origins)
                os_obj = entry.get("origin_server") if isinstance(entry.get("origin_server"), dict) else None
                origin_host = entry.get("origin_server_hostname") or (os_obj.get("hostname") if os_obj else None)
                origin_id = (
                    entry.get("origin_server_id")
                    or (os_obj.get("server_id") if os_obj else None)
                    or entry.get("origin_server")
                )
                origin_desc = os_obj.get("description") if os_obj else None
                origin_port = entry.get("origin_server_port") or (os_obj.get("port") if os_obj else None)
                # Fallbacks for federated remote ports
                if not origin_host or str(origin_host).lower() == "remote":
                    # Try remote_connection_id (peer key like node:<id> or host:<ip> or host:port)
                    rcid = entry.get("remote_connection_id")
                    if rcid:
                        origin_host = rcid
                        origin_port = None
                    else:
                        # Try server_chain first element (origin id)
                        sc = entry.get("server_chain")
                        if isinstance(sc, list) and sc:
                            origin_id = sc[0]
                ftype = entry.get("federation_type")

                label = name if not comp_id else f"{name}  ({comp_id})"
                parts = [f"  {i+1}. {label}"]
                if desc:
                    parts.append(f"- {desc}")
                details = []
                if device:
                    details.append(str(device))
                if status:
                    details.append(status)
                if clients is not None:
                    if rw_cap is not None:
                        details.append(f"{clients} clients/{rw_cap} rw")
                    else:
                        details.append(f"{clients} clients")
                # Compose origin in fixed order: [id=..., desc=..., hostname=...]
                if origin_host or origin_id or origin_desc:
                    origin_fields = []
                    if origin_id:
                        origin_fields.append(f"id={origin_id}")
                    if origin_desc:
                        origin_fields.append(f"desc={origin_desc}")
                    if origin_host:
                        origin_fields.append(f"hostname={origin_host}")
                    origin_label = f"[{', '.join(origin_fields)}]" if origin_fields else "[]"
                    if ftype:
                        details.append(f"origin={origin_label} ({ftype})")
                    else:
                        details.append(f"origin={origin_label}")
                if details:
                    parts.append(" - " + ", ".join(details))
                print_client_info(" ".join(parts))

            # If duplicates exist, provide a short hint with examples
            if dup_names:
                examples = []
                try:
                    for entry in meta_entries:
                        nm = entry.get("name") or entry.get("port")
                        cid = entry.get("id")
                        if nm in dup_names and cid:
                            examples.append(cid)
                        if len(examples) >= 3:
                            break
                except Exception:
                    pass
                if examples:
                    print_client_info(
                        "Duplicate port names detected. Use server_id::name to disambiguate, e.g.: "
                        + ", ".join(examples)
                    )
        else:
            # Fallback: plain names
            for i, port in enumerate(ports):
                print_client_info(f"  {i+1}. {port}")
        return ports

    # Removed cached multi-server listing; client always fetches live now

    async def connect_to_port(self, connection: BaseClientAdapter, port_name: str):
        """Attach an existing connection to a named server port.

        Args:
            connection: Active server connection.
            port_name: Target port name.

        Returns:
            bool: True if connection to port succeeded, else False.
        """
        if not await connection.connect_to_port(port_name):
            print_client_info(f"Failed to connect to {port_name}", "ERROR")
            return False

        return True

    # Removed cache lookup helper; lookup is performed live against servers

    async def _search_servers_for_console(
        self,
        console_name: str,
        username,
        password,
        api_key,
        pubkey_path,
        pubkey_id,
        use_tls,
    ):
        """Search configured servers sequentially for a console name.

        Args:
            console_name: Target console name.
            username: Optional username for auth.
            password: Optional password for auth.
            api_key: Optional API key.
            use_tls: TLS usage flag.

        Returns:
            dict | None: Server/console info on first match, else None.
        """
        for server in self.config.get("servers", []):
            print_client_info(
                f"Searching for '{console_name}' on " f"{server.get('host')}:{server.get('port')}...",
                "INFO",
            )

            # Use provided CLI creds if given; otherwise pull from per-server config
            resolved_username, resolved_password, resolved_api_key = username, password, api_key
            if resolved_username is None and resolved_password is None and resolved_api_key is None:
                cfg_username = server.get("username")
                cfg_password = server.get("password")
                cfg_api_key = server.get("api_key")
                resolved_username = cfg_username if cfg_username else resolved_username
                resolved_password = cfg_password if cfg_password else resolved_password
                resolved_api_key = cfg_api_key if cfg_api_key else resolved_api_key

            console_info = await self._check_server_for_console(
                server,
                console_name,
                resolved_username,
                resolved_password,
                resolved_api_key,
                pubkey_path,
                pubkey_id,
                use_tls,
            )

            if console_info:
                return console_info

        return None

    async def _check_server_for_console(
        self,
        server,
        console_name,
        username,
        password,
        api_key,
        pubkey_path,
        pubkey_id,
        use_tls,
    ):
        """Check one server for the named console.

        Args:
            server: Server config dict with host/port.
            console_name: Desired console name.
            username: Optional username.
            password: Optional password.
            api_key: Optional API key.
            use_tls: TLS flag.

        Returns:
            dict | None: {server, port, console} if found else None.

            pubkey_path,
            pubkey_id,
        Notes:
            Connection is always closed before returning.
        """
        connection = await self.connect_to_server(
            server.get("host"),
            server.get("port"),
            username,
            password,
            api_key,
            pubkey_path,
            pubkey_id,
            use_tls,
        )

        if not connection:
            return None

        try:
            # Get port list and check for console
            ports = await self.list_ports(connection)

            for port in ports:
                port_name = port.get("name") if isinstance(port, dict) else port
                if port_name == console_name:
                    return {
                        "server": server.get("host"),
                        "port": server.get("port"),
                        "console": port_name,
                    }
            return None
        finally:
            await connection.close()

    async def _connect_to_found_console(
        self,
        console_info,
        username,
        password,
        api_key,
        pubkey_path,
        pubkey_id,
        use_tls,
        reconnect_mode: str = "off",
        backoff_initial: float = 1.0,
        backoff_max: float = 30.0,
    ):
        """Connect to a found console and run interactive UI.

        Args:
            console_info: Dict produced by discovery phase.
            username: Optional username.
            password: Optional password.
            api_key: Optional API key.
            use_tls: TLS flag.
            pubkey_path,
            pubkey_id,
            reconnect_mode: Reconnect strategy (off/manual/auto).
            backoff_initial: Initial reconnect delay.
            backoff_max: Maximum reconnect delay.

        Returns:
            bool: Result of `ConsoleUI.run()`.
        """
        print_client_info(
            f"Connecting to {console_info['console']} on " f"{console_info['server']}:{console_info['port']}...",
            "INFO",
        )

        connection = await self.connect_to_server(
            console_info["server"],
            console_info["port"],
            username,
            password,
            api_key,
            pubkey_path,
            pubkey_id,
            use_tls,
        )

        if not connection:
            return False

        # Extract console name
        console_name = console_info["console"]
        if isinstance(console_info["console"], dict):
            console_name = console_info["console"]["name"]

        # Connect to the port
        if not await self.connect_to_port(connection, console_name):
            return False

        # Create and run console UI
        console = ConsoleUI(
            connection,
            reconnect_mode=reconnect_mode,
            backoff_initial=backoff_initial,
            backoff_max=backoff_max,
        )
        return await console.run()

    async def find_and_connect_by_name(
        self,
        console_name: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        api_key: Optional[str] = None,
        pubkey_path: Optional[str] = None,
        pubkey_id: Optional[str] = None,
        use_tls: Optional[bool] = None,
        reconnect_mode: str = "off",
        backoff_initial: float = 1.0,
        backoff_max: float = 30.0,
    ):
        """Locate a console (cache then servers) and connect.

        Args:
            console_name: Target console name.
            username: Optional username.
            password: Optional password.
            api_key: Optional API key.
            use_tls: TLS flag.
            reconnect_mode: Reconnect strategy.
            backoff_initial: Initial reconnect backoff.
            backoff_max: Maximum reconnect backoff.

        Returns:
            bool: Session success result, or False if not found/failure.
        """
        # Search configured servers live for the console
        console_info = await self._search_servers_for_console(
            console_name,
            username,
            password,
            api_key,
            pubkey_path,
            pubkey_id,
            use_tls,
        )

        # Connect to the found console
        if console_info:
            return await self._connect_to_found_console(
                console_info,
                username,
                password,
                api_key,
                pubkey_path,
                pubkey_id,
                use_tls,
                reconnect_mode,
                backoff_initial,
                backoff_max,
            )

        print_client_info(f"Console '{console_name}' not found on any known server", "ERROR")
        return False

    async def run_console(
        self,
        host: str,
        port: int,
        username: Optional[str] = None,
        password: Optional[str] = None,
        api_key: Optional[str] = None,
        pubkey_path: Optional[str] = None,
        pubkey_id: Optional[str] = None,
        use_tls: Optional[bool] = None,
        reconnect_mode: str = "off",
        backoff_initial: float = 1.0,
        backoff_max: float = 30.0,
    ):
        """Connect to a server and launch interactive console.

        Args mirror :meth:`connect_to_server` plus reconnection controls.

        Returns:
            bool: Success flag from console session.
        """
        # Connect to server
        connection = await self.connect_to_server(
            host,
            port,
            username,
            password,
            api_key,
            pubkey_path,
            pubkey_id,
            use_tls,
        )
        if not connection:
            return False

        # Create console UI
        console = ConsoleUI(
            connection,
            reconnect_mode=reconnect_mode,
            backoff_initial=backoff_initial,
            backoff_max=backoff_max,
        )

        # Run console
        return await console.run()

    async def run_console_on_port(
        self,
        host: str,
        port: int,
        port_name: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        api_key: Optional[str] = None,
        pubkey_path: Optional[str] = None,
        pubkey_id: Optional[str] = None,
        use_tls: Optional[bool] = None,
        reconnect_mode: str = "off",
        backoff_initial: float = 1.0,
        backoff_max: float = 30.0,
    ):
        """Connect to a server, attach to specific port, run console UI.

        Returns:
            bool: Success flag from console session.
        """
        # Connect to server
        connection = await self.connect_to_server(
            host,
            port,
            username,
            password,
            api_key,
            pubkey_path,
            pubkey_id,
            use_tls,
        )
        if not connection:
            return False

        # Connect to port
        if not await self.connect_to_port(connection, port_name):
            return False

        # Create console UI
        console = ConsoleUI(
            connection,
            reconnect_mode=reconnect_mode,
            backoff_initial=backoff_initial,
            backoff_max=backoff_max,
        )

        # Run console
        return await console.run()

    async def shutdown(self):
        """Close all active server connections.

        Notes:
            Idempotent; safe to call multiple times.
        """
        # Close all connections
        for connection in self.connections.values():
            await connection.close()

    async def run_batch_command(
        self,
        host: str,
        port: int,
        port_name: str,
        command: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        api_key: Optional[str] = None,
        pubkey_path: Optional[str] = None,
        pubkey_id: Optional[str] = None,
        use_tls: Optional[bool] = None,
    ):
        """Execute a single command non‑interactively against a port.

        Args:
            host: Server host.
            port: Server port.
            port_name: Target port.
            command: Command text to send (CRLF appended automatically).
            username: Optional username.
            password: Optional password.
            api_key: Optional API key.
            use_tls: TLS flag.

        Returns:
            bool: True on full success, False otherwise.
        """
        # Connect to server
        connection = await self.connect_to_server(
            host,
            port,
            username,
            password,
            api_key,
            pubkey_path,
            pubkey_id,
            use_tls,
        )
        if not connection:
            return False

        # Connect to port
        if not await self.connect_to_port(connection, port_name):
            return False

        # Send command
        await connection.send_data(command.encode() + b"\r\n")

        # Wait for response
        response = await connection.read_data(timeout=5)
        # Handle both string and bytes responses
        if isinstance(response, bytes):
            self.logger.info(response.decode())
        else:
            self.logger.info(str(response))

        # Close connection
        await connection.close()

        return True


def main():
    """Parse CLI arguments and dispatch requested client action.

    Command Priority:
        1. --name <console>
        2. --list -s host
        3. port_name command -s host (batch)
        4. port_name -s host (interactive port)
        5. -s host (interactive server)

    Notes:
        Exports OPENMUX_CLIENT_LOG_* env vars for early logging configuration.
    """
    parser = argparse.ArgumentParser(description="OpenMux Client")
    parser.add_argument("-c", "--config", help="Path to configuration file")
    parser.add_argument("-s", "--server", help="Server to connect to")
    parser.add_argument("-p", "--port", type=int, default=8023, help="Server port")
    parser.add_argument("-u", "--username", help="Username for authentication")
    parser.add_argument("-w", "--password", help="Password for authentication")
    parser.add_argument("-k", "--key", help="API key for authentication")
    parser.add_argument("--pubkey", help="Path to Ed25519 private key for public key authentication")
    parser.add_argument("--pubkey-id", help="Optional public key identifier when multiple keys exist")
    parser.add_argument("-l", "--list", action="store_true", help="List available ports")
    # Cache options removed: client always fetches live from server
    parser.add_argument("-n", "--name", help="Connect to a console by name")
    # Tri-state: None when not provided, True when specified, False via --no-encrypt
    parser.add_argument(
        "-e", "--encrypt", dest="encrypt", action="store_const", const=True, default=None, help="Use encrypted connection"
    )
    parser.add_argument("--no-encrypt", dest="encrypt", action="store_const", const=False, help="Disable encrypted connection")
    # Management mode removed
    parser.add_argument(
        "--reconnect",
        choices=["off", "manual", "auto"],
        default="off",
        help="Reconnect behavior when server disconnects: off, manual, or auto",
    )
    parser.add_argument(
        "--reconnect-backoff-initial",
        type=float,
        default=1.0,
        help="Initial reconnect backoff in seconds (auto mode)",
    )
    parser.add_argument(
        "--reconnect-backoff-max",
        type=float,
        default=30.0,
        help="Maximum reconnect backoff in seconds (auto mode)",
    )
    parser.add_argument(
        "--log-to-file",
        action="store_true",
        help="Write logs to file only (no console logging)",
    )
    parser.add_argument(
        "--log-dir",
        default=None,
        help="Directory for client log files (default: logs)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (-v=INFO, -vv=DEBUG)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Custom log file name (default: openmux_client.log)",
    )
    parser.add_argument(
        "--log-max-size",
        type=int,
        default=10,
        help="Maximum log file size in MB before rotation (default: 10)",
    )
    parser.add_argument(
        "--log-backups",
        type=int,
        default=5,
        help="Number of backup log files to keep (default: 5)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress informational messages during console operation",
    )
    parser.add_argument("port_name", nargs="?", help="Port to connect to")
    parser.add_argument("command", nargs="?", help="Command to send (if not interactive)")
    parser.add_argument(
        "--adapter",
        choices=["tcp", "websocket"],
        default="tcp",
        help="Connection adapter type (default: tcp). Use 'websocket' to connect via the web_console adapter's /ws endpoint.",
    )
    parser.add_argument(
        "--ws-user",
        help="Username for Basic Auth when using --adapter websocket (overrides -u).",
    )
    parser.add_argument(
        "--ws-password",
        help="Password for Basic Auth when using --adapter websocket (overrides -w).",
    )
    args = parser.parse_args()

    # Build logging config for client explicitly (no env reliance)
    if args.verbose >= 2:
        level_name = "DEBUG"
    elif args.verbose == 1:
        level_name = "INFO"
    else:
        level_name = "WARNING"

    logging_config = {
        "log_level": level_name,
        "file_only": bool(args.log_to_file) and not args.quiet and not args.log_dir,
        "file_logging_enabled": bool(args.log_to_file) or bool(args.log_file) or bool(args.log_dir),
        "log_dir": args.log_dir or "logs",
        "log_file": args.log_file or "openmux_client.log",
        "log_max_size_mb": int(args.log_max_size or 10),
        "log_backups": int(args.log_backups or 5),
    }

    # Create client with config path; logging manager will receive logging_config from OpenMuxClient
    client = OpenMuxClient(args.config)
    # Inject logging config into client and re-init logging manager accordingly
    if isinstance(client.config, dict):
        existing = client.config.get("logging", {}) or {}
        existing.update(logging_config)
        client.config["logging"] = existing
        client.logging_manager = ClientLoggingManager(client.config["logging"])  # reconfigure logging

    # Run client
    try:
        if args.name:
            # Connect to a port by name
            asyncio.run(
                client.find_and_connect_by_name(
                    args.name,
                    (args.ws_user if args.adapter == "websocket" and args.ws_user else args.username),
                    (args.ws_password if args.adapter == "websocket" and args.ws_password else args.password),
                    args.key,
                    *(
                        client.get_pubkey_for(args.server)[0:2]
                        if (not args.pubkey and not args.pubkey_id)
                        else (args.pubkey, args.pubkey_id)
                    ),
                    args.encrypt,
                    args.reconnect,
                    args.reconnect_backoff_initial,
                    args.reconnect_backoff_max,
                )
            )
        elif args.list:
            # List ports on a server
            resolved = client.resolve_default_server(args.server, args.port)
            if not resolved:
                logging.error("No server specified and no default found in config")
                parser.print_help()
                sys.exit(1)
            server_host, server_port = resolved

            async def list_ports_task():
                # Prefer CLI creds; else use config creds for chosen host
                resolved_username, resolved_password, resolved_api_key = args.username, args.password, args.key
                if resolved_username is None and resolved_password is None and resolved_api_key is None:
                    cfg_username, cfg_password, cfg_api_key = client.get_credentials_for(server_host)
                    resolved_username = cfg_username if cfg_username is not None else resolved_username
                    resolved_password = cfg_password if cfg_password is not None else resolved_password
                    resolved_api_key = cfg_api_key if cfg_api_key is not None else resolved_api_key
                # Determine pubkey fallback
                effective_pubkey = args.pubkey
                effective_pubkey_id = args.pubkey_id
                if not effective_pubkey and not effective_pubkey_id:
                    cfg_pk, cfg_pk_id = client.get_pubkey_for(server_host)
                    if cfg_pk:
                        effective_pubkey, effective_pubkey_id = cfg_pk, cfg_pk_id
                connection = await client.connect_to_server(
                    server_host,
                    server_port,
                    resolved_username,
                    resolved_password,
                    resolved_api_key,
                    effective_pubkey,
                    effective_pubkey_id,
                    args.encrypt,
                    adapter_type=args.adapter,
                    port_name=None,  # discovery mode for websocket
                    ws_basic_user=(args.ws_user if args.ws_user else args.username),
                    ws_basic_password=(args.ws_password if args.ws_password else args.password),
                )
                if connection:
                    try:
                        await client.list_ports(connection)
                    finally:
                        await connection.close()

            asyncio.run(list_ports_task())
        elif args.port_name and args.command:
            # Run batch command (unsupported for websocket adapter at present)
            resolved = client.resolve_default_server(args.server, args.port)
            if not resolved:
                logging.error("No server specified and no default found in config")
                parser.print_help()
                sys.exit(1)
            server_host, server_port = resolved
            if args.adapter == "websocket":
                logging.error("Batch mode (- command) not yet supported with --adapter websocket")
                sys.exit(2)
            else:
                asyncio.run(
                    client.run_batch_command(
                        server_host,
                        server_port,
                        args.port_name,
                        args.command,
                        # Prefer CLI creds, fallback to config for chosen host
                        (args.username if args.username is not None else client.get_credentials_for(server_host)[0]),
                        (args.password if args.password is not None else client.get_credentials_for(server_host)[1]),
                        (args.key if args.key is not None else client.get_credentials_for(server_host)[2]),
                        (args.pubkey if args.pubkey is not None else client.get_pubkey_for(server_host)[0]),
                        (args.pubkey_id if args.pubkey_id is not None else client.get_pubkey_for(server_host)[1]),
                        args.encrypt,
                    )
                )
        elif args.port_name:
            # Connect to port
            resolved = client.resolve_default_server(args.server, args.port)
            if not resolved:
                logging.error("No server specified and no default found in config")
                parser.print_help()
                sys.exit(1)
            server_host, server_port = resolved
            if args.adapter == "websocket":

                async def ws_console_on_port():
                    conn = await client.connect_to_server(
                        server_host,
                        server_port,
                        (args.ws_user if args.ws_user else args.username),
                        (args.ws_password if args.ws_password else args.password),
                        args.key,
                        (args.pubkey if args.pubkey is not None else client.get_pubkey_for(server_host)[0]),
                        (args.pubkey_id if args.pubkey_id is not None else client.get_pubkey_for(server_host)[1]),
                        args.encrypt,
                        adapter_type="websocket",
                        port_name=args.port_name,
                        ws_basic_user=(args.ws_user if args.ws_user else args.username),
                        ws_basic_password=(args.ws_password if args.ws_password else args.password),
                    )
                    if not conn:
                        return
                    from .console import ConsoleUI

                    console = ConsoleUI(
                        conn,
                        reconnect_mode=args.reconnect,
                        backoff_initial=args.reconnect_backoff_initial,
                        backoff_max=args.reconnect_backoff_max,
                    )
                    await console.run()

                asyncio.run(ws_console_on_port())
            else:
                asyncio.run(
                    client.run_console_on_port(
                        server_host,
                        server_port,
                        args.port_name,
                        (args.username if args.username is not None else client.get_credentials_for(server_host)[0]),
                        (args.password if args.password is not None else client.get_credentials_for(server_host)[1]),
                        (args.key if args.key is not None else client.get_credentials_for(server_host)[2]),
                        (args.pubkey if args.pubkey is not None else client.get_pubkey_for(server_host)[0]),
                        (args.pubkey_id if args.pubkey_id is not None else client.get_pubkey_for(server_host)[1]),
                        args.encrypt,
                        args.reconnect,
                        args.reconnect_backoff_initial,
                        args.reconnect_backoff_max,
                    )
                )
        else:
            # Run console
            resolved = client.resolve_default_server(args.server, args.port)
            if not resolved:
                logging.error("Either server (-s) or console name (-n) is required, and no default server was found")
                parser.print_help()
                sys.exit(1)
            server_host, server_port = resolved
            if args.adapter == "websocket":

                async def ws_console():
                    # discovery mode first to list ports
                    conn = await client.connect_to_server(
                        server_host,
                        server_port,
                        (args.ws_user if args.ws_user else args.username),
                        (args.ws_password if args.ws_password else args.password),
                        args.key,
                        (args.pubkey if args.pubkey is not None else client.get_pubkey_for(server_host)[0]),
                        (args.pubkey_id if args.pubkey_id is not None else client.get_pubkey_for(server_host)[1]),
                        args.encrypt,
                        adapter_type="websocket",
                        port_name=None,
                        ws_basic_user=(args.ws_user if args.ws_user else args.username),
                        ws_basic_password=(args.ws_password if args.ws_password else args.password),
                    )
                    if not conn:
                        return
                    ports = await client.list_ports(conn)
                    if not ports:
                        logging.error("No ports available.")
                        await conn.close()
                        return
                    # Simple heuristic: auto-select first port
                    first = ports[0]
                    pn = first.get("name") if isinstance(first, dict) else first
                    await conn.close()
                    conn2 = await client.connect_to_server(
                        server_host,
                        server_port,
                        (args.ws_user if args.ws_user else args.username),
                        (args.ws_password if args.ws_password else args.password),
                        args.key,
                        (args.pubkey if args.pubkey is not None else client.get_pubkey_for(server_host)[0]),
                        (args.pubkey_id if args.pubkey_id is not None else client.get_pubkey_for(server_host)[1]),
                        args.encrypt,
                        adapter_type="websocket",
                        port_name=pn,
                        ws_basic_user=(args.ws_user if args.ws_user else args.username),
                        ws_basic_password=(args.ws_password if args.ws_password else args.password),
                    )
                    if not conn2:
                        return
                    from .console import ConsoleUI

                    console = ConsoleUI(
                        conn2,
                        reconnect_mode=args.reconnect,
                        backoff_initial=args.reconnect_backoff_initial,
                        backoff_max=args.reconnect_backoff_max,
                    )
                    await console.run()

                asyncio.run(ws_console())
            else:
                asyncio.run(
                    client.run_console(
                        server_host,
                        server_port,
                        (args.username if args.username is not None else client.get_credentials_for(server_host)[0]),
                        (args.password if args.password is not None else client.get_credentials_for(server_host)[1]),
                        (args.key if args.key is not None else client.get_credentials_for(server_host)[2]),
                        (args.pubkey if args.pubkey is not None else client.get_pubkey_for(server_host)[0]),
                        (args.pubkey_id if args.pubkey_id is not None else client.get_pubkey_for(server_host)[1]),
                        args.encrypt,
                        args.reconnect,
                        args.reconnect_backoff_initial,
                        args.reconnect_backoff_max,
                    )
                )
    except KeyboardInterrupt:
        logging.info("Client stopped by user")
        asyncio.run(client.shutdown())


if __name__ == "__main__":
    main()
