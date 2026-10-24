#!/usr/bin/env python3
"""Generate the Debian package's server.yaml from the pristine repo config.

The Debian default config is generated from ``config/server.yaml`` (the
single source of truth) at package-build time, applying only the FHS
overrides that differ in a packaged install:

  * ``logging.file`` -> ``/var/log/openmux/openmux_server.log``
  * ``port_actions.actions_dir`` -> ``/etc/openmux/actions`` (the writable,
    user-managed home for port-action scripts, created by the package and
    seeded with the bundled examples by ``openmux.postinst``)

Any other difference between the pristine and packaged server config should
be fixed in ``config/server.yaml`` itself, so the two stop drifting. This
script fails loudly (non-zero exit, package build aborts) if the pristine
file no longer has the expected lines, so a future edit cannot silently
reintroduce a stale packaged config.
"""

import sys

# Exact lines expected in config/server.yaml (fail the build if they move).
LOG_LINE = '  file: "logs/openmux_server.log"'
LOG_LINE_PKG = '  file: "/var/log/openmux/openmux_server.log"'

ACTIONS_DIR_LINE = "  actions_dir: openmux/server/actions/examples"
ACTIONS_DIR_LINE_PKG = "  actions_dir: /etc/openmux/actions"


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(f"usage: {sys.argv[0]} <pristine-server.yaml> <dest-server.yaml>")
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, encoding="utf-8") as fh:
        text = fh.read()

    for expected in (LOG_LINE, ACTIONS_DIR_LINE):
        if expected not in text:
            sys.exit(f"error: {src} has no {expected!r} line; update {sys.argv[0]}")

    text = text.replace(LOG_LINE, LOG_LINE_PKG, 1)
    text = text.replace(ACTIONS_DIR_LINE, ACTIONS_DIR_LINE_PKG, 1)
    if not text.endswith("\n"):
        text += "\n"
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"generated {dst} from {src} (FHS overrides applied)")


if __name__ == "__main__":
    main()
