"""
Utilities for interacting with PortManager.

Provides a safe, canonical way to resolve a single port by name using the
PortManager API without leaking exceptions into callers.
"""

import re
from typing import Any, Optional


def natural_sort_key(s: str) -> list:
    """Return a sort key that orders embedded numbers numerically.

    Splits the string into alternating text/number segments so that
    comparisons like 'loopback2' < 'loopback10' work correctly.

    Examples:
        'loopback10' -> ['loopback', 10, '']
        'ttyUSB2'    -> ['ttyusb', 2, '']
        'SHELL'      -> ['shell']
        'rack1slot2' -> ['rack', 1, 'slot', 2, '']
    """
    segments = re.split(r"(\d+)", s)
    # re.split with a capturing group produces alternating text/digit parts:
    #   "loopback10" -> ["loopback", "10", ""]
    # Convert digit parts to int so "2" < "10" (numeric) instead of "2" > "10" (lexicographic).
    return [int(part) if part.isdigit() else part.lower() for part in segments]


def safe_get_port(port_manager: Any, port_name: Any) -> Optional[Any]:
    """Return the port object for name or None if not found/invalid.

    This prefers the PortManager.get_port API and handles common resolution
    errors, returning None instead of raising. If the given port_manager is
    None or does not expose get_port, None is returned.

    Exceptions intentionally handled:
    - KeyError, LookupError: name not present
    - ValueError: invalid name
    - AttributeError, TypeError: misconfiguration or wrong PM object
    """
    if port_manager is None:
        return None
    get_port_fn = getattr(port_manager, "get_port", None)
    if not callable(get_port_fn):
        return None
    try:
        return get_port_fn(port_name)
    except (KeyError, LookupError, ValueError, AttributeError, TypeError):
        return None


def resolve_port_connected_state(port_obj: Any) -> Optional[bool]:
    """Return the live is_connected state for a port, or None if not applicable.

    Handles both federated proxies (``is_connected`` set directly on the
    port object) and local unified-adapter ports (``is_connected`` set on
    the wrapped ``unified_port``, e.g. SerialPortWrapper/TcpInitiatorPort).
    Returns None when the underlying port type has no connection concept
    (e.g. command/loopback ports are always considered "up").
    """
    if port_obj is None:
        return None
    inner = getattr(port_obj, "unified_port", None)
    if inner is not None and hasattr(inner, "is_connected"):
        return bool(getattr(inner, "is_connected"))
    if hasattr(port_obj, "is_connected"):
        return bool(getattr(port_obj, "is_connected"))
    return None
