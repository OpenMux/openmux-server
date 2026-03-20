"""
Update debian/changelog version from pyproject.toml with minimal dependencies.

Usage:
  python3 scripts/update_deb_changelog.py \
      [--revision 1] [--dist unstable] [--message "Automated build"] [--snapshot auto] [--dry-run]

Environment:
  DEB_REVISION   - default Debian revision (e.g., 1)
  DEB_DIST       - Debian distribution (e.g., unstable)
  DEB_MESSAGE    - changelog entry message
  DEB_SNAPSHOT   - if set to 'auto', append '~gitYYYYMMDDHHMM' to the version

The script writes debian/changelog compatible with dpkg-buildpackage.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
CHANGELOG = REPO_ROOT / "debian" / "changelog"


def read_version_from_pyproject() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    # Try simple TOML key match to avoid tomllib dependency
    # pattern: version = "1.2.3"
    m = re.search(r"^version\s*=\s*\"([^\"]+)\"", text, re.MULTILINE)
    if m:
        return m.group(1).strip()
    # Fallback: minimal parse of [project] section
    in_project = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_project = s == "[project]"
            continue
        if in_project and s.startswith("version") and "=" in s:
            v = s.split("=", 1)[1].strip().strip('"')
            if v:
                return v
    raise RuntimeError("Could not find version in pyproject.toml")


def git_snapshot_suffix() -> str | None:
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        # If git exists and we're in a repo, prefer describe info
        try:
            subprocess.run(["git", "rev-parse", "--git-dir"], cwd=REPO_ROOT, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            desc = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT).decode().strip()
            return f"git{ts}.{desc}"
        except Exception:
            return f"git{ts}"
    except Exception:
        return None


def rfc2822_now() -> str:
    # Example: Mon, 27 Oct 2025 12:00:00 +0000
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")


def write_changelog(pkg: str, version: str, dist: str, message: str, maint: str, dry_run: bool) -> None:
    body = (
        f"{pkg} ({version}) {dist}; urgency=medium\n\n"
        f"  * {message}\n\n"
        f" -- {maint}  {rfc2822_now()}\n\n"
    )
    if dry_run:
        sys.stdout.write(body)
        return
    CHANGELOG.parent.mkdir(parents=True, exist_ok=True)
    CHANGELOG.write_text(body, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--revision", default=os.environ.get("DEB_REVISION", "1"))
    ap.add_argument("--dist", default=os.environ.get("DEB_DIST", "unstable"))
    ap.add_argument("--message", default=os.environ.get("DEB_MESSAGE", "Automated build"))
    ap.add_argument("--snapshot", choices=["auto", "off"], default=os.environ.get("DEB_SNAPSHOT", "off"))
    ap.add_argument("--package", default=os.environ.get("DEB_PACKAGE", "openmux"))
    ap.add_argument("--maintainer", default=os.environ.get("DEB_MAINTAINER", "OpenMux Team <info@openmux.org>"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    base_version = read_version_from_pyproject()
    deb_version = f"{base_version}-{args.revision}"
    if args.snapshot == "auto":
        suf = git_snapshot_suffix()
        if suf:
            deb_version = f"{deb_version}~{suf}"

    write_changelog(
        pkg=args.package,
        version=deb_version,
        dist=args.dist,
        message=args.message,
        maint=args.maintainer,
        dry_run=args.dry_run,
    )
    print(f"Wrote debian/changelog with version: {deb_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
