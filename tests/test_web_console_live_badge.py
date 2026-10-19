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
        # Outlet changes AND PDU-set reconcile changes both re-render the rows.
        for topic in ("power_changed", "pdus_changed"):
            assert topic in page, f"{label}: must react to the {topic} nudge"
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


def test_layout_js_rebuilds_power_menu_from_pdus_changed_nudge():
    """Power sidebar menu rebuilds on the pdus_changed site nudge (topology).

    Mirror of the console-list contract for #power-pdus (server-rendered
    once per page load): the soft-reload reconcile publishes a coalesced
    pdus_changed frame; this view rebuilds the links from /api/power.
    """
    start = LAYOUT_JS.index("Power menu freshness")
    block = LAYOUT_JS[start:]
    assert "addEventListener('omx-site-event'" in block
    assert "'pdus_changed'" in block
    assert "/api/power" in block
    assert 'id="power-pdus"' in block or "'power-pdus'" in block
    assert "data-pdu-name" in block
    # The empty state must match the server template's text.
    assert "No PDUs configured" in block
    # One socket per tab still holds: no page-owned WebSocket here either.
    assert "new WebSocket(" not in block


def test_console_js_refreshes_header_without_touching_the_session():
    """The console header, badge, and Actions button follow the live port set.

    All three cache a decision from page load, so a removal, an in-place feed
    edit, or the first action grant was invisible until reload. console.js
    re-reads on the ports_changed / reload_applied nudges (and on site-socket
    RE-open) and updates rendered text and visibility only: reloading the page,
    or closing the port WebSocket, would interrupt the operator's live session.
    """
    start = CONSOLE_JS.index("Port-set freshness")
    end = CONSOLE_JS.index("Info overlay helpers")
    block = CONSOLE_JS[start:end]
    assert "addEventListener('omx-site-event'" in block
    assert "addEventListener('omx-site-event-open'" in block
    assert "'ports_changed'" in block
    # reload_applied carries what no other frame reports: a port_actions grant
    # and a port's in-place live fields (description, groups, power feeds).
    assert "'reload_applied'" in block
    # Reuses the page's existing loader; opens no socket of its own.
    assert "loadPorts()" in block
    assert "new WebSocket(" not in block
    # The RE-open refresh must skip the first open (page just rendered).
    assert "siteSocketOpenedOnce" in block
    # A failed read must keep the previous rendering instead of blanking it.
    assert "portListReadLive = true" in block
    # "(removed)" is gated on that live-read proof, so a first-paint unknown port
    # still renders plain.
    assert "' (removed)'" in CONSOLE_JS
    assert "portListReadLive && !p" in CONSOLE_JS
    # The power badge and the Actions button re-derive from the same reads.
    assert "updatePowerBadge(" in block
    assert "/actions" in block
    # Revealing the Actions button must not run the catalog loader, which
    # auto-joins a running action and can open the run panel.
    assert "loadActionsCatalog(" not in block
    # Hiding it closes an open Port Actions panel: a list of actions this port can
    # no longer run is not worth keeping on screen. Reveal must not open it, which
    # the assertion above already guarantees.
    assert "if (!has) closeActionsOverlay();" in block
    # Hard contract for the whole page script: no reload, no session teardown.
    assert "location.reload" not in CONSOLE_JS
    assert "location.replace" not in CONSOLE_JS
    assert "ws.close(" not in block
