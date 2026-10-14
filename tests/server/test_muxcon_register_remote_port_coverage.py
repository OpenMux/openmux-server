"""Behavioral coverage for UnifiedMuxConAdapter._register_remote_port_from_dict (62-bracket S1).

Drives the real federated-port registration against a real adapter (cheap
constructor: UnifiedMuxConAdapter("mx", {"listeners": []})), replacing the
best-effort collaborators (stale-session close, re-grant, link notice,
refresh, save-cache, power meta) with recorders, and pinning observable
state: the PortMetadata built from the port dict, the peer->proxy mapping,
reused-proxy in-place updates, and the fresh-proxy create/register path.

Written (and green) against the PRE-refactor method, using ONLY the pre-
refactor surface (the monolithic method + the real collaborators it calls),
so it remains valid after the driver is split into _rp_* helpers.
"""

import logging

import pytest

from openmux.common.federation_types import FederationType, PortMetadata, ServerInfo, ServerType
from openmux.server.access_control import capacity_from_wire, wire_to_mode
from openmux.server.adapters.muxcon import UnifiedMuxConAdapter


def _si(server_id, stype=None):
    t = stype if stype is not None else ServerType.LEAF
    return ServerInfo(server_id=server_id, hostname="h-" + str(server_id), port=1, server_type=t)


def _adapter(monkeypatch, pm=None):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    a._peer_proxies = {}
    a.main_port_manager = pm
    a.calls = {"save": 0, "stale": 0, "power": 0, "regrant": 0, "refresh": 0}
    a.notice_calls = []

    monkeypatch.setattr(a, "_derive_peer_key_from_conn_id", lambda cid: "PK")

    def _refresh():
        a.calls["refresh"] += 1

    monkeypatch.setattr(a, "_refresh_conn_proxies", _refresh)

    def _save():
        a.calls["save"] += 1

    monkeypatch.setattr(a, "_save_federated_cache", _save)

    async def _stale(proxy, conn_id):
        a.calls["stale"] += 1

    monkeypatch.setattr(a, "_close_stale_proxy_sessions", _stale)

    def _notice(proxy, state, src):
        a.notice_calls.append((state, src))

    monkeypatch.setattr(a, "_emit_link_notice_once", _notice)

    async def _power(proxy, metadata):
        a.calls["power"] += 1

    monkeypatch.setattr(a, "_apply_power_meta_to_proxy", _power)

    async def _regrant(proxy, conn_id):
        a.calls["regrant"] += 1

    monkeypatch.setattr(a, "_regrant_proxy_read_write", _regrant)

    return a


class FakePortManager:
    def __init__(self, ports=None, get_result=None, get_raise=False, ports_raise=False, register_ret=True):
        self._ports = ports if ports is not None else {}
        self.get_result = get_result
        self.get_raise = get_raise
        self.ports_raise = ports_raise
        self.register_ret = register_ret
        self.registered = []

    @property
    def ports(self):
        if self.ports_raise:
            raise RuntimeError("ports attr boom")
        return self._ports

    def get_port(self, name):
        if self.get_raise:
            raise RuntimeError("get_port boom")
        return self.get_result

    async def register_federated_port(self, metadata, proxy):
        self.registered.append((metadata, proxy))
        return self.register_ret


def _raise_on(name):
    return RuntimeError("set boom: " + name)


class FakeProxy:
    def __init__(self, has_remote_name=True, missing_callback=True, raise_set=None, close_exc=None, metadata=None):
        object.__setattr__(self, "_raise_set", raise_set or set())
        object.__setattr__(self, "_close_exc", close_exc)
        if has_remote_name:
            self.remote_port_name = "p1"
        # `metadata` is a plain settable attribute on the real RemotePortProxy;
        # this mirrors that so the reuse path's setattr(existing, "metadata", ..)
        # and the duplicate check's getattr(..., "metadata", None) both behave.
        if metadata is not None and "metadata" not in (raise_set or set()):
            object.__setattr__(self, "metadata", metadata)
        # Initial values are set via object.__setattr__ (bypassing _raise_set)
        # so a raise-armed attribute still starts with its real value and only
        # raises when the SOURCE method reassigns it.
        object.__setattr__(self, "is_connected", False)
        object.__setattr__(self, "link_reason", "MuxCon link down")
        object.__setattr__(self, "data_callback", None if missing_callback else object())
        object.__setattr__(self, "connected_clients", [])
        object.__setattr__(self, "reopened", [])
        object.__setattr__(self, "_closed", False)

    def __setattr__(self, name, value):
        if name in object.__getattribute__(self, "_raise_set"):
            raise _raise_on(name)
        object.__setattr__(self, name, value)

    async def close_all_streams(self):
        exc = object.__getattribute__(self, "_close_exc")
        if exc is not None:
            raise exc
        self._closed = True

    async def open_stream_for_client(self, cid):
        self.reopened.append(cid)


def _run(a, pd):
    return a._register_remote_port_from_dict("c1", pd)


def _origin(server_id, server_type="leaf", **kw):
    base = {"server_id": server_id, "hostname": "h-" + str(server_id), "port": 1234, "server_type": server_type}
    base.update(kw)
    return base


def _pd(name="p1", origin=_origin("OSRC"), **kw):
    base = {"name": name, "origin_server": origin}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Fresh create / register path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_fresh_registers_and_maps(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    pm = FakePortManager()
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd())

    assert len(pm.registered) == 1
    metadata, proxy = pm.registered[0]
    assert isinstance(metadata, PortMetadata)
    assert metadata.name == "p1" and metadata.original_name == "p1"
    assert metadata.description == "Remote port p1"
    assert metadata.adapter_type == "remote_muxcon"
    assert metadata.status == "connected"
    assert metadata.federation_type is FederationType.PULL
    assert metadata.serial_config is None
    assert metadata.line_status is None
    assert metadata.read_write_groups is None
    assert metadata.read_only_groups is None
    assert metadata.status_message is None
    assert metadata.readiness is None
    assert metadata.power is None
    assert metadata.max_rw_users == capacity_from_wire(1)
    assert metadata.origin_server.server_id == "OSRC"
    assert metadata.origin_server.hostname == "h-OSRC"
    assert metadata.origin_server.port == 1234
    assert metadata.origin_server.server_type is ServerType.LEAF
    assert metadata.server_chain[0] is metadata.origin_server
    assert len(metadata.server_chain) == 1
    assert isinstance(proxy, UnifiedMuxConAdapter.RemotePortProxy)
    assert proxy.remote_port_name == "p1"
    assert proxy.server_adapter is a
    assert a._peer_proxies["PK"]["p1"] is proxy
    assert a.calls["save"] == 1
    assert "Registered federated port" in caplog.text


@pytest.mark.asyncio
async def test_create_registers_explicit_fields(monkeypatch):
    pm = FakePortManager()
    a = _adapter(monkeypatch, pm=pm)
    pd = _pd(
        name="p9",
        origin=_origin("ORIG2", server_type="hub", hostname="h2", port=9999, description="d2"),
        description="custom desc",
        adapter_type="remote_telnet",
        status="down",
        max_rw_users=2,
        read_write_groups=["g1"],
        read_only_groups=["r1"],
        status_message="link lost",
        readiness="ready",
        serial_config={"device": "/dev/ttyS0", "baudrate": 115200},
        line_status={"status": "connected"},
    )
    await _run(a, pd)
    m = pm.registered[0][0]
    assert m.name == "p9"
    assert m.description == "custom desc"
    assert m.adapter_type == "remote_telnet"
    assert m.status == "down"
    assert m.max_rw_users == capacity_from_wire(2)
    assert m.read_write_groups == ["g1"]
    assert m.read_only_groups == ["r1"]
    assert m.status_message == "link lost"
    assert m.readiness == "ready"
    assert m.serial_config == {
        "device": "/dev/ttyS0",
        "baudrate": 115200,
        "bytesize": None,
        "parity": None,
        "stopbits": None,
        "flow_control": None,
    }
    assert m.line_status == {"status": "connected"}
    assert m.origin_server.server_id == "ORIG2"
    assert m.origin_server.server_type is ServerType.HUB
    assert m.origin_server.hostname == "h2"
    assert m.origin_server.port == 9999
    assert m.origin_server.description == "d2"


@pytest.mark.asyncio
async def test_create_origin_enum_fallback(monkeypatch):
    pm = FakePortManager()
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd(origin=_origin("BAD", server_type="not_a_type")))
    m = pm.registered[0][0]
    assert m.origin_server.server_id == "BAD"
    assert m.origin_server.server_type is ServerType.LEAF
    assert m.origin_server.hostname == "h-BAD"


@pytest.mark.asyncio
async def test_create_origin_legacy_string(monkeypatch):
    pm = FakePortManager()
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, {"name": "p1", "origin_server": "legacy-srv"})
    m = pm.registered[0][0]
    assert m.origin_server.server_id == "legacy-srv"
    assert m.origin_server.hostname == "remote"
    assert m.origin_server.port == 0
    assert m.origin_server.server_type is ServerType.LEAF


@pytest.mark.asyncio
async def test_create_origin_none_and_name_fallbacks(monkeypatch):
    pm = FakePortManager()
    a = _adapter(monkeypatch, pm=pm)
    # no origin_server -> "remote"; no name/original_name -> "remote_port"
    await _run(a, {})
    m = pm.registered[0][0]
    assert m.origin_server.server_id == "remote"
    assert m.name == "remote_port"


@pytest.mark.asyncio
async def test_create_name_from_original_name(monkeypatch):
    pm = FakePortManager()
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, {"original_name": "orig-name", "origin_server": _origin("OSRC")})
    m = pm.registered[0][0]
    assert m.name == "orig-name"
    assert m.description == "Remote port orig-name"


@pytest.mark.asyncio
async def test_create_no_port_manager_warns(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    a = _adapter(monkeypatch, pm=None)
    await _run(a, _pd())
    assert "No main_port_manager set" in caplog.text


@pytest.mark.asyncio
async def test_create_register_falsy_skips_mapping(monkeypatch):
    pm = FakePortManager(register_ret=False)
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd())
    assert len(pm.registered) == 1
    assert a._peer_proxies == {}
    assert a.calls["save"] == 0


@pytest.mark.asyncio
async def test_create_duplicate_same_origin_returns(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    other = FakeProxy(metadata=_pmeta("OSRC"))
    pm = FakePortManager(ports={"p1": other})  # get_port -> None, so no reuse
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd())
    assert pm.registered == []
    assert "Duplicate federated port" in caplog.text


def _pmeta(server_id, stype=None):
    return PortMetadata(
        name="p1",
        original_name="p1",
        description="d",
        adapter_type="remote_muxcon",
        origin_server=_si(server_id, stype),
        server_chain=[],
        status="connected",
        max_rw_users=1.0,
        federation_type=FederationType.PULL,
    )


@pytest.mark.asyncio
async def test_create_duplicate_other_origin_proceeds(monkeypatch):
    other = FakeProxy(metadata=_pmeta("OTHER"))
    pm = FakePortManager(ports={"p1": other})
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd())
    assert len(pm.registered) == 1


@pytest.mark.asyncio
async def test_create_duplicate_name_mismatch_skipped(monkeypatch):
    other = FakeProxy(metadata=_pmeta("OSRC"))
    pm = FakePortManager(ports={"other-port": other})  # name mismatch -> continue
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd())
    assert len(pm.registered) == 1


@pytest.mark.asyncio
async def test_create_duplicate_meta_none_skipped(monkeypatch):
    other = FakeProxy(metadata=None)
    pm = FakePortManager(ports={"p1": other})
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd())
    assert len(pm.registered) == 1


class _RaisingMeta:
    @property
    def metadata(self):
        raise RuntimeError("meta introspection boom")


@pytest.mark.asyncio
async def test_create_duplicate_meta_introspection_raises(monkeypatch):
    pm = FakePortManager(ports={"p1": _RaisingMeta()})
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd())
    # introspection raise is tolerated; registration proceeds
    assert len(pm.registered) == 1


@pytest.mark.asyncio
async def test_create_duplicate_ports_attr_raises(monkeypatch):
    # ports attr raise -> the advisory duplicate check is skipped, register proceeds
    pm = FakePortManager(ports=None, ports_raise=True)
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd())
    assert len(pm.registered) == 1


@pytest.mark.asyncio
async def test_create_register_raising_propagates(monkeypatch):
    pm = FakePortManager()

    async def _boom(metadata, proxy):
        raise RuntimeError("register boom")

    pm.register_federated_port = _boom
    a = _adapter(monkeypatch, pm=pm)
    with pytest.raises(RuntimeError):
        await _run(a, _pd())


# ---------------------------------------------------------------------------
# Server-chain build
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chain_v2_detailed_dedup_skip_relays(monkeypatch):
    pm = FakePortManager()
    a = _adapter(monkeypatch, pm=pm)
    pd = _pd(origin=_origin("OSRC"))
    pd["server_chain_info"] = [
        {"server_id": "OSRC"},  # origin already at index 0 -> skipped
        "not-a-dict",  # skipped
        {"server_id": "R1", "server_type": "relay", "hostname": "r1", "port": 5, "description": "d1"},
        {"server_id": "R2"},
    ]
    await _run(a, pd)
    chain = pm.registered[0][0].server_chain
    assert [s.server_id for s in chain] == ["OSRC", "R1", "R2"]
    assert chain[0].server_type is ServerType.LEAF
    assert chain[1].server_type is ServerType.RELAY
    assert chain[1].hostname == "r1"
    assert chain[1].port == 5
    assert chain[1].description == "d1"
    assert chain[2].hostname == "remote"
    assert chain[2].port == 0


@pytest.mark.asyncio
async def test_chain_v2_bad_enum_fallback_relay(monkeypatch):
    pm = FakePortManager()
    a = _adapter(monkeypatch, pm=pm)
    pd = _pd(origin=_origin("OSRC"))
    pd["server_chain_info"] = [{"server_id": "R1", "server_type": "garbage"}]
    await _run(a, pd)
    chain = pm.registered[0][0].server_chain
    assert [s.server_id for s in chain] == ["OSRC", "R1"]
    assert chain[1].server_type is ServerType.RELAY


@pytest.mark.asyncio
async def test_chain_v2_bad_port_outer_except(monkeypatch):
    pm = FakePortManager()
    a = _adapter(monkeypatch, pm=pm)
    pd = _pd(origin=_origin("OSRC"))
    pd["server_chain_info"] = [{"server_id": "R1", "port": "abc"}]  # int('abc') raises -> outer except
    await _run(a, pd)
    chain = pm.registered[0][0].server_chain
    assert [s.server_id for s in chain] == ["OSRC"]  # whole hop loop aborted


@pytest.mark.asyncio
async def test_chain_legacy_string_ids(monkeypatch):
    pm = FakePortManager()
    a = _adapter(monkeypatch, pm=pm)
    pd = _pd(origin=_origin("OSRC"))
    pd["server_chain_info"] = []
    pd["server_chain"] = ["OSRC", "X1", "X2"]
    await _run(a, pd)
    chain = pm.registered[0][0].server_chain
    assert [s.server_id for s in chain] == ["OSRC", "X1", "X2"]
    assert chain[1].server_type is ServerType.RELAY
    assert chain[1].hostname == "remote"


@pytest.mark.asyncio
async def test_chain_legacy_bad_value_outer_except(monkeypatch):
    pm = FakePortManager()
    a = _adapter(monkeypatch, pm=pm)
    pd = _pd(origin=_origin("OSRC"))
    pd["server_chain_info"] = None
    pd["server_chain"] = 42  # iterating an int raises -> legacy outer except
    await _run(a, pd)
    chain = pm.registered[0][0].server_chain
    assert [s.server_id for s in chain] == ["OSRC"]


# ---------------------------------------------------------------------------
# serial_config / line_status except arms
# ---------------------------------------------------------------------------
class _RaisingGet(dict):
    def get(self, *args, **kwargs):
        raise RuntimeError("get boom")


@pytest.mark.asyncio
async def test_serial_config_get_raises(monkeypatch):
    pm = FakePortManager()
    a = _adapter(monkeypatch, pm=pm)
    pd = _pd()
    pd["serial_config"] = _RaisingGet()  # isinstance dict; .get raises -> except -> None
    await _run(a, pd)
    assert pm.registered[0][0].serial_config is None


# ---------------------------------------------------------------------------
# Power feeds normalization
# ---------------------------------------------------------------------------
class _RaisingLineGet(dict):
    def get(self, key, *args, **kwargs):
        if key == "line_status":
            raise RuntimeError("line_status get boom")
        return super().get(key)


@pytest.mark.asyncio
async def test_line_status_get_raises_swallowed(monkeypatch):
    pm = FakePortManager()
    a = _adapter(monkeypatch, pm=pm)
    pd = _RaisingLineGet(_pd())
    await _run(a, pd)
    assert pm.registered[0][0].line_status is None


@pytest.mark.asyncio
async def test_power_bare_string_then_bad_dict(monkeypatch):
    # bare string ref is qualified; a following dict with a bad ref is skipped
    pm = FakePortManager()
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd(power=["pdu.1", {"ref": 123, "on": True}]))
    assert pm.registered[0][0].power == [{"ref": "OSRC::pdu.1", "on": None}]


@pytest.mark.asyncio
async def test_power_non_str_non_string_entry_dropped(monkeypatch):
    # only a non-dict / non-string entry -> everything dropped -> power is None
    pm = FakePortManager()
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd(power=[5]))
    assert pm.registered[0][0].power is None


@pytest.mark.asyncio
async def _power_result(monkeypatch, power):
    pm = FakePortManager()
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd(power=power))
    return pm.registered[0][0].power


@pytest.mark.asyncio
async def test_power_feeds_dict_valid(monkeypatch):
    assert await _power_result(monkeypatch, [{"ref": "outlet.a", "on": True}]) == [{"ref": "OSRC::outlet.a", "on": True}]
    assert await _power_result(monkeypatch, [{"ref": "outlet.a", "on": False}]) == [{"ref": "OSRC::outlet.a", "on": False}]


@pytest.mark.asyncio
async def test_power_feeds_on_coerced(monkeypatch):
    assert await _power_result(monkeypatch, [{"ref": "outlet.a", "on": "bogus"}]) == [{"ref": "OSRC::outlet.a", "on": None}]
    assert await _power_result(monkeypatch, [{"ref": "outlet.a"}]) == [{"ref": "OSRC::outlet.a", "on": None}]


@pytest.mark.asyncio
async def test_power_feeds_bare_string(monkeypatch):
    assert await _power_result(monkeypatch, ["pdu.1"]) == [{"ref": "OSRC::pdu.1", "on": None}]


@pytest.mark.asyncio
async def test_power_feeds_already_prefixed_kept(monkeypatch):
    # a well-formed remote ref (origin::<pdu>.<id>) is kept as-is
    assert await _power_result(monkeypatch, [{"ref": "OTHER::pdu.1", "on": True}]) == [{"ref": "OTHER::pdu.1", "on": True}]


@pytest.mark.asyncio
async def test_power_feeds_malformed_dropped(monkeypatch):
    # 5 (int), "" (empty str), {"on":1} (no ref) all dropped -> None
    assert await _power_result(monkeypatch, [5, "", {"on": 1}]) is None
    assert await _power_result(monkeypatch, [{"ref": 123, "on": True}]) is None  # non-str ref


@pytest.mark.asyncio
async def test_power_feeds_absent(monkeypatch):
    assert await _power_result(monkeypatch, None) is None
    assert await _power_result(monkeypatch, []) is None


# ---------------------------------------------------------------------------
# Reuse path (existing proxy found by name)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reuse_updates_in_place(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    ex = FakeProxy()
    pm = FakePortManager(get_result=ex)
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd(origin=_origin("OSRC2")))

    # no FRESH register; the only register call is the data-callback re-register
    assert [p for (m, p) in pm.registered] == [ex]
    assert ex.adapter is a
    assert ex.connection_id == "PK"
    assert ex.metadata is not None
    assert ex.metadata.origin_server.server_id == "OSRC2"
    assert ex.server_adapter is a
    assert ex.is_connected is True
    assert ex.link_reason == ""
    assert ex.max_read_write_users == wire_to_mode(1.0)
    # best-effort collaborators exercised
    assert a.calls["stale"] == 1
    assert a.calls["power"] == 1
    assert a.calls["regrant"] == 1
    assert ex._closed is True
    assert a.calls["refresh"] == 1
    assert a.notice_calls == [("restored", "OSRC2::p1")]
    assert a.calls["save"] == 1
    assert a._peer_proxies["PK"]["p1"] is ex
    assert "Proxy reuse check failed" not in caplog.text


@pytest.mark.asyncio
async def test_reuse_link_restore_notice_src(monkeypatch):
    ex = FakeProxy()
    pm = FakePortManager(get_result=ex)
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd(origin=_origin("ORIGX")))
    assert a.notice_calls == [("restored", "ORIGX::p1")]


@pytest.mark.asyncio
async def test_reuse_with_data_callback_no_reregister(monkeypatch):
    ex = FakeProxy(missing_callback=False)
    pm = FakePortManager(get_result=ex)
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd())
    assert pm.registered == []
    assert a._peer_proxies["PK"]["p1"] is ex


@pytest.mark.asyncio
async def test_reuse_fallback_to_ports_when_get_port_raises(monkeypatch):
    ex = FakeProxy()
    pm = FakePortManager(ports={"p1": ex}, get_raise=True)
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd())
    assert [p for (m, p) in pm.registered] == [ex]
    assert ex.adapter is a


@pytest.mark.asyncio
async def test_reuse_both_lookups_fail_falls_to_create(monkeypatch):
    pm = FakePortManager(ports=None, get_raise=True, ports_raise=True)
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd())
    assert len(pm.registered) == 1
    assert all(p is not pm for (m, p) in pm.registered)


def test_reuse_without_remote_name_not_reused(monkeypatch):
    # existing lacks the remote_port_name hook -> not a reuse target, create proceeds
    ex = FakeProxy(has_remote_name=False)
    pm = FakePortManager(get_result=ex)  # ports empty so duplicate check won't match
    a = _adapter(monkeypatch, pm=pm)

    async def _go():
        await _run(a, _pd())

    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_go())
    finally:
        loop.close()
    assert len(pm.registered) == 1
    assert all(p is not ex for (m, p) in pm.registered)


@pytest.mark.asyncio
async def test_reuse_reregister_failure_logged(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    ex = FakeProxy(missing_callback=True)

    class _Reg:
        def get_port(self, name):
            return ex

        @property
        def ports(self):
            return {}

        async def register_federated_port(self, metadata, proxy):
            raise RuntimeError("re-register boom")

    a = _adapter(monkeypatch, pm=_Reg())
    await _run(a, _pd())
    assert "Failed to re-register data callback" in caplog.text


# ---------------------------------------------------------------------------
# Reuse in-place update fault tolerance (each best-effort arm)
# ---------------------------------------------------------------------------
def _reuse_pm(ex, **pm_kw):
    pm = FakePortManager(get_result=ex, **pm_kw)
    return pm


@pytest.mark.asyncio
async def test_reuse_adapter_setattr_raises_swallowed(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    ex = FakeProxy(raise_set={"adapter"})
    pm = _reuse_pm(ex)
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd())
    # adapter rebind failed but the in-place update continued -> reused
    assert ex.connection_id == "PK"
    assert ex.is_connected is True
    assert a._peer_proxies["PK"]["p1"] is ex
    assert "Proxy reuse check failed" not in caplog.text


@pytest.mark.asyncio
async def test_reuse_max_rw_set_raises_swallowed(monkeypatch):
    ex = FakeProxy(raise_set={"max_read_write_users"})
    pm = _reuse_pm(ex)
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd())
    # the capacity refresh failed but the update continued
    assert ex.is_connected is True
    assert a._peer_proxies["PK"]["p1"] is ex


@pytest.mark.asyncio
async def test_reuse_power_meta_raises_swallowed(monkeypatch):
    ex = FakeProxy()
    pm = _reuse_pm(ex)
    a = _adapter(monkeypatch, pm=pm)

    async def _boom(proxy, metadata):
        raise RuntimeError("power meta boom")

    a._apply_power_meta_to_proxy = _boom
    await _run(a, _pd())
    assert a._peer_proxies["PK"]["p1"] is ex


@pytest.mark.asyncio
async def test_reuse_link_reason_set_raises_swallowed(monkeypatch):
    ex = FakeProxy(raise_set={"link_reason"})
    pm = _reuse_pm(ex)
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd())
    assert a._peer_proxies["PK"]["p1"] is ex


@pytest.mark.asyncio
async def test_reuse_close_streams_raises_swallowed(monkeypatch):
    ex = FakeProxy(close_exc=RuntimeError("close boom"))
    pm = _reuse_pm(ex)
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd())
    assert a._peer_proxies["PK"]["p1"] is ex


@pytest.mark.asyncio
async def test_reuse_stale_close_raises_swallowed(monkeypatch):
    ex = FakeProxy()
    pm = _reuse_pm(ex)
    a = _adapter(monkeypatch, pm=pm)

    async def _boom(proxy, conn_id):
        raise RuntimeError("stale boom")

    a._close_stale_proxy_sessions = _boom
    await _run(a, _pd())
    assert a._peer_proxies["PK"]["p1"] is ex


@pytest.mark.asyncio
async def test_reuse_notice_raises_swallowed(monkeypatch):
    ex = FakeProxy()
    pm = _reuse_pm(ex)
    a = _adapter(monkeypatch, pm=pm)

    def _boom(proxy, state, src):
        raise RuntimeError("notice boom")

    a._emit_link_notice_once = _boom
    await _run(a, _pd())
    assert a._peer_proxies["PK"]["p1"] is ex


@pytest.mark.asyncio
async def test_reuse_regrant_raises_swallowed(monkeypatch):
    ex = FakeProxy()
    pm = _reuse_pm(ex)
    a = _adapter(monkeypatch, pm=pm)

    async def _boom(proxy, conn_id):
        raise RuntimeError("regrant boom")

    a._regrant_proxy_read_write = _boom
    await _run(a, _pd())
    assert a._peer_proxies["PK"]["p1"] is ex


@pytest.mark.asyncio
async def test_reuse_reopen_streams_records(monkeypatch, caplog):
    ex = FakeProxy()
    ex.connected_clients = [{"client_id": "c9"}, "not-a-dict", {}]
    pm = _reuse_pm(ex)
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd())
    assert ex.reopened == ["c9"]


@pytest.mark.asyncio
async def test_reuse_reopen_streams_raises_swallowed(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    ex = FakeProxy()
    ex.connected_clients = [{"client_id": "c9"}]

    async def _boom(cid):
        raise RuntimeError("open boom")

    ex.open_stream_for_client = _boom
    pm = _reuse_pm(ex)
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd())
    assert a._peer_proxies["PK"]["p1"] is ex
    assert "Failed to reopen streams" in caplog.text


@pytest.mark.asyncio
async def test_reuse_meta_setattr_raises_aborts_to_create(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    ex = FakeProxy(raise_set={"metadata"})
    pm = _reuse_pm(ex)
    a = _adapter(monkeypatch, pm=pm)
    await _run(a, _pd())
    # the un-wrapped metadata set aborts the whole reuse -> fall to create
    assert "Proxy reuse check failed" in caplog.text
    assert len(pm.registered) == 1
    assert all(p is not ex for (m, p) in pm.registered)


@pytest.mark.asyncio
async def test_reuse_refresh_raises_swallowed(monkeypatch, caplog):
    ex = FakeProxy()
    pm = _reuse_pm(ex)
    a = _adapter(monkeypatch, pm=pm)

    def _boom():
        raise RuntimeError("refresh boom")

    a._refresh_conn_proxies = _boom
    await _run(a, _pd())
    assert a._peer_proxies["PK"]["p1"] is ex


# ---------------------------------------------------------------------------
# Advisory except arms (introspection, save-cache, mapping)
# ---------------------------------------------------------------------------
class _BoomMapDict(dict):
    """Mapping whose writes raise, to trigger the advisory outer excepts."""

    def __setitem__(self, *args):
        raise RuntimeError("mapping boom")


def _raise_server_id_prop(monkeypatch):
    # The dataclass __init__ assigns server_id, so the fake property must
    # ACCEPT sets but RAISE on read. Then origin build completes and only
    # the later getattr(origin, "server_id") introspections see the boom.
    def _boom(self):
        raise RuntimeError("server_id boom")

    def _set(self, value):
        object.__setattr__(self, "_sid", value)

    monkeypatch.setattr(ServerInfo, "server_id", property(_boom, _set), raising=False)


@pytest.mark.asyncio
async def test_reuse_src_origin_introspection_raises(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    ex = FakeProxy()
    pm = FakePortManager(get_result=ex)
    a = _adapter(monkeypatch, pm=pm)
    _raise_server_id_prop(monkeypatch)
    await _run(a, _pd())
    # origin introspection failed -> source path falls back to "unknown"
    assert a.notice_calls == [("restored", "unknown::p1")]
    assert a._peer_proxies["PK"]["p1"] is ex


@pytest.mark.asyncio
async def test_reuse_outer_mapping_raises_swallowed(monkeypatch):
    ex = FakeProxy()
    pm = FakePortManager(get_result=ex)
    a = _adapter(monkeypatch, pm=pm)
    boom = _BoomMapDict()
    a._peer_proxies = boom
    await _run(a, _pd())
    # mapping update failed but the reuse itself succeeded
    assert a.calls["save"] == 1
    assert len(boom) == 0


@pytest.mark.asyncio
async def test_reuse_save_cache_raises_logged(monkeypatch, caplog):
    caplog.set_level(logging.ERROR)
    ex = FakeProxy()
    pm = FakePortManager(get_result=ex)
    a = _adapter(monkeypatch, pm=pm)

    def _boom_save():
        raise RuntimeError("save boom")

    monkeypatch.setattr(a, "_save_federated_cache", _boom_save)
    await _run(a, _pd())
    assert "failed to save federated cache" in caplog.text
    assert a._peer_proxies["PK"]["p1"] is ex


@pytest.mark.asyncio
async def test_create_origin_introspection_for_dup_raises(monkeypatch):
    pm = FakePortManager()
    a = _adapter(monkeypatch, pm=pm)
    _raise_server_id_prop(monkeypatch)
    await _run(a, _pd())
    # origin_id fell back to None; registration proceeded and mapped
    assert len(pm.registered) == 1
    assert a._peer_proxies["PK"]["p1"] is pm.registered[0][1]


@pytest.mark.asyncio
async def test_create_inner_refresh_raises_swallowed(monkeypatch):
    pm = FakePortManager()
    a = _adapter(monkeypatch, pm=pm)

    def _boom():
        raise RuntimeError("refresh boom")

    a._refresh_conn_proxies = _boom
    await _run(a, _pd())
    assert len(pm.registered) == 1
    assert a._peer_proxies["PK"]["p1"] is pm.registered[0][1]
    assert a.calls["save"] == 1


@pytest.mark.asyncio
async def test_create_save_cache_raises_logged(monkeypatch, caplog):
    caplog.set_level(logging.ERROR)
    pm = FakePortManager()
    a = _adapter(monkeypatch, pm=pm)

    def _boom_save():
        raise RuntimeError("save boom")

    monkeypatch.setattr(a, "_save_federated_cache", _boom_save)
    await _run(a, _pd())
    assert "failed to save federated cache" in caplog.text
    assert a._peer_proxies["PK"]["p1"] is pm.registered[0][1]


@pytest.mark.asyncio
async def test_create_outer_mapping_raises_logged(monkeypatch, caplog):
    caplog.set_level(logging.ERROR)
    pm = FakePortManager()
    a = _adapter(monkeypatch, pm=pm)
    boom = _BoomMapDict()
    a._peer_proxies = boom
    await _run(a, _pd())
    assert "remote port registration failed" in caplog.text
    assert len(pm.registered) == 1
    assert len(boom) == 0
