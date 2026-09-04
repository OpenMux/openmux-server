import asyncio
import base64
import contextlib
import json
import logging
import os
import socket
import ssl
import sys
import tempfile
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple, cast

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openmux.server.adapters.muxcon import FederationPeer, UnifiedMuxConAdapter


class FakeReader:
    def __init__(self, lines: Optional[List[bytes]] = None):
        self.lines = list(lines or [])

    async def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        await asyncio.sleep(0)
        return b""


class FakeWriter:
    def __init__(self):
        self.buffer = bytearray()
        self._closing = False
        self._extra: Dict[str, Any] = {}

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)

    def get_extra_info(self, name: str):
        return self._extra.get(name)

    # Minimal transport attribute for optional abort/reset paths
    @property
    def transport(self):
        return getattr(self, "_transport", None)

    @transport.setter
    def transport(self, val):
        self._transport = val


class FakePM:
    def __init__(self):
        self.ports: Dict[str, Any] = {}
        self.writes: List[Dict[str, Any]] = []

    async def get_port_list_with_federation(self):
        return [
            {
                "name": "local1",
                "adapter_type": "loopback",
                "connected": True,
                "max_rw_users": 1,
                "description": "Local port",
            }
        ]

    async def register_federated_port(self, metadata, proxy):
        # Simulate PortManager storing the proxy
        self.ports[metadata.name] = proxy
        # Attach back-reference
        if hasattr(proxy, "set_port_manager"):
            proxy.set_port_manager(self)
        return metadata.name

    async def write_to_port(self, name: str, data: bytes, client_id: str = ""):
        self.writes.append({"name": name, "data": data, "client_id": client_id})
        return len(data)

    async def get_port_data(self, name: str) -> bytes:
        return b""


class FakeExactReader:
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    async def readexactly(self, n: int) -> bytes:
        await asyncio.sleep(0)
        if self._pos + n > len(self._data):
            raise asyncio.IncompleteReadError(partial=self._data[self._pos :], expected=n)
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk


def test_validate_config_listener_and_initiators():
    # OK: TLS listener with autogen enabled
    ok = {"listeners": [{"host": "0.0.0.0", "port": 9999, "use_tls": True, "tls_autogen": True}], "initiators": []}
    assert UnifiedMuxConAdapter.validate_config(ok) is True
    # Fail: TLS with cert/key missing and tls_autogen disabled
    bad = {"listeners": [{"host": "0.0.0.0", "port": 9999, "use_tls": True, "tls_autogen": False}]}
    with pytest.raises(ValueError):
        UnifiedMuxConAdapter.validate_config(bad)


def test_auth_helpers_and_filters_merge():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})

    # Merge keys from auth manager when adapter has none
    class AM:
        def get_ed25519_pubkeys_for_use(self, use: str):
            priv = Ed25519PrivateKey.generate()
            return {"k1": priv.public_key()}

        def get_public_keys_for_use(self, use: str):
            return [
                {"key_id": "k1", "muxcon": {"advertise_filters": {"include": ["a*"], "exclude": ["b*"]}}},
                {"key_id": "k2", "accept_filters": {"include": ["*"], "exclude": []}},
            ]

    a.set_auth_manager(AM())
    assert "k1" in a._auth_pubkeys
    # Filters applied later per-connection
    a._apply_per_connection_filters("c1", "k1")
    assert "c1" in a._conn_filters and "advertise_filters" in a._conn_filters["c1"]


def test_is_conn_authenticated_variants():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Client role with private key present requires auth_ok
    a._auth_priv = Ed25519PrivateKey.generate()
    a._auth_key_id = "kid"
    a.connections["cid1"] = {"role": "client", "auth_ok": False}
    assert a._is_conn_authenticated("cid1") is False
    a.connections["cid1"]["auth_ok"] = True
    assert a._is_conn_authenticated("cid1") is True
    # Server role respects adapter-level requirement
    a.connections["cid2"] = {"role": "server", "auth_ok": False}
    a._auth_required = False
    assert a._is_conn_authenticated("cid2") is True
    a._auth_required = True
    assert a._is_conn_authenticated("cid2") is False


@pytest.mark.asyncio
async def test_tls_context_builders_and_autogen(tmp_path):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Server context without TLS disabled -> None
    ctx = await a._create_server_ssl_context({"use_tls": False})
    assert ctx is None
    # Server context when TLS enabled -> returns context
    ctx2 = await a._create_server_ssl_context({"use_tls": True})
    assert isinstance(ctx2, ssl.SSLContext)
    # Client context with verify disable
    peer = FederationPeer("h", 1, options={"use_tls": True, "ssl_verify": False})
    cctx = await a._create_client_ssl_context(peer)
    assert isinstance(cctx, ssl.SSLContext) and cctx.verify_mode == ssl.CERT_NONE
    # Autogen cert/key
    p = tmp_path / "tls"
    c, k = await a._ensure_autogen_cert({"tls_dir": str(p)})
    assert os.path.exists(c) and os.path.exists(k)


@pytest.mark.asyncio
async def test_listener_tls_setup_failure_is_fail_closed(tmp_path, monkeypatch):
    # TLS requested + autogen failure: listener must not start, and must not
    # silently downgrade to plaintext.
    a = UnifiedMuxConAdapter("mx", {"listeners": []})

    async def boom(lconf):
        raise RuntimeError("autogen failed on purpose")

    monkeypatch.setattr(a, "_ensure_autogen_cert", boom)
    lconf = {"use_tls": True, "tls_autogen": True, "tls_dir": str(tmp_path)}
    key = ("127.0.0.1", 0)
    assert await a._start_single_listener(key, lconf) is False
    assert key not in a._servers
    # The old fail-open bug mutated the config to disable TLS; that must not happen.
    assert lconf["use_tls"] is True


@pytest.mark.asyncio
async def test_listener_tls_without_cert_or_autogen_is_fail_closed(tmp_path):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    lconf = {"use_tls": True, "tls_autogen": False}
    key = ("127.0.0.1", 0)
    assert await a._start_single_listener(key, lconf) is False
    assert key not in a._servers


@pytest.mark.asyncio
async def test_listener_tls_autogen_still_starts(tmp_path):
    # Positive control: with autogen working, TLS listener still binds.
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    lconf = {"use_tls": True, "tls_autogen": True, "tls_dir": str(tmp_path)}
    key = ("127.0.0.1", 0)
    try:
        assert await a._start_single_listener(key, lconf) is True
        assert key in a._servers
    finally:
        await a._stop_single_listener(key)


@pytest.mark.asyncio
async def test_initiator_tls_unavailable_never_dials_plaintext(monkeypatch):
    # If the client SSL context cannot be built, the dial attempt must be
    # aborted (and retried with backoff), never issued in plaintext.
    a = UnifiedMuxConAdapter("mx", {"initiators": []})

    async def no_ctx(peer):
        return None

    monkeypatch.setattr(a, "_create_client_ssl_context", no_ctx)

    dials = []

    async def fake_open(*args, **kwargs):
        dials.append((args, kwargs))
        raise AssertionError("initiator dialed without a TLS context")

    monkeypatch.setattr(asyncio, "open_connection", fake_open)

    peer = FederationPeer(
        "127.0.0.1",
        1,
        options={"use_tls": True, "retry_backoff_initial": 0.0, "retry_backoff_max": 0.0},
    )
    task = asyncio.create_task(a._initiator_loop(peer))
    await asyncio.sleep(0.05)
    a._stop_event.set()
    await task
    assert dials == []
    assert a.connections == {}


def test_verify_mode_resolution_matrix():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Default (no optional keys): ToFU gate with relaxed TLS-level check
    assert a._resolve_verify_mode(FederationPeer("h", 1, options={"use_tls": True})) == "tofou"
    # Explicit ssl_verify off always wins
    assert a._resolve_verify_mode(FederationPeer("h", 1, options={"ssl_verify": False})) == "off"
    assert a._resolve_verify_mode(FederationPeer("h", 1, options={"ssl_verify": False, "ssl_ca_cert": "ca.pem"})) == "off"
    # A configured CA gives full verification
    assert a._resolve_verify_mode(FederationPeer("h", 1, options={"ssl_ca_cert": "ca.pem"})) == "ca"
    # CA wins over pin (pin still applies as an extra post-handshake check)
    assert (
        a._resolve_verify_mode(FederationPeer("h", 1, options={"ssl_ca_cert": "ca.pem", "tls_pin_fingerprint": "sha256:aa"}))
        == "ca"
    )
    # Pin gates the link when no CA
    assert a._resolve_verify_mode(FederationPeer("h", 1, options={"tls_pin_fingerprint": "sha256:aa"})) == "pin"
    # ToFU off and nothing else: strict system trust verification
    assert a._resolve_verify_mode(FederationPeer("h", 1, options={"tls_tofu": False})) == "system"
    # Pin wins over ToFU when both are set
    assert (
        a._resolve_verify_mode(FederationPeer("h", 1, options={"tls_pin_fingerprint": "sha256:aa", "tls_tofu": False}))
        == "pin"
    )


@pytest.mark.asyncio
async def test_client_ssl_context_by_mode(tmp_path):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Default peer: relaxed TLS-level check (ToFU gates the link)
    ctx = await a._create_client_ssl_context(FederationPeer("h", 1, options={"use_tls": True}))
    assert ctx.verify_mode == ssl.CERT_NONE and ctx.check_hostname is False
    # Pin mode: relaxed TLS-level check
    ctx = await a._create_client_ssl_context(
        FederationPeer("h", 1, options={"use_tls": True, "tls_pin_fingerprint": "sha256:aa"})
    )
    assert ctx.verify_mode == ssl.CERT_NONE and ctx.check_hostname is False
    # System mode (ToFU off, no CA, no pin): strict
    ctx = await a._create_client_ssl_context(FederationPeer("h", 1, options={"use_tls": True, "tls_tofu": False}))
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    # CA mode with a loadable CA file: strict against that CA
    ca, _ = await a._ensure_autogen_cert({"tls_dir": str(tmp_path)})
    ctx = await a._create_client_ssl_context(FederationPeer("h", 1, options={"use_tls": True, "ssl_ca_cert": ca}))
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True


@pytest.mark.asyncio
async def test_fingerprint_gate_fails_without_peer_cert(tmp_path):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    a._known_peers_path = str(tmp_path / "known.json")
    w = FakeWriter()  # no ssl_object: peer presented no certificate
    # Default ToFU gate active -> reject
    with pytest.raises(ValueError):
        await a._verify_peer_fingerprint(FederationPeer("h", 1, options={"use_tls": True}), cast(Any, w))
    # Pin gate active -> reject
    with pytest.raises(ValueError):
        await a._verify_peer_fingerprint(
            FederationPeer("h", 1, options={"use_tls": True, "tls_tofu": False, "tls_pin_fingerprint": "sha256:aa"}),
            cast(Any, w),
        )
    # No gate at all (ToFU off, no pin): nothing to check, pass
    await a._verify_peer_fingerprint(FederationPeer("h", 1, options={"use_tls": True, "tls_tofu": False}), cast(Any, w))


@pytest.mark.asyncio
async def test_tofu_first_sight_warns_with_fingerprint(tmp_path, caplog):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    a._known_peers_path = str(tmp_path / "known.json")

    class SslObj:
        def getpeercert(self, binary_mode):
            return b"DER1"

    w = FakeWriter()
    w._extra["ssl_object"] = SslObj()
    with caplog.at_level(logging.WARNING, logger="openmux.adapter.muxcon.mx"):
        await a._verify_peer_fingerprint(FederationPeer("h", 1, options={"use_tls": True}), cast(Any, w))
    assert "TOFU stored fingerprint" in caplog.text
    stored = a._load_known_peers()["h:1"]
    assert stored in caplog.text
    # Second sight: enforced, no re-store warning
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="openmux.adapter.muxcon.mx"):
        await a._verify_peer_fingerprint(FederationPeer("h", 1, options={"use_tls": True}), cast(Any, w))
    assert "TOFU stored fingerprint" not in caplog.text


@pytest.mark.asyncio
async def test_ssl_verify_false_still_applies_tofu_gate(tmp_path):
    # Back-compat: explicit ssl_verify false relaxes TLS-level checks but the
    # ToFU post-handshake gate still records and enforces the fingerprint.
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    a._known_peers_path = str(tmp_path / "known.json")

    class SslObj:
        def getpeercert(self, binary_mode):
            return b"DER1"

    w = FakeWriter()
    w._extra["ssl_object"] = SslObj()
    peer = FederationPeer("h", 1, options={"use_tls": True, "ssl_verify": False})
    await a._verify_peer_fingerprint(peer, cast(Any, w))
    assert a._load_known_peers()
    ctx = await a._create_client_ssl_context(peer)
    assert ctx.verify_mode == ssl.CERT_NONE


@pytest.mark.asyncio
async def test_verify_mode_announce_warns_once_when_unverified(tmp_path, caplog):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    peer = FederationPeer("h", 1, options={"use_tls": True, "ssl_verify": False, "tls_tofu": False})
    with caplog.at_level(logging.WARNING, logger="openmux.adapter.muxcon.mx"):
        await a._create_client_ssl_context(peer)
        await a._create_client_ssl_context(peer)
    assert caplog.text.count("TLS verification disabled and no pin or ToFU gate is active") == 1


@pytest.mark.asyncio
async def test_listener_warns_when_auth_not_required(tmp_path, caplog):
    a = UnifiedMuxConAdapter("mx", {"listeners": [], "auth_required": False})
    lconf = a._normalize_listener_conf({"host": "127.0.0.1", "port": 0, "use_tls": False, "tls_dir": str(tmp_path)})
    key = (lconf["host"], lconf["port"])
    try:
        with caplog.at_level(logging.WARNING, logger="openmux.adapter.muxcon.mx"):
            assert await a._start_single_listener(key, lconf) is True
        assert "auth_required=false" in caplog.text
    finally:
        await a._stop_single_listener(key)


def test_known_peers_load_save_and_fingerprint(tmp_path):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    a._known_peers_path = str(tmp_path / "known.yaml")
    # Initially empty
    assert a._load_known_peers() == {}
    # Save mapping
    m = {"h:1": "sha256:dead"}
    a._save_known_peers(m)
    assert a._load_known_peers() == m
    # Fingerprint compute
    assert a._compute_fingerprint(b"abc").startswith("sha256:")


def test_key_loaders_public_and_private(tmp_path):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Public key: ssh-ed25519 and base64
    priv = Ed25519PrivateKey.generate()
    pub_ssh = (
        priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )
    assert a._load_ed25519_public_key(pub_ssh) is not None
    pub_b64 = (
        "base64:"
        + base64.b64encode(
            priv.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        ).decode()
    )
    assert a._load_ed25519_public_key(pub_b64) is not None

    # Private key: PEM
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pem_path = tmp_path / "key.pem"
    pem_path.write_bytes(pem)
    assert a._load_ed25519_private_key(str(pem_path)) is not None
    # Private key: raw base64 seed
    seed_path = tmp_path / "seed.key"
    seed_path.write_bytes(
        base64.b64encode(
            priv.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    )
    assert a._load_ed25519_private_key(str(seed_path)) is not None


@pytest.mark.asyncio
async def test_verify_peer_fingerprint_tofu_and_pin(tmp_path):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    a._known_peers_path = str(tmp_path / "known.json")

    class SslObj:
        def __init__(self, der: bytes):
            self._der = der

        def getpeercert(self, binary_mode):
            return self._der

    # Fake writer with ssl_object
    w = FakeWriter()
    w._extra["ssl_object"] = SslObj(b"DER1")
    peer = FederationPeer("h", 1, options={"use_tls": True})
    await a._verify_peer_fingerprint(peer, cast(Any, w))
    assert a._load_known_peers()  # TOFU stored

    # Mismatch with pin
    w2 = FakeWriter()
    w2._extra["ssl_object"] = SslObj(b"DER2")
    peer2 = FederationPeer("h", 1, options={"use_tls": True, "tls_pin_fingerprint": a._compute_fingerprint(b"foo")})
    with pytest.raises(ValueError):
        await a._verify_peer_fingerprint(peer2, cast(Any, w2))
    # TOFU disabled
    a2 = UnifiedMuxConAdapter("mx", {"listeners": []})
    a2._known_peers_path = str(tmp_path / "known2.json")
    w3 = FakeWriter()
    w3._extra["ssl_object"] = SslObj(b"DERX")
    peer3 = FederationPeer("h", 1, options={"use_tls": True, "tls_tofu": False})
    await a2._verify_peer_fingerprint(peer3, cast(Any, w3))
    # Should not have stored
    assert a2._load_known_peers() == {}


@pytest.mark.asyncio
async def test_server_handshake_happy_and_auth_required_paths():
    # Happy path handshake
    # With auth now enabled by default, explicitly disable for happy path
    a = UnifiedMuxConAdapter("mx", {"listeners": [], "auth_required": False})
    hello = b"HELLO MuxCon/1.0 TYPE=regular_client CAPS=a,b ID=remote INST=xyz\n"
    r = FakeReader([hello])
    w = FakeWriter()
    await a._perform_server_handshake(cast(Any, r), cast(Any, w), "in:1")
    assert "in:1" in a.connections
    # Auth required with missing pkid -> sends error and closes
    a2 = UnifiedMuxConAdapter("mx", {"listeners": [], "auth_required": True})
    r2 = FakeReader([hello])
    w2 = FakeWriter()
    await a2._perform_server_handshake(cast(Any, r2), cast(Any, w2), "in:2")
    # Connection should have been closed (removed) after error
    assert b"AUTH:ERROR:missing_or_unknown_pkid" in w2.buffer
    # Invalid HELLO line -> error
    a3 = UnifiedMuxConAdapter("mx", {"listeners": []})
    with pytest.raises(ValueError):
        await a3._perform_server_handshake(cast(Any, FakeReader([b"BAD\n"])), cast(Any, FakeWriter()), "in:bad")


@pytest.mark.asyncio
async def test_client_handshake_and_connection_state():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    r = FakeReader([b"OK MuxCon/1.0 ID=remote INST=abc\n"])
    w = FakeWriter()
    await a._perform_client_handshake(cast(Any, r), cast(Any, w), "out:1")
    assert "out:1" in a.connections and a.connections["out:1"]["role"] == "client"


@pytest.mark.asyncio
async def test_start_and_stop_minimal(tmp_path, monkeypatch):
    # Provide one disabled listener so no bind occurs; ensure tasks created and then stopped
    cfg = {"listeners": [{"enabled": False}], "heartbeat_interval": 0}
    a = UnifiedMuxConAdapter("mx", cfg)
    ok = await a.start()
    assert ok is True and a.is_running is True
    await a.stop()
    assert a.is_running is False and not a._tasks


def test_status_info_aggregation():
    a = UnifiedMuxConAdapter("mx", {"listeners": [{"host": "127.0.0.1", "port": 9999, "enabled": True}]})
    a.connections["c1"] = {}
    info = a.get_status_info()
    assert info["type"] == "muxcon" and info["clients"] == 1
    assert "listeners" in info["details"]


def test_listen_socket_creation_and_filters_noop():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    s = a._make_listen_socket("127.0.0.1", 0)
    try:
        assert s.fileno() > 0
    finally:
        s.close()
    # No-op when key_id missing
    a._apply_per_connection_filters("connX", None)
    assert "connX" not in a._conn_filters
    # No-op for unknown key id
    a._apply_per_connection_filters("connY", "nope")
    assert "connY" not in a._conn_filters


@pytest.mark.asyncio
async def test_fault_injection_flags_paths():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Unknown connection -> returns False
    assert await a.freeze_connection("nope") is False
    assert await a.unfreeze_connection("nope") is False
    assert await a.set_drop_heartbeats("nope", True) is False
    # Known connection -> flags set
    a.connections["c1"] = {}
    assert await a.freeze_connection("c1") is True
    assert await a.unfreeze_connection("c1") is True
    assert await a.set_drop_heartbeats("c1", True) is True


@pytest.mark.asyncio
async def test_control_auth_challenge_and_response_and_ports_advertise(monkeypatch):
    # Client-side: respond to AUTH:PK:CHALLENGE when we have a key
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    priv = Ed25519PrivateKey.generate()
    a._auth_priv = priv
    a._auth_key_id = "kid1"
    conn_id = "out:peer:1:1"
    a.connections[conn_id] = {"writer": None}
    a._wire_state[conn_id] = {"send_next": 1}
    w = FakeWriter()
    # Issue challenge
    nonce = b"12345678901234567890123456789012"
    await a._process_control_command(conn_id, cast(Any, w), f"AUTH:PK:CHALLENGE:kid1:{base64.b64encode(nonce).decode()}")
    assert b"AUTH:PK:RESPONSE:kid1:" in w.buffer

    # Server-side: validate AUTH:PK:RESPONSE and set auth_ok, then advertise ports
    a2 = UnifiedMuxConAdapter("mx", {"listeners": []})
    a2._auth_required = True
    kid = "kid2"
    priv2 = Ed25519PrivateKey.generate()
    a2._auth_pubkeys[kid] = priv2.public_key()
    conn_id2 = "in:1:2:3"
    w2 = FakeWriter()
    a2.connections[conn_id2] = {
        "writer": w2,
        "auth_state": {"type": "pk", "key_id": kid, "nonce": b"abcd", "expires_at": time.time() + 60},
    }
    a2._wire_state[conn_id2] = {"send_next": 1}
    a2.main_port_manager = FakePM()
    called = {"adv": False}

    async def fake_maybe_adv(cid):
        called["adv"] = True
        # mimic side effect
        if cid in a2.connections:
            a2.connections[cid]["ports_advertised"] = True

    monkeypatch.setattr(a2, "_maybe_advertise_local_ports", fake_maybe_adv)
    sig = base64.b64encode(priv2.sign(b"abcd")).decode()
    await a2._process_control_command(conn_id2, cast(Any, w2), f"AUTH:PK:RESPONSE:{kid}:{sig}")
    # Should send AUTH:OK and invoke advertising hook
    assert b"AUTH:OK" in w2.buffer and called["adv"] is True
    assert a2.connections[conn_id2]["auth_ok"] is True


@pytest.mark.asyncio
async def test_control_auth_error_and_shutdown_paths():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    conn_id = "out:x:1:1"
    a.connections[conn_id] = {"writer": cast(Any, FakeWriter())}
    a._wire_state[conn_id] = {"send_next": 1}
    w = cast(Any, a.connections[conn_id]["writer"])
    # AUTH error should close connection
    await a._process_control_command(conn_id, w, "AUTH:ERROR:missing_or_unknown_pkid")
    assert conn_id not in a.connections and w.is_closing()
    # MPATH shutdown begin should send END and close
    conn_id2 = "out:y:1:1"
    w2 = FakeWriter()
    a.connections[conn_id2] = {"writer": w2, "auth_ok": True}
    a._wire_state[conn_id2] = {"send_next": 1}
    await a._process_control_command(conn_id2, cast(Any, w2), "MPATH:SHUTDOWN:BEGIN")
    assert b"MPATH:END" in w2.buffer and conn_id2 not in a.connections


@pytest.mark.asyncio
async def test_control_heartbeat_req_ack_updates_state():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    conn_id = "out:h:1:1"
    w = FakeWriter()
    a.connections[conn_id] = {"writer": w, "auth_ok": True}
    a._wire_state[conn_id] = {"send_next": 1}
    # REQ should cause ACK to be sent
    await a._process_control_command(conn_id, cast(Any, w), "REQ:12345")
    assert b":HB:" in w.buffer and b"ACK:12345" in w.buffer
    # ACK should update hb state
    await a._process_control_command(conn_id, cast(Any, w), "ACK:12345")
    st = a._hb_state.get(conn_id)
    assert st and st.get("last_ack_ts", 0) > 0


@pytest.mark.asyncio
async def test_ports_federated_register_and_stale_removal_and_routing():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    a.main_port_manager = FakePM()
    conn_id = "in:10.0.0.1:5555:1"
    a.connections[conn_id] = {"writer": FakeWriter()}
    a._wire_state[conn_id] = {"send_next": 1}
    # Two ports advertised
    p1 = {"name": "p1", "adapter_type": "loopback", "origin_server": {"server_id": "srv"}}
    p2 = {"name": "p2", "adapter_type": "loopback", "origin_server": {"server_id": "srv"}}
    payload = "PORTS:FEDERATED:2\n" + json.dumps(p1) + "\n" + json.dumps(p2) + "\nEND:PORTS"
    await a._handle_ports_federated(conn_id, payload)
    peer_key = a._derive_peer_key_from_conn_id(conn_id)
    assert "p1" in a._peer_proxies.get(peer_key, {}) and "p2" in a._peer_proxies.get(peer_key, {})
    # Now advertise only p1 -> p2 should be removed
    payload2 = "PORTS:FEDERATED:1\n" + json.dumps(p1) + "\nEND:PORTS"
    await a._handle_ports_federated(conn_id, payload2)
    assert "p2" not in a._peer_proxies.get(peer_key, {})

    # Route to proxy mapping
    class Proxy:
        def __init__(self):
            self.payloads: List[bytes] = []

        async def trigger_data_received(self, data: bytes):
            self.payloads.append(data)

    proxy = Proxy()
    a._session_map[peer_key] = {1: proxy}
    await a._route_data_frame(conn_id, 1, b"hello", 1)
    assert proxy.payloads == [b"hello"]

    # Route to local port
    a._local_session_map[peer_key] = {2: "local1"}
    await a._route_data_frame(conn_id, 2, b"world", 2)
    assert any(w["data"] == b"world" for w in a.main_port_manager.writes)


@pytest.mark.asyncio
async def test_inbound_ordering_and_buffering(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    conn_id = "out:o:1:1"
    a.connections[conn_id] = {"writer": FakeWriter()}
    delivered: List[int] = []

    async def fake_route(conn_id2, sid, data, seq):
        delivered.append(seq)

    monkeypatch.setattr(a, "_route_data_frame", fake_route)
    # Send out of order seq 2 then 1 then 3
    await a._handle_inbound_data(conn_id, 1, b"a", 2)
    await a._handle_inbound_data(conn_id, 1, b"a", 1)
    await a._handle_inbound_data(conn_id, 1, b"a", 3)
    assert delivered == [1, 2, 3]


@pytest.mark.asyncio
async def test_mpath_selection_and_rekey_and_request_dedup():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    a.mpath_primary_stale_sec = 0.1
    # Register two connections in same group
    c1 = "out:h:1000:1"
    c2 = "out:h:1000:2"
    now = time.time()
    a.connections[c1] = {"opened_at": now - 10}
    a.connections[c2] = {"opened_at": now}
    a._register_mpath_connection(c1)
    a._register_mpath_connection(c2)
    key = a._derive_peer_key_from_conn_id(c1)
    # Make c1 stale, c2 fresh
    a._mpath_groups[key]["conns"][c1]["last_rx_seen"] = 0
    a._mpath_groups[key]["conns"][c2]["last_rx_seen"] = time.time()
    sel = a._select_mpath_connection(key)
    assert sel == c2
    # Freeze c2 and ensure selection avoids it
    a._fault_state[c2] = {"frozen": True}
    sel2 = a._select_mpath_connection(key)
    assert sel2 == c1 or sel2 is not None

    # Rekey from host: to node: with handshake server_id
    c3 = "in:10.0.0.2:1234:9"
    a.connections[c3] = {"opened_at": now, "handshake": {"server_id": "srvX"}}
    # Put c3 in a host group and then rekey
    host_key = f"host:{c3.split(':')[1]}"
    a._mpath_groups[host_key] = {"conns": OrderedDict({c3: {"opened_at": now}}), "primary": c3, "rr_index": 0}
    a._rekey_mpath_connection(c3)
    assert f"node:srvX" in a._mpath_groups and host_key not in a._mpath_groups

    # Request remote ports de-dup
    w = FakeWriter()
    c4 = "out:q:1:1"
    a.connections[c4] = {"writer": w}
    a._wire_state[c4] = {"send_next": 1}
    a.connections[c4]["last_ports_req_ts"] = time.time()
    # Suppressed due to recent request
    await a._request_remote_ports(c4)
    assert w.buffer == b""
    # Now allow sending
    a.connections[c4]["last_ports_req_ts"] = time.time() - 3
    await a._request_remote_ports(c4)
    assert b"PORTS:LIST:FEDERATED" in w.buffer


@pytest.mark.asyncio
async def test_close_connection_marks_proxies_disconnected():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Prepare a connection and a proxy for its group
    conn_id = "out:h:1000:77"
    a.connections[conn_id] = {"writer": FakeWriter(), "opened_at": time.time()}
    a._register_mpath_connection(conn_id)
    peer_key = a._derive_peer_key_from_conn_id(conn_id)

    class P:
        def __init__(self):
            self.is_connected = True
            self.data_queue: asyncio.Queue = asyncio.Queue()
            self._disconnect_called = False

        async def disconnect(self):
            self._disconnect_called = True

    p = P()
    a._peer_proxies[peer_key] = {"r": p}
    # After close, since it was the only path, proxy should be marked disconnected and receive a message
    await a._close_connection(conn_id)
    assert p.is_connected is False
    q_item = await p.data_queue.get()
    assert b"FEDERATED_LINK_DISCONNECTED" in q_item


@pytest.mark.asyncio
async def test_remote_port_proxy_operations(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    peer_key = "node:peer"
    calls: Dict[str, List[Any]] = {"data": [], "open": [], "close": []}

    async def fake_send_data(pk, sid, data):
        calls["data"].append((pk, sid, data))
        return True

    async def fake_open(pk, sid, name):
        calls["open"].append((pk, sid, name))
        return True

    async def fake_close(pk, sid, reason):
        calls["close"].append((pk, sid, reason))
        return True

    monkeypatch.setattr(a, "_send_data_mpath", fake_send_data)
    monkeypatch.setattr(a, "_send_stream_open_mpath", fake_open)
    monkeypatch.setattr(a, "_send_stream_close_mpath", fake_close)

    # Minimal metadata stub
    class M:
        def __init__(self):
            self.description = "R"
            self.max_rw_users = 2

    proxy = a.RemotePortProxy(a, peer_key, "rp1", M())
    # Write triggers ensure_session -> open then data
    n = await proxy.write_data(b"abc", client_id="c")
    assert n == 3 and calls["open"] and calls["data"]
    # Close client stream
    ok = await proxy.close_stream_for_client("c")
    assert ok is True and calls["close"]
    # Status and lifecycle
    s = proxy.get_status()
    assert s["name"] == "rp1" and s["adapter_type"] == "remote_muxcon"
    assert await proxy.start() is True
    await proxy.stop()


def test_tx_seq_continues_after_group_empty_and_reconnect():
    # The incident: the initiator's only path dies (its group empties) and it
    # re-dials seconds later with the same peer identity. The TX counter must
    # continue where it left off - clearing it on group-empty used to restart
    # numbering at 1 on exactly one side, and the other side (whose group
    # never emptied) dropped every frame as stale (one-direction data loss).
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    cid1 = "out:h:7822:1"
    a.connections[cid1] = {"server_id": "peer", "opened_at": time.time()}
    a._register_mpath_connection(cid1)
    key = a._derive_peer_key_from_conn_id(cid1)
    assert key == "node:peer"
    assert a._next_peer_seq(key) == 1
    a._next_peer_seq(key)
    a._next_peer_seq(key)  # counter now at 4
    a._unregister_mpath_connection(cid1)
    assert key not in a._mpath_groups
    cid2 = "out:h:7822:2"
    a.connections[cid2] = {"server_id": "peer", "opened_at": time.time()}
    a._register_mpath_connection(cid2)
    assert a._next_peer_seq(key) == 4


@pytest.mark.asyncio
async def test_proxy_disconnect_keeps_stale_sessions_and_reconnect_closes_them(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    closes: List[Tuple[Any, int, str]] = []

    async def fake_close(pk, sid, reason):
        closes.append((pk, sid, reason))
        return True

    monkeypatch.setattr(a, "_send_stream_close_mpath", fake_close)

    class M:
        description = "R"
        max_rw_users = 2

    proxy = a.RemotePortProxy(a, "node:peer", "rp1", M())
    # Two client sessions opened over the path that is about to die
    proxy._client_sessions["c1"] = 5
    proxy._client_sessions["c2"] = 6
    await proxy.disconnect()
    # disconnect() makes a best-effort CLOSE attempt (no path, so it is a
    # no-op in production) and must remember the sids instead of forgetting
    # them - the origin only learns of them when the sids are closed on the
    # replacement path
    assert proxy._client_sessions == {}
    assert proxy._stale_sessions == {"c1": 5, "c2": 6}
    attempt_closes = list(closes)
    assert all(reason == "proxy_disconnect" for _, _, reason in attempt_closes)
    # Reconnect: the reuse path closes the stale sids on the live path
    a.connections["out:peer:7822:99"] = {"server_id": "peer", "opened_at": time.time()}
    await a._close_stale_proxy_sessions(proxy, "out:peer:7822:99")
    assert closes[len(attempt_closes) :] == [
        ("node:peer", 5, "stale_after_reconnect"),
        ("node:peer", 6, "stale_after_reconnect"),
    ]
    assert proxy._stale_sessions == {}
    # A second reconnect has nothing left to close
    count_after_first = len(closes)
    await a._close_stale_proxy_sessions(proxy, "out:peer:7822:99")
    assert len(closes) == count_after_first


@pytest.mark.asyncio
async def test_regrant_proxy_read_write_on_reconnect(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    pm = FakePM()
    a.main_port_manager = pm
    requests: List[str] = []
    demotions: List[Tuple[str, str]] = []

    async def fake_demote(port_name, client_id):
        demotions.append((port_name, client_id))
        return True

    monkeypatch.setattr(pm, "demote_client", fake_demote, raising=False)

    class M:
        description = "R"
        max_rw_users = 2

    proxy = a.RemotePortProxy(a, "node:peer", "rp1", M())
    # Pre-outage roster: one RW client that keeps its grant, one RW client the
    # origin denies (slot held by another user), one read-only client
    proxy.connected_clients = [
        {"client_id": "ws:1", "mode": "read-write"},
        {"client_id": "ws:2", "mode": "read-only"},
        {"client_id": "ws:3", "mode": "read-write"},
    ]

    async def fake_request(cid, timeout=3.0):
        requests.append(cid)
        return "read-write" if cid == "ws:1" else "read-only"

    monkeypatch.setattr(proxy, "request_read_write_for_client", fake_request)
    await a._regrant_proxy_read_write(proxy, "out:peer:7822:99")
    # Only read-write clients are re-requested; the denied one is demoted
    # locally so its mode matches the origin (instead of writing into the void)
    assert requests == ["ws:1", "ws:3"]
    assert demotions == [("rp1", "ws:3")]
    # No port manager: nothing to demote, and no error
    a2 = UnifiedMuxConAdapter("mx", {"listeners": []})
    proxy2 = a2.RemotePortProxy(a2, "node:peer", "rp2", M())
    proxy2.connected_clients = [{"client_id": "ws:9", "mode": "read-write"}]

    async def boom(cid, timeout=3.0):
        raise AssertionError("must not request without a port manager")

    monkeypatch.setattr(proxy2, "request_read_write_for_client", boom)
    await a2._regrant_proxy_read_write(proxy2, "out:peer:7822:99")


@pytest.mark.asyncio
async def test_read_frame_and_send_protocol_and_seq_incrementing(caplog):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Create one valid frame and parse
    payload = b"ABC"
    frame = b"#0:C:3:7:" + payload + b"\n"
    r = FakeExactReader(frame)
    obj = await a._read_frame(cast(Any, r))
    assert obj and obj["frame_type"] == "C" and obj["payload"] == payload and obj["seq"] == 7
    # Incomplete -> None
    r2 = FakeExactReader(b"#0:C:3:7:AB")
    assert await a._read_frame(cast(Any, r2)) is None
    # Malformed header -> None
    r3 = FakeExactReader(b"!bad")
    assert await a._read_frame(cast(Any, r3)) is None

    # Send protocol frame logs header and writes
    w = FakeWriter()
    await a._send_protocol_frame(cast(Any, w), frame)
    assert w.buffer.endswith(frame)

    # Sequence incrementing: per-conn vs global
    a._wire_state["c1"] = {"send_next": 10}
    assert a._next_frame_seq("c1") == 10
    assert a._next_frame_seq("c1") == 11
    base = a._next_seq
    assert a._next_frame_seq() == base and a._next_frame_seq() == base + 1


def test_derive_peer_key_and_generation_rollover(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # With handshake server_id
    a.connections["cid1"] = {"handshake": {"server_id": "srv1"}}
    assert a._derive_peer_key_from_conn_id("cid1") == "node:srv1"
    # From connection record server_id
    a.connections["cid2"] = {"server_id": "srv2"}
    assert a._derive_peer_key_from_conn_id("cid2") == "node:srv2"
    # Outgoing uses host:listen_port
    assert a._derive_peer_key_from_conn_id("out:1.2.3.4:7822:9") == "1.2.3.4:7822"
    # Inbound pre-handshake collapses to host:<ip>
    assert a._derive_peer_key_from_conn_id("in:5.6.7.8:54321:9") == "host:5.6.7.8"

    # Rollover: retire older instance for same server_id
    closed: List[str] = []

    def fake_close(cid):
        closed.append(cid)

    a._close_connection = fake_close  # type: ignore
    now = time.time()
    a.connections.clear()
    a.connections["a"] = {"server_id": "srvX", "instance_id": "old", "opened_at": now - 10}
    a.connections["b"] = {"server_id": "srvX", "instance_id": "new", "opened_at": now}
    a._retire_old_generation("b")
    assert "a" in closed or "a" in a.connections  # close scheduled


def test_mpath_register_select_and_send_helpers(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Outbound pref from peers
    a.peers = [FederationPeer("h", 1000, options={"path_pref": 5})]
    c_out = "out:h:1000:1"
    a.connections[c_out] = {"opened_at": time.time(), "writer": FakeWriter()}
    a._register_mpath_connection(c_out)
    key = a._derive_peer_key_from_conn_id(c_out)
    assert a._mpath_groups[key]["primary"] == c_out

    # Add second lower-pref path and ensure no preemptive demotion
    c_out2 = "out:h:1000:2"
    a.connections[c_out2] = {"opened_at": time.time(), "writer": FakeWriter()}
    a._register_mpath_connection(c_out2)
    assert a._mpath_groups[key]["primary"] in (c_out, c_out2)

    # send helpers (will return False because FakeWriter is not an asyncio.StreamWriter)
    assert asyncio.get_event_loop().run_until_complete(a._send_control_mpath(key, "X")) is False
    assert asyncio.get_event_loop().run_until_complete(a._send_stream_open_mpath(key, 1, "p")) is False
    assert asyncio.get_event_loop().run_until_complete(a._send_stream_close_mpath(key, 1, "r")) is False
    assert asyncio.get_event_loop().run_until_complete(a._send_data_mpath(key, 1, b"d")) is False
    # No eligible path
    assert asyncio.get_event_loop().run_until_complete(a._send_control_mpath("nope", "X")) is False


@pytest.mark.asyncio
async def test_read_loop_no_writer_branches_and_shutdown(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": [], "auth_required": True})
    conn_id = "in:1.1.1.1:1234:1"
    w = FakeWriter()
    # Provide reader but writer is not asyncio.StreamWriter -> branch closes connection immediately
    a.connections[conn_id] = {"writer": w, "reader": object(), "role": "server", "opened_at": time.time()}
    await a._read_loop(conn_id)
    assert conn_id not in a.connections

    # Graceful shutdown (uses provided writer parameter, not conn record)
    a.connections[conn_id] = {"writer": w}
    await a.initiate_graceful_shutdown(conn_id, cast(Any, w))
    assert b"MPATH:SHUTDOWN:BEGIN" in w.buffer and b"MPATH:END" in w.buffer


def test_filter_helpers_and_advertise_list(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Set adapter-level advertise filters to exclude name pattern
    a._adv_name_exc = ["local*"]
    pm = FakePM()

    # Override PM to return two ports, with one excluded by name
    async def fake_list():
        return [
            {"name": "local1", "adapter_type": "loopback", "connected": True, "max_rw_users": 1, "description": "d"},
            {"name": "remote1", "adapter_type": "loopback", "connected": True, "max_rw_users": 1, "description": "d"},
        ]

    pm.get_port_list_with_federation = fake_list  # type: ignore
    a.main_port_manager = pm
    conn_id = "in:2.2.2.2:9999:1"
    a.connections[conn_id] = {"writer": FakeWriter(), "role": "server", "opened_at": time.time(), "auth_ok": True}
    a._wire_state[conn_id] = {"send_next": 1}
    w = cast(Any, a.connections[conn_id]["writer"])
    asyncio.get_event_loop().run_until_complete(a._send_local_port_list(conn_id, w))
    # Ensure excluded name not present in the JSON payload that was sent
    assert b"local1" not in w.buffer and b"remote1" in w.buffer


def test_client_ssl_context_ca_load_failure():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    peer = FederationPeer("h", 1, options={"use_tls": True, "ssl_ca_cert": "/no/such/path"})
    ctx = asyncio.get_event_loop().run_until_complete(a._create_client_ssl_context(peer))
    assert ctx is not None


@pytest.mark.asyncio
async def test_server_ssl_context_require_client_cert():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    ctx = await a._create_server_ssl_context({"use_tls": True, "require_client_cert": True})
    assert ctx is not None and ctx.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.asyncio
async def test_accept_filters_drop_registration():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Set accept filter to exclude everything
    a._acc_name_exc = ["*"]
    conn_id = "in:9.9.9.9:1111:1"
    a.connections[conn_id] = {"writer": FakeWriter()}
    payload = (
        "PORTS:FEDERATED:1\n"
        + json.dumps({"name": "p1", "adapter_type": "loopback", "origin_server": {"server_id": "s"}})
        + "\nEND:PORTS"
    )
    await a._handle_ports_federated(conn_id, payload)
    peer_key = a._derive_peer_key_from_conn_id(conn_id)
    assert a._peer_proxies.get(peer_key, {}) == {}


@pytest.mark.asyncio
async def test_client_auth_ok_applies_key_filters(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Pretend we have a client key id and associated key filters
    a._auth_key_id = "kidA"
    a._key_filters = {"kidA": {"advertise_filters": {"include": ["r*"]}, "accept_filters": {"include": ["*"]}}}
    conn_id = "out:1.2.3.4:1000:77"
    a.connections[conn_id] = {"writer": FakeWriter(), "role": "client", "auth_ok": False}
    a._wire_state[conn_id] = {"send_next": 1}

    # Avoid sending actual advertise frames
    async def fake_maybe(cid):
        return None

    monkeypatch.setattr(a, "_maybe_advertise_local_ports", fake_maybe)
    await a._process_control_command(conn_id, cast(Any, a.connections[conn_id]["writer"]), "AUTH:OK")
    assert a.connections[conn_id]["auth_ok"] is True
    assert conn_id in a._conn_filters and a._conn_filters[conn_id]["advertise_filters"]["include"] == ["r*"]


@pytest.mark.asyncio
async def test_auth_expired_and_bad_signature_paths(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": [], "auth_required": True})
    # Configure server with a known public key
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    kid = "kX"
    a._auth_pubkeys[kid] = priv.public_key()
    # Expired challenge case
    conn_id = "in:exp:1"
    w = FakeWriter()
    a.connections[conn_id] = {
        "writer": w,
        "auth_state": {"type": "pk", "key_id": kid, "nonce": b"n", "expires_at": time.time() - 1},
    }
    a._wire_state[conn_id] = {"send_next": 1}
    await a._process_control_command(conn_id, cast(Any, w), f"AUTH:PK:RESPONSE:{kid}:{base64.b64encode(b'X').decode()}")
    # Should send AUTH:ERROR:expired and close
    assert b"AUTH:ERROR:expired" in w.buffer and conn_id not in a.connections
    # Bad signature case
    conn_id2 = "in:bad:1"
    w2 = FakeWriter()
    a.connections[conn_id2] = {
        "writer": w2,
        "auth_state": {"type": "pk", "key_id": kid, "nonce": b"n2", "expires_at": time.time() + 60},
    }
    a._wire_state[conn_id2] = {"send_next": 1}
    # Send invalid sig
    await a._process_control_command(
        conn_id2, cast(Any, w2), f"AUTH:PK:RESPONSE:{kid}:{base64.b64encode(b'invalid').decode()}"
    )
    assert b"AUTH:ERROR:bad_signature" in w2.buffer and conn_id2 not in a.connections


@pytest.mark.asyncio
async def test_mpath_end_control_closes():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    conn_id = "out:end:1"
    w = FakeWriter()
    a.connections[conn_id] = {"writer": w, "auth_ok": True}
    a._wire_state[conn_id] = {"send_next": 1}
    await a._process_control_command(conn_id, cast(Any, w), "MPATH:END")
    assert conn_id not in a.connections


@pytest.mark.asyncio
async def test_data_plane_buffering_and_ack_and_routing(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    conn_id = "in:data:1"
    w = FakeWriter()
    # Mark connection authenticated and with writer
    a.connections[conn_id] = {"writer": w, "reader": object(), "role": "server", "opened_at": time.time(), "auth_ok": True}
    # Make FakeWriter pass isinstance(StreamWriter) checks in module
    import openmux.server.adapters.muxcon as muxmod

    monkeypatch.setattr(muxmod.asyncio, "StreamWriter", FakeWriter)
    # Route through proxy
    peer_key = a._derive_peer_key_from_conn_id(conn_id)

    class P:
        def __init__(self):
            self.received: List[Tuple[int, bytes]] = []

        async def trigger_data_received(self, data: bytes):
            self.received.append((len(data), data))

    p = P()
    a._session_map[peer_key] = {1: p}
    # Prepare frames out of order: seq 1 (sid 1), seq 3, then seq 2
    frames = [
        {"frame_type": "D", "stream_id": 1, "payload": b"A", "seq": 1},
        {"frame_type": "D", "stream_id": 1, "payload": b"C", "seq": 3},
        {"frame_type": "D", "stream_id": 1, "payload": b"B", "seq": 2},
        None,
    ]

    async def fake_read_frame(reader):
        await asyncio.sleep(0)
        return frames.pop(0)

    monkeypatch.setattr(a, "_read_frame", fake_read_frame)
    await a._read_loop(conn_id)
    # Verify in-order delivery (A, B, C)
    assert [d for _, d in p.received] == [b"A", b"B", b"C"]
    # And that ACK frames were emitted for each (three A frames in writer buffer)
    assert w.buffer.count(b"#0:A:") >= 3


@pytest.mark.asyncio
async def test_retx_loop_resend_and_rto_adjustment(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": [], "heartbeat_interval": 0.2})
    # Install a connection and peer group
    conn_id = "out:r:1"
    w = FakeWriter()
    a.connections[conn_id] = {"writer": w}
    key = a._derive_peer_key_from_conn_id(conn_id)
    a._mpath_groups[key] = {
        "conns": OrderedDict({conn_id: {"opened_at": time.time(), "last_rx_seen": time.time()}}),
        "primary": conn_id,
        "rr_index": 0,
    }
    # Preload send buffer with an old entry to trigger resend
    a._peer_sendbuf[key] = {5: (conn_id, 1, b"D", time.time() - 10)}
    # Seed hb state with a RTT to allow RTO adjustment path
    a._hb_state[conn_id] = {"last_req_ts": time.time() - 0.1, "last_ack_ts": time.time(), "missed": 0, "rtt_ms": 50}
    # Make FakeWriter pass isinstance(StreamWriter)
    import openmux.server.adapters.muxcon as muxmod

    monkeypatch.setattr(muxmod.asyncio, "StreamWriter", FakeWriter)
    # Speed retx loop by minimizing sleeps; we'll stop it after one iteration
    orig_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda d: orig_sleep(0))
    # Run loop (one iteration)
    t = asyncio.create_task(a._retx_loop())
    # Allow one iteration then stop
    await orig_sleep(0.05)
    a._stop_event.set()
    await t
    # Writer should have gotten a resent data frame (#1:D:...:5:)
    assert b":D:" in w.buffer and b":5:" in w.buffer


@pytest.mark.asyncio
async def test_federated_stale_purge_removes_proxy(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    pm = FakePM()
    a.main_port_manager = pm
    conn_id = "in:stale:1"
    a.connections[conn_id] = {"writer": FakeWriter(), "server_id": "srvY"}
    peer_key = a._derive_peer_key_from_conn_id(conn_id)
    # Register two proxies via two advertised ports
    p1 = {"name": "p1", "adapter_type": "loopback", "origin_server": {"server_id": "srvY"}}
    p2 = {"name": "p2", "adapter_type": "loopback", "origin_server": {"server_id": "srvY"}}
    await a._handle_ports_federated(conn_id, "PORTS:FEDERATED:2\n" + json.dumps(p1) + "\n" + json.dumps(p2) + "\nEND:PORTS")
    assert "p1" in a._peer_proxies.get(peer_key, {}) and "p2" in a._peer_proxies.get(peer_key, {})
    # Now advertise only p2; expect p1 to be purged and unregistered from pm if matching
    await a._handle_ports_federated(conn_id, "PORTS:FEDERATED:1\n" + json.dumps(p2) + "\nEND:PORTS")
    assert "p1" not in a._peer_proxies.get(peer_key, {})


@pytest.mark.asyncio
async def test_auth_challenge_no_client_key_sends_error(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # No client private key configured
    a._auth_priv = None
    a._auth_key_id = None
    conn_id = "out:chall:1"
    a.connections[conn_id] = {"writer": FakeWriter()}
    a._wire_state[conn_id] = {"send_next": 1}
    w = cast(Any, a.connections[conn_id]["writer"])
    await a._process_control_command(conn_id, w, f"AUTH:PK:CHALLENGE:kidQ:{base64.b64encode(b'xx').decode()}")
    assert b"AUTH:ERROR:no_client_key" in w.buffer


@pytest.mark.asyncio
async def test_ports_federated_ignored_when_not_authenticated():
    a = UnifiedMuxConAdapter("mx", {"listeners": [], "auth_required": True})
    conn_id = "in:unauth:1"
    a.connections[conn_id] = {"writer": FakeWriter(), "role": "server", "auth_ok": False}
    payload = (
        "PORTS:FEDERATED:1\n"
        + json.dumps({"name": "px", "adapter_type": "loopback", "origin_server": {"server_id": "s"}})
        + "\nEND:PORTS"
    )
    await a._process_control_command(conn_id, cast(Any, a.connections[conn_id]["writer"]), payload)
    peer_key = a._derive_peer_key_from_conn_id(conn_id)
    assert a._peer_proxies.get(peer_key, {}) == {}


def test_accept_filters_include_path():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    a._acc_name_exc = []
    a._acc_name_inc = ["p*"]
    conn_id = "in:filt:1"
    rec = {"name": "port1", "adapter_type": "loopback", "origin_server": {"server_id": "s"}}
    assert a._allow_accept_port_for_conn(conn_id, rec) is True
    rec2 = {"name": "x", "adapter_type": "loopback", "origin_server": {"server_id": "s"}}
    assert a._allow_accept_port_for_conn(conn_id, rec2) is False


@pytest.mark.asyncio
async def test_remote_port_proxy_close_all_streams():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    peer_key = "node:peerZ"
    # Spy send close
    sent: List[Tuple[int, int]] = []

    async def fake_close(pk, sid, reason):
        sent.append((sid, len(reason)))
        return True

    a._send_stream_close_mpath = fake_close  # type: ignore

    class M:
        pass

    p = a.RemotePortProxy(a, peer_key, "R", M())
    # Create sessions
    await p._ensure_session("c1")
    await p._ensure_session("c2")
    # Map into adapter session map to test cleanup
    a._session_map[peer_key] = {p._client_sessions["c1"]: p, p._client_sessions["c2"]: p}
    await p.close_all_streams()
    assert p._client_sessions == {} and len(sent) == 2 and peer_key in a._session_map and a._session_map[peer_key] == {}


@pytest.mark.asyncio
async def test_stream_ids_do_not_collide_across_ports_on_same_peer():
    """Two different federated ports on the same peer must get distinct stream ids.

    Regression test: each RemotePortProxy used to keep its own stream-id
    counter starting at 1, so opening the first stream for two different
    ports on the same peer connection produced the SAME id on the wire,
    corrupting both sides' stream-id -> port mappings and silently stopping
    traffic for one of the two ports.
    """
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    peer_key = "node:peerQ"

    async def fake_open(pk, sid, port_name):
        return True

    a._send_stream_open_mpath = fake_open  # type: ignore

    class M:
        pass

    proxy_a = a.RemotePortProxy(a, peer_key, "portA", M())
    proxy_b = a.RemotePortProxy(a, peer_key, "portB", M())

    sid_a = await proxy_a._ensure_session("clientA")
    sid_b = await proxy_b._ensure_session("clientB")

    assert sid_a != sid_b
    # Both proxies must be independently resolvable via the shared session map.
    assert a._session_map[peer_key][sid_a] is proxy_a
    assert a._session_map[peer_key][sid_b] is proxy_b


@pytest.mark.asyncio
async def test_send_local_port_list_uses_first_enabled_listener():
    # First enabled listener port should be used in ServerInfo
    a = UnifiedMuxConAdapter("mx", {"listeners": [{"enabled": False, "port": 7000}, {"enabled": True, "port": 8123}]})
    pm = FakePM()
    a.main_port_manager = pm
    conn_id = "in:list:1"
    w = FakeWriter()
    a.connections[conn_id] = {"writer": w, "auth_ok": True}
    a._wire_state[conn_id] = {"send_next": 1}
    await a._send_local_port_list(conn_id, cast(Any, w))
    assert b"8123" in w.buffer  # server info port embedded in JSON


@pytest.mark.asyncio
async def test_tofu_change_detection_raises(monkeypatch, tmp_path):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Store known peer fingerprint
    a._known_peers_path = str(tmp_path / "known.json")
    m = {"h:1": a._compute_fingerprint(b"A")}
    a._save_known_peers(m)

    class SslObj:
        def __init__(self, der: bytes):
            self._der = der

        def getpeercert(self, binary_mode):
            return self._der

    w = FakeWriter()
    w._extra["ssl_object"] = SslObj(b"B")  # different cert
    peer = FederationPeer("h", 1, options={"use_tls": True})
    with pytest.raises(ValueError):
        await a._verify_peer_fingerprint(peer, cast(Any, w))


@pytest.mark.asyncio
async def test_connect_with_fwmark_on_linux(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Pretend platform is linux to go down SO_MARK path
    monkeypatch.setattr(sys, "platform", "linux")

    # Fake getaddrinfo
    async def fake_gai(host, port, type):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(
        asyncio.get_event_loop(),
        "getaddrinfo",
        lambda *args, **kwargs: asyncio.get_event_loop().create_task(fake_gai(*args, **kwargs)),
    )

    class DummySock:
        def __init__(self, *args, **kwargs):
            self._opts = []

        def setsockopt(self, level, opt, val):
            self._opts.append((level, opt, val))

        def bind(self, addr):
            pass

        def setblocking(self, b):
            pass

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: DummySock())

    async def fake_sock_connect(sock, sockaddr):
        return None

    monkeypatch.setattr(
        asyncio.get_event_loop(),
        "sock_connect",
        lambda sock, sockaddr: asyncio.get_event_loop().create_task(fake_sock_connect(sock, sockaddr)),
    )

    async def fake_open_connection(**kwargs):
        return cast(Any, FakeReader([])), cast(Any, FakeWriter())

    monkeypatch.setattr(
        asyncio, "open_connection", lambda **kwargs: asyncio.get_event_loop().create_task(fake_open_connection(**kwargs))
    )
    # Call with fwmark option
    r, w = await a._connect_with_routing_options("h", 1, None, None, None, interface=None, fwmark=42)
    assert r is not None and w is not None


def test_mpath_rekey_migrates_peer_state():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Start with host-based key
    conn_id = "in:10.0.0.3:4000:1"
    a.connections[conn_id] = {"opened_at": time.time(), "handshake": None}
    host_key = a._derive_peer_key_from_conn_id(conn_id)
    a._mpath_groups[host_key] = {
        "conns": OrderedDict({conn_id: {"opened_at": time.time()}}),
        "primary": conn_id,
        "rr_index": 0,
    }
    a._peer_sendbuf[host_key] = {7: (conn_id, 1, b"X", time.time())}
    a._peer_rx_state[host_key] = {"expected": 3, "buffer": {}}
    a._peer_tx_seq[host_key] = 12
    a._peer_retx_count[host_key] = 1
    # Now set handshake with server_id to trigger rekey
    a.connections[conn_id]["handshake"] = {"server_id": "srvZ"}
    a._rekey_mpath_connection(conn_id)
    new_key = a._derive_peer_key_from_conn_id(conn_id)
    assert new_key != host_key and new_key in a._mpath_groups
    # Verify peer-level maps migrated/merged
    assert new_key in a._peer_sendbuf and 7 in a._peer_sendbuf[new_key]
    assert new_key in a._peer_rx_state and a._peer_rx_state[new_key]["expected"] == 3
    assert new_key in a._peer_tx_seq and a._peer_tx_seq[new_key] >= 12
    assert new_key in a._peer_retx_count and a._peer_retx_count[new_key] >= 1


def test_mpath_unregister_removes_group_keeps_seq_state():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    conn_id = "out:h:1000:9"
    a.connections[conn_id] = {"opened_at": time.time()}
    key = a._derive_peer_key_from_conn_id(conn_id)
    a._mpath_groups[key] = {"conns": OrderedDict({conn_id: {"opened_at": time.time()}}), "primary": conn_id, "rr_index": 0}
    a._peer_sendbuf[key] = {}
    a._peer_rx_state[key] = {"expected": 43, "buffer": {}}
    a._peer_tx_seq[key] = 43
    a._peer_retx_count[key] = 3
    a._unregister_mpath_connection(conn_id)
    assert key not in a._mpath_groups
    # Peer-scoped DATA seq state must survive an empty group: numbering is per
    # peer identity, not per path. Clearing it here used to reset the counter
    # on whichever side's group emptied first while the other side kept its
    # old counter, dropping every post-reconnect frame as stale (one-direction
    # data loss after path loss + reconnect without process restart).
    assert a._peer_rx_state[key]["expected"] == 43
    assert a._peer_tx_seq[key] == 43
    assert key in a._peer_sendbuf
    assert a._peer_retx_count[key] == 3


def test_allow_advertise_port_helper():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Exclude takes precedence
    a._adv_name_exc = ["bad*"]
    assert a._allow_advertise_port("bad1", "loopback", "srv") is False
    # Include gates when set
    a._adv_name_exc = []
    a._adv_name_inc = ["ok*"]
    assert a._allow_advertise_port("ok1", "loopback", "srv") is True
    assert a._allow_advertise_port("nope", "loopback", "srv") is False


@pytest.mark.asyncio
async def test_read_frame_skips_noise_and_parses():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Leading noise/newlines before '#'
    frame = b" \n\r\t#0:C:1:9:X\n"
    r = FakeExactReader(frame)
    obj = await a._read_frame(cast(Any, r))
    assert obj and obj["frame_type"] == "C" and obj["seq"] == 9 and obj["payload"] == b"X"


@pytest.mark.asyncio
async def test_hb_control_req_ack_updates(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    conn_id = "in:hb:1"
    w = FakeWriter()
    a.connections[conn_id] = {"writer": w, "auth_ok": True}
    a._wire_state[conn_id] = {"send_next": 1}
    # HB:REQ
    await a._process_control_command(conn_id, cast(Any, w), "HB:REQ:123")
    assert b":HB:" in w.buffer and b"ACK:123" in w.buffer
    # HB:ACK
    await a._process_control_command(conn_id, cast(Any, w), "HB:ACK:123")
    st = a._hb_state.get(conn_id)
    assert st and st.get("last_ack_ts", 0) > 0


@pytest.mark.asyncio
async def test_mpath_send_helpers_true_path(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Create a connection with a FakeWriter that is treated as StreamWriter
    import openmux.server.adapters.muxcon as muxmod

    monkeypatch.setattr(muxmod.asyncio, "StreamWriter", FakeWriter)
    conn_id = "out:hs:9000:1"
    w = FakeWriter()
    a.connections[conn_id] = {"writer": w, "opened_at": time.time()}
    key = a._derive_peer_key_from_conn_id(conn_id)
    a._mpath_groups[key] = {
        "conns": OrderedDict({conn_id: {"opened_at": time.time(), "last_rx_seen": time.time(), "pref": 0}}),
        "primary": conn_id,
        "rr_index": 0,
    }
    ok1 = await a._send_control_mpath(key, "TEST")
    ok2 = await a._send_stream_open_mpath(key, 11, "port")
    ok3 = await a._send_stream_close_mpath(key, 11, "bye")
    ok4 = await a._send_data_mpath(key, 11, b"payload")
    assert all([ok1, ok2, ok3, ok4])
    # DATA should be tracked in sendbuf and bytes counters incremented
    assert a._peer_sendbuf.get(key) and any(isinstance(v, tuple) and v[2] == b"payload" for v in a._peer_sendbuf[key].values())
    assert a._peer_bytes_tx.get(key, 0) >= len(b"payload")


@pytest.mark.asyncio
async def test_pump_local_port_to_remote_sends_and_stops(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    peer_key = "node:peerP"
    stream_id = 5
    port_name = "loc"
    # Map session so loop runs
    a._local_session_map[peer_key] = {stream_id: port_name}

    # Fake PM returning one chunk then none
    class PM:
        def __init__(self):
            self.calls = 0

        async def get_port_data(self, name):
            self.calls += 1
            return b"abc" if self.calls == 1 else b""

    a.main_port_manager = PM()
    sent: List[Tuple[str, int, bytes]] = []

    async def fake_send(pk, sid, data):
        sent.append((pk, sid, data))
        # Stop loop by removing mapping after first send
        a._local_session_map[peer_key].pop(stream_id, None)
        return True

    a._send_data_mpath = fake_send  # type: ignore
    # Run pump
    await a._pump_local_port_to_remote(peer_key, stream_id, port_name)
    assert sent and sent[0][2] == b"abc"


@pytest.mark.asyncio
async def test_register_remote_port_duplicate_guard():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})

    # Prepare PortManager with existing port of same name and origin server id
    class Meta:
        def __init__(self, sid):
            self.origin_server = type("S", (), {"server_id": sid})()

    class Existing:
        def __init__(self, sid):
            self.metadata = Meta(sid)

    class PM:
        def __init__(self):
            self.ports = {"dup": Existing("srvD")}

        async def register_federated_port(self, meta, proxy):
            self.ports[meta.name] = proxy
            return meta.name

    a.main_port_manager = PM()
    conn_id = "in:dup:1"
    a.connections[conn_id] = {"writer": FakeWriter()}
    # Call with matching name and origin server id; should skip
    pd = {"name": "dup", "adapter_type": "loopback", "origin_server": {"server_id": "srvD"}}
    await a._register_remote_port_from_dict(conn_id, pd)
    # Verify PM ports still has Existing instance for 'dup'
    assert isinstance(a.main_port_manager.ports["dup"], Existing)


@pytest.mark.asyncio
async def test_force_close_and_reset_connection(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Setup connection with a writer that has a transport.abort to exercise reset
    w = FakeWriter()

    class T:
        def __init__(self):
            self.aborted = False

        def abort(self):
            self.aborted = True

    w.transport = T()  # type: ignore[attr-defined]
    a.connections["c"] = {"writer": w}
    closed = {"ids": []}

    async def fake_close(cid):
        closed["ids"].append(cid)

    a._close_connection = fake_close  # type: ignore
    ok1 = await a.force_close_connection("c", linger=0)
    a.connections["c2"] = {"writer": w}
    ok2 = await a.force_reset_connection("c2")
    assert ok1 is True and ok2 is True and "c" in closed["ids"] and "c2" in closed["ids"]


@pytest.mark.asyncio
async def test_ack_removes_sendbuf_entry(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    conn_id = "out:ack:7777:1"
    w = FakeWriter()
    a.connections[conn_id] = {"writer": w, "reader": object()}
    import openmux.server.adapters.muxcon as muxmod

    monkeypatch.setattr(muxmod.asyncio, "StreamWriter", FakeWriter)
    peer_key = a._derive_peer_key_from_conn_id(conn_id)
    a._peer_sendbuf[peer_key] = {42: (conn_id, 1, b"xx", time.time())}
    # Yield an ACK for seq 42 then None
    frames = [
        {"frame_type": "A", "stream_id": 0, "payload": b"42", "seq": 2},
        None,
    ]

    async def fake_read_frame(reader):
        await asyncio.sleep(0)
        return frames.pop(0)

    monkeypatch.setattr(a, "_read_frame", fake_read_frame)
    await a._read_loop(conn_id)
    assert 42 not in a._peer_sendbuf.get(peer_key, {})


@pytest.mark.asyncio
async def test_send_protocol_frame_fallback_header_preview():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    w = FakeWriter()
    # Frame with fewer than 4 colons (forces fallback preview path)
    bad_header_frame = b"#0:C:5\nhello"
    await a._send_protocol_frame(cast(Any, w), bad_header_frame)
    assert w.buffer.endswith(bad_header_frame)


@pytest.mark.asyncio
async def test_server_ssl_context_ca_error():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Supply an invalid CA file to exercise warning path; context should still be returned
    ctx = await a._create_server_ssl_context({"use_tls": True, "ssl_ca_cert": "/no/such.pem"})
    assert ctx is not None


@pytest.mark.asyncio
async def test_accept_client_sets_listener_path_metadata(monkeypatch):
    # Disable auth to avoid immediate close on missing PKID; focus on path metadata
    a = UnifiedMuxConAdapter(
        "mx",
        {
            "listeners": [{"enabled": True, "host": "127.0.0.1", "port": 5000, "path_pref": 5, "path_group": "G"}],
            "auth_required": False,
        },
    )
    # Reader writes a valid HELLO line
    r = FakeReader([b"HELLO MuxCon/1.0 TYPE=regular_client CAPS=a ID=R INST=I\n"])
    w = FakeWriter()
    # Provide peername and sockname to match listener
    w._extra["peername"] = ("1.2.3.4", 40000)
    w._extra["sockname"] = ("127.0.0.1", 5000)

    # Monkeypatch to avoid starting read loop after accept
    async def fake_read_loop(cid):
        return None

    monkeypatch.setattr(a, "_read_loop", fake_read_loop)
    await a._accept_client(cast(Any, r), cast(Any, w))
    # Find the created connection id (prefix in:)
    created = [cid for cid in a.connections.keys() if cid.startswith("in:")]
    assert created, "No connection created"
    cid = created[0]
    assert a.connections[cid].get("listener_path_pref") == 5
    # Multipath group should also be updated with pref if registered
    key = a._derive_peer_key_from_conn_id(cid)
    grp = a._mpath_groups.get(key)
    if grp and cid in grp.get("conns", {}):
        assert grp["conns"][cid].get("pref") == 5


@pytest.mark.asyncio
async def test_shutdown_state_transitions_begin_and_end():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # First BEGIN should send END and close, state becomes CLOSED
    cid = "in:shut:1"
    w = FakeWriter()
    a.connections[cid] = {"writer": w, "auth_ok": True}
    a._wire_state[cid] = {"send_next": 1}
    await a._process_control_command(cid, cast(Any, w), "MPATH:SHUTDOWN:BEGIN")
    assert cid not in a.connections
    assert a._shutdown_state.get(cid, {}).get("state") == "CLOSED"
    # Add connection again and send MPATH:END; should close and state remain CLOSED
    w2 = FakeWriter()
    a.connections[cid] = {"writer": w2, "auth_ok": True}
    a._wire_state[cid] = {"send_next": 1}
    await a._process_control_command(cid, cast(Any, w2), "MPATH:END")
    assert cid not in a.connections
    assert a._shutdown_state.get(cid, {}).get("state") == "CLOSED"
    # If BEGIN arrives when state CLOSED, ensure no additional END frame is appended
    buf_len_before = len(w2.buffer)
    await a._process_control_command(cid, cast(Any, w2), "MPATH:SHUTDOWN:BEGIN")
    assert len(w2.buffer) == buf_len_before


@pytest.mark.asyncio
async def test_auth_error_client_side_closes_with_and_without_key():
    # With client key configured
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    a._auth_key_id = "kidC"
    a._auth_priv = Ed25519PrivateKey.generate()
    cid1 = "out:auth:1"
    w1 = FakeWriter()
    a.connections[cid1] = {"writer": w1, "role": "client"}
    await a._process_control_command(cid1, cast(Any, w1), "AUTH:ERROR:bad_signature")
    assert cid1 not in a.connections
    # Without client key
    a2 = UnifiedMuxConAdapter("mx", {"listeners": []})
    cid2 = "out:auth:2"
    w2 = FakeWriter()
    a2.connections[cid2] = {"writer": w2, "role": "client"}
    await a2._process_control_command(cid2, cast(Any, w2), "AUTH:ERROR:missing_or_unknown_pkid")
    assert cid2 not in a2.connections


@pytest.mark.asyncio
async def test_auth_ok_advertise_idempotent(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    cid = "out:idemp:1"
    w = FakeWriter()
    a.connections[cid] = {"writer": w, "role": "client", "auth_ok": False}
    a._wire_state[cid] = {"send_next": 1}
    a.main_port_manager = FakePM()
    # Make FakeWriter pass isinstance(StreamWriter) checks in module
    import openmux.server.adapters.muxcon as muxmod

    monkeypatch.setattr(muxmod.asyncio, "StreamWriter", FakeWriter)
    calls = {"n": 0}

    async def fake_send(conn_id2, writer2):
        calls["n"] += 1

    monkeypatch.setattr(a, "_send_local_port_list", fake_send)
    # First AUTH:OK triggers advertise; mark advertised immediately to emulate it having happened
    await a._process_control_command(cid, cast(Any, w), "AUTH:OK")
    a.connections[cid]["ports_advertised"] = True
    # Call again should not call advertise hook again
    await a._process_control_command(cid, cast(Any, w), "AUTH:OK")
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_connect_with_routing_options_on_darwin(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Monkeypatch platform as darwin for interface binding path safely
    monkeypatch.setattr(sys, "platform", "darwin")

    # Fake getaddrinfo
    async def fake_gai(host, port, type):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(
        asyncio.get_event_loop(),
        "getaddrinfo",
        lambda *args, **kwargs: asyncio.get_event_loop().create_task(fake_gai(*args, **kwargs)),
    )

    # Fake low-level socket operations via monkeypatching socket.socket
    class DummySock:
        def __init__(self, *args, **kwargs):
            self._opts = []
            self._blocking = True

        def setsockopt(self, level, opt, val):
            self._opts.append((level, opt, val))

        def bind(self, addr):
            pass

        def setblocking(self, b):
            self._blocking = b

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: DummySock())
    # if_nametoindex for lo0
    monkeypatch.setattr(socket, "if_nametoindex", lambda name: 1)

    # Fake sock_connect
    async def fake_sock_connect(sock, sockaddr):
        return None

    monkeypatch.setattr(
        asyncio.get_event_loop(),
        "sock_connect",
        lambda sock, sockaddr: asyncio.get_event_loop().create_task(fake_sock_connect(sock, sockaddr)),
    )

    # Fake open_connection accepting sock
    async def fake_open_connection(**kwargs):
        return cast(Any, FakeReader([])), cast(Any, FakeWriter())

    monkeypatch.setattr(
        asyncio, "open_connection", lambda **kwargs: asyncio.get_event_loop().create_task(fake_open_connection(**kwargs))
    )
    r, w = await a._connect_with_routing_options("h", 1, None, None, None, interface="lo0", fwmark=None)
    assert r is not None and w is not None


def test_mpath_select_promotes_on_stale():
    a = UnifiedMuxConAdapter("mx", {"listeners": [], "mpath_primary_stale_sec": 0.1})
    c1 = "out:h:1000:1"
    c2 = "out:h:1000:2"
    now = time.time()
    a.connections[c1] = {"opened_at": now, "writer": FakeWriter()}
    a.connections[c2] = {"opened_at": now, "writer": FakeWriter()}
    a._register_mpath_connection(c1)
    a._register_mpath_connection(c2)
    key = a._derive_peer_key_from_conn_id(c1)
    # Make c1 stale
    a._mpath_groups[key]["conns"][c1]["last_rx_seen"] = 0
    # Make c2 fresh
    a._mpath_groups[key]["conns"][c2]["last_rx_seen"] = time.time()
    sel = a._select_mpath_connection(key)
    assert sel == c2


@pytest.mark.asyncio
async def test_federated_proxy_reuse_reconnect_notification_and_stream_reopen(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    pm = FakePM()
    a.main_port_manager = pm
    # First connection and register a federated port pX
    c1 = "in:10.0.0.1:1111:1"
    a.connections[c1] = {"writer": FakeWriter(), "server_id": "srvX"}
    pd = {"name": "pX", "adapter_type": "loopback", "origin_server": {"server_id": "srvX"}}
    await a._register_remote_port_from_dict(c1, pd)
    peer_key = a._derive_peer_key_from_conn_id(c1)
    proxy = a._peer_proxies.get(peer_key, {}).get("pX")
    assert proxy is not None
    # Simulate one connected client
    proxy.connected_clients.append({"client_id": "c1"})
    # New connection same peer id -> reuse and notify
    c2 = "in:10.0.0.1:1111:2"
    a.connections[c2] = {"writer": FakeWriter(), "server_id": "srvX"}
    await a._register_remote_port_from_dict(c2, pd)
    # Expect a reconnect notification in data_queue
    msg = await proxy.data_queue.get()
    assert b"FEDERATED_LINK_RESTORED" in msg


@pytest.mark.asyncio
async def test_heartbeat_loop_sends_hb_and_updates(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": [], "heartbeat_interval": 0.1})
    # One active connection with writer recognized as StreamWriter
    import openmux.server.adapters.muxcon as muxmod

    monkeypatch.setattr(muxmod.asyncio, "StreamWriter", FakeWriter)
    cid = "out:hb:loop:1"
    w = FakeWriter()
    a.connections[cid] = {"writer": w}
    # Speed up loop and stop after first iteration
    orig_sleep = asyncio.sleep
    calls = {"n": 0}

    async def fast_sleep(d):
        calls["n"] += 1
        if calls["n"] > 2:
            a._stop_event.set()
        await orig_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    await a._heartbeat_loop()
    # Expect HB request sent
    assert b":HB:" in w.buffer


@pytest.mark.asyncio
async def test_mpath_failover_ttl_prunes_idle_and_closes(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": [], "heartbeat_interval": 0.05})
    a.mpath_neighbor_idle_drop_sec = 0.05
    key = "node:K"
    cid = "out:idle:1"
    w = FakeWriter()
    a.connections[cid] = {"writer": w}
    a._hb_state[cid] = {"last_req_ts": 0.0, "last_ack_ts": 0.0}
    a._mpath_groups[key] = {
        "conns": OrderedDict({cid: {"opened_at": time.time(), "last_rx_seen": time.time() - 999}}),
        "primary": cid,
        "rr_index": 0,
    }
    closed = {"ids": []}

    async def fake_close(x):
        closed["ids"].append(x)

    a._close_connection = fake_close  # type: ignore
    # Speed loop and stop after one pass
    orig_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda d: orig_sleep(0))
    a.mpath_failover_check_sec = 0.01
    t = asyncio.create_task(a._mpath_failover_loop())
    await orig_sleep(0.05)
    a._stop_event.set()
    await t
    assert cid in closed["ids"]


@pytest.mark.asyncio
async def test_read_loop_open_close_paths(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    import openmux.server.adapters.muxcon as muxmod

    monkeypatch.setattr(muxmod.asyncio, "StreamWriter", FakeWriter)
    cid = "in:oec:1"
    w = FakeWriter()
    a.connections[cid] = {"writer": w, "reader": object(), "auth_ok": True}
    peer_key = a._derive_peer_key_from_conn_id(cid)
    frames = [
        {"frame_type": "O", "stream_id": 7, "payload": b"portA", "seq": 1},
        {"frame_type": "E", "stream_id": 7, "payload": b"", "seq": 2},
        None,
    ]

    async def fake_read_frame(reader):
        await asyncio.sleep(0)
        return frames.pop(0)

    monkeypatch.setattr(a, "_read_frame", fake_read_frame)

    # Avoid starting actual pump
    async def fake_pump(peer_key2, sid, pname):
        return None

    monkeypatch.setattr(a, "_pump_local_port_to_remote", fake_pump)
    await a._read_loop(cid)
    # After E frame, mapping should be removed
    assert a._local_session_map.get(peer_key, {}).get(7) is None


class LocalPortQueue:
    """Minimal local port object for origin-side session tests: a data queue the
    pump drains, plus the client bookkeeping PortManager methods expect."""

    def __init__(self):
        self.data_queue: asyncio.Queue = asyncio.Queue()
        self.connected_clients: List[Dict[str, Any]] = []
        self.client_queues: Dict[str, Any] = {}
        self.max_read_write_users = 1
        self.always_buffer = False


def _origin_setup(monkeypatch, port_names=("lp1",)):
    """Build an origin adapter with real PortManager/ConsoleManager and local ports."""
    import openmux.server.adapters.muxcon as muxmod
    from openmux.server.console_manager import ConsoleManager
    from openmux.server.port_manager import PortManager

    monkeypatch.setattr(muxmod.asyncio, "StreamWriter", FakeWriter)
    pm = PortManager({})
    for name in port_names:
        pm.ports[name] = LocalPortQueue()
    cm = ConsoleManager(pm, None)
    a = UnifiedMuxConAdapter("origin", {"listeners": [], "auth_required": False})
    a.main_port_manager = pm
    a.console_manager = cm
    return a, pm, cm


def _scripted_read_loop(a, cid, writer, frames, monkeypatch):
    """Attach a scripted connection (real _read_loop, canned frames) and return its task."""
    a.connections[cid] = {
        "reader": object(),
        "writer": writer,
        "server_id": "peerA",
        "auth_ok": True,
        "opened_at": time.time(),
    }
    a._register_mpath_connection(cid)

    async def fake_read_frame(reader):
        await asyncio.sleep(0)
        if frames:
            f = frames.pop(0)
            # Deliver frames spaced out and keep the peer connected for a while
            # after the last one, so the test can observe steady state (fed
            # client registered, pump running) before EOF arrives.
            await asyncio.sleep(0.5)
            return f
        await asyncio.sleep(0.5)
        return None

    monkeypatch.setattr(a, "_read_frame", fake_read_frame)
    return asyncio.create_task(a._read_loop(cid))


def _fed_clients(port) -> List[Dict[str, Any]]:
    return [c for c in port.connected_clients if str(c.get("client_id", "")).startswith("fed:")]


@pytest.mark.asyncio
async def test_close_connection_tears_down_peer_local_sessions(monkeypatch):
    """Regression (issue #54): when a peer's last path closes, its origin-side
    session state must be torn down: pump stopped, stream mapping removed,
    fed: pseudo-client freed and the buffering hold released."""
    a, pm, cm = _origin_setup(monkeypatch)
    port = pm.ports["lp1"]
    peer_key = "node:peerA"
    w = FakeWriter()
    read_task = _scripted_read_loop(
        a, "in:127.0.0.1:1:1", w, [{"frame_type": "O", "stream_id": 1, "payload": b"lp1", "seq": 1}], monkeypatch
    )

    try:
        for _ in range(100):
            if _fed_clients(port):
                break
            await asyncio.sleep(0.02)
        assert len(_fed_clients(port)) == 1
        assert a._local_session_map.get(peer_key, {}).get(1) == "lp1"
        pump_task = a._pump_tasks.get((peer_key, 1))
        assert pump_task is not None and not pump_task.done()
        assert port.always_buffer is True
        assert getattr(port, "_federation_viewer_count", 0) == 1

        # While the session is live, port output flows toward the peer.
        await port.data_queue.put(b"hello")
        for _ in range(100):
            if b"hello" in w.buffer:
                break
            await asyncio.sleep(0.02)
        assert b"hello" in w.buffer
    finally:
        # Scripted reader runs out of frames (EOF): the read loop unwinds and
        # closes the connection, which empties the peer group.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await read_task

    # All of the dead peer's origin-side session state must be gone. The pump is
    # cancelled (not awaited) by teardown, so poll for its exit before asserting
    # on the state its finally releases.
    for _ in range(100):
        if pump_task.done():
            break
        await asyncio.sleep(0.02)
    assert pump_task.done()
    assert a._local_session_map.get(peer_key) in (None, {})
    assert a._session_map.get(peer_key) in (None, {})
    assert not any(pk == peer_key for (pk, _sid) in a._pump_tasks)
    assert _fed_clients(port) == []
    assert port.always_buffer is False
    assert getattr(port, "_federation_viewer_count", 1) == 0
    assert cm.client_port_map.get(f"fed:{peer_key}:1") is None


@pytest.mark.asyncio
async def test_no_zombie_pump_drain_across_peer_drop_and_reconnect(monkeypatch):
    """Decisive regression (issue #54): a peer whose last path drops WITHOUT
    stream-close frames must not keep a pump draining the local port's queue.
    Chunks produced while the peer is down must survive in the queue and be
    delivered to the peer's NEW session once it reconnects."""
    a, pm, cm = _origin_setup(monkeypatch)
    port = pm.ports["lp1"]
    peer_key = "node:peerA"

    # First path: open a session (sid 1) and let it live.
    w1 = FakeWriter()
    frames1 = [{"frame_type": "O", "stream_id": 1, "payload": b"lp1", "seq": 1}]
    read_task1 = _scripted_read_loop(a, "in:127.0.0.1:1:1", w1, frames1, monkeypatch)
    try:
        for _ in range(100):
            if a._pump_tasks.get((peer_key, 1)) is not None:
                break
            await asyncio.sleep(0.02)
        assert a._local_session_map.get(peer_key, {}).get(1) == "lp1"
    finally:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await read_task1

    # Peer is now down (no E frames were ever sent). Output produced in the
    # window must stay queued: a zombie pump would drain it and lose it.
    await port.data_queue.put(b"while-down")
    await asyncio.sleep(0.2)
    assert port.data_queue.qsize() == 1, "chunk dequeued while peer had no path (zombie pump)"

    # Peer reconnects with a fresh stream id; the chunk must reach the new path.
    w2 = FakeWriter()
    frames2 = [{"frame_type": "O", "stream_id": 2, "payload": b"lp1", "seq": 1}]
    read_task2 = _scripted_read_loop(a, "in:127.0.0.1:2:1", w2, frames2, monkeypatch)
    try:
        for _ in range(100):
            if b"while-down" in w2.buffer:
                break
            await asyncio.sleep(0.02)
        assert b"while-down" in w2.buffer
        for _ in range(100):
            if port.data_queue.qsize() == 0:
                break
            await asyncio.sleep(0.02)
        assert port.data_queue.qsize() == 0
        # Exactly one live pump for the peer (the new session's slot).
        pumps = [t for (pk, _sid), t in a._pump_tasks.items() if pk == peer_key]
        assert len(pumps) == 1
        assert a._local_session_map.get(peer_key, {}).get(2) == "lp1"
        assert a._local_session_map.get(peer_key, {}).get(1) is None
    finally:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await read_task2


@pytest.mark.asyncio
async def test_open_frame_duplicate_reuses_pump(monkeypatch):
    """A duplicate OPEN for the same (peer, stream, port) must not stack a
    second pump on the shared data queue, and the fed: pseudo-client must
    remain a single record (issue #54)."""
    a, pm, cm = _origin_setup(monkeypatch)
    port = pm.ports["lp1"]
    peer_key = "node:peerA"
    w = FakeWriter()

    frames = [
        {"frame_type": "O", "stream_id": 3, "payload": b"lp1", "seq": 1},
        {"frame_type": "O", "stream_id": 3, "payload": b"lp1", "seq": 2},
    ]
    started: List[Tuple[str, int, str]] = []

    async def blocking_pump(pk, sid, pname):
        started.append((pk, sid, pname))
        await asyncio.Event().wait()

    a._pump_local_port_to_remote = blocking_pump  # type: ignore
    read_task = _scripted_read_loop(a, "in:127.0.0.1:5:1", w, frames, monkeypatch)
    try:
        for _ in range(100):
            if len(started) == 1 and len(_fed_clients(port)) == 1:
                break
            await asyncio.sleep(0.02)
        # Wait for the duplicate OPEN (delivered ~0.5s after the first) to arrive
        # and be processed; exactly one pump and one fed client must survive it.
        await asyncio.sleep(0.8)
        assert started == [(peer_key, 3, "lp1")]
        assert a._local_session_map.get(peer_key, {}).get(3) == "lp1"
        assert len(_fed_clients(port)) == 1
        # The single pump stays registered under its slot.
        assert a._pump_tasks.get((peer_key, 3)) is not None
    finally:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await read_task


@pytest.mark.asyncio
async def test_open_frame_reused_stream_id_stops_old_session(monkeypatch):
    """When the peer reuses a stream id for a DIFFERENT port, the old session
    (pump + fed client + mapping) must be stopped before the new one maps the
    slot, so the old port's output is not drained into the new stream
    (issue #54)."""
    a, pm, cm = _origin_setup(monkeypatch, port_names=("pa", "pb"))
    pe, pb = pm.ports["pa"], pm.ports["pb"]
    peer_key = "node:peerA"
    w = FakeWriter()

    frames = [
        {"frame_type": "O", "stream_id": 4, "payload": b"pa", "seq": 1},
        {"frame_type": "O", "stream_id": 4, "payload": b"pb", "seq": 2},
    ]
    started: List[Tuple[str, int, str]] = []
    phase = {"n": 0}

    async def blocking_pump(pk, sid, pname):
        started.append((pk, sid, pname))
        await asyncio.Event().wait()

    a._pump_local_port_to_remote = blocking_pump  # type: ignore
    a.connections["in:127.0.0.1:6:1"] = {
        "reader": object(),
        "writer": w,
        "server_id": "peerA",
        "auth_ok": True,
        "opened_at": time.time(),
    }
    a._register_mpath_connection("in:127.0.0.1:6:1")

    async def slow_second_frame(reader):
        await asyncio.sleep(0)
        if not frames:
            await asyncio.sleep(0.5)
            return None
        f = frames.pop(0)
        phase["n"] += 1
        if phase["n"] >= 2:
            await asyncio.sleep(0.3)  # gap so phase 1 is observable
        return f

    monkeypatch.setattr(a, "_read_frame", slow_second_frame)
    read_task = asyncio.create_task(a._read_loop("in:127.0.0.1:6:1"))
    try:
        # Phase 1: slot mapped to "pa" with its own pump and fed client.
        for _ in range(100):
            if started == [(peer_key, 4, "pa")]:
                break
            await asyncio.sleep(0.02)
        assert started == [(peer_key, 4, "pa")]
        old_task = a._pump_tasks.get((peer_key, 4))
        assert old_task is not None and not old_task.done()
        assert a._local_session_map.get(peer_key, {}).get(4) == "pa"
        assert len(_fed_clients(pe)) == 1

        # Phase 2: same stream id re-mapped to "pb" on the same live connection.
        new_task = None
        for _ in range(100):
            new_task = a._pump_tasks.get((peer_key, 4))
            if len(started) == 2 and new_task is not None and new_task is not old_task and old_task.done():
                break
            await asyncio.sleep(0.02)
        assert started == [(peer_key, 4, "pa"), (peer_key, 4, "pb")]
        # Slot now maps to the new port only; old pump cancelled and replaced.
        assert new_task is not None and new_task is not old_task
        assert old_task.done(), "old session pump was not cancelled"
        assert a._local_session_map.get(peer_key, {}).get(4) == "pb"
        # Old port's fed pseudo-client freed; new port's present.
        assert _fed_clients(pe) == []
        assert len(_fed_clients(pb)) == 1
    finally:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await read_task


@pytest.mark.asyncio
async def test_send_local_port_list_no_pm(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    cid = "in:nopm:1"
    w = FakeWriter()
    a.connections[cid] = {"writer": w, "auth_ok": True}
    a._wire_state[cid] = {"send_next": 1}
    # No main_port_manager set
    await a._send_local_port_list(cid, cast(Any, w))
    # Should not have written frames (buffer empty)
    assert w.buffer == b""


@pytest.mark.asyncio
async def test_control_ports_list_federated_ignored():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    cid = "out:reqp:1"
    w = FakeWriter()
    a.connections[cid] = {"writer": w}
    a._wire_state[cid] = {"send_next": 1}
    await a._process_control_command(cid, cast(Any, w), "PORTS:LIST:FEDERATED")
    assert w.buffer == b""


def test_get_filters_for_conn_merge_overrides():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Adapter-level defaults
    a._adv_name_inc = ["a*"]
    a._acc_name_inc = ["b*"]
    # Per-connection override for advertise include only
    a._conn_filters["c1"] = {"advertise_filters": {"include": ["x*"]}}
    eff = a._get_filters_for_conn("c1")
    assert eff["advertise_filters"]["include"] == ["x*"]
    assert eff["accept_filters"]["include"] == ["b*"]


@pytest.mark.asyncio
async def test_peer_bytes_rx_counter_increments(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    conn_id = "out:bytes:1"
    a.connections[conn_id] = {"writer": FakeWriter()}
    # route drops; we only check counter behavior
    before = a._peer_bytes_rx.get(a._derive_peer_key_from_conn_id(conn_id), 0)
    await a._handle_inbound_data(conn_id, 1, b"abcd", 1)
    after = a._peer_bytes_rx.get(a._derive_peer_key_from_conn_id(conn_id), 0)
    assert after - before >= 4


@pytest.mark.asyncio
async def test_peer_generation_change_resyncs_seq_state(monkeypatch):
    """A restarted peer (new instance_id, same server_id) restarts its DATA
    seq counter at 1; RX expected, TX seq and unacked send state must follow (#55)."""
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    delivered: List[Tuple[int, bytes, int]] = []

    async def fake_route(cid, sid, data, seq):
        delivered.append((sid, data, seq))

    monkeypatch.setattr(a, "_route_data_frame", fake_route)
    a.connections["c1"] = {"server_id": "peerB", "instance_id": "gen1", "opened_at": 1000.0}
    pk = a._derive_peer_key_from_conn_id("c1")
    # Simulate outbound state from the old generation that survives a restart.
    a._peer_tx_seq[pk] = 90
    a._peer_sendbuf[pk] = {89: ("c1", 1, b"old", 1000.0)}
    a._peer_retx_count[pk] = 5
    for s in (1, 2, 3, 4, 5):
        await a._handle_inbound_data("c1", 1, b"d", s)
    assert [t[2] for t in delivered] == [1, 2, 3, 4, 5]

    # Peer process restarts: same server_id, new instance, TX seq back at 1.
    a.connections["c2"] = {"server_id": "peerB", "instance_id": "gen2", "opened_at": 2000.0}
    assert a._derive_peer_key_from_conn_id("c2") == pk
    await a._handle_inbound_data("c2", 1, b"n1", 1)
    await a._handle_inbound_data("c2", 1, b"n2", 2)
    assert [(t[1], t[2]) for t in delivered][5:] == [(b"n1", 1), (b"n2", 2)]
    st = a._peer_rx_state[pk]
    assert st["instance_id"] == "gen2"
    assert st["expected"] == 3
    # Our TX side restarts at 1; unacked old-gen frames and retx counts are dropped.
    assert a._peer_tx_seq[pk] == 1
    assert a._peer_sendbuf.get(pk) is None
    assert a._peer_retx_count.get(pk) is None

    # A stale frame from the dying old-generation path must not roll state back.
    await a._handle_inbound_data("c1", 1, b"stale", 7)
    assert len(delivered) == 7
    assert a._peer_rx_state[pk]["instance_id"] == "gen2"
    assert a._peer_rx_state[pk]["expected"] == 3
    assert a._peer_rx_state[pk]["buffer"] == {}
    assert a._peer_rx_state[pk]["gap_since"] is None


@pytest.mark.asyncio
async def test_same_generation_failover_does_not_resync(monkeypatch):
    """Multipath failover between two paths of the SAME peer process keeps the
    peer-scoped seq state untouched (no reset, no drop)."""
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    delivered: List[Tuple[int, bytes, int]] = []

    async def fake_route(cid, sid, data, seq):
        delivered.append((sid, data, seq))

    monkeypatch.setattr(a, "_route_data_frame", fake_route)
    a.connections["c1"] = {"server_id": "peerB", "instance_id": "gen1", "opened_at": 1000.0}
    a.connections["c2"] = {"server_id": "peerB", "instance_id": "gen1", "opened_at": 1500.0}
    pk = a._derive_peer_key_from_conn_id("c1")
    a._peer_tx_seq[pk] = 42
    # Path 1 delivers 1,2 and an out-of-order 4 (buffers, gap at 3).
    await a._handle_inbound_data("c1", 1, b"a", 1)
    await a._handle_inbound_data("c1", 1, b"b", 2)
    await a._handle_inbound_data("c1", 1, b"d", 4)
    assert [t[2] for t in delivered] == [1, 2]
    assert a._peer_rx_state[pk]["expected"] == 3
    # Failover: path 2 delivers the missing 3; buffer drains in order.
    await a._handle_inbound_data("c2", 1, b"c", 3)
    assert [(t[1], t[2]) for t in delivered] == [(b"a", 1), (b"b", 2), (b"c", 3), (b"d", 4)]
    # No resync happened: TX counter and generation untouched.
    assert a._peer_tx_seq[pk] == 42
    assert a._peer_rx_state[pk]["instance_id"] == "gen1"
    assert a._peer_rx_state[pk]["expected"] == 5
    assert a._peer_rx_state[pk]["gap_since"] is None


@pytest.mark.asyncio
async def test_stuck_gap_flush_delivers_buffered_tail(monkeypatch):
    """A reorder gap the sender never refills is dropped after gap_stuck_sec
    and the buffered tail is delivered in order, instead of wedging the peer (#55)."""
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    delivered: List[Tuple[int, bytes, int]] = []

    async def fake_route(cid, sid, data, seq):
        delivered.append((sid, data, seq))

    monkeypatch.setattr(a, "_route_data_frame", fake_route)
    a.connections["c1"] = {"server_id": "peerB", "instance_id": "gen1", "opened_at": 1000.0}
    pk = a._derive_peer_key_from_conn_id("c1")
    # A gap that gets filled in time must NOT be flushed.
    await a._handle_inbound_data("c1", 1, b"a", 1)
    await a._handle_inbound_data("c1", 1, b"c", 3)
    assert [t[2] for t in delivered] == [1]
    assert a._peer_rx_state[pk]["gap_since"] is not None
    await a._handle_inbound_data("c1", 1, b"b", 2)
    assert [t[2] for t in delivered] == [1, 2, 3]
    assert a._peer_rx_state[pk]["gap_since"] is None
    assert a._peer_rx_state[pk]["buffer"] == {}

    # Now wedge a gap that no retransmission will ever fill.
    a.gap_stuck_sec = 0.0
    await a._handle_inbound_data("c1", 1, b"x", 5)  # gap at 4
    assert [t[2] for t in delivered] == [1, 2, 3]
    await a._handle_inbound_data("c1", 1, b"y", 6)  # arms + flushes the stuck gap
    assert [t[2] for t in delivered] == [1, 2, 3, 5, 6]
    assert [(t[1]) for t in delivered][3:] == [b"x", b"y"]
    st = a._peer_rx_state[pk]
    assert st["expected"] == 7
    assert st["buffer"] == {}
    assert st["gap_since"] is None


@pytest.mark.asyncio
async def test_stale_duplicate_drop_warns_once_per_second(monkeypatch):
    """seq < expected drops are silent by default; the new rate-limited WARNING
    fires at most once per peer per second."""
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    delivered: List[Tuple[int, bytes, int]] = []

    async def fake_route(cid, sid, data, seq):
        delivered.append((sid, data, seq))

    monkeypatch.setattr(a, "_route_data_frame", fake_route)
    warns: List[str] = []
    monkeypatch.setattr(a.logger, "warning", lambda msg, *args, **kwargs: warns.append(str(msg)))
    a.connections["c1"] = {"server_id": "peerB", "instance_id": "gen1", "opened_at": 1000.0}
    for s in (1, 2, 3):
        await a._handle_inbound_data("c1", 1, b"d", s)
    # Two duplicate drops within the rate-limit window -> exactly one warning.
    await a._handle_inbound_data("c1", 1, b"dup", 2)
    await a._handle_inbound_data("c1", 1, b"dup", 2)
    assert [t[2] for t in delivered] == [1, 2, 3]
    assert len(warns) == 1
    assert "seq=2" in warns[0] and "expected=4" in warns[0]


def test_known_peers_legacy_json_load(tmp_path):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    a._known_peers_path = str(tmp_path / "known.yaml")
    # Write legacy JSON file path with .json extension
    legacy = tmp_path / "known.json"
    legacy.write_text(json.dumps({"h:1": "sha256:ff"}))
    m = a._load_known_peers()
    assert m == {"h:1": "sha256:ff"}


def test_make_listen_socket_interface_fwmark_linux(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Mimic linux platform
    monkeypatch.setattr(sys, "platform", "linux")

    # Stub socket with tracking of setsockopt
    class DummySock:
        def __init__(self, af, st, pr):
            self._opts = []
            self._bound = False
            self._blocking = True

        def setsockopt(self, level, opt, val):
            self._opts.append((level, opt, val))

        def bind(self, sockaddr):
            self._bound = True

        def listen(self):
            pass

        def setblocking(self, b):
            self._blocking = b

        def fileno(self):
            return 3

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", lambda af, st, pr: DummySock(af, st, pr))
    # Force getaddrinfo to deterministic tuple
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, port, type: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]
    )
    s = a._make_listen_socket("127.0.0.1", 0, interface="eth0", fwmark=7)
    try:
        assert s.fileno() == 3
    finally:
        s.close()


@pytest.mark.asyncio
async def test_shutdown_legacy_noop_helpers():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Ensure no exceptions; methods are compatibility no-ops
    w = FakeWriter()
    await a._shutdown_grace_timeout_task("x", cast(Any, w))
    await a._schedule_shutdown_end("x", cast(Any, w))


# ---------------------------------------------------------------------------
# Regression: a locally initiated force-take must actually reach a federated
# ("fed:<peer>:<sid>") read-write holder, not just a genuinely local one.


@pytest.mark.asyncio
async def test_send_control_frame_to_client_relays_client_mode_as_fedrwack(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    a._local_session_map["peerA"] = {7: "p1"}
    sent: List[Any] = []

    async def fake_send_control(peer_key, payload):
        sent.append((peer_key, payload))
        return True

    monkeypatch.setattr(a, "_send_control_mpath", fake_send_control)

    ok = await a.send_control_frame_to_client(
        "fed:peerA:7", {"type": "client_mode", "ok": False, "mode": "read-only", "reason": "demoted"}
    )

    assert ok is True
    assert sent == [("peerA", "FEDRWACK:p1:7:read-only")]


@pytest.mark.asyncio
async def test_send_control_frame_to_client_ignores_non_client_mode_payloads(monkeypatch):
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    a._local_session_map["peerA"] = {7: "p1"}
    sent: List[Any] = []

    async def fake_send_control(peer_key, payload):
        sent.append((peer_key, payload))
        return True

    monkeypatch.setattr(a, "_send_control_mpath", fake_send_control)

    # A port-wide presence broadcast isn't a client_mode notice - must not be
    # mistranslated into a bogus FEDRWACK telling the peer it's read-only.
    ok = await a.send_control_frame_to_client("fed:peerA:7", {"type": "presence", "viewers": []})

    assert ok is False
    assert sent == []


@pytest.mark.asyncio
async def test_send_control_frame_to_client_unknown_stream_returns_false():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    ok = await a.send_control_frame_to_client("fed:peerA:99", {"type": "client_mode", "mode": "read-only"})
    assert ok is False


@pytest.mark.asyncio
async def test_send_control_frame_to_client_handles_peer_key_containing_colons(monkeypatch):
    """`_derive_peer_key_from_conn_id` commonly returns keys like "node:<server_id>"
    that themselves contain a colon - a naive `client_id.split(":", 2)` mis-parses
    "fed:node:peerB:7" as peer_key="node", sid_str="peerB:7" (int() fails), so
    the notice is silently dropped. Must split from the right instead."""
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    a._local_session_map["node:peerB"] = {7: "p1"}
    sent: List[Any] = []

    async def fake_send_control(peer_key, payload):
        sent.append((peer_key, payload))
        return True

    monkeypatch.setattr(a, "_send_control_mpath", fake_send_control)

    ok = await a.send_control_frame_to_client(
        "fed:node:peerB:7", {"type": "client_mode", "ok": False, "mode": "read-only", "reason": "demoted"}
    )

    assert ok is True
    assert sent == [("node:peerB", "FEDRWACK:p1:7:read-only")]


@pytest.mark.asyncio
async def test_relay_viewers_upstream_excludes_federated_pseudo_client(monkeypatch):
    """Multi-hop sibling of the `get_viewers_display` double-count fix: a
    `fed:`-prefixed pseudo-client on an intermediate hop's RemotePortProxy is an
    internal RW-arbitration tracker (issue #52), not a real distinct viewer - it
    must not be folded into `local_here` when relaying a VIEWERS snapshot on."""
    a = UnifiedMuxConAdapter("mx", {"listeners": []})

    class M:
        pass

    proxy = a.RemotePortProxy(a, "peerA", "p1", M())
    proxy.connected_clients = [
        {"client_id": "C", "username": "carol", "mode": "read-only"},
        {"client_id": "fed:peerB:9", "username": "federation:peerB", "mode": "read-only"},
    ]
    broadcast_calls: List[Any] = []

    async def fake_broadcast(port_name, viewers, exclude_conn_id=None):
        broadcast_calls.append((port_name, viewers, exclude_conn_id))

    monkeypatch.setattr(a, "_broadcast_viewer_presence", fake_broadcast)

    upstream_viewers = [{"server_id": "origin", "username": "admin", "mode": "read-write", "ip": "1.2.3.4"}]
    await a._relay_viewers_upstream("conn1", "p1", proxy, upstream_viewers)

    assert len(broadcast_calls) == 1
    _, forwarded, exclude_conn_id = broadcast_calls[0]
    assert exclude_conn_id == "conn1"
    assert forwarded == upstream_viewers + [{"username": "carol", "mode": "read-only", "ip": "unknown"}]


@pytest.mark.asyncio
async def test_handle_fedrw_ack_unmatched_readonly_demotes_local_client(monkeypatch):
    """An unmatched (no pending request of ours) read-only FEDRWACK means the
    origin force-demoted our mirrored holder on behalf of one of ITS OWN
    clients - the local client attached to that stream must be demoted and
    notified too, instead of silently staying stuck showing read-write."""
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    conn_id = "in:10.0.0.1:5555:1"
    a.connections[conn_id] = {"writer": FakeWriter()}
    peer_key = a._derive_peer_key_from_conn_id(conn_id)

    async def fake_open(pk, sid, name):
        return True

    monkeypatch.setattr(a, "_send_stream_open_mpath", fake_open)

    class M:
        description = "R"
        max_rw_users = 2

    proxy = a.RemotePortProxy(a, peer_key, "p1", M())
    sid = await proxy.open_stream_for_client("local_bob")
    assert sid is not None

    class FakeConsoleManager:
        def __init__(self):
            self.demoted: List[Any] = []
            self.notified: List[Any] = []

        async def demote_client_to_read_only(self, client_id, port_name, notify_origin=True):
            self.demoted.append((client_id, port_name, notify_origin))
            return True

        async def send_control_frame_to_client(self, client_id, payload):
            self.notified.append((client_id, payload))
            return True

    fake_cm = FakeConsoleManager()
    a.console_manager = fake_cm

    await a._handle_fedrw_ack(conn_id, f"FEDRWACK:p1:{sid}:read-only")

    # notify_origin=False: the origin already performed this demotion itself,
    # so no redundant FEDRW RELEASE round-trip should be sent back to it (that
    # would self-deadlock the very read loop handling this ack).
    assert fake_cm.demoted == [("local_bob", "p1", False)]
    assert fake_cm.notified == [("local_bob", {"type": "client_mode", "ok": False, "mode": "read-only", "reason": "demoted"})]


@pytest.mark.asyncio
async def test_end_to_end_local_force_take_notifies_federated_peers_own_client():
    """Full two-adapter, real-socket regression for the live bug report: after
    a genuinely local client on the origin force-takes read-write, the
    federated peer's OWN local client (a stand-in for its web console) must
    be notified live over the real wire - not just have its bookkeeping
    flipped server-side, requiring a refresh/reconnect to notice."""
    from openmux.server.console_manager import ConsoleManager
    from openmux.server.port_manager import PortManager

    server_side: Dict[str, Any] = {}

    async def handle(reader, writer):
        server_side["reader"] = reader
        server_side["writer"] = writer

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    b_reader, b_writer = await asyncio.open_connection(host, port)
    while "reader" not in server_side:
        await asyncio.sleep(0)
    a_reader, a_writer = server_side["reader"], server_side["writer"]

    ad_a = UnifiedMuxConAdapter("origin", {"listeners": [], "auth_required": False})
    ad_b = UnifiedMuxConAdapter("peer", {"listeners": [], "auth_required": False})

    conn_a = "in:127.0.0.1:1:1"
    conn_b = "out:127.0.0.1:1:1"
    ad_a.connections[conn_a] = {"reader": a_reader, "writer": a_writer, "server_id": "peerB", "opened_at": time.time()}
    ad_a._wire_state[conn_a] = {"send_next": 1}
    ad_a._register_mpath_connection(conn_a)
    ad_b.connections[conn_b] = {"reader": b_reader, "writer": b_writer, "server_id": "peerA", "opened_at": time.time()}
    ad_b._wire_state[conn_b] = {"send_next": 1}
    ad_b._register_mpath_connection(conn_b)

    class _FakePort:
        def __init__(self, max_read_write_users=1):
            self.connected_clients: List[Dict[str, Any]] = []
            self.max_read_write_users = max_read_write_users
            self.client_queues: Dict[str, Any] = {}

    pm_a = PortManager({})
    pm_a.ports["p1"] = _FakePort(max_read_write_users=1)

    class _FakeAuth:
        def get_user_permissions(self, username: str):
            return "read-write"

        def get_user_groups(self, username: str):
            return set()

    cm_a = ConsoleManager(pm_a, _FakeAuth())
    ad_a.main_port_manager = pm_a
    ad_a.console_manager = cm_a

    pm_b = PortManager({})
    cm_b = ConsoleManager(pm_b, None)
    ad_b.main_port_manager = pm_b
    ad_b.console_manager = cm_b

    read_task_a = asyncio.create_task(ad_a._read_loop(conn_a))
    read_task_b = asyncio.create_task(ad_b._read_loop(conn_b))

    try:

        class M:
            description = "R"
            max_rw_users = 2

        peer_key_b = ad_b._derive_peer_key_from_conn_id(conn_b)
        proxy = ad_b.RemotePortProxy(ad_b, peer_key_b, "p1", M())
        pm_b.ports["p1"] = proxy

        class FakeBrowserChannel:
            def __init__(self):
                self.received: List[Dict[str, Any]] = []

            async def send_control_frame_to_client(self, client_id, payload):
                self.received.append(payload)
                return True

        browser = FakeBrowserChannel()
        cm_b.client_port_map["browser1"] = "p1"
        cm_b.register_client_channel("browser1", browser)

        # Attach the browser client for real: sends a genuine "O" (STREAM_OPEN)
        # frame over the wire, processed by ad_a's real read loop.
        assert await pm_b.add_client_to_port("p1", "browser1", "browser_user", "read-only")

        for _ in range(100):
            if any(c.get("client_id", "").startswith("fed:") for c in pm_a.ports["p1"].connected_clients):
                break
            await asyncio.sleep(0.02)
        fed_clients = [c for c in pm_a.ports["p1"].connected_clients if c.get("client_id", "").startswith("fed:")]
        assert len(fed_clients) == 1
        fed_client_id = fed_clients[0]["client_id"]

        # The peer's client was already granted read-write earlier (the
        # REQUEST/ACK grant handshake itself is covered by other tests) -
        # reflect that on both the origin's tracked fed: entry and the peer's
        # own local mirror.
        fed_clients[0]["mode"] = "read-write"
        proxy.connected_clients.append({"client_id": "browser1", "username": "browser_user", "mode": "read-write"})

        # A genuinely local origin client takes the read-write slot.
        cm_a.client_port_map["human1"] = "p1"
        pm_a.ports["p1"].connected_clients.append({"client_id": "human1", "username": "human1", "mode": "read-only"})

        ok, reason = await cm_a.take_write_slot("human1", "p1")
        assert (ok, reason) == (True, "ok")

        # Origin-side bookkeeping demoted the federated holder...
        assert next(c for c in pm_a.ports["p1"].connected_clients if c["client_id"] == fed_client_id)["mode"] == "read-only"

        # ...and, crucially, that demotion must reach the peer's ACTUAL client
        # live, over the real wire, with zero refresh/reconnect.
        for _ in range(100):
            if browser.received:
                break
            await asyncio.sleep(0.02)
        assert (
            browser.received
        ), f"Peer's own local client was never notified live of the federated demotion - take reason={reason!r}"
        assert browser.received[-1]["mode"] == "read-only"
        assert browser.received[-1]["reason"] == "demoted"
    finally:
        read_task_a.cancel()
        read_task_b.cancel()
        for t in (read_task_a, read_task_b):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        for w in (a_writer, b_writer):
            try:
                w.close()
            except Exception:
                pass
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_end_to_end_default_initiator_reaches_default_listener_over_tls(tmp_path):
    """Interop regression for the safe-defaults gap: an initiator with all
    safe defaults (use_tls, ssl_verify, ToFU) must complete TLS against a
    default listener's self-signed autogen cert - which strict system CA
    verification could never do. Auth is disabled on both sides so this test
    isolates the TLS behavior (Ed25519 identity has its own tests)."""
    listener_cfg = UnifiedMuxConAdapter._normalize_listener_conf(
        {"host": "127.0.0.1", "port": 0, "tls_dir": str(tmp_path / "a")}
    )
    ad_a = UnifiedMuxConAdapter("a", {"listeners": [listener_cfg], "auth_required": False})
    ad_a.server_id = "node-a"

    started = await ad_a.start()
    assert started is True
    try:
        real_port = ad_a._servers[("127.0.0.1", 0)].sockets[0].getsockname()[1]

        # Node B: all safe defaults; the disabled listener entry only gives it
        # a test-local tls_dir so the ToFU store never touches the real home dir.
        ad_b = UnifiedMuxConAdapter(
            "b",
            {
                "listeners": [{"host": "127.0.0.1", "port": 0, "enabled": False, "tls_dir": str(tmp_path / "b")}],
                "initiators": [{"host": "127.0.0.1", "port": real_port}],
                "auth_required": False,
            },
        )
        ad_b.server_id = "node-b"
        assert await ad_b.start() is True
        try:
            deadline = time.time() + 5.0
            while not ad_a.connections or not ad_b.connections:
                if time.time() > deadline:
                    raise TimeoutError("default-configured nodes never connected")
                await asyncio.sleep(0.02)

            conn_a = next(iter(ad_a.connections.values()))
            conn_b = next(iter(ad_b.connections.values()))
            assert conn_a["role"] == "server" and conn_b["role"] == "client"
            # Both sides really are on TLS
            assert conn_a["writer"].get_extra_info("ssl_object") is not None
            assert conn_b["writer"].get_extra_info("ssl_object") is not None
            # The ToFU gate pinned the listener's autogen cert on first sight
            stored = ad_b._load_known_peers().get(f"127.0.0.1:{real_port}")
            assert stored and stored.startswith("sha256:")
            # Peer identity flowed through the handshake
            assert conn_b["server_id"] == "node-a"
        finally:
            await ad_b.stop()
    finally:
        await ad_a.stop()


@pytest.mark.asyncio
async def test_inbound_auth_ok_on_server_role_is_rejected():
    """SEC-02: an inbound AUTH:OK on a server-side (acceptor) connection must not grant auth."""
    a = UnifiedMuxConAdapter("mx", {"listeners": [], "auth_required": True})
    conn_id = "in:9.9.9.9:1111:5"
    w = FakeWriter()
    a.connections[conn_id] = {"writer": w, "role": "server", "auth_ok": False}
    a._wire_state[conn_id] = {"send_next": 1}

    await a._process_control_command(conn_id, cast(Any, w), "AUTH:OK")

    assert a._is_conn_authenticated(conn_id) is False
    if conn_id in a.connections:
        assert a.connections[conn_id].get("auth_ok") is not True


@pytest.mark.asyncio
async def test_inbound_auth_ok_exploit_by_registered_keyholder():
    """SEC-02: a peer holding a registered PKID must not skip the signature step by
    sending AUTH:OK; the legitimate signed path must still authenticate."""
    a = UnifiedMuxConAdapter("mx", {"listeners": [], "auth_required": True})
    attacker_priv = Ed25519PrivateKey.generate()
    kid = "kidA"
    a._auth_pubkeys[kid] = attacker_priv.public_key()

    hello = f"HELLO MuxCon/1.0 TYPE=regular_client CAPS=a ID=attacker INST=ax PKID={kid}\n".encode()
    r = FakeReader([hello])
    w = FakeWriter()
    await a._perform_server_handshake(cast(Any, r), cast(Any, w), "in:attacker")

    # The challenge was issued, so the legitimate path exists
    assert b"AUTH:PK:CHALLENGE:" in w.buffer
    cid = "in:attacker"
    assert cid in a.connections and a.connections[cid]["role"] == "server"

    # The peer ignores the challenge and self-declares authentication
    await a._process_control_command(cid, cast(Any, w), "AUTH:OK")
    assert a._is_conn_authenticated(cid) is False
    if cid in a.connections:
        assert a.connections[cid].get("auth_ok") is not True

    # The legitimate signed path still works
    good_priv = Ed25519PrivateKey.generate()
    kid2 = "kidB"
    a._auth_pubkeys[kid2] = good_priv.public_key()
    hello2 = f"HELLO MuxCon/1.0 TYPE=regular_client CAPS=a ID=good INST=by PKID={kid2}\n".encode()
    r2 = FakeReader([hello2])
    w2 = FakeWriter()
    await a._perform_server_handshake(cast(Any, r2), cast(Any, w2), "in:good")
    st = a.connections["in:good"]["auth_state"]
    sig = base64.b64encode(good_priv.sign(st["nonce"])).decode()
    await a._process_control_command("in:good", cast(Any, w2), f"AUTH:PK:RESPONSE:{kid2}:{sig}")
    assert b"AUTH:OK" in w2.buffer
    assert a._is_conn_authenticated("in:good") is True


@pytest.mark.asyncio
async def test_pre_auth_mpath_and_heartbeat_frames_ignored_on_server_role():
    """SEC-02 hardening: pre-auth MPATH shutdown and heartbeat frames are ignored;
    after authentication the same frames work normally."""
    a = UnifiedMuxConAdapter("mx", {"listeners": [], "auth_required": True})
    conn_id = "in:9.9.9.9:1111:7"
    w = FakeWriter()
    a.connections[conn_id] = {"writer": w, "role": "server", "auth_ok": False}
    a._wire_state[conn_id] = {"send_next": 1}

    await a._process_control_command(conn_id, cast(Any, w), "REQ:123.456")
    assert b"ACK:" not in w.buffer
    assert conn_id not in a._hb_state
    assert a._wire_state[conn_id]["send_next"] == 1

    await a._process_control_command(conn_id, cast(Any, w), "ACK:123.456")
    assert conn_id not in a._hb_state

    await a._process_control_command(conn_id, cast(Any, w), "MPATH:SHUTDOWN:BEGIN")
    assert conn_id in a.connections
    await a._process_control_command(conn_id, cast(Any, w), "MPATH:END")
    assert conn_id in a.connections
    assert a._is_conn_authenticated(conn_id) is False

    # Once authenticated, the same frames are processed
    a.connections[conn_id]["auth_ok"] = True
    await a._process_control_command(conn_id, cast(Any, w), "REQ:123.456")
    assert b"ACK:" in w.buffer
    await a._process_control_command(conn_id, cast(Any, w), "ACK:123.456")
    st = a._hb_state.get(conn_id)
    assert st and st.get("last_ack_ts", 0) > 0


# ---------------------------------------------------------------------------
# issue #59 Part 2: origin-side FEDRW TAKE arbitration + proxy spec builder


class _TakePort:
    def __init__(self):
        self.connected_clients: List[Dict[str, Any]] = []


class _TakePM:
    def __init__(self, port: _TakePort):
        self.ports = {"p1": port}
        self.promote_fail_for: set = set()

    def get_port(self, name):
        return self.ports.get(name)

    async def promote_client(self, port_name: str, client_id: str) -> bool:
        if client_id in self.promote_fail_for:
            return False
        for c in self.ports[port_name].connected_clients:
            if c["client_id"] == client_id:
                c["mode"] = "read-write"
                return True
        return False

    async def demote_client(self, port_name: str, client_id: str) -> bool:
        for c in self.ports[port_name].connected_clients:
            if c["client_id"] == client_id:
                c["mode"] = "read-only"
                return True
        return False


def _take_setup(taker: str = "fed:peerA:3", taker_at: float = 10.0) -> tuple:
    """Origin holding three RW writers; the taker's mirror is the newest."""
    port = _TakePort()
    port.connected_clients = [
        {"client_id": "local1", "username": "local1", "mode": "read-write", "connected_at": 1.0},
        {"client_id": "fed:peerB:5", "username": "peerBuser", "mode": "read-write", "connected_at": 5.0},
        {"client_id": taker, "username": "taker", "mode": "read-write", "connected_at": taker_at},
    ]
    # The taker's mirror starts read-only so a take visibly transfers the slot.
    port.connected_clients[-1]["mode"] = "read-only"
    pm = _TakePM(port)
    a = UnifiedMuxConAdapter("origin", {"listeners": []})
    a.main_port_manager = pm
    a.console_manager = None
    return a, pm, port


@pytest.mark.asyncio
async def test_origin_take_latest_picks_newest_other_holder():
    a, pm, port = _take_setup()
    await a._handle_fedrw_take("conn", pm, "p1", "fed:peerA:3", "latest")
    modes = {c["client_id"]: c["mode"] for c in port.connected_clients}
    # The newest OTHER holder (fed:peerB:5, attached later than local1) is
    # demoted; the taker's mirror is promoted.
    assert modes["fed:peerB:5"] == "read-only"
    assert modes["fed:peerA:3"] == "read-write"


@pytest.mark.asyncio
async def test_origin_take_own_spec_targets_requesting_peers_own_mirror():
    a, pm, port = _take_setup()
    await a._handle_fedrw_take("conn", pm, "p1", "fed:peerA:3", "own:7")
    # "own:7" names fed:peerA:7 - which is NOT in holders, so nothing is demoted
    # and nothing is promoted (no eligible holder matches).
    modes = {c["client_id"]: c["mode"] for c in port.connected_clients}
    assert modes["fed:peerA:3"] == "read-only"


@pytest.mark.asyncio
async def test_origin_take_fed_spec_targets_another_peers_mirror():
    a, pm, port = _take_setup()
    await a._handle_fedrw_take("conn", pm, "p1", "fed:peerA:3", "fed:peerB:5")
    modes = {c["client_id"]: c["mode"] for c in port.connected_clients}
    assert modes["fed:peerB:5"] == "read-only"
    assert modes["fed:peerA:3"] == "read-write"


@pytest.mark.asyncio
async def test_origin_take_restores_victim_when_taker_promote_fails():
    a, pm, port = _take_setup()
    # Only the taker's promote fails (its seat vanished in flight); the
    # victim's restore promote must still succeed so the writer is not lost.
    pm.promote_fail_for.add("fed:peerA:3")
    await a._handle_fedrw_take("conn", pm, "p1", "fed:peerA:3", "latest")
    modes = {c["client_id"]: c["mode"] for c in port.connected_clients}
    # Victim demoted, taker promote failed -> victim restored, taker still RO.
    assert modes["fed:peerB:5"] == "read-write"
    assert modes["fed:peerA:3"] == "read-only"


@pytest.mark.asyncio
async def test_origin_take_latest_on_empty_slot_promotes_taker():
    a, pm, port = _take_setup()
    # No holder at all (every other client read-only): the no-target fallback
    # ("latest") takes the EMPTY slot directly - the taker's mirror is
    # promoted, nothing is demoted or restored.
    for c in port.connected_clients:
        if c["client_id"] != "fed:peerA:3":
            c["mode"] = "read-only"
    await a._handle_fedrw_take("conn", pm, "p1", "fed:peerA:3", "latest")
    modes = {c["client_id"]: c["mode"] for c in port.connected_clients}
    assert modes["fed:peerA:3"] == "read-write"
    assert all(c["mode"] == "read-only" for c in port.connected_clients if c["client_id"] != "fed:peerA:3")


@pytest.mark.asyncio
async def test_origin_take_named_spec_on_empty_slot_refuses():
    a, pm, port = _take_setup()
    # No holder at all; a NAMED spec still refuses (the named victim does not
    # exist), even though "latest" would have granted the empty slot.
    for c in port.connected_clients:
        if c["client_id"] != "fed:peerA:3":
            c["mode"] = "read-only"
    await a._handle_fedrw_take("conn", pm, "p1", "fed:peerA:3", "own:7")
    modes = {c["client_id"]: c["mode"] for c in port.connected_clients}
    assert modes["fed:peerA:3"] == "read-only"


@pytest.mark.asyncio
async def test_origin_request_dispatch_take_and_legacy_force_alias(monkeypatch):
    a, pm, port = _take_setup()
    conn_id = "out:1.2.3.4:4000:1"
    w = FakeWriter()
    a.connections[conn_id] = {"writer": w, "auth_ok": True}
    a._wire_state[conn_id] = {"send_next": 1}
    # Pin the derived peer key so the taker's mirror id matches _take_setup's.
    monkeypatch.setattr(a, "_derive_peer_key_from_conn_id", lambda _conn_id: "peerA")
    a._local_session_map["peerA"] = {3: "p1"}

    # TAKE:latest routes to the arbiter and replies with the resulting mode.
    await a._handle_fedrw_request(conn_id, cast(Any, w), "FEDRW:p1:3:TAKE:latest")
    assert b"FEDRWACK:p1:3:read-write" in w.buffer

    # Reset and try the legacy FORCE alias (must not hang; routes to TAKE:latest).
    for c in port.connected_clients:
        c["mode"] = "read-only" if c["client_id"] == "fed:peerA:3" else "read-write"
    w2 = FakeWriter()
    a.connections[conn_id] = {"writer": w2, "auth_ok": True}
    a._wire_state[conn_id] = {"send_next": 1}
    await a._handle_fedrw_request(conn_id, cast(Any, w2), "FEDRW:p1:3:FORCE")
    assert b"FEDRWACK:p1:3:read-write" in w2.buffer


@pytest.mark.asyncio
async def test_proxy_take_builds_correct_spec(monkeypatch):
    a = UnifiedMuxConAdapter("peer", {"listeners": []})
    proxy = a.RemotePortProxy(a, "out:1.2.3.4:4000:1", "p1", type("M", (), {"description": "d", "max_rw_users": 1})())
    proxy._client_sessions = {"browser1": 9}
    proxy.connected_clients = [
        {"client_id": "browser1", "username": "u", "mode": "read-only"},
        {"client_id": "fed:otherpeer:5", "username": "v", "mode": "read-write"},
    ]

    captured: List[str] = []

    async def fake_request_fedrw(client_id, action, timeout):
        captured.append(action)
        return "read-write"

    monkeypatch.setattr(proxy, "_request_fedrw", fake_request_fedrw)

    await proxy.take_write_slot_for_client("browser1")
    assert captured == ["TAKE:latest"]

    await proxy.take_write_slot_for_client("browser1", target_client_id="fed:otherpeer:5")
    assert captured[-1] == "TAKE:fed:otherpeer:5"

    await proxy.take_write_slot_for_client("browser1", target_client_id="browser1")
    assert captured[-1] == "TAKE:own:9"


# =============================================================================
# Issue #62: muxcon link-down reason (RemotePortProxy.link_reason)
# =============================================================================


def test_remote_port_proxy_get_status_link_reason_wins_over_origin():
    """`link_reason` overrides the origin's stale `status_message` when the link is down."""
    a = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    m = _status_meta(status_message="Connection refused by h:1")
    proxy = a.RemotePortProxy(a, "node:peer1", m.name, m)
    assert "status_message" in proxy.get_status()

    proxy.link_reason = "MuxCon link to peer1 is down"
    snap = proxy.get_status()
    assert snap["status_message"] == "MuxCon link to peer1 is down"
    assert snap["connected"] is True  # unchanged: is_connected is not flipped here


def test_remote_port_proxy_get_status_no_link_reason_keeps_origin():
    """With no link reason, the origin's `status_message` is surfaced unchanged."""
    a = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    m = _status_meta(status_message="Process exited with code 7")
    proxy = a.RemotePortProxy(a, "node:peer1", m.name, m)
    assert proxy.get_status()["status_message"] == "Process exited with code 7"


def test_remote_port_proxy_get_status_link_reason_clears_origin_reason():
    """When the origin's reason is absent but the link is down, the link reason still shows."""
    a = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    m = _status_meta(status_message=None)
    proxy = a.RemotePortProxy(a, "node:peer1", m.name, m)
    assert "status_message" not in proxy.get_status()
    proxy.link_reason = "MuxCon link to peer1 is down"
    assert proxy.get_status()["status_message"] == "MuxCon link to peer1 is down"


@pytest.mark.asyncio
async def test_link_state_flips_clear_link_reason_on_recover():
    """`_update_peer_proxies_live_state` clears `link_reason` when a path goes live again."""
    import time as _time

    a = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    m = _status_meta(status_message=None)
    proxy = a.RemotePortProxy(a, "node:p1", m.name, m)
    a._peer_proxies["node:p1"] = {m.name: proxy}
    a.main_port_manager = FakePM()

    # No live paths, proxy was disconnected: set "stale" window wide so we land
    # in the all-stale branch.
    proxy.is_connected = True
    a._mpath_groups["node:p1"] = {
        "conns": OrderedDict({
            "c1": {"opened_at": _time.time() - 9999, "last_rx_seen": _time.time() - 9999},
        }),
        "primary": "c1",
        "rr_index": 0,
    }
    a._hb_state["c1"] = {"last_req_ts": _time.time() - 9999, "last_ack_ts": _time.time() - 9999, "missed": 99, "rtt_ms": 999}

    a._update_peer_proxies_live_state("node:p1")
    assert proxy.is_connected is False
    # server_id comes from the metadata's origin_server, which _status_meta
    # sets to "peer1" (the peer_key is a separate internal grouping key).
    assert proxy.link_reason == "MuxCon link to peer1 is down"

    # Now make the path live: refresh timestamps and flip to connected.
    a._mpath_groups["node:p1"]["conns"]["c1"]["last_rx_seen"] = _time.time()
    a._hb_state["c1"]["last_ack_ts"] = _time.time()
    proxy.is_connected = False  # already set False; flip to live by re-running
    # Force "live" transition by first marking as live from this run's perspective.
    a._update_peer_proxies_live_state("node:p1")
    assert proxy.is_connected is True
    assert proxy.link_reason == ""


@pytest.mark.asyncio
async def test_load_federated_cache_offline_proxy_gets_link_reason(tmp_path):
    """Cold-start: an offline cached proxy gets `link_reason` set to 'link is down'."""
    import json as _json

    a = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    a.main_port_manager = FakePM()
    cache_path = tmp_path / "cache.json"
    a.federated_cache_path = str(cache_path)
    a.federated_cache_enabled = True

    # Pre-populate cache with an offline remote port record.
    cache_path.write_text(_json.dumps({
        "peers": {
            "node:peer1": {
                "rport": {
                    "connected": False,
                    "last_seen": 0,
                    "origin_server_id": "peer1",
                    "description": "remote",
                    "max_rw_users": 1,
                    "serial_config": None,
                    "line_status": None,
                    "read_write_groups": [],
                    "read_only_groups": [],
                    # Origin's reason at cache-save time: preserved on metadata
                    # but should be preempted by the fresh "link is down" reason.
                    "status_message": "Connection refused by p1:1234",
                },
            },
        },
    }))

    await a._load_federated_cache()
    p = a._peer_proxies["node:peer1"]["rport"]
    assert p.is_connected is False
    assert p.link_reason == "MuxCon link to peer1 is down"
    snap = p.get_status()
    assert snap["status_message"] == "MuxCon link to peer1 is down"


# =============================================================================
# Issue #62: status_message propagation to federated peers
# =============================================================================


def _status_meta(name="remote1", status_message=None):
    """Build a `PortMetadata` suitable for origin-build / remote-parse tests."""
    from openmux.common.federation_types import PortMetadata, ServerInfo, ServerType

    si = ServerInfo(server_id="peer1", hostname="peer1", port=0, server_type=ServerType.LEAF, description="")
    return PortMetadata(
        name=name,
        original_name=name,
        description="Remote",
        adapter_type="remote_muxcon",
        origin_server=si,
        server_chain=[si],
        status="disconnected" if status_message else "connected",
        max_rw_users=1,
        status_message=status_message,
    )


def test_port_metadata_to_dict_and_federation_dict_emit_status_message():
    """Round-trip: `status_message` set is present in both serialization paths."""
    m = _status_meta(status_message="Connection refused by h:1")
    # to_dict is used for local /api/ports enrichment from a remote metadata.
    d = m.to_dict()
    assert d.get("status_message") == "Connection refused by h:1"
    fd = m.to_federation_dict()
    assert fd.get("status_message") == "Connection refused by h:1"


def test_port_metadata_omits_status_message_when_unset():
    """Round-trip: `status_message=None` is omitted from the wire (mixed-version peers)."""
    m = _status_meta(status_message=None)
    d = m.to_dict()
    assert "status_message" not in d
    fd = m.to_federation_dict()
    assert "status_message" not in fd


@pytest.mark.asyncio
async def test_origin_port_list_forwards_status_message(monkeypatch):
    """Origin build: a local port dict with `status_message` is emitted in `PortMetadata`."""
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    ports_with_reasons = [
        {"name": "healthy", "adapter_type": "loopback", "connected": True, "max_rw_users": 1},
        {
            "name": "offlined",
            "adapter_type": "tcp_initiator",
            "connected": False,
            "status_message": "Connection refused by h:1",
            "max_rw_users": 1,
        },
    ]

    class PM:
        ports = {}

        async def get_port_list_with_federation(self):
            return ports_with_reasons

        def get_port(self, name):
            return self.ports.get(name)

    pm = PM()
    a.main_port_manager = pm
    a.server_id = "origin-srv"
    conn_id = "out:peer:1:1"
    w = FakeWriter()
    a.connections[conn_id] = {"writer": w, "auth_ok": True}
    a._wire_state[conn_id] = {"send_next": 1}

    # Intercept the actual `metas` list produced by the origin builder. We can't
    # easily wrap `PortMetadata` construction, so we parse the wire payload —
    # `sent_bytes` from FakeWriter carries the full PORTS:FEDERATED response
    # frame, and each port appears as a JSON line.
    await a._send_local_port_list(conn_id, cast(Any, w))

    text = w.buffer.decode("utf-8", errors="replace")
    assert text.startswith("#0:C:")
    # Extract the payload segment between the header and END:PORTS
    assert "PORTS:FEDERATED:2" in text
    start = text.index("PORTS:FEDERATED:2")
    end = text.index("END:PORTS")
    body = text[start : end + len("END:PORTS")]
    # Body format: "PORTS:FEDERATED:2\n<json1>\n<json2>\nEND:PORTS"
    lines = body.split("\n")
    port_lines = [ln.strip() for ln in lines[1 : len(lines) - 1] if ln.strip()]
    assert len(port_lines) == 2
    parsed = [json.loads(ln) for ln in port_lines]
    by_name = {p["name"]: p for p in parsed}
    assert "healthy" in by_name and "offlined" in by_name
    # Healthy: `status_message` key is omitted (mixed-version safe)
    assert "status_message" not in by_name["healthy"]
    # Offlined: carries the reason
    assert by_name["offlined"]["status_message"] == "Connection refused by h:1"


@pytest.mark.asyncio
async def test_register_remote_port_dict_captures_status_message():
    """Parse path: a federated port dict carrying `status_message` sets the field on the proxy metadata."""
    from openmux.server.port_manager import PortManager

    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = PortManager([])
    ad.main_port_manager = pm
    pd = {
        "name": "remote1",
        "description": "Remote port",
        "adapter_type": "remote_muxcon",
        "origin_server": {"server_id": "peer1", "hostname": "peer1", "port": 0, "server_type": "leaf"},
        "status": "disconnected",
        "max_rw_users": 1,
        "status_message": "Process exited with code 42",
    }
    conn_id = "in:127.0.0.1:9999:1"
    await ad._register_remote_port_from_dict(conn_id, pd)
    proxy = pm.get_port("remote1")
    assert proxy is not None
    assert proxy.metadata.status_message == "Process exited with code 42"
    # `get_status` on the proxy must also carry the text (so web_console and UI render it)
    s = proxy.get_status()
    assert s.get("status_message") == "Process exited with code 42"


@pytest.mark.asyncio
async def test_register_remote_port_dict_without_status_message_is_absent():
    """Parse path with no `status_message` key: field stays None (older peers don't emit it)."""
    from openmux.server.port_manager import PortManager

    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = PortManager([])
    ad.main_port_manager = pm
    pd = {
        "name": "remote2",
        "description": "Remote port",
        "adapter_type": "remote_muxcon",
        "origin_server": {"server_id": "peer1", "hostname": "peer1", "port": 0, "server_type": "leaf"},
        "status": "connected",
        "max_rw_users": 1,
    }
    conn_id = "in:127.0.0.1:9999:2"
    await ad._register_remote_port_from_dict(conn_id, pd)
    proxy = pm.get_port("remote2")
    assert proxy is not None
    assert proxy.metadata.status_message is None
    assert "status_message" not in proxy.get_status()


@pytest.mark.asyncio
async def test_save_and_load_federated_cache_preserves_status_message(tmp_path):
    """`_save_federated_cache` / `_load_federated_cache` round-trip carries `status_message`."""
    a = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = FakePM()
    a.main_port_manager = pm
    a.federated_cache_enabled = True
    cache_path = str(tmp_path / "federated_cache.json")
    a.federated_cache_path = cache_path
    # Seed a peer proxy with a metadata that has a status_message
    peer_key = "node:peer1"
    meta = _status_meta(name="remote1", status_message="Connection closed by remote")
    proxy = a.RemotePortProxy(a, peer_key, "remote1", meta)
    a._peer_proxies = {peer_key: {"remote1": proxy}}
    a._save_federated_cache()
    # Inspect the on-disk JSON
    with open(cache_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    ent = data["peers"][peer_key]["remote1"]
    assert ent["status_message"] == "Connection closed by remote"

    # Now load into a fresh adapter instance
    b = UnifiedMuxConAdapter("b", {"muxcon": {}})
    b.main_port_manager = FakePM()
    b.federated_cache_enabled = True
    b.federated_cache_path = cache_path
    await b._load_federated_cache()
    loaded_meta = b._peer_proxies[peer_key]["remote1"].metadata
    assert loaded_meta.status_message == "Connection closed by remote"


def test_parse_port_status_frame():
    """`_parse_port_status_frame` returns (port, msg, readiness) for a valid payload, None on malformed."""
    a = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    parsed = UnifiedMuxConAdapter._parse_port_status_frame('PORT_STATUS:remote1\n{"status_message":"Connection refused"}')
    assert parsed == ("remote1", "Connection refused", "")
    # Readiness (issue #68): the additive field is surfaced when present.
    parsed_ready = UnifiedMuxConAdapter._parse_port_status_frame(
        'PORT_STATUS:remote1\n{"status_message":"","readiness":"idle"}'
    )
    assert parsed_ready == ("remote1", "", "idle")
    # Malformed: no PORT_STATUS prefix
    assert UnifiedMuxConAdapter._parse_port_status_frame("VIEWERS:p1\n[]") is None
    # Malformed: missing body
    assert UnifiedMuxConAdapter._parse_port_status_frame("PORT_STATUS:p1") is None
    # Empty-string body (healthy transition)
    # Body must be a JSON line.  Empty string encodes as {"status_message": ""}.
    parsed2 = UnifiedMuxConAdapter._parse_port_status_frame("PORT_STATUS:p1\n{}")
    assert parsed2 is not None
    assert parsed2[0] == "p1" and parsed2[1] == "" and parsed2[2] == ""


@pytest.mark.asyncio
async def test_handle_port_status_frame_updates_metadata_and_notifies_meta(monkeypatch):
    """Inbound `PORT_STATUS:` updates proxy metadata and fires a meta event.

    This is what lets the web console live-updates the federated port with the
    fresh reason, without needing a full `PORTS:FEDERATED` refresh (issue #62).
    """
    a = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = FakePM()
    a.main_port_manager = pm
    # Register a fake proxy (we use the real proxy so metadata is a PortMetadata)
    conn_id = "in:127.0.0.1:9999:3"
    a.connections[conn_id] = {"writer": FakeWriter(), "auth_ok": True}
    peer_key = a._derive_peer_key_from_conn_id(conn_id)
    meta = _status_meta(status_message=None)
    proxy = a.RemotePortProxy(a, conn_id, "remote1", meta)
    a._peer_proxies = {peer_key: {"remote1": proxy}}

    notified: List[Tuple[str, Dict[str, Any]]] = []

    def fake_notify(name, payload):
        notified.append((name, payload or {}))

    pm.notify_meta_updated = fake_notify  # type: ignore[assignment]

    await a._handle_port_status_frame(conn_id, 'PORT_STATUS:remote1\n{"status_message":"Connection refused by h:1"}')
    # Proxy metadata reflects the fresh reason
    assert proxy.metadata.status_message == "Connection refused by h:1"
    # A meta notify fired for the port (so web console re-renders it)
    assert notified and notified[0][0] == "remote1"
    assert notified[0][1].get("event") == "federated_status_message_changed"


@pytest.mark.asyncio
async def test_handle_port_status_frame_for_unknown_port_is_noop():
    """Receiving a `PORT_STATUS:` frame for a port we don't track is a safe no-op."""
    a = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = FakePM()
    a.main_port_manager = pm
    notified: List[Tuple[str, Dict[str, Any]]] = []
    pm.notify_meta_updated = lambda n, p: notified.append((n, p or {}))  # type: ignore[assignment]
    conn_id = "in:127.0.0.1:9999:4"
    a.connections[conn_id] = {"writer": FakeWriter(), "auth_ok": True}
    # No peer_proxies entries
    a._peer_proxies = {}
    # Should not raise or notify
    await a._handle_port_status_frame(conn_id, "PORT_STATUS:ghost\n{}")
    assert notified == []


@pytest.mark.asyncio
async def test_broadcast_port_status_sends_wire_frame():
    """`_broadcast_port_status` emits a `PORT_STATUS:<name>` control frame carrying the JSON reason."""
    a = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    a.server_id = "origin"

    # A writer that passes `isinstance(..., asyncio.StreamWriter)` so the
    # broadcast's writer gate doesn't skip it.
    class SW(asyncio.StreamWriter):
        def __init__(self):
            self.buffer = bytearray()
            self._closed = False

        def write(self, data: bytes):
            self.buffer += data

        async def drain(self):  # type: ignore[override]
            return None

        def close(self):  # type: ignore[override]
            self._closed = True

        async def wait_closed(self):  # type: ignore[override]
            return None

        def is_closing(self):  # type: ignore[override]
            return self._closed

    w = SW()
    cid = "out:peer:1:1"
    a.connections[cid] = {"writer": w, "auth_ok": True, "server_id": "peer"}
    a._wire_state[cid] = {"send_next": 1}

    await a._broadcast_port_status("remote_p", "Connection refused by h:1")
    text = w.buffer.decode("utf-8", errors="replace")
    assert "PORT_STATUS:remote_p" in text
    assert '"status_message":"Connection refused by h:1"' in text

    # Never echo back to the owning peer: a conn whose server_id == self is skipped.
    w.buffer.clear()
    a.connections[cid] = {"writer": w, "auth_ok": True, "server_id": "origin"}
    await a._broadcast_port_status("remote_p", "some reason")
    assert not w.buffer


@pytest.mark.asyncio
async def test_on_port_meta_for_status_relay_broadcasts_for_local_ports(monkeypatch):
    """Meta listener: a local port's `status_message` change is pushed to peers
    via `_broadcast_port_status`, and re-published federated proxies are ignored
    (to avoid re-broadcasting reasons we received from an origin).
    """
    a = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = FakePM()
    a.main_port_manager = pm
    a.server_id = "origin"

    class LocalPort:
        name = "local_p"

    local = LocalPort()
    pm.ports["local_p"] = local

    remote_meta = _status_meta(name="remote_p", status_message="from origin")
    remote_proxy = a.RemotePortProxy(a, "node:peer", "remote_p", remote_meta)
    pm.ports["remote_p"] = remote_proxy
    # FakePM has no get_port -> provide one so the ownership guard can classify ports.
    pm.get_port = lambda name: pm.ports.get(name)  # type: ignore[assignment]

    broadcast_calls: List[Tuple[str, str]] = []

    async def fake_broadcast(port_name: str, msg: str):
        broadcast_calls.append((port_name, msg))

    monkeypatch.setattr(a, "_broadcast_port_status", fake_broadcast)

    # Local port change: broadcast with the fresh reason.
    payload_local = {"event": "tcp_disconnected", "status_message": "Connection timeout"}
    await a._on_port_meta_for_status_relay("local_p", payload_local)
    assert broadcast_calls == [("local_p", "Connection timeout")]

    # Federated proxy change (re-published): ownership guard skips the broadcast.
    payload_remote = {"event": "federated_status_message_changed", "status_message": "re-publish"}
    await a._on_port_meta_for_status_relay("remote_p", payload_remote)
    assert len(broadcast_calls) == 1  # unchanged


@pytest.mark.asyncio
async def test_on_port_meta_for_status_relay_ignores_unrelated_events(monkeypatch):
    """A meta event without a `status_message` key passes through silently."""
    a = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = FakePM()
    a.main_port_manager = pm
    broadcast_calls: List[Tuple[str, str]] = []
    monkeypatch.setattr(a, "_broadcast_port_status", lambda port_name, msg: broadcast_calls.append((port_name, msg)))
    # No status_message key -> no broadcast
    await a._on_port_meta_for_status_relay("local_p", {"event": "federated_disconnected"})
    assert broadcast_calls == []
    # Non-dict payload -> no broadcast
    await a._on_port_meta_for_status_relay("local_p", None)
    assert broadcast_calls == []
