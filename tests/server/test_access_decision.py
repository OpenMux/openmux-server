"""Tests for the Access decision module (issue #98).

The module owns the outcome vocabulary, the wording, and the `client_mode`
payload. Wording is asserted here and nowhere else: every surface renders what
this table says, so a wording edit costs one row here instead of four files.
"""

import json

import pytest

from openmux.server.access_control import WIRE_MULTIPLE
from openmux.server.access_decision import (
    CLOSE_ATTACH_DENIED,
    FRAME_HOLDERS,
    FRAME_MODE,
    MESSAGE_LIMIT,
    AccessDecision,
    Code,
    Entitlement,
    Holder,
    Kind,
    Mode,
    decide,
    holders_frame,
    holders_of,
    notice,
)


def holder(client_id="cafebabe0000", username="alice", ip="10.0.0.5"):
    return Holder(client_id=client_id, username=username, ip=ip)


def build(code, **kwargs):
    """Build one decision of every code, using the role the code belongs to."""
    kwargs.setdefault("port_name", "p1")
    if code.kind is Kind.NOTICE:
        return notice(code, **kwargs)
    kwargs.setdefault("mode", Mode.READ_ONLY)
    return decide(code, **kwargs)


class TestCodeVocabulary:
    """Every outcome code is worded, classified, and renderable."""

    def test_every_code_has_a_kind(self):
        for code in Code:
            assert isinstance(build(code).kind, Kind)

    def test_kind_is_one_of_four(self):
        assert {k for k in Kind} >= {Kind.GRANT, Kind.REFUSAL, Kind.UNAVAILABLE, Kind.NOTICE}

    def test_ok_never_comes_from_the_caller(self):
        # `ok` is not a parameter: a surface cannot report a result it did not get.
        with pytest.raises(TypeError):
            decide(Code.NOT_ATTACHED, mode=Mode.READ_ONLY, ok=True)

    def test_shipped_wire_strings_are_preserved(self):
        # Values already on the wire keep their spelling so an old client's generic
        # branch still lands on the same outcome (ADR-0001).
        assert Code.GRANTED.value == "ok"
        assert Code.ALREADY_RW.value == "already_rw"
        assert Code.PROMOTE_FAILED.value == "promote_failed"
        assert Code.FEDERATION_DENIED.value == "federation_denied"
        assert Code.INVALID_TARGET.value == "invalid_target"
        assert Code.DENIED_BY_GROUP_ACL.value == "denied_by_group_acl"
        assert Code.DENIED_BY_ACCESS_DEFAULT.value == "denied_by_access_default"
        assert Code.DEMOTED.value == "demoted"
        assert Code.INTERNAL.value == "error"

    def test_unknown_code_string_is_rejected_at_build(self):
        with pytest.raises(ValueError):
            AccessDecision(code="not_a_code", mode="read-only")

    def test_unknown_mode_string_is_rejected_at_build(self):
        with pytest.raises(ValueError):
            AccessDecision(code=Code.GRANTED, mode="drive")


class TestWording:
    """One sentence per outcome, in one tense, for all four surfaces."""

    def test_message_is_one_short_line(self):
        for code in Code:
            message = build(code).message
            assert "\n" not in message and "\r" not in message, code
            assert len(message.encode("utf-8")) <= MESSAGE_LIMIT, code
            assert message, code

    def test_notice_lines_are_bracketed_and_crlf_terminated(self):
        decision = decide(Code.GRANTED, mode=Mode.READ_WRITE, port_name="p1")
        assert decision.text == "\r\n[Read-write access granted]\r\n"

    def test_attach_grant_is_silent(self):
        # The console already prints its own connect line; no banner per connect.
        assert decide(Code.ATTACHED, mode=Mode.READ_WRITE, port_name="p1").lines == ()

    def test_takeover_names_the_victim(self):
        decision = decide(Code.GRANTED, mode=Mode.READ_WRITE, victim=holder(), port_name="p1")
        assert decision.lines == ("[Read-write access granted]", "[Taken from: [babe0000] alice@10.0.0.5 (rw)]")

    def test_takeover_without_victim_is_one_line(self):
        decision = decide(Code.GRANTED, mode=Mode.READ_WRITE, port_name="p1")
        assert decision.lines == ("[Read-write access granted]",)

    def test_capacity_none_names_the_capacity(self):
        decision = decide(Code.NO_WRITE_SLOT, mode=Mode.READ_ONLY, capacity="none", port_name="p1")
        assert "no write slot" in decision.message

    def test_promote_failure_says_the_server_failed(self):
        # A failed promote is not a capacity answer: it must not blame the seat.
        decision = decide(Code.PROMOTE_FAILED, mode=Mode.READ_ONLY, holders=[holder()], port_name="p1")
        assert decision.message == "The server failed to grant read-write access"
        assert "seat" not in decision.message

    def test_demoted_names_the_actor(self):
        assert "bob" in notice(Code.DEMOTED, actor="bob", port_name="p1").message

    def test_demoted_without_actor_says_another_user(self):
        # A MuxCon relay reports no actor; the wording must stay honest.
        assert "another user" in notice(Code.DEMOTED, port_name="p1").message

    def test_attach_refusal_message_is_unbracketed(self):
        # The same sentence is a WebSocket close reason and an `ERROR:CONNECT:` tail.
        decision = decide(Code.NO_PERMISSIONS, mode=Mode.READ_ONLY, port_name="p1")
        assert decision.message == "No permissions assigned to this account"
        assert not decision.message.startswith("[")

    def test_port_unavailable_is_distinct_from_capacity_full(self):
        assert Code.PORT_UNAVAILABLE.kind is Kind.UNAVAILABLE
        assert Code.CAPACITY_FULL.kind is Kind.GRANT


class TestOkAndKind:
    def test_grants_are_ok(self):
        for code in (Code.ATTACHED, Code.GRANTED, Code.ALREADY_RW, Code.RELEASED, Code.CAPACITY_FULL):
            assert decide(code, mode=Mode.READ_WRITE, port_name="p1").ok is True, code

    def test_refusals_are_not_ok(self):
        for code in (Code.NO_PERMISSIONS, Code.INVALID_TARGET, Code.FEDERATION_DENIED, Code.DEMOTE_FAILED):
            assert decide(code, mode=Mode.READ_ONLY, port_name="p1").ok is False, code

    def test_release_failure_keeps_read_write_mode(self):
        # A failed release leaves the client read-write; the frame must not lie.
        decision = decide(Code.DEMOTE_FAILED, mode=Mode.READ_WRITE, port_name="p1")
        assert decision.ok is False and decision.mode is Mode.READ_WRITE

    def test_web_console_branches_on_kind_not_on_a_string(self):
        assert decide(Code.PORT_UNAVAILABLE, mode=Mode.READ_ONLY).kind is Kind.UNAVAILABLE
        assert decide(Code.DENIED_BY_GROUP_ACL, mode=Mode.READ_ONLY).kind is Kind.REFUSAL


class TestConstructionRoles:
    def test_decide_rejects_a_notice_code(self):
        with pytest.raises(ValueError):
            decide(Code.DEMOTED, mode=Mode.READ_ONLY)

    def test_notice_rejects_an_answer_code(self):
        with pytest.raises(ValueError):
            notice(Code.GRANTED)

    def test_notice_takes_no_mode(self):
        with pytest.raises(TypeError):
            notice(Code.DEMOTED, mode=Mode.READ_WRITE)

    def test_notice_fixes_its_mode(self):
        assert notice(Code.DEMOTED).mode is Mode.READ_ONLY
        assert notice(Code.ACTION_RESTORED).mode is Mode.READ_WRITE

    def test_notice_kinds(self):
        assert notice(Code.DEMOTED).kind is Kind.NOTICE
        assert notice(Code.DEMOTED).ok is False
        assert notice(Code.ACTION_RESTORED).ok is True


class TestFrame:
    def test_frame_carries_the_agreed_keys(self):
        frame = decide(
            Code.GRANTED, mode=Mode.READ_WRITE, client_id="c1", holders=[holder()], capacity="one", port_name="p1"
        ).frame()
        assert frame["type"] == FRAME_MODE
        assert frame["ok"] is True
        assert frame["mode"] == "read-write"
        assert frame["code"] == "ok"
        assert frame["rw_holders"] == ["[babe0000] alice@10.0.0.5 (rw)"]
        assert frame["max_rw_users"] == 1
        assert frame["client_id"] == "c1"

    def test_frame_never_carries_the_retired_keys(self):
        for code in Code:
            frame = build(code).frame()
            for retired in ("reason", "takeover", "taken_by"):
                assert retired not in frame, f"{code}: {retired}"

    def test_frame_is_json_serialisable(self):
        for code in Code:
            json.dumps(build(code).frame())

    def test_frame_omits_unknown_capacity_and_absent_holders(self):
        frame = decide(Code.NOT_ATTACHED, mode=Mode.READ_ONLY).frame()
        assert "max_rw_users" not in frame
        assert "rw_holders" not in frame
        assert "client_id" not in frame

    def test_message_is_fit_for_a_websocket_close_reason(self):
        # aiohttp rejects a close reason longer than 123 bytes.
        for code in Code:
            assert len(build(code).message.encode("utf-8")) <= MESSAGE_LIMIT, code

    def test_attach_denial_close_code(self):
        assert CLOSE_ATTACH_DENIED == 4003


class TestMode:
    def test_wire_token_for_the_tcp_line(self):
        assert Mode.READ_WRITE.wire == "READ_WRITE"
        assert Mode.READ_ONLY.wire == "READ_ONLY"

    def test_json_value_is_the_session_word(self):
        assert Mode.READ_WRITE.value == "read-write"
        assert Mode.READ_ONLY.value == "read-only"


class TestHolder:
    def test_label_shape(self):
        assert holder("cafebabe0000", "alice", "10.0.0.5").label == "[babe0000] alice@10.0.0.5 (rw)"

    def test_federated_id_is_not_shortened(self):
        # The id doubles as the takeover target, so a `fed:` id stays whole.
        label = Holder("fed:peer:7", "bob").label
        assert label.startswith("[fed:peer:7] bob@")

    def test_unknown_ip_renders_unknown(self):
        assert Holder("c1", "bob").label.endswith("@unknown (rw)")


class TestHoldersOf:
    def test_keeps_only_read_write_records(self):
        records = [
            {"client_id": "c1", "username": "alice", "mode": "read-write"},
            {"client_id": "c2", "username": "bob", "mode": "read-only"},
        ]
        labels = [h.label for h in holders_of(records, ip_of=lambda cid: "1.2.3.4")]
        assert labels == ["[c1] alice@1.2.3.4 (rw)"]

    def test_tolerates_missing_records_and_broken_entries(self):
        assert holders_of(None, ip_of=lambda cid: "") == ()
        assert holders_of([None], ip_of=lambda cid: "") == ()
        # A record with no id still labels, so a broken entry never drops the whole list.
        assert len(holders_of([{"mode": "read-write"}, "junk"], ip_of=lambda cid: "")) == 1

    def test_missing_ip_resolves_through_the_callback(self):
        (h,) = holders_of([{"client_id": "c1", "username": "a", "mode": "read-write"}], ip_of=lambda cid: "9.9.9.9")
        assert h.ip == "9.9.9.9"

    def test_labels_shorten_a_local_id_and_keep_a_federated_one(self):
        # The short id is what a targeted Takeover is matched on, so a federated id
        # must stay whole: the origin resolves that exact string (issue #61).
        records = [
            {"client_id": "0123456789abcdef", "username": "alice", "mode": "read-write"},
            {"client_id": "fed:peerA:3", "username": "peerAuser", "mode": "read-write"},
            {"client_id": "ro1", "username": "carol", "mode": "read-only"},
        ]
        labels = [h.label for h in holders_of(records, ip_of=lambda cid: "unknown")]
        assert labels == ["[89abcdef] alice@unknown (rw)", "[fed:peerA:3] peerAuser@unknown (rw)"]


class TestHoldersFrame:
    def test_holders_reply_shape(self):
        frame = holders_frame([holder()], capacity="multiple")
        assert frame == {"type": FRAME_HOLDERS, "holders": ["[babe0000] alice@10.0.0.5 (rw)"], "max_rw_users": WIRE_MULTIPLE}

    def test_empty_holders_is_a_valid_answer(self):
        assert holders_frame([], capacity="none")["max_rw_users"] == 0


class TestCapacityNormalisation:
    def test_raw_port_value_becomes_a_mode(self):
        # Callers pass `port.max_read_write_users` straight through, legacy integers included.
        assert decide(Code.PROMOTE_FAILED, mode=Mode.READ_ONLY, capacity=0).capacity == "none"
        assert decide(Code.PROMOTE_FAILED, mode=Mode.READ_ONLY, capacity=1).capacity == "one"
        assert decide(Code.PROMOTE_FAILED, mode=Mode.READ_ONLY, capacity=5).capacity == "multiple"
        assert decide(Code.PROMOTE_FAILED, mode=Mode.READ_ONLY, capacity=None).capacity is None


class TestEntitlement:
    """Entitlement answers "may this identity ask", before any slot is counted."""

    def test_only_a_write_grant_allows_driving(self):
        assert Entitlement.write().may_drive
        assert not Entitlement.read_only().may_drive
        assert not Entitlement.denied(Code.DENIED_BY_GROUP_ACL).may_drive

    def test_a_denial_carries_the_rule_that_refused_it(self):
        ent = Entitlement.denied(Code.DENIED_BY_ACCESS_DEFAULT)
        assert ent.code is Code.DENIED_BY_ACCESS_DEFAULT

    def test_grants_carry_no_refusal_code(self):
        assert Entitlement.write().code is None
        assert Entitlement.read_only().code is None

    def test_denied_rejects_a_code_that_is_not_a_refusal(self):
        # A producer must not label a capacity problem an access-control denial.
        with pytest.raises(ValueError):
            Entitlement.denied(Code.CAPACITY_FULL)
        with pytest.raises(ValueError):
            Entitlement.denied(Code.GRANTED)


class TestCapacityAwareWording:
    """A refusal must not name a reason the Port's own setting contradicts."""

    def test_no_write_slot_says_so(self):
        decision = decide(Code.NO_WRITE_SLOT, mode=Mode.READ_ONLY, capacity=0)
        assert "no write slot" in decision.message
        assert decision.lines == (
            "[Read-write is not available on this port]",
            "[This port has no write slots (capacity: none)]",
        )

    def test_unlimited_seats_denies_that_a_seat_was_free(self):
        decision = decide(Code.TAKE_WITHOUT_SLOT, mode=Mode.READ_ONLY, holders=(holder(),), capacity=WIRE_MULTIPLE)
        assert "no single seat" in decision.message
        # The holder list must not be appended here: no holder blocked this client.
        assert "held by" not in decision.message
        assert decision.lines == (
            "[This port has no single write seat to take]",
            "[Every write-entitled client may hold write here at the same time]",
        )

    def test_one_seat_ports_report_the_holders_in_the_frame(self):
        decision = decide(Code.CAPACITY_FULL, mode=Mode.READ_ONLY, holders=(holder(),), capacity=1)
        # Attaching into a taken seat stays silent: the Client is connected and watching.
        assert decision.lines == ()
        # The badge is data, not a sentence: a surface draws it and no wording repeats it.
        assert decision.frame()["rw_holders"] == ["[babe0000] alice@10.0.0.5 (rw)"]
        assert decision.frame()["max_rw_users"] == 1

    def test_a_code_without_capacity_wording_ignores_capacity(self):
        assert decide(Code.NOT_ATTACHED, mode=Mode.READ_ONLY, capacity=WIRE_MULTIPLE).lines == (
            "[You are not attached to this port]",
        )
