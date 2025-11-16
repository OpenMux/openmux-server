"""
Detailed tests for `TcpClientAdapter` covering connection lifecycle,
authentication, port operations, I/O, and close edge cases.

Previously this coverage lived in `test_connection.py` when migrating
from the legacy `ServerConnection`; redundant patterns have been reduced.
"""

import asyncio
from typing import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openmux.client.adapters import TcpClientAdapter


@pytest.fixture
def adapter():
    return TcpClientAdapter(host="test-server", port=5000, config={"use_tls": False})


def test_init(adapter):
    assert adapter.host == "test-server"
    assert adapter.port == 5000
    assert adapter.use_tls is False
    assert adapter.reader is None and adapter.writer is None
    assert adapter.is_connected is False and adapter.is_authenticated is False


# ---------------------- Connect -------------------------------------------------
@pytest.mark.asyncio
async def test_connect_already_connected(adapter):
    adapter.is_connected = True
    assert await adapter.connect() is True


@pytest.mark.asyncio
async def test_connect_success_unencrypted(adapter):
    mock_reader = AsyncMock()
    mock_writer = MagicMock()
    mock_reader.readline.return_value = b"Authentication required\n"
    with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
        assert await adapter.connect() is True
        assert adapter.reader is mock_reader and adapter.writer is mock_writer


@pytest.mark.asyncio
async def test_connect_success_encrypted():
    adapter = TcpClientAdapter(host="test-server", port=5000, config={"use_tls": True})
    mock_reader = AsyncMock()
    mock_writer = MagicMock()
    mock_reader.readline.return_value = b"Authentication required\n"
    with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
        with patch("ssl.create_default_context") as mock_ctx:
            assert await adapter.connect() is True
            mock_ctx.assert_called()


@pytest.mark.asyncio
async def test_connect_unexpected_response(adapter):
    mock_reader = AsyncMock()
    mock_writer = MagicMock()
    mock_reader.readline.return_value = b"Unexpected\n"
    with patch.object(adapter, "close") as mock_close:
        with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
            assert await adapter.connect() is False
            mock_close.assert_called_once()


@pytest.mark.asyncio
async def test_connect_exception(adapter):
    with patch("asyncio.open_connection", side_effect=Exception("boom")):
        assert await adapter.connect() is False


# ---------------------- Authentication ------------------------------------------
@pytest.mark.parametrize(
    "method,connected,response,expected,username",
    [
        ("password", True, b"AUTH:SUCCESS\n", True, "user"),
        ("password", True, b"Authentication failed\n", False, None),
        ("password", False, b"Authentication successful\n", False, None),
        ("key", True, b"Authentication successful\n", True, "api-user"),
        ("key", True, b"Authentication failed\n", False, None),
        ("key", False, b"Authentication successful\n", False, None),
    ],
)
@pytest.mark.asyncio
async def test_authentication_matrix(method, connected, response, expected, username):
    adapter = TcpClientAdapter("h", 1, config={})
    adapter.is_connected = connected
    if connected:
        adapter.writer = MagicMock()
        adapter.reader = AsyncMock()
        # For password success case, use HMAC challenge->success flow
        if method == "password" and expected is True:
            import base64

            nonce_raw = b"matrix-nonce"
            nonce_b64 = base64.b64encode(nonce_raw).decode()
            adapter.reader.readline = AsyncMock(side_effect=[f"AUTH:CHALLENGE:{nonce_b64}\n".encode(), b"AUTH:SUCCESS\n"])
        else:
            adapter.reader.readline = AsyncMock(return_value=response)
        adapter.writer.drain = AsyncMock()
    else:
        adapter.writer = None
        adapter.reader = None
    if method == "password":
        ok = await adapter.authenticate_with_password("user", "pass")
    else:
        ok = await adapter.authenticate_with_key("api-key")
    assert ok is expected
    if expected:
        assert adapter.is_authenticated is True and adapter.username == username
    else:
        assert (adapter.username is None) or (adapter.username == "api-user" and not expected)


@pytest.mark.asyncio
async def test_authentication_exception_password(adapter):
    adapter.is_connected = True
    adapter.writer = MagicMock()
    adapter.reader = AsyncMock()
    adapter.writer.drain = AsyncMock(side_effect=Exception("x"))
    assert await adapter.authenticate_with_password("u", "p") is False


@pytest.mark.asyncio
async def test_authentication_exception_key(adapter):
    adapter.is_connected = True
    adapter.writer = MagicMock()
    adapter.reader = AsyncMock()
    adapter.writer.drain = AsyncMock(side_effect=Exception("x"))
    assert await adapter.authenticate_with_key("k") is False


# ---------------------- Port listing -------------------------------------------
@pytest.mark.asyncio
async def test_list_ports_success(adapter):
    adapter.is_connected = True
    adapter.is_authenticated = True
    adapter.writer = MagicMock()
    adapter.reader = AsyncMock()
    adapter.writer.drain = AsyncMock()
    adapter.reader.read = AsyncMock(return_value=b"Port List:\nA\nB\n")
    assert await adapter.list_ports() == ["A", "B"]


@pytest.mark.parametrize("connected,authed", [(False, False), (True, False)])
@pytest.mark.asyncio
async def test_list_ports_not_ready_states(connected, authed):
    adapter = TcpClientAdapter("h", 1, config={})
    adapter.is_connected = connected
    adapter.is_authenticated = authed
    assert await adapter.list_ports() == []


@pytest.mark.asyncio
async def test_list_ports_exception(adapter):
    adapter.is_connected = True
    adapter.is_authenticated = True
    adapter.writer = MagicMock()
    adapter.writer.drain = AsyncMock(side_effect=Exception("x"))
    assert await adapter.list_ports() == []


# ---------------------- Port connect -------------------------------------------
@pytest.mark.asyncio
async def test_connect_to_port_success(adapter):
    adapter.is_connected = True
    adapter.is_authenticated = True
    adapter.writer = MagicMock()
    adapter.reader = AsyncMock()
    adapter.reader.readline = AsyncMock(return_value=b"CONNECTED:foo:RW\n")
    adapter.writer.drain = AsyncMock()
    assert await adapter.connect_to_port("foo") is True


@pytest.mark.parametrize("connected,authed", [(False, False), (True, False)])
@pytest.mark.asyncio
async def test_connect_to_port_not_ready(connected, authed):
    adapter = TcpClientAdapter("h", 1, config={})
    adapter.is_connected = connected
    adapter.is_authenticated = authed
    assert await adapter.connect_to_port("x") is False


@pytest.mark.asyncio
async def test_connect_to_port_failure(adapter):
    adapter.is_connected = True
    adapter.is_authenticated = True
    adapter.writer = MagicMock()
    adapter.reader = AsyncMock()
    adapter.reader.readline = AsyncMock(return_value=b"ERROR:oops\n")
    adapter.writer.drain = AsyncMock()
    assert await adapter.connect_to_port("bad") is False


@pytest.mark.asyncio
async def test_connect_to_port_exception(adapter):
    adapter.is_connected = True
    adapter.is_authenticated = True
    adapter.writer = MagicMock()
    adapter.writer.drain = AsyncMock(side_effect=Exception("x"))
    assert await adapter.connect_to_port("foo") is False


# ---------------------- Send / Read --------------------------------------------
@pytest.mark.asyncio
async def test_send_data_success(adapter):
    adapter.is_connected = True
    adapter.writer = MagicMock()
    adapter.writer.drain = AsyncMock()
    assert await adapter.send_data(b"hi") is True


@pytest.mark.asyncio
async def test_send_data_not_connected(adapter):
    adapter.is_connected = False
    assert await adapter.send_data(b"hi") is False


@pytest.mark.asyncio
async def test_send_data_exception(adapter):
    adapter.is_connected = True
    adapter.writer = MagicMock()
    adapter.writer.drain = AsyncMock(side_effect=Exception("x"))
    assert await adapter.send_data(b"hi") is False and adapter.is_connected is False


@pytest.mark.asyncio
async def test_read_data_success(adapter):
    adapter.is_connected = True
    adapter.reader = AsyncMock()
    adapter.reader.read.return_value = b"data"
    assert await adapter.read_data() == b"data"


@pytest.mark.asyncio
async def test_read_data_timeout(adapter):
    adapter.is_connected = True
    adapter.reader = AsyncMock()

    async def slow(n):
        await asyncio.sleep(0.2)
        return b"late"

    adapter.reader.read = slow
    assert await adapter.read_data(timeout=0.01) == b""


@pytest.mark.asyncio
async def test_read_data_exception(adapter):
    adapter.is_connected = True
    adapter.reader = AsyncMock()
    adapter.reader.read.side_effect = Exception("x")
    assert await adapter.read_data() == b"" and adapter.is_connected is False


@pytest.mark.asyncio
async def test_read_data_exception_timeout_path(adapter):
    adapter.is_connected = True
    adapter.reader = AsyncMock()
    adapter.reader.read.side_effect = Exception("x")
    assert await adapter.read_data(timeout=0.1) == b"" and adapter.is_connected is False


# ---------------------- Close --------------------------------------------------
@pytest.mark.asyncio
async def test_close_not_connected(adapter):
    adapter.is_connected = False
    await adapter.close()
    assert adapter.is_connected is False


@pytest.mark.asyncio
async def test_close_success(adapter):
    adapter.is_connected = True
    adapter.is_authenticated = True
    writer = MagicMock()
    writer.write = MagicMock()
    writer.close = MagicMock()

    async def drain():
        return None

    async def wait_closed():
        return None

    writer.drain = drain
    writer.wait_closed = wait_closed
    adapter.writer = writer
    adapter.reader = MagicMock()
    await adapter.close()
    assert adapter.writer is None and adapter.reader is None and adapter.is_connected is False
    writer.write.assert_called_once_with(b"QUIT\n")


@pytest.mark.asyncio
async def test_close_drain_exception(adapter):
    adapter.is_connected = True
    writer = MagicMock()
    writer.write = MagicMock()
    writer.close = MagicMock()

    async def failing():
        raise Exception("x")

    writer.drain = failing
    writer.wait_closed = AsyncMock()
    adapter.writer = writer
    await adapter.close()
    assert adapter.writer is None and adapter.is_connected is False


@pytest.mark.asyncio
async def test_close_close_exception(adapter):
    adapter.is_connected = True
    writer = MagicMock()
    writer.write = MagicMock()

    async def drain():
        return None

    def bad_close():
        raise Exception("boom")

    writer.drain = drain
    writer.close = bad_close
    adapter.writer = writer
    adapter.reader = MagicMock()
    await adapter.close()
    assert adapter.writer is None and adapter.is_connected is False
