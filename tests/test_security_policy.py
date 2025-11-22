import pytest

from openmux.server.security_policy import SecurityPolicy


def test_security_policy_defaults_allow_built_ins():
    policy = SecurityPolicy.from_mapping(None)

    assert "serial_ports" in policy.allowed_sections
    assert not policy.is_config_editor_enforced()
    assert policy.is_adapter_allowed(
        module_name="openmux.server.adapters.serial",
        config_section="serial_ports",
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
                "allowed_sections": ["dummy_section"],
                "allowed_adapter_types": ["Tcp-Initiator"],
            }
        }
    )

    assert policy.is_adapter_allowed(
        module_name=module_name,
        config_section="dummy_section",
        adapter_type="tcp_initiator",
    )
    assert not policy.is_adapter_allowed(
        module_name=module_name,
        config_section="blocked_section",
        adapter_type="tcp_initiator",
    )
