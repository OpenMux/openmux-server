"""WRITE SLOT: who may drive a port, in what words, on every surface (RW-01 to RW-20).

A port grants one thing in five places: attach, request, release, Takeover, and the notice that
follows a Takeover. `access_decision.py` owns the Outcome code and the sentence for every one of
them, and `openmux/README.md` forbids a surface from writing its own wording. So most scenarios in
this file ask the same question on two or three surfaces and compare the answer to the sentence the
product module itself produces. A test string typed by hand would let a surface drift and still pass.

Three measured facts shape these bodies, and each is stated where it is used:

* A plain attach grant is **silent**. The `client_mode` frame for it carries no line, because the
  connect banner already said the client is connected. A scenario reads `mode()` or the page's mode,
  never a frame, to learn what an attach granted.
* The refusal a client gets when it asks to drive depends on the port's Write slot capacity, and one
  function (`ConsoleManager._seat_refusal`) picks it. A `none` port names its capacity, a `multiple`
  port says there is no single seat to take, and a `one` port whose promote failed blames the server.
* A Telnet session's identity is the listener's own name (`telnet_<listener>`) unless the listener
  asks for a login, so write-slot scenarios start their Telnet listener with `require_auth`.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

import pytest

from openmux.server.access_decision import Code, Mode, decide, notice
from openmux.server.adapters.listener_common import TAKE_TARGET_PROMPT

from .conftest import DEFAULT_USERS
from .story import step, surfaces

pytestmark = [pytest.mark.e2e, pytest.mark.feature]

#: The three default accounts plus a second write-entitled one, so a scenario can name two writers.
TWO_WRITERS = tuple(DEFAULT_USERS) + (("user2", "read-write"),)

#: One holder label, as ``Holder.label`` renders it: ``[8 hex] name@ip (rw)``.
HOLDER_LABEL = re.compile(r"^\[[0-9a-f]{8}\] \S+@\S+ \(rw\)$")
#: The short id inside a holder label, which is what a targeted Takeover names.
HOLDER_ID = re.compile(r"^\[([0-9a-f]{8})\]")

#: What the Control menu prints when the Takeover key asks whom to displace. Derived from the
#: product's own prompt, so a reworded prompt is noticed here instead of being re-typed here.
TAKE_PROMPT = TAKE_TARGET_PROMPT.split("(")[0].strip("[] ")

#: A payload big enough to fill a socket that nobody drains, used to stall one viewer on purpose.
STALL_PAYLOAD_BYTES = 512 * 1024


def _lines_for(code: Code, mode: Mode = Mode.READ_ONLY, **fields: Any) -> List[str]:
    """The terminal lines the product itself renders for one Outcome code.

    This is the assertion a drifting surface fails: the expected wording comes from the module that
    owns it, so a scenario never restates a sentence.
    """
    return list(decide(code, mode=mode, **fields).lines)


def _capacity_world(
    servers: Any,
    name: str,
    capacity: str,
    *,
    port: str = "slot1",
    telnet_kwargs: Optional[Dict[str, Any]] = None,
    **knobs: Any,
) -> Any:
    """One server whose only port has the Write slot capacity under test, with two possible writers."""
    return servers(
        name=name,
        users=TWO_WRITERS,
        loopback=(),
        telnet_kwargs=telnet_kwargs,
        configure=lambda builder: builder.with_loopback(port, write_slot_capacity=capacity, **knobs),
        start=True,
    )


def _id_of(label: str) -> str:
    """The short holder id inside a label, which is what ``takeover(target=...)`` matches."""
    match = HOLDER_ID.match(label)
    assert match, f"not a holder label: {label!r}"
    return match.group(1)


def _codes(session: Any) -> List[str]:
    """The Outcome codes this session's `client_mode` frames carried, oldest first."""
    return [str(frame.get("code")) for frame in session.frames("client_mode")]


@surfaces("cli:rw")
def test_rw01_the_first_client_on_a_free_port_holds_the_write_slot(server, clients, story):
    """The first write-entitled client on a free port drives it, and the holders list names that client."""
    with step("the port is free, so the attach grants the write seat"):
        holder = clients.cli(user="rw", attach_to="loop1", surface="cli:holder")
        assert holder.mode() == "read-write", holder.mode()
    with step("it can drive the port, which a Loopback port proves by echoing"):
        holder.echo_of("first-client-drives")
    with step("the holders list carries one label, in the server's own format"):
        reply = holder.query_holders()
        labels = [str(label) for label in reply.get("holders") or []]
        assert len(labels) == 1, labels
        assert HOLDER_LABEL.match(labels[0]), labels[0]
        assert "rw@" in labels[0], labels[0]
        assert int(reply.get("max_rw_users") or 0) == 1, reply
    with step("the grant arrived with no line, because an attach grant is silent"):
        assert _codes(holder) == [], holder.frames()


@surfaces("web:rw", "cli:rw")
def test_rw02_the_second_client_on_a_held_port_is_told_the_seat_is_taken(servers, console, clients, story):
    """A page that opens a port someone else drives comes up read-only and says why, in the server's words."""
    server = _capacity_world(servers, "held", "one")
    holder = clients.cli(user="rw", surface="cli:holder", server=server)
    holder.attach(port="slot1")
    page = console.console("web:ro", server, "slot1", user="rw")
    with step("the page attaches, and the seat it wanted is already held"):
        view = page.wait_for_view("(v) => v.connected === true && v.mode === 'read-only'", timeout=20)
        assert view["port_up"] is True, view
    with step("the page's own frame log says the seat was taken, not that access was refused"):
        page.wait_until(
            lambda: "client_mode:capacity_full" in page.frame_types(), timeout=10, what="the seat-taken answer to arrive"
        )
    with step("the seat-taken answer prints no line, because a held seat is not a refusal"):
        assert _lines_for(Code.CAPACITY_FULL, mode=Mode.READ_ONLY) == []
        for code in (Code.NOT_ENTITLED, Code.DENIED_BY_GROUP_ACL):
            for line in _lines_for(code):
                assert line not in page.text(), f"the page called a held seat {code.value}: {page.text()[-300:]}"
    with step("the driver is unaffected, and its bytes still reach this page"):
        assert holder.mode() == "read-write", holder.mode()
        holder.echo_of("holder-still-drives")
        page.wait_for_text("holder-still-drives", timeout=10)


@surfaces("web:ro", "cli:rw")
def test_rw03_one_holder_label_is_shown_on_every_surface(servers, console, clients, story):
    """The holders list and the web page show the same holder label, built in one place."""
    server = _capacity_world(servers, "label", "one")
    holder = clients.cli(user="rw", surface="cli:holder", server=server)
    holder.attach(port="slot1")
    label = [str(item) for item in holder.query_holders().get("holders") or []][0]
    page = console.console("web:ro", server, "slot1", user="ro")
    with step("the CLI's holders reply is one label in the documented shape"):
        assert HOLDER_LABEL.match(label), label
    with step("the page's read-only menu repeats that exact label after 'Held by'"):
        page.wait_for_view("(v) => v.connected === true", timeout=20)
        page.click("#roIndicator")
        page.wait_until(lambda: "Held by" in page.text_of("#roMenuInfo"), timeout=10, what="the menu to name the holder")
        assert label in page.text_of("#roMenuInfo"), page.text_of("#roMenuInfo")
    with step("a second viewer is told the same label, because the holder belongs to the Port"):
        second = clients.cli(user="ro", surface="cli:second", server=server)
        second.attach(port="slot1")
        assert [str(item) for item in second.query_holders().get("holders") or []] == [label], second.query_holders()


@surfaces("cli:rw")
def test_rw04_a_request_on_a_held_one_slot_port_answers_with_the_servers_failure(servers, clients, story):
    """On a one-seat port whose seat is held, a request is a failed promote, and the server says so plainly."""
    server = _capacity_world(servers, "full", "one")
    holder = clients.cli(user="rw", surface="cli:holder", server=server)
    holder.attach(port="slot1")
    label = [str(item) for item in holder.query_holders().get("holders") or []][0]
    asker = clients.cli(user="rw", surface="cli:asker", server=server)
    asker.attach(port="slot1")
    with step("the second client came up read-only, which is a grant of the lesser standing"):
        assert asker.mode() == "read-only", asker.mode()
    with step("asking for the seat is refused, and the answer is the promote failure"):
        reply = asker.request_read_write()
        assert reply.get("ok") is False, reply
        assert reply.get("code") == Code.PROMOTE_FAILED.value, reply
        assert reply.get("mode") == "read-only", reply
    with step("the terminal prints the line the access module renders for that code"):
        for line in _lines_for(Code.PROMOTE_FAILED):
            asker.wait_for(line, timeout=10)
    with step("the answer names who holds the seat, in the label that holder sees itself"):
        holders = [str(item) for item in reply.get("rw_holders") or []]
        assert holders == [label], reply
    with step("nothing changed for the holder"):
        assert holder.mode() == "read-write", holder.mode()


@surfaces("cli:rw", "telnet:rw", "web:rw")
def test_rw05_one_outcome_code_reads_the_same_sentence_on_every_surface(servers, console, clients, story):
    """The same refusal on three surfaces renders one sentence, which is the point of one wording owner."""
    server = _capacity_world(servers, "parity", "one", telnet_kwargs={"require_auth": True})
    holder = clients.cli(user="rw", surface="cli:holder", server=server)
    holder.attach(port="slot1")
    expected = _lines_for(Code.PROMOTE_FAILED)

    asker = clients.cli(user="rw", surface="cli:asker", server=server)
    asker.attach(port="slot1")
    with step("the CLI listener session prints the module's lines"):
        asker.request_read_write()
        for line in expected:
            asker.wait_for(line, timeout=10)

    tel = clients.telnet(user="rw", surface="telnet:asker", server=server, require_auth=True)
    tel.wait_for_prompt("Port:")
    tel.send_line("slot1")
    tel.wait_for("slot1", timeout=10)
    with step("the Telnet session prints the same lines, framed as text it cannot parse"):
        tel.open_control_menu()
        tel.press("a")
        for line in expected:
            tel.wait_for(line, timeout=20)
        tel_text = tel.text()
        for line in expected:
            assert line in tel_text, tel_text[-400:]

    page = console.console("web:asker", server, "slot1", user="rw")
    with step("and so does the web page, from the same frame it was sent"):
        page.wait_for_view("(v) => v.connected === true && v.mode === 'read-only'", timeout=20)
        page.click("#roIndicator")
        page.click("#roMenuReqRW")
        page.wait_until(
            lambda: all(line in page.text() for line in expected),
            timeout=10,
            what="the page to print the refusal it was sent",
        )


@surfaces("cli:rw")
def test_rw06_releasing_the_seat_lets_the_next_client_take_it(servers, clients, story):
    """Release gives the seat up without leaving the port, and the next request is granted."""
    server = _capacity_world(servers, "release", "one")
    first = clients.cli(user="rw", surface="cli:first", server=server)
    first.attach(port="slot1")
    second = clients.cli(user="rw", surface="cli:second", server=server)
    second.attach(port="slot1")
    with step("the second client is waiting read-only for a seat it cannot take"):
        assert second.request_read_write().get("ok") is False, second.frames()
    with step("the holder releases, which is a grant of read-only and keeps it attached"):
        reply = first.release()
        assert reply.get("code") == Code.RELEASED.value, reply
        assert reply.get("mode") == "read-only", reply
        assert first.mode() == "read-only", first.mode()
        first.wait_for(_lines_for(Code.RELEASED)[0], timeout=10)
    with step("the next client asks again and now gets the seat"):
        granted = second.request_read_write()
        assert granted.get("ok") is True, granted
        assert granted.get("code") == Code.GRANTED.value, granted
        assert second.mode() == "read-write", second.mode()
        second.echo_of("second-drives-now")
    with step("the first client is still on the port, and still sees what it sends"):
        second.echo_of("still-together")
        first.wait_for("still-together", timeout=10)


@surfaces("cli:rw")
def test_rw07_a_client_that_leaves_frees_the_seat_for_the_next_one(servers, clients, story):
    """A session that goes away releases its seat, so a departed client never blocks a port."""
    server = _capacity_world(servers, "leave", "one")
    gone = clients.cli(user="rw", surface="cli:leaving", server=server)
    gone.attach(port="slot1")
    waiter = clients.cli(user="rw", surface="cli:waiting", server=server)
    waiter.attach(port="slot1")
    with step("the seat is held, so the waiter cannot take it"):
        assert waiter.request_read_write().get("ok") is False, waiter.frames()
    with step("the holder's socket closes"):
        gone.close()
        gone.wait_for_eof(timeout=10)
    with step("the waiter takes the seat without being asked to retry anything but the seat"):
        granted = waiter.request_read_write()
        assert granted.get("ok") is True, granted
        assert waiter.mode() == "read-write", waiter.mode()
    with step("and the port still answers it"):
        waiter.echo_of("seat-was-freed")


@surfaces("cli:rw")
def test_rw08_a_takeover_words_both_terminals_and_leaves_the_loser_attached(servers, clients, story):
    """A Takeover grants the taker, names the victim to both, and keeps the loser on the port as a viewer."""
    server = _capacity_world(servers, "take", "one")
    victim = clients.cli(user="rw", surface="cli:victim", server=server)
    victim.attach(port="slot1")
    label = [str(item) for item in victim.query_holders().get("holders") or []][0]
    taker = clients.cli(user="user2", surface="cli:taker", server=server)
    with step("the taker is not attached yet, so it attaches read-only while the seat is held"):
        taker.attach(port="slot1")
        assert taker.mode() == "read-only", taker.mode()
    with step("the Takeover grants the seat and names the holder it displaced"):
        reply = taker.takeover()
        assert reply.get("ok") is True, reply
        assert reply.get("mode") == "read-write", reply
        taken = [str(line) for line in reply.get("lines") or [] if "Taken from" in str(line)]
        assert len(taken) == 1, reply
        assert label in taken[0], f"{label!r} was not named in {taken[0]!r}"
    with step("the taker's terminal shows both lines it was sent"):
        for line in reply.get("lines") or []:
            assert str(line) in taker.read_text(), taker.read_text()[-400:]
    with step("the loser is told who took it, in the module's own sentence"):
        expect = notice(Code.DEMOTED, actor="user2").message
        victim.wait_for(expect, timeout=10)
        assert victim.last_frame("client_mode").get("code") == Code.DEMOTED.value, victim.frames()
    with step("the loser keeps the port and now only watches"):
        assert victim.mode() == "read-only", victim.mode()
        taker.echo_of("taker-drives")
        victim.wait_for("taker-drives", timeout=10)


@surfaces("cli:rw")
def test_rw09_a_targeted_takeover_names_one_holder_and_refuses_a_wrong_id(servers, clients, story):
    """Naming a holder in a Takeover takes from that holder, and an id nobody holds is refused by name."""
    server = _capacity_world(servers, "target", "one")
    victim = clients.cli(user="rw", surface="cli:victim", server=server)
    victim.attach(port="slot1")
    taker = clients.cli(user="user2", surface="cli:taker", server=server)
    taker.attach(port="slot1")
    with step("the holders list is where a client reads the id it may name"):
        label = [str(item) for item in taker.query_holders().get("holders") or []][0]
        target = _id_of(label)
    with step("a Takeover naming an id no holder has is refused, and says to check the list"):
        wrong = taker.takeover(target="deadbeef")
        assert wrong.get("ok") is False, wrong
        assert wrong.get("code") == Code.INVALID_TARGET.value, wrong
        for line in _lines_for(Code.INVALID_TARGET):
            taker.wait_for(line, timeout=10)
    with step("naming the real id grants the seat and names that holder as the victim"):
        granted = taker.takeover(target=target)
        assert granted.get("ok") is True, granted
        assert f"[{target}]" in " ".join(str(line) for line in granted.get("lines") or []), granted
        assert victim.last_frame("client_mode").get("code") == Code.DEMOTED.value, victim.frames()
    with step("and the seat is the taker's, proven by the echo it gets back"):
        assert taker.mode() == "read-write", taker.mode()
        taker.echo_of("targeted-take")


@surfaces("telnet:rw", "cli:rw")
def test_rw10_the_telnet_menu_reports_the_holder_and_refuses_a_held_seat(servers, clients, story):
    """The port menu's holders key names the label, and its request key answers with the same refusal."""
    server = _capacity_world(servers, "menu", "one", telnet_kwargs={"require_auth": True})
    holder = clients.cli(user="rw", surface="cli:holder", server=server)
    holder.attach(port="slot1")
    tel = clients.telnet(user="rw", surface="telnet:viewer", server=server, require_auth=True)
    with step("the session chooses the port and comes up read-only because the seat is held"):
        tel.wait_for_prompt("Port:")
        tel.send_line("slot1")
        tel.wait_for("slot1", timeout=10)
    with step("the holders key prints the same label the CLI got"):
        label = [str(item) for item in holder.query_holders().get("holders") or []][0]
        tel.open_control_menu()
        tel.press("w")
        tel.wait_for(label, timeout=20)
    with step("the request key is refused with the module's lines"):
        tel.open_control_menu()
        tel.press("a")
        for line in _lines_for(Code.PROMOTE_FAILED):
            tel.wait_for(line, timeout=20)
    with step("and the refusal left the session where it was"):
        assert "read-only mode" in tel.text() or "read-only" in tel.text(), tel.text()[-300:]


@surfaces("telnet:rw", "cli:rw")
def test_rw11_a_telnet_takeover_displaces_the_holder_and_is_audited(servers, clients, story):
    """Takeover from the port menu grants the seat, names who it took from, and is written to both logs."""
    server = _capacity_world(servers, "tnake", "one", telnet_kwargs={"require_auth": True})
    victim = clients.cli(user="rw", surface="cli:victim", server=server)
    victim.attach(port="slot1")
    tel = clients.telnet(user="admin", surface="telnet:taker", server=server, require_auth=True)
    tel.wait_for_prompt("Port:")
    tel.send_line("slot1")
    with step("the session attaches read-only, because the CLI client already holds the seat"):
        tel.wait_for("read-only mode", timeout=10)
    with step("the menu's Take key accepts the latest holder when the prompt is answered empty"):
        tel.open_control_menu()
        tel.press("f")
        tel.wait_for(TAKE_PROMPT, timeout=10)
        tel.answer("")
    with step("the taker is granted and the CLI victim is told who took the seat"):
        tel.wait_for("Read-write access granted", timeout=10)
        victim.wait_for(notice(Code.DEMOTED, actor="admin").message, timeout=10)
        assert victim.mode() == "read-only", victim.mode()
    with step("the server log names the port, the taker, and the victim"):
        line = server.wait_for_log("WRITE-SLOT TAKEOVER port=slot1", timeout=15)
        assert "taker=admin:" in line, line
        assert "victim=rw:" in line, line
    with step("and the port's own data log carries the same event"):
        server.wait_for_port_log("slot1", "write_slot_takeover", timeout=15)


@surfaces("cli:rw")
def test_rw12_a_multiple_port_grants_every_entitled_client_at_once(servers, clients, story):
    """On a `multiple` port two write-entitled clients drive together, with no seat to win or lose."""
    server = _capacity_world(servers, "many", "multiple")
    one = clients.cli(user="rw", attach_to="slot1", surface="cli:one", server=server)
    two = clients.cli(user="user2", attach_to="slot1", surface="cli:two", server=server)
    with step("both came up read-write, because no attach had to wait for a seat"):
        assert one.mode() == "read-write", one.mode()
        assert two.mode() == "read-write", two.mode()
    with step("both drive the same port, and each sees the other's bytes"):
        one.echo_of("first-writer")
        two.wait_for("first-writer", timeout=10)
        two.write_line("second-writer")
        one.wait_for("second-writer", timeout=10)
    with step("the holders list shows both, which is what a `multiple` port looks like"):
        labels = [str(item) for item in one.query_holders().get("holders") or []]
        assert len(labels) == 2, labels
        assert all(HOLDER_LABEL.match(label) for label in labels), labels
    with step("and a Takeover is refused because it is not a thing this port can do"):
        reply = one.takeover()
        assert reply.get("code") == Code.ALREADY_RW.value, reply
        assert two.mode() == "read-write", two.mode()


@surfaces("cli:ro")
def test_rw13_a_read_only_account_is_refused_the_seat_and_cannot_drive(servers, clients, story):
    """A read-only account attaches, watches, and is refused in the module's words when it asks to drive."""
    server = _capacity_world(servers, "ro", "one")
    watcher = clients.cli(user="ro", attach_to="slot1", surface="cli:watcher", server=server)
    with step("it attaches, because watching is what it is entitled to"):
        assert watcher.mode() == "read-only", watcher.mode()
    with step("asking for the seat is an entitlement refusal, not a capacity answer"):
        reply = watcher.request_read_write()
        assert reply.get("ok") is False, reply
        assert reply.get("code") == Code.NOT_ENTITLED.value, reply
        for line in _lines_for(Code.NOT_ENTITLED):
            watcher.wait_for(line, timeout=10)
    with step("and what it types never reaches the port, which the echo would prove"):
        watcher.write_line("must-not-arrive")
        watcher.expect_no_text("must-not-arrive", for_seconds=3)


@surfaces("cli:admin", "cli:rw")
def test_rw14_a_none_port_keeps_even_admin_read_only_and_names_the_capacity(servers, clients, story):
    """A port with no Write slot is read-only for every permission, and says its capacity when asked."""
    server = _capacity_world(servers, "none", "none")
    admin = clients.cli(user="admin", attach_to="slot1", surface="cli:admin", server=server)
    writer = clients.cli(user="rw", attach_to="slot1", surface="cli:rw", server=server)
    with step("admin bypasses access control, not capacity, so it is a viewer here"):
        assert admin.mode() == "read-only", admin.mode()
    with step("a read-write account is a viewer too"):
        assert writer.mode() == "read-only", writer.mode()
    with step("asking for write names the port's own capacity, and blames nothing else"):
        reply = admin.request_read_write()
        assert reply.get("ok") is False, reply
        assert reply.get("code") == Code.NO_WRITE_SLOT.value, reply
        for line in _lines_for(Code.NO_WRITE_SLOT):
            admin.wait_for(line, timeout=10)
    with step("so no client ever claims the seat, and the port has no holder"):
        assert [str(item) for item in admin.query_holders().get("holders") or []] == [], admin.query_holders()


@surfaces("telnet:admin")
def test_rw15_a_read_only_listener_refuses_both_write_keys_and_blocks_keystrokes(servers, clients, story):
    """A listener configured `read_only` answers its two write keys with its own reason and drops what it types.

    Measured here and reported rather than asserted as correct: the flag gates this listener's keys
    and its data path, not the Attach. `ConsoleManager.connect_client_to_port` never sees the
    listener, so the Port still records whatever the access ladder granted, and a session on a
    read-only listener can hold the Write slot while being unable to drive. See
    docs/design/e2e_test_suite.md.
    """
    server = servers(
        name="rolistener",
        loopback=("slot1",),
        telnet_kwargs={"require_auth": True, "read_only": True},
        start=True,
    )
    tel = clients.telnet(user="admin", surface="telnet:admin", server=server, require_auth=True)
    tel.wait_for_prompt("Port:")
    tel.send_line("slot1")
    tel.wait_for("slot1", timeout=10)
    with step("the session is announced read-only before it asks for anything"):
        tel.wait_for("read-only mode", timeout=10)
    with step("the request key refuses, and names the listener as the reason"):
        tel.open_control_menu()
        tel.press("a")
        tel.wait_for("configured read-only", timeout=10)
        for line in _lines_for(Code.GRANTED, mode=Mode.READ_WRITE):
            assert line not in tel.text(), tel.text()[-300:]
    with step("the Takeover key refuses too, before it asks whom to displace"):
        tel.open_control_menu()
        tel.press("f")
        tel.wait_for("configured read-only", timeout=10)
        tel.expect_no_text(TAKE_PROMPT, for_seconds=2.0)
    with step("and what this session types never reaches the port, which an echo would prove"):
        tel.send_line("ro-listener-cannot-drive")
        tel.expect_no_text("ro-listener-cannot-drive", for_seconds=3.0)


@surfaces("cli:rw", "cli:ro")
def test_rw16_a_port_with_group_lists_opens_only_for_the_names_it_lists(servers, clients, story):
    """`read_write_groups` decides who drives: a member drives, a watcher watches, an outsider is refused."""

    def configure(builder: Any) -> Any:
        builder.with_user("netops", "read-write", groups=["net"])
        builder.with_user("watcher", "read-only", groups=["watch"])
        builder.with_user("outsider", "read-write")
        builder.with_user("boss", "admin")
        builder.with_loopback("grp1", read_write_groups=["net"], read_only_groups=["watch"])
        return builder

    server = servers(name="groups", users=(), loopback=(), configure=configure, start=True)
    member = clients.cli(user="netops", attach_to="grp1", surface="cli:member", server=server)
    watcher = clients.cli(user="watcher", attach_to="grp1", surface="cli:watcher", server=server)
    with step("the member drives, even though its global permission would allow only watching"):
        assert member.mode() == "read-write", member.mode()
    with step("the watcher watches, though its name is in neither write list"):
        assert watcher.mode() == "read-only", watcher.mode()
    with step("a write-entitled account in neither list never attaches at all"):
        with pytest.raises(AssertionError) as refused:
            clients.cli(user="outsider", attach_to="grp1", surface="cli:outsider", server=server)
        assert decide(Code.DENIED_BY_GROUP_ACL, mode=Mode.READ_ONLY).message in str(refused.value)
    with step("the refusal names the group rule, and no seat was given to the outsider"):
        assert decide(Code.DENIED_BY_ACCESS_DEFAULT, mode=Mode.READ_ONLY).message not in str(refused.value)
        labels = [str(item) for item in member.query_holders().get("holders") or []]
        assert [label for label in labels if "outsider" in label] == [], labels
    with step("admin bypasses access control, so the port opens for it, but a held seat stays held"):
        boss = clients.cli(user="boss", attach_to="grp1", surface="cli:boss", server=server)
        assert boss.mode() == "read-only", boss.mode()
        refusal = boss.request_read_write()
        assert refusal.get("code") == Code.PROMOTE_FAILED.value, refusal


@surfaces("web:rw", "cli:rw")
def test_rw17_a_refused_web_attach_reaches_the_page_with_the_servers_reason(servers, console, clients, story):
    """A page whose attach a group list refuses shows the refusal sentence, and does not pretend to be connected."""

    def configure(builder: Any) -> Any:
        builder.with_user("netops", "read-write", groups=["net"])
        builder.with_user("outsider", "read-write")
        builder.with_loopback("grp1", read_write_groups=["net"])
        return builder

    server = servers(name="webrefuse", users=(), loopback=(), configure=configure, start=True)
    page = console.console("web:outsider", server, "grp1", user="outsider")
    with step("the page does not claim a session it was refused"):
        page.wait_until(lambda: page.view()["connected"] is False, timeout=20, what="the page to settle as not connected")
        assert page.view()["connected"] is False, page.view()
    with step("the terminal carries the refusal the server closed the socket with"):
        sentence = decide(Code.DENIED_BY_GROUP_ACL, mode=Mode.READ_ONLY).message
        page.wait_for_text(sentence, timeout=20)
    with step("and the page says it is disconnected, in its own wording"):
        page.wait_for_text("Disconnected from", timeout=20)
    with step("the member of the write list does reach the same port, so the port is not at fault"):
        member = clients.cli(user="netops", attach_to="grp1", surface="cli:member", server=server)
        assert member.mode() == "read-write", member.mode()


@surfaces("cli:rw", "web:rw")
def test_rw18_a_web_page_can_take_the_seat_from_a_cli_holder(servers, console, clients, story):
    """The page's Take control item is the same Takeover: it grants the page and displaces the holder."""
    server = _capacity_world(servers, "webtake", "one")
    victim = clients.cli(user="rw", surface="cli:victim", server=server)
    victim.attach(port="slot1")
    page = console.console("web:ro", server, "slot1", user="rw")
    page.wait_for_view("(v) => v.connected === true && v.mode === 'read-only'", timeout=20)
    with step("the menu item that takes the seat is offered to a page that is entitled to ask"):
        page.click("#roIndicator")
        page.wait_until(lambda: not page.page.is_hidden("#roMenu"), timeout=10, what="the access menu to open")
        labels = [item["label"] for item in page.menu_items("ro")]
        assert any("Take" in label for label in labels), labels
    with step("taking the seat makes the page read-write"):
        page.click("#roMenuForceRW")
        page.wait_for_view("(v) => v.mode === 'read-write'", timeout=20)
    with step("and the CLI holder loses it, with the server naming the taker"):
        victim.wait_for(notice(Code.DEMOTED, actor="rw").message, timeout=10)
        assert victim.mode() == "read-only", victim.mode()
    with step("the page can now drive, which the Loopback echo proves"):
        page.send_line("page-drives-now")
        page.wait_for_text("page-drives-now", timeout=20)


@surfaces("web:rw", "cli:rw")
def test_rw19_a_page_that_loses_the_seat_stops_claiming_it(servers, console, clients, story):
    """A page demoted by another client changes its mode, its pill, and its body class at once."""
    server = _capacity_world(servers, "weblose", "one")
    page = console.console("web:rw", server, "slot1", user="rw")
    page.wait_for_view("(v) => v.mode === 'read-write'", timeout=20)
    with step("the page holds the seat and says so on the page, not only in the hook"):
        assert "mode-readonly" not in page.body_classes(), page.body_classes()
        assert page.page.is_hidden("#roIndicatorWrap"), "a read-write page must not offer the read-only pill"
    with step("another client takes it"):
        taker = clients.cli(user="user2", surface="cli:taker", server=server)
        taker.attach(port="slot1")
        assert taker.takeover().get("ok") is True, taker.frames()
    with step("the page reports the new standing from the notice it was sent"):
        lost = page.wait_for_view("(v) => v.mode === 'read-only'", timeout=20)
        assert "mode-readonly" in str(lost["body_class"]).split(), lost["body_class"]
        assert page.text_of("#roIndicator").startswith("read-only"), page.text_of("#roIndicator")
    with step("the sentence in the terminal is the module's, with the taker named"):
        page.wait_for_text(notice(Code.DEMOTED, actor="user2").message, timeout=10)
    with step("and what the page types now never reaches the port"):
        page.send_line("demoted-keystrokes")
        page.expect_no_text("demoted-keystrokes", for_seconds=3)
        taker.echo_of("only-the-taker-drives")


@surfaces("cli:ro", "cli:rw")
def test_rw20_one_client_that_stops_reading_does_not_hold_up_the_others(servers, clients, story):
    """Three viewers on one port, one stalled on purpose: a control frame still reaches the others.

    The data path is decoupled per client, so a stalled reader cannot delay another reader's bytes.
    The control path is not: `ConsoleManager.broadcast_control_frame_to_port` awaits each delivery in
    turn, and each delivery ends in a write that blocks when the peer stops reading. This scenario
    fills one client's socket, then changes the viewer set, and asks whether the other two still get
    the frame that change broadcasts.
    """
    server = _capacity_world(servers, "stall", "multiple")
    first = clients.cli(user="ro", attach_to="slot1", surface="cli:first", server=server)
    stalled = clients.cli(user="ro", attach_to="slot1", surface="cli:stalled", server=server)
    third = clients.cli(user="ro", attach_to="slot1", surface="cli:third", server=server)
    writer = clients.cli(user="rw", attach_to="slot1", surface="cli:writer", server=server)
    with step("one viewer stops draining its socket, and the port keeps writing to it"):
        stalled.pause_reader()
        writer.write("x" * STALL_PAYLOAD_BYTES)
        first.wait_for("xxxx", timeout=30)
        third.wait_for("xxxx", timeout=30)
    with step("the viewer set now changes, which broadcasts a control frame to all three"):
        before = {name: len(session.frames("presence")) for name, session in (("first", first), ("third", third))}
        late = clients.cli(user="ro", attach_to="slot1", surface="cli:late", server=server)
        assert late.mode() is not None, late.mode()
    with step("the two readers that kept up receive it, though a client ahead of them is stuck"):
        for name, session in (("first", first), ("third", third)):
            start = before[name]
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and len(session.frames("presence")) <= start:
                time.sleep(0.05)
            assert len(session.frames("presence")) > start, (
                f"{name} received no presence frame while another client was stalled; "
                f"presence counts={before!r} -> {{'first': {len(first.frames('presence'))}, "
                f"'third': {len(third.frames('presence'))}}}"
            )
