# OpenMux

> No license yet: This repository is not currently licensed. You may not use, copy, modify, or distribute this code without explicit permission from the authors. The license will be added later.

OpenMux is a Serial Controller Daemon and client for remotely controlling and logging multiple serial ports on a device. The server acts as the control daemon, and the client can control multiple servers from the command line.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run server
python -m openmux.server -c config/server.yaml

# Connect with client
python -m openmux.client server.example.com
```

### Configuration Files

OpenMux now keeps credentials and security policy in dedicated sidecar files so they can be managed independently of the main server config:

- `config/server.yaml` – core server metadata plus adapter sections (serial, loopback, tcp initiator, muxcon, etc.).
- `config/authentication.yaml` – users, API keys, public keys, and PAM settings.
- `config/security.yaml` – adapter/module allow-lists, Config Editor writable sections, and authentication rate-limit overrides.

When starting the server manually, pass all three paths so reloads and the Config Editor stay in sync:

```bash
python -m openmux.server \
  -c config/server.yaml \
  -a config/authentication.yaml \
  -s config/security.yaml

# long-form flags `--auth-config` and `--security-config` remain available if you prefer explicit names.
```

Optionally you can also specify the directory with these config files instead using the `--config-dir <dir>` command.

```
python -m openmux.server --config-dir ./config/
```

### Web Console Assets

The Web Console serves bundled xterm.js files from `static/`. Download the correct versions before enabling the adapter:

```bash
scripts/install_xtermjs.py

# optional flags
scripts/install_xtermjs.py --force            # re-download even if files exist
scripts/install_xtermjs.py --static-dir /var/lib/openmux/static
```

The script verifies that `xterm.js`, `xterm.css`, and the fit addon are present before exiting so browsers never rely on CDN fetches at runtime.

## Features

### Server
- **Modular Adapter System**: Support for serial devices, SSH/Telnet, TCP/SSL connections, and loopback testing
- Authentication with username/password or API key
- Dynamic configuration reloading without disconnecting clients
- Multiple serial ports active simultaneously
- Continuous logging with file rotation
- Automatic reconnection of failed serial ports
- Multiple concurrent users per console
- Read-write and read-only access modes
- Configurable permissions and read-write access control
- Network binding to specific interfaces or localhost
- Optional raw WebSocket per-port streaming (web_console adapter + websocket client adapter)

### Client
- Secure communication with server
- Interactive console application
- List available console ports with their status
- Connect to remote ports
- User promotion from read-only to read-write with hotkey
- Session disconnection and reconnection capabilities

### Web Client
- Web-based console sessions
- Real-time data display

## Operations Quick Reference


Default locations (may vary by install method):

- Config: `/etc/openmux/server.yaml`
- Virtualenv: `/opt/openmux/venv`
- Working directory: `/opt/openmux`
- Logs: `logs/openmux_*.log` (when running from source) or `journalctl` under systemd

Health and discovery endpoints (web_console adapter enabled):

- `GET /healthz` and `GET /livez`: Always 200 OK when healthy; may return plain text or JSON details when enabled
- `GET /readyz`: 200 OK when ready; 503 otherwise
- `GET /api/ports`: Lists available port names and basic status for discovery/clients

## Reloads and local control CLI

Two ways to trigger a live reload without full restart:

- POSIX signals (from the same host):
  - Soft reload: `SIGHUP` (validate config, add/update/remove adapters incrementally, preserve active sessions when safe)
  - Full reload: `SIGUSR1` (restart all adapters; brief interruption possible)
- Local control socket via CLI:
  - Installable command: `openmuxctl` (provided by this package)
  - Examples (run on the server host):
    - `openmuxctl status` → JSON summary (pid, uptime, adapters)
    - `openmuxctl reload soft` → soft reload
    - `openmuxctl reload full` → full reload

Control socket details:

- Default path: `logs/openmux.sock` (created with permissions 0600)
- Config keys (under `server:`):
  - `server.control_socket`: override control socket path
  - `server.pidfile`: override pid file path (default `logs/openmux.pid`)
- Environment overrides:
  - `OPENMUX_CTL_SOCK` for the control socket path
  - `OPENMUX_PIDFILE` for the pid file path

## Installation

### From Source

```bash
# Clone the repository
git clone https://github.com/yourusername/openmux.git
cd openmux

# Optionally create a virtualenv
# Some systems require the use of venv, as they don't allow modification of the 
# built-in python libraried outside its own package-repository
python3 -m venv .venv

#NOTE: When using venv, the venv needs to be loaded prior to using any openmux commands.
source .venv/bin/activate


# Install required dependencies
pip install -e .

# For web interface support
pip install -e ".[web]"

# For PAM authentication support (and web together)
pip install -e ".[web,pam]"
```

## Configuration

By default the CLI above assumes the configuration directory layout from `config/`. Packaged deployments may place these YAML files under `/etc/openmux/`. All of the examples below reference the sidecar structure described earlier.

### Example Server Configuration

Create a YAML configuration file for the server:

```yaml
server:
  id: rack-controller
  description: "Primary lab console host"
  control_socket: logs/openmux.sock
  pidfile: logs/openmux.pid

client_listener:
  enabled: true
  host: 127.0.0.1
  port: 8023
  max_connections: 100
  connection_timeout: 30

logging:
  level: INFO
  console: true
  log_dir: logs

loopback_ports:
  - name: loop1
    description: Loopback test port
    max_read_write_users: 2

command_ports:
  - name: ssh_server
    description: SSH to remote server
    command: "ssh user@remote-server"
    max_read_write_users: 1

serial_ports:
  - name: console1
    description: Main Server Console
    device: /dev/ttyS0
    baudrate: 9600
    bytesize: 8
    parity: N
    stopbits: 1
    max_read_write_users: 1
```

Because authentication now lives in `config/authentication.yaml`, accompanying credentials are defined there:

```yaml
# config/authentication.yaml
users:
  - username: admin
    password_hash: <REPLACE_WITH_SECURE_HASH>
    permissions: admin
  - username: user1
    password_hash: <REPLACE_WITH_SECURE_HASH>
    permissions: read-write
api_keys:
  - name: lab-agent
    key: <REPLACE_WITH_RANDOM_SECRET>
    permissions: read-only
pam:
  enabled: false
```

### Security Policy (`config/security.yaml`)

The security sidecar enforces which adapters may run, which modules may be imported, and which Config Editor sections are writable. A minimal example:

```yaml
adapters:
  block_unlisted: true
  allowed_modules:
    - openmux.server.adapters.serial
    - openmux.server.adapters.loopback
    - openmux.server.adapters.command
    - openmux.server.adapters.tcp_initiator
  allowed_adapter_types:
    - serial
    - loopback
    - command
    - tcp_initiator

config_editor:
  writable_sections:
    - server
    - logging
    - serial_ports
```

Any adapter whose module or type is not listed is rejected before it can start. Leaving `config_editor.writable_sections` empty makes the UI read-only; omitting the block entirely keeps legacy behavior (all sections editable).

#### Command Adapter Privilege Drop
When the OpenMux server runs as root, the command adapter can optionally drop
to an unprivileged user before executing subprocesses. Configure the target
identity in `config/security.yaml`:

```yaml
command_adapter:
  drop_privileges:
    user: openmux
    group: openmux
    supplementary_groups:
      - dialout
    umask: 0o077
```

The drop only occurs when a privileged server (euid 0) launches the adapter.
If the server already runs as a non-root user, the command adapter logs that
it skipped the drop and continues normally. Supplementary groups and umask are
optional; omit them to keep the defaults.

#### Adapter Fail-Fast (Default-On)
The server aborts startup (exit code 2) if a top-level adapter-like section is present
but no plugin registered it (typo or import failure). Disable explicitly with either:
```
fail_fast_adapters: false
```
or inside `server:`:
```
server:
  fail_fast_adapters: false
```
Details: [Adapter Fail-Fast Mode](docs/configuration/adapter_fail_fast.md)



## Usage

### Starting the Server

```bash
openmux-server -c /path/to/server.yaml
```

### Using the Client

```bash
# Connect to default server
openmux-client

# Connect to a specific server
openmux-client -s 192.168.1.10 -p 8023

# List available ports
openmux-client -l

# Connect to a specific port
openmux-client port_name

# Send a command to a port (non-interactive)
openmux-client port_name "command"
```

### Web Interface

If the web server is enabled, access the web interface at:

```
http://server_address:8080/
```

## Client Control Keys

The client uses a two‑character escape sequence to issue local commands.

- Default escape introducer: `Ctrl+E` then `c` (displayed at startup as `Escape sequence: Ctrl+E c`).
- After the introducer, type one of the following commands:
  - `.`: Disconnect
  - `a`: Attach read‑write
  - `s`: Switch to spy (read‑only)
  - `i`: Show connection info
  - `w`: Who is using this console [not implemented]
  - `v`: Show version
  - `p` / `P`: Playback last N lines [not implemented] / Set playback lines
  - `r` / `R`: Replay last N lines [not implemented] / Set replay lines
  - `l`: List break sequences [not implemented]
  - `o`: Reconnect to session (manual reconnect)
  - `z`: Suspend (not supported)
  - `e`: Change escape sequence (enter two new characters)
  - `^M`: Continue (ignore escape)
  - `^R`: Replay last line [not implemented]
  - `\\ooo`: Send octal character
  - `?`: Show this help

Notes:
- `Ctrl+C` is forwarded to the remote by default (not intercepted).
- When disconnected, you can use `Ctrl+E` followed directly by a command (skip the `c`) for convenience.

## Security Notes

- Password hashes are stored as SHA-256 hashes
- API keys should be kept secure
- For production use, consider enabling TLS for all connections

## Development

### Dependencies

- Python 3.7+
- pyserial
- PyYAML
- FastAPI and Uvicorn (for web interface)

### Running Tests

```bash
# Run unit tests
python -m unittest discover tests
```


## AI-assisted contributions

Parts of this codebase and its documentation were created or refactored with assistance from AI coding tools (e.g., GitHub Copilot/Chat). All changes are reviewed by maintainers and validated by our automated test suite before merging.

- Ownership and licensing: The project’s license is not yet finalized. Contributions will be governed by the project’s eventual license once chosen. Until then, contributions are accepted for inclusion in this repository under the current "No license yet" status.
- Quality and accountability: If you spot unclear code, hallucinated comments, or suspicious logic, please open an issue or PR with details. We’ll prioritize fixes labeled with "ai-generated" or similar.
- Contributor guidance: When using AI tools, ensure you fully review the output, verify correctness, and include tests and documentation updates as needed. It’s helpful to mention AI assistance in your PR description for transparency.

## License

No license yet. All rights reserved by the authors. Until an explicit license is added, you may not use, copy, modify, or distribute this code without permission.

