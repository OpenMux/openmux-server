"""Branch coverage for `WebSocketClientAdapter.read_data` (C901 work, 21-bracket).

The function dispatches on the aiohttp `WSMessage` type and on the OMXCTRL
text-frame payload type. These tests drive `read_data` with a scripted fake
websocket (no server) and pin every branch:

- OMXCTRL power_feeds / power_switch: stashed on `last_power_reply`, `b""` back
- OMXCTRL client_mode / rw_holders: routed through `format_control_response`
- OMXCTRL meta: `port_up` tracking, disconnect/reconnect notices
- OMXCTRL parse failure: swallowed as `b""`
- plain text / binary payload: passed through verbatim
- CLOSE / CLOSING / CLOSED: marks disconnected, closes the aiohttp session
- PING: `b""`
- timeout while open: `b""`
- receive raising: marks disconnected, closes the session, returns None
- not connected: immediate None
"""

import asyncio
import json
import types

import pytest
from aiohttp import WSMsgType

from openmux.client.adapters.websocket_adapter import WebSocketClientAdapter


class _FakeMessage:
    """Minimal stand-in for an aiohttp `WSMessage` carrying type + data."""

    def __init__(self, mtype: WSMsgType, data=None, extra: object = None) -> None:
        self.type = mtype
        self.data = data
        self.extra = extra
        self.text = data


class _FakeWebSocket:
    def __init__(self, messages) -> None:
        self.messages = list(messages)

    async def receive(self):
        if not self.messages:
            raise AssertionError("receive() called but the message queue is empty")
        return self.messages.pop(0)


class _FakeAiohttpSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _adapter(ws) -> WebSocketClientAdapter:
    """A bare adapter with `ws` as its websocket (no real connection)."""
    adapter = WebSocketClientAdapter.__new__(WebSocketClientAdapter)
    adapter.name = "wstest"
    adapter.logger = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None)
    adapter.is_connected = True
    adapter.websocket = ws
    adapter._aiohttp_session = _FakeAiohttpSession()
    adapter.last_power_reply = None
    adapter.access_mode = "read-only"
    adapter.rw_holders = None
    adapter.max_rw_users = None
    adapter._port_up = None
    return adapter


def _ctrl_frame(data) -> str:
    return "OMXCTRL " + json.dumps(data)


@pytest.mark.asyncio
async def test_read_data_not_connected_returns_none():
    adapter = _adapter(_FakeWebSocket([]))
    adapter.is_connected = False
    assert await adapter.read_data(timeout=0.01) is None


@pytest.mark.asyncio
async def test_read_data_power_feeds_stashed_returns_empty():
    info = {"type": "power_feeds", "feeds": ["r.1"], "feeds_total": 1}
    adapter = _adapter(_FakeWebSocket([_FakeMessage(WSMsgType.TEXT, _ctrl_frame(info))]))
    assert await adapter.read_data() == b""
    assert adapter.last_power_reply == info


@pytest.mark.asyncio
async def test_read_data_power_switch_stashed_returns_empty():
    info = {"type": "power_switch", "ok": True, "ref": "r.1"}
    adapter = _adapter(_FakeWebSocket([_FakeMessage(WSMsgType.TEXT, _ctrl_frame(info))]))
    assert await adapter.read_data() == b""
    assert adapter.last_power_reply == info


@pytest.mark.asyncio
async def test_read_data_client_mode_returns_formatted_text():
    info = {"type": "client_mode", "mode": "read-write", "ok": True}
    adapter = _adapter(_FakeWebSocket([_FakeMessage(WSMsgType.TEXT, _ctrl_frame(info))]))
    out = await adapter.read_data()
    assert isinstance(out, str)
    assert "[Read-write access granted]" in out
    assert adapter.access_mode == "read-write"


@pytest.mark.asyncio
async def test_read_data_rw_holders_returns_formatted_text():
    info = {"type": "rw_holders", "holders": ["[A] a (rw)"], "max_rw_users": 1}
    adapter = _adapter(_FakeWebSocket([_FakeMessage(WSMsgType.TEXT, _ctrl_frame(info))]))
    out = await adapter.read_data()
    assert isinstance(out, str)
    assert "Held by: [A] a (rw)" in out
    assert adapter.rw_holders == ["[A] a (rw)"]
    assert adapter.max_rw_users == 1


@pytest.mark.asyncio
async def test_read_data_meta_port_up_transitions():
    ws = _FakeWebSocket(
        [
            _FakeMessage(WSMsgType.TEXT, _ctrl_frame({"type": "meta", "connected": False})),
            _FakeMessage(WSMsgType.TEXT, _ctrl_frame({"type": "meta", "connected": True})),
        ]
    )
    adapter = _adapter(ws)
    # First meta: the initial (down) state only reports a notice when down.
    out = await adapter.read_data()
    assert isinstance(out, str)
    assert "[Port disconnected on server]" in out
    assert adapter._port_up is False
    out = await adapter.read_data()  # False -> True: reconnected notice
    assert isinstance(out, str)
    assert "[Reconnected]" in out
    assert adapter._port_up is True


@pytest.mark.asyncio
async def test_read_data_meta_stable_state_swallowed():
    # No state change: the meta frame is suppressed (returns b"").
    for value in (True, False):
        adapter = _adapter(
            _FakeWebSocket(
                [
                    _FakeMessage(WSMsgType.TEXT, _ctrl_frame({"type": "meta", "connected": value})),
                    _FakeMessage(WSMsgType.TEXT, _ctrl_frame({"type": "meta", "connected": value})),
                ]
            )
        )
        first = await adapter.read_data()
        if value is False:
            assert isinstance(first, str) and "[Port disconnected on server]" in first
        else:
            assert first == b""
        assert await adapter.read_data() == b""  # stable: swallowed


@pytest.mark.asyncio
async def test_read_data_ctrl_json_parse_failure_swallowed():
    adapter = _adapter(_FakeWebSocket([_FakeMessage(WSMsgType.TEXT, "OMXCTRL {not-json")]))
    assert await adapter.read_data() == b""


@pytest.mark.asyncio
async def test_read_data_plain_text_passthrough():
    adapter = _adapter(_FakeWebSocket([_FakeMessage(WSMsgType.TEXT, "hello")]))
    assert await adapter.read_data() == "hello"


@pytest.mark.asyncio
async def test_read_data_binary_passthrough():
    adapter = _adapter(_FakeWebSocket([_FakeMessage(WSMsgType.BINARY, b"\x01\x02")]))
    assert await adapter.read_data() == b"\x01\x02"


@pytest.mark.asyncio
async def test_read_data_close_marks_disconnected_and_closes_session():
    for mtype in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
        adapter = _adapter(_FakeWebSocket([_FakeMessage(mtype)]))
        assert await adapter.read_data() is None
        assert adapter.is_connected is False
        assert adapter._aiohttp_session.closed is True


@pytest.mark.asyncio
async def test_read_data_ping_returns_empty():
    adapter = _adapter(_FakeWebSocket([_FakeMessage(WSMsgType.PING, b"ping")]))
    assert await adapter.read_data() == b""


@pytest.mark.asyncio
async def test_read_data_timeout_returns_empty():
    async def never():
        await asyncio.sleep(3600)

    ws = _FakeWebSocket([])
    ws.receive = never
    adapter = _adapter(ws)
    assert (await adapter.read_data(timeout=0.01)) == b""


class _RaisingWebSocket:
    async def receive(self):
        raise ConnectionError("boom")


@pytest.mark.asyncio
async def test_read_data_receive_exception_closes_and_returns_none():
    adapter = _adapter(_RaisingWebSocket())
    assert await adapter.read_data() is None
    assert adapter.is_connected is False
    assert adapter._aiohttp_session.closed is True
