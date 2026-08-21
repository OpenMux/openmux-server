from openmux.server.web_plugins.config_editor import (
    _SECRET_MASK,
    _mask_config_secrets,
    _restore_masked_secrets,
)


def _sample_config():
    return {
        "authentication": {
            "users": [
                {"username": "alice", "password_hash": "a" * 64, "permissions": "admin"},
                {"username": "bob", "password_hash": "", "permissions": "read-only"},
            ],
            "api_keys": [
                {"name": "ci", "key": "topsecretkey", "permissions": "read-write"},
            ],
        },
        "tcp_initiator_ports": [
            {
                "name": "leaf1",
                "protocol": {"type": "openmux", "password": "hunter2", "api_key": "leafkey"},
            }
        ],
        "openmux_client_ports": [
            {"name": "legacy1", "password": "hunter3", "api_key": "legacykey"},
        ],
    }


def test_mask_config_secrets_hides_password_hash_and_keys():
    masked = _mask_config_secrets(_sample_config())

    assert masked["authentication"]["users"][0]["password_hash"] == _SECRET_MASK
    # Empty password hash is left as-is (nothing to hide)
    assert masked["authentication"]["users"][1]["password_hash"] == ""
    assert masked["authentication"]["api_keys"][0]["key"] == _SECRET_MASK
    assert masked["tcp_initiator_ports"][0]["protocol"]["password"] == _SECRET_MASK
    assert masked["tcp_initiator_ports"][0]["protocol"]["api_key"] == _SECRET_MASK
    assert masked["openmux_client_ports"][0]["password"] == _SECRET_MASK
    assert masked["openmux_client_ports"][0]["api_key"] == _SECRET_MASK


def test_mask_config_secrets_does_not_mutate_original():
    original = _sample_config()
    _mask_config_secrets(original)

    assert original["authentication"]["users"][0]["password_hash"] == "a" * 64
    assert original["tcp_initiator_ports"][0]["protocol"]["password"] == "hunter2"


def test_restore_masked_secrets_keeps_stored_value_when_unchanged():
    current = _sample_config()
    payload = _mask_config_secrets(current)

    _restore_masked_secrets(payload, current)

    assert payload["authentication"]["users"][0]["password_hash"] == "a" * 64
    assert payload["authentication"]["api_keys"][0]["key"] == "topsecretkey"
    assert payload["tcp_initiator_ports"][0]["protocol"]["password"] == "hunter2"
    assert payload["tcp_initiator_ports"][0]["protocol"]["api_key"] == "leafkey"
    assert payload["openmux_client_ports"][0]["password"] == "hunter3"
    assert payload["openmux_client_ports"][0]["api_key"] == "legacykey"


def test_restore_masked_secrets_preserves_explicit_edits():
    current = _sample_config()
    payload = _mask_config_secrets(current)
    payload["authentication"]["users"][0]["password_hash"] = "b" * 64
    payload["tcp_initiator_ports"][0]["protocol"]["password"] = "newpass"

    _restore_masked_secrets(payload, current)

    assert payload["authentication"]["users"][0]["password_hash"] == "b" * 64
    assert payload["tcp_initiator_ports"][0]["protocol"]["password"] == "newpass"


def test_restore_masked_secrets_clears_mask_for_new_entries():
    current = _sample_config()
    payload = _mask_config_secrets(current)
    payload["authentication"]["users"].append({"username": "carol", "password_hash": _SECRET_MASK, "permissions": "read-only"})

    _restore_masked_secrets(payload, current)

    new_user = payload["authentication"]["users"][-1]
    assert new_user["password_hash"] == ""
