import textwrap

from openmux.server.config_manager import ConfigManager
from openmux.server.web_plugins.config_editor import (
    _SECRET_MASK,
    _mask_config_secrets,
    _prepare_payload_for_save,
    _restore_masked_secrets,
    _validate_payload,
)


def _manager_with_user(tmp_path) -> ConfigManager:
    (tmp_path / "authentication.yaml").write_text(
        textwrap.dedent(
            """
            users:
              - username: noc
                password_hash: 3b39ff7cb93b734108faa0db8904d9507f973b27b3b0e355fc94b211c852d50e
                permissions: admin
                groups: []
            """
        )
    )
    config_file = tmp_path / "server.yaml"
    config_file.write_text(
        textwrap.dedent(
            """
            server:
              id: test
            logging:
              level: WARNING
            loopback_ports:
              - name: lb1
            """
        )
    )
    cm = ConfigManager(str(config_file))
    cm.load_config()
    return cm


def test_validate_payload_rejects_masked_secret_sentinel(tmp_path):
    """Pin the regression: a masked payload (what the editor's browser holds)
    fails the `password_hash` pattern unless routed through the save-prep
    that restores the stored value. Without that step the Validate button
    always reports the same error no matter what the admin typed."""
    cm = _manager_with_user(tmp_path)
    payload = _mask_config_secrets(cm.config)
    assert payload["authentication"]["users"][0]["password_hash"] == _SECRET_MASK

    # Raw masked payload must fail — this is what the old `/validate`
    # endpoint was validating and what made the button useless.
    ok, err, _ = _validate_payload(payload, cm)
    assert ok is False
    assert "does not match" in err
    assert "password_hash" in err


def test_prepare_payload_for_save_makes_masked_payload_validate(tmp_path):
    """The /validate and /apply paths must judge the same mapping. After the
    shared save-prep, a masked sentinel is restored to the stored hash and
    the payload passes the schema."""
    cm = _manager_with_user(tmp_path)
    stored_hash = cm.config["authentication"]["users"][0]["password_hash"]

    payload = _mask_config_secrets(cm.config)
    prepared = _prepare_payload_for_save(payload, cm)

    assert prepared is payload  # in-place mutation, no copy
    assert prepared["authentication"]["users"][0]["password_hash"] == stored_hash

    ok, err, _ = _validate_payload(prepared, cm)
    assert ok is True, err


def test_prepare_payload_for_save_keeps_user_edits(tmp_path):
    """A password the user actually replaces in the UI must not be reverted
    back to the stored value by the prepare step."""
    cm = _manager_with_user(tmp_path)
    payload = _mask_config_secrets(cm.config)
    payload["authentication"]["users"][0]["password_hash"] = "f" * 64

    _prepare_payload_for_save(payload, cm)

    assert payload["authentication"]["users"][0]["password_hash"] == "f" * 64


def test_prepare_payload_for_save_drops_access_default(tmp_path):
    cm = _manager_with_user(tmp_path)
    payload = _mask_config_secrets(cm.config)
    payload["access_default"] = "deny"

    _prepare_payload_for_save(payload, cm)

    assert "access_default" not in payload


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
    }


def test_mask_config_secrets_hides_password_hash_and_keys():
    masked = _mask_config_secrets(_sample_config())

    assert masked["authentication"]["users"][0]["password_hash"] == _SECRET_MASK
    # Empty password hash is left as-is (nothing to hide)
    assert masked["authentication"]["users"][1]["password_hash"] == ""
    assert masked["authentication"]["api_keys"][0]["key"] == _SECRET_MASK
    assert masked["tcp_initiator_ports"][0]["protocol"]["password"] == _SECRET_MASK
    assert masked["tcp_initiator_ports"][0]["protocol"]["api_key"] == _SECRET_MASK


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
