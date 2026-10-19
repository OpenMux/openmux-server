"""Tests for the status page live-update path (issue #85).

Covers the three new moving parts:
- `_extract_status_fragments()` — marker-based split of the rendered markup.
- `handle_api_status_fragments()` — auth gate + fragment JSON with sort honored.
- `WebConsoleAdapter._kick_status_change()` — coalesced `status_change` site
  events for the status page (issue #85, migrated from the removed
  /ws/status socket onto the shared /ws/events hub; hub fan-out itself is
  covered by tests/server/test_site_events.py).

Style follows test_web_console_handle_ws_coverage.py: module-level `_get_adapter`
/`web.WebSocketResponse` are monkeypatched and handlers run against scripted
fakes, so no real aiohttp server is started.
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web

import openmux.server.web_console as wc
from openmux.server.web_console import (
    _extract_status_fragments,
    handle_api_status,
    handle_api_status_fragments,
)


class _FakeRequest:
    def __init__(self, *, username="tester", query=None):
        self.rel_url = SimpleNamespace(query=dict(query or {}))
        self._username = username

    def get(self, key, default=None):
        return self._username if key == "username" else default


class Log:
    def __getattr__(self, name):
        def _f(*a, **k):
            return None

        return _f


# --- _extract_status_fragments --------------------------------------------------


def test_extract_status_fragments_splits_marker_pairs():
    html = (
        "<div><!--OMX-STATUS-FRAGMENT:BEGIN:summary-->A<!--OMX-STATUS-FRAGMENT:END:summary--></div>"
        "<tbody><!--OMX-STATUS-FRAGMENT:BEGIN:ports-->rows<!--OMX-STATUS-FRAGMENT:END:ports--></tbody>"
    )
    assert _extract_status_fragments(html) == {"summary": "A", "ports": "rows"}


def test_extract_status_fragments_drops_unpaired_marker():
    html = (
        "x<!--OMX-STATUS-FRAGMENT:BEGIN:summary-->never closed"
        "<!--OMX-STATUS-FRAGMENT:BEGIN:clients-->c<!--OMX-STATUS-FRAGMENT:END:clients-->"
    )
    assert _extract_status_fragments(html) == {"clients": "c"}


def test_template_markers_match_expected_regions():
    """The shipped content template must carry exactly the marker pairs the client swaps."""
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader

    tdir = Path(__file__).resolve().parents[1] / "openmux" / "server" / "webui" / "templates" / "web_console"
    text = (tdir / "status_content.html.j2").read_text(encoding="utf-8")
    for name in wc.STATUS_FRAGMENT_NAMES:
        assert text.count(f"<!--OMX-STATUS-FRAGMENT:BEGIN:{name}-->") == 1
        assert text.count(f"<!--OMX-STATUS-FRAGMENT:END:{name}-->") == 1
    # Renderable, and the split yields all four regions with a representative context.
    env = Environment(loader=FileSystemLoader(str(tdir)))
    env.filters["fmt_ts"] = lambda v: str(v)
    html = env.get_template("status_content.html.j2").render(
        data={},
        ports=[],
        ports_by_name={},
        total_ports=0,
        connected_ports=0,
        federation={},
        multipath={},
        conn_index={},
        rport_map={},
        hb_interval=None,
        peers_cfg=[],
        base_path="",
        status_path="/status",
        sort_key="name",
        sort_dir="asc",
        sort_query="",
        motd="",
    )
    assert set(_extract_status_fragments(html)) == set(wc.STATUS_FRAGMENT_NAMES)


# --- handle_api_status (JSON snapshot) -------------------------------------------


@pytest.mark.asyncio
async def test_api_status_requires_username(monkeypatch):
    _frag_adapter(monkeypatch)
    with pytest.raises(web.HTTPUnauthorized):
        await handle_api_status(_FakeRequest(username=None))


@pytest.mark.asyncio
async def test_api_status_returns_aggregated_payload(monkeypatch):
    payload = {
        "status": {"adapter": "web_console"},
        "ports": [{"name": "p1"}],
        "federation": {"totals": {}},
        "multipath": {"totals": {}},
        "web_clients": [],
    }
    a = _frag_adapter(monkeypatch, snapshot=[{"name": "p1"}])
    monkeypatch.setattr(wc, "_assemble_status_payload", lambda adapter, preloaded_ports=None: payload)
    resp = await handle_api_status(_FakeRequest())
    assert resp.status == 200
    assert json.loads(resp.body) == payload


@pytest.mark.asyncio
async def test_api_status_error_is_500_json(monkeypatch):
    a = _frag_adapter(monkeypatch)
    a.logger = Log()

    def _boom(adapter, preloaded_ports=None):
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(wc, "_assemble_status_payload", _boom)
    resp = await handle_api_status(_FakeRequest())
    assert resp.status == 500
    assert json.loads(resp.body)["error"] is True


# --- handle_api_status_fragments -------------------------------------------------


def _frag_adapter(monkeypatch, *, rendered=None, snapshot=None, captured=None):
    a = MagicMock(name="adapter")
    a._get_ports_snapshot = MagicMock(return_value=snapshot if snapshot is not None else [])
    a._effective_base_path = MagicMock(return_value="")
    a._render_status_fragments = MagicMock(return_value=rendered if rendered is not None else {"ports": "<tr></tr>"})
    monkeypatch.setattr(wc, "_get_adapter", lambda request: a)
    return a


@pytest.mark.asyncio
async def test_fragments_endpoint_requires_username(monkeypatch):
    _frag_adapter(monkeypatch)
    with pytest.raises(web.HTTPUnauthorized):
        await handle_api_status_fragments(_FakeRequest(username=None))


@pytest.mark.asyncio
async def test_fragments_endpoint_returns_fragments_json(monkeypatch):
    rendered = {"summary": "S", "ports": "P", "federation": "F", "clients": "C"}
    _frag_adapter(monkeypatch, rendered=rendered)
    resp = await handle_api_status_fragments(_FakeRequest())
    assert resp.status == 200
    assert json.loads(resp.body) == {"fragments": rendered}


@pytest.mark.asyncio
async def test_fragments_endpoint_honors_sort_query(monkeypatch):
    ports = [
        {"name": "a", "client_count": 1},
        {"name": "b", "client_count": 9},
    ]
    captured = {}
    a = _frag_adapter(monkeypatch, snapshot=ports)
    a._render_status_fragments = MagicMock(side_effect=lambda payload: captured.update(payload) or {})
    await handle_api_status_fragments(_FakeRequest(query={"sort": "clients", "dir": "desc"}))
    assert [p["name"] for p in captured["ports"]] == ["b", "a"]
    assert captured["sort_key"] == "clients" and captured["sort_dir"] == "desc"


@pytest.mark.asyncio
async def test_fragments_endpoint_rejects_bad_sort_gracefully(monkeypatch):
    captured = {}
    a = _frag_adapter(monkeypatch, snapshot=[{"name": "a"}])
    a._render_status_fragments = MagicMock(side_effect=lambda payload: captured.update(payload) or {})
    await handle_api_status_fragments(_FakeRequest(query={"sort": "; DROP TABLE", "dir": "sideways"}))
    # Unknown keys/dirs fall back to the defaults instead of erroring.
    assert captured["sort_key"] == "name" and captured["sort_dir"] == "asc"


@pytest.mark.asyncio
async def test_fragments_endpoint_error_is_500_json(monkeypatch):
    a = _frag_adapter(monkeypatch)
    a._render_status_fragments = MagicMock(side_effect=RuntimeError("template blew up"))
    a.logger = Log()
    resp = await handle_api_status_fragments(_FakeRequest())
    assert resp.status == 500
    assert json.loads(resp.body)["error"] is True


# --- _kick_status_change coalescing (bound to a stub holder) -----------------------


class _KickStub:
    """Exercises the real kick/push methods against stub state (hub-based)."""

    _kick_status_change = wc.WebConsoleAdapter._kick_status_change
    _status_change_push_later = wc.WebConsoleAdapter._status_change_push_later

    def __init__(self, site_listeners=1):
        self.published = []
        self._site_events = SimpleNamespace(client_count=lambda: site_listeners)
        self._status_change_push_task = None
        self._status_change_last_push = 0.0
        # 0 debounce for a deterministic test; production default is 2s.
        self._status_change_min_interval = 0.0
        self.logger = logging.getLogger("kittest")

    def publish_site_event(self, event):
        self.published.append(event)


@pytest.mark.asyncio
async def test_kick_without_site_listeners_is_noop():
    s = _KickStub(site_listeners=0)
    s._kick_status_change()
    assert s._status_change_push_task is None
    assert s.published == []


@pytest.mark.asyncio
async def test_kick_coalesces_burst_into_one_event():
    s = _KickStub()
    # A burst of meta events: the pending-task guard admits exactly one push.
    for _ in range(5):
        s._kick_status_change()
    await s._status_change_push_task
    assert s.published == [{"type": "status_change"}]


@pytest.mark.asyncio
async def test_kick_after_a_completed_push_publishes_again():
    s = _KickStub()
    s._kick_status_change()
    await s._status_change_push_task
    s._kick_status_change()
    await s._status_change_push_task
    assert s.published == [{"type": "status_change"}, {"type": "status_change"}]


# --- route registration order ------------------------------------------------------


def test_routes_registered_with_expected_paths_and_ws_precedence(tmp_path):
    """Fragments endpoint exists; the dedicated /ws/status socket is gone; /ws/events precedes /ws/{port_name}."""

    async def _csrf(request):
        return web.json_response({})

    stub = SimpleNamespace(
        static_dir=str(tmp_path),
        enable_ui=False,
        enable_probes=False,
        logger=Log(),
        _handle_api_csrf=_csrf,
    )
    app = web.Application()
    wc.WebConsoleAdapter._register_routes(stub, app)
    paths = [route.resource.canonical for route in app.router.routes()]
    assert "/api/status" in paths
    assert "/api/status/fragments" in paths
    assert "/ws/status" not in paths, "the migrated socket must not come back"
    assert paths.index("/ws/events") < paths.index("/ws/{port_name}")
