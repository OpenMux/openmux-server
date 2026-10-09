"""Branch tests for WebSocketClientAdapter.connect.

Pins the path construction variants (plain name, composite ``sid::name``,
explicit origin_server_id, existing query string, custom/empty path
prefix), the Basic-Auth header, and the two handshake failure paths
(ws_connect error, immediate close / close on first receive). No real
sockets: ``aiohttp.ClientSession`` is replaced with a scripted fake.
"""

import asyncio

import aiohttp
import pytest

from openmux.client.adapters.websocket_adapter import WebSocketClientAdapter


class _FakeWSDummy:
    def __init__(self, closed=False, receive_timeout=True, msg_type=None):
        self.closed = closed
        self._receive_timeout = receive_timeout
        self._msg_type = msg_type

    async def receive(self):
        if self._receive_timeout:
            await asyncio.sleep(10)  # wait_for(0.05) cancels this
        if self._msg_type is not None:
            return aiohttp.WSMessage(self._msg_type, None, "")
        return None


class _FakeClientSession:
    """Scripted ClientSession stand-in; records ws_connect args."""

    def __init__(self, factory):
        self._factory = factory
        self.connect_calls = []
        self.closed = False

    async def ws_connect(self, url, headers=None):
        self.connect_calls.append((url, dict(headers or {})))
        return self._factory()

    async def close(self):
        self.closed = True


def _make_adapter(**config):
    return WebSocketClientAdapter("h", 1234, config if config else None)


def _patch_aiohttp(monkeypatch, make_ws):
    """Point aiohttp.ClientSession at a fake; returns a holder for the instance."""
    holder = {}

    class _Session(_FakeClientSession):
        def __init__(self, timeout=None):
            super().__init__(make_ws)
            holder["session"] = self

    monkeypatch.setattr(aiohttp, "ClientSession", _Session)
    return holder


@pytest.mark.asyncio
async def test_connect_plain_name_builds_default_path(monkeypatch):
    holder = _patch_aiohttp(monkeypatch, _FakeWSDummy)
    a = _make_adapter()
    a.port_name = "console1"
    a.basic_user = "u"
    a.basic_password = "p"
    assert await a.connect()
    url, headers = holder["session"].connect_calls[0]
    assert url == "http://h:1234/ws/console1?meta=1"
    assert headers["Authorization"].startswith("Basic ")
    assert a.is_connected and a.is_authenticated and a.username == "u"
    assert a._aiohttp_session is holder["session"]


@pytest.mark.asyncio
async def test_connect_composite_name_splits_server_id(monkeypatch):
    holder = _patch_aiohttp(monkeypatch, _FakeWSDummy)
    a = _make_adapter()
    a.port_name = "srv7::console2"
    assert await a.connect()
    url, _ = holder["session"].connect_calls[0]
    assert url == "http://h:1234/ws/srv7/console2?meta=1"


@pytest.mark.asyncio
async def test_connect_origin_config_beats_composite_name(monkeypatch):
    holder = _patch_aiohttp(monkeypatch, _FakeWSDummy)
    a = _make_adapter()
    a.port_name = "srv7::console2"
    a.origin_server_id = "other"
    assert await a.connect()
    url, _ = holder["session"].connect_calls[0]
    # origin_server_id wins the path; the composite name is kept unsplit
    assert url == "http://h:1234/ws/other/srv7::console2?meta=1"


@pytest.mark.asyncio
async def test_connect_composite_with_empty_sid_keeps_plain_name(monkeypatch):
    holder = _patch_aiohttp(monkeypatch, _FakeWSDummy)
    a = _make_adapter()
    a.port_name = "::console3"
    assert await a.connect()
    url, _ = holder["session"].connect_calls[0]
    # Empty sid part is rejected by the heuristic; the full name is kept
    assert url == "http://h:1234/ws/::console3?meta=1"


@pytest.mark.asyncio
async def test_connect_existing_query_appends_meta_flag(monkeypatch):
    holder = _patch_aiohttp(monkeypatch, _FakeWSDummy)
    a = _make_adapter()
    a.port_name = "console4?foo=1"
    assert await a.connect()
    url, _ = holder["session"].connect_calls[0]
    assert url == "http://h:1234/ws/console4?foo=1&meta=1"


@pytest.mark.asyncio
async def test_connect_query_already_annotated_is_not_annotated_twice(monkeypatch):
    holder = _patch_aiohttp(monkeypatch, _FakeWSDummy)
    a = _make_adapter()
    a.port_name = "console5?meta=x"
    assert await a.connect()
    url, _ = holder["session"].connect_calls[0]
    assert url == "http://h:1234/ws/console5?meta=x"


@pytest.mark.asyncio
async def test_connect_custom_prefix_and_empty_prefix(monkeypatch):
    holder = _patch_aiohttp(monkeypatch, _FakeWSDummy)
    a = _make_adapter()
    a.port_name = "c"
    a.path_prefix = "/custom/"
    assert await a.connect()
    assert holder["session"].connect_calls[0][0] == "http://h:1234/custom/c?meta=1"

    b = _make_adapter()
    b.port_name = "c"
    b.path_prefix = ""
    assert await b.connect()
    assert holder["session"].connect_calls[0][0] == "http://h:1234/ws/c?meta=1"


@pytest.mark.asyncio
async def test_connect_tls_scheme(monkeypatch):
    holder = _patch_aiohttp(monkeypatch, _FakeWSDummy)
    a = _make_adapter()
    a.port_name = "c"
    a.use_tls = True
    assert await a.connect()
    assert holder["session"].connect_calls[0][0].startswith("https://h:1234/")


@pytest.mark.asyncio
async def test_connect_without_creds_has_no_auth_header(monkeypatch):
    holder = _patch_aiohttp(monkeypatch, _FakeWSDummy)
    a = _make_adapter()
    a.port_name = "c"
    assert await a.connect()
    url, headers = holder["session"].connect_calls[0]
    assert "Authorization" not in headers
    assert a.is_connected and a.username is None


@pytest.mark.asyncio
async def test_connect_ws_handshake_error_returns_false_and_closes(monkeypatch):
    holder = {}

    def _factory():
        raise aiohttp.ClientError("handshake refused")

    class _Session(_FakeClientSession):
        def __init__(self, timeout=None):
            super().__init__(_factory)
            holder["session"] = self

    monkeypatch.setattr(aiohttp, "ClientSession", _Session)
    a = _make_adapter()
    a.port_name = "c"
    assert await a.connect() is False
    assert not a.is_connected
    assert a.websocket is None
    assert a._aiohttp_session is None
    assert holder["session"].closed is True


@pytest.mark.asyncio
async def test_connect_immediately_closed_socket_returns_false(monkeypatch):
    holder = _patch_aiohttp(monkeypatch, lambda: _FakeWSDummy(closed=True))
    a = _make_adapter()
    a.port_name = "c"
    assert await a.connect() is False
    assert not a.is_connected
    assert a.websocket is None
    assert holder["session"].closed is True


@pytest.mark.asyncio
async def test_connect_first_receive_close_returns_false(monkeypatch):
    holder = _patch_aiohttp(monkeypatch, lambda: _FakeWSDummy(receive_timeout=False, msg_type=aiohttp.WSMsgType.CLOSE))
    a = _make_adapter()
    a.port_name = "c"
    assert await a.connect() is False
    assert not a.is_connected
    assert a.websocket is None
    assert holder["session"].closed is True


@pytest.mark.asyncio
async def test_connect_import_failure_returns_false():
    import builtins

    real_import = builtins.__import__

    def _broken(name, *args, **kwargs):
        if name == "aiohttp":
            raise ImportError("aiohttp missing")
        return real_import(name, *args, **kwargs)

    import builtins as _b

    real = _b.__import__
    _b.__import__ = _b.__import__  # keep reference for clarity

    try:
        builtins.__import__ = _broken
        a = _make_adapter()
        assert await a.connect() is False
        assert not a.is_connected
    finally:
        builtins.__import__ = real
