"""Tests for the /about route and the About page data helpers."""

import base64
from pathlib import Path

import pytest
from aiohttp import ClientSession, TCPConnector

from openmux import __version__
from openmux.server.auth_manager import AuthManager
from openmux.server.console_manager import ConsoleManager
from openmux.server.port_manager import PortManager
from openmux.server.web_console import WebConsoleAdapter, _format_uptime, _read_hardware_info

_USER_HASH = "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"  # md5("password")
_AUTH = {"Authorization": f"Basic {base64.b64encode(b'u:password').decode()}"}


def _make_adapter(port, **cfg):
    config = {
        "web_console": {
            "host": "127.0.0.1",
            "port": port,
            "enable_ui": True,
            **cfg,
        }
    }
    adapter = WebConsoleAdapter("wc", config)
    auth = AuthManager({"users": [{"username": "u", "password_hash": _USER_HASH}]})
    pm = PortManager([])
    cm = ConsoleManager(pm, auth)
    adapter.set_auth_manager(auth)
    adapter.set_console_manager(cm)
    return adapter


def test_format_uptime_values():
    assert _format_uptime(None) == ""
    assert _format_uptime(5) == "5s"
    assert _format_uptime(90) == "1m 30s"
    assert _format_uptime(3725) == "1h 2m"
    assert _format_uptime(93784) == "1d 2h"


def test_read_hardware_info_parses_fields(tmp_path):
    path = tmp_path / "openmux-hardware"
    path.write_text(
        "# comment line\n"
        'OPENMUX_MANUFACTURER="FTDI Ltd."\n'
        'OPENMUX_PRODUCT="Basic RS232-HS"\n'
        'OPENMUX_SERIAL="OMH123"\n'
    )
    info = _read_hardware_info(str(path))
    assert info == {"manufacturer": "FTDI Ltd.", "product": "Basic RS232-HS", "serial": "OMH123"}


def test_read_hardware_info_missing_returns_empty():
    assert _read_hardware_info(str(Path("/nonexistent/openmux-hardware"))) == {}
    assert _read_hardware_info(None) == {}


@pytest.mark.asyncio
async def test_about_page_shows_server_version():
    adapter = _make_adapter(8911)
    assert await adapter.start()
    try:
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.get("http://127.0.0.1:8911/about", headers=_AUTH) as resp:
                assert resp.status == 200
                html = await resp.text()
                assert "OpenMux About" in html
                assert f"v{__version__}" in html
            # Unauthenticated requests are redirected to login.
            async with session.get("http://127.0.0.1:8911/about", allow_redirects=False) as resp:
                assert resp.status in (301, 302, 307, 308)
                assert "/login" in resp.headers.get("Location", "")
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_about_page_shows_hardware_info(tmp_path):
    hw = tmp_path / "hw"
    hw.write_text(
        'OPENMUX_MANUFACTURER="FTDI Ltd."\n'
        'OPENMUX_PRODUCT="Basic RS232-HS"\n'
        'OPENMUX_SERIAL="OMH123"\n'
    )
    adapter = _make_adapter(8912, hardware_info_file=str(hw))
    assert await adapter.start()
    try:
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.get("http://127.0.0.1:8912/about", headers=_AUTH) as resp:
                assert resp.status == 200
                html = await resp.text()
                assert "Hardware" in html
                assert "FTDI Ltd." in html
                assert "Basic RS232-HS" in html
                assert "OMH123" in html
    finally:
        await adapter.stop()
