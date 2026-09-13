"""Regression tests for issue #78: config editor save must not drop schema-valid keys
the UI does not model.

The fix is server-side: ``_merge_preserve_unmodelled`` re-adds a small, explicit
allowlist of known-unmodelled keys (``_PRESERVE_UNMODELLED_KEYS``) before the
apply path writes the payload to ``server.yaml``. These tests cover the merge
rule and the allowlist contract.
"""

import pytest

from openmux.server.web_plugins.config_editor import (
    _PRESERVE_UNMODELLED_KEYS,
    _merge_preserve_unmodelled,
)


def test_preserve_auth_private_key_path_when_ui_does_not_send_it():
    """A schema-valid key the UI does not render must survive a save."""
    payload = {"muxcon": {"auth_required": True, "auth_key_id": "k1"}}
    current = {
        "muxcon": {
            "auth_required": True,
            "auth_key_id": "k1",
            "auth_private_key_path": "/etc/openmux/key.pem",
            "advertise_filters": {"include": ["console_*"]},
            "accept_filters": {"server_include": ["hub-01"]},
        }
    }
    _merge_preserve_unmodelled(payload, current)
    assert payload["muxcon"]["auth_private_key_path"] == "/etc/openmux/key.pem"


def test_preserve_web_console_sso_keys_when_ui_does_not_send_them():
    payload = {"web_console": {"host": "0.0.0.0", "port": 8081}}
    current = {
        "web_console": {
            "host": "0.0.0.0",
            "port": 8081,
            "hardware_info_file": "/etc/openmux-hardware",
            "sso_trust_header": "X-OMX-SSO",
            "sso_secret": "topsecret",
            "sso_max_skew_sec": 120,
        }
    }
    _merge_preserve_unmodelled(payload, current)
    assert payload["web_console"]["hardware_info_file"] == "/etc/openmux-hardware"
    assert payload["web_console"]["sso_trust_header"] == "X-OMX-SSO"
    assert payload["web_console"]["sso_secret"] == "topsecret"
    assert payload["web_console"]["sso_max_skew_sec"] == 120


def test_cleared_preserved_key_is_not_overwritten():
    """A value present in the payload (even an empty string) is the UI's choice.
    The merge must not clobber it with the stored value; an empty string is a
    deliberate clear and stays as-is."""
    payload = {"muxcon": {"auth_required": True, "auth_private_key_path": ""}}
    current = {"muxcon": {"auth_required": True, "auth_private_key_path": "/old/path"}}
    _merge_preserve_unmodelled(payload, current)
    assert payload["muxcon"]["auth_private_key_path"] == ""


def test_preserve_does_not_touch_sections_not_sent():
    """A section absent from the payload is not touched (deletion stays a deletion)."""
    payload = {"web_console": {"host": "0.0.0.0"}}
    # muxcon is in current but not in payload — it should NOT be resurrected.
    current = {
        "muxcon": {"auth_private_key_path": "/etc/openmux/key.pem"},
        "web_console": {"host": "0.0.0.0"},
    }
    _merge_preserve_unmodelled(payload, current)
    assert "muxcon" not in payload


def test_preserve_does_not_overwrite_value_ui_provided():
    """If the UI sends a value for a preserved key, the UI's value wins."""
    payload = {"muxcon": {"auth_required": True, "auth_private_key_path": "/new/path"}}
    current = {"muxcon": {"auth_required": True, "auth_private_key_path": "/old/path"}}
    _merge_preserve_unmodelled(payload, current)
    assert payload["muxcon"]["auth_private_key_path"] == "/new/path"


def test_preserve_skips_if_current_is_not_dict():
    payload = {"muxcon": {"auth_required": True}}
    _merge_preserve_unmodelled(payload, None)
    _merge_preserve_unmodelled(payload, "not a dict")
    # No crash, no change
    assert "auth_private_key_path" not in payload["muxcon"]


def test_preserve_skips_if_payload_section_is_not_dict():
    payload = {"muxcon": "invalid"}
    current = {"muxcon": {"auth_private_key_path": "/etc/openmux/key.pem"}}
    _merge_preserve_unmodelled(payload, current)
    assert payload["muxcon"] == "invalid"


def test_every_preserved_key_is_schema_valid():
    """Guard: every preserved key must be an allowed property of its section in
    the shipped schema. Persisting a schema-invalid key would break the next
    config load, which the 'preserve, don't lose data' goal must not cause."""
    import yaml

    from openmux.server.locations import server_schema_file

    path = server_schema_file()
    schema = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    props = schema.get("properties", {})
    for section, keys in _PRESERVE_UNMODELLED_KEYS.items():
        section_schema = props.get(section, {})
        for key in keys:
            # A key declared in the section's `properties` is schema-valid for
            # both `additionalProperties: true` and `false` sections. Persisting
            # one that is not declared would break the next config load.
            assert key in section_schema.get("properties", {}), f"preserved key '{key}' is not declared on section '{section}'"


def test_advertise_filters_set_by_ui_is_not_preserved():
    """Once the UI renders advertise_filters, it is a modelled field and must
    NOT be in the preserve set (so clearing it from the UI stays a deletion).

    This test verifies the allowlist does not include the filter keys.
    """
    muxcon_keys = set(_PRESERVE_UNMODELLED_KEYS.get("muxcon", ()))
    assert "advertise_filters" not in muxcon_keys
    assert "accept_filters" not in muxcon_keys


def test_is_idempotent():
    """Running the merge twice is a no-op after the first pass."""
    payload = {"muxcon": {"auth_required": True}}
    current = {"muxcon": {"auth_required": True, "auth_private_key_path": "/etc/openmux/key.pem"}}
    _merge_preserve_unmodelled(payload, current)
    first = dict(payload["muxcon"])
    _merge_preserve_unmodelled(payload, current)
    assert payload["muxcon"] == first


def test_does_not_mutate_current():
    """`current` is the read-only source; the merge must not write into it."""
    payload = {"muxcon": {"auth_required": True}}
    current = {"muxcon": {"auth_required": True, "auth_private_key_path": "/etc/openmux/key.pem"}}
    _merge_preserve_unmodelled(payload, current)
    assert current["muxcon"]["auth_private_key_path"] == "/etc/openmux/key.pem"
    assert "auth_private_key_path" in payload["muxcon"]


@pytest.mark.parametrize("bad", [None, "not a dict", 42, ["a", "b"]])
def test_tolerates_unexpected_current_shape(bad):
    """A malformed `current` must not raise and must leave the payload untouched."""
    payload = {"muxcon": {"auth_required": True}, "web_console": {"host": "1"}}
    _merge_preserve_unmodelled(payload, bad)
    assert payload == {"muxcon": {"auth_required": True}, "web_console": {"host": "1"}}


def test_list_valued_ui_tables_are_not_preserved():
    """Preserved keys must be non-list keys. A list-valued key is a UI table the
    user clears by sending none; preserving it would make deletion impossible."""
    list_keys = {"listeners", "initiators", "public_keys", "users", "api_keys"}
    for keys in _PRESERVE_UNMODELLED_KEYS.values():
        for key in keys:
            assert key not in list_keys, f"list-valued table key '{key}' must not be preserved"
