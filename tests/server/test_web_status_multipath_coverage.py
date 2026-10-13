"""Behavior + coverage tests for `WebStatusAdapter._api_get_multipath`.

`_api_get_multipath` (openmux/server/adapters/web_status.py) was the top of
the C901 histogram (38). These tests drive the real method against a real
WebStatusAdapter with an AsyncMock `_send_json` and a fake muxcon adapter,
pinning every branch: muxcon discovery (absent pm / no attr / non-muxcon /
raising), the empty-payload early return, the staleness-window computation
(valid / invalid fallback / raise -> None / heartbeat-window extension),
per-conn stale guardrails (missed count, ACK overdue, fresh last-ack
prevention, junk hb state), identity extraction, the peer metric counters
(sendbuf / rx buffer / retx / tx-rx bytes, incl. the non-numeric and raising
paths), the per-group payload shape, the totals block, the config block, and
the 500 fallback.
"""

import time
from unittest.mock import AsyncMock

import pytest

from openmux.server.adapters.web_status import WebStatusAdapter


class PM:
    def __init__(self, adapters, with_attr=True):
        if with_attr:
            self.unified_adapters = adapters


_UNSET = object()


class Mux:
    """Fake muxcon adapter. Config-block attrs default to None (getattr
    defaults kick in in the payload). A None value on a metric attr makes the
    production `.get(...)` call raise -> the per-field except arm; an Exception
    value for mpath_groups (or connections) makes that access raise."""

    def __init__(self, **kw):
        self.heartbeat_interval = kw.get("hb_interval", 30)
        # A subclass may define this as a read-only (raising) property.
        if not isinstance(getattr(type(self), "mpath_primary_stale_sec", None), property):
            self.mpath_primary_stale_sec = kw.get("stale_sec")
        self.mpath_failover_check_sec = kw.get("failover_check_sec")
        self.mpath_strategy = kw.get("strategy")
        self.mpath_preemptive_promote = kw.get("preemptive")
        self._mpath_groups_attr = kw.get("mpath_groups", {})
        self._hb_state = kw.get("hb_state", {})
        self._connections = kw.get("connections")
        for name in ("_peer_sendbuf", "_peer_rx_state", "_peer_retx_count", "_peer_bytes_tx", "_peer_bytes_rx"):
            setattr(self, name, kw.get(name[1:], _UNSET))

    def get_adapter_type(self):
        return "muxcon"

    @property
    def connections(self):
        if self._connections is None:
            return {}
        if isinstance(self._connections, Exception):
            raise self._connections
        return self._connections

    @property
    def _mpath_groups(self):
        if isinstance(self._mpath_groups_attr, Exception):
            raise self._mpath_groups_attr
        return self._mpath_groups_attr


class RaisingType:
    def get_adapter_type(self):
        raise RuntimeError("type boom")


def _adapter(adapters=None, pm=None, with_pm_attr=True):
    a = WebStatusAdapter("ws", {"port": 9999})
    a._send_json = AsyncMock()
    a.main_port_manager = pm if pm is not None else PM(adapters or [], with_attr=with_pm_attr)
    return a


def _sent(a):
    args, _ = a._send_json.await_args
    _, status, payload = args
    return status, payload


async def _call(a):
    await a._api_get_multipath(object())
    return _sent(a)


def _conn(meta=None, cid="c1", primary=None):
    return {"primary": primary, "conns": {cid: meta or {"opened_at": None, "last_seen": None, "pref": 1}}}


# --- discovery / empty payload -------------------------------------------


@pytest.mark.asyncio
async def test_main_port_manager_none_gives_empty_payload():
    a = _adapter(pm=None)
    status, payload = await _call(a)
    assert status == 200
    assert payload["groups"] == []
    assert payload["totals"] == {"groups": 0, "connections": 0, "primaries": 0, "stale": 0}


@pytest.mark.asyncio
async def test_pm_without_unified_adapters_attr_gives_empty_payload():
    a = _adapter(with_pm_attr=False)
    status, payload = await _call(a)
    assert status == 200
    assert payload["groups"] == []


@pytest.mark.asyncio
async def test_muxcon_discovery_skips_non_muxcon_and_raising():
    class Serial:
        def get_adapter_type(self):
            return "serial"

    status, _ = await _call(_adapter(adapters=[Serial(), RaisingType(), Mux()]))
    assert status == 200  # the real muxcon at the end is found


@pytest.mark.asyncio
async def test_no_muxcon_found_gives_empty_payload():
    class Serial:
        def get_adapter_type(self):
            return "serial"

    status, payload = await _call(_adapter(adapters=[Serial(), RaisingType()]))
    assert status == 200
    assert payload["groups"] == []
    assert isinstance(payload["timestamp"], float)


# --- config block / empty groups ------------------------------------------


@pytest.mark.asyncio
async def test_config_block_reflects_adapter_values():
    a = _adapter(adapters=[Mux(stale_sec=10, failover_check_sec=5, strategy="round-robin", preemptive=True)])
    status, payload = await _call(a)
    assert status == 200
    assert payload["groups"] == []
    assert payload["config"] == {
        "mpath_primary_stale_sec": 10,
        "mpath_failover_check_sec": 5,
        "mpath_strategy": "round-robin",
        "mpath_preemptive_promote": True,
    }


# --- single group shape ----------------------------------------------------


@pytest.mark.asyncio
async def test_single_group_basic_shape():
    now = time.time()
    meta1 = {"opened_at": now - 10, "last_seen": now - 2, "pref": 1}
    meta2 = {"opened_at": now - 30, "last_seen": now - 4, "pref": 2}
    a = _adapter(adapters=[Mux(mpath_groups={"p1": {"primary": "c1", "conns": {"c1": meta1, "c2": meta2}}})])
    status, payload = await _call(a)
    assert status == 200
    g = payload["groups"][0]
    assert g["peer_key"] == "p1"
    assert g["primary"] == "c1"
    assert g["primary_pref"] == 1
    assert g["non_stale"] == 2
    assert g["stale"] == 0
    assert g["server_ids"] == []
    assert g["instance_ids"] == []
    assert g["distinct_instances"] == 0
    assert g["metrics"] == {"sendbuf_size": 0, "rx_buffer_depth": 0, "retransmissions": 0, "tx_bytes": 0, "rx_bytes": 0}
    assert payload["totals"] == {
        "groups": 1,
        "connections": 2,
        "primaries": 1,
        "stale": 0,
        "retransmissions": 0,
        "tx_bytes": 0,
        "rx_bytes": 0,
    }
    by_id = {c["conn_id"]: c for c in g["connections"]}
    assert by_id["c1"]["is_primary"] is True
    assert by_id["c2"]["is_primary"] is False
    assert by_id["c1"]["age_sec"] is not None and 5 <= by_id["c1"]["age_sec"] < 20
    assert by_id["c1"]["idle_sec"] is not None and 1 <= by_id["c1"]["idle_sec"] < 10


@pytest.mark.asyncio
async def test_primary_none_and_missing_primary():
    # primary None: no primary counted, no primary_pref, no is_primary
    a = _adapter(adapters=[Mux(mpath_groups={"p1": _conn(primary=None)})])
    _, payload = await _call(a)
    g = payload["groups"][0]
    assert payload["totals"]["primaries"] == 0
    assert g["primary_pref"] is None
    assert g["connections"][0]["is_primary"] is False
    # primary not present in conns: primary_pref None (fallback of next())
    a2 = _adapter(adapters=[Mux(mpath_groups={"p1": _conn(meta=None, cid="c1", primary="ghost")})])
    _, payload2 = await _call(a2)
    assert payload2["totals"]["primaries"] == 1  # primary is truthy even if unknown
    assert payload2["groups"][0]["primary_pref"] is None
    assert payload2["groups"][0]["connections"][0]["is_primary"] is False


@pytest.mark.asyncio
async def test_opened_and_activity_none():
    meta = {"opened_at": None, "last_seen": None, "pref": 1}
    a = _adapter(adapters=[Mux(mpath_groups={"p1": _conn(meta=meta)})])
    _, payload = await _call(a)
    c = payload["groups"][0]["connections"][0]
    assert c["age_sec"] is None
    assert c["idle_sec"] is None


# --- identity extraction ---------------------------------------------------


@pytest.mark.asyncio
async def test_identity_from_live_connections():
    a = _adapter(
        adapters=[
            Mux(
                mpath_groups={"p1": _conn(primary="c1")},
                connections={"c1": {"server_id": "s1", "instance_id": "i1"}},
            )
        ]
    )
    _, payload = await _call(a)
    g = payload["groups"][0]
    assert g["server_ids"] == ["s1"]
    assert g["instance_ids"] == ["i1"]
    assert g["distinct_instances"] == 1
    assert g["connections"][0]["server_id"] == "s1"


@pytest.mark.asyncio
async def test_identity_extraction_raising_is_best_effort():
    a = _adapter(adapters=[Mux(mpath_groups={"p1": _conn(primary="c1")}, connections=RuntimeError("conn boom"))])
    status, payload = await _call(a)  # must not 500
    assert status == 200
    g = payload["groups"][0]
    assert g["server_ids"] == []
    assert g["connections"][0]["server_id"] is None


# --- staleness -------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_when_old_and_heartbeats_missed():
    now = time.time()
    meta = {"opened_at": now - 100, "last_seen": now - 100, "pref": 1}
    a = _adapter(adapters=[Mux(mpath_groups={"p1": _conn(meta=meta)}, hb_state={"c1": {"missed": 3}})])
    _, payload = await _call(a)
    g = payload["groups"][0]
    assert g["stale"] == 1
    assert g["non_stale"] == 0
    assert payload["totals"]["stale"] == 1


@pytest.mark.asyncio
async def test_not_stale_when_single_missed_and_not_overdue():
    now = time.time()
    meta = {"opened_at": now - 100, "last_seen": now - 100, "pref": 1}
    a = _adapter(adapters=[Mux(mpath_groups={"p1": _conn(meta=meta)}, hb_state={"c1": {"missed": 1}})])
    _, payload = await _call(a)
    assert payload["groups"][0]["stale"] == 0
    assert payload["totals"]["stale"] == 0


@pytest.mark.asyncio
async def test_stale_when_heartbeat_request_overdue():
    now = time.time()
    meta = {"opened_at": now - 100, "last_seen": now - 100, "pref": 1}
    a = _adapter(
        adapters=[Mux(mpath_groups={"p1": _conn(meta=meta)}, hb_interval=10, hb_state={"c1": {"last_req_ts": now - 100}})]
    )
    _, payload = await _call(a)
    assert payload["groups"][0]["stale"] == 1


@pytest.mark.asyncio
async def test_fresh_last_ack_prevents_stale():
    now = time.time()
    meta = {"opened_at": now - 100, "last_seen": now - 100, "pref": 1}
    # last_seen is ancient, but a heartbeat ACK arrived 1s ago; last_activity
    # takes the max, so the conn is NOT a stale candidate at all.
    a = _adapter(adapters=[Mux(mpath_groups={"p1": _conn(meta=meta)}, hb_state={"c1": {"last_ack_ts": now - 1, "missed": 9}})])
    _, payload = await _call(a)
    assert payload["groups"][0]["stale"] == 0


@pytest.mark.asyncio
async def test_stale_sec_property_raising_gives_500():
    # `mpath_primary_stale_sec` is read twice: inside the stale-compute try
    # (a RuntimeError slips past getattr's AttributeError-only default, so
    # that except does set stale_cut=None) and later in the config block
    # (plain getattr). A non-AttributeError-raising property therefore always
    # reaches the outer 500: the compute-arm `stale_cut = None` is set but
    # never used (effectively dead for a 200 response). This test pins the
    # 500 + message.
    class BoomSec(Mux):
        @property
        def mpath_primary_stale_sec(self):
            raise RuntimeError("sec boom")

    a = _adapter(adapters=[BoomSec()])
    status, payload = await _call(a)
    assert status == 500
    assert payload == {"error": True, "message": "sec boom"}


@pytest.mark.asyncio
async def test_invalid_stale_sec_falls_back_to_default_window():
    # stale_sec=-5 -> invalid -> 45.0 fallback; hb_interval 30 -> window 75
    # -> eff 75 (cut ~now-75). last_seen ~now-90 sits below the cut, 2 missed
    # heartbeats -> stays stale. Pins (a) the invalid-value fallback producing
    # a real cut (a missing cut would yield stale=0), (b) the hb window,
    # and (c) the missed-heartbeat guardrail.
    now = time.time()
    meta = {"opened_at": now - 90, "last_seen": now - 90, "pref": 1}
    a = _adapter(
        adapters=[Mux(mpath_groups={"p1": _conn(meta=meta)}, hb_interval=30, stale_sec=-5, hb_state={"c1": {"missed": 2}})]
    )
    _, payload = await _call(a)
    assert payload["groups"][0]["stale"] == 1


@pytest.mark.asyncio
async def test_hb_window_extends_cut():
    # stale_sec=10, hb=10 -> eff = max(10, 25) = 25 -> cut ~now-25.
    # last_seen ~now-20: NOT a stale candidate (la > cut). If the hb window
    # were ignored (cut ~now-10) the same conn would be a candidate.
    now = time.time()
    meta = {"opened_at": now - 20, "last_seen": now - 20, "pref": 1}
    a = _adapter(
        adapters=[Mux(mpath_groups={"p1": _conn(meta=meta)}, hb_interval=10, stale_sec=10, hb_state={"c1": {"missed": 2}})]
    )
    _, payload = await _call(a)
    assert payload["groups"][0]["stale"] == 0


@pytest.mark.asyncio
async def test_hb_state_junk_value_is_best_effort():
    now = time.time()
    meta = {"opened_at": now - 2, "last_seen": now - 2, "pref": 1}
    a = _adapter(adapters=[Mux(mpath_groups={"p1": _conn(meta=meta)}, hb_state={"c1": "junk"})])
    status, payload = await _call(a)
    assert status == 200
    assert payload["groups"][0]["connections"][0]["stale"] is False


@pytest.mark.asyncio
async def test_non_numeric_heartbeat_interval_falls_back_to_zero():
    # float("abc") raises -> hb_int 0.0 in BOTH the stale-window compute
    # (falls back to the bare stale_sec window) and the per-conn guardrail
    # (overdue can never be true). An old conn with 2 missed heartbeats is
    # still stale -> pins the cut actually applied was now - 10 (stale_sec),
    # not a heartbeat-extended window.
    now = time.time()
    meta = {"opened_at": now - 100, "last_seen": now - 100, "pref": 1}
    a = _adapter(
        adapters=[
            Mux(
                mpath_groups={"p1": _conn(meta=meta)},
                hb_interval="abc",
                stale_sec=10,
                hb_state={"c1": {"missed": 2}},
            )
        ]
    )
    _, payload = await _call(a)
    assert payload["groups"][0]["stale"] == 1


@pytest.mark.asyncio
async def test_junk_hb_entry_zeroes_missed():
    # hb_state[cid] = "junk": ("junk").get raises -> the inner except zeros
    # hb_missed (0 <= 1) -> not stale, even with an ancient last_seen. (The
    # earlier last_ack/last_req extraction also fails -> both None.)
    now = time.time()
    meta = {"opened_at": now - 100, "last_seen": now - 100, "pref": 1}
    a = _adapter(adapters=[Mux(mpath_groups={"p1": _conn(meta=meta)}, hb_state={"c1": "junk"})])
    _, payload = await _call(a)
    assert payload["groups"][0]["stale"] == 0


@pytest.mark.asyncio
async def test_junk_last_req_zeroes_overdue():
    # last_req_ts="junk": float("junk") raises inside the overdue calc ->
    # except -> overdue False. missed=2 keeps the stale verdict anyway, so the
    # conn is stale and the except arm is exercised without changing it.
    now = time.time()
    meta = {"opened_at": now - 100, "last_seen": now - 100, "pref": 1}
    a = _adapter(
        adapters=[
            Mux(
                mpath_groups={"p1": _conn(meta=meta)},
                hb_interval=10,
                hb_state={"c1": {"missed": 2, "last_req_ts": "junk"}},
            )
        ]
    )
    _, payload = await _call(a)
    assert payload["groups"][0]["stale"] == 1


# --- peer metrics ------------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_metrics_populated():
    a = _adapter(
        adapters=[
            Mux(
                mpath_groups={
                    "p1": _conn(primary="c1", cid="c1"),
                    "p2": _conn(primary="c1", cid="c2"),
                },
                peer_sendbuf={"p1": {"a": 1, "b": 2}, "p2": ["not-a-dict"]},
                peer_rx_state={"p1": {"buffer": {"x": 1, "y": 2}}, "p2": {"buffer": "str"}},
                peer_retx_count={"p1": 7, "p2": "oops"},
                peer_bytes_tx={"p1": 1234, "p2": "junk"},
                peer_bytes_rx={"p1": 55, "p2": 66},
            )
        ]
    )
    status, payload = await _call(a)
    assert status == 200
    g1, g2 = payload["groups"]
    assert g1["metrics"] == {"sendbuf_size": 2, "rx_buffer_depth": 2, "retransmissions": 7, "tx_bytes": 1234, "rx_bytes": 55}
    assert g2["metrics"] == {"sendbuf_size": 0, "rx_buffer_depth": 0, "retransmissions": 0, "tx_bytes": 0, "rx_bytes": 66}
    totals = payload["totals"]
    assert totals["retransmissions"] == 7
    assert totals["tx_bytes"] == 1234
    assert totals["rx_bytes"] == 121


@pytest.mark.asyncio
async def test_peer_metrics_attr_none_hits_except():
    # A None-valued attr makes `<dict-or-None>.get(...)` raise -> the
    # per-field except arm resets the counter to zero.
    # (A fully ABSENT attr instead takes the getattr-default / `or {}` path -
    # also 0; this test pins the explicit except arm.)
    a = _adapter(
        adapters=[
            Mux(
                mpath_groups={"p1": _conn(primary="c1")},
                peer_sendbuf=None,
                peer_rx_state=None,
                peer_retx_count=None,
                peer_bytes_tx=None,
                peer_bytes_rx=None,
            )
        ]
    )
    status, payload = await _call(a)
    assert status == 200
    assert payload["groups"][0]["metrics"] == {
        "sendbuf_size": 0,
        "rx_buffer_depth": 0,
        "retransmissions": 0,
        "tx_bytes": 0,
        "rx_bytes": 0,
    }


# --- 500 fallback -------------------------------------------------------------


@pytest.mark.asyncio
async def test_outer_error_gives_500():
    # A raising _mpath_groups access happens inside the outer try -> 500.
    a = _adapter(adapters=[Mux(mpath_groups=RuntimeError("groups boom"))])
    status, payload = await _call(a)
    assert status == 500
    assert payload == {"error": True, "message": "groups boom"}
