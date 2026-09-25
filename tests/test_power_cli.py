"""Tests for the PDU POWER text command + in-console notices (client_listener).

Covers the ``POWER`` command forms (list, single PDU, single outlet, switch
on/off with read-write gating) and the ``power_outlet_changed`` meta notice
push. The PduAdapter is real; the client session, console/port manager, and
auth manager are fakes.
"""

import asyncio

import pytest

asyncio_test = pytest.mark.asyncio

from openmux.server.adapters.client_listener import TcpServerAdapter
from openmux.server.adapters.pdu import PduAdapter

POWER_SECTION = {
    "power": {
        "enabled": True,
        "pdus": [
            {
                "name": "rack1",
                "driver": "dummy",
                "poll_interval": 0,
                "options": {"outlets": ["1", "2"]},
                "outlets": [{"id": "2", "description": "Switch A"}],
            },
        ],
    }
}


class _FakePort:
    def __init__(self, name, power=(), rw_groups=(), ro_groups=()):
        self.name = name
        self.power = list(power)
        self.unified_port = self
        self.read_write_groups = list(rw_groups)
        self.read_only_groups = list(ro_groups)


class _FakePortManager:
    def __init__(self, ports, access_default="allow"):
        self.ports = dict(ports)
        self.unified_adapters = []
        self.meta_events = []
        self.access_default = access_default

    def notify_meta_updated(self, port_name, changes):
        self.meta_events.append((port_name, changes))

    def safe_get_port(self, name):
        return None

    def get_port(self, name):
        return self.ports.get(name)


class _FakeConsoleManager:
    def __init__(self, pm, auth=None):
        self.port_manager = pm
        self.auth_manager = auth
        self.security_policy = None

    def blocked_ports_for_user(self, port_names, username):
        """Mirror of the real ConsoleManager ladder (group ACL + access_default)."""
        if self.auth_manager.get_user_permissions(username) == "admin":
            return []
        user_groups = self.auth_manager.get_user_groups(username)
        blocked = []
        for name in port_names:
            port = self.port_manager.ports.get(name)
            rw = set(getattr(port, "read_write_groups", None) or [])
            ro = set(getattr(port, "read_only_groups", None) or [])
            if rw or ro:
                if not (user_groups & rw):
                    blocked.append(name)
            elif getattr(self.port_manager, "access_default", "allow") == "deny":
                blocked.append(name)
        return blocked


class _FakeAuth:
    def __init__(self, permissions, groups=None):
        self._perm = permissions
        self._groups = groups or {}

    def get_user_permissions(self, username):
        return self._perm.get(username)

    def get_user_groups(self, username):
        if self._perm.get(username) is None:
            return set()
        return {"user"} | set(self._groups.get(username) or [])


class _FakeClient:
    def __init__(self, username="u1"):
        self.username = username
        self.lines = []
        self.raw = []
        self.connected_port = None

    async def send_line(self, text):
        self.lines.append(text)

    async def send_raw_data(self, data: bytes):
        self.raw.append(data.decode("utf-8", errors="replace"))

    @property
    def client_id(self):
        return "cid-1"


def _build():
    pm = _FakePortManager({"c1": _FakePort("c1", ["rack1.1", "rack1.2"])})
    auth = _FakeAuth({"u1": "read-write", "ro": "read-only"})
    pdu = PduAdapter("power", POWER_SECTION)
    pdu.main_port_manager = pm
    pdu.set_auth_manager(auth)
    pm.unified_adapters = [pdu]
    cm = _FakeConsoleManager(pm, auth)
    pdu.set_console_manager(cm)
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    adapter.set_console_manager(cm)
    adapter.set_auth_manager(auth)
    return adapter, pdu, pm


async def _started():
    adapter, pdu, pm = _build()
    assert await pdu.start() is True
    await asyncio.sleep(0)
    return adapter, pdu, pm


@pytest.mark.asyncio
async def test_power_not_configured_when_no_adapter():
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    pm = _FakePortManager({})
    cm = _FakeConsoleManager(pm)
    adapter.set_console_manager(cm)
    client = _FakeClient()
    await adapter.process_client_command(client, "POWER")
    assert "not configured" in client.lines[0]


@pytest.mark.asyncio
async def test_power_list_all_pdus():
    adapter, pdu, pm = await _started()
    client = _FakeClient()
    await adapter.process_client_command(client, "POWER")
    text = "\n".join(client.lines)
    assert "PDU rack1 (dummy)" in text
    assert "2/2 on" in text  # dummy outlets start on
    assert "POWER rack1.1 on" in text
    assert "POWER rack1.2 on" in text
    assert " -> c1" in text  # mapped console shown
    await pdu.stop()


@pytest.mark.asyncio
async def test_power_list_single_pdu_and_single_outlet():
    adapter, pdu, pm = await _started()
    client = _FakeClient()
    await adapter.process_client_command(client, "POWER rack1")
    assert all(l.startswith("POWER rack1.") for l in client.lines)
    assert len(client.lines) == 2
    await adapter.process_client_command(client, "POWER rack1.1")
    assert client.lines[-1].startswith("POWER rack1.1 on")
    await pdu.stop()


@pytest.mark.asyncio
async def test_power_switch_toggle_and_state():
    adapter, pdu, pm = await _started()
    client = _FakeClient()
    await adapter.process_client_command(client, "POWER rack1.1 off")
    assert "WARNING:POWER" not in "\n".join(client.lines)  # c1 keeps rack1.2
    assert client.lines[-1] == "POWER rack1.1 -> off"
    # Now c1 has only rack1.2; turning it off loses ALL power -> warning
    client2 = _FakeClient()
    await adapter.process_client_command(client2, "POWER rack1.2 off")
    joined = "\n".join(client2.lines)
    assert "WARNING:POWER: removing all power to: c1" in joined
    assert "POWER rack1.2 -> off" in client2.lines[-1]
    await pdu.stop()


@pytest.mark.asyncio
async def test_power_switch_requires_read_write():
    adapter, pdu, pm = await _started()
    client = _FakeClient(username="ro")
    await adapter.process_client_command(client, "POWER rack1.1 off")
    assert "insufficient permission" in client.lines[0]
    await pdu.stop()


def _build_grouped():
    """Two-group fixture: c2 is ops-RW, c9 is lab-RW; ops/lab users below."""
    pm = _FakePortManager(
        {
            "c1": _FakePort("c1", ["rack1.1"]),
            "c2": _FakePort("c2", ["rack1.2"], rw_groups=["ops"]),
            "c9": _FakePort("c9", ["rack1.2"], rw_groups=["lab"]),
        }
    )
    auth = _FakeAuth(
        {"ops": "read-write", "lab": "read-write", "boss": "admin"},
        groups={"ops": ["ops"], "lab": ["lab"]},
    )
    pdu = PduAdapter("power", POWER_SECTION)
    pdu.main_port_manager = pm
    pdu.set_auth_manager(auth)
    pm.unified_adapters = [pdu]
    cm = _FakeConsoleManager(pm, auth)
    pdu.set_console_manager(cm)
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    adapter.set_console_manager(cm)
    adapter.set_auth_manager(auth)
    return adapter, pdu, pm


async def _started_grouped():
    adapter, pdu, pm = _build_grouped()
    assert await pdu.start() is True
    await asyncio.sleep(0)
    return adapter, pdu, pm


@pytest.mark.asyncio
async def test_power_switch_blocked_outside_groups():
    adapter, pdu, pm = await _started_grouped()
    client = _FakeClient(username="ops")
    await adapter.process_client_command(client, "POWER rack1.2 off")
    text = "\n".join(client.lines)
    assert "outside your groups" in text
    assert "needs admin" in text
    assert "c9" in text  # the other group's console is named
    assert pdu.pdus["rack1"].readings["2"].on is True  # state unchanged


@pytest.mark.asyncio
async def test_power_switch_allowed_when_all_feds_in_groups():
    adapter, pdu, pm = await _started_grouped()
    client = _FakeClient(username="ops")
    await adapter.process_client_command(client, "POWER rack1.1 off")
    assert client.lines[-1] == "POWER rack1.1 -> off"  # c1 has no group lists: global rw applies


@pytest.mark.asyncio
async def test_power_switch_admin_bypasses_groups():
    adapter, pdu, pm = await _started_grouped()
    client = _FakeClient(username="boss")
    await adapter.process_client_command(client, "POWER rack1.2 off")
    joined = "\n".join(client.lines)
    assert "outside your groups" not in joined
    assert "POWER rack1.2 -> off" in client.lines[-1]


@pytest.mark.asyncio
async def test_power_unknown_outlet_and_errors():
    adapter, pdu, pm = await _started()
    client = _FakeClient()
    await adapter.process_client_command(client, "POWER rack1.9 off")
    assert client.lines[0].startswith("ERROR:POWER")
    await adapter.process_client_command(client, "POWER rack1.1")
    assert client.lines[-1].startswith("POWER rack1.1")  # single-outlet line
    await adapter.process_client_command(client, "POWER nope.1")
    assert "unknown outlet" in client.lines[-1]
    await adapter.process_client_command(client, "POWER rack1.1 maybe")
    assert "must be 'on' or 'off'" in client.lines[-1]
    await pdu.stop()


@pytest.mark.asyncio
async def test_power_warning_when_all_lost_and_note_when_staying():
    adapter, pdu, pm = await _started()
    # c1 is fed by rack1.1 and rack1.2. Turn rack1.1 off first (leaving rack1.2).
    await pdu.set_outlet("rack1.1", False)
    client = _FakeClient()
    # Turn rack1.2 off: c1 would lose ALL power -> WARNING
    await adapter.process_client_command(client, "POWER rack1.2 off")
    joined = "\n".join(client.lines)
    assert "WARNING:POWER: removing all power to: c1" in joined
    await pdu.stop()


@pytest.mark.asyncio
async def test_meta_change_pushes_notice_to_attached_clients():
    adapter, pdu, pm = await _started()
    # Attach a client to c1 so the notice target exists
    port_name = "c1"
    cid = "cid-att"
    pm.ports = dict(pm.ports)
    attached = _FakeClient()
    attached.lines = []
    adapter.port_clients = {port_name: [cid]}
    adapter.clients = {cid: attached}
    # Directly invoke the meta listener with a power event (as PortManager does)
    adapter._on_port_meta_update(
        port_name,
        {
            "event": "power_outlet_changed",
            "outlet": "rack1.1",
            "on": False,
            "all_power_lost": False,
            "other_outlets_on": ["rack1.2"],
        },
    )
    await asyncio.sleep(0)
    assert any("feed rack1.1 is now off" in l for l in attached.raw)
    # All-lost variant produces the WARNING notice
    attached.raw.clear()
    adapter._on_port_meta_update(
        port_name,
        {"event": "power_outlet_changed", "outlet": "rack1.2", "on": False, "all_power_lost": True, "other_outlets_on": []},
    )
    await asyncio.sleep(0)
    assert any("POWER WARNING" in l and "rack1.2" in l for l in attached.raw)
    await pdu.stop()


# --- remote feeds (outlet federation) ------------------------------------------


class _FakeRemote:
    """Stands in for a muxcon RemotePortProxy declaring the origin's feeds.

    ``feeds`` are the ORIGIN-LOCAL refs (as the muxcon registration layer
    receives them on the wire); the proxy stores them GLOBALLY qualified as
    "<origin>::<ref>" (outlet federation), mirroring
    `RemotePortProxy`/`_register_remote_port_from_dict`. ``states`` is keyed
    by the same global form.
    """

    def __init__(self, name, feeds=(), states=None, origin_id="peerO", sessions=None):
        self.name = name
        self.remote_port_name = name
        self.power = [f"{origin_id}::{r}" for r in feeds]
        self._feed_states = {f"{origin_id}::{k}": v for k, v in (states or {}).items()}
        self.metadata = type("M", (), {"origin_server": type("O", (), {"server_id": origin_id})})
        self._client_sessions = dict(sessions or {})
        self.is_connected = True


class _FakeFeedMuxcon:
    """Stands in for the muxcon adapter's relay (POWER:SWITCH -> POWER:RESULT)."""

    def __init__(self, reply):
        self.reply = dict(reply)
        self.calls = []

    def get_adapter_type(self):
        return "muxcon"

    async def relay_power_switch(self, port_name, ref, on, claims, client_id=None):
        self.calls.append({"port": port_name, "ref": ref, "on": on, "claims": list(claims), "client_id": client_id})
        return dict(self.reply)


async def _started_federated(states=None, origin_id="peerO", mux_reply=None, feed_port_sessions=None, blocked_feed_port=None):
    # Fed-port feeds are ORIGIN-LOCAL at the call site; the proxy qualifies
    # them globally ("<origin>::<ref>") at registration.
    pm = _FakePortManager(
        {
            "c1": _FakePort("c1", ["rack1.1", "rack1.2"]),
            "r1": _FakeRemote("r1", ["rack9.1", "rack9.2"], states, origin_id, feed_port_sessions),
        }
    )
    if blocked_feed_port is not None:
        pm.ports["r2"] = _FakeRemote("r2", ["rack9.1"], {"rack9.1": True}, "peerO", {"cid-1": 4})
        pm.ports["r2"].read_write_groups = ["other"]
        pm.ports["r2"].read_only_groups = ["other"]
    auth = _FakeAuth({"u1": "read-write", "ro": "read-only", "boss": "admin"})
    pdu = PduAdapter("power", POWER_SECTION)
    pdu.main_port_manager = pm
    pdu.set_auth_manager(auth)
    adapters = [pdu]
    if mux_reply is not None:
        adapters.append(_FakeFeedMuxcon(mux_reply))
    pm.unified_adapters = adapters
    cm = _FakeConsoleManager(pm, auth)
    pdu.set_console_manager(cm)
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    adapter.set_console_manager(cm)
    adapter.set_auth_manager(auth)
    assert await pdu.start() is True
    await asyncio.sleep(0)
    return adapter, pdu, pm


@pytest.mark.asyncio
async def test_switch_remote_owned_ref_relayed_to_origin():
    mux_reply = {"ok": True, "on": False}
    adapter, pdu, pm = await _started_federated(
        states={"rack9.1": True, "rack9.2": True}, mux_reply=mux_reply, feed_port_sessions={"cid-1": 3}
    )
    muxcon = pm.unified_adapters[1]
    client = _FakeClient()
    client.connected_port = "r1"
    await adapter.process_client_command(client, "POWER peerO::rack9.1 off")
    # Last line is the switch confirmation; the (visible) off-impact preview
    # may precede it, as for local refs.
    assert client.lines[-1] == "POWER peerO::rack9.1 -> off"
    # The frame is anchored on the acting client's own session on the fed
    # port, carries the visible fed ports (claims are port names, shared by
    # both sides), and the ref it SENDS is origin-local (the origin checks
    # and executes its own local ref).
    assert muxcon.calls == [{"port": "r1", "ref": "rack9.1", "on": False, "claims": ["r1"], "client_id": "cid-1"}]
    # The local dummy PDU was not touched (no local outlet named rack9.*).
    assert "rack9" not in pdu.pdus
    await pdu.stop()


@pytest.mark.asyncio
async def test_switch_remote_ref_same_name_as_local_outlet_goes_to_origin():
    # The collision regression: BOTH the local node and the origin have an
    # outlet named rack1.1. The fed port's ref is the globally qualified
    # "peerO::rack1.1", so switching it relays to the origin; switching the
    # bare "rack1.1" hits the local PDU. Neither name shadows the other.
    mux_reply = {"ok": True, "on": False}
    adapter, pdu, pm = await _started_federated(states={"rack1.1": True}, mux_reply=mux_reply, feed_port_sessions={"cid-1": 3})
    pm.ports["r1"].power = ["peerO::rack1.1"]
    pm.ports["r1"]._feed_states = {"peerO::rack1.1": True}
    muxcon = pm.unified_adapters[1]
    client = _FakeClient()
    client.connected_port = "r1"
    # The GLOBAL ref ("peerO::rack1.1") goes to the origin (not the local PDU).
    await adapter.process_client_command(client, "POWER peerO::rack1.1 off")
    assert client.lines[-1] == "POWER peerO::rack1.1 -> off"
    assert muxcon.calls == [{"port": "r1", "ref": "rack1.1", "on": False, "claims": ["r1"], "client_id": "cid-1"}]
    assert pdu.pdus["rack1"].readings["1"].on is True  # local outlet untouched
    # The BARE ref still means the local outlet (local path, no relay).
    client2 = _FakeClient()
    await adapter.process_client_command(client2, "POWER rack1.1 off")
    assert client2.lines[-1] == "POWER rack1.1 -> off"
    assert pdu.pdus["rack1"].readings["1"].on is False
    await pdu.stop()


@pytest.mark.asyncio
async def test_off_impact_local_ref_does_not_count_remote_port_same_name():
    # Removing the local rack1.1 powers down c1; r1 also feeds a rack1.1 but
    # on ANOTHER node, so it is NOT "staying up via" the local outlet (the
    # impact preview must not name it).
    _adapter, pdu, pm = await _started_federated(states={"rack1.1": True})
    pm.ports["r1"].power = ["peerO::rack1.1"]
    pm.ports["r1"]._feed_states = {"peerO::rack1.1": True}
    # The preview reads LIVE local readings: with rack1.2 still on, c1 stays
    # up via that feed. Drop it first so the off of rack1.1 leaves c1 off.
    pdu.pdus["rack1"].readings["2"].on = False
    impact = pdu.compute_off_impact("rack1.1", local_ref=True)
    losing = {e["port"] for e in impact["losing_power"]}
    staying = {e["port"] for e in impact["staying_up"]}
    assert losing == {"c1"}
    assert "r1" not in staying


@pytest.mark.asyncio
async def test_switch_remote_ref_relayed_for_admin_too():
    mux_reply = {"ok": True, "on": True}
    adapter, pdu, pm = await _started_federated(
        states={"rack9.1": False}, mux_reply=mux_reply, feed_port_sessions={"cid-1": 5}
    )
    client = _FakeClient(username="boss")
    client.connected_port = "r1"
    await adapter.process_client_command(client, "POWER peerO::rack9.1 on")
    assert client.lines[-1] == "POWER peerO::rack9.1 -> on"
    await pdu.stop()


@pytest.mark.asyncio
async def test_switch_remote_ref_without_federated_session_refused():
    # No read-write session anchored on a fed port: set_outlet cannot relay,
    # so the typed refusal names the origin. Same result for admin (the
    # session is the binder, not the role).
    adapter, pdu, pm = await _started_federated(states={"rack9.1": True})
    client = _FakeClient()
    client.connected_port = "c1"  # attached, but not to a fed port
    await adapter.process_client_command(client, "POWER peerO::rack9.1 off")
    # The (visible) off-impact preview may precede the refusal, as for local
    # refs; the refusal line ends the exchange.
    assert client.lines[-1].startswith("ERROR:POWER: peerO::rack9.1 is owned by federated node peerO")
    client_admin = _FakeClient(username="boss")
    client_admin.connected_port = "c1"
    await adapter.process_client_command(client_admin, "POWER peerO::rack9.1 off")
    assert client_admin.lines[-1].startswith("ERROR:POWER: peerO::rack9.1 is owned by federated node peerO")
    await pdu.stop()


@pytest.mark.asyncio
async def test_switch_remote_ref_origin_coverage_error_propagates():
    # The origin refuses (fed set wider than the peer's visible claim) and
    # its typed error is surfaced verbatim.
    mux_reply = {
        "ok": False,
        "error": "rack9.1 also feeds consoles not known to the requesting node (c7); switch it on the origin node",
    }
    adapter, pdu, pm = await _started_federated(states={"rack9.1": True}, mux_reply=mux_reply, feed_port_sessions={"cid-1": 9})
    client = _FakeClient()
    client.connected_port = "r1"
    await adapter.process_client_command(client, "POWER peerO::rack9.1 off")
    assert any(
        l.startswith("ERROR:POWER: rack9.1 also feeds consoles not known to the requesting node (c7)") for l in client.lines
    )
    assert pm.ports["r1"]._feed_states == {"peerO::rack9.1": True}  # unchanged
    await pdu.stop()


@pytest.mark.asyncio
async def test_switch_remote_ref_group_blocked_on_peer_refused_before_relay():
    # The peer's own group check refuses first (a visible fed port outside the
    # user's groups) - the relay is never reached.
    mux_reply = {"ok": True, "on": False}
    adapter, pdu, pm = await _started_federated(
        states={"rack9.1": True}, mux_reply=mux_reply, feed_port_sessions={"cid-1": 3}, blocked_feed_port=True
    )
    muxcon = pm.unified_adapters[1]
    client = _FakeClient()
    client.connected_port = "r1"
    await adapter.process_client_command(client, "POWER peerO::rack9.1 off")
    assert any("outside your groups" in l and "r2" in l for l in client.lines)
    assert muxcon.calls == []  # refused before any relay
    await pdu.stop()


@pytest.mark.asyncio
async def test_local_switch_still_works_alongside_remote_ports():
    adapter, pdu, pm = await _started_federated(states={"rack9.1": True})
    client = _FakeClient()
    await adapter.process_client_command(client, "POWER rack1.1 off")
    assert client.lines[-1] == "POWER rack1.1 -> off"
    assert pdu.pdus["rack1"].readings["1"].on is False
    await pdu.stop()


class _FakeReader:
    def __init__(self, script):
        self._q = list(script)

    async def read(self, n):
        if not self._q:
            return b""
        return self._q.pop(0)


class _FakeWriter:
    def __init__(self):
        self.data = b""

    def write(self, d):
        self.data += d

    async def drain(self):
        return


@pytest.mark.asyncio
async def test_power_menu_relayed_toggle_for_remote_feed():
    from openmux.server.adapters.power_command import run_power_menu

    mux_reply = {"ok": True, "on": False}
    adapter, pdu, pm = await _started_federated(
        states={"rack9.1": True, "rack9.2": None}, mux_reply=mux_reply, feed_port_sessions={"cid-1": 2}
    )
    cm = pdu.console_manager  # wired in _started_federated
    muxcon = pm.unified_adapters[1]
    lines = _FakeClient()
    reader = _FakeReader([b"1", b"\r", b"\r"])  # toggle feed 1, then exit
    writer = _FakeWriter()
    await run_power_menu(cm, "r1", reader, writer, lines.send_line, "u1", adapter.auth_manager, "cid-1")
    text = "\n".join(lines.lines)
    # Feed rows render the globally qualified ref from the origin's
    # last-reported (cached) state.
    assert "[on]   peerO::rack9.1" in text
    assert "[unknown] peerO::rack9.2" in text
    # The toggle is relayed (the menu is a console session) and succeeds,
    # anchored on the acting session's own federated stream; the frame
    # carries the origin-local ref.
    assert muxcon.calls and muxcon.calls[0]["port"] == "r1"
    assert muxcon.calls[0]["ref"] == "rack9.1"
    assert muxcon.calls[0]["client_id"] == "cid-1"
    assert "POWER peerO::rack9.1 -> off" in lines.lines
    assert "[EXITING POWER]" in lines.lines
    # The cached state is only updated by the origin's POWER:STATE (not here).
    await pdu.stop()
