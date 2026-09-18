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
