"""The timeline recorder renders bytes a person can read (TIM-01..TIM-03).

These tests read nothing but the recorder, and no server starts. They exist because the renderer is the
only place a control byte or a Telnet negotiation becomes readable text, and it drifted twice: once into
Unicode pictures that a plain terminal shows as boxes, and once into the wrong RFC 854 values, which
turned the listener's ``IAC WILL ECHO`` into ``IAC 1``.
"""

from __future__ import annotations

import pytest

from .story import Timeline, render_bytes, surfaces

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
