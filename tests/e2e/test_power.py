"""POWER: what every surface says and shows about PDU power (PWR-01 to PWR-19).

One port state is read in seven places: ``/api/power``, the two PDU pages, the console badge and its
menu, the ``POWER`` command, the Telnet ``p`` menu, and the audit log. Each of those has its own owner,
so a scenario here proves the pair that a person would otherwise have to reconcile by hand:

* outlet state, the impact of a switch, and the group rule come from ``adapters/pdu.py`` and
  ``ConsoleManager.blocked_ports_for_user``;
* every ``POWER`` reply, the ``[POWER]`` notice, and the ``p``-menu rows come from
  ``adapters/power_command.py``, which is the single source for text surfaces;
* the REST status and message come from ``web_plugins/power_monitor.py``;
* the badge, the two pre-flight confirms, and the terminal notice come from ``console.js`` and the two
  ``power_*.html.j2`` templates.

These scenarios use the ``dummy`` driver only, which is a simulation in three ways that matter. It
starts with every outlet on, it reports a fixed 120 W at 230 V whatever the position of the switch, and
the only failure it can be driven into from outside is an unknown outlet ref. Each fact is stated at the
assertion that depends on it instead of being hidden by a loose check.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple

import pytest

from .story import step, surfaces

pytestmark = [pytest.mark.e2e, pytest.mark.feature]

PDU = "rack1"
#: One account per permission, plus the names the group scenario adds. Defined before the world helper
#: because the world builds every account itself: the ``users=`` argument of the ``servers`` fixture
#: cannot state a group.
POWER_USERS: Tuple[Tuple[str, str, Sequence[str]], ...] = (
    ("admin", "admin", ()),
    ("rw", "read-write", ()),
    ("ro", "read-only", ()),
    ("rw2", "read-write", ()),
)
#: One feed each, so a scenario can show a console losing all power, one staying up, and an outlet
#: that no group rule can protect because it feeds no console at all.
FEED_SOLO = "rack1.1"  # feeds "solo" and "dual"
FEED_DUAL = "rack1.2"  # feeds "dual" only
FEED_FREE = "rack1.3"  # feeds no console at all: nothing can protect it, and nothing warns when it falls
FEED_MISSING = "rack1.9"  # a feed a port may declare while no PDU outlet provides it
WATTS_ON = 120.0  # what one dummy outlet reports, on or off
VOLTS = 230.0

#: The notice an attached text session is pushed when the switch takes its last feed away, and the
#: milder line for any other feed change. One function (`format_power_notice`) writes both.
NOTICE_ALL_LOST = f"[POWER WARNING] all power feeds are now OFF for this console ({FEED_SOLO})"
NOTICE_FEED_OFF = f"[POWER] feed {FEED_SOLO} is now off"
NOTICE_FEED_ON = f"[POWER] feed {FEED_SOLO} is now on"

#: The two states a `power:` section that is absent and one that is switched off produce. Both refuse
#: a switch, and neither says so in the other's words; PWR-04 records the pair as measured.
REST_NOT_CONFIGURED = "Power management is not configured"
REST_DISABLED = "power management is disabled"
POWER_NOT_CONFIGURED = "ERROR:POWER: power management is not configured"


def _power_world(
    servers: Any,
    name: str,
    *,
    outlets: Sequence[str] = ("1", "2", "3"),
    feeds: Optional[Dict[str, Sequence[str]]] = None,
    port_knobs: Optional[Dict[str, Dict[str, Any]]] = None,
    extra_users: Sequence[Tuple[str, str, Sequence[str]]] = (),
    pdu_name: str = PDU,
    pdu_enabled: bool = True,
    telnet_kwargs: Optional[Dict[str, Any]] = None,
) -> Any:
    """One server with a dummy PDU, three loopback consoles, and one account per permission.

    ``solo`` hangs off one feed, ``dual`` off two, and ``bare`` off nothing, so a scenario can prove a
    console losing all power, a console holding on through a second feed, and a console with no badge at
    all. ``rack1.3`` feeds no console, which is the fourth case: an outlet no group rule can protect and
    whose fall warns nobody. ``feeds`` replaces that map and ``port_knobs`` adds access keys such as
    ``read_write_groups`` to one port.

    ``extra_users`` carries ``(name, permission, groups)``, because the group rule needs group members
    and ``users=`` on the ``servers`` fixture cannot state a group.
    """

    def configure(builder: Any) -> Any:
        for username, permission, groups in POWER_USERS + tuple(extra_users):
            builder.with_user(username, permission, groups=list(groups) or None)
        builder.with_dummy_pdu(pdu_name, [str(outlet) for outlet in outlets], poll_interval=0, enabled=pdu_enabled)
        world = feeds or {
            "solo": [FEED_SOLO],
            "dual": [FEED_SOLO, FEED_DUAL],
            "bare": [],
        }
        for port_name, refs in world.items():
            knobs: Dict[str, Any] = {"write_slot_capacity": "multiple", "feeds": list(refs)}
            knobs.update((port_knobs or {}).get(port_name) or {})
            builder.with_loopback(port_name, **knobs)
        return builder

    return servers(
        name=name,
        users=(),
        loopback=(),
        configure=configure,
        telnet=bool(telnet_kwargs),
        telnet_kwargs=telnet_kwargs,
        start=True,
    )


def _outlet(snapshot: Dict[str, Any], ref: str) -> Dict[str, Any]:
    """One outlet entry of a ``/api/power`` snapshot, found by its ref."""
    for pdu in snapshot.get("pdus") or []:
        for entry in pdu.get("outlets") or []:
            if entry.get("ref") == ref:
                return dict(entry)
    seen = [str(entry.get("ref")) for pdu in snapshot.get("pdus") or [] for entry in pdu.get("outlets") or []]
    raise AssertionError(f"{ref} is not in the power snapshot; refs present: {seen}")


def _is_watts(value: Any) -> bool:
    """Whether a snapshot wattage is what the dummy driver reports for one outlet."""
    return abs(float(value) - WATTS_ON) < 0.01


def _row(rows: Sequence[Dict[str, Any]], ref: str) -> Dict[str, Any]:
    """One outlet row read off a PDU page, by ref."""
    entry = next((row for row in rows if row["ref"] == ref), None)
    if entry is None:
        raise AssertionError(f"the PDU page has no row for {ref}: {[row['ref'] for row in rows]}")
    return entry


# --- what the API reports, and what an unconfigured server answers ----------------------------------


@surfaces("http")
def test_pwr01_the_power_api_mirrors_the_pdu_and_moves_with_one_switch(servers, api_for_server, story):
    """``/api/power`` reports the configured PDU, and one switch moves only the fields it should."""
    world = _power_world(servers, "pwr01")
    api = api_for_server(world)

    snapshot = api.power()
    with step("the PDU and its outlets are the configured ones"):
        assert snapshot["enabled"] is True
        assert snapshot["unresolved_refs"] == []
        assert len(snapshot["pdus"]) == 1
        pdu = snapshot["pdus"][0]
        assert pdu["name"] == PDU
        assert pdu["driver"] == "dummy"
        assert pdu["online"] is True
        assert pdu["poll_interval"] == 0, "the world asks for no poller, so nothing moves an outlet by itself"
        assert [entry["ref"] for entry in pdu["outlets"]] == [FEED_SOLO, FEED_DUAL, FEED_FREE]
        assert pdu["outlet_count"] == 3
        assert pdu["outlets_on"] == 3
        assert abs(float(pdu["total_watts"]) - 3 * WATTS_ON) < 0.01
    with step("the console mapping is the reverse of the port's own `power:` list"):
        by_ref = {str(entry["ref"]): entry for entry in pdu["outlets"]}
        assert sorted(by_ref[FEED_SOLO]["mapped_ports"]) == ["dual", "solo"]
        assert by_ref[FEED_DUAL]["mapped_ports"] == ["dual"]
        assert by_ref[FEED_FREE]["mapped_ports"] == []
        assert by_ref[FEED_FREE]["any_mapped"] is False
        assert abs(float(by_ref[FEED_SOLO]["volts"]) - VOLTS) < 0.01
        assert _is_watts(by_ref[FEED_SOLO]["watts"])
        impact = by_ref[FEED_SOLO]["off_impact"]
        assert [entry["port"] for entry in impact["losing_power"]] == ["solo"], impact
        assert impact["losing_power"][0][
            "description"
        ], "the impact carries the port's own description, which a confirm prints"
        assert [entry["port"] for entry in impact["staying_up"]] == ["dual"], impact
        assert impact["staying_up"][0]["via"] == [FEED_DUAL], impact

    body = api.set_outlet(FEED_FREE, False)
    with step("one switch moves that outlet, and only the aggregate over the ones still on"):
        assert body["ok"] is True
        assert body["reading"]["on"] is False
        moved = api.power()
        after = _outlet(moved, FEED_FREE)
        assert after["on"] is False
        assert _is_watts(after["watts"]), "the dummy driver reports the same wattage whatever the switch position"
        assert moved["pdus"][0]["outlets_on"] == 2
        assert abs(float(moved["pdus"][0]["total_watts"]) - 2 * WATTS_ON) < 0.01
        assert _outlet(moved, FEED_SOLO)["on"] is True, "one switch never moves a neighbouring outlet"


@surfaces("http", "web:rw")
def test_pwr02_a_feed_no_outlet_provides_is_named_by_the_api_the_page_and_the_badge(servers, api_for_server, console, story):
    """A console that declares a feed no PDU outlet provides is reported everywhere it appears."""
    world = _power_world(
        servers,
        "pwr02",
        feeds={"solo": [FEED_SOLO], "lost": [FEED_MISSING], "free": [FEED_FREE]},
    )
    api = api_for_server(world)
    with step("the API keeps the ref in one list and out of every outlet's mapping"):
        snapshot = api.power()
        assert snapshot["unresolved_refs"] == [FEED_MISSING]
        mapped = {port for pdu in snapshot["pdus"] for entry in pdu["outlets"] for port in entry["mapped_ports"]}
        assert "lost" not in mapped
        assert _outlet(snapshot, FEED_SOLO)["mapped_ports"] == ["solo"]
        assert [entry["port"] for entry in _outlet(snapshot, FEED_SOLO)["off_impact"]["losing_power"]] == ["solo"]
    with step("the landing page names the feed and says what to fix"):
        page = console.user("web:rw", world, user="rw")
        page.goto("/power")
        text = page.page_text()
        assert "Unresolved power feeds" in text, text[:1200]
        assert "no configured PDU outlet provides" in text
        assert FEED_MISSING in text
    with step("and the console it feeds still shows that feed, tinted unknown, because a declared feed is never silent"):
        console_page = console.console("web:lost", world, "lost", user="rw")
        console_page.wait_until(lambda: console_page.power_badge()["label"] == "0/1", timeout=15, what="the badge to read 0/1")
        assert console_page.power_badge()["state"] == "unknown", console_page.view()
        assert console_page.power_badge_title() == "PDU power: 0 of 1 feeds on", console_page.power_badge_title()


@surfaces("http", "cli:rw", "web:rw")
def test_pwr03_power_that_is_absent_and_power_that_is_switched_off_are_refused_differently(
    servers, clients, api_for_server, console, story
):
    """Every surface refuses power nobody configured, but an absent section and a disabled one differ.

    Measured behaviour, recorded rather than smoothed over: a config with no ``power:`` section has no
    power adapter at all, so its four routes answer 404 and the text surfaces say "not configured". A
    section that says ``enabled: false`` keeps the adapter, so ``/api/power`` answers 200 with
    ``enabled`` false, the switch answers 409 and says "disabled", and the page carries its own banner --
    while the ``POWER`` command still says "not configured".
    """
    absent = servers(
        name="pwr03-absent",
        users=(("admin", "admin"), ("rw", "read-write")),
        loopback=(),
        configure=lambda builder: builder.with_loopback("solo", write_slot_capacity="multiple"),
        start=True,
    )
    disabled = _power_world(servers, "pwr03-off", pdu_enabled=False)

    absent_api = api_for_server(absent)
    with step("no power section: every power route is a 404 that says so"):
        _, _, body = absent_api.get("/api/power", expect=(404,))
        assert REST_NOT_CONFIGURED in body
        _, _, body = absent_api.get("/power", expect=(404,))
        assert REST_NOT_CONFIGURED in body
        _, _, body = absent_api.post(f"/api/power/outlets/{FEED_SOLO}", {"on": False}, expect=(404,))
        assert REST_NOT_CONFIGURED in body
    with step("and the text surface uses its own wording for the same absence"):
        cli = clients.cli(user="rw", server=absent, surface="cli:rw")
        cli.command("POWER")
        cli.wait_for(POWER_NOT_CONFIGURED, timeout=20)

    disabled_api = api_for_server(disabled)
    with step("a section that is switched off keeps the API alive and answers the switch with 409"):
        snapshot = disabled_api.power()
        assert snapshot["enabled"] is False
        _, _, body = disabled_api.post(f"/api/power/outlets/{FEED_SOLO}", {"on": False}, expect=(409,))
        assert REST_DISABLED in body
    with step("and the page states the fix in its own banner"):
        page = console.user("web:rw", disabled, user="rw")
        page.goto("/power")
        assert "Power management is disabled" in page.page_text()
    with step("the text surface keeps the one sentence it uses for any missing power"):
        cli = clients.cli(user="rw", server=disabled, surface="cli:rw")
        cli.command("POWER rack1")
        cli.wait_for(POWER_NOT_CONFIGURED, timeout=20)


# --- the two PDU pages -------------------------------------------------------------------------------


@surfaces("web:admin", "http")
def test_pwr04_the_power_pages_list_the_pdu_and_follow_a_switch_someone_else_made(servers, api_for_server, console, story):
    """The landing page counts the estate, its row opens the PDU, and the detail page repaints itself."""
    world = _power_world(servers, "pwr04")
    api = api_for_server(world)
    page = console.user("web:admin", world, user="admin")

    with step("the Power page is where the navigation says it is"):
        page.goto("/")
        assert "Power" in page.nav_labels(), page.nav_labels()
    with step("the cards count PDUs, outlets that are on, and the load"):
        page.goto("/power")
        cards = {card["label"]: card["value"] for card in page.metric_cards()}
        assert cards["PDUs"] == "1", cards
        assert cards["Outlets on"] == "3", cards
        assert cards["Total load"] == "360 W", cards
    with step("the PDU row names the driver, the state, and the load, and links to its page"):
        header, *rows = page.table_rows("#pdu-table")
        assert header == ["Name", "Description", "Driver", "Status", "Outlets on", "Load"], header
        assert rows[0][0] == PDU and rows[0][2] == "dummy" and rows[0][3] == "online", rows
        assert rows[0][4] == "3/3" and rows[0][5] == "360 W", rows
    with step("that row's page offers one switch per outlet, all of them to turn an outlet off"):
        page.goto(f"/power/{PDU}")
        rows = page.outlet_rows()
        assert [row["ref"] for row in rows] == [FEED_SOLO, FEED_DUAL, FEED_FREE]
        assert [row["button"] for row in rows] == ["Turn off"] * 3, rows
        assert "120 W 230 V 0.5 A" in _row(rows, FEED_SOLO)["text"], _row(rows, FEED_SOLO)["text"]
    with step("a switch made over REST repaints the row, because the page listens for power_changed"):
        api.set_outlet(FEED_DUAL, False)
        page.wait_until(
            lambda: _row(page.outlet_rows(), FEED_DUAL)["button"] == "Turn on",
            timeout=15,
            what="the rack1.2 row to read Turn on",
        )
    with step("an outlet that feeds no console repaints too, though no console port carries that change"):
        api.set_outlet(FEED_FREE, False)
        page.wait_until(
            lambda: _row(page.outlet_rows(), FEED_FREE)["button"] == "Turn on",
            timeout=15,
            what="the rack1.3 row to read Turn on",
        )
        assert _outlet(api.power(), FEED_FREE)["on"] is False


@surfaces("web:rw", "web:ro")
def test_pwr05_only_a_write_console_page_offers_an_outlet_switch(servers, console, api_for_server, story):
    """A read-write page carries the switch, a read-only page is told it is read-only, in words."""
    world = _power_world(servers, "pwr05")
    writer = console.user("web:rw", world, user="rw")
    writer.goto(f"/power/{PDU}")
    with step("the writer sees one switch per outlet"):
        rows = writer.outlet_rows()
        assert [row["button"] for row in rows] == ["Turn off", "Turn off", "Turn off"], rows
    with step("the reader sees no switch anywhere and the reason in the same column"):
        reader = console.user("web:ro", world, user="ro")
        reader.goto(f"/power/{PDU}")
        reader_rows = reader.outlet_rows()
        assert [row["button"] for row in reader_rows] == [None, None, None], reader_rows
        assert "read-only" in _row(reader_rows, FEED_SOLO)["text"], _row(reader_rows, FEED_SOLO)["text"]
        reader.expect_absent("button[data-toggle]")
    with step("and the outlet the reader could not touch is still on"):
        assert _outlet(api_for_server(world).power(), FEED_FREE)["on"] is True


@surfaces("web:rw", "http")
def test_pwr06_the_first_confirm_names_the_console_that_loses_all_power_and_a_dismissal_switches_nothing(
    servers, console, api_for_server, story
):
    """Switching a feed off asks first, and names the console that loses everything in the question."""
    world = _power_world(servers, "pwr06")
    api = api_for_server(world)
    page = console.user("web:rw", world, user="rw")
    page.goto(f"/power/{PDU}")
    page.wait_until(
        lambda: len(page.outlet_rows()) == 3,
        timeout=10,
        what="three outlet rows, which is also what fills the page's impact snapshot",
    )
    messages = page.accept_dialogs(accept=False)

    with step("the question names the console that loses all power"):
        page.click_outlet_toggle(FEED_SOLO)
        messages = page.wait_for_dialogs(messages, 1)
        assert messages[0].startswith("This removes ALL power to: solo"), messages
    with step("dismissing it switches nothing, and the row keeps its switch"):
        page.expect_absent_for(f'#outlet-rows tr[data-ref="{FEED_SOLO}"] button[data-target="true"]', for_seconds=2.0)
        assert _row(page.outlet_rows(), FEED_SOLO)["button"] == "Turn off", page.outlet_rows()
        assert _outlet(api.power(), FEED_SOLO)["on"] is True


@surfaces("web:rw", "http")
def test_pwr07_the_second_confirm_names_the_console_that_stays_up_and_an_acceptance_switches(
    servers, console, api_for_server, story
):
    """Accepted, the same two questions switch the feed and the row follows the outlet."""
    world = _power_world(servers, "pwr07")
    api = api_for_server(world)
    page = console.user("web:rw", world, user="rw")
    page.goto(f"/power/{PDU}")
    page.wait_until(lambda: len(page.outlet_rows()) == 3, timeout=10, what="three outlet rows")
    messages = page.accept_dialogs(accept=True)

    with step("one feed off asks twice: what falls, and what holds"):
        page.click_outlet_toggle(FEED_SOLO)
        messages = page.wait_for_dialogs(messages, 2)
        assert messages[0].startswith("This removes ALL power to: solo"), messages
        assert messages[1].startswith(f"Consoles staying up via other feeds: dual (via {FEED_DUAL})"), messages
    with step("the row turns to Turn on, and no error dialog followed the switch"):
        page.wait_until(
            lambda: _row(page.outlet_rows(), FEED_SOLO)["button"] == "Turn on",
            timeout=10,
            what="the outlet row to read Turn on",
        )
        assert len(messages) == 2, messages
    with step("and the API agrees, with dual still fed by its second outlet"):
        snapshot = api.power()
        assert _outlet(snapshot, FEED_SOLO)["on"] is False
        assert _outlet(snapshot, FEED_DUAL)["on"] is True


# --- the console badge -------------------------------------------------------------------------------


@surfaces("web:rw", "http")
def test_pwr08_the_console_badge_counts_the_feeds_that_are_on_and_warns_when_none_are(servers, console, api_for_server, story):
    """The console badge reads n/m, changes tint as its feeds fall, and warns when none are left."""
    world = _power_world(servers, "pwr08")
    api = api_for_server(world)
    page = console.console("web:rw", world, "dual", user="rw")
    with step("both feeds on: full tint, and a tooltip that says so"):
        page.wait_until(lambda: page.power_badge()["label"] == "2/2", timeout=15, what="the badge to read 2/2")
        assert page.power_badge()["state"] == "all", page.view()
        assert page.power_badge_title() == "PDU power: 2 of 2 feeds on", page.power_badge_title()
    with step("one of two: the tint drops to partial, and the badge still counts"):
        api.set_outlet(FEED_SOLO, False)
        page.wait_until(lambda: page.power_badge()["label"] == "1/2", timeout=15, what="the badge to read 1/2")
        assert page.power_badge()["state"] == "some", page.view()
    with step("none of two: the badge says the operator is about to lose this console"):
        api.set_outlet(FEED_DUAL, False)
        page.wait_until(lambda: page.power_badge()["label"] == "0/2", timeout=15, what="the badge to read 0/2")
        assert page.power_badge()["state"] == "none", page.view()
        assert page.power_badge_title().startswith("ALL POWER LOST"), page.power_badge_title()


@surfaces("web:rw", "http")
def test_pwr09_the_badge_menu_switches_one_feed_and_states_its_load(servers, console, api_for_server, story):
    """The badge menu is a switch panel for a read-write console, with the wattage beside each feed."""
    world = _power_world(servers, "pwr09")
    api = api_for_server(world)
    page = console.console("web:rw", world, "dual", user="rw")
    page.wait_until(lambda: page.power_badge()["label"] == "2/2", timeout=15, what="the badge to read 2/2")
    with step("each feed row carries a switch that names the feed, and its load"):
        page.open_power_menu()
        items = page.power_menu_items()
        assert [item["button"] for item in items] == [f"Turn off {FEED_SOLO}", f"Turn off {FEED_DUAL}"], items
        assert all(item["feed"].endswith("120 W") for item in items), items
    with step("clicking one row switches that feed and reports the outcome on the console socket"):
        page.click_power_toggle(FEED_SOLO, off=True)
        page.wait_until(
            lambda: any(frame.get("type") == "power_switch" and frame.get("ok") is True for frame in page.frames()),
            timeout=15,
            what="an answered power_switch frame",
        )
        assert _outlet(api.power(), FEED_SOLO)["on"] is False
        page.wait_until(lambda: page.power_badge()["label"] == "1/2", timeout=15, what="the badge to read 1/2")


@surfaces("web:ro")
def test_pwr10_a_read_only_console_page_offers_no_feed_switch(servers, console, story):
    """The same badge menu on a read-only console lists its feeds and offers no switch at all."""
    world = _power_world(servers, "pwr10")
    page = console.console("web:ro", world, "dual", user="ro")
    page.wait_until(lambda: page.power_badge()["label"] == "2/2", timeout=15, what="the badge to read 2/2")
    page.open_power_menu()
    items = page.power_menu_items()
    with step("both feeds are listed, with the same load a writer sees"):
        assert [item["feed"] for item in items] == [
            f"{FEED_SOLO} on - 120 W",
            f"{FEED_DUAL} on - 120 W",
        ], items
    with step("and no row carries a switch"):
        assert all(item["button"] is None for item in items), items
        page.expect_absent("#powerMenuList button")


@surfaces("web:rw", "http")
def test_pwr11_the_console_terminal_reports_all_power_lost_and_the_restored_feeds(servers, console, api_for_server, story):
    """The terminal prints the warning when the last feed falls and the count when power returns.

    Both sentences come from ``console.js`` and are pushed by the port's own ``power`` meta frame, which
    is why a scenario drives them from REST rather than trusting the badge.
    """
    world = _power_world(servers, "pwr11")
    api = api_for_server(world)
    page = console.console("web:rw", world, "dual", user="rw")
    page.wait_until(lambda: page.power_badge()["label"] == "2/2", timeout=15, what="the badge to read 2/2")

    with step("switching one feed off says nothing about power loss, because one feed is left"):
        api.set_outlet(FEED_SOLO, False)
        page.wait_until(lambda: page.power_badge()["label"] == "1/2", timeout=15, what="the badge to read 1/2")
        page.expect_no_text("all power feeds are OFF", for_seconds=1.0)
    with step("the last feed off is a warning that counts the feeds down"):
        api.set_outlet(FEED_DUAL, False)
        page.expect_notice("[POWER WARNING: all power feeds are OFF - 2 feed(s) down]", timeout=15)
        assert page.power_badge()["state"] == "none", page.view()
    with step("power coming back is reported with the number of feeds back on"):
        api.set_outlet(FEED_SOLO, True)
        page.expect_notice("[POWER restored: 1/2 feed(s) on]", timeout=15)


# --- the two text surfaces that push a notice --------------------------------------------------------


@surfaces("cli:rw", "telnet:rw")
def test_pwr12_an_attached_text_session_is_told_its_feed_changed_in_the_same_words_on_both_surfaces(
    servers, clients, api_for_server, story
):
    """The notice an attached watcher is pushed is one function's output on CLI and on Telnet alike."""
    world = _power_world(servers, "pwr12", telnet_kwargs={"require_auth": True})
    api = api_for_server(world)
    cli = clients.cli(user="rw", server=world, surface="cli:rw", attach_to="solo")
    tel = clients.telnet(user="rw2", server=world, surface="telnet:rw", require_auth=True)
    with step("the Telnet session joins the same console the CLI session drives"):
        tel.choose_port("solo")

    with step("the switch that takes the only feed away warns both"):
        api.set_outlet(FEED_SOLO, False)
        cli.wait_for(NOTICE_ALL_LOST, timeout=20)
        tel.wait_for(NOTICE_ALL_LOST, timeout=20)
    with step("and the switch that brings it back says so, again identically"):
        api.set_outlet(FEED_SOLO, True)
        cli.wait_for(NOTICE_FEED_ON, timeout=20)
        tel.wait_for(NOTICE_FEED_ON, timeout=20)


@surfaces("cli:rw")
def test_pwr13_the_power_command_lists_every_pdu_one_pdu_and_one_outlet(servers, clients, story):
    """``POWER`` prints an inventory, ``POWER <pdu>`` its outlets, and ``POWER <ref>`` one line."""
    world = _power_world(servers, "pwr13")
    cli = clients.cli(user="rw", server=world, surface="cli:rw")

    with step("the bare command names the PDU, counts its outlets, and indents each row"):
        cli.command("POWER")
        cli.wait_for(f"PDU {PDU} (dummy)  3/3 on", timeout=20)
        cli.wait_for(f"  POWER {FEED_FREE} on 120W 230V", timeout=20)
    with step(
        "a mapped row names every console the outlet feeds, in the order the ports are configured, and an unmapped one names none"
    ):
        cli.wait_for(f"POWER {FEED_SOLO} on 120W 230V  -> solo, dual", timeout=20)
        assert f"{FEED_FREE} on 120W 230V ->" not in cli.read_text(), cli.read_text()[-800:]
    with step("one PDU name lists its outlets without the header"):
        mark = cli.offset()
        cli.command(f"POWER {PDU}")
        cli.wait_for(f"POWER {FEED_SOLO} on 120W 230V  -> solo, dual", timeout=20)
        assert "PDU rack1 (dummy)" not in cli.reply_after(mark), cli.reply_after(mark)
    with step("one full ref answers with exactly one line, naming the console it feeds"):
        mark = cli.offset()
        cli.command(f"POWER {FEED_DUAL}")
        cli.wait_for(f"POWER {FEED_DUAL} on 120W 230V  -> dual", timeout=20)
        assert len([line for line in cli.reply_after(mark).splitlines() if FEED_DUAL in line]) == 1, cli.reply_after(mark)


@surfaces("cli:ro")
def test_pwr14_the_power_command_refuses_a_malformed_line_and_an_unknown_target(servers, clients, story):
    """A malformed command or an unknown target is refused with its own sentence, and changes nothing."""
    world = _power_world(servers, "pwr14")
    cli = clients.cli(user="ro", server=world, surface="cli:ro")

    with step("a bad shape is the usage line, which is one constant shared with the parser"):
        cli.command("POWER rack1 extra words")
        cli.wait_for("ERROR:POWER: usage: POWER [<pdu>[.<outlet>] [on|off]]", timeout=20)
        cli.command("powerx")
        cli.wait_for("ERROR:POWER: usage: POWER [<pdu>[.<outlet>] [on|off]]", timeout=20)
    with step("an unknown PDU and an unknown outlet are different answers"):
        cli.command("POWER rack9")
        cli.wait_for("ERROR:POWER: unknown PDU: rack9", timeout=20)
        cli.command(f"POWER {FEED_MISSING}")
        cli.wait_for(f"ERROR:POWER: unknown outlet: {FEED_MISSING}", timeout=20)
    with step("a switch needs the full ref, and a verb that is not on or off is refused first"):
        cli.command("POWER rack1 off")
        cli.wait_for("ERROR:POWER: switching needs a full outlet ref '<pdu>.<outlet>'", timeout=20)
        cli.command(f"POWER {FEED_SOLO} maybe")
        cli.wait_for("ERROR:POWER: target state must be 'on' or 'off'", timeout=20)


@surfaces("cli:rw")
def test_pwr15_the_power_command_switches_and_warns_who_loses_and_who_keeps_power(servers, clients, api_for_server, story):
    """``POWER <ref> off`` names the consoles it is about to kill and the ones that hold on."""
    world = _power_world(servers, "pwr15")
    api = api_for_server(world)
    cli = clients.cli(user="rw", server=world, surface="cli:rw")

    with step("switching off warns before it acts, in both directions"):
        cli.command(f"POWER {FEED_SOLO} off")
        cli.wait_for("WARNING:POWER: removing all power to: solo", timeout=20)
        cli.wait_for(f"NOTE:POWER: dual stays up via {FEED_DUAL}", timeout=20)
        cli.wait_for(f"POWER {FEED_SOLO} -> off", timeout=20)
        assert _outlet(api.power(), FEED_SOLO)["on"] is False
    with step("switching on has nothing to warn about and still confirms"):
        mark = cli.offset()
        cli.command(f"POWER {FEED_SOLO} on")
        cli.wait_for(f"POWER {FEED_SOLO} -> on", timeout=20)
        assert "WARNING:POWER" not in cli.reply_after(mark), cli.reply_after(mark)
        assert _outlet(api.power(), FEED_SOLO)["on"] is True
    with step("an outlet that feeds no console switches with no warning at all"):
        mark = cli.offset()
        cli.command(f"POWER {FEED_FREE} off")
        cli.wait_for(f"POWER {FEED_FREE} -> off", timeout=20)
        assert "WARNING:POWER" not in cli.reply_after(mark), cli.reply_after(mark)


@surfaces("telnet:rw", "telnet:none")
def test_pwr16_the_console_power_menu_toggles_by_number_and_exits_on_enter(servers, clients, api_for_server, story):
    """The ``p`` menu lists this console's feeds, toggles one by number, and refuses a bad number."""
    world = _power_world(servers, "pwr16", telnet_kwargs={"require_auth": True})
    api = api_for_server(world)
    rw = clients.telnet(user="rw", server=world, surface="telnet:rw", require_auth=True)
    with step("the session picks the console with two feeds, and session info proves it drives it"):
        rw.choose_port("dual")

    rw.press("p")
    with step("the header says what a number does, and every row is numbered and tagged"):
        rw.wait_for(f"POWER: feeds for dual  (a number = toggle, a = all, Enter = exit)", timeout=20)
        rw.wait_for(f" 1  [on]   {FEED_SOLO}", timeout=20)
        rw.wait_for(f" 2  [on]   {FEED_DUAL}", timeout=20)
    with step("a number out of range is refused without touching anything"):
        rw.answer("9")
        rw.wait_for("POWER: number out of range (1-2)", timeout=20)
    with step("some other answer is refused the same way"):
        rw.answer("x")
        rw.wait_for("POWER: enter a feed number, 'a' for all, or Enter to exit", timeout=20)
    with step("a real number flips that feed and re-prints the list below the live notice"):
        rw.answer("1")
        rw.wait_for(f"POWER {FEED_SOLO} -> off", timeout=20)
        rw.wait_for(f" 1  [off]  {FEED_SOLO}", timeout=20)
        assert _outlet(api.power(), FEED_SOLO)["on"] is False
        assert _outlet(api.power(), FEED_DUAL)["on"] is True
    with step("Enter leaves the menu with its marker"):
        rw.answer("")
        rw.wait_for("[EXITING POWER]", timeout=20)
    with step("and the keystroke typed next reaches the Port, which no menu line steals"):  # issue #100
        reply = rw.wait_for_echo("[POWER] menu done")
        # A Telnet client ends a line with CR LF, so an echo of an empty line between the exit and this
        # reply is the phantom line that used to close the menu by itself and eat the next keystroke.
        assert "[ENTER]" not in reply.split("[POWER] menu done")[0], reply
    with step("a console with no feeds says so instead of a list"):
        none = clients.telnet(user="rw2", server=world, surface="telnet:none", require_auth=True)
        none.choose_port("bare")  # attached, and nothing drives its slot
        none.press("p")
        none.wait_for("POWER: bare has no power feeds (Enter exits)", timeout=20)
        none.expect_no_text("POWER: feeds for", for_seconds=1.0)
        none.answer("")
        none.wait_for("[EXITING POWER]", timeout=20)


# --- who may switch, and what the refusal says -------------------------------------------------------


@surfaces("cli:ro", "web:ro", "http")
def test_pwr17_a_read_only_identity_is_refused_on_every_surface_it_reaches(servers, clients, console, api_for_server, story):
    """Read-only means no switch anywhere: REST, the ``POWER`` command, and the PDU page."""
    world = _power_world(servers, "pwr17")
    api = api_for_server(world)

    with step("a read-write key switches, so the world is switchable before the refusals"):
        assert api.set_outlet(FEED_FREE, False).get("ok") is True
        api.set_outlet(FEED_FREE, True)
    with step("the same call with a read-only key is refused with no body to argue with"):
        ro_api = api_for_server(world, "ro")
        ro_api.post(f"/api/power/outlets/{FEED_FREE}", {"on": False}, expect=(403,))
    with step("the text command says which permission it wanted"):
        cli = clients.cli(user="ro", server=world, surface="cli:ro")
        cli.command(f"POWER {FEED_SOLO} off")
        cli.wait_for("ERROR:POWER: insufficient permission (need read-write)", timeout=20)
        assert _outlet(api.power(), FEED_SOLO)["on"] is True
    with step("and its page carries no switch to click"):
        page = console.user("web:ro", world, user="ro")
        page.goto(f"/power/{PDU}")
        page.expect_absent("button[data-toggle]")


@surfaces("http", "cli:rw")
def test_pwr18_an_outsider_is_told_which_console_protects_itself_and_a_member_is_not(servers, clients, api_for_server, story):
    """``read_write_groups`` reaches power: only a member, or an admin, may switch the feeds of those consoles.

    Three refusals and one success, each naming the exact console, which is the point of the wording: an
    operator must learn whose console is protected, not just that a rule blocked them. An outlet that
    feeds no console at all is never protected, so ``rack1.3`` stays switchable for the outsider.
    """
    world = _power_world(
        servers,
        "pwr18",
        port_knobs={"solo": {"read_write_groups": ["net"]}},
        extra_users=(("netops", "read-write", ["net"]), ("outsider", "read-write", [])),
    )
    outsider = api_for_server(world, "outsider")
    member_api = api_for_server(world, "netops")

    with step("the outsider's REST switch is refused with the console named"):
        _, _, body = outsider.post(f"/api/power/outlets/{FEED_SOLO}", {"on": False}, expect=(403,))
        assert f"{FEED_SOLO} feeds consoles outside your groups (solo); switching it needs admin" in body, body
    with step("the same refusal on the command line, in the same words"):
        cli = clients.cli(user="outsider", server=world, surface="cli:outsider")
        cli.command(f"POWER {FEED_SOLO} off")
        cli.wait_for(
            f"ERROR:POWER: {FEED_SOLO} feeds consoles outside your groups (solo); switching it needs admin", timeout=20
        )
        assert _outlet(api_for_server(world).power(), FEED_SOLO)["on"] is True
    with step("an outlet that feeds no console is not protected, so the outsider switches it"):
        assert outsider.set_outlet(FEED_FREE, False).get("ok") is True
    with step("a group member switches the protected feed"):
        assert member_api.set_outlet(FEED_SOLO, False).get("ok") is True
        assert _outlet(api_for_server(world).power(), FEED_SOLO)["on"] is False
    with step("and an admin needs no membership"):
        assert api_for_server(world).set_outlet(FEED_SOLO, True).get("ok") is True


@surfaces("http", "cli:rw", "server")
def test_pwr19_a_switch_is_audited_once_where_power_is_recorded_and_a_refusal_not_at_all(
    servers, clients, api_for_server, story
):
    """One switch writes one control line and one notice per console; a refusal writes neither."""
    world = _power_world(servers, "pwr19")
    api = api_for_server(world)

    with step("a switch that is refused writes no audit line, so a count is the proof"):
        api_for_server(world, username="ro").post(f"/api/power/outlets/{FEED_SOLO}", {"on": False}, expect=(403,))
        _, _, body = api.post(f"/api/power/outlets/{FEED_MISSING}", {"on": False}, expect=(404,))
        assert "unknown outlet" in body, body
        before = len([line for line in world.log_lines() if "POWER CONTROL:" in line])
        assert before == 0, [line for line in world.log_lines() if "POWER CONTROL:" in line]

    with step("the server log names who, what, and which consoles fall"):
        api.set_outlet(FEED_SOLO, False)
        line = world.wait_for_log(f"POWER CONTROL: user admin turned {FEED_SOLO} off", timeout=15)
        assert "losing all power: solo" in line, line
        assert f"staying up: dual" in line, line
        assert "; client " not in line, "a REST switch has no session id to record"

    with step("each affected console's own log carries the notice, in its own words"):
        solo_line = world.wait_for_port_log("solo", "power_control_notice", timeout=15)
        assert "outlet=rack1.1" in solo_line and "state=off" in solo_line, solo_line
        assert "all power feeds to this console are now off" in solo_line, solo_line
        dual_line = world.wait_for_port_log("dual", "power_control_notice", timeout=15)
        assert f"text=[POWER] feed {FEED_SOLO} is now off" in dual_line, dual_line
        assert "user=admin" in dual_line, dual_line

    with step("the count grows by exactly one for one switch, and not at all for the failures above"):
        assert len([line for line in world.log_lines() if "POWER CONTROL:" in line]) == before + 1
