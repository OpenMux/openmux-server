import base64
import hashlib
import hmac
import time
from typing import Any, Dict

import pytest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openmux.server.auth_manager import AuthManager


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def test_normalize_public_keys_defaults_and_allowed_uses():
    cfg = {
        "users": [],
        "public_keys": [
            {"username": "alice", "public_key": "base64:" + base64.b64encode(b"x" * 32).decode()},
            {"public_key": "base64:" + base64.b64encode(b"y" * 32).decode()},
            {"username": "bob", "public_key": "base64:" + base64.b64encode(b"z" * 32).decode(), "allowed_uses": "CLIENT"},
            {"public_key": "base64:" + base64.b64encode(b"w" * 32).decode(), "allowed_uses": ["MUXCON", "Client"]},
        ]
    }
    am = AuthManager(cfg)
    # Defaults: with username -> [client]; without username -> [muxcon]
    rec0 = am.public_keys[0]
    rec1 = am.public_keys[1]
    assert rec0["allowed_uses"] == ["client"]
    assert rec1["allowed_uses"] == ["muxcon"]
    # Normalization to lowercase and list coercion
    rec2 = am.public_keys[2]
    rec3 = am.public_keys[3]
    assert rec2["allowed_uses"] == ["client"]
    assert sorted(rec3["allowed_uses"]) == ["client", "muxcon"]


@pytest.mark.asyncio
async def test_password_hmac_challenge_success_and_single_use_and_expiry():
    pw = "secret"
    am = AuthManager({"users": [{"username": "u", "password_hash": sha256_hex(pw)}]})
    nonce_b64 = am.start_password_hmac_challenge("u")
    assert nonce_b64 is not None
    nonce_raw = base64.b64decode(nonce_b64)
    key_bytes = bytes.fromhex(sha256_hex(pw))
    signature = hmac.new(key_bytes, nonce_raw, hashlib.sha256).digest()
    sig_b64 = base64.b64encode(signature).decode()
    assert am.verify_password_hmac("u", sig_b64, src_ip="1.2.3.4") is True
    # Single-use -> second verify should fail
    assert am.verify_password_hmac("u", sig_b64, src_ip="1.2.3.4") is False
    # Expiry path
    am.start_password_hmac_challenge("u")
    am._pw_hmac_challenges["u"]["expires_at"] = time.time() - 1
    assert am.verify_password_hmac("u", sig_b64) is False


def test_failure_tracking_and_lockout():
    am = AuthManager({})
    user = "u"
    ip = "1.2.3.4"
    # Initially unlocked
    assert am.is_user_locked(user, ip) is False
    # Register failures up to threshold
    for _ in range(5):
        am.register_auth_failure(user, ip)
    # Now should be locked
    assert am.is_user_locked(user, ip) is True
    # Clear failures
    am.clear_auth_failures(user, ip)
    assert am.is_user_locked(user, ip) is False


def test_public_key_loading_and_pk_challenge_verify_openssh():
    # Generate a keypair and export public in OpenSSH format
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    pub_ssh = pub.public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()
    cfg = {
        "public_keys": [
            {"username": "alice", "key_id": "k1", "public_key": pub_ssh, "allowed_uses": ["client"]}
        ]
    }
    am = AuthManager(cfg)
    # ed25519 map should contain our key_id
    m = am.get_ed25519_pubkeys_for_use("client")
    assert "k1" in m
    ch = am.start_pubkey_challenge("alice", "k1")
    assert ch and ch["key_id"] == "k1"
    nonce = base64.b64decode(ch["nonce"])
    sig = priv.sign(nonce)
    assert am.verify_pubkey_response("alice", "k1", base64.b64encode(sig).decode()) is True
    # Challenge removed -> second verify should fail
    assert am.verify_pubkey_response("alice", "k1", base64.b64encode(sig).decode()) is False


def test_authenticate_cache_and_user_api_permissions():
    cfg = {
        "users": [
            {"username": "rw", "password_hash": sha256_hex("pw")},
            {"username": "adm", "password_hash": sha256_hex("pw"), "is_admin": True},
            {"username": "ro", "password_hash": sha256_hex("pw"), "permissions": "read-only"},
        ],
        "api_keys": [
            {"key": "K1", "permissions": "read-write"},
            {"key": "K2"},
        ],
    }
    am = AuthManager(cfg)
    # Authenticate true/false (cache behavior implicitly exercised by calling twice)
    assert am.authenticate("rw", "pw") is True
    assert am.authenticate("rw", "pw") is True
    assert am.authenticate("rw", "bad") is False
    # API key verification and permissions
    assert am.verify_api_key("K1") is True and am.get_api_key_permissions("K1") == "read-write"
    assert am.verify_api_key("K2") is True and am.get_api_key_permissions("K2") == "read-only"
    assert am.verify_api_key("KX") is False and am.get_api_key_permissions("KX") is None
    # User permissions precedence
    assert am.get_user_permissions("ro") == "read-only"
    assert am.get_user_permissions("adm") == "admin"
    assert am.get_user_permissions("rw") == "read-write"
    assert am.get_user_permissions("nope") is None


def test_hash_verify_and_generate_key():
    am = AuthManager({})
    h = am.hash_password("pw")
    assert h == sha256_hex("pw")
    assert am._verify_password("pw", h) is True
    token = am.generate_api_key()
    assert isinstance(token, str) and len(token) == 32 and all(c in "0123456789abcdef" for c in token)


def test_get_public_keys_for_use_filters():
    # invalid key data and disabled should be ignored in ed25519 map
    cfg = {
        "public_keys": [
            {"username": "u1", "key_id": "a", "public_key": "invalid", "allowed_uses": ["client"]},
            {"username": "u1", "key_id": "b", "public_key": "base64:" + base64.b64encode(b"x" * 32).decode(), "allowed_uses": ["client"], "disabled": True},
            {"key_id": "c", "public_key": "base64:" + base64.b64encode(b"y" * 32).decode(), "allowed_uses": ["muxcon"]},
        ]
    }
    am = AuthManager(cfg)
    # get_public_keys_for_use should return entries matching the use
    recs = am.get_public_keys_for_use("client")
    assert all("client" in r.get("allowed_uses", []) for r in recs)
    # ed25519 map for client should be empty due to invalid/disabled
    assert am.get_ed25519_pubkeys_for_use("client") == {}