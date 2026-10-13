"""Behavior + coverage tests for WebStatusAdapter._api_get_federation.

`_api_get_federation` was only ~58% line-covered (122 of 291 lines missing).
These tests drive the real method against a real WebStatusAdapter whose
`main_port_manager` is a fake exposing unified adapters, and whose `_send_json`
is an AsyncMock, pinning the outcome of every branch: muxcon discovery,
peers enumeration, per-connection enrichment (peer from writer conn / from
conn_id, handshake dict vs object, origin/chain metadata to_dict vs attrs,
streams counts, heartbeat view, multipath merge), remote-port enrichment
(live meta vs legacy fallback), retransmission totals, and the 500 fallback.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from openmux.server.adapters.web_status import WebStatusAdapter


def _adapter(pm):
    a = WebStatusAdapter("ws", {"port": 9999})
    a._send_json = AsyncMock()
    a.main_port_manager = pm
    return a


def _sent(a):
    args, _ = a._send_json.await_args
    _, status, payload = args
    return status, payload


def _enum(value):
    return type("V", (), {"value": value})()


class FakeWriter:
    def __init__(self, peername=("10.0.0.1", 1234), raise_peername=False):
        self._peer = peername
        self._raise = raise_peername

    def get_extra_info(self, name):
        if self._raise:
            raise RuntimeError("peername boom")
        return self._peer if name == "peername" else None


class Mux:
    """Configurable fake muxcon adapter; pass an Exception as a value to make
    the corresponding attribute/property raise when accessed."""

    def __init__(self, **kw):
        self.server_id = kw.get("server_id", "node-1")
        self.heartbeat_interval = kw.get("heartbeat_interval", 30)
        self._conns = kw.get("connections", {})
        self._hb = kw.get("hb_state", {})
        self._conn_proxies_attr = kw.get("conn_proxies", {})
        self._session_map = kw.get("session_map", {})
        self._local_session_map = kw.get("local_session_map", {})
        self._peer_retx_attr = kw.get("retx", {})
        self._mpath_groups = kw.get("mpath_groups", {})
        if "peers" in kw:
            self.peers = kw["peers"]
        if "derive_peer_key" in kw:
            self._derive_peer_key_from_conn_id = kw["derive_peer_key"]

    def get_adapter_type(self):
        return "muxcon"

    @property
    def connections(self):
        if isinstance(self._conns, Exception):
            raise self._conns
        return self._conns

    @property
    def _hb_state(self):
        if isinstance(self._hb, Exception):
            raise self._hb
        return self._hb

    @property
    def _conn_proxies(self):
        v = self._conn_proxies_attr
        if isinstance(v, Exception):
            raise v
        return v

    @property
    def _peer_retx_count(self):
        if isinstance(self._peer_retx_attr, Exception):
            raise self._peer_retx_attr
        return self._peer_retx_attr


class FakeMeta:
    def __init__(self, origin_server=None, server_chain=None, federation_type=None, max_rw_users=None):
        self.origin_server = origin_server
        self.server_chain = server_chain if server_chain is not None else []
        self.federation_type = federation_type
        self.max_rw_users = max_rw_users


class FakeProxy:
    def __init__(self, metadata=None, is_connected=True):
        self.metadata = metadata
        self.is_connected = is_connected


class DtoObj:
    """Origin/server-chain entry exposed via ``to_dict()``; carries no plain
    ``server_id`` attr (so the V1-compat ``origin_server_id`` resolves to None
    for it). Falls back to ``str()`` for chain-id rendering."""

    def __init__(self, **dto):
        self._dto = dto

    def __str__(self):
        return str(self._dto.get("server_id"))

    def to_dict(self):
        return self._dto


class AttrObj:
    """Origin/server-chain entry exposed via plain attrs (no ``to_dict``)."""

    def __init__(self, server_id, hostname=None, port=None, server_type=None):
        self.server_id = server_id
        self.hostname = hostname
        self.port = port
        self.server_type = server_type


def _conn(writer=None, handshake=None, opened_at_key=None, **extra):
    c = {
        "role": "local",
        "writer": writer,
        "handshake": handshake if handshake is not None else {"version": 2, "type": "client", "capabilities": ["a"]},
    }
    if opened_at_key is not None:
        c["opened_at"] = opened_at_key
    c.update(extra)
    return c


class PM:
    def __init__(self, adapters=None, ports=None, raise_ports=False):
        self.unified_adapters = adapters or []
        self._ports = ports or []
        self._raise = raise_ports

    async def get_port_list_with_federation(self):
        if self._raise:
            raise RuntimeError("list boom")
        return self._ports


class OtherAdapter:
    def get_adapter_type(self):
        return "loopback"


@pytest.mark.asyncio
async def test_no_port_manager_empty_payload():
    a = _adapter(None)
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    assert status == 200
    assert payload["node"]["server_id"] is None
    assert payload["peers_configured"] == []
    assert payload["connections"] == []
    assert payload["remote_ports"] == []
    assert payload["totals"]["retransmissions"] == 0


@pytest.mark.asyncio
async def test_no_muxcon_finds_nothing():
    a = _adapter(PM(adapters=[]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    assert status == 200
    assert payload["node"]["server_id"] is None
    assert payload["totals"]["connections_total"] == 0


@pytest.mark.asyncio
async def test_muxcon_identity_and_peers():
    peers = [type("P", (), {"host": "h1", "port": 8443, "options": {"tls": True}})()]
    m = Mux(server_id="n7", heartbeat_interval=42, peers=peers)
    a = _adapter(PM(adapters=[OtherAdapter(), m]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    assert payload["node"]["server_id"] == "n7"
    assert payload["heartbeat_interval_sec"] == 42
    assert payload["peers_configured"] == [{"host": "h1", "port": 8443, "options": {"tls": True}}]
    assert payload["totals"]["peers_configured"] == 1


class BoomPeers(Mux):
    @property
    def peers(self):
        raise RuntimeError("peers boom")


@pytest.mark.asyncio
async def test_peers_enumeration_error_ignored():
    m = BoomPeers()
    a = _adapter(PM(adapters=[m]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    assert status == 200
    assert payload["peers_configured"] == []


@pytest.mark.asyncio
async def test_conn_peer_from_writer_and_heartbeat_ok():
    m = Mux(
        connections={"cid1": _conn(writer=FakeWriter(("10.9.8.7", 80)))},
        hb_state={"cid1": {"last_req_ts": 1, "last_ack_ts": 2, "rtt_ms": 5, "missed": 0}},
    )
    a = _adapter(PM(adapters=[m]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    conn = payload["connections"][0]
    assert conn["remote_peer"] == {"host": "10.9.8.7", "port": 80}
    assert conn["uptime_seconds"] is None  # no opened_at available
    assert conn["heartbeat"]["status"] == "ok"
    assert conn["heartbeat"]["rtt_ms"] == 5
    assert conn["counts"] == {"streams": 0, "ports": 0}
    assert payload["totals"]["connections_active"] == 1


@pytest.mark.asyncio
async def test_conn_peername_error_yields_none_peer():
    m = Mux(connections={"cid1": _conn(writer=FakeWriter(raise_peername=True))})
    a = _adapter(PM(adapters=[m]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    assert payload["connections"][0]["remote_peer"] is None
    assert payload["connections"][0]["heartbeat"]["status"] is None  # no hb_state entry


@pytest.mark.asyncio
async def test_conn_outgoing_peer_and_opened_at_from_cid():
    # conn_id format: out:host:port:xyz:ts -> peer from parts 1:2, opened_at last part
    m = Mux(connections={"out:10.1.1.1:9000:abc:1800000000": _conn()})
    a = _adapter(PM(adapters=[m]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    conn = payload["connections"][0]
    assert conn["remote_peer"] == {"host": "10.1.1.1", "port": 9000}
    assert conn["opened_at"] == 1800000000
    assert conn["last_seen"] == 1800000000  # falls back to opened_at
    assert conn["uptime_seconds"] is not None


@pytest.mark.asyncio
async def test_conn_opened_at_cid_last_part_not_int():
    m = Mux(connections={"out:h1:1234:notanumber": _conn()})
    a = _adapter(PM(adapters=[m]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    conn = payload["connections"][0]
    assert conn["remote_peer"] == {"host": "h1", "port": 1234}
    assert conn["opened_at"] is None
    assert conn["uptime_seconds"] is None


@pytest.mark.asyncio
async def test_handshake_object_with_enum_client_type():
    hs = type("HS", (), {"version": 3, "client_type": _enum("telnet"), "capabilities": []})()
    m = Mux(connections={"cid1": _conn(writer=None, handshake=hs)})
    a = _adapter(PM(adapters=[m]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    assert payload["connections"][0]["handshake"]["client_type"] == "telnet"
    assert payload["connections"][0]["handshake"]["version"] == 3


@pytest.mark.asyncio
async def test_outgoing_peer_derivation_error_ignored():
    m = Mux(connections={"out:10.1.1.1:notaport:abc:123": _conn()})
    a = _adapter(PM(adapters=[m]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    assert payload["connections"][0]["remote_peer"] is None  # int('notaport') failed
    assert payload["connections"][0]["opened_at"] == 123


@pytest.mark.asyncio
async def test_proxies_metadata_enrichment_to_dict():
    meta = FakeMeta(
        origin_server=DtoObj(server_id="o1", hostname="o.h", port=1, server_type="hub"),
        server_chain=[DtoObj(server_id="c1"), DtoObj(server_id="c2")],
        federation_type=_enum("full"),
        max_rw_users=2,
    )
    proxy = FakeProxy(metadata=meta)
    m = Mux(connections={"cid1": _conn()}, conn_proxies={"cid1": {"p1": proxy}})
    a = _adapter(PM(adapters=[m]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    pr = payload["connections"][0]["ports_registered"][0]
    assert pr["name"] == "p1"
    assert pr["origin_server"] == {"server_id": "o1", "hostname": "o.h", "port": 1, "server_type": "hub"}
    assert pr["server_chain"] == ["c1", "c2"]
    assert pr["server_chain_info"] == [{"server_id": "c1"}, {"server_id": "c2"}]
    assert pr["federation_type"] == "full"
    assert pr["max_rw_users"] == 2
    # V1 compat origin attr comes from the object; to_dict origins expose no attrs
    assert pr["origin_server_id"] is None
    assert payload["connections"][0]["counts"]["ports"] == 1


@pytest.mark.asyncio
async def test_origin_attrs_without_to_dict():
    meta = FakeMeta(origin_server=AttrObj("o2", hostname="h2", port=9, server_type=_enum("leaf")))
    proxy = FakeProxy(metadata=meta)
    m = Mux(connections={"cid1": _conn()}, conn_proxies={"cid1": {"p1": proxy}})
    a = _adapter(PM(adapters=[m]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    pr = payload["connections"][0]["ports_registered"][0]
    assert pr["origin_server"] == {"server_id": "o2", "hostname": "h2", "port": 9, "server_type": "leaf"}
    assert pr["origin_server_id"] == "o2"


@pytest.mark.asyncio
async def test_chain_fallback_and_int_entries():
    # mixed-object / non-object chain entries: ids via getattr(..., str(s))
    chain = [AttrObj("cA"), 42]
    meta = FakeMeta(origin_server=None, server_chain=chain)
    proxy = FakeProxy(metadata=meta)
    m = Mux(connections={"cid1": _conn()}, conn_proxies={"cid1": {"p1": proxy}})
    a = _adapter(PM(adapters=[m]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    pr = payload["connections"][0]["ports_registered"][0]
    assert pr["server_chain"] == ["cA", "42"]
    assert pr["server_chain_info"] == [{"server_id": "cA"}, {"server_id": "42"}]
    assert pr["origin_server"] is None
    assert pr["origin_server_id"] is None


def _to_dict_boom():
    raise RuntimeError("to_dict boom")


@pytest.mark.asyncio
async def test_origin_to_dict_failure_fallback():
    # origin exposes server_id (V1 compat) but its to_dict() blows up
    o = AttrObj("o9")
    o.to_dict = _to_dict_boom
    meta = FakeMeta(origin_server=o)
    m = Mux(connections={"cid1": _conn()}, conn_proxies={"cid1": {"p1": FakeProxy(metadata=meta)}})
    a = _adapter(PM(adapters=[m]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    pr = payload["connections"][0]["ports_registered"][0]
    assert pr["origin_server"] == {"server_id": "o9"}
    assert pr["origin_server_id"] == "o9"


@pytest.mark.asyncio
async def test_chain_to_dict_failure_fallback():
    s = DtoObj(server_id="cX")
    s.to_dict = _to_dict_boom
    chain = [AttrObj("cA"), s]
    meta = FakeMeta(origin_server=None, server_chain=chain)
    m = Mux(connections={"cid1": _conn()}, conn_proxies={"cid1": {"p1": FakeProxy(metadata=meta)}})
    a = _adapter(PM(adapters=[m]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    pr = payload["connections"][0]["ports_registered"][0]
    # handler rebuilt both entries as {"server_id": sid} from the precomputed ids
    assert pr["server_chain"] == ["cA", "cX"]
    assert pr["server_chain_info"] == [{"server_id": "cA"}, {"server_id": "cX"}]


class BoomProxies(Mux):
    @property
    def _conn_proxies(self):
        raise RuntimeError("proxies boom")


@pytest.mark.asyncio
async def test_proxies_listing_error_skipped():
    m = BoomProxies(connections={"cid1": _conn()})
    a = _adapter(PM(adapters=[m]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    assert payload["connections"][0]["ports_registered"] == []
    assert payload["connections"][0]["counts"]["ports"] == 0


def _derive_key(cid):
    return f"pk:{cid}"


@pytest.mark.asyncio
async def test_streams_count_from_session_maps():
    m = Mux(
        connections={"cid1": _conn()},
        derive_peer_key=_derive_key,
        session_map={"pk:cid1": {"s1": 1}},
        local_session_map={"pk:cid1": {"s2": 1, "s3": 1}},
    )
    a = _adapter(PM(adapters=[m]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    assert payload["connections"][0]["counts"]["streams"] == 3


class BoomDerive:
    def get_adapter_type(self):
        return "muxcon"

    server_id = "n"
    heartbeat_interval = None
    connections = {"cid1": _conn()}

    def _derive_peer_key_from_conn_id(self, cid):
        raise RuntimeError("derive boom")


@pytest.mark.asyncio
async def test_streams_count_error_defaults_zero():
    a = _adapter(PM(adapters=[BoomDerive()]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    assert payload["connections"][0]["counts"]["streams"] == 0


@pytest.mark.asyncio
async def test_heartbeat_degraded():
    m = Mux(
        connections={"cid1": _conn()}, hb_state={"cid1": {"missed": 2, "last_req_ts": 9, "last_ack_ts": None, "rtt_ms": None}}
    )
    a = _adapter(PM(adapters=[m]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    assert payload["connections"][0]["heartbeat"]["status"] == "degraded"


@pytest.mark.asyncio
async def test_mpath_info_merged():
    m = Mux(connections={"cid1": _conn()}, mpath_groups={"g1": {"primary": "cid0", "conns": {"cid0": 1, "cid1": 1}}})
    a = _adapter(PM(adapters=[m]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    conn = payload["connections"][0]
    assert conn["mpath_group"] == "g1"
    assert conn["mpath_primary"] is False
    assert conn["mpath_paths"] == 2


class BoomConns(Mux):
    @property
    def connections(self):
        raise RuntimeError("conns boom")


@pytest.mark.asyncio
async def test_connections_property_error_yields_empty():
    m = BoomConns()
    m._conns = None
    a = _adapter(PM(adapters=[m]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    assert payload["connections"] == []


REMOTE_PORT = {
    "name": "pA",
    "description": "d",
    "adapter_type": "remote_muxcon",
    "connected": True,
    "adapter_status": {"status": "up"},
    "server_chain": ["c1"],
    "federation_type": "full",
    "max_read_write_users": 2,
    "client_count": 3,
}


@pytest.mark.asyncio
async def test_remote_port_live_meta_and_status():
    # live meta path: the port name matches a port registered on a connection
    meta = FakeMeta(origin_server=DtoObj(server_id="live"), server_chain=[])
    m = Mux(connections={"cid1": _conn()}, conn_proxies={"cid1": {"pA": FakeProxy(metadata=meta)}})
    a = _adapter(PM(adapters=[m], ports=[dict(REMOTE_PORT)]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    r = payload["remote_ports"][0]
    assert r["origin_server"] == {"server_id": "live"}
    assert r["status"] == "up"
    assert r["max_rw_users"] == "multiple"  # capacity_display_label(2)
    assert r["connected_clients"] == 3
    assert r["connected"] is True


@pytest.mark.asyncio
async def test_remote_port_legacy_fallback():
    # no live meta (port name not seen on any connection) -> legacy origin fields
    m = Mux(connections={"cid1": _conn()})
    p = {
        "name": "pB",
        "adapter_type": "remote_muxcon",
        "is_running": True,
        "state": "down",
        "origin_server_id": "lgc",
        "origin_server_hostname": "lg.h",
        "origin_server_port": 5,
        "origin_server_type": "hub",
        "max_rw_users": "one",
        "connected_clients": 1,
    }
    a = _adapter(PM(adapters=[m], ports=[p]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    r = payload["remote_ports"][0]
    assert r["origin_server"] == {"server_id": "lgc", "hostname": "lg.h", "port": 5, "server_type": "hub"}
    assert r["status"] == "down"
    assert r["max_rw_users"] == "one"
    assert r["connected"] is True  # from is_running


@pytest.mark.asyncio
async def test_non_remote_muxcon_port_skipped():
    m = Mux(connections={})
    a = _adapter(PM(adapters=[m], ports=[{"name": "x", "adapter_type": "serial"}]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    assert payload["remote_ports"] == []


@pytest.mark.asyncio
async def test_port_list_error_defaults_empty():
    m = Mux(connections={})
    a = _adapter(PM(adapters=[m], raise_ports=True))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    assert payload["remote_ports"] == []


@pytest.mark.asyncio
async def test_retransmission_totals():
    m = Mux(connections={}, retx={"a": 3, "b": 2})
    a = _adapter(PM(adapters=[m]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    assert payload["totals"]["retransmissions"] == 5


class BoomRetx:
    def get_adapter_type(self):
        return "muxcon"

    server_id = "n"
    heartbeat_interval = None
    connections = {}

    @property
    def _peer_retx_count(self):
        raise RuntimeError("retx boom")


@pytest.mark.asyncio
async def test_retransmission_error_defaults_zero():
    a = _adapter(PM(adapters=[BoomRetx()]))
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    assert payload["totals"]["retransmissions"] == 0


class BoomPM:
    @property
    def unified_adapters(self):
        raise RuntimeError("pm boom")


@pytest.mark.asyncio
async def test_top_level_error_500():
    a = _adapter(BoomPM())
    await a._api_get_federation(MagicMock())
    status, payload = _sent(a)
    assert status == 500
    assert payload["error"] is True
    a._send_json.assert_awaited_once()
