"""LIFECYCLE: the server process, its listeners, and its signals (LIF-01 to LIF-11).

No browser here. This suite is the first thing to run on a new machine, because it proves the
harness and the product agree about the process contract: what boots, what answers without a login,
what a round trip looks like on the wire, what a listener does with keystrokes that arrive together,
what a clean stop looks like, and what a client leaving should and should not write to the log.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import stat
from typing import Any, Dict, List

import pytest

from .harness import TEST_PASSWORD, Api
from .story import step, surfaces

pytestmark = [pytest.mark.e2e]

#: The retired ``serial_ports`` dict form, kept in the repo as a hard error (config_manager
#: ._validate_serial_ports_config). A mistake like this must stop the boot and name the key.
RETIRED_SERIAL_PORTS_FORM = """
server:
  id: e2e-broken
  description: OpenMux e2e server with a retired config form

serial_ports:
  adapter_type: serial
  ports:
    - name: serial1
      device: /dev/null
"""


def _anonymous(server: Any) -> Api:
    """An ``Api`` that sends no credentials at all."""
    return Api(server.web_url, username=None, password=None)


def _port_names(payload: Any) -> List[str]:
    """Every ``name`` value in any list inside an API payload, whatever shape the endpoint uses."""
    names: List[str] = []
    if isinstance(payload, dict):
        for value in payload.values():
            names.extend(_port_names(value))
    elif isinstance(payload, list):
        for entry in payload:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.append(entry["name"])
            else:
                names.extend(_port_names(entry))
    return sorted(set(names))


def _control_request(path, request: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
    """Send one JSON line to the control socket and return the JSON line it answers with."""
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(str(path))
        connection.sendall((json.dumps(request) + "\n").encode("utf-8"))
        raw = connection.makefile("rb").readline()
    finally:
        connection.close()
    payload = json.loads(raw.decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def _assert_client_left_quietly(server: Any) -> None:
    """Fail with the server tail when a client exit left an error behind.

    The markers are the paths that reported a dead socket as a server fault: the half-close in
    ``ClientSession.close()``, and the two reads in ``ClientSession``. A client that was killed, or a
    tab that was closed, ends those calls with ``ConnectionResetError``; that is how a client leaves,
    not a failure of the server.
    """
    output = server.output()
    markers = (
        "Error during client close",
        "write_eof",
        "Error receiving bytes from client",
        "Error receiving char from client",
    )
    for marker in markers:
        assert marker not in output, f"{marker!r} appeared in the server log:\n{server.tail(40)}"


def _assert_no_unhandled_task_error(server: Any) -> None:
    """Fail with the server tail when a task raised and nobody awaited it.

    asyncio prints such an exception once, with its traceback, when the task object is garbage
    collected. It never reaches a client and never fails a request, so a log marker is the only sign
    that a cleanup path raised at all.
    """
    output = server.output()
    for marker in ("Task exception was never retrieved", "Traceback (most recent call last)"):
        assert marker not in output, f"{marker!r} appeared in the server log:\n{server.tail(40)}"


@surfaces("http", "server log")
def test_lif01_the_server_boots_and_its_probe_answers_publicly(server, story):
    """The server boots, answers /healthz with no login, and challenges everything else."""
    anonymous = _anonymous(server)
    with step("GET /healthz answers without credentials"):
        status, _, body = anonymous.get("/healthz")
        assert status == 200, body
    with step("GET /api/ports challenges an anonymous request"):
        status, headers, _ = anonymous.get("/api/ports", expect=(401,))
        assert status == 401, headers
        assert "www-authenticate" in {key.lower() for key in headers}, headers
    with step("GET /console sends an anonymous reader to the login page"):
        status, headers, _ = anonymous.get("/console", expect=(302, 303))
        location = headers.get("Location", "")
        assert "/login" in location and "next=" in location, location
    with step("the pid file names the running process"):
        assert server.pidfile().read_text().strip() == str(server.proc.pid), server.pidfile()


@surfaces("http")
def test_lif02_an_admin_sees_the_configured_ports(server, api, story):
    """An admin sees each configured port through the API."""
    with step("GET /api/ports lists loop1 and the command port"):
        names = _port_names(api.json("/api/ports"))
        assert "loop1" in names, names
        assert "cat1" in names, names
    with step("GET /api/status agrees about the port set"):
        assert set(names) <= set(api.port_names()), (names, api.port_names())


@surfaces("CLI", "loopback port")
def test_lif03_a_cli_client_round_trips_a_loopback_port(server, clients, story):
    """A CLI client lists the port, attaches with the write slot, and gets its own bytes back."""
    with step("the client lists the ports and sees loop1"):
        cli_holder = clients.cli(user="rw", surface="cli:holder")
        assert "loop1" in cli_holder.list_ports(), cli_holder.list_ports()
    with step("the client attaches to loop1 and is granted the write slot"):
        cli_holder.attach(port="loop1")
        assert cli_holder.mode() == "read-write", cli_holder.mode()
        assert "CONNECTED:loop1:READ_WRITE" in cli_holder.wire_text(), cli_holder.wire_text()
    with step("bytes written to the port come back on the same socket"):
        cli_holder.write_line("lif03-ping")
        assert "lif03-ping" in cli_holder.wait_for("lif03-ping"), cli_holder.read_text()


@surfaces("CLI", "command port", "process")
def test_lif04_a_command_port_echoes_and_its_child_dies_with_the_server(server, clients, story):
    """A command port runs a real child, and the child leaves when the server leaves."""
    with step("the client attaches to the cat1 command port"):
        cli_writer = clients.cli(user="rw", surface="cli:writer", attach_to="cat1")
        assert cli_writer.mode() == "read-write", cli_writer.mode()
    with step("the child answers on its own process"):
        children = server.child_pids()
        assert children, f"the command adapter started no child: {server.tail(30)}"
        cli_writer.write_line("lif04-echo")
        assert "lif04-echo" in cli_writer.wait_for("lif04-echo"), cli_writer.read_text()
    with step("stopping the server leaves no child behind"):
        cli_writer.close()
        assert server.stop_clean() == 0, f"children survived: {server.child_pids()}"


@surfaces("process", "server log")
def test_lif05_a_retired_config_form_stops_the_boot_and_names_the_key(servers, story):
    """A retired serial_ports shape stops the boot, and the message names the key."""
    with step("a server.yaml in the retired serial_ports dict form is written"):
        broken = servers(name="broken", web=False, telnet=False, start=False, raw_server_yaml=RETIRED_SERIAL_PORTS_FORM)
    with step("the server refuses to boot on it"):
        broken.expected_exit = 1
        with pytest.raises(RuntimeError) as refusal:
            broken.start(timeout=20)
        assert "serial_ports" in str(refusal.value), str(refusal.value)[-1500:]
        assert broken.exit_code not in (None, 0), f"a bad config let the server run: exit {broken.exit_code}"


@surfaces("process", "server log")
def test_lif06_sigint_stops_the_server_with_no_traceback(server, story):
    """SIGINT stops the server cleanly, with no traceback and no closed-loop error."""
    with step("SIGINT reaches a running server"):
        server.send_signal(signal.SIGINT)
    with step("the process exits by itself, with code 0"):
        assert server.stop(timeout=20) == 0, server.tail(40)
    with step("nothing in the output looks like a crash"):
        output = server.output()
        for marker in ("Traceback (most recent call last)", "Event loop is closed", "Task exception was never retrieved"):
            assert marker not in output, f"{marker} appeared on shutdown:\n{server.tail(40)}"


@surfaces("signal", "CLI", "server log")
def test_lif07_a_soft_reload_keeps_an_attached_client(server, clients, story):
    """SIGHUP reloads the config and keeps an attached client usable."""
    with step("a client holds the write slot on loop1"):
        cli_holder = clients.cli(user="rw", surface="cli:holder", attach_to="loop1")
        cli_holder.write_line("before-reload")
        assert "before-reload" in cli_holder.wait_for("before-reload")
    with step("SIGHUP asks for a soft reload, and it completes"):
        server.reload_soft()
        server.wait_for_log("[reload-soft:sighup] Completed with summary")
    with step("the same client is still attached and still round-trips"):
        cli_holder.write_line("after-reload")
        assert "after-reload" in cli_holder.wait_for("after-reload"), cli_holder.read_text()
        assert cli_holder.mode() == "read-write", cli_holder.mode()


@surfaces("signal", "CLI", "server log")
def test_lif08_a_full_reload_stops_and_restores_the_adapters(server, clients, story):
    """SIGUSR1 stops and starts every adapter: the client drops, then a new one works."""
    with step("a client holds the write slot on loop1"):
        cli_holder = clients.cli(user="rw", surface="cli:holder", attach_to="loop1")
    with step("SIGUSR1 asks for a full reload, and it completes"):
        server.reload_full()
        server.wait_for_log("[reload-full:sigusr1] Reload complete", timeout=40)
    with step("the stop phase closed the attached session"):
        cli_holder.wait_for_eof(timeout=15)
    with step("a new client attaches and round-trips, so the port came back"):
        cli_next = clients.cli(user="rw", surface="cli:after", attach_to="loop1")
        cli_next.write_line("after-full-reload")
        assert "after-full-reload" in cli_next.wait_for("after-full-reload", timeout=15), cli_next.read_text()
    with step("the reload left no cleanup error in the log"):
        # stop() disconnects every client while each connection's own handler is in the same teardown.
        # Both used to pass the "is this client known" check before their first await, so the loser
        # reached `del self.clients[client_id]` after the winner had removed it. Nobody awaits the per
        # connection task, so that KeyError surfaced only as an unhandled task exception in the log.
        _assert_no_unhandled_task_error(server)


@surfaces("control socket")
def test_lif09_the_control_socket_answers_status_locally(server, story):
    """The local control socket answers a status request, and only its owner may use it."""
    with step("the socket exists in the run dir, readable by its owner only"):
        path = server.control_socket()
        server.wait_for_log("Control socket listening")
        assert path.exists(), f"no control socket at {path}\n{server.tail(30)}"
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, f"{path} has mode {oct(mode)}, expected 0o600"
    with step("one JSON line asks for status and gets one JSON line back"):
        reply = _control_request(path, {"action": "status"})
        assert reply.get("ok") is True, reply
        result = reply.get("result") or {}
        assert result.get("total", 0) >= 1, result
        assert result.get("started") == result.get("total"), result
    with step("an unknown action is refused rather than ignored"):
        refusal = _control_request(path, {"action": "not-a-real-action"})
        assert refusal.get("ok") is False, refusal
        assert "unknown_action" in str(refusal.get("error")), refusal


@surfaces("CLI", "loopback port", "server log")
def test_lif10_a_session_that_ends_is_closed_without_an_error_in_the_log(server, clients, story):
    """A client that leaves, by exit or by force, must not leave an ERROR or a traceback behind."""
    with step("a client attaches to loop1 and completes one round trip"):
        cli_leaver = clients.cli(user="rw", surface="cli:leaver", attach_to="loop1")
        cli_leaver.write_line("lif10-before-close")
        assert "lif10-before-close" in cli_leaver.wait_for("lif10-before-close", timeout=15), cli_leaver.read_text()
    with step("that client closes its connection, the way `openmux` does on exit"):
        cli_leaver.close()
        server.wait_for_log("disconnected from port loop1")
    with step("closing it logged nothing worse than the disconnect itself"):
        # The half-close in ClientSession.close() guards only AttributeError and RuntimeError, and a
        # socket the peer already left raises OSError instead. That escaped to the caller's handler and
        # logged ERROR plus a traceback for every ordinary client exit.
        _assert_client_left_quietly(server)
    with step("a client that dies without a word is also closed quietly"):
        cli_killed = clients.cli(user="rw", surface="cli:killed", attach_to="loop1")
        cli_killed.write_line("lif10-before-reset")
        assert "lif10-before-reset" in cli_killed.wait_for("lif10-before-reset", timeout=15), cli_killed.read_text()
        mark = len(server.log_lines())
        cli_killed.abort()
        server.wait_for_log("disconnected from port loop1", since=mark, timeout=15)
        _assert_client_left_quietly(server)
    with step("the write slot came free for the next client"):
        cli_next = clients.cli(user="rw", surface="cli:next", attach_to="loop1")
        assert cli_next.mode() == "read-write", cli_next.mode()
        cli_next.write_line("lif10-after")
        assert "lif10-after" in cli_next.wait_for("lif10-after", timeout=15), cli_next.read_text()
    with step("and the server still stops cleanly"):
        assert server.stop_clean() == 0, server.tail(40)


@surfaces("Telnet", "loopback port")
def test_lif11_input_that_arrives_with_the_login_still_reaches_the_port(servers, clients, story):
    """Every line one write carries takes effect: the login, the Port choice, and the Port's own data.

    One socket carries the login, the password, the Port choice, and after them the Port's data. A client
    that types while the server still writes a prompt sends the next line in the same segment, so the
    listener may read one line but must keep what came with it: the rest belongs to the next prompt, and
    after the last prompt to the Port (issue #103). A Loopback port echoes, so an echoed marker proves the
    Port received the line that travelled with the login.
    """
    world = servers(name="ahead", telnet_kwargs={"require_auth": True}, start=True)
    early = clients.telnet(user="rw", surface="telnet:ahead", server=world, require_auth=False)
    with step("login, password, Port choice and one Port line arrive in one write"):
        early.send_lines_at_once("rw", TEST_PASSWORD, "loop1", "lif11-typed-ahead")
    with step("the Port echoes the line that shared that write, so nothing before it was thrown away"):
        assert "lif11-typed-ahead" in early.wait_for("lif11-typed-ahead", timeout=15), early.text()
    with step("and the session drives the Port that the same write chose"):
        # The echo shows the bytes reached the Port; session info names the state, which is what a person
        # reads when they wonder which Port a fast login landed on.
        early.open_control_menu()
        early.press("i")
        early.wait_for("Port: loop1", timeout=10)
