"""Alignment checks for muxcon defaults.

Three places must agree on what a missing muxcon option means:
  1. The runtime normalization in muxcon.py (what the server actually uses).
  2. UnifiedMuxConAdapter.validate_config (what startup validation checks).
  3. The config schema (what the Config Editor shows as defaults).

When any of them drifts, the UI displays values the server never has, or the
server silently applies a default validation did not check. These tests lock
the three together.
"""

from pathlib import Path

import pytest
import yaml

from openmux.server.adapters.muxcon import UnifiedMuxConAdapter

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATHS = (
    REPO_ROOT / "config_schema" / "openmux_config_schema.yaml",
    REPO_ROOT / "docs" / "to_check" / "openmux_config_schema.yaml",
)


def _muxcon_schema(path: Path) -> dict:
    return yaml.safe_load(path.read_text())["properties"]["muxcon"]


def test_validate_config_listener_tls_default_matches_normalize():
    """A listener that enables no cert and disables autogen must be rejected.

    Runtime normalization defaults use_tls to True; the validator must apply
    the same effective setting. With the old False default here this error
    could never fire, and a bare listener silently meant plaintext until
    normalize flipped it to TLS.
    """
    with pytest.raises(ValueError):
        UnifiedMuxConAdapter.validate_config({"muxcon": {"listeners": [{"port": 1, "tls_autogen": False}]}})
    # Bare listener passes: autogen is on by default and covers the missing cert.
    assert UnifiedMuxConAdapter.validate_config({"muxcon": {"listeners": [{"port": 1}]}}) is True
    # Explicit plaintext is still allowed.
    assert UnifiedMuxConAdapter.validate_config({"muxcon": {"listeners": [{"port": 1, "use_tls": False}]}}) is True


def test_normalize_listener_defaults_are_safe():
    conf = UnifiedMuxConAdapter._normalize_listener_conf({"port": 1})
    assert conf["enabled"] is True
    assert conf["host"] == "0.0.0.0"
    assert conf["use_tls"] is True
    assert conf["tls_autogen"] is True
    assert conf["require_client_cert"] is False
    assert conf["tls_dir"] == "~/.openmux/muxcon"


@pytest.mark.parametrize("schema_path", SCHEMA_PATHS, ids=["config_schema", "docs/to_check"])
def test_schema_muxcon_defaults_match_runtime(schema_path: Path):
    props = _muxcon_schema(schema_path)["properties"]
    li = props["listeners"]["items"]["properties"]
    ini = props["initiators"]["items"]["properties"]
    # Every schema default below must equal the runtime default in muxcon.py.
    assert props["auth_required"]["default"] is True
    assert li["enabled"]["default"] is True
    assert li["host"]["default"] == "0.0.0.0"
    assert li["port"]["default"] == 7822
    assert li["use_tls"]["default"] is True
    assert li["tls_autogen"]["default"] is True
    assert li["require_client_cert"]["default"] is False
    assert li["tls_dir"]["default"] == "~/.openmux/muxcon"
    assert ini["port"]["default"] == 7822
    assert ini["use_tls"]["default"] is True
    assert ini["ssl_verify"]["default"] is True
    assert ini["tls_tofu"]["default"] is True


@pytest.mark.parametrize("schema_path", SCHEMA_PATHS, ids=["config_schema", "docs/to_check"])
def test_schema_muxcon_initiators_present_and_single_tags(schema_path: Path):
    mx = _muxcon_schema(schema_path)
    # 'initiators' must exist: the muxcon object sets additionalProperties:
    # false, so an absent 'initiators' key rejects every real config that
    # dials a peer.
    assert "initiators" in mx["properties"]
    assert mx["properties"]["initiators"]["items"]["required"] == ["host", "port"]
    # Listener 'tags' must be defined exactly once and allow tag keys
    # (a duplicated key made the strict definition win and rejected all tags).
    assert schema_path.read_text()[schema_path.read_text().index("muxcon:") :].count("tags:") == 1
    tags = mx["properties"]["listeners"]["items"]["properties"]["tags"]
    assert tags["additionalProperties"] is True


def test_schema_copies_muxcon_sections_in_sync():
    assert _muxcon_schema(SCHEMA_PATHS[0]) == _muxcon_schema(SCHEMA_PATHS[1])
