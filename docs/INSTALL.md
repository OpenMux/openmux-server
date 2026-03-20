# OpenMux Install and Build Guide

This guide covers installing and running OpenMux via multiple supported paths.

Most methods installs the same console scripts:
- `openmux-server` (server)
- `openmux-client` (client)
- `openmuxctl` (local control CLI via Unix domain socket)

Minimum Python: 3.9

---

## Pip / Wheel install

Install directly from a built wheel or from the source tree using pip.

```sh
# From a built wheel (recommended for prod)
python3 -m pip install dist/openmux-*.whl

# Or install from source (editable is fine for dev machines)
python3 -m pip install -e .

# Extras:
#   web UI pieces and templates are already part of core; extras are optional
#   PAM auth support requires python-pam and six
python3 -m pip install -e ".[pam]"
```

Run:

```sh
openmux-server -c config/loopback_test.yaml
# Local control from the same host
openmuxctl status
openmuxctl reload --soft
openmuxctl reload --full
```

Uninstall:

```sh
python3 -m pip uninstall openmux
```

---

## Install from source in a virtualenv (development)

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip setuptools wheel
pip install -e .[dev]

# Optional: PAM support
pip install -e ".[pam]"

# Run server
python -m openmux.server.main -c config/loopback_test.yaml

# Control
python -m openmux.cli.openmuxctl status
```

Makefile helpers:

```sh
make venv            # create venv and install package editable
make venv-dev        # venv + dev deps
make run-server      # run openmux.server.main
make test            # run tests
```

---

### Run from source without installing the package

You can run OpenMux directly from the source tree without `pip install .`. Install only the third‑party dependencies, then invoke the modules with `python -m ...`.

```sh
# Create and activate a venv
python3 -m venv .venv
. .venv/bin/activate

# Install runtime dependencies
pip install -r requirements.txt
# Optional (developer tooling)
pip install -r requirements-dev.txt

# Run the server directly from source
python -m openmux.server -c config/loopback_test.yaml

# Control via module path (since console scripts aren’t installed)
python -m openmux.cli.openmuxctl status
python -m openmux.cli.openmuxctl reload --soft

# Client (interactive)
python -m openmux.client --server localhost --port 8023
# Or list ports first
python -m openmux.client --list --server localhost --port 8023
```

Notes:
- Without installing the package, the console scripts (`openmux-server`, `openmux-client`, `openmuxctl`) are not created on your `PATH`. Use `python -m openmux.server`, `python -m openmux.client`, and `python -m openmux.cli.openmuxctl` instead.
- Running from the repository root works because Python includes the current directory on `sys.path`. If running from elsewhere, set `PYTHONPATH=/path/to/checkout`.
- Prefer not to use pip? `uv` is a fast alternative:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv
. .venv/bin/activate
uv pip install -r requirements.txt
python -m openmux.server -c config/loopback_test.yaml
```

---

## Debian package (.deb) — no venv, no pip

Builds a native Debian package using debhelper+pybuild and installs into
system Python’s dist-packages, exposing the same console scripts.

### Build dependencies

```sh
sudo apt-get update
sudo apt-get install -y \
  dpkg-dev devscripts debhelper dh-python python3-all pybuild-plugin-pyproject
```

### Runtime dependencies

These mirror the pip requirements with Debian-native packages:

```sh
sudo apt-get install -y \
  python3-aiohttp python3-jinja2 python3-websockets \
  python3-cryptography python3-serial python3-serial-asyncio \
  python3-yaml python3-six

# Optional (only if enabling PAM auth):
sudo apt-get install -y python3-pam
```

### Build the package

From the repository root:

```sh
# Using Makefile helper
make deb

# Or directly
dpkg-buildpackage -us -uc -b
```

Artifacts are placed one directory up, e.g. `../openmux_1.0.0-1_all.deb`.

### Automated Debian versioning

OpenMux includes a small helper to keep the Debian package version in sync with `pyproject.toml` and to optionally add a snapshot suffix for nightly builds.

- Script: `scripts/update_deb_changelog.py`
  - Reads `project.version` from `pyproject.toml`
  - Writes `debian/changelog` with an RFC 2822 date
  - Supports a Debian revision and optional snapshot suffix
  - No external dependencies (pure stdlib)

- Makefile integration: the `make deb` target runs the updater automatically before `dpkg-buildpackage`.

Controls (via env or make variables):

- `DEB_REVISION` (default `1`): Debian revision appended as `-<rev>`
- `DEB_DIST` (default `unstable`): distribution field in changelog
- `DEB_SNAPSHOT` (default `off`): set to `auto` to append `~gitYYYYMMDDHHMM` (and short SHA when available)

Examples:

```sh
# Standard release from pyproject version (e.g., 1.0.0-1)
make deb

# Bump Debian revision, set suite
make DEB_REVISION=2 DEB_DIST=sid deb

# Nightly snapshot builds (e.g., 1.0.0-1~git202510270617.ab12cd3)
make DEB_SNAPSHOT=auto deb
```

Run the updater directly (useful in CI scripts):

```sh
# Show the generated changelog entry without writing
python3 scripts/update_deb_changelog.py --dry-run --snapshot auto

# Write changelog, then build
python3 scripts/update_deb_changelog.py --revision 1 --dist unstable --snapshot auto \
  --message "Nightly build"
dpkg-buildpackage -us -uc -b
```

Minimal GitHub Actions snippet for snapshot .deb artifacts:

```yaml
jobs:
  deb:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install build deps
        run: |
          sudo apt-get update
          sudo apt-get install -y dpkg-dev devscripts debhelper dh-python python3-all pybuild-plugin-pyproject
      - name: Build snapshot .deb
        run: |
          make DEB_SNAPSHOT=auto deb
      - uses: actions/upload-artifact@v4
        with:
          name: deb-packages
          path: ../openmux_*_all.deb
```

### Install / remove the package

```sh
sudo apt-get install -y ../openmux_1.0.0-1_all.deb

# Remove
sudo apt-get remove -y openmux
```

### Run (after .deb install)

```sh
openmux-server -c /etc/openmux/server.yaml   # if you place your config under /etc
# or point to a project config
openmux-server -c /path/to/loopback_test.yaml
openmux-server --config-dir /etc/openmux     # loads server/auth/security sidecars
openmux-server -c server.yaml -a authentication.yaml -s security.yaml

openmuxctl status
openmuxctl reload --soft
```

---

## Install xterm.js Assets

The Web Console depends on locally bundled xterm.js files. Run the helper script before enabling `web_console`:

```sh
scripts/install_xtermjs.py                 # installs into ./static by default
scripts/install_xtermjs.py --force         # re-download even if files exist
scripts/install_xtermjs.py --static-dir /var/lib/openmux/static
```

The script exits with an error if any of `xterm.js`, `xterm.css`, or the fit addon fail to download, so failures are caught during installation—not at runtime.

Recommended approach:

- Use xterm.js as vendored static assets under `static/`.
- Do not require Node.js or `npm` on deployment targets just to run OpenMux.
- Do not fetch xterm.js during Debian package builds or at service startup.
- Pin the xterm.js and `xterm-addon-fit` versions during development or release preparation, then ship those files in the source tree and packages.

This is the lowest-friction option for OpenMux because it keeps packaging simple, avoids network access during installation, and ensures the Web Console works without any runtime CDN dependency.

---

## Configuration locations and defaults

- Use `--config-dir /path/to/config` to load `server.yaml`, `authentication.yaml`, and `security.yaml` from the same directory, or point directly at a server YAML via `-c/--config`.
- Override sidecar locations explicitly with `-a/--auth-config` and `-s/--security-config`.
- Control socket and pidfile defaults (can be overridden in config or env):
  - `server.control_socket`: `logs/openmux.sock` (override with `OPENMUX_CTL_SOCK`)
  - `server.pidfile`: `logs/openmux.pid` (override with `OPENMUX_PIDFILE`)

Example minimal config is provided at `config/loopback_test.yaml`.

---

## Troubleshooting

- PAM errors like `ModuleNotFoundError: No module named 'six'`:
  - Ensure `python3-six` is installed (or `pip install six` if using pip)
- TLS autogen failure complaining about cryptography:
  - Install `python3-cryptography`
- Control socket permission denied:
  - The socket is created with 0600; run `openmuxctl` as the same user that started the server, or change the path to a root-owned directory only if you also run the server as root.
- Schema/UI in the web console not loading:
  - The server falls back to a permissive schema if the YAML schema isn’t found. Ensure `docs/to_check/openmux_config_schema.yaml` exists when running from the repo.

---

## Optional: systemd unit (example)

You can create `/etc/systemd/system/openmux-server.service` like this:

```ini
[Unit]
Description=OpenMux Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=openmux
Group=openmux
ExecStart=/usr/bin/openmux-server -c /etc/openmux/server.yaml
WorkingDirectory=/var/lib/openmux
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now openmux-server
```

---

## Optional: “Install” without pip

If you don’t want to use pip to install the package and also don’t have a Debian system, you can create simple wrapper scripts that call the source tree directly. For example, on macOS or other Unix systems:

```sh
# Assuming your checkout lives at /opt/openmux (adjust path accordingly)
sudo install -d -m 0755 /opt/openmux
sudo rsync -a --delete ./ /opt/openmux/

# Create lightweight launchers on PATH
sudo tee /usr/local/bin/openmux-server >/dev/null <<'SH'
#!/usr/bin/env sh
exec /usr/bin/env python3 -m openmux.server "$@"
SH
sudo chmod +x /usr/local/bin/openmux-server

sudo tee /usr/local/bin/openmux-client >/dev/null <<'SH'
#!/usr/bin/env sh
exec /usr/bin/env python3 -m openmux.client "$@"
SH
sudo chmod +x /usr/local/bin/openmux-client

sudo tee /usr/local/bin/openmuxctl >/dev/null <<'SH'
#!/usr/bin/env sh
exec /usr/bin/env python3 -m openmux.cli.openmuxctl "$@"
SH
sudo chmod +x /usr/local/bin/openmuxctl
```

You still need the Python dependencies available. Either:
- Use a virtual environment and activate it before running the scripts, or
- Install runtime dependencies system‑wide: `pip install -r /opt/openmux/requirements.txt` (or with `uv pip install ...`).

This provides runnable commands without packaging or pip‑installing the project itself.

## Reference

- Full docs and architecture: see `docs/README.md` and `docs/ARCHITECTURE.md`.
- Defaults and config keys: `docs/DEFAULTS.md`
- Example configs: `config/`
