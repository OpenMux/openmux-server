"""Tests that a refused console stream reports the server's sentence (issue #98).

The server closes a refused WebSocket console stream with its access sentence in the close
reason. The peek and replay tests live in `test_websocket_adapter_read_data.py`. These cover
the last step: the sentence must survive from the adapter up to what the Client prints, or
the user sees only an unreachable host.
"""

import types

import pytest

import openmux.client.main as client_main
from openmux.client.adapters.websocket_adapter import WebSocketClientAdapter
from openmux.server.access_decision import CLOSE_ATTACH_DENIED

REFUSAL = f"{CLOSE_ATTACH_DENIED} Access denied to this port"


class _NoopSession:
    """Stand-in for `aiohttp.ClientSession`: `connect()` builds one and never uses it."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def close(self) -> None:
        pass


def _adapter() -> WebSocketClientAdapter:
    """An adapter with no socket, only the fields `connect()` reads, plus recorded warnings."""
    adapter = WebSocketClientAdapter.__new__(WebSocketClientAdapter)
    adapter.name = "wstest"
    adapter.is_connected = False
    adapter.websocket = None
    adapter.close_reason = ""
    adapter.host = "openmux.example"
    adapter.port = 443
    adapter.use_tls = True
    adapter.port_name = "p1"
    adapter.origin_server_id = None
    adapter.timeout = 1.0
    adapter.path_prefix = "/ws/"
    adapter.basic_user = None
    adapter.basic_password = None
    adapter.warnings = []
    adapter.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda fmt, *a: adapter.warnings.append(fmt % a if a else fmt),
        error=lambda *a, **k: None,
    )
    return adapter


@pytest.mark.asyncio
async def test_connect_warns_with_the_servers_sentence(monkeypatch):
    """A refusal during the handshake is logged, not flattened into a failed connect."""
    monkeypatch.setattr("aiohttp.ClientSession", _NoopSession)
    adapter = _adapter()

    async def refuse(session, url, headers):
        adapter.close_reason = REFUSAL
        await session.close()
        return None

    monkeypatch.setattr(adapter, "_ws_handshake_session", refuse)

    assert await adapter.connect() is False
    assert adapter.close_reason == REFUSAL
    assert adapter.warnings == [f"Server refused the console stream: {REFUSAL}"]


@pytest.mark.asyncio
async def test_a_refused_connect_without_a_reason_stays_plain(monkeypatch):
    """An unreachable host has no sentence, so the old message must not gain a colon."""
    monkeypatch.setattr("aiohttp.ClientSession", _NoopSession)
    adapter = _adapter()

    async def refuse(session, url, headers):
        await session.close()
        return None

    monkeypatch.setattr(adapter, "_ws_handshake_session", refuse)

    assert await adapter.connect() is False
    assert adapter.warnings == []


@pytest.mark.asyncio
async def test_connect_to_server_puts_the_sentence_in_its_error(monkeypatch):
    """`connect_to_server` reports why the server refused, not just that it did."""

    class _Refused:
        close_reason = REFUSAL

        async def connect(self):
            return False

    logged = []
    client = client_main.OpenMuxClient.__new__(client_main.OpenMuxClient)
    client.config = {}
    client.logger = types.SimpleNamespace(error=lambda fmt, *a: logged.append(fmt % a))
    monkeypatch.setattr(client_main.ClientAdapterFactory, "create_adapter", lambda **kwargs: _Refused())

    created = await client.connect_to_server("openmux.example", 443, adapter_type="websocket", port_name="p1")

    assert created is None
    assert logged == [f"Failed to connect to openmux.example:443: {REFUSAL}"]
