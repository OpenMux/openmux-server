#!/usr/bin/env python3
"""Validate an OpenMux unified server configuration against the JSON Schema.

Usage:
  python scripts/validate_config.py --config config/serial_unified.yaml \
      --schema docs/openmux_config_schema.yaml

Exit codes:
  0 = valid
  1 = invalid (schema violations)
  2 = usage / unexpected error
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

try:
    import jsonschema
except ImportError:  # pragma: no cover
    print(
        "ERROR: jsonschema package not installed. Install with 'pip install jsonschema'.",
        file=sys.stderr,
    )
    sys.exit(2)


def load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate OpenMux server config")
    parser.add_argument("--config", required=True, help="Path to configuration YAML file")
    parser.add_argument("--schema", required=True, help="Path to schema YAML file")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON result (machine readable)",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    schema_path = Path(args.schema)

    if not config_path.is_file():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        return 2
    if not schema_path.is_file():
        print(f"Schema file not found: {schema_path}", file=sys.stderr)
        return 2

    try:
        schema = load_yaml(schema_path)
        config = load_yaml(config_path)
    except Exception as e:  # pragma: no cover
        print(f"Failed to load files: {e}", file=sys.stderr)
        return 2

    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(config), key=lambda e: e.path)
    if errors:
        if args.json:
            print(
                json.dumps(
                    {
                        "valid": False,
                        "errors": [
                            {
                                "path": "/" + "/".join([str(p) for p in err.path]),
                                "message": err.message,
                                "validator": err.validator,
                            }
                            for err in errors
                        ],
                    },
                    indent=2,
                )
            )
        else:
            print(f"Validation FAILED for {config_path} against {schema_path}\n")
            for err in errors:
                path = "/".join([str(p) for p in err.path]) or "<root>"
                print(f" - [{path}] {err.message}")
        return 1
    else:
        if args.json:
            print(json.dumps({"valid": True}, indent=2))
        else:
            print(f"Validation OK: {config_path}")
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
