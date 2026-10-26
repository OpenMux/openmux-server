"""Tests for versioned static asset URLs in the web console pages (issue #98 follow-up).

A browser keeps a cached script until its URL changes. The web console scripts carry
behaviour, not only looks, so an upgrade must not leave a page running the previous
release's script. These tests check the rendered page, because a link written outside a
Jinja expression still looks right in the template source.
"""

from __future__ import annotations

import re
from pathlib import Path

from openmux.server.web_console import WebConsoleAdapter

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "openmux" / "server" / "webui" / "templates" / "web_console"
STAMP = "9.9.9"

# The helper name outside a Jinja expression reaches the browser as literal text.
LEAKED_HELPER = re.compile(r"""["'>]\s*asset_url\(""")
# A behaviour-bearing link that keeps the old cache forever.
UNWRAPPED_LINK = re.compile(r"""(?:src|href)="\{\{ *base_path *\}\}/[^"]+\.(?:js|css)""")


def _adapter() -> WebConsoleAdapter:
    """A web console adapter with its real Jinja environment and a fixed stamp."""
    adapter = WebConsoleAdapter("wc", {"web_console": {}})
    adapter._prepare_templates()
    assert adapter._jinja_env is not None, "the shipped template dir must build a Jinja env"
    adapter._asset_stamp_cache = STAMP
    return adapter


def test_rendered_login_page_requests_its_assets_by_version():
    """The HTML a browser receives carries `?v=<version>` on each script and stylesheet."""
    html = _adapter()._render_login().decode("utf-8")
    assert f"/static/js/login.js?v={STAMP}" in html, html[:400]
    assert f"/static/js/theme-init.js?v={STAMP}" in html
    assert f"/static/web_console.css?v={STAMP}" in html


def test_rendered_page_never_leaks_the_asset_helper_as_text():
    """`asset_url` without `{{ }}` renders as literal text and breaks the page."""
    html = _adapter()._render_login().decode("utf-8")
    assert not LEAKED_HELPER.search(html), html[:400]


def test_rendered_console_page_requests_the_terminal_assets_by_version():
    """The console page carries the stamp on xterm and on its own script.

    This page holds the Write slot controls, so a stale cached script here is the case
    that hides an access answer.
    """
    html = _adapter()._render_console().decode("utf-8")
    assert f"/static/js/console.js?v={STAMP}" in html, html[:400]
    assert f"/static/xterm/lib/xterm.js?v={STAMP}" in html
    assert not LEAKED_HELPER.search(html), html[:400]


def test_every_page_template_compiles():
    """A broken Jinja expression raises at render time, not at edit time."""
    adapter = _adapter()
    compiled = 0
    for template in sorted(TEMPLATE_DIR.glob("*.j2")):
        adapter._jinja_env.get_template(template.name)
        compiled += 1
    assert compiled >= 6, f"expected the shipped pages to compile, got {compiled}"


def test_asset_url_appends_the_stamp_to_a_bare_path():
    """`asset_url` in the shipped templates appends this version to the path."""
    adapter = _adapter()
    rendered = (
        adapter._jinja_env.from_string("{{ asset_url(base_path ~ '/static/js/console.js') }}")
        .render(base_path="/openmux")
        .strip()
    )
    assert rendered == "/openmux/static/js/console.js?v=9.9.9"


def test_asset_url_keeps_an_existing_query_string():
    """A path that already carries a query gets `&v=`, never a second `?`."""
    adapter = _adapter()
    rendered = adapter._jinja_env.from_string("{{ asset_url('/static/js/x.js?theme=dark') }}").render().strip()
    # Autoescape writes the joiner as `&amp;`, the correct form inside an HTML attribute.
    assert rendered == "/static/js/x.js?theme=dark&amp;v=9.9.9"


def test_asset_url_is_plain_path_when_version_is_unknown():
    """Without a version, the link stays as written instead of carrying a blank stamp."""
    adapter = _adapter()
    adapter._asset_stamp_cache = ""
    rendered = adapter._jinja_env.from_string("{{ asset_url('/static/js/x.js') }}").render().strip()
    assert rendered == "/static/js/x.js"


def test_every_script_and_style_link_carries_the_stamp():
    """No page may link a script or stylesheet straight from `base_path`."""
    unwrapped = []
    leaked = []
    for template in sorted(TEMPLATE_DIR.glob("*.j2")):
        text = template.read_text(encoding="utf-8")
        for line in text.splitlines():
            if UNWRAPPED_LINK.search(line):
                unwrapped.append(f"{template.name}: {line.strip()[:90]}")
            if LEAKED_HELPER.search(line) or ("asset_url(" in line and "{{ asset_url(" not in line):
                leaked.append(f"{template.name}: {line.strip()[:90]}")
    assert not unwrapped, f"links a behaviour-bearing asset without asset_url(): {unwrapped}"
    assert not leaked, f"asset_url() written outside a Jinja expression: {leaked}"
