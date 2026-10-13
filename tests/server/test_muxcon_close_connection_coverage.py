"""Behavioral coverage for UnifiedMuxConAdapter._close_connection (39-bracket S1).

Drives the real _close_connection against a real adapter instance (cheap
constructor: UnifiedMuxConAdapter("mx", {"listeners": []})) with the
collaborators (unregister, cleanup, save-cache, refresh, live-state,
link-notice) replaced by recorders, and pinning the observable state:
connection/wire/hb registries, proxy flags (is_connected, link_reason),
PortManager notifications, and writer close behavior.

Written (and green) against the PRE-refactor method.
"""

import asyncio

from openmux.server.adapters.muxcon import UnifiedMuxConAdapter

_MISSING = object()


class FakeWriter:
    """Stream-writer stand-in: is_closing/close/wait_closed all recorded."""

    def __init__(self, wait_exc=None, closing=False, close_exc=None):
        self._closing = closing
        self.wait_exc = wait_exc
        self.close_exc = close_exc
        self.closed = False
        self.waited = False

    def is_closing(self):
        return self._closing

    def close(self):
        if self.close_exc:
            raise self.close_exc
        self.closed = True

    async def wait_closed(self):
        self.waited = True
        if self.wait_exc:
            raise self.wait_exc


class PM:
    """PortManager stand-in recording notify_meta_updated calls."""

    def __init__(self, raise_for=None):
        self.calls = []
        self.raise_for = raise_for or set()

    def notify_meta_updated(self, pname, payload):
        self.calls.append((pname, payload))
        if pname in self.raise_for:
            raise RuntimeError("notify boom")


class Origin:
    def __init__(self, server_id):
        self.server_id = server_id


class Meta:
    def __init__(self, origin):
        self.origin_server = origin


class Proxy:
    """Federated port-proxy stand-in with controllable failure modes."""

    def __init__(
        self,
        src="OSRC",
        raise_set_connected=False,
        raise_metadata_read=False,
        read_only_last_seen=None,
    ):
        self.is_connected = True
        self._raise_set_connected = raise_set_connected
        self._src = src
        self._raise_metadata = raise_metadata_read
        self._read_only_last_seen = read_only_last_seen
        self.disconnect_called = 0
        self._ls = None

    @property
    def metadata(self):
        if self._raise_metadata:
            raise RuntimeError("meta boom")
        return Meta(Origin(self._src))

    @property
    def last_seen(self):
        if self._read_only_last_seen is not None:
            return self._read_only_last_seen
        return self._ls

    @last_seen.setter
    def last_seen(self, value):
        if self._read_only_last_seen is not None:
            raise RuntimeError("read-only")
        self._ls = value

    def __setattr__(self, name, value):
        if name == "is_connected" and getattr(self, "_raise_set_connected", False):
            raise RuntimeError("conn flag boom")
        super().__setattr__(name, value)


def _async_rec(calls, name):
    async def _f(arg):
        calls.append((name, arg))
        return None

    return _f


def _rec(calls, name, nargs=1):
    def _f(*args):
        calls.append((name, args))
        return None

    return _f


def _adapter(monkeypatch, connections=None, wire=None, hb=None, groups=None, proxies=None, pm=None):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    a.connections = connections if connections is not None else {}
    if wire is not None:
        a._wire_state = wire
    if hb is not None:
        a._hb_state = hb
    if groups is not None:
        a._mpath_groups = groups
    if proxies is not None:
        a._peer_proxies = proxies
    if pm is not None:
        a.main_port_manager = pm
    return a


def _patch_collab(a, monkeypatch):
    calls = []
    monkeypatch.setattr(a, "_unregister_mpath_connection", _rec(calls, "unreg", 1))
    a._maybe_cleanup_empty_peer = _async_rec(calls, "cleanup")
    a._save_federated_cache = lambda: calls.append(("save", ()))
    a._refresh_conn_proxies = _rec(calls, "refresh", 0)
    a._update_peer_proxies_live_state = _rec(calls, "live_state", 1)
    a._emit_link_notice_once = _rec(calls, "emit", 3)
    return calls


def _key(a, value=None, raise_key=False):
    if raise_key:

        def _d(_cid):
            raise RuntimeError("derive boom")

        return _d
    return (lambda _cid: value) if value is not None else (lambda _cid: None)


async def _close(a, cid="c1"):
    await a._close_connection(cid)


def test_absent_conn_clears_state_and_cleans_up(monkeypatch):
    a = _adapter(monkeypatch, connections={}, wire={"c1": {"w": 1}}, hb={"c1": {"h": 1}})
    calls = _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert a._wire_state == {}
    assert a._hb_state == {}
    assert ("unreg", ("c1",)) in calls
    assert ("cleanup", "node:S1") in calls
    # absent branch returns early: no federated-cache save
    assert not any(c[0] == "save" for c in calls)


def test_absent_conn_stable_key_from_derive(monkeypatch):
    a = _adapter(monkeypatch)
    calls = _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _key(a, "node:S7")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert ("cleanup", "node:S7") in calls


def test_with_conn_closes_writer_and_tears_down(monkeypatch):
    w = FakeWriter()
    pm = PM()
    a = _adapter(monkeypatch, connections={"c1": {"writer": w, "server_id": "S1"}}, pm=pm)
    calls = _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert w.closed is True
    assert w.waited is True
    assert a.connections == {}
    assert a._hb_state == {}
    assert any(c[0] == "cleanup" and c[1] == "node:S1" for c in calls)
    assert any(c[0] == "save" for c in calls)
    assert any(c[0] == "refresh" for c in calls)


def test_writer_wait_closed_raises_is_suppressed(monkeypatch):
    w = FakeWriter(wait_exc=RuntimeError("drain"))
    a = _adapter(monkeypatch, connections={"c1": {"writer": w}})
    calls = _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert a._hb_state == {}
    assert any(c[0] == "cleanup" for c in calls)


def test_writer_is_closing_at_entry_skips_close(monkeypatch):
    w = FakeWriter(closing=True)
    a = _adapter(monkeypatch, connections={"c1": {"writer": w}})
    _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert w.closed is False
    assert w.waited is False


def test_writer_close_raises_is_suppressed(monkeypatch):
    w = FakeWriter(close_exc=RuntimeError("close boom"))
    a = _adapter(monkeypatch, connections={"c1": {"writer": w}})
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert a._hb_state == {}


def test_derive_raising_skips_proxy_marking(monkeypatch):
    a = _adapter(monkeypatch, connections={"c1": {"server_id": "S9"}}, proxies={"node:S9": {"p1": Proxy()}})
    calls = _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _key(a, raise_key=True)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    # outer try swallowed the raise: no offline marking, no live-state
    assert not any(c[0] == "emit" for c in calls)
    assert not any(c[0] == "save" for c in calls)
    assert a._hb_state == {}
    assert any(c[0] == "cleanup" and c[1] is None for c in calls)


def test_live_paths_remain_recomputes_live_state(monkeypatch):
    a = _adapter(
        monkeypatch,
        connections={"c1": {"server_id": "S1"}},
        groups={"node:S1": {"conns": {"c2": {}}}},
        proxies={"node:S1": {"p1": Proxy()}},
    )
    calls = _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert ("live_state", ("node:S1",)) in calls
    assert not any(c[0] == "save" for c in calls)
    assert a._peer_proxies["node:S1"]["p1"].is_connected is True
    assert any(c[0] == "cleanup" for c in calls)


def test_live_state_raise_is_suppressed(monkeypatch):
    a = _adapter(monkeypatch, connections={"c1": {"_x": 1}}, groups={"node:S1": {"conns": {"c2": {}}}})
    calls = _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    def _boom(_pk):
        raise RuntimeError("live boom")

    a._update_peer_proxies_live_state = _boom

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert a._hb_state == {}
    assert any(c[0] == "cleanup" for c in calls)


def test_offline_mark_disconnected_with_node_fallback(monkeypatch):
    pm = PM()
    p1 = Proxy(src="OSRC")
    a = _adapter(
        monkeypatch,
        connections={"c1": {"server_id": "S9"}},
        # empty conns -> group exists but no paths remain
        groups={"host:1.2.3.4": {"conns": {}}},
        # proxies under the node: key only; host key looks up empty
        proxies={"host:1.2.3.4": {}, "node:S9": {"p1": p1}},
        pm=pm,
    )
    calls = _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _key(a, "host:1.2.3.4")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert p1.is_connected is False
    assert p1.link_reason == "MuxCon link to OSRC is down"
    assert ("emit", (p1, "disconnected", "OSRC::p1")) in calls
    assert ("p1", {"event": "federated_disconnected", "peer_key": "node:S9"}) in pm.calls
    # cached-offline notify with last_seen
    cached = [payload for pname, payload in pm.calls if payload.get("event") == "federated_cached_offline"]
    assert len(cached) == 1
    assert cached[0]["peer_key"] == "node:S9"
    assert cached[0]["connected"] is False
    assert any(c[0] == "save" for c in calls)


def test_offline_unknown_source(monkeypatch):
    p1 = Proxy(src=None)
    a = _adapter(monkeypatch, connections={"c1": {"_x": 1}}, proxies={"node:S1": {"p1": p1}})
    calls = _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert p1.link_reason == "MuxCon link to unknown is down"
    assert ("emit", (p1, "disconnected", "unknown::p1")) in calls


def test_proxy_metadata_read_raises(monkeypatch):
    p1 = Proxy(raise_metadata_read=True)
    a = _adapter(monkeypatch, connections={"c1": {"_x": 1}}, proxies={"node:S1": {"p1": p1}})
    calls = _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    # link_reason not set (that read raised), but disconnect/notice proceeds:
    # the notice read also raised -> src fallback "unknown"
    assert not hasattr(p1, "link_reason") or p1.link_reason is None
    assert ("emit", (p1, "disconnected", "unknown::p1")) in calls


def test_proxy_is_connected_set_raises_skips_that_proxy(monkeypatch):
    pm = PM()
    bad = Proxy(raise_set_connected=True)
    good = Proxy()
    a = _adapter(
        monkeypatch,
        connections={"c1": {"_x": 1}},
        proxies={"node:S1": {"bad": bad, "good": good}},
        pm=pm,
    )
    calls = _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    # the outer per-proxy except skipped 'bad' but still handled 'good'
    assert good.is_connected is False
    assert ("emit", (good, "disconnected", "OSRC::good")) in calls
    assert not any(c[0] == "emit" and c[1][0] is bad for c in calls)


def test_proxy_disconnect_sync(monkeypatch):
    p1 = Proxy()

    def _disc():
        p1.disconnect_called += 1

    p1.disconnect = _disc
    a = _adapter(monkeypatch, connections={"c1": {"_x": 1}}, proxies={"node:S1": {"p1": p1}})
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")
    _patch_collab(a, monkeypatch)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert p1.disconnect_called == 1


def test_proxy_disconnect_async(monkeypatch):
    p1 = Proxy()

    async def _disc():
        p1.disconnect_called += 1

    p1.disconnect = _disc
    a = _adapter(monkeypatch, connections={"c1": {"_x": 1}}, proxies={"node:S1": {"p1": p1}})
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")
    _patch_collab(a, monkeypatch)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert p1.disconnect_called == 1


def test_proxy_without_disconnect_skipped(monkeypatch):
    p1 = Proxy()  # Proxy defines no disconnect attribute by default
    a = _adapter(monkeypatch, connections={"c1": {"_x": 1}}, proxies={"node:S1": {"p1": p1}})
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")
    calls = _patch_collab(a, monkeypatch)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert p1.is_connected is False
    assert ("emit", (p1, "disconnected", "OSRC::p1")) in calls


def test_proxy_non_callable_disconnect_skipped(monkeypatch):
    p1 = Proxy()
    p1.disconnect = 5  # not callable
    a = _adapter(monkeypatch, connections={"c1": {"_x": 1}}, proxies={"node:S1": {"p1": p1}})
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")
    _patch_collab(a, monkeypatch)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert p1.is_connected is False


def test_notify_disconnected_raise_is_suppressed(monkeypatch):
    pm = PM(raise_for={"p1"})
    p1 = Proxy()
    a = _adapter(monkeypatch, connections={"c1": {"_x": 1}}, proxies={"node:S1": {"p1": p1}}, pm=pm)
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")
    calls = _patch_collab(a, monkeypatch)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert p1.is_connected is False
    assert a._hb_state == {}
    assert any(c[0] == "save" for c in calls)


def test_cached_offline_notify_partial_failure(monkeypatch):
    pm = PM(raise_for={"bad"})
    good = Proxy()
    bad = Proxy()
    a = _adapter(
        monkeypatch,
        connections={"c1": {"_x": 1}},
        proxies={"node:S1": {"bad": bad, "good": good}},
        pm=pm,
    )
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")
    _patch_collab(a, monkeypatch)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    # both got last_seen set (the per-proxy notify fail is isolated)
    cached = [payload for pname, payload in pm.calls if payload.get("event") == "federated_cached_offline"]
    # each proxy is notified twice (federated_disconnected + federated_cached_offline);
    # PM appends before raising, so 'bad' still records both
    events = [pname for pname, _p in pm.calls]
    assert events.count("good") == 2
    assert events.count("bad") == 2
    assert len(cached) == 2


def test_read_only_last_seen_survives(monkeypatch):
    p1 = Proxy(read_only_last_seen=123.0)
    pm = PM()
    a = _adapter(monkeypatch, connections={"c1": {"_x": 1}}, proxies={"node:S1": {"p1": p1}}, pm=pm)
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")
    _patch_collab(a, monkeypatch)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    cached = [payload for pname, payload in pm.calls if payload.get("event") == "federated_cached_offline"]
    assert len(cached) == 1
    assert cached[0]["last_seen"] == 123.0


def test_save_cache_raise_is_suppressed(monkeypatch):
    a = _adapter(monkeypatch, connections={"c1": {"_x": 1}})
    _patch_collab(a, monkeypatch)

    def _boom():
        raise RuntimeError("save boom")

    a._save_federated_cache = _boom
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert a._hb_state == {}


# ---------------------------------------------------------------------------
# Raising-state dicts (best-effort pop arms in _wire_state / _hb_state)
# ---------------------------------------------------------------------------


class RaisingPopDict(dict):
    """dict whose .pop raises, to drive the best-effort cleanup arms."""

    def pop(self, *args, **kwargs):
        raise RuntimeError("pop boom")


def test_wire_pop_raises_suppressed(monkeypatch):
    a = _adapter(monkeypatch, connections={"c1": {"_x": 1}}, wire=RaisingPopDict())
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")
    _patch_collab(a, monkeypatch)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert any(True for _ in [0])  # completed without raising


def test_hb_pop_raises_suppressed(monkeypatch):
    a = _adapter(monkeypatch, connections={"c1": {"_x": 1}}, hb=RaisingPopDict())
    calls = _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert any(c[0] == "refresh" for c in calls)
    assert any(c[0] == "cleanup" for c in calls)


def test_refresh_raise_suppressed(monkeypatch):
    a = _adapter(monkeypatch, connections={"c1": {"_x": 1}})
    calls = _patch_collab(a, monkeypatch)

    def _boom():
        raise RuntimeError("refresh boom")

    a._refresh_conn_proxies = _boom
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert any(c[0] == "cleanup" for c in calls)


def test_unregister_raise_suppressed(monkeypatch):
    a = _adapter(monkeypatch, connections={"c1": {"_x": 1}})
    calls = _patch_collab(a, monkeypatch)

    def _boom(_cid):
        raise RuntimeError("unreg boom")

    a._unregister_mpath_connection = _boom
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert a._hb_state == {}
    assert any(c[0] == "cleanup" for c in calls)


def test_transition_block_outer_raise_still_saves(monkeypatch):
    # main_port_manager property read raises -> the group-transition block
    # logs "group transition failed" but the save-cache call still runs.
    pm = PM()
    a = _adapter(monkeypatch, connections={"c1": {"_x": 1}}, proxies={"node:S1": {"p1": Proxy()}}, pm=pm)
    calls = _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    class _PMBoom:
        def __get__(self, obj, objtype=None):
            raise RuntimeError("pm boom")

        def __set__(self, obj, value):
            object.__setattr__(obj, "_main_port_manager", value)

    monkeypatch.setattr(type(a), "main_port_manager", _PMBoom())

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    # save-cache sits OUTSIDE the transition try -> still called
    assert any(c[0] == "save" for c in calls)


def test_empty_group_absent_key_still_offline(monkeypatch):
    # No mpath group at all for the peer key: any_paths False -> offline path
    a = _adapter(monkeypatch, connections={"c1": {"_x": 1}}, proxies={"node:S1": {"p1": Proxy()}})
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")
    _patch_collab(a, monkeypatch)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert a._peer_proxies["node:S1"]["p1"].is_connected is False


def test_absent_conn_wire_pop_raises(monkeypatch):
    # No connection record + _wire_state.pop raising: the absent-branch
    # best-effort cleanup must swallow it and still run the empty-peer cleanup.
    a = _adapter(monkeypatch, connections={}, wire=RaisingPopDict())
    calls = _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert ("cleanup", "node:S1") in calls
    assert not any(c[0] == "save" for c in calls)


class _NoConnAttrProxy:
    """Proxy with no is_connected, no metadata, no disconnect attribute."""


class _DisconnectBoom:
    def __init__(self):
        self.disconnected = 0

    def disconnect(self):
        raise RuntimeError("disc boom")


def test_disconnect_call_outside_inner_guard(monkeypatch):
    # First statement to raise on a proxy: no is_connected (the flag set and the
    # link_reason block are both skipped), no metadata, and a disconnect that
    # raises. The outer per-proxy except must handle both proxies.
    boom = _DisconnectBoom()
    ok = Proxy()
    pm = PM()
    a = _adapter(
        monkeypatch,
        connections={"c1": {"_x": 1}},
        proxies={"node:S1": {"boom": boom, "ok": ok}},
        pm=pm,
    )
    calls = _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    # 'boom' hit the outer except (no emit); 'ok' processed normally
    assert not any(c[0] == "emit" and c[1][0] is boom for c in calls)
    assert ("emit", (ok, "disconnected", "OSRC::ok")) in calls
    assert ok.is_connected is False


class _RaisingGroup(dict):
    """dict whose .get raises once the call threshold is hit exactly."""

    calls = 0
    at = 999

    def get(self, key, default=None):
        type(self).calls += 1
        if type(self).calls == type(self).at:
            raise RuntimeError("grp boom")
        return super().get(key, default)


def test_offline_node_fallback_found_nothing(monkeypatch):
    # Stable key not in _peer_proxies, stable server id present, but the node:
    # fallback key is absent too -> proxies revert to the empty fallback and no
    # proxy is marked (covers the `proxies = self._peer_proxies.get(node_key, proxies)`
    # no-hit arm).
    _RaisingGroup.calls = 0
    _RaisingGroup.at = 999
    a = _adapter(
        monkeypatch,
        connections={"c1": {"_x": 1, "server_id": "S9"}},
        proxies={"host:1.2.3.4": {}},  # no node:S9 entry
    )
    calls = _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _key(a, "host:1.2.3.4")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert not any(c[0] == "emit" for c in calls)
    # cache save still runs (no proxies, but the offline transition did happen)
    assert any(c[0] == "save" for c in calls)


class _SecondCallBoom:
    """derive: first call returns the stable key, second call raises."""

    def __init__(self, first):
        self.first = first
        self.n = 0

    def __call__(self, _cid):
        self.n += 1
        if self.n == 1:
            return self.first
        raise RuntimeError("derive boom 2nd")


def test_group_state_derive_fallback_raises(monkeypatch):
    # stable_peer_key is None (record had no handshake identity); the inner
    # _derive_peer_key_from_conn_id call raises -> the OUTER try/except in the
    # mark block swallows it, so no live-state recompute and no offline marking.
    a = _adapter(monkeypatch, connections={"c1": {"_x": 1}}, groups={"node:S1": {"conns": {"c2": {}}}})
    calls = _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _SecondCallBoom(None)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert not any(c[0] == "live_state" for c in calls)
    assert not any(c[0] == "save" for c in calls)
    assert a._hb_state == {}
    assert any(c[0] == "cleanup" and c[1] is None for c in calls)


def test_notice_emit_raise(monkeypatch):
    # The link-notice emission raises: only the metadata read is inside the
    # inner guard, so the outer notice except swallows the emit failure and the
    # rest of the per-proxy flow (info log + PortManager notify) still runs.
    p1 = Proxy()
    calls = _patch_collab(
        a := _adapter(monkeypatch, connections={"c1": {"_x": 1}}, proxies={"node:S1": {"p1": p1}}), monkeypatch
    )

    def _boom(*x):
        raise RuntimeError("emit boom")

    a._emit_link_notice_once = _boom
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert p1.is_connected is False
    assert any(c[0] == "save" for c in calls)
    assert not any(c[0] == "emit" for c in calls)


class _RaisingGetDict(dict):
    """dict whose .get raises."""

    def get(self, key, default=None):
        raise RuntimeError("get boom")


def test_stable_server_id_derive_raises(monkeypatch):
    # connections.get raises -> the best-effort origin server_id falls back to
    # None and the close still proceeds.
    a = _adapter(monkeypatch, connections=_RaisingGetDict())
    calls = _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert ("unreg", ("c1",)) in calls
    assert a._hb_state == {}
    assert any(c[0] == "cleanup" and c[1] == "node:S1" for c in calls)


def test_absent_conn_hb_pop_raises(monkeypatch):
    # Absent record + both state pops raising: both best-effort arms swallow.
    a = _adapter(monkeypatch, connections={}, wire=RaisingPopDict(), hb=RaisingPopDict())
    calls = _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert ("cleanup", "node:S1") in calls


def test_absent_conn_cleanup_raise(monkeypatch):
    # Absent record + the empty-peer session cleanup coroutine raises: the
    # debug-log arm handles it and the method still returns.
    a = _adapter(monkeypatch, connections={})
    calls = _patch_collab(a, monkeypatch)

    async def _boom(_key):
        raise RuntimeError("cleanup boom")

    a._maybe_cleanup_empty_peer = _boom
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert ("unreg", ("c1",)) in calls
    assert not any(c[0] == "save" for c in calls)


def test_derive_none_peer_key(monkeypatch):
    # derive returns None for both calls: peer_key is None, _mpath_groups /
    # _peer_proxies lookups miss, and peer_key.startswith raises AttributeError
    # inside the offline key-resolution try (server_id stays None).
    a = _adapter(monkeypatch, connections={"c1": {"_x": 1}})
    calls = _patch_collab(a, monkeypatch)
    a._derive_peer_key_from_conn_id = _key(a)  # always None

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert not any(c[0] == "emit" for c in calls)
    assert any(c[0] == "save" for c in calls)
    assert a._hb_state == {}


def test_tail_cleanup_raise(monkeypatch):
    # Conn present + the trailing empty-peer cleanup raises: the final
    # debug-log arm handles it after all teardown is done.
    a = _adapter(monkeypatch, connections={"c1": {"_x": 1}})
    calls = _patch_collab(a, monkeypatch)

    async def _boom(_key):
        raise RuntimeError("cleanup boom")

    a._maybe_cleanup_empty_peer = _boom
    a._derive_peer_key_from_conn_id = _key(a, "node:S1")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_close(a))
    finally:
        loop.close()
    assert a.connections == {}
    assert any(c[0] == "refresh" for c in calls)
    assert any(c[0] == "save" for c in calls)
