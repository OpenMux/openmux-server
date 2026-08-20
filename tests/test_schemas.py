"""Schema integrity and config validation regression tests.

Guards the authoritative schemas in config_schema/ against drifting from the
real config files in config/. Each config file must validate against its
routed schema:

  - server-style configs  -> openmux_config_schema.yaml
  - authentication.yaml   -> openmux_authentication_schema.yaml
  - security.yaml         -> openmux_security_schema.yaml
  - client.yaml           -> openmux_client_schema.yaml
"""

import inspect
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from openmux.server.web_plugins import config_editor

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = REPO_ROOT / "config_schema"
CONFIG_DIR = REPO_ROOT / "config"

SIDECAR_SCHEMAS = {
    "authentication.yaml": "openmux_authentication_schema.yaml",
    "security.yaml": "openmux_security_schema.yaml",
    "client.yaml": "openmux_client_schema.yaml",
}
DEFAULT_SCHEMA = "openmux_config_schema.yaml"

SCHEMA_FILES = sorted(SCHEMA_DIR.glob("openmux_*.yaml"))
CONFIG_FILES = sorted(CONFIG_DIR.glob("*.yaml"))


def _schema_for(config_file: Path) -> Path:
    return SCHEMA_DIR / SIDECAR_SCHEMAS.get(config_file.name, DEFAULT_SCHEMA)


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
def test_config_validates_against_routed_schema(config_file: Path):
    schema_path = _schema_for(config_file)
    schema = yaml.safe_load(schema_path.read_text())
    config = yaml.safe_load(config_file.read_text())
    errors = sorted(Draft202012Validator(schema).iter_errors(config), key=lambda e: list(e.path))
    detail = "; ".join(f"/{'/'.join(map(str, e.path))}: {e.message}" for e in errors[:5])
    assert not errors, f"{config_file.name} failed against {schema_path.name}: {detail}"


def test_debian_authentication_config_matches_auth_schema():
    schema = yaml.safe_load((SCHEMA_DIR / "openmux_authentication_schema.yaml").read_text())
    config = yaml.safe_load((REPO_ROOT / "debian" / "package-config" / "authentication.yaml").read_text())
    errors = sorted(Draft202012Validator(schema).iter_errors(config), key=lambda e: list(e.path))
    assert not errors, f"debian authentication.yaml failed: {errors[0].message}"
