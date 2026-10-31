"""E2E fixture external-auth helper: reply per user name, in the protocol the server speaks.

``openmux/server/auth_manager.py`` (_external_authenticate) writes the user name and the password on stdin,
one per line, and reads the exit code plus one JSON object on stdout. Exit ``0`` with ``ok`` true is a
success; anything else is a refused login. The ``groups`` list of the reply is what decides the role,
through the ``external_auth.groups`` mapping in ``authentication.yaml``.

Four names cover the states one helper can leave a person in, so one script serves a whole scenario:

- ``ext_ro``: authenticates and names the group that ``AUTH-11`` maps to ``read-only``, so the mapping is what
  hands the role out.
- ``ext_unmapped``: authenticates and names a group nobody mapped. The mapping, not the helper, then decides
  the role, and with no ``default_permission`` the person holds no permission at all.
- ``ext_nogroup``: authenticates with an empty group list, which is the same nothing by a different route.
- ``ext_slow``: sleeps before it answers, so a probe can measure what one slow helper costs the server. Issue #114
  carries that defect, and no scenario asserts a duration.

Any other name, or a wrong password for these four, is exit ``1``. The password is one constant, because a
scenario needs to get both halves right: the helper's password accepted and any other refused.

The script reads no config and imports nothing from ``openmux``, so it runs under any interpreter.
"""

from __future__ import annotations

import json
import sys
import time

#: The password this helper accepts for each of its four names.
HELPER_PASSWORD = "helper-secret"

#: Seconds ``ext_slow`` waits before answering. Shorter than the helper timeout the harness writes (5 s), so
#: a scenario measures a slow answer rather than a timeout.
SLOW_SECONDS = 2.0

#: The groups to report per name, and ``None`` where the answer is a refusal.
GROUPS = {
    "ext_ro": ["openmux_read"],
    "ext_unmapped": ["staff"],
    "ext_nogroup": [],
    "ext_slow": ["openmux_read"],
}


def main() -> int:
    """Answer one login on the protocol's terms."""
    lines = sys.stdin.read().splitlines()
    if len(lines) < 2:
        sys.stderr.write("expected a user name and a password on stdin\n")
        return 2
    username, password = lines[0].strip(), lines[1].strip()
    if username == "ext_slow":
        time.sleep(SLOW_SECONDS)
    if username not in GROUPS or password != HELPER_PASSWORD:
        sys.stderr.write(f"helper refuses {username!r}\n")
        return 1
    json.dump({"ok": True, "user": username, "groups": list(GROUPS[username])}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
