#!/usr/bin/env python3
import sys
import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = [
    ROOT / 'docs' / 'to_check' / 'openmux_config_schema.yaml',
    ROOT / 'config_schema' / 'openmux_config_schema.yaml',
]
CONFIG = ROOT / 'config' / 'loopback_test.yaml'

def main() -> int:
    ok = True
    cfg = yaml.safe_load(CONFIG.read_text())
    for sp in SCHEMAS:
        schema = yaml.safe_load(sp.read_text())
        v = Draft202012Validator(schema)
        errors = sorted(v.iter_errors(cfg), key=lambda e: list(e.path))
        if errors:
            ok = False
            print(f'VALIDATION FAIL for {sp}:')
            for e in errors:
                path = '/'.join([str(p) for p in e.path])
                print(f' - {path or "."}: {e.message}')
        else:
            print(f'VALIDATION OK for {sp}')
    print('RESULT:', 'valid' if ok else 'invalid')
    return 0 if ok else 1

if __name__ == '__main__':
    raise SystemExit(main())
