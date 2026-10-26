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

The last tests cover the connect peek in `_ws_handshake_session`. The server answers an
attach with one control frame, and refuses with that frame followed by a close whose reason
carries its sentence (issue #98). Both arrive before the reader starts, so the peek has to
hand them on instead of dropping them.
"""

import asyncio
import json
import types

import pytest
from aiohttp import WSMsgType

from openmux.client.adapters.websocket_adapter import WebSocketClientAdapter, ws_close_reason
from openmux.server.access_decision import CLOSE_ATTACH_DENIED, Code, Mode, decide
from tests.support.decisions import holders_frame_of


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


class _FakeHandshakeSession(_FakeAiohttpSession):
    """A session whose handshake yields a prepared message queue."""

    def __init__(self, ws) -> None:
        super().__init__()
        self._ws = ws

    async def ws_connect(self, url, headers=None):
        return self._ws


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
    adapter._pending_ws_messages = []
    adapter.close_reason = ""
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
async def test_read_data_client_mode_returns_the_servers_lines():
    # The frame is the one the server builds, so the CLI is tested against the real wire
    # shape instead of a hand-written dict (issue #98).
    info = decide(Code.GRANTED, mode=Mode.READ_WRITE, client_id="c1").frame()
    adapter = _adapter(_FakeWebSocket([_FakeMessage(WSMsgType.TEXT, _ctrl_frame(info))]))
    out = await adapter.read_data()
    assert isinstance(out, str)
    assert out == "\r\n[Read-write access granted]\r\n"
    assert adapter.access_mode == "read-write"


@pytest.mark.asyncio
async def test_read_data_client_mode_without_lines_is_silent():
    # An attach grant carries no line: the CLI stays silent behind its connect banner.
    info = decide(Code.ATTACHED, mode=Mode.READ_WRITE, client_id="c1").frame()
    adapter = _adapter(_FakeWebSocket([_FakeMessage(WSMsgType.TEXT, _ctrl_frame(info))]))
    assert await adapter.read_data() == b""
    assert adapter.access_mode == "read-write"


@pytest.mark.asyncio
async def test_read_data_rw_holders_returns_formatted_text():
    port = type(
        "P", (), {"connected_clients": [{"client_id": "A", "username": "a", "mode": "read-write"}], "max_read_write_users": 1}
    )()
    info = holders_frame_of(port)
    adapter = _adapter(_FakeWebSocket([_FakeMessage(WSMsgType.TEXT, _ctrl_frame(info))]))
    out = await adapter.read_data()
    assert isinstance(out, str)
    assert out == "\r\n[Read-write held by: [A] a@unknown (rw)]\r\n"
    assert adapter.rw_holders == ["[A] a@unknown (rw)"]
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


@pytest.mark.asyncio
async def test_connect_peek_hands_the_attach_answer_to_the_reader():
    """A frame the peek already read is replayed, so the reader still applies it."""
    granted = decide(Code.ATTACHED, mode=Mode.READ_WRITE, client_id="c1").frame()
    ws = _FakeWebSocket([_FakeMessage(WSMsgType.TEXT, _ctrl_frame(granted))])
    adapter = _adapter(None)

    assert await adapter._ws_handshake_session(_FakeHandshakeSession(ws), "ws://x/ws/p1", {}) is ws

    assert await adapter.read_data() == b""  # a silent grant renders nothing
    assert adapter.access_mode == "read-write"  # the frame was applied, not dropped
    assert adapter._pending_ws_messages == []


@pytest.mark.asyncio
async def test_a_refusal_sentence_reaches_the_reader_before_the_close():
    """Refusal frame then close: the reader gets the sentence, then the close."""
    refusal = decide(Code.PROMOTE_FAILED, mode=Mode.READ_ONLY, client_id="c1")
    ws = _FakeWebSocket(
        [
            _FakeMessage(WSMsgType.TEXT, _ctrl_frame(refusal.frame())),
            _FakeMessage(WSMsgType.CLOSE, CLOSE_ATTACH_DENIED, refusal.message.encode()),
        ]
    )
    adapter = _adapter(None)

    assert await adapter._ws_handshake_session(_FakeHandshakeSession(ws), "ws://x/ws/p1", {}) is ws

    message = await adapter.read_data()
    text = message if isinstance(message, str) else message.decode("utf-8", "replace")
    assert "failed to grant read-write access" in text
    assert await adapter.read_data() is None
    assert adapter.is_connected is False


@pytest.mark.asyncio
async def test_a_close_before_any_frame_keeps_its_reason():
    """When the server closes first, its code and sentence survive on the adapter."""
    ws = _FakeWebSocket([_FakeMessage(WSMsgType.CLOSE, CLOSE_ATTACH_DENIED, b"Access denied to this port")])
    adapter = _adapter(None)

    assert await adapter._ws_handshake_session(_FakeHandshakeSession(ws), "ws://x/ws/p1", {}) is None
    assert adapter.websocket is None
    assert adapter.close_reason == f"{CLOSE_ATTACH_DENIED} Access denied to this port"


def test_ws_close_reason_reads_code_and_sentence():
    """`ws_close_reason` handles a code alone, a reason alone, and neither."""
    assert ws_close_reason(_FakeMessage(WSMsgType.CLOSE, 4003, b"Denied")) == "4003 Denied"
    assert ws_close_reason(_FakeMessage(WSMsgType.CLOSE, 1011, None)) == "1011"
    assert ws_close_reason(_FakeMessage(WSMsgType.CLOSE, None, None)) == ""
    # Undecodable bytes must not raise: the reason is shown, never parsed.
    assert ws_close_reason(_FakeMessage(WSMsgType.CLOSE, 4003, b"\xff\xfe")).startswith("4003")
