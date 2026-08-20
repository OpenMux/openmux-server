"""Schema integrity and config validation regression tests.

Guards the authoritative schemas in config_schema/ against drifting from the
real config files in config/. Each config file's schema is detected from its
top-level keys (shared detector in scripts/validate_schema.py): the file must
be covered by exactly one schema, then validate against it.
"""

import importlib.util
import inspect
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from openmux.server.web_plugins import config_editor

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = REPO_ROOT / "config_schema"
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
CONFIG_FILES = sorted(CONFIG_DIR.glob("*.yaml"))


def test_all_schema_files_exist_and_are_valid():
    assert len(SCHEMA_FILES) == 4, f"expected 4 schemas in config_schema/, found {[p.name for p in SCHEMA_FILES]}"
    for path in SCHEMA_FILES:
        Draft202012Validator.check_schema(yaml.safe_load(path.read_text()))


def test_config_editor_serves_the_authoritative_schema():
    src = inspect.getsource(config_editor)
    assert '"config_schema" / "openmux_config_schema.yaml"' in src
    assert "to_check" not in src


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
    errors = sorted(Draft202012Validator(schema).iter_errors(config), key=lambda e: list(e.path))
    assert not errors, f"debian authentication.yaml failed: {errors[0].message}"
