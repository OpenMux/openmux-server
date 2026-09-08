# OpenMux Client Configuration

This client supports a config file (YAML or JSON) so you don't have to pass `-s`, `-p`, or TLS flags every time.

First match wins from the search order below.

- `OPENMUX_CLIENT_CONFIG` (absolute path)
- Current directory: `client.(yaml|yml|json)`, `openmux_client.*`, `openmux-client.*` (including dotfile variants)
- XDG: `$XDG_CONFIG_HOME/openmux/client.*` or `~/.config/openmux/client.*`
- macOS: `~/Library/Application Support/OpenMux/client.*`
- Home dotfiles: `~/.openmux_client.*`, `~/.openmux-client.*`, `~/.openmux/client.*`
- System: `/etc/openmux/client.*`, `/usr/local/etc/openmux/client.*`

See `docs/examples/client.yaml` for a complete template.

## Schema (YAML)

- `servers`: list of servers to use for listing and discovery
  - `name`: friendly name
  - `host`: hostname or IP
  - `port`: TCP port (default 8023)
  - `username`, `password`, `api_key`: credentials for this server
  - `pubkey_path`, `pubkey_id`: Ed25519 key for this server
- `default_server`: name or host to use when `-s` is omitted
- `use_tls`: default TLS behavior (overridden by CLI)
- `logging`: optional logging config for the client
  - `log_level`: one of DEBUG, INFO, WARNING, ERROR, CRITICAL (default WARNING)
  - `file_only`: log to file only, no console output (default false)
  - `file_logging_enabled`: also write to a log file (default false)
  - `log_dir`: log directory (default `logs`)
  - `log_file`: log file name (default `openmux_client.log`)
  - `log_max_size_mb`: rotation threshold in MB, 0 disables rotation (default 10)
  - `log_backups`: rotated backup count, 0 disables rotation (default 5)

Example:

```yaml
servers:
  - name: lab-hub
    host: 127.0.0.1
    port: 8023
  - name: remote1
    host: remote1.example.com
    port: 8023

default_server: lab-hub
use_tls: false

logging:
  log_level: INFO
  file_logging_enabled: true
  log_dir: logs
  log_file: openmux_client.log
  log_max_size_mb: 10
  log_backups: 5
```

## CLI Precedence

- `-s` picks a server by name or host. With `-s`, `-p` sets the port (omit it for 8023).
- Without `-s`, the chosen server entry's own `port` is used; `-p` only applies when the entry has no `port`.
- Server credentials resolve CLI first, then the chosen server entry (`-u`/`-w`/`-k`, `--pubkey`/`--pubkey-id`). The CLI value wins when both are set.
- If `--encrypt/--no-encrypt` is omitted, `use_tls` decides; default is `false`.
- Logging: the client rebuilds the logging config from its CLI flags (`-v`, `--log-file`, `--log-dir`, `--log-max-size`, `--log-backups`, `--quiet`) on top of `logging:`. Only `file_only` takes effect from the config file.

## Quick Start

- Put your config at `~/.config/openmux/client.yaml`:

```bash
mkdir -p ~/.config/openmux
cp docs/examples/client.yaml ~/.config/openmux/client.yaml
```

- List ports using the default server:

```bash
openmux-client --list
```

- Connect to a port using the default server:

```bash
openmux-client tty-R1
```

- Override server and TLS on the fly:

```bash
openmux-client --list -s other-host -p 9000 --encrypt
openmux-client --no-encrypt tty-R2
```
