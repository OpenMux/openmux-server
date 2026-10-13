"""Behavior + coverage tests for LoopbackPort.sanitize_data.

`sanitize_data` converted escape/C0 control sequences into bracketed tokens.
It was covered only partially (33 of 132 lines missing: the CSI HOME/END
letters, the digit-~ code 1/2/4 and fall-through token, the "not a ~" and
unrecognized-CSI fallbacks, the SS3 B/C/D arrows, the incomplete-buffer
breaks, and the CTRL fallback). These tests pin every branch on the real
port object (no mocking of the method under test).
"""

from openmux.server.adapters.loopback import LoopbackAdapter, LoopbackPort


def _port(**cfg):
    adapter = LoopbackAdapter("loopback", {"loopback_ports": []})
    return LoopbackPort("lp", cfg or {}, adapter)


def test_disabled_or_empty_returns_original():
    p = _port(sanitize_control=False)
    assert p.sanitize_data(b"\x1b[A") == b"\x1b[A"
    assert p.sanitize_data(b"") == b""
    p2 = _port(sanitize_control=True)
    # empty data returns the (empty) bytes object unchanged
    assert p2.sanitize_data(b"") == b""


def test_crlf_printable_del_nonascii_passthrough():
    p = _port(sanitize_control=True)
    assert p.sanitize_data(b"a\r\nb") == b"a\r\nb"
    assert p.sanitize_data("Hello world ~".encode()) == "Hello world ~".encode()
    assert p.sanitize_data(b"\x7f") == b"[DEL]"
    assert p.sanitize_data("é".encode("utf-8")) == "é".encode("utf-8")
    assert p.sanitize_data(b"\xc3\xa9\x80\xff") == b"\xc3\xa9\x80\xff"


def test_csi_arrows_and_home_end_letters():
    p = _port(sanitize_control=True)
    assert p.sanitize_data(b"\x1b[A") == b"[UP]"
    assert p.sanitize_data(b"\x1b[B") == b"[DOWN]"
    assert p.sanitize_data(b"\x1b[C") == b"[RIGHT]"
    assert p.sanitize_data(b"\x1b[D") == b"[LEFT]"
    assert p.sanitize_data(b"\x1b[H") == b"[HOME]"
    assert p.sanitize_data(b"\x1b[F") == b"[END]"
    # embedded in printable text, plus mixed sequence
    assert p.sanitize_data(b"x\x1b[Hy") == b"x[HOME]y"
    assert p.sanitize_data(b"\x1b[A\x1b[D") == b"[UP][LEFT]"


def test_csi_digit_tilde_codes():
    p = _port(sanitize_control=True)
    assert p.sanitize_data(b"\x1b[1~") == b"[HOME]"
    assert p.sanitize_data(b"\x1b[2~") == b"[INSERT]"
    assert p.sanitize_data(b"\x1b[3~") == b"[DEL]"
    assert p.sanitize_data(b"\x1b[4~") == b"[END]"
    assert p.sanitize_data(b"\x1b[5~") == b"[PGUP]"
    assert p.sanitize_data(b"\x1b[6~") == b"[PGDN]"
    # unknown code falls through to the generic token
    assert p.sanitize_data(b"\x1b[7~") == b"[CSI-7~]"
    assert p.sanitize_data(b"\x1b[11~") == b"[CSI-11~]"


def test_csi_digit_without_tilde_is_bare_esc():
    p = _port(sanitize_control=True)
    # ESC [ 5 x: digits consumed by the digit run, no ~ terminator -> only the
    # ESC is consumed ([ESC]); the '[' and digits survive as printables.
    assert p.sanitize_data(b"\x1b[5x") == b"[ESC][5x"
    # incomplete digit run at the end of the chunk: nothing is emitted, the
    # whole sequence is buffered for the next chunk
    assert p.sanitize_data(b"\x1b[12") == b""
    assert bytes(p._esc_buf) == b"\x1b[12"


def test_csi_unrecognized_third_is_bare_esc():
    p = _port(sanitize_control=True)
    assert p.sanitize_data(b"\x1b[ZX") == b"[ESC][ZX"
    # '[', 'm' are printable, so both survive after the bare-ESC token
    assert p.sanitize_data(b"\x1b[m") == b"[ESC][m"


def test_ss3_arrows():
    p = _port(sanitize_control=True)
    assert p.sanitize_data(b"\x1bOA") == b"[UP]"
    assert p.sanitize_data(b"\x1bOB") == b"[DOWN]"
    assert p.sanitize_data(b"\x1bOC") == b"[RIGHT]"
    assert p.sanitize_data(b"\x1bOD") == b"[LEFT]"
    # unknown SS3 letter -> [ESC] + the printable rest (O and the letter survive)
    assert p.sanitize_data(b"\x1bOH") == b"[ESC]OH"
    assert p.sanitize_data(b"\x1bOX") == b"[ESC]OX"


def test_bare_esc_variants():
    p = _port(sanitize_control=True)
    # ESC followed by an unrecognised prefix (not [ or O)
    assert p.sanitize_data(b"\x1bX") == b"[ESC]X"
    assert p.sanitize_data(b"\x1b \x1b[A") == b"[ESC] [UP]"


def test_ctrl_tokens_and_nul():
    p = _port(sanitize_control=True)
    assert p.sanitize_data(b"\x00") == b"[NUL]"
    assert p.sanitize_data(b"\x01") == b"[CTRL-A]"
    assert p.sanitize_data(b"\x19") == b"[CTRL-Y]"
    assert p.sanitize_data(b"\t") == b"[TAB]"
    # mixed: every C0 control between 0x01 and 0x1x
    p2 = _port(sanitize_control=True)
    assert p2.sanitize_data(bytes([0x02, 0x0B, 0x08])) == b"[CTRL-B][CTRL-K][CTRL-H]"


def test_incomplete_esc_at_end_buffers():
    p = _port(sanitize_control=True)
    # lone ESC at chunk end
    assert p.sanitize_data(b"\x1b") == b""
    assert bytes(p._esc_buf) == b"\x1b"
    # completes on next chunk
    assert p.sanitize_data(b"[A") == b"[UP]"
    assert bytes(p._esc_buf) == b""


def test_incomplete_csi_prefix_buffers():
    p = _port(sanitize_control=True)
    assert p.sanitize_data(b"hi\x1b[") == b"hi"
    assert bytes(p._esc_buf) == b"\x1b["
    assert p.sanitize_data(b"D") == b"[LEFT]"


def test_incomplete_ss3_prefix_buffers():
    p = _port(sanitize_control=True)
    assert p.sanitize_data(b"\x1bO") == b""
    assert bytes(p._esc_buf) == b"\x1bO"
    assert p.sanitize_data(b"C") == b"[RIGHT]"


def test_incomplete_digit_run_buffers_whole_sequence():
    p = _port(sanitize_control=True)
    # digits never terminated within the chunk -> buffered for completion
    assert p.sanitize_data(b"\x1b[3") == b""
    assert bytes(p._esc_buf) == b"\x1b[3"
    # the completion chunk arrives split so the terminator lands later
    assert p.sanitize_data(b"") == b""  # empty call does not consume
    assert p.sanitize_data(b"~") == b"[DEL]"
    assert bytes(p._esc_buf) == b""


def test_buffer_prepend_merges_with_new_chunk():
    p = _port(sanitize_control=True)
    # buffer holds an ESC; the next chunk carries the FULL next sequence
    p.sanitize_data(b"a\x1b")
    assert p.sanitize_data(b"[5~b") == b"[PGUP]b"
    # buffer holds an incomplete CSI; new chunk starts with more text
    p.sanitize_data(b"\x1b[1")
    assert p.sanitize_data(b"~text") == b"[HOME]text"


def test_mixed_stream_end_to_end():
    p = _port(sanitize_control=True)
    data = b"user\x1b[A\x1b[2~\thello\r\n\x00\xff\x1bOB"
    assert p.sanitize_data(data) == b"user[UP][INSERT][TAB]hello\r\n[NUL]\xff[DOWN]"
