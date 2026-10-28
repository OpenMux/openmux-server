"""A full reload carries the web console's login sessions to the console that replaces it (issue #104).

Two owners, one rule. `WebConsoleAdapter` holds the login sessions in its own instance and decides what may be
carried (`login_sessions_snapshot`, `adopt_login_sessions`). `OpenMuxServer` reads them before the stop phase
(`_capture_login_sessions`) and hands them to the recreated console while it wires dependencies
(`_reload_full_wire`).

The carry moves one fact only: a browser already proved who it is. Permission stays a live question for
`auth_manager`, so these tests pin both the carry and the limits that keep it from becoming a bypass.
"""

import textwrap
import time

import pytest
from aiohttp import web

from openmux.server.main import OpenMuxServer
from openmux.server.web_console import WebConsoleAdapter, _attach_session_user

SID = "sid-carried"
SESSION = {"username": "roger", "created": 0, "last_seen": 0, "ip": "10.0.0.7"}


class _FakeRequest(dict):
    """The two things a session reader touches: the cookies to read, and the request as a mapping."""

    def __init__(self, cookies):
        super().__init__()
        self.cookies = dict(cookies)


def _console(**cfg) -> WebConsoleAdapter:
    config = {"web_console": {"enable_ui": False, **cfg}}
    return WebConsoleAdapter("web_console", config)


def _signed_in_console() -> WebConsoleAdapter:
    adapter = _console()
    adapter._sessions[SID] = dict(SESSION, username="roger", created=time.time(), last_seen=time.time())
    return adapter


class _Auth:
    """Stand-in auth manager: one answer for every permission lookup."""

    def __init__(self, permissions=None):
        self._permissions = permissions

    def get_user_permissions(self, username):
        return self._permissions


# --- the receiving half: what may be carried -------------------------------------------------------


def test_snapshot_hands_over_a_copy_and_not_the_store():
    adapter = _signed_in_console()
    snapshot = adapter.login_sessions_snapshot()
    assert snapshot == {SID: adapter._sessions[SID]}
    snapshot[SID]["username"] = "someone-else"
    assert adapter._sessions[SID]["username"] == "roger", "the snapshot shared the live entry"


def test_an_adopted_session_serves_the_browser_that_holds_its_cookie():
    issuer = _signed_in_console()
    receiver = _console()
    assert receiver.adopt_login_sessions(issuer.login_sessions_snapshot(), issuer._session_cookie_name) == 1

    request = _FakeRequest({receiver._session_cookie_name: SID})
    assert _attach_session_user(request, receiver) is True
    assert request["username"] == "roger"


def test_a_console_that_never_adopted_refuses_the_same_cookie():
    # The same id is live where it was minted and refused by a console that never adopted it: the
    # carry is what makes a cookie work, not the cookie itself.
    issuer = _signed_in_console()
    stranger = _console()
    assert stranger._session_cookie_name == issuer._session_cookie_name

    def request():
        return _FakeRequest({issuer._session_cookie_name: SID})

    assert _attach_session_user(request(), issuer) is True
    refused = request()
    assert _attach_session_user(refused, stranger) is False
    assert "username" not in refused


def test_a_session_cookie_name_from_another_console_carries_nothing():
    """A reload that changes `base_path` cannot deliver a carry, because the browser sends the old cookie name."""
    issuer = _signed_in_console()
    receiver = _console(base_path="/rack2")
    assert receiver._session_cookie_name != issuer._session_cookie_name

    assert receiver.adopt_login_sessions(issuer.login_sessions_snapshot(), issuer._session_cookie_name) == 0
    assert receiver._sessions == {}


def test_a_session_past_its_ttl_is_not_adopted():
    receiver = _console(session_ttl_seconds=60)
    stale = {SID: dict(SESSION, created=0, last_seen=time.time() - 3600)}
    assert receiver.adopt_login_sessions(stale, receiver._session_cookie_name) == 0
    assert receiver._sessions == {}


def test_an_entry_without_an_identity_is_not_adopted():
    receiver = _console()
    assert receiver.adopt_login_sessions({SID: dict(SESSION, username="")}, receiver._session_cookie_name) == 0
    assert receiver._sessions == {}


def test_a_second_full_reload_carries_a_session_the_first_one_carried():
    # A reload after a reload is the normal case: an operator who reloads twice in a day signs nobody
    # out on the second one. The console that adopted a session snaps it shot again, so the chain
    # holds as long as each console passes what it holds.
    first = _signed_in_console()
    second = _console()
    assert second.adopt_login_sessions(first.login_sessions_snapshot(), first._session_cookie_name) == 1

    third = _console()
    assert third._session_cookie_name == second._session_cookie_name
    assert third.adopt_login_sessions(second.login_sessions_snapshot(), second._session_cookie_name) == 1

    request = _FakeRequest({third._session_cookie_name: SID})
    assert _attach_session_user(request, third) is True
    assert request["username"] == "roger"


# --- what the carry does not carry: permission stays a live question -------------------------------


def test_a_carried_identity_grants_nothing_the_live_check_refuses():
    issuer = _signed_in_console()
    receiver = _console()
    receiver.adopt_login_sessions(issuer.login_sessions_snapshot(), issuer._session_cookie_name)
    receiver.auth_manager = _Auth(permissions=None)  # the reload removed or demoted this account

    request = _FakeRequest({receiver._session_cookie_name: SID})
    assert _attach_session_user(request, receiver) is True, "the browser keeps its session"
    assert receiver._get_effective_permission(request["username"], request) is None
    with pytest.raises(web.HTTPForbidden):
        receiver._require_permission(request, allowed=("admin",))


def test_a_carried_identity_still_reaches_a_live_admin():
    issuer = _signed_in_console()
    receiver = _console()
    receiver.adopt_login_sessions(issuer.login_sessions_snapshot(), issuer._session_cookie_name)
    receiver.auth_manager = _Auth(permissions="admin")

    request = _FakeRequest({receiver._session_cookie_name: SID})
    _attach_session_user(request, receiver)
    assert receiver._require_permission(request, allowed=("admin",)) == "roger"


# --- the server half: capture before the stop, hand over while wiring ------------------------------


def _server(tmp_path) -> OpenMuxServer:
    (tmp_path / "authentication.yaml").write_text(
        textwrap.dedent(
            """
            users:
              - username: admin
                password_hash: 5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8
                permissions: admin
            """
        )
    )
    (tmp_path / "server.yaml").write_text(textwrap.dedent("server:\n  id: test\nlogging:\n  level: WARNING\n"))
    return OpenMuxServer(str(tmp_path / "server.yaml"), log_level="DEBUG")


class _Wired:
    """One adapter in the recreated list, recording what the wiring phase gave it."""

    def __init__(self, name="web_console"):
        self.name = name
        self.carried = []

    def get_adapter_type(self):
        return "web_console" if self.name == "web_console" else "loopback"

    def adopt_login_sessions(self, sessions, source_cookie_name=None):
        self.carried.append((source_cookie_name, sessions))
        return len(sessions)


class _PlainPort:
    """One recreated port adapter, which cannot read a web session cookie and must not be asked to."""

    def __init__(self, name="loopback_ports"):
        self.name = name

    def get_adapter_type(self):
        return "loopback"


def test_the_server_reads_the_sessions_off_the_console_it_is_about_to_replace(tmp_path):
    server = _server(tmp_path)
    assert server._capture_login_sessions() is None, "a server with no web console has nothing to carry"

    server.web_console = _console()
    assert server._capture_login_sessions() is None, "a signed-out console has nothing to carry"

    server.web_console = _console()
    server.web_console._sessions[SID] = dict(SESSION, last_seen=time.time())
    carried = server._capture_login_sessions()
    assert carried is not None and carried[0] == server.web_console._session_cookie_name
    assert list(carried[1]) == [SID]


def test_the_wiring_phase_hands_the_sessions_only_to_the_web_console(tmp_path):
    server = _server(tmp_path)
    console, port = _Wired("web_console"), _PlainPort()
    server.unified_adapters = [console, port]
    carried = ("omx_session", {SID: dict(SESSION)})

    server._reload_full_wire({"server": {}}, "test", {"errors": []}, carried)

    assert console.carried == [carried], f"the recreated console got {console.carried}"
    assert not hasattr(port, "adopt_login_sessions"), "a port adapter must not offer to take a web session"


def test_the_wiring_phase_carries_nothing_when_the_console_had_no_sessions(tmp_path):
    server = _server(tmp_path)
    console = _Wired("web_console")
    server.unified_adapters = [console]

    server._reload_full_wire({"server": {}}, "test", {"errors": []}, None)
    assert console.carried == [], "a console with nothing to hand over must not be handed an empty carry"

    server.unified_adapters = [_PlainPort()]
    server._reload_full_wire({"server": {}}, "test", {"errors": []}, ("omx_session", {SID: dict(SESSION)}))

    assert console.carried == []
