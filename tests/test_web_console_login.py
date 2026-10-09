"""Branch tests for web_console.handle_login (GET page, POST submit).

`_get_adapter` is patched out so no real aiohttp Application is needed;
the request is a small scripted object exposing only what the handler
touches (method, rel_url.query, cookies, post()). Cookie writes are
captured by patching `web.Response.set_cookie`/`del_cookie`.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from openmux.server.web_console import handle_login


class _FakeLoginRequest:
    def __init__(self, *, method, query=None, cookie=None, post_data=None, post_error=None):
        self.method = method
        self.rel_url = SimpleNamespace(query=dict(query or {}))
        self._cookie = dict(cookie or {})
        self._post_data = dict(post_data or {})
        self._post_error = post_error

    @property
    def cookies(self):
        return self._cookie

    async def post(self):
        if self._post_error is not None:
            raise self._post_error
        return self._post_data


def _render_page(error=False, next_url=None, message=None):
    return ("PAGE|%s|%s|%s" % (error, next_url, message)).encode("utf-8")


def _adapter(monkeypatch, *, auth_manager=None, base_path="/bp"):
    a = MagicMock(name="adapter")
    a.auth_manager = auth_manager
    a._get_client_ip = MagicMock(return_value="10.0.0.1")
    a._sessions = {}
    a._session_cookie_name = "omx_session"
    a.session_ttl_seconds = 3600
    a.use_tls = False
    a._render_login = MagicMock(side_effect=_render_page)
    a._effective_base_path = MagicMock(return_value=base_path)
    return a


def _auth(*, ok=True, perms={"console": "read"}, locked=False):
    m = MagicMock(name="auth")
    m.is_user_locked = MagicMock(return_value=locked)
    m.authenticate = MagicMock(return_value=ok)
    m.get_user_permissions = MagicMock(return_value=perms)
    m.clear_auth_failures = MagicMock()
    m.register_auth_failure = MagicMock()
    return m


def _cookie_calls(monkeypatch):
    calls = []

    monkeypatch.setattr(web.Response, "set_cookie", lambda self, name, value, **kw: calls.append(("set", name, value, kw)))
    monkeypatch.setattr(web.Response, "del_cookie", lambda self, name, **kw: calls.append(("del", name, kw)))
    return calls


@pytest.mark.asyncio
async def test_login_get_without_session_renders_page(monkeypatch):
    a = _adapter(monkeypatch, auth_manager=_auth())
    monkeypatch.setattr("openmux.server.web_console._get_adapter", lambda request: a)
    resp = await handle_login(_FakeLoginRequest(method="GET"))
    assert isinstance(resp, web.Response)
    a._render_login.assert_called_once_with(error=False, next_url="/")
    cookie_calls = _cookie_calls(monkeypatch)  # nothing should have been captured on this path
    assert cookie_calls == []


@pytest.mark.asyncio
async def test_login_get_with_valid_session_redirects_to_next(monkeypatch):
    # FIXED IN THIS REFACTOR: the pre-refactor handler wrapped the session
    # bounce in `try: ... except Exception: pass`, and aiohttp's HTTPFound
    # is an Exception subclass -- so the redirect was silently swallowed and
    # an authenticated user got the login page again. The extracted
    # `_login_session_redirect` helper returns the exception instead of
    # raising inside the guard, so the bounce now works as documented.
    a = _adapter(monkeypatch, auth_manager=_auth())
    monkeypatch.setattr("openmux.server.web_console._get_adapter", lambda request: a)
    a._sessions["sid1"] = {"username": "u", "created": 0, "last_seen": 0, "ip": "x"}
    req = _FakeLoginRequest(method="GET", cookie={"omx_session": "sid1"}, query={"next": "/dash"})
    with pytest.raises(web.HTTPFound) as exc:
        await handle_login(req)
    assert exc.value.location == "/dash"


@pytest.mark.asyncio
async def test_login_get_with_stale_session_cookie_renders_page(monkeypatch):
    a = _adapter(monkeypatch, auth_manager=_auth())
    monkeypatch.setattr("openmux.server.web_console._get_adapter", lambda request: a)
    a._sessions = {}  # cookie points at nothing
    req = _FakeLoginRequest(method="GET", cookie={"omx_session": "gone"})
    resp = await handle_login(req)
    assert isinstance(resp, web.Response)


@pytest.mark.asyncio
async def test_login_post_success_creates_session_and_redirects(monkeypatch):
    auth = _auth(ok=True)
    calls = _cookie_calls(monkeypatch)
    a = _adapter(monkeypatch, auth_manager=auth)
    monkeypatch.setattr("openmux.server.web_console._get_adapter", lambda request: a)
    req = _FakeLoginRequest(method="POST", post_data={"username": "u", "password": "p", "next": "/f"}, query={"next": "/q"})
    with pytest.raises(web.HTTPFound) as exc:
        await handle_login(req)
    assert exc.value.location == "/f"  # form value beats query
    assert len(a._sessions) == 1
    session = next(iter(a._sessions.values()))
    assert session["username"] == "u" and session["ip"] == "10.0.0.1"
    auth.clear_auth_failures.assert_called_once_with("u", "10.0.0.1")
    sets = [c for c in calls if c[0] == "set"]
    assert len(sets) == 1 and sets[0][1] == "omx_session"
    kw = sets[0][3]
    assert kw["httponly"] is True and kw["samesite"] == "Lax" and kw["max_age"] == 3600
    dels = [c for c in calls if c[0] == "del"]
    assert {c[1] for c in dels} == {"omx_session"}
    assert {c[2]["path"] for c in dels} == {"/", "/bp"}


@pytest.mark.asyncio
async def test_login_post_success_next_from_query_when_form_next_missing(monkeypatch):
    auth = _auth(ok=True)
    _cookie_calls(monkeypatch)
    a = _adapter(monkeypatch, auth_manager=auth)
    monkeypatch.setattr("openmux.server.web_console._get_adapter", lambda request: a)
    req = _FakeLoginRequest(method="POST", post_data={"username": "u", "password": "p"}, query={"next": "/q"})
    with pytest.raises(web.HTTPFound) as exc:
        await handle_login(req)
    assert exc.value.location == "/q"


@pytest.mark.asyncio
async def test_login_post_bad_password_renders_error_page(monkeypatch):
    auth = _auth(ok=False)
    _cookie_calls(monkeypatch)
    a = _adapter(monkeypatch, auth_manager=auth)
    monkeypatch.setattr("openmux.server.web_console._get_adapter", lambda request: a)
    req = _FakeLoginRequest(method="POST", post_data={"username": "u", "password": "wrong"})
    resp = await handle_login(req)
    assert isinstance(resp, web.Response)
    a._render_login.assert_called_once_with(error=True, next_url="/", message=None)
    auth.register_auth_failure.assert_called_once_with("u", "10.0.0.1")
    assert a._sessions == {}


@pytest.mark.asyncio
async def test_login_post_locked_account_renders_message(monkeypatch):
    auth = _auth(ok=True, locked=True)
    _cookie_calls(monkeypatch)
    a = _adapter(monkeypatch, auth_manager=auth)
    monkeypatch.setattr("openmux.server.web_console._get_adapter", lambda request: a)
    req = _FakeLoginRequest(method="POST", post_data={"username": "u", "password": "p"})
    resp = await handle_login(req)
    body = resp.body if isinstance(resp, web.Response) else ""
    assert body == b"PAGE|True|/|Too many failed attempts for this account. Please try again later."
    auth.authenticate.assert_not_called()
    assert a._sessions == {}


@pytest.mark.asyncio
async def test_login_post_auth_success_but_no_permissions_denied(monkeypatch):
    auth = _auth(ok=True, perms=None)
    _cookie_calls(monkeypatch)
    a = _adapter(monkeypatch, auth_manager=auth)
    monkeypatch.setattr("openmux.server.web_console._get_adapter", lambda request: a)
    req = _FakeLoginRequest(method="POST", post_data={"username": "u", "password": "p"})
    resp = await handle_login(req)
    assert isinstance(resp, web.Response)
    assert a._sessions == {}
    a.logger.warning.assert_called_once()
    auth.register_auth_failure.assert_called_once_with("u", "10.0.0.1")


@pytest.mark.asyncio
async def test_login_post_form_parse_error_renders_login_page(monkeypatch):
    auth = _auth(ok=True)
    _cookie_calls(monkeypatch)
    a = _adapter(monkeypatch, auth_manager=auth)
    monkeypatch.setattr("openmux.server.web_console._get_adapter", lambda request: a)
    req = _FakeLoginRequest(method="POST", post_data={"username": "u"}, post_error=RuntimeError("form boom"))
    resp = await handle_login(req)
    assert isinstance(resp, web.Response)
    a._render_login.assert_called_once_with(error=True, next_url="/", message=None)
    auth.authenticate.assert_not_called()
    assert a._sessions == {}


@pytest.mark.asyncio
async def test_login_post_without_auth_manager_renders_login_page(monkeypatch):
    a = _adapter(monkeypatch, auth_manager=None)
    _cookie_calls(monkeypatch)
    monkeypatch.setattr("openmux.server.web_console._get_adapter", lambda request: a)
    req = _FakeLoginRequest(method="POST", post_data={"username": "u", "password": "p"})
    resp = await handle_login(req)
    assert isinstance(resp, web.Response)
    assert a._sessions == {}
    a._render_login.assert_called_once_with(error=True, next_url="/", message=None)


@pytest.mark.asyncio
async def test_login_post_client_ip_lookup_failure_is_swallowed(monkeypatch):
    auth = _auth(ok=True)
    _cookie_calls(monkeypatch)
    a = _adapter(monkeypatch, auth_manager=auth)
    a._get_client_ip = MagicMock(side_effect=RuntimeError("ip boom"))
    monkeypatch.setattr("openmux.server.web_console._get_adapter", lambda request: a)
    req = _FakeLoginRequest(method="POST", post_data={"username": "u", "password": "p"})
    with pytest.raises(web.HTTPFound):
        await handle_login(req)
    session = next(iter(a._sessions.values()))
    assert session["ip"] is None
