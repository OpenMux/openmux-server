#!/usr/bin/env python3
"""Validate every OpenMux config file under ./config against its schema.

Routes each file to the authoritative schema in config_schema/:
  - server-style configs  -> openmux_config_schema.yaml
  - authentication.yaml   -> openmux_authentication_schema.yaml
  - security.yaml         -> openmux_security_schema.yaml
  - client.yaml           -> openmux_client_schema.yaml

Exits 0 when everything validates, 1 otherwise.
"""
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "config_schema"

SIDECAR_SCHEMAS = {
    "authentication.yaml": "openmux_authentication_schema.yaml",
    "security.yaml": "openmux_security_schema.yaml",
    "client.yaml": "openmux_client_schema.yaml",
}
DEFAULT_SCHEMA = "openmux_config_schema.yaml"


def _schema_for(config_file: Path):
    name = config_file.name
    schema_name = SIDECAR_SCHEMAS.get(name, DEFAULT_SCHEMA)
    return SCHEMA_DIR / schema_name


def main() -> int:
    ok = True
    configs = sorted((ROOT / "config").glob("*.yaml"))
    if not configs:
        print(f"No config files found in {ROOT / 'config'}")
        return 1
    for cfg_path in configs:
        schema_path = _schema_for(cfg_path)
        if not schema_path.is_file():
            ok = False
            print(f"SCHEMA MISSING for {cfg_path}: {schema_path}")
            continue
        cfg = yaml.safe_load(cfg_path.read_text())
        schema = yaml.safe_load(schema_path.read_text())
        errors = sorted(Draft202012Validator(schema).iter_errors(cfg), key=lambda e: list(e.path))
        if errors:
            ok = False
            print(f"VALIDATION FAIL for {cfg_path} against {schema_path.name}:")
            for e in errors:
                path = "/".join([str(p) for p in e.path])
                print(f" - {path or '.'}: {e.message}")
        else:
            print(f"VALIDATION OK for {cfg_path.name} against {schema_path.name}")
    print("RESULT:", "valid" if ok else "invalid")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
