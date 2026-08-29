"""Write-slot capacity for console ports (issue #59).

A port's ``max_read_write_users`` key holds a write-capacity mode, a
tri-value: ``none`` (0 concurrent writers), ``one`` (1, the default), or
``multiple`` (unlimited). A slot is a resource, not a privilege: the mode
caps how many writers a port holds, while access control (issue #58)
decides who is entitled to try for a slot. Admin bypasses access control,
not capacity.

Legacy integer config values are still accepted so existing configs keep
working: ``0`` maps to ``none``, ``1`` to ``one``, and any value >= 2 to
``multiple`` (exact counts had no use case). Each migrated port logs the
deprecation line once. Any other value is a hard error.
"""

import logging
import math
from typing import Any, Dict, Optional

#: Accepted write-capacity modes, in display order.
WRITE_MODES = ("none", "one", "multiple")

#: Port-level wire value sent over muxcon federation for ``multiple``.
#: Old peers receive an ordinary large integer and keep their legacy
#: >= 2 behavior, which is exactly ``multiple``.
WIRE_MULTIPLE = 2147483647

#: Port-level wire capacity for each mode (none = 0, one = 1, multiple = WIRE_MULTIPLE).
WIRE_CAPACITY: Dict[str, int] = {"none": 0, "one": 1, "multiple": WIRE_MULTIPLE}


class InvalidWriteMode(ValueError):
    """Raised when a max_read_write_users value is neither a mode nor a legacy integer."""


def parse_write_mode(raw: Any, *, port_name: Optional[str] = None, logger: Optional[logging.Logger] = None) -> str:
    """Resolve a raw ``max_read_write_users`` value to a write-capacity mode.

    Args:
        raw: Config value: a mode string, a legacy integer (0/1/>= 2), or a
            numeric string. ``None`` means the key is unset.
        port_name: Port name for log lines (may be None).
        logger: Logger for the one-time legacy deprecation line.

    Returns:
        str: One of WRITE_MODES.

    Raises:
        InvalidWriteMode: Any value that is neither a mode nor a non-negative
            integer (e.g. "two", -1, True, 2.5).
    """
    if raw is None:
        return "one"  # unset keeps the same default as before (issue #59: "default stays at the same place")
    if isinstance(raw, bool):
        raise InvalidWriteMode(f"Invalid max_read_write_users for port {port_name or 'unknown'}: {raw!r}")
    if isinstance(raw, int) and raw >= 0:
        mode = _legacy_int_to_mode(raw)
        _deprecate_legacy_int(raw, mode, port_name, logger)
        return mode
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in WRITE_MODES:
            return text
        # Numeric strings are a YAML/CLI convenience; they migrate like integers.
        try:
            value = int(text)  # "1", "2", " 0 " resolve
        except ValueError:
            value = None
        if value is not None and value >= 0:
            mode = _legacy_int_to_mode(value)
            _deprecate_legacy_int(value, mode, port_name, logger)
            return mode
    raise InvalidWriteMode(f"Invalid max_read_write_users for port {port_name or 'unknown'}: {raw!r}")


def _legacy_int_to_mode(value: int) -> str:
    """Map a legacy non-negative integer to its tri-value mode (issue #59 table)."""
    if value == 0:
        return "none"
    if value == 1:
        return "one"
    return "multiple"


def _deprecate_legacy_int(value: int, mode: str, port_name: Optional[str], logger: Optional[logging.Logger]) -> None:
    """Log the one-time deprecation line for a legacy integer capacity value."""
    if logger is None:
        return
    logger.warning(
        "Port %s: max_read_write_users=%s is a legacy integer value; "
        "it maps to '%s'. Use the tri-value 'none'/'one'/'multiple' instead.",
        port_name or "unknown",
        value,
        mode,
    )


def wire_to_mode(value: Any, *, port_name: Optional[str] = None) -> str:
    """Resolve a port-level ``max_rw_users`` wire value to a mode, without deprecation logs.

    Used by the muxcon RemotePortProxy: peers send the tri-value for local
    ports and plain wire integers (0/1/2, incl. the ``multiple`` sentinel)
    from older peers. That integer traffic is legitimate protocol, not a
    user config, so it maps silently (>= 2 is ``multiple``).
    """
    if isinstance(value, str):
        text = value.strip().lower()
        if text in WRITE_MODES:
            return text
        try:
            return _legacy_int_to_mode(int(text))
        except ValueError:
            return "one"
    if isinstance(value, bool):
        return "one"
    if isinstance(value, int) and value >= 0:
        return _legacy_int_to_mode(value)
    if isinstance(value, float) and float(value).is_integer() and value >= 0:
        return _legacy_int_to_mode(int(value))
    return "one"


def write_capacity(mode: Any) -> float:
    """Return the maximum concurrent writers for a write-capacity mode.

    Args:
        mode: A write mode string; legacy integers and numeric strings are
            tolerated (and migrate like the parser); unknown values act as
            ``one`` so a broken port never unlocks unlimited writers.

    Returns:
        float: 0.0 for ``none``, 1.0 for ``one``, math.inf for ``multiple``.
    """
    try:
        normalized = parse_write_mode(mode)
    except InvalidWriteMode:
        return 1.0
    return 0.0 if normalized == "none" else (1.0 if normalized == "one" else math.inf)


def capacity_display_label(mode: Any) -> str:
    """Return the user-facing label for a write-capacity mode."""
    try:
        return parse_write_mode(mode)
    except InvalidWriteMode:
        return str(mode)


def capacity_to_wire(mode: Any) -> int:
    """Return the port-level wire value for a write-capacity mode.

    The int is used for the muxcon federation ``max_rw_users`` field so old
    peers (integer-only) apply the same >= 2 behavior on their side.
    """
    try:
        normalized = parse_write_mode(mode)
    except InvalidWriteMode:
        return 1
    return WIRE_CAPACITY[normalized]


def capacity_from_wire(value: Any) -> float:
    """Return the concurrent-writer count (0/1/inf) for a wire or local value.

    Accepts a mode string (new peers) or a wire/legacy integer (0/1/n, unknown
    values default to ``one`` = 1). Unknown values never unlock unlimited
    writers.
    """
    return write_capacity(wire_to_mode(value))
