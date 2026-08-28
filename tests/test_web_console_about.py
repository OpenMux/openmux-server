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


def test_about_version_split(monkeypatch):
    import openmux.server.web_console as wc

    adapter = _make_adapter(0)
    monkeypatch.setattr(wc, "_get_dist_version", lambda: "9.8.7.post3+gabc1234.d20260101")
    info = wc._about_server_info(adapter, ports_snapshot=[])
    assert info["version"] == "9.8.7.post3+gabc1234.d20260101"
    assert info["version_base"] == "9.8.7.post3"

    # Without a local segment the base equals the full string
    monkeypatch.setattr(wc, "_get_dist_version", lambda: "1.0.1")
    info = wc._about_server_info(adapter, ports_snapshot=[])
    assert info["version_base"] == "1.0.1"


def test_login_page_shows_server_version(monkeypatch):
    import openmux.server.web_console as wc
    from jinja2 import Environment, FileSystemLoader

    adapter = _make_adapter(0)
    # tests/ -> repo root, so templates live at <repo>/templates/web_console
    tdir = Path(__file__).resolve().parents[1] / "templates" / "web_console"
    adapter._jinja_env = Environment(loader=FileSystemLoader(str(tdir)))

    monkeypatch.setattr(wc, "_get_dist_version", lambda: "9.8.7.post3+gabc1234.d20260101")
    html = adapter._render_login().decode()
    assert "OpenMux v9.8.7.post3+gabc1234.d20260101" in html

    # A plain release tag shows just the tag version
    monkeypatch.setattr(wc, "_get_dist_version", lambda: "1.0.1")
    html = adapter._render_login().decode()
    assert "OpenMux v1.0.1" in html


def test_login_page_shows_motd():
    from jinja2 import Environment, FileSystemLoader

    tdir = Path(__file__).resolve().parents[1] / "templates" / "web_console"
    motd = "Planned maintenance\nSaturday 22:00-02:00"

    adapter = _make_adapter(0, motd=motd)
    adapter._jinja_env = Environment(loader=FileSystemLoader(str(tdir)))
    html = adapter._render_login().decode()
    assert "login-motd" in html
    assert "Planned maintenance" in html
    assert "Saturday 22:00-02:00" in html

    # No motd in config -> no MOTD block at all
    adapter_no_motd = _make_adapter(0)
    adapter_no_motd._jinja_env = Environment(loader=FileSystemLoader(str(tdir)))
    html = adapter_no_motd._render_login().decode()
    assert "login-motd" not in html

    # Blank motd -> hidden (same as unset)
    adapter_blank = _make_adapter(0, motd="   \n  ")
    adapter_blank._jinja_env = Environment(loader=FileSystemLoader(str(tdir)))
    html = adapter_blank._render_login().decode()
    assert "login-motd" not in html
    assert adapter_blank.motd == ""


def test_login_page_never_shows_logged_in_motd():
    """The logged-in MOTD may hold sensitive text; it must not leak pre-auth."""
    from jinja2 import Environment, FileSystemLoader

    tdir = Path(__file__).resolve().parents[1] / "templates" / "web_console"
    li_motd = "Internal detail: rack B42, PSU 2 failing"

    adapter = _make_adapter(0, logged_in_motd=li_motd)
    adapter._jinja_env = Environment(loader=FileSystemLoader(str(tdir)))
    html = adapter._render_login().decode()
    assert "login-motd" not in html
    assert "rack B42" not in html

    # logged_in_motd renders at the top of the status page
    status = adapter._jinja_env.get_template("status.html.j2")
    html = status.render(
        base_path="", realm="R", user_permission="admin", plugin_nav=[], ports=[],
        motd=adapter.logged_in_motd, total_ports=0, connected_ports=0,
        federation={}, multipath={}, ports_by_name={}, data={},
        sort_key="name", sort_dir="asc",
        sort_query="", status_path="/", server_version="", server_uptime="",
    )
    assert "status-motd" in html
    assert "rack B42" in html
    # and no longer appears in the sidebar (layout) or the Ctrl+E menu (console)
    layout = adapter._jinja_env.get_template("layout.html.j2")
    html = layout.render(base_path="", realm="R", user_permission="admin",
                         plugin_nav=[], ports=[], motd=adapter.logged_in_motd)
    assert "sidebar-motd" not in html


def test_logged_in_motd_blank_and_unset():
    adapter = _make_adapter(0)
    assert adapter.motd == ""
    assert adapter.logged_in_motd == ""
    adapter_blank = _make_adapter(0, logged_in_motd="  \n ")
    assert adapter_blank.logged_in_motd == ""
    adapter_str = _make_adapter(0, logged_in_motd=42)
    assert adapter_str.logged_in_motd == "42"


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
