"""Loading and validation for Port Action scripts.

See docs/design/port_actions.md ("Script format", "Security / permissions").
A script is a plain Python module exposing:

- A module-level `ACTION` dict (id, name, description, timeout, params).
- `async def run(session, params, log)`.
"""

import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from openmux.server.actions.choices import Choice, normalize_choices
from openmux.server.actions.errors import ActionValidationError

logger = logging.getLogger("openmux.server.actions.registry")

_ALLOWED_TYPES = {"str", "int", "float", "bool"}
_ALLOWED_WIDGETS = {"text", "select", "radio"}


@dataclass
class ActionParam:
    """A single declared input parameter for an action script."""

    name: str
    type: str = "str"
    required: bool = True
    default: Any = None
    sensitive: bool = False
    description: str = ""
    widget: str = "text"
    choices: Optional[List[Choice]] = field(default=None)

    def __post_init__(self) -> None:
        if self.type not in _ALLOWED_TYPES:
            raise ActionValidationError(
                f"Param {self.name!r} declares unsupported type {self.type!r} " f"(expected one of {sorted(_ALLOWED_TYPES)})"
            )
        if self.widget not in _ALLOWED_WIDGETS:
            raise ActionValidationError(
                f"Param {self.name!r} declares unsupported widget {self.widget!r} "
                f"(expected one of {sorted(_ALLOWED_WIDGETS)})"
            )
        if self.widget in ("select", "radio"):
            if not self.choices:
                raise ActionValidationError(f"Param {self.name!r} widget={self.widget!r} requires a non-empty choices list")
            self.choices = normalize_choices(self.choices)


@dataclass
class ActionScript:
    """A loaded action script: its metadata plus the `run` entry point."""

    id: str
    name: str
    description: str
    params: List[ActionParam]
    run_func: Callable[..., Any]
    module_path: str
    timeout: float = 60.0

    def param(self, name: str) -> Optional[ActionParam]:
        """Return the declared `ActionParam` named `name`, or None."""
        return next((p for p in self.params if p.name == name), None)


def _forget_sibling_modules(script_path: Path) -> None:
    """Drop previously imported modules from the script's directory.

    Action scripts import sibling helper modules by bare name (for example
    ``from dabaru_api import fetch_init_config``). Python caches imported
    modules in ``sys.modules``, so a re-import of the action script would
    keep the siblings as executed at their first import. Dropping the cached
    modules makes the next import re-execute them from the current file.
    """
    directory = script_path.parent.resolve()
    for name in list(sys.modules):
        module_file = getattr(sys.modules[name], "__file__", None)
        if module_file and Path(module_file).resolve().parent == directory:
            del sys.modules[name]


def load_action_from_file(path: str) -> ActionScript:
    """Import a script file and build its `ActionScript` from `ACTION` + `run`.

    Args:
        path: Filesystem path to the action script module.

    Returns:
        The loaded `ActionScript`.

    Raises:
        ActionValidationError: the file is missing, fails to import (syntax
            error, unresolvable `import`), or lacks a valid `ACTION` dict /
            `run` entry point.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise ActionValidationError(f"Action script not found: {path}")

    _forget_sibling_modules(file_path)
    spec = importlib.util.spec_from_file_location(f"openmux_action_{file_path.stem}", file_path)
    if spec is None or spec.loader is None:
        raise ActionValidationError(f"Could not load action script: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # convert any import failure into the loader's domain error
        raise ActionValidationError(f"Could not import action script {path}: {exc}") from exc

    meta = getattr(module, "ACTION", None)
    if not isinstance(meta, dict):
        raise ActionValidationError(f"Action script {path} is missing a module-level ACTION dict")
    run_func = getattr(module, "run", None)
    if not callable(run_func):
        raise ActionValidationError(f"Action script {path} is missing an async def run(session, params, log)")

    action_id = meta.get("id") or file_path.stem
    raw_params = meta.get("params", []) or []
    params = [ActionParam(**p) if isinstance(p, dict) else p for p in raw_params]

    return ActionScript(
        id=str(action_id),
        name=str(meta.get("name", action_id)),
        description=str(meta.get("description", "")),
        params=params,
        run_func=run_func,
        module_path=str(file_path),
        timeout=float(meta.get("timeout", 60.0)),
    )


def validate_params(action: ActionScript, raw_params: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and coerce `raw_params` against `action.params`.

    Applies declared defaults and coerces values to their declared type.
    Unknown parameter names are rejected to catch typos early.

    Raises:
        ActionValidationError: an unknown, missing required, or badly-typed
            parameter was supplied.
    """
    declared = {p.name: p for p in action.params}
    unknown = set(raw_params) - set(declared)
    if unknown:
        raise ActionValidationError(f"Unknown parameter(s): {', '.join(sorted(unknown))}")

    result: Dict[str, Any] = {}
    for p in action.params:
        if p.name in raw_params:
            value = raw_params[p.name]
        elif p.required and p.default is None:
            raise ActionValidationError(f"Missing required parameter: {p.name}")
        else:
            value = p.default
        if value is not None:
            value = _coerce(p, value)
        result[p.name] = value
    return result


def _coerce(param: ActionParam, value: Any) -> Any:
    """Coerce `value` to `param.type`, raising ActionValidationError if it can't be."""
    try:
        if param.type == "str":
            return str(value)
        if param.type == "int":
            return int(value)
        if param.type == "float":
            return float(value)
        if param.type == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")
    except (TypeError, ValueError) as exc:
        raise ActionValidationError(f"Parameter {param.name!r} must be of type {param.type}") from exc
    return value


def redact_params(action: ActionScript, params: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of `params` with values of `sensitive` params replaced."""
    sensitive_names = {p.name for p in action.params if p.sensitive}
    return {k: ("<redacted>" if k in sensitive_names else v) for k, v in params.items()}
