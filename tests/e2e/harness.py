"""Real-process harness: synthetic config generation, server lifecycle, and HTTP access.

Every file this module writes goes into a caller-supplied temporary directory. It never reads or
copies ``config/`` or ``config-local/``, because ``config-local/server.yaml`` holds a real PDU host,
username, and SNMP community string that must not reach a pasteable test log.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import yaml

#: Password for every synthetic user. The test data is fake, so the logs stay readable and no
#: secret masking is needed anywhere in this tier.
TEST_PASSWORD = "passwd"

#: Directories that hold real operator config. A test must never boot either one. The guard is
#: anchored on the repository root, so a temporary working directory may be named ``config``.
REPO_ROOT = Path(__file__).resolve().parents[2]
GUARDED_CONFIG_DIRS = (REPO_ROOT / "config", REPO_ROOT / "config-local")

_DEFAULT_USER_PERMISSIONS = ("admin", "read-write", "read-only")

#: The checked-in fake PDU device for ``driver: command``. That driver spawns a command per read and per switch, so
#: a scenario of it needs a device to run. This script is that device: outlet states live in one JSON file, so a
#: scenario can read what the device holds and change it without going through the server.
POWER_BOX_FIXTURE = Path(__file__).resolve().parent / "power_fixtures" / "outlet_box.py"

#: The checked-in fake PDU device for ``driver: command``. That driver spawns a command per read and per switch, so
#: a scenario of it needs a device to run. This script is that device: outlet states live in one JSON file, so a
#: scenario can read what the device holds and change it without going through the server.
POWER_BOX_FIXTURE = Path(__file__).resolve().parent / "power_fixtures" / "outlet_box.py"


def free_ports(count: int) -> List[int]:
    """Return ``count`` ports that were free a moment ago, bound on 127.0.0.1.

    The kernel picks the port by binding port 0, then releases it. A short window stays
    race-prone, so ask once for a whole suite's ports and never re-use one across suites.
    """
    ports: List[int] = []
    sockets: List[socket.socket] = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
            ports.append(int(sock.getsockname()[1]))
    finally:
        for sock in sockets:
            sock.close()
    if len(set(ports)) != count:
        raise RuntimeError(f"free_ports returned duplicates: {ports}")
    return ports


def password_hash(password: str) -> str:
    """The ``password_hash`` form the server expects: SHA-256 hex, lower case."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def ssh_ed25519_keypair(comment: str = "openmux-e2e") -> Tuple[str, Any]:
    """Generate one Ed25519 SSH key pair: the OpenSSH public line, and the private key object.

    The SSH listener stores public keys as OpenSSH text in ``authentication.yaml`` and compares the first
    two fields of a presented key, so both halves come from the library the client uses and a scenario
    registers the line it was handed. ``asyncssh`` is a product dependency, but the import stays inside
    this function so importing the harness costs nothing.
    """
    import asyncssh

    key = asyncssh.generate_private_key("ssh-ed25519")
    public = key.export_public_key("openssh").decode("ascii").strip()
    if comment and public.count(" ") == 1:
        public = f"{public} {comment}"
    return public, key


def sso_v1_header(
    secret: str,
    user: str = "sso-user",
    perm: Optional[str] = None,
    node: Optional[str] = None,
    issued_at: Optional[int] = None,
    expires_at: Optional[int] = None,
    tamper: bool = False,
) -> str:
    """Build one ``v1`` trust-header value, exactly as the server verifies it.

    The format is ``v1;<base64url of the JSON claims>;<hex HMAC-SHA256 of the base64 text>``, and the
    server reads ``user``, ``perm``, ``node``, ``iat``, and ``exp`` from those claims. A scenario that
    wants the refusal case passes ``tamper=True``, which keeps a well-formed header and breaks the
    signature.

    Args:
        secret: The ``web_console.sso_secret`` the server is configured with.
        user: Claimed identity.
        perm: Claimed permission override, for example ``admin``.
        node: Claimed server id. Leave unset, because a node the server does not know is refused.
        issued_at: Claim issue time. Defaults to now.
        expires_at: Claim expiry time. Defaults to five minutes from now.
        tamper: Flip one signature character while keeping the header well formed.

    Returns:
        str: The header value.
    """
    claims: Dict[str, Any] = {"iat": int(time.time()) if issued_at is None else issued_at}
    claims["exp"] = int(time.time()) + 300 if expires_at is None else expires_at
    claims["user"] = user
    if perm:
        claims["perm"] = perm
    if node:
        claims["node"] = node
    raw = json.dumps(claims, separators=(",", ":")).encode("utf-8")
    payload = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    mac = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if tamper:
        last = "0" if mac[-1] != "0" else "1"
        mac = mac[:-1] + last
    return f"v1;{payload};{mac}"


class ConfigBuilder:
    """Builds one synthetic server config directory.

    The fluent methods mirror what a scenario needs, and ``write()`` emits the three files the
    server reads: ``server.yaml``, ``authentication.yaml``, and ``security.yaml``.
    """

    def __init__(self, config_dir: Path, server_id: str = "e2e-server"):
        """Create a builder for ``config_dir``, which must not hold real config.

        Raises:
            ValueError: when the directory is a guarded real config dir, or already holds a
                ``server.yaml`` written by someone else.
        """
        self.path = Path(config_dir)
        self._refuse_real_config_dir(self.path)
        self._refuse_foreign_config(self.path)
        self.server_id = server_id
        self.log_dir: Optional[Path] = None
        self._users: List[Dict[str, Any]] = []
        self._public_keys: List[Dict[str, Any]] = []
        self._external_auth: Dict[str, Any] = {"enabled": False, "allow_root": False}
        self._loopback: List[Dict[str, Any]] = []
        self._command: List[Dict[str, Any]] = []
        self._tcp: List[Dict[str, Any]] = []
        self._serial: List[Dict[str, Any]] = []
        self._telnet: List[Dict[str, Any]] = []
        self._ssh: List[Dict[str, Any]] = []
        self._pdus: List[Dict[str, Any]] = []
        # Where each ``driver: command`` PDU's fake device keeps its state, keyed by PDU name. The builder owns the
        # path, because it writes the commands that name it; ``E2EServer.power_boxes`` carries it to the scenario.
        self._power_boxes: Dict[str, Path] = {}
        self._web: Optional[Dict[str, Any]] = None
        self._client_listener: Optional[Dict[str, Any]] = None
        self._power_enabled = False
        self._actions: Dict[str, Any] = {}
        self._writable_sections: Optional[List[str]] = None
        self._access_default = "allow"
        self._rate_limits: Dict[str, Any] = {
            "window_seconds": 300,
            "failure_threshold": 5,
            "base_lock_seconds": 30,
        }

    # --- guards --------------------------------------------------------------------------

    @staticmethod
    def _refuse_real_config_dir(path: Path) -> None:
        """Refuse the repository's real config directories, or anything inside them."""
        resolved = path.resolve()
        for guarded in GUARDED_CONFIG_DIRS:
            if resolved == guarded or guarded in resolved.parents:
                raise ValueError(f"ConfigBuilder refuses to write inside a real config dir: {path} (inside {guarded})")

    @staticmethod
    def _refuse_foreign_config(path: Path) -> None:
        """Refuse a directory that already holds a ``server.yaml`` this builder did not write."""
        existing = path / "server.yaml"
        if existing.exists():
            raise ValueError(f"ConfigBuilder refuses an existing server.yaml: {existing}")

    # --- users ---------------------------------------------------------------------------

    def with_user(
        self, username: str, permissions: str, password: str = TEST_PASSWORD, groups: Optional[Sequence[str]] = None
    ) -> "ConfigBuilder":
        """Add one static user. ``permissions`` is ``admin``, ``read-write``, or ``read-only``.

        ``groups`` are the console groups the port access lists match, beside the built-in ``user``
        group every known account gets. A scenario needs them only to prove ``read_write_groups``.

        Fails on an unknown permission: a typo here silently changes what every permission test
        proves.
        """
        if permissions not in _DEFAULT_USER_PERMISSIONS:
            raise ValueError(f"Unknown permission {permissions!r}; use one of {_DEFAULT_USER_PERMISSIONS}")
        entry: Dict[str, Any] = {
            "username": username,
            "password_hash": password_hash(password),
            "permissions": permissions,
        }
        if groups:
            entry["groups"] = list(groups)
        self._users.append(entry)
        return self

    def with_default_users(self) -> "ConfigBuilder":
        """Add the three users almost every scenario needs: ``admin``, ``rw``, and ``ro``."""
        for name, permission in (("admin", "admin"), ("rw", "read-write"), ("ro", "read-only")):
            self.with_user(name, permission)
        return self

    def with_public_key(
        self, key_id: str, public_key: str, username: str, allowed_uses: Sequence[str] = ("ssh",)
    ) -> "ConfigBuilder":
        """Register one Ed25519 public key, in the OpenSSH text form the server parses.

        ``allowed_uses`` must contain ``ssh`` or the SSH listener never offers this key: a key registered
        for ``client`` or ``muxcon`` only is refused for a session, which is the state a scenario proves.
        The record names its user, because SSH public-key auth claims the username before the key.
        """
        self._public_keys.append(
            {
                "key_id": key_id,
                "public_key": public_key,
                "username": username,
                "allowed_uses": list(allowed_uses),
            }
        )
        return self

    # --- ports ---------------------------------------------------------------------------

    def with_loopback(
        self,
        name: str,
        write_slot_capacity: str = "one",
        feeds: Optional[Sequence[str]] = None,
        read_write_groups: Optional[Sequence[str]] = None,
        read_only_groups: Optional[Sequence[str]] = None,
    ) -> "ConfigBuilder":
        """Add one Loopback port, which echoes what it receives.

        Args:
            name: Port name.
            write_slot_capacity: ``one``, ``multiple``, or ``none`` (Write slot capacity).
            feeds: Outlet refs for the ``power:`` key, for example ``["rack1.1", "rack1.2"]``.
            read_write_groups: Console groups that may drive this port. Any group list turns the
                port into a closed boundary: an identity in neither list is refused.
            read_only_groups: Console groups that may watch this port and never drive it.
        """
        port: Dict[str, Any] = {
            "name": name,
            "description": f"e2e loopback {name}",
            "echo_delay": 0.0,
            "buffer_size": 4096,
            "max_read_write_users": write_slot_capacity,
        }
        if feeds:
            port["power"] = list(feeds)
        if read_write_groups:
            port["read_write_groups"] = list(read_write_groups)
        if read_only_groups:
            port["read_only_groups"] = list(read_only_groups)
        self._loopback.append(port)
        return self

    def with_serial_port(
        self,
        name: str,
        device: str,
        baudrate: int = 9600,
        parity: str = "N",
        stopbits: float = 1,
        flow_control: str = "none",
        write_slot_capacity: str = "one",
        feeds: Optional[Sequence[str]] = None,
    ) -> "ConfigBuilder":
        """Add one Serial port backed by ``device``, which may be a path no host has.

        Args:
            name: Port name.
            device: Device path. A path that does not exist starts the port offline instead of failing the reload.
            baudrate: Line speed.
            parity: One of the letters the schema accepts: ``N``, ``E``, ``O``, ``M``, or ``S``.
            stopbits: Stop bits: ``1``, ``1.5``, or ``2``.
            flow_control: ``none`` (default), ``rtscts``, ``dsrdtr``, or ``xonxoff``.
            write_slot_capacity: ``one``, ``multiple``, or ``none`` (Write slot capacity).
            feeds: Outlet refs for the ``power:`` key.
        """
        port: Dict[str, Any] = {
            "name": name,
            "description": f"e2e serial {name}",
            "device": device,
            "baudrate": baudrate,
            "bytesize": 8,
            "parity": parity,
            "stopbits": stopbits,
            "flow_control": flow_control,
            "max_read_write_users": write_slot_capacity,
        }
        if feeds:
            port["power"] = list(feeds)
        self._serial.append(port)
        return self

    def with_command_port(
        self,
        name: str,
        command: str = "/bin/cat",
        write_slot_capacity: str = "one",
        scrollback_size: Optional[int] = None,
    ) -> "ConfigBuilder":
        """Add one command port that runs ``command`` as a real child process.

        ``scrollback_size`` keeps the last that many bytes of output for a late viewer to replay. The
        key stays out of server.yaml when it is None, because the server default is no history.
        """
        entry: Dict[str, Any] = {
            "name": name,
            "description": f"e2e command port {name}",
            "command": command,
            "shell": False,
            "normalize_newlines": True,
            "max_read_write_users": write_slot_capacity,
        }
        if scrollback_size is not None:
            entry["scrollback_size"] = int(scrollback_size)
        self._command.append(entry)
        return self

    def with_tcp_initiator_port(
        self,
        name: str,
        host: str,
        port: int,
        description: Optional[str] = None,
        auto_reconnect: bool = True,
        reconnect_delay: float = 0.5,
    ) -> "ConfigBuilder":
        """Add one TCP-initiator port: OpenMux dials out to ``host``:``port`` as the console source.

        The dial-out direction is what makes a port's link controllable from a test: a scenario owns the
        listening side, so it can drop the link and bring it back without touching the server.
        ``reconnect_delay`` stays short so a recovery is visible inside a scenario, not after 5 s.
        """
        self._tcp.append(
            {
                "name": name,
                "description": description or f"e2e TCP initiator {name}",
                "host": host,
                "port": int(port),
                "auto_reconnect": bool(auto_reconnect),
                "reconnect_delay": float(reconnect_delay),
            }
        )
        return self

    def with_telnet_listener(
        self, port: int, name: str = "telnet1", target: str = "*", require_auth: bool = False, read_only: bool = False
    ) -> "ConfigBuilder":
        """Add one Telnet listener. ``target`` is ``*`` for the port menu, or ``local::<port>``.

        The adapter defaults ``require_auth`` to true, so this method states the value every time.
        """
        self._telnet.append(
            {
                "name": name,
                "bind_host": "127.0.0.1",
                "bind_port": int(port),
                "target": target,
                "require_auth": require_auth,
                "read_only": read_only,
            }
        )
        return self

    def with_ssh_listener(
        self,
        port: int,
        name: str = "ssh1",
        target: str = "*",
        require_auth: bool = True,
        read_only: bool = False,
        acl: Optional[Sequence[str]] = None,
    ) -> "ConfigBuilder":
        """Add one SSH listener, and state ``require_auth`` the way the adapter reads it.

        ``target`` is ``*`` for the port menu or ``<port>`` for one Port. Unlike Telnet, this adapter
        defaults ``require_auth`` to true, so a scenario that wants an anonymous listener says so here.
        ``acl`` holds host or subnet strings the listener accepts before the SSH handshake finishes.
        """
        entry: Dict[str, Any] = {
            "name": name,
            "bind_host": "127.0.0.1",
            "bind_port": int(port),
            "target": target,
            "require_auth": require_auth,
            "read_only": read_only,
        }
        if acl is not None:
            entry["acl"] = list(acl)
        self._ssh.append(entry)
        return self

    # --- listeners and adapters ----------------------------------------------------------

    def with_client_listener(self, port: int) -> "ConfigBuilder":
        """Add the client listener, which the CLI protocol and this harness both speak."""
        self._client_listener = {
            "host": "127.0.0.1",
            "port": int(port),
            "max_connections": 50,
            "connection_timeout": 30,
        }
        return self

    def with_web(
        self, port: int, enable_ui: bool = True, enable_probes: bool = True, config_editor: bool = True
    ) -> "ConfigBuilder":
        """Add the web console over plain HTTP: no TLS, so the browser needs no certificate trust.

        ``enable_probes`` also controls ``/healthz``, which ``E2EServer.wait_ready`` polls.
        """
        web: Dict[str, Any] = {
            "host": "127.0.0.1",
            "port": int(port),
            "enable_ui": enable_ui,
            "enable_probes": enable_probes,
            "use_tls": False,
            "tls_autogen": False,
            "respect_forwarded_prefix": False,
        }
        if config_editor:
            web["plugins"] = ["openmux.server.web_plugins.config_editor"]
        self._web = web
        return self

    def with_dummy_pdu(
        self, name: str, outlets: Sequence[str], poll_interval: int = 0, enabled: bool = True
    ) -> "ConfigBuilder":
        """Add one PDU with the in-memory ``dummy`` driver, which needs no device.

        ``poll_interval`` 0 disables the poll task, so a test sees only state it caused.
        """
        self._power_enabled = self._power_enabled or enabled
        self._pdus.append(
            {
                "name": name,
                "description": f"e2e PDU {name}",
                "driver": "dummy",
                "poll_interval": int(poll_interval),
                "outlets": [{"id": str(outlet)} for outlet in outlets],
            }
        )
        return self

    def with_command_pdu(
        self,
        name: str,
        outlets: Sequence[str],
        *,
        poll_interval: float = 1,
        enabled: bool = True,
        box_args: Sequence[str] = (),
        timeout: Optional[float] = None,
        state_path: Optional[Path] = None,
    ) -> "ConfigBuilder":
        """Add one PDU whose driver spawns the checked-in outlet box (``driver: command``).

        This driver runs a command per read and a command per switch, so a scenario needs a device to run. The
        fixture behind it, `tests/e2e/power_fixtures/outlet_box.py`, keeps its outlet states in one JSON file, so a
        scenario can read what the device holds and change it without going through the server. Three commands are
        written for it: one batch ``state_cmd`` that prints every outlet, and the two switch templates behind the
        ``{outlet_id}`` placeholder.

        Args:
            name: the PDU name; an outlet is addressed ``name.outlet``.
            outlets: the outlet ids this PDU declares.
            poll_interval: seconds between device reads; 0 reads on demand only. Unlike the ``dummy`` driver, this
                device changes between two reads, so this value decides whether a device-side change is ever seen.
            enabled: whether the ``power`` section is on.
            box_args: extra words on every command, which is how a scenario builds in a device fault:
                ``("--fail-state",)`` for a dead sense line, ``("--fail-switch",)`` for a relay that refuses to move,
                ``("--slow", "6", "--child")`` for a command to time out.
            timeout: the driver's per-command seconds, for a scenario that drives a command past its timeout.
            state_path: where the device keeps its state. Default ``{workdir}/power_boxes/<name>.json``. The journal
                and the recorded child pid are that path plus ``.journal`` and ``.child``.
        """
        self._power_enabled = self._power_enabled or enabled
        box = Path(state_path) if state_path else self.path.parent / "power_boxes" / f"{name}.json"
        self._power_boxes[name] = box
        ids = [str(outlet) for outlet in outlets]
        argv = [sys.executable, str(POWER_BOX_FIXTURE), "--state", str(box), "--ids", ",".join(ids)]
        argv += [str(item) for item in box_args]
        words = [shlex.quote(item) for item in argv]
        options: Dict[str, Any] = {
            "on_cmd": " ".join(words + ["on", "'{outlet_id}'"]),
            "off_cmd": " ".join(words + ["off", "'{outlet_id}'"]),
            "state_cmd": " ".join(words + ["state"]),
        }
        if timeout is not None:
            options["timeout"] = float(timeout)
        self._pdus.append(
            {
                "name": name,
                "description": f"e2e command PDU {name}",
                "driver": "command",
                "poll_interval": poll_interval,
                "outlets": [{"id": outlet} for outlet in ids],
                "options": options,
            }
        )
        return self

    @property
    def power_boxes(self) -> Dict[str, Path]:
        """The state file of each fake device this config wires, by PDU name."""
        return dict(self._power_boxes)

    def with_actions(self, grants: Dict[str, Any], actions_dir: Optional[str] = None) -> "ConfigBuilder":
        """Grant Port Action ids to ports. ``actions_dir`` defaults to the bundled examples."""
        self._actions = {
            "actions_dir": actions_dir or "openmux/server/actions/examples",
            "action_ports": dict(grants),
        }
        return self

    # --- server-wide policy --------------------------------------------------------------

    def with_writable_sections(self, sections: Optional[Sequence[str]]) -> "ConfigBuilder":
        """Limit the sections the Config Editor may write. ``None`` leaves all sections writable.

        Used by the Config Editor tests for a non-writable section. An empty list makes the editor
        fully read-only.
        """
        self._writable_sections = None if sections is None else list(sections)
        return self

    def with_access_default(self, value: str) -> "ConfigBuilder":
        """Set ``security.yaml`` ``access_default``: ``allow`` (default) or ``deny``."""
        if value not in ("allow", "deny"):
            raise ValueError(f"access_default must be allow or deny, got {value!r}")
        self._access_default = value
        return self

    def with_login_lockout(self, window_seconds: int, failure_threshold: int, base_lock_seconds: int) -> "ConfigBuilder":
        """Set ``security.yaml`` ``rate_limits.authentication``, which gates the login form.

        The shipped default (5 failures in 300 s, then a 30 s lock) is right for an operator and far
        too slow for a scenario that proves the lock and its release. A scenario that lowers
        ``base_lock_seconds`` must keep the lock longer than the network round trip it asserts on.
        """
        self._rate_limits = {
            "window_seconds": int(window_seconds),
            "failure_threshold": int(failure_threshold),
            "base_lock_seconds": int(base_lock_seconds),
        }
        return self

    def with_sso(self, secret: Optional[str], header: str = "X-OMX-SSO") -> "ConfigBuilder":
        """Set the ``web_console`` trust-header keys, and rename the header if asked.

        With ``secret`` unset the server accepts no ``v1`` header, because an unsigned header used to
        allow an unauthenticated admin takeover. ``sso_max_skew_sec`` stays at its default, so a claim
        signed with a fresh ``iat`` is inside the window.
        """
        if self._web is None:
            raise ValueError("with_sso needs a web console: call with_web() first")
        self._web["sso_trust_header"] = header
        if secret is not None:
            self._web["sso_secret"] = secret
        return self

    def with_external_auth(
        self,
        helper: Sequence[str],
        *,
        groups: Optional[Dict[str, str]] = None,
        default_permission: Optional[str] = None,
        allowed_users: Optional[Sequence[str]] = None,
        timeout: float = 5,
        service: str = "openmux",
    ) -> "ConfigBuilder":
        """Turn on ``external_auth`` in ``authentication.yaml`` and point it at ``helper``.

        The helper speaks the server's own protocol: it reads the user name and the password on stdin,
        answers exit ``0`` plus one JSON object on stdout, and that object's ``groups`` list decides the
        role through the ``groups`` mapping here. A scenario passes a command list, so a fixture script
        runs under the interpreter the server itself runs on and needs no executable bit.

        ``default_permission`` is the fallback for a person the helper authenticates whose groups map to
        nothing. Left unset -- what a deployment ships -- that person is signed in with no permission at
        all, which is the state ``AUTH-11`` proves every surface refuses on its own words.

        ``allowed_users`` is the server's own allow-list, checked before the helper runs.
        """
        if not helper:
            raise ValueError("with_external_auth needs a helper command")
        config: Dict[str, Any] = {"enabled": True, "helper": list(helper), "service": service, "timeout": timeout}
        if groups:
            config["groups"] = dict(groups)
        if default_permission is not None:
            config["default_permission"] = default_permission
        if allowed_users is not None:
            config["allowed_users"] = list(allowed_users)
        self._external_auth = config
        return self

    def with_log_dir(self, log_dir: Path) -> "ConfigBuilder":
        """Put the server's own log file under ``log_dir`` instead of the working directory."""
        self.log_dir = Path(log_dir)
        return self

    # --- output --------------------------------------------------------------------------

    def server_config(self) -> Dict[str, Any]:
        """The ``server.yaml`` mapping, without the sections a scenario left out."""
        config: Dict[str, Any] = {
            "server": {"id": self.server_id, "description": "OpenMux e2e test server", "fail_fast_adapters": False},
            "logging": {
                "level": os.environ.get("E2E_SERVER_LOG_LEVEL", "INFO"),
                "file": str((self.log_dir or self.path) / "server.log"),
                "console": True,
            },
        }
        if self._loopback:
            config["loopback_ports"] = self._loopback
        if self._serial:
            config["serial_ports"] = self._serial
        if self._command:
            config["command_ports"] = self._command
        if self._tcp:
            config["tcp_initiator_ports"] = self._tcp
        if self._client_listener:
            config["client_listener"] = self._client_listener
        if self._telnet:
            config["telnet_listener"] = self._telnet
        if self._ssh:
            config["ssh_listener"] = self._ssh
        if self._web:
            config["web_console"] = self._web
        if self._pdus:
            config["power"] = {"enabled": self._power_enabled, "pdus": self._pdus}
        if self._actions:
            config["port_actions"] = self._actions
        return config

    def authentication_config(self) -> Dict[str, Any]:
        """The ``authentication.yaml`` mapping: synthetic users, registered keys, and the external-auth block.

        ``external_auth`` is always written, disabled unless ``with_external_auth`` was called, because a
        missing block and a disabled block are the same state to the server and a scenario should not have
        to know which one it is looking at. ``public_keys`` is written only when a scenario registered one.
        """
        config: Dict[str, Any] = {
            "users": self._users
            or [{"username": "admin", "password_hash": password_hash(TEST_PASSWORD), "permissions": "admin"}],
            "external_auth": dict(self._external_auth),
        }
        if self._public_keys:
            config["public_keys"] = list(self._public_keys)
        return config

    def security_config(self) -> Dict[str, Any]:
        """The ``security.yaml`` mapping: every adapter allowed, writable sections as configured."""
        editor: Dict[str, Any] = (
            {"allowed": ["*"], "disabled": []}
            if self._writable_sections is None
            else {"allowed": list(self._writable_sections), "disabled": []}
        )
        return {
            "adapters": {"allowed": ["*"], "disabled": []},
            "config_editor": editor,
            "rate_limits": {"authentication": dict(self._rate_limits)},
            "access_default": self._access_default,
        }

    def write(self) -> Path:
        """Write the three files and return the config directory."""
        self.path.mkdir(parents=True, exist_ok=True)
        _write_yaml(self.path / "server.yaml", self.server_config())
        _write_yaml(self.path / "authentication.yaml", self.authentication_config())
        _write_yaml(self.path / "security.yaml", self.security_config())
        return self.path

    def write_raw_server_yaml(self, text: str) -> Path:
        """Write ``server.yaml`` verbatim, for a scenario that needs an invalid or unknown key.

        This bypasses the generator on purpose, so a test can prove the server rejects a bad file
        rather than fixing it up.
        """
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "server.yaml").write_text(text, encoding="utf-8")
        if not (self.path / "authentication.yaml").exists():
            _write_yaml(self.path / "authentication.yaml", self.authentication_config())
        if not (self.path / "security.yaml").exists():
            _write_yaml(self.path / "security.yaml", self.security_config())
        return self.path


def _write_yaml(path: Path, payload: Dict[str, Any]) -> None:
    """Write one mapping as block-style YAML, keeping key order."""
    path.write_text(yaml.safe_dump(payload, sort_keys=False, default_flow_style=False), encoding="utf-8")


@dataclass
class ServerAddress:
    """One address the harness opened, for the artifact bundle and the ``--e2e-live`` header."""

    name: str
    port: int
    kind: str = "tcp"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}" if self.kind == "http" else f"127.0.0.1:{self.port}"


class E2EServer:
    """One ``openmux.server.main`` subprocess, with its own config, ports, state, and logs.

    The child is started with ``--config-dir`` and with ``OPENMUX_STATE_DIR``, ``OPENMUX_RUN_DIR``,
    and ``OPENMUX_LOG_DIR`` pinned, so a test can never write ``~/.openmux`` or the repository's
    ``logs/``. ``stop`` asserts a clean exit code, which is what surfaces a shutdown crash in the
    test that caused it.
    """

    def __init__(self, workdir: Path, config_dir: Path, addresses: Sequence[ServerAddress], name: str = "e2e"):
        self.name = name
        self.workdir = Path(workdir)
        self.config_dir = Path(config_dir)
        self.addresses = list(addresses)
        self.state_dir = self.workdir / "state"
        self.log_dir = self.workdir / "logs"
        # The run dir holds the pid file and the control socket, and a Unix socket path is capped
        # at 104 bytes on macOS while pytest's tmp_path is already about 90 bytes deep. A long path
        # makes ``asyncio.start_unix_server`` fail, so the run dir gets its own short base under
        # the system temp dir. ``cleanup_run_dir`` removes it.
        self.run_dir = Path(tempfile.mkdtemp(prefix=f"omxe2e-{os.getpid()}-"))
        self.proc: Optional[subprocess.Popen] = None
        self.exit_code: Optional[int] = None
        self._lines: List[str] = []
        self._lines_lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._log_file: Optional[Path] = None
        # Set by the story fixture. The timeline is also the live channel when ``--e2e-live`` is on,
        # so the reader below never prints: a second print here would duplicate every line.
        self.recorder: Optional[Any] = None
        self.expected_exit: Optional[int] = None
        """Exit code a scenario expects, for a server meant to fail. ``stop_clean`` accepts it."""
        #: Where each ``driver: command`` PDU's fake device keeps its state, by PDU name. ``ServerRegistry.make``
        #: copies it off the builder, which is the object that chose the path. Empty for every other world.
        self.power_boxes: Dict[str, Path] = {}

    # --- addresses -----------------------------------------------------------------------

    def port_of(self, name: str) -> int:
        """The bound port of one configured address, by name (``web``, ``cli``, ``telnet``)."""
        for address in self.addresses:
            if address.name == name:
                return address.port
        raise KeyError(f"{self.name} has no address named {name!r}: {[a.name for a in self.addresses]}")

    @property
    def web_url(self) -> str:
        """The base URL of the web console, with no trailing slash."""
        return f"http://127.0.0.1:{self.port_of('web')}"

    def descriptor_kinds(self) -> Dict[str, int]:
        """Count the descriptors this server process holds, grouped by kind.

        Uses ``lsof``, so no new dependency. A churn scenario compares the dict before and after a
        burst, where only the return to baseline matters and the absolute numbers do not. A socket the
        process still refers to keeps its entry after the peer left, and macOS adds one ``KQUEUE`` per
        live transport, which is what a dropped session reference fails to release.
        """
        if self.proc is None:
            raise RuntimeError("no process to inspect")
        if LSOF is None:
            raise RuntimeError(LSOF_MISSING)
        out = subprocess.run(
            [LSOF, "-n", "-P", "-p", str(self.proc.pid)], capture_output=True, text=True, timeout=60, check=False
        )
        kinds: Dict[str, int] = {}
        for line in out.stdout.splitlines()[1:]:
            fields = line.split()
            if len(fields) > 4:
                kinds[fields[4]] = kinds.get(fields[4], 0) + 1
        kinds["total"] = sum(kinds.values())
        return kinds

    def tcp_queue(self, address: str, peer_port: int) -> Dict[str, int]:
        """The bytes queued on the server's side of one live connection, in bytes.

        ``send_q`` above zero says the server handed bytes to the kernel and the peer has not taken them,
        so a scenario can tell a viewer that stopped reading from a viewer whose bytes the server still
        holds. Only the first can delay a write the two of them share. Reads ``netstat``, which is the
        ``-p proto`` spelling macOS and the BSDs use; Linux gives ``-p`` to the owning program, so this
        raises rather than returning another host's columns.

        Raises:
            RuntimeError: when ``netstat`` is missing, or the host is not one this column layout fits.
            LookupError: when no connection matches the two ends.
        """
        if NETSTAT is None:
            raise RuntimeError(NETSTAT_MISSING)
        if not sys.platform.startswith("darwin"):
            raise RuntimeError(f"tcp_queue reads macOS netstat columns, not {sys.platform}")
        local_port = self.port_of(address)
        out = subprocess.run([NETSTAT, "-an", "-p", "tcp"], capture_output=True, text=True, timeout=60, check=False)
        for line in out.stdout.splitlines():
            fields = line.split()
            if len(fields) < 5 or not fields[0].startswith("tcp"):
                continue
            if fields[3].rsplit(".", 1)[-1] == str(local_port) and fields[4].rsplit(".", 1)[-1] == str(peer_port):
                return {"recv_q": int(fields[1]), "send_q": int(fields[2])}
        raise LookupError(f"no TCP connection on {address} port {local_port} to peer port {peer_port}")

    def power_box(self, pdu_name: str) -> Path:
        """The state file of one fake ``command`` device, which is the device's own state.

        A scenario reads this to ask what the device holds rather than trusting what the server last reported, and
        writes it through the fixture to move an outlet with no switch command.
        """
        if pdu_name not in self.power_boxes:
            raise KeyError(f"{self.name} has no command PDU {pdu_name!r}: {sorted(self.power_boxes)}")
        return self.power_boxes[pdu_name]

    def power_box_journal(self, pdu_name: str) -> Path:
        """The journal of one fake device: one line per switch it was told to run."""
        return Path(f"{self.power_box(pdu_name)}.journal")

    def power_box_child(self, pdu_name: str) -> Path:
        """The file one fake device records a spawned helper's pid in, for a group-kill proof."""
        return Path(f"{self.power_box(pdu_name)}.child")

    def power_box_flip(self, pdu_name: str, outlet_id: str) -> str:
        """Change one outlet on the fake device itself, with no switch command involved.

        This is how a scenario produces what a poll must catch: a switch someone threw by hand, or a device
        that changed on its own. The fixture reads, flips and rewrites its own state file, so no scenario has
        to know that file's shape. Returns the state the fixture printed.
        """
        result = subprocess.run(
            [sys.executable, str(POWER_BOX_FIXTURE), "--state", str(self.power_box(pdu_name)), "flip", outlet_id],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"{self.name}: flipping {pdu_name}.{outlet_id} on the device failed "
                f"(exit {result.returncode}): {result.stderr.strip()[:300]}"
            )
        return result.stdout.strip()

    def power_box_fault(self, pdu_name: str, *modes: str) -> None:
        """Make the fake device broken for one kind of call, while the server keeps running.

        Pass ``"state"`` to fail every read, ``"switch"`` to refuse every switch, both to model a dead device,
        or no mode to heal it. The fault lives in a file the device reads on each call, so it needs no config
        change and no restart: a scenario sets it after the server reached the device, which is what a device
        that fails mid-shift does. See ``power_fixtures/outlet_box.py`` for the exit code and stderr each
        fault produces.
        """
        path = Path(f"{self.power_box(pdu_name)}.faults")
        if modes:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(modes) + "\n", encoding="utf-8")
        elif path.exists():
            path.unlink()

    # --- lifecycle -----------------------------------------------------------------------

    def env(self) -> Dict[str, str]:
        """The environment for the child: the parent's, plus the pinned OpenMux directories."""
        env = dict(os.environ)
        env["OPENMUX_STATE_DIR"] = str(self.state_dir)
        env["OPENMUX_RUN_DIR"] = str(self.run_dir)
        env["OPENMUX_LOG_DIR"] = str(self.log_dir)
        env.pop("OPENMUX_PIDFILE", None)
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def start(self, timeout: float = 45.0) -> "E2EServer":
        """Start the process and wait until every listener answers.

        Raises:
            RuntimeError: when the process exits before it is ready, or is not ready in time. The
                message carries the captured output, because that is where the cause is.
        """
        for directory in (self.workdir, self.state_dir, self.run_dir, self.log_dir, self.log_dir / "ports"):
            directory.mkdir(parents=True, exist_ok=True)
        self._log_file = self.log_dir / "process.log"
        command = [sys.executable, "-m", "openmux.server.main", "--config-dir", str(self.config_dir)]
        self.proc = subprocess.Popen(
            command,
            cwd=str(self.workdir),
            env=self.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._start_reader()
        try:
            self.wait_ready(timeout=timeout)
        except Exception as exc:
            output = self.output()
            self.stop(kill=True)
            # Both ends of the output, because the reason is usually early ("address already in use", a config
            # refusal) while a web console that is already ready fills the tail with request lines. `wait_ready`
            # names the ports that stayed shut, which is the first fact a reader needs.
            raise RuntimeError(
                f"{self.name} failed to start: {exc}\nfirst output:\n{output[:2000]}\nlast output:\n{output[-4000:]}"
            ) from None
        return self

    def _start_reader(self) -> None:
        """Drain the child's combined output into memory, the artifact file, and the timeline.

        The reader writes to no console. ``Timeline.record`` echoes each line once, with its offset and
        its ``server`` label, when the timeline runs in echo mode (``--e2e-live``).
        """
        handle = self.proc.stdout if self.proc else None
        if handle is None:  # pragma: no cover - only absent when Popen was built without a pipe
            return

        def pump() -> None:
            while True:
                try:
                    raw = handle.readline()
                except (ValueError, OSError):  # justification: the pipe closes when the process stops, which ends this reader
                    return
                if not raw:
                    return
                line = raw.rstrip("\n")
                with self._lines_lock:
                    self._lines.append(line)
                if self._log_file is not None:
                    try:
                        with self._log_file.open("a", encoding="utf-8") as sink:
                            sink.write(raw)
                    except OSError:
                        pass  # justification: the artifact copy is a convenience, the memory copy is the record
                if self.recorder is not None:
                    self.recorder.record("server", "out", line)
            close = getattr(handle, "close", None)
            if callable(close):
                close()

        self._reader = threading.Thread(target=pump, name=f"{self.name}-output", daemon=True)
        self._reader.start()

    def wait_ready(self, timeout: float = 45.0) -> None:
        """Wait until the web console answers any HTTP status and every TCP port accepts a socket."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(f"process exited early with code {self.proc.returncode}")
            tcp_ports = [address.port for address in self.addresses if address.kind != "http"]
            if self._web_answers() and all(self._port_open(port) for port in tcp_ports):
                return
            time.sleep(0.1)
        raise RuntimeError(f"not ready after {timeout}s: open={self._open_ports()} web={self._web_answers()}")

    def _open_ports(self) -> List[int]:
        """The TCP addresses that accept a connection right now."""
        return [address.port for address in self.addresses if address.kind != "http" and self._port_open(address.port)]

    @staticmethod
    def _port_open(port: int, timeout: float = 0.4) -> bool:
        """True when one TCP listener accepts a connection, with the probe closed politely.

        The probe reads what the listener offers and half-closes before it closes. Closing at once
        makes the adapter log a failed send with a traceback (``Failed to send line to client ...
        Connection lost``), which buries the output a reader needs in every failure bundle.
        """
        try:
            connection = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        except OSError:
            return False
        try:
            try:
                connection.recv(4096)
            except OSError:  # justification: the banner is not what this probe waits for
                pass
            try:
                connection.shutdown(socket.SHUT_WR)
            except OSError:  # justification: the listener may already have closed its side
                pass
        finally:
            connection.close()
        return True

    def _web_answers(self) -> bool:
        """True when the web console answers anything, including a 401 challenge."""
        if not any(address.kind == "http" for address in self.addresses):
            return True
        try:
            with urllib.request.urlopen(f"{self.web_url}/healthz", timeout=1.0):
                return True
        except urllib.error.HTTPError:
            return True
        except OSError:
            return False

    def stop(self, kill: bool = False, timeout: float = 20.0) -> Optional[int]:
        """Stop the process with SIGINT, then SIGKILL if needed. Returns the exit code.

        A non-zero exit is a finding, so the caller decides how to fail: use ``stop_clean`` in
        teardown, which raises with the last output lines.
        """
        if self.proc is None:
            return self.exit_code
        if self.proc.poll() is None:
            try:
                if kill:
                    self.proc.kill()
                else:
                    self.proc.send_signal(signal.SIGINT)
            except ProcessLookupError:  # justification: the process already exited; the code is what matters
                pass
            try:
                self.exit_code = self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.exit_code = self.proc.wait(timeout=timeout)
        else:
            self.exit_code = self.proc.poll()
        if self._reader is not None:
            self._reader.join(timeout=3.0)
        if self.proc.stdout is not None:
            try:
                self.proc.stdout.close()
            except (OSError, ValueError):  # justification: the reader thread may already have closed it
                pass
        return self.exit_code

    def restart(self, timeout: float = 45.0) -> "E2EServer":
        """Stop cleanly and start again with the same config dir and the same state dir.

        This is how a scenario proves material that must survive a process. The SSH host key and the web
        console's auto-generated certificate live under ``OPENMUX_STATE_DIR``, which a reload never touches.
        Nothing is rewritten here, so the second process reads exactly what the first one left, on the
        ports this world was allocated.

        Captured log lines are kept across the restart, so a scenario that wants only the second run's
        output searches with ``wait_for_log(needle, since=len(before))`` -- the offset trick a second
        reload already needs.

        The ports are waited for, not assumed. A listening socket whose last connection the *server* closed
        ends in TIME_WAIT, and macOS then refuses a rebind of that port: one SSH session this harness closed
        politely is enough to leave one behind. A restart that starts half a second after a stop loses that
        race, and the listener that cannot bind is logged as failed and left stopped while the rest of the
        server runs on (issue #115). Waiting for the port costs a fraction of a second in the common case.
        """
        self.stop()
        self.proc = None
        self.exit_code = None
        self._wait_rebindable()
        return self.start(timeout=timeout)

    @staticmethod
    def _port_bindable(port: int) -> bool:
        """True when a server could bind ``port`` right now, checked the way a server binds it."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
        finally:
            probe.close()
        return True

    def _wait_rebindable(self, timeout: float = 30.0) -> None:
        """Wait until every address of this world can be bound again, and name them when it cannot.

        A blocked port is a fact about the previous process, not about the scenario, so a timeout says so
        rather than letting the next start fail with an adapter that never came back.
        """
        ports = [address.port for address in self.addresses]
        deadline = time.monotonic() + timeout
        blocked = [port for port in ports if not self._port_bindable(port)]
        while blocked and time.monotonic() < deadline:
            time.sleep(0.25)
            blocked = [port for port in ports if not self._port_bindable(port)]
        if blocked:
            raise AssertionError(
                f"{self.name} could not rebind {blocked} within {timeout}s; the previous process still "
                "holds those ports (TIME_WAIT), so a listener started here would be logged dead"
            )

    def stop_clean(self, timeout: float = 20.0) -> int:
        """Stop the process and fail when it did not exit as expected or left a child behind.

        The child check covers the command adapter: a ``/bin/cat`` that outlives the server is a
        real leak, and this is the place that can still see it.
        """
        orphans = self.child_pids()
        code = self.stop(timeout=timeout)
        still_alive = [pid for pid in orphans if _pid_alive(pid)]
        wanted = self.expected_exit if self.expected_exit is not None else 0
        if code != wanted:
            raise AssertionError(f"{self.name} exited with code {code}, expected {wanted}\nlast output:\n{self.tail(40)}")
        if still_alive:
            raise AssertionError(f"{self.name} left child processes behind: {still_alive}")
        return code if code is not None else 0

    def child_pids(self) -> List[int]:
        """Direct child pids of the server process.

        ``pgrep -P`` works on both macOS and Linux, and takes the parent as a plain argument.
        ``ps -o pid= -ppid`` looked like the obvious choice and is not portable: macOS answers
        ``ps: Invalid process id: pid``. ``pgrep`` exits 1 when there is no child, which is an
        empty list here and not a failure.

        This is a subprocess in a sync helper called between tests. No event loop is involved, so
        it does not block the harness.
        """
        if self.proc is None:
            return []
        try:
            listing = subprocess.run(
                ["pgrep", "-P", str(self.proc.pid)],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        return [int(token) for token in listing.split() if token.isdigit()]

    # --- signals and output --------------------------------------------------------------

    def send_signal(self, signal_number: int) -> None:
        """Send one signal to the server process."""
        if self.proc is None or self.proc.poll() is not None:
            raise RuntimeError(f"{self.name} is not running")
        self.proc.send_signal(signal_number)

    def reload_soft(self) -> None:
        """SIGHUP: soft reload, which keeps listeners and attached clients."""
        self.send_signal(signal.SIGHUP)

    def reload_full(self) -> None:
        """SIGUSR1: full reload, which stops and starts adapters."""
        self.send_signal(signal.SIGUSR1)

    def output(self) -> str:
        """Everything the process has written, combined stdout and stderr."""
        with self._lines_lock:
            return "\n".join(self._lines)

    def tail(self, count: int = 60) -> str:
        """The last ``count`` output lines, for a failure message."""
        with self._lines_lock:
            return "\n".join(self._lines[-count:])

    def log_lines(self) -> List[str]:
        """A copy of the output lines so far."""
        with self._lines_lock:
            return list(self._lines)

    def wait_for_log(self, needle: str, timeout: float = 15.0, since: int = 0) -> str:
        """Wait for one line containing ``needle`` at or after line ``since``, and return it.

        Fails with the last lines of output, so a missing log line reads as what it is: the server
        never reached that point. Pass ``since=len(server.log_lines())``, taken before the action, when
        the same line appeared earlier in the run: the server repeats this kind of line per client, and
        a plain search is satisfied by the older one.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in self.log_lines()[since:]:
                if needle in line:
                    return line
            if self.proc is not None and self.proc.poll() is not None:
                break
            time.sleep(0.05)
        raise AssertionError(f"log line {needle!r} never appeared after line {since}\nlast output:\n{self.tail(40)}")

    def port_log_lines(self, port_name: str) -> List[str]:
        """Lines from ``{OPENMUX_LOG_DIR}/ports/<port>.log``, the port data log.

        The server creates that file on the first record, so a missing file means no record landed
        yet — the caller decides whether that is a failure or simply early.
        """
        path = self.log_dir / "ports" / f"{port_name}.log"
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()

    def wait_until(self, predicate: Callable[[], bool], timeout: float = 20.0, what: str = "") -> None:
        """Wait for a condition about this server, and say what was waited for when it does not happen.

        A log line is not the only thing that moves: a fake device's own state file, or one field of
        ``/api/power``, changes on the server's own schedule. Poll the fact rather than sleeping a fixed
        time, so a slow server stays slow and a wrong one still fails.
        """
        deadline = time.monotonic() + timeout
        last: Optional[BaseException] = None
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except AssertionError as exc:  # justification: a predicate reading a partial answer is a retry, not a failure
                last = exc
            time.sleep(0.05)
        raise AssertionError(f"{self.name}: {what or 'condition'} never became true within {timeout:.0f}s") from last

    def wait_for_port_log(self, port_name: str, needle: str, timeout: float = 15.0) -> str:
        """Wait for one line containing ``needle`` in the port data log and return it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in self.port_log_lines(port_name):
                if needle in line:
                    return line
            time.sleep(0.05)
        raise AssertionError(
            f"port log {port_name} never contained {needle!r}; lines:\n" "\n".join(self.port_log_lines(port_name)[-20:])
        )

    def config_text(self, filename: str = "server.yaml") -> str:
        """The config file as written on disk, for a test that checks a Config Editor save."""
        return (self.config_dir / filename).read_text(encoding="utf-8")

    def pidfile(self) -> Path:
        """The pid file path inside the pinned run dir."""
        return self.run_dir / "openmux.pid"

    def control_socket(self) -> Path:
        """The control socket path inside the pinned run dir."""
        return self.run_dir / "openmux.sock"

    def cleanup_run_dir(self) -> None:
        """Remove the short run dir, once the server that owned it has stopped."""
        if self.proc is not None and self.proc.poll() is None:
            return
        shutil.rmtree(self.run_dir, ignore_errors=True)

    def meta(self) -> Dict[str, Any]:
        """The facts a failure bundle needs to reproduce this run by hand."""
        return {
            "name": self.name,
            "config_dir": str(self.config_dir),
            "workdir": str(self.workdir),
            "run_dir": str(self.run_dir),
            "addresses": [{"name": a.name, "port": a.port, "kind": a.kind} for a in self.addresses],
            "web_url": self.web_url if any(a.kind == "http" for a in self.addresses) else None,
            "exit_code": self.exit_code,
            "command": (
                None if self.proc is None else " ".join(self.proc.args) if isinstance(self.proc.args, list) else self.proc.args
            ),
        }


def _pid_alive(pid: int) -> bool:
    """True when ``pid`` still exists and is not a zombie we should ignore."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Api:
    """HTTP access with Basic auth, for setup and for cross-checking what a page shows.

    Basic auth without a cookie is exempt from the CSRF check, so this helper needs no token for a
    POST. A test that proves the CSRF rule drives the browser or passes ``csrf=True``.

    Raises:
        AssertionError: when a call returns a status outside 200-299 and ``expect`` allowed only
            success.
    """

    def __init__(self, base_url: str, username: str = "admin", password: str = TEST_PASSWORD):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password

    def request(
        self,
        method: str,
        path: str,
        json_body: Optional[Any] = None,
        expect: Tuple[int, ...] = (200, 201, 202, 204),
        username: Optional[str] = None,
        password: Optional[str] = None,
        csrf: bool = False,
        headers: Optional[Dict[str, str]] = None,
        allow_redirects: bool = False,
    ) -> Tuple[int, Dict[str, str], str]:
        """Perform one request and return ``(status, headers, text)``.

        Fails when the status is outside ``expect``, with the body in the message.
        """
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        data = None
        sent_headers = dict(headers or {})
        user = username or self.username
        secret = password if password is not None else self.password
        if user:
            token = base64.b64encode(f"{user}:{secret}".encode("utf-8")).decode("ascii")
            sent_headers["Authorization"] = f"Basic {token}"
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            sent_headers["Content-Type"] = "application/json"
        if csrf:
            sent_headers["X-OMX-CSRF"] = self.csrf_token(username=user, password=secret)
        request = urllib.request.Request(url, data=data, headers=sent_headers, method=method.upper())
        opener = urllib.request.build_opener() if allow_redirects else urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=20) as response:
                status, raw_headers, body = response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            status, raw_headers, body = error.code, dict(error.headers or {}), error.read()
        text = body.decode("utf-8", errors="replace")
        if status not in expect:
            raise AssertionError(f"{method} {url} -> {status}, expected {expect}\n{response_snippet(text)}")
        return status, raw_headers, text

    def csrf_token(self, username: Optional[str] = None, password: Optional[str] = None) -> str:
        """The CSRF token for a session, from ``GET /api/csrf``."""
        _, _, text = self.request("GET", "/api/csrf", username=username, password=password)
        payload = _json_or_text(text)
        if isinstance(payload, dict):
            for key in ("token", "csrf", "csrf_token"):
                if key in payload:
                    return str(payload[key])
        raise AssertionError(f"/api/csrf returned no token field: {text[:300]}")

    def get(self, path: str, **kwargs: Any) -> Tuple[int, Dict[str, str], str]:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, json_body: Optional[Any] = None, **kwargs: Any) -> Tuple[int, Dict[str, str], str]:
        return self.request("POST", path, json_body=json_body, **kwargs)

    def json(self, path: str, **kwargs: Any) -> Any:
        """The decoded JSON body of one GET."""
        _, _, text = self.get(path, **kwargs)
        return _json_or_text(text)

    def status(self) -> Dict[str, Any]:
        """ "/api/status": ports, clients, and counts."""
        payload = self.json("/api/status")
        return payload if isinstance(payload, dict) else {}

    def port_names(self) -> List[str]:
        """Every port name the server reports, sorted. Used to check a Config Editor save landed."""
        status = self.status()
        names: List[str] = []
        for group in status.values():
            if isinstance(group, list):
                for entry in group:
                    if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                        names.append(entry["name"])
        return sorted(set(names))

    def power(self) -> Dict[str, Any]:
        """``/api/power``: the PDU and outlet snapshot, plus ``unresolved_refs``."""
        payload = self.json("/api/power")
        return payload if isinstance(payload, dict) else {}

    def set_outlet(self, outlet_ref: str, on: bool, **kwargs: Any) -> Dict[str, Any]:
        """One outlet switch through REST: ``POST /api/power/outlets/<ref>``."""
        _, _, text = self.request(
            "POST", f"/api/power/outlets/{urllib.parse.quote(outlet_ref, safe='')}", {"on": on}, **kwargs
        )
        payload = _json_or_text(text)
        return payload if isinstance(payload, dict) else {}


class HttpSession:
    """One cookie-keeping HTTP session: what a browser does, which ``Api`` cannot do.

    ``Api`` sends Basic credentials and no cookie, which is the path exempt from the CSRF check. This
    class keeps the session cookie that ``POST /login`` issues, never follows a redirect, and keeps the
    raw ``Set-Cookie`` headers, so a scenario can prove the refusal paths a signed-in or anonymous
    visitor meets: the login page, the redirect target, the cookie attributes, the CSRF rule, and the
    lockout page.

    A scenario about refusals must see the refusal, so every method accepts any status unless it is
    told otherwise with ``expect``.

    Args:
        base_url: Server root, for example ``http://127.0.0.1:53110``.
        surface: Timeline surface name, default ``http``.
        timeline: Optional ``Timeline``, so a failure shows each request with its status.
    """

    def __init__(self, base_url: str, surface: str = "http", timeline: Any = None, cookie_name: str = "omx_session"):
        self.base_url = base_url.rstrip("/")
        self.surface = surface
        self.timeline = timeline
        self.cookie_name = cookie_name
        self._jar: Dict[str, str] = {}
        #: The raw ``Set-Cookie`` headers of the last response, in order. The attributes are what a
        #: scenario asserts on, and folding them into a dict would lose them.
        self.set_cookies: List[str] = []
        self.last_status: int = 0
        self.last_headers: Dict[str, str] = {}
        self.last_body: str = ""

    # --- what the last response said -------------------------------------------------------

    @property
    def cookie(self) -> Optional[str]:
        """The session cookie value this session holds, or ``None`` when signed out."""
        return self._jar.get(self.cookie_name)

    @property
    def csrf_token(self) -> str:
        """The CSRF token, which for this product is the session id itself."""
        value = self.cookie
        if not value:
            raise AssertionError("this session has no cookie to carry a CSRF token: sign in first")
        return value

    @property
    def location(self) -> Optional[str]:
        """The ``Location`` header of the last response, unmodified."""
        return self.header("Location")

    def header(self, name: str) -> Optional[str]:
        """One response header of the last response, whatever case the server sent it in.

        Header names are case-insensitive, and aiohttp and a proxy do not always agree on the case,
        so a scenario must be able to ask for ``WWW-Authenticate`` and find it.
        """
        wanted = name.lower()
        for key, value in self.last_headers.items():
            if key.lower() == wanted:
                return value
        return None

    def body_contains(self, needle: str) -> bool:
        """True when the last body holds ``needle`` (the login page says its own sentence)."""
        return needle in self.last_body

    # --- requests --------------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        body: Optional[bytes] = None,
        content_type: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        expect: Optional[Tuple[int, ...]] = None,
        csrf: bool = False,
    ) -> Tuple[int, Dict[str, str], str]:
        """Send one request with the current cookie jar, and keep any cookie handed back.

        Raises:
            AssertionError: when ``expect`` is set and the status is outside it.
        """
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        sent = dict(headers or {})
        if csrf:
            sent["X-OMX-CSRF"] = self.csrf_token
        if self._jar:
            sent["Cookie"] = "; ".join(f"{name}={value}" for name, value in self._jar.items())
        if content_type:
            sent["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, headers=sent, method=method.upper())
        self._record("out", f"{method.upper()} {path}")
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=20) as response:
                status, raw_headers, payload = response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            status, raw_headers, payload = error.code, error.headers or {}, error.read()
        self.set_cookies = list(raw_headers.get_all("Set-Cookie", [])) if raw_headers else []
        self._apply_cookies(self.set_cookies)
        self.last_status = int(status)
        self.last_headers = {key: value for key, value in (raw_headers.items() if raw_headers else [])}
        self.last_body = payload.decode("utf-8", errors="replace")
        self._record("in", self._summary_of_last())
        if expect is not None and self.last_status not in expect:
            raise AssertionError(
                f"{method.upper()} {url} -> {self.last_status}, expected {expect}\n{response_snippet(self.last_body)}"
            )
        return self.last_status, self.last_headers, self.last_body

    def get(self, path: str, **kwargs: Any) -> Tuple[int, Dict[str, str], str]:
        """One ``GET``, with the cookie jar."""
        return self.request("GET", path, **kwargs)

    def post_form(self, path: str, fields: Dict[str, str], **kwargs: Any) -> Tuple[int, Dict[str, str], str]:
        """One ``POST`` with an ``application/x-www-form-urlencoded`` body, as an HTML form sends."""
        encoded = urllib.parse.urlencode(fields).encode("utf-8")
        return self.request("POST", path, body=encoded, content_type="application/x-www-form-urlencoded", **kwargs)

    def post_json(self, path: str, payload: Any, **kwargs: Any) -> Tuple[int, Dict[str, str], str]:
        """One ``POST`` with a JSON body."""
        return self.request("POST", path, body=json.dumps(payload).encode("utf-8"), content_type="application/json", **kwargs)

    def with_basic(self, username: str, password: str = TEST_PASSWORD) -> "BasicSession":
        """A view of this session that also sends Basic credentials, for the CSRF-exempt path."""
        return BasicSession(self, username, password)

    # --- the login flow --------------------------------------------------------------------

    def login(
        self,
        username: str,
        password: str = TEST_PASSWORD,
        next_url: Optional[str] = None,
        expect: Optional[Tuple[int, ...]] = None,
    ) -> Tuple[int, Dict[str, str], str]:
        """Fill and submit the real login form. Returns the response without following its redirect.

        Goes through the form, not a hand-built cookie, so a change to the login page fails a scenario
        instead of being hidden by a shortcut.
        """
        fields = {"username": username, "password": password, "next": next_url or "/"}
        return self.post_form("/login", fields, expect=expect)

    def logout(self, next_url: Optional[str] = None, **kwargs: Any) -> Tuple[int, Dict[str, str], str]:
        """One ``GET /logout``, the only method that route answers."""
        target = "/logout" + (f"?next={urllib.parse.quote(next_url, safe='')}" if next_url else "")
        return self.get(target, **kwargs)

    def sign_out(self) -> None:
        """Forget the cookie jar locally, without asking the server."""
        self._jar.clear()

    # --- internals -------------------------------------------------------------------------

    def _apply_cookies(self, headers: Sequence[str]) -> None:
        """Store or drop each cookie, keeping the jar as a browser would.

        An empty value means the same thing as ``Max-Age=0``: the server is taking the cookie back.
        aiohttp writes that empty value quoted (``omx_session=""``), so the quotes come off first.
        """
        for raw in headers:
            name, _, rest = raw.partition("=")
            value, _, attributes = rest.partition(";")
            value = value.strip().strip('"')
            attributes = attributes.lower()
            if not value or "max-age=0" in attributes.replace(" ", ""):
                self._jar.pop(name.strip(), None)
                continue
            self._jar[name.strip()] = value

    def _summary_of_last(self) -> str:
        """A one-line response summary for the timeline: status, redirect target, cookie change."""
        parts = [str(self.last_status)]
        if self.location:
            parts.append(f"location={self.location}")
        for raw in self.set_cookies:
            parts.append(f"set-cookie={raw.split(';')[0]}")
        return " ".join(parts)

    def _record(self, direction: str, text: str) -> None:
        """Write one line to the timeline, when this session has one."""
        if self.timeline is not None:
            self.timeline.record(self.surface, direction, text)


class BasicSession:
    """The same session, plus a Basic header: the pair a CSRF rule treats differently.

    A request that carries both Basic credentials and a session cookie is not exempt from the CSRF
    check, so a scenario needs both shapes from the same jar.
    """

    def __init__(self, session: HttpSession, username: str, password: str = TEST_PASSWORD):
        self.session = session
        self.username = username
        self.password = password

    def auth_header(self) -> Dict[str, str]:
        """The ``Authorization`` header value for these credentials."""
        token = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """``extra`` plus these credentials, so a scenario can add a proxy header or a CSRF token.

        The credentials are applied last: a scenario must not lose them by accident.
        """
        return {**(extra or {}), **self.auth_header()}

    def get(self, path: str, headers: Optional[Dict[str, str]] = None, **kwargs: Any) -> Tuple[int, Dict[str, str], str]:
        """One ``GET`` with Basic credentials, plus any ``headers`` the scenario adds."""
        return self.session.request("GET", path, headers=self.headers(headers), **kwargs)

    def post_json(
        self, path: str, payload: Any, headers: Optional[Dict[str, str]] = None, **kwargs: Any
    ) -> Tuple[int, Dict[str, str], str]:
        """One JSON ``POST`` with Basic credentials, plus any ``headers`` the scenario adds."""
        return self.session.request(
            "POST",
            path,
            body=json.dumps(payload).encode("utf-8"),
            content_type="application/json",
            headers=self.headers(headers),
            **kwargs,
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keeps a redirect as a 302 so a test can assert on the location instead of the landing page."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102 - stdlib signature
        return None


class DeviceStation:
    """A fake TCP device on 127.0.0.1 that an OpenMux TCP-initiator port dials, and that a test unplugs.

    OpenMux is the client here, so this is the listener. It runs on one thread with a self-pipe, and holds
    one accepted connection at a time. ``unplug`` closes that connection and refuses the next one, which is
    what a device losing power looks like on the wire; ``plug_back_in`` accepts again, and the port's own
    ``auto_reconnect`` restores the link with no reload.

    This is the only supported way for a scenario to take a port's link down: every other adapter's
    "down" state needs a real serial device, a real remote host, or a process a test has no handle on.
    """

    def __init__(self, name: str = "device", echo: bool = False) -> None:
        """Bind a free port and start accepting. The port number never changes across an unplug."""
        self.name = name
        self.echo = echo
        self.port = int(free_ports(1)[0])
        self.received = bytearray()
        self.accept_count = 0
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._listener: Optional[socket.socket] = None
        self._link: Optional[socket.socket] = None
        self._listen()
        self._thread = threading.Thread(target=self._serve, name=f"omx-e2e-device-{name}", daemon=True)
        self._thread.start()

    def _listen(self) -> None:
        """Open the listener on the station's port, so the address answers a dial."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port))
        listener.listen(4)
        listener.settimeout(0.2)
        self._listener = listener

    def _serve(self) -> None:
        """Accept connections while the listener stands, and read the live one until it closes."""
        while not self._stopped.is_set():
            listener = self._listener
            if listener is None:
                time.sleep(0.05)
                continue
            try:
                connection, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                continue  # the listener went away, which is an unplug; the loop keeps watching
            self.accept_count += 1
            with self._lock:
                previous = self._link
                self._link = connection
            if previous is not None:
                _close_quietly(previous)
            threading.Thread(target=self._pump, args=(connection,), daemon=True).start()

    def _pump(self, connection: socket.socket) -> None:
        """Read one accepted connection until either side closes it."""
        connection.settimeout(0.2)
        try:
            while not self._stopped.is_set():
                try:
                    payload = connection.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not payload:
                    break
                self.received.extend(payload)
                if self.echo:
                    try:
                        connection.sendall(payload)
                    except OSError:
                        break
        finally:
            with self._lock:
                if self._link is connection:
                    self._link = None
            _close_quietly(connection)

    def unplug(self) -> None:
        """Drop the live link and stop answering the dial, so the port goes offline for good.

        Closing the listener is the point: the next dial is refused, which is what a device that lost
        power looks like. Accepting and then closing instead flaps the link, and the page debounces a
        brief flicker on purpose, so a flap hides the very warning a scenario wants to see.
        """
        with self._lock:
            link, self._link = self._link, None
        if link is not None:
            _close_quietly(link)
        listener, self._listener = self._listener, None
        if listener is not None:
            _close_quietly(listener)

    def plug_back_in(self) -> None:
        """Answer the dial again on the same port, so the port reconnects on its own."""
        self._listen()

    def wait_connected(self, timeout: float = 20.0) -> None:
        """Wait until a live link stands, after the start or after a plug-back-in.

        Raises:
            AssertionError: when no link stands within ``timeout``, which means the port never dialed.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._link is not None:
                    return
            time.sleep(0.05)
        raise AssertionError(f"device {self.name}:{self.port}: no OpenMux link stood up within {timeout}s")

    def close(self) -> None:
        """Stop the station and release its port."""
        self._stopped.set()
        self.unplug()
        _close_quietly(self._listener)
        self._thread.join(timeout=5)


#: Where ``socat`` lives, or ``None`` when the host has none. Homebrew puts it at
#: ``/opt/homebrew/bin/socat`` on macOS.
SOCAT = shutil.which("socat")

#: The reason a Serial Port scenario states when it cannot run because the host has no ``socat``.
SOCAT_MISSING = "these scenarios need socat (brew install socat, or apt install socat)"

LSOF = shutil.which("lsof")
LSOF_MISSING = "these scenarios need lsof to count the server's descriptors (brew install lsof, or apt install lsof)"

NETSTAT = shutil.which("netstat")
NETSTAT_MISSING = "these scenarios need netstat to read a connection queue (it ships with macOS and Linux)"

#: The termios bit that hands the RTS pin to the kernel handshake. Linux names it ``CRTSCTS``, macOS
#: and the other BSDs name it ``CCTS_OFLOW``, and pyserial tries the two in the same order.
_CRTSCTS = getattr(termios, "CRTSCTS", None) or getattr(termios, "CCTS_OFLOW", 0x00010000)


class SerialStation:
    """One fake serial device: a ``socat`` pty pair whose far end the test holds.

    OpenMux opens :attr:`path`, the near link, exactly as it opens a real ``/dev/ttyUSB0``. The test
    holds the far link, so it is the device on the other end of a null-modem cable: it reads the
    exact bytes the server transmits and supplies the exact bytes the server reads, with nothing in
    between to sanitise or echo.

    ``unplug`` stops socat, which leaves :attr:`path` dangling -- the state an operator creates by
    pulling the cable. ``plug_back_in`` starts a pair again under the same two link names, so the
    Port finds its device again on its own reconnect with no config change and no reload.

    The line settings the server applied are readable from a second fd on :attr:`path`
    (:meth:`line_settings`), which is how a scenario proves a configured speed or parity reached the
    device rather than only the config file.
    """

    def __init__(self, tag: str = "dev") -> None:
        """Reserve the two link paths. Nothing exists on disk until :meth:`start`."""
        if SOCAT is None:
            raise RuntimeError(SOCAT_MISSING)
        self.tag = tag
        # The device path is quoted in the API status, the adapter log, and every failure message, so
        # it gets its own short directory instead of pytest's ~90-character tmp_path.
        self.workdir = Path(tempfile.mkdtemp(prefix=f"omxser-{os.getpid()}-"))
        self.path = self.workdir / f"{tag}A"
        self.far_path = self.workdir / f"{tag}B"
        self.proc: Optional[subprocess.Popen] = None
        self._fd: Optional[int] = None
        self._rx: List[bytes] = []
        self._rx_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._closing = threading.Event()
        self.plugged_in = False

    # --- the cable -----------------------------------------------------------------------

    def start(self) -> "SerialStation":
        """Create the pair and open the far end, so OpenMux and the test each hold one side.

        Raises:
            AssertionError: when socat does not create both links, which is its own failure and not
                something a scenario should have to interpret.
        """
        self._closing.clear()
        self.proc = subprocess.Popen(
            [SOCAT, "-d", "-d", f"pty,raw,echo=0,link={self.path}", f"pty,raw,echo=0,link={self.far_path}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.path.is_symlink() and self.far_path.is_symlink():
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"{self.tag}: socat never created both pty links at {self.path}; ran {SOCAT}")
        self._fd = os.open(str(self.far_path), os.O_RDWR | os.O_NOCTTY)
        # Raw, so the far end neither echoes what the server sent nor turns CR into LF on its own.
        tty.setraw(self._fd)
        self._thread = threading.Thread(target=self._pump, name=f"omxser-{self.tag}", daemon=True)
        self._thread.start()
        self.plugged_in = True
        return self

    def plug_back_in(self, timeout: float = 15.0) -> "SerialStation":
        """Bring the device back on the same two paths, which is what a repaired cable looks like."""
        return self.start()

    def unplug(self) -> None:
        """Stop the pair, leaving both link paths dangling. Idempotent."""
        self._closing.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._fd is not None:
            _close_quietly_fd(self._fd)
            self._fd = None
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None
        self.plugged_in = False

    # --- the far end -----------------------------------------------------------------------

    def _pump(self) -> None:
        """Read every byte the server writes, until the pair dies or the test is over."""
        while not self._closing.is_set():
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.05)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            try:
                chunk = os.read(self._fd, 65536)
            except OSError:
                return
            if not chunk:
                return
            with self._rx_lock:
                self._rx.append(chunk)

    def received(self) -> bytes:
        """Everything the device has read from the server since it was opened, in order."""
        with self._rx_lock:
            return b"".join(self._rx)

    def clear(self) -> None:
        """Forget what the device read, so a later assertion sees only the next exchange."""
        with self._rx_lock:
            self._rx.clear()

    def send(self, data: bytes) -> None:
        """Transmit from the device to the server. One call is one write, which may arrive in pieces."""
        if self._fd is None:
            raise AssertionError(f"{self.tag}: the device is unplugged; call plug_back_in() first")
        os.write(self._fd, data)

    def line_settings(self) -> Dict[str, Any]:
        """The line settings the device itself carries.

        Reads the termios of :attr:`path` from a fresh fd, which changes nothing. ``stopbits`` reports
        ``2`` when the two-stop-bit flag is set and ``1`` otherwise, because that flag is also what a
        1.5 stop-bit line uses; ``parity`` names ``N``, ``E``, or ``O`` and does not tell Mark from
        Space, which share those two flags.
        """
        fd = os.open(str(self.path), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            iflag, oflag, cflag, lflag, ispeed, _, _ = termios.tcgetattr(fd)
        finally:
            _close_quietly_fd(fd)
        parity_bits = (bool(cflag & termios.PARENB), bool(cflag & termios.PARODD))
        data_bits = {termios.CS5: 5, termios.CS6: 6, termios.CS7: 7, termios.CS8: 8}.get(cflag & termios.CSIZE)
        return {
            "speed": ispeed,
            "bytesize": data_bits,
            "parity": {(False, False): "N", (True, False): "E", (True, True): "O"}.get(parity_bits, "?"),
            "stopbits": 2 if cflag & termios.CSTOPB else 1,
            "rtscts": bool(cflag & _CRTSCTS),
            "xonxoff": bool(iflag & termios.IXON) and bool(iflag & termios.IXOFF),
            "clocal": bool(cflag & termios.CLOCAL),
            "echo": bool(lflag & termios.ECHO),
            "icanon": bool(lflag & termios.ICANON),
            "onlcr": bool(oflag & termios.ONLCR),
        }

    def close(self) -> None:
        """Stop the pair and remove its directory, so a pass leaves no process and no link behind."""
        self.unplug()
        shutil.rmtree(self.workdir, ignore_errors=True)


def _close_quietly_fd(fd: int) -> None:
    """Close one file descriptor and ignore a failure, which only says it was already closed."""
    try:
        os.close(fd)
    except OSError:
        pass


def _close_quietly(sock: Optional[socket.socket]) -> None:
    """Close one socket and ignore a failure, which only says it was already closed or never opened."""
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


def _json_or_text(text: str) -> Any:
    """Decode JSON, or return the text when the body is not JSON."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def response_snippet(text: str, limit: int = 800) -> str:
    """A short, readable slice of a response body for a failure message."""
    flattened = " ".join(text.split())
    return flattened[:limit] + ("…" if len(flattened) > limit else "")


def read_json(path: Path) -> Dict[str, Any]:
    """Read one JSON file from an artifact bundle."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}
