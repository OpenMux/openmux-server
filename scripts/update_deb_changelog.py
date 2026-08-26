"""
Update debian/changelog version from git with minimal dependencies.

The upstream (Python) version is computed by setuptools-scm from the nearest
v* tag: "<base>.post<N>+g<sha>". Debian versions cannot contain "+", and a
dpkg revision belongs to a package version, so this script translates: the
base stays the tag's version and the commit distance N becomes the Debian
revision. Both sides gain one entry per commit, so the sort order agrees.

Clean exactly at a tag uses revision DEB_REVISION (default 1).

Usage:
  python3 scripts/update_deb_changelog.py \
      [--revision 1] [--dist unstable] [--message "Automated build"] [--snapshot auto] [--dry-run]

Environment:
  DEB_REVISION   - Debian revision used when the tree is exactly at a tag
  DEB_DIST       - Debian distribution (e.g., unstable)
  DEB_MESSAGE    - changelog entry message
  DEB_SNAPSHOT   - if set to 'auto', append '~gitYYYYMMDDHHMM[.sha]' to the version

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
CHANGELOG = REPO_ROOT / "debian" / "changelog"


def _run(args: list[str], cwd: Path = REPO_ROOT) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()


def read_git_version() -> tuple[str, int]:
    """Return (base version, commit distance from the tag) from git.

    Mirrors the setuptools-scm git_describe_command in pyproject.toml so the
    deb version always tracks the Python version. With no tag at all the base
    is 0.0.0 and the distance is the total commit count. Without git there is
    no base, so (0.0.0, 0) is returned and the caller warns.
    """
    try:
        _run(["git", "rev-parse", "--git-dir"])
    except subprocess.CalledProcessError:
        return "0.0.0", 0
    try:
        tag = _run(["git", "describe", "--tags", "--abbrev=0", "--match", "v*"])
        long_desc = _run(["git", "describe", "--tags", "--long", "--match", "v*"])
    except subprocess.CalledProcessError:
        count = int(_run(["git", "rev-list", "--count", "HEAD"]) or 0)
        return "0.0.0", count
    m = re.match(r"^v?(\d+\.\d+(?:\.\d+)?(?:rc\d+|a\d+|b\d+)?)[0-9A-Za-z.-]*$", tag)
    base = m.group(1) if m else tag.lstrip("v")
    d = re.search(r"-(\d+)-g[0-9a-f]+(-dirty)?$", long_desc)
    return base, int(d.group(1)) if d else 0


def git_snapshot_suffix() -> str | None:
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        # If git exists and we're in a repo, prefer describe info
        try:
            subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=REPO_ROOT,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
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
    body = f"""{pkg} ({version}) {dist}; urgency=medium

  * {message}

 -- {maint}  {rfc2822_now()}

"""
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

    base_version, distance = read_git_version()
    # Commit distance becomes the deb revision; exactly at a tag the
    # fallback DEB_REVISION (min 1) is used instead
    revision = distance if distance > 0 else max(1, int(args.revision))
    deb_version = f"{base_version}-{revision}"
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
