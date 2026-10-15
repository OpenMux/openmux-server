"""Pinning tests for UnifiedMuxConAdapter._process_control_command.

This file is written against the ORIGINAL 300-line method and must pass
unmodified before the 70-bracket refactor replaces the body with
_process_control_command + _ctl_* helpers. The refactor is pure
extract-method, so every observable behavior (frame bytes sent, state
mutations, log lines, close/advertise/filter calls) is pinned here.

The method is async and does NOT type-check the writer, so a plain
DummyWriter plus monkeypatched collaborators is sufficient: no socket
pair, no real I/O, and (except for the two Ed25519 paths) no real crypto.
"""

import base64
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openmux.server.adapters.muxcon import UnifiedMuxConAdapter


class DummyWriter:
    def __init__(self):
        self.buffer = bytearray()
        self._closed = False

    def write(self, data: bytes):
        self.buffer.extend(data)

    async def drain(self):
        return

    def is_closing(self):
        return self._closed

    def close(self):
        self._closed = True

    async def wait_closed(self):
        return


class _RecLog:
    def __init__(self, rec, debug_raises=False):
        self._rec = rec
        self._debug_raises = debug_raises

    def _cap(self, lvl, args):
        try:
            msg = args[0] % args[1:] if len(args) > 1 else args[0]
        except Exception:
            msg = str(args)
        self._rec.log.append((lvl, msg))

    def debug(self, *a, **kw):
        if self._debug_raises:
            raise RuntimeError("debug boom")
        self._cap("debug", a)

    def info(self, *a, **kw):
        self._cap("info", a)

    def warning(self, *a, **kw):
        self._cap("warning", a)

    def error(self, *a, **kw):
        self._cap("error", a)


class Rec:
    def __init__(self):
        self.frames = []  # bytes passed to _send_protocol_frame
        self.closed = []
        self.advertised = []
        self.filters = []  # (conn_id, key_id)
        self.handlers = {}  # handler_name -> [args tuple]
        self.log = []  # (level, msg) tuples captured from the adapter logger

    def log_msgs(self, lvl):
        return [m for l, m in self.log if l == lvl]


def _mk(monkeypatch, *, auth_required=True, debug_raises=False):
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    ad._auth_required = auth_required
    rec = Rec()
    ad.logger = _RecLog(rec, debug_raises=debug_raises)

    async def _send(writer, frame):
        rec.frames.append(frame)

    async def _close(conn_id):
        rec.closed.append(conn_id)

    async def _adv(conn_id):
        rec.advertised.append(conn_id)

    def _filt(conn_id, key_id):
        rec.filters.append((conn_id, key_id))

    monkeypatch.setattr(ad, "_send_protocol_frame", _send)
    monkeypatch.setattr(ad, "_close_connection", _close)
    monkeypatch.setattr(ad, "_maybe_advertise_local_ports", _adv)
    monkeypatch.setattr(ad, "_apply_per_connection_filters", _filt)
    return ad, rec


def _arm_conn(ad, conn_id, *, role="server", auth_ok=True, extra=None):
    ad.connections[conn_id] = {"role": role, "auth_ok": auth_ok}
    if extra:
        ad.connections[conn_id].update(extra)
    ad._wire_state[conn_id] = {"send_next": 1}


def _frame_str(b: bytes) -> str:
    return b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else str(b)


async def _run(ad, rec, conn_id, payload):
    writer = DummyWriter()
    await ad._process_control_command(conn_id, writer, payload)
    return [_frame_str(f) for f in rec.frames]


# ---------------------------------------------------------------------------
# AUTH:PK:CHALLENGE
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_auth_challenge_matching_key_sends_response(monkeypatch):
    ad, rec = _mk(monkeypatch)
    priv = Ed25519PrivateKey.generate()
    ad._auth_priv = priv
    ad._auth_key_id = "kid1"
    _arm_conn(ad, "c1", role="client", auth_ok=False)
    nonce_b64 = base64.b64encode(b"N" * 32).decode()
    frames = await _run(ad, rec, "c1", f"AUTH:PK:CHALLENGE:kid1:{nonce_b64}")
    assert any("AUTH:PK:RESPONSE:kid1:" in f for f in frames)
    assert rec.closed == []


@pytest.mark.asyncio
async def test_auth_challenge_no_key_sends_error(monkeypatch):
    ad, rec = _mk(monkeypatch)  # no _auth_priv
    _arm_conn(ad, "c1", role="client", auth_ok=False)
    nonce_b64 = base64.b64encode(b"N" * 32).decode()
    frames = await _run(ad, rec, "c1", f"AUTH:PK:CHALLENGE:kid1:{nonce_b64}")
    assert any("AUTH:ERROR:no_client_key" in f for f in frames)


@pytest.mark.asyncio
async def test_auth_challenge_key_mismatch_sends_error(monkeypatch):
    ad, rec = _mk(monkeypatch)
    priv = Ed25519PrivateKey.generate()
    ad._auth_priv = priv
    ad._auth_key_id = "ours"  # != the kid in the payload
    _arm_conn(ad, "c1", role="client", auth_ok=False)
    nonce_b64 = base64.b64encode(b"N" * 32).decode()
    frames = await _run(ad, rec, "c1", f"AUTH:PK:CHALLENGE:theirs:{nonce_b64}")
    assert any("AUTH:ERROR:no_client_key" in f for f in frames)


@pytest.mark.asyncio
async def test_auth_challenge_sign_raises_is_swallowed(monkeypatch):
    ad, rec = _mk(monkeypatch)

    class _Priv:
        ok = True

        def sign(self, nonce):
            raise RuntimeError("sign boom")

    ad._auth_priv = _Priv()
    ad._auth_key_id = "kid1"
    _arm_conn(ad, "c1", role="client", auth_ok=False)
    nonce_b64 = base64.b64encode(b"N" * 32).decode()
    frames = await _run(ad, rec, "c1", f"AUTH:PK:CHALLENGE:kid1:{nonce_b64}")
    # inner CHALLENGE except swallows; no frame, no outer error
    assert frames == []
    assert "Error processing control command" not in " ".join(rec.log_msgs("error"))


# ---------------------------------------------------------------------------
# AUTH:PK:RESPONSE
# ---------------------------------------------------------------------------
def _response_setup(ad, conn_id, *, sig, expires=30, kid="kid1", nonce=b"N" * 32, type_=None, key_id=None):
    st = {"type": "pk", "key_id": kid, "nonce": nonce, "expires_at": time.time() + expires}
    if type_ is not None:
        st["type"] = type_
    if key_id is not None:
        st["key_id"] = key_id
    # A pending challenge/response connection is NOT yet authenticated.
    _arm_conn(ad, conn_id, auth_ok=False, extra={"auth_state": st})


@pytest.mark.asyncio
async def test_auth_response_ok(monkeypatch):
    ad, rec = _mk(monkeypatch)
    priv = Ed25519PrivateKey.generate()
    ad._auth_pubkeys = {"kid1": priv.public_key()}
    nonce = b"N" * 32
    sig = base64.b64encode(priv.sign(nonce)).decode()
    _response_setup(ad, "c1", sig=sig)
    frames = await _run(ad, rec, "c1", f"AUTH:PK:RESPONSE:kid1:{sig}")
    assert any(f.strip() == "AUTH:OK" or "AUTH:OK" in f for f in frames)
    assert ad.connections["c1"]["auth_ok"] is True
    assert "auth_state" not in ad.connections["c1"]
    assert rec.filters == [("c1", "kid1")]
    assert rec.advertised == ["c1"]
    assert rec.closed == []


@pytest.mark.asyncio
async def test_auth_response_expired_closes(monkeypatch):
    ad, rec = _mk(monkeypatch)
    priv = Ed25519PrivateKey.generate()
    ad._auth_pubkeys = {"kid1": priv.public_key()}
    sig = base64.b64encode(priv.sign(b"N" * 32)).decode()
    _response_setup(ad, "c1", sig=sig, expires=-5)
    frames = await _run(ad, rec, "c1", f"AUTH:PK:RESPONSE:kid1:{sig}")
    assert any("AUTH:ERROR:expired" in f for f in frames)
    assert rec.closed == ["c1"]
    assert ad.connections["c1"]["auth_ok"] is False


@pytest.mark.asyncio
async def test_auth_response_bad_signature_closes(monkeypatch):
    ad, rec = _mk(monkeypatch)
    priv = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()  # signed by a different key
    ad._auth_pubkeys = {"kid1": priv.public_key()}
    bad_sig = base64.b64encode(other.sign(b"N" * 32)).decode()
    _response_setup(ad, "c1", sig=bad_sig)
    frames = await _run(ad, rec, "c1", f"AUTH:PK:RESPONSE:kid1:{bad_sig}")
    assert any("AUTH:ERROR:bad_signature" in f for f in frames)
    assert rec.closed == ["c1"]


@pytest.mark.asyncio
async def test_auth_response_unknown_pubkey_closes(monkeypatch):
    ad, rec = _mk(monkeypatch)
    priv = Ed25519PrivateKey.generate()
    ad._auth_pubkeys = {}  # kid1 not present
    sig = base64.b64encode(priv.sign(b"N" * 32)).decode()
    _response_setup(ad, "c1", sig=sig)
    frames = await _run(ad, rec, "c1", f"AUTH:PK:RESPONSE:kid1:{sig}")
    assert any("AUTH:ERROR:bad_signature" in f for f in frames)
    assert rec.closed == ["c1"]


@pytest.mark.asyncio
async def test_auth_response_wrong_state_type_ignored(monkeypatch):
    ad, rec = _mk(monkeypatch)
    priv = Ed25519PrivateKey.generate()
    ad._auth_pubkeys = {"kid1": priv.public_key()}
    sig = base64.b64encode(priv.sign(b"N" * 32)).decode()
    _response_setup(ad, "c1", sig=sig, type_="x509")  # not "pk"
    frames = await _run(ad, rec, "c1", f"AUTH:PK:RESPONSE:kid1:{sig}")
    assert frames == []
    assert rec.closed == []
    assert ad.connections["c1"]["auth_ok"] is False


@pytest.mark.asyncio
async def test_auth_response_no_auth_state_ignored(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1")  # no auth_state
    frames = await _run(ad, rec, "c1", "AUTH:PK:RESPONSE:kid1:AAAA")
    assert frames == []
    assert rec.closed == []


@pytest.mark.asyncio
async def test_auth_response_filters_raise_is_swallowed(monkeypatch):
    ad, rec = _mk(monkeypatch)
    priv = Ed25519PrivateKey.generate()
    ad._auth_pubkeys = {"kid1": priv.public_key()}
    sig = base64.b64encode(priv.sign(b"N" * 32)).decode()

    def _boom(conn_id, key_id):
        raise RuntimeError("filter boom")

    monkeypatch.setattr(ad, "_apply_per_connection_filters", _boom)
    _response_setup(ad, "c1", sig=sig)
    frames = await _run(ad, rec, "c1", f"AUTH:PK:RESPONSE:kid1:{sig}")
    # filters failed (logged) but AUTH:OK still sent and advertised
    assert any("AUTH:OK" in f for f in frames)
    assert ("error", "muxcon: failed to apply per-connection filters for conn=c1") in rec.log
    assert rec.advertised == ["c1"]


# ---------------------------------------------------------------------------
# AUTH:OK
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_auth_ok_client_role_auths(monkeypatch):
    ad, rec = _mk(monkeypatch)
    ad._auth_key_id = "ours"
    _arm_conn(ad, "c1", role="client", auth_ok=False)
    frames = await _run(ad, rec, "c1", "AUTH:OK")
    assert ad.connections["c1"]["auth_ok"] is True
    assert rec.filters == [("c1", "ours")]
    assert rec.advertised == ["c1"]
    assert rec.closed == []
    assert frames == []


@pytest.mark.asyncio
async def test_auth_ok_no_client_key_no_filters(monkeypatch):
    ad, rec = _mk(monkeypatch)
    ad._auth_key_id = None
    _arm_conn(ad, "c1", role="client", auth_ok=False)
    await _run(ad, rec, "c1", "AUTH:OK")
    assert ad.connections["c1"]["auth_ok"] is True
    assert rec.filters == []  # no key id -> no filter call
    assert rec.advertised == ["c1"]


@pytest.mark.asyncio
async def test_auth_ok_non_client_role_closes(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1", role="server", auth_ok=False)
    await _run(ad, rec, "c1", "AUTH:OK")
    assert rec.closed == ["c1"]
    assert ad.connections["c1"]["auth_ok"] is False
    assert any("self-authentication attempt" in m for m in rec.log_msgs("warning"))


@pytest.mark.asyncio
async def test_auth_ok_missing_conn_noop(monkeypatch):
    ad, rec = _mk(monkeypatch)
    await _run(ad, rec, "ghost", "AUTH:OK")
    assert rec.closed == []
    assert rec.advertised == []


# ---------------------------------------------------------------------------
# AUTH:ERROR
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_auth_error_unknown_pkid_with_key(monkeypatch):
    ad, rec = _mk(monkeypatch)
    ad._auth_key_id = "mykey"
    ad._auth_priv = object()
    await _run(ad, rec, "c1", "AUTH:ERROR:missing_or_unknown_pkid")
    assert rec.closed == ["c1"]
    assert any("does not recognize PKID 'mykey'" in m for m in rec.log_msgs("error"))


@pytest.mark.asyncio
async def test_auth_error_unknown_pkid_no_key(monkeypatch):
    ad, rec = _mk(monkeypatch)
    ad._auth_key_id = None
    await _run(ad, rec, "c1", "AUTH:ERROR:missing_or_unknown_pkid")
    assert rec.closed == ["c1"]
    assert any("client did not advertise one" in m for m in rec.log_msgs("error"))


@pytest.mark.asyncio
async def test_auth_error_bad_signature(monkeypatch):
    ad, rec = _mk(monkeypatch)
    await _run(ad, rec, "c1", "AUTH:ERROR:bad_signature")
    assert rec.closed == ["c1"]
    assert any("bad signature" in m for m in rec.log_msgs("error"))


@pytest.mark.asyncio
async def test_auth_error_expired(monkeypatch):
    ad, rec = _mk(monkeypatch)
    await _run(ad, rec, "c1", "AUTH:ERROR:expired")
    assert rec.closed == ["c1"]
    assert any("clock skew" in m for m in rec.log_msgs("warning"))


@pytest.mark.asyncio
async def test_auth_error_generic(monkeypatch):
    ad, rec = _mk(monkeypatch)
    await _run(ad, rec, "c1", "AUTH:ERROR:something_else")
    assert rec.closed == ["c1"]
    assert any("MuxCon auth failed with error: AUTH:ERROR:something_else" in m for m in rec.log_msgs("error"))


# ---------------------------------------------------------------------------
# MPATH:SHUTDOWN:BEGIN / MPATH:END
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_shutdown_begin_unauth_ignored(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1", auth_ok=False)
    frames = await _run(ad, rec, "c1", "MPATH:SHUTDOWN:BEGIN:maint")
    assert frames == []
    assert rec.closed == []
    assert "c1" not in ad._shutdown_state


@pytest.mark.asyncio
async def test_shutdown_begin_sends_end_and_closes(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1")
    frames = await _run(ad, rec, "c1", "MPATH:SHUTDOWN:BEGIN:maint")
    assert any("MPATH:END" in f for f in frames)
    assert rec.closed == ["c1"]
    assert ad._shutdown_state["c1"]["state"] == "CLOSED"


@pytest.mark.asyncio
async def test_shutdown_begin_idempotent_when_closed(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1")
    ad._shutdown_state["c1"] = {"state": "CLOSED"}
    frames = await _run(ad, rec, "c1", "MPATH:SHUTDOWN:BEGIN:maint")
    # already CLOSED -> no END frame, no close
    assert frames == []
    assert rec.closed == []


@pytest.mark.asyncio
async def test_shutdown_begin_end_send_raises_still_closes(monkeypatch):
    ad, rec = _mk(monkeypatch)

    async def _boom_send(writer, frame):
        raise RuntimeError("send boom")

    monkeypatch.setattr(ad, "_send_protocol_frame", _boom_send)
    _arm_conn(ad, "c1")
    await _run(ad, rec, "c1", "MPATH:SHUTDOWN:BEGIN:maint")
    assert rec.closed == ["c1"]
    assert ad._shutdown_state["c1"]["state"] == "CLOSED"


@pytest.mark.asyncio
async def test_mpath_end_unauth_ignored(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1", auth_ok=False)
    await _run(ad, rec, "c1", "MPATH:END")
    assert rec.closed == []


@pytest.mark.asyncio
async def test_mpath_end_marks_closed(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1")
    await _run(ad, rec, "c1", "MPATH:END")
    assert rec.closed == ["c1"]
    assert ad._shutdown_state["c1"]["state"] == "CLOSED"


# ---------------------------------------------------------------------------
# Heartbeat REQ / ACK
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hb_req_unauth_ignored(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1", auth_ok=False)
    frames = await _run(ad, rec, "c1", f"REQ:{int(time.time())}")
    assert frames == []


@pytest.mark.asyncio
async def test_hb_req_echoes_ack(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1", extra={"last_seen": 0})
    ad._mpath_groups["peer:grp"] = {
        "conns": {"c1": {"last_seen": 0, "last_rx_seen": 0}},
        "primary": "c1",
    }
    # point the conn at its group via derive (peer key from conn_id)
    frames = await _run(ad, rec, "c1", f"REQ:{int(time.time())}")
    assert len(frames) == 1
    # an ack frame was written
    assert frames[0] != ""


@pytest.mark.asyncio
async def test_hb_req_updates_last_seen(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1")
    ad.connections["c1"]["last_seen"] = 0
    await _run(ad, rec, "c1", f"REQ:{int(time.time())}")
    # connection-level last_seen is refreshed on an inbound heartbeat
    assert ad.connections["c1"]["last_seen"] > 0


@pytest.mark.asyncio
async def test_hb_req_malformed_ts_falls_back(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1")
    frames = await _run(ad, rec, "c1", "REQ:not_a_number")
    assert len(frames) == 1  # still echoes an ack (ts fell back to time.time())


@pytest.mark.asyncio
async def test_hb_ack_unauth_ignored(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1", auth_ok=False)
    await _run(ad, rec, "c1", f"ACK:{int(time.time())}")
    assert "c1" not in ad._hb_state


@pytest.mark.asyncio
async def test_hb_ack_sets_state_and_mrtt(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1")
    await _run(ad, rec, "c1", f"ACK:{int(time.time())}")
    st = ad._hb_state["c1"]
    assert st["last_ack_ts"] > 0
    assert st["missed"] == 0


# ---------------------------------------------------------------------------
# Data/frame handlers (PORTS / VIEWERS / PORT_STATUS / POWER / FEDRW)
# ---------------------------------------------------------------------------
def _patch_handle(monkeypatch, ad, rec, name):
    async def _h(*args):
        rec.handlers.setdefault(name, []).append(args)

    monkeypatch.setattr(ad, name, _h)


@pytest.mark.asyncio
async def test_ports_list_federated_noop(monkeypatch):
    ad, rec = _mk(monkeypatch)
    await _run(ad, rec, "c1", "PORTS:LIST:FEDERATED")
    assert rec.handlers == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prefix,payload,handler",
    [
        ("PORTS:FEDERATED:", "PORTS:FEDERATED:1", "_handle_ports_federated"),
        ("VIEWERS:", "VIEWERS:p1", "_handle_viewers_frame"),
        ("PORT_STATUS:", "PORT_STATUS:p1\n{}", "_handle_port_status_frame"),
        ("POWER:STATE:", "POWER:STATE:r1\n{}", "_handle_power_state_frame"),
        ("POWER:RESULT:", "POWER:RESULT:p1\n{}", "_handle_power_result_frame"),
        ("FEDRWACK:", "FEDRWACK:p1", "_handle_fedrw_ack"),
    ],
)
async def test_frame_handlers_dispatch(monkeypatch, prefix, payload, handler):
    ad, rec = _mk(monkeypatch)
    _patch_handle(monkeypatch, ad, rec, handler)
    _arm_conn(ad, "c1")
    await _run(ad, rec, "c1", payload)
    assert handler in rec.handlers and len(rec.handlers[handler]) == 1
    assert rec.closed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prefix,payload,handler",
    [
        ("VIEWERS:", "VIEWERS:p1", "_handle_viewers_frame"),
        ("PORT_STATUS:", "PORT_STATUS:p1\n{}", "_handle_port_status_frame"),
        ("POWER:STATE:", "POWER:STATE:r1\n{}", "_handle_power_state_frame"),
        ("POWER:RESULT:", "POWER:RESULT:p1\n{}", "_handle_power_result_frame"),
        ("FEDRWACK:", "FEDRWACK:p1", "_handle_fedrw_ack"),
    ],
)
async def test_frame_handlers_unauth_not_called(monkeypatch, prefix, payload, handler):
    ad, rec = _mk(monkeypatch)
    _patch_handle(monkeypatch, ad, rec, handler)
    _arm_conn(ad, "c1", auth_ok=False)
    await _run(ad, rec, "c1", payload)
    assert handler not in rec.handlers  # unauthenticated -> handler skipped
    assert rec.closed == []


# POWER:SWITCH needs a pdu adapter + writer
@pytest.mark.asyncio
async def test_power_switch_finds_pdu(monkeypatch):
    ad, rec = _mk(monkeypatch)

    class _Pdu:
        def get_adapter_type(self):
            return "power"

    class _Other:
        def get_adapter_type(self):
            raise RuntimeError("boom")  # must be skipped, not fatal

    class _PM:
        unified_adapters = [_Other(), _Pdu()]

    ad._main_port_manager = _PM()
    _patch_handle(monkeypatch, ad, rec, "_handle_power_switch_frame")
    _arm_conn(ad, "c1")
    await _run(ad, rec, "c1", 'POWER:SWITCH:lp\n{"ref":"r","on":true}')
    assert "_handle_power_switch_frame" in rec.handlers
    args = rec.handlers["_handle_power_switch_frame"][0]
    # (conn_id, writer, payload, pdu_adapter)
    assert args[3] is not None  # pdu found despite the boom adapter
    assert rec.closed == []


@pytest.mark.asyncio
async def test_power_switch_no_pdu_passes_none(monkeypatch):
    ad, rec = _mk(monkeypatch)

    class _PM:
        unified_adapters = []

    ad._main_port_manager = _PM()
    _patch_handle(monkeypatch, ad, rec, "_handle_power_switch_frame")
    _arm_conn(ad, "c1")
    await _run(ad, rec, "c1", "POWER:SWITCH:lp")
    args = rec.handlers["_handle_power_switch_frame"][0]
    assert args[3] is None


@pytest.mark.asyncio
async def test_power_switch_unauth_not_called(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _patch_handle(monkeypatch, ad, rec, "_handle_power_switch_frame")
    _arm_conn(ad, "c1", auth_ok=False)
    await _run(ad, rec, "c1", "POWER:SWITCH:lp")
    assert "_handle_power_switch_frame" not in rec.handlers


# FEDRW: needs writer
@pytest.mark.asyncio
async def test_fedrw_dispatch_and_unauth(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _patch_handle(monkeypatch, ad, rec, "_handle_fedrw_request")
    _arm_conn(ad, "c1")
    await _run(ad, rec, "c1", "FEDRW:p1")
    assert "_handle_fedrw_request" in rec.handlers
    # unauth variant
    ad2, rec2 = _mk(monkeypatch)
    _patch_handle(monkeypatch, ad2, rec2, "_handle_fedrw_request")
    _arm_conn(ad2, "c1", auth_ok=False)
    await _run(ad2, rec2, "c1", "FEDRW:p1")
    assert "_handle_fedrw_request" not in rec2.handlers


# ---------------------------------------------------------------------------
# Fallback: AUTH prefix not recognized, unknown command, outer exception
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unknown_command_noop(monkeypatch):
    ad, rec = _mk(monkeypatch)
    await _run(ad, rec, "c1", "SOMETHING:UNKNOWN")
    assert rec.frames == []
    assert rec.handlers == {}
    assert rec.closed == []


@pytest.mark.asyncio
async def test_auth_prefix_not_matched_falls_through(monkeypatch):
    ad, rec = _mk(monkeypatch)
    # "AUTH:FOO" is not PK/OK/ERROR -> handled by _ctl_auth?No -> dispatch -> noop
    await _run(ad, rec, "c1", "AUTH:FOO")
    assert rec.frames == []
    assert rec.handlers == {}
    assert rec.closed == []


@pytest.mark.asyncio
async def test_outer_exception_logged(monkeypatch):
    ad, rec = _mk(monkeypatch, debug_raises=True)  # logger.debug raises inside the try
    _arm_conn(ad, "c1")
    await _run(ad, rec, "c1", "AUTH:OK")
    assert any("Error processing control command" in m for m in rec.log_msgs("error"))


# ---------------------------------------------------------------------------
# Advisory except-arms (each test arms ONE collaborator to raise)
# ---------------------------------------------------------------------------
def _with_group(ad, conn_id):
    """Add conn_id to its derived multipath group so the metrics branch reaches in."""
    key = ad._derive_peer_key_from_conn_id(conn_id)
    ad._mpath_groups[key] = {"conns": {conn_id: {"last_seen": 0, "last_rx_seen": 0}}, "primary": conn_id}
    return key


class _BoomStr:
    def __str__(self):
        raise RuntimeError("str boom")


class _BoomConn(dict):
    def __setitem__(self, k, v):
        if k == "last_seen":
            raise RuntimeError("setitem boom")
        super().__setitem__(k, v)


@pytest.mark.asyncio
async def test_auth_response_advertise_raises_is_swallowed(monkeypatch):
    ad, rec = _mk(monkeypatch)
    priv = Ed25519PrivateKey.generate()
    ad._auth_pubkeys = {"kid1": priv.public_key()}
    sig = base64.b64encode(priv.sign(b"N" * 32)).decode()

    async def _boom_adv(c):
        raise RuntimeError("adv boom")

    monkeypatch.setattr(ad, "_maybe_advertise_local_ports", _boom_adv)
    _response_setup(ad, "c1", sig=sig)
    frames = await _run(ad, rec, "c1", f"AUTH:PK:RESPONSE:kid1:{sig}")
    # AUTH:OK still sent; the advertise failure is logged and swallowed
    assert any("AUTH:OK" in f for f in frames)
    assert any("local port advertisement failed" in m for m in rec.log_msgs("error"))
    assert ad.connections["c1"]["auth_ok"] is True


@pytest.mark.asyncio
async def test_auth_response_processing_raises_is_logged(monkeypatch):
    ad, rec = _mk(monkeypatch)
    priv = Ed25519PrivateKey.generate()
    ad._auth_pubkeys = {"kid1": priv.public_key()}
    sig = base64.b64encode(priv.sign(b"N" * 32)).decode()

    def _boom_seq(c=None):
        raise RuntimeError("seq boom")

    monkeypatch.setattr(ad, "_next_frame_seq", _boom_seq)
    _response_setup(ad, "c1", sig=sig)
    frames = await _run(ad, rec, "c1", f"AUTH:PK:RESPONSE:kid1:{sig}")
    # _next_frame_seq raised inside the RESPONSE body -> outer arm logs it
    assert any("AUTH:PK:RESPONSE processing failed" in m for m in rec.log_msgs("error"))
    assert frames == []
    assert rec.closed == []


@pytest.mark.asyncio
async def test_auth_ok_client_filters_raises_is_swallowed(monkeypatch):
    ad, rec = _mk(monkeypatch)
    ad._auth_key_id = "ours"

    def _boom_filt(c, k):
        raise RuntimeError("filt boom")

    monkeypatch.setattr(ad, "_apply_per_connection_filters", _boom_filt)
    _arm_conn(ad, "c1", role="client", auth_ok=False)
    await _run(ad, rec, "c1", "AUTH:OK")
    assert ad.connections["c1"]["auth_ok"] is True
    assert any("per-connection filters" in m for m in rec.log_msgs("error"))
    assert rec.advertised == ["c1"]  # advertise still ran after the filter failure


@pytest.mark.asyncio
async def test_auth_ok_client_advertise_raises_is_swallowed(monkeypatch):
    ad, rec = _mk(monkeypatch)
    ad._auth_key_id = "ours"

    async def _boom_adv(c):
        raise RuntimeError("adv boom")

    monkeypatch.setattr(ad, "_maybe_advertise_local_ports", _boom_adv)
    _arm_conn(ad, "c1", role="client", auth_ok=False)
    await _run(ad, rec, "c1", "AUTH:OK")
    assert ad.connections["c1"]["auth_ok"] is True
    assert any("local port advertisement failed" in m for m in rec.log_msgs("error"))


@pytest.mark.asyncio
async def test_auth_ok_processing_raises_is_logged(monkeypatch):
    ad, rec = _mk(monkeypatch)
    ad._auth_key_id = "ours"
    # A conn dict whose .get raises -> the AUTH:OK body raises to the outer arm

    class _GetBoom(dict):
        def get(self, k, *a):
            raise RuntimeError("get boom")

    ad.connections["c1"] = _GetBoom({"role": "client", "auth_ok": False})
    await _run(ad, rec, "c1", "AUTH:OK")
    assert any("AUTH:OK processing failed" in m for m in rec.log_msgs("error"))


@pytest.mark.asyncio
async def test_auth_error_keyid_str_raises_hits_except(monkeypatch):
    ad, rec = _mk(monkeypatch)
    ad._auth_key_id = _BoomStr()
    ad._auth_priv = object()  # truthy so the "with key" log branch runs
    await _run(ad, rec, "c1", "AUTH:ERROR:missing_or_unknown_pkid")
    # the f-string (self._auth_key_id) raises -> inner except -> generic log
    assert rec.closed == ["c1"]
    assert any("MuxCon auth failed with error: AUTH:ERROR:missing_or_unknown_pkid" in m for m in rec.log_msgs("error"))


@pytest.mark.asyncio
async def test_ports_federated_unauth_not_handled(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _patch_handle(monkeypatch, ad, rec, "_handle_ports_federated")
    _arm_conn(ad, "c1", auth_ok=False)
    await _run(ad, rec, "c1", "PORTS:FEDERATED:1")
    assert "_handle_ports_federated" not in rec.handlers
    assert rec.closed == []


@pytest.mark.asyncio
async def test_mpath_end_from_begin_seen_marks_closed(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1")
    ad._shutdown_state["c1"] = {"state": "BEGIN_SEEN"}  # not yet CLOSED -> the assignment runs
    await _run(ad, rec, "c1", "MPATH:END")
    assert rec.closed == ["c1"]
    assert ad._shutdown_state["c1"]["state"] == "CLOSED"


@pytest.mark.asyncio
async def test_hb_req_updates_group_metrics(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1")
    key = _with_group(ad, "c1")
    await _run(ad, rec, "c1", f"REQ:{int(time.time())}")
    c = ad._mpath_groups[key]["conns"]["c1"]
    assert c["last_seen"] > 0
    assert c["last_rx_seen"] > 0


@pytest.mark.asyncio
async def test_hb_req_metrics_derive_raises_is_swallowed(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1")

    def _boom_derive(c):
        raise RuntimeError("derive boom")

    monkeypatch.setattr(ad, "_derive_peer_key_from_conn_id", _boom_derive)
    await _run(ad, rec, "c1", f"REQ:{int(time.time())}")
    # conn-level last_seen is set before the failed derive; outer arm swallows
    assert ad.connections["c1"]["last_seen"] > 0


@pytest.mark.asyncio
async def test_hb_ack_updates_group_metrics(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1")
    key = _with_group(ad, "c1")
    await _run(ad, rec, "c1", f"ACK:{int(time.time())}")
    c = ad._mpath_groups[key]["conns"]["c1"]
    assert c["last_seen"] > 0
    assert c["last_rx_seen"] > 0


@pytest.mark.asyncio
async def test_hb_ack_last_seen_setitem_raises_is_swallowed(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1")
    ad.connections["c1"] = _BoomConn({"role": "server", "auth_ok": True})
    await _run(ad, rec, "c1", f"ACK:{int(time.time())}")
    # the last_seen setitem raises inside the try -> outer arm swallows; no crash
    assert "c1" in ad._hb_state


@pytest.mark.asyncio
async def test_hb_ack_last_seen_derive_raises_is_swallowed(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1")

    def _boom_derive(c):
        raise RuntimeError("derive boom")

    monkeypatch.setattr(ad, "_derive_peer_key_from_conn_id", _boom_derive)
    await _run(ad, rec, "c1", f"ACK:{int(time.time())}")
    assert ad.connections["c1"]["last_seen"] > 0


@pytest.mark.asyncio
async def test_hb_ack_malformed_ts_falls_back(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1")
    await _run(ad, rec, "c1", "ACK:not_a_number")
    st = ad._hb_state["c1"]
    assert st["last_ack_ts"] > 0  # fell back to time.time()


@pytest.mark.asyncio
async def test_hb_ack_rtt_computed(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1")
    ad._hb_state["c1"] = {"last_req_ts": time.time() - 0.05, "last_ack_ts": 0.0, "missed": 3, "rtt_ms": None}
    await _run(ad, rec, "c1", f"ACK:{int(time.time())}")
    st = ad._hb_state["c1"]
    assert st["rtt_ms"] is not None and st["rtt_ms"] >= 0
    assert st["missed"] == 0


@pytest.mark.asyncio
async def test_hb_ack_rtt_bad_req_ts_raises_is_swallowed(monkeypatch):
    ad, rec = _mk(monkeypatch)
    _arm_conn(ad, "c1")
    ad._hb_state["c1"] = {"last_req_ts": "bad", "last_ack_ts": 0.0, "missed": 2, "rtt_ms": None}
    await _run(ad, rec, "c1", f"ACK:{int(time.time())}")
    st = ad._hb_state["c1"]
    # rtt computation raised (float - str) -> except arm; missed reset is INSIDE
    # the same try, so it was not reached: rtt_ms stays None, missed stays 2
    assert st["rtt_ms"] is None
    assert st["missed"] == 2
