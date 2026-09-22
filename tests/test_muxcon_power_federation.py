"""Tests for PDU power outlet federation (MuxCon).

Covers the wire + state half of the feature:
- PortMetadata.power serialization (present / absent).
- Advertising: a local port's power feeds + states ride in PORTS:FEDERATED.
- Registration: a federated port dict carrying power produces a proxy with
  the ref list + state cache (incl. malformed-entry tolerance).
- POWER:STATE frame: origin-side broadcast (authenticated only, local ports
  only), peer-side apply (meta event shape, all-lost math, unknown ref,
  unauthenticated ignore), and no-echo (remote proxies are not re-broadcast).
- Federated cache: save/load round-trip of the power field.
- Re-advertise refresh: POWER:STATE-cached states are replaced by the fresh
  metadata; changed refs re-emit power_outlet_changed, a pure removal emits
  nothing (the badge/menu re-renders from the refreshed list).
"""

import asyncio
import json
import time

import pytest

from openmux.common.federation_types import PortMetadata, ServerInfo, ServerType
from openmux.server.adapters.muxcon import UnifiedMuxConAdapter
from openmux.server.port_manager import PortManager

ORIGIN_ID = "peerO"
ORIGIN = {
    "server_id": ORIGIN_ID,
    "hostname": "peerO",
    "port": 0,
    "server_type": "leaf",
    "description": "",
}


def _server_info(sid: str, host: str) -> ServerInfo:
    return ServerInfo(server_id=sid, hostname=host, port=0, server_type=ServerType.LEAF, description="")


def _origin_meta(name: str = "r1") -> PortMetadata:
    si = _server_info(ORIGIN_ID, "peerO")
    return PortMetadata(
        name=name,
        original_name=name,
        description="",
        adapter_type="remote_muxcon",
        origin_server=si,
        server_chain=[si],
        status="connected",
    )


class DummyWriter:
    """Records every byte written (no real transport needed)."""

    def __init__(self):
        self.data = bytearray()

    def write(self, data: bytes):
        self.data.extend(data)

    async def drain(self):
        return

    def is_closing(self):
        return False

    def close(self):
        return

    async def wait_closed(self):
        return


class FakePdu:
    """Minimal power-adapter stand-in so feed_states is available on advertise."""

    def __init__(self, states):
        self._states = states

    def get_adapter_type(self) -> str:
        return "power"

    def feed_states(self, port_name):
        return dict(self._states.get(port_name) or {})


class FakePM:
    """Listing-only port manager for the advertise path."""

    def __init__(self, entries, adapters=()):
        self._entries = list(entries)
        self.unified_adapters = list(adapters)

    async def get_port_list_with_federation(self):
        return self._entries


async def _make_stream_pair():
    server_side = {}

    async def handle(reader, writer):
        server_side["reader"] = reader
        server_side["writer"] = writer

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    client_reader, client_writer = await asyncio.open_connection(host, port)
    while "reader" not in server_side:
        await asyncio.sleep(0)
    return server, server_side["reader"], server_side["writer"], client_reader, client_writer


def _frame_payload(header_line: bytes) -> str:
    """Extract the ASCII payload from the leading '#sid:T:len:seq:' header line.

    Control frame payload may itself contain newlines, so only the FIRST
    physical line (up to the first embedded newline) is captured here; callers
    read the remaining body lines separately.
    """
    body = header_line.decode("utf-8").rstrip("\n")
    inner = body[body.index("#") + 1 :]
    return inner.split(":", 4)[4]


def _close_pair(server, s_writer, c_writer):
    s_writer.close()
    c_writer.close()
    server.close()


async def _read_power_state_frame(c_reader):
    """Read a two-line POWER:STATE control frame; return (port_name, body_dict)."""
    hline = await asyncio.wait_for(c_reader.readline(), timeout=1)
    assert hline, "no frame line received"
    hline = hline[: hline.index(b"\n")] if b"\n" in hline else hline
    payload = _frame_payload(hline)
    assert payload.startswith("POWER:STATE:"), payload
    port_name = payload[len("POWER:STATE:") :]
    body = json.loads((await asyncio.wait_for(c_reader.readline(), timeout=1)).decode("utf-8"))
    return port_name, body


async def _read_ports_federated(c_reader):
    """Read the leading header line + one line per port until END:PORTS.

    Feed refs are GLOBALLY qualified on the wire ("<server_id>::<ref>"); this
    strips the local-node prefix so the LOCAL ref form is asserted, which is
    what registration then re-qualifies with the ORIGIN's id (outlet
    federation).
    """
    hline = await asyncio.wait_for(c_reader.readline(), timeout=1)
    assert hline, "no header line received"
    lines = []
    while True:
        part = await asyncio.wait_for(c_reader.readline(), timeout=1)
        if not part:
            break
        txt = part.decode("utf-8").rstrip("\n")
        if txt == "END:PORTS" or "END:PORTS" in txt:
            break
        lines.append(txt)
    entries = [json.loads(l) for l in lines if l.strip()]
    for e in entries:
        if isinstance(e.get("power"), list):
            e["power"] = [
                {**f, "ref": "::".join(str(f.get("ref")).split("::", 1)[1:])} for f in e["power"] if isinstance(f, dict)
            ]
    return entries


# --- PortMetadata.power serialization ----------------------------------------


def test_port_metadata_power_serializes_when_present():
    meta = _origin_meta()
    meta.power = [{"ref": "rack1.1", "on": True}, {"ref": "rack1.2", "on": None}]
    d = meta.to_federation_dict()
    assert d["power"] == [{"ref": "rack1.1", "on": True}, {"ref": "rack1.2", "on": None}]
    assert meta.to_dict()["power"] == d["power"]


def test_port_metadata_power_omitted_when_absent():
    meta = _origin_meta()
    assert "power" not in meta.to_federation_dict()
    assert "power" not in meta.to_dict()


# --- Advertise ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_advertise_includes_power_feed_states():
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {"listeners": [{"host": "127.0.0.1", "port": 9000, "enabled": True}]}})
    pm = FakePM(
        [
            {"name": "p1", "adapter_type": "loopback", "description": "", "connected": True, "max_read_write_users": 1},
            {"name": "p2", "adapter_type": "loopback", "description": "", "connected": True},
        ],
        adapters=[FakePdu({"p1": {"rack1.1": True, "rack1.2": None}})],
    )
    ad.main_port_manager = pm
    ad._adv_name_inc = ["*"]  # default-deny (#77): opt into advertise-all

    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        conn_id = "in:127.0.0.1:12345:1"
        ad.connections[conn_id] = {"writer": s_writer, "auth_ok": True}
        ad._wire_state[conn_id] = {"send_next": 1}
        await ad._maybe_advertise_local_ports(conn_id)
        by_name = {e["name"]: e for e in await _read_ports_federated(c_reader)}
        assert by_name["p1"]["power"] == [{"ref": "rack1.1", "on": True}, {"ref": "rack1.2", "on": None}]
        assert "power" not in by_name["p2"]  # p2 has no feeds: field omitted
    finally:
        _close_pair(server, s_writer, c_writer)
        c_writer.close()


@pytest.mark.asyncio
async def test_advertise_omits_power_without_pdu_adapter():
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {"listeners": [{"host": "127.0.0.1", "port": 9000, "enabled": True}]}})
    pm = FakePM([{"name": "p1", "adapter_type": "loopback", "description": "", "connected": True}])
    ad.main_port_manager = pm
    ad._adv_name_inc = ["*"]

    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        conn_id = "in:127.0.0.1:12345:2"
        ad.connections[conn_id] = {"writer": s_writer, "auth_ok": True}
        ad._wire_state[conn_id] = {"send_next": 1}
        await ad._maybe_advertise_local_ports(conn_id)
        entries = await _read_ports_federated(c_reader)
        assert entries and "power" not in entries[0]
    finally:
        _close_pair(server, s_writer, c_writer)
        c_writer.close()


# --- Registration -------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_remote_port_parses_power():
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = PortManager([])
    pm.set_unified_adapters([ad])
    pd = {
        "name": "r1",
        "description": "Remote Port 1",
        "adapter_type": "remote_muxcon",
        "origin_server": ORIGIN,
        "status": "connected",
        "max_rw_users": 1,
        "power": [{"ref": "rack1.1", "on": True}, {"ref": "rack1.2", "on": False}, "rack1.3"],
    }
    await ad._register_remote_port_from_dict("in:127.0.0.1:9999:1", pd)
    proxy = pm.ports["r1"]
    # The origin's local feed refs are stored GLOBALLY qualified
    # ("<origin>::<ref>"), so a same-named outlet on another node never
    # collides (outlet federation).
    assert proxy.power == ["peerO::rack1.1", "peerO::rack1.2", "peerO::rack1.3"]
    assert proxy._feed_states == {"peerO::rack1.1": True, "peerO::rack1.2": False, "peerO::rack1.3": None}
    assert proxy.metadata.power == [
        {"ref": "peerO::rack1.1", "on": True},
        {"ref": "peerO::rack1.2", "on": False},
        {"ref": "peerO::rack1.3", "on": None},
    ]


@pytest.mark.asyncio
async def test_register_remote_port_drops_malformed_power_entries():
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = PortManager([])
    pm.set_unified_adapters([ad])
    pd = {
        "name": "r2",
        "origin_server": ORIGIN,
        "status": "connected",
        "power": [{"on": True}, {"ref": ""}, 42, None, {"ref": "rack1.1", "on": "bogus"}, "rack1.2"],
    }
    await ad._register_remote_port_from_dict("in:127.0.0.1:9999:2", pd)
    proxy = pm.ports["r2"]
    assert proxy.power == ["peerO::rack1.1", "peerO::rack1.2"]
    assert proxy._feed_states == {"peerO::rack1.1": None, "peerO::rack1.2": None}


@pytest.mark.asyncio
async def test_register_remote_port_without_power_has_no_feeds():
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = PortManager([])
    pm.set_unified_adapters([ad])
    pd = {"name": "r3", "origin_server": ORIGIN, "status": "connected"}
    await ad._register_remote_port_from_dict("in:127.0.0.1:9999:3", pd)
    proxy = pm.ports["r3"]
    assert proxy.power == []
    assert proxy._feed_states == {}
    assert proxy.metadata.power is None


# --- POWER:STATE origin broadcast ----------------------------------------------


@pytest.mark.asyncio
async def test_power_relay_broadcasts_for_local_ports():
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = PortManager([])
    pm.ports["lp"] = type("L", (), {})()  # a genuine local port (no remote_port_name)
    ad.main_port_manager = pm  # setter wires _on_port_meta_for_power_relay

    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        conn_id = "in:127.0.0.1:1:1"
        ad.connections[conn_id] = {"writer": s_writer, "auth_ok": True}
        ad._wire_state[conn_id] = {"send_next": 1}
        pm.notify_meta_updated("lp", {"event": "power_outlet_changed", "outlet": "rack1.1", "on": False})
        await asyncio.sleep(0.05)  # let the scheduled relay task run
        port_name, body = await _read_power_state_frame(c_reader)
        assert port_name == "lp"
        assert body == {"ref": "rack1.1", "on": False}
    finally:
        _close_pair(server, s_writer, c_writer)
        c_writer.close()


@pytest.mark.asyncio
async def test_power_relay_skips_remote_ports_and_non_power_events():
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = PortManager([])
    pm.ports["r1"] = type("P", (), {"remote_port_name": "r1"})()  # a remote proxy
    ad.main_port_manager = pm

    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        conn_id = "in:127.0.0.1:1:2"
        ad.connections[conn_id] = {"writer": s_writer, "auth_ok": True}
        ad._wire_state[conn_id] = {"send_next": 1}
        # Remote proxy: its origin already sent POWER:STATE; never re-relayed.
        pm.notify_meta_updated("r1", {"event": "power_outlet_changed", "outlet": "rack1.1", "on": True})
        # Non-power event on the same port: ignored.
        pm.notify_meta_updated("r1", {"event": "federated_status_message_changed"})
        # Give any (mis)broadcast a chance to land, then assert nothing did.
        await asyncio.sleep(0.05)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(c_reader.readline(), timeout=0.1)
    finally:
        _close_pair(server, s_writer, c_writer)
        c_writer.close()


@pytest.mark.asyncio
async def test_power_relay_skips_unauthenticated_conn():
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = PortManager([])
    pm.ports["lp"] = type("L", (), {})()
    ad.main_port_manager = pm

    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        conn_id = "in:127.0.0.1:1:3"
        ad.connections[conn_id] = {"writer": s_writer, "auth_ok": False}
        ad._wire_state[conn_id] = {"send_next": 1}
        pm.notify_meta_updated("lp", {"event": "power_outlet_changed", "outlet": "rack1.1", "on": True})
        await asyncio.sleep(0.05)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(c_reader.readline(), timeout=0.1)
    finally:
        _close_pair(server, s_writer, c_writer)
        c_writer.close()


# --- POWER:STATE peer apply ----------------------------------------------------


async def _peer_with_registered_port(send_sid: str = "peerO"):
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = PortManager([])
    pm.set_unified_adapters([ad])
    events = []
    pm.register_meta_listener(lambda p, c: events.append((p, c or {})))
    # The conn record gives both the PROXY registration and POWER:STATE sender
    # scoping a server identity (same origin peer group).
    ad.connections["in:127.0.0.1:9:1"] = {"server_id": send_sid}
    ad.connections["in:anyone:1"] = {"server_id": send_sid}
    pd = {
        "name": "r1",
        "origin_server": ORIGIN,
        "status": "connected",
        "power": [{"ref": "rack1.1", "on": True}, {"ref": "rack1.2", "on": True}],
    }
    await ad._register_remote_port_from_dict("in:127.0.0.1:9:1", pd)
    return ad, pm, events


@pytest.mark.asyncio
async def test_power_state_frame_applies_and_fires_meta():
    ad, pm, events = await _peer_with_registered_port()
    await ad._handle_power_state_frame("in:anyone:1", 'POWER:STATE:r1\n{"ref":"rack1.1","on":false}')
    proxy = pm.ports["r1"]
    # States are cached under the GLOBAL ref ("peerO::<ref>").
    assert proxy._feed_states["peerO::rack1.1"] is False
    assert proxy._feed_states["peerO::rack1.2"] is True
    ev = [c for p, c in events if p == "r1" and c.get("event") == "power_outlet_changed"]
    assert len(ev) == 1
    assert ev[0]["outlet"] == "peerO::rack1.1"
    assert ev[0]["on"] is False
    assert ev[0]["all_power_lost"] is False
    assert ev[0]["other_outlets_on"] == ["peerO::rack1.2"]
    on_vals = {f["ref"]: f["on"] for f in proxy.metadata.power}
    assert on_vals == {"peerO::rack1.1": False, "peerO::rack1.2": True}


@pytest.mark.asyncio
async def test_power_state_frame_all_lost_math():
    ad, pm, events = await _peer_with_registered_port()
    await ad._handle_power_state_frame("in:anyone:1", 'POWER:STATE:r1\n{"ref":"rack1.1","on":false}')
    await ad._handle_power_state_frame("in:anyone:1", 'POWER:STATE:r1\n{"ref":"rack1.2","on":false}')
    assert pm.ports["r1"]._feed_states == {"peerO::rack1.1": False, "peerO::rack1.2": False}
    lost = [c for p, c in events if c.get("event") == "power_outlet_changed" and c.get("all_power_lost")]
    assert len(lost) == 1  # only after the second feed drops
    await ad._handle_power_state_frame("in:anyone:1", 'POWER:STATE:r1\n{"ref":"rack1.1","on":true}')
    back = [c for p, c in events if c.get("event") == "power_outlet_changed" and c.get("on") is True]
    assert len(back) == 1
    assert back[0]["all_power_lost"] is False


@pytest.mark.asyncio
async def test_power_state_frame_applies_to_all_sending_peers_proxies():
    # Two proxies from THE SAME peer both fed by the ref: the sender-scoped
    # frame applies to both of them + the meta event for each port. A proxy
    # from a DIFFERENT origin holding the same local ref stays untouched.
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = PortManager([])
    pm.set_unified_adapters([ad])
    events = []
    pm.register_meta_listener(lambda p, c: events.append((p, c or {})))
    for name in ("ra", "rb"):
        ad.connections[f"in:127.0.0.1:{name}:1"] = {"server_id": "peerO"}
        pd = {
            "name": name,
            "origin_server": dict(ORIGIN, server_id="peerO"),
            "status": "connected",
            "power": [{"ref": "shared.7", "on": True}],
        }
        await ad._register_remote_port_from_dict(f"in:127.0.0.1:{name}:1", pd)
    # A foreign origin with its OWN "shared.7" must not see the update.
    ad.connections["in:127.0.0.1:rz:1"] = {"server_id": "otherZ"}
    pdz = {
        "name": "rz",
        "origin_server": dict(ORIGIN, server_id="otherZ"),
        "status": "connected",
        "power": [{"ref": "shared.7", "on": True}],
    }
    await ad._register_remote_port_from_dict("in:127.0.0.1:rz:1", pdz)
    # The frame arrives from the peerO group.
    ad.connections["in:anyone:1"] = {"server_id": "peerO"}
    await ad._handle_power_state_frame("in:anyone:1", 'POWER:STATE:whatever\n{"ref":"shared.7","on":false}')
    assert pm.ports["ra"]._feed_states["peerO::shared.7"] is False
    assert pm.ports["rb"]._feed_states["peerO::shared.7"] is False
    assert pm.ports["rz"]._feed_states["otherZ::shared.7"] is True  # untouched
    changed = {p for p, c in events if c.get("event") == "power_outlet_changed"}
    assert changed == {"ra", "rb"}


@pytest.mark.asyncio
async def test_power_state_frame_unknown_ref_ignored():
    ad, pm, events = await _peer_with_registered_port()
    await ad._handle_power_state_frame("in:anyone:1", 'POWER:STATE:r1\n{"ref":"other.9","on":true}')
    assert pm.ports["r1"]._feed_states == {"peerO::rack1.1": True, "peerO::rack1.2": True}
    power_events = [c for p, c in events if c.get("event") == "power_outlet_changed"]
    assert power_events == []


@pytest.mark.asyncio
async def test_power_state_frame_requires_auth_via_dispatch():
    ad, pm, _events = await _peer_with_registered_port()
    conn_id = "in:127.0.0.1:8:8"
    # An authenticated muxcon conn carries the peer's handshake identity
    # (server id) in its record - that is how the sender's peer group is
    # derived on the other side.
    ad.connections[conn_id] = {"writer": DummyWriter(), "auth_ok": False, "server_id": "peerO"}
    ad._wire_state[conn_id] = {"send_next": 1}
    # Unauthenticated conn: the POWER:STATE branch drops the frame.
    await ad._process_control_command(conn_id, DummyWriter(), 'POWER:STATE:r1\n{"ref":"rack1.1","on":false}')
    # States are cached under the GLOBAL ref (outlet federation).
    assert pm.ports["r1"]._feed_states["peerO::rack1.1"] is True  # untouched
    # Authenticated conn: the same dispatch path now applies it.
    ad.connections[conn_id]["auth_ok"] = True
    await ad._process_control_command(conn_id, DummyWriter(), 'POWER:STATE:r1\n{"ref":"rack1.1","on":false}')
    assert pm.ports["r1"]._feed_states["peerO::rack1.1"] is False


@pytest.mark.asyncio
async def test_power_state_frame_malformed_ignored():
    ad, pm, events = await _peer_with_registered_port()
    for bad in (
        "POWER:STATE:r1",  # no body line
        "POWER:STATE:r1\n{not json",
        'POWER:STATE:r1\n{"ref":"","on":false}',
        'OTHER:cmd\n{"ref":"rack1.1","on":false}',
    ):
        await ad._handle_power_state_frame("in:anyone:9", bad)
    assert pm.ports["r1"]._feed_states == {"peerO::rack1.1": True, "peerO::rack1.2": True}
    power_events = [c for p, c in events if c.get("event") == "power_outlet_changed"]
    assert power_events == []


@pytest.mark.asyncio
async def test_power_state_same_ref_different_nodes_isolated():
    # The collision regression: two peers both have an outlet named rack1.1
    # and the local node has one too. A frame from one origin must update
    # ONLY that origin's proxy, never the other proxy or the local port.
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = PortManager([])
    pm.set_unified_adapters([ad])
    events = []
    pm.register_meta_listener(lambda p, c: events.append((p, c or {})))
    local = type("L", (), {"name": "lp", "power": ["rack1.1"]})()
    pm.ports["lp"] = local
    for name in ("ra", "rb"):
        # Each origin registers over its own conn carrying its server id.
        ad.connections[f"in:127.0.0.1:{name}:1"] = {"server_id": name}
        pd = {
            "name": name,
            "origin_server": dict(ORIGIN, server_id=name),
            "status": "connected",
            "power": [{"ref": "rack1.1", "on": True}],
        }
        await ad._register_remote_port_from_dict(f"in:127.0.0.1:{name}:1", pd)
    # The frame arrives from the "ra" peer group (sender scoping identity).
    ad.connections["in:anyone:1"] = {"server_id": "ra"}
    # The "ra" origin turns its rack1.1 off: only the ra proxy changes.
    await ad._handle_power_state_frame("in:anyone:1", 'POWER:STATE:lp\n{"ref":"rack1.1","on":false}')
    assert pm.ports["ra"]._feed_states == {"ra::rack1.1": False}
    assert pm.ports["rb"]._feed_states == {"rb::rack1.1": True}  # untouched
    power_events = [c for p, c in events if c.get("event") == "power_outlet_changed"]
    changed = {p for p, c in events if c.get("event") == "power_outlet_changed"}
    assert changed == {"ra"}
    assert all(c["outlet"] == "ra::rack1.1" for c in power_events)


# --- Federated cache round-trip -------------------------------------------------


@pytest.mark.asyncio
async def test_federated_cache_round_trips_power(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMUX_STATE_DIR", str(tmp_path / "state"))
    import openmux.server.locations as locations

    locations._ENV_FILE_CACHE = None

    cache_file = tmp_path / "state" / "muxcon" / "federated_cache.json"
    ad1 = UnifiedMuxConAdapter("mx1", {"muxcon": {"federated_cache_enabled": True}})
    pm1 = PortManager([])
    pm1.set_unified_adapters([ad1])
    conn_id = "in:10.0.0.1:8888:1"
    ad1.connections[conn_id] = {"server_id": "peerP", "opened_at": time.time()}
    pd = {
        "name": "rx2",
        "origin_server": ORIGIN,
        "status": "connected",
        "power": [{"ref": "rack1.1", "on": True}, {"ref": "rack1.2", "on": None}],
    }
    await ad1._register_remote_port_from_dict(conn_id, pd)
    assert "rx2" in pm1.ports
    pr = pm1.ports["rx2"]
    pr.is_connected = False
    pr.last_seen = time.time() - 1
    ad1._save_federated_cache()
    assert cache_file.exists()
    saved = json.loads(cache_file.read_text())
    power_saved = None
    for ports in saved["peers"].values():
        if "rx2" in ports:
            power_saved = ports["rx2"]["power"]
    # The cache keeps the GLOBAL (prefixed) form; restore re-derives from it.
    assert power_saved == [{"ref": "peerO::rack1.1", "on": True}, {"ref": "peerO::rack1.2", "on": None}]

    ad2 = UnifiedMuxConAdapter("mx2", {"muxcon": {"federated_cache_enabled": True}})
    pm2 = PortManager([])
    pm2.set_unified_adapters([ad2])
    ad2.main_port_manager = pm2
    await ad2._load_federated_cache()
    pr2 = pm2.ports["rx2"]
    assert pr2.power == ["peerO::rack1.1", "peerO::rack1.2"]
    assert pr2._feed_states == {"peerO::rack1.1": True, "peerO::rack1.2": None}
    # The restore path must NOT double-prefix an already-global ref.
    assert all(r.count("::") == 1 for r in pr2.power)


# --- Re-advertise refresh ------------------------------------------------------


async def _peer_ready_for_readvertise():
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = PortManager([])
    pm.set_unified_adapters([ad])
    events = []
    pm.register_meta_listener(lambda p, c: events.append((p, c or {})))
    conn_id = "in:127.0.0.1:7:7"
    ad.connections[conn_id] = {"writer": DummyWriter(), "auth_ok": True, "server_id": ORIGIN_ID}
    ad._wire_state[conn_id] = {"send_next": 1}
    pd = {
        "name": "r1",
        "origin_server": ORIGIN,
        "status": "connected",
        "power": [{"ref": "rack1.1", "on": True}, {"ref": "rack1.2", "on": True}],
    }
    await ad._register_remote_port_from_dict(conn_id, pd)
    return ad, pm, pm.ports["r1"], events


@pytest.mark.asyncio
async def test_readvertise_refreshes_power_states_and_notifies():
    ad, pm, proxy, events = await _peer_ready_for_readvertise()
    proxy._feed_states["peerO::rack1.1"] = False  # a cached POWER:STATE change
    # Fresh metadata carries the origin's LOCAL refs; the refresh re-qualifies
    # them globally (no double-prefix).
    meta = _origin_meta("r1")
    meta.power = [{"ref": "rack1.1", "on": True}, {"ref": "rack1.2", "on": False}]
    proxy.metadata = meta  # mirror the reuse path: fresh metadata is assigned first
    await ad._apply_power_meta_to_proxy(proxy, meta)
    assert proxy.power == ["peerO::rack1.1", "peerO::rack1.2"]
    assert proxy._feed_states == {"peerO::rack1.1": True, "peerO::rack1.2": False}
    on_vals = {f["ref"]: f["on"] for f in proxy.metadata.power}
    assert on_vals == {"peerO::rack1.1": True, "peerO::rack1.2": False}
    changed = [c for p, c in events if p == "r1" and c.get("event") == "power_outlet_changed"]
    assert {c["outlet"] for c in changed} == {"peerO::rack1.1", "peerO::rack1.2"}
    rack12 = [c for c in changed if c["outlet"] == "peerO::rack1.2"][0]
    assert rack12["on"] is False and rack12["all_power_lost"] is False


@pytest.mark.asyncio
async def test_readvertise_handles_feed_removal():
    ad, pm, proxy, events = await _peer_ready_for_readvertise()
    meta = _origin_meta("r1")
    meta.power = [{"ref": "rack1.1", "on": True}]  # origin removed rack1.2
    proxy.metadata = meta  # mirror the reuse path: fresh metadata is assigned first
    await ad._apply_power_meta_to_proxy(proxy, meta)
    assert proxy.power == ["peerO::rack1.1"]
    assert proxy._feed_states == {"peerO::rack1.1": True}
    on_vals = {f["ref"]: f["on"] for f in proxy.metadata.power}
    assert on_vals == {"peerO::rack1.1": True}
    # A pure removal emits no meta event (the badge/menu re-renders from the
    # refreshed list); the surviving feed is unchanged, so no event for it either.
    power_events = [c for p, c in events if c.get("event") == "power_outlet_changed"]
    assert power_events == []


# --- POWER:SWITCH / POWER:RESULT (outlet federation switch relay) -------------


class _FakeSwitchPdu:
    """Minimal power adapter: a local PDU whose set_outlet records the call."""

    def __init__(self):
        self.calls = []

    def get_adapter_type(self):
        return "power"

    @staticmethod
    def _refs_of(port_obj):
        inner = getattr(port_obj, "unified_port", port_obj)
        refs = getattr(inner, "power", None)
        return [str(r) for r in refs] if isinstance(refs, (list, tuple)) else []

    def _port_objects(self):
        pm = self.main_port_manager
        return [(name, obj) for name, obj in (getattr(pm, "ports", {}) or {}).items()]

    async def set_outlet(self, ref, on, user=None, client_id=None):
        self.calls.append({"ref": ref, "on": on, "user": user, "client_id": client_id})
        return {
            "ok": True,
            "reading": {"on": bool(on)},
            "impact": {"change": "on" if on else "off", "losing_power": [], "staying_up": []},
        }


async def _origin_for_switch(port_name="lp", fed_mode="read-write", pdu=None):
    """Origin-side adapter with one local session from the peer (FEDRW-granted)."""
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = PortManager([])
    port = type(
        "L",
        (),
        {
            "name": port_name,
            "power": ["rack1.1", "rack1.2"],
            "unified_port": None,
            "connected_clients": [{"client_id": "fed:node:peerO:7", "mode": fed_mode}],
        },
    )()
    port.unified_port = port
    pm.ports[port_name] = port
    if pdu is not None:
        pdu.main_port_manager = pm
    pm.unified_adapters = [pdu] if pdu is not None else []
    ad.main_port_manager = pm
    ad._local_session_map["node:peerO"] = {7: port_name}
    return ad, pm, pdu


def _switch_payload(port_name, sid, body):
    return f"POWER:SWITCH:{port_name}:{sid}\n" + json.dumps(body, separators=(",", ":"))


async def _read_power_result(c_reader):
    hline = await asyncio.wait_for(c_reader.readline(), timeout=1)
    assert hline, "no frame line received"
    hline = hline[: hline.index(b"\n")] if b"\n" in hline else hline
    payload = _frame_payload(hline)
    assert payload.startswith("POWER:RESULT:"), payload
    sid = int(payload[len("POWER:RESULT:") :])
    body = json.loads((await asyncio.wait_for(c_reader.readline(), timeout=1)).decode("utf-8"))
    return sid, body


async def _setup_peer_link(ad, s_writer):
    """Register an authenticated peer connection (adapter + mpath group)."""
    conn_id = "in:127.0.0.1:70000:1"
    ad.connections[conn_id] = {"writer": s_writer, "auth_ok": True, "server_id": "peerO"}
    ad._wire_state[conn_id] = {"send_next": 1}
    ad._mpath_groups["node:peerO"] = {
        "conns": {conn_id: {"opened_at": 0, "pref": 0, "last_seen": time.time(), "last_rx_seen": time.time()}},
        "primary": conn_id,
        "rr_index": 0,
    }
    return conn_id


# --- peer side -----------------------------------------------------------------


async def _peer_with_session():
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = PortManager([])
    pm.set_unified_adapters([ad])
    # The conn record must exist BEFORE registration: the proxy's
    # connection_id (its mpath group key) is derived from it.
    ad.connections["in:127.0.0.1:77777:1"] = {"server_id": ORIGIN_ID}
    pd = {
        "name": "r1",
        "origin_server": ORIGIN,
        "status": "connected",
        "power": [{"ref": "rack9.1", "on": True}],
    }
    await ad._register_remote_port_from_dict("in:127.0.0.1:77777:1", pd)
    pm.ports["r1"]._client_sessions = {"cid-1": 3}  # an open console session (anchor)
    assert pm.ports["r1"].connection_id == "node:" + ORIGIN_ID
    # The proxy stores the global "origin::"-qualified feed ref.
    assert pm.ports["r1"].power == ["peerO::rack9.1"]
    return ad, pm


@pytest.mark.asyncio
async def test_relay_power_switch_sends_frame_and_applies_result():
    # One adapter plays both ends of the relaying path: the peer-side relay
    # and the origin-side dispatch. Real stream pair: the relay's writes land
    # in c_reader (the origin's receive side); the origin's reply lands in
    # s_reader (the relay's receive side). Port name is the SAME on both
    # sides (remote ports keep their origin-side name), so the relay
    # session's port "r1" is also the origin's local port.
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = PortManager([])
    pm.set_unified_adapters([ad])
    # The conn record must exist BEFORE registration: the proxy's
    # connection_id (its mpath group key) is derived from it.
    ad.connections["in:127.0.0.1:77777:1"] = {"server_id": ORIGIN_ID}
    pd = {
        "name": "r1",
        "origin_server": ORIGIN,
        "status": "connected",
        "power": [{"ref": "rack9.1", "on": True}, {"ref": "rack9.2", "on": True}],
    }
    await ad._register_remote_port_from_dict("in:127.0.0.1:77777:1", pd)
    # The open console session (the anchor) is a mirror of this side's
    # session on the port; read-write was granted by FEDRW on the origin.
    pm.ports["r1"]._client_sessions = {"cid-1": 3}
    # Origin-side state on the shared port: the federated client's mirror
    # entry is read-write, and the console feeds two outlets.
    pm.ports["r1"].connected_clients = [{"client_id": "fed:node:peerO:3", "mode": "read-write"}]
    # Origin anchor map: the (peer, sid) pair is a real origin-side session.
    ad._local_session_map["node:peerO"] = {3: "r1"}
    pdu = _FakeSwitchPdu()
    pdu.main_port_manager = pm
    pm.unified_adapters = [ad, pdu]
    conn_id = "in:127.0.0.1:77777:1"
    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        # The relay's outbound connection (writer toward the origin) and the
        # origin's outbound connection (writer toward the relay).
        ad.connections[conn_id] = {"writer": s_writer, "auth_ok": True, "server_id": ORIGIN_ID}
        ad._wire_state[conn_id] = {"send_next": 1}
        ad._mpath_groups["node:peerO"] = {
            "conns": {conn_id: {"opened_at": 0, "pref": 0, "last_seen": time.time(), "last_rx_seen": time.time()}},
            "primary": conn_id,
            "rr_index": 0,
        }
        wire = []
        dispatched = asyncio.Event()

        async def origin_responder():
            # One control frame: the header line, then the JSON body line.
            hline = await asyncio.wait_for(c_reader.readline(), timeout=1)
            bodyline = await asyncio.wait_for(c_reader.readline(), timeout=1)
            wire.append((hline[: hline.index(b"\n")], bodyline[: bodyline.index(b"\n")] if b"\n" in bodyline else bodyline))
            payload = _frame_payload(hline) + "\n" + bodyline.decode("utf-8").rstrip("\n")
            # Origin dispatch, answering on the origin's connection writer
            # (the relay's receive side).
            await ad._process_control_command(conn_id, c_writer, payload)
            dispatched.set()

        rt = asyncio.create_task(origin_responder())
        # The caller passes the GLOBALLY qualified ref (what the port's feed
        # list holds); the relay strips the origin prefix for the wire. Claims
        # are PORT names (the remote port keeps its origin name on both sides,
        # so they match the origin's coverage set without a prefix).
        task = asyncio.create_task(ad.relay_power_switch("r1", "peerO::rack9.1", False, ["r1"], "cid-1"))
        # The result round-trips over the same connection; feed it to the
        # result handler exactly as the receive loop would.
        r_hline = await asyncio.wait_for(s_reader.readuntil(b"\n"), timeout=2)
        r_bodyline = await asyncio.wait_for(s_reader.readuntil(b"\n"), timeout=2)
        r_payload = _frame_payload(r_hline[: r_hline.index(b"\n")])
        await ad._handle_power_result_frame(conn_id, r_payload + "\n" + r_bodyline.decode("utf-8"))
        res = await asyncio.wait_for(task, timeout=2)
        await asyncio.wait_for(dispatched.wait(), timeout=2)
        await asyncio.wait_for(rt, timeout=1)
        # The frame: header line + JSON body line (captured from the wire).
        hline, bodyline = wire[0]
        assert _frame_payload(hline).startswith("POWER:SWITCH:r1:3"), hline
        # The wire carries the ORIGIN-LOCAL ref (prefix stripped); claims carry
        # the fed port NAMES (shared by both sides, so the origin's coverage
        # check matches them directly).
        assert json.loads(bodyline.decode("utf-8")) == {"ref": "rack9.1", "on": False, "claims": ["r1"]}
        # The origin executed the switch under the anchored mirror id and the
        # result round-tripped back to the relay.
        assert pdu.calls == [{"ref": "rack9.1", "on": False, "user": "fed:node:peerO:3", "client_id": "fed:node:peerO:3"}]
        assert res == {"ok": True, "on": False}
        assert ad._power_switch_pending == {}
    finally:
        _close_pair(server, s_writer, c_writer)
        c_writer.close()


@pytest.mark.asyncio
async def test_relay_power_switch_sends_origin_local_ref():
    # Unit view of the prefix rule: the caller passes the global
    # "peerO::rack9.1" ref (what the port's feed list holds); the relay strips
    # this node's origin prefix before the frame goes out, and refuses when
    # the port is not fed by the ref at all (global or bare form).
    ad, pm = await _peer_with_session()
    res = await ad.relay_power_switch("r1", "other.1", False, ["r1"], "cid-1")
    assert res["ok"] is False
    assert "not fed by" in res["error"]
    # No path to the peer -> refused AFTER the anchor + ref checks, so the
    # global ref must have resolved to the port's local feed first.
    res = await ad.relay_power_switch("r1", "peerO::rack9.1", False, ["r1"], "cid-1")
    assert res["ok"] is False
    assert "unreachable" in res["error"]


@pytest.mark.asyncio
async def test_relay_power_switch_missing_session_refused():
    ad, pm = await _peer_with_session()
    pm.ports["r1"]._client_sessions = {}  # no session open: refused before any frame
    res = await ad.relay_power_switch("r1", "peerO::rack9.1", False, ["r1"], "cid-1")
    assert res["ok"] is False
    assert "no read-write console session" in res["error"]
    # A session keyed under a DIFFERENT client is not an anchor for this one
    # (strict: the acting client's own open session only).
    pm.ports["r1"]._client_sessions = {"cid-other": 3}
    res = await ad.relay_power_switch("r1", "peerO::rack9.1", False, ["r1"], "cid-1")
    assert res["ok"] is False
    assert "no read-write console session" in res["error"]


@pytest.mark.asyncio
async def test_relay_power_switch_ref_not_on_port_refused():
    ad, pm = await _peer_with_session()
    res = await ad.relay_power_switch("r1", "other.1", False, ["r1"], "cid-1")
    assert res["ok"] is False
    assert "not fed by" in res["error"]


@pytest.mark.asyncio
async def test_relay_power_switch_no_path_refused():
    ad, pm = await _peer_with_session()
    # No mpath group for the peer: the frame cannot go out (the session anchor
    # resolves first, so the acting client's own session is needed).
    res = await ad.relay_power_switch("r1", "peerO::rack9.1", False, ["r1"], "cid-1")
    assert res["ok"] is False
    assert "unreachable" in res["error"]


@pytest.mark.asyncio
async def test_relay_power_switch_timeout_refused():
    ad, _pm = await _peer_with_session()
    ad.power_switch_timeout = 0.1
    conn_id = "in:127.0.0.1:77777:1"
    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        ad.connections[conn_id] = {"writer": s_writer, "auth_ok": True, "server_id": ORIGIN_ID}
        ad._wire_state[conn_id] = {"send_next": 1}
        ad._mpath_groups["node:peerO"] = {
            "conns": {conn_id: {"opened_at": 0, "pref": 0, "last_seen": time.time(), "last_rx_seen": time.time()}},
            "primary": conn_id,
            "rr_index": 0,
        }
        res = await ad.relay_power_switch("r1", "peerO::rack9.1", False, ["r1"], "cid-1")
        assert res["ok"] is False
        assert "did not confirm" in res["error"]
        assert ad._power_switch_pending == {}
    finally:
        _close_pair(server, s_writer, c_writer)
        c_writer.close()


@pytest.mark.asyncio
async def test_peer_power_result_resolves_pending_future():
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    conn_id = "in:127.0.0.1:77777:1"
    ad.connections[conn_id] = {"server_id": "peerO"}
    fut = asyncio.get_event_loop().create_future()
    ad._power_switch_pending[("node:peerO", 3)] = (conn_id, fut)
    await ad._handle_power_result_frame(
        conn_id,
        'POWER:RESULT:3\n{"ok":false,"error":"console session is not read-write; switch it from a read-write session"}',
    )
    assert fut.result() == {"ok": False, "error": "console session is not read-write; switch it from a read-write session"}
    assert ad._power_switch_pending == {}
    # An unmatched result is dropped without error.
    await ad._handle_power_result_frame(conn_id, 'POWER:RESULT:9\n{"ok":true}')
    # Malformed result frames are dropped.
    await ad._handle_power_result_frame(conn_id, "POWER:RESULT:nonsense")


# --- origin side ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_origin_power_switch_executes_and_replies_ok():
    pdu = _FakeSwitchPdu()
    ad, pm, _ = await _origin_for_switch(pdu=pdu)
    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        conn_id = await _setup_peer_link(ad, s_writer)
        await ad._process_control_command(
            conn_id, s_writer, _switch_payload("lp", 7, {"ref": "rack1.1", "on": False, "claims": ["lp"]})
        )
        sid, body = await _read_power_result(c_reader)
        assert sid == 7
        assert body == {"ok": True, "on": False}
        # Executed under the anchored mirror's federated pseudo-client id.
        assert pdu.calls == [{"ref": "rack1.1", "on": False, "user": "fed:node:peerO:7", "client_id": "fed:node:peerO:7"}]
    finally:
        _close_pair(server, s_writer, c_writer)
        c_writer.close()


@pytest.mark.asyncio
async def test_origin_power_switch_local_wrapped_port_executes():
    # Regression: on the origin, the fed console is a LOCAL port, and the
    # port manager exposes local ports through a UnifiedPortWrapper that does
    # NOT copy the "power" feed list onto itself (it lives on
    # wrapper.unified_port). The declared-ref check must unwrap the wrapper,
    # else a correct origin-local ref is refused as "not fed by".
    pdu = _FakeSwitchPdu()
    ad, pm, _ = await _origin_for_switch(pdu=pdu)
    inner = type("Inner", (), {"name": "lp", "power": ["rack1.1", "rack1.2"]})()
    wrapper = type(
        "W",
        (),
        {"name": "lp", "unified_port": inner, "connected_clients": [{"client_id": "fed:node:peerO:7", "mode": "read-write"}]},
    )()
    pm.ports["lp"] = wrapper
    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        conn_id = await _setup_peer_link(ad, s_writer)
        await ad._process_control_command(
            conn_id, s_writer, _switch_payload("lp", 7, {"ref": "rack1.1", "on": False, "claims": ["lp"]})
        )
        sid, body = await _read_power_result(c_reader)
        assert sid == 7
        assert body == {"ok": True, "on": False}
        assert pdu.calls == [{"ref": "rack1.1", "on": False, "user": "fed:node:peerO:7", "client_id": "fed:node:peerO:7"}]
    finally:
        _close_pair(server, s_writer, c_writer)
        c_writer.close()


@pytest.mark.asyncio
async def test_origin_power_switch_unmapped_stream_refused():
    pdu = _FakeSwitchPdu()
    ad, pm, _ = await _origin_for_switch(pdu=pdu)
    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        conn_id = await _setup_peer_link(ad, s_writer)
        # sid 9 has no origin-side session -> anti-spoof refusal.
        await ad._process_control_command(
            conn_id, s_writer, _switch_payload("lp", 9, {"ref": "rack1.1", "on": False, "claims": ["lp"]})
        )
        sid, body = await _read_power_result(c_reader)
        assert body == {"ok": False, "error": "no open console session to anchor the switch on"}
        assert pdu.calls == []
    finally:
        _close_pair(server, s_writer, c_writer)
        c_writer.close()


@pytest.mark.asyncio
async def test_origin_power_switch_read_only_mirror_refused():
    pdu = _FakeSwitchPdu()
    ad, pm, _ = await _origin_for_switch(fed_mode="read-only", pdu=pdu)
    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        conn_id = await _setup_peer_link(ad, s_writer)
        await ad._process_control_command(
            conn_id, s_writer, _switch_payload("lp", 7, {"ref": "rack1.1", "on": False, "claims": ["lp"]})
        )
        _sid, body = await _read_power_result(c_reader)
        assert body == {"ok": False, "error": "console session is not read-write; switch it from a read-write session"}
        assert pdu.calls == []
    finally:
        _close_pair(server, s_writer, c_writer)
        c_writer.close()


@pytest.mark.asyncio
async def test_origin_power_switch_uncovered_fed_port_refused():
    pdu = _FakeSwitchPdu()
    ad, pm, _ = await _origin_for_switch(pdu=pdu)
    # c7 also feeds the same outlets, but the peer never saw it (not federated
    # there, or filtered by _adv_name_inc): the peer cannot claim entitlement.
    c7 = type("C7", (), {"name": "c7", "power": ["rack1.1", "rack1.2"], "unified_port": None})()
    c7.unified_port = c7
    pm.ports["c7"] = c7
    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        conn_id = await _setup_peer_link(ad, s_writer)
        await ad._process_control_command(
            conn_id, s_writer, _switch_payload("lp", 7, {"ref": "rack1.1", "on": False, "claims": ["lp"]})
        )
        _sid, body = await _read_power_result(c_reader)
        assert body["ok"] is False
        assert "c7" in body["error"] and "switch it on the origin node" in body["error"]
        assert pdu.calls == []
    finally:
        _close_pair(server, s_writer, c_writer)
        c_writer.close()


@pytest.mark.asyncio
async def test_origin_power_switch_port_not_fed_by_ref_refused():
    pdu = _FakeSwitchPdu()
    ad, pm, _ = await _origin_for_switch(pdu=pdu)
    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        conn_id = await _setup_peer_link(ad, s_writer)
        await ad._process_control_command(
            conn_id, s_writer, _switch_payload("lp", 7, {"ref": "other.9", "on": False, "claims": ["lp"]})
        )
        _sid, body = await _read_power_result(c_reader)
        assert body == {"ok": False, "error": "lp is not fed by other.9"}
        assert pdu.calls == []
    finally:
        _close_pair(server, s_writer, c_writer)
        c_writer.close()


@pytest.mark.asyncio
async def test_origin_power_switch_malformed_payload_refused_or_ignored():
    pdu = _FakeSwitchPdu()
    ad, pm, _ = await _origin_for_switch(pdu=pdu)
    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        conn_id = await _setup_peer_link(ad, s_writer)
        # Anchored stream + missing JSON body: the requester IS waiting for an
        # outcome, so a typed refusal reply goes out (nothing is executed).
        await ad._process_control_command(conn_id, s_writer, "POWER:SWITCH:lp:7")
        _sid, body = await _read_power_result(c_reader)
        assert body == {"ok": False, "error": "malformed power switch request"}
        # Bad header (no stream id): nothing can be correlated, silently dropped.
        await ad._process_control_command(conn_id, s_writer, 'POWER:SWITCH:lp\n{"ref":"rack1.1","on":true,"claims":["lp"]}')
        await asyncio.sleep(0.05)
        assert pdu.calls == []
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(c_reader.readline(), timeout=0.1)
    finally:
        _close_pair(server, s_writer, c_writer)
        c_writer.close()


@pytest.mark.asyncio
async def test_origin_power_switch_dispatch_requires_auth():
    pdu = _FakeSwitchPdu()
    ad, pm, _ = await _origin_for_switch(pdu=pdu)
    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        conn_id = await _setup_peer_link(ad, s_writer)
        ad.connections[conn_id]["auth_ok"] = False  # simulate an unauthenticated path
        await ad._process_control_command(
            conn_id, s_writer, _switch_payload("lp", 7, {"ref": "rack1.1", "on": False, "claims": ["lp"]})
        )
        await asyncio.sleep(0.05)
        assert pdu.calls == []
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(c_reader.readline(), timeout=0.1)
    finally:
        _close_pair(server, s_writer, c_writer)
        c_writer.close()


# --- dotted-FQDN origin (outlet federation regression) -------------------------
# Server ids are often dotted FQDNs (e.g. "openmux.borge.nu"). The "origin::"
# separator must still split origin from local ref unambiguously: the local
# part "<pdu>.<id>" has exactly one dot and no "::", so the FIRST "::" is the
# separator. These pin that a dotted origin never double-qualifies its own
# refs (the source of the "openmux.borge.nu::openmux.borge.nu::rack1.1" bug)
# and that POWER:STATE keeps working end to end.


def _fqdn_origin(name="openmux.borge.nu"):
    return {"server_id": name, "hostname": name, "port": 0, "server_type": "leaf", "description": ""}


@pytest.mark.asyncio
async def test_register_dotted_fqdn_origin_ref_not_doubled():
    # The origin's own server id has dots. Its advertised feed refs are
    # already globally qualified with that id ("<fqdn>::<ref>"); the peer must
    # NOT add a second prefix.
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = PortManager([])
    pm.set_unified_adapters([ad])
    pd = {
        "name": "r1",
        "origin_server": _fqdn_origin(),
        "status": "connected",
        "power": [{"ref": "openmux.borge.nu::rack1.1", "on": True}],  # already qualified
    }
    await ad._register_remote_port_from_dict("in:127.0.0.1:1:1", pd)
    proxy = pm.ports["r1"]
    assert proxy.power == ["openmux.borge.nu::rack1.1"]  # no double prefix
    assert proxy._feed_states == {"openmux.borge.nu::rack1.1": True}
    assert proxy.metadata.power == [{"ref": "openmux.borge.nu::rack1.1", "on": True}]


@pytest.mark.asyncio
async def test_register_bare_ref_dotted_fqdn_origin_qualifies_once():
    # A bare local ref from a dotted origin is qualified exactly once.
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = PortManager([])
    pm.set_unified_adapters([ad])
    pd = {
        "name": "r1",
        "origin_server": _fqdn_origin(),
        "status": "connected",
        "power": [{"ref": "rack1.1", "on": True}],
    }
    await ad._register_remote_port_from_dict("in:127.0.0.1:2:1", pd)
    proxy = pm.ports["r1"]
    assert proxy.power == ["openmux.borge.nu::rack1.1"]


@pytest.mark.asyncio
async def test_readvertise_dotted_fqdn_origin_not_doubled():
    # The reuse/refresh path replaces metadata.power wholesale. An
    # already-prefixed dotted-FQDN ref must survive the refresh verbatim:
    # the "no double-prefix" guard must accept dotted origins (this is where
    # the doubled ref leaked in).
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = PortManager([])
    pm.set_unified_adapters([ad])
    pd = {
        "name": "r1",
        "origin_server": _fqdn_origin(),
        "status": "connected",
        "power": [{"ref": "openmux.borge.nu::rack1.1", "on": True}],
    }
    await ad._register_remote_port_from_dict("in:127.0.0.1:3:1", pd)
    proxy = pm.ports["r1"]
    meta = _origin_meta("r1")
    meta.origin_server = ServerInfo(
        server_id="openmux.borge.nu",
        hostname="openmux.borge.nu",
        port=0,
        server_type=ServerType.LEAF,
        description="",
    )
    meta.power = [{"ref": "openmux.borge.nu::rack1.1", "on": False}]
    proxy.metadata = meta  # mirror the reuse path: fresh metadata first
    await ad._apply_power_meta_to_proxy(proxy, meta)
    assert proxy.power == ["openmux.borge.nu::rack1.1"]  # not doubled
    assert proxy._feed_states == {"openmux.borge.nu::rack1.1": False}


@pytest.mark.asyncio
async def test_power_state_dotted_fqdn_origin_applies_once():
    # A peer's proxy fed by a dotted-FQDN origin: an inbound POWER:STATE that
    # names the ORIGIN-LOCAL ref must update under the global "fqdn::<ref>"
    # key (single qualification) and emit exactly one meta event.
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = PortManager([])
    pm.set_unified_adapters([ad])
    events = []
    pm.register_meta_listener(lambda p, c: events.append((p, c or {})))
    ad.connections["in:127.0.0.1:5:1"] = {"server_id": "openmux.borge.nu"}
    pd = {
        "name": "r1",
        "origin_server": _fqdn_origin(),
        "status": "connected",
        "power": [{"ref": "rack1.1", "on": True}],
    }
    await ad._register_remote_port_from_dict("in:127.0.0.1:5:1", pd)
    ad.connections["in:anyone:1"] = {"server_id": "openmux.borge.nu"}
    await ad._handle_power_state_frame("in:anyone:1", 'POWER:STATE:r1\n{"ref":"rack1.1","on":false}')
    assert pm.ports["r1"]._feed_states == {"openmux.borge.nu::rack1.1": False}
    power_events = [c for p, c in events if p == "r1" and c.get("event") == "power_outlet_changed"]
    assert len(power_events) == 1
    assert power_events[0]["outlet"] == "openmux.borge.nu::rack1.1"
