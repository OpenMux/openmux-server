"""Contract tests for the "Live data" header badge (issue #86).

The badge element lives once in `layout.html.j2` (hidden by default); pages
with a page-level WebSocket opt in through the shared helper
`window.omxSetLive()` in `layout.js`. These tests pin the static contract:
which files call the helper, which must never, and the helper's three states.
"""

import re
from pathlib import Path

WEBUI = Path(__file__).resolve().parents[1] / "openmux" / "server" / "webui"
JS = WEBUI / "static" / "js"
TPL = WEBUI / "templates" / "web_console"

LAYOUT = (TPL / "layout.html.j2").read_text(encoding="utf-8")
LAYOUT_JS = (JS / "layout.js").read_text(encoding="utf-8")
STATUS_JS = (JS / "status.js").read_text(encoding="utf-8")
CONSOLE_JS = (JS / "console.js").read_text(encoding="utf-8")
POWER_LIST = (TPL / "power_list.html.j2").read_text(encoding="utf-8")
POWER_DETAIL = (TPL / "power_detail.html.j2").read_text(encoding="utf-8")
STATUS_TPL = (TPL / "status.html.j2").read_text(encoding="utf-8")


def test_layout_defines_shared_badge_hidden_by_default():
    assert 'id="liveDataBadge"' in LAYOUT
    m = re.search(r'id="liveDataBadge"[^>]*', LAYOUT)
    assert "display:none" in m.group(0), "badge must start hidden until a page opts in"


def test_layout_js_helper_has_two_states():
    assert "window.omxSetLive = function (open)" in LAYOUT_JS
    # green while open, debounced red while closed:
    assert "apply(GREEN)" in LAYOUT_JS
    assert "apply(RED)" in LAYOUT_JS
    assert "clearTimeout(redTimer)" in LAYOUT_JS
    # The debounce must be pending-timer based (fast reconnect never flickers red).
    assert re.search(r"setTimeout\(\(\) => \{ redTimer = null; apply\(RED\); \}, RED_DEBOUNCE_MS\)", LAYOUT_JS)


def test_power_pages_drive_helper_from_site_events():
    """Migrated off the dedicated /ws/power socket: the pages own NO socket.

    The shared /ws/events socket in layout.js drives them via the
    omx-site-event / -open / -close signals, same contract as status.js.
    """
    for page, label in ((POWER_LIST, "power_list"), (POWER_DETAIL, "power_detail")):
        assert "window.omxSetLive(true)" in page, f"{label}: site-open must set green"
        assert "window.omxSetLive(false)" in page, f"{label}: site-close must set red state"
        assert "new WebSocket(" not in page, f"{label}: page must not open a socket"
        assert '"/ws/' not in page and "'/ws/" not in page, f"{label}: no server ws path"
        for event in ('"omx-site-event"', '"omx-site-event-open"', '"omx-site-event-close"'):
            assert f"addEventListener({event}" in page, f"{label}: must listen for {event}"
        assert "power_changed" in page, f"{label}: must react to the power_changed nudge"
        # Initial badge state honors the site socket's current state.
        assert "window.omxSiteLive" in page, f"{label}: must seed from omxSiteLive"


def test_status_page_uses_shared_helper_and_has_no_toggle():
    assert "window.omxSetLive" in STATUS_JS
    assert "statusLiveState" not in STATUS_JS
    assert "statusLiveState" not in STATUS_TPL, "the #85 page-local badge must be gone"
    # The page is always live (issue #86 follow-up): no Auto Refresh button,
    # no per-browser pref.
    assert "autoRefreshToggle" not in STATUS_JS
    assert "autoRefreshToggle" not in STATUS_TPL
    assert "localStorage.setItem" not in STATUS_JS, "the Auto Refresh pref must be gone"
    # Migrated off the dedicated status socket: the page owns NO socket.
    # The shared /ws/events socket in layout.js drives it via site events.
    assert "new WebSocket(" not in STATUS_JS
    assert "'/ws/" not in STATUS_JS, "status.js must not open a server socket"
    for event in ("'omx-site-event'", "'omx-site-event-open'", "'omx-site-event-close'"):
        assert f"addEventListener({event}" in STATUS_JS, f"must listen for {event}"
    assert "status_change" in STATUS_JS, "must react to the status_change nudge frame"
    # Badge contract: red opt-in unless the site socket is already open at
    # script init (a socket that never opens turns the badge red after the debounce).
    assert "window.omxSiteLive" in STATUS_JS
    assert "setLive(true)" in STATUS_JS
    assert "setLive(false)" in STATUS_JS


def test_terminal_pages_never_call_the_helper():
    assert "omxSetLive" not in CONSOLE_JS
    for name in ("console.html.j2",):
        text = (TPL / name).read_text(encoding="utf-8")
        assert "omxSetLive" not in text


def test_layout_js_rebuilds_sidebar_from_ports_changed_nudge():
    """Sidebar console list rebuilds on the ports_changed site nudge.

    A port added/removed after load is otherwise invisible until reload; the
    shared /ws/events socket (owned by layout.js) makes #console-ports follow
    the live set. This pins the static contract of that rebuild block.
    """
    # Pull just the sidebar-refresh IIFE (from its comment to the file end).
    start = LAYOUT_JS.index("Sidebar console-list freshness")
    block = LAYOUT_JS[start:]
    # It consumes exactly the ports_changed frame and reuses /api/ports.
    assert "'ports_changed'" in block
    assert "/api/ports" in block
    assert "addEventListener('omx-site-event'" in block
    # The rebuild carries the label data applyPortLabels()/sorting need, and
    # re-captures the natural order so prefs and dots survive.
    assert "data-origin" in block
    assert "data-desc" in block
    assert "window.setPortOrder" in block
    assert "window.applyPortLabels" in block
    # No page-owned socket here either; it rides the layout site socket.
    assert "new WebSocket(" not in block
