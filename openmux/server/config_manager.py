"""
OpenMux Server Configuration Manager

Purpose
- Centralizes loading, validating, reading, and writing of the OpenMux server YAML configuration.
- Provides a stable API for other server components (e.g., main, adapters, managers) to obtain validated configuration values.

Key responsibilities
- Load YAML from a filesystem path with basic I/O guards (existence, non-empty content).
- Parse YAML into a Python dictionary via yaml.safe_load.
- Validate required sections and normalize legacy fields:
    * Required top-level sections: "server", "authentication".
        * Server: legacy host/port/bind_address removed from usage; keep only metadata
            (e.g., id/description). For backwards compatibility, metadata getters
            proxy to client_listener.host/port when requested.
    * Authentication: require either users or api_keys to be present.
    * Serial ports: support both old list format and new unified adapter format {adapter_type: "serial", ports: [...]}; validate required fields.
- Provide getters that lazily load configuration on first access.
- Persist configuration changes atomically with a simple .bak backup of the previous file.

Inputs/Outputs
- Input: path to a YAML file on disk provided at construction.
- Output: in-memory dict accessible via getters; supports writing updated configs back to the same path.

Main API
- load_config() -> Dict[str, Any]: load + validate + return the config.
- get_server_host() -> str, get_server_port() -> int
- get_authentication_config() -> Dict[str, Any]
- get_serial_ports_config() -> list
- get_web_server_config() -> Dict[str, Any], is_web_server_enabled() -> bool
- get_port_config(port_name: str) -> Optional[Dict[str, Any]]
- save_config(config: Dict[str, Any]) -> bool

Error handling & logging
- Logs failures (missing file, empty content, YAML parse errors, validation errors) and re-raises where appropriate.
- Validation helpers raise ValueError on schema issues.

Compatibility notes
- Supports both legacy and unified serial port configuration formats to ease migration.
- Normalizes host from bind_address when present to maintain backward compatibility.
"""

import logging
import os
from typing import Any, Dict, Optional

import yaml


class ConfigManager:
    def __init__(self, config_path: str):
        """Initialize the configuration manager.

        Args:
            config_path: Path to the YAML configuration file.
        """
        self.config_path = config_path
        self.logger = logging.getLogger("openmux.config")
        self.config: Optional[Dict[str, Any]] = None

    def load_config(self) -> Dict[str, Any]:
        """Load, validate, and return the configuration.

        Reads the file, ensures it exists and is non-empty, parses YAML, and
        performs validation/normalization steps (server host/port defaults,
        authentication requirements, optional serial port validation).

        Returns:
            The validated configuration dictionary.

        Raises:
            FileNotFoundError: If the configuration file does not exist.
            ValueError: If the file is empty or validation fails.
            yaml.YAMLError: If the file cannot be parsed as valid YAML.
            Exception: For any other unexpected error during load.
        """
        self.logger.info(f"Loading configuration from {self.config_path}")

        try:
            if not os.path.exists(self.config_path):
                self.logger.error(f"Configuration file not found: {self.config_path}")
                raise FileNotFoundError(f"Configuration file not found: {self.config_path}")

            with open(self.config_path, "r") as f:
                content = f.read()
                if not content.strip():
                    self.logger.error(f"Configuration file is empty: {self.config_path}")
                    raise ValueError(f"Configuration file is empty: {self.config_path}")

                self.config = yaml.safe_load(content)

            # Validate configuration
            self._validate_config()

            # Ensure we have a valid config after validation
            if self.config is None:
                raise ValueError("Configuration is None after loading and validation")

            return self.config
        except FileNotFoundError:
            self.logger.error(f"Configuration file not found: {self.config_path}")
            raise
        except yaml.YAMLError as e:
            self.logger.error(f"Error parsing YAML configuration: {e}", exc_info=True)
            raise
        except Exception as e:
            self.logger.error(f"Error loading configuration: {e}", exc_info=True)
            raise

    def _check_config_loaded(self):
        """Ensure the configuration dictionary has been loaded.

        Raises:
            ValueError: If the configuration has not been loaded yet.
        """
        if self.config is None:
            raise ValueError("Configuration file could not be loaded properly - empty or invalid YAML")

    def _validate_required_sections(self):
        """Validate presence of required top-level sections.

        Required sections are: ``server`` and ``authentication``.

        Raises:
            ValueError: If any required section is missing.
        """
        assert self.config is not None  # Type narrowing
        required_sections = ["server", "authentication"]
        for section in required_sections:
            if section not in self.config:
                raise ValueError(f"Missing required configuration section: {section}")

    def _validate_server_config(self):
        """Validate the ``server`` section (metadata only).

        Notes:
            - Legacy fields ``host``, ``port``, and ``bind_address`` are no longer
              used for binding. Connection endpoints are configured per-adapter
              (e.g., ``client_listener``, ``web_console``, ``muxcon``).
            - We keep ``server`` for metadata like ``id`` and ``description``.
              Any legacy binding keys are ignored if present.
        """
        assert self.config is not None  # Type narrowing
        srv = self.config.get("server", {})
        # Purposely do not synthesize host/port. If present, they are ignored.
        # Optionally, we could strip them, but we avoid mutating on load.
        # Leave any values as-is for round-trip friendliness.

    def _validate_authentication_config(self):
        """Validate the ``authentication`` section.

        Ensures that at least one of ``users``, ``api_keys``, ``public_keys``, or ``pam`` is present.

        Raises:
            ValueError: If neither users nor api_keys is defined.
        """
        assert self.config is not None  # Type narrowing
        auth = self.config["authentication"]
        if not ("users" in auth or "api_keys" in auth or "public_keys" in auth or "pam" in auth):
            raise ValueError("Authentication section must contain 'users', 'api_keys', 'public_keys', or 'pam'")

    def _validate_serial_ports_config(self):
        """Validate the ``serial_ports`` section (if present).

        Supports both legacy list format and the unified adapter dict format.

        Raises:
            ValueError: If structure or required fields are invalid.
        """
        assert self.config is not None  # Type narrowing

        # Handle both old and new format
        serial_config = self.config["serial_ports"]

        # New unified format: serial_ports is a dict with adapter_type and ports
        if isinstance(serial_config, dict) and "adapter_type" in serial_config:
            # Unified adapter format
            if serial_config.get("adapter_type") != "serial":
                raise ValueError("serial_ports section must have adapter_type: serial")

            ports = serial_config.get("ports", [])
            if not isinstance(ports, list):
                raise ValueError("serial_ports.ports must be a list")

            for i, port in enumerate(ports):
                if "name" not in port:
                    raise ValueError(f"Serial port at index {i} is missing required 'name' field")
                if "device" not in port:
                    raise ValueError(f"Serial port '{port['name']}' is missing required 'device' field")
        else:
            # Old format: serial_ports is a list of port dicts
            if not isinstance(serial_config, list):
                raise ValueError("serial_ports must be a list or unified adapter config")

            for i, port in enumerate(serial_config):
                if "name" not in port:
                    raise ValueError(f"Serial port at index {i} is missing required 'name' field")
                # In unified system, adapter type is determined by section name
                # No need to check for 'adapter' field anymore

    def _validate_config(self):
        """Run all validation and normalization steps on the config."""
        self._check_config_loaded()
        self._validate_required_sections()
        self._validate_server_config()
        self._validate_authentication_config()

        # Only validate serial ports if the section exists
        assert self.config is not None  # Type narrowing
        if "serial_ports" in self.config:
            self._validate_serial_ports_config()

    def get_server_host(self) -> str:
        """Deprecated: Return effective console host (client_listener.host).

        Returns:
            Host where the TCP console listens (client_listener.host). Defaults
            to 127.0.0.1 when not configured.
        """
        if not self.config:
            self.load_config()
        assert self.config is not None
        try:
            host = (self.config.get("client_listener") or {}).get("host")
            if isinstance(host, str) and host.strip():
                self.logger.debug("get_server_host is deprecated; proxying to client_listener.host")
                return host
        except Exception:
            pass
        return "127.0.0.1"

    def get_server_port(self) -> int:
        """Deprecated: Return effective console port (client_listener.port).

        Returns:
            Port where the TCP console listens (client_listener.port). Defaults
            to 8023 when not configured.
        """
        if not self.config:
            self.load_config()
        assert self.config is not None
        try:
            port = (self.config.get("client_listener") or {}).get("port")
            if isinstance(port, int) and 1 <= port <= 65535:
                self.logger.debug("get_server_port is deprecated; proxying to client_listener.port")
                return port
        except Exception:
            pass
        return 8023

    def get_client_listener_config(self) -> Dict[str, Any]:
        """Return the client listener configuration mapping.

        Returns:
            Dict with host/port and optional tuning keys; empty dict if not set.
        """
        if not self.config:
            self.load_config()
        assert self.config is not None
        cfg = self.config.get("client_listener", {})
        return cfg if isinstance(cfg, dict) else {}

    def get_authentication_config(self) -> Dict[str, Any]:
        """Return the ``authentication`` configuration section.

        Returns:
            A dictionary containing authentication settings (users, api_keys, etc.).
        """
        if not self.config:
            self.load_config()
        assert self.config is not None  # Type narrowing
        return self.config["authentication"]

    def get_serial_ports_config(self) -> list:
        """Return the ``serial_ports`` configuration section.

        Returns:
            Either a list of port definitions (legacy) or a unified adapter
            config dict with ``adapter_type`` and ``ports`` list.
        """
        if not self.config:
            self.load_config()
        assert self.config is not None  # Type narrowing
        return self.config["serial_ports"]

    def get_web_server_config(self) -> Dict[str, Any]:
        """Return the ``web_server`` configuration subsection.

        Returns:
            Dict of web server settings, or empty dict if not configured.
        """
        if not self.config:
            self.load_config()
        assert self.config is not None  # Type narrowing
        return self.config.get("web_server", {})

    def is_web_server_enabled(self) -> bool:
        """Return whether the optional web server is enabled.

        Returns:
            True if enabled, otherwise False.
        """
        web_config = self.get_web_server_config()
        return web_config.get("enabled", False)

    def get_port_config(self, port_name: str) -> Optional[Dict[str, Any]]:
        """Return configuration for a specific serial port by name.

        Args:
            port_name: The name of the port to look up.

        Returns:
            The port dictionary if found, else None.
        """
        if not self.config:
            self.load_config()
        assert self.config is not None  # Type narrowing

        for port in self.config["serial_ports"]:
            if port["name"] == port_name:
                return port

        return None

    def save_config(self, config: Dict[str, Any]) -> bool:
        """Persist a configuration mapping back to disk.

        Creates a ``.bak`` backup of the existing file (if present) before
        writing the new configuration.

        Args:
            config: The configuration mapping to serialize and save.

        Returns:
            True on success, False if an error occurs (also logged).
        """
        try:
            # Create a backup of the current config
            if os.path.exists(self.config_path):
                backup_path = f"{self.config_path}.bak"
                with (
                    open(self.config_path, "r") as src,
                    open(backup_path, "w") as dst,
                ):
                    dst.write(src.read())

            # Write new config
            with open(self.config_path, "w") as f:
                yaml.dump(config, f, default_flow_style=False)

            self.config = config
            return True
        except Exception as e:
            self.logger.error(f"Error saving configuration: {e}", exc_info=True)
            return False
