"""Tests for the authoritative runtime schema validation (see --check-config).

Covers the single validator module (openmux/server/config_validation.py), the
lenient log-only wiring in ConfigManager, the packaged schema resolution
(locations.server_schema_file), and the --check-config CLI early path.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from openmux.server import config_validation
from openmux.server.config_validation import (
    AUTH_SCHEMA,
    SECURITY_SCHEMA,
    SERVER_SCHEMA,
    check_config_files,
    check_mapping,
    config_file_violations,
    payload_violations,
    schema_violations,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"
SERVER_SCHEMA_YAML = REPO_ROOT / "openmux" / "config_schema" / "openmux_config_schema.yaml"


def _server_schema() -> dict:
    return yaml.safe_load(SERVER_SCHEMA_YAML.read_text())


def _valid_server_config() -> dict:
    """A minimal config that satisfies the top-level anyOf constraints."""
    return {
        "server": {"id": "test-server"},
        "loopback_ports": [{"name": "lb1"}],
        "web_status": {},
    }


# ---------------------------------------------------------------------------
# schema_violations: flat (path, message) pairs
# ---------------------------------------------------------------------------


def test_schema_violations_empty_for_valid_config():
    assert schema_violations(_valid_server_config(), SERVER_SCHEMA) == []


def test_schema_violations_unknown_key():
    cfg = _valid_server_config()
    cfg["client_listner"] = {"host": "127.0.0.1"}
    pairs = schema_violations(cfg, SERVER_SCHEMA)
    assert len(pairs) == 1
    path, message = pairs[0]
    assert path == "(root)" or path == "" or "client_listner" in message, f"unexpected pair: {pairs}"
    assert "client_listner" in f"{path} {message}"


def test_schema_violations_missing_required_key():
    cfg = _valid_server_config()
    cfg["loopback_ports"] = [{}]
    pairs = schema_violations(cfg, SERVER_SCHEMA)
    text = " | ".join(f"{p}: {m}" for p, m in pairs)
    assert "name" in text, f"expected missing-required 'name' in {text}"


def test_schema_violations_nested_path_rendering():
    cfg = _valid_server_config()
    cfg["loopback_ports"][0]["bogus"] = 1
    pairs = schema_violations(cfg, SERVER_SCHEMA)
    assert len(pairs) == 1
    assert "loopback_ports[0]" in pairs[0][0], f"unexpected path: {pairs[0][0]!r}"


def test_schema_violations_bad_type():
    cfg = _valid_server_config()
    cfg["loopback_ports"] = "not-a-list"
    pairs = schema_violations(cfg, SERVER_SCHEMA)
    assert any("array" in m.lower() for p, m in pairs), f"unexpected pairs: {pairs}"


def _valid_auth() -> dict:
    return {"users": [{"username": "x", "password_hash": "a" * 64}]}


def test_schema_violations_auth_schema():
    bad = _valid_auth()
    bad["users"][0]["permissions"] = "bogus-role"
    pairs = schema_violations(bad, AUTH_SCHEMA)
    text = " | ".join(f"{p}: {m}" for p, m in pairs)
    assert "bogus-role" in text, f"expected enum violation in {text}"
    # valid minimal auth config
    assert schema_violations(_valid_auth(), AUTH_SCHEMA) == []


def test_schema_violations_security_schema():
    assert schema_violations({}, SECURITY_SCHEMA) == []
    assert schema_violations({"access_default": "bogus"}, SECURITY_SCHEMA) != []


# ---------------------------------------------------------------------------
# config_file_violations
# ---------------------------------------------------------------------------


def test_config_file_violations_clean_file(tmp_path: Path):
    p = tmp_path / "server.yaml"
    p.write_text(yaml.safe_dump(_valid_server_config()))
    assert config_file_violations(str(p), SERVER_SCHEMA) == []


def test_config_file_violations_missing_file(tmp_path: Path):
    lines = config_file_violations(str(tmp_path / "nope.yaml"), SERVER_SCHEMA)
    assert len(lines) == 1
    assert "not found" in lines[0]


def test_config_file_violations_broken_yaml(tmp_path: Path):
    p = tmp_path / "server.yaml"
    p.write_text("server:\n  bad: [1\n")
    lines = config_file_violations(str(p), SERVER_SCHEMA)
    assert len(lines) == 1
    assert "could not parse YAML" in lines[0]


def test_config_file_violations_violating_file(tmp_path: Path):
    p = tmp_path / "server.yaml"
    cfg = _valid_server_config()
    cfg["typo_key"] = True
    p.write_text(yaml.safe_dump(cfg))
    lines = config_file_violations(str(p), SERVER_SCHEMA)
    assert any("typo_key" in line for line in lines)


# ---------------------------------------------------------------------------
# check_config_files (strict pass used by --check-config)
# ---------------------------------------------------------------------------


def test_check_config_files_all_clean(tmp_path: Path):
    server = tmp_path / "server.yaml"
    server.write_text(yaml.safe_dump(_valid_server_config()))
    auth = tmp_path / "authentication.yaml"
    auth.write_text(yaml.safe_dump(_valid_auth()))
    security = tmp_path / "security.yaml"
    security.write_text("{}\n")
    violations, problems = check_config_files(str(server), str(auth), str(security))
    assert violations == [] and problems == []


def test_check_config_files_violation_and_missing(tmp_path: Path):
    server = tmp_path / "server.yaml"
    cfg = _valid_server_config()
    cfg["typo_key"] = True
    server.write_text(yaml.safe_dump(cfg))
    violations, problems = check_config_files(str(server), str(tmp_path / "nope.yaml"), None)
    assert violations, "expected schema violations"
    assert any("nope.yaml" in p and "not found" in p for p in problems)


# ---------------------------------------------------------------------------
# payload_violations (Config Editor path)
# ---------------------------------------------------------------------------


def test_payload_violations_splits_authentication_and_server():
    payload = _valid_server_config()
    payload["authentication"] = _valid_auth()
    assert payload_violations(payload) == []
    # server-side violation
    payload["bad_key"] = 1
    assert any("bad_key" in line for line in payload_violations(payload))
    # auth-side violation is checked against the AUTH schema
    payload = _valid_server_config()
    auth = _valid_auth()
    auth["users"][0]["permissions"] = "bogus-role"
    payload["authentication"] = auth
    assert payload_violations(payload) != []


def test_payload_violations_server_keys_rejected_in_auth_section():
    payload = _valid_server_config()
    payload["authentication"] = {"server": {"id": "x"}}
    assert payload_violations(payload) != []


# ---------------------------------------------------------------------------
# check_mapping (lenient live-server pass)
# ---------------------------------------------------------------------------


def test_check_mapping_logs_and_returns_count(caplog):
    logger = config_validation.LOGGER
    import logging

    with caplog.at_level(logging.ERROR, logger="openmux.config"):
        # A bare {"bogus": 1} violates the server-required rule, the
        # additionalProperties rule, AND the top-level anyOf runtime
        # constraint: 3 violations total.
        n = check_mapping(logger, {"bogus": 1}, SERVER_SCHEMA, "unit-test.yaml")
    assert n == 3
    assert any("bogus" in rec.message and "unit-test.yaml" in rec.message for rec in caplog.records)


def test_check_mapping_never_raises_on_weird_instances(caplog):
    logger = config_validation.LOGGER
    with caplog.at_level("ERROR", logger="openmux.config"):
        assert check_mapping(logger, None, SERVER_SCHEMA, "x") >= 0
        assert check_mapping(logger, [1, 2], SERVER_SCHEMA, "x") >= 0


# ---------------------------------------------------------------------------
# locations: packaged schema resolution
# ---------------------------------------------------------------------------


def test_locations_schema_resolves_from_package(monkeypatch, tmp_path: Path):
    """Resolution must not depend on the current working directory."""
    from openmux.server.locations import schema_dir, schema_file, server_schema_file

    assert schema_dir().name == "config_schema"
    default = server_schema_file()
    assert default.is_file()
    assert schema_file(SERVER_SCHEMA) == default

    # chdir away: still resolves the packaged schema
    monkeypatch.chdir(tmp_path)
    assert server_schema_file() == default

    # and the env override still wins
    override = tmp_path / "override.yaml"
    override.write_text(SERVER_SCHEMA_YAML.read_text())
    monkeypatch.setenv("OPENMUX_CONFIG_SCHEMA", str(override))
    assert server_schema_file() == override


def test_package_data_ships_all_schemas():
    """Guard: the package-dir holds all 4 schemas (setuptools package-data)."""
    from openmux.server.locations import schema_dir

    names = {p.name for p in schema_dir().glob("openmux_*.yaml")}
    assert names == {
        "openmux_config_schema.yaml",
        "openmux_authentication_schema.yaml",
        "openmux_security_schema.yaml",
        "openmux_client_schema.yaml",
    }


# ---------------------------------------------------------------------------
# Config Editor: _validate_payload rejects schema-invalid payloads
# ---------------------------------------------------------------------------


def test_editor_validate_payload_rejects_schema_violation(tmp_path: Path):
    import shutil as _shutil

    from openmux.server.config_manager import ConfigManager
    from openmux.server.web_plugins.config_editor import _validate_payload

    server = tmp_path / "server.yaml"
    server.write_text((CONFIG_DIR / "server.yaml").read_text())
    _shutil.copy(CONFIG_DIR / "authentication.yaml", tmp_path / "authentication.yaml")
    cm = ConfigManager(str(server))

    import copy

    payload = copy.deepcopy(_valid_server_config())
    payload["bad_key"] = 1
    ok, message, exc = _validate_payload(payload, cm)
    assert ok is False
    assert exc is None  # schema gate rejects before the semantic gate
    assert "bad_key" in message
    assert message.startswith("Schema validation failed:")

    # a valid payload still passes
    ok, message, exc = _validate_payload(_valid_server_config(), cm)
    assert ok is True, f"unexpected failure: {message}"


# ---------------------------------------------------------------------------
# ConfigManager: lenient startup behavior
# ---------------------------------------------------------------------------


def test_config_manager_logs_violation_but_loads(tmp_path: Path, caplog):
    """A config with a typo loads, and logs one ERROR per violation."""
    import logging
    import shutil as _shutil

    from openmux.server.config_manager import ConfigManager

    server = tmp_path / "server.yaml"
    src = (CONFIG_DIR / "server.yaml").read_text().replace("client_listener:", "client_listner:")
    server.write_text(src)
    _shutil.copy(CONFIG_DIR / "authentication.yaml", tmp_path / "authentication.yaml")

    with caplog.at_level(logging.ERROR, logger="openmux.config"):
        cfg = ConfigManager(str(server)).load_config()
    # Load succeeded (lenient)
    assert "client_listner" in cfg
    # Exactly the violation was logged
    violations = [rec.message for rec in caplog.records if rec.levelno == logging.ERROR and "client_listner" in rec.message]
    assert violations, "expected a logged schema violation for the typo"


def test_config_manager_clean_config_logs_no_violation(tmp_path: Path, caplog):
    import logging
    import shutil as _shutil

    from openmux.server.config_manager import ConfigManager

    server = tmp_path / "server.yaml"
    server.write_text((CONFIG_DIR / "server.yaml").read_text())
    _shutil.copy(CONFIG_DIR / "authentication.yaml", tmp_path / "authentication.yaml")

    with caplog.at_level(logging.ERROR, logger="openmux.config"):
        ConfigManager(str(server)).load_config()
    assert not [rec for rec in caplog.records if rec.levelno == logging.ERROR and "schema violation" in rec.message.lower()]


# ---------------------------------------------------------------------------
# --check-config CLI: main() early path (in-process, SystemExit captured)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate, expected_code, expected_in_output",
    [
        (None, 0, "Config validation OK"),
        (lambda src: src.replace("client_listener:", "client_listner:"), 1, "client_listner"),
    ],
    ids=["clean", "typo"],
)
def test_main_check_config_in_process(tmp_path: Path, monkeypatch, mutate, expected_code, expected_in_output, capsys):
    import shutil as _shutil

    import openmux.server.main as main_mod

    server = tmp_path / "server.yaml"
    src = (CONFIG_DIR / "server.yaml").read_text()
    if mutate is not None:
        src = mutate(src)
    server.write_text(src)
    _shutil.copy(CONFIG_DIR / "authentication.yaml", tmp_path / "authentication.yaml")
    _shutil.copy(CONFIG_DIR / "security.yaml", tmp_path / "security.yaml")

    monkeypatch.setattr(sys, "argv", ["openmux", "--check-config", "-c", str(server)])
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code == expected_code
    out = capsys.readouterr().out
    assert expected_in_output in out
