#!/usr/bin/env python3
"""Download xterm.js assets required by the Web Console.

This helper installs xterm.js and the fit addon locally so the OpenMux
web console can serve them without fetching from a CDN at runtime.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ASSETS = [
    (
        "xterm/css/xterm.css",
        "xterm.js stylesheet",
        [
            "https://unpkg.com/xterm@latest/css/xterm.css",
            "https://cdn.jsdelivr.net/npm/xterm@latest/css/xterm.css",
        ],
    ),
    (
        "xterm/lib/xterm.js",
        "xterm.js library",
        [
            "https://unpkg.com/xterm@latest/lib/xterm.js",
            "https://cdn.jsdelivr.net/npm/xterm@latest/lib/xterm.js",
        ],
    ),
    (
        "xterm-addon-fit/lib/xterm-addon-fit.js",
        "xterm fit addon",
        [
            "https://unpkg.com/xterm-addon-fit@latest/lib/xterm-addon-fit.js",
            "https://cdn.jsdelivr.net/npm/xterm-addon-fit@latest/lib/xterm-addon-fit.js",
        ],
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


def verify_assets(static_dir: Path) -> bool:
    missing: list[Path] = []
    for rel_path, label, _ in ASSETS:
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    static_dir = Path(args.static_dir) if args.static_dir else repo_root / "static"
    print(f"[info] Installing xterm assets into {static_dir}")
    static_dir.mkdir(parents=True, exist_ok=True)

    success = True
    for rel_path, label, sources in ASSETS:
        target = static_dir / rel_path
        success = download_asset(target, label, sources, args.force) and success

    if not success:
        print("[err] One or more downloads failed; see messages above")
        return 1

    if not verify_assets(static_dir):
        return 1

    print("[done] xterm assets available")
    return 0


if __name__ == "__main__":
    sys.exit(main())
