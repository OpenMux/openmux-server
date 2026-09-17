# Configuration Examples

OpenMux splits configuration across three files: `server.yaml` (server
identity, listeners, and port adapters), `authentication.yaml` (users, API
keys, public keys, external auth), and `security.yaml` (allow-lists and
Config Editor writable sections). The `server:` section is required in
`server.yaml`, and at least one runtime section (`client_listener`,
`serial_ports`, `loopback_ports`, `command_ports`, or
`tcp_initiator_ports`) must be present.

All examples below are `server.yaml` content. The examples use the list
forms of the port sections (`serial_ports:` as a list of port entries).
This is the form used by the shipped configs.

## Common Use Cases

### 1. Data Center Servers

Physical consoles and remote servers:

```yaml
server:
  id: dc-edge-01
  description: "Data center edge OpenMux"

serial_ports:
  # Physical server consoles
  - name: server01_console
    description: "Web server 01 console"
    device: /dev/ttyUSB0
    baudrate: 115200
    max_read_write_users: multiple

  - name: server02_console
    description: "Database server console"
    device: /dev/ttyUSB1
    baudrate: 115200
    max_read_write_users: multiple

command_ports:
  # Remote servers via SSH
  - name: cloud_server
    description: "Cloud server access"
    command: ssh -i /etc/openmux/keys/cloud_key admin@cloud.example.com
    max_read_write_users: multiple

tcp_initiator_ports:
  # Network equipment (Telnet)
  - name: core_switch
    description: "Core network switch"
    host: 192.168.1.10
    port: 23
    timeout: 5.0
    max_read_write_users: one
```

The matching `authentication.yaml`:

```yaml
users:
  - username: admin
    # SHA-256 hex of "secure_password"
    password_hash: ff2f12ec5c6a2e9ef6b61c958ed701c327469190a18075fd909ec2a9b42b94f2
    permissions: admin
api_keys:
  - key: datacenter-key
    name: Data Center Automation
    permissions: admin
```

User entries require `password_hash` (64 hex characters, SHA-256). OpenMux
does not store plaintext passwords.

### 2. Network Operations Center (NOC)

```yaml
server:
  id: noc-openmux

tcp_initiator_ports:
  # Core network devices
  - name: core_router_1
    description: "Primary core router"
    host: 10.0.0.1
    port: 23
    timeout: 5.0

  - name: core_router_2
    description: "Secondary core router"
    host: 10.0.0.2
    port: 23
    timeout: 5.0

command_ports:
  # Access switches via SSH
  - name: access_switch_floor1
    description: "Floor 1 access switch"
    command: ssh netops@192.168.10.1

  # Monitoring via custom script
  - name: snmp_gateway
    description: "SNMP monitoring gateway"
    command: /opt/monitoring/snmp_console.py
    cwd: /opt/monitoring

serial_ports:
  # Out-of-band access
  - name: oob_console_1
    description: "Out-of-band console server port 1"
    device: /dev/ttyUSB0
    baudrate: 9600
```

### 3. Development Environment

```yaml
server:
  id: dev-openmux

serial_ports:
  # Development boards
  - name: arduino_dev
    description: "Arduino development board"
    device: /dev/ttyACM0
    baudrate: 9600

  - name: raspberry_pi
    description: "Raspberry Pi debug console"
    device: /dev/ttyUSB0
    baudrate: 115200

# Virtual test devices
loopback_ports:
  - name: test_device_1
    description: "Test device simulator"
    echo_delay: 0.1
    max_read_write_users: multiple

# Remote development server
command_ports:
  - name: dev_server
    description: "Development server"
    command: ssh developer@dev.internal.com
    max_read_write_users: multiple
```

Serial ports coalesce small read bursts before forwarding to clients.
Coalescing is on by default; the tunables are `read_coalesce`,
`read_coalesce_max_delay_ms`, and `read_coalesce_max_bytes`
(see [../configuration/adapters.md](../configuration/adapters.md)).

### 4. IoT Devices

```yaml
server:
  id: iot-openmux

tcp_initiator_ports:
  # IoT gateways
  - name: iot_gateway_1
    description: "IoT Gateway Building A"
    host: 192.168.100.10
    port: 8080
    use_tls: true
    ssl_verify: false

serial_ports:
  # Embedded devices via serial
  - name: sensor_node_1
    description: "Environmental sensor node"
    device: /dev/ttyUSB2
    baudrate: 38400

command_ports:
  # LoRaWAN concentrator
  - name: lorawan_gw
    description: "LoRaWAN concentrator"
    command: socat - /dev/ttyAMA0,b115200,raw

  # MQTT bridge via script
  - name: mqtt_bridge
    description: "MQTT to serial bridge"
    command: /opt/iot/mqtt_serial_bridge.py --device sensor_array
    cwd: /opt/iot
```

### 5. Manufacturing Test Setup

```yaml
server:
  id: testbench-openmux

tcp_initiator_ports:
  # Test equipment
  - name: oscilloscope_1
    description: "Keysight oscilloscope"
    host: 192.168.50.10
    port: 5025

serial_ports:
  - name: power_supply_1
    description: "Programmable power supply"
    device: /dev/ttyUSB3
    baudrate: 9600
    parity: E

  # Device under test
  - name: dut_console_1
    description: "Device under test console"
    device: /dev/ttyUSB4
    baudrate: 115200

command_ports:
  # Test automation
  - name: test_controller
    description: "Automated test controller"
    command: /opt/test/run_test_sequence.sh
    cwd: /opt/test
    env:
      TEST_CONFIG: /etc/test/config.yaml
      LOG_LEVEL: DEBUG
```

## Advanced Configuration Patterns

### Failover Configuration

Run two `tcp_initiator_ports` entries to the same device under different
names. OpenMux connects both independently; the operator chooses which to
attach to (automatic per-connection failover is not a built-in feature):

```yaml
tcp_initiator_ports:
  # Primary connection
  - name: primary_device
    description: "Primary device connection"
    host: primary.example.com
    port: 23
    timeout: 5.0

  # Backup connection
  - name: backup_device
    description: "Backup device connection (same device)"
    host: backup.example.com
    port: 23
    timeout: 5.0
```

### Multi-Protocol Access

Expose the same device through several ports:

```yaml
command_ports:
  - name: device_ssh
    description: "Device via SSH"
    command: ssh admin@device.local

tcp_initiator_ports:
  - name: device_telnet
    description: "Device via Telnet (fallback)"
    host: device.local
    port: 23

serial_ports:
  - name: device_serial
    description: "Device via serial (emergency)"
    device: /dev/ttyUSB0
    baudrate: 9600
```

### Secure Tunneled Connections

Reach a device through an SSH tunnel by running a command adapter that
forwards over the tunnel:

```yaml
command_ports:
  - name: secured_device
    description: "Device through SSH tunnel"
    command: ssh -L 2323:internal-device:23 jumphost.example.com "nc localhost 2323"
```

### Cascading OpenMux Servers

Connect to another OpenMux server with a `tcp_initiator_ports` entry that
uses the OpenMux client protocol. `protocol.remote_port` selects the port
on the remote server, and authentication uses `api_key` or
`username` + `password`:

```yaml
tcp_initiator_ports:
  - name: site_a_servers
    description: "Production servers at Site A via OpenMux"
    host: site-a.company.com
    port: 8080
    use_tls: true
    max_read_write_users: multiple
    protocol:
      type: openmux
      remote_port: production_cluster
      api_key: "secret"

  - name: site_b_devices
    description: "Network devices at Site B via OpenMux"
    host: site-b.company.com
    port: 8080
    use_tls: true
    max_read_write_users: one
    protocol:
      type: openmux
      remote_port: network_switches
      username: cascade
      password: "secret"

# Local emergency access
serial_ports:
  - name: local_emergency
    description: "Local emergency console bypass"
    device: /dev/ttyUSB0
    baudrate: 115200
    max_read_write_users: one
```

### Login Prompt (Local System)

Expose a local system login prompt using the Command adapter with a PTY.
Prefer on-demand spawning with an idle timeout and make the session
exclusive.

macOS (login(1)):

```yaml
command_ports:
  - name: local_login
    description: "macOS login prompt (on demand)"
    command: /usr/bin/login
    interactive: true
    spawn_on_demand: true
    idle_timeout_sec: 60
    max_read_write_users: one
```

Linux (agetty → login recommended):

```yaml
command_ports:
  - name: local_login
    description: "Linux getty+login (on demand)"
    command: agetty -L - 9600 xterm
    interactive: true
    spawn_on_demand: true
    idle_timeout_sec: 60
    max_read_write_users: one
```

Linux (direct login(1), distro-dependent):

```yaml
command_ports:
  - name: local_login
    description: "Direct login(1) on PTY"
    command: /bin/login
    interactive: true
    spawn_on_demand: true
    idle_timeout_sec: 60
    max_read_write_users: one
```

Alternative via SSH to localhost (reuses SSH policies/keys):

```yaml
command_ports:
  - name: local_ssh_login
    command: ssh -o StrictHostKeyChecking=no localhost
    interactive: true
    spawn_on_demand: true
    idle_timeout_sec: 60
    max_read_write_users: one
```

## Interactive TUI over Command Adapter

For interactive shells and editors (bash, vim, neovim) over the command
adapter, enable a PTY:

```yaml
command_ports:
  - name: bash_console
    description: "Interactive shell"
    command: bash
    interactive: true
```

Notes:
- `interactive: true` allocates a PTY and enables buffering and newline
  normalization. Output (PTY -> clients) and writes (clients -> PTY) are
  batched automatically at fixed thresholds (issue #67 removed the tuning
  keys). The environment is sanitized and XTGETTCAP queries are intercepted
  automatically, so editor probes do not stall the session.

## Validity Check

The server config is checked against the JSON schema at load time, but
the check is log-only: violations are logged as ERROR and the server
keeps starting, so a currently working deployment never breaks on a
newly caught typo. Run the check yourself before shipping a config:

```
.venv/bin/python -c "
import yaml
from openmux.server.config_manager import ConfigManager
ConfigManager('config/server.yaml').load_config()"
grep 'schema' logs/openmux.log
```

or point `OPENMUX_CONFIG_SCHEMA` at a modified schema to test variants.
