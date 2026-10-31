"""The timeline recorder, and the one story it is there to record (TIM-01..TIM-06).

TIM-01 to TIM-05 read nothing but the recorder, and no server starts. They exist because the renderer is
the only place a control byte or a Telnet negotiation becomes readable text, and it drifted twice: once
into Unicode pictures that a plain terminal shows as boxes, and once into the wrong RFC 854 values, which
turned the listener's ``IAC WILL ECHO`` into ``IAC 1``.

TIM-06 is the reason the recorder exists: one operator's shift, end to end, in one server process. Every
other suite in this tier proves one surface of one event. A person meets the product as a sequence, so
this scenario runs the whole of one -- a PDU added in the browser, the reload that page runs by itself, an
open console gaining the feed it had been waiting for, a Write slot taken by another console, a feed
falling while both terminals watch, and the audit lines the shift must leave behind. One timeline carries
the numbered steps of that shift in order, which is what ``story`` and ``timeline`` are for.

The scenario quotes nothing this file writes. The Write-slot wording comes from ``access_decision``, the
text-surface power notice from ``power_command.format_power_notice``, the web notices from ``console.js``,
the editor's status line from ``config_editor.js``, and the audit lines from ``PduAdapter._audit_control``.
Only one fact is quoted rather than followed: the PDU is switched through REST, because the same switch
made from a console session adds ``; client <id>`` to the audit line, and that half belongs to ``PWR-19``.

Two measured boundaries worth knowing before reading the steps. A refusal to promote names its reason on
the reply frame (``rw_holders``) and only its own sentence on the terminal, so the scenario asserts the
holder from the frame and the sentence from the module. And the editor's page-level Save writes nothing
when pressed in the same tick as accepting a row -- the page then reports "Loaded current config" -- so
the scenario lets the row land in the page's own table first, which is what an operator does.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from openmux.server.access_decision import Code, Mode, decide, notice

from .conftest import DEFAULT_USERS
from .story import Timeline, render_bytes, step, surfaces

pytestmark = [pytest.mark.e2e]

#: What the Telnet listener writes before the password prompt (``telnet_listener._IAC_WILL_ECHO``).
IAC_WILL_ECHO = bytes([255, 251, 1])


@surfaces("recorder")
def test_tim01_control_bytes_use_caret_notation_and_a_trailing_line_ending_is_dropped():
    """A timeline line shows control bytes as caret notation and omits the line ending that closes it."""
    # A record is one line, so the line ending that ends a payload is framing, not content.
    assert render_bytes(b"after-full-reload\r\n") == "after-full-reload"
    # A control byte inside a payload stays visible, or the reader cannot see where one line stops.
    assert render_bytes(b"hi\r\nthere\n") == "hi^M^Jthere"
    # A payload that is only a line ending is still the event under test, so it is never an empty record.
    assert render_bytes(b"\r\n") == "^M^J"
    assert render_bytes(b"") == "<empty>"
    # Caret notation is ASCII: it survives a terminal with no font for Unicode control pictures.
    assert render_bytes(b"\x1b[A") == "^[[A"
    assert render_bytes(b"\x00") == "^@"
    assert all(char < "\x7f" for char in render_bytes(b"\x00\x07\x0d\x1b\x7f"))


@surfaces("recorder")
def test_tim02_telnet_negotiation_is_named_by_the_rfc854_values():
    """The listener's ``IAC WILL ECHO`` reaches the timeline by name, with its option named."""
    assert render_bytes(IAC_WILL_ECHO) == "IAC WILL ECHO"
    assert render_bytes(IAC_WILL_ECHO + b"Password: ") == "IAC WILL ECHO Password: "
    assert render_bytes(bytes([255, 252, 1]) + b"\r\n") == "IAC WONT ECHO"
    # An option the harness does not know shows as a byte, never as a bare decimal that reads as text.
    assert render_bytes(bytes([255, 253, 42])) == "IAC DO 0x2a"
    # Two IAC bytes carry a literal 0xFF data byte, which is not a command.
    assert render_bytes(b"\xff\xffA") == "\\xff\\xffA"


@surfaces("recorder")
def test_tim03_a_command_cut_short_by_the_chunk_end_is_marked_not_guessed():
    """A split negotiation says it was truncated, because guessing would invent an option."""
    assert render_bytes(bytes([255, 251])) == "IAC WILL <truncated>"
    # The option byte of a full command is protected even when it is a line-ending byte.
    assert render_bytes(bytes([255, 251, 13])) == "IAC WILL 0x0d"


@surfaces("recorder")
def test_tim04_one_record_is_always_one_line():
    """A payload with embedded newlines still writes a single timeline record."""
    timeline = Timeline(path=None, echo=False)
    timeline.record("cli:holder", "out", b"first\r\nsecond\r\n")
    lines = timeline.lines()
    assert len(lines) == 1, f"a payload wrote {len(lines)} records, which breaks a reader's line order"
    assert "first^M^Jsecond" in lines[0]


@surfaces("recorder")
def test_tim05_a_long_payload_is_cut_on_the_line_and_kept_whole_beside_it(tmp_path):
    """A capped line points at the full payload, written next to ``timeline.log``."""
    timeline = Timeline(path=tmp_path / "timeline.log", echo=False)
    payload = b"x" * (4 * 400)
    timeline.record("cli:big", "out", payload)
    (line,) = timeline.lines()
    assert "…" in line, "a long payload was not capped, so one record would wrap in a terminal"
    assert "raw=raw-cli-big.bin@0" in line, f"the capped line lost its pointer to the full payload: {line}"
    spilled = tmp_path / "raw-cli-big.bin"
    assert spilled.exists(), f"{spilled} was never written, so the capped part of the payload is lost"
    assert spilled.read_text(encoding="utf-8").strip() == "x" * len(payload)


# ===============================================================================
# TIM-06: one operator's shift, end to end, in one server process
# ===============================================================================

#: The story's one console. It is configured with a feed and no PDU behind that feed, which is the
#: state an operator reaches by naming a feed before writing the ``power:`` section. Measured: the Port
#: starts anyway, ``GET /api/power`` answers 404, and no log line names the missing PDU.
PORT = "solo"
PDU = "rack1"
FEED = "rack1.1"

#: The editor's own report, from ``saveConfig()`` in ``config_editor.js``. The page's Save button posts
#: the draft and then runs a soft reload itself, so one press is the whole write.
EDITOR_SAVED = "Saved; Soft reload completed"

#: What ``console.js`` writes into the terminal when the port's last feed falls and when one returns.
WEB_ALL_LOST = "[POWER WARNING: all power feeds are OFF - 1 feed(s) down]"
WEB_RESTORED = "[POWER restored: 1/1 feed(s) on]"
#: The same two events for an attached text session, both from ``power_command.format_power_notice``.
TEXT_ALL_LOST = f"[POWER WARNING] all power feeds are now OFF for this console ({FEED})"
TEXT_FEED_ON = f"[POWER] feed {FEED} is now on"

#: One administrator, because only that account may open the editor (CFG-01), and two write-entitled
#: accounts, so the Write slot changes hands between two names a notice can quote.
STORY_USERS = tuple(DEFAULT_USERS) + (("user2", "read-write"),)


def _story_world(servers: Any, name: str) -> Any:
    """One server whose only console waits for a PDU its config does not hold yet.

    No Telnet listener: nobody in this story dials one, and a listener nobody uses only adds a line to
    the cast.
    """
    return servers(
        name=name,
        users=STORY_USERS,
        loopback=(),
        telnet=False,
        configure=lambda builder: builder.with_loopback(PORT, write_slot_capacity="one", feeds=[FEED]),
        start=True,
    )


def _outlet(snapshot: Dict[str, Any], ref: str) -> Dict[str, Any]:
    """One outlet of a ``/api/power`` snapshot, found by the ref a console names in its ``power`` list."""
    for pdu in snapshot.get("pdus") or []:
        for outlet in pdu.get("outlets") or []:
            if str(outlet.get("ref")) == ref:
                return dict(outlet)
    raise AssertionError(f"{ref} is no outlet of {snapshot}")


def _open_view(page: Any, view: str) -> None:
    """Open one Config Editor sub-page and wait for its region.

    This page carries no ``?e2e=1`` hook, so it is read at page level, and it keeps every region in the
    document and hides the others -- hence the wait for the region the query asked for (CFG-02).
    """
    page.goto(f"/config-editor?view={view}")
    page.wait_for_selector(f"#view-{view}", state="visible")


def _rows(page: Any, region: str) -> List[List[str]]:
    """The text of every row of the table in one region, as a person reads the columns left to right."""
    raw = page.page.evaluate(
        """(region) => Array.from(document.querySelectorAll(`${region} tbody tr`)).map((tr) =>
               Array.from(tr.querySelectorAll('td,th')).map((c) => (c.textContent || '').trim()))""",
        region,
    )
    return [[str(cell) for cell in row] for row in raw or []]


def _press_caption(page: Any, region: str, caption: str) -> bool:
    """Press, inside ``region``, the first button whose caption starts with ``caption``.

    Scoped on purpose: an inline row and the page footer both carry a button whose caption starts with
    ``Save``, and ``querySelector`` answers with the first one it meets.
    """
    return bool(
        page.page.evaluate(
            """([region, caption]) => {
                const root = document.querySelector(region);
                if (!root) return false;
                const button = Array.from(root.querySelectorAll('button')).find((b) =>
                    (b.textContent || '').trim().toLowerCase().startsWith(caption.toLowerCase()));
                if (!button) return false;
                button.click();
                return true;
            }""",
            [region, caption],
        )
    )


def _press_id(page: Any, element_id: str) -> bool:
    """Press one control by id, because this page repeats captions and never repeats an id."""
    return bool(
        page.page.evaluate(
            "(id) => { const el = document.getElementById(id); if (!el) return false; el.click(); return true; }",
            element_id,
        )
    )


def _fill_row_field(page: Any, label: str, value: str) -> bool:
    """Type into the inline row's field whose label starts with ``label``, found as CFG-11 finds it."""
    return bool(
        page.page.evaluate(
            """([label, value]) => {
                const panel = document.querySelector('.inline-editor-panel');
                if (!panel) return false;
                for (const field of panel.querySelectorAll('.field')) {
                    const name = ((field.querySelector('label,span') || field).textContent || '').trim();
                    if (!name.toLowerCase().startsWith(label.toLowerCase())) continue;
                    const input = field.querySelector('input,select,textarea');
                    if (!input) continue;
                    input.value = value;
                    input.dispatchEvent(new Event('input', {bubbles: true}));
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }
                return false;
            }""",
            [label, value],
        )
    )


def _status_line(page: Any) -> str:
    """What the editor's status line says, which is where a save reports itself."""
    return str(page.page.evaluate("() => { const s = document.getElementById('status'); return s ? s.innerText : ''; }"))


@pytest.mark.e2e_browser
@surfaces("web:admin", "web:rw", "cli:taker", "http", "server log")
def test_tim06_one_operators_shift_in_one_process(
    servers: Any, console: Any, clients: Any, api_for_server: Any, story: Any
) -> None:
    """One server, one shift: a PDU added in the browser, a seat taken, a feed lost, and both logs written."""
    server = _story_world(servers, "story")
    api = api_for_server(server)
    page = console.console("web:rw", server, PORT, user="rw")

    with step("The console page holds the only Write slot, and has no feed to report yet"):
        page.wait_for_view("(v) => v.connected === true && v.mode === 'read-write'", timeout=20)
        assert page.power_badge()["label"] != "1/1", page.power_badge()
        # A marker of our own, to tell a live update apart from a refresh afterwards: the same window
        # still answers after the reload below, so nothing reloaded this page to make it agree.
        page.page.evaluate("() => { window.__same_document = true; }")
        api.get("/api/power", expect=(404,))

    admin = console.user("web:admin", server, user="admin")
    with step("The editor is opened on a power section the running server does not have"):
        _open_view(admin, "power")
        assert _rows(admin, "#view-power") == [], _rows(admin, "#view-power")
        assert _press_caption(admin, "#view-power", "Add item"), "the PDUs table offers no Add item"
        admin.wait_for_selector(".inline-editor-panel", state="visible")
        assert _fill_row_field(admin, "Name", PDU), "the new PDU row has no Name field"
        assert _press_caption(admin, ".inline-editor-panel", "Save item"), "the inline row has no Save item"

    with step("Accepting the row changes the page only: the server still has no power section"):
        admin.wait_until(
            lambda: any(row and row[0] == PDU for row in _rows(admin, "#view-power")),
            timeout=10,
            what=f"the editor to list {PDU} in its own PDUs table",
        )
        api.get("/api/power", expect=(404,))

    with step("One press of the page's Save button writes the file and reloads the server"):
        dialogs = admin.accept_dialogs(True)
        assert _press_id(admin, "saveBtn"), "the editor page has no Save button"
        admin.wait_until(lambda: EDITOR_SAVED in _status_line(admin), timeout=30, what="the editor to report the save")
        assert dialogs == [], f"the save asked for a confirmation this path does not expect: {dialogs}"

    with step("The running server now has the PDU, and the port's feed is the one the port named"):
        snapshot = api.power()
        assert _outlet(snapshot, FEED)["mapped_ports"] == [PORT], snapshot

    with step("The console page is not reloaded, and still gains the feed it was missing"):
        # The badge follows the Port's status feed, and the server's notice arrives as terminal text, so
        # the page shows the same sentence the CLI session reads -- measured here, not assumed.
        page.wait_for_text(TEXT_FEED_ON, timeout=20)
        page.wait_until(
            lambda: page.power_badge()["label"] == "1/1",
            timeout=20,
            what="the console page to pick the new feed up into its badge",
        )
        assert page.page.evaluate("() => window.__same_document === true"), "the page had to be reloaded to see the feed"
        assert page.mode() == "read-write", page.mode()

    cli = clients.cli(user="user2", surface="cli:taker", server=server)
    with step("A second console arrives on the same Port and comes up read-only"):
        cli.attach(port=PORT)
        assert cli.mode() == "read-only", cli.mode()

    with step("It asks to drive, and the Port says no in the words the access module owns"):
        refused = cli.request_read_write()
        assert refused.get("ok") is False, refused
        assert refused.get("code") == Code.PROMOTE_FAILED.value, refused
        holders = [str(holder) for holder in refused.get("rw_holders") or []]
        assert len(holders) == 1 and "rw@" in holders[0], refused
        for line in list(decide(Code.PROMOTE_FAILED, mode=Mode.READ_ONLY).lines):
            cli.wait_for(str(line), timeout=10)

    with step("It takes the slot, and is told on its own terminal whom it took it from"):
        granted = cli.takeover()
        assert granted.get("ok") is True, granted
        assert granted.get("mode") == "read-write", granted
        named = [str(line) for line in granted.get("lines") or [] if "Taken from" in str(line)]
        assert len(named) == 1 and holders[0] in named[0], granted
        for line in granted.get("lines") or []:
            assert str(line) in cli.read_text(), cli.read_text()[-400:]

    with step("The page that lost the slot says so in the same server wording, on itself"):
        lost = page.wait_for_view("(v) => v.mode === 'read-only'", timeout=20)
        assert "mode-readonly" in str(lost["body_class"]).split(), lost["body_class"]
        page.wait_for_text(notice(Code.DEMOTED, actor="user2").message, timeout=20)
        takeover = server.wait_for_log(f"WRITE-SLOT TAKEOVER port={PORT}", timeout=15)
        assert "taker=user2:" in takeover and "victim=rw:" in takeover, takeover

    with step("One switch takes the Port's last feed away, and both terminals warn"):
        api.set_outlet(FEED, False)
        cli.wait_for(TEXT_ALL_LOST, timeout=20)
        # Two sentences land in the web terminal: the server's notice, which every surface gets alike, and
        # the page's own all-power-lost line, which it re-writes on each status frame while power stays lost.
        page.wait_for_text(TEXT_ALL_LOST, timeout=20)
        page.expect_notice(WEB_ALL_LOST, timeout=20)
        assert page.power_badge()["state"] == "none", page.view()
        assert page.power_badge_title().startswith("ALL POWER LOST"), page.power_badge_title()

    with step("The feed comes back, and both say so with the count that came back"):
        api.set_outlet(FEED, True)
        cli.wait_for(TEXT_FEED_ON, timeout=20)
        page.expect_notice(WEB_RESTORED, timeout=20)
        page.wait_until(lambda: page.power_badge()["label"] == "1/1", timeout=15, what="the badge to read 1/1 again")

    with step("The audit says who switched what, which ports fell, and nothing that did not happen"):
        audit = server.wait_for_log(f"POWER CONTROL: user admin turned {FEED} off", timeout=15)
        assert f"losing all power: {PORT}" in audit, audit
        assert "; client " not in audit, f"a REST switch has no session to record: {audit}"
        recorded = server.wait_for_port_log(PORT, "power_control_notice", timeout=15)
        assert f"outlet={FEED}" in recorded and "state=off" in recorded, recorded

    with step("The shift ends with the PDU in the editor's own table and the Port still fed by it"):
        _open_view(admin, "power")
        assert [row[:2] for row in _rows(admin, "#view-power")] == [[PDU, "dummy"]], _rows(admin, "#view-power")
        assert _outlet(api.power(), FEED)["mapped_ports"] == [PORT], api.power()
