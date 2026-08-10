"""Port Actions: scripted automation against a port.

See docs/design/port_actions.md for the full design. This package implements
rollout Phase 1: script format + expect-style session wrapper + an in-process
runner that reuses the port's existing read-write slot for locking.
"""

from openmux.server.actions.errors import (
    ActionError,
    ActionSessionError,
    ActionTimeoutError,
    ActionValidationError,
    PortBusyError,
)
from openmux.server.actions.registry import ActionParam, ActionScript, load_action_from_file, validate_params
from openmux.server.actions.runner import ActionRun, ActionRunner
from openmux.server.actions.session import ActionSession

__all__ = [
    "ActionError",
    "ActionSessionError",
    "ActionTimeoutError",
    "ActionValidationError",
    "PortBusyError",
    "ActionParam",
    "ActionScript",
    "load_action_from_file",
    "validate_params",
    "ActionRun",
    "ActionRunner",
    "ActionSession",
]
