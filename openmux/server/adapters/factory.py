"""Generic Adapter Factory and Plugin Registry.

Creates `BaseGenericAdapter` instances from configuration using a plugin
registry. Supports both legacy section-based configs and the unified
``adapters:`` list format. Handles optional fail-fast behavior for missing
or invalid adapter definitions.
"""

import logging
from typing import Any, Dict, List, Optional, Type

from ..security_policy import SecurityPolicy
from .base_adapter import BaseGenericAdapter
from .lifecycle import DynamicPortManager

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
        logger.debug("Registering plugin: %s", plugin)
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
                logger.debug("Found active plugin: %s", plugin)
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
                module_name: Module within this package containing the class,
                    or a fully-qualified dotted path (e.g. starting with
                    "openmux.") for adapters that live outside this package.
                class_name: Adapter class name to import.
                display_name: Human-friendly adapter label.
                config_section: Config section key mapping to this plugin.

            Notes:
                On failure logs a warning and records import error; continues
                without raising to allow partial adapter availability.
            """
            full_module_name = module_name if module_name.startswith("openmux.") else f"{__package__}.{module_name}"
            try:
                module = __import__(full_module_name, fromlist=[class_name])  # type: ignore
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
            ("ssh_listener", "SshListenerAdapter", "SSH Listener", "ssh_listener"),
            # WebConsole is a core, integrated component, not a peripheral port adapter -
            # it lives at openmux.server.web_console rather than under this package.
            ("openmux.server.web_console", "WebConsoleAdapter", "Web Console", "web_console"),
            ("muxcon", "UnifiedMuxConAdapter", "MuxCon Federation", "muxcon"),
            ("web_status", "WebStatusAdapter", "Web Status", "web_status"),
            # PDU power management (outlets are managed, not console ports)
            ("pdu", "PduAdapter", "PDU Power", "power"),
        ]

        for mod, cls, disp, section in built_ins:
            _import_and_register(mod, cls, disp, section)

        # Note: the legacy section keys client_initiator_ports and
        # openmux_client_ports have been removed; use tcp_initiator_ports

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
        fail_fast = self._resolve_fail_fast(config)
        if fail_fast:
            logger.info("Adapter fail-fast mode ENABLED (default). To disable, set server.fail_fast_adapters: false")
        if "adapters" in config:
            logger.info("Using unified adapters configuration format")
            self._create_unified_adapters(config["adapters"], policy, adapters)
        else:
            # Fall back to legacy format
            active_plugins = self.registry.discover_active_plugins(config)
            logger.info("Creating adapters for %s active plugins", len(active_plugins))
            # Emit warnings for config sections that have no registered plugin (likely import failure or typo)
            self._warn_unregistered_sections(config)
            self._create_legacy_adapters(active_plugins, policy, config, adapters)
        # Fail-fast handling (legacy path only) after attempting creation.
        # The "active_plugins" in locals() guard keeps the legacy-branch abort context.
        if fail_fast and "active_plugins" in locals():
            abort_msg = self._fail_fast_abort(config)
            if abort_msg is not None:
                raise RuntimeError(abort_msg)
        logger.info("Successfully created %s total adapter instances", len(adapters))
        return adapters  # Returns List[BaseGenericAdapter]

    def _resolve_fail_fast(self, config: Dict[str, Any]) -> bool:
        """Resolve adapter fail-fast mode from config.

        Defaults to True unless explicitly disabled by a top-level or
        server-level ``fail_fast_adapters`` value. A parse error falls back
        to fail-fast (the safer default).
        """
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
        return fail_fast

    def _create_unified_adapters(
        self,
        adapter_configs: List[Dict[str, Any]],
        policy: Optional[SecurityPolicy],
        adapters: List[BaseGenericAdapter],
    ) -> None:
        """Create adapters from the unified ``adapters`` list format.

        Skips (with logging) entries that have no plugin, fail validation,
        are blocked by policy, or raise during construction.
        """
        for adapter_config in adapter_configs:
            try:
                adapter_type = adapter_config.get("type")
                adapter_name = adapter_config.get("name", f"{adapter_type}_adapter")

                plugin = self._resolve_unified_plugin(adapter_type)
                if not plugin:
                    logger.warning("No plugin found for adapter type: %s", adapter_type)
                    continue

                # Validate configuration
                if not self._unified_config_ok(plugin.adapter_class, adapter_config, adapter_type):
                    continue

                # Security policy enforcement
                if not self._unified_policy_ok(policy, plugin, adapter_name, adapter_type):
                    continue

                # Create adapter instance
                adapter = plugin.adapter_class(adapter_name, adapter_config)
                adapters.append(adapter)
                logger.info("Created %s adapter '%s'", adapter_type, adapter_name)

            except Exception as e:
                logger.error("Failed to create adapter from config %s: %s", adapter_config, e, exc_info=True)
                continue

    def _resolve_unified_plugin(self, adapter_type: Optional[str]) -> Optional[AdapterPlugin]:
        """Resolve a unified ``type`` string to a registered plugin.

        Uses the dynamic adapter_type index first, then candidate section
        names in order: ``<type>_ports`` (canonical), then bare ``<type>``.
        """
        if not adapter_type:
            return None
        at = adapter_type.lower()
        # Primary lookup: dynamic adapter_type index
        plugin = self.registry.get_by_adapter_type(at)
        if not plugin:
            # Preserve order: first existing candidate wins
            for name in (f"{at}_ports", at):
                plugin = self.registry.get_plugin(name)
                if plugin:
                    break
        return plugin

    def _unified_config_ok(
        self,
        adapter_class: Type[BaseGenericAdapter],
        adapter_config: Dict[str, Any],
        adapter_type: Optional[str],
    ) -> bool:
        """Check the unified adapter config (False if validate_config rejects it)."""
        validate_fn = getattr(adapter_class, "validate_config", None)
        if callable(validate_fn) and not validate_fn(adapter_config):
            logger.error("Invalid configuration for %s adapter", adapter_type)
            return False
        return True

    def _unified_policy_ok(
        self,
        policy: Optional[SecurityPolicy],
        plugin: AdapterPlugin,
        adapter_name: str,
        adapter_type: Optional[str],
    ) -> bool:
        """Check the unified adapter against the security policy."""
        if policy and not self._is_adapter_allowed(policy, plugin, adapter_type):
            logger.error(
                "Adapter '%s' (type=%s) blocked by security policy",
                adapter_name,
                adapter_type,
            )
            return False
        return True

    def _warn_unregistered_sections(self, config: Dict[str, Any]) -> None:
        """Warn for config sections with no registered adapter plugin (legacy path)."""
        core_sections = {"adapters", "server", "authentication", "logging", "port_actions"}
        for section, val in config.items():
            if section in core_sections or not isinstance(val, (dict, list)):
                continue
            if section not in self.registry._plugins:
                logger.warning(
                    "Config section '%s' present but no adapter plugin registered (import failure or typo)",
                    section,
                )

    def _create_legacy_adapters(
        self,
        active_plugins: List[AdapterPlugin],
        policy: Optional[SecurityPolicy],
        config: Dict[str, Any],
        adapters: List[BaseGenericAdapter],
    ) -> None:
        """Create one adapter per active legacy plugin; skip the plugin on error."""
        for plugin in active_plugins:
            try:
                plugin_config = config[plugin.config_section]
                adapter_class = plugin.adapter_class  # This is a BaseGenericAdapter subclass
                if not self._legacy_section_ok(plugin, adapter_class, plugin_config, policy):
                    continue

                # Create adapter instance(s) - all inherit from BaseGenericAdapter
                created_adapters = self._create_adapter_instances(plugin, plugin_config)
                adapters.extend(created_adapters)

                logger.info("Created %s adapter instance(s) for %s", len(created_adapters), plugin.name)

            except Exception as e:
                logger.error("Failed to create adapter for plugin %s: %s", plugin.name, e, exc_info=True)
                # Continue with other plugins rather than failing completely

    def _legacy_section_ok(
        self,
        plugin: AdapterPlugin,
        adapter_class: Type[BaseGenericAdapter],
        plugin_config: Any,
        policy: Optional[SecurityPolicy],
    ) -> bool:
        """Validate and policy-check one legacy adapter section.

        Raises ValueError when validation fails (the caller logs and skips the
        plugin). Returns False when the security policy blocks the section.
        """
        # Validate plugin-specific configuration
        validate_fn = getattr(adapter_class, "validate_config", None)
        if callable(validate_fn):
            # Pass the full config section dict for validation
            config_to_validate = {plugin.config_section: plugin_config}
            if not validate_fn(config_to_validate):
                # The adapter logs the reason when it has one (issue #96); the
                # caller logs this failure, so no line here (it printed the same
                # text twice per rejected section).
                raise ValueError(f"Invalid configuration for {plugin.name}")

        # Security policy enforcement (legacy sections)
        atype = self._detect_adapter_type(adapter_class)
        if policy and not self._is_adapter_allowed(policy, plugin, atype):
            logger.error(
                "Adapter section '%s' blocked by security policy",
                plugin.config_section,
            )
            return False
        return True

    def _detect_adapter_type(self, adapter_class: Type[BaseGenericAdapter]) -> Optional[str]:
        """Best-effort adapter-type id for a class; None when undetectable."""
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
        return atype

    def _fail_fast_abort(self, config: Dict[str, Any]) -> Optional[str]:
        """Build the fail-fast abort message, or None when no abort is required."""
        import_errors = self.registry.get_import_errors()
        missing = self._missing_adapter_sections(config)
        if not missing:
            return None
        detail = ", ".join(f"{k}: {v}" for k, v in import_errors.items()) or "no import error details"
        return f"Adapter fail-fast: aborting startup due to missing adapters for sections: {', '.join(missing)} | {detail}"

    def _missing_adapter_sections(self, config: Dict[str, Any]) -> List[str]:
        """Return configured sections that have no registered adapter plugin."""
        core_sections = {"adapters", "server", "authentication", "logging", "port_actions"}
        configured_sections = [s for s in config.keys() if s not in core_sections]
        produced_sections = {p.config_section for p in self.registry.get_all_plugins()}
        return [s for s in configured_sections if s not in produced_sections]

    def set_security_policy(self, policy: Optional[SecurityPolicy]) -> None:
        self.security_policy = policy

    def _is_adapter_allowed(
        self,
        policy: SecurityPolicy,
        plugin: AdapterPlugin,
        adapter_type: Optional[str],
    ) -> bool:
        return policy.is_adapter_allowed(adapter_type=(adapter_type or plugin.config_section))

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
        logger.debug("Creating adapter instance: %s (%s)", adapter_name, adapter_class.__name__)

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

        # Create and attach port manager for dynamic lifecycle management.
        # The constructor self-wires adapter.port_manager, so no binding is
        # needed here.
        DynamicPortManager(adapter)

        logger.debug("Adapter %s created with dynamic port management", adapter_name)
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
        logger.info("Registering external plugin: %s", plugin)
        self.registry.register_plugin(plugin)
