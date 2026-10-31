"""Tests for WebConsoleAdapter._gather_web_clients.

Covers the pre-refactor behavior contract for the C901 17-bracket:
port connected_clients enumeration (dict and object forms), dedup against
the websocket _client_meta fallback, and login-session inclusion.

Also covers how that list names each console's transport: the adapter that
accepted the connection is asked first, and the rendered Clients table writes
what the server knows instead of a default that a Telnet console would wear as
a websocket.
"""

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

from openmux.server.web_console import WebConsoleAdapter, _assemble_status_payload


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


# --- what names the transport a console arrived on ----------------------------------------


class _Listener:
    """A listener adapter that answers what the web console asks its adapters: how did this client arrive?"""

    def __init__(self, answer: Dict[str, Any], adapter_type: str = "telnet_listener"):
        self._answer = answer
        self._adapter_type = adapter_type

    def get_adapter_type(self) -> str:
        return self._adapter_type

    def _resolve_client_meta(self, client_id: str) -> Dict[str, Any]:
        return dict(self._answer)


class _TcpListener:
    """A CLI listener: it has sessions with an address, and no answer to the resolver question."""

    def __init__(self, clients: Dict[str, Any]):
        self.clients = clients

    def get_adapter_type(self) -> str:
        return "client_listener"


def _meta_adapter(owning: Dict[str, Any]) -> WebConsoleAdapter:
    """A web console with no registry of its own, plus the adapters that own the consoles under test."""
    adapter = WebConsoleAdapter("wc", {})
    adapter._client_meta = {}
    adapter._sessions = {}
    adapter.console_manager = SimpleNamespace(client_to_manager=dict(owning), port_manager=None)
    return adapter


def test_the_adapter_that_owns_a_console_names_its_transport():
    """A Telnet console is listed as telnet, with the peer address the Telnet listener holds.

    The listener mints that client id itself, so the web console never saw the connection and can only ask.
    Before this hop the id matched no rule and the row fell through to the websocket guess, with no address.
    """
    listener = _Listener({"type": "telnet", "ip": "10.9.8.7", "username": "ro"})
    adapter = _meta_adapter({"telnet:telnet1:aaaa": listener})
    meta = adapter._resolve_client_meta("telnet:telnet1:aaaa")
    assert meta["type"] == "telnet", meta
    assert meta["ip"] == "10.9.8.7", meta


def test_a_console_on_a_tcp_manager_is_still_named_by_its_session():
    """The CLI listener carries no resolver, so the session address path still has to name that console."""
    session = SimpleNamespace(address="10.1.2.3")
    adapter = _meta_adapter({"c-1": _TcpListener({"c-1": session})})
    meta = adapter._resolve_client_meta("c-1")
    assert meta["type"] == "tcp", meta
    assert meta["ip"] == "10.1.2.3", meta


def test_this_adapter_is_never_asked_about_its_own_console():
    """The web console answers for its own consoles from its own registry, never by asking itself.

    ``_meta_from_manager`` runs inside ``_resolve_client_meta``, so a hop back to this adapter would call
    straight into that method again. The stub resolver below fails the test if it is ever reached.
    """
    adapter = _meta_adapter({})
    calls: List[str] = []

    def spy(client_id: str) -> Dict[str, Any]:
        calls.append(client_id)
        return {"type": "stubbed"}

    adapter._resolve_client_meta = spy  # type: ignore[method-assign]
    meta: Dict[str, Any] = {}
    assert adapter._meta_from_owning_adapter(adapter, "ws:42", meta) is False
    assert calls == [], "the owning-adapter hop must not call this adapter's own resolver"


def test_a_console_no_adapter_claims_has_no_type_for_the_page_to_fill_in():
    """An id no adapter owns and that does not look like a websocket gets no type at all.

    The page renders such a row as ``unknown``, which is why the server must not return a guess here.
    """
    adapter = _meta_adapter({})
    assert adapter._resolve_client_meta("remote:7")["type"] is None
    assert adapter._resolve_client_meta("ws:42")["type"] == "websocket"


def test_rendered_clients_table_never_invents_a_transport():
    """The HTML a browser receives names each transport it was given, and says ``unknown`` once.

    Asserted on rendered markup because a template default reads correctly in the template source while
    showing every Telnet console in the room as a websocket.
    """
    adapter = WebConsoleAdapter("wc", {"web_console": {}})
    adapter._prepare_templates()
    assert adapter._jinja_env is not None, "the shipped template dir must build a Jinja env"
    payload = _assemble_status_payload(adapter, preloaded_ports=[])
    payload["web_clients"] = [
        {"client_id": "telnet:telnet1:aaaa", "username": "ro", "port": "loop1", "type": "telnet", "ip": "10.9.8.7"},
        {"client_id": "ws:42", "username": "admin", "port": "loop1", "type": None, "ip": None},
    ]
    payload["sort_key"], payload["sort_dir"] = "name", "asc"
    payload["sort_query"], payload["status_path"] = "", "/status"
    html = adapter._render_status(payload).decode("utf-8")
    assert "<td>telnet</td>" in html, html
    assert "<td>10.9.8.7</td>" in html, html
    assert "<td>unknown</td>" in html, html
    assert "<td>websocket</td>" not in html, "a type the server never named must not appear as a transport"
