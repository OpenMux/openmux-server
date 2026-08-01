"""Tests for the SSH listener's auth/menu-mode/login-shortcut/session features."""

import asyncio
from typing import Any, Dict, List, Optional

import asyncssh
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openmux.server.adapters import ssh_listener as ssh_listener_module
from openmux.server.adapters.ssh_listener import (
    ListenerConfig,
    SshListenerAdapter,
    _match_ssh_pubkey,
    _OpenMuxSshServer,
)


class FakePort:
    """A local (non-federated) port stand-in: no `metadata.origin_server`."""


class FakePortManager:
    def __init__(self, port_names: List[str]):
        self._ports = {name: FakePort() for name in port_names}

    def get_port(self, name: str):
        return self._ports.get(name)

    async def get_port_list_with_federation(self) -> List[Dict[str, Any]]:
        return [{"name": name} for name in self._ports]


class FakeAuthManager:
    def __init__(
        self,
        valid: Optional[Dict[str, str]] = None,
        locked_users: Optional[List[str]] = None,
        pubkeys: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        self.valid = valid or {}
        self.locked_users = set(locked_users or [])
        self.pubkeys = pubkeys or {}
        self.failures: List[str] = []
        self.cleared: List[str] = []

    def is_user_locked(self, username: str, src_ip: Optional[str]) -> bool:
        return username in self.locked_users

    def authenticate_user(self, username: str, password: str) -> bool:
        return self.valid.get(username) == password

    def register_auth_failure(self, username: str, src_ip: Optional[str]) -> None:
        self.failures.append(username)

    def clear_auth_failures(self, username: str, src_ip: Optional[str]) -> None:
        self.cleared.append(username)

    def get_ed25519_pubkeys_for_user_and_use(self, username: str, use: str) -> Dict[str, Any]:
        return dict(self.pubkeys.get(username, {}))


class FakeConsoleManager:
    """Echoes any data written to a port straight back to the same client."""

    def __init__(self, mode: str = "read-write"):
        self.mode = mode
        self.attached: Dict[str, str] = {}
        self.adapter: Optional[SshListenerAdapter] = None
        self.attach_ok = True
        self.client_modes: Dict[str, str] = {}
        self.promote_result = True
        self.rw_holders: List[str] = []

    async def connect_client_to_port(self, client_id: str, port_name: str, username: str):
        if not self.attach_ok:
            return False, None, "port_full"
        self.attached[client_id] = port_name
        self.client_modes[client_id] = self.mode
        return True, self.mode, None

    async def disconnect_client_from_port(self, client_id: str, port_name: str) -> None:
        self.attached.pop(client_id, None)
        self.client_modes.pop(client_id, None)

    async def write_to_port(self, port_name: str, data: bytes, client_id: str) -> None:
        if self.adapter:
            await self.adapter.send_data_to_client(client_id, data)

    async def promote_client_to_read_write(self, client_id: str, port_name: str) -> bool:
        if self.promote_result:
            self.client_modes[client_id] = "read-write"
        return self.promote_result

    async def demote_client_to_read_only(self, client_id: str, port_name: str) -> bool:
        self.client_modes[client_id] = "read-only"
        return True

    async def force_promote_client(self, client_id: str, port_name: str):
        """Demote every other attached client, promote `client_id`, and push
        a cross-adapter demotion notice via the owning adapter directly
        (mirrors ConsoleManager.force_promote_client for single-adapter tests)."""
        undelivered: List[str] = []
        for other_id in list(self.attached):
            if other_id == client_id:
                continue
            self.client_modes[other_id] = "read-only"
            demotion = {"type": "client_mode", "ok": False, "mode": "read-only", "reason": "demoted"}
            if self.adapter is not None:
                delivered = await self.adapter.send_control_frame_to_client(other_id, demotion)
                if not delivered:
                    undelivered.append(other_id)
            else:
                undelivered.append(other_id)
        self.client_modes[client_id] = "read-write"
        return True, undelivered

    def get_rw_holders_display(self, port_name: str) -> List[str]:
        return list(self.rw_holders)

    def register_client_manager(self, manager) -> None:
        pass

    def register_client_channel(self, client_id: str, manager) -> None:
        pass

    def unregister_client_channel(self, client_id: str) -> None:
        pass


async def make_running_adapter(
    tmp_path,
    monkeypatch,
    entry: Dict[str, Any],
    port_names: List[str],
    auth_manager: Optional[FakeAuthManager] = None,
    console_mode: str = "read-write",
    attach_ok: bool = True,
) -> SshListenerAdapter:
    host_key_dir = str(tmp_path / "ssh_listener")
    monkeypatch.setattr(ssh_listener_module, "_HOST_KEY_DIR", host_key_dir)
    monkeypatch.setattr(ssh_listener_module, "_HOST_KEY_PATH", host_key_dir + "/ssh_host_key")

    adapter = SshListenerAdapter("s1", {"ssh_listener": [entry]})
    adapter.main_port_manager = FakePortManager(port_names)
    console = FakeConsoleManager(console_mode)
    console.adapter = adapter
    console.attach_ok = attach_ok
    adapter.set_console_manager(console)
    if auth_manager is not None:
        adapter.set_auth_manager(auth_manager)
    ok = await adapter.start()
    assert ok, "adapter failed to start"
    return adapter


def make_entry(**overrides) -> Dict[str, Any]:
    entry = {
        "name": "s1",
        "bind_host": "127.0.0.1",
        "bind_port": 0,
        "target": "loopback1",
        "require_auth": True,
    }
    entry.update(overrides)
    return entry


async def _echo_roundtrip(port: int, payload: bytes = b"hello\n", **connect_kwargs) -> bytes:
    connect_kwargs.setdefault("client_keys", None)
    async with asyncssh.connect(
        "127.0.0.1",
        port=port,
        known_hosts=None,
        **connect_kwargs,
    ) as conn:
        process = await conn.create_process(encoding=None)
        process.stdin.write(payload)
        await process.stdin.drain()
        return await asyncio.wait_for(process.stdout.read(len(payload)), timeout=5)


# ---------------------------------------------------------------------------
# validate_config


def test_validate_config_require_auth_bool():
    ok = {"ssh_listener": [{"name": "s1", "bind_port": 2222, "target": "loopback1", "require_auth": True}]}
    bad = {"ssh_listener": [{"name": "s1", "bind_port": 2222, "target": "loopback1", "require_auth": "yes"}]}
    assert SshListenerAdapter.validate_config(ok) is True
    assert SshListenerAdapter.validate_config(bad) is False


def test_validate_config_requires_target_and_port():
    missing_target = {"ssh_listener": [{"name": "s1", "bind_port": 2222}]}
    bad_port = {"ssh_listener": [{"name": "s1", "bind_port": 70000, "target": "x"}]}
    assert SshListenerAdapter.validate_config(missing_target) is False
    assert SshListenerAdapter.validate_config(bad_port) is False


def test_build_listener_defaults_require_auth_true():
    adapter = SshListenerAdapter("s1", {"ssh_listener": []})
    spec = adapter._build_listener({"name": "s1", "bind_port": 2222, "target": "loopback1"})
    assert spec is not None
    assert spec.require_auth is True


# ---------------------------------------------------------------------------
# Shared parse_login regression (via listener_common, reused from telnet_listener)


def test_parse_login_shared_with_telnet_listener():
    from openmux.server.adapters.listener_common import parse_login

    assert parse_login("alice+prod-serial0") == ("alice", "prod-serial0")
    assert parse_login("alice:prod-serial0") == ("alice", "prod-serial0")
    assert parse_login("alice:myserver::prod-serial0") == ("alice", "myserver::prod-serial0")
    assert parse_login("alice") == ("alice", None)


# ---------------------------------------------------------------------------
# Real asyncssh integration tests


@pytest.mark.asyncio
async def test_password_auth_success_and_pass_through(tmp_path, monkeypatch):
    auth = FakeAuthManager(valid={"alice": "secret"})
    entry = make_entry(target="loopback1")
    adapter = await make_running_adapter(tmp_path, monkeypatch, entry, ["loopback1"], auth)
    port = adapter.listeners[0].effective_port
    try:
        data = await _echo_roundtrip(port, username="alice", password="secret")
        assert data == b"hello\n"
        assert auth.cleared == ["alice"]
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_password_auth_failure_then_success(tmp_path, monkeypatch):
    auth = FakeAuthManager(valid={"alice": "secret"})
    entry = make_entry(target="loopback1")
    adapter = await make_running_adapter(tmp_path, monkeypatch, entry, ["loopback1"], auth)
    port = adapter.listeners[0].effective_port
    try:
        with pytest.raises(asyncssh.PermissionDenied):
            await asyncssh.connect(
                "127.0.0.1", port=port, username="alice", password="wrong", known_hosts=None, client_keys=None
            )
        assert "alice" in auth.failures
        data = await _echo_roundtrip(port, username="alice", password="secret")
        assert data == b"hello\n"
    finally:
        await adapter.stop()


def test_pubkey_offers_do_not_count_against_password_attempt_cap():
    """Regression test: a real SSH client auto-offers several identities
    (each a routine, unsigned query - not a failed login) before ever
    reaching a password prompt. Those offers must not share the same
    connection-close budget as actual typed password attempts, or a
    legitimate user can be disconnected after typing just one wrong
    password (see the ssh_listener 'Disconnected by application' bug).
    """

    class FakeConn:
        def __init__(self):
            self.closed = False

        def get_extra_info(self, name):
            return ("127.0.0.1", 12345)

        def set_extra_info(self, **kwargs):
            pass

        def close(self):
            self.closed = True

    auth = FakeAuthManager(valid={"alice": "secret"})
    entry = make_entry(target="loopback1")
    adapter = SshListenerAdapter("s1", {"ssh_listener": [entry]})
    adapter.set_auth_manager(auth)
    listener = adapter.listeners[0]

    server = _OpenMuxSshServer(adapter, listener)
    conn = FakeConn()
    server.connection_made(conn)
    server.begin_auth("alice")

    # Two unmatched identities offered automatically by the client - not
    # registered for "alice", so each query is rejected, but this must not
    # consume the password-attempt budget.
    assert server.validate_public_key("alice", object()) is False
    assert server.validate_public_key("alice", object()) is False
    assert conn.closed is False

    # A single mistyped password - previously the 3rd combined attempt,
    # which force-closed the connection before the user could retry.
    assert server.validate_password("alice", "wrong") is False
    assert conn.closed is False
    assert "alice" in auth.failures

    # The correct password still works - the account was never locked out.
    assert server.validate_password("alice", "secret") is True
    assert conn.closed is False


@pytest.mark.asyncio
async def test_password_auth_locked_user_rejected(tmp_path, monkeypatch):
    auth = FakeAuthManager(valid={"alice": "secret"}, locked_users=["alice"])
    entry = make_entry(target="loopback1")
    adapter = await make_running_adapter(tmp_path, monkeypatch, entry, ["loopback1"], auth)
    port = adapter.listeners[0].effective_port
    try:
        with pytest.raises(asyncssh.PermissionDenied):
            await asyncssh.connect(
                "127.0.0.1", port=port, username="alice", password="secret", known_hosts=None, client_keys=None
            )
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_public_key_auth_success(tmp_path, monkeypatch):
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    client_key = asyncssh.import_private_key(priv_pem)
    auth = FakeAuthManager(pubkeys={"alice": {"k1": pub}})
    entry = make_entry(target="loopback1")
    adapter = await make_running_adapter(tmp_path, monkeypatch, entry, ["loopback1"], auth)
    port = adapter.listeners[0].effective_port
    try:
        data = await _echo_roundtrip(port, username="alice", client_keys=[client_key])
        assert data == b"hello\n"
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_public_key_auth_wrong_key_rejected(tmp_path, monkeypatch):
    registered = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    other_pem = other.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    client_key = asyncssh.import_private_key(other_pem)
    auth = FakeAuthManager(pubkeys={"alice": {"k1": registered.public_key()}})
    entry = make_entry(target="loopback1")
    adapter = await make_running_adapter(tmp_path, monkeypatch, entry, ["loopback1"], auth)
    port = adapter.listeners[0].effective_port
    try:
        with pytest.raises(asyncssh.PermissionDenied):
            await asyncssh.connect(
                "127.0.0.1", port=port, username="alice", client_keys=[client_key], known_hosts=None
            )
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_require_auth_false_allows_anonymous(tmp_path, monkeypatch):
    entry = make_entry(target="loopback1", require_auth=False)
    adapter = await make_running_adapter(tmp_path, monkeypatch, entry, ["loopback1"], auth_manager=None)
    port = adapter.listeners[0].effective_port
    try:
        data = await _echo_roundtrip(port, username="anybody")
        assert data == b"hello\n"
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_embedded_login_plus_delimiter_skips_menu(tmp_path, monkeypatch):
    auth = FakeAuthManager(valid={"alice": "secret"})
    entry = make_entry(target="*", require_auth=True)
    adapter = await make_running_adapter(tmp_path, monkeypatch, entry, ["loopback1", "loopback2"], auth)
    port = adapter.listeners[0].effective_port
    try:
        data = await _echo_roundtrip(port, username="alice+loopback2", password="secret")
        assert data == b"hello\n"
        console = adapter.console_manager
        assert list(console.attached.values()) == ["loopback2"]
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_menu_mode_selects_port(tmp_path, monkeypatch):
    auth = FakeAuthManager(valid={"alice": "secret"})
    entry = make_entry(target="*", require_auth=True)
    adapter = await make_running_adapter(tmp_path, monkeypatch, entry, ["loopback1", "loopback2"], auth)
    port = adapter.listeners[0].effective_port
    try:
        async with asyncssh.connect(
            "127.0.0.1", port=port, username="alice", password="secret", known_hosts=None, client_keys=None
        ) as conn:
            process = await conn.create_process(encoding=None)
            await asyncio.wait_for(process.stdout.readuntil(b"Port: "), timeout=5)
            process.stdin.write(b"loopback2\n")
            await process.stdin.drain()
            process.stdin.write(b"ping\n")
            await process.stdin.drain()
            data = await asyncio.wait_for(process.stdout.read(4), timeout=5)
            assert data == b"ping"
        console = adapter.console_manager
        assert list(console.attached.values()) == ["loopback2"]
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_menu_mode_selects_port_with_pty_and_bare_cr(tmp_path, monkeypatch):
    """A real interactive client (pty allocated) sends Enter as a bare `\r`.

    Regression test: `_read_ssh_line` used to rely on `readline()`, which
    only recognizes `\n` and hung forever on pty sessions since asyncssh's
    own line editor is disabled on this raw (`encoding=None`) channel.
    """
    auth = FakeAuthManager(valid={"alice": "secret"})
    entry = make_entry(target="*", require_auth=True)
    adapter = await make_running_adapter(tmp_path, monkeypatch, entry, ["loopback1", "loopback2"], auth)
    port = adapter.listeners[0].effective_port
    try:
        async with asyncssh.connect(
            "127.0.0.1", port=port, username="alice", password="secret", known_hosts=None, client_keys=None
        ) as conn:
            process = await conn.create_process(term_type="xterm", encoding=None)
            await asyncio.wait_for(process.stdout.readuntil(b"Port: "), timeout=5)
            process.stdin.write(b"loopback2\r")
            await process.stdin.drain()
            process.stdin.write(b"ping\r")
            await process.stdin.drain()
            data = await asyncio.wait_for(process.stdout.readuntil(b"ping"), timeout=5)
            assert data.endswith(b"ping")
        console = adapter.console_manager
        assert list(console.attached.values()) == ["loopback2"]
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_acl_denies_disallowed_peer(tmp_path, monkeypatch):
    auth = FakeAuthManager(valid={"alice": "secret"})
    entry = make_entry(target="loopback1", require_auth=True, acl=["203.0.113.5"])
    adapter = await make_running_adapter(tmp_path, monkeypatch, entry, ["loopback1"], auth)
    port = adapter.listeners[0].effective_port
    try:
        with pytest.raises((asyncssh.Error, OSError, ConnectionError, asyncio.TimeoutError)):
            await asyncio.wait_for(
                asyncssh.connect(
                    "127.0.0.1", port=port, username="alice", password="secret", known_hosts=None, client_keys=None
                ),
                timeout=5,
            )
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_exec_request_rejected(tmp_path, monkeypatch):
    auth = FakeAuthManager(valid={"alice": "secret"})
    entry = make_entry(target="loopback1")
    adapter = await make_running_adapter(tmp_path, monkeypatch, entry, ["loopback1"], auth)
    port = adapter.listeners[0].effective_port
    try:
        async with asyncssh.connect(
            "127.0.0.1", port=port, username="alice", password="secret", known_hosts=None, client_keys=None
        ) as conn:
            with pytest.raises(asyncssh.ProcessError):
                await conn.run("echo hi", check=True)
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_read_only_listener_drops_client_input(tmp_path, monkeypatch):
    auth = FakeAuthManager(valid={"alice": "secret"})
    entry = make_entry(target="loopback1", read_only=True)
    adapter = await make_running_adapter(tmp_path, monkeypatch, entry, ["loopback1"], auth)
    port = adapter.listeners[0].effective_port
    try:
        async with asyncssh.connect(
            "127.0.0.1", port=port, username="alice", password="secret", known_hosts=None, client_keys=None
        ) as conn:
            process = await conn.create_process(encoding=None)
            # Connecting to a read-only listener now sends a one-time
            # in-band warning banner; drain it before asserting silence.
            await asyncio.wait_for(process.stdout.readuntil(b"]\r\n"), timeout=1.0)
            process.stdin.write(b"hello\n")
            await process.stdin.drain()
            # Enter re-announces the read-only warning; the input itself is never echoed.
            data = await asyncio.wait_for(process.stdout.readuntil(b"]\r\n"), timeout=1.0)
            assert b"read-only" in data.lower()
            assert b"hello" not in data
    finally:
        await adapter.stop()


# ---------------------------------------------------------------------------
# Ctrl+E,c control-menu escape system


@pytest.mark.asyncio
async def test_escape_menu_help_lists_commands(tmp_path, monkeypatch):
    auth = FakeAuthManager(valid={"alice": "secret"})
    entry = make_entry(target="loopback1")
    adapter = await make_running_adapter(tmp_path, monkeypatch, entry, ["loopback1"], auth)
    port = adapter.listeners[0].effective_port
    try:
        async with asyncssh.connect(
            "127.0.0.1", port=port, username="alice", password="secret", known_hosts=None, client_keys=None
        ) as conn:
            process = await conn.create_process(encoding=None)
            process.stdin.write(b"\x05c?")
            await process.stdin.drain()
            data = await asyncio.wait_for(process.stdout.readuntil(b"Show this menu\r\n"), timeout=5)
            assert b"Control Menu" in data
            for cmd in [b"a ", b"f ", b"s ", b"w ", b"i ", b"v ", b"e ", b". "]:
                assert cmd in data
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_escape_menu_version_and_info(tmp_path, monkeypatch):
    auth = FakeAuthManager(valid={"alice": "secret"})
    entry = make_entry(target="loopback1")
    adapter = await make_running_adapter(tmp_path, monkeypatch, entry, ["loopback1"], auth)
    port = adapter.listeners[0].effective_port
    try:
        async with asyncssh.connect(
            "127.0.0.1", port=port, username="alice", password="secret", known_hosts=None, client_keys=None
        ) as conn:
            process = await conn.create_process(encoding=None)
            process.stdin.write(b"\x05cv")
            await process.stdin.drain()
            data = await asyncio.wait_for(process.stdout.readuntil(b"]\r\n"), timeout=5)
            assert b"OpenMux Server v" in data

            process.stdin.write(b"\x05ci")
            await process.stdin.drain()
            data = await asyncio.wait_for(process.stdout.readuntil(b"--- Session Info ---"), timeout=5)
            assert b"Session Info" in data
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_escape_menu_request_rw_promotes_and_enables_writes(tmp_path, monkeypatch):
    auth = FakeAuthManager(valid={"alice": "secret"})
    entry = make_entry(target="loopback1")
    adapter = await make_running_adapter(
        tmp_path, monkeypatch, entry, ["loopback1"], auth, console_mode="read-only"
    )
    port = adapter.listeners[0].effective_port
    try:
        async with asyncssh.connect(
            "127.0.0.1", port=port, username="alice", password="secret", known_hosts=None, client_keys=None
        ) as conn:
            process = await conn.create_process(encoding=None)
            # Drain the initial read-only warning banner.
            await asyncio.wait_for(process.stdout.readuntil(b"]\r\n"), timeout=5)
            process.stdin.write(b"\x05ca")
            await process.stdin.drain()
            data = await asyncio.wait_for(process.stdout.readuntil(b"]\r\n"), timeout=5)
            assert b"granted" in data

            process.stdin.write(b"hello\n")
            await process.stdin.drain()
            echoed = await asyncio.wait_for(process.stdout.read(6), timeout=5)
            assert echoed == b"hello\n"
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_escape_menu_release_rw_disables_writes(tmp_path, monkeypatch):
    auth = FakeAuthManager(valid={"alice": "secret"})
    entry = make_entry(target="loopback1")
    adapter = await make_running_adapter(tmp_path, monkeypatch, entry, ["loopback1"], auth)
    port = adapter.listeners[0].effective_port
    try:
        async with asyncssh.connect(
            "127.0.0.1", port=port, username="alice", password="secret", known_hosts=None, client_keys=None
        ) as conn:
            process = await conn.create_process(encoding=None)
            process.stdin.write(b"\x05cs")
            await process.stdin.drain()
            data = await asyncio.wait_for(process.stdout.readuntil(b"]\r\n"), timeout=5)
            assert b"read-only" in data.lower()

            process.stdin.write(b"hello\n")
            await process.stdin.drain()
            # Enter re-announces the read-only warning; "hello" is never echoed/forwarded.
            data = await asyncio.wait_for(process.stdout.readuntil(b"]\r\n"), timeout=1.0)
            assert b"read-only" in data.lower()
            assert b"hello" not in data
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_escape_menu_show_holders(tmp_path, monkeypatch):
    auth = FakeAuthManager(valid={"alice": "secret"})
    entry = make_entry(target="loopback1")
    adapter = await make_running_adapter(tmp_path, monkeypatch, entry, ["loopback1"], auth)
    adapter.console_manager.rw_holders = ["bob@10.0.0.9"]
    port = adapter.listeners[0].effective_port
    try:
        async with asyncssh.connect(
            "127.0.0.1", port=port, username="alice", password="secret", known_hosts=None, client_keys=None
        ) as conn:
            process = await conn.create_process(encoding=None)
            process.stdin.write(b"\x05cw")
            await process.stdin.drain()
            data = await asyncio.wait_for(process.stdout.readuntil(b"]\r\n"), timeout=5)
            assert b"bob@10.0.0.9" in data
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_escape_menu_force_take_demotes_other_session(tmp_path, monkeypatch):
    auth = FakeAuthManager(valid={"alice": "secret", "bob": "secret"})
    entry = make_entry(target="loopback1")
    adapter = await make_running_adapter(tmp_path, monkeypatch, entry, ["loopback1"], auth)
    port = adapter.listeners[0].effective_port
    try:
        async with asyncssh.connect(
            "127.0.0.1", port=port, username="alice", password="secret", known_hosts=None, client_keys=None
        ) as conn1, asyncssh.connect(
            "127.0.0.1", port=port, username="bob", password="secret", known_hosts=None, client_keys=None
        ) as conn2:
            p1 = await conn1.create_process(encoding=None)
            p2 = await conn2.create_process(encoding=None)
            console = adapter.console_manager
            first_id, second_id = list(console.attached.keys())

            p2.stdin.write(b"\x05cf")
            await p2.stdin.drain()

            demotion = await asyncio.wait_for(p1.stdout.readuntil(b"]\r\n"), timeout=5)
            assert b"taken by another user" in demotion
            assert console.client_modes[first_id] == "read-only"
            assert console.client_modes[second_id] == "read-write"

            grant = await asyncio.wait_for(p2.stdout.readuntil(b"]\r\n"), timeout=5)
            assert b"granted" in grant
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_escape_menu_change_escape_sequence(tmp_path, monkeypatch):
    auth = FakeAuthManager(valid={"alice": "secret"})
    entry = make_entry(target="loopback1")
    adapter = await make_running_adapter(tmp_path, monkeypatch, entry, ["loopback1"], auth)
    port = adapter.listeners[0].effective_port
    try:
        async with asyncssh.connect(
            "127.0.0.1", port=port, username="alice", password="secret", known_hosts=None, client_keys=None
        ) as conn:
            process = await conn.create_process(encoding=None)
            process.stdin.write(b"\x05ce\x01x")
            await process.stdin.drain()
            data = await asyncio.wait_for(process.stdout.readuntil(b"]\r\n"), timeout=5)
            assert b"Escape sequence changed" in data

            # The old escape sequence is now just plain forwarded data.
            process.stdin.write(b"\x05ci")
            await process.stdin.drain()
            echoed = await asyncio.wait_for(process.stdout.read(4), timeout=5)
            assert echoed == b"\x05ci"

            # The new escape sequence triggers the control menu.
            process.stdin.write(b"\x01xi")
            await process.stdin.drain()
            data = await asyncio.wait_for(process.stdout.readuntil(b"--- Session Info ---"), timeout=5)
            assert b"Session Info" in data
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_read_only_listener_rejects_request_rw_but_allows_help(tmp_path, monkeypatch):
    auth = FakeAuthManager(valid={"alice": "secret"})
    entry = make_entry(target="loopback1", read_only=True)
    adapter = await make_running_adapter(tmp_path, monkeypatch, entry, ["loopback1"], auth)
    port = adapter.listeners[0].effective_port
    try:
        async with asyncssh.connect(
            "127.0.0.1", port=port, username="alice", password="secret", known_hosts=None, client_keys=None
        ) as conn:
            process = await conn.create_process(encoding=None)
            # Drain the initial read-only warning banner.
            await asyncio.wait_for(process.stdout.readuntil(b"]\r\n"), timeout=5)

            process.stdin.write(b"\x05ca")
            await process.stdin.drain()
            data = await asyncio.wait_for(process.stdout.readuntil(b"]\r\n"), timeout=5)
            assert b"configured read-only" in data

            process.stdin.write(b"\x05c?")
            await process.stdin.drain()
            data = await asyncio.wait_for(process.stdout.readuntil(b"Show this menu\r\n"), timeout=5)
            assert b"Control Menu" in data
    finally:
        await adapter.stop()


# ---------------------------------------------------------------------------
# _match_ssh_pubkey unit coverage (no real network)


@pytest.mark.asyncio
async def test_match_ssh_pubkey_true_and_false(tmp_path, monkeypatch):
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    presented = asyncssh.import_private_key(priv_pem)

    auth = FakeAuthManager(pubkeys={"alice": {"k1": pub}})
    assert _match_ssh_pubkey(auth, "alice", presented) is True
    assert _match_ssh_pubkey(auth, "bob", presented) is False
