"""Coverage for UnifiedMuxConAdapter._update_peer_proxies_live_state — 30-bracket S1.

Drives every previously-uncovered branch (defensive except/pass arms and a few
structural branches) through the real method using fakes with property-backed
attributes that raise / return controlled values. Coverage-first: all tests must
pass on the pre-refactor code before any refactoring is applied.
"""

import time

from openmux.server.adapters.muxcon import UnifiedMuxConAdapter

# ── fakes ────────────────────────────────────────────────────────────────────


def _prop(fget, fset):
    return property(fget, fset)


def _raise_set(self, v):
    raise RuntimeError("setter raises")


class _Origin:
    def __init__(self, server_id):
        self.server_id = server_id


class _Meta:
    def __init__(self, server_id):
        self.origin_server = _Origin(server_id)


class _Proxy:
    """Baseline remote-port-proxy stub. All attributes normal."""

    def __init__(self, is_connected=True, server_id="svr", last_seen=None):
        self._ic = is_connected
        self._server_id = server_id
        self.link_reason = ""
        self._last_link_notice = None
        self._last_link_notice_ts = 0.0
        self.data_queue = None
        self._last_seen = last_seen if last_seen is not None else time.time()

    @property
    def is_connected(self):
        return self._ic

    @is_connected.setter
    def is_connected(self, v):
        self._ic = v

    @property
    def metadata(self):
        return _Meta(self._server_id)

    @property
    def last_seen(self):
        return self._last_seen


class _SetICRaises(_Proxy):
    # Redefine the FULL property (a bare setter line in a subclass body would
    # fail to resolve the parent's property object).
    is_connected = _prop(_Proxy.is_connected.fget, _raise_set)  # type: ignore[reportPropertyTypeMismatch]


class _GetICRaises:
    def _get_ic(self):
        raise RuntimeError("get is_connected fails")

    is_connected = _prop(_get_ic, _raise_set)


class _MetaRaises(_Proxy):
    @property
    def metadata(self):
        raise RuntimeError("metadata access fails")


class _BadLastSeen(_Proxy):
    @property
    def last_seen(self):
        return "not-a-number"


# Normal proxy (metadata resolves server_id) but the link_reason setter
# raises — covers the _set_link_reason try/except now that the reason write
# sits in its own helper. __init__ is re-declared (not super()'d) because
# _Proxy.__init__ assigns self.link_reason = "", which would trip the
# raising setter during construction.
class _SetLinkRaise(_Proxy):
    def __init__(self, is_connected=True, server_id="svr", last_seen=None):
        self._ic = is_connected
        self._server_id = server_id
        self._last_link_notice = None
        self._last_link_notice_ts = 0.0
        self.data_queue = None
        self._last_seen = last_seen if last_seen is not None else time.time()

    @property
    def link_reason(self):
        return getattr(self, "_link_reason", "")

    @link_reason.setter
    def link_reason(self, v):
        raise RuntimeError("set link_reason fails")


class _PMOK:
    def __init__(self):
        self.calls = []

    def notify_meta_updated(self, name, payload):
        self.calls.append((name, payload))


class _PMRaises:
    def notify_meta_updated(self, name, payload):
        raise RuntimeError("notify fails")


class _RaiseMul:
    def __mul__(self, other):
        raise RuntimeError("heartbeat_interval * 1.5 fails")


class _RaiseEverything:
    """Truthy stand-in whose keys()/get() both raise — forces the peer_keys
    enumeration except and the outer proxy-fetch except."""

    def keys(self):
        raise RuntimeError("keys fails")

    def get(self, k, d=None):
        raise RuntimeError("get fails")

    def __bool__(self):
        return True


class _RaiseKeysDict(dict):
    def keys(self):
        raise RuntimeError("mpath groups keys fails")


# ── helpers ──────────────────────────────────────────────────────────────────


def _adapter():
    return UnifiedMuxConAdapter("mx", {"muxcon": {"federated_cache_enabled": False}})


def _grp(conns, primary=None):
    return {"primary": primary, "conns": conns}


def _conn(last_rx):
    return {"last_rx_seen": last_rx, "last_seen": last_rx}


def run(ad, peer_key=None):
    ad._update_peer_proxies_live_state(peer_key)


def _now() -> float:
    """Fresh wall-clock now, per test run.

    A module-level `time.time()` capture goes stale once the suite runs
    longer than the multipath stale window (45 s), which flips
    "fresh" connections to stale and fails `test_recovery_flips_to_
    connected`. Read the clock at each use site instead."""
    return time.time()


# ── tests ────────────────────────────────────────────────────────────────────


def test_live_no_flip():
    # Baseline: fresh path, proxy connected -> no state change.
    ad = _adapter()
    ad._mpath_groups = {"pk": _grp({"c1": _conn(_now())})}
    ad._peer_proxies = {"pk": {"p1": _Proxy(is_connected=True)}}
    run(ad)  # should not raise


def test_stale_flips_to_disconnected():
    ad = _adapter()
    ad.main_port_manager = _PMOK()
    ad._mpath_groups = {"pk": _grp({"c1": _conn(_now() - 1000)})}
    ad._peer_proxies = {"pk": {"p1": _Proxy(is_connected=True, server_id="peerS")}}
    run(ad)
    assert ad._peer_proxies["pk"]["p1"].is_connected is False
    assert "peerS" in ad._peer_proxies["pk"]["p1"].link_reason


def test_heartbeat_interval_raises():
    # 4144/4145: self.heartbeat_interval * 1.5 raises -> hb_window = 60.0
    ad = _adapter()
    ad.heartbeat_interval = _RaiseMul()
    ad._mpath_groups = {"pk": _grp({"c1": _conn(_now())})}
    ad._peer_proxies = {"pk": {"p1": _Proxy(is_connected=True)}}
    run(ad)  # must not raise


def test_mpath_primary_stale_sec_non_numeric():
    # 4149/4150: float(self.mpath_primary_stale_sec) raises -> 45.0
    ad = _adapter()
    ad.mpath_primary_stale_sec = "not-a-number"  # type: ignore[reportAttributeAccessIssue]
    ad._mpath_groups = {"pk": _grp({"c1": _conn(_now())})}
    ad._peer_proxies = {"pk": {"p1": _Proxy(is_connected=True)}}
    run(ad)  # must not raise


def test_frozen_conn_skipped():
    # 4162: frozen connection is skipped in _has_live_path
    ad = _adapter()
    ad._mpath_groups = {"pk": _grp({"c1": _conn(_now())})}
    ad._fault_state = {"c1": {"frozen": True}}
    ad._peer_proxies = {"pk": {"p1": _Proxy(is_connected=True)}}
    run(ad)  # frozen -> no live path -> flip to disconnected; must not raise


def test_mpath_groups_keys_raises():
    # 4179/4180: list((self._mpath_groups or {}).keys()) raises -> peer_keys = []
    ad = _adapter()
    d = _RaiseKeysDict({"pk": _grp({"c1": _conn(_now())})})
    ad._mpath_groups = d
    ad._peer_proxies = {"pk": {"p1": _Proxy(is_connected=True)}}
    run(ad)  # peer_keys ends empty -> no-op; must not raise


def test_proxy_peer_not_in_groups():
    # 4184/4185: proxy peer with no group entry is appended to peer_keys
    ad = _adapter()
    ad._mpath_groups = {}
    ad._peer_proxies = {"pkX": {"p1": _Proxy(is_connected=True)}}
    run(ad)  # pkX added, no live path -> flip to disconnected; must not raise
    assert ad._peer_proxies["pkX"]["p1"].is_connected is False


def test_peer_proxies_keys_raises():
    # 4186/4188: (self._peer_proxies or {}).keys() raises -> pass
    ad = _adapter()
    ad._mpath_groups = {}
    ad._peer_proxies = _RaiseEverything() # type: ignore[reportAttributeAccessIssue]
    run(ad)  # peer_keys empty -> no-op; must not raise


def test_set_is_connected_raises():
    # 4203/4205: proxy.is_connected = live raises -> pass
    ad = _adapter()
    ad._mpath_groups = {"pk": _grp({"c1": _conn(_now() - 1000)})}
    ad._peer_proxies = {"pk": {"p1": _SetICRaises(is_connected=True)}}
    run(ad)  # must not raise


def test_metadata_raises_reason_and_notice():
    # 4219/4221 (reason block) + 4233/4235 (notice block): metadata getter raises
    ad = _adapter()
    ad._mpath_groups = {"pk": _grp({"c1": _conn(_now() - 1000)})}
    ad._peer_proxies = {"pk": {"p1": _MetaRaises(is_connected=True, server_id="peerS")}}
    run(ad)  # must not raise


def test_last_seen_float_raises():
    # 4244/4246: float(getattr(proxy, "last_seen")) raises -> pass
    ad = _adapter()
    ad.main_port_manager = _PMOK()
    ad._mpath_groups = {"pk": _grp({"c1": _conn(_now() - 1000)})}
    ad._peer_proxies = {"pk": {"p1": _BadLastSeen(is_connected=True, server_id="peerS")}}
    run(ad)  # must not raise


def test_notify_raises():
    # 4248/4250: pm.notify_meta_updated raises -> pass
    ad = _adapter()
    ad.main_port_manager = _PMRaises()
    ad._mpath_groups = {"pk": _grp({"c1": _conn(_now() - 1000)})}
    ad._peer_proxies = {"pk": {"p1": _Proxy(is_connected=True, server_id="peerS")}}
    run(ad)  # must not raise


def test_link_reason_set_raises():
    # 4210/4212: metadata resolves (server_id present) but proxy.link_reason =
    # fails -> except/pass in _set_link_reason. Notice still emitted.
    ad = _adapter()
    ad._mpath_groups = {"pk": _grp({"c1": _conn(_now() - 1000)})}
    ad._peer_proxies = {"pk": {"p1": _SetLinkRaise(is_connected=True, server_id="peerS")}}
    run(ad)  # must not raise


def test_emit_link_notice_raises():
    # 4252/4254: _emit_link_notice_once raises -> except/pass; notify still runs
    ad = _adapter()
    ad.main_port_manager = _PMOK()
    ad._mpath_groups = {"pk": _grp({"c1": _conn(_now() - 1000)})}
    ad._peer_proxies = {"pk": {"p1": _Proxy(is_connected=True, server_id="peerS")}}

    def _boom(proxy, state, src_path):
        raise RuntimeError("emit link notice fails")

    ad._emit_link_notice_once = _boom
    run(ad)  # must not raise


def test_recovery_flips_to_connected():
    # Flip direction live=True: link_reason cleared, no notice emitted.
    ad = _adapter()
    ad.main_port_manager = _PMOK()
    ad._mpath_groups = {"pk": _grp({"c1": _conn(_now())})}
    p = _Proxy(is_connected=False, server_id="peerS")
    p.link_reason = "was down"
    ad._peer_proxies = {"pk": {"p1": p}}
    run(ad)
    assert ad._peer_proxies["pk"]["p1"].is_connected is True
    assert ad._peer_proxies["pk"]["p1"].link_reason == ""


def test_get_is_connected_raises():
    # 4251/4252: bool(getattr(proxy, "is_connected", True)) raises -> continue
    ad = _adapter()
    ad._mpath_groups = {"pk": _grp({"c1": _conn(_now() - 1000)})}
    ad._peer_proxies = {"pk": {"p1": _GetICRaises()}}
    run(ad)  # must not raise


def test_outer_proxy_fetch_raises():
    # 4253/4255: (self._peer_proxies or {}).get(pk) in the for loop raises -> outer pass
    ad = _adapter()
    ad._mpath_groups = {"pk1": _grp({"c1": _conn(_now())})}
    ad._peer_proxies = _RaiseEverything() # type: ignore[reportAttributeAccessIssue]
    run(ad)  # must not raise
