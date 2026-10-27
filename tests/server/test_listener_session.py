"""Shared behaviour suite for the listener session module (issue #100).

These tests pin the behaviour that both transports (telnet_listener, ssh_listener)
share through `openmux/server/listener_session.py`: the session registry, the
Control menu, the read-write gate, the Escape sequence scanner, the access-frame
rendering, and the power menu call. Per-transport files keep only what the seam
does not cover (auth/menu phase, wire protocol, reconcile).
"""

import asyncio
import logging
import types
from typing import Any, Dict, List, Optional

import pytest

from openmux.server.access_decision import Code, Holder, Mode, decide, holders_frame, notice
from openmux.server.adapters.ssh_listener import SshListenerAdapter
from openmux.server.adapters.telnet_listener import TelnetListenerAdapter
from openmux.server.listener_session import (
    CONTROL_MENU_HELP,
    EscapeState,
    ListenerConfig,
    ListenerSession,
    ListenerSessionManager,
    feed_escape_byte,
    format_rw_notice,
)

# ---------------------------------------------------------------------------
# Fakes


class FakeTransport:
    """SessionTransport stand-in: queued byte chunks in, one buffer out."""

    def __init__(self, chunks: Optional[List[bytes]] = None):
        self.chunks: List[bytes] = list(chunks or [])
        self.buffer = bytearray()
        self.closed = False

    async def read(self, n: int) -> bytes:
        await asyncio.sleep(0)
        if not self.chunks:
            return b""
        return self.chunks.pop(0)

    async def readexactly(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = await self.read(n - len(data))
            if not chunk:
                raise asyncio.IncompleteReadError(data, n)
            data += chunk
        return data

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:
        await asyncio.sleep(0)

    async def close(self) -> None:
        self.closed = True


class _Wiring:
    """Late-bound manager inputs: adapters wire console/auth/port managers late."""

    def __init__(self, cm: Any = None, auth: Any = None, pm: Any = None):
        self.cm = cm
        self.auth = auth
        self.pm = pm


def make_manager(wiring: Optional[_Wiring] = None, *, display_name: str = "Telnet") -> ListenerSessionManager:
    wiring = wiring or _Wiring()
    return ListenerSessionManager(
        display_name=display_name,
        logger=logging.getLogger(f"test.listener_session.{display_name}"),
        channel_owner=object(),
        get_console_manager=lambda: wiring.cm,
        get_auth_manager=lambda: wiring.auth,
        get_port_manager=lambda: wiring.pm,
    )


def make_session(
    chunks: Optional[List[bytes]] = None,
    *,
    read_only: bool = False,
    port_mode: str = "read-only",
    port_name: str = "loopback1",
) -> ListenerSession:
    listener = ListenerConfig(name="l1", bind_host="0.0.0.0", bind_port=1234, target=port_name, read_only=read_only)
    return ListenerSession(
        client_id="c1",
        listener=listener,
        transport=FakeTransport(chunks),
        port_name=port_name,
        read_only=read_only,
        remote_host="127.0.0.1",
        username="bob",
        port_mode=port_mode,
    )


class _FakeCm:
    """A console manager that answers with real ``AccessDecision`` objects.

    The module renders whatever the decision says, so a fake returning the old
    ``(ok, reason)`` tuple would test a shape that no longer exists (issue #98).
    """

    def __init__(self, decision=None, promote_decision=None, demote_decision=None):
        self.take_calls = []
        self.decision = decision if decision is not None else decide(Code.GRANTED, mode=Mode.READ_WRITE)
        self.promote_decision = promote_decision
        self.demote_decision = demote_decision
        self.promote_calls = []
        self.demote_calls = []

    async def take_write_slot(self, client_id, port_name, target=None):
        self.take_calls.append((client_id, port_name, target))
        return self.decision

    async def promote_client_to_read_write(self, client_id, port_name):
        self.promote_calls.append((client_id, port_name))
        return self.promote_decision or decide(Code.GRANTED, mode=Mode.READ_WRITE)

    async def demote_client_to_read_only(self, client_id, port_name):
        self.demote_calls.append((client_id, port_name))
        return self.demote_decision or decide(Code.RELEASED, mode=Mode.READ_ONLY)


# ---------------------------------------------------------------------------
# Registry basics


def test_create_session_client_id_keeps_historical_shape():
    mgr = make_manager(display_name="Telnet")
    listener = ListenerConfig(name="t1", bind_host="0.0.0.0", bind_port=2323, target="loopback1")
    session = mgr.create_session(listener, FakeTransport(), port_name="loopback1", peer_ip="10.0.0.2", username="alice")
    assert session.client_id.startswith("telnet:t1:")
    assert session.username == "alice"
    assert session.remote_host == "10.0.0.2"
    assert session.port_mode == "read-only"


@pytest.mark.asyncio
async def test_disconnect_pops_notifies_and_closes_transport():
    wiring = _Wiring()

    class _Cm:
        def __init__(self):
            self.disconnects = []

        async def disconnect_client_from_port(self, client_id, port_name):
            self.disconnects.append((client_id, port_name))

    cm = _Cm()
    wiring.cm = cm
    mgr = make_manager(wiring)
    session = make_session()
    mgr.sessions["c1"] = session

    await mgr.disconnect("c1", reason="test")

    assert mgr.sessions == {}
    assert cm.disconnects == [("c1", "loopback1")]
    assert session.transport.closed is True


def test_client_meta_reports_lowercase_type_ip_username():
    mgr = make_manager(display_name="SSH")
    session = make_session()
    mgr.sessions["c1"] = session
    assert mgr.client_meta("c1") == {"type": "ssh", "ip": "127.0.0.1", "username": "bob"}
    assert mgr.client_meta("nope") == {}


# ---------------------------------------------------------------------------
# validate_listener_entries, driven through both transports' classmethods

_TRANSPORTS = [(TelnetListenerAdapter, "telnet_listener"), (SshListenerAdapter, "ssh_listener")]


@pytest.mark.parametrize("adapter_cls,section", _TRANSPORTS)
def test_validate_config_require_auth_bool(adapter_cls, section):
    ok = {section: [{"name": "x1", "bind_port": 2323, "target": "loopback1", "require_auth": True}]}
    bad = {section: [{"name": "x1", "bind_port": 2323, "target": "loopback1", "require_auth": "yes"}]}
    assert adapter_cls.validate_config(ok) is True
    assert adapter_cls.validate_config(bad) is False


@pytest.mark.parametrize("adapter_cls,section", _TRANSPORTS)
def test_validate_config_container_branches(adapter_cls, section):
    # Non-dict config is rejected outright; a config without the section is vacuously valid.
    assert adapter_cls.validate_config(["x1"]) is False
    assert adapter_cls.validate_config({}) is True
    assert adapter_cls.validate_config({section: {"name": "x1"}}) is False
    assert adapter_cls.validate_config({section: ["x1"]}) is False


@pytest.mark.parametrize("adapter_cls,section", _TRANSPORTS)
def test_validate_config_name_and_target_branches(adapter_cls, section):
    def cfg(**over):
        entry = {"name": "x1", "bind_port": 2323, "target": "x"}
        entry.update(over)
        return {section: [entry]}

    assert adapter_cls.validate_config(cfg(name=1)) is False
    assert adapter_cls.validate_config(cfg(name="  ")) is False
    dup = {section: [{"name": "x1", "bind_port": 2323, "target": "x"}, {"name": "x1", "bind_port": 2324, "target": "y"}]}
    assert adapter_cls.validate_config(dup) is False
    entry = {"name": "x1", "bind_port": 2323}
    assert adapter_cls.validate_config({section: [entry]}) is False
    assert adapter_cls.validate_config(cfg(target=1)) is False
    assert adapter_cls.validate_config(cfg(target=" ")) is False


@pytest.mark.parametrize("adapter_cls,section", _TRANSPORTS)
def test_validate_config_port_and_host_branches(adapter_cls, section):
    def cfg(**over):
        entry = {"name": "x1", "bind_port": 2323, "target": "x"}
        entry.update(over)
        return {section: [entry]}

    assert adapter_cls.validate_config(cfg(bind_port="abc")) is False
    assert adapter_cls.validate_config(cfg(bind_port=0)) is False
    assert adapter_cls.validate_config(cfg(bind_port=None)) is False
    assert adapter_cls.validate_config(cfg(bind_host=" ")) is False
    assert adapter_cls.validate_config(cfg(bind_host=1)) is False
    assert adapter_cls.validate_config(cfg(bind_host=None)) is True


@pytest.mark.parametrize("adapter_cls,section", _TRANSPORTS)
def test_validate_config_read_only_and_enabled_bools(adapter_cls, section):
    for key in ("read_only", "enabled", "require_auth"):
        bad = {section: [{"name": "x1", "bind_port": 2323, "target": "x", key: 1}]}
        assert adapter_cls.validate_config(bad) is False
    ok = {
        section: [{"name": "x1", "bind_port": 2323, "target": "x", "read_only": False, "enabled": True, "require_auth": False}]
    }
    assert adapter_cls.validate_config(ok) is True


@pytest.mark.parametrize("adapter_cls,section", _TRANSPORTS)
def test_validate_config_acl_branches(adapter_cls, section):
    # acl, when present, must be a list of valid IP / CIDR strings (None acl is allowed).
    assert (
        adapter_cls.validate_config({section: [{"name": "x1", "bind_port": 2323, "target": "x", "acl": "1.2.3.4"}]}) is False
    )
    assert adapter_cls.validate_config({section: [{"name": "x1", "bind_port": 2323, "target": "x", "acl": None}]}) is True
    for rule in (1, "  ", "not-an-ip", "10.0.0.0/33", "10.0.0.256"):
        bad = {section: [{"name": "x1", "bind_port": 2323, "target": "x", "acl": [rule]}]}
        assert adapter_cls.validate_config(bad) is False
    ok = {section: [{"name": "x1", "bind_port": 2323, "target": "x", "acl": ["10.0.0.1", "192.168.0.0/24", "::1/128"]}]}
    assert adapter_cls.validate_config(ok) is True


# ---------------------------------------------------------------------------
# Escape sequence state machine (moved out of listener_common, issue #100)


def _feed(state: EscapeState, data: bytes):
    """Feed a byte string through feed_escape_byte, returning (forwarded, commands)."""
    forwarded = bytearray()
    commands = []
    for i in range(len(data)):
        extra, cmd = feed_escape_byte(state, data[i : i + 1])
        forwarded.extend(extra)
        if cmd:
            commands.append(cmd)
    return bytes(forwarded), commands


def test_feed_escape_byte_plain_data_passes_through():
    state = EscapeState()
    forwarded, commands = _feed(state, b"hello")
    assert forwarded == b"hello"
    assert commands == []


def test_feed_escape_byte_full_sequence_yields_command():
    state = EscapeState()
    forwarded, commands = _feed(state, b"\x05ca")
    assert forwarded == b""
    assert commands == ["a"]


def test_feed_escape_byte_sequence_embedded_in_data():
    state = EscapeState()
    forwarded, commands = _feed(state, b"foo\x05cwbar")
    assert forwarded == b"foobar"
    assert commands == ["w"]


def test_feed_escape_byte_false_alarm_replays_both_bytes():
    # \x05 followed by something other than 'c' is not an escape sequence;
    # both buffered bytes must be replayed verbatim (mirrors console.py).
    state = EscapeState()
    forwarded, commands = _feed(state, b"\x05x")
    assert forwarded == b"\x05x"
    assert commands == []
    assert state.state == 0


def test_feed_escape_byte_state_persists_across_chunk_boundary():
    state = EscapeState()
    # First chunk ends right after the escape char1 byte.
    forwarded1, commands1 = _feed(state, b"\x05")
    assert forwarded1 == b""
    assert commands1 == []
    assert state.state == 1
    # Second chunk supplies char2 and the command mnemonic.
    forwarded2, commands2 = _feed(state, b"ci")
    assert forwarded2 == b""
    assert commands2 == ["i"]


def test_feed_escape_byte_custom_escape_sequence():
    state = EscapeState(escape_char1=b"\x01", escape_char2=b"e")
    forwarded, commands = _feed(state, b"\x01ev")
    assert forwarded == b""
    assert commands == ["v"]
    # The default sequence no longer triggers the menu.
    forwarded2, commands2 = _feed(state, b"\x05ca")
    assert forwarded2 == b"\x05ca"
    assert commands2 == []


def test_control_menu_help_lists_all_commands():
    for cmd in ["a", "f", "s", "w", "i", "v", "e", ".", "?"]:
        assert f"{cmd} " in CONTROL_MENU_HELP or f"{cmd}  " in CONTROL_MENU_HELP


# ---------------------------------------------------------------------------
# format_rw_notice: renders a server-authored access frame (issue #98)
#
# The wording belongs to the access module and is pinned in
# tests/server/test_access_decision.py. Pinned here is the renderer's own job: which
# frames reach a raw terminal, and in what frame.


def test_grant_frame_writes_its_own_line():
    assert format_rw_notice(decide(Code.GRANTED, mode=Mode.READ_WRITE).frame()) == "\r\n[Read-write access granted]\r\n"


def test_takeover_frame_writes_the_taken_from_line():
    victim = Holder(client_id="A", username="alice", ip="10.0.0.5")
    frame = decide(Code.GRANTED, mode=Mode.READ_WRITE, victim=victim).frame()
    assert format_rw_notice(frame) == "\r\n[Read-write access granted]\r\n[Taken from: [A] alice@10.0.0.5 (rw)]\r\n"


def test_refusal_frame_writes_the_servers_sentence():
    frame = decide(Code.INVALID_TARGET, mode=Mode.READ_ONLY).frame()
    assert "does not hold read-write" in format_rw_notice(frame).lower()


def test_demotion_notice_writes_its_line():
    frame = notice(Code.DEMOTED, client_id="c1").frame()
    assert "your read-write access was taken by another user" in format_rw_notice(frame).lower()


def test_a_frame_with_no_wording_writes_nothing():
    # An attach grant stays silent on a terminal that already printed its banner, and a
    # frame the server does not word stays silent: this renderer invents no wording.
    assert format_rw_notice(decide(Code.ATTACHED, mode=Mode.READ_WRITE).frame()) == ""
    assert format_rw_notice({"type": "client_mode", "ok": True, "mode": "read-write"}) == ""
    assert format_rw_notice({}) == ""


def test_presence_push_stays_off_the_terminal():
    assert format_rw_notice({"type": "presence", "holders": ["[A] alice@1.2.3.4 (rw)"]}) == ""


def test_holders_frame_lists_the_labels():
    frame = holders_frame([Holder(client_id="A", username="alice", ip="1.2.3.4")])
    assert format_rw_notice(frame) == "\r\n[Read-write held by: [A] alice@1.2.3.4 (rw)]\r\n"


def test_holders_frame_says_when_no_one_holds_the_slot():
    assert format_rw_notice(holders_frame([])) == "\r\n[No client currently holds read-write access]\r\n"


@pytest.mark.asyncio
async def test_a_decision_from_no_access_manager_says_the_request_failed():
    # The old access_manager_missing_frame round trip collapsed into
    # decide(...).text (issue #100): same sentence, no frame dict.
    mgr = make_manager()
    session = make_session(read_only=False, port_mode="read-write")

    await mgr._reply_access_decision(session, None)

    out = session.transport.buffer.decode().lower()
    assert "could not complete the request" in out
    assert session.port_mode == "read-write"  # the text mode keeps the shown mode


# ---------------------------------------------------------------------------
# _forward_payload read-only gate


@pytest.mark.asyncio
async def test_forward_payload_readonly_reannounces_on_enter():
    mgr = make_manager()
    session = make_session(read_only=True)

    ok = await mgr._forward_payload(session, b"hello\n")

    assert ok is True
    assert b"[WARNING: console is in read-only mode]" in session.transport.buffer


@pytest.mark.asyncio
async def test_forward_payload_readonly_silent_without_enter():
    mgr = make_manager()
    session = make_session(read_only=True)

    ok = await mgr._forward_payload(session, b"hello")

    assert ok is True
    assert session.transport.buffer == b""


# ---------------------------------------------------------------------------
# Read-write commands: a / f / s (targeted takeover, issue #61)


@pytest.mark.asyncio
async def test_force_rw_prompts_and_passes_target():
    wiring = _Wiring()
    mgr = make_manager(wiring)
    session = make_session([b"f", b"4", b"\n"], read_only=False)
    victim = Holder(client_id="f4", username="alice", ip="10.0.0.5")
    cm = _FakeCm(decide(Code.GRANTED, mode=Mode.READ_WRITE, victim=victim))
    wiring.cm = cm

    await mgr._cmd_force_rw(session, cm)

    assert cm.take_calls == [("c1", "loopback1", "f4")]
    out = session.transport.buffer.decode()
    assert "[Take from holder" in out
    assert "Read-write access granted" in out
    assert "[Taken from: [f4] alice@10.0.0.5 (rw)]" in out
    assert session.port_mode == "read-write"


@pytest.mark.asyncio
async def test_force_rw_enter_is_no_target_fallback():
    wiring = _Wiring()
    mgr = make_manager(wiring)
    session = make_session([b"\n"], read_only=False)
    cm = _FakeCm()
    wiring.cm = cm

    await mgr._cmd_force_rw(session, cm)

    assert cm.take_calls == [("c1", "loopback1", None)]
    out = session.transport.buffer.decode()
    assert "granted" in out.lower()
    assert "Taken from" not in out


@pytest.mark.asyncio
async def test_force_rw_invalid_target_refusal_text():
    wiring = _Wiring()
    mgr = make_manager(wiring)
    session = make_session([b"ghost", b"\n"], read_only=False)
    cm = _FakeCm(decide(Code.INVALID_TARGET, mode=Mode.READ_ONLY))
    wiring.cm = cm

    await mgr._cmd_force_rw(session, cm)

    assert session.port_mode != "read-write"
    assert "does not hold read-write" in session.transport.buffer.decode().lower()


@pytest.mark.asyncio
async def test_request_rw_grant_prints_the_servers_line():
    wiring = _Wiring()
    mgr = make_manager(wiring)
    session = make_session(read_only=False)
    cm = _FakeCm(promote_decision=decide(Code.GRANTED, mode=Mode.READ_WRITE))
    wiring.cm = cm

    await mgr._cmd_request_rw(session, cm)

    assert session.port_mode == "read-write"
    assert "[Read-write access granted]\r\n" in session.transport.buffer.decode()


@pytest.mark.asyncio
async def test_release_that_failed_keeps_the_session_read_write():
    """A refused release must not drop the session to read-only (issue #98).

    The pump decides whether to forward keystrokes from ``port_mode``, so a
    session that believed a failed release would stop typing while the Port still
    listed it as the writer.
    """
    wiring = _Wiring()
    mgr = make_manager(wiring)
    session = make_session(read_only=False, port_mode="read-write")
    cm = _FakeCm(demote_decision=decide(Code.DEMOTE_FAILED, mode=Mode.READ_WRITE))
    wiring.cm = cm

    await mgr._cmd_release_rw(session, cm)

    assert session.port_mode == "read-write"
    assert "you do not hold the read-write slot" in session.transport.buffer.decode().lower()


@pytest.mark.asyncio
async def test_force_rw_read_only_listener_rejects_before_prompt():
    wiring = _Wiring()
    mgr = make_manager(wiring)
    session = make_session([b"f", b"4", b"\n"], read_only=True)
    cm = _FakeCm()
    wiring.cm = cm

    await mgr._cmd_force_rw(session, cm)

    assert cm.take_calls == []
    assert b"This listener is configured read-only" in session.transport.buffer


@pytest.mark.asyncio
async def test_force_rw_eof_mid_prompt_stops_session():
    wiring = _Wiring()
    mgr = make_manager(wiring)
    session = make_session([b"f4"], read_only=False)  # EOF, no newline
    cm = _FakeCm()
    wiring.cm = cm

    await mgr._cmd_force_rw(session, cm)

    assert cm.take_calls == []  # the take must NOT run


# ---------------------------------------------------------------------------
# `p` control-menu command: the power menu runs over the session transport


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

    def feed_states(self, port_name):
        # Mirrors the real PduAdapter's local branch (live PDU readings).
        return {ref: self._outlet_on_state(ref) for ref in self.port_power_map(port_name)}

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


def _wire_power(mgr: ListenerSessionManager, wiring: _Wiring, pdu) -> None:
    wiring.cm = types.SimpleNamespace(port_manager=_FakePowerPortManager(pdu))
    wiring.pm = wiring.cm.port_manager
    wiring.auth = _FakeAuth()


@pytest.mark.asyncio
async def test_power_switch_on_numbered_entry():
    wiring = _Wiring()
    mgr = make_manager(wiring)
    pdu = _FakePdu()
    _wire_power(mgr, wiring, pdu)
    session = make_session(_byte_chunks("1\n"), read_only=False)
    session.username = "rw"
    # The menu renders this console's two feeds; typing "1" toggles the first.

    await mgr._handle_control_command(session, "p")

    assert pdu.set_calls == [("rack1.1", False, "rw", "c1")]
    out = session.transport.buffer.decode()
    assert " 1  [on]   rack1.1" in out  # numbered, one per line
    assert " 2  [on]   rack1.2" in out
    assert "POWER rack1.1 -> off" in out


@pytest.mark.asyncio
async def test_power_menu_empty_exits():
    wiring = _Wiring()
    mgr = make_manager(wiring)
    pdu = _FakePdu()
    _wire_power(mgr, wiring, pdu)
    session = make_session([b"\n"], read_only=False)  # just Enter: exit, no change
    session.username = "rw"

    await mgr._handle_control_command(session, "p")

    assert pdu.set_calls == []
    out = session.transport.buffer.decode()
    assert " 1  [on]   rack1.1" in out  # feeds listed
    assert "[EXITING POWER]" in out


@pytest.mark.asyncio
async def test_power_menu_notice_printed_before_prompt():
    wiring = _Wiring()
    mgr = make_manager(wiring)
    pdu = _FakePdu()
    _wire_power(mgr, wiring, pdu)
    session = make_session(_byte_chunks("1\n\n"), read_only=False)
    session.username = "rw"
    mgr.sessions["c1"] = session

    # Mimic the real fan-out: pm.notify_meta_updated runs the (sync) meta
    # listener synchronously, and the listener pushes the [POWER] notice to
    # the session as a background task (ensure_future).
    def emit_meta(port_name, changes):
        mgr.on_port_meta(port_name, changes)

    pdu.meta_emit = emit_meta

    await mgr._handle_control_command(session, "p")

    assert pdu.set_calls == [("rack1.1", False, "rw", "c1")]
    out = session.transport.buffer.decode()
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
    wiring = _Wiring()
    mgr = make_manager(wiring)
    pdu = _FakePdu()
    _wire_power(mgr, wiring, pdu)
    session = make_session(_byte_chunks("1\n"), read_only=False)
    session.username = "nobody"  # not in the fake auth map -> no read-write

    await mgr._handle_control_command(session, "p")

    assert pdu.set_calls == []
    assert "insufficient permission" in session.transport.buffer.decode()


@pytest.mark.asyncio
async def test_power_menu_group_blocked():
    wiring = _Wiring()
    mgr = make_manager(wiring)
    pdu = _FakePdu()
    pdu.blocked = {"rack1.2": ["c2"]}
    _wire_power(mgr, wiring, pdu)
    session = make_session(_byte_chunks("2\n"), read_only=False)  # toggles the 2nd feed
    session.username = "rw"

    await mgr._handle_control_command(session, "p")

    assert pdu.set_calls == []
    out = session.transport.buffer.decode()
    assert "ERROR:POWER: rack1.2 feeds consoles outside your groups (c2)" in out
    assert "needs admin" in out


@pytest.mark.asyncio
async def test_power_menu_invalid_entry_keeps_looping():
    wiring = _Wiring()
    mgr = make_manager(wiring)
    pdu = _FakePdu()
    _wire_power(mgr, wiring, pdu)
    session = make_session(_byte_chunks("9\n2\n\n"), read_only=False)  # "9" is out of range, then "2", then Enter
    session.username = "rw"

    await mgr._handle_control_command(session, "p")

    assert pdu.set_calls == [("rack1.2", False, "rw", "c1")]
    out = session.transport.buffer.decode()
    assert "number out of range (1-2)" in out
    assert "POWER rack1.2 -> off" in out


@pytest.mark.asyncio
async def test_power_notice_pushed_to_attached_session():
    wiring = _Wiring()
    mgr = make_manager(wiring)
    session = make_session()
    mgr.sessions["c1"] = session

    mgr.on_port_meta("loopback1", {"event": "power_outlet_changed", "outlet": "rack1.1", "on": False, "all_power_lost": False})
    await asyncio.sleep(0)
    assert b"[POWER] feed rack1.1 is now off" in session.transport.buffer

    mgr.on_port_meta("loopback1", {"event": "power_outlet_changed", "outlet": "rack1.2", "on": False, "all_power_lost": True})
    await asyncio.sleep(0)
    assert b"[POWER WARNING] all power feeds are now OFF" in session.transport.buffer


@pytest.mark.asyncio
async def test_drop_listener_sessions_only_touches_that_listener():
    wiring = _Wiring()
    mgr = make_manager(wiring)
    keep = make_session(port_name="loopback1")
    keep.listener = ListenerConfig(name="other", bind_host="0.0.0.0", bind_port=1, target="loopback1")
    drop = make_session()
    drop.client_id = "c2"
    mgr.sessions["c1"] = keep
    mgr.sessions["c2"] = drop

    await mgr.drop_listener_sessions("l1", reason="listener removed")

    assert list(mgr.sessions) == ["c1"]
    assert drop.transport.closed is True
    assert keep.transport.closed is False
