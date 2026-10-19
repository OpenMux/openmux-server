"""Site event hub tests (openmux/server/site_events.py) and the /ws/events handler.

Unit level: fan-out, dead-send survival, sync-context safety, and the
`WebConsoleAdapter.publish_site_event()` wrapper. The end-to-end socket walk
(real HTTP + a published frame reaching a client) lives in
tests/test_port_actions_plugin.py::test_ws_events_delivers_action_status_frames_end_to_end.
"""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from openmux.server.site_events import SiteEventHub


class _FakeWS:
    """Stands in for aiohttp WebSocketResponse at the send_str boundary."""

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_str(self, text):
        if self.fail:
            raise ConnectionResetError("peer is gone")
        self.sent.append(text)


class _RecordingHub(SiteEventHub):
    def __init__(self):
        super().__init__()
        self.added = []
        self.discarded = []

    def add(self, ws):
        self.added.append(ws)
        super().add(ws)

    def discard(self, ws):
        self.discarded.append(ws)
        super().discard(ws)


@pytest.mark.asyncio
async def test_publish_fans_out_json_frames_and_discard_stops_delivery():
    hub = SiteEventHub()
    a, b = _FakeWS(), _FakeWS()
    hub.add(a)
    hub.add(b)

    hub.publish({"type": "action_status", "port": "p1"})
    await asyncio.sleep(0.01)
    assert json.loads(a.sent[0]) == {"type": "action_status", "port": "p1"}
    assert b.sent == a.sent

    hub.discard(a)
    hub.publish({"type": "x"})
    await asyncio.sleep(0.01)
    assert len(a.sent) == 1  # no longer subscribed
    assert len(b.sent) == 2
    assert hub.client_count() == 1


@pytest.mark.asyncio
async def test_publish_survives_dead_client_and_unserializable_frame():
    """A failing send must not break the other clients (their sockets' own
    handler prunes them), and a producer building a bad frame must not raise."""
    hub = SiteEventHub()
    dead, alive = _FakeWS(fail=True), _FakeWS()
    hub.add(dead)
    hub.add(alive)

    hub.publish({"type": "t"})
    hub.publish({"type": "t", "bad": object()})
    await asyncio.sleep(0.01)

    assert alive.sent == ['{"type":"t"}']
    assert hub.client_count() == 2  # dead-socket pruning is the handler's job


def test_publish_without_running_loop_is_silent():
    """Sync contexts (CLI runner, unit tests) have no connected browser at all:
    the frame is dropped without raising."""
    hub = SiteEventHub()
    ws = _FakeWS()
    hub.add(ws)

    hub.publish({"type": "t"})

    assert ws.sent == []


def test_publish_site_event_forwards_to_the_hub():
    from openmux.server.web_console import WebConsoleAdapter

    hub = _RecordingHub()
    published = []
    hub.publish = published.append  # replace fan-out; the wrapper must pass the dict through
    adapter = SimpleNamespace(_site_events=hub, logger=logging.getLogger("test.site_events"))

    WebConsoleAdapter.publish_site_event(adapter, {"type": "action_status", "port": "p1"})

    assert published == [{"type": "action_status", "port": "p1"}]


def test_publish_site_event_swallows_hub_failures():
    from openmux.server.web_console import WebConsoleAdapter

    class _BoomHub:
        def publish(self, event):
            raise RuntimeError("hub exploded")

    adapter = SimpleNamespace(_site_events=_BoomHub(), logger=logging.getLogger("test.site_events"))

    WebConsoleAdapter.publish_site_event(adapter, {"type": "x"})  # must not raise


@pytest.mark.asyncio
async def test_handle_ws_events_registers_and_prunes_socket(monkeypatch):
    """The /ws/events handler adds the socket to the hub for the stream's
    lifetime and prunes it on close (the fake socket ends the iteration
    immediately, simulating a disconnect)."""
    from openmux.server import web_console as wc

    hub = _RecordingHub()
    adapter = SimpleNamespace(_site_events=hub, logger=logging.getLogger("test.site_events"))

    class _FakeServerWS:
        def __init__(self, heartbeat=None):
            self.heartbeat = heartbeat

        async def prepare(self, request):
            pass

        def __aiter__(self):
            async def _no_frames():
                return
                yield  # pragma: no cover - makes the function an async generator

            return _no_frames()

        async def close(self):
            pass

    monkeypatch.setattr(wc, "_get_adapter", lambda request: adapter)
    monkeypatch.setattr(wc.web, "WebSocketResponse", _FakeServerWS)

    ws = await wc.handle_ws_events({"username": "user"})

    assert isinstance(ws, _FakeServerWS)
    assert hub.added == [ws]
    assert hub.discarded == [ws]
    assert hub.client_count() == 0


@pytest.mark.asyncio
async def test_handle_ws_events_requires_login(monkeypatch):
    from aiohttp import web as aiohttp_web

    from openmux.server import web_console as wc

    adapter = SimpleNamespace(logger=logging.getLogger("test.site_events"))
    monkeypatch.setattr(wc, "_get_adapter", lambda request: adapter)

    with pytest.raises(aiohttp_web.HTTPUnauthorized):
        await wc.handle_ws_events({})  # no "username": request.get("username") is falsy
