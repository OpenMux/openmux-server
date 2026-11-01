"""A client that vanishes must not fill the log with tracebacks.

The console listener discovers a dead peer at the next write, because it never holds the peer
waiting. That is the normal end of a session: closing a browser tab, killing a telnet client, or an
aborted probe socket all reset the connection mid-write. Soak testing against the pre-fix server
produced 48 ERROR records and 48 tracebacks from exactly this path in one short run.

Policy under test, matching the read path in `receive_char`:

- `send_line` and `send_raw_data` report a transport-level failure with one INFO line and return
  False with no traceback.
- A non-connection failure keeps the ERROR record and the traceback. Both cases end the session.
- The adapter passes that result to the caller instead of raising, so a peer that vanished never
  also arrives as an unexpected port write failure at the same instant.

FakeReader/FakeWriter mirror tests/server/test_client_listener_issue88.py.
"""

import asyncio
import logging
from typing import Any, cast

import pytest

from openmux.server.adapters.client_listener import CTRL_MARKER, ClientSession, TcpServerAdapter

# The three transport errors asyncio raises when a peer left. Each must be quiet.
PEER_ERRORS = [
    ConnectionResetError(54, "Connection reset by peer"),
    BrokenPipeError(32, "Broken pipe"),
    ConnectionAbortedError(32, "Connection aborted"),
]


class FakeReader:
    async def read(self, n: int) -> bytes:
        await asyncio.sleep(0)
        return b""


class BaseWriter:
    def __init__(self):
        self.buffer = bytearray()
        self.closed = False

    def get_extra_info(self, name: str):
        return ("127.0.0.1", 55555) if name == "peername" else None

    def write(self, data: bytes) -> None:
        self.buffer += data

    def is_closing(self) -> bool:
        return self.closed

    def write_eof(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


class DeadWriter(BaseWriter):
    """Writer whose every drain fails with the given error, as a real transport does after a RST."""

    def __init__(self, error: BaseException):
        super().__init__()
        self.error = error

    async def drain(self) -> None:
        raise self.error


class ClosedDeadWriter(DeadWriter):
    """Dead writer whose transport is already closed, the state a teardown sees after a reset."""

    def is_closing(self) -> bool:
        return True


class GoodWriter(BaseWriter):
    async def drain(self) -> None:
        await asyncio.sleep(0)


def make_adapter() -> TcpServerAdapter:
    return TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})


def add(adapter: TcpServerAdapter, client_id: str, writer, port_name: str = "loop1") -> ClientSession:
    """Register a session with the adapter, as a login followed by a port attach would."""
    session = ClientSession(client_id, "127.0.0.1", cast(Any, FakeReader()), cast(Any, writer), adapter.logger)
    session.connected_port = port_name
    adapter.clients[client_id] = session
    adapter.port_clients.setdefault(port_name, []).append(client_id)
    return session


def records(caplog, adapter: TcpServerAdapter):
    return [r for r in caplog.records if r.name == adapter.logger.name]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", PEER_ERRORS, ids=lambda e: type(e).__name__)
async def test_peer_gone_write_is_info_without_a_traceback(error, caplog):
    """Each transport error ends the session with one INFO line and no traceback."""
    adapter = make_adapter()
    session = add(adapter, "c1", DeadWriter(error))

    with caplog.at_level(logging.DEBUG, logger=adapter.logger.name):
        assert await session.send_line("hello") is False
        assert await session.send_raw_data(b"hello") is False

    seen = records(caplog, adapter)
    assert seen, "a failed write must still be reported"
    assert all(r.levelno == logging.INFO for r in seen), [(r.levelno, r.getMessage()) for r in seen]
    assert all(r.exc_info is None for r in seen), "a peer that vanished must not carry a traceback"
    assert session.connected is False


@pytest.mark.asyncio
async def test_other_write_failure_keeps_error_with_traceback(caplog):
    """A write failure that is not a dead peer stays ERROR and keeps its traceback."""
    adapter = make_adapter()
    session = add(adapter, "c1", DeadWriter(RuntimeError("transport is wedged")))

    with caplog.at_level(logging.DEBUG, logger=adapter.logger.name):
        assert await session.send_line("hello") is False

    errors = [r for r in records(caplog, adapter) if r.levelno >= logging.ERROR]
    assert len(errors) == 1, [(r.levelno, r.getMessage()) for r in records(caplog, adapter)]
    assert errors[0].exc_info is not None, "a real fault must still be diagnosed"
    assert "transport is wedged" in errors[0].getMessage()
    assert session.connected is False


@pytest.mark.asyncio
async def test_send_data_to_client_reports_the_write_result(caplog):
    """The port write path sees False for a dead peer, True for a live one, and stays quiet."""
    adapter = make_adapter()
    add(adapter, "dead", DeadWriter(BrokenPipeError(32, "Broken pipe")))
    keeper = add(adapter, "alive", GoodWriter())

    with caplog.at_level(logging.DEBUG, logger=adapter.logger.name):
        assert await adapter.send_data_to_client("dead", b"port bytes") is False
        assert await adapter.send_data_to_client("alive", b"port bytes") is True

    assert bytes(keeper.writer.buffer) == b"port bytes", "a viewer that is gone must not stop delivery to the others"
    loud = [r for r in records(caplog, adapter) if r.levelno >= logging.WARNING]
    assert [r.getMessage() for r in loud] == [], "a dead peer must not reach a caller as a failure"


@pytest.mark.asyncio
async def test_send_data_to_client_reports_an_unknown_client(caplog):
    """A client id the adapter does not know stays a WARNING, which is a routing fault not a peer fault."""
    adapter = make_adapter()

    with caplog.at_level(logging.DEBUG, logger=adapter.logger.name):
        assert await adapter.send_data_to_client("nobody", b"port bytes") is False

    loud = [(r.levelno, r.getMessage()) for r in records(caplog, adapter) if r.levelno >= logging.WARNING]
    assert loud == [(logging.WARNING, "Tried to send data to unknown client nobody")]


@pytest.mark.asyncio
async def test_control_frame_result_and_pending_mode(caplog):
    """A control frame that did not reach the socket returns False and does not record the mode."""
    adapter = make_adapter()
    dead = add(adapter, "dead", DeadWriter(ConnectionResetError(54, "Connection reset by peer")))

    with caplog.at_level(logging.DEBUG, logger=adapter.logger.name):
        sent = await adapter.send_control_frame_to_client("dead", {"type": "client_mode", "mode": "read-only"})

    assert sent is False
    assert dead.mode is None, "the server must not record a mode the client never received"

    alive = add(adapter, "alive", GoodWriter())
    assert await adapter.send_control_frame_to_client("alive", {"type": "client_mode", "mode": "read-only"}) is True
    assert alive.mode == "read-only"
    assert bytes(alive.writer.buffer).startswith(CTRL_MARKER)
    assert [r.getMessage() for r in records(caplog, adapter) if r.levelno >= logging.WARNING] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error", PEER_ERRORS, ids=lambda e: type(e).__name__)
async def test_teardown_after_a_failed_send_stays_quiet(error, caplog):
    """Ending the session after a dead-peer write releases the port and logs nothing loud."""
    adapter = make_adapter()
    add(adapter, "c1", ClosedDeadWriter(error))

    with caplog.at_level(logging.DEBUG, logger=adapter.logger.name):
        assert await adapter.send_data_to_client("c1", b"last words") is False
        await adapter.disconnect_client("c1")

    loud = [r for r in records(caplog, adapter) if r.levelno >= logging.WARNING]
    assert [(r.levelno, r.getMessage()) for r in loud] == []
    assert "c1" not in adapter.clients
    assert "loop1" not in adapter.port_clients, "the seat must be released with the socket"
