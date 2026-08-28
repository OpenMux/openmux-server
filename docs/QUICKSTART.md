# OpenMux Quick Start Guide

This guide gets one OpenMux server running, then shows how to configure
MuxCon so two OpenMux nodes can share ports.

For full install options (pip, Debian package, without pip), see
[INSTALL.md](INSTALL.md). For every adapter option, see
[configuration/adapters.md](configuration/adapters.md).

## 1. Install and start a server

Use a Python virtual environment for development.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Start the server with the bundled loopback test config. This config needs no
real serial device or network gear.

```sh
python -m openmux.server.main -c config/loopback_test.yaml
```

The server logs each adapter it starts, for example the `client_listener` on
port 8023 and the web console on port 80 (or 443 for HTTPS).

## 2. Connect a client

Use the CLI client to list and open ports.

```sh
python -m openmux.client --list --server localhost --port 8023
python -m openmux.client --server localhost --port 8023
```

Default credentials for `config/loopback_test.yaml` and `config/server.yaml`
come from `config/authentication.yaml`. Check that file for the current
username and password, or add your own user (see
[Generate a user password hash](INSTALL.md#generate-a-user-password-hash)).

You can also open the web console in a browser at `http://localhost` (or the
`port`/`ssl_port` set in your config's `web_console` section).

The Status page (`/`) shows the server version and uptime in its header.
The About page (`/about`, link in the sidebar footer) shows the server
version, runtime details, and the hardware identity from
`/etc/openmux-hardware` on OpenMux console hardware.

## 3. Local control with `openmuxctl`

`openmuxctl` talks to the running server through a Unix domain socket
(`server.control_socket` in your config, for example `logs/openmux.sock`).

```sh
python -m openmux.cli.openmuxctl status
python -m openmux.cli.openmuxctl reload --soft
```

A soft reload re-reads the config and updates authentication, web console UI
settings (MOTD, realm), adapters, and ports without disconnecting clients.
Use a full reload (`reload --full` or `SIGUSR1`) only when a soft reload
cannot pick up the change (for example, the web console port or TLS settings,
or some adapter type changes).

## 4. Configure MuxCon

MuxCon is the federation protocol that links two or more OpenMux nodes so
they can share ports with each other. Each node runs its own `muxcon`
adapter section in its server config. A node can:

- run a **listener**, which accepts inbound MuxCon connections from other
  nodes, and/or
- run an **initiator**, which makes an outbound MuxCon connection to one
  peer node.

A node can do both at the same time, and does not need MuxCon at all if it
never shares ports with another server.

### 4.1 Give the node an identity

Set `server.id` in your config. Other nodes use this ID to identify your
node during federation (`server_include`/`server_exclude` filters, known-peer
records, and the web console viewer badge all show it).

```yaml
server:
  id: "hub-01"
  description: "Primary OpenMux Server"
```

### 4.2 Minimal MuxCon listener (accept inbound connections)

Add a `muxcon` section with one listener. This is enough for another node
to reach this one.

```yaml
muxcon:
  listeners:
    - enabled: true
      host: "0.0.0.0"
      port: 7822
      use_tls: true
      tls_autogen: true    # generate a self-signed cert on first start
```

Start (or reload) the server. It logs a line like:

```
MuxCon listener[0] started on 0.0.0.0:7822 TLS
```

### 4.3 Authenticate peers with Ed25519 keys (recommended)

By default `auth_required: true`, so a node rejects an inbound MuxCon
connection unless it recognizes the peer's Ed25519 public key.

Generate a key pair for the connecting node:

```sh
ssh-keygen -t ed25519 -f id_ed25519_openmux_client -N ""
```

This gives you `id_ed25519_openmux_client` (private key) and
`id_ed25519_openmux_client.pub` (public key, `ssh-ed25519 AAAA...`).

On the **listener** node, add the peer's public key under `public_keys`:

```yaml
muxcon:
  auth_required: true
  listeners:
    - enabled: true
      port: 7822
      use_tls: true
      tls_autogen: true
  public_keys:
    - key_id: "leaf-01-key"
      public_key: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA...leaf-key-here"
```

On the **initiator** node, point at the private key it signs with:

```yaml
muxcon:
  auth:
    private_key: "./id_ed25519_openmux_client"
    key_id: "leaf-01-key"
```

### 4.4 Connect two nodes: initiator config

On the node that dials out, add an `initiators` entry with the listener
node's host and port:

```yaml
muxcon:
  auth:
    private_key: "./id_ed25519_openmux_client"
    key_id: "leaf-01-key"
  initiators:
    - host: "hub-01.example.com"
      port: 7822
      use_tls: true
      ssl_verify: false   # or true with a real/CA-signed cert
      tls_tofu: true       # trust the peer cert on first connect, then pin it
```

`tls_tofu: true` is Trust-On-First-Use: the initiator stores the peer's
certificate fingerprint on first connect (under `muxcon.tls_dir`,
`known_peers.yaml`) and rejects a different certificate later. For
production, prefer real certificates and `ssl_verify: true`, or pin an exact
fingerprint with `tls_pin_fingerprint: "sha256:<hex>"`.

Reload or restart both nodes. The initiator's log shows a successful
handshake; the listener's `openmuxctl status` (or the web console status
page) lists the new connection.

### 4.5 Control which ports each node shares

Filter which local ports a node advertises to peers, and which
peer-advertised ports it accepts, with `advertise_filters`/`accept_filters`.
Patterns are glob-style (`*` wildcards).

```yaml
muxcon:
  advertise_filters:
    include: ["console_*"]     # only advertise these local ports
    exclude: ["debug_*"]
  accept_filters:
    server_include: ["hub-01"] # only accept ports from this node
```

Once a peer's port is accepted, it appears in `PortManager` as a remote
port, and any client (CLI, web console) can open it exactly like a local
port.

### 4.6 Verify

- `openmuxctl status` (or `GET /api/status` on `web_status`) shows active
  MuxCon connections and federated ports.
- The web console's Config Editor (`/config-editor?view=muxcon`) can edit the
  `muxcon` section directly for a running server, if `muxcon` is a writable
  section in `security.yaml`.

## Next steps

- Full option reference: [configuration/adapters.md](configuration/adapters.md#muxcon-federation-muxcon)
- All runtime defaults: [DEFAULTS.md](DEFAULTS.md)
- Full protocol/implementation details (handshake, wire frames, multipath,
  filters, fault injection): [design/muxcon.md](design/muxcon.md)
- Example two-node setup already in the repo:
  [config/loopback_test.yaml](../config/loopback_test.yaml) (listener) and
  [config/remote_leaf_server.yaml](../config/remote_leaf_server.yaml) (initiator)
