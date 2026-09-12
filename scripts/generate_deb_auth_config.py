#!/usr/bin/env python3
"""Generate a per-install authentication.yaml from the packaged template.

The packaged template (debian/package-config/authentication.yaml) carries the
sentinel password hash ``CHANGE_ME_ON_FIRST_BOOT``. On a fresh install the
postinst calls this script to replace that sentinel with the hash of a
randomly generated admin password:

    python3 generate-auth-config.py \
        --template /usr/share/openmux/default-config/authentication.yaml \
        --output /etc/openmux/authentication.yaml \
        --credentials-out /etc/openmux/.initial_credentials

Behavior:
* The plaintext password is written to ``--credentials-out`` (0600, no
  trailing newline) and printed as the last line of stdout so the installer
  can display it.
* The output file keeps only the admin user plus the non-credential
  top-level sections of the template (for example ``external_auth``).
* Any template drift (a real hash, extra users, or shipped keys) aborts with
  a non-zero exit code.
"""

import argparse
import hashlib
import os
import secrets
import sys
from typing import Any, Dict

import yaml

SENTINEL = "CHANGE_ME_ON_FIRST_BOOT"
# Entropy source for fresh installs: 24 chars from a 57-char alphabet is
# ~143 bits, far above the 128 bits used for API keys.
PASSWORD_LENGTH = 24
# Unambiguous alphabet: A-Z minus I/O, a-z minus l/o, digits 2-9.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ" "abcdefghijkmnpqrstuvwxyz" "23456789"


def generate_password(length: int) -> str:
    """Return a uniformly random password from ``_ALPHABET``."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def validate_template(template: Dict[str, Any]) -> None:
    """Reject templates that drifted from the expected single-admin shape."""
    users = template.get("users") or []
    if not users:
        raise SystemExit("template authentication config has no users")
    non_admin = [str(u.get("username", "?")) for u in users if u.get("username") != "admin"]
    if non_admin:
        raise SystemExit(f"template authentication config must carry only the admin user, found: {non_admin}")
    admin = next(u for u in users if u.get("username") == "admin")
    if str(admin.get("password_hash", "")) != SENTINEL:
        raise SystemExit(f"template admin password_hash must be {SENTINEL!r}, got {admin.get('password_hash')!r}")
    for key in ("api_keys", "public_keys"):
        if template.get(key):
            raise SystemExit(f"template authentication config must not carry {key}")


def write_credentials(path: str, password: str) -> None:
    """Write the plaintext password with 0600 permissions and no newline."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(password)
    os.chmod(path, 0o600)  # be explicit even if the umask weakened O_CREAT mode


def build_output(template: Dict[str, Any], password_hash: str) -> Dict[str, Any]:
    """Assemble the install-time auth config (admin only, template order)."""
    admin = next(u for u in template["users"] if u.get("username") == "admin")
    output: Dict[str, Any] = {
        "users": [
            {
                "username": "admin",
                "permissions": str(admin.get("permissions", "admin")),
                "password_hash": password_hash,
            }
        ]
    }
    for key, value in template.items():
        if key in ("users", "api_keys", "public_keys"):
            continue
        output[key] = value
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a per-install authentication.yaml")
    parser.add_argument("--template", required=True, help="packaged template authentication.yaml")
    parser.add_argument("--output", required=True, help="destination authentication.yaml")
    parser.add_argument("--credentials-out", required=True, help="root-only file for the plaintext password")
    args = parser.parse_args()

    with open(args.template, encoding="utf-8") as handle:
        template = yaml.safe_load(handle)
    if not isinstance(template, dict):
        raise SystemExit("template authentication config must be a mapping")
    validate_template(template)

    password = generate_password(PASSWORD_LENGTH)
    password_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
    output = build_output(template, password_hash)

    header = (
        "# Generated at install time: the admin password was random per install.\n"
        "# It was printed on the console and stored in /etc/openmux/.initial_credentials\n"
        "# (root only). Change the admin password, then delete that file.\n"
        "# Regenerate any user password hash with:\n"
        "#   printf '%s' 'your-password' | sha256sum | awk '{print $1}'\n"
    )
    with open(args.output, "w", encoding="utf-8") as handle:
        handle.write(header)
        yaml.safe_dump(output, handle, default_flow_style=False, sort_keys=False)

    write_credentials(args.credentials_out, password)
    print(password, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
