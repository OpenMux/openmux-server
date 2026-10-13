"""Behavioral coverage for UnifiedMuxConAdapter._mpath_failover_loop (40-bracket S1).

Drives the real background loop with asyncio.sleep and asyncio.create_task
patched (the loop starts with a mandatory 1s sleep and would spin on a fake
sleep, so a call counter sets the stop event after N ticks). Collab
_select_mpath_connection and _close_connection are replaced by recorders; the
observable multipath-group mutations, created close tasks, and log messages
are pinned.

Written (and green) against the PRE-refactor method.
"""

import asyncio
import logging
import time

from openmux.server.adapters.muxcon import UnifiedMuxConAdapter


def _meta(last_activity, pref=0, opened=None):
    return {"last_rx_seen": last_activity, "last_seen": last_activity, "opened_at": opened or last_activity, "pref": pref}


def _adapter(monkeypatch, groups, debug=False):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    a._mpath_groups = groups if groups is not None else {}
    if debug:
        # caplog only raises its own handler's level; the adapter logger's own
        # effective level still gates DEBUG records before propagation
        a.logger.setLevel(logging.DEBUG)
    return a


class _RaisingGet(dict):
    def get(self, key, default=None):
        raise RuntimeError("dict get boom")


class _BoomMul:
    """Raises on arithmetic use: `heartbeat_interval * 2.5` boom."""

    def __mul__(self, other):
        raise RuntimeError("heartbeat mul boom")


def _raising_prop():
    # Non-data descriptor (only __get__). Set on the CLASS via monkeypatch and
    # the matching instance attr deleted, so attribute resolution for the
    # instance falls through to the class descriptor (a __get__-only
    # descriptor is normally shadowed by the instance dict, where __init__
    # stored the plain default). monkeypatch restores the class attr after the
    # test and re-raises the RuntimeError from any instance read.
    class _Prop:
        def __get__(self, obj, objtype=None):
            raise RuntimeError("attr boom")

    return _Prop()


def _run_loop(a, monkeypatch, stop_after=2, sleep_exc=None, close_raises=False):
    """Run one loop under a fake sleep; stop after stop_after sleeps.

    Returns (sleeps, selected_keys, closed_cids, created_tasks).
    """
    sleeps = []
    stop_evt = a._stop_event
    tasks = []
    real_create_task = asyncio.create_task
    exc_count = [0]

    async def fake_sleep(sec, *args, **kwargs):
        sleeps.append(sec)
        if sleep_exc is not None:
            exc_count[0] += 1
            # Raise only from sleep #2 onward: #1 is the mandatory startup
            # sleep, whose raise would escape the method entirely.
            if exc_count[0] >= 2:
                raise sleep_exc
        if len(sleeps) >= stop_after:
            stop_evt.set()

    def fake_create_task(coro):
        t = real_create_task(coro)
        tasks.append(t)
        return t

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(asyncio, "create_task", fake_create_task)
    selected = []
    a._select_mpath_connection = lambda k: selected.append(k) or "NEWPRIM"
    closed = []

    async def rec_close(cid):
        if close_raises:
            raise RuntimeError("close boom")
        closed.append(cid)

    a._close_connection = rec_close

    async def driver():
        await a._mpath_failover_loop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    loop0 = asyncio.new_event_loop()
    try:
        loop0.run_until_complete(driver())
    finally:
        loop0.close()
    return sleeps, selected, closed, tasks


def test_check_sec_disabled_returns_immediately(monkeypatch):
    a = _adapter(monkeypatch, {"pk": {"conns": {}, "primary": None}})
    a.mpath_failover_check_sec = 0
    sleeps, selected, closed, _t = _run_loop(a, monkeypatch)
    assert sleeps == []
    assert selected == []
    assert closed == []


def test_stop_event_pre_set_runs_no_iterations(monkeypatch):
    a = _adapter(monkeypatch, {})
    a._stop_event.set()
    sleeps, selected, closed, _t = _run_loop(a, monkeypatch)
    assert sleeps == [1.0]  # only the mandatory startup sleep
    assert selected == []
    assert closed == []


def test_idle_ttl_single_conn_drops_group(monkeypatch, caplog):
    now = time.time()
    caplog.set_level(logging.WARNING, logger="openmux.adapter.muxcon.mx")
    grp = {"conns": {"c1": _meta(now - 10)}, "primary": "c1"}
    a = _adapter(monkeypatch, {"pk": grp})
    a.mpath_neighbor_idle_drop_sec = 1
    _s, selected, closed, _t = _run_loop(a, monkeypatch)
    assert closed == ["c1"]
    assert a._mpath_groups == {}  # empty group popped
    assert selected == ["pk"]  # group ref still local: primary now None -> select
    assert any("Idle TTL exceeded" in m for m in caplog.messages)


def test_idle_ttl_two_conns_repoints_primary(monkeypatch, caplog):
    now = time.time()
    caplog.set_level(logging.WARNING, logger="openmux.adapter.muxcon.mx")
    grp = {"conns": {"c1": _meta(now - 10), "c2": _meta(now)}, "primary": "c1"}
    a = _adapter(monkeypatch, {"pk": grp})
    a.mpath_neighbor_idle_drop_sec = 1
    _s, _sel, closed, _t = _run_loop(a, monkeypatch)
    assert closed == ["c1"]
    assert grp["primary"] == "c2"
    assert "c2" in grp["conns"]
    assert a._mpath_groups == {"pk": grp}


def test_idle_ttl_fresh_conn_kept(monkeypatch):
    now = time.time()
    grp = {"conns": {"c1": _meta(now)}, "primary": "c1"}
    a = _adapter(monkeypatch, {"pk": grp})
    a.mpath_neighbor_idle_drop_sec = 1
    _s, selected, closed, _t = _run_loop(a, monkeypatch)
    assert closed == []
    assert selected == []
    assert "c1" in grp["conns"]


def test_idle_ttl_attr_raises_skips_prune(monkeypatch, caplog):
    """getattr(self, mpath_neighbor_idle_drop_sec) raising -> ttl 0 -> no prune,
    but the stale path still runs (no alternative -> throttled debug warn)."""
    now = time.time()
    caplog.set_level(logging.DEBUG, logger="openmux.adapter.muxcon.mx")
    # now-1000: idle beyond even the default 900s TTL, so closed == [] proves
    # the ttl getattr raised (otherwise the prune would close c1)
    grp = {"conns": {"c1": _meta(now - 1000)}, "primary": "c1"}
    a = _adapter(monkeypatch, {"pk": grp}, debug=True)
    # raising=False: the attr is instance-only (no class attribute exists).
    # del the instance dict entry so the class descriptor is consulted.
    monkeypatch.setattr(UnifiedMuxConAdapter, "mpath_neighbor_idle_drop_sec", _raising_prop(), raising=False)
    del a.__dict__["mpath_neighbor_idle_drop_sec"]
    _s, selected, closed, _t = _run_loop(a, monkeypatch)
    assert closed == []
    assert selected == []
    assert any("no alternative path available" in m for m in caplog.messages)


def test_per_conn_hb_state_raises_conn_kept(monkeypatch):
    """_hb_state.get raising in the per-conn TTL-prune read -> per-conn except
    -> conn not pruned. With ttl=1 and activity now-1000 the prune WOULD close
    c1 if no except fired, so closed == [] is the discriminator. The same
    raising dict on the later primary hb read aborts the tick via the outer
    loop except (silent), so at most one tick of work happens."""
    now = time.time()
    grp = {"conns": {"c1": _meta(now - 1000)}, "primary": "c1"}
    a = _adapter(monkeypatch, {"pk": grp})
    a.mpath_neighbor_idle_drop_sec = 1
    a._hb_state = _RaisingGet()
    _s, selected, closed, _t = _run_loop(a, monkeypatch)
    assert closed == []
    assert "c1" in grp["conns"]
    assert selected == []


def test_no_primary_selects(monkeypatch):
    now = time.time()
    grp = {"conns": {"c2": _meta(now)}, "primary": None}
    a = _adapter(monkeypatch, {"pk": grp})
    _s, selected, closed, _t = _run_loop(a, monkeypatch)
    assert selected == ["pk"]
    assert closed == []


def test_primary_meta_missing_selects(monkeypatch):
    now = time.time()
    grp = {"conns": {"c2": _meta(now)}, "primary": "ghost"}
    a = _adapter(monkeypatch, {"pk": grp})
    _s, selected, closed, _t = _run_loop(a, monkeypatch)
    assert selected == ["pk"]
    assert closed == []


def test_stale_primary_with_alternative_attempts_promotion(monkeypatch, caplog):
    now = time.time()
    caplog.set_level(logging.WARNING, logger="openmux.adapter.muxcon.mx")
    grp = {"conns": {"c1": _meta(now - 100, pref=0), "c2": _meta(now, pref=1)}, "primary": "c1"}
    a = _adapter(monkeypatch, {"pk": grp})
    _s, selected, closed, _t = _run_loop(a, monkeypatch)
    assert selected == ["pk"]
    assert any("attempting promotion" in m for m in caplog.messages)
    assert grp.get("last_primary_change")
    assert closed == []


def test_stale_no_alternative_warns_once(monkeypatch, caplog):
    now = time.time()
    caplog.set_level(logging.DEBUG, logger="openmux.adapter.muxcon.mx")
    grp = {"conns": {"c1": _meta(now - 100)}, "primary": "c1"}
    a = _adapter(monkeypatch, {"pk": grp}, debug=True)
    _s, selected, closed, _t = _run_loop(a, monkeypatch, stop_after=3)  # 2 iterations
    assert selected == []
    assert closed == []
    warns = [m for m in caplog.messages if "no alternative path available" in m]
    assert len(warns) == 1  # 2nd iteration throttled (3 * check interval)
    assert grp["primary"] == "c1"


def test_stale_churn_recent_change_skips_promotion(monkeypatch, caplog):
    now = time.time()
    caplog.set_level(logging.WARNING, logger="openmux.adapter.muxcon.mx")
    grp = {
        "conns": {"c1": _meta(now - 100, pref=0), "c2": _meta(now, pref=1)},
        "primary": "c1",
        "last_primary_change": time.time(),
    }
    a = _adapter(monkeypatch, {"pk": grp})
    _s, selected, closed, _t = _run_loop(a, monkeypatch)
    assert selected == []
    assert closed == []
    assert not any("attempting promotion" in m for m in caplog.messages)


def test_preemptive_promote_higher_pref(monkeypatch, caplog):
    now = time.time()
    caplog.set_level(logging.INFO, logger="openmux.adapter.muxcon.mx")
    # last_primary_change seeded old: the loop seeds it from the primary's
    # opened_at (= now) when absent, which would trip the churn throttle
    grp = {
        "conns": {"c1": _meta(now, pref=0), "c2": _meta(now, pref=5)},
        "primary": "c1",
        "last_primary_change": now - 1000,
    }
    a = _adapter(monkeypatch, {"pk": grp})
    _s, _sel, closed, _t = _run_loop(a, monkeypatch)
    assert grp["primary"] == "c2"
    assert grp.get("last_primary_change")
    assert any("Preemptive promote c2 over c1" in m for m in caplog.messages)
    assert closed == []


def test_preemptive_promote_stale_candidate_skipped(monkeypatch, caplog):
    now = time.time()
    caplog.set_level(logging.INFO, logger="openmux.adapter.muxcon.mx")
    # c2 has higher pref but is itself stale -> not a candidate for preemption
    grp = {"conns": {"c1": _meta(now, pref=0), "c2": _meta(now - 100, pref=5)}, "primary": "c1"}
    a = _adapter(monkeypatch, {"pk": grp})
    _s, _sel, closed, _t = _run_loop(a, monkeypatch)
    assert grp["primary"] == "c1"
    assert not any("Preemptive promote" in m for m in caplog.messages)
    assert closed == []


def test_preemptive_throttled_recent_change(monkeypatch, caplog):
    now = time.time()
    caplog.set_level(logging.INFO, logger="openmux.adapter.muxcon.mx")
    grp = {
        "conns": {"c1": _meta(now, pref=0), "c2": _meta(now, pref=5)},
        "primary": "c1",
        "last_primary_change": time.time(),
    }
    a = _adapter(monkeypatch, {"pk": grp})
    _s, _sel, closed, _t = _run_loop(a, monkeypatch)
    assert grp["primary"] == "c1"
    assert not any("Preemptive promote" in m for m in caplog.messages)


def test_heartbeat_widens_stale_window(monkeypatch, caplog):
    now = time.time()
    caplog.set_level(logging.WARNING, logger="openmux.adapter.muxcon.mx")
    # stale_sec 0.5 would flag now-10 as stale; heartbeat 30 widens it to 75s
    grp = {"conns": {"c1": _meta(now - 10, pref=0)}, "primary": "c1"}
    a = _adapter(monkeypatch, {"pk": grp}, debug=True)
    a.mpath_primary_stale_sec = 0.5
    a.heartbeat_interval = 30.0
    _s, selected, closed, _t = _run_loop(a, monkeypatch)
    assert selected == []
    assert closed == []
    assert not any("stale for" in m for m in caplog.messages)


def test_heartbeat_window_compute_raises_uses_default(monkeypatch, caplog):
    """heartbeat_interval read raises. The window-try catches the raise, so the
    effective stale window falls back to mpath_primary_stale_sec (0.5s). With
    a now-10 activity that IS stale, the no-alternative debug warn fires
    (tick 1); on tick 2 the class read raises, aborts the tick via the per-tick
    except, and the loop exits. Pinned: warn once, group untouched, no
    select/close, no error log."""
    now = time.time()
    caplog.set_level(logging.DEBUG, logger="openmux.adapter.muxcon.mx")
    grp = {"conns": {"c1": _meta(now - 10)}, "primary": "c1"}
    a = _adapter(monkeypatch, {"pk": grp}, debug=True)
    a.mpath_primary_stale_sec = 0.5
    # raising=False: the attr is instance-only (no class attribute exists).
    # del the instance dict entry so the class descriptor is actually
    # consulted. monkeypatch removes class attrs it added on teardown.
    monkeypatch.setattr(UnifiedMuxConAdapter, "heartbeat_interval", _raising_prop(), raising=False)
    del a.__dict__["heartbeat_interval"]
    _s, selected, closed, _t = _run_loop(a, monkeypatch)
    assert selected == []
    assert closed == []
    warns = [m for m in caplog.messages if "no alternative path available" in m]
    assert len(warns) == 1  # tick 1 fires; tick 2 raise aborts before warn (also throttled)
    assert grp["primary"] == "c1"


def test_stale_cutoff_none_not_stale(monkeypatch, caplog):
    """stale_sec 0 and heartbeat 0 -> stale_cutoff None -> stale check is
    skipped entirely (primary now-100 kept, no warn, no select)."""
    now = time.time()
    caplog.set_level(logging.WARNING, logger="openmux.adapter.muxcon.mx")
    grp = {"conns": {"c1": _meta(now - 100, pref=0)}, "primary": "c1"}
    a = _adapter(monkeypatch, {"pk": grp}, debug=True)
    a.mpath_primary_stale_sec = 0.0
    a.heartbeat_interval = 0.0
    a.mpath_preemptive_promote = False
    _s, selected, closed, _t = _run_loop(a, monkeypatch)
    assert selected == []
    assert closed == []
    assert not any("stale for" in m or "no alternative" in m for m in caplog.messages)
    assert grp["primary"] == "c1"


def test_group_level_exception_continues_scan(monkeypatch, caplog):
    """No per-group try/except exists: a raise during evaluation of the 'bad'
    group (a non-dict) is caught by the per-tick best-effort except, which
    aborts the rest of THAT tick; the while loop then keeps going. Group
    order pins this: k2 (valid, stale, no alternative) is fully processed
    first and fires its debug warn; 'bad' then raises. On the 2nd tick k2's
    warn is throttled (3 * check interval), so exactly one warn total."""
    now = time.time()
    caplog.set_level(logging.DEBUG, logger="openmux.adapter.muxcon.mx")
    valid = {"conns": {"c1": _meta(now - 100)}, "primary": "c1"}
    a = _adapter(monkeypatch, {"k2": valid, "bad": "not-a-dict"}, debug=True)
    a.mpath_neighbor_idle_drop_sec = 0
    _s, selected, closed, _t = _run_loop(a, monkeypatch, stop_after=3)  # 2 ticks
    assert selected == []
    assert closed == []
    warns = [m for m in caplog.messages if "no alternative path available" in m]
    assert len(warns) == 1  # 2nd tick throttled
    assert a._mpath_groups.get("bad") == "not-a-dict"


def test_cancelled_error_exits_silently(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="openmux.adapter.muxcon.mx")
    a = _adapter(monkeypatch, {})
    _s, selected, closed, _t = _run_loop(a, monkeypatch, sleep_exc=asyncio.CancelledError())
    assert selected == []
    assert closed == []
    assert not any("exited with error" in m for m in caplog.messages)


def test_stale_alternative_stale_candidate_skipped(monkeypatch, caplog):
    now = time.time()
    caplog.set_level(logging.WARNING, logger="openmux.adapter.muxcon.mx")
    # c2 is itself stale -> skipped inside the alternatives loop; c3 is fresh
    # so promotion proceeds on its basis
    grp = {
        "conns": {
            "c1": _meta(now - 100, pref=0),
            "c2": _meta(now - 100, pref=1),
            "c3": _meta(now, pref=2),
        },
        "primary": "c1",
        "last_primary_change": now - 1000,
    }
    a = _adapter(monkeypatch, {"pk": grp})
    _s, selected, closed, _t = _run_loop(a, monkeypatch)
    assert selected == ["pk"]
    assert closed == []
    assert any("attempting promotion" in m for m in caplog.messages)
    assert grp.get("last_primary_change") > now - 5


def test_ttl_prune_inner_pop_fails_close_still_runs(monkeypatch):
    """`grp["conns"].pop` raising -> the inner prune except (justification:
    optional failover prune) swallows it; the `create_task` close (outside
    that try) still runs, and the group evaluation continues with the stale
    primary (no alternative -> no select)."""
    now = time.time()

    class _PopBoom(dict):
        def pop(self, *args, **kwargs):
            raise RuntimeError("pop boom")

    grp = {"conns": _PopBoom({"c1": _meta(now - 1000)}), "primary": "c1"}
    a = _adapter(monkeypatch, {"pk": grp}, debug=True)
    a.mpath_neighbor_idle_drop_sec = 1
    _s, selected, closed, _t = _run_loop(a, monkeypatch)
    assert closed == ["c1"]  # close still scheduled after the failed pop
    assert "c1" in grp["conns"]  # pop failed: conn stays
    assert "pk" in a._mpath_groups  # group not (fully) pruned
    assert selected == []  # stale primary, no alternative


def test_preemptive_scan_pref_int_fails_continues(monkeypatch):
    """`int(meta.get("pref", 0))` raising (pref="abc") inside the preemptive
    scan -> the advisory except swallows it; group untouched, no select."""
    now = time.time()
    grp = {"conns": {"c1": _meta(now, pref="abc"), "c2": _meta(now, pref=5)}, "primary": "c1"}
    a = _adapter(monkeypatch, {"pk": grp}, debug=True)
    _s, selected, closed, _t = _run_loop(a, monkeypatch)
    assert selected == []
    assert closed == []
    assert grp["primary"] == "c1"


def test_close_conn_call_fails_prune_completes(monkeypatch):
    """self._close_connection(cid) raising (set to None) at the create_task
    line is caught by the per-connection except; the prune (conn pop, group
    repoint/pop) already completed inside the inner try."""
    now = time.time()
    grp = {"conns": {"c1": _meta(now - 1000)}, "primary": "c1"}
    a = _adapter(monkeypatch, {"pk": grp})
    a.mpath_neighbor_idle_drop_sec = 1
    a._close_connection = None  # `self._close_connection(cid)` -> TypeError
    _s, selected, closed, _t = _run_loop(a, monkeypatch, close_raises=True)
    assert closed == []
    assert a._mpath_groups == {}  # conn popped -> empty group popped
    assert selected == ["pk"]  # local grp's primary repointed to None -> select


def test_outer_error_logged_debug(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="openmux.adapter.muxcon.mx")
    a = _adapter(monkeypatch, {})
    _s, selected, closed, _t = _run_loop(a, monkeypatch, sleep_exc=RuntimeError("loop boom"))
    assert selected == []
    assert closed == []
    assert any("mpath failover loop exited with error" in m for m in caplog.messages)
