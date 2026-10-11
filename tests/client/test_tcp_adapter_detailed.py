"""
Detailed tests for `TcpClientAdapter` covering connection lifecycle,
authentication, port operations, I/O, and close edge cases.

Previously this coverage lived in `test_connection.py` when migrating
from the legacy `ServerConnection`; redundant patterns have been reduced.
"""

import asyncio
import json
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


# ---------------------- _list_ports_standard framing ----------------------
# The connect/auth tests above exercise the legacy "Port List:" blob path but
# leave the modern LIST framing (JSON payload, LIST:SUCCESS ... END:LIST) and
# the writer/reader guards untested. These pin that behavior so the bracket-
# C901 refactor of _list_ports_standard is provably behavior-preserving.
# ---------------------------------------------------------------------------


class _R:
    def __init__(self, readlines=None, read=b"", read_raises=False, reader=True):
        self._ql = list(readlines or [])
        self._read = read
        self._read_raises = read_raises
        self._reader = reader

    def __bool__(self):
        return self._reader

    async def readline(self):
        if self._ql:
            return self._ql.pop(0)
        return b""

    async def read(self, n=4096):
        if self._read_raises:
            raise OSError("boom")
        return self._read


class _W:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data: bytes):
        self.buf += data

    async def drain(self):
        return None


def _a(write_ok=True, read_side=None):
    a = TcpClientAdapter(host="test-server", port=5000, config={"use_tls": False})
    a.writer = _W() if write_ok else None
    a.reader = read_side
    return a


@pytest.mark.asyncio
async def test_list_ports_no_writer():
    a = _a(write_ok=False)
    assert await a._list_ports_standard() == []


@pytest.mark.asyncio
async def test_list_ports_no_reader():
    a = _a()
    a.reader = None
    assert await a._list_ports_standard() == []


@pytest.mark.asyncio
async def test_list_ports_readline_raises_fallback():
    a = _a()
    r = _R(readlines=[b""])

    async def boom():
        raise ValueError("no readline")

    r.readline = boom
    a.reader = r
    assert await a._list_ports_standard() == []


@pytest.mark.asyncio
async def test_list_ports_json_happy_path():
    a = _a()
    payload = {"type": "PORT_LIST", "ports": [{"name": "a"}, {"name": "b"}], "server": {"server_id": "s1"}}
    a.reader = _R(readlines=[("LIST:" + json.dumps(payload)).encode()])
    out = await a._list_ports_standard()
    assert out == ["a", "b"]
    # last_port_metadata / last_server_info were stored from the JSON payload
    assert [m["name"] for m in a.last_port_metadata] == ["a", "b"]
    assert a.last_server_info == {"server_id": "s1"}


@pytest.mark.asyncio
async def test_list_ports_json_payload_is_list():
    a = _a()
    # JSON decodes to a list (not a dict) -> raw_ports [] and server None
    a.reader = _R(readlines=[b"LIST:[" + b'{"name": "a"}' + b"]"])
    assert await a._list_ports_standard() == []


@pytest.mark.asyncio
async def test_list_ports_json_dedup_by_port_key():
    a = _a()
    payload = {"ports": [{"name": "a"}, {"name": "b"}, {"port": "b"}]}
    a.reader = _R(readlines=[("LIST:" + json.dumps(payload)).encode()])
    # "b" appears via name then via port (last occurrence wins); "a" unique
    assert await a._list_ports_standard() == ["a", "b"]


@pytest.mark.asyncio
async def test_list_ports_json_bad_parse():
    a = _a()
    # "LIST:{" prefix but the remainder is invalid JSON -> parse error -> []
    a.reader = _R(readlines=[b"LIST:{not json"])
    assert await a._list_ports_standard() == []


@pytest.mark.asyncio
async def test_list_ports_legacy_blob():
    a = _a()
    a.reader = _R(read=b"Port List:\nX\n\nY\n")
    assert await a._list_ports_standard() == ["X", "Y"]


@pytest.mark.asyncio
async def test_list_ports_legacy_read_raises():
    a = _a()
    a.reader = _R(read=b"", read_raises=True)
    # first line empty -> non-SUCCESS -> legacy blob path -> read raises -> error -> []
    assert await a._list_ports_standard() == []


@pytest.mark.asyncio
async def test_list_ports_unexpected_first_line():
    a = _a()
    a.reader = _R(readlines=[b"NOPE\n"])
    assert await a._list_ports_standard() == []


@pytest.mark.asyncio
async def test_list_ports_success_framed():
    a = _a()
    a.reader = _R(readlines=[b"LIST:SUCCESS\n", b"p1\n", b"p2\n", b"END:LIST\n"])
    assert await a._list_ports_standard() == ["p1", "p2"]


@pytest.mark.asyncio
async def test_list_ports_success_framed_eof():
    a = _a()
    a.reader = _R(readlines=[b"LIST:SUCCESS\n", b"p1\n"])
    # second loop readline returns "" -> break
    assert await a._list_ports_standard() == ["p1"]


@pytest.mark.asyncio
async def test_list_ports_success_framed_error():
    a = _a()
    a.reader = _R(readlines=[b"LIST:SUCCESS\n", b"ERROR:bad\n"])
    assert await a._list_ports_standard() == []


class _RaisingMetadata(TcpClientAdapter):
    """A subclass whose last_port_metadata assignment raises, to pin the
    JSON metadata-try/except (the functional list is unaffected)."""

    @property
    def last_port_metadata(self):
        return None

    @last_port_metadata.setter
    def last_port_metadata(self, val):
        raise RuntimeError("no metadata attr")


@pytest.mark.asyncio
async def test_list_ports_json_metadata_assignment_raises():
    a = _RaisingMetadata(host="test-server", port=5000, config={"use_tls": False})
    a.writer = _W()
    payload = {"ports": [{"name": "a"}, {"name": "b"}]}
    a.reader = _R(readlines=[("LIST:" + json.dumps(payload)).encode()])
    # the metadata assignment raises -> best-effort except; functional list returned
    out = await a._list_ports_standard()
    assert out == ["a", "b"]
    assert a.last_port_metadata is None
