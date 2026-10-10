"""Security tests for the web console SSO trust header (X-OMX-SSO).

Covers the regression for the removed unsigned "v1e" fallback: claims must
always be verified (Ed25519 signature against a registered MuxCon public key,
or HMAC against sso_secret). Unsigned claims are rejected even when the node
claim and a /proxy/ forwarded-prefix signal are present.
"""

import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession, TCPConnector
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openmux.server.auth_manager import AuthManager
from openmux.server.console_manager import ConsoleManager
from openmux.server.port_manager import PortManager
from openmux.server.web_console import WebConsoleAdapter


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64(std: str) -> str:
    pad = "=" * (-len(std) % 4)
    return base64.urlsafe_b64decode(std + pad)


def _make_adapter(port: int, **cfg_extra) -> WebConsoleAdapter:
    config = {
        "web_console": {
            "host": "127.0.0.1",
            "port": port,
            "enable_ui": False,
            "enable_probes": True,
            "probes_include_details": False,
            **cfg_extra,
        }
    }
    adapter = WebConsoleAdapter("wc", config)
    auth = AuthManager({"users": []})
    pm = PortManager([])
    cm = ConsoleManager(pm, auth)
    adapter.set_auth_manager(auth)
    adapter.set_console_manager(cm)
    return adapter


async def _readyz(adapter: WebConsoleAdapter, port: int, headers: dict) -> int:
    async with ClientSession(connector=TCPConnector(ssl=False)) as session:
        async with session.get(f"http://127.0.0.1:{port}/readyz", headers=headers) as resp:
            return resp.status


@pytest.mark.asyncio
async def test_exploit_unsigned_v1e_rejected_no_muxcon():
    """SEC-01 exploit: unsigned v1e claim with node 'None' + /proxy/ prefix must not authenticate."""
    adapter = _make_adapter(0)
    assert await adapter.start()
    port = int(adapter._http_site._server.sockets[0].getsockname()[1])
    try:
        now = int(time.time())
        claims = {"ver": 1, "user": "admin", "perm": "admin", "node": "None", "iat": now, "exp": now + 60}
        payload_b64 = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
        header = f"v1e;evil;{payload_b64};AAAA"
        status = await _readyz(adapter, port, {"X-OMX-SSO": header, "X-Forwarded-Prefix": "/proxy/x"})
        assert status == 401
        assert adapter._verify_sso_header(header) is None
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_exploit_unsigned_v1e_rejected_with_local_server_id(monkeypatch):
    """SEC-01 exploit variant: node matches the local server_id but the kid is unregistered."""
    adapter = _make_adapter(0)
    assert await adapter.start()
    port = int(adapter._http_site._server.sockets[0].getsockname()[1])
    try:
        stub = SimpleNamespace(_auth_pubkeys={}, server_id="mbp")
        monkeypatch.setattr(adapter, "_find_muxcon_adapter", lambda: stub)
        now = int(time.time())
        claims = {"ver": 1, "user": "admin", "perm": "admin", "node": "mbp", "iat": now, "exp": now + 60}
        payload_b64 = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
        header = f"v1e;malicious-kid;{payload_b64};AAAA"
        assert adapter._verify_sso_header(header) is None
        status = await _readyz(adapter, port, {"X-OMX-SSO": header, "X-Forwarded-Prefix": "/proxy/x"})
        assert status == 401
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_signed_v1e_accepted(monkeypatch):
    """A v1e claim signed with a registered MuxCon key must authenticate."""
    adapter = _make_adapter(0)
    assert await adapter.start()
    port = int(adapter._http_site._server.sockets[0].getsockname()[1])
    try:
        priv = Ed25519PrivateKey.generate()
        kid = "k1"
        stub = SimpleNamespace(_auth_pubkeys={kid: priv.public_key()}, server_id="mbp")
        monkeypatch.setattr(adapter, "_find_muxcon_adapter", lambda: stub)
        now = int(time.time())
        claims = {"ver": 1, "user": "ops", "perm": "read-write", "node": "mbp", "iat": now, "exp": now + 60}
        payload = json.dumps(claims, separators=(",", ":")).encode("utf-8")
        payload_b64 = _b64url(payload)
        sig = priv.sign(payload)
        header = f"v1e;{kid};{payload_b64};{_b64url(sig)}"
        returned = adapter._verify_sso_header(header)
        assert returned is not None and returned["user"] == "ops" and returned["perm"] == "read-write"
        status = await _readyz(adapter, port, {"X-OMX-SSO": header})
        assert status == 200
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_signed_v1e_bad_signature_rejected(monkeypatch):
    adapter = _make_adapter(0)
    assert await adapter.start()
    port = int(adapter._http_site._server.sockets[0].getsockname()[1])
    try:
        priv = Ed25519PrivateKey.generate()
        kid = "k1"
        stub = SimpleNamespace(_auth_pubkeys={kid: priv.public_key()}, server_id="mbp")
        monkeypatch.setattr(adapter, "_find_muxcon_adapter", lambda: stub)
        now = int(time.time())
        claims = {"ver": 1, "user": "ops", "perm": "admin", "node": "mbp", "iat": now, "exp": now + 60}
        payload = json.dumps(claims, separators=(",", ":")).encode("utf-8")
        forged_sig = priv.sign(payload + b"tampered")
        header = f"v1e;{kid};{_b64url(payload)};{_b64url(forged_sig)}"
        assert adapter._verify_sso_header(header) is None
        status = await _readyz(adapter, port, {"X-OMX-SSO": header})
        assert status == 401
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_expired_v1e_rejected(monkeypatch):
    adapter = _make_adapter(0)
    assert await adapter.start()
    try:
        priv = Ed25519PrivateKey.generate()
        kid = "k1"
        stub = SimpleNamespace(_auth_pubkeys={kid: priv.public_key()}, server_id="mbp")
        monkeypatch.setattr(adapter, "_find_muxcon_adapter", lambda: stub)
        now = int(time.time())
        claims = {"ver": 1, "user": "ops", "perm": "admin", "node": "mbp", "iat": now - 600, "exp": now - 300}
        payload = json.dumps(claims, separators=(",", ":")).encode("utf-8")
        header = f"v1e;{kid};{_b64url(payload)};{_b64url(priv.sign(payload))}"
        assert adapter._verify_sso_header(header) is None
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_hmac_v1_still_accepted():
    """Legacy v1 HMAC SSO (sso_secret configured) must keep working."""
    adapter = _make_adapter(0, sso_secret="topsecret")
    assert await adapter.start()
    port = int(adapter._http_site._server.sockets[0].getsockname()[1])
    try:
        now = int(time.time())
        claims = {"ver": 1, "user": "ops", "perm": "read-write", "node": "mbp", "iat": now, "exp": now + 60}
        payload_b64 = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
        mac = hmac.new(b"topsecret", payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
        good = f"v1;{payload_b64};{mac}"
        bad = f"v1;{payload_b64};{'0' * len(mac)}"
        assert adapter._verify_sso_header(good) is not None
        assert adapter._verify_sso_header(bad) is None
        assert await _readyz(adapter, port, {"X-OMX-SSO": good}) == 200
        assert await _readyz(adapter, port, {"X-OMX-SSO": bad}) == 401
    finally:
        await adapter.stop()


# ---------------------------------------------------------------------------
# Coverage-first tests: branches of WebConsoleAdapter._verify_sso_header not
# yet exercised above. They pin the empty/short parse guards, the legacy v1
# HMAC format (valid, decode-fail, expired, missing-secret, bad-mac), the
# v1e node-allow checks (local vs peer-rejected vs peer-allowed vs
# find-muxcon raising) and the bad-kid / sig-fail / unknown-version rejections,
# so the bracket-C901 refactor is provably behavior-preserving.
# ---------------------------------------------------------------------------


_SECRET = "ssotestsecret"


def _sso_adapter(secret: str = _SECRET) -> WebConsoleAdapter:
    adapter = _make_adapter(0)
    try:
        adapter.sso_secret = secret
    except Exception:
        # some builds may lock the attr; setattr in the tests
        pass
    return adapter


def _v1_header(claims: dict, secret: bytes = _SECRET.encode("utf-8"), bad_mac: bool = False) -> str:
    payload_b64 = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    mac = hmac.new(secret, payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    if bad_mac:
        mac = "0" * len(mac)
    return f"v1;{payload_b64};{mac}"


def _v1e_header(kid: str, claims: dict, priv=None, bad_sig: bool = False) -> str:
    payload = json.dumps(claims, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64url(payload)
    if priv is not None and not bad_sig:
        sig = priv.sign(payload)
    else:
        sig = b"\x00" * 64
    return f"v1e;{kid};{payload_b64};{_b64url(sig)}"


def test_sso_verify_empty_and_short(monkeypatch):
    """S1/S2: empty header and <3 parts return None."""
    a = _sso_adapter()
    monkeypatch.setattr(a, "_find_muxcon_adapter", lambda: None)
    assert a._verify_sso_header("") is None
    assert a._verify_sso_header("v1") is None
    assert a._verify_sso_header("v1;payload") is None


def test_sso_verify_v1_valid(monkeypatch):
    """S3: a correctly-signed v1 HMAC is accepted."""
    a = _sso_adapter()
    monkeypatch.setattr(a, "_find_muxcon_adapter", lambda: None)
    now = int(time.time())
    claims = {"user": "u", "perm": "admin", "iat": now, "exp": now + 60}
    assert a._verify_sso_header(_v1_header(claims)) is not None


def test_sso_verify_v1_payload_not_dict(monkeypatch):
    """S4: a payload that decodes but is not a dict returns None."""
    a = _sso_adapter()
    monkeypatch.setattr(a, "_find_muxcon_adapter", lambda: None)
    payload_b64 = _b64url(json.dumps([1, 2, 3]).encode("utf-8"))
    mac = hmac.new(_SECRET.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    assert a._verify_sso_header(f"v1;{payload_b64};{mac}") is None


def test_sso_verify_v1_expired(monkeypatch):
    """S5: a v1 claim outside iat-skew..exp+skew returns None."""
    a = _sso_adapter()
    monkeypatch.setattr(a, "_find_muxcon_adapter", lambda: None)
    now = int(time.time())
    claims = {"user": "u", "iat": now - 100000, "exp": now - 99000}
    assert a._verify_sso_header(_v1_header(claims)) is None


def test_sso_verify_v1_missing_secret(monkeypatch):
    """S6: a v1 claim with no sso_secret configured returns None."""
    a = _make_adapter(0)
    a.sso_secret = None
    monkeypatch.setattr(a, "_find_muxcon_adapter", lambda: None)
    now = int(time.time())
    claims = {"user": "u", "iat": now, "exp": now + 60}
    assert a._verify_sso_header(_v1_header(claims)) is None


def test_sso_verify_v1_bad_mac(monkeypatch):
    """S7: a v1 claim with a wrong MAC returns None."""
    a = _sso_adapter()
    monkeypatch.setattr(a, "_find_muxcon_adapter", lambda: None)
    now = int(time.time())
    claims = {"user": "u", "iat": now, "exp": now + 60}
    assert a._verify_sso_header(_v1_header(claims, bad_mac=True)) is None


def test_sso_verify_v1e_decode_fail(monkeypatch):
    """S8: a v1e header whose payload is not valid JSON returns None."""
    a = _sso_adapter()
    monkeypatch.setattr(a, "_find_muxcon_adapter", lambda: None)
    payload_b64 = _b64url(b"not-valid-json")
    assert a._verify_sso_header(f"v1e;k1;{payload_b64};AAAA") is None


def test_sso_verify_v1e_expired(monkeypatch):
    """S8b: a v1e claim outside the skew window returns None."""
    a = _sso_adapter()
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setattr(a, "_find_muxcon_adapter", lambda: SimpleNamespace(_auth_pubkeys={"k1": priv.public_key()}, server_id="local"))
    now = int(time.time())
    claims = {"user": "u", "node": "local", "iat": now - 100000, "exp": now - 99000}
    assert a._verify_sso_header(_v1e_header("k1", claims, priv)) is None


def test_sso_verify_v1e_find_muxcon_raises(monkeypatch):
    """S9: _find_muxcon_adapter raising (and a node claim present) returns None."""

    def _raise():
        raise RuntimeError("no muxcon")

    a = _sso_adapter()
    monkeypatch.setattr(a, "_find_muxcon_adapter", _raise)
    now = int(time.time())
    claims = {"user": "u", "node": "n1", "iat": now, "exp": now + 60}
    assert a._verify_sso_header(_v1e_header("k1", claims)) is None


def test_sso_verify_v1e_node_rejected(monkeypatch):
    """S10: a v1e claim whose node is neither local nor an active peer is rejected."""
    a = _sso_adapter()
    monkeypatch.setattr(
        a, "_find_muxcon_adapter", lambda: SimpleNamespace(_auth_pubkeys={"k1": None}, server_id="local", connections={"c": {"server_id": "other"}})
    )
    now = int(time.time())
    claims = {"user": "u", "node": "intruder", "iat": now, "exp": now + 60}
    assert a._verify_sso_header(_v1e_header("k1", claims)) is None


def test_sso_verify_v1e_node_peer_allowed_rejects_bad_kid(monkeypatch):
    """S11: node matches an active peer (allowed) but the kid has no public key -> None."""
    a = _sso_adapter()
    monkeypatch.setattr(
        a, "_find_muxcon_adapter", lambda: SimpleNamespace(_auth_pubkeys={}, server_id="local", connections={"c": {"server_id": "peerX"}})
    )
    now = int(time.time())
    claims = {"user": "u", "node": "peerX", "iat": now, "exp": now + 60}
    assert a._verify_sso_header(_v1e_header("k1", claims)) is None


def test_sso_verify_v1e_sig_fail(monkeypatch):
    """S12: a v1e claim with a wrong signature returns None."""
    priv = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    a = _sso_adapter()
    monkeypatch.setattr(a, "_find_muxcon_adapter", lambda: SimpleNamespace(_auth_pubkeys={"k1": priv.public_key()}, server_id="local"))
    now = int(time.time())
    claims = {"user": "u", "node": "local", "iat": now, "exp": now + 60}
    # signed by a DIFFERENT key than the registered public key
    assert a._verify_sso_header(_v1e_header("k1", claims, other)) is None


def test_sso_verify_unknown_version(monkeypatch):
    """S13: v99;... (3 parts) falls through both formats -> None."""
    a = _sso_adapter()
    monkeypatch.setattr(a, "_find_muxcon_adapter", lambda: None)
    assert a._verify_sso_header("v99;payload;abc") is None


def test_sso_verify_v1e_known_version_malformed_payload(monkeypatch):
    """S14: a v1e header whose payload decodes to a non-dict returns None."""
    a = _sso_adapter()
    monkeypatch.setattr(a, "_find_muxcon_adapter", lambda: None)
    payload_b64 = _b64url(json.dumps(7).encode("utf-8"))
    assert a._verify_sso_header(f"v1e;k1;{payload_b64};AAAA") is None


class _RaisingAttr:
    def __init__(self, attr: str, exc: Exception):
        self._exc = exc

    def __getattr__(self, name):
        if name == "_exc":
            return self._exc
        raise self._exc


def test_sso_verify_v1e_node_check_raises(monkeypatch):
    """S15: a node claim whose muxcon server_id access raises returns None (the
    node-allow except)."""

    class _Stub:
        _auth_pubkeys = {"k1": None}

        @property
        def server_id(self):
            raise RuntimeError("attr boom")

    a = _sso_adapter()
    monkeypatch.setattr(a, "_find_muxcon_adapter", lambda: _Stub())
    now = int(time.time())
    claims = {"user": "u", "node": "n1", "iat": now, "exp": now + 60}
    assert a._verify_sso_header(_v1e_header("k1", claims)) is None


def test_sso_verify_v1e_pubkeys_access_raises(monkeypatch):
    """S16: a muxcon whose _auth_pubkeys access raises still rejects (pub=None)."""

    class _Stub:
        server_id = "local"
        connections = {}

        @property
        def _auth_pubkeys(self):
            raise RuntimeError("keys boom")

    a = _sso_adapter()
    monkeypatch.setattr(a, "_find_muxcon_adapter", lambda: _Stub())
    now = int(time.time())
    claims = {"user": "u", "iat": now, "exp": now + 60}
    assert a._verify_sso_header(_v1e_header("k1", claims)) is None


def test_sso_verify_outer_skew_not_int(monkeypatch):
    """S17: a non-int sso_max_skew_sec trips the outer guard -> None."""
    a = _sso_adapter()
    a.sso_max_skew_sec = "junk"
    monkeypatch.setattr(a, "_find_muxcon_adapter", lambda: None)
    now = int(time.time())
    claims = {"user": "u", "iat": now, "exp": now + 60}
    assert a._verify_sso_header(_v1_header(claims)) is None
