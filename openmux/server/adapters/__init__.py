"""
OpenMux Unified Adapter System

This module provides the unified adapter system that consolidates
port_adapters and connection_adapters into a single, flexible plugin architecture.
"""

from .base_adapter import AdapterCapability, BaseGenericAdapter
from .client_listener import TcpServerAdapter
from .telnet_listener import TelnetListenerAdapter
from .factory import AdapterPlugin, GenericAdapterFactory, PluginRegistry
from .lifecycle import DynamicPortManager, PortLifecycleEvent, PortState
from .tcp_initiator import TcpInitiatorAdapter, TcpInitiatorPort

# Backward-compatibility aliases — prefer TcpInitiatorAdapter for new code
OpenMuxClientAdapter = TcpInitiatorAdapter
OpenMuxClientPort = TcpInitiatorPort

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
