import asyncio
import json
from typing import Any, Dict, List, Optional, Tuple, cast

import pytest

from openmux.server.adapters.client_listener import ClientSession, TcpServerAdapter


class FakeReader:
    def __init__(self, chunks: Optional[List[bytes]] = None):
        self.chunks: List[bytes] = list(chunks or [])

    async def read(self, n: int) -> bytes:  # pragma: no cover (covered in tests)
        if not self.chunks:
            await asyncio.sleep(0)
            return b""
        data = self.chunks.pop(0)
        # Respect 1-byte reads crudely
        if n == 1 and len(data) > 1:
            # push back remainder
            self.chunks.insert(0, data[1:])
            return data[:1]
        return data


class FakeWriter:
    def __init__(self, peer: Tuple[str, int] = ("127.0.0.1", 55555)):
        self.buffer = bytearray()
        self.closed = False
        self._closing = False
        self._peer = peer
        self._wrote_eof = False

    def get_extra_info(self, name: str):
        if name == "peername":
            return self._peer
        return None

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def is_closing(self) -> bool:
        return self._closing

    def write_eof(self) -> None:
        self._wrote_eof = True

    def close(self) -> None:
        self._closing = True
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


def make_line_chars(s: str) -> List[bytes]:
    return [c.encode() for c in (s + "\n")]


def parse_last_list_payload(writer: FakeWriter) -> Dict[str, Any]:
    # Find last line starting with LIST:
    lines = writer.buffer.decode().splitlines()
    for line in reversed(lines):
        if line.startswith("LIST:"):
            payload = json.loads(line[len("LIST:") :])
            return payload
    raise AssertionError("LIST payload not found")


def test_validate_config_ok_and_bad():
    ok = {"client_listener": {"host": "0.0.0.0", "port": 1234}}
    bad1 = {}
    bad2 = {"client_listener": {"host": "0.0.0.0"}}
    bad3 = {"client_listener": {"host": "0.0.0.0", "port": 70000}}
    assert TcpServerAdapter.validate_config(ok) is True
    assert TcpServerAdapter.validate_config(bad1) is False
    # Missing port should be accepted (defaults apply)
    assert TcpServerAdapter.validate_config(bad2) is True
    assert TcpServerAdapter.validate_config(bad3) is False


@pytest.mark.asyncio
async def test_handle_client_protocol_anonymous_then_quit():
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    # No auth_manager -> anonymous allowed
    reader = FakeReader(make_line_chars("QUIT"))
    writer = FakeWriter()
    client = ClientSession("c1", "127.0.0.1", cast(Any, reader), cast(Any, writer), adapter.logger)

    await adapter.handle_client_protocol(client)
    out = writer.buffer.decode()
    assert "Authentication required" in out
    assert "AUTH:SUCCESS:Welcome anonymous" in out


@pytest.mark.asyncio
async def test_auth_hmac_success_then_quit(monkeypatch):
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    # Build input: HMAC auth then response, then QUIT for command phase
    reader = FakeReader(
        make_line_chars("AUTH:USER:HMAC:alice") + make_line_chars("AUTH:RESPONSE:deadb64") + make_line_chars("QUIT")
    )
    writer = FakeWriter()
    client = ClientSession("c1", "127.0.0.1", cast(Any, reader), cast(Any, writer), adapter.logger)

    class AM:
        def is_user_locked(self, u, ip):
            return False

        def start_password_hmac_challenge(self, username):
            return "nonceB64"

        def verify_password_hmac(self, username, hmac_b64, src_ip=None):
            return True

    adapter.set_auth_manager(AM())
    await adapter.handle_client_protocol(client)
    out = writer.buffer.decode()
    assert "AUTH:SUCCESS:Welcome alice" in out


@pytest.mark.asyncio
async def test_auth_pubkey_failure():
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    reader = FakeReader(make_line_chars("AUTH:PK:INIT:alice:key1") + make_line_chars("AUTH:PK:RESPONSE:key1:abcd"))
    writer = FakeWriter()
    client = ClientSession("c1", "127.0.0.1", cast(Any, reader), cast(Any, writer), adapter.logger)

    class AM:
        def start_pubkey_challenge(self, username, key_id):
            return {"key_id": "key1", "nonce": "xyz"}

        def verify_pubkey_response(self, username, key_id, sig):
            return False

    adapter.set_auth_manager(AM())
    await adapter.handle_client_protocol(client)
    out = writer.buffer.decode()
    assert "AUTH:FAILED:Authentication failed" in out


@pytest.mark.asyncio
async def test_handle_list_ports_success_and_timeout(monkeypatch):
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    writer = FakeWriter()
    client = ClientSession("c1", "127.0.0.1", cast(Any, FakeReader([])), cast(Any, writer), adapter.logger)

    class PM1:
        async def get_port_list_with_federation(self):
            return [{"name": "p1"}]

    adapter.console_manager = type("CM", (), {"port_manager": PM1()})()
    await adapter.handle_list_ports_request(client)
    payload = parse_last_list_payload(writer)
    assert payload["count"] == 1 and payload["ports"][0]["name"] == "p1"
    assert payload["timed_out"] is False

    # Timeout fallback path: slow PM
    class SlowPM:
        async def get_port_list_with_federation(self):
            await asyncio.sleep(1.1)
            return []

        # Fallback snapshot source
        def __init__(self):
            class Port:
                def get_status(self):
                    return {"name": "snap"}

            self.ports = {"snap": Port()}

    writer2 = FakeWriter()
    client2 = ClientSession("c2", "127.0.0.1", cast(Any, FakeReader([])), cast(Any, writer2), adapter.logger)
    adapter.console_manager = type("CM", (), {"port_manager": SlowPM()})()
    await adapter.handle_list_ports_request(client2)
    payload2 = parse_last_list_payload(writer2)
    assert payload2["timed_out"] is True and payload2["count"] == 1


@pytest.mark.asyncio
async def test_connect_disconnect_and_forwarding():
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    writer = FakeWriter()
    client = ClientSession("c1", "127.0.0.1", cast(Any, FakeReader([])), cast(Any, writer), adapter.logger)
    client.username = "u"

    class CM:
        def __init__(self):
            self.connected = []
            self.disconnected = []
            self.channels = []
            self.unreg = []
            self.port_manager = type("PM", (), {"write_to_port": self.write_to_port})()

        async def connect_client_to_port(self, cid, port, user):
            self.connected.append((cid, port, user))
            return True, "read-write", None

        async def disconnect_client_from_port(self, cid, port):
            self.disconnected.append((cid, port))

        def register_client_channel(self, cid, adapter_ref):
            self.channels.append(cid)

        def unregister_client_channel(self, cid):
            self.unreg.append(cid)

        async def write_to_port(self, pname, data, cid):
            self.last_write = (pname, data, cid)
            return True

    cm = CM()
    adapter.set_console_manager(cm)
    # Connect
    await adapter.handle_port_connection_request_text(client, "p1")
    assert client.connected_port == "p1"
    assert adapter.port_clients["p1"] == ["c1"]
    assert b"CONNECTED:p1:READ_WRITE" in writer.buffer
    # Forward a character and bytes
    await adapter.forward_character_to_port(client, b"A")
    await adapter.forward_bytes_to_port(client, b"BC")
    assert cm.last_write == ("p1", b"BC", "c1")
    # Disconnect
    await adapter.disconnect_client_from_port(client)
    assert client.connected_port is None
    assert "p1" not in adapter.port_clients
    assert cm.disconnected == [("c1", "p1")]


@pytest.mark.asyncio
async def test_handle_client_protocol_malformed_auth_lines():
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    # Provide a dummy auth_manager to avoid anonymous bypass
    adapter.set_auth_manager(object())

    # 1) Malformed PK INIT (missing username)
    w1 = FakeWriter()
    c1 = ClientSession(
        "c1", "127.0.0.1", cast(Any, FakeReader(make_line_chars("AUTH:PK:INIT"))), cast(Any, w1), adapter.logger
    )
    await adapter.handle_client_protocol(c1)
    out1 = w1.buffer.decode()
    assert "Authentication required" in out1
    assert "AUTH:FAILED:Authentication failed" in out1

    # 2) Malformed HMAC (missing username)
    w2 = FakeWriter()
    c2 = ClientSession(
        "c2", "127.0.0.1", cast(Any, FakeReader(make_line_chars("AUTH:USER:HMAC:"))), cast(Any, w2), adapter.logger
    )
    await adapter.handle_client_protocol(c2)
    out2 = w2.buffer.decode()
    assert "AUTH:FAILED:Authentication failed" in out2

    # 3) Plaintext disabled path (explicit disabled message + final failure)
    class AM:
        def is_user_locked(self, u, ip):
            return False

    adapter.set_auth_manager(AM())
    w3 = FakeWriter()
    c3 = ClientSession(
        "c3", "127.0.0.1", cast(Any, FakeReader(make_line_chars("AUTH:USER:alice"))), cast(Any, w3), adapter.logger
    )
    await adapter.handle_client_protocol(c3)
    out3 = w3.buffer.decode()
    assert "AUTH:FAILED:Plaintext password auth disabled" in out3
    assert "AUTH:FAILED:Authentication failed" in out3

    # 4) Unknown prefix
    w4 = FakeWriter()
    c4 = ClientSession("c4", "127.0.0.1", cast(Any, FakeReader(make_line_chars("HELLO"))), cast(Any, w4), adapter.logger)
    await adapter.handle_client_protocol(c4)
    out4 = w4.buffer.decode()
    assert "AUTH:FAILED:Authentication failed" in out4


@pytest.mark.asyncio
async def test_process_client_command_forwards_unknown_when_attached():
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    w = FakeWriter()
    c = ClientSession("c1", "127.0.0.1", cast(Any, FakeReader([])), cast(Any, w), adapter.logger)
    c.connected_port = "pX"

    class CM:
        def __init__(self):
            self.last = None

            class PM:
                def __init__(self, outer):
                    self._outer = outer

                async def write_to_port(self, pname, data, cid):
                    self._outer.last = (pname, data, cid)
                    return True

            self.port_manager = PM(self)

    cm = CM()
    adapter.set_console_manager(cm)

    await adapter.process_client_command(c, "FOO")
    # Ensure it forwarded with newline
    assert cm.last == ("pX", b"FOO\n", "c1")
    # No error should be sent to client
    assert b"ERROR:Unknown command" not in w.buffer


@pytest.mark.asyncio
async def test_forward_data_to_port_loopback_echo_and_errors():
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    writer = FakeWriter()
    client = ClientSession("c1", "127.0.0.1", cast(Any, FakeReader([])), cast(Any, writer), adapter.logger)
    client.connected_port = "loop0"

    class CM:
        def __init__(self, ok: bool):
            class PM:
                def __init__(self, okv: bool):
                    self._ok = okv

                async def write_to_port(self, pname, data, cid):
                    return self._ok

            self.port_manager = PM(ok)

    # Success path: echoes non-newline bytes immediately
    adapter.console_manager = CM(ok=True)
    await adapter.forward_data_to_port(client, b"ab\n")
    # Expect two echoed bytes (a and b) and no error
    assert writer.buffer.count(b"a") == 1 and writer.buffer.count(b"b") == 1

    # Failure path: write_to_port returns False -> send error line
    writer2 = FakeWriter()
    client2 = ClientSession("c2", "127.0.0.1", cast(Any, FakeReader([])), cast(Any, writer2), adapter.logger)
    client2.connected_port = "loop0"
    adapter.console_manager = CM(ok=False)
    await adapter.forward_data_to_port(client2, b"z")
    assert b"ERROR:Failed to write to port" in writer2.buffer


@pytest.mark.asyncio
async def test_send_data_to_client_unknown_and_ok():
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    ok = await adapter.send_data_to_client("nope", b"x")
    assert ok is False
    # Add a client
    w = FakeWriter()
    c = ClientSession("c1", "127.0.0.1", cast(Any, FakeReader([])), cast(Any, w), adapter.logger)
    adapter.clients["c1"] = c
    ok2 = await adapter.send_data_to_client("c1", b"xyz")
    assert ok2 is True
    assert b"xyz" in w.buffer


@pytest.mark.asyncio
async def test_destroy_port_disconnects_clients():
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    writer = FakeWriter()
    client = ClientSession("c1", "127.0.0.1", cast(Any, FakeReader([])), cast(Any, writer), adapter.logger)
    adapter.clients["c1"] = client
    adapter.port_clients["p1"] = ["c1"]

    # Minimal console manager for disconnect
    class CM:
        async def disconnect_client_from_port(self, cid, port):
            return None

        def unregister_client_channel(self, cid):
            return None

    adapter.set_console_manager(CM())
    await adapter.destroy_port("p1")
    assert client.connected_port is None


@pytest.mark.asyncio
async def test_server_start_and_stop(monkeypatch):
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})

    class FakeServer:
        def __init__(self):
            self.started = False
            self.closed = False

        async def start_serving(self):
            self.started = True

        def close(self):
            self.closed = True

        async def wait_closed(self):
            await asyncio.sleep(0)

    async def fake_start_server(cb, host, port):  # type: ignore[override]
        return FakeServer()

    monkeypatch.setattr(asyncio, "start_server", fake_start_server)
    ok = await adapter.start()
    assert ok is True and adapter.is_running is True
    await adapter.stop()
    assert adapter.is_running is False


@pytest.mark.asyncio
async def test_handle_client_connection_lifecycle(monkeypatch):
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    # Supply a QUIT line so protocol ends quickly
    reader = FakeReader(make_line_chars("QUIT"))
    writer = FakeWriter()
    await adapter.handle_client_connection(cast(Any, reader), cast(Any, writer))
    # Client should be disconnected and removed
    assert not adapter.clients


# ---------------------------------------------------------------------------
# Access-mode control frames: holder labels + targeted takeover (issue #61)


class _TakeCm:
    """Console-manager stand-in that records take_write_slot calls and
    serves a fixed holder list / port capacity for the label paths."""

    def __init__(self, port_clients, take_result=(True, "ok")):
        self.take_calls = []
        self.take_result = take_result
        self.holder_list = port_clients
        self.client_to_manager: Any = {}
        self.port_manager = _TakePm(port_clients)

    async def take_write_slot(self, taker_id, port_name, target=None):
        self.take_calls.append((taker_id, port_name, target))
        return self.take_result


class _TakePm:
    def __init__(self, port_clients):
        self.ports = {"p1": type("P", (), {"connected_clients": port_clients, "max_read_write_users": "one"})()}


async def _make_attached_client(adapter: TcpServerAdapter, writer: FakeWriter) -> ClientSession:
    class _Cm:
        port_manager = _TakePm([])
        client_to_manager = {}

        async def connect_client_to_port(self, cid, port, user):
            return True, "read-write", None

    adapter.set_console_manager(_Cm())
    client = ClientSession("c1", "127.0.0.1", cast(Any, FakeReader([])), cast(Any, writer), adapter.logger)
    client.username = "u"
    await adapter.handle_port_connection_request_text(client, "p1")
    assert client.connected_port == "p1"
    return client


from openmux.server.adapters.client_listener import CTRL_MARKER


def _ctrl_frame(payload: Dict[str, Any]) -> bytes:
    return CTRL_MARKER + json.dumps(payload).encode() + b"\n"


def _last_ctrl_json(writer: FakeWriter) -> Dict[str, Any]:
    """Parse the last OMXCTRL response line written to `writer`."""
    data = bytes(writer.buffer)
    idx = data.rfind(CTRL_MARKER)
    assert idx != -1, f"no control frame in {data!r}"
    line = data[idx + len(CTRL_MARKER) :].split(b"\n", 1)[0]
    return json.loads(line)


@pytest.mark.asyncio
async def test_force_promote_frame_defaults_to_no_target():
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    writer = FakeWriter()
    client = await _make_attached_client(adapter, writer)
    cm = _TakeCm([], take_result=(True, "ok"))
    adapter.set_console_manager(cm)

    handled = await adapter._handle_control_frame(client, _ctrl_frame({"type": "force_promote"}))

    assert handled is True
    assert cm.take_calls == [("c1", "p1", None)]
    resp = _last_ctrl_json(writer)
    assert resp["type"] == "client_mode"
    assert resp["ok"] is True
    assert resp.get("takeover") is None


@pytest.mark.asyncio
async def test_force_promote_frame_passes_target_and_surfaces_victim():
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    writer = FakeWriter()
    client = await _make_attached_client(adapter, writer)
    cm = _TakeCm([], take_result=(True, "takeover from alice [A]"))
    adapter.set_console_manager(cm)

    handled = await adapter._handle_control_frame(client, _ctrl_frame({"type": "force_promote", "client_id": "A"}))

    assert handled is True
    assert cm.take_calls == [("c1", "p1", "A")]
    resp = _last_ctrl_json(writer)
    assert resp["ok"] is True
    # The victim's label rides along; the reason is carried too.
    assert resp["takeover"] == "alice [A]"
    assert resp["reason"].startswith("takeover from ")


@pytest.mark.asyncio
async def test_force_promote_frame_non_string_client_id_is_no_target():
    # A numeric client_id is not a holder id: treat as no-target fallback.
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    writer = FakeWriter()
    client = await _make_attached_client(adapter, writer)
    cm = _TakeCm([])
    adapter.set_console_manager(cm)

    await adapter._handle_control_frame(client, _ctrl_frame({"type": "force_promote", "client_id": 42}))

    assert cm.take_calls == [("c1", "p1", None)]


def test_rw_holders_label_format_and_id_shortening():
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    clients = [
        {"client_id": "0123456789abcdef", "username": "alice", "mode": "read-write"},
        {"client_id": "fed:peerA:3", "username": "peerAuser", "mode": "read-write"},
        {"client_id": "ro1", "username": "carol", "mode": "read-only"},  # excluded
    ]
    adapter.set_console_manager(_TakeCm(clients))

    labels = adapter._rw_holders_for_port("p1")

    # Long local id shortened; federated id kept verbatim; read-only excluded.
    assert labels == ["[89abcdef] alice@unknown (rw)", "[fed:peerA:3] peerAuser@unknown (rw)"]


@pytest.mark.asyncio
async def test_client_mode_denial_carries_holder_labels():
    # A request_rw denial lists the current holders with their take-target
    # labels (issue #61): "[id] username@ip (rw)".
    clients = [{"client_id": "A", "username": "alice", "mode": "read-write"}]
    adapter = _TakeAdapter(clients)
    writer = FakeWriter()
    client = ClientSession("c9", "127.0.0.1", cast(Any, FakeReader([])), cast(Any, writer), adapter.adapter.logger)
    client.username = "u"
    client.connected_port = "p1"

    await adapter.adapter._handle_control_frame(client, _ctrl_frame({"type": "request_rw"}))

    resp = _last_ctrl_json(writer)
    assert resp["type"] == "client_mode"
    assert resp["ok"] is False
    assert resp["rw_holders"] == ["[A] alice@unknown (rw)"]


@pytest.mark.asyncio
async def test_force_promote_refusal_carries_reason_not_takeover():
    # A refused take (invalid_target): the frame carries the reason so the
    # client can show a specific refusal, and no takeover field.
    adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
    writer = FakeWriter()
    client = ClientSession("c1", "127.0.0.1", cast(Any, FakeReader([])), cast(Any, writer), adapter.logger)
    client.connected_port = "p1"
    adapter.set_console_manager(_TakeCm([], take_result=(False, "invalid_target")))

    handled = await adapter._handle_control_frame(client, _ctrl_frame({"type": "force_promote", "client_id": "ghost"}))

    assert handled is True
    resp = _last_ctrl_json(writer)
    assert resp["ok"] is False
    assert resp["reason"] == "invalid_target"
    assert "takeover" not in resp


class _TakeAdapter:
    """A TcpServerAdapter with a console manager wired for holder labels."""

    def __init__(self, clients):
        self.adapter = TcpServerAdapter("cli", {"client_listener": {"host": "127.0.0.1", "port": 0}})
        cm = _TakeCm(clients)

        async def _promote(self, cid, port_name):
            return False  # the denial branch attaches the holder list

        cm.promote_client_to_read_write = _promote.__get__(cm, type(cm))
        self.adapter.set_console_manager(cm)
