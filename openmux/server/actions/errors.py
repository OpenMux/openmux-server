"""Exceptions for Port Actions (see docs/design/port_actions.md)."""

from typing import Optional


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


class ActionPowerError(ActionError):
    """Raised when an action script's power call is refused, out of scope, or fails.

    `ref` carries the outlet ref the call refused (when one ref applies), so a
    script can point at it without parsing the message (issue #93).
    """

    def __init__(self, message: str, ref: Optional[str] = None):
        super().__init__(message)
        self.ref = ref
