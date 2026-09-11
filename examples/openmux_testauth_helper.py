#!/usr/bin/env python3
"""Test stub for the OpenMux external auth helper protocol (openmux-pam-helper).

WHAT THIS IS
------------
A small, self-contained stand-in for ``openmux-pam-helper``. OpenMux can
delegate authentication to an external binary instead of the built-in
``users:`` password-hash list (see ``authentication.external_auth``). This
script speaks the same wire protocol as the real helper, but authenticates
against a few hard-coded users, so it is useful for local and integration
testing of external authentication.

It is a TEST STUB ONLY. Do not point a production server at it: the
credentials are plain text in this file, and it performs no real system
lookup (no PAM, no TACACS+).

PROTOCOL (see openmux/server/auth_manager.py::_external_authenticate)
---------------------------------------------------------------------
The helper is invoked::

    helper [<service>]          ; <service> is OpenMux's external_auth.service

  stdin line 1: username
  stdin line 2: password

  exit 0  = success     (stdout: JSON object with an "ok" field, see below)
  exit 1  = authentication failed
  exit 2  = input error (username or password missing)

On success the helper prints a single-line JSON object to stdout. OpenMux
treats the login as valid when the exit code is 0 AND the parsed "ok"
field is true, and it caches the "groups" list for permission lookup::

    {"ok":true,"user":"admin","uid":1000,"gid":1000,"full_name":"...",
     "home":"...","shell":"...","groups":["admin","users"],
     "pam":{"service":...,"tty":...,"rhost":...,"ruser":...},
     "env":[...],"service_used":"..."}

The "groups" list is what maps a user to admin / read-write / read-only via
``external_auth.groups.admin_group`` / ``write_group`` / ``read_group``.

TEST USERS
----------
  admin / secret123   groups admin, users
  alice / alicepw     groups openmux_admin, users, dev
  bob   / bobpw       groups users, dev

USAGE
-----
Print username then password on two stdin lines::

  printf 'admin\\nsecret123\\n'   | python3 examples/openmux_testauth_helper.py
  printf 'admin\\nwrongpw\\n'     | python3 examples/openmux_testauth_helper.py

Pass a service name as the first argument (OpenMux passes it from config)::

  printf 'bob\\nbobpw\\n' | python3 examples/openmux_testauth_helper.py openmux-web

POINT A SERVER AT IT (test only -- never use in production)
-----------------------------------------------------------
  authentication.yaml:
    external_auth:
      enabled: true
      helper: python3 /abs/path/to/openmux/examples/openmux_testauth_helper.py
      service: openmux
      timeout: 10

"""
import json
import sys

# Per-user credentials + synthetic reply (edit freely)
USERS = {
	"admin": {
		"password": "secret123",
		"reply": {
			"uid": 1000,
			"gid": 1000,
			"full_name": "Demo Admin",
			"home": "/home/admin",
			"shell": "/bin/bash",
			"groups": ["admin", "users"],
			"pam": {"tty": "pts/0", "rhost": "127.0.0.1", "ruser": ""},
			"env": ["LANG=en_US.UTF-8", "TERM=xterm-256color"],
		},
	},
	"alice": {
		"password": "alicepw",
		"reply": {
			"uid": 1101,
			"gid": 1101,
			"full_name": "Alice Example",
			"home": "/home/alice",
			"shell": "/bin/zsh",
			"groups": ["openmux_admin", "users", "dev"],
			"pam": {"tty": "pts/1", "rhost": "10.0.0.25", "ruser": ""},
			"env": ["LANG=en_US.UTF-8", "TERM=screen"],
		},
	},
	"bob": {
		"password": "bobpw",
		"reply": {
			"uid": 1102,
			"gid": 1102,
			"full_name": "Bob Example",
			"home": "/home/bob",
			"shell": "/bin/zsh",
			"groups": ["users", "dev"],
			"pam": {"tty": "pts/1", "rhost": "10.0.0.25", "ruser": ""},
			"env": ["LANG=en_US.UTF-8", "TERM=screen"],
		},
	},
}


def main():
	# Protocol: openmux-pam-helper [<service>]
	#   stdin line 1: username
	#   stdin line 2: password
	service = sys.argv[1] if len(sys.argv) > 1 else "openmux"

	username = sys.stdin.readline()
	password = sys.stdin.readline()
	if not username or not password:
		print(json.dumps({"ok": False, "error": "missing_input"}), file=sys.stderr)
		sys.exit(2)
	username = username.rstrip("\n")
	password = password.rstrip("\n")

	entry = USERS.get(username)
	if not entry or entry.get("password") != password:
		print(json.dumps({"ok": False, "error": "authentication_failed", "service": service}))
		sys.exit(1)

	r = entry["reply"]
	out = {
		"ok": True,
		"user": username,
		"uid": r.get("uid", 0),
		"gid": r.get("gid", 0),
		"full_name": r.get("full_name", ""),
		"home": r.get("home", ""),
		"shell": r.get("shell", ""),
		"groups": r.get("groups", []),
		"pam": {
			"service": service,
			"tty": r.get("pam", {}).get("tty", ""),
			"rhost": r.get("pam", {}).get("rhost", ""),
			"ruser": r.get("pam", {}).get("ruser", ""),
		},
		"env": r.get("env", []),
		"service_used": service,
	}

	print(json.dumps(out, separators=(",", ":")))
	sys.exit(0)


if __name__ == "__main__":
	main()
