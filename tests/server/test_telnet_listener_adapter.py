"""Tests for the telnet listener's auth/menu-mode/login-shortcut features."""

import asyncio
import types
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


# ---------------------------------------------------------------------------
# _cmd_force_rw: targeted takeover prompt (issue #61)


class _FakeCm:
    def __init__(self, take_result=(True, "ok")):
        self.take_calls = []
        self.take_result = take_result

    async def take_write_slot(self, client_id, port_name, target=None):
        self.take_calls.append((client_id, port_name, target))
        return self.take_result


@pytest.mark.asyncio
async def test_force_rw_prompts_and_passes_target():
    adapter = make_adapter(["loopback1"])
    session = make_session(read_only=False)
    session.reader = FakeReader([b"f", b"4", b"\n"])
    cm = _FakeCm((True, "takeover from alice [f4]"))
    adapter.console_manager = cm

    await adapter._cmd_force_rw(session, cm)

    assert cm.take_calls == [("c1", "loopback1", "f4")]
    out = session.writer.buffer.decode()
    assert "[Take from holder" in out
    assert "Read-write access granted" in out
    assert "[Taken from: alice [f4]]" in out
    assert session.port_mode == "read-write"


@pytest.mark.asyncio
async def test_force_rw_enter_is_no_target_fallback():
    adapter = make_adapter(["loopback1"])
    session = make_session(read_only=False)
    session.reader = FakeReader([b"\n"])
    cm = _FakeCm((True, "ok"))
    adapter.console_manager = cm

    await adapter._cmd_force_rw(session, cm)

    assert cm.take_calls == [("c1", "loopback1", None)]
    out = session.writer.buffer.decode()
    assert "granted" in out.lower()
    assert "Taken from" not in out


@pytest.mark.asyncio
async def test_force_rw_invalid_target_refusal_text():
    adapter = make_adapter(["loopback1"])
    session = make_session(read_only=False)
    session.reader = FakeReader([b"ghost", b"\n"])
    cm = _FakeCm((False, "invalid_target"))
    adapter.console_manager = cm

    await adapter._cmd_force_rw(session, cm)

    assert session.port_mode != "read-write"
    out = session.writer.buffer.decode()
    assert "does not hold read-write" in out.lower()


@pytest.mark.asyncio
async def test_force_rw_read_only_listener_rejects_before_prompt():
    adapter = make_adapter(["loopback1"])
    session = make_session(read_only=True)
    session.reader = FakeReader([b"f", b"4", b"\n"])
    cm = _FakeCm()
    adapter.console_manager = cm

    await adapter._cmd_force_rw(session, cm)

    assert cm.take_calls == []
    assert b"This listener is configured read-only" in session.writer.buffer


@pytest.mark.asyncio
async def test_force_rw_eof_mid_prompt_stops_session():
    adapter = make_adapter(["loopback1"])
    session = make_session(read_only=False)
    session.reader = FakeReader([b"f4"])  # EOF, no newline
    cm = _FakeCm()
    adapter.console_manager = cm

    await adapter._cmd_force_rw(session, cm)

    assert cm.take_calls == []  # the take must NOT run


@pytest.mark.asyncio
async def test_forward_payload_readonly_silent_without_enter():
    adapter = make_adapter(["loopback1"])
    session = make_session()

    ok = await adapter._forward_payload(session, b"hello")

    assert ok is True
    assert session.writer.buffer == b""


# ---------------------------------------------------------------------------
# `p` control-menu command: interactive, per-console power menu (v3)


def _byte_chunks(text: str) -> List[bytes]:
    """Feed each typed character as its own chunk (one read() per byte)."""
    return [c.encode("latin1") for c in text]


class _FakePdu:
    """Duck-typed PDU adapter: one PDU, two outlets."""

    def __init__(self):
        self.enabled = True
        self.set_calls = []
        self.blocked = {}
        self.meta_emit = None  # when set: called(ref->port,changes) on switch, like the real fan-out

    def get_adapter_type(self):
        return "power"

    def port_power_map(self, port_name):
        # This one console is fed by both outlets of the single PDU.
        return ["rack1.1", "rack1.2"]

    def _outlet_on_state(self, ref):
        return True

    def compute_off_impact(self, ref):
        return {"losing_power": [], "staying_up": []}

    def _power_blocked_ports(self, ref, _username):
        return self.blocked.get(ref, [])

    async def set_outlet(self, ref, on, user=None, client_id=None):
        self.set_calls.append((ref, on, user, client_id))
        if self.meta_emit is not None:
            self.meta_emit(
                "loopback1",
                {"event": "power_outlet_changed", "outlet": ref, "on": on, "all_power_lost": False},
            )
        return {"ok": True, "reading": {"on": on}}

    def get_power_snapshot(self):
        return {
            "pdus": [
                {
                    "name": "rack1",
                    "driver": "dummy",
                    "online": True,
                    "outlets_on": 2,
                    "outlet_count": 2,
                    "outlets": [
                        {"ref": "rack1.1", "on": True, "watts": None, "volts": None, "mapped_ports": []},
                        {"ref": "rack1.2", "on": True, "watts": None, "volts": None, "mapped_ports": []},
                    ],
                }
            ],
            "unresolved_refs": [],
        }


class _FakeAuth:
    def get_user_permissions(self, username):
        return {"admin": "admin", "rw": "read-write"}.get(username)


class _FakePowerPortManager:
    def __init__(self, pdu):
        self.unified_adapters = [pdu]


def _wire_power(adapter, pdu, port_name="loopback1"):
    cm = types.SimpleNamespace(port_manager=_FakePowerPortManager(pdu))
    adapter.console_manager = cm
    adapter.main_port_manager = cm.port_manager
    adapter.auth_manager = _FakeAuth()
    return cm


@pytest.mark.asyncio
async def test_power_switch_on_numbered_entry():
    adapter = make_adapter(["loopback1"])
    pdu = _FakePdu()
    _wire_power(adapter, pdu)
    session = make_session(read_only=False)
    session.username = "rw"
    # The menu renders this console's two feeds; typing "1" toggles the first.
    session.reader = FakeReader(_byte_chunks("1\n"))

    await adapter._handle_control_command(session, "p")

    assert pdu.set_calls == [("rack1.1", False, "rw", "c1")]
    out = session.writer.buffer.decode()
    assert " 1  [on]   rack1.1" in out  # numbered, one per line
    assert " 2  [on]   rack1.2" in out
    assert "POWER rack1.1 -> off" in out


@pytest.mark.asyncio
async def test_power_menu_empty_exits():
    adapter = make_adapter(["loopback1"])
    pdu = _FakePdu()
    _wire_power(adapter, pdu)
    session = make_session(read_only=False)
    session.username = "rw"
    session.reader = FakeReader([b"\n"])  # just Enter: exit, no change

    await adapter._handle_control_command(session, "p")

    assert pdu.set_calls == []
    out = session.writer.buffer.decode()
    assert " 1  [on]   rack1.1" in out  # feeds listed
    assert "[EXITING POWER]" in out


@pytest.mark.asyncio
async def test_power_menu_notice_printed_before_prompt():
    adapter = make_adapter(["loopback1"])
    pdu = _FakePdu()
    _wire_power(adapter, pdu)
    session = make_session(read_only=False)
    session.username = "rw"
    adapter.sessions["c1"] = session
    session.reader = FakeReader(_byte_chunks("1\n\n"))

    # Mimic the real fan-out: pm.notify_meta_updated runs the (sync) meta
    # listener synchronously, and the listener pushes the [POWER] notice to
    # the session as a background task (ensure_future).
    def emit_meta(port_name, changes):
        adapter._on_port_meta_update(port_name, changes)

    pdu.meta_emit = emit_meta

    await adapter._handle_control_command(session, "p")

    assert pdu.set_calls == [("rack1.1", False, "rw", "c1")]
    out = session.writer.buffer.decode()
    notice_i = out.find("[POWER] feed rack1.1 is now off")
    # The header prints once; the list re-renders without it. Mark the
    # re-render by the last feed line's second occurrence.
    feed = " 2  [on]   rack1.2"
    rerender_i = out.find(feed, out.find("POWER rack1.1 -> off"))
    assert notice_i != -1 and rerender_i != -1
    assert notice_i < rerender_i  # the notice must not displace the re-rendered list
    assert out.rfind(feed) > notice_i  # the loop re-renders the list after the notice


@pytest.mark.asyncio
async def test_power_menu_requires_read_write():
    adapter = make_adapter(["loopback1"])
    pdu = _FakePdu()
    _wire_power(adapter, pdu)
    session = make_session(read_only=False)
    session.username = "nobody"  # not in the fake auth map -> no read-write
    session.reader = FakeReader(_byte_chunks("1\n"))

    await adapter._handle_control_command(session, "p")

    assert pdu.set_calls == []
    assert "insufficient permission" in session.writer.buffer.decode()


@pytest.mark.asyncio
async def test_power_menu_group_blocked():
    adapter = make_adapter(["loopback1"])
    pdu = _FakePdu()
    pdu.blocked = {"rack1.2": ["c2"]}
    _wire_power(adapter, pdu)
    session = make_session(read_only=False)
    session.username = "rw"
    session.reader = FakeReader(_byte_chunks("2\n"))  # toggles the 2nd feed

    await adapter._handle_control_command(session, "p")

    assert pdu.set_calls == []
    out = session.writer.buffer.decode()
    assert "ERROR:POWER: rack1.2 feeds consoles outside your groups (c2)" in out
    assert "needs admin" in out


@pytest.mark.asyncio
async def test_power_menu_invalid_entry_keeps_looping():
    adapter = make_adapter(["loopback1"])
    pdu = _FakePdu()
    _wire_power(adapter, pdu)
    session = make_session(read_only=False)
    session.username = "rw"
    # "9" is out of range, then "2" toggles the second feed, then Enter exits.
    session.reader = FakeReader(_byte_chunks("9\n2\n\n"))

    await adapter._handle_control_command(session, "p")

    assert pdu.set_calls == [("rack1.2", False, "rw", "c1")]
    out = session.writer.buffer.decode()
    assert "number out of range (1-2)" in out
    assert "POWER rack1.2 -> off" in out


@pytest.mark.asyncio
async def test_power_notice_pushed_to_attached_session():
    adapter = make_adapter(["loopback1"])
    pdu = _FakePdu()
    _wire_power(adapter, pdu)
    session = make_session()
    adapter.sessions["c1"] = session

    adapter._on_port_meta_update(
        "loopback1",
        {"event": "power_outlet_changed", "outlet": "rack1.1", "on": False, "all_power_lost": False},
    )
    await asyncio.sleep(0)
    assert b"[POWER] feed rack1.1 is now off" in session.writer.buffer

    adapter._on_port_meta_update(
        "loopback1",
        {"event": "power_outlet_changed", "outlet": "rack1.2", "on": False, "all_power_lost": True},
    )
    await asyncio.sleep(0)
    assert b"[POWER WARNING] all power feeds are now OFF" in session.writer.buffer
