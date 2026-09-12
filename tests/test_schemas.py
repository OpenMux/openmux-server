"""Schema integrity and config validation regression tests.

Guards the authoritative schemas in openmux/config_schema/ (shipped with the
package) against drifting from the real config files in config/. Each config
file's schema is detected from its top-level keys (shared detector in
scripts/validate_schema.py): the file must be covered by exactly one schema,
then validate against it.
"""

import importlib.util
import inspect
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from openmux.server.web_plugins import config_editor

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = REPO_ROOT / "openmux" / "config_schema"
CONFIG_DIR = REPO_ROOT / "config"


def _load_validate_schema_module():
    """Load scripts/validate_schema.py as a module (single source for detection)."""
    path = REPO_ROOT / "scripts" / "validate_schema.py"
    spec = importlib.util.spec_from_file_location("openmux_validate_schema", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validate_schema = _load_validate_schema_module()

SCHEMA_FILES = sorted(SCHEMA_DIR.glob("openmux_*.yaml"))
# `*.local.yaml` files (e.g. internal-tool credentials) are gitignored local
# files, not OpenMux configs - exclude them from the config inventory.
CONFIG_FILES = sorted(p for p in CONFIG_DIR.glob("*.yaml") if not p.name.endswith(".local.yaml"))


def test_all_schema_files_exist_and_are_valid():
    assert len(SCHEMA_FILES) == 4, f"expected 4 schemas in openmux/config_schema/, found {[p.name for p in SCHEMA_FILES]}"
    for path in SCHEMA_FILES:
        Draft202012Validator.check_schema(yaml.safe_load(path.read_text()))


def test_client_schema_accepts_valid_config_and_rejects_drift():
    """The client schema must match the keys the client code actually reads.

    Regression: the schema had no `logging` property while the client reads it,
    so any client config with `logging:` would fail validation as soon as the
    client runs schema checks.
    """
    schema = yaml.safe_load((SCHEMA_DIR / "openmux_client_schema.yaml").read_text())
    errors = list(Draft202012Validator(schema).iter_errors(_valid_client_config()))
    assert errors == [], yaml.safe_dump([e.message for e in errors])

    # A logging typo (unknown key) is still rejected.
    bad = _valid_client_config()
    bad["logging"]["log_levl"] = b"INFO"
    errors = list(Draft202012Validator(schema).iter_errors(bad))
    assert len(errors) == 1 and "log_levl" in errors[0].message

    # The shipped example config validates.
    example = yaml.safe_load((REPO_ROOT / "docs" / "examples" / "client.yaml").read_text())
    errors = list(Draft202012Validator(schema).iter_errors(example))
    assert errors == [], yaml.safe_dump([e.message for e in errors])


def _valid_client_config():
    return {
        "servers": [
            {"name": "lab-hub", "host": "127.0.0.1", "port": 8023, "username": "admin", "api_key": "k"},
        ],
        "default_server": "lab-hub",
        "use_tls": False,
        "logging": {
            "log_level": "INFO",
            "file_only": False,
            "file_logging_enabled": True,
            "log_dir": "logs",
            "log_file": "openmux_client.log",
            "log_max_size_mb": 10,
            "log_backups": 5,
        },
    }


def test_config_editor_serves_the_authoritative_schema():
    """The /schema endpoint must serve the packaged schema, not a CWD guess."""
    src = inspect.getsource(config_editor)
    # Resolved through locations.server_schema_file() -> openmux/config_schema/
    assert "server_schema_file" in src
    # No CWD-relative or repo-root-relative schema lookup remains.
    assert 'Path.cwd() / "config_schema"' not in src
    assert (
        'config_schema\\" / "openmux_config_schema.yaml"' not in src
        and '"config_schema" / "openmux_config_schema.yaml"' not in src
    )
    assert "to_check" not in src


def test_server_schema_file_resolves_the_packaged_schema():
    """locations.server_schema_file() finds the schema from the install location."""
    from openmux.server.locations import schema_dir, schema_file, server_schema_file

    assert str(schema_dir()).endswith("config_schema")
    default = server_schema_file()
    assert default.is_file(), f"packaged schema missing at {default}"
    assert default == schema_file("openmux_config_schema.yaml")


def test_config_dir_is_not_empty():
    assert CONFIG_FILES, "no config files found in config/"


@pytest.mark.parametrize("config_file", CONFIG_FILES, ids=lambda p: p.name)
def test_config_schema_detection_and_validation(config_file: Path):
    schema_name, detail = validate_schema.detect_schema(config_file)
    assert schema_name is not None, f"could not detect a schema for {config_file.name}: {detail}"
    schema = yaml.safe_load((SCHEMA_DIR / schema_name).read_text())
    config = yaml.safe_load(config_file.read_text())
    errors = sorted(Draft202012Validator(schema).iter_errors(config), key=lambda e: list(e.path))
    detail_msg = "; ".join(f"/{'/'.join(map(str, e.path))}: {e.message}" for e in errors[:5])
    assert not errors, f"{config_file.name} failed against {schema_name}: {detail_msg}"


def test_detection_rejects_unknown_top_level_key(tmp_path: Path):
    bogus = tmp_path / "bogus.yaml"
    bogus.write_text("severything: {}\n")
    schema_name, detail = validate_schema.detect_schema(bogus)
    assert schema_name is None
    assert "no schema covers" in detail
    assert "severything" in detail


def test_detection_rejects_empty_file(tmp_path: Path):
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    schema_name, detail = validate_schema.detect_schema(empty)
    assert schema_name is None
    assert "empty" in detail


def test_debian_authentication_config_matches_auth_schema():
    schema = yaml.safe_load((SCHEMA_DIR / "openmux_authentication_schema.yaml").read_text())
    config = yaml.safe_load((REPO_ROOT / "debian" / "package-config" / "authentication.yaml").read_text())
    # The packaged template carries the CHANGE_ME_ON_FIRST_BOOT sentinel;
    # postinst replaces it with a per-install random hash. Validate against
    # a stand-in 64-hex hash to exercise the real schema shape.
    for user in config.get("users", []):
        if user.get("password_hash") == "CHANGE_ME_ON_FIRST_BOOT":
            user["password_hash"] = "0" * 64
    errors = sorted(Draft202012Validator(schema).iter_errors(config), key=lambda e: list(e.path))
    assert not errors, f"debian authentication.yaml failed: {errors[0].message}"


# --- Schema-vs-code drift guards -------------------------------------------
# These mirror config keys the code reads (with working runtime defaults). If a
# key is removed from the schema here, a deployment that sets it silently loses
# that setting at startup -- keep this list in sync with the adapters.


def _config_schema():
    return yaml.safe_load((SCHEMA_DIR / "openmux_config_schema.yaml").read_text())


_RUNTIME_KEYS = (
    "client_listener",
    "serial_ports",
    "loopback_ports",
    "command_ports",
    "tcp_initiator_ports",
    "muxcon",
    "web_console",
    "web_status",
)


def _validate(config: dict):
    """Validate config against the server schema; fail on any error.

    Pads the section-level test configs with the scaffolding every real
    server.yaml carries: the required server section and (when the section
    under test is not a runtime provider in itself) one empty runtime
    provider section for the top-level anyOf.
    """
    cfg = dict(config)
    cfg.setdefault("server", {})
    if not any(k in cfg for k in _RUNTIME_KEYS):
        cfg["web_status"] = {}
    errors = sorted(Draft202012Validator(_config_schema()).iter_errors(cfg), key=lambda e: list(e.path))
    assert not errors, "unexpectedly rejected:\n" + "\n".join(
        f"  /{'/'.join(map(str, e.path))}: {e.message}" for e in errors[:5]
    )


@pytest.mark.parametrize(
    "base,extra,where",
    [
        # Web console keys that landed in the code after the original schema
        # (M21): base_path / ssl_cert / ssl_key / session_ttl_seconds / sso_*.
        ({"web_console": {}}, {}, "web_console"),
        ({"web_console": {}}, {"web_console": {"base_path": "/mux"}}, "web_console"),
        ({"web_console": {}}, {"web_console": {"ssl_cert": "/c.pem"}}, "web_console"),
        ({"web_console": {}}, {"web_console": {"ssl_key": "/k.pem"}}, "web_console"),
        ({"web_console": {}}, {"web_console": {"session_ttl_seconds": 3600}}, "web_console"),
        ({"web_console": {}}, {"web_console": {"sso_trust_header": "X-SPO"}}, "web_console"),
        ({"web_console": {}}, {"web_console": {"sso_secret": "s"}}, "web_console"),
        ({"web_console": {}}, {"web_console": {"sso_max_skew_sec": 60}}, "web_console"),
        # web_status keys are all defaulted in code; a minimal section must
        # validate (the schema once required host and port).
        ({"web_status": {}}, {}, "web_status"),
    ],
)
def test_code_read_keys_are_accepted(base: dict, extra: dict, where: str):
    cfg = dict(base)
    if extra:
        key = next(iter(extra))
        cfg[key] = {**base.get(key, {}), **extra[key]}
    _validate(cfg)


def test_muxcon_code_read_keys_are_accepted():
    _validate({"muxcon": {"server_id": "leaf-1"}})
    _validate({"muxcon": {"retx_initial_ms": 100, "retx_max_ms": 4000}})
    _validate({"muxcon": {"listeners": [{"host": "10.0.0.1"}]}})
    _validate({"muxcon": {"listeners": [{"bind_interface": "eth0", "routing_mark": 42}]}})
    _validate({"muxcon": {"listeners": [{"so_mark": 77}]}})
    # Per-public-key federation filters (flat keys, the only supported form).
    _validate({"muxcon": {"public_keys": [{"key_id": "k1", "public_key": "AAA", "advertise_filters": {"include": ["*"]}}]}})
    _validate({"muxcon": {"public_keys": [{"key_id": "k1", "public_key": "AAA", "accept_filters": {"exclude": ["b"]}}]}})


def test_server_metadata_fallback_name_keys_are_accepted():
    # server.name and server.server_id are identity fallbacks read by the
    # command, web_console, and muxcon adapters when `id` is absent.
    _validate({"server": {"name": "leaf-1"}})
    _validate({"server": {"server_id": "leaf-1"}})
    # fail_fast_adapters read top-level when server.fail_fast_adapters is absent.
    _validate({"server": {}, "fail_fast_adapters": False})


def test_serial_ports_unified_dict_form_rejected():
    # The unified adapter dict form is not a supported config format
    # (array-only, like every other *_ports section).
    cfg = {"server": {}, "serial_ports": {"adapter_type": "serial", "ports": [{"name": "c1", "device": "/dev/ttyS0"}]}}
    errors = list(Draft202012Validator(_config_schema()).iter_errors(cfg))
    assert errors, "unified dict form must be rejected"


def test_serial_ports_section_shapes():
    # Array form, the only supported shape for the section.
    _validate({"serial_ports": [{"name": "c1", "device": "/dev/ttyS0"}]})
    # Per-port logging keys.
    _validate(
        {
            "serial_ports": [
                {
                    "name": "c1",
                    "device": "/dev/ttyS0",
                    "log_file": "/var/log/x.log",
                    "log_format": "jsonl",
                    "log_line_template": "{t} {d}",
                    "log_direction": "out",
                    "log_directions": ["in", "out"],
                    "max_read_write_users": 5,
                }
            ]
        }
    )


def test_serial_ports_unknown_key_still_rejected():
    cfg = {"serial_ports": [{"name": "c1", "device": "/dev/ttyS0", "baudratee": 115200}]}
    errors = list(Draft202012Validator(_config_schema()).iter_errors(cfg))
    assert errors, "unknown per-port key must be rejected"


def test_serial_ports_read_write_users_alias_rejected():
    # The legacy read_write_users alias was removed from the schema;
    # max_read_write_users is the only supported key (ticket #75 removes
    # the code fallback).
    cfg = {"serial_ports": [{"name": "c1", "device": "/dev/ttyS0", "read_write_users": 5}]}
    errors = list(Draft202012Validator(_config_schema()).iter_errors(cfg))
    assert errors, "legacy read_write_users alias must be rejected"


def test_openmux_client_ports_section_rejected():
    # The deprecated section was removed from the schema; configs must use
    # tcp_initiator_ports with protocol: {type: openmux}.
    cfg = {"server": {}, "openmux_client_ports": [{"name": "p1", "host": "h", "port": 1000, "remote_port": "8023"}]}
    errors = list(Draft202012Validator(_config_schema()).iter_errors(cfg))
    assert errors, "removed deprecated section must be rejected"


def test_muxcon_public_keys_nested_filters_rejected():
    # The nested public_keys[].muxcon form was removed from the schema; the
    # flat advertise_filters/accept_filters keys are the only supported form.
    cfg = {
        "muxcon": {"public_keys": [{"key_id": "k1", "public_key": "AAA", "muxcon": {"advertise_filters": {"include": ["*"]}}}]}
    }
    errors = list(Draft202012Validator(_config_schema()).iter_errors(cfg))
    assert errors, "removed nested per-key muxcon form must be rejected"
