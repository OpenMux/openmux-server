"""S4 coverage: TcpServerAdapter.authenticate_client_char_mode auth branches.

The main client-listener suite only exercises the anonymous path, the HMAC
success, and the pubkey verify-failure. This file pins the remaining branches:
empty line; pubkey no-support / no-challenge / bad-response / short-response /
verify-raises / verify-ok; HMAC locked / no-method / no-challenge / bad-response
/ verify-raises / verify-false; API-key success / fail; plaintext-USER locked /
rejected; and the outer exception handler.

Lines 507-509 (pubkey ok), 551-555 (key ok), and 543-546 (HMAC ok) are the
success arms; the rest are rejection arms. Line 536 (HMAC ``len(rparts) < 3``)
is unreachable: a line that passes the ``startswith("AUTH:RESPONSE:")`` gate
always splits into >= 3 parts, so it is pinned dead, not covered.
"""

from typing import List

import pytest

from openmux.server.adapters.client_listener import ClientSession, TcpServerAdapter


class FakeReader:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, n: int):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class FakeWriter:
    def __init__(self):
        self.buffer = bytearray()

    def write(self, data: bytes):
        self.buffer += data

    async def drain(self):
        pass

    def is_closing(self):
        return False

    def close(self):
        pass


def make_line_chars(s: str) -> List[bytes]:
    return [c.encode() for c in (s + "\n")]


def _adapter() -> TcpServerAdapter:
    return TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})


async def _run(am, lines: List[str]):
    """Drive authenticate_client_char_mode with a fresh client over `lines`.

    Returns (result, client, out_text).
    """
    adapter = _adapter()
    adapter.set_auth_manager(am)
    chunks: List[bytes] = []
    for ln in lines:
        chunks += make_line_chars(ln)
    reader = FakeReader(chunks)
    writer = FakeWriter()
    client = ClientSession("c1", "127.0.0.1", reader, writer, adapter.logger)
    result = await adapter.authenticate_client_char_mode(client)
    return result, client, writer.buffer.decode()


@pytest.mark.asyncio
async def test_auth_empty_line_returns_false():
    # auth_manager present but the client sends nothing -> collect returns None.
    result, _client, _out = await _run(object(), [])
    assert result is False


@pytest.mark.asyncio
async def test_auth_pk_no_pubkey_support():
    class AM:
        pass  # no start_pubkey_challenge

    result, _c, out = await _run(AM(), ["AUTH:PK:INIT:alice"])
    assert result is False


@pytest.mark.asyncio
async def test_auth_pk_challenge_none():
    class AM:
        def start_pubkey_challenge(self, u, key_id):
            return None

    result, _c, out = await _run(AM(), ["AUTH:PK:INIT:alice:key1"])
    assert result is False
    assert "AUTH:FAILED:Authentication failed" in out


@pytest.mark.asyncio
async def test_auth_pk_bad_response_prefix():
    class AM:
        def start_pubkey_challenge(self, u, key_id):
            return {"key_id": "key1", "nonce": "n"}

        def verify_pubkey_response(self, u, kid, sig):
            raise AssertionError("verify must not be reached")

    result, _c, out = await _run(AM(), ["AUTH:PK:INIT:alice:key1", "AUTH:BADDATA"])
    assert result is False
    assert "AUTH:BADDATA" not in out


@pytest.mark.asyncio
async def test_auth_pk_response_short():
    class AM:
        def start_pubkey_challenge(self, u, key_id):
            return {"key_id": "key1", "nonce": "n"}

        def verify_pubkey_response(self, u, kid, sig):
            raise AssertionError("verify must not be reached")

    # "AUTH:PK:RESPONSE:key1" splits into 4 parts (< 5) -> rejected before verify.
    result, _c, out = await _run(AM(), ["AUTH:PK:INIT:alice:key1", "AUTH:PK:RESPONSE:key1"])
    assert result is False


@pytest.mark.asyncio
async def test_auth_pk_verify_raises():
    class AM:
        def start_pubkey_challenge(self, u, key_id):
            return {"key_id": "key1", "nonce": "n"}

        def verify_pubkey_response(self, u, kid, sig):
            raise RuntimeError("verify blew up for test")

    result, _c, out = await _run(AM(), ["AUTH:PK:INIT:alice:key1", "AUTH:PK:RESPONSE:key1:sigb64"])
    assert result is False
    assert "AUTH:FAILED:Authentication failed" in out


@pytest.mark.asyncio
async def test_auth_pk_success():
    class AM:
        def start_pubkey_challenge(self, u, key_id):
            return {"key_id": "key1", "nonce": "n"}

        def verify_pubkey_response(self, u, kid, sig):
            return u == "alice" and kid == "key1" and sig == "sigb64"

    result, client, out = await _run(AM(), ["AUTH:PK:INIT:alice:key1", "AUTH:PK:RESPONSE:key1:sigb64"])
    assert result is True
    assert client.username == "alice"
    assert client.is_authenticated is True
    assert "AUTH:PK:CHALLENGE:key1:n" in out


@pytest.mark.asyncio
async def test_auth_hmac_locked():
    class AM:
        def is_user_locked(self, u, ip):
            return u == "alice"

        def start_password_hmac_challenge(self, u):
            return "nonceB64"

    result, _c, out = await _run(AM(), ["AUTH:USER:HMAC:alice"])
    assert result is False
    assert "Account temporarily locked" in out


@pytest.mark.asyncio
async def test_auth_hmac_no_hmac_method():
    class AM:
        def is_user_locked(self, u, ip):
            return False

        # no start_password_hmac_challenge

    result, _c, out = await _run(AM(), ["AUTH:USER:HMAC:alice"])
    assert result is False


@pytest.mark.asyncio
async def test_auth_hmac_challenge_none():
    class AM:
        def is_user_locked(self, u, ip):
            return False

        def start_password_hmac_challenge(self, u):
            return None

    result, _c, out = await _run(AM(), ["AUTH:USER:HMAC:alice"])
    assert result is False
    assert "AUTH:FAILED:Authentication failed" in out


@pytest.mark.asyncio
async def test_auth_hmac_bad_response_prefix():
    class AM:
        def is_user_locked(self, u, ip):
            return False

        def start_password_hmac_challenge(self, u):
            return "nonceB64"

        def verify_password_hmac(self, u, h, ip):
            raise AssertionError("verify must not be reached")

    result, _c, out = await _run(AM(), ["AUTH:USER:HMAC:alice", "AUTH:BADDATA"])
    assert result is False


@pytest.mark.asyncio
async def test_auth_hmac_verify_raises():
    class AM:
        def is_user_locked(self, u, ip):
            return False

        def start_password_hmac_challenge(self, u):
            return "nonceB64"

        def verify_password_hmac(self, u, h, ip):
            raise RuntimeError("hmac verify blew up for test")

    result, _c, out = await _run(AM(), ["AUTH:USER:HMAC:alice", "AUTH:RESPONSE:hmacb64"])
    assert result is False
    assert "AUTH:FAILED:Authentication failed" in out


@pytest.mark.asyncio
async def test_auth_hmac_verify_false():
    class AM:
        def is_user_locked(self, u, ip):
            return False

        def start_password_hmac_challenge(self, u):
            return "nonceB64"

        def verify_password_hmac(self, u, h, ip):
            return False

    result, _c, out = await _run(AM(), ["AUTH:USER:HMAC:alice", "AUTH:RESPONSE:hmacb64"])
    assert result is False
    assert "AUTH:FAILED:Authentication failed" in out


@pytest.mark.asyncio
async def test_auth_key_success():
    class AM:
        def authenticate_key(self, k):
            return k == "sekret"

        def get_api_key_name(self, k):
            return "keyuser"

    result, client, out = await _run(AM(), ["AUTH:KEY:sekret"])
    assert result is True
    assert client.username == "keyuser"
    assert client.is_authenticated is True


@pytest.mark.asyncio
async def test_auth_key_fail():
    class AM:
        def authenticate_key(self, k):
            return False

        def get_api_key_name(self, k):
            return "keyuser"

    result, _c, out = await _run(AM(), ["AUTH:KEY:wrong"])
    assert result is False
    assert "AUTH:FAILED:Authentication failed" in out


@pytest.mark.asyncio
async def test_auth_user_plaintext_locked():
    class AM:
        def is_user_locked(self, u, ip):
            return u == "bob"

    result, _c, out = await _run(AM(), ["AUTH:USER:bob:pw"])
    assert result is False
    assert "Account temporarily locked" in out


@pytest.mark.asyncio
async def test_auth_user_plaintext_rejected():
    class AM:
        def is_user_locked(self, u, ip):
            return False

    result, _c, out = await _run(AM(), ["AUTH:USER:bob:pw"])
    assert result is False
    assert "Plaintext password auth disabled" in out


@pytest.mark.asyncio
async def test_auth_outer_exception():
    # start_password_hmac_challenge is OUTSIDE the inner verify-try, so a raise
    # there trips the outer exception handler (line 573).
    class AM:
        def is_user_locked(self, u, ip):
            return False

        def start_password_hmac_challenge(self, u):
            raise RuntimeError("hmac setup blew up for test")

    result, _c, out = await _run(AM(), ["AUTH:USER:HMAC:alice"])
    assert result is False
