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

# Install required dependencies
pip install -e .

# For web interface support
pip install -e ".[web]"

# For PAM authentication support (and web together)
pip install -e ".[web,pam]"
```

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

