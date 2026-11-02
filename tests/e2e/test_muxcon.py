"""MUXCON: two real nodes, one link, and what each side may see and do (MXC-01 to MXC-10).

Federation was proven only as an empty state before these ids: `ST-04` shows the status page saying
`No federation connections`, and the unit suites drive `UnifiedMuxConAdapter` with fakes. No test started a
second `openmux.server.main` process and let it dial the first over a socket. These ids do, on two servers
in one test - which the harness already did for `CFG-07` and `PWR-03` - and add the one capability it
lacked: a `muxcon:` section, which `ConfigBuilder.with_muxcon` now writes (issue #111).

Nine measured facts shape these bodies, and each is stated where it is used:

* Both directions of the link default to **deny-all** (issue #77), and a Port a filter drops is reported
  only at `DEBUG`. So each end states its filters, and a scenario never reads a missing Port as a transfer
  failure without the advertise count the sender logged.
* The answering side lists a peer Port under the **origin's plain name** (`loop1`, never `<node>::loop1`),
  so the hub runs with no local Port of its own: a Port named `loop1` there can only have arrived over the
  link.
* The **origin** decides the mode, and it decides it from its own Port. Asked at attach, it hands the
  Write slot to the first write-entitled console across the link and refuses the next: a federated Port
  carries the ONE slot of the Port it stands for, shared by every console on every peer node. The asking
  node decides nothing — it asks, and obeys the answer. A released slot goes to **nobody**: the origin
  keeps no queue of waiting consoles, so the next holder is whoever asks next.
* A console reads that answer differently per surface. The CLI protocol carries the mode and nothing else
  (`CONNECTED:loop1:READ_ONLY`), so an id is what these ids assert there; only a listener surface prints the
  sentence that names the reason, which is why `MXC-04` dials Telnet on the hub.
* A TLS link needs no shared CA. The answering side autogenerates a self-signed certificate under its own
  state dir, and the dialling side's default check relaxes the TLS-level verification and gates the link on
  a SHA-256 fingerprint stored against `host:port` — trust-on-first-use. The first connect is unverified and
  is recorded as such; a peer that returns with a different certificate is refused, and a refusal never
  rewrites the stored fingerprint. `MXC-05` records, `MXC-06` refuses.
* Key-authenticated peering proves a signature, not a name. The dialler puts its `key_id` in `HELLO` as
  `PKID=<id>`; the acceptor issues `AUTH:PK:CHALLENGE` with a 32-byte nonce and verifies the Ed25519
  signature on it against the public key stored under that id. A `key_id` nobody knows, or a matching name
  with the wrong private key, closes the connection before any Port moves — and the acceptor also drops a
  `PORTS:FEDERATED` from a connection it has not authenticated, so a peer that advertises anyway injects
  nothing. `MXC-07` proves all three outcomes on one hub.
* A Port can be addressed through the node that owns it. The menu lists a federated Port as
  `<origin>::<name>` and both menu surfaces and the CLI wire accept that qualified form, `local::<name>`,
  and the bare name; each is checked against the Port's **origin**, so a qualifier naming a node that does
  not own the Port is refused even when the bare name would have matched one. `MXC-08` refuses both wrong
  shapes and reaches the Port with both right ones, through the menu and on the wire.
* An outlet on the peer node is switched by request, never directly. The qualified ref
  `<node>::<pdu>.<outlet>` relays to the origin, but only when the acting console ITSELF drives, read-write, a
  port that ref powers — the request is anchored on that console's own session and the origin re-verifies the
  mirror of it before running its own local switch. The origin's audit names its own local ref; the state update
  flowing back is what powers off the typing console's own notices on this side of the link. `MXC-09` runs the
  round trip.
* The link itself is editable at runtime. `POST /config-editor/apply` replaces the WHOLE config (a section left
  out reads as removed), the JSON schema refuses a `muxcon` section carrying an unmodelled key, and the write
  itself starts nothing — a SOFT reload bootstraps the muxcon adapter onto a running node that started without
  one and starts initiators added since. `MXC-10` writes an initiator into a live node and watches the Port
  arrive on the peer.
"""

from __future__ import annotations

import os
from typing import Any, List, Optional, Tuple

import pytest
import yaml

from .harness import ssh_ed25519_keypair
from .test_config_editor import _apply, _config, _data, _payload, _reconciled, _reload, _summary

from openmux.server.access_decision import Code, read_only_grant_notice, write_blocked_notice

from .story import step, surfaces

pytestmark = [pytest.mark.e2e, pytest.mark.feature]

#: What the answering side logs once a peer Port is in its own Port list. The count line
#: `Registered <n> federated ports` counts what arrived before the accept filter and fires even when every
#: Port was dropped, so only this per-Port line proves the Port became usable here.
PORT_REGISTERED = "Registered federated port:"

#: What the dialling side logs after it sends the list of Ports it offers.
PORTS_SENT = "local ports in PORTS:FEDERATED response"

#: The leaf's dummy PDU. Its one outlet feeds `loop1` alone, so one relayed switch off takes a console's
#: LAST feed away — a fall to half-power would warn nothing and prove less. The dummy driver starts with
#: every outlet ON, so a scenario reads "still on" as the untouched state, never as a prior switch.
PDU = "rack1"
PDU_FEED = f"{PDU}.1"


def _linked_pair(
    servers: Any,
    *,
    port: str = "loop1",
    hub_accept: Optional[List[str]] = None,
    leaf_advertise: Optional[List[str]] = None,
    hub_telnet: bool = False,
    use_tls: bool = False,
    hub_loopback: Tuple[str, ...] = (),
) -> Tuple[Any, Any]:
    """Start two servers, the second dialling the first, and return ``(hub, leaf)``.

    The hub answers on a federation port the factory allocated, so ``wait_ready`` proves the listener bound:
    a listener that fails to bind only logs an error, and these servers run with ``fail_fast_adapters:
    false``. The leaf owns ``port`` and the hub owns nothing — unless ``hub_loopback`` names Ports the hub
    owns too — so a Port of that name on the hub can only have come from the peer. ``hub_loopback`` is for
    a scenario that aims an origin-qualified name at a Port this node owns but the peer does not.

    ``use_tls`` turns the link over to TLS on both sides at once; the default is the plaintext link every
    other id in this file runs. A TLS world carries its own material: the hub autogenerates a certificate
    in its state dir, and the leaf pins the hub's fingerprint there. Both files belong to this world alone,
    because its state dir is created with it.
    """
    hub = servers(
        name="hub",
        loopback=hub_loopback,
        telnet=hub_telnet,
        telnet_kwargs={"require_auth": True} if hub_telnet else None,
        muxcon=True,
        muxcon_kwargs={"accept": hub_accept if hub_accept is not None else ["*"], "use_tls": use_tls},
        start=True,
    )
    dial_to = hub.port_of("muxcon")
    leaf = servers(
        name="leaf",
        loopback=(port,),
        telnet=False,
        configure=lambda builder: builder.with_muxcon(
            dial=[("127.0.0.1", dial_to)],
            advertise=leaf_advertise if leaf_advertise is not None else [port],
            use_tls=use_tls,
        ),
        start=True,
    )
    hub.wait_for_log("Handshake OK (server-side)", timeout=30)
    return hub, leaf


def _origin_gave_the_slot(leaf: Any, hub: Any) -> None:
    """Wait until the leaf says it registered the peer's stream and then let it drive.

    Two lines, in this order, in the log of the node that OWNS the Port: the stream arrives as a
    read-only client of the local Port, and the origin promotes it only when the peer asks. That is
    the proof that the origin arbitrates and the asking node decides nothing.
    """
    leaf.wait_for_log("to port loop1 in read-only mode", timeout=20)
    leaf.wait_for_log(f"Promoted client fed:node:{hub.server_id}", timeout=20)


def _holders(session: Any) -> List[str]:
    """The holder labels the node OWNING the Port reports for it, asked over that console's own link.

    A federated Port's Write slot lives on the origin, so this is the only holder list that counts for
    it: it names a console that reached the Port from the other node exactly as it names a local one.
    An empty list means the slot is free — the product keeps no queue of waiting consoles.
    """
    return [str(label) for label in (session.query_holders().get("holders") or [])]


def _promotions(leaf: Any) -> int:
    """How many times the origin has handed the Write slot of ``loop1`` to anyone, ever."""
    return len([line for line in leaf.log_lines() if " to read-write mode on port loop1" in line])


def _tofu_fingerprint(leaf: Any, dial_key: str) -> str:
    """The one fingerprint the dialling node trusts for the answering node, read from its store.

    The store is ``known_peers.yaml`` under the node's own state dir — a ``host:port`` to ``sha256:`` map
    the product wrote after the first TLS connect. A refusal must never rewrite it, so a scenario reads
    the file itself rather than trusting a log line.
    """
    path = leaf.state_dir / "muxcon" / "known_peers.yaml"
    assert path.exists(), f"the dialling node stored no trust material at {path}"
    stored = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    fingerprint = str(stored.get(dial_key) or "")
    assert fingerprint.startswith("sha256:"), f"{dial_key!r} is absent from the TOFU store: {stored!r}"
    return fingerprint


def _ports(api: Any) -> List[dict]:
    """The Port list one node serves, as ``/api/ports`` renders it."""
    return list((api.json("/api/ports") or {}).get("ports") or [])


def _port_named(api: Any, name: str) -> dict:
    """The one Port entry with this name, or a failure listing what the node really serves."""
    entries = [entry for entry in _ports(api) if str(entry.get("name")) == name]
    assert len(entries) == 1, f"{name!r} appears {len(entries)} times in {[e.get('name') for e in _ports(api)]}"
    return entries[0]


@surfaces("server:hub", "server:leaf", "api:status")
def test_mxc01_two_nodes_link_and_report_each_other(servers, story, api_for_server) -> None:
    """Two nodes finish the handshake, and the answering node names the peer in its status."""
    hub, leaf = _linked_pair(servers)

    with step(f"the leaf reached the hub on federation port {hub.port_of('muxcon')}"):
        payload = api_for_server(hub).status()
        connections = payload.get("federation", {}).get("connections") or []
        assert len(connections) == 1, f"expected one federation connection: {connections!r}"
        entry = connections[0]
        assert entry.get("active"), f"the link is listed but not active: {entry!r}"
        assert entry.get("role"), f"the connection reports no role: {entry!r}"
        handshake = entry.get("handshake") or {}
        assert handshake.get("server_id") == leaf.server_id, f"the peer is not the node that dialled: {handshake!r}"

    with step("the multipath group of that link is keyed by the peer's server id"):
        status = api_for_server(hub).status()
        groups = status.get("multipath", {}).get("groups") or []
        assert [group.get("peer_key") for group in groups] == [f"node:{leaf.server_id}"], groups
        totals = status.get("multipath", {}).get("totals") or {}
        assert int(totals.get("groups") or 0) == 1, totals
        assert int(totals.get("primaries") or 0) == 1, totals

    with step("and each side counts the link it holds"):
        hub_totals = api_for_server(hub).status().get("federation", {}).get("totals") or {}
        leaf_totals = api_for_server(leaf).status().get("federation", {}).get("totals") or {}
        assert int(hub_totals.get("connections_active") or 0) >= 1, hub_totals
        assert int(leaf_totals.get("connections_active") or 0) >= 1, leaf_totals


@surfaces("server:hub", "cli:rw", "cli:ro", "server:leaf")
def test_mxc02_a_peer_port_keeps_the_origin_identity_and_drives_for_an_entitled_console(
    servers, clients, story, api_for_server
) -> None:
    """The leaf's Port appears on the hub under its own name, with the leaf's identity on it."""
    hub, leaf = _linked_pair(servers)

    with step("the leaf sent its Port list and the hub registered that Port"):
        leaf.wait_for_log(PORTS_SENT, timeout=30)
        hub.wait_for_log(f"{PORT_REGISTERED} loop1", timeout=30)

    with step("the hub lists it as a remote Port whose id names the node that owns it"):
        entry = _port_named(api_for_server(hub), "loop1")
        assert entry.get("adapter_type") == "remote_muxcon", entry
        assert str(entry.get("origin_server_id")) == leaf.server_id, entry
        # The interactive menu builds a peer Port's label from the same field (`render_port_list`), so
        # the api and the menu now agree. Before this, the api put this node's own id here, which no
        # node resolves.
        assert str(entry.get("id")) == f"{leaf.server_id}::loop1", entry

    with step("a write-entitled console on the hub drives it, because the origin had a free slot"):
        driver = clients.cli(user="rw", surface="cli:rw", attach_to="loop1", server=hub)
        assert "CONNECTED:loop1:READ_WRITE" in driver.wire_text(), driver.wire_text()[-400:]
        assert driver.mode() == "read-write", driver.wire_text()[-400:]
        leaf.wait_for_log("-> loop1", timeout=20)
        _origin_gave_the_slot(leaf, hub)
        driver.close()

    with step("an account with no write permission at home does not get the slot even when it is free"):
        watcher = clients.cli(user="ro", surface="cli:ro", attach_to="loop1", server=hub)
        assert "CONNECTED:loop1:READ_ONLY" in watcher.wire_text(), watcher.wire_text()[-400:]
        assert watcher.mode() == "read-only", "the link granted a drive the home node never gave"
        promoted = [line for line in leaf.log_lines() if "to read-write mode on port loop1" in line]
        assert len(promoted) == 1, f"the viewer was promoted too: {promoted}"


@surfaces("cli:rw", "cli:blocked", "cli:local", "server:leaf")
def test_mxc03_a_released_write_slot_belongs_to_nobody_until_a_console_asks(servers, clients, story) -> None:
    """Two consoles on the hub, one Port on the leaf: one drives, and a freed slot waits for a request.

    The slot the origin hands out is the slot of its own Port, so a console on the owner node competes
    for it with a console across the link, and the owner's holders list is where both are visible.
    Nothing hands a freed slot to the next viewer: the origin keeps no queue — only `REQUEST` promotes,
    from a peer or from a local console alike — so after the holder leaves the owner reports no holder
    at all, the viewer stays a viewer, and the slot goes to whoever asks.
    """
    hub, leaf = _linked_pair(servers)
    hub.wait_for_log(f"{PORT_REGISTERED} loop1", timeout=30)

    with step("the first console across the link holds the Write slot of the leaf's Port"):
        first = clients.cli(user="rw", surface="cli:rw", attach_to="loop1", server=hub)
        assert "CONNECTED:loop1:READ_WRITE" in first.wire_text(), first.wire_text()[-400:]
        _origin_gave_the_slot(leaf, hub)

    with step("the owner node counts that peer console as its only holder"):
        owner = clients.cli(user="ro", surface="cli:local", attach_to="loop1", server=leaf)
        assert len(_holders(owner)) == 1, f"the owner does not see the peer's holder: {_holders(owner)}"

    with step("the second console across the link gets the same Port as a viewer"):
        second = clients.cli(user="rw", surface="cli:blocked", attach_to="loop1", server=hub)
        assert "CONNECTED:loop1:READ_ONLY" in second.wire_text(), second.wire_text()[-400:]
        assert len(_holders(owner)) == 1, "the viewer was let drive as well"

    with step("when the holder leaves, the slot belongs to nobody — it is not handed to the viewer"):
        first.close()
        # `CLOSE stream` is the origin learning that the peer's stream ended. The poll then waits for
        # that close to free the slot, so the checks below say "nobody holds it" rather than "the close
        # has not arrived yet".
        leaf.wait_for_log("CLOSE stream 1", timeout=20)
        leaf.wait_until(lambda: not _holders(owner), timeout=15, what="the released slot to belong to nobody")
        assert second.mode() == "read-only", "the link drove the console that only ever watched"
        assert _promotions(leaf) == 1, "a released slot promoted a console that never asked"

    with step("and it goes to the console that asks, the same way it went to the first"):
        reply = second.request_read_write()
        assert reply.get("ok") is True, f"a free slot was refused to an entitled console: {reply!r}"
        assert second.mode() == "read-write", second.wire_text()[-400:]
        assert _promotions(leaf) == 2, [line for line in leaf.log_lines() if " to read-write mode on port loop1" in line]
        assert len(_holders(owner)) == 1, f"the owner does not count the new holder: {_holders(owner)}"


@surfaces("cli:holder", "telnet:blocked", "server:leaf")
def test_mxc04_a_console_blocked_across_the_link_is_told_why_and_not_by_whom(servers, clients, story) -> None:
    """The refusal a federated console reads names the reason, and no line names a node or a client."""
    hub, leaf = _linked_pair(servers, hub_telnet=True)
    hub.wait_for_log(f"{PORT_REGISTERED} loop1", timeout=30)

    with step("one console on the hub holds the Write slot of the leaf's Port"):
        holder = clients.cli(user="rw", surface="cli:holder", attach_to="loop1", server=hub)
        assert holder.mode() == "read-write", holder.wire_text()[-400:]

    with step("the console that arrives second is told the reason in words, on a listener surface"):
        blocked = clients.telnet(user="rw", surface="telnet:blocked", server=hub, require_auth=True)
        blocked.choose_port("loop1")
        blocked.wait_for(read_only_grant_notice(Code.CAPACITY_FULL), timeout=20)
        blocked.send_line("")
        blocked.wait_for(write_blocked_notice(Code.CAPACITY_FULL), timeout=20)

    with step("and nothing in that transcript names the peer node, this node, or the console in front of it"):
        transcript = blocked.text()
        # The menu above the notice does name the peer, because a peer Port is labelled
        # `<node>::<port>` there. The check is therefore per notice line: neither the node that owns
        # the Port, nor this node, nor the console holding the slot may appear in the reason.
        for notice in (read_only_grant_notice(Code.CAPACITY_FULL), write_blocked_notice(Code.CAPACITY_FULL)):
            lines = [line for line in transcript.splitlines() if notice in line]
            assert lines, f"{notice!r} never arrived as its own line: {transcript[-400:]}"
            for named in (hub.server_id, leaf.server_id, "rw:"):
                assert named.lower() not in lines[0].lower(), lines[0]


@surfaces("server:hub", "server:leaf", "api:ports")
def test_mxc05_a_tls_link_needs_no_shared_ca_because_the_dialler_pins_what_it_first_saw(
    servers, story, api_for_server
) -> None:
    """The answering node serves an autogenerated certificate, and the dialling node records its fingerprint."""
    hub, leaf = _linked_pair(servers, use_tls=True)

    with step("the dialling side names the mode that let a self-signed peer in, before it dials"):
        # `_create_client_ssl_context` relaxes the TLS-level check for the trust-on-first-use gate, and
        # says so once per peer: without that line a green link could just mean verification was off.
        leaf.wait_for_log("trust-on-first-use gates the link", timeout=30)

    with step("the answering side served a certificate it generated on its own first start"):
        cert = hub.state_dir / "muxcon" / "server.crt"
        key = hub.state_dir / "muxcon" / "server.key"
        assert cert.exists() and key.exists(), f"no autogen certificate pair under {cert.parent}"

    with step("the dialling side recorded the peer's fingerprint, warning that this first sight is unverified"):
        dial_key = f"127.0.0.1:{hub.port_of('muxcon')}"
        leaf.wait_for_log(f"TOFU stored fingerprint for {dial_key}", timeout=30)
        assert _tofu_fingerprint(leaf, dial_key).startswith("sha256:")

    with step("and the encrypted link carries the leaf's Port like the plaintext one does"):
        hub.wait_for_log(f"{PORT_REGISTERED} loop1", timeout=30)
        entry = _port_named(api_for_server(hub), "loop1")
        assert str(entry.get("origin_server_id")) == leaf.server_id, entry


@surfaces("server:hub", "server:leaf")
def test_mxc06_a_peer_that_returns_with_a_different_certificate_is_refused(servers, story) -> None:
    """The answering node comes back without its certificate, and the stored fingerprint closes the gate."""
    hub, leaf = _linked_pair(servers, use_tls=True)
    hub.wait_for_log(f"{PORT_REGISTERED} loop1", timeout=30)
    dial_key = f"127.0.0.1:{hub.port_of('muxcon')}"

    with step("the first link left exactly one trusted fingerprint on the dialling side"):
        leaf.wait_for_log(f"TOFU stored fingerprint for {dial_key}", timeout=30)
        stored = _tofu_fingerprint(leaf, dial_key)

    with step("the answering node restarts without its certificate, so its next one is different"):
        handshakes = len([line for line in hub.log_lines() if "Handshake OK" in line])
        hub.stop()
        (hub.state_dir / "muxcon" / "server.crt").unlink()
        (hub.state_dir / "muxcon" / "server.key").unlink()
        hub.restart()

    with step("the dialling side reaches it again, recognises a stranger, and refuses the link"):
        # This warning proves the dialler redialled AFTER the restart and saw the new certificate over a
        # live socket, so the absence checked next is bounded: the thing the peer dialed is this one.
        leaf.wait_for_log("TOFU fingerprint changed", timeout=60)

    with step("the stranger completed no handshake, and what the node trusted is unchanged"):
        found = len([line for line in hub.log_lines() if "Handshake OK" in line])
        assert found == handshakes, "a peer with a changed certificate finished a handshake"
        assert _tofu_fingerprint(leaf, dial_key) == stored, "the refusal overwrote the trusted fingerprint"


@surfaces("server:hub", "server:leaf", "server:forged", "server:nokey", "api:ports")
def test_mxc07_a_peered_key_opens_the_link_and_a_stolen_name_does_not(servers, story, api_for_server, tmp_path) -> None:
    """One hub checks one key: its true holder links, a wrong key under its name is refused, nothing links without a key.

    The proof is a signature, never a name: the acceptor challenges the claimed ``key_id`` with a 32-byte
    nonce and verifies the Ed25519 signature on it. So the interesting dialler is the one whose ``key_id``
    MATCHES a configured entry while its private key does not — it passes the name gate and dies at the
    crypto gate, which is what no name-matching scheme could catch.
    """
    real_pub, real_key = ssh_ed25519_keypair("mxc07-real")
    _forged_pub, forged_key = ssh_ed25519_keypair("mxc07-forged")

    def _private_file(name: str, key) -> str:
        path = tmp_path / name
        path.write_text(key.export_private_key("openssh").decode("ascii"))
        os.chmod(path, 0o600)
        return str(path)

    hub = servers(
        name="hub",
        loopback=(),
        muxcon=True,
        muxcon_kwargs={
            "accept": ["*"],
            "auth_required": True,
            "public_keys": [{"key_id": "op-1", "public_key": real_pub}],
        },
        start=True,
    )
    dial_to = hub.port_of("muxcon")

    def _dialer(name: str, port: str, auth: Optional[dict]) -> Any:
        return servers(
            name=name,
            loopback=(port,),
            telnet=False,
            configure=lambda builder: builder.with_muxcon(dial=[("127.0.0.1", dial_to)], advertise=[port], auth=auth),
            start=True,
        )

    real_id = _private_file("real_id", real_key)
    forged_id = _private_file("forged_id", forged_key)

    with step("the hub answers only the peer that presents the configured key_id"):
        # The initiator advertises its Ports only after AUTH:OK, so this registration line IS the proof
        # that the challenge round trip completed — no earlier frame can cause it.
        _dialer("leaf", "loop1", {"private_key": real_id, "key_id": "op-1"})
        hub.wait_for_log(f"{PORT_REGISTERED} loop1", timeout=30)

    with step("the same key_id with a different private key is refused at the signature, not the name"):
        forged = _dialer("forged", "loop9", {"private_key": forged_id, "key_id": "op-1"})
        forged.wait_for_log("MuxCon auth failed: bad signature", timeout=30)

    with step("and a dialler with no key never reaches the challenge at all"):
        nokey = _dialer("nokey", "loop8", None)
        nokey.wait_for_log("server requires a known PKID", timeout=30)

    with step("neither refused peer put a Port on the hub, because the acceptor drops Port lists it cannot authenticate"):
        # The keyless dialler treats itself as discoverable and really does send its Port list on the
        # unauthenticated link; `_ctl_ports` checks `_is_conn_authenticated` before registering anything,
        # so the list is discarded without a log and without a Port. The send line bounds the absence
        # below: the offer reached the node, and only the auth gate kept `loop8` off the list.
        nokey.wait_for_log(PORTS_SENT, timeout=30)
        names = {str(entry.get("name")) for entry in _ports(api_for_server(hub))}
        assert "loop1" in names, names
        assert not names & {"loop8", "loop9"}, f"an unauthenticated peer registered a Port: {sorted(names)}"


@surfaces(
    "server:hub",
    "server:leaf",
    "cli:unknown-node",
    "cli:wrong-origin",
    "cli:local",
    "cli:driver",
    "telnet:menu",
    "telnet:chooser",
)
def test_mxc08_the_qualified_port_name_reaches_the_node_it_names_and_only_that_node(servers, clients, story) -> None:
    """`<node>::<port>` reaches the Port at the node it names — on the menu and on the wire, and nowhere else.

    The menu prints a federated Port qualified, so the qualification has to carry weight: a resolver that
    matched the name and ignored the origin would make the menu's own label a lie. The sharpest shape is a
    name that stands on THIS node (`loop9`) while the named node does not own it — the qualifier must
    refuse it even though the bare name is a Port right here.
    """
    hub, leaf = _linked_pair(servers, hub_telnet=True, hub_loopback=("loop9",))
    hub.wait_for_log(f"{PORT_REGISTERED} loop1", timeout=30)

    with step("a qualifier naming a node that does not exist is refused, though the bare name would attach"):
        unknown = clients.cli(user="rw", surface="cli:unknown-node", server=hub)
        unknown.command("CONNECT:not-a-node::loop1")
        unknown.wait_for("ERROR:CONNECT:Port not found for given server_id", timeout=20)

    with step("and so is a real node that does not own that Port, though the Port stands on this one"):
        wrong = clients.cli(user="rw", surface="cli:wrong-origin", server=hub)
        wrong.command(f"CONNECT:{leaf.server_id}::loop9")
        wrong.wait_for("ERROR:CONNECT:Port not found for given server_id", timeout=20)
        wrong.expect_no_text("CONNECTED:", for_seconds=2.0)

    with step("local:: names this node's own Port, and attaches it"):
        local = clients.cli(user="rw", surface="cli:local", server=hub, attach_to="local::loop9")
        assert "CONNECTED:loop9:READ_WRITE" in local.wire_text(), local.wire_text()[-400:]

    with step("the qualifier that names the true owner reaches the peer's Port, and drives it"):
        driver = clients.cli(user="rw", surface="cli:driver", server=hub, attach_to=f"{leaf.server_id}::loop1")
        # The CONNECTED line carries the RESOLVED Port name, not the typed string: the qualifier is
        # consumed by resolution. The mode itself is the origin's answer — its semantics are `MXC-02`/`03`.
        assert "CONNECTED:loop1:READ_WRITE" in driver.wire_text(), driver.wire_text()[-400:]
        _origin_gave_the_slot(leaf, hub)
        driver.echo_of("mxc08", timeout=20)

    with step("the menu prints that qualifier, and refuses both wrong claims about the listed Port"):
        menu = clients.telnet(user="rw", surface="telnet:menu", server=hub, require_auth=True)
        menu.wait_for_prompt("Port:")
        assert f"{leaf.server_id}::loop1" in menu.text(), menu.text()[-400:]
        # `loop1` stands listed here, but `local::` claims a Port this node owns — the origin gate says no.
        menu.answer("local::loop1")
        menu.wait_for("Unknown port: local::loop1", timeout=20)
        menu.answer("nobody::loop1")
        menu.wait_for("Unknown port: nobody::loop1", timeout=20)

    with step("answering the prompt with the printed label attaches the console to the leaf's Port"):
        chooser = clients.telnet(user="rw", surface="telnet:chooser", server=hub, require_auth=True)
        chooser.wait_for_prompt("Port:")
        chooser.answer(f"{leaf.server_id}::loop1")
        # `i` prints `Port: <name>` only for an attached session — here the resolved short name.
        chooser.press_until("i", "Port: loop1")
        # The console that arrived over the wire holds the origin's one Write slot, so the console that
        # arrived through the menu joins it as a viewer: both paths compete in the same single queue.
        chooser.wait_for(read_only_grant_notice(Code.CAPACITY_FULL), timeout=20)


def _power_pair(servers: Any) -> Tuple[Any, Any]:
    """Two linked nodes: the leaf's ``loop1`` hangs off its dummy PDU, and each node owns a PDU of its own.

    The relayed switch is refused by ``PduAdapter.set_outlet`` on the asking node BEFORE anything reaches
    the link when that node has no power section at all — its own words are `power management is not
    configured` — so the hub needs one, and it holds an outlet (`rack9.9`) the leaf has never heard of:
    a ref can then only be relayed because its qualifier names the leaf, not because the hub lacks PDUs.
    ``loop1`` feeds off the leaf's one outlet, so a single off-switch powers a console on the OTHER node.
    """
    hub = servers(
        name="hub",
        loopback=(),
        telnet=True,
        telnet_kwargs={"require_auth": True},
        muxcon=True,
        muxcon_kwargs={"accept": ["*"]},
        configure=lambda builder: builder.with_dummy_pdu("rack9", ["9"], poll_interval=0),
        start=True,
    )
    dial_to = hub.port_of("muxcon")

    def _leaf_config(builder: Any) -> Any:
        builder.with_dummy_pdu(PDU, ["1"], poll_interval=0)
        builder.with_loopback("loop1", feeds=[PDU_FEED])
        return builder.with_muxcon(dial=[("127.0.0.1", dial_to)], advertise=["loop1"])

    leaf = servers(name="leaf", loopback=(), telnet=False, configure=_leaf_config, start=True)
    hub.wait_for_log("Handshake OK (server-side)", timeout=30)
    hub.wait_for_log(f"{PORT_REGISTERED} loop1", timeout=30)
    return hub, leaf


def _outlet_on(api: Any, ref: str) -> Optional[bool]:
    """What the node owning this power snapshot reports for one LOCAL outlet ref: True, False, or None."""
    for pdu in (api.power() or {}).get("pdus") or []:
        for entry in pdu.get("outlets") or []:
            if entry.get("ref") == ref:
                return bool(entry.get("on"))
    return None


@surfaces("server:hub", "server:leaf", "cli:guest", "telnet:menu", "api:power")
def test_mxc09_a_switch_typed_on_one_node_moves_the_outlet_on_the_other(servers, clients, story, api_for_server) -> None:
    """A federated outlet moves only by request, and only from a console that drives a Port that outlet powers.

    The qualified ref names an outlet on ANOTHER node, so this node cannot honour it: what crosses the link
    is a switch request anchored on the acting console's own session, and the origin re-verifies the mirror
    of that session before running its own local switch. A console that drives nothing has nothing to
    anchor on — and the anchored switch runs as the origin's OWN relayed pseudo-client, never as the typing
    user's name. The refusal keeps a viewer node from switching a peer's power by name alone.
    """
    hub, leaf = _power_pair(servers)
    qualified = f"{leaf.server_id}::{PDU_FEED}"

    with step("a console driving no Port cannot move the peer's outlet, and the outlet stays where it was"):
        guest = clients.cli(user="rw", surface="cli:guest", server=hub)
        guest.command(f"POWER {qualified} off")
        guest.wait_for(f"ERROR:POWER: {qualified} is owned by federated node {leaf.server_id}", timeout=20)
        guest.wait_for("switch it from a session attached to a port it powers", timeout=20)
        assert _outlet_on(api_for_server(leaf), PDU_FEED) is True, "the refused switch moved the outlet anyway"

    with step("the console driving the Port this outlet powers sees the peer's outlet among its own feeds"):
        menu = clients.telnet(user="rw", surface="telnet:menu", server=hub, require_auth=True)
        menu.choose_port("loop1")
        menu.press("p")
        menu.wait_for("POWER: feeds for loop1  (a number = toggle, a = all, Enter = exit)", timeout=20)
        # The row carries the QUALIFIED ref: the hub caches the feed under the global name, which is what
        # makes it switchable here at all and distinguishable from a same-named local outlet.
        menu.wait_for(f" 1  [on]   {qualified}", timeout=20)

    with step("toggling that row crosses the link, and the origin audits the switch as its own relayed session"):
        menu.answer("1")
        menu.wait_for(f"POWER {qualified} -> off", timeout=20)
        line = leaf.wait_for_log("POWER CONTROL:", timeout=15)
        # The audit reads like any local switch on the origin — local ref, and the origin's own fed
        # pseudo-client as the user, because that mirror session is what the request was anchored on.
        assert f"turned {PDU_FEED} off" in line, line
        assert "fed:node:" in line, f"the relayed switch was audited under the wrong identity: {line}"
        assert _outlet_on(api_for_server(leaf), PDU_FEED) is False

    with step("the state update flowing back powers off the typing console's own notice, qualified ref and all"):
        # The hub never owned rack1.1 — this warning can only come from the POWER:STATE update applied to
        # the proxy Port and re-emitted under the global ref. It is the flip side of the audit line above.
        menu.wait_for(f"[POWER WARNING] all power feeds are now OFF for this console ({qualified})", timeout=20)
        menu.wait_for(f" 1  [off]  {qualified}", timeout=20)

    with step("and switching it back on the same menu returns the owner's outlet, then the menu exits on Enter"):
        menu.answer("1")
        menu.wait_for(f"POWER {qualified} -> on", timeout=20)
        menu.wait_for(f"[POWER] feed {qualified} is now on", timeout=20)
        assert _outlet_on(api_for_server(leaf), PDU_FEED) is True
        menu.answer("")
        menu.wait_for("[EXITING POWER]", timeout=20)


@surfaces("server:hub", "server:leaf", "web:editor", "api:ports")
def test_mxc10_the_config_editor_writes_a_live_link_into_a_node_that_had_none(servers, story, api_for_server) -> None:
    """The federation link is a config section like any other: the editor saves it, and a SOFT reload runs it.

    The leaf starts with no `muxcon` section at all — no adapter exists to reconcile — and the answering hub
    therefore knows nothing of its Port either. What turns that into a live link is exactly the two shipped
    editor steps: `/config-editor/apply` replaces the WHOLE config (the payload is what `/data` served, with
    the section added), and the save itself starts nothing. Only the soft reload bootstraps the adapter the
    node never had and starts the initiator the section names, and the reload summary is where it admits
    doing so.
    """
    hub = servers(
        name="hub",
        loopback=(),
        telnet=False,
        muxcon=True,
        muxcon_kwargs={"accept": ["loop1"], "use_tls": False},
        start=True,
    )
    leaf = servers(name="leaf", loopback=("loop1",), telnet=False, start=True)
    hub_fed_port = hub.port_of("muxcon")
    api = api_for_server(leaf)

    with step(
        "the leaf holds no link to break, its policy calls the federated section writable, and the hub owns no peer Port"
    ):
        data = _data(api)
        assert (
            "muxcon" in data["writable_sections"]
        ), f"policy will not let this node save its own link: {data['writable_sections']}"
        assert "muxcon" not in data["config"], "the leaf was built with a muxcon section; the write would prove nothing"
        assert "loop1" not in [str(port.get("name")) for port in _ports(api_for_server(hub))]

    with step("a section carrying a key the schema does not model is refused whole, and the leaf still holds no link"):
        malformed = {"auth_required": False, "initiators": [], "not_a_link_key": True}
        refused = _apply(api, dict(_payload(api), muxcon=malformed), expect=400)
        assert "muxcon" in str(refused), f"the refusal did not name the section it refused: {refused}"
        assert "muxcon" not in _config(api), "the refused save reached the running config anyway"

    with step("the real section saves clean, and it is the SOFT reload that bootstrap-starts the initiator"):
        link = {
            "auth_required": False,
            "federated_cache_enabled": False,
            "listeners": [],
            "initiators": [{"host": "127.0.0.1", "port": hub_fed_port, "use_tls": False}],
            "advertise_filters": {"include": ["loop1"]},
        }
        assert _apply(api, dict(_payload(api), muxcon=link)) == {"ok": True}
        # The write started nothing on its own — the link exists only once the reload summary admits it.
        muxcon_now = _reconciled(_summary(_reload(api, "soft")), "muxcon")
        assert muxcon_now["initiators"]["added"] == [f"127.0.0.1:{hub_fed_port}"], muxcon_now

    with step("and the saved section is a live link: the hub answers, registers the Port, and the editor reads it back"):
        hub.wait_for_log("Handshake OK (server-side)", timeout=30)
        hub.wait_for_log(f"{PORT_REGISTERED} loop1", timeout=20)
        saved = _config(api)["muxcon"]
        assert [entry["port"] for entry in saved["initiators"]] == [hub_fed_port], saved
