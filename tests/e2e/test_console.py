"""CONSOLE: one signed-in browser tab against the real server (CON-01 to CON-14).

These are the tests that drive the web console as a user does: a real Chromium tab, a real login, and
the real console page. They exist to prove two things at once. The browser tier runs, and the page
test hook (``window.__omxE2E.view()`` in ``console.js``) reports what a person sees. Every console
scenario reads state through that hook instead of scraping the DOM.

Two rules come from how the page is built, and both are easy to get wrong. Status regions are
replaced with ``innerHTML``, so a state assertion goes through ``view()`` and never through a cached
element. And several regions exist from the first paint but are hidden by an inline ``display``: the
banner holds a stale error sentence on a healthy page, so a scenario asserts on visibility, never on
the presence of a node.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
import yaml

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


def _sidebar_hrefs(page: Any) -> List[str]:
    """The ``href`` of every port row in the sidebar's console group, in the order shown.

    The row's own text is not a contract: ``applyPortLabels()`` rewrites it from two localStorage
    switches, so it can read ``server::port``, ``port (description)``, or ``port``. The link target is
    what opens that console, so the scenario asserts on it.
    """
    return list(
        page.page.eval_on_selector_all("#console-ports a.nav-sub-item", "els => els.map((e) => e.getAttribute('href'))")
    )


@surfaces("web:admin")
def test_con03_an_empty_console_page_says_so_and_its_button_asks_for_a_port(server, console, story):
    """A console page opened with no port names itself empty instead of showing a dead terminal."""
    page = console.user("web:admin", server, user="admin")
    alerts: List[str] = page.accept_dialogs()
    page.open(e2e=True)
    with step("the header states that no console is selected"):
        assert page.text_of("#portDisplayName") == "No console selected", page.text_of("#portDisplayName")
        assert page.text_of("#portDisplayDesc") == "", page.text_of("#portDisplayDesc")
    with step("the terminal explains what to do next"):
        page.wait_for_text("Select a Console from the left menu and click Connect", timeout=10)
        assert "Welcome to the OpenMux Web Console" in page.text(), page.text()
    with step("nothing is connected, so no terminal state is claimed"):
        view = page.snapshot("empty")
        assert view["connected"] is False, view
        assert view["banner"]["text"] is None, view["banner"]
    with step("Connect with no port asks for one and opens no socket"):
        page.connect()
        page.wait_until(lambda: alerts == ["Select a console"], timeout=5, what="the page to ask for a console")
        assert page.view()["connected"] is False, page.view()


@surfaces("web:rw")
def test_con04_the_sidebar_lists_each_port_by_the_link_that_opens_it(server, servers, console, story):
    """The sidebar's console group lists one row per port, marks the one the page shows, and empties with the server."""
    page = console.console("web:rw", server, "loop1", user="rw")
    page.wait_for_view("(v) => v.connected === true", timeout=20)
    with step("the group stays folded until the reader opens it"):
        assert page.page.is_hidden("#console-ports"), "the port group must start folded"
        page.click("#console-expand-btn")
        page.page.wait_for_selector("#console-ports a.nav-sub-item", timeout=10000)
    with step("each port is reachable by the link that opens it"):
        hrefs = _sidebar_hrefs(page)
        assert any(href.endswith("/console?port=loop1") for href in hrefs), hrefs
        assert any(href.endswith("/console?port=cat1") for href in hrefs), hrefs
    with step("the port this page shows is the one marked as current"):
        current = page.page.eval_on_selector_all(
            "#console-ports a.nav-sub-item.active", "els => els.map((e) => e.getAttribute('href'))"
        )
        assert len(current) == 1, current
        assert str(current[0]).endswith("/console?port=loop1"), current
    with step("a server with no ports says so in the same place"):
        portless = servers(name="no-ports", loopback=[], command_ports=[], start=True)
        visitor = console.user("web:none", portless, user="admin")
        visitor.open(e2e=True)
        visitor.click("#console-expand-btn")
        visitor.page.wait_for_selector("#console-ports .nav-sub-empty", timeout=10000)
        assert visitor.text_of("#console-ports .nav-sub-empty") == "No ports", visitor.text_of("#console-ports .nav-sub-empty")
        assert _sidebar_hrefs(visitor) == [], _sidebar_hrefs(visitor)


@surfaces("web:rw")
def test_con05_the_connect_button_says_what_it_will_do_next(server, console, story):
    """The one button on the page is named for its next action: Disconnect once attached, Reconnect after a stop."""
    page = console.console("web:rw", server, "loop1", user="rw")
    with step("a page opened with a port connects on its own, and the button offers to stop it"):
        page.wait_for_view("(v) => v.connected === true", timeout=20)
        assert page.text_of("#connect") == "Disconnect", page.text_of("#connect")
    with step("pressing it leaves the port, and the terminal says so in the server's wording"):
        page.click("#connect")
        page.wait_for_text("Disconnected from local::loop1", timeout=10)
        page.wait_for_view("(v) => v.connected === false", timeout=10)
    with step("the same button now offers to go back"):
        page.page.wait_for_function("() => document.getElementById('connect').textContent === 'Reconnect'", timeout=10000)
    with step("pressing it again returns to the port"):
        page.click("#connect")
        page.wait_for_view("(v) => v.connected === true", timeout=20)
        assert page.text_of("#connect") == "Disconnect", page.text_of("#connect")


@surfaces("web:rw", "cli:ro")
def test_con06_a_second_client_on_the_port_shows_itself_to_the_first(server, console, clients, story):
    """The viewer badge counts the other clients on the port, and forgets them when they leave."""
    page = console.console("web:rw", server, "loop1", user="rw")
    # The hook reports the badge as the text it shows, so the count compares as a string.
    page.wait_for_view("(v) => v.connected === true && v.viewers === '0'", timeout=20)
    with step("one reader alone shows no badge, because the count is about the others"):
        assert page.view()["viewers"] == "0", page.view()
        assert page.page.is_hidden("#viewersBadgeWrap"), "a lone viewer must not be shown a badge"
    with step("a second client arrives over the CLI listener and appears on the badge"):
        viewer = clients.cli(user="ro", attach_to="loop1", surface="cli:viewer")
        counted = page.wait_for_view("(v) => v.viewers === '1'", timeout=20)
        assert page.text_of("#viewersCount") == "1", counted
        assert page.page.get_attribute("#viewersBadge", "title") == "1 other viewer", page.page.get_attribute(
            "#viewersBadge", "title"
        )
    with step("opening the badge names that client and the mode it holds"):
        assert page.page.is_hidden("#viewersMenu"), "the viewer list must stay shut until the badge is clicked"
        page.click("#viewersBadge")
        page.wait_until(lambda: not page.page.is_hidden("#viewersMenu"), timeout=10, what="the viewer list to open")
        rows = page.menu_text("viewers")
        assert any(row.startswith("ro@") and "read-only" in row for row in rows), rows
        # The list holds every holder, so the reader's own row is there too, marked as such.
        assert any("(me)" in row and "read-write" in row for row in rows), rows
    with step("when it leaves, the badge goes away rather than reading zero"):
        viewer.close()
        released = page.wait_for_view("(v) => v.viewers === '0'", timeout=20)
        assert released["viewers"] == "0", released
        assert page.page.is_hidden("#viewersBadgeWrap"), page.page.get_attribute("#viewersBadgeWrap", "style")


@surfaces("web:ro")
def test_con07_a_read_only_console_shows_its_mode_and_swallows_keystrokes(server, console, story):
    """A read-only reader sees the mode on the page, and what they type never reaches the port."""
    page = console.console("web:ro", server, "loop1", user="ro")
    with step("the page carries the mode in its pill and on the body"):
        view = page.wait_for_view("(v) => v.connected === true && v.mode === 'read-only'", timeout=20)
        assert "mode-readonly" in str(view["body_class"]).split(), view["body_class"]
        assert page.text_of("#roIndicator").startswith("read-only"), page.text_of("#roIndicator")
    with step("the port never sees the text, because a Loopback port echoes what it receives"):
        # Absence of the echo is the proof here. The page does not disable the input, so a disabled
        # attribute would be a weaker claim than the byte that never arrives.
        page.send_line("keystrokes-must-not-travel")
        page.expect_no_text("keystrokes-must-not-travel", for_seconds=3)
    with step("the page says in writing why the Enter key did nothing"):
        page.wait_for_text("[WARNING: console is in read-only mode]", timeout=10)


@surfaces("web:rw")
def test_con08_the_info_overlay_reports_the_port_the_reader_is_looking_at(server, console, story):
    """The Info panel names the port, its description, and whether this reader is connected to it."""
    page = console.console("web:rw", server, "loop1", user="rw")
    page.wait_for_view("(v) => v.connected === true", timeout=20)
    with step("the panel stays closed until the reader asks for it"):
        assert page.page.is_hidden("#infoOverlay"), page.page.get_attribute("#infoOverlay", "style")
    with step("Info lists the port and calls it connected"):
        page.click("#infoToggle")
        page.page.wait_for_selector("#infoOverlay table.mini-table", timeout=10000)
        body = page.text_of("#infoBody")
        assert "loop1" in body, body
        assert "e2e loopback loop1" in body, body
        assert "Connected" in body and "yes" in body, body
    with step("the panel follows the port, so it is not a copy of the page header"):
        assert "cat1" not in body, body
    with step("the close button puts the panel away"):
        page.click("#infoClose")
        page.wait_until(lambda: page.page.is_hidden("#infoOverlay"), timeout=10, what="the panel to close")


@surfaces("web:rw", "cli:ro")
def test_con09_a_reader_who_arrives_late_gets_what_the_port_already_carried(servers, console, clients, story):
    """A console that asks for scrollback replays the port's recent bytes and is told how many."""
    # This world keeps its own history: the ring buffer is off unless a port sets ``scrollback_size``
    # in server.yaml, and the default Loopback port does not. A Command port is where the feature
    # lives, so the scenario builds one rather than bending the shared world.
    server = servers(
        name="history",
        command_ports=(),
        configure=lambda builder: builder.with_command_port("hist1", command="/bin/cat", scrollback_size=4096),
        start=True,
    )
    marker = "sent-before-the-reader"
    with step("one client puts a line on the port before any browser opens"):
        # ``clients`` defaults to the shared world, so a scenario with its own server names it. The
        # writer stays attached on purpose: the ring buffer belongs to the running process, and a
        # stopped Command port forgets it, so closing here would test the wrong thing.
        writer = clients.cli(user="admin", attach_to="hist1", surface="cli:writer", server=server)
        writer.write_line(marker)
        writer.wait_for(marker, timeout=20)
    page = console.console("web:rw", server, "hist1", user="admin")
    with step("the reader's terminal shows that line without anyone typing it"):
        # Nothing is typed after this page opens, so the only way the marker can appear is a replay.
        page.wait_for_text(marker, timeout=20)
    with step("the server closes the replay with one frame, which the page does not show"):
        # ``scrollback_done`` leaves no mark on the page: the frame in the hook log is the visible
        # proof that the replay finished rather than stalled part way.
        page.wait_until(lambda: "scrollback_done" in page.frame_types(), timeout=15, what="the replay to finish")
    with step("a reader who asks for no history does not get it"):
        # Same port, same moment, one query parameter apart. Without this the marker above could only
        # be live data that happened to arrive late.
        quiet = console.user("web:quiet", server, user="admin")
        quiet.goto("/console?port=hist1&scrollback=0&e2e=1")
        quiet.wait_for_view("(v) => v.connected === true", timeout=20)
        assert "scrollback_done" not in quiet.frame_types(), quiet.frame_types()
        quiet.expect_no_text(marker, for_seconds=4)


@surfaces("web:rw")
def test_con10_a_lost_socket_says_so_and_the_next_connect_clears_it(server, console, story):
    """The page warns after its WebSocket dies, waits out a brief flicker, and quiets down when the socket returns."""
    page = console.console("web:rw", server, "loop1", user="rw")
    page.wait_for_view("(v) => v.connected === true", timeout=20)
    with step("a healthy page shows no banner, though the node is there with an old sentence in it"):
        assert page.banner()["text"] is None, page.banner()
    with step("Disconnect brings the banner once the debounce has passed"):
        # The banner waits 1000 ms so a quick reconnect never flashes it. ``expect_banner`` polls for
        # the sentence instead of sleeping, which is what makes the debounce assertable at all.
        page.click("#connect")
        banner = page.expect_banner("WebSocket disconnected. Click Reconnect to retry.", timeout=15)
        assert banner["class"] == "error", banner
        assert "ws-down" in page.body_classes(), page.body_classes()
    with step("Reconnect clears the banner and the terminal shows the reader is back"):
        page.click("#connect")
        page.wait_for_view("(v) => v.connected === true", timeout=20)
        page.wait_until(lambda: page.banner()["text"] is None, timeout=10, what="the banner to clear")
        page.wait_for_text("Connected to console: local::loop1", timeout=10)


@surfaces("web:rw", "tcp:device")
def test_con11_a_device_that_stops_answering_shows_why_the_console_went_quiet(servers, devices, console, story):
    """A port whose link drops warns the reader with the server's reason, and quiets down when it returns."""
    device = devices.tcp("con1")
    server = servers(
        name="dropped",
        configure=lambda builder: builder.with_tcp_initiator_port("con1", host="127.0.0.1", port=device.port),
        start=True,
    )
    page = console.console("web:rw", server, "con1", user="rw")
    with step("an open link shows no banner, and the device sees the dial"):
        device.wait_connected(timeout=20)
        page.wait_for_view("(v) => v.connected === true && v.port_up === true", timeout=20)
        assert page.banner()["text"] is None, page.banner()
    with step("unplugging the device makes the port say so, in the server's words"):
        # A closed listener answers the next dial with a refusal, so the server carries a reason and the
        # page puts it under the fixed sentence. The banner debounces for 1000 ms like the socket one.
        device.unplug()
        banner = page.expect_banner("Port is disconnected on server. Data will resume when it becomes available.", timeout=20)
        assert banner["class"] == "warn", banner
        assert "port-down" in page.body_classes(), page.body_classes()
        assert page.view()["port_up"] is False, page.view()
    with step("the reason the server measured rides along under that sentence"):
        page.wait_until(
            lambda: "127.0.0.1" in page.text_of("#banner") and str(device.port) in page.text_of("#banner"),
            timeout=10,
            what="the address of the device that refused the dial",
        )
    with step("when the device answers again, the warning goes away on its own"):
        device.plug_back_in()
        device.wait_connected(timeout=30)
        page.wait_for_view("(v) => v.port_up === true", timeout=20)
        page.wait_until(lambda: page.banner()["text"] is None, timeout=10, what="the banner to clear")


@surfaces("web:rw", "http:admin")
def test_con12_a_port_added_after_the_page_loaded_shows_itself_without_a_reload(server, console, api, story):
    """The port list a reader first saw is not the list they are stuck with: a new port shows up by itself."""
    page = console.console("web:rw", server, "loop1", user="rw")
    page.wait_for_view("(v) => v.connected === true", timeout=20)
    with step("a second port appears in the list once the browser loaded the first one"):
        before = _sidebar_hrefs(page)
        assert "/console?port=loop2" not in before, before
        # An operator adds one port and reloads. The port under read is read back from the world's own
        # generated file, so the reload carries it unchanged and cannot disturb this reader.
        # ``api`` sends Basic credentials and no cookie, which is the path exempt from the CSRF check.
        built = yaml.safe_load((server.config_dir / "server.yaml").read_text(encoding="utf-8"))
        ports = list(built.get("loopback_ports") or [])
        assert [entry.get("name") for entry in ports] == ["loop1"], ports
        ports.append(_loop2_entry())
        api.post("/api/reload", {"loopback_ports": ports}, expect=(200,))
    with step("the open page adds the new row on its own, with no reload"):
        page.wait_until(
            lambda: "/console?port=loop2" in _sidebar_hrefs(page),
            timeout=30,
            what="the new port to reach the sidebar of the page that was already open",
        )
    with step("the list keeps the port the reader is using, and still marks it active"):
        hrefs = _sidebar_hrefs(page)
        assert "/console?port=loop1" in hrefs, hrefs
        active = page.page.eval_on_selector_all(
            "#console-ports a.nav-sub-item.active", "els => els.map((e) => e.getAttribute('href'))"
        )
        assert len(active) == 1 and str(active[0]).endswith("port=loop1"), active
    with step("the console the reader was using was not disturbed by the rebuild"):
        page.send_line("after-the-rebuild")
        page.wait_for_text("after-the-rebuild", timeout=20)


def _loop2_entry() -> Dict[str, Any]:
    """The Loopback port a reload adds while the reader's page is open."""
    return {"name": "loop2", "description": "e2e loopback loop2", "echo_delay": 0.0, "buffer_size": 4096}


@surfaces("web:rw")
def test_con13_a_reader_who_switches_to_light_keeps_it_across_a_reload(server, console, story):
    """The theme control changes the page now and remembers the choice for the next page."""
    page = console.console("web:rw", server, "loop1", user="rw")
    page.wait_for_view("(v) => v.connected === true", timeout=20)

    def theme() -> Optional[str]:
        return page.page.evaluate("() => document.documentElement.getAttribute('data-theme')")

    with step("the console starts dark, which it shows by carrying no theme attribute"):
        assert theme() is None, theme()
        assert page.page.evaluate("() => localStorage.getItem('omx_theme')") in (None, "dark")
    with step("the button offers the other theme, by name"):
        assert page.text_of("#themeText") == "Light Mode", page.text_of("#themeText")
    with step("one click applies light and stores the choice"):
        page.click("#themeToggle")
        page.wait_until(lambda: theme() == "light", timeout=10, what="the light theme to apply")
        assert page.page.evaluate("() => localStorage.getItem('omx_theme')") == "light"
        assert page.text_of("#themeText") == "Dark Mode", page.text_of("#themeText")
    with step("the next page load starts light, because the page reads the stored choice first"):
        page.reload()
        assert theme() == "light", theme()
    with step("a second click returns to dark, which is again no attribute at all"):
        page.click("#themeToggle")
        page.wait_until(lambda: theme() is None, timeout=10, what="the dark theme to return")
        assert page.page.evaluate("() => localStorage.getItem('omx_theme')") == "dark"


@surfaces("web:rw")
def test_con14_an_embedded_page_keeps_the_console_and_hides_the_navigation(server, console, story):
    """The embed parameter gives a console page with no sidebar, still working as a console."""
    page = console.console("web:rw", server, "loop1", user="rw")
    page.wait_for_view("(v) => v.connected === true && v.port_up === true", timeout=20)
    with step("the same page asked to be embedded"):
        full_width = page.sidebar_width()
        assert full_width > 0, full_width
        page.goto("/console?port=loop1&embed=1&e2e=1")
        page.wait_until(lambda: page.sidebar_width() == 0, timeout=10, what="the sidebar to collapse")
    with step("the server rendered the list and hid it, rather than leaving it out"):
        assert "collapsed" in page.page.evaluate("() => document.getElementById('sidebar').className")
        assert page.nav_links(), "an embedded page must keep its navigation in the document"
    with step("the console itself is untouched by the parameter"):
        view = page.wait_for_view("(v) => v.connected === true && v.port_up === true", timeout=20)
        assert view["connected"] is True, view
        page.send_line("still-a-working-console")
        page.wait_for_text("still-a-working-console", timeout=20)
