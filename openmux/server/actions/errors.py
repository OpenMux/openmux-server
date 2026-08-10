"""Exceptions for Port Actions (see docs/design/port_actions.md)."""


class ActionError(Exception):
    """Base class for all port-action errors."""


class ActionValidationError(ActionError):
    """Raised when an action script or its supplied parameters are invalid."""


class PortBusyError(ActionError):
    """Raised when an action cannot obtain the port's read-write slot."""


class ActionTimeoutError(ActionError):
    """Raised when a run, or a session call within it, exceeds its timeout."""


class ActionSessionError(ActionError):
    """Raised for session I/O failures (write rejected, missing client queue, etc.)."""
