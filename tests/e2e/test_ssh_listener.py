"""SSH LISTENER: the encrypted console surface, driven by the real client library (SSH-01 to SSH-10).

`asyncssh` terminates this surface, and `openmux/server/adapters/ssh_listener.py` decides who gets in, which
Port they land on, and what they may type. The unit tests for that adapter pass a fake console manager and a
fake process object, so no unit test completes a handshake, offers a host key, or answers a ``Port: `` prompt.
Every scenario here dials a running server with the same client library a person uses.

Five measured facts shape these bodies, and each is used where it applies:

* The adapter generates one Ed25519 host key per state directory and logs ``Generated new SSH host key at
  <path>`` once. Starting the same state directory again reuses the file and logs nothing.
* A refused login shows up on the wire only. The client sees ``Permission denied for user <name>``, and the
  product log gained no line of its own for a wrong password or an unknown account: only asyncssh's own
  ``Beginning auth for user <name>`` lines appeared. So these scenarios judge a login refusal from the client
  side. A refused *attach* is the opposite: the listener prints the line the decision module renders on the
  channel, then closes it with status 1, and writes two warnings naming the code it used.
* A command or subsystem request never reaches a Port. It comes back with exit status 1 and
  ``Only interactive sessions are supported.`` on stderr.
* The escape prefix on this transport is two bytes, which the Control menu's own session-info block reports
  as ``Escape sequence: b'\\x05'+b'c'``.
* A username of the form ``user+port`` or ``user:port`` skips the ``Port: `` prompt and lands on that Port.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest

from openmux.server.access_decision import Code, Mode, decide, notice

from .harness import free_ports, ssh_ed25519_keypair
from .story import step, surfaces

pytestmark = [pytest.mark.e2e, pytest.mark.feature]

#: Without asyncssh there is no SSH client to dial with, so the whole surface is unrunnable here.
asyncssh = pytest.importorskip("asyncssh")

#: What a listener prints for the Port menu when it waits for a Port name.
PORT_PROMPT = "Port: "

#: The listener's own answer to a command or subsystem request.
NOT_INTERACTIVE = "Only interactive sessions are supported."

#: The warning every listener writes when a port menu is open without a login (`listener_common`).
MENU_WARNING = "uses menu mode (target: '*') without require_auth"


def _ssh_world(servers: Any, name: str, ssh_kwargs: Optional[dict] = None, **kwargs: Any) -> Any:
    """Start one world with an SSH listener and no Telnet listener, so only SSH answers a client.

    The Telnet listener would bind a port menu too, and its own start-up warnings would then sit next to the
    SSH ones. A scenario that reads a log line should not have to say which listener wrote it.
    """
    return servers(name=name, ssh=True, ssh_kwargs=ssh_kwargs or {}, telnet=False, **kwargs)


def _public_line(path: Path) -> str:
    """The OpenSSH public line for the private key file at ``path``, as a client would see it."""
    return asyncssh.import_private_key(path.read_text(encoding="utf-8")).export_public_key().decode().strip()


def _key_material(line: str) -> str:
    """The base64 key inside an OpenSSH public line.

    The third field of such a line is a comment, and the two sides of a comparison do not agree on it: the
    server's stored key carries none, a generated pair carries a label. The material is the key.
    """
    parts = line.split()
    assert len(parts) >= 2, line
    return parts[1]


def _home_host_key_stamp() -> Optional[Tuple[int, float]]:
    """Size and mtime of the host key under the home directory, or ``None`` when there is none.

    The server inherits ``HOME`` from this process, so the two agree on where a home-directory key would be.
    `SSH-01` reads this before and after a boot to show the state directory won.
    """
    path = Path.home() / ".openmux" / "ssh_listener" / "ssh_host_key"
    if not path.exists():
        return None
    stat = path.stat()
    return (stat.st_size, stat.st_mtime)


def _lines_for(code: Code, mode: Mode = Mode.READ_ONLY, **fields: Any) -> List[str]:
    """The terminal lines the product itself renders for one Outcome code.

    The listener prints a refusal in the server's own words, and this asks that module for them, so a reworded
    notice fails here instead of passing against a copy kept in a test.
    """
    try:
        decision = decide(code, mode=mode, **fields)
    except ValueError:
        decision = notice(code, **fields)
    return list(decision.lines)


def _refusal(clients: Any, server: Any, *, user: str, surface: str, **kwargs: Any) -> str:
    """Return the text of a refused SSH session, and fail when the listener admitted it instead.

    ``clients.ssh`` connects as it builds the session, so the refusal arrives as the ``AssertionError`` that
    ``SshSession`` raises, with the listener's own words inside it.
    """
    with pytest.raises(AssertionError) as captured:
        clients.ssh(user=user, surface=surface, server=server, **kwargs)
    return str(captured.value)


@surfaces("ssh:viewer", "state dir", "server log")
def test_ssh01_the_host_key_is_born_in_the_state_dir_and_outlives_the_process(servers, clients, story):
    """The server generates its host key inside the state dir, offers that key, and reuses it after a restart."""
    home_before = _home_host_key_stamp()
    world = _ssh_world(servers, "hostkey", start=True)
    key_path = world.state_dir / "ssh_listener" / "ssh_host_key"

    with step("the generated key sits under the state dir and belongs to its owner alone"):
        line = world.wait_for_log("Generated new SSH host key at")
        assert str(key_path) in line, line
        assert key_path.stat().st_mode & 0o777 == 0o600, oct(key_path.stat().st_mode & 0o777)

    with step("the client is offered exactly that key, and no other"):
        session = clients.ssh(user="rw", surface="ssh:viewer", server=world)
        assert _key_material(session.server_host_key()) == _key_material(_public_line(key_path)), session.server_host_key()

    with step("the home directory kept whatever key it had, untouched"):
        assert _home_host_key_stamp() == home_before, (home_before, _home_host_key_stamp())
        session.close()

    with step("a restart reuses the stored key instead of minting a second identity"):
        before = len(world.log_lines())
        stored = key_path.read_bytes()
        world.restart()
        assert key_path.read_bytes() == stored, "the restarted server replaced the host key"
        with pytest.raises(AssertionError):
            world.wait_for_log("Generated new SSH host key", since=before, timeout=5.0)

    with step("and a client still gets in on the key it already saw"):
        again = clients.ssh(user="rw", surface="ssh:again", server=world)
        assert _key_material(again.server_host_key()) == _key_material(_public_line(key_path)), again.server_host_key()
        again.close()


@surfaces("ssh:rw", "ssh:viewer", "authentication.yaml")
def test_ssh02_the_account_behind_the_password_decides_the_seat(servers, clients, story):
    """A password the config accepts opens a shell, and the account's permission decides what that shell may do."""
    world = _ssh_world(servers, "accounts", start=True)

    with step("the write-entitled account comes up read-write on the Port it picks"):
        writer = clients.ssh(user="rw", surface="ssh:rw", server=world)
        writer.choose_port("loop1")
        assert writer.mode() == "read-write", writer.session_info()

    with step("the read-only account is admitted too, and is granted no seat"):
        viewer = clients.ssh(user="ro", surface="ssh:viewer", server=world)
        viewer.choose_port("loop1")
        assert viewer.mode() == "read-only", viewer.session_info()

    with step("both sit on one Port, because the viewer receives what the writer types"):
        writer.send_line("shared-bus")
        viewer.wait_for("shared-bus", timeout=10)

    with step("a wrong password never reaches a prompt"):
        refused = _refusal(clients, world, user="rw", password="not-the-password", surface="ssh:wrong")
        assert "Permission denied for user rw" in refused, refused

    with step("and an account the config does not know is answered the same way"):
        refused = _refusal(clients, world, user="nosuchuser", password="whatever", surface="ssh:nosuch")
        assert "Permission denied for user nosuchuser" in refused, refused


@surfaces("ssh:key", "authentication.yaml")
def test_ssh03_a_key_opens_this_door_only_for_the_use_and_the_account_it_names(servers, clients, story):
    """A registered key opens an SSH session only when allowed_uses names ssh, and only for its own account."""
    ssh_public, ssh_key = ssh_ed25519_keypair("e2e-ssh-use")
    client_public, client_key = ssh_ed25519_keypair("e2e-client-use")
    unknown_public, unknown_key = ssh_ed25519_keypair("e2e-unregistered")

    def register(builder: Any) -> Any:
        return builder.with_public_key("k-ssh", ssh_public, "rw", ("ssh",)).with_public_key(
            "k-client", client_public, "ro", ("client",)
        )

    world = _ssh_world(servers, "keys", configure=register, start=True)

    with step("the record that names the ssh use opens a session for its account"):
        session = clients.ssh(user="rw", password=None, client_keys=[ssh_key], surface="ssh:key", server=world)
        session.choose_port("loop1")
        assert "loop1" in session.session_info(), session.session_info()
        session.close()

    with step("the config, not the client, carried that key"):
        written = world.config_text("authentication.yaml")
        assert "k-ssh" in written and "k-client" in written, written
        assert _key_material(unknown_public) not in written, "the unregistered key is in the config after all"

    with step("a key the config never names is refused"):
        refused = _refusal(clients, world, user="rw", password=None, client_keys=[unknown_key], surface="ssh:unknown")
        assert "Permission denied for user rw" in refused, refused

    with step("a key registered for the CLI use only does not open the SSH door"):
        refused = _refusal(clients, world, user="ro", password=None, client_keys=[client_key], surface="ssh:clientuse")
        assert "Permission denied for user ro" in refused, refused

    with step("and a valid key cannot claim somebody else's account"):
        refused = _refusal(clients, world, user="admin", password=None, client_keys=[ssh_key], surface="ssh:wrongacct")
        assert "Permission denied for user admin" in refused, refused


@surfaces("ssh:anon", "ssh:stranger", "server log")
def test_ssh04_a_listener_without_a_login_refuses_every_attach_it_offers(servers, clients, story):
    """With require_auth off the listener asks for no credential, then refuses the attach its own warning promises.

    Measured here and reported rather than asserted as correct: the warning promises a port anyone can attach
    to, and no attach is possible. With no login the adapter has no account to carry the session, so it names
    the client after the listener (``ssh_<name>``) and the access ladder grants that identity nothing. The
    username a client offers is never consulted, so ``rw`` and a made-up name are refused identically. GitHub
    issue #110 tracks that decision; see docs/design/e2e_test_suite.md.
    """
    world = _ssh_world(servers, "anon", ssh_kwargs={"require_auth": False}, start=True)

    with step("the start-up warning states what this opened, in one line naming the listener"):
        line = world.wait_for_log("SSH listener 'ssh1' uses menu mode")
        assert MENU_WARNING in line, line
        assert "any client can browse and attach to any port" in line, line

    with step("a write-entitled name walks in with no password and no key at all, and is shown the Port menu"):
        session = clients.ssh(user="rw", password=None, client_keys=[], surface="ssh:anon", server=world)
        session.wait_for(PORT_PROMPT, timeout=10)

    with step("the seat that name's account holds is never offered, because the name is not carried in"):
        session.choose_port("loop1")
        session.wait_for(_lines_for(Code.NO_PERMISSIONS)[0], timeout=10)
        assert session.exit_status() == 1, session.exit_status()
        assert session.is_eof(timeout=5), session.text()[-200:]

    with step("the log blames the listener's own identity, never the account the client claimed"):
        denied = world.wait_for_log("Denying connection for 'ssh_ssh1' to port loop1")
        assert "no permissions assigned" in denied, denied
        rejected = world.wait_for_log("rejected SSH client ssh:ssh1:")
        assert "(code=no_permissions)" in rejected, rejected
        assert "for 'rw' " not in "\n".join(world.log_lines()), world.log_lines()[-6:]

    with step("and a name the server has never heard of is refused the same way, two copies of one refusal"):
        stranger = clients.ssh(user="walkin", password=None, client_keys=[], surface="ssh:stranger", server=world)
        stranger.choose_port("loop1")
        stranger.wait_for(_lines_for(Code.NO_PERMISSIONS)[0], timeout=10)
        assert stranger.exit_status() == 1, stranger.exit_status()
        stranger.close()


@surfaces("ssh:fenced", "ssh:open", "server log")
def test_ssh05_an_address_list_refuses_before_a_login_and_only_that_listener(servers, clients, story):
    """A listener whose acl omits the client's address closes the socket before a prompt, while its neighbour works."""
    other_port = free_ports(1)[0]

    def fence(builder: Any) -> Any:
        return builder.with_ssh_listener(other_port, name="ssh2")

    world = _ssh_world(servers, "acl", ssh_kwargs={"acl": ["192.0.2.10"]}, configure=fence, start=True)

    with step("the fenced listener never reaches a prompt"):
        refused = _refusal(clients, world, user="rw", surface="ssh:fenced")
        assert "Permission denied" not in refused, refused
        assert "Connection reset" in refused, refused

    with step("the log names the listener, the address, and the reason"):
        line = world.wait_for_log("denied 127.0.0.1 (ACL)")
        assert "SSH listener 'ssh1'" in line, line

    with step("the listener on the next port, which lists no acl, admits the same client"):
        open_session = clients.ssh(user="rw", surface="ssh:open", server=world, port=other_port)
        open_session.choose_port("loop1")
        assert open_session.mode() == "read-write", open_session.session_info()
        open_session.close()


@surfaces("ssh:rw")
def test_ssh06_a_command_request_never_reaches_a_port(servers, clients, story):
    """A shell is the only thing this listener serves, and a command request is answered with its own refusal."""
    world = _ssh_world(servers, "exec", start=True)
    session = clients.ssh(user="rw", surface="ssh:rw", server=world)

    with step("the session attaches, the way any interactive client does"):
        session.choose_port("loop1")
        session.send_line("interactive")
        session.wait_for("interactive", timeout=10)

    with step("the same connection asks for one command, and is refused in the listener's own words"):
        status, out, err = session.request_command("echo hi")
        assert status == 1, status
        assert out == "", out
        assert NOT_INTERACTIVE in err, err

    with step("the refusal left the shell alone, because it refused the new request only"):
        session.send_line("still-here")
        session.wait_for("still-here", timeout=10)


@surfaces("ssh:victim", "cli:taker", "server log", "port data log")
def test_ssh07_a_takeover_from_the_cli_takes_a_seat_held_over_ssh(servers, clients, story):
    """A CLI Takeover displaces an SSH holder, tells it in band, and writes the audit line every surface writes."""
    world = servers(
        name="seat",
        ssh=True,
        telnet=False,
        configure=lambda builder: builder.with_loopback("slot1", write_slot_capacity="one"),
        start=True,
    )

    with step("the SSH session came up read-write, so it holds the one seat"):
        holder = clients.ssh(user="rw", surface="ssh:victim", server=world)
        holder.choose_port("slot1")
        assert holder.mode() == "read-write", holder.session_info()

    with step("a CLI client with the same entitlement takes the seat"):
        taker = clients.cli(user="admin", surface="cli:taker", server=world)
        taker.attach(port="slot1")
        reply = taker.takeover()
        assert reply.get("ok") is True, reply

    with step("the SSH holder is told in band, in the sentence the shared module owns"):
        holder.wait_for(notice(Code.DEMOTED, actor="admin").message, timeout=10)
        assert holder.mode() == "read-only", holder.session_info()

    with step("the server log records who took it from whom"):
        line = world.wait_for_log("WRITE-SLOT TAKEOVER port=slot1", timeout=15)
        assert "taker=admin:" in line, line
        assert "victim=rw:" in line, line

    with step("and the Port's own data log carries the same event"):
        world.wait_for_port_log("slot1", "write_slot_takeover", timeout=15)
        holder.close()


@surfaces("ssh:admin", "server log")
def test_ssh08_the_power_menu_key_drives_this_consoles_feeds_over_ssh(servers, clients, story):
    """The Control menu's power key lists the console's feeds over SSH, toggles one, and is audited to that client."""
    world = _ssh_world(
        servers,
        "power",
        start=True,
        configure=lambda builder: builder.with_dummy_pdu("rack1", ["1", "2"], poll_interval=0).with_loopback(
            "feed1", write_slot_capacity="one", feeds=["rack1.1", "rack1.2"]
        ),
    )
    session = clients.ssh(user="admin", surface="ssh:admin", server=world)

    with step("the session attaches to the Port backed by two feeds"):
        session.choose_port("feed1")

    with step("the power key lists both feeds, both on"):
        session.open_control_menu()
        session.press("p")
        session.wait_for("POWER: feeds for feed1", timeout=10)
        # Each listing row is waited for, not read off a snapshot: the listener writes the header, the rows,
        # and the `> ` prompt as separate writes, so any one of them can be the last byte in flight.
        session.wait_for(" 1  [on]   rack1.1", timeout=10)
        session.wait_for(" 2  [on]   rack1.2", timeout=10)

    with step("answering one outlet switches it off and says why the Port stays up"):
        session.answer("1")
        granted = session.wait_for("POWER rack1.1 -> off", timeout=10)
        assert "feed1 stays up via rack1.2" in granted, granted
        session.wait_for(" 1  [off]  rack1.1", timeout=10)

    with step("the attached session is told the feed changed, in the same transcript"):
        session.wait_for("[POWER] feed rack1.1 is now off", timeout=10)

    with step("the power audit line names the person, the outlet, and the SSH client"):
        line = world.wait_for_log("POWER CONTROL: user admin turned rack1.1 off", timeout=15)
        assert "staying up: feed1" in line, line
        assert "client ssh:ssh1:" in line, line
        session.close()


@surfaces("ssh:embedded", "ssh:menu")
def test_ssh09_the_username_can_name_the_port_and_skip_the_port_menu(servers, clients, story):
    """A username that carries a Port name lands on it directly, and an unknown name is answered then closed."""
    world = _ssh_world(servers, "embedded", start=True)

    with step("the plus form lands on the Port with no prompt in between"):
        session = clients.ssh(user="rw+loop1", surface="ssh:embedded", server=world)
        session.send_line("straight-in")
        session.wait_for("straight-in", timeout=10)
        assert PORT_PROMPT not in session.text(), session.text()
        assert "loop1" in session.session_info(), session.session_info()
        session.close()

    with step("the colon form does the same job"):
        colon = clients.ssh(user="admin:loop1", surface="ssh:colon", server=world)
        colon.send_line("colon-in")
        colon.wait_for("colon-in", timeout=10)
        colon.close()

    with step("a Port the server does not have is named back, and the channel is closed with a status"):
        missing = clients.ssh(user="rw+nosuch", surface="ssh:nosuch", server=world)
        missing.wait_for("Port nosuch unavailable", timeout=10)
        assert missing.exit_status() == 1, missing.exit_status()
        assert missing.is_eof(timeout=5), missing.text()[-200:]

    with step("and a plain username still gets the Port menu, which is what the form does not replace"):
        menu = clients.ssh(user="rw", surface="ssh:menu", server=world)
        menu.wait_for(PORT_PROMPT, timeout=10)
        menu.close()


@surfaces("ssh:rw", "server log")
def test_ssh10_a_port_that_excludes_the_account_answers_on_screen_and_in_the_log(servers, clients, story):
    """A Port whose own list excludes the account still appears in the Port menu, then refuses the name on the channel.

    The ladder runs when the client names a Port, not when the Port menu is built, so the listing is not a promise.
    The refusal arrives on the session in the same words every transport uses, the channel closes with a status,
    and the log carries the code the ladder gave.
    """

    def gate(builder: Any) -> Any:
        return builder.with_loopback("gated", write_slot_capacity="one", read_write_groups=["nope"], read_only_groups=["nope"])

    world = _ssh_world(servers, "gated", configure=gate, start=True)
    session = clients.ssh(user="rw", surface="ssh:rw", server=world)

    with step("the Port menu lists it, because nothing has asked for it yet"):
        session.wait_for(PORT_PROMPT, timeout=10)
        # The listing is written before the prompt, so once the prompt has arrived the row is on screen.
        assert "gated" in session.text(), session.text()

    with step("naming it is answered on the session, in the words the decision module owns"):
        session.choose_port("gated")
        session.wait_for(_lines_for(Code.DENIED_BY_GROUP_ACL)[0], timeout=10)

    with step("the channel ends with a status, which is how a refused attach reads from a client"):
        assert session.exit_status() == 1, session.exit_status()
        assert session.is_eof(timeout=5), session.text()[-200:]

    with step("the log carries the ladder's reason and the listener's own reject line"):
        denied = world.wait_for_log("Denying connection for 'rw' to port gated")
        assert "denied_by_group_acl" in denied, denied
        rejected = world.wait_for_log("rejected SSH client ssh:ssh1:")
        assert "for port gated" in rejected, rejected
        assert "(code=denied_by_group_acl)" in rejected, rejected
