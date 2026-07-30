"""Tests for the telnet listener's auth/menu-mode/login-shortcut features."""

import asyncio
from typing import Any, Dict, List, Optional, Tuple

import pytest

from openmux.server.adapters.protocols.plain import TelnetIacStripper
from openmux.server.adapters.telnet_listener import ListenerConfig, TelnetListenerAdapter, TelnetSession


class FakeReader:
    """Yields one queued byte chunk per read() call, regardless of n."""

    def __init__(self, chunks: Optional[List[bytes]] = None):
        self.chunks: List[bytes] = list(chunks or [])

    async def read(self, n: int) -> bytes:
        await asyncio.sleep(0)
        if not self.chunks:
            return b""
        return self.chunks.pop(0)


class FakeWriter:
    def __init__(self, peer: Tuple[str, int] = ("127.0.0.1", 55555)):
        self.buffer = bytearray()
        self.closed = False

    def get_extra_info(self, name: str):
        if name == "peername":
            return ("127.0.0.1", 55555)
        return None

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


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
    def __init__(self, valid: Dict[str, str], locked_users: Optional[List[str]] = None):
        self.valid = valid
        self.locked_users = set(locked_users or [])
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


def make_adapter(port_names: List[str], auth_manager: Optional[FakeAuthManager] = None) -> TelnetListenerAdapter:
    adapter = TelnetListenerAdapter("t1", {"telnet_listener": []})
    adapter.main_port_manager = FakePortManager(port_names)
    if auth_manager is not None:
        adapter.auth_manager = auth_manager
    return adapter


def lines(*strings: str) -> List[bytes]:
    return [s.encode() + b"\n" for s in strings]


# ---------------------------------------------------------------------------
# _parse_login delimiter parsing


def test_parse_login_no_delimiter():
    assert TelnetListenerAdapter._parse_login("alice") == ("alice", None)


def test_parse_login_plus_delimiter():
    assert TelnetListenerAdapter._parse_login("alice+prod-serial0") == ("alice", "prod-serial0")


def test_parse_login_colon_delimiter():
    assert TelnetListenerAdapter._parse_login("alice:prod-serial0") == ("alice", "prod-serial0")


def test_parse_login_colon_ignores_double_colon_run():
    # Single ':' delimiter, but the port descriptor itself uses '::' federation syntax.
    assert TelnetListenerAdapter._parse_login("alice:myserver::prod-serial0") == ("alice", "myserver::prod-serial0")


def test_parse_login_plus_wins_over_colon():
    assert TelnetListenerAdapter._parse_login("alice+myserver::prod-serial0") == ("alice", "myserver::prod-serial0")


# ---------------------------------------------------------------------------
# validate_config require_auth field


def test_validate_config_require_auth_bool():
    ok = {"telnet_listener": [{"name": "t1", "bind_port": 2323, "target": "loopback1", "require_auth": True}]}
    bad = {"telnet_listener": [{"name": "t1", "bind_port": 2323, "target": "loopback1", "require_auth": "yes"}]}
    assert TelnetListenerAdapter.validate_config(ok) is True
    assert TelnetListenerAdapter.validate_config(bad) is False


# ---------------------------------------------------------------------------
# _run_login


@pytest.mark.asyncio
async def test_run_login_success_no_embedded_port():
    adapter = make_adapter(["loopback1"], FakeAuthManager({"alice": "secret"}))
    listener = ListenerConfig(name="t1", bind_host="0.0.0.0", bind_port=2323, target="loopback1", require_auth=True)
    reader = FakeReader(lines("alice", "secret"))
    writer = FakeWriter()

    result = await adapter._run_login(listener, reader, writer, "127.0.0.1")

    assert result == ("alice", None)
    out = writer.buffer
    assert b"login: " in out
    assert b"Password: " in out
    assert bytes([255, 251, 1]) in out  # IAC WILL ECHO
    assert bytes([255, 252, 1]) in out  # IAC WONT ECHO


@pytest.mark.asyncio
async def test_run_login_success_with_embedded_port():
    adapter = make_adapter(["loopback1"], FakeAuthManager({"alice": "secret"}))
    listener = ListenerConfig(name="t1", bind_host="0.0.0.0", bind_port=2323, target="*", require_auth=True)
    reader = FakeReader(lines("alice+loopback1", "secret"))
    writer = FakeWriter()

    result = await adapter._run_login(listener, reader, writer, "127.0.0.1")

    assert result == ("alice", "loopback1")


@pytest.mark.asyncio
async def test_run_login_wrong_password_retries_then_fails():
    auth = FakeAuthManager({"alice": "secret"})
    adapter = make_adapter(["loopback1"], auth)
    listener = ListenerConfig(name="t1", bind_host="0.0.0.0", bind_port=2323, target="loopback1", require_auth=True)
    reader = FakeReader(lines("alice", "wrong1", "alice", "wrong2", "alice", "wrong3"))
    writer = FakeWriter()

    result = await adapter._run_login(listener, reader, writer, "127.0.0.1")

    assert result is None
    assert auth.failures == ["alice", "alice", "alice"]
    assert writer.buffer.count(b"Login incorrect") == 3
    assert writer.closed


@pytest.mark.asyncio
async def test_run_login_locked_user_rejected_immediately():
    auth = FakeAuthManager({"alice": "secret"}, locked_users=["alice"])
    adapter = make_adapter(["loopback1"], auth)
    listener = ListenerConfig(name="t1", bind_host="0.0.0.0", bind_port=2323, target="loopback1", require_auth=True)
    reader = FakeReader(lines("alice"))
    writer = FakeWriter()

    result = await adapter._run_login(listener, reader, writer, "127.0.0.1")

    assert result is None
    assert b"Login incorrect" in writer.buffer
    assert writer.closed


@pytest.mark.asyncio
async def test_run_login_no_auth_manager_configured_fails_closed():
    adapter = make_adapter(["loopback1"], auth_manager=None)
    listener = ListenerConfig(name="t1", bind_host="0.0.0.0", bind_port=2323, target="loopback1", require_auth=True)
    reader = FakeReader(lines("alice", "secret"))
    writer = FakeWriter()

    result = await adapter._run_login(listener, reader, writer, "127.0.0.1")

    assert result is None
    assert writer.closed


# ---------------------------------------------------------------------------
# _run_port_menu


@pytest.mark.asyncio
async def test_run_port_menu_valid_selection():
    adapter = make_adapter(["loopback1", "loopback2"])
    reader = FakeReader(lines("loopback1"))
    writer = FakeWriter()

    port_name = await adapter._run_port_menu(writer, reader)

    assert port_name == "loopback1"
    assert b"Available ports:" in writer.buffer


@pytest.mark.asyncio
async def test_run_port_menu_invalid_then_valid():
    adapter = make_adapter(["loopback1"])
    reader = FakeReader(lines("bogus", "loopback1"))
    writer = FakeWriter()

    port_name = await adapter._run_port_menu(writer, reader)

    assert port_name == "loopback1"
    assert b"Unknown port: bogus" in writer.buffer


@pytest.mark.asyncio
async def test_run_port_menu_quit_disconnects():
    adapter = make_adapter(["loopback1"])
    reader = FakeReader(lines("quit"))
    writer = FakeWriter()

    port_name = await adapter._run_port_menu(writer, reader)

    assert port_name is None
    assert writer.closed


@pytest.mark.asyncio
async def test_run_port_menu_list_reprints_then_selects():
    adapter = make_adapter(["loopback1", "loopback2"])
    reader = FakeReader(lines("list", "loopback2"))
    writer = FakeWriter()

    port_name = await adapter._run_port_menu(writer, reader)

    assert port_name == "loopback2"
    assert writer.buffer.count(b"Available ports:") == 2


@pytest.mark.asyncio
async def test_run_port_menu_gives_up_after_max_attempts():
    adapter = make_adapter(["loopback1"])
    reader = FakeReader(lines("bogus", "bogus", "bogus", "bogus", "bogus"))
    writer = FakeWriter()

    port_name = await adapter._run_port_menu(writer, reader)

    assert port_name is None
    assert writer.closed


# ---------------------------------------------------------------------------
# _resolve_session_target integration


@pytest.mark.asyncio
async def test_resolve_session_target_no_auth_fixed_target_unchanged():
    adapter = make_adapter(["loopback1"])
    listener = ListenerConfig(name="t1", bind_host="0.0.0.0", bind_port=2323, target="loopback1", require_auth=False)
    reader = FakeReader([])
    writer = FakeWriter()

    result = await adapter._resolve_session_target(listener, reader, writer, "127.0.0.1")

    assert result == ("telnet_t1", "loopback1")
    assert not writer.buffer  # no prompts printed when auth is disabled


@pytest.mark.asyncio
async def test_resolve_session_target_embedded_port_ignored_on_fixed_listener():
    auth = FakeAuthManager({"alice": "secret"})
    adapter = make_adapter(["loopback1", "loopback2"], auth)
    listener = ListenerConfig(name="t1", bind_host="0.0.0.0", bind_port=2323, target="loopback1", require_auth=True)
    reader = FakeReader(lines("alice+loopback2", "secret"))
    writer = FakeWriter()

    result = await adapter._resolve_session_target(listener, reader, writer, "127.0.0.1")

    # Fixed target always wins; the embedded "loopback2" selector is ignored.
    assert result == ("alice", "loopback1")


@pytest.mark.asyncio
async def test_resolve_session_target_embedded_port_honored_in_menu_mode():
    auth = FakeAuthManager({"alice": "secret"})
    adapter = make_adapter(["loopback1", "loopback2"], auth)
    listener = ListenerConfig(name="t1", bind_host="0.0.0.0", bind_port=2323, target="*", require_auth=True)
    reader = FakeReader(lines("alice+loopback2", "secret"))
    writer = FakeWriter()

    result = await adapter._resolve_session_target(listener, reader, writer, "127.0.0.1")

    assert result == ("alice", "loopback2")


@pytest.mark.asyncio
async def test_resolve_session_target_menu_mode_no_embedded_prompts_for_port():
    auth = FakeAuthManager({"alice": "secret"})
    adapter = make_adapter(["loopback1"], auth)
    listener = ListenerConfig(name="t1", bind_host="0.0.0.0", bind_port=2323, target="*", require_auth=True)
    reader = FakeReader(lines("alice", "secret", "loopback1"))
    writer = FakeWriter()

    result = await adapter._resolve_session_target(listener, reader, writer, "127.0.0.1")

    assert result == ("alice", "loopback1")
    assert b"Port: " in writer.buffer


# ---------------------------------------------------------------------------
# TelnetIacStripper reuse (regression guard for the plain.py extraction)


def test_telnet_iac_stripper_basic():
    stripper = TelnetIacStripper()
    data = bytes([255, 251, 1]) + b"hello" + bytes([255, 255]) + b"world"
    assert stripper.strip(data) == b"hello" + bytes([255]) + b"world"


# ---------------------------------------------------------------------------
# _forward_payload read-only re-announcement (matches CLI/web_console behavior)


def make_session(read_only: bool = True) -> TelnetSession:
    listener = ListenerConfig(name="t1", bind_host="0.0.0.0", bind_port=2323, target="loopback1", read_only=read_only)
    return TelnetSession(
        client_id="c1",
        listener=listener,
        reader=FakeReader(),
        writer=FakeWriter(),
        port_name="loopback1",
        read_only=read_only,
        remote_host="127.0.0.1",
        port_mode="read-only",
    )


@pytest.mark.asyncio
async def test_forward_payload_readonly_reannounces_on_enter():
    adapter = make_adapter(["loopback1"])
    session = make_session()

    ok = await adapter._forward_payload(session, b"hello\n")

    assert ok is True
    assert b"[WARNING: console is in read-only mode]" in session.writer.buffer


@pytest.mark.asyncio
async def test_forward_payload_readonly_silent_without_enter():
    adapter = make_adapter(["loopback1"])
    session = make_session()

    ok = await adapter._forward_payload(session, b"hello")

    assert ok is True
    assert session.writer.buffer == b""
