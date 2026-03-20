#!/usr/bin/env python3
"""Download xterm.js assets and license texts required by the Web Console.

This helper installs xterm.js and the fit addon locally so the OpenMux
web console can serve them without fetching from a CDN at runtime. It also
downloads upstream license texts for packaging under /usr/share/doc.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

DEFAULT_XTERM_VERSION = "latest"
DEFAULT_XTERM_ADDON_FIT_VERSION = "latest"


def build_assets(xterm_version: str, addon_fit_version: str) -> list[tuple[str, str, list[str]]]:
    return [
        (
            "xterm/css/xterm.css",
            "xterm.js stylesheet",
            [
                f"https://unpkg.com/xterm@{xterm_version}/css/xterm.css",
                f"https://cdn.jsdelivr.net/npm/xterm@{xterm_version}/css/xterm.css",
            ],
        ),
        (
            "xterm/lib/xterm.js",
            "xterm.js library",
            [
                f"https://unpkg.com/xterm@{xterm_version}/lib/xterm.js",
                f"https://cdn.jsdelivr.net/npm/xterm@{xterm_version}/lib/xterm.js",
            ],
        ),
        (
            "xterm-addon-fit/lib/xterm-addon-fit.js",
            "xterm fit addon",
            [
                f"https://unpkg.com/xterm-addon-fit@{addon_fit_version}/lib/xterm-addon-fit.js",
                f"https://cdn.jsdelivr.net/npm/xterm-addon-fit@{addon_fit_version}/lib/xterm-addon-fit.js",
            ],
        ),
    ]


def _license_sources(package_name: str, version: str) -> list[str]:
    sources = [f"https://cdn.jsdelivr.net/npm/{package_name}@{version}/LICENSE"]
    if version != "latest":
        sources.insert(0, f"https://raw.githubusercontent.com/xtermjs/xterm.js/{version}/LICENSE")
    return sources


def build_license_files(xterm_version: str, addon_fit_version: str) -> list[tuple[str, str, list[str]]]:
    return [
        (
            "xterm/LICENSE",
            "xterm.js license",
            _license_sources("xterm", xterm_version),
        ),
        (
            "xterm-addon-fit/LICENSE",
            "xterm-addon-fit license",
            _license_sources("xterm-addon-fit", addon_fit_version),
        ),
    ]


def download_asset(target: Path, label: str, sources: list[str], force: bool) -> bool:
    """Download one asset, trying each source until one works."""
    if target.exists() and not force:
        print(f"[skip] {label} already present at {target}")
        return True

    target.parent.mkdir(parents=True, exist_ok=True)
    for url in sources:
        try:
            print(f"[info] Fetching {label} from {url}")
            with urlopen(url, timeout=30) as resp:  # nosec B310 (trusted URLs)
                data = resp.read()
            if not data:
                raise RuntimeError("empty response")
            target.write_bytes(data)
            print(f"[ ok ] Wrote {label} to {target}")
            return True
        except (HTTPError, URLError, TimeoutError, RuntimeError) as exc:
            print(f"[warn] {label} download failed from {url}: {exc}")
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[warn] Unexpected error downloading {label} from {url}: {exc}")
    print(f"[err] Unable to download {label}; tried all sources")
    return False


def verify_assets(static_dir: Path, assets: list[tuple[str, str, list[str]]]) -> bool:
    missing: list[Path] = []
    for rel_path, label, _ in assets:
        target = static_dir / rel_path
        if not target.is_file():
            missing.append(target)
            continue
        try:
            if target.stat().st_size <= 0:
                missing.append(target)
        except OSError:
            missing.append(target)
    if missing:
        print("[err] Verification failed; missing or empty files:")
        for path in missing:
            print(f"       - {path}")
        return False
    print("[ ok ] All xterm assets verified")
    return True


def verify_licenses(license_dir: Path, license_files: list[tuple[str, str, list[str]]]) -> bool:
    missing: list[Path] = []
    for rel_path, label, _ in license_files:
        target = license_dir / rel_path
        if not target.is_file():
            missing.append(target)
            continue
        try:
            if target.stat().st_size <= 0:
                missing.append(target)
        except OSError:
            missing.append(target)
    if missing:
        print("[err] License verification failed; missing or empty files:")
        for path in missing:
            print(f"       - {path}")
        return False
    print("[ ok ] All xterm license files verified")
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download xterm.js assets for the OpenMux Web Console")
    parser.add_argument(
        "--static-dir",
        help="Target static assets directory (default: <repo_root>/static)",
        default=None,
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download assets even if the target files already exist",
    )
    parser.add_argument(
        "--license-dir",
        help="Target license directory (default: <repo_root>/third_party_licenses)",
        default=None,
    )
    parser.add_argument(
        "--xterm-version",
        default=DEFAULT_XTERM_VERSION,
        help=f"xterm.js version to download (default: {DEFAULT_XTERM_VERSION})",
    )
    parser.add_argument(
        "--xterm-addon-fit-version",
        default=DEFAULT_XTERM_ADDON_FIT_VERSION,
        help=f"xterm-addon-fit version to download (default: {DEFAULT_XTERM_ADDON_FIT_VERSION})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    static_dir = Path(args.static_dir) if args.static_dir else repo_root / "static"
    license_dir = Path(args.license_dir) if args.license_dir else repo_root / "third_party_licenses"
    assets = build_assets(args.xterm_version, args.xterm_addon_fit_version)
    license_files = build_license_files(args.xterm_version, args.xterm_addon_fit_version)

    print(f"[info] Installing xterm assets into {static_dir}")
    static_dir.mkdir(parents=True, exist_ok=True)
    print(f"[info] Installing third-party licenses into {license_dir}")
    license_dir.mkdir(parents=True, exist_ok=True)

    success = True
    for rel_path, label, sources in assets:
        target = static_dir / rel_path
        success = download_asset(target, label, sources, args.force) and success

    for rel_path, label, sources in license_files:
        target = license_dir / rel_path
        success = download_asset(target, label, sources, args.force) and success

    if not success:
        print("[err] One or more downloads failed; see messages above")
        return 1

    if not verify_assets(static_dir, assets):
        return 1

    if not verify_licenses(license_dir, license_files):
        return 1

    print("[done] xterm assets available")
    return 0


if __name__ == "__main__":
    sys.exit(main())
