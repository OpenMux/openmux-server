"""Coverage for web_console.auth_middleware — 28-bracket section 2.

Covers every previously-uncovered branch:
- adapter not initialized (500)
- _effective_base_path raising (base_path falls back to "")
- _pref inner except (via a _BadStr whose __add__ raises)
- no session cookie (sid falsy → return False)
- session expired, del succeeds and del raises
- valid session, _get_client_ip present returning a value or raising
- valid session, no _get_client_ip (hasattr False → ip None)
- request.cookies.get raising (outer except in _attach_session_user)
- public path early return (handler called directly)
- SSO trust header, _verify_sso_header raising
- Basic Auth, auth_manager.authenticate raising (except path)
- Basic Auth, wrong credentials (401)
- valid session cookie fallback path (step 2 → handler)

Coverage-first: all tests must pass on the pre-refactor code before any
refactoring is applied.
"""

import base64 as _b64
import time
import types
from unittest.mock import MagicMock

import aiohttp.web as web
import pytest

import openmux.server.web_console as wc
from openmux.server.web_plugins import ADAPTER_APP_KEY

_COOKIE = "omxsid"


class _BadStr:
    """Non-str whose __add__ always raises — forces _pref's inner except,
    which is unreachable with real strings."""

    def __add__(self, other):
        raise TypeError("__add__ disabled")

    __radd__ = __add__


class _RaiseGetCookies:
    """Cookie store whose .get() always raises — forces _attach_session_user
    to enter its outer except block."""

    def get(self, key, default=None):
        raise RuntimeError("cookies unavailable")


class _RaiseDelDict(dict):
    """dict whose __delitem__ raises — forces the expired-session
    del-exception path in _attach_session_user."""

    def __delitem__(self, key):
        raise KeyError(key)


class _RefusingAuth:
    """AuthManager whose authenticate() returns False — covers the
    'failed Basic Auth' 401 path."""

    def authenticate(self, username, password):
        return False


class _RaisingAuth:
    """AuthManager whose authenticate() raises — covers the Basic Auth
    except / log-and-fall-through path."""

    def authenticate(self, username, password):
        raise RuntimeError("authenticate failed")


def _adapter(**kw):
    """Build a SimpleNamespace that matches WebConsoleAdapter's interface
    as used by auth_middleware.

    SimpleNamespace (not MagicMock) is used so that hasattr(adapter,
    '_get_client_ip') correctly returns False when the attribute is unset.
    """
    a = types.SimpleNamespace(
        name="wc",
        _session_cookie_name=_COOKIE,
        realm="openmux",
        session_ttl_seconds=300.0,
        _sessions={},
        sso_trust_header="X-OMX-SSO",
        auth_manager=None,
        logger=MagicMock(),
    )
    a._effective_base_path = lambda req: ""
    a._verify_sso_header = lambda value: {}
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def _req(path="/", cookies=None, headers=None, adapter=None):
    r = MagicMock()
    r.path = path
    r.cookies = cookies if cookies is not None else {}
    r.headers = headers if headers is not None else {}
    r.app = {ADAPTER_APP_KEY: adapter} if adapter is not None else {}
    r.rel_url = MagicMock()
    return r


async def _handler_ok(request):
    return web.Response(status=200, text="ok")


def _valid_session(age=10.0, username="alice"):
    """Return a session dict that is not expired relative to the TTL."""
    return {"last_seen": time.time() - age, "username": username}


# ── adapter initialization ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_not_initialized():
    # 351: request.app has no ADAPTER_APP_KEY → 500
    req = _req(path="/", adapter=None)
    res = await wc.auth_middleware(req, _handler_ok)
    assert res.status == 500


# ── base_path / _pref ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_base_path_function_raises():
    # 358-359: adapter._effective_base_path raises → base_path = ""
    # The valid session cookie then drives the request to the handler.
    def _boom(req):
        raise RuntimeError("effective base path failed")

    adapter = _adapter(_effective_base_path=_boom)
    adapter._sessions = {"sess1": _valid_session()}
    req = _req(path="/private", cookies={_COOKIE: "sess1"}, adapter=adapter)
    res = await wc.auth_middleware(req, _handler_ok)
    # base_path raised → fallback to "" → public path checks fail → no SSO →
    # no Basic Auth → session cookie authenticates → handler → 200
    assert res.status == 200


@pytest.mark.asyncio
async def test_pref_inner_except():
    # 366-367: _pref's inner try/except — base_path is a _BadStr whose
    # __add__ raises, so each _pref(p) call enters the except and returns p.
    # The non-matching path ensures the check chain evaluates _pref before
    # falling through to the redirect.
    adapter = _adapter(_effective_base_path=lambda req: _BadStr())
    adapter._sessions = {}
    req = _req(path="/non-public-path", adapter=adapter)
    with pytest.raises(web.HTTPFound):
        await wc.auth_middleware(req, _handler_ok)


# ── _attach_session_user: no cookie ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_session_cookie_non_public_path():
    # 375-377: request.cookies is empty → sid is None → return False
    # Falls through to the 401/redirect exit.
    adapter = _adapter()
    req = _req(path="/private", adapter=adapter)  # no cookies, no headers
    with pytest.raises(web.HTTPFound):
        await wc.auth_middleware(req, _handler_ok)


@pytest.mark.asyncio
async def test_unknown_session_sid_non_public_path():
    # 376-377: cookie has a sid that is not in adapter._sessions →
    # sess is None → return False → falls through to redirect exit.
    adapter = _adapter(_sessions={})  # empty session store
    req = _req(path="/private", cookies={_COOKIE: "ghost"}, adapter=adapter)
    with pytest.raises(web.HTTPFound):
        await wc.auth_middleware(req, _handler_ok)


# ── _attach_session_user: expired session ────────────────────────────────────


@pytest.mark.asyncio
async def test_expired_session_del_succeeds():
    # 378-383, 386: valid cookie, last_seen too old, del succeeds
    old = time.time() - 600  # 2 min ago; session_ttl_seconds = 300
    adapter = _adapter(_sessions={"sess1": {"last_seen": old, "username": "alice"}})
    req = _req(path="/private", cookies={_COOKIE: "sess1"}, adapter=adapter)
    with pytest.raises(web.HTTPFound):
        await wc.auth_middleware(req, _handler_ok)
    # Session was deleted from the adapter's session store
    assert "sess1" not in adapter._sessions


@pytest.mark.asyncio
async def test_expired_session_del_raises():
    # 381-385: expired session, del raises → caught (pass) → return False
    old = time.time() - 600
    adapter = _adapter(_sessions=_RaiseDelDict({"sess1": {"last_seen": old, "username": "alice"}}))
    req = _req(path="/private", cookies={_COOKIE: "sess1"}, adapter=adapter)
    with pytest.raises(web.HTTPFound):
        await wc.auth_middleware(req, _handler_ok)


# ── _attach_session_user: valid session ──────────────────────────────────────


@pytest.mark.asyncio
async def test_valid_session_with_client_ip():
    # 387-392, 396-397: valid session, _get_client_ip present, ip recorded
    def _get_ip(req):
        return "10.0.0.1"

    sess = _valid_session()
    adapter = _adapter(_sessions={"sess1": sess}, _get_client_ip=_get_ip)
    req = _req(path="/private", cookies={_COOKIE: "sess1"}, adapter=adapter)
    res = await wc.auth_middleware(req, _handler_ok)
    assert res.status == 200
    assert adapter._sessions["sess1"]["ip"] == "10.0.0.1"


@pytest.mark.asyncio
async def test_valid_session_no_client_ip_fn():
    # 387, 390 (hasattr False → ip None branch), 396-397
    # adapter does NOT have _get_client_ip
    adapter = _adapter(_sessions={"sess1": _valid_session()})
    req = _req(path="/private", cookies={_COOKIE: "sess1"}, adapter=adapter)
    res = await wc.auth_middleware(req, _handler_ok)
    assert res.status == 200
    assert "ip" not in adapter._sessions["sess1"]


@pytest.mark.asyncio
async def test_get_client_ip_raises():
    # 393-395: _get_client_ip raises → caught → session proceeds → 396-397
    def _get_ip(req):
        raise RuntimeError("ip lookup failed")

    adapter = _adapter(_sessions={"sess1": _valid_session()}, _get_client_ip=_get_ip)
    req = _req(path="/private", cookies={_COOKIE: "sess1"}, adapter=adapter)
    res = await wc.auth_middleware(req, _handler_ok)
    assert res.status == 200
    assert "ip" not in adapter._sessions["sess1"]


@pytest.mark.asyncio
async def test_cookies_get_raises_outer_except():
    # 398-399: request.cookies.get() raises → outer except → return False
    adapter = _adapter(_sessions={"sess1": _valid_session()})
    req = _req(path="/private", cookies=_RaiseGetCookies(), adapter=adapter)
    with pytest.raises(web.HTTPFound):
        await wc.auth_middleware(req, _handler_ok)


# ── public path ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_public_path_early_return():
    # 420-421: path = "/healthz" (public), no session →
    # _attach_session_user() called (no cookie → return False),
    # handler called directly
    adapter = _adapter()
    req = _req(path="/healthz", adapter=adapter)
    res = await wc.auth_middleware(req, _handler_ok)
    assert res.status == 200


@pytest.mark.asyncio
async def test_proxy_path_semi_public():
    # 419-421: path = "/proxy" (semi-public) → _attach_session_user() called
    # (no cookie → False), handler called directly (login enforcement is left
    # to the proxy handler itself).
    adapter = _adapter()
    req = _req(path="/proxy", adapter=adapter)
    res = await wc.auth_middleware(req, _handler_ok)
    assert res.status == 200


# ── SSO ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sso_verify_raises():
    # 438-440: SSO trust header present, _verify_sso_header raises →
    # authenticated_via_sso stays False → no Basic Auth → no session →
    # redirect
    def _bad_verify(value):
        raise RuntimeError("sso verify failed")

    adapter = _adapter(_verify_sso_header=_bad_verify)
    adapter._sessions = {}
    req = _req(path="/private", headers={"X-OMX-SSO": "bad-token"}, adapter=adapter)
    with pytest.raises(web.HTTPFound):
        await wc.auth_middleware(req, _handler_ok)


# ── Basic Auth ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_basic_auth_parse_raises():
    # 458-459: valid Basic Auth header, auth_manager.authenticate raises →
    # caught (debug log) → falls through to 401 (463)
    credentials = _b64.b64encode(b"alice:pass").decode()
    adapter = _adapter(auth_manager=_RaisingAuth())
    req = _req(
        path="/private",
        headers={"Authorization": f"Basic {credentials}"},
        adapter=adapter,
    )
    res = await wc.auth_middleware(req, _handler_ok)
    assert res.status == 401
    assert "WWW-Authenticate" in res.headers


@pytest.mark.asyncio
async def test_basic_auth_wrong_credentials():
    # 456-463: valid Basic Auth header, auth_manager rejects credentials →
    # authenticated_via_basic stays False → 401 with WWW-Authenticate
    credentials = _b64.b64encode(b"alice:wrong").decode()
    adapter = _adapter(auth_manager=_RefusingAuth())
    req = _req(
        path="/private",
        headers={"Authorization": f"Basic {credentials}"},
        adapter=adapter,
    )
    res = await wc.auth_middleware(req, _handler_ok)
    assert res.status == 401
    assert "WWW-Authenticate" in res.headers


# ── session cookie fallback (step 2) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_cookie_fallback_authenticates():
    # 469: no SSO, no Basic Auth, valid session cookie → step 2 returns True
    # → handler called
    adapter = _adapter(_sessions={"sess1": _valid_session()})
    req = _req(path="/private", cookies={_COOKIE: "sess1"}, adapter=adapter)
    res = await wc.auth_middleware(req, _handler_ok)
    assert res.status == 200
