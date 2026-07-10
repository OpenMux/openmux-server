"""Tests for the protocol handler layer.

Covers PlainHandler, ConserverHandler, and OpenMuxHandler in isolation
(no real network connections required).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openmux.server.adapters.protocols import PROTOCOL_HANDLERS, get_handler
from openmux.server.adapters.protocols.conserver import ConserverHandler
from openmux.server.adapters.protocols.openmux_handler import OpenMuxHandler
from openmux.server.adapters.protocols.plain import PlainHandler


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────

def test_protocol_registry_keys():
    assert set(PROTOCOL_HANDLERS.keys()) == {"plain", "openmux", "conserver"}


def test_get_handler_plain():
    h = get_handler("plain", {})
    assert isinstance(h, PlainHandler)


def test_get_handler_conserver():
    h = get_handler("conserver", {})
    assert isinstance(h, ConserverHandler)


def test_get_handler_openmux():
    h = get_handler("openmux", {})
    assert isinstance(h, OpenMuxHandler)


def test_get_handler_unknown_falls_back_to_plain():
    h = get_handler("rfc2217", {})
    assert isinstance(h, PlainHandler)


def test_get_handler_empty_string_falls_back_to_plain():
    h = get_handler("", {})
    assert isinstance(h, PlainHandler)


# ─────────────────────────────────────────────────────────────────────────────
# PlainHandler — encode / decode
# ─────────────────────────────────────────────────────────────────────────────

class TestPlainHandlerPassthrough:
    def test_encode_is_passthrough(self):
        h = PlainHandler({})
        assert h.encode(b"hello\xff") == b"hello\xff"

    def test_decode_no_strip_passthrough(self):
        h = PlainHandler({})
        assert h.decode(b"hello\xff\xfb\x01") == b"hello\xff\xfb\x01"


class TestPlainHandlerTelnetStrip:
    def _handler(self):
        return PlainHandler({"protocol": {"telnet_negotiation": "strip"}})

    def test_plain_bytes_pass_through(self):
        h = self._handler()
        assert h.decode(b"hello world") == b"hello world"

    def test_iac_iac_becomes_literal_ff(self):
        h = self._handler()
        assert h.decode(b"\xff\xff") == b"\xff"

    def test_iac_will_option_stripped(self):
        # IAC WILL ECHO = 3 bytes → gone
        h = self._handler()
        assert h.decode(b"\xff\xfb\x01") == b""

    def test_iac_do_option_stripped(self):
        # IAC DO SUPPRESS-GA = 3 bytes → gone
        h = self._handler()
        assert h.decode(b"\xff\xfd\x03") == b""

    def test_iac_single_byte_command_stripped(self):
        # IAC GA (249) = 2 bytes → gone
        h = self._handler()
        assert h.decode(b"\xff\xf9") == b""

    def test_iac_subneg_stripped(self):
        # IAC SB 0x18 "xterm" IAC SE → gone
        payload = b"\xff\xfa\x18xterm\xff\xf0"
        h = self._handler()
        assert h.decode(payload) == b""

    def test_data_before_and_after_iac(self):
        h = self._handler()
        data = b"ABC\xff\xfd\x01XYZ"
        assert h.decode(data) == b"ABCXYZ"

    def test_iac_split_across_chunks(self):
        h = self._handler()
        # 0xFF arrives in first chunk, command byte in second
        result = h.decode(b"A\xff") + h.decode(b"\xfb\x01B")
        assert result == b"AB"

    def test_iac_iac_split_across_chunks(self):
        h = self._handler()
        result = h.decode(b"\xff") + h.decode(b"\xff")
        assert result == b"\xff"

    def test_subneg_split_across_chunks(self):
        h = self._handler()
        r = h.decode(b"\xff\xfa\x18xte")
        r += h.decode(b"rm\xff")
        r += h.decode(b"\xf0")
        assert r == b""

    def test_multiple_sequences(self):
        h = self._handler()
        data = b"\xff\xfb\x01hello\xff\xff world\xff\xfd\x03"
        assert h.decode(data) == b"hello\xff world"


class TestPlainHandlerValidateConfig:
    def test_valid_none(self):
        assert PlainHandler.validate_config({}) == []

    def test_valid_strip(self):
        cfg = {"protocol": {"telnet_negotiation": "strip"}}
        assert PlainHandler.validate_config(cfg) == []

    def test_invalid_negotiation_value(self):
        cfg = {"protocol": {"telnet_negotiation": "respond"}}
        errors = PlainHandler.validate_config(cfg)
        assert len(errors) == 1


@pytest.mark.asyncio
async def test_plain_handler_establish(monkeypatch):
    fake_reader = MagicMock()
    fake_writer = MagicMock()

    async def fake_open(host, port, ssl=None):
        return fake_reader, fake_writer

    monkeypatch.setattr(asyncio, "open_connection", fake_open)

    h = PlainHandler({})
    r, w = await h.establish("1.2.3.4", 23, {"timeout": 5.0})
    assert r is fake_reader
    assert w is fake_writer


@pytest.mark.asyncio
async def test_plain_handler_establish_tls(monkeypatch):
    captured = {}

    async def fake_open(host, port, ssl=None):
        captured["ssl"] = ssl
        return MagicMock(), MagicMock()

    monkeypatch.setattr(asyncio, "open_connection", fake_open)

    h = PlainHandler({})
    await h.establish("host", 443, {"use_tls": True, "ssl_verify": True, "timeout": 5.0})
    import ssl as _ssl
    assert isinstance(captured["ssl"], _ssl.SSLContext)


# ─────────────────────────────────────────────────────────────────────────────
# ConserverHandler — validate_config
# ─────────────────────────────────────────────────────────────────────────────

class TestConserverValidateConfig:
    def test_valid(self):
        cfg = {"protocol": {"console_name": "blade1", "username": "admin"}}
        assert ConserverHandler.validate_config(cfg) == []

    def test_missing_console_name(self):
        cfg = {"protocol": {"username": "admin"}}
        errors = ConserverHandler.validate_config(cfg)
        assert any("console_name" in e for e in errors)

    def test_missing_username(self):
        cfg = {"protocol": {"console_name": "blade1"}}
        errors = ConserverHandler.validate_config(cfg)
        assert any("username" in e for e in errors)

    def test_both_missing(self):
        errors = ConserverHandler.validate_config({})
        assert len(errors) == 2


# ─────────────────────────────────────────────────────────────────────────────
# ConserverHandler — decode / encode
# ─────────────────────────────────────────────────────────────────────────────

class TestConserverByteTransforms:
    def test_plain_data_passthrough(self):
        h = ConserverHandler({})
        assert h.decode(b"hello") == b"hello"
        assert h.encode(b"hello") == b"hello"

    def test_decode_iac_iac_is_literal_ff(self):
        h = ConserverHandler({})
        assert h.decode(b"\xff\xff") == b"\xff"

    def test_decode_iac_command_stripped(self):
        h = ConserverHandler({})
        # 0xFF 'G' is a conserver out-of-band signal → discard both
        assert h.decode(b"\xff" + b"G") == b""

    def test_decode_iac_split_across_chunks(self):
        h = ConserverHandler({})
        r = h.decode(b"A\xff") + h.decode(b"\xff" + b"B")
        # First 0xFF is IAC, next 0xFF makes IAC IAC → literal 0xFF; then B
        assert r == b"A\xffB"

    def test_encode_escapes_ff(self):
        h = ConserverHandler({})
        assert h.encode(b"\xff") == b"\xff\xff"

    def test_encode_escapes_multiple_ff(self):
        h = ConserverHandler({})
        assert h.encode(b"\xff\xff") == b"\xff\xff\xff\xff"

    def test_encode_no_ff_is_passthrough(self):
        h = ConserverHandler({})
        data = b"hello world"
        assert h.encode(data) is data  # same object (no copy needed)

    def test_round_trip(self):
        h_enc = ConserverHandler({})
        h_dec = ConserverHandler({})
        original = b"data with \xff byte and more \xff bytes"
        encoded = h_enc.encode(original)
        decoded = h_dec.decode(encoded)
        assert decoded == original


# ─────────────────────────────────────────────────────────────────────────────
# ConserverHandler — establish (mocked network)
# ─────────────────────────────────────────────────────────────────────────────

def _make_conserver_mock_streams(lines: list):
    """Return (reader, writer) mocks for a conserver session."""
    encoded = [ln.encode() + b"\n" for ln in lines]

    async def readline():
        if encoded:
            return encoded.pop(0)
        return b""

    reader = MagicMock()
    reader.readline = readline

    writer = MagicMock()
    writer.write = MagicMock()
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    return reader, writer


@pytest.mark.asyncio
async def test_conserver_establish_full_flow(monkeypatch):
    """Happy path: master login + call → group login + attach."""
    connections = []

    # Master: ok → login admin → ok → call blade1 → 7782
    master_r, master_w = _make_conserver_mock_streams(
        ["ok", "ok", "7782"]
    )
    # Group: ok → login admin → ok → call blade1 → [attached]
    group_r, group_w = _make_conserver_mock_streams(
        ["ok", "ok", "[attached]"]
    )

    async def fake_open(host, port, ssl=None):
        conn = (MagicMock(), MagicMock())
        connections.append((host, port))
        if port == 782:
            return master_r, master_w
        return group_r, group_w

    monkeypatch.setattr(asyncio, "open_connection", fake_open)

    config = {
        "timeout": 5.0,
        "protocol": {"type": "conserver", "console_name": "blade1", "username": "admin"},
    }
    h = ConserverHandler(config)
    r, w = await h.establish("conserver.local", 782, config)

    assert r is group_r
    assert w is group_w
    # Connected to master then group
    assert connections[0] == ("conserver.local", 782)
    assert connections[1] == ("conserver.local", 7782)


@pytest.mark.asyncio
async def test_conserver_establish_with_password(monkeypatch):
    """Master requires a password challenge."""
    master_r, master_w = _make_conserver_mock_streams(
        ["ok", "passwd? conserver.local", "ok", "7782"]
    )
    group_r, group_w = _make_conserver_mock_streams(
        ["ok", "ok", "[spy]"]
    )

    async def fake_open(host, port, ssl=None):
        if port == 782:
            return master_r, master_w
        return group_r, group_w

    monkeypatch.setattr(asyncio, "open_connection", fake_open)

    config = {
        "timeout": 5.0,
        "protocol": {
            "type": "conserver",
            "console_name": "blade1",
            "username": "admin",
            "password": "secret",
        },
    }
    h = ConserverHandler(config)
    r, w = await h.establish("conserver.local", 782, config)
    assert r is group_r


@pytest.mark.asyncio
async def test_conserver_establish_bad_greeting_raises(monkeypatch):
    master_r, master_w = _make_conserver_mock_streams(["access from your host is refused"])

    async def fake_open(host, port, ssl=None):
        return master_r, master_w

    monkeypatch.setattr(asyncio, "open_connection", fake_open)

    config = {
        "timeout": 5.0,
        "protocol": {"console_name": "x", "username": "u"},
    }
    h = ConserverHandler(config)
    with pytest.raises(ConnectionError, match="not ready"):
        await h.establish("h", 782, config)


@pytest.mark.asyncio
async def test_conserver_establish_remote_redirect_raises(monkeypatch):
    master_r, master_w = _make_conserver_mock_streams(
        ["ok", "ok", "@other-conserver.local"]
    )

    async def fake_open(host, port, ssl=None):
        return master_r, master_w

    monkeypatch.setattr(asyncio, "open_connection", fake_open)

    config = {
        "timeout": 5.0,
        "protocol": {"console_name": "x", "username": "u"},
    }
    h = ConserverHandler(config)
    with pytest.raises(ConnectionError, match="remote redirect"):
        await h.establish("h", 782, config)


@pytest.mark.asyncio
async def test_conserver_establish_attach_failed_raises(monkeypatch):
    master_r, master_w = _make_conserver_mock_streams(["ok", "ok", "7782"])
    group_r, group_w = _make_conserver_mock_streams(
        ["ok", "ok", "unknown console 'blade1'"]
    )

    async def fake_open(host, port, ssl=None):
        if port == 782:
            return master_r, master_w
        return group_r, group_w

    monkeypatch.setattr(asyncio, "open_connection", fake_open)

    config = {
        "timeout": 5.0,
        "protocol": {"console_name": "blade1", "username": "u"},
    }
    h = ConserverHandler(config)
    with pytest.raises(ConnectionError, match="attach failed"):
        await h.establish("h", 782, config)


# ─────────────────────────────────────────────────────────────────────────────
# OpenMuxHandler — validate_config
# ─────────────────────────────────────────────────────────────────────────────

class TestOpenMuxValidateConfig:
    def test_valid_with_api_key(self):
        cfg = {"protocol": {"remote_port": "p1", "api_key": "key"}}
        assert OpenMuxHandler.validate_config(cfg) == []

    def test_valid_with_user_pass(self):
        cfg = {"protocol": {"remote_port": "p1", "username": "u", "password": "p"}}
        assert OpenMuxHandler.validate_config(cfg) == []

    def test_missing_remote_port(self):
        cfg = {"protocol": {"api_key": "key"}}
        errors = OpenMuxHandler.validate_config(cfg)
        assert any("remote_port" in e for e in errors)

    def test_missing_auth(self):
        cfg = {"protocol": {"remote_port": "p1"}}
        errors = OpenMuxHandler.validate_config(cfg)
        assert any("api_key" in e for e in errors)

    def test_incomplete_user_pass(self):
        # username without password → fails
        cfg = {"protocol": {"remote_port": "p1", "username": "u"}}
        errors = OpenMuxHandler.validate_config(cfg)
        assert len(errors) > 0


# ─────────────────────────────────────────────────────────────────────────────
# OpenMuxHandler — encode / decode (passthrough)
# ─────────────────────────────────────────────────────────────────────────────

def test_openmux_encode_passthrough():
    h = OpenMuxHandler({})
    data = b"hello\xff"
    assert h.encode(data) == data


def test_openmux_decode_passthrough():
    h = OpenMuxHandler({})
    data = b"hello\xff"
    assert h.decode(data) == data
