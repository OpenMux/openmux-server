import asyncio
import base64
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from openmux.server.adapters.muxcon import UnifiedMuxConAdapter
from openmux.server.muxcon_protocol import MuxConProtocolHandler


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


@pytest.mark.asyncio
async def test_process_control_auth_challenge_and_response_ok(monkeypatch):
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    # Prepare client-side key for responding to challenge
    priv = Ed25519PrivateKey.generate()
    ad._auth_priv = priv
    ad._auth_key_id = "kid1"
    # Prepare connection state and a real writer/reader
    conn_id_client = "out:peer:1:1"
    ad.connections[conn_id_client] = {"auth_ok": False}
    ad._wire_state[conn_id_client] = {"send_next": 1}
    server1, s_reader1, s_writer1, c_reader1, c_writer1 = await _make_stream_pair()

    # Simulate AUTH:PK:CHALLENGE arriving to client
    nonce = b"N" * 32
    nonce_b64 = base64.b64encode(nonce).decode()
    await ad._process_control_command(conn_id_client, s_writer1, f"AUTH:PK:CHALLENGE:kid1:{nonce_b64}")
    # Client should respond with AUTH:PK:RESPONSE containing a signature; read it
    line = await asyncio.wait_for(c_reader1.readline(), timeout=1)
    assert b"AUTH:PK:RESPONSE:kid1:" in line

    # Now server-side: verify RESPONSE and emit AUTH:OK
    ad._auth_pubkeys = {"kid1": priv.public_key()}
    conn_id_srv = "in:127.0.0.1:1234:1"
    ad.connections[conn_id_srv] = {
        "auth_ok": False,
        "auth_state": {"type": "pk", "key_id": "kid1", "nonce": nonce, "expires_at": time.time() + 30},
    }
    ad._wire_state[conn_id_srv] = {"send_next": 1}
    server2, s_reader2, s_writer2, c_reader2, c_writer2 = await _make_stream_pair()
    sig = priv.sign(nonce)
    sig_b64 = base64.b64encode(sig).decode()
    await ad._process_control_command(conn_id_srv, s_writer2, f"AUTH:PK:RESPONSE:kid1:{sig_b64}")
    # Server should accept and send AUTH:OK
    line2 = await asyncio.wait_for(c_reader2.readline(), timeout=1)
    assert b"AUTH:OK" in line2
    assert ad.connections[conn_id_srv]["auth_ok"] is True

    # Expired challenge should yield AUTH:ERROR:expired and close
    closed = {"flag": False}

    async def fake_close(cid):
        closed["flag"] = True

    monkeypatch.setattr(ad, "_close_connection", fake_close)
    ad.connections[conn_id_srv] = {
        "auth_ok": False,
        "auth_state": {"type": "pk", "key_id": "kid1", "nonce": nonce, "expires_at": time.time() - 1},
    }
    server3, s_reader3, s_writer3, c_reader3, c_writer3 = await _make_stream_pair()
    await ad._process_control_command(conn_id_srv, s_writer3, f"AUTH:PK:RESPONSE:kid1:{sig_b64}")
    line3 = await asyncio.wait_for(c_reader3.readline(), timeout=1)
    assert b"AUTH:ERROR:expired" in line3
    assert closed["flag"] is True
    # Cleanup stream pairs
    s_writer1.close()
    c_writer1.close()
    await s_writer1.wait_closed()
    await c_writer1.wait_closed()
    server1.close()
    await server1.wait_closed()
    s_writer2.close()
    c_writer2.close()
    await s_writer2.wait_closed()
    await c_writer2.wait_closed()
    server2.close()
    await server2.wait_closed()
    s_writer3.close()
    c_writer3.close()
    await s_writer3.wait_closed()
    await c_writer3.wait_closed()
    server3.close()
    await server3.wait_closed()


@pytest.mark.asyncio
async def test_process_control_heartbeat_req_and_ack_updates():
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    conn_id = "in:127.0.0.1:9999:1"
    ad.connections[conn_id] = {"last_seen": 0}
    ad._wire_state[conn_id] = {"send_next": 1}
    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    ts = int(time.time())
    # HB request should result in an HB ACK frame being written
    await ad._process_control_command(conn_id, s_writer, f"REQ:{ts}")
    line = await asyncio.wait_for(c_reader.readline(), timeout=1)
    assert b"HB" in line and b"ACK:" in line
    # ACK processing should update hb state and last_seen
    await ad._process_control_command(conn_id, s_writer, f"ACK:{ts}")
    st = ad._hb_state.get(conn_id)
    assert st and st.get("last_ack_ts", 0) > 0
    s_writer.close()
    c_writer.close()
    await s_writer.wait_closed()
    await c_writer.wait_closed()
    server.close()
    await server.wait_closed()


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


@pytest.mark.asyncio
async def test_read_loop_auth_required_and_ack_clear():
    proto = MuxConProtocolHandler()
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    ad._auth_required = True

    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        conn_id = "in:127.0.0.1:5555:1"
        # Register connection as server role and unauthenticated
        ad.connections[conn_id] = {"reader": s_reader, "writer": s_writer, "role": "server", "auth_ok": False}
        ad._wire_state[conn_id] = {"send_next": 1}

        async def writer_task():
            # Send a DATA frame; expect AUTH:REQUIRED control back
            dseq = ad._next_frame_seq(conn_id)
            df = proto.create_data_frame(1, dseq, b"hello")
            c_writer.write(df)
            await c_writer.drain()
            # Read control response
            line = await asyncio.wait_for(c_reader.readline(), timeout=1)
            assert b"AUTH:REQUIRED" in line
            # Send an ACK frame referencing an arbitrary seq and ensure it clears from sendbuf
            peer_key = "host:127.0.0.1"
            ad._peer_sendbuf[peer_key] = {42: (conn_id, 0, b"x", time.time())}
            aseq = ad._next_frame_seq(conn_id)
            ack = proto.create_ack_frame(42, aseq)
            c_writer.write(ack)
            await c_writer.drain()
            # Close to end read loop
            c_writer.close()
            await c_writer.wait_closed()

        t = asyncio.create_task(writer_task())
        await ad._read_loop(conn_id)
        await t
        # Ensure ACK removed from send buffer
        assert 42 not in ad._peer_sendbuf.get("host:127.0.0.1", {})
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_mpath_send_helpers(monkeypatch):
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    # Create a real stream pair to get a proper StreamWriter
    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        conn_id = "out:1.2.3.4:7822:1"
        ad.connections[conn_id] = {"writer": s_writer}
        ad._wire_state[conn_id] = {"send_next": 1}

        # Force selector to return our connection id
        monkeypatch.setattr(ad, "_select_mpath_connection", lambda key: conn_id)

        # OPEN
        ok = await ad._send_stream_open_mpath("node:peer", 7, "portA")
        assert ok is True
        line = await asyncio.wait_for(c_reader.readline(), timeout=1)
        assert line.startswith(b"#7:O:")

        # CLOSE
        ok = await ad._send_stream_close_mpath("node:peer", 7, "done")
        line = await asyncio.wait_for(c_reader.readline(), timeout=1)
        assert line.startswith(b"#7:E:")

        # DATA (also populates peer sendbuf)
        ok = await ad._send_data_mpath("node:peer", 7, b"xyz")
        line = await asyncio.wait_for(c_reader.readline(), timeout=1)
        assert line.startswith(b"#7:D:")
        # Check send buffer contains one entry for this peer
        assert ad._peer_sendbuf.get("node:peer")
    finally:
        s_writer.close()
        c_writer.close()
        await s_writer.wait_closed()
        await c_writer.wait_closed()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_inbound_ordering_and_routing_to_proxy(monkeypatch):
    # Set up adapter and a proxy mapped to a stream id
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    conn_id = "in:10.0.0.2:40000:1"
    # Register a reader/writer pair for the read loop to function; but we won't use it here
    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    ad.connections[conn_id] = {"reader": s_reader, "writer": s_writer, "role": "server", "auth_ok": True}
    ad._wire_state[conn_id] = {"send_next": 1}
    peer_key = ad._derive_peer_key_from_conn_id(conn_id)

    # Create a simple proxy that records data
    class Probe:
        def __init__(self):
            self.seen = []

        async def trigger_data_received(self, data: bytes):
            self.seen.append(data)

    proxy = Probe()
    ad._map_session(peer_key, 5, proxy)

    # Deliver out-of-order frames: seq 2 then 1, both for stream 5
    await ad._handle_inbound_data(conn_id, 5, b"two", 2)
    # Not delivered yet because seq 1 missing
    assert proxy.seen == []
    await ad._handle_inbound_data(conn_id, 5, b"one", 1)
    # Should deliver in order: one, then buffered two
    assert proxy.seen == [b"one", b"two"]

    s_writer.close()
    c_writer.close()
    await s_writer.wait_closed()
    await c_writer.wait_closed()
    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_route_to_local_port_via_session_map(monkeypatch):
    # Adapter with a fake PortManager that provides data plumbing
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    conn_id = "in:10.0.0.3:40001:1"
    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    ad.connections[conn_id] = {"reader": s_reader, "writer": s_writer, "role": "server", "auth_ok": True}
    ad._wire_state[conn_id] = {"send_next": 1}
    peer_key = ad._derive_peer_key_from_conn_id(conn_id)

    # Map a local session for stream 9 to a local port 'lp1'
    ad._local_session_map[peer_key] = {9: "lp1"}

    # Fake PortManager that records writes
    class FakePM:
        def __init__(self):
            self.writes = []

        async def write_to_port(self, name, data, client_id=None):
            self.writes.append((name, data, client_id))

    ad.main_port_manager = FakePM()

    # Route data frame; ensure PortManager.write_to_port is called
    await ad._route_data_frame(conn_id, 9, b"abc", 11)
    assert ad.main_port_manager.writes and ad.main_port_manager.writes[0][0] == "lp1"


@pytest.mark.asyncio
async def test_ports_advertise_and_request_dedup(monkeypatch):
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {"listeners": [{"host": "127.0.0.1", "port": 9000, "enabled": True}]}})

    # Fake PortManager listing one local port
    class PM:
        async def get_port_list_with_federation(self):
            return [
                {"name": "p1", "adapter_type": "loopback", "description": "", "connected": True, "max_read_write_users": 1}
            ]

    ad.main_port_manager = PM()

    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    conn_id = "in:127.0.0.1:12345:1"
    ad.connections[conn_id] = {"writer": s_writer, "auth_ok": True}
    ad._wire_state[conn_id] = {"send_next": 1}

    # _maybe_advertise_local_ports should send exactly one PORTS:FEDERATED
    await ad._maybe_advertise_local_ports(conn_id)
    line = await asyncio.wait_for(c_reader.readline(), timeout=1)
    assert b"#:C:" in line or line.startswith(b"#0:C:")
    # Drain the multi-line PORTS:FEDERATED payload until END:PORTS
    accum = b""
    while True:
        part = await asyncio.wait_for(c_reader.readline(), timeout=1)
        accum += part
        if b"END:PORTS" in part or part == b"":
            break
    # Second call should be a no-op (dedup by flag)
    await ad._maybe_advertise_local_ports(conn_id)
    assert c_reader.at_eof() is False

    # Test _request_remote_ports de-dup within 2 seconds
    await ad._request_remote_ports(conn_id)
    l1 = await asyncio.wait_for(c_reader.readline(), timeout=1)
    assert b"PORTS:LIST:FEDERATED" in l1
    # Immediate second call should be deduped and not emit a new frame
    await ad._request_remote_ports(conn_id)
    # Try to read another line with a short timeout; it should timeout (no new data)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(c_reader.readline(), timeout=0.05)

    s_writer.close()
    c_writer.close()
    await s_writer.wait_closed()
    await c_writer.wait_closed()
    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_remote_port_proxy_lifecycle_and_write(monkeypatch):
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    # Provide a writer path via mpath selection
    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    conn_id = "out:peer:7822:1"
    ad.connections[conn_id] = {"writer": s_writer}
    ad._wire_state[conn_id] = {"send_next": 1}
    monkeypatch.setattr(ad, "_select_mpath_connection", lambda key: conn_id)

    # Create proxy and attach minimal metadata
    meta = type("M", (), {"description": "d", "max_rw_users": 2})()
    proxy = ad.RemotePortProxy(ad, "node:peer", "r1", meta)
    await proxy.start()
    assert proxy.get_status()["connected"] is True

    # Write data; expect OPEN (first write) then DATA
    await proxy.write_data(b"hello", client_id="c1")
    l1 = await asyncio.wait_for(c_reader.readline(), timeout=1)
    assert l1.startswith(b"#1:O:")  # stream 1
    l2 = await asyncio.wait_for(c_reader.readline(), timeout=1)
    assert l2.startswith(b"#1:D:")

    # Close stream for client
    ok = await proxy.close_stream_for_client("c1")
    assert ok is True
    l3 = await asyncio.wait_for(c_reader.readline(), timeout=1)
    assert l3.startswith(b"#1:E:")

    # Disconnect cleanup
    await proxy.disconnect()
    assert proxy.state.name in ("DESTROYED", "DESTROYING")

    s_writer.close()
    c_writer.close()
    await s_writer.wait_closed()
    await c_writer.wait_closed()
    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_fast_shutdown_begin_sends_end_and_closes(monkeypatch):
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        conn_id = "in:127.0.0.1:5555:99"
        ad.connections[conn_id] = {"writer": s_writer}
        ad._wire_state[conn_id] = {"send_next": 1}
        closed = {"flag": False}

        async def fake_close(cid):
            closed["flag"] = True

        monkeypatch.setattr(ad, "_close_connection", fake_close)
        # Trigger fast shutdown begin; adapter should send END and close
        await ad._process_control_command(conn_id, s_writer, "MPATH:SHUTDOWN:BEGIN:maintenance")
        line = await asyncio.wait_for(c_reader.readline(), timeout=1)
        assert b"#0:C:" in line or line.startswith(b"#0:C:")
        # Payload contains MPATH:END
        assert b"MPATH:END" in line
        assert closed["flag"] is True
        # State should be CLOSED
        st = ad._shutdown_state.get(conn_id) or {}
        assert st.get("state") == "CLOSED"
    finally:
        s_writer.close()
        c_writer.close()
        await s_writer.wait_closed()
        await c_writer.wait_closed()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_fast_shutdown_end_closes(monkeypatch):
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        conn_id = "in:127.0.0.1:5555:100"
        ad.connections[conn_id] = {"writer": s_writer}
        ad._wire_state[conn_id] = {"send_next": 1}
        closed = {"flag": False}

        async def fake_close(cid):
            closed["flag"] = True

        monkeypatch.setattr(ad, "_close_connection", fake_close)
        await ad._process_control_command(conn_id, s_writer, "MPATH:END")
        assert closed["flag"] is True
        st = ad._shutdown_state.get(conn_id) or {}
        assert st.get("state") == "CLOSED"
        # No frames are expected to be sent on MPATH:END; ensure no immediate line
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(c_reader.readline(), timeout=0.05)
    finally:
        s_writer.close()
        c_writer.close()
        await s_writer.wait_closed()
        await c_writer.wait_closed()
        server.close()
        await server.wait_closed()
