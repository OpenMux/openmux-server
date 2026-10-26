"""Issue #98: one Access decision must reach every terminal with the server's wording.

The tests in this file drive the real ``ConsoleManager`` and the real TCP client-listener
session. They assert on the bytes each terminal receives, so a surface that rebuilds a
sentence from a code, or drops the wording, fails here. See
``docs/design/architecture-review-2026-10-25.md`` section 2.
"""

import asyncio
import json
from typing import Any, Dict, List, cast

import pytest

from openmux.client.adapters.rw_control import apply_client_mode_response
from openmux.server.access_decision import Code, Mode
from openmux.server.adapters.client_listener import CTRL_MARKER, ClientSession, TcpServerAdapter
from openmux.server.console_manager import ConsoleManager
from tests.server.test_console_manager import FakeAuthManager, FakePort, FakePortManager


class FakeReader:
    """``asyncio.StreamReader`` stand-in that never returns data on its own."""

    async def read(self, n: int) -> bytes:
        await asyncio.sleep(3600)
        return b""

    async def readline(self) -> bytes:
        await asyncio.sleep(3600)
        return b""


class RecordingWriter:
    """``asyncio.StreamWriter`` stand-in recording every byte written to one terminal."""

    def __init__(self, ip: str = "203.0.113.7"):
        self.buffer = bytearray()
        self.ip = ip
        self.closed = False

    def get_extra_info(self, name: str):
        return (self.ip, 40000) if name == "peername" else None

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def is_closing(self) -> bool:
        return self.closed

    def write_eof(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)

    # --- assertions helpers used by the tests below -------------------------------
    def text(self) -> str:
        return self.buffer.decode("utf-8", "replace")

    def lines(self) -> List[str]:
        return [line for line in self.text().splitlines() if line.startswith("[")]

    def frames(self) -> List[Dict[str, Any]]:
        out = []
        for line in self.buffer.split(b"\n"):
            if line.startswith(CTRL_MARKER):
                out.append(json.loads(line[len(CTRL_MARKER) :]))
        return out


def make_adapter(console_manager: ConsoleManager) -> TcpServerAdapter:
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    adapter.console_manager = console_manager
    return adapter


def make_session(adapter: TcpServerAdapter, client_id: str, writer: RecordingWriter) -> ClientSession:
    session = ClientSession(client_id, writer.ip, cast(Any, FakeReader()), cast(Any, writer), adapter.logger)
    # The adapter delivers notices through `clients`, exactly as a connected socket would.
    adapter.clients[client_id] = session
    return session


class _World:
    """Real ConsoleManager over a capacity-one Port, plus two connected CLI terminals."""

    def __init__(self):
        self.port_manager = _WritingPortManager()
        self.port_manager.ports["p1"] = FakePort(mode="one")
        self.console_manager = ConsoleManager(self.port_manager, FakeAuthManager())
        self.adapter = make_adapter(self.console_manager)
        self.alice_writer = RecordingWriter(ip="203.0.113.1")
        self.bob_writer = RecordingWriter(ip="203.0.113.2")
        self.alice = make_session(self.adapter, "client-alice-0001", self.alice_writer)
        self.bob = make_session(self.adapter, "client-bob-0002", self.bob_writer)
        self.console_manager.register_client_manager(self.adapter)
        # A real owning adapter answers the ConsoleManager's address lookup, so a
        # takeover notice names the taker the way a holders list names them.
        self.adapter._resolve_client_meta = self._resolve_client_meta

    def _resolve_client_meta(self, client_id: str) -> Dict[str, Any]:
        """Answer the ConsoleManager's IP lookup, as a real owning adapter does."""
        writer = {"client-alice-0001": self.alice_writer, "client-bob-0002": self.bob_writer}.get(client_id)
        peer = writer.get_extra_info("peername") if writer is not None else None
        return {"type": "client", "ip": peer[0] if peer else "unknown"}

    async def attach(self, session: ClientSession, username: str, mode: str = "read-write") -> None:
        """Attach one terminal the way the listener does, and map it to its adapter.

        ``mode`` is the standing the Port record starts on: a takeover test needs a holder,
        and the terminal under test has to start without the Write slot.
        """
        session.username = username
        self.port_manager.ports["p1"].connected_clients.append(
            {"client_id": session.client_id, "username": username, "mode": mode}
        )
        self.console_manager.register_client_channel(session.client_id, self.adapter)
        self.console_manager.register_client_port(session.client_id, "p1")

    async def ask_for_write(self, session: ClientSession) -> Dict[str, Any]:
        """Answer Take control the way the listener does, then render it the way the CLI does.

        The listener writes one control frame; the openmux CLI turns that frame into terminal
        lines. Both halves here are the real ones, so the wire contract and the shared
        renderer are covered at once.
        """
        frame = await self.adapter._ctrl_force_reply(cast(Any, session), "p1", {})
        await session.send_raw_data(CTRL_MARKER + json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n")
        session.writer.write(apply_client_mode_response(_CliState(), frame).encode("utf-8"))
        return frame


class _CliState:
    """The few adapter fields ``apply_client_mode_response`` updates."""

    def __init__(self):
        self.access_mode = "read-only"
        self.rw_holders: List[str] = []
        self.max_rw_users: Any = None


class _WritingPortManager(FakePortManager):
    """``FakePortManager`` plus the attach call ``ConsoleManager`` makes."""

    async def add_client_to_port(self, port_name: str, client_id: str, username: str, mode: str) -> bool:
        port = self.ports.get(port_name)
        if port is None:
            return False
        port.connected_clients.append({"client_id": client_id, "username": username, "mode": mode})
        return True

    async def remove_client_from_port(self, port_name: str, client_id: str) -> bool:
        port = self.ports.get(port_name)
        if port is None:
            return False
        port.connected_clients = [c for c in port.connected_clients if c.get("client_id") != client_id]
        return True


@pytest.fixture
def world() -> _World:
    return _World()


@pytest.mark.asyncio
async def test_a_free_write_slot_is_answered_with_the_servers_line(world: _World):
    """A viewer who takes a free Port gets the server's grant line on its own terminal."""
    await world.attach(world.alice, "alice", mode="read-only")

    frame = await world.ask_for_write(world.alice)

    assert frame["code"] == Code.GRANTED.value
    assert frame["mode"] == Mode.READ_WRITE.value
    assert world.alice.writer.lines() == ["[Read-write access granted]"]


@pytest.mark.asyncio
async def test_a_takeover_words_both_terminals_from_one_holder_label(world: _World):
    """The demoted terminal and the taking terminal quote the same holder.

    The ConsoleManager builds both notices, so the label inside "Taken from: ..." and the
    name inside "taken by ..." cannot drift apart.
    """
    await world.attach(world.alice, "alice", mode="read-write")
    await world.attach(world.bob, "bob", mode="read-only")

    frame = await world.ask_for_write(world.bob)

    # The holder lives in the Port record, which carries the client's own address; the
    # ConsoleManager resolves the IP through the owning adapter (`_resolve_client_meta`).
    assert frame["lines"][0] == "[Read-write access granted]"
    assert frame["lines"][1].startswith("[Taken from: [ice-0001] alice@"), frame["lines"]
    demoted = world.alice.writer.frames()[-1]
    assert demoted["code"] == Code.DEMOTED.value
    assert demoted["lines"] == ["[Your read-write access was taken by bob@203.0.113.2]"]
    assert "alice@" in frame["lines"][1] and demoted["lines"][0].startswith("[Your read-write access was taken by bob")
    # No terminal receives a field for a console to decode.
    for session in (world.alice, world.bob):
        for sent in session.writer.frames():
            assert "reason" not in sent and "taken_by" not in sent and "takeover" not in sent


@pytest.mark.asyncio
async def test_a_port_without_a_write_slot_says_so_instead_of_being_busy(world: _World):
    """A Refusal on a Port with no Write slot never claims the seat is merely busy."""
    world.port_manager.ports["p1"] = FakePort(mode="none")
    await world.attach(world.alice, "alice", mode="read-only")

    frame = await world.ask_for_write(world.alice)

    assert frame["ok"] is False
    assert frame["mode"] == Mode.READ_ONLY.value
    assert frame["max_rw_users"] == 0
    assert world.alice_writer.lines() == [
        "[Read-write is not available on this port]",
        "[This port has no write slots (capacity: none)]",
    ]
