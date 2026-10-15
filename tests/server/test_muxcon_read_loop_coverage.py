"""Behavioral coverage for UnifiedMuxConAdapter._read_loop (65-bracket S1).

Drives the real read loop by monkeypatching ``_read_frame`` to return a
script of frame dicts (an exhausted script returns None/EOF and ends the
loop) and replacing the side-effect collaborators (control processing,
data delivery, stream open/close bookkeeping, pumps, acks, telemetry,
connection close) with recorders. A real loopback socket pair supplies the
StreamReader/StreamWriter the loop requires: the writer is checked with
``isinstance(..., asyncio.StreamWriter)`` and must be a genuine one.

Written green against the PRE-refactor _read_loop (old span 3824-4125),
so it stays valid after the driver is split into _rl_* helpers.
"""

import asyncio
import logging
import time

import pytest

CID = "in:127.0.0.1:5555:1"


# ---------------------------------------------------------------------------
# Stream pair (loopback socket) — real StreamReader/StreamWriter
# ---------------------------------------------------------------------------
async def _make_stream_pair():
    server_side = {}

    async def handle(reader, writer):
        server_side["reader"] = reader
        server_side["writer"] = writer

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    client_reader, client_writer = await asyncio.open_connection(host, port)
    while "reader" not in server_side:
        await asyncio.sleep(0)
    return server, server_side["reader"], server_side["writer"], client_reader, client_writer


async def _teardown_pair(server, client_writer):
    client_writer.close()
    try:
        await client_writer.wait_closed()
    except Exception:
        pass
    server.close()
    try:
        await server.wait_closed()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Recorder collaborators
# ---------------------------------------------------------------------------
class _PM:
    def __init__(self):
        self.added = []

    async def add_client_to_port(self, port, feed_id, label, mode):
        self.added.append((port, feed_id, label, mode))
        return True


class _CM:
    def __init__(self):
        self.ports = []
        self.channels = []

    def register_client_port(self, feed_id, port):
        self.ports.append((feed_id, port))

    def register_client_channel(self, feed_id, adapter):
        self.channels.append((feed_id, adapter))


class _Rec:
    """Installs recorder stand-ins for every _read_loop side effect."""

    def __init__(self, ad, monkeypatch, script, auth=True, pm=None, cm=None):
        self.ad = ad
        self.calls = {
            "control": [],  # (conn_id, text)
            "data": [],  # (conn_id, sid, payload, seq)
            "acks": [],  # (writer, frame) data ACK sends
            "auth_notice": [],  # (writer, frame) AUTH:REQUIRED sends
            "stop": [],  # (peer_key, sid, reason)
            "telemetry": 0,
        }
        self.proto_calls = []

        async def _rf(reader, conn_id=None):
            return script.pop(0) if script else None

        monkeypatch.setattr(ad, "_read_frame", _rf)
        monkeypatch.setattr(ad, "_is_conn_authenticated", lambda cid: auth)

        async def _control(cid, writer, text):
            self.calls["control"].append((cid, text))

        async def _data(cid, sid, payload, seq):
            self.calls["data"].append((cid, sid, payload, seq))

        async def _stop(peer_key, sid, reason=""):
            self.calls["stop"].append((peer_key, sid, reason))

        async def _pump(peer_key, sid, port_name):
            return None

        async def _send(writer, frame):
            (self.calls["auth_notice"] if b"AUTH:REQUIRED" in frame else self.calls["acks"]).append((writer, frame))

        def _telemetry(key=None):
            self.calls["telemetry"] += 1
            return None

        async def _close(cid):
            self.calls["close_called"] = True

        for nm, fn in [
            ("_process_control_command", _control),
            ("_handle_inbound_data", _data),
            ("_stop_local_session", _stop),
            ("_pump_local_port_to_remote", _pump),
            ("_send_protocol_frame", _send),
            ("_update_peer_proxies_live_state", _telemetry),
        ]:
            monkeypatch.setattr(ad, nm, fn)
        self.closed = {"n": 0}

        async def _close2(cid):
            self.closed["n"] += 1

        monkeypatch.setattr(ad, "_close_connection", _close2)

        class _Proto:
            def __init__(self, calls):
                self._calls = calls

            def create_control_frame(self, stream_id, seq, payload):
                self._calls.append(("control", stream_id, seq, payload))
                return b"#" + str(stream_id).encode() + b":C:" + str(payload).encode()

            def create_ack_frame(self, acked, seq):
                self._calls.append(("ack", acked, seq))
                return b"#" + str(acked).encode() + b":A"

        ad.proto = _Proto(self.proto_calls)
        if pm is not None:
            ad._main_port_manager = pm
        if cm is not None:
            ad.console_manager = cm


def _adapter():
    from openmux.server.adapters.muxcon import UnifiedMuxConAdapter

    return UnifiedMuxConAdapter("mx", {"muxcon": {}})


def _frame(ftype, stream_id=None, payload=b"", seq=None):
    f = {"frame_type": ftype, "stream_id": stream_id, "payload": payload}
    if seq is not None:
        f["seq"] = seq
    return f


async def _run(monkeypatch, script, *, auth=True, pm=None, cm=None, conn=None, setup=None, override=None):
    """Run one _read_loop over `script` with recorders, then tear down.

    `setup(ad, rec)` runs before the loop so tests can pre-seed state.
    `override` is a {attr: value} dict applied after the recorder so tests
    can arm a collaborator to raise.
    """
    ad = _adapter()
    server, r, w, cr, cw = await _make_stream_pair()
    rec = _Rec(ad, monkeypatch, script, auth=auth, pm=pm, cm=cm)
    conn = conn if conn is not None else {"reader": r, "writer": w, "role": "server", "auth_ok": True}
    if "reader" not in conn:
        conn["reader"] = r
    if "writer" not in conn:
        conn["writer"] = w
    ad.connections[CID] = conn
    ad._wire_state[CID] = {"send_next": 1}  # _next_frame_seq needs an entry to build ACK frames
    if setup is not None:
        setup(ad, rec)
    if override:
        for name, val in override.items():
            monkeypatch.setattr(ad, name, val)
    try:
        await ad._read_loop(CID)
    finally:
        await _teardown_pair(server, cw)
    return ad, rec


def _key(ad):
    return ad._derive_peer_key_from_conn_id(CID)


# ---------------------------------------------------------------------------
# Guard / shutdown
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_conn_missing_returns(monkeypatch):
    ad = _adapter()
    await ad._read_loop("nope")  # no conn -> early return, no crash


@pytest.mark.asyncio
async def test_writer_missing_closes(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    ad = _adapter()
    server, r, w, cr, cw = await _make_stream_pair()
    closed = []

    async def _close(cid):
        closed.append(cid)

    monkeypatch.setattr(ad, "_close_connection", _close)
    try:
        ad.connections[CID] = {"reader": r, "writer": None, "role": "server", "auth_ok": True}
        await ad._read_loop(CID)
    finally:
        await _teardown_pair(server, cw)
    assert closed == [CID]
    assert "no valid writer" in caplog.text


@pytest.mark.asyncio
async def test_stop_event_breaks(monkeypatch):
    ad = _adapter()
    server, r, w, cr, cw = await _make_stream_pair()
    closed = []

    async def _close(cid):
        closed.append(cid)

    async def _never(reader, conn_id=None):
        await asyncio.sleep(30)  # only reached if the stop check is bypassed

    monkeypatch.setattr(ad, "_read_frame", _never)
    monkeypatch.setattr(ad, "_close_connection", _close)
    ad.connections[CID] = {"reader": r, "writer": w, "role": "server", "auth_ok": True}
    ad._stop_event.set()
    try:
        await asyncio.wait_for(ad._read_loop(CID), timeout=5)
    finally:
        ad._stop_event.clear()
        await _teardown_pair(server, cw)
    assert closed == [CID]


# ---------------------------------------------------------------------------
# Fault freeze
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fault_frozen_thaws_and_reads(monkeypatch):
    # frozen on entry; a first freeze poll thaws it, then one frame, then EOF.
    script = [_frame("C", payload=b"REQ:1", seq=1)]

    ad = _adapter()
    server, r, w, cr, cw = await _make_stream_pair()
    rec = _Rec(ad, monkeypatch, script)

    class _Flip(dict):
        _hits = 0

        def get(self, key, default=None):
            v = super().get(key, default)
            if key == CID and v is not None:
                _Flip._hits += 1
                if _Flip._hits >= 2:
                    return {}  # thaw after the first frozen poll
            return v

    ad._fault_state = _Flip({CID: {"frozen": True}})
    ad.connections[CID] = {"reader": r, "writer": w, "role": "server", "auth_ok": True}
    try:
        await asyncio.wait_for(ad._read_loop(CID), timeout=5)
    finally:
        await _teardown_pair(server, cw)
    assert rec.calls["control"] == [(CID, "REQ:1")]
    assert _Flip._hits >= 2  # proved at least one frozen poll happened


# ---------------------------------------------------------------------------
# RX diagnostics
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rx_diag_reorder_warns_once(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    # rseq (3) < last (5), no warn yet -> "Out-of-order" warn, flag set
    script = [_frame("D", stream_id=1, payload=b"x", seq=5), _frame("D", stream_id=1, payload=b"x", seq=3)]
    ad, rec = await _run(monkeypatch, script)
    assert "Out-of-order frame" in caplog.text
    assert "Sequence gap" not in caplog.text
    assert ad._rx_last_seq[CID] == 3
    assert ad._rx_order_warned[CID] is True


@pytest.mark.asyncio
async def test_rx_diag_gap_warns_once(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    # rseq (3) > last (1) + 1, no warn yet -> "Sequence gap" warn, flag set
    script = [_frame("D", stream_id=1, payload=b"x", seq=1), _frame("D", stream_id=1, payload=b"x", seq=3)]
    ad, rec = await _run(monkeypatch, script)
    assert "Sequence gap detected" in caplog.text
    assert "Out-of-order" not in caplog.text
    assert ad._rx_last_seq[CID] == 3
    assert ad._rx_order_warned[CID] is True


@pytest.mark.asyncio
async def test_rx_diag_inorder_no_warn(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    script = [_frame("D", stream_id=1, payload=b"x", seq=1), _frame("D", stream_id=1, payload=b"x", seq=2)]
    ad, rec = await _run(monkeypatch, script)
    assert "Sequence gap" not in caplog.text
    assert "Out-of-order" not in caplog.text
    assert ad._rx_last_seq[CID] == 2


@pytest.mark.asyncio
async def test_rx_diag_equal_seq_warns_once(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    # rseq == last (<=) and no prior warn -> out-of-order warn, then flag set
    script = [_frame("D", stream_id=1, payload=b"x", seq=2), _frame("D", stream_id=1, payload=b"x", seq=2)]
    ad, rec = await _run(monkeypatch, script)
    assert "Out-of-order" in caplog.text
    assert ad._rx_last_seq[CID] == 2


@pytest.mark.asyncio
async def test_rx_diag_bad_seq_swallowed_on_control(monkeypatch, caplog):
    # C handler never re-parses seq, so a bad seq only trips the diagnostic's
    # own int() (caught), and control is still processed. No "Read loop error".
    caplog.set_level(logging.WARNING)
    script = [_frame("C", payload=b"REQ:5", seq="notanint")]
    ad, rec = await _run(monkeypatch, script)
    assert rec.calls["control"] == [(CID, "REQ:5")]
    assert ad._rx_last_seq.get(CID) is None  # diagnostic's int() failed before the write
    assert "Read loop error" not in caplog.text


@pytest.mark.asyncio
async def test_data_bad_seq_aborts_loop(monkeypatch, caplog):
    # D handler's int(seq) for _handle_inbound_data is NOT wrapped, so a bad
    # seq raises to the loop's outer error handler. Pin that abort behavior.
    caplog.set_level(logging.WARNING)
    script = [_frame("D", stream_id=1, payload=b"x", seq="notanint")]
    ad, rec = await _run(monkeypatch, script)
    assert rec.calls["data"] == []  # never delivered
    assert "Read loop error" in caplog.text
    assert rec.closed["n"] >= 1


# ---------------------------------------------------------------------------
# C — control
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_control_processes_and_last_seen(monkeypatch):
    ad, rec = await _run(monkeypatch, [_frame("C", payload=b"REQ:5", seq=1)])
    assert rec.calls["control"] == [(CID, "REQ:5")]
    assert "last_seen" in ad.connections[CID]


def _seed_group(ad, rec):
    ad._mpath_groups[_key(ad)] = {"conns": {CID: {"last_seen": 0}}}


@pytest.mark.asyncio
async def test_control_updates_mpath_group(monkeypatch):
    ad, rec = await _run(monkeypatch, [_frame("C", payload=b"REQ:5", seq=1)], setup=_seed_group)
    conn = ad._mpath_groups[_key(ad)]["conns"][CID]
    assert conn["last_seen"] is not None
    assert conn["last_rx_seen"] is not None
    assert rec.calls["telemetry"] >= 1


# ---------------------------------------------------------------------------
# D — data
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_data_unauthenticated_auth_notice(monkeypatch):
    ad, rec = await _run(monkeypatch, [_frame("D", stream_id=1, payload=b"zz", seq=7)], auth=False)
    assert rec.calls["auth_notice"]
    assert rec.calls["data"] == []
    # RX diagnostics run before dispatch, so the seq IS recorded
    assert ad._rx_last_seq[CID] == 7
    assert any(c[0] == "control" and c[3] == "AUTH:REQUIRED" for c in rec.proto_calls)


async def _raising_send(writer, frame):
    raise RuntimeError("send boom")


@pytest.mark.asyncio
async def test_data_auth_notice_send_raises(monkeypatch):
    ad, rec = await _run(
        monkeypatch,
        [_frame("D", stream_id=1, payload=b"zz", seq=7)],
        auth=False,
        override={"_send_protocol_frame": _raising_send},
    )
    assert rec.calls["data"] == []  # still rejected


@pytest.mark.asyncio
async def test_data_delivered_and_acked(monkeypatch):
    ad, rec = await _run(monkeypatch, [_frame("D", stream_id=2, payload=b"body", seq=9)])
    assert rec.calls["data"] == [(CID, 2, b"body", 9)]
    assert any(c[0] == "ack" and c[1] == 9 for c in rec.proto_calls)
    assert ad._rx_last_seq[CID] == 9


async def _raising_ack_send(writer, frame):
    raise RuntimeError("ack send boom")


@pytest.mark.asyncio
async def test_data_ack_send_raises(monkeypatch):
    ad, rec = await _run(
        monkeypatch, [_frame("D", stream_id=1, payload=b"zz", seq=1)], override={"_send_protocol_frame": _raising_ack_send}
    )
    # delivered, then the ack send failed silently
    assert rec.calls["data"] == [(CID, 1, b"zz", 1)]
    assert ad._rx_last_seq[CID] == 1


@pytest.mark.asyncio
async def test_data_bad_sid_defaults_0(monkeypatch):
    ad, rec = await _run(monkeypatch, [_frame("D", stream_id="oops", payload=b"b", seq=4)])
    assert rec.calls["data"] == [(CID, 0, b"b", 4)]


# ---------------------------------------------------------------------------
# O — open stream
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_open_unauthenticated_auth_notice(monkeypatch):
    ad, rec = await _run(monkeypatch, [_frame("O", stream_id=1, payload=b"portA", seq=1)], auth=False)
    assert rec.calls["auth_notice"]
    assert 1 not in ad._local_session_map.get(_key(ad), {})


@pytest.mark.asyncio
async def test_open_registers_session(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    ad, rec = await _run(monkeypatch, [_frame("O", stream_id=5, payload=b"portA", seq=1)])
    assert ad._local_session_map[_key(ad)].get(5) == "portA"
    assert "OPEN stream" in caplog.text


@pytest.mark.asyncio
async def test_open_registers_federated_client(monkeypatch):
    pm, cm = _PM(), _CM()
    ad, rec = await _run(monkeypatch, [_frame("O", stream_id=5, payload=b"portA", seq=1)], pm=pm, cm=cm)
    expect = "fed:%s:5" % _key(ad)
    assert (expect, "portA") in cm.ports
    assert (expect, ad) in cm.channels
    assert pm.added and pm.added[0][0] == "portA"


def _seed_old_session(ad, rec):
    ad._local_session_map[_key(ad)] = {5: "portOld"}


@pytest.mark.asyncio
async def test_open_reused_stream_warns(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    ad, rec = await _run(monkeypatch, [_frame("O", stream_id=5, payload=b"portNew", seq=1)], setup=_seed_old_session)
    assert any(s[2] == "stream_id_reused" for s in rec.calls["stop"])
    assert ad._local_session_map[_key(ad)][5] == "portNew"


def _seed_same_session(ad, rec):
    ad._local_session_map[_key(ad)] = {5: "portA"}


@pytest.mark.asyncio
async def test_open_duplicate_same_port_keeps(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    ad, rec = await _run(monkeypatch, [_frame("O", stream_id=5, payload=b"portA", seq=1)], setup=_seed_same_session)
    assert ad._local_session_map[_key(ad)][5] == "portA"
    assert rec.calls["stop"] == []


@pytest.mark.asyncio
async def test_open_bad_sid_default_0(monkeypatch):
    ad, rec = await _run(monkeypatch, [_frame("O", stream_id="bad", payload=b"portA", seq=1)])
    assert ad._local_session_map[_key(ad)].get(0) == "portA"


@pytest.mark.asyncio
async def test_open_empty_port_name(monkeypatch):
    ad, rec = await _run(monkeypatch, [_frame("O", stream_id=5, payload=b"   ", seq=1)])
    assert ad._local_session_map[_key(ad)].get(5) == ""


class _BadPM(_PM):
    async def add_client_to_port(self, *a):
        raise RuntimeError("pm boom")


@pytest.mark.asyncio
async def test_open_fedrw_register_raises(monkeypatch):
    pm, cm = _BadPM(), _CM()
    ad, rec = await _run(monkeypatch, [_frame("O", stream_id=5, payload=b"portA", seq=1)], pm=pm, cm=cm)
    # registration failed but the session mapping is still recorded
    assert ad._local_session_map[_key(ad)].get(5) == "portA"


# ---------------------------------------------------------------------------
# E — close stream
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_close_unauthenticated_auth_notice(monkeypatch):
    ad, rec = await _run(monkeypatch, [_frame("E", stream_id=1, seq=1)], auth=False)
    assert rec.calls["auth_notice"]


def _seed_session(ad, rec):
    ad._session_map[_key(ad)] = {7: object()}


@pytest.mark.asyncio
async def test_close_stops_session(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    ad, rec = await _run(monkeypatch, [_frame("E", stream_id=7, seq=1)], setup=_seed_session)
    assert rec.calls["stop"] == [(_key(ad), 7, "stream_close")]
    assert 7 not in ad._session_map.get(_key(ad), {})
    assert "CLOSE stream" in caplog.text


@pytest.mark.asyncio
async def test_close_no_session_entry(monkeypatch):
    ad, rec = await _run(monkeypatch, [_frame("E", stream_id=9, seq=1)])
    assert rec.calls["stop"] == [(_key(ad), 9, "stream_close")]


async def _raising_stop(peer_key, sid, reason=""):
    raise RuntimeError("stop boom")


@pytest.mark.asyncio
async def test_close_cleanup_raises(monkeypatch):
    ad, rec = await _run(
        monkeypatch, [_frame("E", stream_id=1, seq=1)], setup=_seed_session, override={"_stop_local_session": _raising_stop}
    )
    # cleanup swallowed; loop exits normally
    assert rec.closed["n"] == 1


@pytest.mark.asyncio
async def test_close_bad_sid_default_0(monkeypatch):
    ad, rec = await _run(monkeypatch, [_frame("E", stream_id="bad", seq=1)])
    assert rec.calls["stop"] == [(_key(ad), 0, "stream_close")]


# ---------------------------------------------------------------------------
# HB — heartbeat
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_heartbeat_process_control(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    ad, rec = await _run(monkeypatch, [_frame("HB", payload=b"REQ:111", seq=1)])
    assert rec.calls["control"] == [(CID, "REQ:111")]


@pytest.mark.asyncio
async def test_heartbeat_updates_mpath_group(monkeypatch):
    ad, rec = await _run(monkeypatch, [_frame("HB", payload=b"REQ:111", seq=1)], setup=_seed_group)
    conn = ad._mpath_groups[_key(ad)]["conns"][CID]
    assert conn["last_seen"] is not None
    assert conn["last_rx_seen"] is not None
    assert rec.calls["telemetry"] >= 1


# ---------------------------------------------------------------------------
# A — data ACK
# ---------------------------------------------------------------------------
def _seed_sendbuf(ad, rec):
    ad._peer_sendbuf[_key(ad)] = {42: (CID, 0, b"x", time.time()), 43: (CID, 0, b"y", time.time())}


@pytest.mark.asyncio
async def test_ack_clears_sendbuf(monkeypatch):
    ad, rec = await _run(monkeypatch, [_frame("A", payload=b"42", seq=1)], setup=_seed_sendbuf)
    buf = ad._peer_sendbuf[_key(ad)]
    assert 42 not in buf and 43 in buf


@pytest.mark.asyncio
async def test_ack_not_in_buf_ignored(monkeypatch):
    ad, rec = await _run(monkeypatch, [_frame("A", payload=b"99", seq=1)], setup=_seed_sendbuf)
    assert 99 not in ad._peer_sendbuf.get(_key(ad), {})
    assert ad._peer_sendbuf[_key(ad)].get(42) is not None


@pytest.mark.asyncio
async def test_ack_bad_payload_swallowed(monkeypatch):
    ad, rec = await _run(monkeypatch, [_frame("A", payload=b"notanum", seq=1)], setup=_seed_sendbuf)
    assert ad._peer_sendbuf[_key(ad)].get(42) is not None  # nothing popped


@pytest.mark.asyncio
async def test_ack_no_sendbuf(monkeypatch):
    ad, rec = await _run(monkeypatch, [_frame("A", payload=b"42", seq=1)])
    assert _key(ad) not in ad._peer_sendbuf


# ---------------------------------------------------------------------------
# Advisory arms, mpath groups, pump tasks, cancellation (old-span 100%)
# ---------------------------------------------------------------------------
def _boom(*a, **k):
    raise RuntimeError("boom")


class _BoomLastSeen(dict):
    """A connection record whose last_seen assignment raises.

    Targets exactly the first write inside the last_seen bookkeeping block
    so its outer advisory arm is exercised, without touching the global
    time module (a time.time() stub would also break logging.makeRecord).
    """

    def __setitem__(self, key, value):
        if key == "last_seen":
            raise RuntimeError("boom")
        super().__setitem__(key, value)


def _boom_conn():
    c = _BoomLastSeen()
    c["role"] = "server"
    c["auth_ok"] = True
    return c


@pytest.mark.asyncio
async def test_control_telemetry_raises(monkeypatch):
    ad, rec = await _run(
        monkeypatch, [_frame("C", payload=b"REQ:5", seq=1)], override={"_update_peer_proxies_live_state": _boom}
    )
    assert rec.calls["control"] == [(CID, "REQ:5")]


@pytest.mark.asyncio
async def test_control_last_seen_outer_raises(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    ad, rec = await _run(
        monkeypatch, [_frame("C", payload=b"REQ:5", seq=1)], override={"_derive_peer_key_from_conn_id": _boom}
    )
    assert rec.calls["control"] == [(CID, "REQ:5")]
    assert "Read loop error" not in caplog.text


@pytest.mark.asyncio
async def test_data_updates_mpath_group(monkeypatch):
    ad, rec = await _run(monkeypatch, [_frame("D", stream_id=1, payload=b"x", seq=1)], setup=_seed_group)
    conn = ad._mpath_groups[_key(ad)]["conns"][CID]
    assert conn["last_seen"] is not None
    assert conn["last_rx_seen"] is not None
    assert rec.calls["telemetry"] >= 1
    assert rec.calls["data"] == [(CID, 1, b"x", 1)]


@pytest.mark.asyncio
async def test_data_telemetry_raises(monkeypatch):
    ad, rec = await _run(
        monkeypatch, [_frame("D", stream_id=1, payload=b"x", seq=1)], override={"_update_peer_proxies_live_state": _boom}
    )
    assert rec.calls["data"] == [(CID, 1, b"x", 1)]
    assert any(c[0] == "ack" and c[1] == 1 for c in rec.proto_calls)


@pytest.mark.asyncio
async def test_data_last_seen_outer_raises(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    ad, rec = await _run(
        monkeypatch, [_frame("D", stream_id=1, payload=b"x", seq=1)], override={"_derive_peer_key_from_conn_id": _boom}
    )
    assert rec.calls["data"] == [(CID, 1, b"x", 1)]
    assert "Read loop error" not in caplog.text


@pytest.mark.asyncio
async def test_open_updates_mpath_group(monkeypatch):
    ad, rec = await _run(monkeypatch, [_frame("O", stream_id=5, payload=b"portA", seq=1)], setup=_seed_group)
    conn = ad._mpath_groups[_key(ad)]["conns"][CID]
    assert conn["last_rx_seen"] is not None
    assert rec.calls["telemetry"] >= 1
    assert ad._local_session_map[_key(ad)].get(5) == "portA"


@pytest.mark.asyncio
async def test_open_telemetry_raises(monkeypatch):
    ad, rec = await _run(
        monkeypatch, [_frame("O", stream_id=5, payload=b"portA", seq=1)], override={"_update_peer_proxies_live_state": _boom}
    )
    assert ad._local_session_map[_key(ad)].get(5) == "portA"


@pytest.mark.asyncio
async def test_open_last_seen_outer_raises(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    ad, rec = await _run(monkeypatch, [_frame("O", stream_id=5, payload=b"portA", seq=1)], conn=_boom_conn())
    assert ad._local_session_map[_key(ad)].get(5) == "portA"
    assert "Read loop error" not in caplog.text


@pytest.mark.asyncio
async def test_open_bad_payload_decode_empty(monkeypatch):
    # payload is a str -> .decode raises -> port_name defaults to ""
    ad, rec = await _run(monkeypatch, [_frame("O", stream_id=5, payload="notbytes", seq=1)])
    assert ad._local_session_map[_key(ad)].get(5) == ""


@pytest.mark.asyncio
async def test_open_reuses_existing_pump(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    state = {}

    def _setup(ad, rec):
        ad._local_session_map[_key(ad)] = {5: "portA"}
        t = asyncio.ensure_future(asyncio.sleep(3600))
        ad._pump_tasks[(_key(ad), 5)] = t
        state["t"] = t

    ad, rec = await _run(monkeypatch, [_frame("O", stream_id=5, payload=b"portA", seq=1)], setup=_setup)
    assert "reusing existing pump" in caplog.text
    assert state["t"] == ad._pump_tasks[(_key(ad), 5)]  # kept, not replaced
    if not state["t"].done():
        state["t"].cancel()
        try:
            await state["t"]
        except BaseException:
            pass


@pytest.mark.asyncio
async def test_open_cancels_stale_pump(monkeypatch):
    state = {}

    def _setup(ad, rec):
        ad._local_session_map[_key(ad)] = {5: "portOld"}
        t = asyncio.ensure_future(asyncio.sleep(3600))
        ad._pump_tasks[(_key(ad), 5)] = t
        state["t"] = t

    ad, rec = await _run(monkeypatch, [_frame("O", stream_id=5, payload=b"portA", seq=1)], setup=_setup)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert state["t"].cancelled()  # stale pump for a different port was cancelled
    slot_task = ad._pump_tasks[(_key(ad), 5)]
    assert slot_task is not state["t"]
    assert slot_task.done()  # fresh pump (recorder) already ran


@pytest.mark.asyncio
async def test_close_unauth_notice_send_raises(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    ad, rec = await _run(
        monkeypatch, [_frame("E", stream_id=1, seq=1)], auth=False, override={"_send_protocol_frame": _raising_send}
    )
    assert rec.calls["stop"] == []
    assert rec.closed["n"] == 1
    assert "Read loop error" not in caplog.text


@pytest.mark.asyncio
async def test_open_unauth_notice_send_raises(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    ad, rec = await _run(
        monkeypatch,
        [_frame("O", stream_id=1, payload=b"portA", seq=1)],
        auth=False,
        override={"_send_protocol_frame": _raising_send},
    )
    assert rec.calls["stop"] == []
    assert 1 not in ad._local_session_map.get(_key(ad), {})
    assert "Read loop error" not in caplog.text


@pytest.mark.asyncio
async def test_close_updates_mpath_group(monkeypatch):
    def _setup(ad, rec):
        _seed_group(ad, rec)
        _seed_session(ad, rec)

    ad, rec = await _run(monkeypatch, [_frame("E", stream_id=7, seq=1)], setup=_setup)
    conn = ad._mpath_groups[_key(ad)]["conns"][CID]
    assert conn["last_rx_seen"] is not None
    assert rec.calls["telemetry"] >= 1
    assert rec.calls["stop"] == [(_key(ad), 7, "stream_close")]


@pytest.mark.asyncio
async def test_close_telemetry_raises(monkeypatch):
    ad, rec = await _run(
        monkeypatch,
        [_frame("E", stream_id=7, seq=1)],
        setup=_seed_session,
        override={"_update_peer_proxies_live_state": _boom},
    )
    assert rec.calls["stop"] == [(_key(ad), 7, "stream_close")]


@pytest.mark.asyncio
async def test_close_last_seen_outer_raises(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    ad, rec = await _run(monkeypatch, [_frame("E", stream_id=7, seq=1)], setup=_seed_session, conn=_boom_conn())
    assert rec.calls["stop"] == [(_key(ad), 7, "stream_close")]
    assert "Read loop error" not in caplog.text


@pytest.mark.asyncio
async def test_heartbeat_telemetry_raises(monkeypatch):
    ad, rec = await _run(
        monkeypatch, [_frame("HB", payload=b"REQ:111", seq=1)], override={"_update_peer_proxies_live_state": _boom}
    )
    assert rec.calls["control"] == [(CID, "REQ:111")]


@pytest.mark.asyncio
async def test_heartbeat_mpath_advisory_raises(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    ad, rec = await _run(
        monkeypatch, [_frame("HB", payload=b"REQ:111", seq=1)], override={"_derive_peer_key_from_conn_id": _boom}
    )
    assert rec.calls["control"] == [(CID, "REQ:111")]
    assert "Read loop error" not in caplog.text


@pytest.mark.asyncio
async def test_heartbeat_outer_bookkeeping_raises(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    ad, rec = await _run(monkeypatch, [_frame("HB", payload=b"REQ:111", seq=1)], conn=_boom_conn())
    # the HB control processing lives outside the bookkeeping try, so it runs
    assert rec.calls["control"] == [(CID, "REQ:111")]
    assert "Read loop error" not in caplog.text


@pytest.mark.asyncio
async def test_freeze_poll_cancel_breaks(monkeypatch):
    # A CancelledError during the freeze's 0.2s wait: the old code's freeze
    # arm catches it and breaks; the refactored helper lets it propagate to
    # the loop's CancelledError arm. Either way: closed exactly once, no frame.
    sleeps = {"n": 0}

    async def _cancel_once(delay):
        sleeps["n"] += 1
        raise asyncio.CancelledError()

    ad = _adapter()
    server, r, w, cr, cw = await _make_stream_pair()
    closed = []

    async def _close(cid):
        closed.append(cid)

    async def _never(reader, conn_id=None):
        assert False, "read_frame must not be reached while frozen"

    monkeypatch.setattr(asyncio, "sleep", _cancel_once)
    monkeypatch.setattr(ad, "_read_frame", _never)
    monkeypatch.setattr(ad, "_close_connection", _close)
    ad._fault_state = {CID: {"frozen": True}}
    ad.connections[CID] = {"reader": r, "writer": w, "role": "server", "auth_ok": True}
    try:
        await ad._read_loop(CID)
    finally:
        await _teardown_pair(server, cw)
    assert closed == [CID]
    assert sleeps["n"] == 1


@pytest.mark.asyncio
async def test_freeze_poll_stop_during_wait(monkeypatch):
    # Stop event set while waiting inside the freeze poll: the helper yields
    # None and the loop breaks out cleanly through the finally.
    ad = _adapter()
    server, r, w, cr, cw = await _make_stream_pair()
    closed = []

    async def _close(cid):
        closed.append(cid)

    async def _set_stop(delay):
        ad._stop_event.set()

    async def _never(reader, conn_id=None):
        assert False, "read_frame must not be reached while frozen"

    monkeypatch.setattr(asyncio, "sleep", _set_stop)
    monkeypatch.setattr(ad, "_read_frame", _never)
    monkeypatch.setattr(ad, "_close_connection", _close)
    ad._fault_state = {CID: {"frozen": True}}
    ad.connections[CID] = {"reader": r, "writer": w, "role": "server", "auth_ok": True}
    try:
        await ad._read_loop(CID)
    finally:
        ad._stop_event.clear()
        await _teardown_pair(server, cw)
    assert closed == [CID]


@pytest.mark.asyncio
async def test_cancellation_swallowed(monkeypatch):
    # A real task cancellation landing in _read_frame: the loop's
    # CancelledError arm swallows it (no re-raise) and still closes.
    ad = _adapter()
    server, r, w, cr, cw = await _make_stream_pair()
    closed = []

    async def _close(cid):
        closed.append(cid)

    async def _block(reader, conn_id=None):
        await asyncio.sleep(30)

    monkeypatch.setattr(ad, "_read_frame", _block)
    monkeypatch.setattr(ad, "_close_connection", _close)
    ad.connections[CID] = {"reader": r, "writer": w, "role": "server", "auth_ok": True}
    task = asyncio.get_event_loop().create_task(ad._read_loop(CID))
    await asyncio.sleep(0.05)
    task.cancel()
    await task
    await _teardown_pair(server, cw)
    assert closed == [CID]


# ---------------------------------------------------------------------------
# Mixed dispatch
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_mixed_frames_dispatch(monkeypatch):
    script = [
        _frame("C", payload=b"REQ:1", seq=1),
        _frame("D", stream_id=1, payload=b"d", seq=2),
        _frame("O", stream_id=2, payload=b"p1", seq=3),
        _frame("E", stream_id=2, seq=4),
        _frame("HB", payload=b"ACK:1", seq=5),
        _frame("A", payload=b"7", seq=6),
    ]
    ad, rec = await _run(monkeypatch, script, pm=_PM(), cm=_CM())
    assert rec.calls["control"] == [(CID, "REQ:1"), (CID, "ACK:1")]
    assert len(rec.calls["data"]) == 1
    assert rec.calls["stop"] == [(_key(ad), 2, "stream_close")]
    assert rec.closed["n"] == 1
