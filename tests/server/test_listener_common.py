"""Tests for the shared escape-sequence/control-menu helpers in listener_common.py."""

import asyncio

import pytest

from openmux.server.adapters.listener_common import (
    CONTROL_MENU_HELP,
    TAKE_TARGET_PROMPT,
    EscapeState,
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
# format_rw_notice


def test_format_rw_notice_granted():
    text = format_rw_notice({"type": "client_mode", "ok": True, "mode": "read-write"})
    assert "granted" in text.lower()


def test_format_rw_notice_switched_read_only():
    text = format_rw_notice({"type": "client_mode", "ok": True, "mode": "read-only"})
    assert "read-only" in text.lower()


def test_format_rw_notice_demoted_by_other_user():
    text = format_rw_notice({"type": "client_mode", "ok": False, "mode": "read-only", "reason": "demoted"})
    assert "taken by another user" in text.lower()


def test_format_rw_notice_denied_with_holders():
    text = format_rw_notice({"type": "client_mode", "ok": False, "rw_holders": ["alice@1.2.3.4"]})
    assert "alice@1.2.3.4" in text
    assert "denied" in text.lower()


def test_format_rw_notice_denied_without_holders():
    text = format_rw_notice({"type": "client_mode", "ok": False})
    assert "not available" in text.lower()


def test_format_rw_notice_rw_holders_query_with_holders():
    text = format_rw_notice({"type": "rw_holders", "holders": ["bob@10.0.0.1"]})
    assert "bob@10.0.0.1" in text


def test_format_rw_notice_rw_holders_query_empty():
    text = format_rw_notice({"type": "rw_holders", "holders": []})
    assert "no client" in text.lower()


def test_format_rw_notice_targeted_take_success_names_victim():
    # Targeted success (issue #61): the grant plus the displaced holder.
    text = format_rw_notice({"type": "client_mode", "ok": True, "mode": "read-write", "takeover": "[A] alice@10.0.0.5 (rw)"})
    assert "granted" in text.lower()
    assert "[Taken from: [A] alice@10.0.0.5 (rw)]" in text


def test_format_rw_notice_untargeted_success_has_no_taken_from():
    text = format_rw_notice({"type": "client_mode", "ok": True, "mode": "read-write"})
    assert "Taken from" not in text


def test_format_rw_notice_invalid_target():
    text = format_rw_notice({"type": "client_mode", "ok": False, "mode": "read-only", "reason": "invalid_target"})
    assert "does not hold read-write" in text.lower()


def test_format_rw_notice_federation_denied():
    text = format_rw_notice({"type": "client_mode", "ok": False, "mode": "read-only", "reason": "federation_denied"})
    assert "origin server did not grant" in text.lower()


def test_format_rw_notice_promote_failed_with_holders():
    text = format_rw_notice(
        {
            "type": "client_mode",
            "ok": False,
            "mode": "read-only",
            "reason": "promote_failed",
            "rw_holders": ["[A] alice@1.2.3.4 (rw)"],
        }
    )
    assert "no free read-write seat" in text.lower()
    assert "[A] alice@1.2.3.4 (rw)" in text


def test_format_rw_notice_promote_failed_without_holders():
    text = format_rw_notice({"type": "client_mode", "ok": False, "mode": "read-only", "reason": "promote_failed"})
    assert "no free read-write seat" in text.lower()


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
