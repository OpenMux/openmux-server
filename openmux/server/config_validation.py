"""Runtime JSON Schema validation for OpenMux configuration files.

The authoritative draft 2020-12 schemas ship inside the package (see
``openmux/config_schema``). This module is the single place that validates a
config mapping against one of those schemas and turns jsonschema's error
tree into flat, readable ``(path, message)`` violations.

Two usage modes, mirroring the startup policy:

- Lenient (live server): ``ConfigManager`` calls the per-file helpers on
  every load. Each violation is logged as ERROR and loading continues, so a
  currently-working deployment never breaks because of a new check.
- Strict (``--check-config``, Config Editor): ``config_file_violations`` and
  ``payload_violations`` return violations for the caller to report and act
  on; nothing is printed or logged here.
"""

import logging
import os
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import yaml
from jsonschema import Draft202012Validator

from .locations import schema_file, server_schema_file

LOGGER = logging.getLogger("openmux.config")

SERVER_SCHEMA = "openmux_config_schema.yaml"
AUTH_SCHEMA = "openmux_authentication_schema.yaml"
SECURITY_SCHEMA = "openmux_security_schema.yaml"

# (path, message) pairs with an empty path meaning the top level.
ViolationPairs = List[Tuple[str, str]]


@lru_cache(maxsize=None)
def _load_schema(name: str) -> Dict[str, Any]:
    """Load and cache one schema by file name.

    The server schema honors the ``OPENMUX_CONFIG_SCHEMA`` override via
    ``locations.server_schema_file()``; the others always resolve inside the
    package. Raises OSError when the file is missing.
    """
    if name == SERVER_SCHEMA:
        path = server_schema_file()
    else:
        path = schema_file(name)
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Schema file {path} did not parse to a mapping")
    Draft202012Validator.check_schema(data)
    return data


def _format_path(segs: List[Any]) -> str:
    """Render a jsonschema error path as a dotted string (serial_ports[0])."""
    out: List[str] = []
    for seg in segs:
        if isinstance(seg, int):
            out.append(f"[{seg}]")
        else:
            out.append(f".{seg}" if out else str(seg))
    return "".join(out)


_REQUIRED_RE = re.compile(r"['\"]([^'\"]+)['\"] is a required property")


def _required_key(message: str) -> Optional[str]:
    """Extract the key from jsonschema's '... is a required property' message."""
    match = _REQUIRED_RE.search(message)
    return match.group(1) if match else None


def _summarize_anyof(context: List[Any]) -> str:
    """Readable message for a failed anyOf (ours: at-least-one-runtime-section).

    When every branch complains about a missing required key, say which
    keys satisfy the requirement instead of repeating N raw messages.
    """
    keys = [k for c in context if (k := _required_key(c.message)) is not None]
    if keys and len(keys) == len(context):
        return f"expected at least one of: {', '.join(sorted(set(keys)))}"
    return context[0].message if context else "not valid under any of the given schemas"


def _flatten_error(err: Any, prefix: Tuple[Any, ...] = ()) -> List[Tuple[Tuple[Any, ...], str]]:
    """Expand one jsonschema error into flat (path-tuple, message) pairs.

    ``anyOf`` collapses to one summarized line, ``oneOf`` keeps its own
    message (per-branch errors would be noise), other composites
    (``allOf``/``not``) recurse into their context errors.
    """
    path = prefix + tuple(err.absolute_path)
    context = getattr(err, "context", None)
    if context:
        if err.validator == "anyOf":
            return [(path, _summarize_anyof(context))]
        if err.validator == "oneOf":
            return [(path, err.message)]
        return [item for child in context for item in _flatten_error(child)]
    return [(path, err.message)]


def _collect(instance: Any, validator: Any) -> List[Tuple[Tuple[Any, ...], str]]:
    """Walk iter_errors into flat (path-tuple, message) pairs."""
    return [item for err in validator.iter_errors(instance) for item in _flatten_error(err)]


def schema_violations(instance: Any, schema_name: str) -> ViolationPairs:
    """Validate ``instance`` against ``schema_name``; return (path, message) pairs.

    Returns an empty list when the instance validates. A schema that is
    missing or malformed raises ValueError/OSError; callers that must never
    break (live start) are expected to catch that and degrade to logging.
    """
    validator = Draft202012Validator(_load_schema(schema_name))
    pairs: ViolationPairs = []
    seen = set()
    for path, message in _collect(instance, validator):
        rendered = _format_path(list(path))
        key = (rendered, message)
        if key in seen:
            continue
        seen.add(key)
        pairs.append((rendered, message))
    return pairs


def config_file_violations(path: str, schema_name: str) -> List[str]:
    """Load one config file and return rendered violations against a schema.

    Each rendered line is ``file: path: message`` (the path part is omitted
    when the violation is at the top level). A missing file, an unreadable
    file, or a YAML parse error is returned as a single rendered line rather
    than raised, so ``--check-config`` can report all files in one pass.
    """
    file = str(path)
    try:
        with open(file, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except FileNotFoundError:
        return [f"{file}: file not found"]
    except Exception as exc:
        return [f"{file}: could not parse YAML: {exc}"]
    if raw is None:
        return [f"{file}: file is empty"]
    if not isinstance(raw, dict):
        return [f"{file}: top level must be a mapping, got {type(raw).__name__}"]
    out: List[str] = []
    for rendered, message in schema_violations(raw, schema_name):
        out.append(f"{file}: {rendered}: {message}" if rendered else f"{file}: {message}")
    return out


def payload_violations(payload: Dict[str, Any]) -> List[str]:
    """Validate a Config Editor payload (server.yaml content, optional inline auth).

    The ``authentication`` section is split off and checked against the
    authentication schema; the rest is checked against the server schema,
    which does not know the inline ``authentication`` key. Rendered lines
    use the same ``path: message`` format (file prefix omitted).
    """
    lines: List[str] = []
    auth = payload.get("authentication")
    server_payload = {k: v for k, v in payload.items() if k != "authentication"}
    for rendered, message in schema_violations(server_payload, SERVER_SCHEMA):
        lines.append(f"{rendered}: {message}" if rendered else message)
    if isinstance(auth, dict):
        for rendered, message in schema_violations(auth, AUTH_SCHEMA):
            where = f"authentication.{rendered}" if rendered else "authentication"
            lines.append(f"{where}: {message}")
    return lines


def check_config_files(
    config_path: str,
    auth_config_path: Optional[str] = None,
    security_config_path: Optional[str] = None,
) -> Tuple[List[str], List[str]]:
    """Strict schema pass over the three server config files.

    Returns:
        ``(violations, problems)`` — ``violations`` are rendered schema
        violation lines (the caller should exit 1); ``problems`` are
        missing-file / unparseable-file lines (the caller should exit 2).
        An empty file parses as YAML ``None`` and is reported as a schema
        violation, not a problem.
    """
    violations: List[str] = []
    problems: List[str] = []
    for path, schema_name in (
        (config_path, SERVER_SCHEMA),
        (auth_config_path, AUTH_SCHEMA),
        (security_config_path, SECURITY_SCHEMA),
    ):
        if not path:
            continue
        if not os.path.exists(path):
            problems.append(f"{path}: file not found")
            continue
        try:
            with open(path, "r") as handle:
                instance = yaml.safe_load(handle)
        except (OSError, yaml.YAMLError) as exc:
            problems.append(f"{path}: could not be read or parsed ({exc})")
            continue
        pairs = schema_violations(instance, schema_name)
        if pairs:
            LOGGER.error("Config validation %s: %d schema violation(s)", path, len(pairs))
            for rendered, message in pairs:
                loc = f" @ {rendered}" if rendered else ""
                violations.append(f"{path}: {message}{loc}")
    return violations, problems


def check_mapping(logger: Any, instance: Any, schema_name: str, label: str) -> int:
    """Lenient live-server pass: log each violation as ERROR, never raise.

    Args:
        logger: The manager's logger (``openmux.config`` in practice).
        instance: The parsed config mapping.
        schema_name: One of the ``*_SCHEMA`` constants.
        label: Short human label for the log lines (e.g. ``server.yaml``).

    Returns:
        The number of violations logged (also 0 when logging is skipped
        because the schema itself could not be loaded).
    """
    try:
        pairs = schema_violations(instance, schema_name)
    except Exception as exc:  # justification: schema-check must never break load
        logger.warning("Skipping JSON schema check for %s: %s", label, exc)
        return 0
    for rendered, message in pairs:
        logger.error("Config validation %s%s: %s", label, f" @ {rendered}" if rendered else "", message)
    if pairs:
        logger.error("Config validation: %d schema violation(s) in %s", len(pairs), label)
    return len(pairs)
