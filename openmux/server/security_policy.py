"""Security policy helpers for OpenMux server components.

Provides a structured representation of the security configuration so that
other subsystems (adapter factory, authentication manager, config editor)
can consult a single source of truth for allowed modules, adapter types,
config-editor write permissions, and rate-limit settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Set


def _normalize_optional_str(value: Optional[Any]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_umask(value: Optional[Any]) -> Optional[int]:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            # int(x, 0) respects prefixes like 0o for octal
            parsed = int(text, 0)
        elif isinstance(value, (int, float)):
            parsed = int(value)
        else:
            return None
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed & 0o777


def _normalize_str_set(values: Optional[Iterable[Any]], *, lower: bool = False) -> Set[str]:
    if not values:
        return set()
    result: Set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        result.add(text.lower() if lower else text)
    return result


def _canonical_adapter_type(value: Optional[str]) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    if not text:
        return ""
    for ch in ("_", "-", " "):
        if ch in text:
            text = text.replace(ch, "")
    return text


@dataclass
class CommandPrivilegePolicy:
    enabled: bool = False
    user: Optional[str] = None
    group: Optional[str] = None
    supplementary_groups: Set[str] = field(default_factory=set)
    umask: Optional[int] = None


@dataclass
class SecurityPolicy:
    """In-memory representation of security.yaml.

    Attributes:
        allowed_modules: Python module dotted paths permitted for adapters.
        allowed_adapter_types: Adapter types (e.g. "serial") permitted in the
            unified adapter list.
        block_unlisted_modules: When True, adapters whose modules are not in
            ``allowed_modules`` are rejected.
        config_editor_writable_sections: Sections that the Config Editor may
            modify. Empty set => UI is read-only.
        auth_rate_limits: Mapping containing ``window_seconds``,
            ``failure_threshold``, and ``base_lock_seconds`` overrides for
            AuthManager's failure tracker.
        command_privilege_policy: Drop-to-user settings for the command adapter.
    """

    allowed_modules: Set[str] = field(default_factory=set)
    allowed_adapter_types: Set[str] = field(default_factory=set)
    block_unlisted_modules: bool = True
    config_editor_writable_sections: Set[str] = field(default_factory=set)
    config_editor_enforced: bool = False
    auth_rate_limits: Dict[str, int] = field(default_factory=dict)
    command_privilege_policy: CommandPrivilegePolicy = field(default_factory=CommandPrivilegePolicy)

    DEFAULT_ALLOWED_MODULES = {
        "openmux.server.adapters.loopback",
        "openmux.server.adapters.tcp_initiator",
        "openmux.server.adapters.serial",
        "openmux.server.adapters.command",
        "openmux.server.adapters.client_listener",
        "openmux.server.adapters.web_console",
        "openmux.server.adapters.telnet_listener",
        "openmux.server.adapters.muxcon",
        "openmux.server.adapters.web_status",
        "openmux.server.adapters.client_initiator",
    }
    DEFAULT_ALLOWED_ADAPTER_TYPES = {
        "loopback",
        "tcp_initiator",
        "serial",
        "command",
        "client_listener",
        "telnet_listener",
        "web_console",
        "muxcon",
        "web_status",
        "openmux_client",
    }
    DEFAULT_AUTH_RATE_LIMITS = {
        "window_seconds": 300,
        "failure_threshold": 5,
        "base_lock_seconds": 30,
    }

    @classmethod
    def from_mapping(cls, raw: Optional[Dict[str, Any]]) -> "SecurityPolicy":
        data = raw or {}
        adapters_cfg = (data.get("adapters") or {}) if isinstance(data, dict) else {}
        config_editor_cfg = (data.get("config_editor") or {}) if isinstance(data, dict) else {}
        rate_limits_cfg = (data.get("rate_limits") or {}) if isinstance(data, dict) else {}
        command_cfg = (data.get("command_adapter") or {}) if isinstance(data, dict) else {}

        allowed_modules = _normalize_str_set(adapters_cfg.get("allowed_modules"))
        if not allowed_modules:
            allowed_modules = set(cls.DEFAULT_ALLOWED_MODULES)

        allowed_adapter_types_raw = _normalize_str_set(
            adapters_cfg.get("allowed_adapter_types"), lower=True
        )
        if not allowed_adapter_types_raw:
            allowed_adapter_types_raw = set(cls.DEFAULT_ALLOWED_ADAPTER_TYPES)
        allowed_adapter_types = {
            canon for canon in (_canonical_adapter_type(val) for val in allowed_adapter_types_raw) if canon
        }

        block_unlisted = bool(adapters_cfg.get("block_unlisted", True))

        enforce_editor = isinstance(config_editor_cfg, dict) and ("writable_sections" in config_editor_cfg)
        writable_sections = _normalize_str_set(config_editor_cfg.get("writable_sections"))

        auth_limits_source = None
        if isinstance(rate_limits_cfg.get("authentication"), dict):
            auth_limits_source = rate_limits_cfg.get("authentication")
        elif isinstance(rate_limits_cfg.get("auth"), dict):
            auth_limits_source = rate_limits_cfg.get("auth")
        elif isinstance(rate_limits_cfg.get("auth_failures"), dict):
            auth_limits_source = rate_limits_cfg.get("auth_failures")
        auth_limits = dict(cls.DEFAULT_AUTH_RATE_LIMITS)
        if isinstance(auth_limits_source, dict):
            for key in ("window_seconds", "failure_threshold", "base_lock_seconds"):
                value = auth_limits_source.get(key)
                if isinstance(value, (int, float)):
                    auth_limits[key] = max(int(value), 1)

        command_privileges = cls._parse_command_privileges(command_cfg)

        return cls(
            allowed_modules=allowed_modules,
            allowed_adapter_types=allowed_adapter_types,
            block_unlisted_modules=block_unlisted,
            config_editor_writable_sections=writable_sections,
            config_editor_enforced=enforce_editor,
            auth_rate_limits=auth_limits,
            command_privilege_policy=command_privileges,
        )

    @staticmethod
    def _parse_command_privileges(command_cfg: Dict[str, Any]) -> CommandPrivilegePolicy:
        if not isinstance(command_cfg, dict):
            return CommandPrivilegePolicy()
        drop_cfg = command_cfg.get("drop_privileges") or {}
        if not isinstance(drop_cfg, dict):
            return CommandPrivilegePolicy()
        enabled = drop_cfg.get("enabled")
        enabled_flag = True if enabled is None else bool(enabled)
        user = _normalize_optional_str(drop_cfg.get("user"))
        group = _normalize_optional_str(drop_cfg.get("group"))
        supplementary = _normalize_str_set(drop_cfg.get("supplementary_groups"))
        umask = _parse_umask(drop_cfg.get("umask"))
        if not (user or group or supplementary or umask is not None):
            return CommandPrivilegePolicy(enabled=False)
        return CommandPrivilegePolicy(
            enabled=enabled_flag,
            user=user,
            group=group,
            supplementary_groups=supplementary,
            umask=umask,
        )

    def is_adapter_allowed(
        self,
        *,
        module_name: Optional[str],
        adapter_type: Optional[str],
    ) -> bool:
        """Return True if the adapter is permitted under this policy."""

        if self.block_unlisted_modules and module_name:
            if module_name not in self.allowed_modules:
                return False

        if adapter_type:
            canon = _canonical_adapter_type(adapter_type)
            if self.allowed_adapter_types and canon not in self.allowed_adapter_types:
                return False

        return True

    def is_section_writable(self, section: str) -> bool:
        if not self.config_editor_enforced:
            return True
        if not self.config_editor_writable_sections:
            return False
        return section in self.config_editor_writable_sections

    def get_writable_sections(self) -> Set[str]:
        return set(self.config_editor_writable_sections)

    def get_auth_rate_limits(self) -> Dict[str, int]:
        return dict(self.auth_rate_limits)

    def is_config_editor_enforced(self) -> bool:
        return self.config_editor_enforced

    def get_command_privilege_policy(self) -> CommandPrivilegePolicy:
        return self.command_privilege_policy