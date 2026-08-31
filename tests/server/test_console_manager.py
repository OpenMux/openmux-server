"""Tests for ConsoleManager.take_write_slot / get_rw_holders_display / _resolve_client_ip."""

import asyncio
import itertools
import logging
from typing import Any, Dict, List, Optional

import pytest

from openmux.server.access_control import holder_id_short
from openmux.server.console_manager import ConsoleManager


class FakePort:
    def __init__(self, connected_clients: Optional[List[Dict[str, Any]]] = None, mode: str = "one"):
        self.connected_clients = connected_clients or []
        self.max_read_write_users = mode
        self.read_write_groups = []
        self.read_only_groups = []

    async def demote_client(self, client_id: str) -> bool:
        # Idempotent stand-in for the unified port wrapper: ConsoleManager only
        # falls back to this when the PortManager-level demote already failed.
        self._set_mode(client_id, "read-only")
        return True

    async def promote_client(self, client_id: str) -> bool:
        self._set_mode(client_id, "read-write")
        return True

    def _set_mode(self, client_id: str, mode: str) -> bool:
        for c in self.connected_clients:
            if c.get("client_id") == client_id:
                c["mode"] = mode
                return True
        return False


class FakeAuthManager:
    """Minimal auth stand-in: per-user global permission + groups."""

    def __init__(self):
        self.permissions: Dict[str, Optional[str]] = {"alice": "read-write", "bob": "read-write", "root": "admin"}
        self.groups: Dict[str, set] = {}

    def get_user_permissions(self, username: str) -> Optional[str]:
        return self.permissions.get(username, "read-write")

    def get_user_groups(self, username: str) -> set:
        return set(self.groups.get(username, ()))


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
        try:
            for c in port.connected_clients:
                if c["client_id"] == client_id:
                    c["mode"] = "read-write"
                    return True
        except Exception:
            return False  # a broken wrapper surfaces as a failed promote
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
    return ConsoleManager(port_manager, FakeAuthManager())


_attach_seq = itertools.count(1)


def _attach(cm: ConsoleManager, port: FakePort, port_name: str, client_id: str, username: str, mode: str):
    port.connected_clients.append(
        {"client_id": client_id, "username": username, "mode": mode, "connected_at": float(next(_attach_seq))}
    )
    cm.client_port_map[client_id] = port_name


# ---------------------------------------------------------------------------
# take_write_slot (issue #59 Part 2)


@pytest.mark.asyncio
async def test_take_demotes_other_holder_and_promotes_target(cm, port_manager):
    port = FakePort()
    port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "A", "alice", "read-write")
    _attach(cm, port, "p1", "B", "bob", "read-only")

    ok, reason = await cm.take_write_slot("B", "p1")

    assert (ok, reason) == (True, "ok")
    modes = {c["client_id"]: c["mode"] for c in port.connected_clients}
    assert modes["A"] == "read-only"
    assert modes["B"] == "read-write"


@pytest.mark.asyncio
async def test_take_cross_notifies_via_client_to_manager(cm, port_manager):
    port = FakePort()
    port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "A", "alice", "read-write")
    _attach(cm, port, "p1", "B", "bob", "read-only")
    other_adapter = FakeAdapterChannel(accept=True)
    cm.client_to_manager["A"] = other_adapter

    ok, reason = await cm.take_write_slot("B", "p1")

    assert (ok, reason) == (True, "ok")
    # Presence broadcasts (issue #48) also land on this channel now; filter down to
    # the demotion notice this test actually cares about. The victim gets a
    # single client_mode frame: reason "demoted" (rendered as "read-write was
    # taken") plus "taken_by" naming the taker.
    demotion_frames = [p for p in other_adapter.received if p.get("type") == "client_mode" and p.get("reason")]
    assert demotion_frames == [
        {"type": "client_mode", "ok": False, "mode": "read-only", "reason": "demoted", "taken_by": "bob"}
    ]


@pytest.mark.asyncio
async def test_take_demotes_and_notifies_a_federated_holder(cm, port_manager):
    """A locally initiated takeover must reach a federated ("fed:") RW holder too.

    Regression test: `fed:<peer_key>:<stream_id>` pseudo-clients are added
    directly to PortManager by UnifiedMuxConAdapter (never through
    `connect_client_to_port`), so without `register_client_port`/
    `register_client_channel` at stream-open, `demote_client_to_read_only`
    silently no-ops for them (not in `client_port_map`) and the origin's own
    promote_client then denies the local caller because the port still looks
    full - a local takeover against a federated holder simply failed.
    """
    port = FakePort()
    port_manager.ports["p1"] = port
    port.connected_clients.append(
        {"client_id": "fed:peerA:7", "username": "federation:peerA", "mode": "read-write", "connected_at": 1.0}
    )
    cm.register_client_port("fed:peerA:7", "p1")
    muxcon_adapter = FakeAdapterChannel(accept=True)
    cm.register_client_channel("fed:peerA:7", muxcon_adapter)
    _attach(cm, port, "p1", "B", "bob", "read-only")

    ok, reason = await cm.take_write_slot("B", "p1")

    assert (ok, reason) == (True, "ok")
    modes = {c["client_id"]: c["mode"] for c in port.connected_clients}
    assert modes["fed:peerA:7"] == "read-only"
    assert modes["B"] == "read-write"
    demotion_frames = [p for p in muxcon_adapter.received if p.get("type") == "client_mode" and p.get("reason")]
    assert demotion_frames == [
        {"type": "client_mode", "ok": False, "mode": "read-only", "reason": "demoted", "taken_by": "bob"}
    ]

    cm.unregister_client_port("fed:peerA:7")
    cm.unregister_client_channel("fed:peerA:7")
    assert "fed:peerA:7" not in cm.client_port_map
    assert "fed:peerA:7" not in cm.client_to_manager


@pytest.mark.asyncio
async def test_take_empty_slot_grants(cm, port_manager):
    """Nobody holds the slot: a no-target take takes the EMPTY slot.

    Legacy force behavior, restored: the taker is promoted directly, no
    demotion happens (there is no one to demote) and no notice is sent.
    """
    port = FakePort()
    port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "B", "bob", "read-only")

    ok, reason = await cm.take_write_slot("B", "p1")

    assert (ok, reason) == (True, "ok")
    assert port.connected_clients[0]["mode"] == "read-write"


@pytest.mark.asyncio
async def test_take_empty_slot_already_rw_when_sole_holder(cm, port_manager):
    """The taker is already the sole RW holder: the no-target take is a no-op
    ``already_rw`` (matching multiple-mode), with nothing demoted or sent."""
    port = FakePort()
    port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "A", "alice", "read-write")

    ok, reason = await cm.take_write_slot("A", "p1")

    assert (ok, reason) == (True, "already_rw")
    assert port.connected_clients[0]["mode"] == "read-write"


@pytest.mark.asyncio
async def test_take_empty_slot_named_target_refuses(cm, port_manager):
    """A named target that matches no holder is refused even on an empty
    slot: the named victim does not (or no longer) exist, so nothing
    changes."""
    port = FakePort()
    port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "B", "bob", "read-only")

    ok, reason = await cm.take_write_slot("B", "p1", target="ghost")

    assert (ok, reason) == (False, "no_holder")
    assert port.connected_clients[0]["mode"] == "read-only"


@pytest.mark.asyncio
async def test_take_empty_slot_audits(cm, port_manager, monkeypatch):
    """An empty-slot grant writes the same ``write_slot_takeover`` event as a
    demoting takeover (victim empty) plus a distinct INFO line."""
    port = FakePort()
    port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "B", "bob", "read-only")

    logs: List[str] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord):
            logs.append(record.getMessage())

    handler = _CaptureHandler()
    cm.logger.addHandler(handler)
    old_level = cm.logger.level
    cm.logger.setLevel(logging.INFO)  # tests inherit the WARNING root level by default

    from openmux.server import data_logger as dl_mod

    recorded: List[Dict[str, Any]] = []

    class _RecLogger:
        def record_meta(self, **kwargs):
            recorded.append(kwargs)

    monkeypatch.setattr(dl_mod.DataLogger, "get", classmethod(lambda cls: _RecLogger()))

    ok, reason = await cm.take_write_slot("B", "p1")
    cm.logger.removeHandler(handler)
    cm.logger.setLevel(old_level)

    assert (ok, reason) == (True, "ok")
    assert any("WRITE-SLOT TAKEN (empty slot)" in m and "port=p1" in m for m in logs)
    events = [r for r in recorded if r.get("event") == "write_slot_takeover"]
    assert len(events) == 1
    assert events[0]["meta"]["victim"] is None
    assert events[0]["meta"]["taker"] == "B"


@pytest.mark.asyncio
async def test_take_not_attached_refuses(cm, port_manager):
    port = FakePort()
    port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "A", "alice", "read-write")

    ok, reason = await cm.take_write_slot("ghost", "p1")

    assert (ok, reason) == (False, "not_attached")
    assert port.connected_clients[0]["mode"] == "read-write"


# ---------------------------------------------------------------------------
# take_write_slot: entitlement denials (issue #59 Part 2, closes SEC-07)


def _deny_cm(mode: str = "one") -> ConsoleManager:
    """A console manager that denies read-write for every non-admin user."""
    cm = ConsoleManager(FakePortManager(), FakeAuthManager())
    cm.auth_manager.permissions = {"alice": "read-only", "bob": "read-only"}
    return cm


def _deny_setup(port: FakePort) -> None:
    port.read_write_groups = ["ops"]
    port.read_only_groups = ["viewers"]


@pytest.mark.asyncio
async def test_take_read_only_global_user_denied():
    """A read-only global permission can never take, even though it is attached."""
    cm = _deny_cm()
    port = FakePort()
    cm.port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "A", "alice", "read-write")
    _attach(cm, port, "p1", "B", "bob", "read-only")

    ok, reason = await cm.take_write_slot("B", "p1")

    assert (ok, reason) == (False, "not_entitled")
    modes = {c["client_id"]: c["mode"] for c in port.connected_clients}
    assert modes["A"] == "read-write"
    assert modes["B"] == "read-only"


@pytest.mark.asyncio
async def test_take_ro_group_user_denied_even_over_entitled_holder():
    cm = _deny_cm()
    port = FakePort()
    _deny_setup(port)
    cm.port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "A", "alice", "read-write")
    _attach(cm, port, "p1", "B", "bob", "read-only")
    cm.auth_manager.groups["bob"] = {"viewers"}

    ok, reason = await cm.take_write_slot("B", "p1")

    assert (ok, reason) == (False, "not_entitled")
    assert {c["client_id"]: c["mode"] for c in port.connected_clients}["A"] == "read-write"


@pytest.mark.asyncio
async def test_take_unlisted_user_denied_by_group_acl():
    """Group lists are a closed boundary: an unlisted user is denied to take."""
    cm = _deny_cm()
    port = FakePort()
    _deny_setup(port)
    cm.port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "A", "alice", "read-write")
    _attach(cm, port, "p1", "B", "bob", "read-only")

    ok, reason = await cm.take_write_slot("B", "p1")

    assert (ok, reason) == (False, "denied_by_group_acl")


@pytest.mark.asyncio
async def test_take_denied_by_server_access_default_deny():
    pm = FakePortManager()
    cm = ConsoleManager(pm, FakeAuthManager())
    port = FakePort()
    pm.ports["p1"] = port
    _attach(cm, port, "p1", "A", "alice", "read-write")
    _attach(cm, port, "p1", "B", "bob", "read-only")

    class _DenyPolicy:
        def get_access_default(self):
            return "deny"

    cm.security_policy = _DenyPolicy()

    ok, reason = await cm.take_write_slot("B", "p1")

    assert (ok, reason) == (False, "denied_by_access_default")


@pytest.mark.asyncio
async def test_take_admin_allowed_to_take():
    """Admin bypasses access control and may take an entitled holder's slot."""
    pm = FakePortManager()
    cm = ConsoleManager(pm, FakeAuthManager())
    port = FakePort()
    pm.ports["p1"] = port
    _attach(cm, port, "p1", "A", "alice", "read-write")
    _attach(cm, port, "p1", "R", "root", "read-only")

    ok, reason = await cm.take_write_slot("R", "p1")

    assert (ok, reason) == (True, "ok")
    modes = {c["client_id"]: c["mode"] for c in port.connected_clients}
    assert modes["R"] == "read-write"
    assert modes["A"] == "read-only"


# ---------------------------------------------------------------------------
# take_write_slot: write-capacity mode gates


def _mode_cm(mode: str):
    pm = FakePortManager()
    cm = ConsoleManager(pm, FakeAuthManager())
    port = FakePort(mode=mode)
    pm.ports["p1"] = port
    return pm, cm, port


@pytest.mark.asyncio
async def test_take_multiple_mode_already_rw_when_holding():
    pm, cm, port = _mode_cm("multiple")
    _attach(cm, port, "p1", "B", "bob", "read-write")
    ok, reason = await cm.take_write_slot("B", "p1")
    assert (ok, reason) == (True, "already_rw")


@pytest.mark.asyncio
async def test_take_none_mode_no_holder_to_take():
    pm, cm, port = _mode_cm("none")
    _attach(cm, port, "p1", "A", "alice", "read-only")
    _attach(cm, port, "p1", "B", "bob", "read-only")
    ok, reason = await cm.take_write_slot("B", "p1")
    assert (ok, reason) == (False, "no_holder")
    assert all(c["mode"] == "read-only" for c in port.connected_clients)


# ---------------------------------------------------------------------------
# take_write_slot: targeted selection


def _three_holder(cm, pm: FakePortManager, port: FakePort):
    _attach(cm, port, "p1", "A", "alice", "read-write")
    _attach(cm, port, "p1", "M", "moe", "read-write")
    _attach(cm, port, "p1", "B", "bob", "read-only")


@pytest.mark.asyncio
async def test_take_target_specific_holder(cm, port_manager):
    # Targeted take (issue #61): the success reason names the demoted
    # holder so the taker's console can show "taken from <holder>".
    port = FakePort()
    port_manager.ports["p1"] = port
    _three_holder(cm, port_manager, port)

    ok, reason = await cm.take_write_slot("B", "p1", target="A")

    assert (ok, reason) == (True, "takeover from alice [A]")
    modes = {c["client_id"]: c["mode"] for c in port.connected_clients}
    assert modes == {"A": "read-only", "M": "read-write", "B": "read-write"}


@pytest.mark.asyncio
async def test_take_target_reason_shortens_long_holder_id(cm, port_manager):
    port = FakePort()
    port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "holder-abcdefgh-123456", "carol", "read-write")
    _attach(cm, port, "p1", "B", "bob", "read-only")

    ok, reason = await cm.take_write_slot("B", "p1", target="holder-abcdefgh-123456")

    assert (ok, reason) == (True, f"takeover from carol [{holder_id_short('holder-abcdefgh-123456')}]")


@pytest.mark.asyncio
async def test_take_target_invalid_refuses(cm, port_manager):
    port = FakePort()
    port_manager.ports["p1"] = port
    _three_holder(cm, port_manager, port)

    ok, reason = await cm.take_write_slot("B", "p1", target="B")  # cannot take own seat

    assert (ok, reason) == (False, "invalid_target")
    ok, reason = await cm.take_write_slot("B", "p1", target="ghost")  # never a holder
    assert (ok, reason) == (False, "invalid_target")


@pytest.mark.asyncio
async def test_take_without_target_picks_most_recent_holder(cm, port_manager):
    # No-target takes keep the plain "ok" reason (no victim named).
    port = FakePort()
    port_manager.ports["p1"] = port
    _three_holder(cm, port_manager, port)

    ok, reason = await cm.take_write_slot("B", "p1")

    assert (ok, reason) == (True, "ok")
    modes = {c["client_id"]: c["mode"] for c in port.connected_clients}
    assert modes == {"A": "read-write", "M": "read-only", "B": "read-write"}


# ---------------------------------------------------------------------------
# take_write_slot: slot-count invariance + victim restore


def _make_broken_cm():
    """A cm/pm/port triple where the test may swap out ``promote_client``
    to force the taker's promote to fail (victim-restore path)."""
    pm = FakePortManager()
    cm = ConsoleManager(pm, FakeAuthManager())
    port = FakePort()
    pm.ports["p1"] = port
    return pm, cm, port


@pytest.mark.asyncio
async def test_take_restores_victim_when_taker_promote_fails():
    """Transfer-not-creation: if the taker cannot be promoted, the victim is
    restored so the port is never left with zero writers."""
    pm, cm, port = _make_broken_cm()
    _attach(cm, port, "p1", "A", "alice", "read-write")
    _attach(cm, port, "p1", "B", "bob", "read-only")
    # The taker's own promote is the single point of failure; the victim's
    # restore promote must still succeed so the writer is not lost.
    real_promote = pm.promote_client

    async def flaky_promote(port_name, client_id):
        if client_id == "B":
            return False
        return await real_promote(port_name, client_id)

    pm.promote_client = flaky_promote
    ok, reason = await cm.take_write_slot("B", "p1")

    assert (ok, reason) == (False, "promote_failed")
    modes = {c["client_id"]: c["mode"] for c in port.connected_clients}
    # Alice was demoted, then restored to read-write because bob's promote failed.
    assert modes["A"] == "read-write"
    assert modes["B"] == "read-only"


@pytest.mark.asyncio
async def test_take_keeps_slot_count_invariant():
    """A take never creates a second writer: exactly one read-write holder
    exists both before and after."""
    pm, cm, port = _make_broken_cm()
    _attach(cm, port, "p1", "A", "alice", "read-write")
    _attach(cm, port, "p1", "B", "bob", "read-only")

    assert sum(1 for c in port.connected_clients if c["mode"] == "read-write") == 1
    ok, reason = await cm.take_write_slot("B", "p1")

    assert (ok, reason) == (True, "ok")
    assert sum(1 for c in port.connected_clients if c["mode"] == "read-write") == 1


# ---------------------------------------------------------------------------
# take_write_slot: audit line + DataLogger event


class _AuditPortManager(FakePortManager):
    pass


@pytest.mark.asyncio
async def test_take_writes_audit_line_and_data_logger_event(cm, port_manager, monkeypatch):
    port = FakePort()
    port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "A", "alice", "read-write")
    _attach(cm, port, "p1", "B", "bob", "read-only")

    logs: List[str] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord):
            logs.append(record.getMessage())

    handler = _CaptureHandler()
    cm.logger.addHandler(handler)
    old_level = cm.logger.level
    cm.logger.setLevel(logging.INFO)  # tests inherit the WARNING root level by default

    from openmux.server import data_logger as dl_mod

    recorded: List[Dict[str, Any]] = []

    class _RecLogger:
        def record_meta(self, **kwargs):
            recorded.append(kwargs)

    monkeypatch.setattr(dl_mod.DataLogger, "get", classmethod(lambda cls: _RecLogger()))

    ok, reason = await cm.take_write_slot("B", "p1")
    cm.logger.removeHandler(handler)
    cm.logger.setLevel(old_level)

    assert (ok, reason) == (True, "ok")
    assert any("WRITE-SLOT TAKEOVER" in m and "port=p1" in m for m in logs)
    assert any(r.get("event") == "write_slot_takeover" for r in recorded)


@pytest.mark.asyncio
async def test_two_takers_race_empty_slot_leaves_exactly_one_writer(cm, port_manager):
    """Two concurrent no-target takes on an EMPTY slot: the port always ends
    with at most one read-write holder. One path grabs the empty slot
    directly (``ok``); the other path's promote is refused by capacity
    (``promote_failed``) OR both grab in sequence and the earlier taker is
    demoted by the later take (it still got ``ok``; the winner gets the
    slot). Only the final state and the frame count are pinned."""
    port = FakePort()
    port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "B", "bob", "read-only")
    _attach(cm, port, "p1", "C", "carol", "read-only")
    other_receiver = FakeAdapterChannel(accept=True)
    cm.client_to_manager["B"] = other_receiver  # capture B's demotion notice, if any
    # Force maximum interleaving: yield at every promote/demote step. The
    # yielding promote also models the REAL PortManager's one-port capacity
    # check (a 2nd concurrent promote is refused), which the plain fake does
    # not carry.
    real_promote = port_manager.promote_client
    real_demote = port_manager.demote_client

    async def yielding_promote(port_name, client_id):
        await asyncio.sleep(0)
        port_obj = port_manager.ports[port_name]
        already = any(c["client_id"] == client_id and c.get("mode") == "read-write" for c in port_obj.connected_clients)
        if not already:
            current_rw = sum(1 for c in port_obj.connected_clients if c.get("mode") == "read-write")
            if current_rw >= 1:
                return False
        return await real_promote(port_name, client_id)

    async def yielding_demote(port_name, client_id):
        await asyncio.sleep(0)
        return await real_demote(port_name, client_id)

    port_manager.promote_client = yielding_promote
    port_manager.demote_client = yielding_demote

    results = await asyncio.gather(cm.take_write_slot("B", "p1"), cm.take_write_slot("C", "p1"))

    rw = [c["client_id"] for c in port.connected_clients if c["mode"] == "read-write"]
    assert len(rw) == 1  # the invariant: the one-writer count is never exceeded
    # Each taker's own report is legitimate for ITS operation: an ``ok`` take
    # that later lost the slot to the other take is accepted preemption
    # semantics (the loser sees a "demoted" notice); a capacity-refused
    # interleaving reports ``promote_failed``.
    for ok, reason in results:
        assert (ok, reason) in [(True, "ok"), (True, "already_rw"), (False, "promote_failed")]
    # At most one demotion notice is sent (two takers; one may lose its seat
    # to the other's take).
    demotions = [p for p in other_receiver.received if p.get("type") == "client_mode" and p.get("reason") == "demoted"]
    assert len(demotions) <= 1
    if demotions:
        assert demotions[0]["mode"] == "read-only"


# ---------------------------------------------------------------------------
# take_write_slot: federated (origin-arbitrated) path


class _StubProxy:
    """Stands in for a muxcon RemotePortProxy on the request side.

    Carries ``connected_clients`` (for the taker's username lookup AND the
    local mirror a granted takeover must write) and ``max_read_write_users``
    (for ``_taker_entitled``) so that the entitlement check can run. The
    federation branch then just returns the stub's mode. NOTE: the stub does
    NOT demote its "other" holder on grant - the real origin does that in
    order (victim relay first, then the taker's ack), which the local
    mirror's capacity check depends on.
    """

    def __init__(self, result: str, taker_present: bool = True):
        self.result = result
        self.connected_clients = [
            {"client_id": "A", "username": "alice", "mode": "read-write"},
        ]
        if taker_present:
            self.connected_clients.append({"client_id": "B", "username": "bob", "mode": "read-only"})
        self.max_read_write_users = "one"
        self.read_write_groups = []
        self.read_only_groups = []

    async def take_write_slot_for_client(self, client_id: str, target_client_id=None, timeout: float = 3.0) -> str:
        return self.result


@pytest.mark.asyncio
async def test_take_federated_origin_grants(cm, port_manager):
    """A granted federated take must MIRROR onto the taker's LOCAL record.

    Regression for the "UI shows control but every keystroke is WRITE
    BLOCKED until reconnect" bug: the origin's promote only reaches its own
    ``fed:<peer>:<sid>`` pseudo-client; the local write gate reads THIS
    node's ``connected_clients`` mode, so the take must promote the taker's
    own local record too.
    """
    port = _StubProxy("read-write")
    port_manager.ports["p1"] = port
    cm.client_port_map["B"] = "p1"

    ok, reason = await cm.take_write_slot("B", "p1")

    assert (ok, reason) == (True, "ok")
    modes = {c["client_id"]: c["mode"] for c in port.connected_clients}
    assert modes["B"] == "read-write"  # the local mirror - this is what the write gate reads
    assert modes["A"] == "read-write"  # the stub holds (the real origin demoted it in order before the ack)


@pytest.mark.asyncio
async def test_take_federated_origin_denies(cm, port_manager):
    """A refused take leaves the local record exactly as it was."""
    port = _StubProxy("read-only")
    port_manager.ports["p1"] = port
    cm.client_port_map["B"] = "p1"

    ok, reason = await cm.take_write_slot("B", "p1")

    assert (ok, reason) == (False, "federation_denied")
    assert all(c["mode"] == "read-only" for c in port.connected_clients if c["client_id"] == "B")


@pytest.mark.asyncio
async def test_take_federated_origin_grant_missing_local_seat_refuses(cm, port_manager):
    """Origin granted but the taker has no local record to mirror onto
    (corrupt state): refuse rather than report success the write gate
    cannot honor. Permission resolves via the adapter metadata path so the
    entitlement check still passes and we reach the federation branch."""
    port = _StubProxy("read-write", taker_present=False)
    port_manager.ports["p1"] = port
    cm.client_port_map["B"] = "p1"  # attached, but absent from the port's local record list
    cm.client_to_manager["B"] = FakeAdapterChannel()

    ok, reason = await cm.take_write_slot("B", "p1")

    # A grant the local write gate cannot honor is refused (promote_failed)
    # rather than reported as success.
    assert (ok, reason) == (False, "promote_failed")
    assert all(c["client_id"] != "B" for c in port.connected_clients)  # no record invented


# ---------------------------------------------------------------------------
# take_write_slot: unresolvable identity


@pytest.mark.asyncio
async def test_take_taker_without_username_is_not_entitled(cm, port_manager):
    """A client with no username and no resolvable permission cannot take."""
    port = FakePort()
    port_manager.ports["p1"] = port
    _attach(cm, port, "p1", "A", "alice", "read-write")
    _attach(cm, port, "p1", "ghost", "", "read-only")

    ok, reason = await cm.take_write_slot("ghost", "p1")

    assert (ok, reason) == (False, "not_entitled")


# ---------------------------------------------------------------------------
# get_rw_holders_display / _resolve_client_ip


def test_get_rw_holders_display_label_shape(cm, port_manager):
    # Label (issue #61): "[client_id] username@ip (rw)" - the id in brackets
    # is the exact value a targeted takeover's client_id field matches on.
    port = FakePort([{"client_id": "A", "username": "alice", "mode": "read-write"}])
    port_manager.ports["p1"] = port
    assert cm.get_rw_holders_display("p1") == ["[A] alice@unknown (rw)"]


def test_get_rw_holders_display_resolves_ip_via_adapter(cm, port_manager):
    port = FakePort([{"client_id": "A", "username": "alice", "mode": "read-write"}])
    port_manager.ports["p1"] = port
    cm.client_to_manager["A"] = FakeAdapterChannel(ip="10.0.0.5")
    assert cm.get_rw_holders_display("p1") == ["[A] alice@10.0.0.5 (rw)"]


def test_get_rw_holders_display_shortens_long_local_ids(cm, port_manager):
    # Long local client_ids keep their last 8 characters in the label.
    port = FakePort([{"client_id": "abcdef1234567890", "username": "alice", "mode": "read-write"}])
    port_manager.ports["p1"] = port
    assert cm.get_rw_holders_display("p1") == ["[34567890] alice@unknown (rw)"]


def test_get_rw_holders_display_keeps_federated_ids_verbatim(cm, port_manager):
    # fed: ids are the wire spec the origin resolves - never shortened.
    port = FakePort([{"client_id": "fed:peerA:3", "username": "peerAuser", "mode": "read-write"}])
    port_manager.ports["p1"] = port
    assert cm.get_rw_holders_display("p1") == ["[fed:peerA:3] peerAuser@unknown (rw)"]


def test_get_rw_holders_display_excludes_read_only_clients(cm, port_manager):
    port = FakePort(
        [
            {"client_id": "A", "username": "alice", "mode": "read-write"},
            {"client_id": "B", "username": "bob", "mode": "read-only"},
        ]
    )
    port_manager.ports["p1"] = port
    assert cm.get_rw_holders_display("p1") == ["[A] alice@unknown (rw)"]


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
        max_rw: Any = 1,
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

    def test_admin_binds_to_write_slot_capacity(self):
        # issue #59: admin bypasses access control but NOT capacity. A slot is
        # a resource, not a privilege -- an exhausted port demotes admin to
        # read-only.
        cm = self._cm({"users": [{"username": "root", "password_hash": "x", "permissions": "admin"}]})
        # "one" with a free slot: admin gets read-write.
        assert cm._resolve_access_mode(_LadderPort(max_rw="one"), "p1", "admin", "root") == ("read-write", None)
        # "one" full (legacy int 1 accepted): admin demotes, even with group grants.
        assert cm._resolve_access_mode(_LadderPort(max_rw=1, rw_clients=1, rw_groups=("ops",)), "p1", "admin", "root") == (
            "read-only",
            None,
        )
        # "none" has no slots at all: admin is always read-only.
        assert cm._resolve_access_mode(_LadderPort(max_rw="none", rw_groups=("ops",)), "p1", "admin", "root") == (
            "read-only",
            None,
        )
        # "multiple" never demotes, no matter how busy.
        assert cm._resolve_access_mode(
            _LadderPort(max_rw="multiple", rw_clients=7, rw_groups=("ops",)), "p1", "admin", "root"
        ) == ("read-write", None)

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
