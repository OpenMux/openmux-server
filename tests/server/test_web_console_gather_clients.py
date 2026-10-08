"""Tests for WebConsoleAdapter._gather_web_clients.

Covers the pre-refactor behavior contract for the C901 17-bracket:
port connected_clients enumeration (dict and object forms), dedup against
the websocket _client_meta fallback, and login-session inclusion.
"""

from unittest.mock import MagicMock

from openmux.server.web_console import WebConsoleAdapter


def _adapter():
    a = WebConsoleAdapter("wc", {})
    a._resolve_client_meta = lambda cid: {"type": "tcp", "ip": "10.0.0.1"}
    return a


def _port(clients):
    p = MagicMock()
    p.connected_clients = clients
    return p


def test_port_dict_clients_are_included():
    a = _adapter()
    a._sessions = {}
    a._client_meta = {}
    a.console_manager = MagicMock()
    a.console_manager.port_manager = MagicMock()
    a.console_manager.port_manager.ports = {
        "p1": _port(
            [
                {"client_id": "c1", "username": "alice"},
                {"client_id": "c2", "username": "bob"},
            ]
        )
    }
    out = a._gather_web_clients()
    assert [e["client_id"] for e in out] == ["c1", "c2"]
    first = out[0]
    assert first["username"] == "alice"
    assert first["port"] == "p1"
    assert first["type"] == "tcp"
    assert first["ip"] == "10.0.0.1"


def test_port_object_clients_are_included():
    a = _adapter()
    a._sessions = {}
    a._client_meta = {}
    obj = MagicMock()
    obj.client_id = "obj1"
    obj.username = "carol"
    a.console_manager = MagicMock()
    a.console_manager.port_manager = MagicMock()
    a.console_manager.port_manager.ports = {"p1": _port([obj])}
    out = a._gather_web_clients()
    assert out[0]["client_id"] == "obj1"
    assert out[0]["username"] == "carol"


def test_client_meta_fallback_adds_only_unseen_bound_clients():
    a = _adapter()
    a._sessions = {}
    a._client_meta = {
        "c1": {"port": "p1", "username": "dup"},  # already listed from the port
        "ws1": {"port": "p9", "username": "ws-user"},  # new
        "ws2": {"username": "no-port"},  # not bound -> skipped
        "ws3": "not-a-dict",  # bad shape -> skipped
    }
    a.console_manager = MagicMock()
    a.console_manager.port_manager = MagicMock()
    a.console_manager.port_manager.ports = {"p1": _port([{"client_id": "c1", "username": "alice"}])}
    out = a._gather_web_clients()
    ids = [e["client_id"] for e in out]
    assert ids == ["c1", "ws1"]
    ws1 = out[1]
    assert ws1["type"] == "websocket"  # default when meta has none


def test_sessions_included_with_shortened_id():
    a = _adapter()
    a._client_meta = {}
    a._sessions = {"abc-very-long-session-id": {"username": "dave", "ip": "10.0.0.2"}}
    a.console_manager = None
    out = a._gather_web_clients()
    assert len(out) == 1
    e = out[0]
    assert e["client_id"] == "session:abc-very"
    assert e["type"] == "session"
    assert e["username"] == "dave"


def test_no_port_manager_still_lists_sessions():
    a = _adapter()
    a._client_meta = {"ws1": {"port": "p9", "username": "ws-user"}}
    a._sessions = {}
    a.console_manager = None
    out = a._gather_web_clients()
    assert [e["client_id"] for e in out] == ["ws1"]
