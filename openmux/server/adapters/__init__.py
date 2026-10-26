"""
OpenMux Unified Adapter System

This module provides the unified adapter system that consolidates
port_adapters and connection_adapters into a single, flexible plugin architecture.
"""

from .base_adapter import AdapterCapability, BaseGenericAdapter
from .factory import AdapterPlugin, GenericAdapterFactory, PluginRegistry
from .lifecycle import DynamicPortManager, PortLifecycleEvent, PortState

# Concrete adapter classes are re-exported lazily (PEP 562). Importing this
# package must not import adapter modules eagerly: `openmux/server/listener_session.py`
# imports the pure leaf `adapters/listener_common`, and the adapter modules import
# `listener_session` back (issue #100). An eager re-export here turns that pair into
# an import cycle. `PluginRegistry` registers built-ins through direct module
# imports (factory._register_built_in_plugins), so nothing relies on this package
# importing the adapters.
_LAZY_EXPORTS = {
    "TcpServerAdapter": (".client_listener", "TcpServerAdapter"),
    "TelnetListenerAdapter": (".telnet_listener", "TelnetListenerAdapter"),
    "TcpInitiatorAdapter": (".tcp_initiator", "TcpInitiatorAdapter"),
    "TcpInitiatorPort": (".tcp_initiator", "TcpInitiatorPort"),
    # Backward-compatibility aliases - prefer TcpInitiatorAdapter for new code.
    "OpenMuxClientAdapter": (".tcp_initiator", "TcpInitiatorAdapter"),
    "OpenMuxClientPort": (".tcp_initiator", "TcpInitiatorPort"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(target[0], __name__), target[1])


__all__ = [
    "BaseGenericAdapter",
    "AdapterCapability",
    "PortLifecycleEvent",
    "PortState",
    "DynamicPortManager",
    "GenericAdapterFactory",
    "PluginRegistry",
    "AdapterPlugin",
    "TcpServerAdapter",  # client_listener
    "TelnetListenerAdapter",
    "TcpInitiatorAdapter",
    "TcpInitiatorPort",
    "OpenMuxClientAdapter",
    "OpenMuxClientPort",
]
