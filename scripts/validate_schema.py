#!/usr/bin/env python3
"""Validate every OpenMux config file under ./config against its schema.

The schema for each file is detected from its top-level keys: every schema in
config_schema/ declares the top-level keys it accepts, and a file must be
covered by exactly one schema. There is no filename mapping and no default
schema; a file covered by zero schemas (or by several) is a hard error.

Exits 0 when everything validates, 1 otherwise.
"""
import sys
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "config_schema"
SCHEMA_FILES = sorted(SCHEMA_DIR.glob("openmux_*.yaml"))


def _schema_key_sets() -> Dict[str, Set[str]]:
    """Top-level keys accepted by each schema in config_schema/."""
    return {
        path.name: set(yaml.safe_load(path.read_text())["properties"].keys())
        for path in SCHEMA_FILES
    }


def detect_schema(config_file: Path) -> Tuple[Optional[str], str]:
    """Detect which schema covers this config file's top-level keys.

    Returns (schema_name, detail). On success schema_name is set and detail
    is empty; on failure schema_name is None and detail explains why.
    """
    cfg = yaml.safe_load(config_file.read_text())
    if cfg is None:
        return None, "file is empty"
    if not isinstance(cfg, dict):
        return None, f"top level is {type(cfg).__name__}, expected a mapping"
    keys = set(cfg.keys())
    if not keys:
        return None, "no top-level keys to match against"

    key_sets = _schema_key_sets()
    matches = {name for name, accepted in key_sets.items() if keys <= accepted}
    if len(matches) == 1:
        return next(iter(matches)), ""
    if len(matches) > 1:
        return None, f"ambiguous: covered by {', '.join(sorted(matches))}"

    best_name, best_keys = min(key_sets.items(), key=lambda item: len(keys - item[1]))
    uncovered = sorted(keys - best_keys)
    return None, (
        f"no schema covers {config_file.name}: uncovered top-level keys {uncovered} "
        f"(closest: {best_name})"
    )


def main() -> int:
    ok = True
    # `*.local.yaml` files (e.g. internal-tool credentials) are gitignored local
    # files, not OpenMux configs - exclude them from the config inventory.
    configs = sorted(p for p in (ROOT / "config").glob("*.yaml") if not p.name.endswith(".local.yaml"))
    if not configs:
        print(f"No config files found in {ROOT / 'config'}")
        return 1
    for cfg_path in configs:
        schema_name, detail = detect_schema(cfg_path)
        if schema_name is None:
            ok = False
            print(f"DETECTION FAIL for {cfg_path.name}: {detail}")
            continue
        cfg = yaml.safe_load(cfg_path.read_text())
        schema = yaml.safe_load((SCHEMA_DIR / schema_name).read_text())
        errors = sorted(Draft202012Validator(schema).iter_errors(cfg), key=lambda e: list(e.path))
        if errors:
            ok = False
            print(f"VALIDATION FAIL for {cfg_path.name} against {schema_name}:")
            for e in errors:
                path = "/".join([str(p) for p in e.path])
                print(f" - {path or '.'}: {e.message}")
        else:
            print(f"VALIDATION OK for {cfg_path.name} against {schema_name}")
    print("RESULT:", "valid" if ok else "invalid")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
