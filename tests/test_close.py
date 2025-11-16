from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openmux.client.adapters import TcpClientAdapter


@pytest.mark.asyncio
async def test_close():
    """Isolated test for closing connection"""
    # Create a TCP client adapter with a well-defined writer
    conn = TcpClientAdapter(host="localhost", port=8023, config={"use_tls": False})
    conn.is_connected = True
    conn.is_authenticated = True

    # Create a separate writer mock with explicit methods
    writer_mock = MagicMock()
    writer_mock.write = MagicMock()  # write is sync
    writer_mock.drain = AsyncMock()  # drain is async
    writer_mock.close = MagicMock()  # close is sync
    writer_mock.wait_closed = AsyncMock()  # wait_closed is async

    # Set the writer on the connection
    conn.writer = writer_mock

    # Call close
    await conn.close()

    # Verify the writer was used correctly
    writer_mock.write.assert_called_once_with(b"QUIT\n")
    writer_mock.close.assert_called_once()

    # Verify connection state
    assert conn.is_connected is False
    assert conn.is_authenticated is False
    assert conn.writer is None
