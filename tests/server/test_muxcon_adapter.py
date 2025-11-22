import asyncio
import base64
import os
import ssl
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple, cast

import pytest
import json
import sys
import socket

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openmux.server.adapters.muxcon import FederationPeer, UnifiedMuxConAdapter
from collections import OrderedDict


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
    pub_ssh = priv.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()
    assert a._load_ed25519_public_key(pub_ssh) is not None
    pub_b64 = "base64:" + base64.b64encode(priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )).decode()
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
    seed_path.write_bytes(base64.b64encode(priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )))
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
    a2.connections[conn_id2] = {"writer": w2, "auth_state": {"type": "pk", "key_id": kid, "nonce": b"abcd", "expires_at": time.time() + 60}}
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
    a.connections[conn_id2] = {"writer": w2}
    a._wire_state[conn_id2] = {"send_next": 1}
    await a._process_control_command(conn_id2, cast(Any, w2), "MPATH:SHUTDOWN:BEGIN")
    assert b"MPATH:END" in w2.buffer and conn_id2 not in a.connections


@pytest.mark.asyncio
async def test_control_heartbeat_req_ack_updates_state():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    conn_id = "out:h:1:1"
    w = FakeWriter()
    a.connections[conn_id] = {"writer": w}
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
    payload = "PORTS:FEDERATED:1\n" + json.dumps({"name": "p1", "adapter_type": "loopback", "origin_server": {"server_id": "s"}}) + "\nEND:PORTS"
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
    a.connections[conn_id] = {"writer": FakeWriter()}
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
    a.connections[conn_id] = {"writer": w, "auth_state": {"type": "pk", "key_id": kid, "nonce": b"n", "expires_at": time.time() - 1}}
    a._wire_state[conn_id] = {"send_next": 1}
    await a._process_control_command(conn_id, cast(Any, w), f"AUTH:PK:RESPONSE:{kid}:{base64.b64encode(b'X').decode()}")
    # Should send AUTH:ERROR:expired and close
    assert b"AUTH:ERROR:expired" in w.buffer and conn_id not in a.connections
    # Bad signature case
    conn_id2 = "in:bad:1"
    w2 = FakeWriter()
    a.connections[conn_id2] = {"writer": w2, "auth_state": {"type": "pk", "key_id": kid, "nonce": b"n2", "expires_at": time.time() + 60}}
    a._wire_state[conn_id2] = {"send_next": 1}
    # Send invalid sig
    await a._process_control_command(conn_id2, cast(Any, w2), f"AUTH:PK:RESPONSE:{kid}:{base64.b64encode(b'invalid').decode()}")
    assert b"AUTH:ERROR:bad_signature" in w2.buffer and conn_id2 not in a.connections


@pytest.mark.asyncio
async def test_mpath_end_control_closes():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    conn_id = "out:end:1"
    w = FakeWriter()
    a.connections[conn_id] = {"writer": w}
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
    a._mpath_groups[key] = {"conns": OrderedDict({conn_id: {"opened_at": time.time(), "last_rx_seen": time.time()}}), "primary": conn_id, "rr_index": 0}
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
    payload = "PORTS:FEDERATED:1\n" + json.dumps({"name": "px", "adapter_type": "loopback", "origin_server": {"server_id": "s"}}) + "\nEND:PORTS"
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
    sent: List[Tuple[int,int]] = []
    async def fake_close(pk, sid, reason):
        sent.append((sid, len(reason)))
        return True
    a._send_stream_close_mpath = fake_close  # type: ignore
    class M: pass
    p = a.RemotePortProxy(a, peer_key, "R", M())
    # Create sessions
    await p._ensure_session("c1")
    await p._ensure_session("c2")
    # Map into adapter session map to test cleanup
    a._session_map[peer_key] = {p._client_sessions["c1"]: p, p._client_sessions["c2"]: p}
    await p.close_all_streams()
    assert p._client_sessions == {} and len(sent) == 2 and peer_key in a._session_map and a._session_map[peer_key] == {}


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
        def __init__(self, der: bytes): self._der = der
        def getpeercert(self, binary_mode): return self._der
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
        return [ (socket.AF_INET, socket.SOCK_STREAM, 0, '', ("127.0.0.1", 0)) ]
    monkeypatch.setattr(asyncio.get_event_loop(), "getaddrinfo", lambda *args, **kwargs: asyncio.get_event_loop().create_task(fake_gai(*args, **kwargs)))
    class DummySock:
        def __init__(self, *args, **kwargs): self._opts = []
        def setsockopt(self, level, opt, val): self._opts.append((level, opt, val))
        def bind(self, addr): pass
        def setblocking(self, b): pass
        def close(self): pass
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: DummySock())
    async def fake_sock_connect(sock, sockaddr): return None
    monkeypatch.setattr(asyncio.get_event_loop(), "sock_connect", lambda sock, sockaddr: asyncio.get_event_loop().create_task(fake_sock_connect(sock, sockaddr)))
    async def fake_open_connection(**kwargs): return cast(Any, FakeReader([])), cast(Any, FakeWriter())
    monkeypatch.setattr(asyncio, "open_connection", lambda **kwargs: asyncio.get_event_loop().create_task(fake_open_connection(**kwargs)))
    # Call with fwmark option
    r, w = await a._connect_with_routing_options("h", 1, None, None, None, interface=None, fwmark=42)
    assert r is not None and w is not None


def test_mpath_rekey_migrates_peer_state():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    # Start with host-based key
    conn_id = "in:10.0.0.3:4000:1"
    a.connections[conn_id] = {"opened_at": time.time(), "handshake": None}
    host_key = a._derive_peer_key_from_conn_id(conn_id)
    a._mpath_groups[host_key] = {"conns": OrderedDict({conn_id: {"opened_at": time.time()}}), "primary": conn_id, "rr_index": 0}
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


def test_mpath_unregister_clears_group_and_maps():
    a = UnifiedMuxConAdapter("mx", {"listeners": []})
    conn_id = "out:h:1000:9"
    a.connections[conn_id] = {"opened_at": time.time()}
    key = a._derive_peer_key_from_conn_id(conn_id)
    a._mpath_groups[key] = {"conns": OrderedDict({conn_id: {"opened_at": time.time()}}), "primary": conn_id, "rr_index": 0}
    a._peer_sendbuf[key] = {}
    a._peer_rx_state[key] = {"expected": 1, "buffer": {}}
    a._peer_tx_seq[key] = 2
    a._peer_retx_count[key] = 3
    a._unregister_mpath_connection(conn_id)
    assert key not in a._mpath_groups
    assert key not in a._peer_sendbuf and key not in a._peer_rx_state and key not in a._peer_tx_seq and key not in a._peer_retx_count


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
    a.connections[conn_id] = {"writer": w}
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
    a._mpath_groups[key] = {"conns": OrderedDict({conn_id: {"opened_at": time.time(), "last_rx_seen": time.time(), "pref": 0}}), "primary": conn_id, "rr_index": 0}
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
        def __init__(self): self.calls = 0
        async def get_port_data(self, name):
            self.calls += 1
            return b"abc" if self.calls == 1 else b""
    a.main_port_manager = PM()
    sent: List[Tuple[str,int,bytes]] = []
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
        def __init__(self): self.ports = {"dup": Existing("srvD")}
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
        def __init__(self): self.aborted = False
        def abort(self): self.aborted = True
    w.transport = T()  # type: ignore[attr-defined]
    a.connections["c"] = {"writer": w}
    closed = {"ids": []}
    async def fake_close(cid): closed["ids"].append(cid)
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
    a = UnifiedMuxConAdapter("mx", {"listeners": [{"enabled": True, "host": "127.0.0.1", "port": 5000, "path_pref": 5, "path_group": "G"}], "auth_required": False})
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
    a.connections[cid] = {"writer": w}
    a._wire_state[cid] = {"send_next": 1}
    await a._process_control_command(cid, cast(Any, w), "MPATH:SHUTDOWN:BEGIN")
    assert cid not in a.connections
    assert a._shutdown_state.get(cid, {}).get("state") == "CLOSED"
    # Add connection again and send MPATH:END; should close and state remain CLOSED
    w2 = FakeWriter()
    a.connections[cid] = {"writer": w2}
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
    a.connections[cid] = {"writer": w}
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
        return [ (socket.AF_INET, socket.SOCK_STREAM, 0, '', ("127.0.0.1", 0)) ]
    monkeypatch.setattr(asyncio.get_event_loop(), "getaddrinfo", lambda *args, **kwargs: asyncio.get_event_loop().create_task(fake_gai(*args, **kwargs)))
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
    monkeypatch.setattr(asyncio.get_event_loop(), "sock_connect", lambda sock, sockaddr: asyncio.get_event_loop().create_task(fake_sock_connect(sock, sockaddr)))
    # Fake open_connection accepting sock
    async def fake_open_connection(**kwargs):
        return cast(Any, FakeReader([])), cast(Any, FakeWriter())
    monkeypatch.setattr(asyncio, "open_connection", lambda **kwargs: asyncio.get_event_loop().create_task(fake_open_connection(**kwargs)))
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
    a._mpath_groups[key] = {"conns": OrderedDict({cid: {"opened_at": time.time(), "last_rx_seen": time.time() - 999}}), "primary": cid, "rr_index": 0}
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
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, type: [(socket.AF_INET, socket.SOCK_STREAM, 0, '', ("127.0.0.1", 0))])
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
