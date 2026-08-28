"""Tests for ConsoleManager.force_promote_client / get_rw_holders_display / _resolve_client_ip."""

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from openmux.server.console_manager import ConsoleManager


class FakePort:
    def __init__(self, connected_clients: Optional[List[Dict[str, Any]]] = None):
        self.connected_clients = connected_clients or []


class FakePortManager:
    """Minimal port manager stand-in exposing only what ConsoleManager needs."""

    def __init__(self):
        self.ports: Dict[str, FakePort] = {}

    def get_port(self, name: str):
        return self.ports.get(name)

    async def promote_client(self, port_name: str, client_id: str) -> bool:
        port = self.ports.get(port_name)
        if port is None:
            return False
        for c in port.connected_clients:
            if c["client_id"] == client_id:
                c["mode"] = "read-write"
                return True
        return False

    async def demote_client(self, port_name: str, client_id: str) -> bool:
        port = self.ports.get(port_name)
        if port is None:
            return False
        for c in port.connected_clients:
            if c["client_id"] == client_id:
                c["mode"] = "read-only"
                return True
        return False


class FakeAdapterChannel:
    """Stand-in for an owning adapter, supporting cross-adapter notify/IP lookup."""

    def __init__(self, ip: str = "1.2.3.4", accept: bool = True):
        self.ip = ip
        self.accept = accept
        self.received: List[Dict[str, Any]] = []

    async def send_control_frame_to_client(self, client_id: str, payload: Dict[str, Any]) -> bool:
        self.received.append(payload)
        return self.accept

    def _resolve_client_meta(self, client_id: str) -> Dict[str, Any]:
        return {"type": "fake", "ip": self.ip, "username": "someone"}


@pytest.fixture
def port_manager() -> FakePortManager:
    return FakePortManager()


@pytest.fixture
def cm(port_manager: FakePortManager) -> ConsoleManager:
    return ConsoleManager(port_manager, MagicMock())


def _attach(cm: ConsoleManager, port: FakePort, port_name: str, client_id: str, username: str, mode: str):
    port.connected_clients.append({"client_id": client_id, "username": username, "mode": mode})
    cm.client_port_map[client_id] = port_name


# ---------------------------------------------------------------------------
# force_promote_client


@pytest.mark.asyncio
async def test_force_promote_demotes_other_holder_and_promotes_target(cm, port_manager):
    port = FakePort()
    port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "A", "alice", "read-write")
    _attach(cm, port, "p1", "B", "bob", "read-only")

    ok, undelivered = await cm.force_promote_client("B", "p1")

    assert ok is True
    modes = {c["client_id"]: c["mode"] for c in port.connected_clients}
    assert modes["A"] == "read-only"
    assert modes["B"] == "read-write"
    # No adapter registered for A -> cross-notify fails -> falls back to caller.
    assert undelivered == ["A"]


@pytest.mark.asyncio
async def test_force_promote_cross_notifies_via_client_to_manager(cm, port_manager):
    port = FakePort()
    port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "A", "alice", "read-write")
    _attach(cm, port, "p1", "B", "bob", "read-only")
    other_adapter = FakeAdapterChannel(accept=True)
    cm.client_to_manager["A"] = other_adapter

    ok, undelivered = await cm.force_promote_client("B", "p1")

    assert ok is True
    assert undelivered == []
    # Presence broadcasts (issue #48) also land on this channel now; filter down to
    # the demotion notice this test actually cares about.
    client_mode_frames = [p for p in other_adapter.received if p.get("type") == "client_mode"]
    assert client_mode_frames == [{"type": "client_mode", "ok": False, "mode": "read-only", "reason": "demoted"}]


@pytest.mark.asyncio
async def test_force_promote_demotes_and_notifies_a_federated_holder(cm, port_manager):
    """A locally initiated force-take must reach a federated ("fed:") RW holder too.

    Regression test: `fed:<peer_key>:<stream_id>` pseudo-clients are added
    directly to PortManager by UnifiedMuxConAdapter (never through
    `connect_client_to_port`), so without `register_client_port`/
    `register_client_channel` at stream-open, `demote_client_to_read_only`
    silently no-ops for them (not in `client_port_map`) and the origin's own
    promote_client then denies the local caller because the port still looks
    full - a local force-take against a federated holder simply failed.
    """
    port = FakePort()
    port_manager.ports["p1"] = port
    port.connected_clients.append({"client_id": "fed:peerA:7", "username": "federation:peerA", "mode": "read-write"})
    cm.register_client_port("fed:peerA:7", "p1")
    muxcon_adapter = FakeAdapterChannel(accept=True)
    cm.register_client_channel("fed:peerA:7", muxcon_adapter)
    _attach(cm, port, "p1", "B", "bob", "read-only")

    ok, undelivered = await cm.force_promote_client("B", "p1")

    assert ok is True
    assert undelivered == []
    modes = {c["client_id"]: c["mode"] for c in port.connected_clients}
    assert modes["fed:peerA:7"] == "read-only"
    assert modes["B"] == "read-write"
    client_mode_frames = [p for p in muxcon_adapter.received if p.get("type") == "client_mode"]
    assert client_mode_frames == [{"type": "client_mode", "ok": False, "mode": "read-only", "reason": "demoted"}]

    cm.unregister_client_port("fed:peerA:7")
    cm.unregister_client_channel("fed:peerA:7")
    assert "fed:peerA:7" not in cm.client_port_map
    assert "fed:peerA:7" not in cm.client_to_manager


@pytest.mark.asyncio
async def test_force_promote_no_other_holders_just_promotes(cm, port_manager):
    port = FakePort()
    port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "B", "bob", "read-only")

    ok, undelivered = await cm.force_promote_client("B", "p1")

    assert ok is True
    assert undelivered == []
    assert port.connected_clients[0]["mode"] == "read-write"


@pytest.mark.asyncio
async def test_force_promote_returns_false_when_promote_fails(cm, port_manager):
    port = FakePort()
    port_manager.ports["p1"] = port
    # Client never attached (not in client_port_map) -> promote_client_to_read_write fails.
    ok, undelivered = await cm.force_promote_client("ghost", "p1")
    assert ok is False
    assert undelivered == []


# ---------------------------------------------------------------------------
# get_rw_holders_display / _resolve_client_ip


def test_get_rw_holders_display_unknown_ip_without_adapter(cm, port_manager):
    port = FakePort([{"client_id": "A", "username": "alice", "mode": "read-write"}])
    port_manager.ports["p1"] = port
    assert cm.get_rw_holders_display("p1") == ["alice@unknown"]


def test_get_rw_holders_display_resolves_ip_via_adapter(cm, port_manager):
    port = FakePort([{"client_id": "A", "username": "alice", "mode": "read-write"}])
    port_manager.ports["p1"] = port
    cm.client_to_manager["A"] = FakeAdapterChannel(ip="10.0.0.5")
    assert cm.get_rw_holders_display("p1") == ["alice@10.0.0.5"]


def test_get_rw_holders_display_excludes_read_only_clients(cm, port_manager):
    port = FakePort(
        [
            {"client_id": "A", "username": "alice", "mode": "read-write"},
            {"client_id": "B", "username": "bob", "mode": "read-only"},
        ]
    )
    port_manager.ports["p1"] = port
    assert cm.get_rw_holders_display("p1") == ["alice@unknown"]


def test_get_rw_holders_display_empty_for_unknown_port(cm):
    assert cm.get_rw_holders_display("does-not-exist") == []


# ---------------------------------------------------------------------------
# get_viewers_display


def test_get_viewers_display_excludes_federated_pseudo_client(cm, port_manager):
    """Regression test for the viewers-badge double-count/mis-format bug.

    `fed:<peer_key>:<sid>` pseudo-clients are added directly to
    `connected_clients` by UnifiedMuxConAdapter purely for RW-arbitration/notify
    routing (issue #52) - they must never appear in `get_viewers_display`, since
    the SAME remote viewer is already reported (properly formatted, with a real
    username/ip/server_id) via `remote_viewers` from muxcon's VIEWERS relay.
    """
    port = FakePort(
        [
            {"client_id": "A", "username": "admin", "mode": "read-write"},
            {"client_id": "fed:node:mbp:3", "username": "federation:node:mbp", "mode": "read-only"},
        ]
    )
    port.remote_viewers = [{"server_id": "mbp", "username": "admin", "mode": "read-only", "ip": "127.0.0.1"}]
    port_manager.ports["p1"] = port

    viewers = cm.get_viewers_display("p1")

    assert viewers == [
        {"username": "admin", "mode": "read-write", "client_id": "A", "ip": "unknown"},
        {"server_id": "mbp", "username": "admin", "mode": "read-only", "ip": "127.0.0.1"},
    ]


def test_get_viewers_display_empty_for_unknown_port(cm):
    assert cm.get_viewers_display("does-not-exist") == []


# ---------------------------------------------------------------------------
# broadcast_control_frame_to_port


@pytest.mark.asyncio
async def test_broadcast_control_frame_to_port_delivers_to_all_viewers_of_that_port(cm, port_manager):
    port = FakePort()
    port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "A", "alice", "read-write")
    _attach(cm, port, "p1", "B", "bob", "read-only")
    _attach(cm, port, "other", "C", "carol", "read-write")
    adapter_a = FakeAdapterChannel(accept=True)
    adapter_b = FakeAdapterChannel(accept=True)
    adapter_c = FakeAdapterChannel(accept=True)
    cm.client_to_manager["A"] = adapter_a
    cm.client_to_manager["B"] = adapter_b
    cm.client_to_manager["C"] = adapter_c

    payload = {"type": "action_run", "event": "action_started", "run_id": "r1"}
    delivered = await cm.broadcast_control_frame_to_port("p1", payload)

    assert delivered == 2
    assert adapter_a.received == [payload]
    assert adapter_b.received == [payload]
    assert adapter_c.received == []  # different port, not delivered


@pytest.mark.asyncio
async def test_broadcast_control_frame_to_port_counts_only_successful_deliveries(cm, port_manager):
    port = FakePort()
    port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "A", "alice", "read-write")
    _attach(cm, port, "p1", "B", "bob", "read-only")
    cm.client_to_manager["A"] = FakeAdapterChannel(accept=True)
    cm.client_to_manager["B"] = FakeAdapterChannel(accept=False)

    delivered = await cm.broadcast_control_frame_to_port("p1", {"type": "action_run"})

    assert delivered == 1


@pytest.mark.asyncio
async def test_broadcast_control_frame_to_port_no_viewers_returns_zero(cm, port_manager):
    port_manager.ports["p1"] = FakePort()
    assert await cm.broadcast_control_frame_to_port("p1", {"type": "action_run"}) == 0


# ---------------------------------------------------------------------------
# connect_client_to_port: console-group access control (issue #24)


class TestConnectClientToPortGroupAcl:
    """Integration tests for group-based per-console access control.

    Uses the real `PortManager` + `LoopbackAdapter` + `AuthManager` stack so
    the attributes threaded through the port classes (`read_write_groups`,
    `read_only_groups`) and `UnifiedPortWrapper` are exercised end to end.
    """

    async def _make_manager(self, monkeypatch, port_config: Dict[str, Any], auth_config: Dict[str, Any]):
        from openmux.server import data_logger as dl_mod
        from openmux.server.adapters.loopback import LoopbackAdapter
        from openmux.server.auth_manager import AuthManager
        from openmux.server.port_manager import PortManager

        class _DummyDataLogger:
            def record(self, *args, **kwargs):
                pass

        monkeypatch.setattr(dl_mod.DataLogger, "get", classmethod(lambda cls: _DummyDataLogger()))

        pm = PortManager([])
        adapter = LoopbackAdapter("loop", {"loopback_ports": [port_config]})
        adapter.main_port_manager = pm
        pm.set_unified_adapters([adapter])
        assert await adapter.start() is True

        auth = AuthManager(auth_config)
        return ConsoleManager(pm, auth)

    @pytest.mark.asyncio
    async def test_default_allow_when_no_groups_configured(self, monkeypatch):
        # No read_write_groups/read_only_groups -> today's default-allow parity:
        # a non-loopback-forced port grants read-write while a slot is free.
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "max_read_write_users": 1},
            {"users": [{"username": "alice", "password_hash": "x"}]},
        )
        ok, mode, reason = await cm.connect_client_to_port("c1", "p1", "alice")
        assert (ok, mode, reason) == (True, "read-write", None)

    @pytest.mark.asyncio
    async def test_rw_group_member_gets_read_write(self, monkeypatch):
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "max_read_write_users": 2, "read_write_groups": ["ops"]},
            {"users": [{"username": "alice", "password_hash": "x", "groups": ["ops"]}]},
        )
        ok, mode, reason = await cm.connect_client_to_port("c1", "p1", "alice")
        assert (ok, mode, reason) == (True, "read-write", None)

    @pytest.mark.asyncio
    async def test_ro_group_member_gets_read_only_never_promoted(self, monkeypatch):
        cm = await self._make_manager(
            monkeypatch,
            {
                "name": "p1",
                "max_read_write_users": 5,
                "read_write_groups": ["ops"],
                "read_only_groups": ["viewers"],
            },
            {"users": [{"username": "bob", "password_hash": "x", "groups": ["viewers"]}]},
        )
        ok, mode, reason = await cm.connect_client_to_port("c1", "p1", "bob")
        assert (ok, mode, reason) == (True, "read-only", None)

    @pytest.mark.asyncio
    async def test_user_not_in_any_group_is_denied(self, monkeypatch):
        cm = await self._make_manager(
            monkeypatch,
            {
                "name": "p1",
                "max_read_write_users": 5,
                "read_write_groups": ["ops"],
                "read_only_groups": ["viewers"],
            },
            {"users": [{"username": "eve", "password_hash": "x"}]},
        )
        ok, mode, reason = await cm.connect_client_to_port("c1", "p1", "eve")
        assert (ok, mode, reason) == (False, None, "denied_by_group_acl")

    @pytest.mark.asyncio
    async def test_admin_bypasses_group_acl(self, monkeypatch):
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "max_read_write_users": 5, "read_write_groups": ["ops"]},
            {"users": [{"username": "root", "password_hash": "x", "permissions": "admin"}]},
        )
        ok, mode, reason = await cm.connect_client_to_port("c1", "p1", "root")
        assert (ok, mode, reason) == (True, "read-write", None)

    @pytest.mark.asyncio
    async def test_unknown_user_is_denied_no_permissions(self, monkeypatch):
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "max_read_write_users": 5},
            {"users": [{"username": "alice", "password_hash": "x"}]},
        )
        ok, mode, reason = await cm.connect_client_to_port("c1", "p1", "ghost")
        assert (ok, mode, reason) == (False, None, "no_permissions")

    @pytest.mark.asyncio
    async def test_loopback_without_acl_follows_slot_rules(self, monkeypatch):
        """Loopback ports are not special: a default read-write user gets
        read-write while the slot is free, like any other port."""
        cm = await self._make_manager(
            monkeypatch,
            {"name": "loopback1", "max_read_write_users": 1},
            {"users": [{"username": "alice", "password_hash": "x"}]},
        )
        ok, mode, reason = await cm.connect_client_to_port("c1", "loopback1", "alice")
        assert (ok, mode, reason) == (True, "read-write", None)

    @pytest.mark.asyncio
    async def test_loopback_with_explicit_acl_is_not_auto_promoted(self, monkeypatch):
        cm = await self._make_manager(
            monkeypatch,
            {
                "name": "loopback1",
                "max_read_write_users": 1,
                "read_write_groups": ["ops"],
                "read_only_groups": ["viewers"],
            },
            {"users": [{"username": "bob", "password_hash": "x", "groups": ["viewers"]}]},
        )
        ok, mode, reason = await cm.connect_client_to_port("c1", "loopback1", "bob")
        assert (ok, mode, reason) == (True, "read-only", None)

    # ------------------------------------------------------------------
    # Issue #58 Part 1: ladder fixes beyond the original issue #24 ACL block
    #
    # NOTE: every port in this class lives on a LoopbackAdapter, so the wrapper
    # marks them `loopback=True` and the ladder's loopback auto-promotion
    # exception applies. The generic (non-loopback) cases live in
    # TestResolveAccessModeLadder below, which drives `_resolve_access_mode`
    # directly with non-loopback stand-in ports.

    @pytest.mark.asyncio
    async def test_loopback_port_treats_read_only_global_as_read_only(self, monkeypatch):
        """The old loopback auto-promotion is gone (#58): a global read-only
        user stays read-only on a no-list loopback port, even with a free slot."""
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "max_read_write_users": 1},
            {"users": [{"username": "auditor", "password_hash": "x", "permissions": "read-only"}]},
        )
        ok, mode, reason = await cm.connect_client_to_port("c1", "p1", "auditor")
        assert (ok, mode, reason) == (True, "read-only", None)

    @pytest.mark.asyncio
    async def test_global_read_write_user_demotes_when_port_full(self, monkeypatch):
        """Demote, never reject: a write-entitled user on a full port gets read-only."""
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "max_read_write_users": 1},
            {"users": [{"username": "alice", "password_hash": "x"}, {"username": "bob", "password_hash": "x"}]},
        )
        ok1, mode1, _ = await cm.connect_client_to_port("c1", "p1", "alice")
        assert (ok1, mode1) == (True, "read-write")
        ok2, mode2, reason2 = await cm.connect_client_to_port("c2", "p1", "bob")
        assert (ok2, mode2, reason2) == (True, "read-only", None)

    @pytest.mark.asyncio
    async def test_rw_group_member_respects_slot_limit(self, monkeypatch):
        """Bug 2 fix: group grants are subject to max_read_write_users."""
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "max_read_write_users": 1, "read_write_groups": ["ops"]},
            {
                "users": [
                    {"username": "a", "password_hash": "x", "groups": ["ops"]},
                    {"username": "b", "password_hash": "x", "groups": ["ops"]},
                ]
            },
        )
        ok1, mode1, _ = await cm.connect_client_to_port("c1", "p1", "a")
        assert (ok1, mode1) == (True, "read-write")
        ok2, mode2, reason2 = await cm.connect_client_to_port("c2", "p1", "b")
        assert (ok2, mode2, reason2) == (True, "read-only", None)

    @pytest.mark.asyncio
    async def test_rw_group_grant_beats_read_only_global_permission(self, monkeypatch):
        """Explicit grants beat the global permission, both directions."""
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "max_read_write_users": 1, "read_write_groups": ["ops"]},
            {"users": [{"username": "auditor", "password_hash": "x", "permissions": "read-only", "groups": ["ops"]}]},
        )
        ok, mode, reason = await cm.connect_client_to_port("c1", "p1", "auditor")
        assert (ok, mode, reason) == (True, "read-write", None)

    @pytest.mark.asyncio
    async def test_ro_group_grant_beats_read_write_global_permission(self, monkeypatch):
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "max_read_write_users": 5, "read_only_groups": ["viewers"]},
            {"users": [{"username": "bob", "password_hash": "x", "groups": ["viewers"]}]},
        )
        ok, mode, reason = await cm.connect_client_to_port("c1", "p1", "bob")
        assert (ok, mode, reason) == (True, "read-only", None)

    @pytest.mark.asyncio
    async def test_loopback_port_demotes_second_write_user_when_full(self, monkeypatch):
        """A full no-list loopback port demotes the next write-entitled user
        to read-only, never rejected or overflowed."""
        cm = await self._make_manager(
            monkeypatch,
            {"name": "loopback1", "max_read_write_users": 1},
            {"users": [{"username": "a", "password_hash": "x"}, {"username": "b", "password_hash": "x"}]},
        )
        ok1, mode1, _ = await cm.connect_client_to_port("c1", "loopback1", "a")
        assert (ok1, mode1) == (True, "read-write")
        ok2, mode2, reason2 = await cm.connect_client_to_port("c2", "loopback1", "b")
        assert (ok2, mode2, reason2) == (True, "read-only", None)

    @pytest.mark.asyncio
    async def test_acl_port_denies_unlisted_user_even_with_read_write_global(self, monkeypatch):
        """The closed boundary: a list-bearing port never falls back to the slot branch."""
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "max_read_write_users": 5, "read_write_groups": ["ops"]},
            {"users": [{"username": "stranger", "password_hash": "x", "permissions": "read-write"}]},
        )
        ok, mode, reason = await cm.connect_client_to_port("c1", "p1", "stranger")
        assert (ok, mode, reason) == (False, None, "denied_by_group_acl")

    # ---------------- access_default integration (issue #58, part 2) ----------------

    @pytest.mark.asyncio
    async def test_access_default_deny_denies_unlisted_user_end_to_end(self, monkeypatch):
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "max_read_write_users": 1},
            {"users": [{"username": "alice", "password_hash": "x"}]},
        )
        cm.security_policy = _StubPolicy("deny")
        ok, mode, reason = await cm.connect_client_to_port("c1", "p1", "alice")
        assert (ok, mode, reason) == (False, None, "denied_by_access_default")

    @pytest.mark.asyncio
    async def test_access_default_deny_admin_still_connects(self, monkeypatch):
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "max_read_write_users": 1},
            {"users": [{"username": "root", "password_hash": "x", "permissions": "admin"}]},
        )
        cm.security_policy = _StubPolicy("deny")
        ok, mode, reason = await cm.connect_client_to_port("c1", "p1", "root")
        assert (ok, mode, reason) == (True, "read-write", None)

    @pytest.mark.asyncio
    async def test_access_default_deny_group_grant_still_connects(self, monkeypatch):
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "max_read_write_users": 1, "read_write_groups": ["ops"]},
            {"users": [{"username": "alice", "password_hash": "x", "groups": ["ops"]}]},
        )
        cm.security_policy = _StubPolicy("deny")
        ok, mode, reason = await cm.connect_client_to_port("c1", "p1", "alice")
        assert (ok, mode, reason) == (True, "read-write", None)

    @pytest.mark.asyncio
    async def test_access_default_allow_is_default_end_to_end(self, monkeypatch):
        """Without a policy (and with an explicit allow stub) no-list ports are open."""
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "max_read_write_users": 1},
            {"users": [{"username": "alice", "password_hash": "x"}]},
        )
        ok0, mode0, _ = await cm.connect_client_to_port("c1", "p1", "alice")
        assert (ok0, mode0) == (True, "read-write")
        cm.security_policy = _StubPolicy("allow")  # explicit stub: same expectation
        await cm.disconnect_client_from_port("c1", "p1")
        ok1, mode1, _ = await cm.connect_client_to_port("c2", "p1", "alice")
        assert (ok1, mode1) == (True, "read-write")


# ---------------------------------------------------------------------------
# _resolve_access_mode: issue #58 ladder unit matrix (non-loopback ports)


class _StubPolicy:
    """Minimal stand-in for SecurityPolicy exposing only get_access_default."""

    def __init__(self, access_default: str = "allow"):
        self._access_default = access_default

    def get_access_default(self) -> str:
        return self._access_default


class _LadderPort:
    """Stand-in port with explicit ladder inputs (no loopback, no sockets)."""

    def __init__(
        self,
        max_rw: int = 1,
        rw_clients: int = 0,
        ro_groups: tuple = (),
        rw_groups: tuple = (),
        loopback: bool = False,
        adapter_type: str = "fake",
    ):
        self.max_read_write_users = max_rw
        self.read_write_groups = list(rw_groups)
        self.read_only_groups = list(ro_groups)
        self.loopback = loopback
        self.adapter_type = adapter_type
        self.connected_clients = [
            {"client_id": f"rw{i}", "username": f"rw{i}", "mode": "read-write"} for i in range(rw_clients)
        ]


class TestResolveAccessModeLadder:
    """Unit matrix for the issue #58 access ladder, driven directly.

    ``_resolve_access_mode(port, port_name, permissions, username)`` returns
    ``(mode, deny_reason)``. ``permissions`` is the user's global permission
    as resolved by AuthManager; the unknown-identity case (None) is rejected
    by the caller before the ladder runs and is covered by the integration
    tests (``no_permissions``).
    """

    def _cm(self, auth_config: Dict[str, Any]) -> ConsoleManager:
        from openmux.server.auth_manager import AuthManager

        return ConsoleManager(FakePortManager(), AuthManager(auth_config))

    def test_admin_gets_read_write_everywhere(self):
        cm = self._cm({"users": [{"username": "root", "password_hash": "x", "permissions": "admin"}]})
        port = _LadderPort(rw_clients=1, rw_groups=("ops",))  # full + ACL'd
        assert cm._resolve_access_mode(port, "p1", "admin", "root") == ("read-write", None)

    def test_global_read_write_demotes_on_full_port(self):
        cm = self._cm({"users": [{"username": "alice", "password_hash": "x"}]})
        assert cm._resolve_access_mode(_LadderPort(max_rw=1), "p1", "read-write", "alice") == ("read-write", None)
        assert cm._resolve_access_mode(_LadderPort(max_rw=1, rw_clients=1), "p1", "read-write", "alice") == (
            "read-only",
            None,
        )

    def test_global_read_only_never_promoted(self):
        """Bug 1 fix: the slot branch consults the global permission."""
        cm = self._cm({"users": [{"username": "auditor", "password_hash": "x", "permissions": "read-only"}]})
        assert cm._resolve_access_mode(_LadderPort(max_rw=1), "p1", "read-only", "auditor") == ("read-only", None)
        assert cm._resolve_access_mode(_LadderPort(max_rw=1, rw_clients=1), "p1", "read-only", "auditor") == (
            "read-only",
            None,
        )

    def test_rw_group_member_gets_read_write_on_free_slot(self):
        cm = self._cm({"users": [{"username": "alice", "password_hash": "x", "groups": ["ops"]}]})
        port = _LadderPort(max_rw=1, rw_groups=("ops",))
        assert cm._resolve_access_mode(port, "p1", "read-write", "alice") == ("read-write", None)

    def test_rw_group_member_demotes_when_port_full(self):
        """Bug 2 fix: group grants respect the slot cap at resolution time."""
        cm = self._cm({"users": [{"username": "alice", "password_hash": "x", "groups": ["ops"]}]})
        port = _LadderPort(max_rw=1, rw_clients=1, rw_groups=("ops",))
        assert cm._resolve_access_mode(port, "p1", "read-write", "alice") == ("read-only", None)

    def test_rw_group_grant_beats_read_only_global(self):
        """Explicit grants beat the global permission, both directions."""
        cm = self._cm(
            {"users": [{"username": "auditor", "password_hash": "x", "permissions": "read-only", "groups": ["ops"]}]}
        )
        port = _LadderPort(max_rw=1, rw_groups=("ops",))
        assert cm._resolve_access_mode(port, "p1", "read-only", "auditor") == ("read-write", None)

    def test_ro_group_grant_beats_read_write_global(self):
        cm = self._cm({"users": [{"username": "bob", "password_hash": "x", "groups": ["viewers"]}]})
        port = _LadderPort(max_rw=5, ro_groups=("viewers",))
        assert cm._resolve_access_mode(port, "p1", "read-write", "bob") == ("read-only", None)

    def test_group_acl_is_closed_boundary_even_with_read_write_global(self):
        cm = self._cm({"users": [{"username": "stranger", "password_hash": "x", "permissions": "read-write"}]})
        port = _LadderPort(max_rw=1, rw_groups=("ops",), ro_groups=("viewers",))
        assert cm._resolve_access_mode(port, "p1", "read-write", "stranger") == (None, "denied_by_group_acl")

    def test_loopback_ports_get_no_special_treatment(self):
        """The old loopback auto-promotion is gone (#58): no-list loopback
        ports follow the same ladder and slot rules as any port."""
        cm = self._cm({"users": [{"username": "auditor", "password_hash": "x", "permissions": "read-only"}]})
        # Free slot does not promote a global read-only user anymore.
        port = _LadderPort(max_rw=1, loopback=True, adapter_type="loopback")
        assert cm._resolve_access_mode(port, "p1", "read-only", "auditor") == ("read-only", None)
        # A full port demotes a global read-write user, loopback or not.
        port_full = _LadderPort(max_rw=1, rw_clients=1, loopback=True, adapter_type="loopback")
        assert cm._resolve_access_mode(port_full, "p1", "read-write", "alice") == ("read-only", None)

    # ---------------- access_default (issue #58, part 2) ----------------

    def test_access_default_unset_policy_defaults_to_allow(self):
        """Unwired console manager (no policy) keeps today's allow behavior."""
        cm = self._cm({"users": [{"username": "alice", "password_hash": "x"}]})
        assert cm.security_policy is None
        assert cm._resolve_access_mode(_LadderPort(), "p1", "read-write", "alice") == ("read-write", None)

    def test_access_default_deny_denies_no_list_port(self):
        cm = self._cm({"users": [{"username": "alice", "password_hash": "x"}]})
        cm.security_policy = _StubPolicy("deny")
        assert cm._resolve_access_mode(_LadderPort(), "p1", "read-write", "alice") == (None, "denied_by_access_default")

    def test_access_default_deny_still_allows_admin(self):
        cm = self._cm({"users": [{"username": "root", "password_hash": "x", "permissions": "admin"}]})
        cm.security_policy = _StubPolicy("deny")
        assert cm._resolve_access_mode(_LadderPort(), "p1", "admin", "root") == ("read-write", None)

    def test_access_default_deny_group_grant_unaffected(self):
        """deny only acts on no-list ports: list-bearing ports keep their grants."""
        cm = self._cm({"users": [{"username": "alice", "password_hash": "x", "groups": ["ops"]}]})
        cm.security_policy = _StubPolicy("deny")
        port = _LadderPort(max_rw=1, rw_groups=("ops",))
        assert cm._resolve_access_mode(port, "p1", "read-write", "alice") == ("read-write", None)

    def test_access_default_deny_unlisted_on_acl_port_keeps_group_acl_reason(self):
        cm = self._cm({"users": [{"username": "stranger", "password_hash": "x", "permissions": "read-write"}]})
        cm.security_policy = _StubPolicy("deny")
        port = _LadderPort(rw_groups=("ops",))
        assert cm._resolve_access_mode(port, "p1", "read-write", "stranger") == (None, "denied_by_group_acl")

    def test_loopback_with_groups_is_not_auto_promoted(self):
        cm = self._cm({"users": [{"username": "bob", "password_hash": "x", "groups": ["viewers"]}]})
        port = _LadderPort(loopback=True, adapter_type="loopback", ro_groups=("viewers",))
        assert cm._resolve_access_mode(port, "p1", "read-write", "bob") == ("read-only", None)
