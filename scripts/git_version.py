"""Print the project version derived from git, same rule as setuptools-scm.

Prints "<base>.post<N>+g<node>" for N commits past the nearest "v*" tag,
adding ".dYYYYMMDD" for a dirty tree, or the bare base version when the
tree is clean exactly at the tag. Falls back to "0.0.0" when git (or any
tag) is unavailable, so Makefile-driven builds keep working there.

Pure stdlib + the git binary: no venv or build dependencies needed.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DESCRIBE = ["git", "describe", "--tags", "--long", "--dirty", "--match", "v*"]
DESC_RE = re.compile(r"^v?(\d+\.\d+(?:\.\d+)?(?:rc\d+|a\d+|b\d+)?)-(\d+)-g([0-9a-f]+)(-dirty)?$")


def _run(args: list[str]) -> str:
    return subprocess.check_output(args, cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL).strip()


def derive_version() -> str:
    """Compute the PEP 440 version from git describe (raises on git failure)."""
    desc = _run(DESCRIBE)
    m = DESC_RE.match(desc)
    if not m:
        return "0.0.0"
    base, distance, node, dirty = m.groups()
    if distance == "0" and not dirty:
        return base
    version = f"{base}.post{int(distance)}+g{node}"
    if dirty:
        version += f".d{datetime.now():%Y%m%d}"
    return version


def main() -> int:
    try:
        print(derive_version())
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        print("0.0.0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
