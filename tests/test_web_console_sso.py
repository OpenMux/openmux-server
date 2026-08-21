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

from openmux.server.web_console import WebConsoleAdapter
from openmux.server.auth_manager import AuthManager
from openmux.server.console_manager import ConsoleManager
from openmux.server.port_manager import PortManager


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
    adapter = _make_adapter(8931)
    assert await adapter.start()
    try:
        now = int(time.time())
        claims = {"ver": 1, "user": "admin", "perm": "admin", "node": "None", "iat": now, "exp": now + 60}
        payload_b64 = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
        header = f"v1e;evil;{payload_b64};AAAA"
        status = await _readyz(adapter, 8931, {"X-OMX-SSO": header, "X-Forwarded-Prefix": "/proxy/x"})
        assert status == 401
        assert adapter._verify_sso_header(header) is None
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_exploit_unsigned_v1e_rejected_with_local_server_id(monkeypatch):
    """SEC-01 exploit variant: node matches the local server_id but the kid is unregistered."""
    adapter = _make_adapter(8932)
    assert await adapter.start()
    try:
        stub = SimpleNamespace(_auth_pubkeys={}, server_id="mbp")
        monkeypatch.setattr(adapter, "_find_muxcon_adapter", lambda: stub)
        now = int(time.time())
        claims = {"ver": 1, "user": "admin", "perm": "admin", "node": "mbp", "iat": now, "exp": now + 60}
        payload_b64 = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
        header = f"v1e;malicious-kid;{payload_b64};AAAA"
        assert adapter._verify_sso_header(header) is None
        status = await _readyz(adapter, 8932, {"X-OMX-SSO": header, "X-Forwarded-Prefix": "/proxy/x"})
        assert status == 401
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_signed_v1e_accepted(monkeypatch):
    """A v1e claim signed with a registered MuxCon key must authenticate."""
    adapter = _make_adapter(8933)
    assert await adapter.start()
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
        status = await _readyz(adapter, 8933, {"X-OMX-SSO": header})
        assert status == 200
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_signed_v1e_bad_signature_rejected(monkeypatch):
    adapter = _make_adapter(8934)
    assert await adapter.start()
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
        status = await _readyz(adapter, 8934, {"X-OMX-SSO": header})
        assert status == 401
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_expired_v1e_rejected(monkeypatch):
    adapter = _make_adapter(8935)
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
    adapter = _make_adapter(8936, sso_secret="topsecret")
    assert await adapter.start()
    try:
        now = int(time.time())
        claims = {"ver": 1, "user": "ops", "perm": "read-write", "node": "mbp", "iat": now, "exp": now + 60}
        payload_b64 = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
        mac = hmac.new(b"topsecret", payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
        good = f"v1;{payload_b64};{mac}"
        bad = f"v1;{payload_b64};{'0' * len(mac)}"
        assert adapter._verify_sso_header(good) is not None
        assert adapter._verify_sso_header(bad) is None
        assert await _readyz(adapter, 8936, {"X-OMX-SSO": good}) == 200
        assert await _readyz(adapter, 8936, {"X-OMX-SSO": bad}) == 401
    finally:
        await adapter.stop()
