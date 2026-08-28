import pytest

from openmux.server.security_policy import SecurityPolicy, SecurityPolicyError


def test_security_policy_defaults_allow_everything():
    policy = SecurityPolicy.from_mapping(None)

    assert policy.is_adapter_allowed(adapter_type="serial")
    assert policy.is_adapter_allowed(adapter_type="tcp_initiator")
    assert policy.is_section_writable("server")
    assert policy.is_section_writable("logging")


def test_security_policy_writable_sections_allow_disable():
    policy = SecurityPolicy.from_mapping(
        {
            "config_editor": {
                "allowed": ["server", "logging", "serial_ports"],
                "disabled": ["serial_ports"],
            }
        }
    )

    assert policy.is_section_writable("server") is True
    assert policy.is_section_writable("logging") is True
    assert policy.is_section_writable("serial_ports") is False
    assert policy.get_writable_sections() == {"server", "logging"}


def test_security_policy_fully_read_only_config_editor():
    policy = SecurityPolicy.from_mapping(
        {
            "config_editor": {
                "allowed": ["*"],
                "disabled": ["*"],
            }
        }
    )

    assert policy.get_writable_sections() == set()
    assert policy.is_section_writable("server") is False


def test_security_policy_canonicalizes_adapter_types():
    policy = SecurityPolicy.from_mapping(
        {
            "adapters": {
                "allowed": ["Tcp-Initiator"],
            }
        }
    )

    assert policy.is_adapter_allowed(adapter_type="tcp_initiator")
    assert not policy.is_adapter_allowed(adapter_type="serial")


def test_security_policy_wildcard_minus_disabled():
    policy = SecurityPolicy.from_mapping(
        {
            "adapters": {
                "allowed": ["*"],
                "disabled": ["telnet_listener"],
            }
        }
    )

    assert policy.is_adapter_allowed(adapter_type="serial")
    assert not policy.is_adapter_allowed(adapter_type="telnet_listener")


def test_security_policy_rate_limit_overrides():
    policy = SecurityPolicy.from_mapping(
        {
            "rate_limits": {
                "authentication": {
                    "window_seconds": 60,
                    "failure_threshold": 3,
                    "base_lock_seconds": 10,
                }
            }
        }
    )

    assert policy.get_auth_rate_limits() == {
        "window_seconds": 60,
        "failure_threshold": 3,
        "base_lock_seconds": 10,
    }


def test_security_policy_rejects_unknown_top_level_key():
    with pytest.raises(SecurityPolicyError):
        SecurityPolicy.from_mapping({"command_adapter": {}})


def test_security_policy_rejects_unknown_adapters_key():
    with pytest.raises(SecurityPolicyError):
        SecurityPolicy.from_mapping({"adapters": {"allowed_modules": []}})


def test_security_policy_rejects_unknown_adapter_type_value():
    with pytest.raises(SecurityPolicyError):
        SecurityPolicy.from_mapping({"adapters": {"allowed": ["not_a_real_adapter"]}})


def test_security_policy_rejects_unknown_config_editor_section_value():
    with pytest.raises(SecurityPolicyError):
        SecurityPolicy.from_mapping({"config_editor": {"allowed": ["not_a_real_section"]}})


def test_security_policy_rejects_non_list_allowed():
    with pytest.raises(SecurityPolicyError):
        SecurityPolicy.from_mapping({"adapters": {"allowed": "serial"}})


# ---------------------------------------------------------------------------
# access_default (issue #58, part 2)


def test_security_policy_access_default_defaults_to_allow():
    assert SecurityPolicy.from_mapping(None).get_access_default() == "allow"
    assert SecurityPolicy.from_mapping({}).get_access_default() == "allow"


def test_security_policy_access_default_accepts_deny():
    assert SecurityPolicy.from_mapping({"access_default": "deny"}).get_access_default() == "deny"
    assert SecurityPolicy.from_mapping({"access_default": "  deny "}).get_access_default() == "deny"
    assert SecurityPolicy.from_mapping({"access_default": "allow"}).get_access_default() == "allow"


def test_security_policy_access_default_rejects_invalid_values():
    for bad in ("AllowAll", "allow-all", "yes", 1, 0, ["allow"], True):
        with pytest.raises(SecurityPolicyError):
            SecurityPolicy.from_mapping({"access_default": bad})
