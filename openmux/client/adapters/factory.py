"""Factory utilities for constructing OpenMux client adapters.

Centralizes adapter type registration and creation logic so that downstream
code can request an implementation by simple type key (e.g. ``"tcp"``,
``"websocket"``). Includes helpers for configuration validation. The legacy
``create_connection`` helper has been removed; callers should use
``create_adapter`` and then explicitly invoke ``connect`` / authentication.

Adapter summaries:
    tcp:
        Traditional persistent TCP client connection speaking the native
        OpenMux protocol.
    websocket:
        Raw per‑port streaming adapter using the server-side ``web_console``
        adapter. Authentication is Basic Auth during the handshake; listing
        of ports is performed via HTTP GET ``/api/ports`` (discovery mode
        when ``port_name`` not supplied). One WebSocket maps to exactly one
        port; switching ports requires a new adapter instance.
"""

from typing import Any, Dict, Optional

from .base_adapter import BaseClientAdapter
from .tcp_adapter import TcpClientAdapter
from .websocket_adapter import WebSocketClientAdapter


class ClientAdapterFactory:
    """Create and validate concrete client adapter instances.

    Adapter classes are looked up from the ``ADAPTER_TYPES`` registry. New
    implementations can be added by extending that mapping without changing
    factory call sites.
    """

    ADAPTER_TYPES = {
        "tcp": TcpClientAdapter,
        "websocket": WebSocketClientAdapter,
        # Future adapters can be added here:
        # 'grpc': GrpcClientAdapter,
        # 'http': HttpClientAdapter,
    }

    @classmethod
    def create_adapter(
        cls,
        host: str,
        port: int,
        adapter_type: str = "tcp",
        config: Optional[Dict[str, Any]] = None,
    ) -> BaseClientAdapter:
        """Instantiate a concrete adapter.

        Args:
            host: Server hostname or IP.
            port: Server port number.
            adapter_type: Registry key selecting the adapter implementation.
            config: Optional adapter-specific configuration dictionary.

        Returns:
            BaseClientAdapter: Fully constructed adapter (not necessarily
            connected yet) ready for ``connect`` and authentication steps.

        Raises:
            ValueError: If the adapter type is unknown or construction fails.
        """
        if adapter_type not in cls.ADAPTER_TYPES:
            available_types = ", ".join(cls.ADAPTER_TYPES.keys())
            raise ValueError(f"Unknown adapter type '{adapter_type}'. " f"Available types: {available_types}")

        adapter_class = cls.ADAPTER_TYPES[adapter_type]

        try:
            return adapter_class(host, port, config)
        except Exception as e:  # justification: factory rewraps as ValueError to propagate to caller
            raise ValueError(f"Failed to create {adapter_type} adapter: {e}")

    @classmethod
    def get_supported_types(cls) -> list:
        """Return registered adapter type keys.

        Returns:
            list: Supported adapter type names (strings).
        """
        return list(cls.ADAPTER_TYPES.keys())

    @classmethod
    def validate_config(cls, adapter_type: str, config: Optional[Dict[str, Any]] = None) -> bool:
        """Validate configuration for a given adapter type.

        Instantiates an adapter with placeholder connection parameters to
        exercise constructor validation logic without performing a real
        network connection.

        Args:
            adapter_type: Registry key of adapter to probe.
            config: Candidate configuration mapping.

        Returns:
            bool: True if construction succeeded (configuration considered
            valid).

        Raises:
            ValueError: If the adapter type is unknown or construction failed.
        """
        if adapter_type not in cls.ADAPTER_TYPES:
            available_types = ", ".join(cls.ADAPTER_TYPES.keys())
            raise ValueError(f"Unknown adapter type '{adapter_type}'. " f"Available types: {available_types}")

        # Try to create adapter to validate configuration
        try:
            cls.create_adapter("localhost", 8023, adapter_type, config)
            return True
        except Exception as e:  # justification: validation intentionally rethrows as ValueError for API
            raise ValueError(f"Invalid configuration for {adapter_type} adapter: {e}")
