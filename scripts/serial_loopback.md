# serial_loopback.py

Standalone serial loopback utility for OpenMux testing.

Echoes all data received on a serial port back to the sender, applying the
same control-character sanitization and line-boundary annotation used by the
in-process `LoopbackPort` adapter.  Designed to sit on one end of a physical
or virtual null-modem pair while an OpenMux serial adapter connects on the other.

## Requirements

```
pip install pyserial-asyncio
```

`pyserial-asyncio` is already listed as an OpenMux project dependency.

---

## Quick start

```bash
# Basic loopback on USB serial at 115200 baud
python scripts/serial_loopback.py --port /dev/ttyUSB0 --baud 115200

# Named port, hardware flow control, control line monitoring, verbose logging
python scripts/serial_loopback.py \
    -p /dev/ttyUSB0 \
    -n "Asterix Console" \
    --baud 115200 \
    --flow-control rts-cts \
    --monitor-lines \
    -v
```

---

## Virtual null-modem pair with socat (for testing without hardware)

```bash
# Create a linked PTY pair
socat -d -d PTY,raw,echo=0 PTY,raw,echo=0
# socat reports two device paths, e.g. /dev/pts/4 and /dev/pts/5

# Run loopback on one end
python scripts/serial_loopback.py -p /dev/pts/4 -n "TestPort"

# Point the OpenMux serial adapter at the other end (/dev/pts/5) in server.yaml
```

---

## Arguments

### Serial port

| Argument | Short | Default | Description |
|---|---|---|---|
| `--port DEVICE` | `-p` | *(required)* | Serial device path, e.g. `/dev/ttyUSB0` or `/dev/pts/3` |
| `--name NAME` | `-n` | basename of port | Human-friendly name shown in the summary page and event messages |
| `--baud RATE` | `-b` | `9600` | Baud rate |
| `--bytesize` | | `8` | Data bits: `5`, `6`, `7`, or `8` |
| `--parity` | | `N` | `N`=None  `E`=Even  `O`=Odd  `M`=Mark  `S`=Space |
| `--stopbits` | | `1.0` | Stop bits: `1`, `1.5`, or `2` |
| `--timeout` | | `1.0` | Serial read timeout in seconds |
| `--flow-control` | | `none` | `none`, `rts-cts`, or `xon-xoff` |

### Echo behaviour

| Argument | Default | Description |
|---|---|---|
| `--echo-delay SECS` | `0.0` | Artificial delay before echoing each received chunk |
| `--no-sanitize` | off | Echo raw bytes; skip control-character sanitization |
| `--no-banner` | off | Suppress the `[ENTER on <name>]` annotation on newline bytes |

### Monitoring

| Argument | Default | Description |
|---|---|---|
| `--summary-key CHAR` | `\x14` (Ctrl+T) | Byte received from the remote end that triggers the status summary page; consumed and not echoed back |
| `--monitor-lines` | off | Print a `[LINE ...]` notice on the serial link whenever a control line changes state |
| `--monitor-interval SECS` | `0.1` | Polling interval for control line state checks |

### Connection

| Argument | Default | Description |
|---|---|---|
| `--no-reconnect` | off | Exit on disconnect instead of attempting to reconnect |
| `--reconnect-delay SECS` | `5.0` | Seconds to wait between reconnection attempts |

### Logging

| Argument | Short | Description |
|---|---|---|
| `--verbose` | `-v` | Enable DEBUG-level logging to stderr |
| `--quiet` | `-q` | Suppress all output except errors |

---

## Features

### Control-character sanitization

Enabled by default (disable with `--no-sanitize`).  Incoming bytes are
converted to readable bracketed tokens before being echoed back:

| Input | Echoed as |
|---|---|
| Arrow keys | `[UP]` `[DOWN]` `[LEFT]` `[RIGHT]` |
| Home / End | `[HOME]` `[END]` |
| Page Up/Dn | `[PGUP]` `[PGDN]` |
| Insert | `[INSERT]` |
| Delete (0x7F) | `[DEL]` |
| Escape | `[ESC]` |
| Tab | `[TAB]` |
| Ctrl+X | `[CTRL-X]` |
| NUL | `[NUL]` |
| Printable ASCII, CR, LF, bytes ≥ 0x80 | passed through unchanged |

### Line-boundary banner

When a newline byte is received the script echoes `[ENTER on <name>]\r\n`
after the sanitized line content.  Disable with `--no-banner`.

### Status summary page

Send the summary key (default **Ctrl+T**, `\x14`) from the remote side at
any time.  The key is stripped from the echo stream and the script writes
back a plain-text ASCII box:

```
+------------------------------------------------+
| OpenMux Serial Loopback -- Asterix Console     |
+------------------------------------------------+
| Port      : /dev/ttyUSB0                       |
| Baud      : 115200  Format : 8N1               |
| Flow ctrl : none                               |
+------------------------------------------------+
| Uptime    : 00:05:23                           |
| Bytes in  : 1,234   Bytes out : 1,234          |
| Chars in  : 1,200   Reconnects: 0              |
+------------------------------------------------+
| Lines (out): DTR=ON   RTS=ON                   |
| Lines (in) : CTS=ON   DSR=ON                   |
|              DCD=OFF  RI =OFF                  |
+------------------------------------------------+
| Press Ctrl+T for this summary                  |
+------------------------------------------------+
```

Control line states are read from the underlying pyserial `Serial` object
exposed by `serial_asyncio` (`writer.transport.serial`).

**Output lines** (driven by this script): `DTR`, `RTS`  
**Input lines** (driven by the remote end): `CTS`, `DSR`, `DCD`, `RI`

### Control line monitoring (`--monitor-lines`)

A polling coroutine checks all six control line states every
`--monitor-interval` seconds.  When any line changes state a notice is
written immediately to the serial link:

```
[LINE Asterix Console: CTS: ON ->OFF  DSR: OFF->ON ]
```

Multiple simultaneous changes are coalesced into a single message.  All
writes (echo, banner, summary, line events) are serialised through a shared
`asyncio.Lock` to prevent interleaving.

### Auto-reconnect

The supervisor loop retries automatically after each disconnect or failed
connection attempt, waiting `--reconnect-delay` seconds between tries.  Set
`--no-reconnect` to exit on the first disconnect instead.  If the device
file does not exist the script polls silently until it appears (with a
rate-limited warning logged at most once per hour).

---

## systemd template unit

Save as `/etc/systemd/system/openmux-loopback@.service`.  The instance name
is the systemd-escaped device path (use `systemd-escape --path /dev/ttyUSB0`
to get the escaped form).

```ini
[Unit]
Description=OpenMux Serial Loopback - %i
Documentation=file:///opt/openmux/scripts/serial_loopback.py
After=network.target

# Bind lifetime to the USB device; stops on unplug, starts on re-plug.
# %i = systemd-escaped device path, e.g. dev-ttyUSB0
After=%i.device
BindsTo=%i.device

[Service]
Type=simple
User=openmux
Group=dialout

# %I is the unescaped instance name: dev-ttyUSB0 → dev/ttyUSB0 → /dev/ttyUSB0
ExecStart=/opt/openmux/.venv/bin/python /opt/openmux/scripts/serial_loopback.py \
    --port /%I \
    --name "%i" \
    --baud 115200 \
    --flow-control none \
    --monitor-lines \
    --reconnect-delay 5

Restart=on-failure
RestartSec=10
TimeoutStopSec=10

StandardOutput=journal
StandardError=journal
SyslogIdentifier=openmux-loopback-%i

[Install]
WantedBy=multi-user.target
```

**Usage:**

```bash
# Start for /dev/ttyUSB0
sudo systemctl enable --now "openmux-loopback@$(systemd-escape --path /dev/ttyUSB0).service"

# Start for /dev/ttyACM1
sudo systemctl enable --now "openmux-loopback@$(systemd-escape --path /dev/ttyACM1).service"

# Follow logs
journalctl -u "openmux-loopback@dev-ttyUSB0" -f
```

**Per-device overrides** (baud rate, name, etc.) without editing the template:

```bash
sudo systemctl edit openmux-loopback@dev-ttyUSB0
```

This creates `/etc/systemd/system/openmux-loopback@dev-ttyUSB0.service.d/override.conf`
where you can override `ExecStart` with a fully customised command line.

---

## Notes

- The `dialout` group (Linux) or `uucp` group (macOS) grants access to serial
  devices without `root`.  Add the service user: `usermod -aG dialout openmux`.
- The script has **no OpenMux module dependencies** — it only requires
  `pyserial-asyncio` and the Python standard library.
- All log output goes to **stderr**; the serial link carries only echo data,
  summary pages, and event notices.
