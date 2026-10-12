"""Coverage tests for WebConsoleAdapter._gather_federation_overview (sync method).

The method builds the federation overview payload (configured peers, active
muxcon connections with per-conn registered-port metadata, the remote-port
summary enriched with live muxcon metadata, and totals). Tests stub
``adapter._find_muxcon_adapter`` and ``adapter._get_ports_snapshot`` on a real
WebConsoleAdapter and drive the real method against fake muxcon / port data.
``time.time`` is pinned where the per-conn uptime math is asserted.
"""

from types import SimpleNamespace

import openmux.server.web_console as _mod
from openmux.server.access_control import capacity_to_wire
from openmux.server.web_console import WebConsoleAdapter

NOW = 1000.0


def _run(ad, when=NOW):
    real = _mod.time

    class _T:
        @staticmethod
        def time():
            return when

        @staticmethod
        def monotonic():
            return real.monotonic()

    _mod.time = _T
    try:
        return ad._gather_federation_overview()
    finally:
        _mod.time = real


class _Raising:
    def __getattr__(self, name):
        raise RuntimeError("boom " + name)

    def __iter__(self):
        raise RuntimeError("boom iter")


class _Wrap:
    """Delegates to an inner object but makes one attribute raise."""

    def __init__(self, inner, boom_attr):
        self._inner = inner
        self._boom = boom_attr

    def __getattr__(self, name):
        if name == self._boom:
            raise RuntimeError("boom " + name)
        return getattr(self._inner, name)


def _peer(host, port, opts=None):
    return SimpleNamespace(host=host, port=port, options=opts if opts is not None else {"tls": True})


def _meta(origin=None, chain=None, ftype=None, serial=None, line=None):
    return SimpleNamespace(
        origin_server=origin,
        server_chain=chain if chain is not None else [],
        federation_type=ftype,
        serial_config=serial,
        line_status=line,
    )


_DEFAULT = object()


def _proxy(meta=_DEFAULT, status_snap=None, status_raises=False, no_status=False, is_connected=True):
    def _resolve():
        if meta is _DEFAULT:
            return _meta(ftype="direct")
        return meta  # None stays None; a value is used as-is

    class _P:
        def __init__(self):
            self.metadata = _resolve()

        @property
        def is_connected(self):
            return is_connected

        def get_status(self):
            if status_raises:
                raise RuntimeError("boom")
            return status_snap

    if no_status:
        del _P.get_status
    return _P()


def _muxcon(
    server_id="node-1",
    hb=30.0,
    peers=None,
    connections=None,
    hb_state=None,
    conn_proxies=None,
    derive=None,
    smap=None,
    lmap=None,
    retx=None,
):
    return SimpleNamespace(
        server_id=server_id,
        heartbeat_interval=hb,
        peers=peers if peers is not None else [_peer("h1", 9000)],
        connections=connections if connections is not None else {},
        _hb_state=hb_state if hb_state is not None else {},
        _conn_proxies=conn_proxies if conn_proxies is not None else {},
        _derive_peer_key_from_conn_id=derive if derive is not None else (lambda cid: "pk1"),
        _session_map=smap if smap is not None else {"pk1": {"s1": 1, "s2": 1}},
        _local_session_map=lmap if lmap is not None else {"pk1": {"s3": 1}},
        _peer_retx_count=retx if retx is not None else {"p1": 4},
    )


def _conn(role="primary", writer=None, hs=None, opened_at=None, last_seen=None):
    c = {
        "role": role,
        "writer": writer,
        "handshake": hs if hs is not None else {"version": 3, "type": "client", "capabilities": ["x"]},
    }
    if opened_at is not None:
        c["opened_at"] = opened_at
    if last_seen is not None:
        c["last_seen"] = last_seen
    return c


def _wc():
    return WebConsoleAdapter("wc", {"web_console": {"enable_ui": False}})


def _setup(mux=None, ports=None, mpath=None):
    ad = _wc()
    ad._find_muxcon_adapter = lambda: mux
    ad._get_ports_snapshot = lambda: ports if ports is not None else []
    if mpath is not None:
        ad._derive_mpath_info_for_muxcon = lambda mc, cid: mpath
    return ad


REMOTE_PORT = {
    "name": "rp1",
    "adapter_type": "remote_muxcon",
    "state": "idle",
    "server_chain": ["x"],
    "federation_type": "direct",
    "max_read_write_users": capacity_to_wire("one"),
    "client_count": 2,
}


# ------------------------------------------------------------------ no muxcon / outer
def test_no_muxcon():
    ad = _setup(mux=None, ports=[dict(REMOTE_PORT)] + [{"name": "lp", "adapter_type": "loopback"}])
    out = _run(ad)
    assert out["node"] == {"server_id": None, "adapter": "muxcon"}
    assert out["heartbeat_interval_sec"] is None
    assert out["peers_configured"] == []
    assert out["connections"] == []
    assert out["remote_ports"] == [
        {
            "name": "rp1",
            "description": None,
            "connected": False,
            "adapter_type": "remote_muxcon",
            "status": "idle",
            "origin_server": None,
            "server_chain_info": None,
            "server_chain": ["x"],
            "federation_type": "direct",
            "max_rw_users": "one",
            "connected_clients": 2,
            "serial_config": None,
            "line_status": None,
        }
    ]
    assert out["totals"] == {
        "peers_configured": 0,
        "connections_active": 0,
        "connections_total": 0,
        "remote_ports_total": 1,
        "remote_ports_connected": 0,
        "retransmissions": 0,
    }


def test_muxcon_plain_namespace():
    # a truthy adapter with no muxcon attrs at all: every getattr falls to defaults
    ad = _setup(mux=object(), ports=[])
    out = _run(ad)
    assert out["node"] == {"server_id": None, "adapter": "muxcon"}
    assert out["peers_configured"] == []
    assert out["connections"] == []
    assert out["totals"]["retransmissions"] == 0


def test_outer_error():
    # _get_ports_snapshot is wrapped in its OWN try/except, so it cannot reach the
    # outer handler; _find_muxcon_adapter (called first, unguarded) can.
    ad = _setup(mux=None)

    def boom():
        raise RuntimeError("boom")

    ad._find_muxcon_adapter = boom
    out = _run(ad)
    assert out == {"error": True, "message": "boom"}


# ------------------------------------------------------------------ muxcon happy shape
CID = "p:primary:o:100"


def _happy_fixture():
    proxies = {CID: {"rp1": _proxy(status_snap={"status_message": "ok", "readiness": "ready"})}}
    mux = _muxcon(
        connections={
            CID: _conn(
                role="primary", hs={"version": 3, "type": "client", "capabilities": ["x"]}, opened_at=900, last_seen=990
            )
        },
        hb_state={CID: {"last_req_ts": 998.0, "last_ack_ts": 999.0, "rtt_ms": 12.0, "missed": 0}},
        conn_proxies=proxies,
        retx={"p1": 4},
    )
    ad = _setup(mux=mux, ports=[dict(REMOTE_PORT)], mpath={"mpath_group": "g1", "mpath_primary": True, "mpath_paths": 2})
    return ad


def test_happy_shape():
    ad = _happy_fixture()
    out = _run(ad)
    assert out["node"] == {"server_id": "node-1", "adapter": "muxcon"}
    assert out["heartbeat_interval_sec"] == 30.0
    assert "heartbeat_interval" in out["config_note"]
    assert out["peers_configured"] == [{"host": "h1", "port": 9000, "options": {"tls": True}}]
    c = out["connections"][0]
    assert c["connection_id"] == "p:primary:o:100"
    assert c["role"] == "primary"
    assert c["opened_at"] == 900
    assert c["last_seen"] == 990
    assert c["uptime_seconds"] == NOW - 900
    assert c["remote_peer"] is None
    assert c["handshake"] == {
        "version": 3,
        "client_type": "client",
        "capabilities": ["x"],
        "server_id": None,
        "instance_id": None,
    }
    assert c["active"] is True
    assert c["counts"] == {"streams": 3, "ports": 2}
    # NOTE: pre-existing behavior — the same prepped port dict is appended
    # TWICE, so one proxy yields two identical entries. Preserved, not fixed.
    assert len(c["ports_registered"]) == 2
    assert c["ports_registered"][0] is c["ports_registered"][1]
    assert c["heartbeat"] == {
        "interval_sec": 30.0,
        "last_req_ts": 998.0,
        "last_ack_ts": 999.0,
        "rtt_ms": 12.0,
        "missed": 0,
        "status": "ok",
    }
    assert c["mpath_group"] == "g1" and c["mpath_primary"] is True and c["mpath_paths"] == 2
    prepped = c["ports_registered"][0]
    assert prepped["name"] == "rp1"
    assert prepped["adapter_type"] == "remote_muxcon"
    assert prepped["connected"] is True
    assert prepped["origin_server_id"] is None
    assert prepped["server_chain"] == []
    assert prepped["origin_server"] is None
    assert prepped["server_chain_info"] == []
    assert prepped["federation_type"] == "direct"
    assert prepped["max_rw_users"] is None
    assert prepped["status_message"] == "ok"
    assert prepped["readiness"] == "ready"
    port = out["remote_ports"][0]
    assert port["origin_server"] is None
    assert port["server_chain_info"] == []
    assert port["max_rw_users"] == "one"
    assert port["connected_clients"] == 2
    assert out["totals"] == {
        "peers_configured": 1,
        "connections_active": 1,
        "connections_total": 1,
        "remote_ports_total": 1,
        "remote_ports_connected": 0,
        "retransmissions": 4,
    }


def test_peers_raising():
    ad = _setup(mux=_muxcon(peers=_Raising()), ports=[])
    out = _run(ad)
    assert out["peers_configured"] == []
    assert out["totals"]["peers_configured"] == 0


def test_connections_block_raising():
    # _hb_state getattr raises before the inner per-conn try -> whole list skipped
    ad = _setup(mux=_Wrap(_muxcon(connections={"mux-c-id": _conn()}), "_hb_state"), ports=[])
    out = _run(ad)
    assert out["connections"] == []
    assert out["totals"]["connections_total"] == 0


# ------------------------------------------------------------------ per-connection details
def _single_conn(cid="mux-c-id", mux_over=None, **conn_kw):
    ad = _setup(mux=_muxcon(**(mux_over or {}), connections={cid: _conn(**conn_kw)}), ports=[])
    return _run(ad)["connections"][0]


def test_writer_peer():
    writer = SimpleNamespace(get_extra_info=lambda what: ("10.1.2.3", 9999) if what == "peername" else None)
    c = _single_conn(writer=writer)
    assert c["remote_peer"] == {"host": "10.1.2.3", "port": 9999}


def test_writer_no_peername():
    writer = SimpleNamespace(get_extra_info=lambda what: None)
    c = _single_conn(writer=writer)
    assert c["remote_peer"] is None


def test_writer_raising():
    writer = SimpleNamespace(get_extra_info=lambda what: (_ for _ in ()).throw(RuntimeError("boom")))
    c = _single_conn(writer=writer)
    assert c["remote_peer"] is None


def test_cid_tuple_opened_fail():
    # tuple connection id: str().split(":") gives one part -> opened_at stays None
    c = _single_conn(cid=("a", "b"))
    assert c["uptime_seconds"] is None
    assert c["opened_at"] is None


def test_opened_from_cid_suffix():
    c = _single_conn(cid="a:b:c:777")
    assert c["uptime_seconds"] == NOW - 777


def test_cid_non_numeric_suffix():
    # 4-part cid but non-numeric suffix -> int() raises -> opened_at None
    c = _single_conn(cid="a:b:c:junk")
    assert c["uptime_seconds"] is None
    assert c["opened_at"] is None


def test_hs_object_form():
    hs = SimpleNamespace(version=9, client_type=SimpleNamespace(value="telnet"), capabilities=["c"])
    c = _single_conn(hs=hs)
    assert c["handshake"]["version"] == 9
    assert c["handshake"]["client_type"] == "telnet"
    assert c["handshake"]["capabilities"] == ["c"]


def test_hs_object_plain_type():
    hs = SimpleNamespace(version=9, client_type="telnet", capabilities=["c"])
    c = _single_conn(hs=hs)
    assert c["handshake"]["client_type"] == "telnet"


# ------------------------------------------------------------------ connection proxies / metadata
def _prox_entries(proxies):
    ad = _setup(mux=_muxcon(connections={"mux-c-id": _conn()}, conn_proxies=proxies), ports=[])
    return _run(ad)["connections"][0]["ports_registered"]


def test_origin_none_defaults():
    entries = _prox_entries({"mux-c-id": {"rp1": _proxy()}})
    e = entries[0]
    assert e["origin_server"] is None
    assert e["server_chain"] == []
    assert e["server_chain_info"] == []
    assert e["federation_type"] == "direct"


def test_origin_dict_and_chain():
    # an origin WITHOUT to_dict renders as the full 4-key dict; chain entries are
    # read via getattr(server_id), so object entries resolve to their id
    origin = SimpleNamespace(server_id="sX", hostname="hX", port=88, server_type=SimpleNamespace(value="ssh"))
    chain = [SimpleNamespace(server_id="s1"), SimpleNamespace(server_id="s2")]
    e = _prox_entries({"mux-c-id": {"rp1": _proxy(_meta(origin=origin, chain=chain))}})[0]
    assert e["origin_server"] == {"server_id": "sX", "hostname": "hX", "port": 88, "server_type": "ssh"}
    assert e["origin_server_id"] == "sX"
    assert e["server_chain"] == ["s1", "s2"]
    assert e["server_chain_info"] == [{"server_id": "s1"}, {"server_id": "s2"}]


def test_origin_to_dict():
    origin = SimpleNamespace(to_dict=lambda: {"server_id": "sT", "custom": 1})
    e = _prox_entries({"mux-c-id": {"rp1": _proxy(_meta(origin=origin))}})[0]
    assert e["origin_server"] == {"server_id": "sT", "custom": 1}
    assert e["origin_server_id"] is None  # origin object has no server_id attr


def test_origin_to_dict_raising():
    origin = SimpleNamespace(to_dict=lambda: (_ for _ in ()).throw(RuntimeError("boom")), server_id="sE")
    e = _prox_entries({"mux-c-id": {"rp1": _proxy(_meta(origin=origin))}})[0]
    assert e["origin_server"] == {"server_id": "sE"}


def test_chain_to_dict_raising_fallback():
    # a chain entry whose to_dict raises -> the whole chain_info except arm + fallback
    boom = SimpleNamespace(server_id="sC", to_dict=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    e = _prox_entries({"mux-c-id": {"rp1": _proxy(_meta(origin=None, chain=[boom]))}})[0]
    assert e["server_chain"] == ["sC"]
    assert e["server_chain_info"] == [{"server_id": "sC"}]


def test_origin_and_chain_fallbacks():
    # origin without to_dict -> full 4-key dict (with None placeholders); a non-object
    # (int) chain entry is rendered via str() because getattr(42, server_id) -> str(42)
    origin = SimpleNamespace(server_id="sO")
    e = _prox_entries({"mux-c-id": {"rp1": _proxy(_meta(origin=origin, chain=[42]))}})[0]
    assert e["origin_server"] == {"server_id": "sO", "hostname": None, "port": None, "server_type": None}
    assert e["server_chain"] == ["42"]
    assert e["server_chain_info"] == [{"server_id": "42"}]


def test_chain_to_dict_and_ftype_value():
    chain = [SimpleNamespace(server_id="s1", to_dict=lambda: {"server_id": "s1", "extra": "e"})]
    e = _prox_entries({"mux-c-id": {"rp1": _proxy(_meta(origin=None, chain=chain, ftype=SimpleNamespace(value="pooled")))}})[0]
    assert e["origin_server"] is None
    assert e["origin_server_id"] is None
    assert e["server_chain"] == ["s1"]
    assert e["server_chain_info"] == [{"server_id": "s1", "extra": "e"}]
    assert e["federation_type"] == "pooled"


def test_proxy_metadata_none():
    p = _proxy(meta=None, no_status=True)
    e = _prox_entries({"mux-c-id": {"rp1": p}})[0]
    assert e["origin_server"] is None
    assert e["server_chain"] == []
    assert e["federation_type"] is None
    assert e["max_rw_users"] is None
    assert e["serial_config"] is None
    assert e["line_status"] is None
    assert e["connected"] is True


def _p_class(metadata, status_fn=None, raise_metadata=False):
    class _P:
        def __init__(self):
            self.metadata = metadata

        def __getattr__(self, name):
            if name == "is_connected":
                return True
            if name == "get_status":
                return status_fn
            raise AttributeError(name)

    if raise_metadata:

        class _Q:
            @property
            def metadata(self):
                raise RuntimeError("boom")

        return _Q()
    return _P()


def test_proxy_status_fields_off():
    e = _prox_entries({"mux-c-id": {"rp1": _p_class(None, lambda: {"other": 1})}})[0]
    assert "status_message" not in e
    assert "readiness" not in e


def test_proxy_get_status_raising():
    e = _prox_entries({"mux-c-id": {"rp1": _p_class(None, lambda: (_ for _ in ()).throw(RuntimeError("boom")))}})[0]
    assert e["name"] == "rp1"
    assert "status_message" not in e


def test_proxy_get_status_non_dict():
    e = _prox_entries({"mux-c-id": {"rp1": _p_class(None, lambda: "not-a-dict")}})[0]
    assert "status_message" not in e


def test_proxy_without_get_status():
    # no get_status attr -> psnap {} -> keys absent
    e = _prox_entries({"mux-c-id": {"rp1": _proxy(no_status=True)}})[0]
    assert "status_message" not in e
    assert e["connected"] is True


def test_proxy_disconnected():
    e = _prox_entries({"mux-c-id": {"rp1": _proxy(is_connected=False)}})[0]
    assert e["connected"] is False


def test_proxy_metadata_raising():
    # metadata property raises RuntimeError (not AttributeError) -> propagates to the
    # single ports_registered except -> the whole per-conn proxy loop is aborted
    ad = _setup(
        mux=_muxcon(
            connections={"mux-c-id": _conn()}, conn_proxies={"mux-c-id": {"rp1": _p_class(None, raise_metadata=True)}}
        ),
        ports=[],
    )
    c = _run(ad)["connections"][0]
    assert c["counts"]["ports"] == 0
    assert c["ports_registered"] == []
    assert c["counts"]["streams"] == 3


def test_counts_derive_raising():
    def boom(cid):
        raise RuntimeError("boom")

    c = _single_conn(mux_over={"derive": boom})
    assert c["counts"]["streams"] == 0


# ------------------------------------------------------------------ heartbeat view
def _hb_status(hb_entry):
    ad = _setup(mux=_muxcon(connections={"mux-c-id": _conn()}, hb_state={"mux-c-id": hb_entry}), ports=[])
    return _run(ad)["connections"][0]["heartbeat"]["status"]


def test_hb_degraded():
    assert _hb_status({"missed": 1}) == "degraded"


def test_hb_empty():
    assert _hb_status({}) is None


# ------------------------------------------------------------------ remote port summary enrichment
def _ports_only(ports, mux=None):
    ad = _setup(mux=mux if mux is not None else _muxcon(), ports=ports)
    return _run(ad)["remote_ports"]


def test_port_not_muxcon_skipped():
    assert _ports_only([{"name": "lp", "adapter_type": "loopback"}, {"name": "lc", "adapter": "loopback2"}]) == []


def test_ports_snapshot_raising():
    # _get_ports_snapshot has its own try/except -> data_ports [] ; no outer error
    ad = _setup(mux=_muxcon(), ports=None)
    ad._get_ports_snapshot = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    out = _run(ad)
    assert "error" not in out
    assert out["remote_ports"] == []
    assert out["totals"]["remote_ports_total"] == 0


def test_port_status_dict_and_connected():
    ports = [dict(REMOTE_PORT, adapter_status={"status": "busy"}, connected=True)]
    out = _ports_only(ports)[0]
    assert out["status"] == "busy"
    assert out["connected"] is True
    assert out["max_rw_users"] == "one"


def test_port_status_dict_bad_type():
    ports = [dict(REMOTE_PORT, adapter_status="not-a-dict")]
    assert _ports_only(ports)[0]["status"] == "idle"  # falls back to state


def test_port_legacy_fields_and_live_meta_wins():
    # legacy port keys (adapter / is_running / max_rw_users int / connected_clients)
    # plus live muxcon-side meta for rp1 (serial None, line None) — live meta wins
    # UNCONDITIONALLY, even when its values are None.
    proxies = {"mux-c-id": {"rp1": _proxy(status_snap=None, no_status=True)}}
    mux = _muxcon(connections={"mux-c-id": _conn()}, conn_proxies=proxies)
    ports = [
        {
            "name": "rp1",
            "adapter": "remote_muxcon",
            "is_running": True,
            "state": "up",
            "max_rw_users": 2,
            "connected_clients": 5,
            "serial_config": "port-say",
            "line_status": "port-ls",
        }
    ]
    out = _ports_only(ports, mux=mux)[0]
    assert out["connected"] is True
    assert out["status"] == "up"
    assert out["max_rw_users"] == "multiple"
    assert out["connected_clients"] == 5
    assert out["serial_config"] is None
    assert out["line_status"] is None


def test_port_max_rw_bogus_label():
    ports = [dict(REMOTE_PORT, max_read_write_users="bogus")]
    assert _ports_only(ports)[0]["max_rw_users"] == "bogus"


# ------------------------------------------------------------------ retransmission totals
def test_retx_values():
    ad = _setup(mux=_muxcon(retx={"a": 1, "b": 2}), ports=[])
    assert _run(ad)["totals"]["retransmissions"] == 3


def test_retx_raising():
    ad = _setup(mux=_muxcon(retx=_Raising()), ports=[])
    assert _run(ad)["totals"]["retransmissions"] == 0
