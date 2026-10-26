"""Tests for the shared prompt primitives in listener_common.py (the leaf below the seam).

The escape scanner, Control menu, and frame rendering moved with the session module
to tests/server/test_listener_session.py (issue #100).
"""

import asyncio

import pytest

from openmux.server.adapters.listener_common import (
    TAKE_TARGET_PROMPT,
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
