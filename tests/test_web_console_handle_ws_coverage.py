"""Pinning/coverage tests for the web-console websocket handler `handle_ws`.

`handle_ws` is a module-level async function. It builds a WebSocketResponse,
attaches the client to a port via the console manager, pumps inbound messages
until the socket closes (or it is cancelled), then tears down.

The handler does NOT type-check the websocket, so a scripted fake driven by
`async for` works once the module's `web.WebSocketResponse` is patched. The
adapter is a lightweight stub (owning the tracking dicts, logger, and the
optional _get_client_ip/_broadcast_meta hooks) plus a stub console manager;
only `_get_adapter` and `safe_get_port` are monkeypatched, so the module-level
holder/capacity readers run for real against a port object the stub
port_manager exposes.

These tests pin the PRE-refactor behavior; they must be green before and after
the refactor, at 100% of the `handle_ws` span.
"""

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from aiohttp import WSMessage, WSMsgType
from aiohttp.web import HTTPBadRequest

import openmux.server.web_console as wc
from openmux.server.web_console import handle_ws


def _boom():
    raise RuntimeError("boom")


def _text(data):
    return WSMessage(WSMsgType.TEXT, data, None)


def _binary(data):
    return WSMessage(WSMsgType.BINARY, data, None)


def _close():
    return WSMessage(WSMsgType.CLOSE, None, None)


def _ping():
    return WSMessage(WSMsgType.PING, None, None)


def _ctrl_frame(data):
    return _text("OMXCTRL " + json.dumps(data))


def _cid_of(ws):
    return f"ws:{id(ws)}"


class Log:
    def __init__(self):
        self.records = []

    def __getattr__(self, name):
        def _f(msg, *a, **k):
            self.records.append((name, msg))
            return None

        return _f

    def has(self, lvl):
        return [m for (l, m) in self.records if l == lvl]


class FakeWS:
    def __init__(self, incoming=()):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False
        self.close_code = None
        self.close_calls = 0

    async def prepare(self, request):
        return None

    async def send_str(self, text):
        self.sent.append(text)

    async def send_bytes(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.incoming:
            raise StopAsyncIteration
        return self.incoming.pop(0)

    def exception(self):
        return None

    async def close(self, code=None, message=None):
        self.close_calls += 1
        self.closed = True
        self.close_code = code


class _CondWS(FakeWS):
    """send_str that, after `n` SUCCESSFUL sends, raises on the next frame whose
    text contains `when`. Other frames record normally. Arms the best-effort
    control-message except arms (client_mode, presence, replies)."""

    def __init__(self, incoming=(), n=0, when=""):
        super().__init__(incoming)
        self.n = n
        self.when = when

    async def send_str(self, text):
        if len(self.sent) >= self.n and self.when and self.when in text:
            _boom()
        self.sent.append(text)


class _CloseBoomWS(FakeWS):
    async def close(self, code=None, message=None):
        _boom()


class _AnextBoomWS(FakeWS):
    """First __anext__ raises (loop-level error); later yields stop cleanly."""

    def __init__(self, incoming=()):
        super().__init__(incoming)
        self._boomed = False

    async def __anext__(self):
        if not self._boomed:
            self._boomed = True
            raise RuntimeError("loop boom")
        if not self.incoming:
            raise StopAsyncIteration
        return self.incoming.pop(0)


class _CancelMsg:
    @property
    def type(self):
        raise asyncio.CancelledError()


class _PortObj:
    def __init__(self, connected_clients=None, maxrw=2, fed=False, up=True):
        self.connected_clients = connected_clients or []
        self.max_read_write_users = maxrw
        self.remote_port_name = "remote1" if fed else None
        self.is_connected = up


class _BoomIsPort:
    remote_port_name = "remote1"

    @property
    def is_connected(self):
        _boom()


class _PM:
    def __init__(self, cm):
        self._cm = cm
        self.ports = {}
        self.writes = []

    async def write_to_port(self, port_name, data, client_id):
        if self._cm.write_boom:
            _boom()
        self.writes.append((port_name, data, client_id))

    def get_scrollback(self, port_name):
        if self._cm.pm_boom:
            _boom()
        return self._cm.scrollback


class CM:
    def __init__(self):
        self.result = (True, "read-only", None)
        self.connect_boom = False
        self.register_boom = False
        self.unregister_boom = False
        self.disconnect_boom = False
        self.viewers = []
        self.viewers_boom = False
        self.promote = True
        self.promote_boom = False
        self.demote_boom = False
        self.take = (False, "not_attached")
        self.take_boom = False
        self.write_boom = False
        self.pm_boom = False
        self.scrollback = b""
        self.pm = _PM(self)
        self.connect_calls = []
        self.registered = []
        self.unregistered = []
        self.connected = []
        self.promoted = []
        self.demoted = []
        self.taken = []
        self.pop_calls = []

    async def connect_client_to_port(self, client_id, port_name, username):
        if self.connect_boom:
            _boom()
        self.connect_calls.append((client_id, port_name, username))
        return self.result

    def register_client_channel(self, client_id, client_manager):
        if self.register_boom:
            _boom()
        self.registered.append(client_id)

    async def disconnect_client_from_port(self, client_id, port_name):
        if self.disconnect_boom:
            _boom()
        self.connected.append((client_id, port_name))

    def unregister_client_channel(self, client_id):
        if self.unregister_boom:
            _boom()
        self.unregistered.append(client_id)

    def get_viewers_display(self, port_name):
        if self.viewers_boom:
            _boom()
        return self.viewers

    async def promote_client_to_read_write(self, client_id, port_name):
        if self.promote_boom:
            _boom()
        self.promoted.append((client_id, port_name))
        return self.promote

    async def demote_client_to_read_only(self, client_id, port_name, notify_origin=True):
        if self.demote_boom:
            _boom()
        self.demoted.append((client_id, port_name))

    async def take_write_slot(self, client_id, port_name, target=None):
        if self.take_boom:
            _boom()
        self.taken.append((client_id, port_name, target))
        return self.take

    def _track_pop(self, key):
        self.pop_calls.append(key)

    @property
    def port_manager(self):
        return self.pm


class _NoConnect:
    def __getattr__(self, name):
        raise AttributeError(name)


class _SetdefaultBoom:
    def setdefault(self, *a, **k):
        _boom()


class _GetBoom(dict):
    def get(self, *a, **k):
        _boom()


class _PopBoomDict(dict):
    def __init__(self):
        super().__init__()
        self.pop_calls = []

    def pop(self, *a, **k):
        self.pop_calls.append(a[0] if a else None)
        raise RuntimeError("pop boom")


class _BoomQuery:
    def get(self, *a, **k):
        _boom()


class Adapter:
    def __init__(self):
        self.logger = Log()
        self._clients = {}
        self._ws_to_client = {}
        self._client_meta = {}
        self._meta_subscribers = {}
        self.console_manager = None
        self.pdu = None
        self.pdu_boom = False
        self.broadcasts = []
        self.broadcast_boom = False

    def _find_power_adapter(self):
        if self.pdu_boom:
            _boom()
        return self.pdu

    async def _broadcast_meta(self, port_name, changes=None):
        if self.broadcast_boom:
            _boom()
        self.broadcasts.append(port_name)


class _IPAdapter(Adapter):
    def __init__(self):
        super().__init__()
        self.ip_calls = 0

    def _get_client_ip(self, request):
        self.ip_calls += 1
        return "1.2.3.4"


class _IPAdapterNoMeta(Adapter):
    """_client_meta is absent, so the IP try's init branch (adapter._client_meta = {}) runs."""

    def __init__(self):
        super().__init__()
        del self._client_meta

    def _get_client_ip(self, request):
        return "1.2.3.4"


class _IPAdapterNone(Adapter):
    def _get_client_ip(self, request):
        return None


class _IPAdapterBoom(Adapter):
    def _get_client_ip(self, request):
        _boom()


class Pdu:
    def __init__(self, frame, boom=False):
        self.frame = frame
        self.boom = boom
        self.calls = []

    async def handle_power_frame(self, port_name, req, username, client_id=None):
        if self.boom:
            _boom()
        self.calls.append((port_name, req, username, client_id))
        return self.frame


def _ctrl(ws):
    return [json.loads(t[len("OMXCTRL ") :]) for t in ws.sent if isinstance(t, str) and t.startswith("OMXCTRL ")]


def _ctrl_of(ws, t):
    return [c for c in _ctrl(ws) if c.get("type") == t]


def _one(ws, t):
    frames = _ctrl_of(ws, t)
    assert frames, f"no {t} frame; got {_ctrl(ws)}"
    return frames[0]


def _after_cmd(ws, t):
    """The handler reply for a control command = the LAST `t` frame sent. The
    command itself is received (not in ws.sent), and the connect phase sends at
    most one `t` frame, so the command's reply is the most recent one."""
    frames = _ctrl_of(ws, t)
    assert frames, f"no {t} frame; sent={ws.sent}"
    return frames[-1]


def _byte_payloads(ws):
    return [b for b in ws.sent if isinstance(b, (bytes, bytearray))]


def _req(port_name="console1", username="u", meta="1", rel_query=None):
    request = MagicMock()
    request.match_info = {"port_name": port_name}
    request.get = lambda key, default=None: username if key == "username" else default
    if rel_query is not None:
        request.rel_url.query = rel_query
    else:
        request.rel_url.query = {} if meta is None else {"meta": meta}
    return request


async def _run(
    monkeypatch,
    incoming=None,
    result=(True, "read-only", None),
    *,
    no_connect=False,
    cm_attrs=None,
    ad_attrs=None,
    port=None,
    safe_boom=False,
    client_meta=None,
    meta_subs=None,
    req_kw=None,
    adapter_cls=Adapter,
    ws_factory=None,
    incoming_factory=None,
):
    ad = adapter_cls()
    if client_meta is not None:
        ad._client_meta = client_meta
    if meta_subs is not None:
        ad._meta_subscribers = meta_subs
    cm = _NoConnect() if no_connect else CM()
    if not no_connect:
        cm.result = result
        if cm_attrs:
            for key, val in cm_attrs.items():
                setattr(cm, key, val)
    if port is not None:
        cm.pm.ports["console1"] = port
    ad.console_manager = cm
    if ad_attrs:
        for key, val in ad_attrs.items():
            setattr(ad, key, val)

    monkeypatch.setattr(wc, "_get_adapter", lambda request: ad)

    def _sp(pm, p):
        if safe_boom:
            _boom()
        return port

    monkeypatch.setattr(wc, "safe_get_port", _sp)
    ws = ws_factory([]) if ws_factory else FakeWS([])
    monkeypatch.setattr(wc.web, "WebSocketResponse", lambda **k: ws)
    msgs = incoming_factory(ws) if incoming_factory else (list(incoming) if incoming else [])
    ws.incoming = msgs
    res = await handle_ws(_req(**(req_kw or {})))
    return ws, res, ad, cm


# ---------------- prepare phase ----------------


@pytest.mark.asyncio
async def test_missing_port_name(monkeypatch):
    ad = Adapter()
    ad.console_manager = _NoConnect()
    monkeypatch.setattr(wc, "_get_adapter", lambda request: ad)
    ws = FakeWS()
    monkeypatch.setattr(wc.web, "WebSocketResponse", lambda **k: ws)
    with pytest.raises(HTTPBadRequest):
        await handle_ws(_req(port_name=""))


@pytest.mark.asyncio
async def test_prepare_unescape_state(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch, incoming=[_close()], req_kw=dict(port_name="console&amp;bar", username="bob", meta="0")
    )
    assert res is ws
    assert cm.connect_calls[0][0].startswith("ws:")
    # html.unescape decodes HTML entities (not URL percent encoding)
    assert cm.connect_calls[0][1] == "console&bar"
    assert cm.connect_calls[0][2] == "bob"


@pytest.mark.asyncio
async def test_prepare_ip_set(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], adapter_cls=_IPAdapter)
    # the IP path was taken; the metadata was set then popped by teardown
    assert ad.ip_calls == 1
    assert ad._client_meta == {}


@pytest.mark.asyncio
async def test_prepare_ip_init(monkeypatch):
    # _client_meta is absent, so the IP try takes the init branch (adapter._client_meta = {})
    res = await _run(monkeypatch, incoming=[_close()], adapter_cls=_IPAdapterNoMeta)
    assert res[2]._client_meta == {}  # recreated empty by the init branch, then popped by teardown


@pytest.mark.asyncio
async def test_prepare_ip_none(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], adapter_cls=_IPAdapterNone)
    assert ad._client_meta == {}


@pytest.mark.asyncio
async def test_prepare_ip_no_method(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()])
    assert ad._client_meta == {}


@pytest.mark.asyncio
async def test_prepare_ip_boom(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], adapter_cls=_IPAdapterBoom)
    assert ad._client_meta == {}


# ---------------- want_meta query ----------------


@pytest.mark.asyncio
async def test_meta_true(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], req_kw=dict(meta="1"))
    assert ad.broadcasts == ["console1"]


@pytest.mark.asyncio
async def test_meta_true_yes(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], req_kw=dict(meta="YES"))
    assert ad.broadcasts == ["console1"]


@pytest.mark.asyncio
async def test_meta_false(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], req_kw=dict(meta="no"))
    assert ad.broadcasts == []


@pytest.mark.asyncio
async def test_meta_none(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], req_kw=dict(meta=None))
    assert ad.broadcasts == []


@pytest.mark.asyncio
async def test_meta_query_boom(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], req_kw=dict(rel_query=_BoomQuery()))
    assert ad.broadcasts == []


@pytest.mark.asyncio
async def test_meta_query_nonstr(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], req_kw=dict(rel_query={"meta": 1}))
    assert ad.broadcasts == []


# ---------------- connect: fallback / denied ----------------


@pytest.mark.asyncio
async def test_fallback_metaonly(monkeypatch):
    port = _PortObj(fed=True, up=False)
    ws, res, ad, cm = await _run(monkeypatch, incoming=[], port=port, result=(False, "read-only", "port_full"))
    assert res is ws
    assert not ws.closed
    assert any("meta-only" in m for m in ad.logger.has("info"))
    assert ad._clients == {}


@pytest.mark.asyncio
async def test_fallback_port_up(monkeypatch):
    port = _PortObj(fed=True, up=True)
    ws, res, ad, cm = await _run(monkeypatch, incoming=[], port=port, result=(False, "read-only", "port_full"))
    assert ws.close_code == 4003
    assert ad._clients == {}


@pytest.mark.asyncio
async def test_fallback_not_fed(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[], result=(False, "read-only", "port_full"))
    assert ws.close_code == 4003
    assert ad._clients == {}


@pytest.mark.asyncio
async def test_fallback_safe_boom(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[], safe_boom=True, result=(False, "read-only", "port_full"))
    assert ws.close_code == 4003
    assert ad._clients == {}


@pytest.mark.asyncio
async def test_fallback_isconnected_boom(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[], port=_BoomIsPort(), result=(False, "read-only", "port_full"))
    assert ws.close_code == 4003
    assert ad._clients == {}


@pytest.mark.asyncio
async def test_denied_group_acl(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[], result=(False, None, "denied_by_group_acl"))
    assert ws.close_code == 4003
    assert ad._clients == {}


@pytest.mark.asyncio
async def test_no_permissions(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[], result=(False, None, "no_permissions"))
    assert ws.close_code == 4003
    assert ad._clients == {}


@pytest.mark.asyncio
async def test_no_console_manager(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[], no_connect=True)
    assert ws.close_code == 1011
    assert ad._clients == {}


# ---------------- connect: sends / register / errors ----------------


@pytest.mark.asyncio
async def test_connect_readwrite_mode(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], result=(True, "read-write", None))
    mode = _one(ws, "client_mode")
    assert mode["mode"] == "read-write"
    assert mode["client_id"].startswith("ws:")
    assert "rw_holders" not in mode and "max_rw_users" not in mode


@pytest.mark.asyncio
async def test_connect_readonly_holders(monkeypatch):
    port = _PortObj(connected_clients=[{"mode": "read-write", "username": "h", "client_id": "hc"}], maxrw=2)
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], port=port)
    mode = _one(ws, "client_mode")
    assert mode["mode"] == "read-only"
    assert len(mode["rw_holders"]) == 1
    assert "max_rw_users" in mode
    assert any(c["type"] == "presence" for c in _ctrl(ws))


@pytest.mark.asyncio
async def test_connect_readonly_empty_holders(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()])
    mode = _one(ws, "client_mode")
    assert mode["mode"] == "read-only"
    assert "rw_holders" not in mode and "max_rw_users" not in mode


@pytest.mark.asyncio
async def test_connect_presence_viewers(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], cm_attrs=dict(viewers=[{"id": "v1"}]))
    assert _one(ws, "presence")["viewers"] == [{"id": "v1"}]


@pytest.mark.asyncio
async def test_connect_presence_boom(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], cm_attrs=dict(viewers_boom=True))
    assert _one(ws, "client_mode")
    assert "presence" not in [c["type"] for c in _ctrl(ws)]


@pytest.mark.asyncio
async def test_connect_clientmode_send_boom(monkeypatch):
    # the very first send (client_mode) raises -> the connect phase's client_mode except
    ws, res, ad, cm = await _run(
        monkeypatch, incoming=[_close()], ws_factory=lambda inc: _CondWS(inc, n=0, when="client_mode")
    )
    assert "client_mode" not in [c["type"] for c in _ctrl(ws)]
    assert any(c["type"] == "presence" for c in _ctrl(ws))


@pytest.mark.asyncio
async def test_connect_presence_send_boom(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], ws_factory=lambda inc: _CondWS(inc, n=1, when="presence"))
    assert _one(ws, "client_mode")
    assert "presence" not in [c["type"] for c in _ctrl(ws)]


@pytest.mark.asyncio
async def test_connect_register_boom(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], cm_attrs=dict(register_boom=True))
    assert any("register_client_channel failed" in m for m in ad.logger.has("error"))


@pytest.mark.asyncio
async def test_connect_mode_none(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], result=(True, None, None))
    assert _one(ws, "client_mode")["mode"] == "read-only"


@pytest.mark.asyncio
async def test_connect_phase_outer_boom(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[], cm_attrs=dict(connect_boom=True))
    assert res is ws
    assert any("Error connecting web client" in m for m in ad.logger.has("error"))
    assert ws.close_code == 1011
    assert ad._clients == {}


@pytest.mark.asyncio
async def test_connect_phase_outer_close_boom(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch, incoming=[], cm_attrs=dict(connect_boom=True), ws_factory=lambda i: _CloseBoomWS(i)
    )
    assert res is ws
    assert ad._clients == {}


# ---------------- meta subscription ----------------


@pytest.mark.asyncio
async def test_subscribe_broadcast_boom(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], ad_attrs=dict(broadcast_boom=True))
    assert ad.broadcasts == []


@pytest.mark.asyncio
async def test_subscribe_meta_subs_not_dict(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], meta_subs=_SetdefaultBoom())
    assert isinstance(ad._meta_subscribers, _SetdefaultBoom)
    assert ad.broadcasts == []


# ---------------- read loop: forwarding + errors ----------------


@pytest.mark.asyncio
async def test_write_text(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_text("hello"), _close()], req_kw=dict(meta=None))
    assert cm.pm.writes == [("console1", b"hello", cm.connect_calls[0][0])]


@pytest.mark.asyncio
async def test_write_binary(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_binary(b"\x01\x02"), _close()], req_kw=dict(meta=None))
    assert cm.pm.writes == [("console1", b"\x01\x02", cm.connect_calls[0][0])]


@pytest.mark.asyncio
async def test_write_meta_only_dropped(monkeypatch):
    port = _PortObj(fed=True, up=False)
    ws, res, ad, cm = await _run(
        monkeypatch, incoming=[_text("drop"), _close()], port=port, result=(False, "read-only", "port_full")
    )
    assert cm.pm.writes == []


@pytest.mark.asyncio
async def test_write_unknown_type_skipped(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_ping(), _close()], req_kw=dict(meta=None))
    assert cm.pm.writes == []


@pytest.mark.asyncio
async def test_write_boom_breaks(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch, incoming=[_text("x"), _close()], cm_attrs=dict(write_boom=True), req_kw=dict(meta=None)
    )
    assert cm.pm.writes == []
    assert any("Write to port error" in m for m in ad.logger.has("error"))


@pytest.mark.asyncio
async def test_loop_error_breaks(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch, incoming=[_text("x")], ws_factory=lambda i: _AnextBoomWS(i), req_kw=dict(meta=None)
    )
    assert cm.pm.writes == []
    assert any("Websocket loop error" in m for m in ad.logger.has("error"))


@pytest.mark.asyncio
async def test_cancel_closes_silently(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_CancelMsg()], req_kw=dict(meta=None))
    assert ws.close_code == 1011
    assert "Websocket loop error" not in str(ad.logger.has("error"))


@pytest.mark.asyncio
async def test_cancel_close_boom(monkeypatch):
    # CancelledError in the read loop + a close that raises -> the shutdown close except
    ws, res, ad, cm = await _run(
        monkeypatch, incoming=[_CancelMsg()], ws_factory=lambda i: _CloseBoomWS(i), req_kw=dict(meta=None)
    )
    assert "Websocket loop error" not in str(ad.logger.has("error"))
    assert any("disconnected" in m for m in ad.logger.has("info"))


@pytest.mark.asyncio
async def test_close_breaks(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], req_kw=dict(meta=None))
    assert ws.close_code is None
    assert ad._clients == {}


# ---------------- OMXCTRL: dispatch + outer ----------------


@pytest.mark.asyncio
async def test_omxctrl_outer_badjson(monkeypatch):
    # unparseable control text is NOT a matched command; only the connect frames exist
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_text("OMXCTRL {not json"), _close()], req_kw=dict(meta=None))
    assert [c["type"] for c in _ctrl(ws)] == ["client_mode", "presence"]


@pytest.mark.asyncio
async def test_omxctrl_outer_nonlist(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_text("OMXCTRL 123"), _close()], req_kw=dict(meta=None))
    assert [c["type"] for c in _ctrl(ws)] == ["client_mode", "presence"]


@pytest.mark.asyncio
async def test_omxctrl_unknown_type(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_ctrl_frame({"type": "zzz"}), _close()], req_kw=dict(meta=None))
    assert [c["type"] for c in _ctrl(ws)] == ["client_mode", "presence"]
    assert len(cm.pm.writes) == 1  # forwarded (not matched as a command)


@pytest.mark.asyncio
async def test_omxctrl_not_dict(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_text("OMXCTRL [1,2]"), _close()], req_kw=dict(meta=None))
    assert [c["type"] for c in _ctrl(ws)] == ["client_mode", "presence"]


# ---------------- handler: request_rw / promote ----------------


@pytest.mark.asyncio
async def test_request_rw_ok(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch,
        incoming=[_ctrl_frame({"type": "request_rw"}), _close()],
        cm_attrs=dict(promote=True),
        req_kw=dict(meta=None),
    )
    resp = _after_cmd(ws, "client_mode")
    assert resp["ok"] is True and resp["mode"] == "read-write"
    assert "rw_holders" not in resp and "max_rw_users" not in resp
    assert cm.taken == []


@pytest.mark.asyncio
async def test_request_rw_denied_holders(monkeypatch):
    port = _PortObj(connected_clients=[{"mode": "read-write", "username": "h", "client_id": "hc"}], maxrw=2)
    ws, res, ad, cm = await _run(
        monkeypatch,
        incoming=[_ctrl_frame({"type": "promote"}), _close()],
        port=port,
        cm_attrs=dict(promote=False),
        req_kw=dict(meta=None),
    )
    resp = _after_cmd(ws, "client_mode")
    assert resp["ok"] is False and resp["mode"] == "read-only"
    assert len(resp["rw_holders"]) == 1
    assert "max_rw_users" in resp


@pytest.mark.asyncio
async def test_request_rw_boom(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch,
        incoming=[_ctrl_frame({"type": "request_rw"}), _close()],
        cm_attrs=dict(promote_boom=True),
        req_kw=dict(meta=None),
    )
    resp = _after_cmd(ws, "client_mode")
    assert resp["ok"] is False
    assert "rw_holders" not in resp and "max_rw_users" not in resp


@pytest.mark.asyncio
async def test_request_rw_send_boom(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch,
        incoming=[_ctrl_frame({"type": "request_rw"}), _close()],
        cm_attrs=dict(promote=True),
        ws_factory=lambda i: _CondWS(i, n=2, when="client_mode"),
    )
    assert cm.pm.writes == []


# ---------------- handler: release_rw ----------------


@pytest.mark.asyncio
async def test_release_rw_ok(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_ctrl_frame({"type": "release_rw"}), _close()], req_kw=dict(meta=None))
    resp = _after_cmd(ws, "client_mode")
    assert resp["ok"] is True and resp["mode"] == "read-only"
    assert len(cm.demoted) == 1


@pytest.mark.asyncio
async def test_release_rw_demote_boom(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch,
        incoming=[_ctrl_frame({"type": "release_rw"}), _close()],
        cm_attrs=dict(demote_boom=True),
        req_kw=dict(meta=None),
    )
    resp = _after_cmd(ws, "client_mode")
    assert resp["ok"] is True


@pytest.mark.asyncio
async def test_release_rw_send_boom(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch,
        incoming=[_ctrl_frame({"type": "release_rw"}), _close()],
        ws_factory=lambda i: _CondWS(i, n=1, when="client_mode"),
        req_kw=dict(meta=None),
    )
    assert cm.pm.writes == []


# ---------------- handler: force_promote ----------------


@pytest.mark.asyncio
async def test_force_promote_takeover_ok(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch,
        incoming=[_ctrl_frame({"type": "force_promote", "client_id": "victim"}), _close()],
        cm_attrs=dict(take=(True, "takeover from VICT")),
        req_kw=dict(meta=None),
    )
    resp = _after_cmd(ws, "client_mode")
    assert resp["ok"] is True and resp["mode"] == "read-write"
    assert resp["takeover"] == "VICT"
    assert cm.taken and cm.taken[0][2] == "victim"


@pytest.mark.asyncio
async def test_force_promote_already_rw(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch,
        incoming=[_ctrl_frame({"type": "force_promote"}), _close()],
        cm_attrs=dict(take=(True, "already_rw")),
        req_kw=dict(meta=None),
    )
    resp = _after_cmd(ws, "client_mode")
    assert resp["ok"] is True
    assert "reason" not in resp
    assert cm.taken and cm.taken[0][2] is None


@pytest.mark.asyncio
async def test_force_promote_target_self(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch,
        incoming_factory=lambda ws: [_ctrl_frame({"type": "force_promote", "client_id": _cid_of(ws)}), _close()],
        cm_attrs=dict(take=(True, "already_rw")),
        req_kw=dict(meta=None),
    )
    assert cm.taken and cm.taken[0][2] is None


@pytest.mark.asyncio
async def test_force_promote_take_boom(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch,
        incoming=[_ctrl_frame({"type": "force_promote"}), _close()],
        cm_attrs=dict(take_boom=True),
        req_kw=dict(meta=None),
    )
    resp = _after_cmd(ws, "client_mode")
    assert resp["ok"] is False
    assert resp["reason"] == "error"
    assert "max_rw_users" not in resp


@pytest.mark.asyncio
async def test_force_promote_maxrw(monkeypatch):
    port = _PortObj(maxrw=2)
    ws, res, ad, cm = await _run(
        monkeypatch,
        incoming=[_ctrl_frame({"type": "force_promote"}), _close()],
        port=port,
        cm_attrs=dict(take=(False, "full")),
        req_kw=dict(meta=None),
    )
    assert "max_rw_users" in _after_cmd(ws, "client_mode")


@pytest.mark.asyncio
async def test_force_promote_maxrw_none(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch,
        incoming=[_ctrl_frame({"type": "force_promote"}), _close()],
        cm_attrs=dict(take=(False, "full")),
        req_kw=dict(meta=None),
    )
    assert "max_rw_users" not in _after_cmd(ws, "client_mode")


@pytest.mark.asyncio
async def test_force_promote_send_boom(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch,
        incoming=[_ctrl_frame({"type": "force_promote"}), _close()],
        ws_factory=lambda i: _CondWS(i, n=2, when="client_mode"),
        req_kw=dict(meta=None),
    )
    assert cm.pm.writes == []


# ---------------- handler: query_rw_holders ----------------


@pytest.mark.asyncio
async def test_query_holders_ok(monkeypatch):
    port = _PortObj(connected_clients=[{"mode": "read-write", "username": "h", "client_id": "hc"}], maxrw=2)
    ws, res, ad, cm = await _run(
        monkeypatch, incoming=[_ctrl_frame({"type": "query_rw_holders"}), _close()], port=port, req_kw=dict(meta=None)
    )
    resp = _one(ws, "rw_holders")
    assert len(resp["holders"]) == 1
    assert "max_rw_users" in resp


@pytest.mark.asyncio
async def test_query_holders_send_boom(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch,
        incoming=[_ctrl_frame({"type": "query_rw_holders"}), _close()],
        ws_factory=lambda i: _CondWS(i, n=1, when="rw_holders"),
        req_kw=dict(meta=None),
    )
    assert cm.pm.writes == []


# ---------------- handler: power_query / power_switch ----------------


@pytest.mark.asyncio
async def test_power_query_unconfigured(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch, incoming=[_ctrl_frame({"type": "power_query"}), _close()], req_kw=dict(meta=None)
    )
    resp = _one(ws, "power_feeds")
    assert resp["ok"] is False
    assert resp["error"] == "power management is not configured"
    assert resp["feeds"] == []


@pytest.mark.asyncio
async def test_power_switch_unconfigured(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch, incoming=[_ctrl_frame({"type": "power_switch", "ref": "r"}), _close()], req_kw=dict(meta=None)
    )
    resp = _one(ws, "power_switch")
    assert resp["ok"] is False
    assert resp["error"] == "power management is not configured"


@pytest.mark.asyncio
async def test_power_pdu_ok(monkeypatch):
    frame = {"type": "power_feeds", "ok": True, "feeds": [{"ref": "r"}], "feeds_total": 1}
    pdu = Pdu(frame)
    ws, res, ad, cm = await _run(
        monkeypatch, incoming=[_ctrl_frame({"type": "power_query"}), _close()], ad_attrs=dict(pdu=pdu), req_kw=dict(meta=None)
    )
    assert _one(ws, "power_feeds")["feeds"] == [{"ref": "r"}]
    assert pdu.calls[0][0] == "console1"


@pytest.mark.asyncio
async def test_power_pdu_none_default(monkeypatch):
    pdu = Pdu(None)
    ws, res, ad, cm = await _run(
        monkeypatch, incoming=[_ctrl_frame({"type": "power_query"}), _close()], ad_attrs=dict(pdu=pdu), req_kw=dict(meta=None)
    )
    resp = _one(ws, "power_switch")
    assert resp["ok"] is False
    assert resp["error"] == "power management is not configured"


@pytest.mark.asyncio
async def test_power_pdu_boom(monkeypatch):
    pdu = Pdu(None, boom=True)
    ws, res, ad, cm = await _run(
        monkeypatch,
        incoming=[_ctrl_frame({"type": "power_switch", "ref": "r"}), _close()],
        ad_attrs=dict(pdu=pdu),
        req_kw=dict(meta=None),
    )
    # handle_power_frame raised -> the power handler's outer except -> forwarded
    assert [c["type"] for c in _ctrl(ws)] == ["client_mode", "presence"]
    assert len(cm.pm.writes) == 1


@pytest.mark.asyncio
async def test_power_find_boom(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch, incoming=[_ctrl_frame({"type": "power_query"}), _close()], req_kw=dict(meta=None)
    )
    monkeypatch.setattr(ad, "_find_power_adapter", _boom)
    ws2 = FakeWS([_ctrl_frame({"type": "power_query"})])
    monkeypatch.setattr(wc.web, "WebSocketResponse", lambda **k: ws2)
    await handle_ws(_req(meta=None))
    assert "power_feeds" not in [c["type"] for c in _ctrl(ws2)]


@pytest.mark.asyncio
async def test_power_send_boom(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch,
        incoming=[_ctrl_frame({"type": "power_query"}), _close()],
        ws_factory=lambda i: _CondWS(i, n=1, when="power_feeds"),
        req_kw=dict(meta=None),
    )
    assert cm.pm.writes == []


# ---------------- handler: request_scrollback ----------------


@pytest.mark.asyncio
async def test_scrollback_with_bytes(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch,
        incoming=[_ctrl_frame({"type": "request_scrollback"}), _close()],
        cm_attrs=dict(scrollback=b"old"),
        req_kw=dict(meta=None),
    )
    assert b"old" in _byte_payloads(ws)
    assert _one(ws, "scrollback_done")["bytes"] == len(b"old")


@pytest.mark.asyncio
async def test_scrollback_empty(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch,
        incoming=[_ctrl_frame({"type": "request_scrollback"}), _close()],
        cm_attrs=dict(scrollback=b""),
        req_kw=dict(meta=None),
    )
    assert _one(ws, "scrollback_done")["bytes"] == 0
    assert _byte_payloads(ws) == []


@pytest.mark.asyncio
async def test_scrollback_boom(monkeypatch):
    ws, res, ad, cm = await _run(
        monkeypatch,
        incoming=[_ctrl_frame({"type": "request_scrollback"}), _close()],
        cm_attrs=dict(pm_boom=True),
        req_kw=dict(meta=None),
    )
    assert any("scrollback send error" in m for m in ad.logger.has("debug"))
    assert cm.pm.writes == []


# ---------------- teardown (finally) ----------------


@pytest.mark.asyncio
async def test_teardown_subscription_get_boom(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], meta_subs=_GetBoom())
    assert ad._clients == {}


@pytest.mark.asyncio
async def test_teardown_subscription_nonset(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], meta_subs={"console1": "notASET"})
    assert ad._clients == {}


@pytest.mark.asyncio
async def test_teardown_meta_pop_boom(monkeypatch):
    pbd = _PopBoomDict()
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], client_meta=pbd)
    assert any("disconnected" in m for m in ad.logger.has("info"))


@pytest.mark.asyncio
async def test_teardown_disconnect_boom(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], cm_attrs=dict(disconnect_boom=True))
    assert any("teardown error" in m for m in ad.logger.has("warning"))


@pytest.mark.asyncio
async def test_teardown_unregister_boom(monkeypatch):
    ws, res, ad, cm = await _run(monkeypatch, incoming=[_close()], cm_attrs=dict(unregister_boom=True))
    assert cm.unregistered == []
    assert len(cm.connected) == 1
