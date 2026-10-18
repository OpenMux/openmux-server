"""Issue #88: OMXCTRL control-frame framing in the TCP client read path.

TCP coalesces writes, so a control frame can arrive stuck behind port data
(the detach ack follows its ``DISCONNECTED:`` line) or split across read
chunks. `_split_control_frames` must extract every complete frame from
anywhere in a chunk and pass everything else through as port data.
"""

import json

from openmux.client.adapters.tcp_adapter import CTRL_MARKER, TcpClientAdapter


def _adapter() -> TcpClientAdapter:
    return TcpClientAdapter("127.0.0.1", 1)


def _frame(payload) -> bytes:
    return CTRL_MARKER + json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"


DETACH_ACK = {"type": "detached", "ok": True, "port": "loop1"}


def test_plain_data_passes_through_unchanged():
    a = _adapter()
    assert a._split_control_frames(b"hello world") == b"hello world"
    assert a._ctrl_rx_frag == b""


def test_frame_alone_is_extracted():
    a = _adapter()
    assert a._split_control_frames(_frame(DETACH_ACK)) == b""
    assert a.last_detach_reply == DETACH_ACK


def test_frame_after_data_is_still_extracted():
    a = _adapter()
    out = a._split_control_frames(b"DISCONNECTED:loop1\n" + _frame(DETACH_ACK))
    assert out == b"DISCONNECTED:loop1\n"
    assert a.last_detach_reply == DETACH_ACK


def test_frame_before_trailing_data():
    a = _adapter()
    out = a._split_control_frames(_frame(DETACH_ACK) + b"port bytes")
    assert out == b"port bytes"
    assert a.last_detach_reply == DETACH_ACK


def test_two_frames_keep_data_order():
    a = _adapter()
    out = a._split_control_frames(b"A" + _frame(DETACH_ACK) + b"B" + _frame(DETACH_ACK) + b"C")
    assert out == b"ABC"
    assert a.last_detach_reply == DETACH_ACK


def test_half_frame_waits_for_next_chunk():
    a = _adapter()
    whole = b"xy" + _frame(DETACH_ACK) + b"tail"
    cut = len(b"xy") + len(CTRL_MARKER) + 5  # split inside the JSON body
    assert a._split_control_frames(whole[:cut]) == b"xy"
    assert a.last_detach_reply is None
    assert a._ctrl_rx_frag  # complete marker held back for framing
    assert a._split_control_frames(whole[cut:]) == b"tail"
    assert a.last_detach_reply == DETACH_ACK
    assert a._ctrl_rx_frag == b""


def test_marker_never_completed_by_cap_flushes_as_data():
    a = _adapter()
    junk = CTRL_MARKER + b"x" * 70000  # over the 64 KiB frame cap
    out = a._split_control_frames(junk)
    assert out == junk  # oversized tail passes through instead of buffering
    assert a._ctrl_rx_frag == b""


def test_oversized_pending_half_frame_passes_through():
    a = _adapter()
    a._ctrl_rx_frag = CTRL_MARKER + b"y" * 70000
    out = a._split_control_frames(b"")
    assert out == CTRL_MARKER + b"y" * 70000
    assert a._ctrl_rx_frag == b""


def test_malformed_frame_body_silently_consumed():
    a = _adapter()
    out = a._split_control_frames(CTRL_MARKER + b"{not json\n" + b"keep")
    assert out == b"keep"
    assert a.last_detach_reply is None
