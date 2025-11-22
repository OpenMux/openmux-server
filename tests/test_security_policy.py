import pytest

from openmux.server.security_policy import SecurityPolicy


def test_security_policy_defaults_allow_built_ins():
    policy = SecurityPolicy.from_mapping(None)

    assert not policy.is_config_editor_enforced()
    assert policy.is_adapter_allowed(
        module_name="openmux.server.adapters.serial",
        adapter_type="serial",
    )


def test_security_policy_enforces_writable_sections():
    policy = SecurityPolicy.from_mapping(
        {
            "config_editor": {
                "writable_sections": ["server", "logging"],
            }
        }
    )

    assert policy.is_config_editor_enforced() is True
    assert policy.is_section_writable("server") is True
    assert policy.is_section_writable("logging") is True
    assert policy.is_section_writable("serial_ports") is False
    assert policy.get_writable_sections() == {"server", "logging"}


def test_security_policy_canonicalizes_adapter_types():
    module_name = __name__
    policy = SecurityPolicy.from_mapping(
        {
            "adapters": {
                "allowed_modules": [module_name],
                "allowed_adapter_types": ["Tcp-Initiator"],
            }
        }
    )

    assert policy.is_adapter_allowed(
        module_name=module_name,
        adapter_type="tcp_initiator",
    )
    assert not policy.is_adapter_allowed(
        module_name=module_name,
        adapter_type="serial",
    )


def test_security_policy_parses_command_privilege_policy():
    policy = SecurityPolicy.from_mapping(
        {
            "command_adapter": {
                "drop_privileges": {
                    "user": "openmux",
                    "group": "tty",
                    "supplementary_groups": ["dialout", "uucp"],
                    "umask": "0o077",
                }
            }
        }
    )

    drop = policy.get_command_privilege_policy()
    assert drop.enabled is True
    assert drop.user == "openmux"
    assert drop.group == "tty"
    assert drop.supplementary_groups == {"dialout", "uucp"}
    assert drop.umask == 0o077


def test_security_policy_disables_command_privileges_when_missing():
    policy = SecurityPolicy.from_mapping({})
    drop = policy.get_command_privilege_policy()
    assert drop.enabled is False
    assert drop.user is None
