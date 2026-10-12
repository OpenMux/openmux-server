"""Coverage tests for WebConsoleAdapter._gather_multipath_overview (sync method).

The method summarizes multipath groups from a muxcon adapter (heartbeat-aware
stale detection + optional per-peer metrics). All tests stub only
``adapter._find_muxcon_adapter`` and drive the real method against fake
muxcon adapters; ``time.time`` is pinned where stale math is asserted.
"""

from types import SimpleNamespace

import openmux.server.web_console as _mod
from openmux.server.web_console import WebConsoleAdapter

NOW = 1000.0


def wc():
    return WebConsoleAdapter("wc", {"web_console": {"enable_ui": False}})


def mux(**over):
    d = {
        "groups": {},
        "hb": 20.0,
        "stale": 30.0,
        "conns": {},
        "hb_state": {},
        "sendbuf": {},
        "rx": {},
        "retx": {},
        "tx": {},
        "rxb": {},
        "failover": 2.0,
        "strategy": "failover",
        "preempt": True,
    }
    d.update(over)
    return SimpleNamespace(
        _mpath_groups=d["groups"],
        heartbeat_interval=d["hb"],
        mpath_primary_stale_sec=d["stale"],
        connections=d["conns"],
        _heartbeat_state=d["hb_state"],
        _peer_sendbuf=d["sendbuf"],
        _peer_rx_state=d["rx"],
        _peer_retx_count=d["retx"],
        _peer_bytes_tx=d["tx"],
        _peer_bytes_rx=d["rxb"],
        mpath_failover_check_sec=d["failover"],
        mpath_strategy=d["strategy"],
        mpath_preemptive_promote=d["preempt"],
    )


def conn(last_seen, opened_at=None, pref=50):
    return {"opened_at": opened_at, "last_seen": last_seen, "pref": pref}


def grp(primary, conns):
    return {"primary": primary, "conns": conns}


def run_patch(ad, mk, when=NOW):
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
        return ad._gather_multipath_overview()
    finally:
        _mod.time = real


class _Wrap:
    """Delegates to an inner SimpleNamespace but makes one attribute raise."""

    def __init__(self, inner, boom_attr):
        self._inner = inner
        self._boom = boom_attr

    def __getattr__(self, name):
        if name == self._boom:
            raise RuntimeError("boom " + name)
        return getattr(self._inner, name)


def _boom_once_mux(real_base, boom_attr, **kw):
    """A mux whose boom_attr raises on the FIRST read, then returns the real value.

    That lets the per-block except fallback fire while the final config read
    (same attribute) still succeeds.
    """
    inner = mux(stale=real_base, **kw)
    state = {"boomed": False}

    class _W:
        def __init__(self, inner):
            self._i = inner

        def __getattr__(self, name):
            if name == boom_attr and not state["boomed"]:
                state["boomed"] = True
                raise RuntimeError("boom " + name)
            return getattr(self._i, name)

    return _W(inner)


# ---------------------------------------------------------------- full shape
def test_basic_shape():
    m = mux(
        groups={"p1": grp("c1", {"c1": conn(999.0, 950.0, 100), "c2": conn(998.0, 960.0, 50)}), "p2": grp(None, {})},
        conns={"c1": {"server_id": "sx", "instance_id": "i1"}, "c2": {"server_id": "sy"}},
        retx={"p1": 2},
        tx={"p1": 10},
        rxb={"p1": 11},
        sendbuf={"p1": {"k1": 1, "k2": 2}},
        rx={"p1": {"buffer": {"b": 1}}},
    )
    ad = wc()
    ad._find_muxcon_adapter = lambda: m
    out = run_patch(ad, m)
    assert out["timestamp"] == NOW
    assert len(out["groups"]) == 2
    g1 = [g for g in out["groups"] if g["peer_key"] == "p1"][0]
    assert g1["primary"] == "c1"
    assert g1["primary_pref"] == 100
    assert [c["conn_id"] for c in g1["connections"]] == ["c1", "c2"]
    c1 = g1["connections"][0]
    assert c1["age_sec"] == NOW - 950.0
    assert c1["idle_sec"] == NOW - 999.0
    assert c1["is_primary"] is True
    assert c1["stale"] is False
    c2 = g1["connections"][1]
    assert c2["is_primary"] is False
    assert c2["server_id"] == "sy"
    assert c2["instance_id"] is None
    assert g1["server_ids"] == ["sx", "sy"]
    assert g1["instance_ids"] == ["i1"]
    assert g1["distinct_instances"] == 1
    assert g1["non_stale"] == 2
    assert g1["stale"] == 0
    assert g1["metrics"] == {"sendbuf_size": 2, "rx_buffer_depth": 1, "retransmissions": 2, "tx_bytes": 10, "rx_bytes": 11}
    assert out["totals"] == {
        "groups": 2,
        "connections": 2,
        "primaries": 1,
        "stale": 0,
        "retransmissions": 2,
        "tx_bytes": 10,
        "rx_bytes": 11,
    }
    assert out["config"] == {
        "mpath_primary_stale_sec": 30.0,
        "mpath_failover_check_sec": 2.0,
        "mpath_strategy": "failover",
        "mpath_preemptive_promote": True,
    }


def test_no_muxcon_early_return():
    ad = wc()
    ad._find_muxcon_adapter = lambda: None
    out = ad._gather_multipath_overview()
    assert out["groups"] == []
    assert out["totals"] == {"groups": 0, "connections": 0, "primaries": 0, "stale": 0}
    assert out["timestamp"] > 0


def test_no_muxcon_falsy():
    ad = wc()
    ad._find_muxcon_adapter = lambda: False
    out = ad._gather_multipath_overview()
    assert out["groups"] == []


def test_outer_error():
    ad = wc()

    def boom():
        raise RuntimeError("boom")

    ad._find_muxcon_adapter = boom
    out = ad._gather_multipath_overview()
    assert out == {"error": True, "message": "boom"}


# ---------------------------------------------------------------- stale gating
def _stale_out(**hb_over):
    """One conn (last_seen=100 -> always a stale candidate), hb entry overridable."""
    hb = {"last_ack_ts": None, "last_req_ts": 990.0, "missed": 0}
    hb.update(hb_over)
    m = mux(groups={"p1": grp("c1", {"c1": conn(100.0)})}, hb_state={"c1": hb})
    ad = wc()
    ad._find_muxcon_adapter = lambda: m
    return run_patch(ad, m)


def _stale_flag(out):
    return out["groups"][0]["connections"][0]["stale"]


def test_missed2_stale():
    out = _stale_out(missed=2)
    assert _stale_flag(out) is True
    assert out["totals"]["stale"] == 1


def test_missed1_not_overdue_fresh():
    assert _stale_flag(_stale_out(missed=1)) is False
    assert _stale_flag(_stale_out()) is False


def test_overdue_stale():
    # hb=20.0 -> overdue when now - last_req > 50
    assert _stale_flag(_stale_out(missed=0, last_req_ts=900.0)) is True


def test_missed1_and_overdue_stale():
    assert _stale_flag(_stale_out(missed=1, last_req_ts=900.0)) is True


def test_last_ack_recent_not_candidate():
    assert _stale_flag(_stale_out(missed=9, last_ack_ts=990.0)) is False


def test_no_activity_and_missed_stale():
    # conn with no last_seen at all -> last_activity 0 -> candidate
    m = mux(
        groups={"p1": grp("c1", {"c1": conn(None)})}, hb_state={"c1": {"last_ack_ts": None, "last_req_ts": None, "missed": 2}}
    )
    ad = wc()
    ad._find_muxcon_adapter = lambda: m
    out = run_patch(ad, m)
    c = out["groups"][0]["connections"][0]
    assert c["stale"] is True
    assert c["idle_sec"] is None


def test_hb_entry_non_dict():
    # a non-dict heartbeat entry raises on .get -> treated as {} -> fresh
    m = mux(groups={"p1": grp("c1", {"c1": conn(100.0)})}, hb_state={"c1": "nope"})
    ad = wc()
    ad._find_muxcon_adapter = lambda: m
    out = run_patch(ad, m)
    assert _stale_flag(out) is False


def test_hb_state_non_dict():
    # whole _heartbeat_state attribute is not a dict -> per-conn hb falls back to {}
    m = mux(groups={"p1": grp("c1", {"c1": conn(100.0)})})
    m._heartbeat_state = "garbage"
    ad = wc()
    ad._find_muxcon_adapter = lambda: m
    out = run_patch(ad, m)
    assert _stale_flag(out) is False


def test_hb_state_getattr_raises():
    # getattr(_heartbeat_state) raises -> hb_state {} fallback arm
    inner = mux(groups={"p1": grp("c1", {"c1": conn(100.0)})})
    m = _Wrap(inner, "_heartbeat_state")
    ad = wc()
    ad._find_muxcon_adapter = lambda: m
    out = run_patch(ad, m)
    assert _stale_flag(out) is False


# ---------------------------------------------------------------- effective stale window
def _grid(stale_attr, hb, last_seen):
    m = mux(
        stale=stale_attr,
        hb=hb,
        groups={"p1": grp("c1", {"c1": conn(last_seen)})},
        hb_state={"c1": {"missed": 9, "last_ack_ts": None, "last_req_ts": None}},
    )
    ad = wc()
    ad._find_muxcon_adapter = lambda: m
    return run_patch(ad, m)


def test_window_grid():
    # effective window = max(base_stale, hb*2.5) with a 45.0 fallback; the conn
    # is a stale candidate only when last_activity < now - window (missed=9
    # forces the gate open for every candidate)
    cases = [
        (30.0, 20.0, 960.0, False),  # window 50: 960 >= 950 -> fresh
        (30.0, 20.0, 940.0, True),  # 940 < 950 -> stale
        (30.0, 0, 970.0, False),  # window 30: 970 >= 970 -> fresh
        (30.0, 0, 965.0, True),
        (None, 20.0, 960.0, False),  # base None: window from hb only
        (None, 20.0, 940.0, True),
        (None, 0, 955.0, False),  # neither -> 45.0 default
        (None, 0, 950.0, True),
        ("30", "20", 950.0, True),  # string numerics are NOT coerced -> 45.0 fallback
        (0, 0, 955.0, False),  # zero base is falsy -> 45.0 fallback
    ]
    for st, hb_iv, ls, expect_stale in cases:
        out = _grid(st, hb_iv, ls)
        assert _stale_flag(out) is expect_stale, f"stale={st} hb={hb_iv} last_seen={ls}: expected {expect_stale}"
        assert out["config"]["mpath_primary_stale_sec"] == st


def test_hb_interval_raising():
    # getattr(heartbeat_interval) raises -> hb_interval falls back to 0.0
    inner = mux(groups={"p1": grp("c1", {"c1": conn(965.0)})})
    m = _Wrap(inner, "heartbeat_interval")
    ad = wc()
    ad._find_muxcon_adapter = lambda: m
    out = run_patch(ad, m)
    # hb 0.0 -> window = base 30.0 -> cut 970; 965 < 970 candidate; missed 0, not overdue -> fresh
    assert _stale_flag(out) is False


def test_base_stale_raising():
    # mpath_primary_stale_sec read raises inside the window block -> 45.0 fallback
    m = _boom_once_mux(
        30.0,
        "mpath_primary_stale_sec",
        groups={"p1": grp("c1", {"c1": conn(960.0)})},
        hb=20.0,
        hb_state={"c1": {"missed": 2, "last_ack_ts": None, "last_req_ts": None}},
    )
    ad = wc()
    ad._find_muxcon_adapter = lambda: m
    out = run_patch(ad, m)
    # window 45.0 -> cut 955; 960 not < 955 -> fresh. (If fallback lost to base 30.0
    # the cut would be 970 and this would be stale.)
    assert _stale_flag(out) is False
    assert out["config"]["mpath_primary_stale_sec"] == 30.0


# ---------------------------------------------------------------- peer metrics
class _RaiseDict(dict):
    def get(self, *a, **k):
        raise RuntimeError("nope")


class _RaiseGetForPDict(dict):
    """A dict whose .get raises for one specific key (and only that key)."""

    def __init__(self, boom_key, *a, **k):
        super().__init__(*a, **k)
        self._boom_key = boom_key

    def get(self, key, *default):
        if key == self._boom_key:
            raise RuntimeError("nope")
        return super().get(key, *default)


def test_peer_metrics_grid():
    m = mux(
        groups={"pA": grp(None, {}), "pB": grp(None, {})},
        sendbuf=_RaiseGetForPDict("pA", pB={"k": 1, "j": 2}),
        rx={"pA": {"buffer": [1, 2, 3]}, "pB": _RaiseDict()},
        retx={"pA": 5, "pB": "3"},
        tx={"pA": 100, "pB": "7"},
        rxb={"pB": 8},
    )
    ad = wc()
    ad._find_muxcon_adapter = lambda: m
    out = ad._gather_multipath_overview()
    by_key = {g["peer_key"]: g for g in out["groups"]}
    # pA: sendbuf map .get raising -> 0; non-dict buffer -> depth 0; retx 5; tx 100
    assert by_key["pA"]["metrics"] == {
        "sendbuf_size": 0,
        "rx_buffer_depth": 0,
        "retransmissions": 5,
        "tx_bytes": 100,
        "rx_bytes": 0,
    }
    # pB: same sendbuf map, normal key -> 2; rx value .get raising -> 0; retx "3" -> 3; tx "7" -> 7; rx 8
    assert by_key["pB"]["metrics"] == {
        "sendbuf_size": 2,
        "rx_buffer_depth": 0,
        "retransmissions": 3,
        "tx_bytes": 7,
        "rx_bytes": 8,
    }
    assert out["totals"]["retransmissions"] == 8
    assert out["totals"]["tx_bytes"] == 107
    assert out["totals"]["rx_bytes"] == 8


def test_peer_metrics_tx_and_retx_junk():
    m = mux(
        groups={"p1": grp(None, {}), "p2": grp(None, {}), "p3": grp(None, {})},
        tx={"p1": "junk"},
        rxb={"p1": "junk"},
        rx={"p2": {"buffer": {"a": 1, "b": 2, "c": 3}}},
        retx={"p3": "junk"},
    )
    ad = wc()
    ad._find_muxcon_adapter = lambda: m
    out = ad._gather_multipath_overview()
    by_key = {g["peer_key"]: g for g in out["groups"]}
    # non-numeric byte counters raise inside int() -> 0
    assert by_key["p1"]["metrics"] == {
        "sendbuf_size": 0,
        "rx_buffer_depth": 0,
        "retransmissions": 0,
        "tx_bytes": 0,
        "rx_bytes": 0,
    }
    assert by_key["p2"]["metrics"]["rx_buffer_depth"] == 3
    # non-numeric retx count raises inside int() -> 0
    assert by_key["p3"]["metrics"]["retransmissions"] == 0
    assert out["totals"]["retransmissions"] == 0


def test_overdue_float_junk():
    # last_req_ts non-numeric -> float() raises -> overdue stays False
    m = mux(
        groups={"p1": grp("c1", {"c1": conn(100.0)})},
        hb=20.0,
        hb_state={"c1": {"last_ack_ts": None, "last_req_ts": "junk", "missed": 0}},
    )
    ad = wc()
    ad._find_muxcon_adapter = lambda: m
    out = run_patch(ad, m)
    assert _stale_flag(out) is False


# ---------------------------------------------------------------- metadata
def test_cinfo_raising_and_ids():
    # a connections dict whose .get raises -> every conn id stays None
    m = mux(
        groups={
            "p1": grp("c1", {"c1": conn(995.0), "c2": conn(995.0)}),
            "p2": grp(None, {"c3": conn(995.0), "c4": conn(995.0)}),
        },
        conns=_RaiseDict(c1={"server_id": "s1"}, c3={"server_id": 7}),
    )
    ad = wc()
    ad._find_muxcon_adapter = lambda: m
    out = run_patch(ad, m)
    by_key = {g["peer_key"]: g for g in out["groups"]}
    for g in by_key.values():
        assert all(c["server_id"] is None and c["instance_id"] is None for c in g["connections"])
        assert g["server_ids"] == [] and g["instance_ids"] == []


def test_ids_collected_from_connections():
    # c3 has no entry in connections -> .get(cid, {}) -> ids None (missing tolerated)
    m = mux(
        groups={"p1": grp("c1", {"c1": conn(995.0), "c2": conn(995.0), "c3": conn(995.0)})},
        conns={"c1": {"server_id": "s1", "instance_id": "i1"}, "c2": {"server_id": "s2", "instance_id": "i1"}},
    )
    ad = wc()
    ad._find_muxcon_adapter = lambda: m
    out = ad._gather_multipath_overview()
    g = out["groups"][0]
    assert g["server_ids"] == ["s1", "s2"]
    assert g["instance_ids"] == ["i1"]
    assert g["distinct_instances"] == 1
    c3 = [c for c in g["connections"] if c["conn_id"] == "c3"][0]
    assert c3["server_id"] is None and c3["instance_id"] is None


def test_primary_pref_missing():
    m = mux(groups={"p1": grp("ghost", {"c1": conn(999.0, 950.0, 60)})})
    ad = wc()
    ad._find_muxcon_adapter = lambda: m
    out = run_patch(ad, m)
    g = out["groups"][0]
    assert g["primary"] == "ghost"
    assert g["primary_pref"] is None
    assert g["connections"][0]["is_primary"] is False


def test_primary_counted_and_ages_none():
    m = mux(groups={"p1": grp("c1", {"c1": conn(None, None, 50)})})
    ad = wc()
    ad._find_muxcon_adapter = lambda: m
    out = run_patch(ad, m)
    c = out["groups"][0]["connections"][0]
    assert out["totals"]["primaries"] == 1
    assert c["age_sec"] is None
    assert c["idle_sec"] is None


def test_idle_sec_from_last_seen():
    # last_ack absent, last_seen present -> idle uses last_seen, age_sec None
    m = mux(groups={"p1": grp(None, {"c1": conn(980.0, None, 50)})})
    ad = wc()
    ad._find_muxcon_adapter = lambda: m
    out = run_patch(ad, m)
    c = out["groups"][0]["connections"][0]
    assert c["age_sec"] is None
    assert c["idle_sec"] == NOW - 980.0
