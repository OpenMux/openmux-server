"""Behavioral coverage for WebConsoleAdapter._get_ports_snapshot (63-bracket S1).

Drives the (synchronous) per-port enrichment directly: a real WebConsoleAdapter
with a fake ConsoleManager/PortManager, faked ``_find_power_adapter`` /
``_find_muxcon_adapter`` / ``_resolve_client_meta`` / ``_client_meta``, and port
objects of three shapes (no get_status / get_status dict / get_status raising).
Written green against the PRE-refactor method (old span 3990-4322), so it stays
valid after the body is split into the ``_pinfo_*`` helpers.
"""

from openmux.server.adapters.lifecycle import READINESS_ACTIVE, READINESS_IDLE, READINESS_OFFLINE
from openmux.server.web_console import WebConsoleAdapter


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _PlainPort:
    """A port without get_status: the snapshot starts from {"name": name}."""

    def __init__(self, **attrs):
        for k, v in attrs.items():
            setattr(self, k, v)


class _StatusPort:
    """A port whose get_status returns (or raises) a configured value."""

    def __init__(self, status, **attrs):
        self._status = status
        for k, v in attrs.items():
            setattr(self, k, v)

    def get_status(self):
        if isinstance(self._status, BaseException):
            raise self._status
        return self._status


class _BoomLastSeen(_StatusPort):
    @property
    def last_seen(self):
        raise RuntimeError("ls boom")


class _ClientObj:
    def __init__(self, username=None, client_id=None):
        self.username = username
        self.client_id = client_id


class _Meta:
    def __init__(self, **kw):
        self.serial_config = kw.get("serial_config")
        self.line_status = kw.get("line_status")
        self.origin_server = kw.get("origin_server")
        self.server_chain = kw.get("server_chain") or []
        self.federation_type = kw.get("federation_type")
        self.status_message = kw.get("status_message")


class _Origin:
    def __init__(
        self, server_id="o1", hostname="oh", port=9001, server_type="server", description=None, to_dict=None, to_dict_exc=None
    ):
        self.server_id = server_id
        self.hostname = hostname
        self.port = port
        self.server_type = server_type
        self.description = description
        self._to_dict = to_dict
        self._to_dict_exc = to_dict_exc

    @property
    def to_dict(self):
        if self._to_dict_exc:
            raise self._to_dict_exc
        return self._to_dict


class _ChainEl:
    def __init__(
        self, server_id="c1", hostname=None, port=None, server_type="relay", description=None, to_dict=None, to_dict_exc=None
    ):
        self.server_id = server_id
        self.hostname = hostname
        self.port = port
        self.server_type = server_type
        self.description = description
        self._to_dict = to_dict
        self._to_dict_exc = to_dict_exc

    @property
    def to_dict(self):
        if self._to_dict_exc:
            raise self._to_dict_exc
        return self._to_dict


class _FakePM:
    def __init__(self, ports, ports_exc=None):
        self._ports = ports
        self._ports_exc = ports_exc

    @property
    def ports(self):
        if self._ports_exc:
            raise self._ports_exc
        return self._ports


class _FakeCM:
    def __init__(self, pm, raise_pm=None):
        self._pm = pm
        self._raise = raise_pm

    @property
    def port_manager(self):
        if self._raise:
            raise self._raise
        return self._pm


class _FakeMux:
    def __init__(self, server_id="mux1"):
        self.server_id = server_id


class _FakePower:
    def __init__(self, payload="__payload__", exc=None):
        self.payload = payload
        self._exc = exc

    def port_power_payload(self, name):
        if self._exc:
            raise self._exc
        return self.payload


# --- raising / degenerate fakes (arm specific advisory except arms) ---
class _NoLower:
    """Truthiness-only object with no .lower() (arms the loopback try)."""


class _ThrowingAttr:
    """Meta-like object: normal attrs except one that raises on access."""

    def __init__(self, exc_attr, **defaults):
        object.__setattr__(self, "_exc_attr", exc_attr)
        for k, v in defaults.items():
            object.__setattr__(self, k, v)

    def __getattr__(self, n):
        if n == object.__getattribute__(self, "_exc_attr"):
            raise RuntimeError("boom")
        raise AttributeError(n)


class _ClientMetaItemsBoom:
    """A _client_meta whose .items() raises (arms the outer fallback try)."""

    def items(self):
        raise RuntimeError("items boom")

    def get(self, *a, **k):
        return None


class _ClientMetaGetBoom:
    """A _client_meta whose .get() raises (arms the inner enumeration try)."""

    def __init__(self, items):
        self._items = items

    def items(self):
        return self._items

    def get(self, *a, **k):
        raise RuntimeError("get boom")


class _PortMetaRaise(_StatusPort):
    @property
    def metadata(self):
        raise RuntimeError("meta boom")


class _PortLinkRaise(_StatusPort):
    @property
    def link_reason(self):
        raise RuntimeError("lr boom")


class _PortIsConnRaise(_StatusPort):
    @property
    def is_connected(self):
        raise RuntimeError("ic boom")


class _PortCCRaise(_StatusPort):
    @property
    def connected_clients(self):
        raise RuntimeError("cc boom")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
def _adapter():
    return WebConsoleAdapter("wc", {"web_console": {"host": "127.0.0.1", "port": 0, "enable_ui": False}})


def _run(
    ports, *, pm=None, cm_raises=None, power="__none__", mux="__none__", resolve=None, resolve_exc=None, client_meta=None
):
    ad = _adapter()
    cm = _FakeCM(pm if pm is not None else _FakePM(ports), raise_pm=cm_raises)
    ad.set_console_manager(cm)
    ad._client_meta = client_meta or {}

    ad._find_power_adapter = (lambda: power) if power != "__none__" else (lambda: None)

    if mux == "__none__":
        ad._find_muxcon_adapter = lambda: None
    elif mux == "raises":
        ad._find_muxcon_adapter = lambda: (_ for _ in ()).throw(RuntimeError("mux boom"))
    else:
        ad._find_muxcon_adapter = lambda: _FakeMux(server_id=mux)

    def _resolve(cid):
        if resolve_exc:
            raise resolve_exc
        if not cid:
            return {}
        if callable(resolve):
            return resolve(cid)
        return (resolve or {}).get(cid, {})

    ad._resolve_client_meta = _resolve
    return ad._get_ports_snapshot()


def _by_name(snap, name):
    for entry in snap:
        if entry.get("name") == name:
            return entry
    raise AssertionError("no port %r in %r" % (name, snap))


# ---------------------------------------------------------------------------
# Guard arms
# ---------------------------------------------------------------------------
def test_no_console_manager():
    ad = _adapter()
    ad._find_power_adapter = lambda: None
    ad._find_muxcon_adapter = lambda: None
    ad._resolve_client_meta = lambda cid: {}
    ad._client_meta = {}
    assert ad._get_ports_snapshot() == []


def test_console_manager_pm_attr_raises():
    assert _run({"p": _PlainPort()}, cm_raises=RuntimeError("cm boom")) == []


def test_pm_ports_attr_raises_resets_list():
    pm = _FakePM(None, ports_exc=ValueError("ports boom"))
    assert _run({}, pm=pm) == []


def test_empty_ports():
    assert _run({}) == []


# ---------------------------------------------------------------------------
# Base entry
# ---------------------------------------------------------------------------
def test_plain_port_no_get_status():
    snap = _run({"p1": _PlainPort()})
    assert snap == [{"name": "p1", "id": "local::p1"}]


def test_get_status_dict_used():
    snap = _run({"p1": _StatusPort({"adapter_type": "serial"})})
    assert snap[0]["name"] == "p1"
    assert snap[0]["adapter_type"] == "serial"
    assert snap[0]["id"] == "local::p1"


def test_get_status_missing_name_filled():
    snap = _run({"p1": _StatusPort({"connected": True})})
    assert snap[0]["name"] == "p1"


def test_get_status_raises_port_skipped():
    snap = _run({"p1": _StatusPort(RuntimeError("status boom"))})
    assert snap == []


# ---------------------------------------------------------------------------
# Power feed
# ---------------------------------------------------------------------------
def test_power_payload_attached():
    snap = _run({"p1": _PlainPort()}, power=_FakePower(payload={"feeds": ["a"]}))
    assert snap[0]["power"] == {"feeds": ["a"]}


def test_power_payload_none_omitted():
    snap = _run({"p1": _PlainPort()}, power=_FakePower(payload=None))
    assert "power" not in snap[0]


def test_power_adapter_raises_entry_complete():
    snap = _run({"p1": _PlainPort()}, power=_FakePower(exc=RuntimeError("pdu boom")))
    assert "power" not in snap[0]
    assert snap[0]["id"] == "local::p1"


# ---------------------------------------------------------------------------
# Composite id
# ---------------------------------------------------------------------------
def test_composite_from_origin_server_id():
    snap = _run({"p1": _StatusPort({"origin_server_id": "osrv"})}, mux="mux1")
    assert snap[0]["id"] == "osrv::p1"


def test_composite_via_muxcon():
    snap = _run({"p1": _PlainPort()}, mux="mux1")
    assert snap[0]["id"] == "mux1::p1"


def test_composite_local_fallback():
    snap = _run({"p1": _PlainPort()}, mux=None)
    assert snap[0]["id"] == "local::p1"


def test_composite_mux_lookup_raises():
    snap = _run({"p1": _PlainPort()}, mux="raises")
    assert snap[0]["id"] == "local::p1"


# ---------------------------------------------------------------------------
# last_seen
# ---------------------------------------------------------------------------
def test_last_seen_float():
    snap = _run({"p1": _PlainPort(last_seen=1234.5)})
    assert snap[0]["last_seen"] == 1234.5


def test_last_seen_none_omitted():
    snap = _run({"p1": _PlainPort(last_seen=None)})
    assert "last_seen" not in snap[0]


def test_last_seen_absent_omitted():
    snap = _run({"p1": _PlainPort()})
    assert "last_seen" not in snap[0]


def test_last_seen_raises_entry_complete():
    snap = _run({"p1": _BoomLastSeen({})})
    assert "last_seen" not in snap[0]
    assert snap[0]["id"] == "local::p1"


# ---------------------------------------------------------------------------
# Loopback defaults
# ---------------------------------------------------------------------------
def test_loopback_defaults_applied():
    snap = _run({"lb1": _StatusPort({"adapter_type": "loopback"})})
    e = snap[0]
    assert e["serial_config"] == {
        "device": "loopback:lb1",
        "baudrate": 9600,
        "bytesize": 8,
        "parity": "N",
        "stopbits": 1,
        "flow_control": "none",
    }
    assert e["line_status"] == {"DCD": False, "DSR": True, "CTS": True, "RTS": True, "DTR": True}


def test_loopback_existing_kept():
    sc = {"device": "keep-sc"}
    ls = {"DCD": True}
    snap = _run({"lb1": _StatusPort({"adapter": "loopback", "serial_config": sc, "line_status": ls})})
    assert snap[0]["serial_config"] is sc
    assert snap[0]["line_status"] is ls


def test_non_loopback_untouched():
    snap = _run({"p1": _StatusPort({"adapter_type": "serial"})})
    assert "serial_config" not in snap[0]
    assert "line_status" not in snap[0]


# ---------------------------------------------------------------------------
# Federated enrichment
# ---------------------------------------------------------------------------
def test_federated_sc_ls():
    sc = {"device": "9600"}
    ls = {"DCD": True}
    meta = _Meta(serial_config=sc, line_status=ls)
    e = _by_name(_run({"f1": _StatusPort({}, metadata=meta)}), "f1")
    assert e["serial_config"] is sc
    assert e["line_status"] is ls


def test_federated_origin_to_dict():
    od = {"server_id": "o1", "full": True}
    origin = _Origin(to_dict=lambda: dict(od))
    meta = _Meta(origin_server=origin)
    e = _by_name(_run({"f1": _StatusPort({}, metadata=meta)}), "f1")
    assert e["origin_server_id"] == "o1"
    assert e["origin_server_hostname"] == "oh"
    assert e["origin_server_port"] == 9001
    assert e["origin_server_type"] == "server"
    assert e["origin_server"] == od


def test_federated_origin_full_dict():
    origin = _Origin(to_dict=None)  # not callable -> object fallback
    meta = _Meta(origin_server=origin)
    e = _by_name(_run({"f1": _StatusPort({}, metadata=meta)}), "f1")
    # note: the top-level origin_server_type falls back to the raw value
    # (getattr(st, "value", st)), but the object form does not
    # (getattr(server_type, "value", None)) - pin both.
    assert e["origin_server_type"] == "server"
    assert e["origin_server"] == {
        "server_id": "o1",
        "hostname": "oh",
        "port": 9001,
        "server_type": None,
        "description": None,
    }


def test_federated_origin_to_dict_raises():
    origin = _Origin(to_dict_exc=RuntimeError("od boom"))
    meta = _Meta(origin_server=origin)
    e = _by_name(_run({"f1": _StatusPort({}, metadata=meta)}), "f1")
    assert e["origin_server"] == {"server_id": "o1"}


def test_federated_chain_to_dict_and_fallback():
    chain = [_ChainEl(to_dict=lambda: {"server_id": "c1", "full": True}), "plain-str"]
    meta = _Meta(origin_server=_Origin(), server_chain=chain)
    e = _by_name(_run({"f1": _StatusPort({}, metadata=meta)}), "f1")
    assert e["server_chain"] == ["c1", "plain-str"]
    assert e["server_chain_info"][0] == {"server_id": "c1", "full": True}
    assert e["server_chain_info"][1]["server_id"] == "plain-str"


def test_federated_federation_type_value():
    class FT:
        value = "leaf"

    meta = _Meta(origin_server=_Origin(), federation_type=FT())
    e = _by_name(_run({"f1": _StatusPort({}, metadata=meta)}), "f1")
    assert e["federation_type"] == "leaf"


def test_federated_no_origin_still_readiness():
    meta = _Meta()  # no origin; readiness still derived
    e = _by_name(_run({"f1": _StatusPort({}, metadata=meta)}), "f1")
    assert "origin_server_id" not in e
    assert e["readiness"] == READINESS_IDLE
    assert e["federation_type"] is None


# ---------------------------------------------------------------------------
# Federated tail: status_message + readiness
# ---------------------------------------------------------------------------
def test_status_message_from_meta():
    meta = _Meta(origin_server=_Origin(), status_message="down: reason A")
    e = _by_name(_run({"f1": _StatusPort({}, metadata=meta)}), "f1")
    assert e["status_message"] == "down: reason A"
    assert e["readiness"] == READINESS_OFFLINE


def test_link_reason_precedence():
    meta = _Meta(origin_server=_Origin(), status_message="meta-reason")
    port = _StatusPort({}, metadata=meta)
    port.link_reason = "link-down"
    e = _by_name(_run({"f1": port}), "f1")
    assert e["status_message"] == "link-down"  # port.link_reason overrides meta
    assert e["readiness"] == READINESS_OFFLINE


def test_readiness_active_when_connected():
    meta = _Meta(origin_server=_Origin())
    e = _by_name(_run({"f1": _StatusPort({"connected": True}, metadata=meta)}), "f1")
    assert e["readiness"] == READINESS_ACTIVE


def test_readiness_idle_when_not_connected():
    meta = _Meta(origin_server=_Origin())
    e = _by_name(_run({"f1": _StatusPort({}, metadata=meta)}), "f1")
    assert e["readiness"] == READINESS_IDLE


def test_readiness_preserved_when_already_valid():
    meta = _Meta(origin_server=_Origin())
    e = _by_name(_run({"f1": _StatusPort({"readiness": READINESS_OFFLINE, "connected": True}, metadata=meta)}), "f1")
    assert e["readiness"] == READINESS_OFFLINE  # untouched


# ---------------------------------------------------------------------------
# Clients (connected_clients list)
# ---------------------------------------------------------------------------
def test_clients_list_dict_entries():
    cc = [{"username": "u1", "client_id": "ws:1"}, {"username": None, "client_id": "ws:2"}]
    e = _by_name(_run({"p1": _StatusPort({}, connected_clients=cc)}), "p1")
    assert e["client_count"] == 2
    assert e["clients"] == ["u1", "ws:2"]  # u2 has no username -> falls to client_id
    assert len(e["client_details"]) == 2
    assert e["client_details"][0]["username"] == "u1"


def test_clients_list_object_entries():
    cc = [_ClientObj(username="bob", client_id="ws:9"), _ClientObj(username=None, client_id=None)]
    e = _by_name(_run({"p1": _StatusPort({}, connected_clients=cc)}), "p1")
    assert e["client_count"] == 2
    assert e["clients"] == ["bob"]  # second has no username


def test_clients_list_empty_usernames_no_clients_key():
    cc = [{"username": None, "client_id": None}]
    e = _by_name(_run({"p1": _StatusPort({}, connected_clients=cc)}), "p1")
    assert e["client_count"] == 1
    assert "clients" not in e


def test_clients_meta_fallback():
    cm = {"ws:5": {"port": "p1", "username": "alice"}}
    e = _by_name(_run({"p1": _StatusPort({}, connected_clients=None)}, client_meta=cm), "p1")
    assert e["client_count"] == 1
    assert e["clients"] == ["alice"]
    assert e["client_details"][0]["client_id"] == "ws:5"


def test_clients_meta_fallback_no_match():
    cm = {"ws:5": {"port": "other", "username": "alice"}}
    e = _by_name(_run({"p1": _StatusPort({}, connected_clients=None)}, client_meta=cm), "p1")
    assert "client_count" not in e


def test_clients_merge_extra_from_meta():
    cc = [{"username": "u1", "client_id": "ws:1"}]
    cm = {"ws:1": {"port": "p1", "username": "u1"}, "ws:2": {"port": "p1", "username": "u2"}}
    resolve = {"ws:2": {"ip": "1.2.3.4", "type": "tcp", "username": "u2"}}
    e = _by_name(_run({"p1": _StatusPort({}, connected_clients=cc)}, client_meta=cm, resolve=resolve), "p1")
    # ws:1 already in cc; ws:2 is extra from _client_meta
    assert e["client_count"] == 2
    assert "u2" in e["clients"]
    assert any(d["client_id"] == "ws:2" and d["ip"] == "1.2.3.4" for d in e["client_details"])


def test_clients_merge_no_extra_double_counted():
    cc = [{"username": "u1", "client_id": "ws:1"}]
    cm = {"ws:1": {"port": "p1", "username": "u1"}}
    e = _by_name(_run({"p1": _StatusPort({}, connected_clients=cc)}, client_meta=cm), "p1")
    # ws:1 already in cc, so NOT double-counted
    assert e["client_count"] == 1


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------
def test_natural_sort_order():
    # natural_sort_key orders embedded numbers numerically: lb2 < lb10
    snap = _run({"lb10": _PlainPort(), "zz": _PlainPort(), "lb2": _PlainPort()})
    names = [e["name"] for e in snap]
    assert names == ["lb2", "lb10", "zz"]


# ---------------------------------------------------------------------------
# Advisory except arms (raise one collaborator at a time; the run continues)
# ---------------------------------------------------------------------------
def test_loopback_no_lower_arms_inner():
    # adapter_type present but neither key is a str -> .lower() raises
    e = _by_name(_run({"lb": _StatusPort({"adapter_type": _NoLower()})}), "lb")
    assert "serial_config" not in e


def test_federated_wrapper_arms():
    # port.metadata access raises -> the outer enrichment wrapper swallows it
    e = _by_name(_run({"f1": _PortMetaRaise({})}), "f1")
    assert e["id"] == "local::f1"
    assert "serial_config" not in e
    assert "readiness" not in e  # the whole federated enrichment was skipped


def test_federated_origin_arms():
    # meta present but origin_server access raises -> the big origin try
    # swallows and aborts chain/ftype/tail too (they sit inside that try)
    meta = _ThrowingAttr("origin_server")
    e = _by_name(_run({"f1": _StatusPort({}, metadata=meta)}), "f1")
    assert "origin_server_id" not in e
    assert "server_chain" not in e
    assert "federation_type" not in e
    assert "readiness" not in e
    assert e["id"] == "local::f1"


def test_federated_chain_info_degrades():
    # one chain element's to_dict raises -> the whole chain_info rebuilds
    # from the server_id list (the except assigns, it does not pass)
    chain = ["a", _ChainEl(server_id="b", to_dict_exc=RuntimeError("cb"))]
    meta = _Meta(origin_server=_Origin(), server_chain=chain)
    e = _by_name(_run({"f1": _StatusPort({}, metadata=meta)}), "f1")
    assert e["server_chain_info"] == [{"server_id": "a"}, {"server_id": "b"}]


def test_federated_sm_arms():
    # meta.status_message access raises -> meta reason skipped, readiness still
    meta = _ThrowingAttr("status_message", origin_server=_Origin())
    e = _by_name(_run({"f1": _StatusPort({}, metadata=meta)}), "f1")
    assert "status_message" not in e
    assert e["readiness"] == READINESS_IDLE


def test_federated_lr_arms():
    # port.link_reason access raises -> link reason skipped
    meta = _Meta(origin_server=_Origin(), status_message="meta-reason")
    port = _PortLinkRaise({}, metadata=meta)
    e = _by_name(_run({"f1": port}), "f1")
    assert e["status_message"] == "meta-reason"


def test_federated_readiness_arms():
    # is_connected access raises inside readiness derivation -> no readiness key
    meta = _Meta(origin_server=_Origin())
    port = _PortIsConnRaise({}, metadata=meta)
    e = _by_name(_run({"f1": port}), "f1")
    assert "readiness" not in e


def test_clients_detail_raises_arms():
    def _resolve_boom(cid):
        if cid == "ws:1":
            raise RuntimeError("resolve boom")
        return {}

    cc = [{"username": "u1", "client_id": "ws:1"}, {"client_id": "ws:2"}]
    e = _by_name(_run({"p1": _StatusPort({}, connected_clients=cc)}, resolve=_resolve_boom), "p1")
    # count and usernames intact; ws:1 got no detail entry
    assert e["client_count"] == 2
    assert e["clients"] == ["u1", "ws:2"]
    assert [d["client_id"] for d in e["client_details"]] == ["ws:2"]


def test_clients_client_meta_get_arms():
    # ex_usernames build (_client_meta.get raises) -> enumeration degrades,
    # but the extra clients are still counted
    cm = _ClientMetaGetBoom([("ws:2", {"port": "p1", "username": "u2"})])
    cc = [{"client_id": "ws:1"}]
    e = _by_name(_run({"p1": _StatusPort({}, connected_clients=cc)}, client_meta=cm), "p1")
    # extra counted; ws:1's cc username stays, the extra-username block degraded
    assert e["client_count"] == 2
    assert e["clients"] == ["ws:1"]
    assert e["client_details"] == [{"client_id": "ws:1", "username": "ws:1", "ip": None, "type": None}]


def test_clients_extra_resolve_arms():
    # per-extra resolve raises -> that detail is skipped (continue), count kept
    def _resolve_boom(cid):
        if cid == "ws:2":
            raise RuntimeError("extra boom")
        return {}

    cm = {"ws:2": {"port": "p1", "username": "u2"}}
    cc = [{"client_id": "ws:1"}]
    e = _by_name(_run({"p1": _StatusPort({}, connected_clients=cc)}, client_meta=cm, resolve=_resolve_boom), "p1")
    assert e["client_count"] == 2
    assert e["clients"] == ["ws:1", "u2"]  # cc username, then the extra
    # only ws:1's own detail survived; ws:2's raised and was skipped
    assert e["client_details"] == [{"client_id": "ws:1", "username": "ws:1", "ip": None, "type": None}]


def test_clients_cc_iter_arms():
    class _BoomList(list):
        def __iter__(self):
            raise RuntimeError("iter boom")

    e = _by_name(_run({"p1": _StatusPort({}, connected_clients=_BoomList())}), "p1")
    # len() ran before the first next() -> count 0; the cc loop boomed, so no
    # usernames/clients key and no details
    assert e["client_count"] == 0
    assert "clients" not in e
    assert e["id"] == "local::p1"


def test_clients_cc_getattr_arms():
    # connected_clients access raises -> clients wrapper swallowed
    e = _by_name(_run({"p1": _PortCCRaise({})}), "p1")
    assert "client_count" not in e
    assert e["id"] == "local::p1"


def test_clients_meta_fallback_items_arms():
    # _client_meta.items() raises -> outer fallback try swallowed
    e = _by_name(_run({"p1": _StatusPort({}, connected_clients=None)}, client_meta=_ClientMetaItemsBoom()), "p1")
    assert "client_count" not in e
    assert e["id"] == "local::p1"


def test_clients_meta_fallback_resolve_arms():
    # per-extra resolve raises -> that detail skipped, extras still listed
    def _resolve_boom(cid):
        if cid == "ws:2":
            raise RuntimeError("fallback boom")
        return {}

    cm = {"ws:2": {"port": "p1", "username": "u2"}}
    e = _by_name(_run({"p1": _StatusPort({}, connected_clients=None)}, client_meta=cm, resolve=_resolve_boom), "p1")
    assert e["client_count"] == 1
    assert e["clients"] == ["u2"]  # usernames list built
    assert "client_details" not in e  # the only detail raised -> det empty


def test_clients_dict_username_used():
    # a dict cc entry WITH username -> the "if u" append runs
    cc = [{"username": "uX", "client_id": "ws:x"}]
    e = _by_name(_run({"p1": _StatusPort({}, connected_clients=cc)}), "p1")
    assert e["clients"] == ["uX"]  # username wins over the client_id fallback


def test_clients_detail_enriched_from_meta():
    # a truthy resolved meta -> the ip/type enrichment lines run
    cc = [{"client_id": "ws:1"}]
    resolve = {"ws:1": {"ip": "10.0.0.9", "type": "web"}}
    e = _by_name(_run({"p1": _StatusPort({}, connected_clients=cc)}, resolve=resolve), "p1")
    assert e["clients"] == ["ws:1"]
    assert e["client_details"] == [{"client_id": "ws:1", "username": "ws:1", "ip": "10.0.0.9", "type": "web"}]


def test_clients_cc_entry_arms():
    # a cc object whose client_id property raises AttributeError on the FIRST
    # access (suppressed by getattr in the username loop) but RuntimeError on
    # the SECOND (the merge existing_ids loop) -> that entry's merge except
    class _StatefulCC:
        def __init__(self):
            self._n = 0

        @property
        def username(self):
            return "u1"

        @property
        def client_id(self):
            self._n += 1
            if self._n == 1:
                raise AttributeError("client_id")
            raise RuntimeError("boom")

    cc = [_StatefulCC()]
    e = _by_name(_run({"p1": _StatusPort({}, connected_clients=cc)}), "p1")
    assert e["client_count"] == 1
    assert e["clients"] == ["u1"]  # username survived the first loop
    assert e["client_details"] == [{"client_id": None, "username": "u1", "ip": None, "type": None}]


def test_clients_merge_items_arms():
    # _client_meta.items() raises in the merge (list branch) -> merge outer
    # swallowed; the cc-list part of the entry stands
    cm = _ClientMetaItemsBoom()
    cc = [{"client_id": "ws:1"}]
    e = _by_name(_run({"p1": _StatusPort({}, connected_clients=cc)}, client_meta=cm), "p1")
    assert e["client_count"] == 1
    assert e["clients"] == ["ws:1"]  # set by the cc loop, before the merge
    assert "client_details" not in e  # the merge aborted before details was committed


def test_clients_fallback_username_arms():
    # _client_meta.get raises while building the fallback usernames list ->
    # the inner try swallows everything after it (no clients, no details)
    cm = _ClientMetaGetBoom([("ws:1", {"port": "p1", "username": "u1"})])
    e = _by_name(_run({"p1": _StatusPort({}, connected_clients=None)}, client_meta=cm), "p1")
    assert e["client_count"] == 1
    assert "clients" not in e
    assert "client_details" not in e


def test_dedupe_get_boom_arms():
    # two pm entries whose status name collides; the info dicts' .get raises
    # -> both dedupe (4307/4309) and sort (4319/4321) except arms fire: the
    # second "dup" is NOT removed and the list is returned unsorted-as-is
    class _GetBoomDict(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError("get boom")

    class _DupPort(_StatusPort):
        def get_status(self):
            d = _GetBoomDict()
            d["name"] = "dup"
            return d

    snap = _run({"k1": _DupPort({}), "k2": _DupPort({})})
    assert snap == [
        {"name": "dup", "id": "local::dup"},
        {"name": "dup", "id": "local::dup"},
    ]
