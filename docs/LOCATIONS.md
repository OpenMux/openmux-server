# Locations and environment

The server resolves every path it reads or writes through one precedence
chain, in `openmux/server/locations.py`:

| # | Source | Example |
|---|---|---|
| 1 | process environment (`OPENMUX_*`) | `OPENMUX_RUN_DIR=/run/openmux` |
| 2 | `/etc/defaults/openmux` (the defaults file) | same key, in the file |
| 3 | systemd directory variables | `$RUNTIME_DIRECTORY`, `$STATE_DIRECTORY`, `$LOGS_DIRECTORY` |
| 4 | built-in dev default | `logs/`, `~/.openmux` |

An empty or unset value at each level falls through to the next. The three
locations:

| Location | Variable | Packaged default | Dev default |
|---|---|---|---|
| logs (aggregate + `ports/`) | `OPENMUX_LOG_DIR` | `/var/log/openmux` | `logs/` |
| runtime (pidfile, control socket) | `OPENMUX_RUN_DIR` | `/run/openmux` | `logs/` |
| state (muxcon TLS, ssh host keys, web TLS) | `OPENMUX_STATE_DIR` | `/var/lib/openmux` | `~/.openmux` |

- Tier 1 beats the defaults file, the unit, and every launch mode.
- Tier 2 is the single relocation point that works in every launch mode.
- Tier 3 only exists when systemd starts the service and the unit declares
  the matching `*Directory=` (the Debian package does: its
  `RuntimeDirectory=` etc. are the packaged defaults). An absent variable
  never breaks resolution; it falls through.
- Dev runs (no unit, no file) keep tier 4, unchanged.

## The defaults file

`/etc/defaults/openmux` holds directory locations only, as `KEY=VALUE`
lines (systemd `EnvironmentFile` compatible, so a unit and the server read
the same file). The package ships it with the three values commented out:

```sh
#OpenMUX_LOG_DIR=/var/log/openmux
#OpenMUX_RUN_DIR=/run/openmux
#OPENMUX_STATE_DIR=/var/lib/openmux
```

- Relocate OpenMux directories here, not in the unit. The unit's
  `*Directory=` lines are the packaged defaults; a value in this file
  overrides them. If you change a default in the unit only, the server
  moves but `openmuxctl` does not - keep the file in sync.
- Never put secrets in the file. It must stay 0644. In particular,
  `OPENMUX_PUBKEY_PASSPHRASE` is always set via the process environment or
  the config, never in the file.
- The file is read directly by the server and by `openmuxctl`; the
  `OPENMUX_ENV_FILE` environment variable points other tools (tests,
  Docker, manual starts) at a different file.

## openmuxctl resolution

`openmuxctl` runs in a user shell: it sees neither the server's exported
variables nor systemd's. Its control-socket resolution:

1. `--socket PATH`
2. `OPENMUX_CTL_SOCK` (shell, then the defaults file)
3. `OPENMUX_RUN_DIR` (shell, then the defaults file); the socket is
   `openmux.sock` inside it
4. the packaged run dir (`/run/openmux`), probed for existence
5. the dev default `logs/openmux.sock`

## Where things live

- Logs: `OPENMUX_LOG_DIR` (packaged `/var/log/openmux`), with `ports/`
  inside.
- Runtime: `OPENMUX_RUN_DIR` (packaged `/run/openmux`): `openmux.pid` and
  `openmux.sock`.
- State: `OPENMUX_STATE_DIR` (packaged `/var/lib/openmux`):
  `muxcon/` (certs, keys, known peers, federated cache), `ssh_listener/`
  (host keys), `web_console/` (web TLS).
- Port-action scripts: `port_actions.actions_dir` in config (packaged
  default `/etc/openmux/actions`, created by the package at 0750).
- Web UI assets: inside the python package
  (`openmux/server/webui/`); never copied at install or boot.

## Debian packaging

`/usr/lib/systemd/system/openmux-server.service`:

- `RuntimeDirectory=openmux` / `StateDirectory=openmux` /
  `LogsDirectory=openmux` create the three FHS dirs owned by `openmux`
  before the service starts (and recreate `/run/openmux` on every boot,
  since `/run` is tmpfs).
- `EnvironmentFile=-/etc/defaults/openmux` applies the admin overrides;
  the `-` keeps the service startable if the file is removed.
- The postinst script creates `/etc/openmux/actions` (0750
  `openmux:openmux`) for port-action scripts.
