import asyncio
import json
import os
import pytest
import textwrap

from openmux.server.main import OpenMuxServer


@pytest.mark.asyncio
async def test_control_socket_status_and_reloads(tmp_path):
    # Create a minimal config file
    cfg_path = tmp_path / "server.yaml"
    cfg_path.write_text(
        textwrap.dedent(
            """
            server:
              id: test
            authentication:
              users:
                - username: admin
                  password: secret
            logging:
              level: WARNING
            runtime:
              pidfile: "{pid}"
            """
        ).format(pid=str(tmp_path / "openmux.pid"))
    )

    # Instantiate server (won't start adapters in this test)
    server = OpenMuxServer(str(cfg_path))

    # Create a temp unix socket path
    # Use short path under /tmp due to AF_UNIX path length limits
    sock_path = os.path.join("/tmp", f"omuxctl_{os.getpid()}_{abs(hash(str(tmp_path)))}.sock")

    # Start control socket
    await server._start_control_socket(str(sock_path))

    try:
        # Permissions should be strict (0600)
        mode = os.stat(sock_path).st_mode & 0o777
        assert mode == 0o600

        # Connect and ask for status
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        writer.write((json.dumps({"action": "status"}) + "\n").encode("utf-8"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        writer.close()
        await writer.wait_closed()
        resp = json.loads(line.decode("utf-8"))
        assert resp.get("ok") is True
        assert isinstance(resp.get("result"), dict)
        assert {"adapters", "started", "total"}.issubset(resp["result"].keys())

        # Monkeypatch soft/full reload to return sentinel results
        async def fake_soft(context=None):
            return {"soft": True}

        async def fake_full(context=None):
            return {"full": True}

        server.reload_adapters_soft = fake_soft  # type: ignore
        server.reload_adapters_full = fake_full  # type: ignore

        # Soft reload
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        writer.write((json.dumps({"action": "reload", "scope": "soft"}) + "\n").encode("utf-8"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        writer.close()
        await writer.wait_closed()
        resp = json.loads(line.decode("utf-8"))
        assert resp.get("ok") is True
        assert resp.get("result") == {"soft": True}

        # Full reload
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        writer.write((json.dumps({"action": "reload", "scope": "full"}) + "\n").encode("utf-8"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        writer.close()
        await writer.wait_closed()
        resp = json.loads(line.decode("utf-8"))
        assert resp.get("ok") is True
        assert resp.get("result") == {"full": True}
    finally:
        await server._stop_control_socket()
