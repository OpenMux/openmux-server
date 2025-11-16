import asyncio
import os
import ssl
from types import SimpleNamespace

import pytest

from openmux.server.adapters.muxcon import FederationPeer, UnifiedMuxConAdapter


async def make_stream_pair():
    server_side = {}

    async def handle(reader, writer):
        server_side["reader"] = reader
        server_side["writer"] = writer

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    client_reader, client_writer = await asyncio.open_connection(host, port)
    # Wait until server handler stores streams
    while "reader" not in server_side:
        await asyncio.sleep(0)
    return server, server_side["reader"], server_side["writer"], client_reader, client_writer


@pytest.mark.asyncio
async def test_server_handshake_success(tmp_path):
    # Default auth is on; disable explicitly for this happy-path test
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {"listeners": [], "auth_required": False}})
    server, s_reader, s_writer, c_reader, c_writer = await make_stream_pair()
    try:
        # Send HELLO from client
        hello = "HELLO MuxCon/1.0 TYPE=regular ID=peer INST=peer1"
        c_writer.write((hello + "\n").encode())
        await c_writer.drain()
        conn_id = "in:test:1"
        await ad._perform_server_handshake(s_reader, s_writer, conn_id)
        # Read server's response from client
        resp = (await c_reader.readline()).decode().strip()
        assert resp.startswith("OK MuxCon/1.0")
        assert conn_id in ad.connections
    finally:
        s_writer.close()
        c_writer.close()
        await s_writer.wait_closed()
        await c_writer.wait_closed()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_server_handshake_auth_required_missing_pkid(tmp_path):
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {"listeners": []}})
    ad._auth_required = True
    server, s_reader, s_writer, c_reader, c_writer = await make_stream_pair()
    try:
        c_writer.write(b"HELLO MuxCon/1.0 TYPE=regular ID=peer INST=peer1\n")
        await c_writer.drain()
        await ad._perform_server_handshake(s_reader, s_writer, "in:auth:1")
        # First response is the OK handshake line
        ok_line = await asyncio.wait_for(c_reader.readline(), timeout=0.5)
        assert ok_line.startswith(b"OK MuxCon/1.0")
        # Then the AUTH:ERROR control frame should follow
        frame = await asyncio.wait_for(c_reader.readline(), timeout=0.5)
        assert b"AUTH:ERROR" in frame
    finally:
        s_writer.close()
        c_writer.close()
        await s_writer.wait_closed()
        await c_writer.wait_closed()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_client_handshake_success():
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {"listeners": []}})
    server, s_reader, s_writer, c_reader, c_writer = await make_stream_pair()
    try:
        # Server side: respond with OK line
        async def server_respond():
            line = (await s_reader.readline()).decode()
            assert line.startswith("HELLO MuxCon/1.0 ")
            s_writer.write(b"OK MuxCon/1.0 ID=SRV INST=I1\n")
            await s_writer.drain()

        task = asyncio.create_task(server_respond())
        await ad._perform_client_handshake(c_reader, c_writer, "out:1")
        await task
        assert "out:1" in ad.connections
    finally:
        s_writer.close()
        c_writer.close()
        await s_writer.wait_closed()
        await c_writer.wait_closed()
        server.close()
        await server.wait_closed()


def test_apply_per_connection_filters():
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    ad._key_filters = {"kid": {"advertise_filters": {"include": ["x*"]}, "accept_filters": {}}}
    ad._apply_per_connection_filters("cid", "kid")
    assert "cid" in ad._conn_filters
    assert ad._conn_filters["cid"]["advertise_filters"]["include"] == ["x*"]


@pytest.mark.asyncio
async def test_tls_autogen_and_server_context(tmp_path):
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    lconf = {"use_tls": True, "tls_dir": str(tmp_path)}
    cert_path, key_path = await ad._ensure_autogen_cert(lconf)
    assert os.path.exists(cert_path) and os.path.exists(key_path)
    # Create server SSL context and require client cert
    lconf.update({"ssl_cert": cert_path, "ssl_key": key_path, "require_client_cert": True})
    ctx = await ad._create_server_ssl_context(lconf)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.asyncio
async def test_connect_with_routing_options_loopback():
    # Start a simple echo server
    async def handle(reader, writer):
        data = await reader.read(1)
        writer.write(data)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    try:
        r, w = await ad._connect_with_routing_options(host, port, None, None, None)
        w.write(b"X")
        await w.drain()
        got = await r.read(1)
        assert got == b"X"
        w.close()
        await w.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_fault_injection_flags():
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    ad.connections["c1"] = {"last_seen": 0}
    assert await ad.freeze_connection("c1") is True
    assert await ad.unfreeze_connection("c1") is True
    assert await ad.set_drop_heartbeats("c1", True) is True
