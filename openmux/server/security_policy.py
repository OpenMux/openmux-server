"""Security policy helpers for OpenMux server components.

Provides a structured representation of `security.yaml` so that other
subsystems (adapter factory, authentication manager, config editor) can
consult a single source of truth for which adapter types may run, which
Config Editor sections are writable, and authentication rate-limit
overrides.

Schema (three top-level blocks, each optional):

    adapters:
      allowed: ["*"]            # adapter type names, or "*" for all known types
      disabled: []              # subtracted from `allowed`; always wins on overlap

    config_editor:
      allowed: ["*"]            # config section names, or "*" for all known sections
      disabled: []              # subtracted from `allowed`; always wins on overlap

    rate_limits:
      authentication:
        window_seconds: 300
        failure_threshold: 5
        base_lock_seconds: 30

Resolution rule (applies identically to both `adapters` and `config_editor`):
the effective set is always ``resolve(allowed) - resolve(disabled)`` (plain
set difference), where ``"*"`` resolves to every known name for that block.
Omitting `allowed` defaults to `["*"]`; omitting `disabled` defaults to `[]`.
There is no other special-cased behavior - e.g. a fully read-only Config
Editor is simply `{"allowed": ["*"], "disabled": ["*"]}`.

Unknown top-level keys, unknown keys within a block, and unknown values
inside an `allowed`/`disabled` list all raise `SecurityPolicyError` - a
misspelled key must never be silently ignored in a security file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Set

# Adapter-type names as returned by each adapter's get_adapter_type(), which is not
# consistently underscore_separated (e.g. "WebConsole", "WebStatus") - so this
# normalization strips separators in addition to lowercasing.
_KNOWN_ADAPTER_TYPES: FrozenSet[str] = frozenset(
    {
        "loopback",
        "tcpinitiator",
        "serial",
        "command",
        "clientlistener",
        "webconsole",
        "muxcon",
        "webstatus",
        "openmuxclient",
        "telnetlistener",
        "sshlistener",
    }
)

# Config Editor top-level section keys are always literal, already-underscored
# dict keys from server.yaml - plain lowercasing is enough here.
_KNOWN_CONFIG_SECTIONS: FrozenSet[str] = frozenset(
    {
        "server",
        "authentication",
        "logging",
        "loopback_ports",
        "serial_ports",
        "command_ports",
        "tcp_initiator_ports",
        "openmux_client_ports",
        "client_listener",
        "telnet_listener",
        "ssh_listener",
        "muxcon",
        "web_console",
        "web_status",
        "port_actions",
    }
)

_TOP_LEVEL_ALLOWED_KEYS = {"adapters", "config_editor", "rate_limits"}
_ALLOW_DENY_KEYS = {"allowed", "disabled"}
_RATE_LIMITS_ALLOWED_KEYS = {"authentication"}
_AUTH_RATE_LIMIT_KEYS = {"window_seconds", "failure_threshold", "base_lock_seconds"}

_DEFAULT_AUTH_RATE_LIMITS = {
    "window_seconds": 300,
    "failure_threshold": 5,
    "base_lock_seconds": 30,
}


class SecurityPolicyError(ValueError):
    """Raised when security.yaml is structurally invalid.

    Covers unknown/misspelled top-level or nested keys, wrong value types,
    and unknown entries inside an `allowed`/`disabled` list. Callers decide
    whether this aborts startup or aborts a hot-reload while keeping the
    last-known-good policy (see main.py `_refresh_security_policy`).
    """


def _canonical_adapter_type(value: Any) -> str:
    text = str(value).strip().lower()
    for ch in ("_", "-", " "):
        text = text.replace(ch, "")
    return text


def _canonical_section_name(value: Any) -> str:
    return str(value).strip().lower()


def _resolve_allow_deny_list(
    block_name: str,
    block_cfg: Dict[str, Any],
    known_universe: FrozenSet[str],
    normalize,
) -> Set[str]:
    """Compute ``resolve(allowed) - resolve(disabled)`` for one policy block."""

    def _resolve_one(key: str, default: List[str]) -> Set[str]:
        raw = block_cfg.get(key, default)
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise SecurityPolicyError(f"'{block_name}.{key}' must be a list")
        normalized = [normalize(v) for v in raw]
        if "*" in normalized:
            return set(known_universe)
        unknown = sorted({v for v in normalized if v not in known_universe})
        if unknown:
            raise SecurityPolicyError(f"'{block_name}.{key}' contains unknown value(s): {', '.join(unknown)}")
        return set(normalized)

    allowed = _resolve_one("allowed", ["*"])
    disabled = _resolve_one("disabled", [])
    return allowed - disabled


def _validated_block(data: Dict[str, Any], block_name: str, allowed_keys: Set[str]) -> Dict[str, Any]:
    """Return `data[block_name]` as a dict, validated against `allowed_keys`."""

    block_cfg = data.get(block_name) or {}
    if not isinstance(block_cfg, dict):
        raise SecurityPolicyError(f"'{block_name}' must be a mapping")
    unknown = sorted(set(block_cfg.keys()) - allowed_keys)
    if unknown:
        raise SecurityPolicyError(f"Unknown key(s) under '{block_name}': {', '.join(unknown)}")
    return block_cfg


def _parse_auth_rate_limits(rate_limits_cfg: Dict[str, Any]) -> Dict[str, int]:
    auth_limits_cfg = _validated_block(rate_limits_cfg, "authentication", _AUTH_RATE_LIMIT_KEYS)
    auth_limits = dict(_DEFAULT_AUTH_RATE_LIMITS)
    for key in _AUTH_RATE_LIMIT_KEYS:
        if key not in auth_limits_cfg:
            continue
        value = auth_limits_cfg[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise SecurityPolicyError(f"'rate_limits.authentication.{key}' must be a number")
        auth_limits[key] = max(int(value), 1)
    return auth_limits


@dataclass
class SecurityPolicy:
    """In-memory representation of security.yaml.

    Attributes:
        allowed_adapter_types: Canonicalized adapter-type names permitted in
            the unified adapter list (already `allowed - disabled`).
        config_editor_writable_sections: Config Editor section names that may
            be written (already `allowed - disabled`).
        auth_rate_limits: Mapping containing ``window_seconds``,
            ``failure_threshold``, and ``base_lock_seconds`` overrides for
            AuthManager's failure tracker.
    """

    allowed_adapter_types: Set[str] = field(default_factory=set)
    config_editor_writable_sections: Set[str] = field(default_factory=set)
    auth_rate_limits: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Optional[Dict[str, Any]]) -> "SecurityPolicy":
        data = raw or {}
        if not isinstance(data, dict):
            raise SecurityPolicyError("security.yaml must be a mapping at the top level")

        unknown_top = sorted(set(data.keys()) - _TOP_LEVEL_ALLOWED_KEYS)
        if unknown_top:
            raise SecurityPolicyError(f"Unknown top-level key(s) in security.yaml: {', '.join(unknown_top)}")

        adapters_cfg = _validated_block(data, "adapters", _ALLOW_DENY_KEYS)
        allowed_adapter_types = _resolve_allow_deny_list(
            "adapters", adapters_cfg, _KNOWN_ADAPTER_TYPES, _canonical_adapter_type
        )

        config_editor_cfg = _validated_block(data, "config_editor", _ALLOW_DENY_KEYS)
        writable_sections = _resolve_allow_deny_list(
            "config_editor", config_editor_cfg, _KNOWN_CONFIG_SECTIONS, _canonical_section_name
        )

        rate_limits_cfg = _validated_block(data, "rate_limits", _RATE_LIMITS_ALLOWED_KEYS)
        auth_limits = _parse_auth_rate_limits(rate_limits_cfg)

        return cls(
            allowed_adapter_types=allowed_adapter_types,
            config_editor_writable_sections=writable_sections,
            auth_rate_limits=auth_limits,
        )

    def is_adapter_allowed(self, *, adapter_type: Optional[str]) -> bool:
        """Return True if the given adapter type is permitted under this policy."""

        if not adapter_type:
            return False
        return _canonical_adapter_type(adapter_type) in self.allowed_adapter_types

    def is_section_writable(self, section: str) -> bool:
        return _canonical_section_name(section) in self.config_editor_writable_sections

    def get_writable_sections(self) -> Set[str]:
        return set(self.config_editor_writable_sections)

    def get_auth_rate_limits(self) -> Dict[str, int]:
        return dict(self.auth_rate_limits)
