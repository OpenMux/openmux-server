"""Tests for TcpClientAdapter.authenticate_with_pubkey.

Covers the pre-refactor behavior contract for the C901 17-bracket:
key loading (PEM, SSH, passphrase, non-Ed25519 rejection), challenge and
nonce validation, and the multi-line success/failure read loop.
"""

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from openmux.client.adapters.tcp_adapter import TcpClientAdapter


def _priv_pem(key, passphrase=None):
    from cryptography.hazmat.primitives import serialization

    enc = serialization.BestAvailableEncryption(passphrase) if passphrase is not None else serialization.NoEncryption()
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=enc,
    )


NONCE = base64.b64encode(b"0123456789abcdef0123456789abcdef")  # 32 bytes
CHAL = f"AUTH:PK:CHALLENGE:k1:{NONCE.decode()}\n".encode()


def _adapter(writer, reader, connected=True):
    a = TcpClientAdapter("host", 1, {})
    a.is_connected = connected
    a.writer = writer
    a.reader = reader
    return a


def _writer():
    w = MagicMock()
    w.drain = AsyncMock()
    return w


def _reader(lines):
    r = MagicMock()
    r.readline = AsyncMock(side_effect=list(lines))
    return r


class TestAuthenticateWithPubkey:
    @pytest.mark.asyncio
    async def test_returns_false_without_connection_or_streams(self):
        a = _adapter(None, None, connected=False)
        assert await a.authenticate_with_pubkey("u", "/nonexistent") is False
        a2 = _adapter(None, None, connected=True)
        assert await a2.authenticate_with_pubkey("u", "/nonexistent") is False

    @pytest.mark.asyncio
    async def test_key_file_missing(self, tmp_path):
        w = _writer()
        a = _adapter(w, _reader([]))
        assert await a.authenticate_with_pubkey("u", str(tmp_path / "nope")) is False
        w.write.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_ed25519_key_rejected(self, tmp_path):
        from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

        path = tmp_path / "rsa.pem"
        path.write_bytes(_priv_pem(generate_private_key(65537, 2048)))
        w = _writer()
        a = _adapter(w, _reader([]))
        assert await a.authenticate_with_pubkey("u", str(path)) is False

    @pytest.mark.asyncio
    async def test_ssh_private_key_format(self, tmp_path):
        """The OpenSSH key format is the second accepted loader."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import NoEncryption, PrivateFormat

        try:
            from cryptography.hazmat.primitives.serialization import BestAvailableOpenSSHEncoding
        except ImportError:
            pytest.skip("this cryptography build has no OpenSSH private-key encoder")

        key = Ed25519PrivateKey.generate()
        path = tmp_path / "id_ed25519"
        path.write_bytes(key.private_bytes(NoEncryption(), PrivateFormat.OpenSSH, BestAvailableOpenSSHEncoding()))
        w = _writer()
        a = _adapter(w, _reader([CHAL, b"AUTH:SUCCESS\n"]))
        assert await a.authenticate_with_pubkey("u", str(path)) is True
        sig = w.write.call_args_list[-1].args[0].decode().split(":")[4]
        key.public_key().verify(base64.b64decode(sig), base64.b64decode(NONCE))

    @pytest.mark.asyncio
    async def test_passphrase_env_decrypts(self, tmp_path, monkeypatch):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        monkeypatch.setenv("OPENMUX_PUBKEY_PASSPHRASE", "ph")
        key = Ed25519PrivateKey.generate()
        path = tmp_path / "enc.pem"
        path.write_bytes(_priv_pem(key, b"ph"))
        w = _writer()
        a = _adapter(w, _reader([CHAL, b"AUTH:SUCCESS\n"]))
        assert await a.authenticate_with_pubkey("u", str(path)) is True

    @pytest.mark.asyncio
    async def test_success_sets_state_and_sends_valid_signature(self, tmp_path):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        path = tmp_path / "k.pem"
        path.write_bytes(_priv_pem(key))
        w = _writer()
        a = _adapter(w, _reader([CHAL, b"AUTH:SUCCESS\n"]))
        assert await a.authenticate_with_pubkey("u", str(path)) is True

        # Two writes: init command, then signed response.
        assert len(w.write.call_args_list) == 2
        assert w.write.call_args_list[0].args[0] == b"AUTH:PK:INIT:u\n"
        resp = w.write.call_args_list[-1].args[0].decode()
        assert resp.startswith("AUTH:PK:RESPONSE:k1:")
        # AUTH:PK:RESPONSE:<key_id>:<sig>
        sig = base64.b64decode(resp.split(":")[4])
        key.public_key().verify(sig, base64.b64decode(NONCE))

        assert a.is_authenticated is True
        assert a.username == "u"
        assert a._last_auth == {"method": "pubkey", "username": "u", "key_id": "k1", "private_key_path": str(path)}

    @pytest.mark.asyncio
    async def test_key_id_in_init_command(self, tmp_path):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        path = tmp_path / "k.pem"
        path.write_bytes(_priv_pem(Ed25519PrivateKey.generate()))
        w = _writer()
        a = _adapter(w, _reader([CHAL, b"AUTH:SUCCESS\n"]))
        await a.authenticate_with_pubkey("u", str(path), key_id="k9")
        assert w.write.call_args_list[0].args[0] == b"AUTH:PK:INIT:u:k9\n"

    @pytest.mark.asyncio
    async def test_bad_challenge_prefix_rejected(self, tmp_path):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        path = tmp_path / "k.pem"
        path.write_bytes(_priv_pem(Ed25519PrivateKey.generate()))
        w = _writer()
        a = _adapter(w, _reader([b"AUTH:OTHER:k1\n"]))
        assert await a.authenticate_with_pubkey("u", str(path)) is False
        # Only the init command was sent; no RESPONSE.
        assert len(w.write.call_args_list) == 1

    @pytest.mark.asyncio
    async def test_malformed_challenge_parts_rejected(self, tmp_path):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        path = tmp_path / "k.pem"
        path.write_bytes(_priv_pem(Ed25519PrivateKey.generate()))
        w = _writer()
        a = _adapter(w, _reader([b"AUTH:PK:CHALLENGE:k1\n"]))
        # Prefix matches but there is no nonce part (len(parts) < 5).
        assert await a.authenticate_with_pubkey("u", str(path)) is False

    @pytest.mark.asyncio
    async def test_invalid_nonce_encoding_rejected(self, tmp_path):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        path = tmp_path / "k.pem"
        # "A=" is not a valid base64 payload.
        path.write_bytes(_priv_pem(Ed25519PrivateKey.generate()))
        w = _writer()
        a = _adapter(w, _reader([b"AUTH:PK:CHALLENGE:k1:A=\n"]))
        assert await a.authenticate_with_pubkey("u", str(path)) is False

    @pytest.mark.asyncio
    async def test_banner_and_empty_lines_skipped(self, tmp_path):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        path = tmp_path / "k.pem"
        path.write_bytes(_priv_pem(Ed25519PrivateKey.generate()))
        w = _writer()
        # b"\n" (whitespace-only) is skipped; a true b"" would be EOF and stop
        # the read loop first.
        a = _adapter(w, _reader([CHAL, b"CONNECTED banner\r\n", b"\n", b"AUTH:SUCCESS\n"]))
        assert await a.authenticate_with_pubkey("u", str(path)) is True

    @pytest.mark.asyncio
    async def test_auth_failed_line(self, tmp_path):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        path = tmp_path / "k.pem"
        path.write_bytes(_priv_pem(Ed25519PrivateKey.generate()))
        w = _writer()
        a = _adapter(w, _reader([CHAL, b"AUTH:FAILED:bad key\n"]))
        assert await a.authenticate_with_pubkey("u", str(path)) is False
        assert a.is_authenticated is False

    @pytest.mark.asyncio
    async def test_no_response_lines(self, tmp_path):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        path = tmp_path / "k.pem"
        path.write_bytes(_priv_pem(Ed25519PrivateKey.generate()))
        w = _writer()
        r = MagicMock()
        r.readline = AsyncMock(return_value=b"")  # stream ends immediately
        a = _adapter(w, r)
        # Empty stream end -> no AUTH:SUCCESS seen.
        assert await a.authenticate_with_pubkey("u", str(path)) is False
