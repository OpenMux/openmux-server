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


def test_power_pages_call_helper_from_socket_lifecycle():
    for page, label in ((POWER_LIST, "power_list"), (POWER_DETAIL, "power_detail")):
        assert "window.omxSetLive(true)" in page, f"{label}: onopen must set green"
        assert "window.omxSetLive(false)" in page, f"{label}: onclose must set red state"
        # Opt-in runs before the top-level connect() call (a socket that never
        # opens must still turn the badge red), not only from onclose.
        opt_in = page.index("if (window.omxSetLive) window.omxSetLive(false);")
        top_level_connect = re.search(r"\n\s*connect\(\);", page)
        assert top_level_connect, f"{label}: no top-level connect() call found"
        assert opt_in < top_level_connect.start(), f"{label}: must opt in before connecting"


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
