Smoke Test Workflow

This blueprint provides a fast end‑to‑end check that the OpenMux server and client path works across key adapters: loopback, command, and (optionally) serial.

Artifacts
- Server config: `config/integration_test.yaml` (ports: `loop1`, `cat`, and optional `vserial1`)
- Script: `scripts/smoke_workflow.py` (starts server, runs client flows)

Prerequisites
- Python venv with project installed (`pip install -r requirements.txt`)
- Optional serial test: install `socat` and run `./setup_virtual_serial.sh` in another terminal

Quick Run
1) Start optional virtual serials (macOS):

```sh
brew install socat    # once
./setup_virtual_serial.sh
```

2) Run the smoke test:

```sh
python3 scripts/smoke_workflow.py --server-config config/integration_test.yaml
```

What it does
- Starts the OpenMux server on 127.0.0.1:8123 using the test config
- Uses the TCP client adapter to:
  - Authenticate as `admin` / `password` (from `config/authentication.yaml`)
  - LIST ports and ensure presence of `loop1` and `cat`
  - For `loop1`: write `hello-loop\n` and expect an echoed payload
  - For `cat`: write `hello-cat\n` and expect an echoed payload
  - For `vserial1` (if `/tmp/vserial1` exists): write `hello-serial\n` and expect an echoed payload from the peer

Exit codes
- 0: success
- non‑zero: failure (prints minimal diagnostic output)

Notes
- The serial test is optional; it will be skipped if `/tmp/vserial1` is absent.
- You can customize the `command_ports` entry to another interactive command if `/bin/cat` is unavailable.

---

Reload verification (signals)

You can trigger reloads from the CLI without using the UI. The server writes a PID file on startup (default `logs/openmux.pid`, override with `OPENMUX_PIDFILE` env or `server.pidfile` in config).

- Soft reload (SIGHUP):

  ```zsh
  kill -HUP $(cat logs/openmux.pid)
  ```

  Expected:
  - Logs show `[reload-soft:sighup]` messages and `AuthManager updated` when applicable
  - No listener restarts; only in-place updates and port reconciliations

- Full reload (SIGUSR1):

  ```zsh
  kill -USR1 $(cat logs/openmux.pid)
  ```

  Expected:
  - Logs show `[reload-full:sigusr1]` with counts of stopped/created/started and any errors
  - If triggered from the Web Console, restart is deferred to avoid self-stop during the HTTP request

Notes:
- On macOS and Linux, both SIGHUP and SIGUSR1 are available. If a signal isn't supported on your platform, you'll see a log line indicating that handler registration was skipped.
- If you enable log rotation externally, you can send SIGHUP to re-apply log levels from config. Full file re-open can be added later if needed.

---

Local control socket and openmuxctl

Phase 2 adds a Unix domain control socket for local commands (status, soft/full reload) and a tiny CLI helper.

- Default socket path: `logs/openmux.sock` (override with env `OPENMUX_CTL_SOCK` or `server.control_socket` in config)
- Permissions: the server restricts the socket file to `0600` on startup

Quick usage with the helper script:

```zsh
# Status
python3 scripts/openmuxctl.py status

# Soft reload (auth + in-place port reconcile)
python3 scripts/openmuxctl.py reload --soft

# Full reload (stop/recreate/start adapters)
python3 scripts/openmuxctl.py reload --full

# Use a custom socket path
python3 scripts/openmuxctl.py --socket /path/to/openmux.sock status
```

Expected:
- Each command prints a compact JSON result to stdout
- On reload, the server logs include the same `[reload-soft:*]` or `[reload-full:*]` phases as signal-triggered reloads

Troubleshooting:
- If you see “Control socket not found”, ensure the server started successfully and the socket path is correct
- On permission issues, verify ownership and that the socket is created with `0600` permissions under your user
