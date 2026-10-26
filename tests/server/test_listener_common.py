"""Tests for the shared escape-sequence/control-menu helpers in listener_common.py."""

import asyncio

import pytest

from openmux.server.access_decision import Code, Holder, Mode, decide, holders_frame, notice
from openmux.server.adapters.listener_common import (
    CONTROL_MENU_HELP,
    TAKE_TARGET_PROMPT,
    EscapeState,
    access_manager_missing_frame,
    feed_escape_byte,
    format_rw_notice,
    read_take_target,
)


class _PromptReader:
    """Streams queued bytes one `read(n)`-sized slice at a time."""

    def __init__(self, chunks=None):
        self.pending = bytearray()
        self.chunks = list(chunks or [])

    async def read(self, n):
        if not self.pending:
            await asyncio.sleep(0)
            if not self.chunks:
                return b""
            self.pending = bytearray(self.chunks.pop(0))
        out = bytes(self.pending[:n])
        del self.pending[:n]
        return out


class _PromptWriter:
    def __init__(self):
        self.buffer = bytearray()

    def write(self, data):
        self.buffer += data

    async def drain(self):
        await asyncio.sleep(0)


def _feed(state: EscapeState, data: bytes):
    """Feed a byte string through feed_escape_byte, returning (forwarded, commands)."""
    forwarded = bytearray()
    commands = []
    for i in range(len(data)):
        extra, cmd = feed_escape_byte(state, data[i : i + 1])
        forwarded.extend(extra)
        if cmd:
            commands.append(cmd)
    return bytes(forwarded), commands


# ---------------------------------------------------------------------------
# feed_escape_byte / EscapeState


def test_feed_escape_byte_plain_data_passes_through():
    state = EscapeState()
    forwarded, commands = _feed(state, b"hello")
    assert forwarded == b"hello"
    assert commands == []


def test_feed_escape_byte_full_sequence_yields_command():
    state = EscapeState()
    forwarded, commands = _feed(state, b"\x05ca")
    assert forwarded == b""
    assert commands == ["a"]


def test_feed_escape_byte_sequence_embedded_in_data():
    state = EscapeState()
    forwarded, commands = _feed(state, b"foo\x05cwbar")
    assert forwarded == b"foobar"
    assert commands == ["w"]


def test_feed_escape_byte_false_alarm_replays_both_bytes():
    # \x05 followed by something other than 'c' is not an escape sequence;
    # both buffered bytes must be replayed verbatim (mirrors console.py).
    state = EscapeState()
    forwarded, commands = _feed(state, b"\x05x")
    assert forwarded == b"\x05x"
    assert commands == []
    assert state.state == 0


def test_feed_escape_byte_state_persists_across_chunk_boundary():
    state = EscapeState()
    # First chunk ends right after the escape char1 byte.
    forwarded1, commands1 = _feed(state, b"\x05")
    assert forwarded1 == b""
    assert commands1 == []
    assert state.state == 1
    # Second chunk supplies char2 and the command mnemonic.
    forwarded2, commands2 = _feed(state, b"ci")
    assert forwarded2 == b""
    assert commands2 == ["i"]


def test_feed_escape_byte_custom_escape_sequence():
    state = EscapeState(escape_char1=b"\x01", escape_char2=b"e")
    forwarded, commands = _feed(state, b"\x01ev")
    assert forwarded == b""
    assert commands == ["v"]
    # The default sequence no longer triggers the menu.
    forwarded2, commands2 = _feed(state, b"\x05ca")
    assert forwarded2 == b"\x05ca"
    assert commands2 == []


# ---------------------------------------------------------------------------
# CONTROL_MENU_HELP


def test_control_menu_help_lists_all_commands():
    for cmd in ["a", "f", "s", "w", "i", "v", "e", ".", "?"]:
        assert f"{cmd} " in CONTROL_MENU_HELP or f"{cmd}  " in CONTROL_MENU_HELP


# ---------------------------------------------------------------------------
# format_rw_notice: renders a server-authored access frame (issue #98)
#
# The wording belongs to the access module and is pinned in
# tests/server/test_access_decision.py. Pinned here is the renderer's own job: which
# frames reach a raw terminal, and in what frame.


def test_grant_frame_writes_its_own_line():
    assert format_rw_notice(decide(Code.GRANTED, mode=Mode.READ_WRITE).frame()) == "\r\n[Read-write access granted]\r\n"


def test_takeover_frame_writes_the_taken_from_line():
    victim = Holder(client_id="A", username="alice", ip="10.0.0.5")
    frame = decide(Code.GRANTED, mode=Mode.READ_WRITE, victim=victim).frame()
    assert format_rw_notice(frame) == "\r\n[Read-write access granted]\r\n[Taken from: [A] alice@10.0.0.5 (rw)]\r\n"


def test_refusal_frame_writes_the_servers_sentence():
    frame = decide(Code.INVALID_TARGET, mode=Mode.READ_ONLY).frame()
    assert "does not hold read-write" in format_rw_notice(frame).lower()


def test_demotion_notice_writes_its_line():
    frame = notice(Code.DEMOTED, client_id="c1").frame()
    assert "your read-write access was taken by another user" in format_rw_notice(frame).lower()


def test_a_frame_with_no_wording_writes_nothing():
    # An attach grant stays silent on a terminal that already printed its banner, and a
    # frame the server does not word stays silent: this renderer invents no wording.
    assert format_rw_notice(decide(Code.ATTACHED, mode=Mode.READ_WRITE).frame()) == ""
    assert format_rw_notice({"type": "client_mode", "ok": True, "mode": "read-write"}) == ""
    assert format_rw_notice({}) == ""


def test_presence_push_stays_off_the_terminal():
    assert format_rw_notice({"type": "presence", "holders": ["[A] alice@1.2.3.4 (rw)"]}) == ""


def test_holders_frame_lists_the_labels():
    frame = holders_frame([Holder(client_id="A", username="alice", ip="1.2.3.4")])
    assert format_rw_notice(frame) == "\r\n[Read-write held by: [A] alice@1.2.3.4 (rw)]\r\n"


def test_holders_frame_says_when_no_one_holds_the_slot():
    assert format_rw_notice(holders_frame([])) == "\r\n[No client currently holds read-write access]\r\n"


def test_a_frame_with_no_access_manager_says_so():
    assert "could not complete the request" in format_rw_notice(access_manager_missing_frame("read-write")).lower()


# ---------------------------------------------------------------------------
# read_take_target (issue #61): the `f` command's id prompt (telnet/SSH)


def test_prompt_shown_and_echoed():
    assert TAKE_TARGET_PROMPT.startswith("[Take from holder")
    assert "Enter for latest" in TAKE_TARGET_PROMPT


@pytest.mark.asyncio
async def test_read_take_target_reads_id_and_echoes():
    writer = _PromptWriter()
    target, keep_going = await read_take_target(_PromptReader([b"f", b"4", b"\n"]), writer)
    assert target == "f4"
    assert keep_going is True
    assert writer.buffer.startswith(TAKE_TARGET_PROMPT.encode())
    assert b"f4" in writer.buffer  # each byte echoed


@pytest.mark.asyncio
async def test_read_take_target_enter_is_no_target():
    target, keep_going = await read_take_target(_PromptReader([b"\n"]), _PromptWriter())
    assert target is None
    assert keep_going is True


@pytest.mark.asyncio
async def test_read_take_target_cr_terminated():
    target, keep_going = await read_take_target(_PromptReader([b"abc", b"\r"]), _PromptWriter())
    assert target == "abc"
    assert keep_going is True


@pytest.mark.asyncio
async def test_read_take_target_backspace_edits():
    writer = _PromptWriter()
    # Type "ab", backspace (drop 'b'), type "c": result "ac".
    target, keep_going = await read_take_target(_PromptReader([b"a", b"b", b"\x7f", b"c", b"\n"]), writer)
    assert target == "ac"
    assert b"\b \b" in writer.buffer
    assert keep_going is True


@pytest.mark.asyncio
async def test_read_take_target_eof_returns_none_and_stops():
    target, keep_going = await read_take_target(_PromptReader([b"f4"]), _PromptWriter())
    assert target is None
    assert keep_going is False


@pytest.mark.asyncio
async def test_read_take_target_echo_error_keeps_target():
    class _BrokenWriter(_PromptWriter):
        async def drain(self):
            raise OSError("gone")

    writer = _BrokenWriter()
    target, keep_going = await read_take_target(_PromptReader([b"f4", b"\n"]), writer)
    assert target == "f4"
    assert keep_going is True
