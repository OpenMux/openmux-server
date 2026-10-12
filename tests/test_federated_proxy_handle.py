"""Behavior + coverage tests for federated_proxy._handle_proxy.

_handle_proxy was previously at 0% line coverage. These tests pin the outcome
of every control-flow branch: auth/permission guards, method + allowlist +
console-redirect checks, SSO header construction (HMAC / Ed25519 / none), the
upstream candidate loop, HTML rewrite, streaming, byte caps, and the
all-candidates-fail fallback. ClientSession is monkeypatched so no real socket
is opened.
"""

import base64
import hashlib
import hmac
import json

import pytest
from aiohttp import web

from openmux.server.web_plugins import ADAPTER_APP_KEY, federated_proxy
from openmux.server.web_plugins.federated_proxy import _handle_proxy


# ---------------------------------------------------------------------------- fakes
class FakeApp:
    """Minimal request.app: returns the configured adapter for ADAPTER_APP_KEY.

    Uses a custom .get (not a real dict) so the ADAPTER_APP_KEY object does not
    need to be hashable.
    """

    def __init__(self, adapter=None):
        self._adapter = adapter

    def get(self, key, default=None):
        return self._adapter if key == ADAPTER_APP_KEY else default


class FakeRelUrl:
    query_string = ""


class FakeRequest:
    def __init__(self, app, username="alice", match_info=None, method="GET", headers=None, query_string=b""):
        self.app = app
        self._username = username
        self.match_info = match_info if match_info is not None else {"node": "node1", "tail": "status"}
        self.method = method
        self.headers = headers if headers is not None else {}
        self.query_string = query_string
        self.rel_url = FakeRelUrl()

    def get(self, key):
        return self._username if key == "username" else None


class FakeAdapter:
    """Configurable adapter. Attributes set in **kw are used; absent ones fall
    back to the function's getattr defaults. Muxcon is returned by
    _find_muxcon_adapter when _muxcon is set."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        if "_muxcon" in kw:
            self._muxcon = kw.pop("_muxcon", None)

    def _find_muxcon_adapter(self):
        return getattr(self, "_muxcon", None)


class BasePathHelper:
    """Factory for an adapter._effective_base_path that returns a value or raises."""

    def __init__(self, value=None, raise_=False):
        self.value = value
        self.raise_ = raise_

    def __call__(self, request):
        if self.raise_:
            raise RuntimeError("no base path")
        return self.value


class FakeWriter:
    def __init__(self, peername=("192.0.2.10", 8443)):
        self._peer = peername

    def get_extra_info(self, name):
        return self._peer if name == "peername" else None


class FakePriv:
    def sign(self, payload):
        return b"fake-bytes-signature"


class FakeMuxcon:
    def __init__(self, server_id="node1", peername=("192.0.2.10", 8443), priv=None, kid=None):
        self.connections = {"c1": {"server_id": server_id, "writer": FakeWriter(peername)}}
        if priv is not None:
            self._auth_priv = priv
        if kid is not None:
            self._auth_key_id = kid


class UpstreamContent:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def iter_chunked(self, _n):
        for c in self._chunks:
            yield c


class FakeUpstream:
    def __init__(self, status=200, headers=None, chunks=None):
        self.status = status
        self.headers = dict(headers or {})
        self._chunks = list(chunks or [])

    @property
    def content(self):
        return UpstreamContent(self._chunks)


class UpstreamCtx:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        if isinstance(self._value, Exception):
            raise self._value
        return self._value

    async def __aexit__(self, *a):
        return False


class FakeClientSession:
    """Monkeypatched stand-in for aiohttp.ClientSession.

    ``results`` is a list consumed one item per candidate (first candidate =
    first item). Each item is a FakeUpstream (success) or an Exception (the
    request for that candidate fails).
    """

    timeout_arg = None
    results = []
    calls = []

    def __init__(self, timeout=None):
        FakeClientSession.timeout_arg = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, headers=None, ssl=None):
        FakeClientSession.calls.append({"url": url, "headers": headers, "ssl": ssl})
        if FakeClientSession.results:
            return UpstreamCtx(FakeClientSession.results.pop(0))
        return UpstreamCtx(RuntimeError("no more results"))


class FakeStreamResponse(web.StreamResponse):
    """StreamResponse whose I/O calls are no-ops so no real request is needed."""

    def __init__(self, status=200, headers=None):
        super().__init__(status=status, headers=headers)
        self._prepared = False
        self.written = b""
        self.eof = False

    async def prepare(self, request):
        self._prepared = True

    async def write(self, data):
        self.written += data

    async def write_eof(self):
        self.eof = True


def _mk(adapter, **req_kw):
    return FakeRequest(FakeApp(adapter), **req_kw)


def _adm_adapter(**kw):
    """An adapter that passes the admin guard (get_user_permissions -> 'admin'),
    no muxcon/sso/base-path set unless passed."""

    class _AuthManager:
        def get_user_permissions(self, username):
            return "admin"

    kw.setdefault("auth_manager", _AuthManager())
    return FakeAdapter(**kw)


@pytest.fixture
def monkey_sess(monkeypatch):
    monkeypatch.setattr(federated_proxy, "ClientSession", FakeClientSession)
    FakeClientSession.results = []
    FakeClientSession.calls = []
    FakeClientSession.timeout_arg = None
    return monkeypatch


# ---------------------------------------------------------------------------- guards
@pytest.mark.asyncio
async def test_no_adapter_500(monkey_sess):
    req = FakeRequest(FakeApp(None), username="alice")
    with pytest.raises(web.HTTPInternalServerError) as ei:
        await _handle_proxy(req)
    assert "Adapter not available" in ei.value.text


@pytest.mark.asyncio
async def test_no_username_401(monkey_sess):
    req = _mk(_adm_adapter(), username=None)
    resp = await _handle_proxy(req)
    assert resp.status == 401
    assert "WWW-Authenticate" in resp.headers


@pytest.mark.asyncio
async def test_perm_not_admin_403(monkey_sess):
    class _AuthManager:
        def get_user_permissions(self, username):
            return "none"

    req = _mk(FakeAdapter(auth_manager=_AuthManager()))
    with pytest.raises(web.HTTPForbidden):
        await _handle_proxy(req)


@pytest.mark.asyncio
async def test_no_auth_manager_403(monkey_sess):
    req = _mk(FakeAdapter())  # no auth_manager -> perms None -> forbidden
    with pytest.raises(web.HTTPForbidden):
        await _handle_proxy(req)


@pytest.mark.asyncio
async def test_auth_exception_403(monkey_sess):
    class _AuthManager:
        def get_user_permissions(self, username):
            raise RuntimeError("perm boom")

    req = _mk(FakeAdapter(auth_manager=_AuthManager()))
    with pytest.raises(web.HTTPForbidden):
        await _handle_proxy(req)


@pytest.mark.asyncio
async def test_http_exception_passthrough(monkey_sess):
    class _AuthManager:
        def get_user_permissions(self, username):
            raise web.HTTPConflict(text="nope")

    req = _mk(FakeAdapter(auth_manager=_AuthManager()))
    with pytest.raises(web.HTTPConflict):
        await _handle_proxy(req)


@pytest.mark.asyncio
async def test_not_get_405(monkey_sess):
    req = _mk(_adm_adapter(), method="POST")
    with pytest.raises(web.HTTPMethodNotAllowed):
        await _handle_proxy(req)


@pytest.mark.asyncio
async def test_forbidden_tail(monkey_sess):
    req = _mk(_adm_adapter(), match_info={"node": "node1", "tail": "api/secret"})
    with pytest.raises(web.HTTPForbidden) as ei:
        await _handle_proxy(req)
    assert "Path not allowed" in ei.value.text


@pytest.mark.asyncio
async def test_console_redirect(monkey_sess):
    req = _mk(_adm_adapter(), match_info={"node": "node1", "tail": "console"})
    with pytest.raises(web.HTTPFound) as ei:
        await _handle_proxy(req)
    assert ei.value.headers["Location"] == "/console?server=node1"


@pytest.mark.asyncio
async def test_console_redirect_with_base_path(monkey_sess):
    adapter = _adm_adapter(_effective_base_path=BasePathHelper(value="/base/"))
    req = _mk(adapter, match_info={"node": "n9", "tail": "console"})
    with pytest.raises(web.HTTPFound) as ei:
        await _handle_proxy(req)
    assert ei.value.headers["Location"] == "/base/console?server=n9"


@pytest.mark.asyncio
async def test_base_path_falls_back_to_header(monkey_sess):
    adapter = _adm_adapter(_effective_base_path=BasePathHelper(raise_=True))
    req = _mk(
        adapter,
        match_info={"node": "node1", "tail": "console"},
        headers={"X-Forwarded-Prefix": "/xpf"},
    )
    with pytest.raises(web.HTTPFound) as ei:
        await _handle_proxy(req)
    assert ei.value.headers["Location"] == "/xpf/console?server=node1"


@pytest.mark.asyncio
async def test_missing_node_400(monkey_sess):
    req = _mk(_adm_adapter(), match_info={})  # no node, no tail
    with pytest.raises(web.HTTPBadRequest) as ei:
        await _handle_proxy(req)
    assert "Missing node" in ei.value.text


@pytest.mark.asyncio
async def test_no_upstream_502(monkey_sess, monkeypatch):
    adapter = _adm_adapter(_muxcon=FakeMuxcon(server_id="node1", kid="k-only", priv=None))
    monkeypatch.setattr(federated_proxy, "_resolve_upstream", lambda *a, **k: None)
    req = _mk(adapter, match_info={"node": "node1", "tail": "status"})
    with pytest.raises(web.HTTPBadGateway) as ei:
        await _handle_proxy(req)
    assert "No upstream mapping for node" in ei.value.text


# ---------------------------------------------------------------------------- html / redirect rewrites
@pytest.mark.asyncio
async def test_html_response_rewritten(monkey_sess):
    adapter = _adm_adapter(_muxcon=FakeMuxcon(server_id="node1"))
    body = b'<html><body><a href="/login">x</a><img src="/static/a.png"/></body></html>'
    up = FakeUpstream(status=200, headers={"Content-Type": "text/html"}, chunks=[body])
    FakeClientSession.results = [up]
    req = _mk(adapter, match_info={"node": "node1", "tail": "status"})
    resp = await _handle_proxy(req)
    assert resp.status == 200
    assert isinstance(resp, web.Response)
    out = resp.body.decode("utf-8")
    assert 'href="/proxy/node1/login"' in out
    assert 'src="/proxy/node1/static/a.png"' in out
    assert "proxy-remote-banner" in out
    assert resp.headers["Content-Length"] == str(len(resp.body))
    assert FakeClientSession.calls[0]["headers"]["X-Forwarded-Prefix"] == "/proxy/node1"


@pytest.mark.asyncio
async def test_redirect_headers_rewritten(monkey_sess):
    adapter = _adm_adapter(_muxcon=FakeMuxcon(server_id="node1"))
    up = FakeUpstream(
        status=302,
        headers={"Content-Type": "text/html", "Location": "/old", "Set-Cookie": "sid=abc; Path=/"},
        chunks=[b""],
    )
    FakeClientSession.results = [up]
    req = _mk(adapter, match_info={"node": "node1", "tail": "status"})
    resp = await _handle_proxy(req)
    assert resp.status == 302
    assert resp.headers["Location"] == "/proxy/node1/old"
    assert "Path=/proxy/node1/" in resp.headers["Set-Cookie"]


# ---------------------------------------------------------------------------- stream
@pytest.mark.asyncio
async def test_stream_response(monkey_sess, monkeypatch):
    monkeypatch.setattr(federated_proxy.web, "StreamResponse", FakeStreamResponse)
    adapter = _adm_adapter(_muxcon=FakeMuxcon(server_id="node1"))
    up = FakeUpstream(status=200, headers={"Content-Type": "application/json"}, chunks=[b'{"a":1}', b'{"b":2}'])
    FakeClientSession.results = [up]
    req = _mk(adapter, match_info={"node": "node1", "tail": "status"})
    resp = await _handle_proxy(req)
    assert isinstance(resp, FakeStreamResponse)
    assert resp._prepared is True
    assert resp.eof is True
    assert resp.written == b'{"a":1}{"b":2}'


# ---------------------------------------------------------------------------- SSO header
def _sso_header_value(req):
    return FakeClientSession.calls[0]["headers"].get("X-OMX-SSO")


@pytest.mark.asyncio
async def test_sso_hmac_secret(monkey_sess):
    adapter = _adm_adapter(_muxcon=FakeMuxcon(server_id="node1"), sso_secret="shhh")
    FakeClientSession.results = [FakeUpstream(200, {"Content-Type": "text/html"}, [b"ok"])]
    req = _mk(adapter, match_info={"node": "node1", "tail": "status"}, username="alice")
    await _handle_proxy(req)
    val = _sso_header_value(req)
    parts = val.split(";")
    assert parts[0] == "v1"
    # recover payload and verify the claims
    pad = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(pad))
    assert claims["user"] == "alice"
    assert claims["perm"] == "admin"
    assert claims["node"] == "node1"
    assert claims["exp"] == claims["iat"] + 60
    # verify the HMAC over the base64 payload
    expect = hmac.new(b"shhh", parts[1].encode("utf-8"), hashlib.sha256).hexdigest()
    assert parts[2] == expect


@pytest.mark.asyncio
async def test_sso_trust_header_alias(monkey_sess):
    adapter = _adm_adapter(
        _muxcon=FakeMuxcon(server_id="node1"),
        sso_secret="shhh",
        sso_trust_header="X-Custom-SSO",
    )
    FakeClientSession.results = [FakeUpstream(200, {"Content-Type": "text/html"}, [b"ok"])]
    req = _mk(adapter, match_info={"node": "node1", "tail": "status"})
    await _handle_proxy(req)
    hdrs = FakeClientSession.calls[0]["headers"]
    assert hdrs["X-OMX-SSO"] == hdrs["X-Custom-SSO"]
    assert hdrs["X-OMX-SSO"].startswith("v1;")


@pytest.mark.asyncio
async def test_sso_ed25519(monkey_sess):
    adapter = _adm_adapter(_muxcon=FakeMuxcon(server_id="node1", priv=FakePriv(), kid="kid-7"))
    FakeClientSession.results = [FakeUpstream(200, {"Content-Type": "text/html"}, [b"ok"])]
    req = _mk(adapter, match_info={"node": "node1", "tail": "status"})
    await _handle_proxy(req)
    val = _sso_header_value(req)
    assert val.startswith("v1e;kid-7;")
    assert val.count(";") == 3


@pytest.mark.asyncio
async def test_sso_none(monkey_sess):
    adapter = _adm_adapter(_muxcon=FakeMuxcon(server_id="node1"))  # no sso_secret, no key
    FakeClientSession.results = [FakeUpstream(200, {"Content-Type": "text/html"}, [b"ok"])]
    req = _mk(adapter, match_info={"node": "node1", "tail": "status"})
    await _handle_proxy(req)
    assert "X-OMX-SSO" not in FakeClientSession.calls[0]["headers"]


# ---------------------------------------------------------------------------- caps + all-fail
BIG = b"x" * 1_200_000  # > 1 MiB minimum cap


@pytest.mark.asyncio
async def test_html_cap_exceeded(monkey_sess):
    adapter = _adm_adapter(_muxcon=FakeMuxcon(server_id="node1"), proxy_max_bytes=1)
    # every candidate returns an oversized html body -> per-candidate 413 (caught)
    FakeClientSession.results = [FakeUpstream(200, {"Content-Type": "text/html"}, [BIG]) for _ in range(4)]
    req = _mk(adapter, match_info={"node": "node1", "tail": "status"})
    with pytest.raises(web.HTTPBadGateway):
        await _handle_proxy(req)


@pytest.mark.asyncio
async def test_stream_cap_exceeded(monkey_sess, monkeypatch):
    monkeypatch.setattr(federated_proxy.web, "StreamResponse", FakeStreamResponse)
    adapter = _adm_adapter(_muxcon=FakeMuxcon(server_id="node1"), proxy_max_bytes=1)
    FakeClientSession.results = [FakeUpstream(200, {"Content-Type": "application/json"}, [BIG]) for _ in range(4)]
    req = _mk(adapter, match_info={"node": "node1", "tail": "status"})
    with pytest.raises(web.HTTPBadGateway):
        await _handle_proxy(req)


@pytest.mark.asyncio
async def test_all_candidates_fail(monkey_sess):
    adapter = _adm_adapter(_muxcon=FakeMuxcon(server_id="node1"))
    FakeClientSession.results = [RuntimeError("conn refused")] * 4
    req = _mk(adapter, match_info={"node": "node1", "tail": "status"})
    with pytest.raises(web.HTTPBadGateway) as ei:
        await _handle_proxy(req)
    assert "Upstream connect failed" in ei.value.text
    assert "conn refused" in ei.value.text


@pytest.mark.asyncio
async def test_sso_sign_exception_unsigned(monkey_sess, monkeypatch):
    class BrokenPriv:
        def sign(self, payload):
            raise RuntimeError("sign boom")

    adapter = _adm_adapter(_muxcon=FakeMuxcon(server_id="node1", priv=BrokenPriv(), kid="kid-x"))
    FakeClientSession.results = [FakeUpstream(200, {"Content-Type": "text/html"}, [b"ok"])]
    req = _mk(adapter, match_info={"node": "node1", "tail": "status"})
    await _handle_proxy(req)
    # sign() raised -> unsigned; request must still proceed
    assert "X-OMX-SSO" not in FakeClientSession.calls[0]["headers"]


@pytest.mark.asyncio
async def test_https_candidate_with_explicit_port(monkey_sess, monkeypatch):
    adapter = _adm_adapter(_muxcon=FakeMuxcon(server_id="node1"))
    FakeClientSession.results = [RuntimeError("x"), FakeUpstream(200, {"Content-Type": "text/html"}, [b"ok"])]
    monkeypatch.setattr(federated_proxy, "_resolve_upstream", lambda *a, **k: "https://192.0.2.9:9443/base/status")
    req = _mk(adapter, match_info={"node": "node1", "tail": "status"})
    await _handle_proxy(req)
    # explicit-port candidate first, then the standard 8443 candidate
    assert "https://192.0.2.9:9443" in FakeClientSession.calls[0]["url"]
    assert "https://192.0.2.9:8443" in FakeClientSession.calls[1]["url"]


@pytest.mark.asyncio
async def test_http_url_candidates(monkey_sess, monkeypatch):
    adapter = _adm_adapter(_muxcon=FakeMuxcon(server_id="node1"))
    FakeClientSession.results = [
        RuntimeError("first down"),
        # hop-by-hop + Content-Length headers must be stripped from the response
        FakeUpstream(200, {"Content-Type": "text/html", "Connection": "keep-alive", "Content-Length": "5"}, [b"ok"]),
    ]
    monkeypatch.setattr(federated_proxy, "_resolve_upstream", lambda *a, **k: "http://192.0.2.9:80/ok?x=1")
    req = _mk(adapter, match_info={"node": "node1", "tail": "status"})
    await _handle_proxy(req)
    urls = [c["url"] for c in FakeClientSession.calls]
    assert urls[0] == "http://192.0.2.9:80/ok?x=1"
    assert urls[1] == "http://192.0.2.9:8081/ok?x=1"
    # http candidates never use ssl=False
    assert all(c["ssl"] is None for c in FakeClientSession.calls)


class _MidStreamFailure(Exception):
    """Raised from an async-generator body to simulate a mid-stream failure.

    Must be defined at module scope: generator bodies resolve globals by name
    when resumed, so a test-local class is not visible to them.
    """


@pytest.mark.asyncio
async def test_candidate_midstream_error_continue(monkey_sess, monkeypatch):
    monkeypatch.setattr(federated_proxy.web, "StreamResponse", FakeStreamResponse)

    class _Content:
        async def iter_chunked(self, _n):
            yield b"part1"  # first chunk written, then the stream fails
            raise _MidStreamFailure("boom")

    class _Up:
        status = 200
        headers = {"Content-Type": "application/json"}
        content = _Content()

    adapter = _adm_adapter(_muxcon=FakeMuxcon(server_id="node1"))
    FakeClientSession.results = [_Up()] * 4
    req = _mk(adapter, match_info={"node": "node1", "tail": "status"})
    with pytest.raises(web.HTTPBadGateway) as ei:
        await _handle_proxy(req)
    assert "Upstream connect failed" in ei.value.text
    assert "boom" in ei.value.text


@pytest.mark.asyncio
async def test_candidate_fail_then_succeed(monkey_sess):
    adapter = _adm_adapter(_muxcon=FakeMuxcon(server_id="node1"))
    # first candidate fails, second succeeds
    FakeClientSession.results = [
        RuntimeError("first down"),
        FakeUpstream(200, {"Content-Type": "text/html"}, [b"<body>hi</body>"]),
    ]
    req = _mk(adapter, match_info={"node": "node1", "tail": "status"})
    resp = await _handle_proxy(req)
    assert resp.status == 200
    assert len(FakeClientSession.calls) == 2
