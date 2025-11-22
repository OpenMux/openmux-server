"""Generic Adapter Factory and Plugin Registry.

Creates `BaseGenericAdapter` instances from configuration using a plugin
registry. Supports both legacy section-based configs and the unified
``adapters:`` list format. Handles optional fail-fast behavior for missing
or invalid adapter definitions.
"""

import logging
from typing import Any, Dict, List, Optional, Type

from .base_adapter import BaseGenericAdapter
from .lifecycle import DynamicPortManager
from ..security_policy import SecurityPolicy

logger = logging.getLogger(__name__)


class AdapterPlugin:
    """Metadata describing a registered adapter plugin.

    Wraps identifying information plus the adapter class reference used by
    the factory to materialize instances from configuration.

    Args:
        name: Human-readable adapter display name.
        config_section: Top-level configuration section key this plugin maps to.
        adapter_class: Concrete subclass of `BaseGenericAdapter`.
    """

    def __init__(
        self,
        name: str,
        config_section: str,
        adapter_class: Type[BaseGenericAdapter],
    ):
        self.name = name
        self.config_section = config_section  # Top-level config section name
        self.adapter_class = adapter_class  # Class that inherits from BaseGenericAdapter

    def __repr__(self) -> str:
        return f"AdapterPlugin(name='{self.name}', section='{self.config_section}', class={self.adapter_class.__name__})"


class PluginRegistry:
    """Central registry of adapter plugins.

    Maintains mapping from configuration section -> `AdapterPlugin` and
    captures import errors for diagnostics (used by fail-fast logic).
    Public methods are intentionally minimal: register, lookup, list.
    """

    def __init__(self):
        """Initialize an empty plugin registry.

        Sets up internal maps for plugins and recorded import errors, then
        registers built-in adapters available within this package.
        """
        self._plugins = {}
        # Track adapter import errors: { adapter_class_name: "ExcType: message" }
        self._import_errors = {}
        self._register_built_in_plugins()

    def register_plugin(self, plugin: AdapterPlugin) -> None:
        """Register an adapter plugin.

        Args:
            plugin: Plugin metadata object to add.
        """
        logger.debug(f"Registering plugin: {plugin}")
        self._plugins[plugin.config_section] = plugin
        # Capture adapter_type from class if provided (method or attribute)
        adapter_cls = plugin.adapter_class
        atype = None
        # Prefer get_adapter_type() method
        get_type = getattr(adapter_cls, "get_adapter_type", None)
        if callable(get_type):
            try:
                atype = get_type(adapter_cls)  # call as unbound if defined without @staticmethod
            except Exception:  # justification: non-critical capability introspection fallback to next form
                try:
                    atype = get_type()  # possible @staticmethod
                except (
                    Exception
                ):  # justification: capability detection is optional; safe to continue without adapter_type index
                    atype = None
        if not atype:
            atype = getattr(adapter_cls, "adapter_type", None)
        if isinstance(atype, str) and atype:
            # Normalize
            atype_l = atype.lower()
            # Maintain mapping from adapter_type -> plugin if not already present
            existing = getattr(self, "_adapter_type_index", None)
            if existing is None:
                self._adapter_type_index = {}
            if atype_l not in self._adapter_type_index:
                self._adapter_type_index[atype_l] = plugin

    def get_by_adapter_type(self, adapter_type: str) -> Optional[AdapterPlugin]:
        """Look up a plugin by its adapter type identifier.

        Adapter types are compared case-insensitively. The mapping is
        populated when plugins are registered and expose either an
        `adapter_type` attribute or a `get_adapter_type()` method.

        Args:
            adapter_type: Adapter type key, e.g. "serial", "muxcon".

        Returns:
            Matching `AdapterPlugin` or None if unknown.
        """
        idx = getattr(self, "_adapter_type_index", {})
        return idx.get(adapter_type.lower())

    def get_plugin(self, config_section: str) -> Optional[AdapterPlugin]:
        """Return plugin by configuration section name.

        Args:
            config_section: Top-level config section key.

        Returns:
            Matching `AdapterPlugin` or None.
        """
        return self._plugins.get(config_section)

    def discover_active_plugins(self, config: Dict[str, Any]) -> List[AdapterPlugin]:
        """Return list of plugins that have matching sections in config.

        Args:
            config: Full server configuration mapping.

        Returns:
            List of active plugins whose section names appear in config.
        """
        active_plugins = []
        for section_name, plugin in self._plugins.items():
            if section_name in config:
                logger.debug(f"Found active plugin: {plugin}")
                active_plugins.append(plugin)
        return active_plugins

    def get_all_plugins(self) -> List[AdapterPlugin]:
        """Return list of all registered plugins."""
        return list(self._plugins.values())

    def get_import_errors(self) -> Dict[str, str]:
        """Return a shallow copy of recorded import errors.

        Returns:
            Mapping of adapter class name -> error description.
        """
        return dict(self._import_errors)

    def _register_built_in_plugins(self) -> None:
        """Register all built-in adapter plugins.

        Logs import failures at WARNING and stores error details so startup
        can optionally abort in fail-fast mode.
        """
        logger.debug("Registering built-in adapter plugins")

        def _import_and_register(
            module_name: str,
            class_name: str,
            display_name: str,
            config_section: str,
        ) -> None:
            """Attempt import and register plugin if successful.

            Args:
                module_name: Module within this package containing the class.
                class_name: Adapter class name to import.
                display_name: Human-friendly adapter label.
                config_section: Config section key mapping to this plugin.

            Notes:
                On failure logs a warning and records import error; continues
                without raising to allow partial adapter availability.
            """
            try:
                module = __import__(f"{__package__}.{module_name}", fromlist=[class_name])  # type: ignore
                adapter_cls = getattr(module, class_name)
            except Exception as e:  # pragma: no cover (import failure path)
                msg = f"{e.__class__.__name__}: {e}"
                self._import_errors[class_name] = msg
                logger.warning("Adapter import failed: %s (%s)", class_name, msg, exc_info=True)
                return

            # Only register if class successfully imported
            self.register_plugin(AdapterPlugin(display_name, config_section, adapter_cls))

        # Unified declaration of built-in adapters (order preserved)
        built_ins = [
            # Phase 1 / core testing adapters
            ("loopback", "LoopbackAdapter", "Basic Loopback", "loopback_ports"),
            # New canonical TCP initiator (formerly client_initiator)
            ("tcp_initiator", "TcpInitiatorAdapter", "TCP Initiator", "tcp_initiator_ports"),
            ("serial", "SerialAdapter", "Serial Ports", "serial_ports"),
            ("command", "CommandAdapter", "Command Ports", "command_ports"),
            # Connection / federation / client facing adapters
            ("client_listener", "TcpServerAdapter", "Client Listener", "client_listener"),
            ("telnet_listener", "TelnetListenerAdapter", "Telnet Listener", "telnet_listener"),
            ("web_console", "WebConsoleAdapter", "Web Console", "web_console"),
            ("muxcon", "UnifiedMuxConAdapter", "MuxCon Federation", "muxcon"),
            ("web_status", "WebStatusAdapter", "Web Status", "web_status"),
            ("client_initiator", "OpenMuxClientAdapter", "OpenMux Client", "openmux_client_ports"),
        ]

        for mod, cls, disp, section in built_ins:
            _import_and_register(mod, cls, disp, section)

        # Note: legacy alias for client_initiator_ports has been removed; use tcp_initiator_ports

        # These will be implemented during Phase 2 migration if/when available
        logger.debug("Built-in plugin registration complete")


class GenericAdapterFactory:
    """Instantiate adapters from configuration.

    Supports both legacy (per-section) and unified list-based adapter
    configuration formats. Provides optional fail-fast semantics: when
    enabled (default) missing or invalid adapters abort startup.
    """

    def __init__(
        self,
        registry: Optional[PluginRegistry] = None,
        security_policy: Optional[SecurityPolicy] = None,
    ):
        """Create a factory bound to a plugin registry.

        If no registry is provided, a default `PluginRegistry` with built-in
        adapters is created.

        Args:
            registry: Optional external registry to use.
        """
        self.registry = registry or PluginRegistry()
        self.security_policy = security_policy

    def create_adapters_from_config(
        self,
        config: Dict[str, Any],
        security_policy: Optional[SecurityPolicy] = None,
    ) -> List[BaseGenericAdapter]:
        """Create all adapter instances from configuration.

        Args:
            config: Loaded server configuration mapping.

        Returns:
            List of constructed adapter instances.

        Raises:
            RuntimeError: In fail-fast mode when required adapters are missing.
        """
        adapters = []
        policy = security_policy or self.security_policy
        # Fail-fast defaults to True unless explicitly set to False in either
        # top-level 'fail_fast_adapters' or 'server.fail_fast_adapters'. Any
        # truthy value keeps it enabled. Only explicit boolean false disables.
        fail_fast = True
        try:
            srv_cfg = config.get("server", {}) or {}
            raw_val = srv_cfg.get("fail_fast_adapters")
            if raw_val is None:
                raw_val = config.get("fail_fast_adapters")
            if raw_val is not None:
                # Interpret only explicit False / 'false' / '0' as disabling
                if isinstance(raw_val, str):
                    if raw_val.strip().lower() in {"false", "0", "no", "off"}:
                        fail_fast = False
                elif raw_val is False:
                    fail_fast = False
                else:
                    fail_fast = True
        except Exception:  # justification: defensive config parsing; defaulting to fail_fast maintains safer behavior
            fail_fast = True
        if fail_fast:
            logger.info("Adapter fail-fast mode ENABLED (default). To disable, set server.fail_fast_adapters: false")

        # Check for unified adapters format if explicitly provided
        if "adapters" in config:
            logger.info(f"Using unified adapters configuration format")
            for adapter_config in config["adapters"]:
                try:
                    adapter_type = adapter_config.get("type")
                    adapter_name = adapter_config.get("name", f"{adapter_type}_adapter")

                    # Map adapter_type to registered config section name (strict names retained)
                    plugin = None
                    if adapter_type:
                        at = adapter_type.lower()
                        # Primary lookup: dynamic adapter_type index
                        plugin = self.registry.get_by_adapter_type(at)
                        if not plugin:
                            # Derive candidate section names dynamically.
                            # Canonical naming convention: <type>_ports when such a section exists;
                            # also allow direct <type> for adapters whose section is not pluralized.
                            candidates = []
                            candidates.append(f"{at}_ports")
                            candidates.append(at)
                            # Preserve order: first existing match wins
                            for name in candidates:
                                plugin = self.registry.get_plugin(name)
                                if plugin:
                                    break

                    if not plugin:
                        logger.warning(f"No plugin found for adapter type: {adapter_type}")
                        continue

                    # Validate configuration
                    adapter_class = plugin.adapter_class
                    validate_fn = getattr(adapter_class, "validate_config", None)
                    if callable(validate_fn):
                        # For unified format, pass the adapter config directly
                        if not validate_fn(adapter_config):
                            logger.error(f"Invalid configuration for {adapter_type} adapter")
                            continue

                    # Security policy enforcement
                    if policy and not self._is_adapter_allowed(policy, plugin, adapter_type):
                        logger.error(
                            "Adapter '%s' (type=%s) blocked by security policy",
                            adapter_name,
                            adapter_type,
                        )
                        continue

                    # Create adapter instance
                    adapter = adapter_class(adapter_name, adapter_config)
                    adapters.append(adapter)
                    logger.info(f"Created {adapter_type} adapter '{adapter_name}'")

                except Exception as e:
                    logger.error(f"Failed to create adapter from config {adapter_config}: {e}", exc_info=True)
                    continue
        else:
            # Fall back to legacy format
            active_plugins = self.registry.discover_active_plugins(config)
            logger.info(f"Creating adapters for {len(active_plugins)} active plugins")
            # Emit warnings for config sections that have no registered plugin (likely import failure or typo)
            core_sections = {"adapters", "server", "authentication", "logging"}
            missing_sections: List[str] = []
            for section, val in config.items():
                if section in core_sections:
                    continue
                if section not in self.registry._plugins and isinstance(val, (dict, list)):
                    logger.warning(
                        "Config section '%s' present but no adapter plugin registered (import failure or typo)",
                        section,
                    )
                    missing_sections.append(section)

            for plugin in active_plugins:
                try:
                    plugin_config = config[plugin.config_section]

                    # Validate plugin-specific configuration
                    adapter_class = plugin.adapter_class  # This is a BaseGenericAdapter subclass
                    validate_fn = getattr(adapter_class, "validate_config", None)
                    if callable(validate_fn):
                        # Pass the full config section dict for validation
                        config_to_validate = {plugin.config_section: plugin_config}
                        if not validate_fn(config_to_validate):
                            logger.error(f"Invalid configuration for {plugin.name}")
                            raise ValueError(f"Invalid configuration for {plugin.name}")

                    # Security policy enforcement (legacy sections)
                    atype = None
                    get_type = getattr(adapter_class, "get_adapter_type", None)
                    if callable(get_type):
                        try:
                            atype = get_type(adapter_class)
                        except Exception:
                            try:
                                atype = get_type()
                            except Exception:
                                atype = None
                    if policy and not self._is_adapter_allowed(policy, plugin, atype):
                        logger.error(
                            "Adapter section '%s' blocked by security policy",
                            plugin.config_section,
                        )
                        continue

                    # Create adapter instance(s) - all inherit from BaseGenericAdapter
                    created_adapters = self._create_adapter_instances(plugin, plugin_config)
                    adapters.extend(created_adapters)

                    logger.info(f"Created {len(created_adapters)} adapter instance(s) for {plugin.name}")

                except Exception as e:
                    logger.error(f"Failed to create adapter for plugin {plugin.name}: {e}", exc_info=True)
                    # Continue with other plugins rather than failing completely

        # Fail-fast handling (legacy path only) after attempting creation
        if fail_fast:
            import_errors = self.registry.get_import_errors()
            # Any explicitly configured sections missing adapters should abort
            if "active_plugins" in locals():  # ensure we are in legacy branch context
                core_sections = {"adapters", "server", "authentication", "logging"}
                configured_sections = [s for s in config.keys() if s not in core_sections]
                produced_sections = {p.config_section for p in self.registry.get_all_plugins()}
                missing = [s for s in configured_sections if s not in produced_sections]
                # Refine to those we warned about (import failures / typos)
                if missing:
                    detail_parts = []
                    for k, v in import_errors.items():
                        detail_parts.append(f"{k}: {v}")
                    detail = ", ".join(detail_parts) if detail_parts else "no import error details"
                    raise RuntimeError(
                        f"Adapter fail-fast: aborting startup due to missing adapters for sections: {', '.join(missing)} | {detail}"
                    )

        logger.info(f"Successfully created {len(adapters)} total adapter instances")
        return adapters  # Returns List[BaseGenericAdapter]

    def set_security_policy(self, policy: Optional[SecurityPolicy]) -> None:
        self.security_policy = policy

    def _is_adapter_allowed(
        self,
        policy: SecurityPolicy,
        plugin: AdapterPlugin,
        adapter_type: Optional[str],
    ) -> bool:
        module_name = getattr(plugin.adapter_class, "__module__", None)
        return policy.is_adapter_allowed(
            module_name=module_name,
            adapter_type=(adapter_type or plugin.config_section),
        )

    def _create_adapter_instances(self, plugin: AdapterPlugin, plugin_config: Any) -> List[BaseGenericAdapter]:
        """Create one or more adapter instances for a plugin.

        Args:
            plugin: Adapter plugin metadata.
            plugin_config: Raw configuration object for the plugin section.

        Returns:
            List with one or more adapter instances.
        """
        instances = []
        adapter_class = plugin.adapter_class
        # _create_adapter_instances should only create adapters; fail-fast logic lives in create_adapters_from_config
        instances.append(self._create_single_adapter(adapter_class, plugin.config_section, plugin_config))
        return instances

    def _create_single_adapter(
        self,
        adapter_class: Type[BaseGenericAdapter],
        adapter_name: str,
        config: Any,
    ) -> BaseGenericAdapter:
        """Create a single adapter instance and attach port manager.

        Args:
            adapter_class: Concrete adapter class.
            adapter_name: Logical adapter name / config section.
            config: Config object (dict or list) for this adapter.

        Returns:
            Instantiated adapter with `DynamicPortManager` attached.
        """
        logger.debug(f"Creating adapter instance: {adapter_name} ({adapter_class.__name__})")

        # For the unified adapter system, we need to pass the config in the correct format
        # The config should contain the plugin-specific config section
        if isinstance(config, list):
            # For list-based configs like loopback_ports, wrap it in a dict
            adapter_config = {adapter_name: config}
        else:
            # For dict-based configs, pass as-is
            adapter_config = config

        # Create adapter instance
        adapter = adapter_class(adapter_name, adapter_config)
        if self.security_policy is not None:
            setter = getattr(adapter, "set_security_policy", None)
            if callable(setter):
                try:
                    setter(self.security_policy)
                except Exception:
                    logger.warning("Adapter %s failed to accept security policy", adapter_name, exc_info=True)

        # Create and attach port manager for dynamic lifecycle management
        port_manager = DynamicPortManager(adapter)

        logger.debug(f"Adapter {adapter_name} created with dynamic port management")
        return adapter

    def get_registry(self) -> PluginRegistry:
        """Return the plugin registry for external plugin registration.

        Exposes internal registry to allow external plugin injection during
        application bootstrap.
        """
        return self.registry

    def register_external_plugin(self, plugin: AdapterPlugin) -> None:
        """Register an external plugin.

        Args:
            plugin: Plugin metadata to register.
        """
        logger.info(f"Registering external plugin: {plugin}")
        self.registry.register_plugin(plugin)
