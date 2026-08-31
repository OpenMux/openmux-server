"""Tests for write-slot capacity tri-value (issue #59, part 1).

Covers the parse/migration matrix in the new ``access_control`` helper, the
per-mode access ladder matrix (including admin under ``none`` and loopback
under ``none``), adapter-level parse/validate hard errors, and the MuxCon
wire mapping (local mode -> wire int -> remote mode).
"""

import logging
import math
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from openmux.server.access_control import (
    WIRE_MULTIPLE,
    WRITE_MODES,
    InvalidWriteMode,
    capacity_from_wire,
    capacity_to_wire,
    holder_id_short,
    parse_write_mode,
    wire_to_mode,
    write_capacity,
)
from openmux.server.console_manager import ConsoleManager

# ---------------------------------------------------------------------------
# Parse / migration matrix
# ---------------------------------------------------------------------------


class TestParseWriteMode:
    def test_tri_values(self):
        assert parse_write_mode("none") == "none"
        assert parse_write_mode("one") == "one"
        assert parse_write_mode("multiple") == "multiple"

    def test_case_and_whitespace_insensitive(self):
        assert parse_write_mode("None") == "none"
        assert parse_write_mode("  ONE  ") == "one"
        assert parse_write_mode("Multiple") == "multiple"

    def test_unset_is_one(self):
        assert parse_write_mode(None) == "one"

    def test_legacy_integers(self):
        assert parse_write_mode(0) == "none"
        assert parse_write_mode(1) == "one"
        assert parse_write_mode(2) == "multiple"
        assert parse_write_mode(3) == "multiple"
        assert parse_write_mode(2**31) == "multiple"

    def test_legacy_numeric_strings(self):
        assert parse_write_mode("0") == "none"
        assert parse_write_mode("1") == "one"
        assert parse_write_mode(" 7 ") == "multiple"

    @pytest.mark.parametrize(
        "bad",
        ["two", "-1", "2.5", "", True, False, 2.5, -1, {}, []],
    )
    def test_invalid_values_raise(self, bad):
        with pytest.raises(InvalidWriteMode):
            parse_write_mode(bad)

    def test_deprecation_log_names_the_port(self, caplog):
        logger = logging.getLogger("openmux.ac")
        with caplog.at_level(logging.WARNING, logger="openmux.ac"):
            assert parse_write_mode(2, port_name="consoleX", logger=logger) == "multiple"
            assert parse_write_mode("0", port_name="consoleX", logger=logger) == "none"
            assert parse_write_mode("one", port_name="consoleX", logger=logger) == "one"
        legacy_lines = [r for r in caplog.records if "legacy integer" in r.getMessage()]
        # Mode strings must NOT log; legacy ints/strings must (once per call site).
        assert len(legacy_lines) == 2
        for rec in legacy_lines:
            assert "consoleX" in rec.getMessage()
            assert "max_read_write_users" in rec.getMessage()

    def test_write_modes_constant(self):
        assert WRITE_MODES == ("none", "one", "multiple")


class TestHolderIdShort:
    """The client_id shown next to a holder label (issue #61): it is the exact
    value a targeted takeover's ``client_id`` field matches on, so long local
    ids are shortened for display while federated ids (the wire spec the
    origin resolves) stay verbatim."""

    def test_short_local_id_kept(self):
        assert holder_id_short("A") == "A"
        assert holder_id_short("abcdef12") == "abcdef12"

    def test_long_local_id_keeps_last_eight(self):
        assert holder_id_short("0123456789abcdef") == "89abcdef"
        assert holder_id_short("holder-abcdefgh-123456") == "h-123456"

    def test_federated_id_kept_verbatim(self):
        # fed:<peer>:<stream> is the TAKE spec grammar (issue #59 Part 2).
        assert holder_id_short("fed:peerA:3") == "fed:peerA:3"

    def test_empty_and_none(self):
        assert holder_id_short("") == ""
        assert holder_id_short(None) == ""


class TestCapacityMapping:
    def test_write_capacity_values(self):
        assert write_capacity("none") == 0.0
        assert write_capacity("one") == 1.0
        assert write_capacity("multiple") == math.inf
        # Legacy ints and numeric strings still resolve through the parser.
        assert write_capacity(0) == 0.0
        assert write_capacity(1) == 1.0
        assert write_capacity(9) == math.inf
        # An unparseable value never unlocks unlimited writers.
        assert write_capacity("garbage") == 1.0

    def test_wire_roundtrip(self):
        assert capacity_to_wire("none") == 0
        assert capacity_to_wire("one") == 1
        assert capacity_to_wire("multiple") == WIRE_MULTIPLE
        # Legacy ints map to their wire capacity unchanged.
        assert capacity_to_wire(1) == 1
        assert capacity_to_wire(0) == 0
        assert capacity_to_wire(3) == WIRE_MULTIPLE

    def test_wire_to_mode(self):
        # New peers send the mode; older peers send their legacy count.
        assert wire_to_mode("none") == "none"
        assert wire_to_mode("one") == "one"
        assert wire_to_mode("multiple") == "multiple"
        assert wire_to_mode(0) == "none"
        assert wire_to_mode(1) == "one"
        assert wire_to_mode(2) == "multiple"
        assert wire_to_mode(WIRE_MULTIPLE) == "multiple"
        # Missing/broken metadata falls back to one (never unlimited).
        assert wire_to_mode(None) == "one"
        assert wire_to_mode("garbage") == "one"

    def test_capacity_from_wire(self):
        assert capacity_from_wire(0) == 0.0
        assert capacity_from_wire(1) == 1.0
        assert capacity_from_wire(WIRE_MULTIPLE) == math.inf
        assert capacity_from_wire("none") == 0.0
        assert math.isinf(capacity_from_wire("multiple"))
        assert capacity_from_wire(None) == 1.0


# ---------------------------------------------------------------------------
# Access-ladder matrix per capacity mode (unit level, like the #58 ladder tests)
# ---------------------------------------------------------------------------


class _CapPort:
    """Stand-in port whose max_read_write_users is already the stored mode."""

    def __init__(self, mode: str = "one", rw_clients: int = 0, rw_groups=(), ro_groups=()):
        self.max_read_write_users = mode
        self.read_write_groups = list(rw_groups)
        self.read_only_groups = list(ro_groups)
        self.loopback = False
        self.adapter_type = "fake"
        self.connected_clients = [
            {"client_id": f"rw{i}", "username": f"rw{i}", "mode": "read-write"} for i in range(rw_clients)
        ]


class TestCapacityLadder:
    def _cm(self, auth_config: Dict[str, Any]) -> ConsoleManager:
        from openmux.server.auth_manager import AuthManager
        from tests.server.test_console_manager import FakePortManager

        return ConsoleManager(FakePortManager(), AuthManager(auth_config))

    def test_admin_under_one_free_slot_gets_write(self):
        cm = self._cm({"users": [{"username": "root", "password_hash": "x", "permissions": "admin"}]})
        assert cm._resolve_access_mode(_CapPort("one"), "p1", "admin", "root") == ("read-write", None)

    def test_admin_under_one_full_slot_demotes(self):
        cm = self._cm({"users": [{"username": "root", "password_hash": "x", "permissions": "admin"}]})
        assert cm._resolve_access_mode(_CapPort("one", rw_clients=1), "p1", "admin", "root") == ("read-only", None)

    def test_admin_under_multiple_always_gets_write(self):
        cm = self._cm({"users": [{"username": "root", "password_hash": "x", "permissions": "admin"}]})
        full = _CapPort("multiple", rw_clients=7)
        assert cm._resolve_access_mode(full, "p1", "admin", "root") == ("read-write", None)

    def test_admin_under_none_is_read_only(self):
        """issue #59 behavior change: a slot is a resource, not a privilege."""
        cm = self._cm({"users": [{"username": "root", "password_hash": "x", "permissions": "admin"}]})
        assert cm._resolve_access_mode(_CapPort("none"), "p1", "admin", "root") == ("read-only", None)

    def test_admin_under_none_demotes_even_with_group_grant(self):
        cm = self._cm({"users": [{"username": "root", "password_hash": "x", "permissions": "admin"}]})
        port = _CapPort("none", rw_groups=("ops",))
        assert cm._resolve_access_mode(port, "p1", "admin", "root") == ("read-only", None)

    def test_global_rw_under_multiple_never_demotes(self):
        cm = self._cm({"users": [{"username": "alice", "password_hash": "x"}]})
        full = _CapPort("multiple", rw_clients=9)
        assert cm._resolve_access_mode(full, "p1", "read-write", "alice") == ("read-write", None)

    def test_global_rw_under_one_demotes_when_full(self):
        cm = self._cm({"users": [{"username": "alice", "password_hash": "x"}]})
        assert cm._resolve_access_mode(_CapPort("one"), "p1", "read-write", "alice") == ("read-write", None)
        assert cm._resolve_access_mode(_CapPort("one", rw_clients=1), "p1", "read-write", "alice") == (
            "read-only",
            None,
        )

    def test_global_rw_under_none_demotes(self):
        cm = self._cm({"users": [{"username": "alice", "password_hash": "x"}]})
        assert cm._resolve_access_mode(_CapPort("none"), "p1", "read-write", "alice") == ("read-only", None)

    def test_read_only_user_under_multiple_stays_read_only(self):
        cm = self._cm({"users": [{"username": "auditor", "password_hash": "x", "permissions": "read-only"}]})
        assert cm._resolve_access_mode(_CapPort("multiple"), "p1", "read-only", "auditor") == ("read-only", None)

    def test_group_rw_grant_under_none_demotes(self):
        cm = self._cm({"users": [{"username": "alice", "password_hash": "x", "groups": ["ops"]}]})
        port = _CapPort("none", rw_groups=("ops",))
        assert cm._resolve_access_mode(port, "p1", "read-write", "alice") == ("read-only", None)

    def test_group_ro_grant_unaffected_by_capacity(self):
        cm = self._cm({"users": [{"username": "bob", "password_hash": "x", "groups": ["viewers"]}]})
        assert cm._resolve_access_mode(_CapPort("none", ro_groups=("viewers",)), "p1", "read-only", "bob") == (
            "read-only",
            None,
        )


# ---------------------------------------------------------------------------
# End-to-end with the real PortManager + LoopbackAdapter (like the #58 suite)
# ---------------------------------------------------------------------------


class TestLoopbackCapacityEndToEnd:
    """Real stack so the mode threads through port, wrapper, and manager."""

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
    async def test_loopback_default_mode_is_one(self, monkeypatch):
        # Unconfigured loopback defaults to "one" (consistent with serial/command).
        cm = await self._make_manager(monkeypatch, {"name": "p1"}, {})
        port = cm.port_manager.ports["p1"].unified_port
        assert port.max_read_write_users == "one"

    @pytest.mark.asyncio
    async def test_loopback_none_admin_is_read_only(self, monkeypatch):
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "max_read_write_users": "none"},
            {"users": [{"username": "root", "password_hash": "x", "permissions": "admin"}]},
        )
        ok, mode, reason = await cm.connect_client_to_port("c1", "p1", "root")
        assert (ok, mode, reason) == (True, "read-only", None)

    @pytest.mark.asyncio
    async def test_loopback_none_second_user_and_promotion_refused(self, monkeypatch):
        """No driver at all: read-write attach falls back, promotion is refused."""
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "max_read_write_users": "none"},
            {"users": [{"username": "alice", "password_hash": "x"}]},
        )
        ok, mode, reason = await cm.connect_client_to_port("c1", "p1", "alice")
        assert (ok, mode, reason) == (True, "read-only", None)
        assert await cm.promote_client_to_read_write("c1", "p1") is False
        # The seat stayed read-only.
        port = cm.port_manager.ports["p1"]
        assert all(c["mode"] == "read-only" for c in port.connected_clients)

    @pytest.mark.asyncio
    async def test_loopback_multiple_two_writers_no_demotion(self, monkeypatch):
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "max_read_write_users": "multiple"},
            {"users": [{"username": "alice", "password_hash": "x"}, {"username": "bob", "password_hash": "x"}]},
        )
        ok1, m1, _ = await cm.connect_client_to_port("c1", "p1", "alice")
        ok2, m2, _ = await cm.connect_client_to_port("c2", "p1", "bob")
        assert (ok1, m1) == (True, "read-write")
        assert (ok2, m2) == (True, "read-write")

    @pytest.mark.asyncio
    async def test_loopback_one_second_write_demotes(self, monkeypatch):
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "max_read_write_users": "one"},
            {"users": [{"username": "alice", "password_hash": "x"}, {"username": "bob", "password_hash": "x"}]},
        )
        ok1, m1, _ = await cm.connect_client_to_port("c1", "p1", "alice")
        ok2, m2, _ = await cm.connect_client_to_port("c2", "p1", "bob")
        assert (ok1, m1) == (True, "read-write")
        assert (ok2, m2) == (True, "read-only")

    @pytest.mark.asyncio
    async def test_loopback_legacy_int_two_maps_to_multiple(self, monkeypatch):
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "max_read_write_users": 2},
            {"users": [{"username": "alice", "password_hash": "x"}, {"username": "bob", "password_hash": "x"}]},
        )
        ok1, m1, _ = await cm.connect_client_to_port("c1", "p1", "alice")
        ok2, m2, _ = await cm.connect_client_to_port("c2", "p1", "bob")
        assert m1 == "read-write" and m2 == "read-write"


# ---------------------------------------------------------------------------
# PortManager seat accounting per mode (wrapper level)
# ---------------------------------------------------------------------------


class _FakePort:
    def __init__(self, mode: str):
        self.max_read_write_users = mode
        self.connected_clients: list = []


class _FakePM:
    """Just enough of PortManager to drive add_client/promote capacity checks."""

    def __init__(self, port: _FakePort):
        self.ports = {"p1": port}
        self.logger = logging.getLogger("test")

    async def add_client_to_port(self, port_name, client_id, username, mode):
        port = self.ports[port_name]
        if mode == "read-write":
            capacity = write_capacity(port.max_read_write_users)
            current_rw = sum(
                1 for c in port.connected_clients if c.get("mode") == "read-write" and c.get("client_id") != client_id
            )
            if current_rw >= capacity:
                return False
        port.connected_clients.append({"client_id": client_id, "username": username, "mode": mode})
        return True

    # Reuse the real capacity helper for promote, like the production code.
    async def promote_client(self, port_name, client_id):
        port = self.ports[port_name]
        if write_capacity(port.max_read_write_users) == 0.0:
            return False
        for c in port.connected_clients:
            if c["client_id"] == client_id:
                c["mode"] = "read-write"
                return True
        return False


def test_add_client_refused_on_none_port():
    pm = _FakePM(_FakePort("none"))

    async def run():
        return await pm.add_client_to_port("p1", "c1", "alice", "read-write")

    assert _async(run()) is False


def test_add_client_two_writers_on_multiple_port():
    pm = _FakePM(_FakePort("multiple"))

    async def run():
        assert await pm.add_client_to_port("p1", "c1", "alice", "read-write") is True
        return await pm.add_client_to_port("p1", "c2", "bob", "read-write")

    assert _async(run()) is True


def test_add_client_one_writer_on_one_port_second_refused():
    pm = _FakePM(_FakePort("one"))

    async def run():
        assert await pm.add_client_to_port("p1", "c1", "alice", "read-write") is True
        return await pm.add_client_to_port("p1", "c2", "bob", "read-write")

    assert _async(run()) is False


def _async(coro):
    import asyncio

    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Adapter-level parse / validate hard errors
# ---------------------------------------------------------------------------


class TestAdapterValidation:
    def test_loopback_rejects_invalid_mode(self):
        from openmux.server.adapters.loopback import LoopbackAdapter

        bad = {"loopback_ports": [{"name": "p1", "max_read_write_users": "two"}]}
        assert LoopbackAdapter.validate_config(bad) is False
        # Legacy ints and all three modes are accepted.
        good = {"loopback_ports": [{"name": f"p{i}", "max_read_write_users": v} for i, v in enumerate(["none", 1, 0, 3])]}
        assert LoopbackAdapter.validate_config(good) is True

    def test_loopback_port_stores_mode_and_migrates_legacy(self, caplog):
        from openmux.server.adapters.loopback import LoopbackPort

        with caplog.at_level(logging.WARNING):
            port = LoopbackPort("p1", {"name": "p1", "max_read_write_users": 5}, adapter=None)
        assert port.max_read_write_users == "multiple"
        assert any("legacy integer" in r.getMessage() for r in caplog.records)
        port2 = LoopbackPort("p2", {"name": "p2", "max_read_write_users": "none"}, adapter=None)
        assert port2.max_read_write_users == "none"
        # Unset keeps the one default (consistent with serial/command).
        port3 = LoopbackPort("p3", {"name": "p3"}, adapter=None)
        assert port3.max_read_write_users == "one"

    def test_loopback_port_rejects_invalid_mode(self):
        from openmux.server.adapters.loopback import LoopbackPort

        with pytest.raises(InvalidWriteMode):
            LoopbackPort("p1", {"name": "p1", "max_read_write_users": "two"}, adapter=None)

    def test_serial_config_rejects_invalid_mode_in_post_init(self):
        from openmux.server.adapters.serial import SerialPortConfig

        with pytest.raises(ValueError):
            SerialPortConfig(name="p1", description="d", device="/dev/ttyX", max_read_write_users="two")
        cfg = SerialPortConfig(name="p1", description="d", device="/dev/ttyX")
        assert cfg.max_read_write_users == "one"

    def test_serial_validate_config_rejects_invalid_mode(self):
        from openmux.server.adapters.serial import SerialAdapter

        bad = {"serial_ports": [{"name": "p1", "device": "/dev/ttyX", "max_read_write_users": "two"}]}
        assert SerialAdapter.validate_config(bad) is False
        good = {"serial_ports": [{"name": "p1", "device": "/dev/ttyX", "max_read_write_users": "none"}]}
        assert SerialAdapter.validate_config(good) is True
        # Legacy ints stay valid.
        legacy = {"serial_ports": [{"name": "p1", "device": "/dev/ttyX", "max_read_write_users": 2}]}
        assert SerialAdapter.validate_config(legacy) is True

    def test_serial_resolve_migrates_and_holds_modes(self):
        from openmux.server.adapters.serial import SerialAdapter

        adapter = SerialAdapter.__new__(SerialAdapter)
        adapter.logger = logging.getLogger("test")
        assert adapter._resolve_max_rw_users({"name": "p", "device": "d"}) == "one"
        assert adapter._resolve_max_rw_users({"name": "p", "device": "d", "max_read_write_users": "none"}) == "none"
        assert adapter._resolve_max_rw_users({"name": "p", "device": "d", "max_read_write_users": 4}) == "multiple"
        # Legacy read_write_users fallback key still honored.
        assert adapter._resolve_max_rw_users({"name": "p", "device": "d", "read_write_users": 0}) == "none"
        with pytest.raises(InvalidWriteMode):
            adapter._resolve_max_rw_users({"name": "p", "device": "d", "max_read_write_users": "two"})

    def test_command_validate_config_rejects_invalid_mode(self):
        from openmux.server.adapters.command import CommandAdapter

        bad = {"command_ports": [{"name": "p1", "command": "sh", "max_read_write_users": "two"}]}
        assert CommandAdapter.validate_config(bad) is False
        good = {"command_ports": [{"name": "p1", "command": "sh", "max_read_write_users": "multiple"}]}
        assert CommandAdapter.validate_config(good) is True

    @pytest.mark.asyncio
    async def test_command_port_migrates_legacy(self, caplog):
        # async: the port constructor creates an asyncio.Queue (needs a live loop).
        from openmux.server.adapters.command import CommandPort

        with caplog.at_level(logging.WARNING):
            port = CommandPort("p1", {"name": "p1", "command": "sh", "max_read_write_users": 3}, adapter=None)
        assert port.max_read_write_users == "multiple"
        assert any("legacy integer" in r.getMessage() for r in caplog.records)
        port2 = CommandPort("p2", {"name": "p2", "command": "sh", "max_read_write_users": "none"}, adapter=None)
        assert port2.max_read_write_users == "none"

    def test_tcp_initiator_validate_config_rejects_invalid_mode(self):
        """Issue #60: TCP initiator ports take the same tri-value."""
        from openmux.server.adapters.tcp_initiator import TcpInitiatorAdapter

        bad = {"tcp_initiator_ports": [{"name": "p1", "host": "127.0.0.1", "port": 1, "max_read_write_users": "two"}]}
        assert TcpInitiatorAdapter.validate_config(bad) is False
        good = {"tcp_initiator_ports": [{"name": "p1", "host": "127.0.0.1", "port": 1, "max_read_write_users": "multiple"}]}
        assert TcpInitiatorAdapter.validate_config(good) is True
        # Legacy ints stay valid.
        legacy = {"tcp_initiator_ports": [{"name": "p1", "host": "127.0.0.1", "port": 1, "max_read_write_users": 2}]}
        assert TcpInitiatorAdapter.validate_config(legacy) is True

    @pytest.mark.asyncio
    async def test_tcp_initiator_port_stores_mode_and_migrates_legacy(self, caplog):
        from openmux.server.adapters.tcp_initiator import TcpInitiatorPort

        with caplog.at_level(logging.WARNING):
            port = TcpInitiatorPort(
                "p1",
                {"name": "p1", "host": "127.0.0.1", "port": 1, "max_read_write_users": 3, "connect_on_demand": True},
                adapter=None,
            )
        assert port.max_read_write_users == "multiple"
        assert any("legacy integer" in r.getMessage() for r in caplog.records)
        port2 = TcpInitiatorPort(
            "p2",
            {"name": "p2", "host": "127.0.0.1", "port": 1, "max_read_write_users": "none", "connect_on_demand": True},
            adapter=None,
        )
        assert port2.max_read_write_users == "none"
        # Unset keeps the one default (consistent with serial/loopback/command).
        port3 = TcpInitiatorPort("p3", {"name": "p3", "host": "127.0.0.1", "port": 1, "connect_on_demand": True}, adapter=None)
        assert port3.max_read_write_users == "one"


# ---------------------------------------------------------------------------
# TCP initiator: reconcile detects a mode change (issue #60)
# ---------------------------------------------------------------------------


class TestTcpInitiatorReconcile:
    @pytest.mark.asyncio
    async def test_mode_change_triggers_recreate_legacy_int_stays_unchanged(self):
        from openmux.server.adapters.tcp_initiator import TcpInitiatorAdapter, TcpInitiatorPort

        adapter = TcpInitiatorAdapter("tcp", {})
        port = TcpInitiatorPort("p1", {"name": "p1", "host": "h", "port": 1, "max_read_write_users": "one"}, adapter=None)
        adapter.ports["p1"] = port

        destroyed, created = [], []

        async def fake_destroy(n):
            destroyed.append(n)
            adapter.ports.pop(n, None)

        async def fake_create(n, cfg):
            created.append((n, cfg.get("max_read_write_users")))
            adapter.ports[n] = TcpInitiatorPort(n, dict(cfg, connect_on_demand=True), adapter)
            return adapter.ports[n]

        adapter.destroy_port = fake_destroy
        adapter.create_port = fake_create

        # Identical config -> unchanged.
        res = await adapter.reconcile_ports([{"name": "p1", "host": "h", "port": 1}])
        assert res["unchanged"] == ["p1"] and not res["updated"]
        # A legacy int equal to the stored mode -> unchanged (silent normalization).
        res = await adapter.reconcile_ports([{"name": "p1", "host": "h", "port": 1, "max_read_write_users": 1}])
        assert res["unchanged"] == ["p1"] and not res["updated"]
        # A different mode -> recreate.
        res = await adapter.reconcile_ports([{"name": "p1", "host": "h", "port": 1, "max_read_write_users": "multiple"}])
        assert res["updated"] == ["p1"] and destroyed == ["p1"] and created == [("p1", "multiple")]


# ---------------------------------------------------------------------------
# End-to-end with the real PortManager + TcpInitiatorAdapter (issue #60)
# ---------------------------------------------------------------------------


class TestTcpInitiatorCapacityEndToEnd:
    """Real stack so the mode threads through port, wrapper, and manager.

    Ports use ``connect_on_demand`` so no real connection is attempted.
    """

    async def _make_manager(self, monkeypatch, port_config: Dict[str, Any], auth_config: Dict[str, Any]):
        from openmux.server import data_logger as dl_mod
        from openmux.server.adapters.tcp_initiator import TcpInitiatorAdapter
        from openmux.server.auth_manager import AuthManager
        from openmux.server.port_manager import PortManager

        class _DummyDataLogger:
            def record(self, *args, **kwargs):
                pass

        monkeypatch.setattr(dl_mod.DataLogger, "get", classmethod(lambda cls: _DummyDataLogger()))

        cfg = dict(port_config, connect_on_demand=True)
        pm = PortManager([])
        adapter = TcpInitiatorAdapter("tcp", {"tcp_initiator_ports": [cfg]})
        adapter.main_port_manager = pm
        pm.set_unified_adapters([adapter])
        assert await adapter.start() is True

        auth = AuthManager(auth_config)
        return ConsoleManager(pm, auth)

    @pytest.mark.asyncio
    async def test_tcp_initiator_default_mode_is_one(self, monkeypatch):
        # Unconfigured TCP initiator defaults to "one" (consistent with
        # serial/loopback/command) - previously the missing attribute fell
        # through to the seat-accounting default, which happened to be one.
        cm = await self._make_manager(monkeypatch, {"name": "p1", "host": "127.0.0.1", "port": 1}, {})
        port = cm.port_manager.ports["p1"].unified_port
        assert port.max_read_write_users == "one"

    @pytest.mark.asyncio
    async def test_tcp_initiator_none_admin_is_read_only(self, monkeypatch):
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "host": "127.0.0.1", "port": 1, "max_read_write_users": "none"},
            {"users": [{"username": "root", "password_hash": "x", "permissions": "admin"}]},
        )
        ok, mode, reason = await cm.connect_client_to_port("c1", "p1", "root")
        assert (ok, mode, reason) == (True, "read-only", None)

    @pytest.mark.asyncio
    async def test_tcp_initiator_none_second_user_and_promotion_refused(self, monkeypatch):
        """No driver at all: read-write attach falls back, promotion is refused."""
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "host": "127.0.0.1", "port": 1, "max_read_write_users": "none"},
            {"users": [{"username": "alice", "password_hash": "x"}]},
        )
        ok, mode, reason = await cm.connect_client_to_port("c1", "p1", "alice")
        assert (ok, mode, reason) == (True, "read-only", None)
        assert await cm.promote_client_to_read_write("c1", "p1") is False
        # The seat stayed read-only.
        port = cm.port_manager.ports["p1"]
        assert all(c["mode"] == "read-only" for c in port.connected_clients)

    @pytest.mark.asyncio
    async def test_tcp_initiator_multiple_two_writers_no_demotion(self, monkeypatch):
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "host": "127.0.0.1", "port": 1, "max_read_write_users": "multiple"},
            {"users": [{"username": "alice", "password_hash": "x"}, {"username": "bob", "password_hash": "x"}]},
        )
        ok1, m1, _ = await cm.connect_client_to_port("c1", "p1", "alice")
        ok2, m2, _ = await cm.connect_client_to_port("c2", "p1", "bob")
        assert (ok1, m1) == (True, "read-write")
        assert (ok2, m2) == (True, "read-write")

    @pytest.mark.asyncio
    async def test_tcp_initiator_one_second_write_demotes(self, monkeypatch):
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "host": "127.0.0.1", "port": 1, "max_read_write_users": "one"},
            {"users": [{"username": "alice", "password_hash": "x"}, {"username": "bob", "password_hash": "x"}]},
        )
        ok1, m1, _ = await cm.connect_client_to_port("c1", "p1", "alice")
        ok2, m2, _ = await cm.connect_client_to_port("c2", "p1", "bob")
        assert (ok1, m1) == (True, "read-write")
        assert (ok2, m2) == (True, "read-only")

    @pytest.mark.asyncio
    async def test_tcp_initiator_legacy_int_two_maps_to_multiple(self, monkeypatch):
        cm = await self._make_manager(
            monkeypatch,
            {"name": "p1", "host": "127.0.0.1", "port": 1, "max_read_write_users": 2},
            {"users": [{"username": "alice", "password_hash": "x"}, {"username": "bob", "password_hash": "x"}]},
        )
        ok1, m1, _ = await cm.connect_client_to_port("c1", "p1", "alice")
        ok2, m2, _ = await cm.connect_client_to_port("c2", "p1", "bob")
        assert m1 == "read-write" and m2 == "read-write"


# ---------------------------------------------------------------------------
# MuxCon wire: local mode travels, remote side evaluates
# ---------------------------------------------------------------------------


class TestMuxConWire:
    def test_local_mode_maps_to_wire_int(self):
        # The port-list builder sends capacity_to_wire(port.max_read_write_users).
        assert capacity_to_wire("one") == 1
        assert capacity_to_wire("multiple") == WIRE_MULTIPLE
        assert capacity_to_wire("none") == 0
        # And an older peer's legacy count is still a legal wire value.
        assert capacity_to_wire(1) == 1

    def test_remote_proxy_mode_from_new_origin(self):
        # New origin advertises capacity ints; remote derives the mode.
        assert wire_to_mode(capacity_to_wire("none")) == "none"
        assert wire_to_mode(capacity_to_wire("one")) == "one"
        assert wire_to_mode(capacity_to_wire("multiple")) == "multiple"

    def test_remote_proxy_mode_from_legacy_origin(self):
        # Older origins send their raw counts: >= 2 keeps acting as multiple.
        assert wire_to_mode(3) == "multiple"
        assert wire_to_mode(1) == "one"
        assert wire_to_mode(0) == "none"

    @pytest.mark.asyncio
    async def test_remote_port_proxy_init_evaluates_mode(self):
        # async: RemotePortProxy creates an asyncio.Queue (needs a live loop).
        from types import SimpleNamespace

        from openmux.server.adapters.muxcon import UnifiedMuxConAdapter

        class _Adapter:
            pass

        for wire, expected in [(0, "none"), (1, "one"), (WIRE_MULTIPLE, "multiple"), (3, "multiple"), (None, "one")]:
            meta = SimpleNamespace(description="d", max_rw_users=wire)
            proxy = UnifiedMuxConAdapter.RemotePortProxy(_Adapter(), "conn", "remote:p1", meta)
            assert proxy.max_read_write_users == expected, f"wire={wire}"
            # Local seat accounting must agree with the derived mode.
            assert write_capacity(proxy.max_read_write_users) == capacity_from_wire(wire)

    def test_capacity_from_wire_never_unbounded_on_garbage(self):
        assert capacity_from_wire("garbage") == 1.0
        assert capacity_from_wire(-5) == 1.0
        assert capacity_from_wire(None) == 1.0
