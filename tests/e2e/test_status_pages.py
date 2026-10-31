"""STATUS: what the status page reports, and what it does while nobody reloads it (ST-01 to ST-06).

The status page is one server-rendered document that then keeps itself current. Four regions
(``summary``, ``ports``, ``federation``, ``clients``) are swapped in place from
``GET /api/status/fragments`` when the site event socket carries a status poke (``status_change``), and nothing
else on the page reloads. That split is what every scenario here presses on, because the two halves have
different owners: ``status.js`` owns the swap and which rows stay open, ``web_console.py`` owns the sort
links, the fragment markers, and the client list, and ``layout.js`` owns the socket and the ``Live data``
badge a page wears while that socket is up.

Each scenario reads the page after real server state moves — a client attaches, an outlet falls — so one
check on a swapped region proves the state and the swap at once. Where two pages watch one change, both
are read: what one page learned must reach the other, and what belongs to one page, such as an open
Details row, must not reach it.

Two measured boundaries hold this suite back from claims it cannot make. ``ConfigBuilder`` has no muxcon
knob, so federation is proven only as the empty state (ST-04). And ``ConsoleSession.set_offline()``
blocks fetches but cannot drop an open WebSocket, so the stale red badge stays with its unit test in
``tests/test_web_console_live_badge.py``; these scenarios prove the green half instead. The anonymous
refusal of ``/status`` belongs to AUTH-01, so ST-06 covers only the fragment endpoint that the page calls.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from .harness import TEST_PASSWORD, Api, HttpSession
from .story import step, surfaces

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_browser]

#: The four cards of the summary region, in the order the template writes them.
CARDS = ("Total Ports", "Active/Connected", "Federation Connections", "Multipath Groups")

#: The Clients cells of a port row, which leads with the count and then names each client. ``status_content.html.j2``
#: writes it as the ninth cell: toggle, name, description, device, speed, origin, status, power, clients, actions.
COL_CLIENTS = 8

#: The sentence the federation region shows when the server reports no multipath peer.
FEDERATION_EMPTY = "No federation connections"

#: The tooltip ``layout.js`` writes on the badge of a page whose own site socket is up.
LIVE_TITLE = "Live data: updates over WebSocket, no reload"

#: The start of a holder label: the short id the Port knows a console by, then who that console is.
_HOLDER = re.compile(r"^\[([^]]+)\]\s+(\S+)")

#: The columns of the Clients table, in the order the template writes its header row.
CLIENT_COLUMNS = ("id", "user", "ip", "type", "port", "created", "last_seen")


def _status_world(
    servers: Any,
    name: str,
    *,
    seats: str = "multiple",
    telnet_auth: bool = False,
    pdu: Optional[str] = None,
    feeds: Sequence[str] = (),
) -> Any:
    """One server with two ports: ``loop1``, which every scenario acts on, and ``cat1``, which nobody joins.

    Args:
        seats: ``write_slot_capacity`` of ``loop1``, default ``multiple`` so several consoles share it.
        telnet_auth: Require a signed-in Telnet login, which is the only way a Telnet console arrives as a
            named user on a port. A listener without it must not name a port (LFC-06).
        pdu: Name of a dummy PDU to create, with outlets ``1`` and ``2``, for a power scenario.
        feeds: The power feeds of ``loop1``, for example ``("rack1.1",)``.
    """

    def configure(builder: Any) -> Any:
        if pdu is not None:
            builder.with_dummy_pdu(pdu, ["1", "2"], poll_interval=0)
        builder.with_loopback("loop1", write_slot_capacity=seats, feeds=list(feeds))
        return builder

    return servers(
        name=name,
        loopback=(),
        command_ports=("cat1",),
        configure=configure,
        telnet_kwargs=({"require_auth": True} if telnet_auth else None),
        start=True,
    )


# --- reading the status page --------------------------------------------------------------
#
# Every reader here rebuilds its answer from the live document on each call, and no scenario keeps a row
# handle. The swap replaces the whole ``<tbody>``, so anything bound to the old nodes is stale the moment
# a client attaches.


def _port_rows(page: Any) -> List[Dict[str, Any]]:
    """Every port row in the order the page shows it, with its cells and the state of its detail row.

    A main row and its detail row are siblings in one ``<tbody>``, so a scenario that read only the cells
    would mix them. ``detail_open`` asks the inline style rather than the browser: the page hides a closed
    detail row with ``display: none`` and leaves it in the document, which is why ``status.js`` restores an
    open one by writing that style back after a swap.
    """
    raw = page.page.evaluate(
        """() => Array.from(document.querySelectorAll('#portsTbody tr[data-port]')).map((tr) => {
               const text = (el) => (el ? (el.textContent || '').replace(/\\s+/g, ' ').trim() : '');
               const button = tr.querySelector('button.toggleInfo');
               const target = button ? button.getAttribute('data-target') : null;
               const detail = target ? document.getElementById(target) : null;
               const dot = tr.querySelector('.power-dot');
               return {
                 port: tr.getAttribute('data-port'),
                 cells: Array.from(tr.querySelectorAll('td')).map((td) => text(td)),
                 toggle: button ? text(button) : null,
                 detail_open: !!detail && detail.style.display === 'table-row',
                 detail_text: text(detail),
                 power_class: dot ? dot.className : '',
                 power_title: dot ? dot.getAttribute('title') || '' : '',
                 power_text: dot ? text(dot.closest('td')) : text(tr.querySelectorAll('td')[7]),
               };
             })"""
    )
    return list(raw or [])


def _row(page: Any, port: str) -> Dict[str, Any]:
    """One port row by its ``data-port``, which the server stamps and a scenario cannot mix up."""
    rows = {str(entry.get("port") or ""): entry for entry in _port_rows(page)}
    assert port in rows, f"{page.surface}: {port} is not among the port rows: {sorted(rows)}"
    return rows[port]


def _client_count(row: Dict[str, Any]) -> Optional[int]:
    """The count a port row leads its Clients cell with, or ``None`` when the cell holds none."""
    match = re.match(r"(\d+)", str(row["cells"][COL_CLIENTS]))
    return int(match.group(1)) if match else None


def _wait_client_count(page: Any, port: str, count: int, timeout: float = 20.0) -> Dict[str, Any]:
    """Wait until one port row counts ``count`` clients, and return that row as the swap left it.

    A client arriving or leaving pokes every status page with ``status_change``, which makes it rebuild this
    table, so the count moving is the proof that the region was replaced. The row is read again after the
    wait, because the node the predicate matched on is gone.
    """
    page.wait_until(
        lambda: _client_count(_row(page, port)) == count,
        timeout=timeout,
        what=f"{page.surface}: the Clients count of {port} reached {count}",
    )
    return _row(page, port)


def _sort_links(page: Any) -> Dict[str, Dict[str, Any]]:
    """The sortable headers by column title: the link each offers, whether it is active, and its arrow.

    ``status_content.html.j2`` builds these links, so a header's ``href`` is the claim worth reading: the
    browser follows it and the server answers with a reordered table. The macro writes the arrow as the
    *current* direction and the link as the *next* one, so ``arrow`` and the ``dir`` of ``href`` disagree
    on purpose, and a scenario reads both. The arrow lives inside the link, so the title is read from its
    own span instead of the link's text.
    """
    raw = page.page.evaluate(
        """() => Array.from(document.querySelectorAll('table.table-compact thead a.sort-link')).map((a) => {
               const indicator = a.querySelector('.sort-indicator');
               const title = a.querySelector('span:not(.sort-indicator)');
               return {
                 title: title ? (title.textContent || '').trim() : '',
                 arrow: indicator ? (indicator.textContent || '').trim() : '',
                 href: a.getAttribute('href') || '',
                 active: a.classList.contains('active'),
               };
             })"""
    )
    return {str(entry.get("title") or ""): entry for entry in raw or []}


def _query_of(href: str) -> Tuple[str, str]:
    """The ``(sort, dir)`` pair one sort link carries, or an empty pair when it carries none."""
    match = re.search(r"[?&]sort=([^&]+)&dir=([^&]+)", href or "")
    return (match.group(1), match.group(2)) if match else ("", "")


def _cards(page: Any) -> Dict[str, str]:
    """The four summary cards as ``{label: value}``, ignoring cards another region carries."""
    return {card["label"]: card["value"] for card in page.metric_cards() if card["label"] in CARDS}


def _client_rows(page: Any) -> Dict[str, Dict[str, str]]:
    """The Clients table by client id, so a scenario names the row it means."""
    rows: Dict[str, Dict[str, str]] = {}
    for cells in page.table_rows("#clientsTbody"):
        text = [str(cell) for cell in cells]
        if len(text) >= len(CLIENT_COLUMNS):
            rows[text[0]] = dict(zip(CLIENT_COLUMNS, text))
    return rows


def _holder_label(label: str) -> Tuple[str, str]:
    """Split one ``view holders`` label into its short id and the console it names.

    A label reads ``[a1b2c9d4] rw@127.0.0.1 (rw)``, where the bracketed part is the tail of that console's
    client id. It is how a text surface names a console that the Clients table lists under a longer id, so
    a scenario can tie a table row to the console the Port itself reports.
    """
    match = _HOLDER.match(label.strip())
    assert match, f"{label!r} is not a holder label in the documented shape"
    return match.group(1), match.group(2)


def _served_ports(body: str) -> List[str]:
    """The ports one ``ports`` fragment carries, in the order it carries them."""
    return re.findall(r'<tr data-port="([^"]+)"', body or "")


def _fragments(api: Any, query: str = "") -> Dict[str, str]:
    """The live regions of the status page, keyed by region name, from ``/api/status/fragments``.

    The endpoint wraps the four regions in one ``fragments`` key, which is the object ``status.js`` reads,
    and honours the ``sort``/``dir`` query so a swapped table keeps the order the operator chose.
    """
    payload = api.json(f"/api/status/fragments{query}")
    body = payload.get("fragments") if isinstance(payload, dict) else None
    assert isinstance(body, dict), payload
    return {str(name): str(markup) for name, markup in body.items()}


def _power_outlet(api: Any, pdu_name: str, ref: str) -> Dict[str, Any]:
    """One outlet of one PDU, as the power API reports it."""
    pdus = {str(pdu.get("name") or pdu.get("id")): pdu for pdu in api.power().get("pdus", [])}
    assert pdu_name in pdus, sorted(pdus)
    outlets = {str(outlet.get("ref")): outlet for outlet in pdus[pdu_name].get("outlets", [])}
    assert ref in outlets, sorted(outlets)
    return outlets[ref]


# --- scenarios ----------------------------------------------------------------------------


@surfaces("web:rw", "cli:rw", "http")
def test_st01_status_cards_and_sort_links_say_what_the_server_says(
    servers: Any, console: Any, clients: Any, api_for_server: Any
) -> None:
    """The cards and the sortable columns of the status page agree with the server"""
    server = _status_world(servers, "st01")
    page = console.user("web:rw", server, user="rw")
    page.goto("/status")
    api = api_for_server(server)

    with step("The summary cards carry the counts the status API reports"):
        ports = api.status().get("ports") or []
        assert sorted(str(entry.get("name")) for entry in ports) == ["cat1", "loop1"], ports
        assert _cards(page) == {
            "Total Ports": "2",
            "Active/Connected": "2",
            "Federation Connections": "0",
            "Multipath Groups": "0",
        }, _cards(page)

    with step("Six headers are sort links, and the page opens sorted by Name descending"):
        links = _sort_links(page)
        assert sorted(links) == ["Clients", "Description", "Device", "Name", "Origin", "Status"], links
        assert links["Name"]["active"], links
        assert {title: _query_of(entry["href"]) for title, entry in links.items()} == {
            "Name": ("name", "desc"),
            "Description": ("description", "asc"),
            "Device": ("device", "asc"),
            "Origin": ("origin", "asc"),
            "Status": ("status", "asc"),
            "Clients": ("clients", "asc"),
        }, {title: entry["href"] for title, entry in links.items()}

    with step("The arrow marks the active column only, and a client arriving moves the Clients count"):
        cli = clients.cli(user="rw", surface="cli:rw", server=server)
        cli.attach(port="loop1")
        _wait_client_count(page, "loop1", 1)
        links = _sort_links(page)
        assert links["Name"]["active"] and links["Name"]["arrow"] == "↑", links["Name"]
        for title in ("Status", "Clients"):
            assert not links[title]["active"], links[title]
            assert links[title]["arrow"] == "", links[title]

    with step("Following a sort link reorders the table by the count the server holds"):
        page.goto("/status?sort=clients&dir=desc")
        assert [entry["port"] for entry in _port_rows(page)] == ["loop1", "cat1"], _port_rows(page)
        clients_link = _sort_links(page)["Clients"]
        assert clients_link["active"] and clients_link["arrow"] == "↓", clients_link
        assert _query_of(clients_link["href"]) == (
            "clients",
            "asc",
        ), f"the arrow says descending, so the link must offer the flip back: {clients_link}"
        page.goto("/status?sort=clients&dir=asc")
        assert [entry["port"] for entry in _port_rows(page)] == ["cat1", "loop1"], _port_rows(page)

    with step("The chosen order survives a live swap, which replaces the whole table"):
        page.goto("/status?sort=clients&dir=desc")
        assert [entry["port"] for entry in _port_rows(page)] == ["loop1", "cat1"], _port_rows(page)
        cli.close()
        _wait_client_count(page, "loop1", 0)
        assert [entry["port"] for entry in _port_rows(page)] == ["loop1", "cat1"], _port_rows(page)

    with step("A column name the server does not know is answered as Name ascending"):
        page.goto("/status?sort=bogus&dir=sideways")
        assert [entry["port"] for entry in _port_rows(page)] == ["cat1", "loop1"], _port_rows(page)
        links = _sort_links(page)
        assert links["Name"]["active"] and links["Name"]["arrow"] == "↑", links
        assert _query_of(links["Name"]["href"]) == ("name", "desc"), links


@surfaces("web:rw", "web:viewer", "cli:rw")
def test_st02_an_open_port_detail_survives_a_live_swap(servers: Any, console: Any, clients: Any) -> None:
    """A Details row an operator opened stays open across a swap that rebuilds the table"""
    server = _status_world(servers, "st02")
    page = console.user("web:rw", server, user="rw")
    other = console.user("web:viewer", server, user="ro")
    page.goto("/status")
    other.goto("/status")

    with step("The detail row shows nothing until its own row asks for it"):
        first = _row(page, "loop1")
        assert first["detail_open"] is False and first["toggle"] == "+", first
        assert page.is_visible("#info-ports-loop1") is False

    with step("Opening it shows the adapter behind the port, which the row itself never names"):
        page.click('button.toggleInfo[data-target="info-ports-loop1"]')
        opened = _row(page, "loop1")
        assert opened["detail_open"] and opened["toggle"] == "-", opened
        assert page.is_visible("#info-ports-loop1") is True
        assert "Adapter" in opened["detail_text"] and "loopback" in opened["detail_text"], opened["detail_text"]

    with step("A second page keeps its own choice open instead of borrowing the first page's"):
        other.click('button.toggleInfo[data-target="info-ports-cat1"]')
        assert _row(other, "cat1")["detail_open"], _row(other, "cat1")
        assert _row(other, "loop1")["detail_open"] is False, _row(other, "loop1")

    with step("A client attaching rebuilds the table under both pages, and each keeps its own row open"):
        cli = clients.cli(user="rw", surface="cli:rw", server=server)
        cli.attach(port="loop1")
        rebuilt = _wait_client_count(page, "loop1", 1)
        assert rebuilt["detail_open"], f"the swap lost a row an operator had opened: {rebuilt}"
        assert rebuilt["toggle"] == "-" and "loopback" in rebuilt["detail_text"], rebuilt
        assert _row(page, "cat1")["detail_open"] is False, _row(page, "cat1")
        other.wait_until(
            lambda: _client_count(_row(other, "cat1")) == 0,
            what=f"{other.surface}: its own table was rebuilt",
        )
        kept = _row(other, "cat1")
        assert kept["detail_open"] and "command" in kept["detail_text"], kept
        assert "loopback" not in kept["detail_text"], kept
        assert _row(other, "loop1")["detail_open"] is False, _row(other, "loop1")

    with step("An operator who closed a row is not shown it again by the next swap"):
        page.click('button.toggleInfo[data-target="info-ports-loop1"]')
        assert _row(page, "loop1")["toggle"] == "+", _row(page, "loop1")
        cli.close()
        closed = _wait_client_count(page, "loop1", 0)
        assert closed["detail_open"] is False and closed["toggle"] == "+", closed


@surfaces("http", "cli:rw", "telnet:ro", "web:admin", "web:holder", "web:viewer")
def test_st03_the_clients_table_names_each_transport(servers: Any, console: Any, clients: Any, api_for_server: Any) -> None:
    """One Clients table says which transport, user, and port each console arrived on"""
    server = _status_world(servers, "st03", telnet_auth=True)
    admin = console.user("web:admin", server, user="admin")
    viewer = console.user("web:viewer", server, user="ro")
    admin.goto("/status")

    with step("Three consoles join one port over three transports"):
        cli = clients.cli(user="rw", surface="cli:rw", server=server)
        cli.attach(port="loop1")
        telnet = clients.telnet(user="ro", surface="telnet:ro", server=server, require_auth=True)
        telnet.choose_port("loop1")
        # Its own surface name: one surface is one browser context, and a second page in that context
        # would already be signed in and never meet the login form.
        holder = console.console("web:holder", server, "loop1", user="admin")
        joined = holder.wait_for_view("(v) => v.connected === true", timeout=25.0)
        assert joined.get("port") == "loop1", joined
        counted = _wait_client_count(admin, "loop1", 3, timeout=25.0)

    with step("The port's own holders list names each console that holds a Write slot"):
        labels = [str(label) for label in cli.query_holders().get("holders") or []]
        pairs = [_holder_label(label) for label in labels]
        assert sorted(who.split("@")[0] for _, who in pairs) == [
            "admin",
            "rw",
        ], f"the two write-entitled consoles hold a slot and the read-only console does not: {labels}"
        short_of_admin = next(short for short, who in pairs if who.startswith("admin@"))

    with step("Each of the three rows carries the console and transport that console really used"):
        listed = _client_rows(admin)
        joined = {key: row for key, row in listed.items() if not key.startswith("session:")}
        assert len(joined) == 3, listed
        by_user: Dict[str, List[Dict[str, str]]] = {}
        for row in joined.values():
            assert row["port"] == "loop1", f"{row['id']} joined loop1 in this scenario: {row}"
            by_user.setdefault(row["user"], []).append(row)
        assert sorted(by_user) == ["admin", "ro", "rw"], joined
        cli_row = by_user["rw"][0]
        assert cli_row["type"] == "tcp", f"a console on the CLI listener arrived over TCP: {cli_row}"
        assert cli_row["ip"], f"a TCP console has a peer address to show: {cli_row}"
        ws_rows = [row for row in by_user["admin"] if row["id"].endswith(short_of_admin)]
        assert len(ws_rows) == 1, (
            f"the Clients row whose id ends with the short id {short_of_admin!r} that the CLI reported "
            f"should be the web console: {joined}"
        )
        ws_row = ws_rows[0]
        assert ws_row["id"].startswith("ws:") and ws_row["type"] == "websocket", ws_row
        assert ws_row["ip"], f"a web console has an address to show: {ws_row}"
        telnet_row = by_user["ro"][0]
        assert telnet_row["id"].startswith("telnet:telnet1:"), telnet_row
        assert telnet_row["type"] == "telnet", (
            "the Telnet listener knows this console's transport, so the Clients table must name telnet "
            f"instead of guessing websocket: {telnet_row}"
        )
        assert telnet_row["ip"], f"a Telnet console has a peer address to show: {telnet_row}"
        assert counted["cells"][COL_CLIENTS].startswith("3 "), counted["cells"][COL_CLIENTS]

    with step("The status API answers with the transport and port each row shows"):
        served = {str(entry["client_id"]): entry for entry in api_for_server(server).status().get("web_clients", [])}
        for client_id, row in joined.items():
            entry = served[client_id]
            assert str(entry.get("type") or "") == row["type"], (client_id, entry, row)
            assert str(entry.get("port") or "") == row["port"], (client_id, entry, row)

    with step("Each signed-in page is its own session row, and holds no port"):
        sessions = {key: row for key, row in listed.items() if key.startswith("session:")}
        assert len(sessions) == 3, f"one row per signed-in page of this scenario: {sessions}"
        assert sorted(row["user"] for row in sessions.values()) == ["admin", "admin", "ro"], sessions
        for row in sessions.values():
            assert row["type"] == "session", row
            assert row["port"] == "", f"a login session holds no port: {row}"
            assert row["created"] and row["last_seen"], f"a session row says when it signed in and spoke: {row}"

    with step("A read-only operator reads the same transports for the same consoles"):
        viewer.goto("/status")
        viewer.wait_until(
            lambda: any(key.startswith("telnet:telnet1:") for key in _client_rows(viewer)),
            timeout=25.0,
            what=f"{viewer.surface}: the Clients table lists the Telnet console",
        )
        seen = _client_rows(viewer)
        for client_id, row in joined.items():
            assert client_id in seen and seen[client_id]["type"] == row["type"], (client_id, seen.get(client_id))

    with step("The port row labels each client, so an operator need not open the Clients table"):
        preview = _row(admin, "loop1")["cells"][COL_CLIENTS]
        for user in ("rw", "ro", "admin"):
            assert user in preview, preview
        assert "tcp@" in preview and "websocket@" in preview, preview
        assert (
            "telnet@" in preview
        ), f"the preview labels every console with its transport, so a Telnet console is never a bare name: {preview}"


@surfaces("http", "web:rw", "cli:rw")
def test_st04_the_federation_region_has_no_peers_and_says_so(
    servers: Any, console: Any, clients: Any, api_for_server: Any
) -> None:
    """With no multipath peer, the federation region stays empty and the page says so"""
    server = _status_world(servers, "st04")
    page = console.user("web:rw", server, user="rw")
    page.goto("/status")
    api = api_for_server(server)

    with step("The server reports no multipath group and no federation connection"):
        payload = api.status()
        assert payload.get("multipath", {}).get("groups") == [], payload.get("multipath")
        federation = payload.get("federation") or {}
        assert federation.get("connections") == [], federation
        assert (federation.get("totals") or {}).get("connections_total") == 0, federation

    with step("The region names itself, says there is nothing, and shows no peer card"):
        assert page.is_visible("#federationArea") is True
        text = page.page_text("#federationArea")
        assert "Federation by Multipath Peer" in text, text
        assert FEDERATION_EMPTY in text, text
        page.expect_absent("#federationArea .peer-card")

    with step("The two federation cards count zero, as the API does"):
        cards = _cards(page)
        assert cards["Federation Connections"] == "0" and cards["Multipath Groups"] == "0", cards

    with step("The fragment carries the same sentence, so a live update cannot invent a peer"):
        body = _fragments(api)["federation"]
        assert FEDERATION_EMPTY in body, body
        assert "peer-card" not in body, body

    with step("A console joining a port refreshes the page and the region still says there is no peer"):
        cli = clients.cli(user="rw", surface="cli:rw", server=server)
        cli.attach(port="loop1")
        _wait_client_count(page, "loop1", 1)
        assert FEDERATION_EMPTY in page.page_text("#federationArea"), page.page_text("#federationArea")
        page.expect_absent("#federationArea .peer-card")
        assert _cards(page)["Multipath Groups"] == "0", _cards(page)


@surfaces("web:admin", "web:rw", "http")
def test_st05_a_power_cell_follows_an_outlet_on_every_open_page(servers: Any, console: Any, api_for_server: Any) -> None:
    """When a feed falls, every open status page tints that port's power cell the same way"""
    server = _status_world(servers, "st05", pdu="rack1", feeds=("rack1.1",))
    first = console.user("web:admin", server, user="admin")
    second = console.user("web:rw", server, user="rw")
    first.goto("/status")
    second.goto("/status")
    api = api_for_server(server)

    with step("Both pages hold their own live socket, which is what carries the poke"):
        for page in (first, second):
            badge = page.live_badge()
            assert badge["shown"] and badge["state"] == "live", badge
            assert badge["title"] == LIVE_TITLE, badge
            assert badge["text"] == "● Live data", badge

    with step("The fed port carries a healthy cell and its tooltip, the unfed port a dash"):
        fed = _row(first, "loop1")
        assert "power-dot" in fed["power_class"] and fed["power_class"].endswith("all"), fed
        assert "1/1" in fed["power_title"] and "rack1.1" in fed["power_text"], fed
        unfed = _row(first, "cat1")
        assert unfed["power_class"] == "" and unfed["power_text"] == "—", unfed
        assert _row(second, "loop1")["power_class"].endswith("all"), _row(second, "loop1")

    with step("One outlet is switched off over REST, with no console on this PDU to push a port event"):
        api.set_outlet("rack1.1", False)
        assert _power_outlet(api, "rack1", "rack1.1")["on"] is False, api.power()

    with step("Each page tints the cell for the dead feed and says that port lost all power"):
        for page in (first, second):
            page.wait_until(
                lambda: _row(page, "loop1")["power_class"].endswith("none"),
                timeout=20.0,
                what=f"{page.surface}: the power cell of loop1 went to none",
            )
            row = _row(page, "loop1")
            assert "0/1" in row["power_title"] and "ALL POWER LOST" in row["power_title"], row
            assert "off" in row["power_text"], row
            assert page.live_badge()["state"] == "live", page.live_badge()
            assert _row(page, "cat1")["power_class"] == "", _row(page, "cat1")

    with step("The page agrees with the power API about both outlets of that PDU"):
        assert _power_outlet(api, "rack1", "rack1.1")["on"] is False, api.power()
        assert _power_outlet(api, "rack1", "rack1.2")["on"] is True, api.power()

    with step("Switching the feed back restates the healthy cell on both pages"):
        api.set_outlet("rack1.1", True)
        for page in (first, second):
            page.wait_until(
                lambda: _row(page, "loop1")["power_class"].endswith("all"),
                timeout=20.0,
                what=f"{page.surface}: the power cell of loop1 came back healthy",
            )
            assert "1/1" in _row(page, "loop1")["power_title"], _row(page, "loop1")


@surfaces("http", "web:rw", "cli:rw")
def test_st06_the_status_fragments_are_regions_the_page_swaps(
    servers: Any, console: Any, clients: Any, api_for_server: Any
) -> None:
    """/api/status/fragments returns the four regions, in the shape the page swaps them with"""
    server = _status_world(servers, "st06")
    page = console.user("web:rw", server, user="rw")
    page.goto("/status")
    api = api_for_server(server)

    with step("The endpoint answers with exactly the four region names the page swaps"):
        fragments = _fragments(api)
        assert sorted(fragments) == ["clients", "federation", "ports", "summary"], sorted(fragments)
        for name, body in fragments.items():
            assert isinstance(body, str) and body.strip(), name

    with step("A fragment is a region's contents only, so a container is never swapped twice"):
        assert '<tr data-port="' in fragments["ports"], fragments["ports"][:300]
        for name, container in (
            ("ports", "portsTbody"),
            ("clients", "clientsTbody"),
            ("summary", "statusSummary"),
            ("federation", "federationArea"),
        ):
            assert f'id="{container}"' not in fragments[name], f"{name}: the page would replace its own container"
        assert "sort-link" not in fragments["ports"], "the header stays put while the rows under it are replaced"
        assert 'class="cards"' in fragments["summary"], fragments["summary"][:300]
        assert "<h2" in fragments["federation"], fragments["federation"][:300]

    with step("The endpoint honours the sort each header links for, in both directions"):
        assert _served_ports(_fragments(api, "?sort=name&dir=desc")["ports"]) == ["loop1", "cat1"]
        assert _served_ports(_fragments(api, "?sort=name&dir=asc")["ports"]) == ["cat1", "loop1"]

    with step("A signed-in cookie answers for the fetch the page itself makes, and an anonymous one is challenged"):
        web = HttpSession(server.web_url, surface="http")
        status, _, body = web.login("rw", TEST_PASSWORD)
        assert status in (200, 302), (status, body[:200])
        status, _, body = web.get("/api/status/fragments")
        assert status == 200, (status, body[:200])
        assert sorted(json.loads(body)["fragments"]) == ["clients", "federation", "ports", "summary"], body[:200]
        anonymous = Api(server.web_url, username=None, password=None)
        status, headers, body = anonymous.get("/api/status/fragments", expect=(401,))
        assert status == 401, body
        assert "www-authenticate" in {key.lower() for key in headers}, headers
        assert "loop1" not in body and "cat1" not in body, body

    with step("What an open page shows is what the endpoint serves for the same query"):
        cli = clients.cli(user="rw", surface="cli:rw", server=server)
        cli.attach(port="loop1")
        _wait_client_count(page, "loop1", 1)
        # The page carries no sort query, and status.js asks for the fragments with its own query string,
        # so the plain endpoint answer is what the page just swapped in.
        served = _fragments(api)
        shown = [entry["port"] for entry in _port_rows(page)]
        assert shown == _served_ports(served["ports"]) == ["cat1", "loop1"], (shown, served["ports"][:300])
        clients_text = page.page_text("#clientsTbody")
        for client_id in _client_rows(page):
            assert client_id in clients_text, client_id
            assert client_id in served["clients"], f"{client_id} is on the page but not in the fragment it swaps in"
