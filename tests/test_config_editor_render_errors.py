"""Regression tests for `GET /config-editor` error pages (issue #80).

The browser-landing route used to degrade to a raw JSON dump of the server
config on any template failure. Operators could not tell a broken install
(no Jinja2 engine) from a broken template. Both failure modes now raise the
house 500 text page, matching the rest of the web console (`web_console.py`
ends every page render in `HTTPInternalServerError` with a plain-text body),
and `GET /config-editor/data` keeps serving JSON in all cases.

The handlers are called directly with a fake request: no socket is bound,
so the tests run in a restricted sandbox.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from jinja2 import DictLoader, Environment, FileSystemLoader, StrictUndefined

from openmux.server.web_plugins import ADAPTER_APP_KEY
from openmux.server.web_plugins.config_editor import _handle_data, _handle_view

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = REPO_ROOT / "openmux" / "server" / "webui" / "templates" / "web_console"


class _FakeAdapter:
    """Minimal stand-in exposing exactly what the handlers touch."""

    realm = "test"

    def __init__(self, username: str, jinja_env=None, config: dict = None):
        self.username = username
        if jinja_env is not None:
            self._jinja_env = jinja_env
        self.logger = MagicMock()
        if config is not None:
            cm = MagicMock()
            cm.config = config
            cm.config_path = None
            policy = MagicMock()
            policy.get_writable_sections.return_value = []
            policy.get_access_default.return_value = "allow"
            cm.get_security_policy.return_value = policy
            server = MagicMock()
            server.config_manager = cm
            console_manager = MagicMock()
            console_manager.server = server
            self.console_manager = console_manager

    def _require_permission(self, request, permissions):
        if self.username != "admin":
            raise web.HTTPForbidden(text="forbidden")

    def _get_effective_permission(self, username, request):
        return "admin"

    def _get_allowed_plugin_nav(self, username, request=None):
        return []

    def _get_ports_snapshot(self):
        return []

    def _effective_base_path(self, request):
        return ""


def _make_request(adapter: _FakeAdapter):
    """Fake aiohttp request with the identity middleware already resolved."""
    request = MagicMock()
    request.app = {ADAPTER_APP_KEY: adapter}
    request.get = MagicMock(side_effect=lambda key, default=None: adapter.username if key == "username" else default)
    request.query = MagicMock()
    request.query.get = MagicMock(side_effect=lambda key, default=None: None)
    request.remote = "127.0.0.1"
    return request


async def _call_view(username: str, jinja_env=None, config: dict = None) -> web.StreamResponse:
    adapter = _FakeAdapter(username, jinja_env=jinja_env, config=config)
    return await _handle_view(_make_request(adapter))


async def _call_data(username: str, config: dict = None) -> web.StreamResponse:
    adapter = _FakeAdapter(username, config=config)
    return await _handle_data(_make_request(adapter))


def _real_jinja_env() -> Environment:
    return Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))


def _broken_jinja_env() -> Environment:
    """Engine present but rendering fails: a template that references a
    variable that is never passed in. This mirrors the real failure shape
    (broken template / missing helper) instead of a loader exception."""
    loader = DictLoader({"config_editor.html.j2": "<html>{{ oops_undefined }}</html>"})
    return Environment(loader=loader, undefined=StrictUndefined)


async def _logged_errors(adapter: _FakeAdapter):
    """Return the message argument of every adapter.logger.error() call."""
    messages = []
    for call in adapter.logger.error.call_args_list:
        messages.append(call.args[0] if call.args else "")
    return messages


@pytest.mark.asyncio
async def test_view_no_engine_returns_500_text_page_and_logs():
    """Mode 1: `_jinja_env` unset (jinja2 missing or template dir gone).

    The route must return a 500 text page naming the broken install, log the
    "template engine not initialized" error, and never serve a JSON body.
    """
    adapter = _FakeAdapter("admin")  # _jinja_env attribute absent
    request = _make_request(adapter)
    with pytest.raises(web.HTTPInternalServerError) as exc_info:
        await _handle_view(request)
    body = exc_info.value.text
    assert exc_info.value.status == 500
    assert "Config Editor is unavailable" in body
    assert "templates are missing" in body
    assert "application/json" not in exc_info.value.content_type
    errors = adapter.logger.error.call_args_list
    assert errors, "expected the engine-not-initialized error to be logged"
    assert any("template engine not initialized" in str(c) for c in errors)
    # And no warning-level JSON-fallback message (the old path):
    assert not adapter.logger.warning.call_args_list


@pytest.mark.asyncio
async def test_view_render_failure_returns_500_text_page_with_traceback():
    """Mode 2: engine present but the render raises.

    The route must return a DIFFERENT 500 text page pointing at the broken
    template and log the failure with a full traceback (exc_info).
    """
    adapter = _FakeAdapter("admin", jinja_env=_broken_jinja_env())
    request = _make_request(adapter)
    with pytest.raises(web.HTTPInternalServerError) as exc_info:
        await _handle_view(request)
    body = exc_info.value.text
    assert exc_info.value.status == 500
    assert "Failed to render Config Editor" in body
    # Distinct from the engine-missing page:
    assert "templates are missing" not in body
    errors = [c for c in adapter.logger.error.call_args_list if "Config Editor HTML render failed" in str(c)]
    assert errors, "expected the render-failed error to be logged"
    # The traceback is in the log: exc_info must be enabled on that call.
    assert any(c.kwargs.get("exc_info") is True for c in errors)


@pytest.mark.asyncio
async def test_view_engine_none_explicit_is_mode_1():
    """`_jinja_env` set to None (explicit) behaves like the attribute missing."""
    adapter = _FakeAdapter("admin")
    adapter._jinja_env = None  # attribute present, value falsy
    request = _make_request(adapter)
    with pytest.raises(web.HTTPInternalServerError) as exc_info:
        await _handle_view(request)
    assert "Config Editor is unavailable" in exc_info.value.text
    assert any("template engine not initialized" in str(c) for c in adapter.logger.error.call_args_list)


@pytest.mark.asyncio
async def test_view_happy_path_unchanged():
    """With a healthy engine the browser still gets the HTML page."""
    config = {
        "server": {"id": "test"},
        "logging": {"level": "WARNING"},
        "loopback_ports": [{"name": "lb1"}],
    }
    adapter = _FakeAdapter("admin", jinja_env=_real_jinja_env(), config=config)
    response = await _handle_view(_make_request(adapter))
    assert isinstance(response, web.Response)
    assert response.status == 200
    assert response.content_type == "text/html"
    body = response.body.decode("utf-8")
    assert "OpenMux Config Editor" in body
    # The PDU power view and its driver catalog render into the bootstrap.
    # The driver list is the single source of truth for the driver select,
    # so a new driver added in pdu.py shows up here without a template edit.
    assert 'id="view-power"' in body
    assert "powerDrivers" in body
    assert '"driver": "dummy"' in body
    assert "options_keys" in body
    # No error logging on the happy path
    assert not adapter.logger.error.call_args_list


@pytest.mark.asyncio
async def test_data_route_still_serves_json_with_real_config():
    """`GET /config-editor/data` keeps serving JSON, even in the same states
    where the browser page now answers a 500 text page."""
    config = {
        "server": {"id": "test"},
        "authentication": {"users": [{"username": "admin", "password_hash": "ab" * 32, "permissions": "admin"}]},
    }
    response = await _call_data("admin", config=config)
    assert response.status == 200
    assert response.content_type == "application/json"
    payload = json.loads(response.body.decode("utf-8"))
    # Secret masking is intact (the data route is the reason it must stay JSON)
    assert payload["config"]["authentication"]["users"][0]["password_hash"] == "********"
    assert "writable_sections" in payload


@pytest.mark.asyncio
async def test_view_requires_admin_as_before():
    """Permission enforcement is untouched: non-admin still 403s."""
    adapter = _FakeAdapter("user", jinja_env=_real_jinja_env())
    with pytest.raises(web.HTTPForbidden):
        await _handle_view(_make_request(adapter))
