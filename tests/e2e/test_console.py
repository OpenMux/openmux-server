"""CONSOLE: one signed-in browser tab against the real server (CON-01 to CON-02).

These are the first tests that drive the web console as a user does: a real Chromium tab, a real
login, and the real console page. They exist to prove two things at once. The browser tier runs, and
the page test hook (``window.__omxE2E.view()`` in ``console.js``) reports what a person sees. Every
later console scenario reads state through that hook instead of scraping the DOM.
"""

from __future__ import annotations

import pytest

from .story import step, surfaces

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_browser]


@surfaces("web:rw")
def test_con01_a_signed_in_user_opens_the_console_and_gets_their_text_back(server, console, story):
    """A read-write user opens the console on a Loopback port and sees their own text come back."""
    page = console.console("web:rw", server, "loop1", user="rw")
    with step("the page reports a live session on the port it opened"):
        view = page.wait_for_view("(v) => v.connected === true && v.mode === 'read-write'", timeout=20)
        assert view["port"] == "loop1", view["port"]
        assert view["connected_port"] == "loop1", view["connected_port"]
        assert view["port_up"] is True, view["port_up"]
    with step("the server's own connect line is on the screen"):
        assert "Connected to console" in page.text(), page.text()
    with step("text typed in the terminal comes back from the Loopback port"):
        page.send_line("browser-round-trip-42")
        page.wait_for_text("browser-round-trip-42", timeout=20)
    with step("the access decision arrived as a control frame, not as a local guess"):
        assert "client_mode:attached" in page.frame_types(), page.frame_types()
    with step("nothing is shouting on the page"):
        after = page.snapshot("typed")
        assert after["banner"]["text"] is None, after["banner"]
        assert page.body_classes() == ["console"], page.body_classes()


@surfaces("web:admin")
def test_con02_the_test_hook_stays_off_unless_the_url_asks_for_it(server, console, story):
    """The page exports its test hook only when the URL carries ``?e2e=1``.

    A read-only hook is still new surface on a security-relevant page, so it must not exist in a
    normal session. This test fails if a future edit exports it unconditionally.
    """
    page = console.user("web:admin", server, user="admin")
    with step("a normal console URL exports nothing"):
        page.goto("/console")
        assert page.page.evaluate("() => typeof window.__omxE2E") == "undefined"
    with step("the same page with '?e2e=1' exports the hook"):
        page.open(e2e=True)
        assert page.page.evaluate("() => typeof window.__omxE2E") == "object"
        assert page.view()["mode"] in ("read-write", "read-only"), page.view()["mode"]
