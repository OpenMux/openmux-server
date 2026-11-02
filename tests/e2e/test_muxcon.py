"""MUXCON: two real nodes, one link, and what each side may see and do (MXC-01 to MXC-04).

Federation was proven only as an empty state before these ids: `ST-04` shows the status page saying
`No federation connections`, and the unit suites drive `UnifiedMuxConAdapter` with fakes. No test started a
second `openmux.server.main` process and let it dial the first over a socket. These ids do, on two servers
in one test - which the harness already did for `CFG-07` and `PWR-03` - and add the one capability it
lacked: a `muxcon:` section, which `ConfigBuilder.with_muxcon` now writes (issue #111).

Four measured facts shape these bodies, and each is stated where it is used:

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
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

import pytest

from openmux.server.access_decision import Code, read_only_grant_notice, write_blocked_notice

from .story import step, surfaces

pytestmark = [pytest.mark.e2e, pytest.mark.feature]

#: What the answering side logs once a peer Port is in its own Port list. The count line
#: `Registered <n> federated ports` counts what arrived before the accept filter and fires even when every
#: Port was dropped, so only this per-Port line proves the Port became usable here.
PORT_REGISTERED = "Registered federated port:"

#: What the dialling side logs after it sends the list of Ports it offers.
PORTS_SENT = "local ports in PORTS:FEDERATED response"


def _linked_pair(
    servers: Any,
    *,
    port: str = "loop1",
    hub_accept: Optional[List[str]] = None,
    leaf_advertise: Optional[List[str]] = None,
    hub_telnet: bool = False,
) -> Tuple[Any, Any]:
    """Start two servers, the second dialling the first, and return ``(hub, leaf)``.

    The hub answers on a federation port the factory allocated, so ``wait_ready`` proves the listener bound:
    a listener that fails to bind only logs an error, and these servers run with ``fail_fast_adapters:
    false``. The leaf owns ``port`` and the hub owns nothing, so a Port of that name on the hub can only
    have come from the peer.
    """
    hub = servers(
        name="hub",
        loopback=(),
        telnet=hub_telnet,
        telnet_kwargs={"require_auth": True} if hub_telnet else None,
        muxcon=True,
        muxcon_kwargs={"accept": hub_accept if hub_accept is not None else ["*"]},
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
