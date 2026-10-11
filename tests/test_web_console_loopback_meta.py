import json

import pytest

from openmux.server.adapters.loopback import LoopbackAdapter
from openmux.server.auth_manager import AuthManager
from openmux.server.console_manager import ConsoleManager
from openmux.server.port_manager import PortManager
from openmux.server.web_console import WebConsoleAdapter


@pytest.mark.asyncio
async def test_ports_snapshot_loopback_connected_and_id_local():
    # Setup a loopback adapter with one port and attach to PortManager
    la = LoopbackAdapter("loop", {"loopback_ports": [{"name": "L1"}]})
    pm = PortManager([])
    pm.set_unified_adapters([la])
    assert await la.start()

    # Setup minimal WebConsole without starting HTTP server
    wc = WebConsoleAdapter("wc", {"web_console": {"enable_ui": False}})
    auth = AuthManager({"users": []})
    cm = ConsoleManager(pm, auth)
    wc.set_auth_manager(auth)
    wc.set_console_manager(cm)

    snap = wc._get_ports_snapshot()
    entry = next(p for p in snap if p.get("name") == "L1")
    assert entry.get("connected") is True
    # id should be local::<name> when no muxcon server_id is available
    assert entry.get("id", "").endswith("local::L1")


@pytest.mark.asyncio
async def test_meta_broadcast_loopback_connected_true(monkeypatch):
    la = LoopbackAdapter("loop", {"loopback_ports": [{"name": "L2"}]})
    pm = PortManager([])
    pm.set_unified_adapters([la])
    assert await la.start()

    wc = WebConsoleAdapter("wc", {"web_console": {"enable_ui": False}})
    auth = AuthManager({"users": []})
    cm = ConsoleManager(pm, auth)
    wc.set_auth_manager(auth)
    wc.set_console_manager(cm)

    # Inject a fake websocket client subscribed to meta
    cid = "ws:test"

    class DummyWS:
        def __init__(self):
            self.sent = []

        async def send_str(self, s):
            self.sent.append(s)

    ws = DummyWS()
    wc._clients[cid] = ws
    wc._meta_subscribers.setdefault("L2", set()).add(cid)

    await wc._broadcast_meta("L2")

    assert ws.sent, "No meta payload sent"
    # Last sent payload should be an OMXCTRL meta with connected:true
    payload = ws.sent[-1]
    assert payload.startswith("OMXCTRL ")
    meta = json.loads(payload[len("OMXCTRL ") :])
    assert meta.get("type") == "meta"
    assert meta.get("name") == "L2"
    assert meta.get("connected") is True


# --- _broadcast_meta: branch coverage (bracket C901, 27) -------------------
# The loopback test above only covers the happy path (snapshot hit + live
# unified port connected). These pin the remaining unreachable branches on the
# OLD code so the C901 split is provably behavior-preserving: snapshot merge
# / snapshot exceptions, the event-connected precedence (incl. its except),
# the live unified serial-config builder + its inner except, the federated
# is_connected fallback, the outer live except, the snapshot-only payload keys
# (server_chain / last_seen), and the send-loop failure cleanup (ws missing,
# send raising, discard raising, and a non-JSON-serializable payload).
# Everything is faked (no adapter start, no auth) because _broadcast_meta only
# reads console_manager.port_manager, _get_ports_snapshot, _clients and
# _meta_subscribers.
# ---------------------------------------------------------------------------


class _WS_OK:
    def __init__(self):
        self.sent = []

    async def send_str(self, s):
        self.sent.append(s)


class _WS_RAISE:
    async def send_str(self, s):
        raise RuntimeError("send boom")


class _BoolRaise:
    def __bool__(self):
        raise RuntimeError("bool boom")


class _DiscardRaiseSet(set):
    def discard(self, x):
        raise RuntimeError("discard boom")


class _Inner:
    def __init__(self, is_connected=True, config=None):
        self.is_connected = is_connected
        self.config = config


class _InnerCfgRaise:
    is_connected = True

    @property
    def config(self):
        raise RuntimeError("cfg boom")


class _Port:
    def __init__(self, inner=None, is_connected=None, adapter_type=None):
        if inner is not None:
            self.unified_port = inner
        if is_connected is not None:
            self.is_connected = is_connected
        if adapter_type is not None:
            self.adapter_type = adapter_type


class _PM:
    def __init__(self, ports=None):
        self._ports = ports or {}

    def get_port(self, name):
        return self._ports.get(name)


class _StubCM:
    def __init__(self, pm):
        self.port_manager = pm


class _CMRaisePort:
    @property
    def port_manager(self):
        raise RuntimeError("pm boom")


def _mkwc(snapshot=None, cids=None, clients=None, cm=None, port="P"):
    wc = WebConsoleAdapter("wc", {"web_console": {"enable_ui": False}})
    wc.console_manager = cm
    wc._get_ports_snapshot = lambda: list(snapshot) if snapshot is not None else []
    wc._clients = dict(clients) if clients is not None else {}
    wc._meta_subscribers = {port: set(cids)} if cids else {}
    return wc


def _meta_of(ws):
    return json.loads(ws.sent[-1][len("OMXCTRL ") :])


@pytest.mark.asyncio
async def test_broadcast_meta_no_matching_snapshot_fallback():
    ws = _WS_OK()
    wc = _mkwc(snapshot=[{"name": "other", "connected": True}], cids=["c1"], clients={"c1": ws})
    await wc._broadcast_meta("P")
    meta = _meta_of(ws)
    # No snapshot entry named "P" -> info None -> fallback {name, connected: False}
    assert meta.get("name") == "P" and meta.get("connected") is False


@pytest.mark.asyncio
async def test_broadcast_meta_snapshot_raises():
    def _boom():
        raise RuntimeError("snap boom")

    ws = _WS_OK()
    wc = _mkwc(snapshot=[], cids=["c1"], clients={"c1": ws})
    wc._get_ports_snapshot = _boom
    await wc._broadcast_meta("P")
    # _get_ports_snapshot raised -> outer except -> info None -> fallback
    meta = _meta_of(ws)
    assert meta.get("name") == "P" and meta.get("connected") is False


@pytest.mark.asyncio
async def test_broadcast_meta_event_connected_true():
    ws = _WS_OK()
    wc = _mkwc(snapshot=[{"name": "P", "connected": False}], cids=["c1"], clients={"c1": ws})
    await wc._broadcast_meta("P", {"connected": True})
    # event "connected" (True) beats the snapshot (False)
    assert _meta_of(ws).get("connected") is True


@pytest.mark.asyncio
async def test_broadcast_meta_event_connected_bool_raises():
    ws = _WS_OK()
    wc = _mkwc(snapshot=[{"name": "P", "connected": False}], cids=["c1"], clients={"c1": ws})
    await wc._broadcast_meta("P", {"connected": _BoolRaise()})
    # bool(changes["connected"]) raised -> event_connected None -> snapshot False
    assert _meta_of(ws).get("connected") is False


@pytest.mark.asyncio
async def test_broadcast_meta_live_serial_cfg():
    inner = _Inner(
        is_connected=True,
        config={"device": "/dev/ttyS0", "baudrate": 9600, "bytesize": 8, "parity": "N", "stopbits": 1, "flow_control": "none"},
    )
    port = _Port(inner=inner, adapter_type="serial")
    ws = _WS_OK()
    wc = _mkwc(snapshot=[{"name": "P"}], cids=["c1"], clients={"c1": ws}, cm=_StubCM(_PM({"P": port})))
    await wc._broadcast_meta("P")
    meta = _meta_of(ws)
    assert meta.get("connected") is True
    assert meta.get("adapter") == "serial"
    assert meta.get("serial_config")["device"] == "/dev/ttyS0"


@pytest.mark.asyncio
async def test_broadcast_meta_live_serial_cfg_raises():
    port = _Port(adapter_type="serial")
    port.unified_port = _InnerCfgRaise()
    ws = _WS_OK()
    wc = _mkwc(snapshot=[{"name": "P"}], cids=["c1"], clients={"c1": ws}, cm=_StubCM(_PM({"P": port})))
    await wc._broadcast_meta("P")
    meta = _meta_of(ws)
    # config getter raised -> live_serial_cfg None; snapshot has none -> key absent
    assert meta.get("connected") is True
    assert "serial_config" not in meta


@pytest.mark.asyncio
async def test_broadcast_meta_live_is_connected_fallback():
    port = _Port(is_connected=True, adapter_type="remote")  # no unified_port
    ws = _WS_OK()
    wc = _mkwc(snapshot=[{"name": "P"}], cids=["c1"], clients={"c1": ws}, cm=_StubCM(_PM({"P": port})))
    await wc._broadcast_meta("P")
    meta = _meta_of(ws)
    # No unified_port.is_connected -> federated is_connected fallback
    assert meta.get("connected") is True and meta.get("adapter") == "remote"


@pytest.mark.asyncio
async def test_broadcast_meta_live_outer_raises():
    ws = _WS_OK()
    wc = _mkwc(snapshot=[{"name": "P", "connected": False}], cids=["c1"], clients={"c1": ws}, cm=_CMRaisePort())
    await wc._broadcast_meta("P")
    # console_manager.port_manager raised -> outer live except -> None -> snapshot False
    assert _meta_of(ws).get("connected") is False


@pytest.mark.asyncio
async def test_broadcast_meta_snapshot_chain_and_last_seen():
    ws = _WS_OK()
    wc = _mkwc(
        snapshot=[{"name": "P", "connected": True, "server_chain": ["a", "b"], "last_seen": "2026-01-01T00:00:00"}],
        cids=["c1"],
        clients={"c1": ws},
    )
    await wc._broadcast_meta("P")
    meta = _meta_of(ws)
    assert meta.get("server_chain") == ["a", "b"]
    assert meta.get("last_seen") == "2026-01-01T00:00:00"


@pytest.mark.asyncio
async def test_broadcast_meta_subscriber_not_in_clients():
    wc = _mkwc(snapshot=[{"name": "P", "connected": True}], cids=["ghost"], clients={})
    # subscriber id not present in _clients -> ws None -> continue (no send)
    await wc._broadcast_meta("P")


@pytest.mark.asyncio
async def test_broadcast_meta_send_raises_discards_sub():
    ws = _WS_RAISE()
    wc = _mkwc(snapshot=[{"name": "P", "connected": True}], cids=["c1"], clients={"c1": ws})
    await wc._broadcast_meta("P")
    # send failed -> c1 removed from this port's subscribers
    assert "c1" not in (wc._meta_subscribers.get("P") or set())


@pytest.mark.asyncio
async def test_broadcast_meta_discard_raises_inner():
    wc = _mkwc(snapshot=[{"name": "P", "connected": True}], cids=["c1"], clients={"c1": _WS_RAISE()})
    # set subclass: iterates normally for the `subs` snapshot, but .discard
    # raises -> pins the inner cleanup except (send-raise -> discard-raise)
    wc._meta_subscribers["P"] = _DiscardRaiseSet(["c1"])
    await wc._broadcast_meta("P")


@pytest.mark.asyncio
async def test_broadcast_meta_power_feed_key():
    ws = _WS_OK()
    wc = _mkwc(
        snapshot=[{"name": "P", "connected": True, "power": {"feeds": ["out1"], "state": "on"}}],
        cids=["c1"],
        clients={"c1": ws},
    )
    await wc._broadcast_meta("P")
    meta = _meta_of(ws)
    assert meta.get("power") == {"feeds": ["out1"], "state": "on"}


@pytest.mark.asyncio
async def test_broadcast_meta_payload_not_jsonable():
    ws = _WS_OK()
    wc = _mkwc(snapshot=[{"name": "P", "connected": True, "last_seen": {1, 2, 3}}], cids=["c1"], clients={"c1": ws})
    await wc._broadcast_meta("P")
    # json.dumps(set) raises inside the big try -> outer except -> nothing sent
    assert ws.sent == []
