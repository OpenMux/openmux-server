"""
Utilities for interacting with PortManager.

Provides a safe, canonical way to resolve a single port by name using the
PortManager API without leaking exceptions into callers.
"""

from typing import Any, Optional


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
