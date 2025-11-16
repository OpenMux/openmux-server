import asyncio
import time

import pytest

from openmux.server.adapters.muxcon import UnifiedMuxConAdapter
from openmux.server.port_manager import PortManager


async def _make_stream_pair():
    server_side = {}

    async def handle(reader, writer):
        server_side["reader"] = reader
        server_side["writer"] = writer

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    client_reader, client_writer = await asyncio.open_connection(host, port)
    # Wait until server handler has assigned reader/writer
    while "reader" not in server_side:
        await asyncio.sleep(0)
    return server, server_side["reader"], server_side["writer"], client_reader, client_writer


@pytest.mark.asyncio
async def test_last_path_close_unregisters_federated_ports_and_notifies():
    # Adapter + PortManager with meta listener to capture events
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {"federated_cache_enabled": False}})
    pm = PortManager({})
    pm.set_unified_adapters([ad])

    events = []

    def on_meta(port_name, changes):
        events.append((port_name, changes or {}))

    pm.register_meta_listener(on_meta)

    # Create a real stream pair to satisfy writer.close() in _close_connection
    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        conn_id = "in:127.0.0.1:5555:1"
        # Register connection with a stable server_id so peer_key is node:<server_id>
        ad.connections[conn_id] = {
            "writer": s_writer,
            "reader": s_reader,
            "server_id": "peerS",
            "opened_at": time.time(),
        }
        ad._wire_state[conn_id] = {"send_next": 1}
        # Place it into a multipath group so close can detect last-path
        ad._register_mpath_connection(conn_id)

        # Register a federated remote port for this connection
        pd = {
            "origin_server": {
                "server_id": "peerS",
                "hostname": "remote",
                "port": 0,
                "server_type": "leaf",
                "description": "",
            },
            "name": "r1",
            "description": "Remote Port 1",
            "adapter_type": "remote_muxcon",
            "max_rw_users": 1,
            "status": "connected",
        }
        await ad._register_remote_port_from_dict(conn_id, pd)

        # Sanity: port is registered and connected
        assert "r1" in pm.ports
        proxy_ref = pm.ports["r1"]
        assert getattr(proxy_ref, "is_connected", False) is True

        # Now close the only path for this peer; expect disconnect but keep cached
        await ad._close_connection(conn_id)

        # Proxy should have been marked disconnected and destroyed
        assert getattr(proxy_ref, "is_connected", True) is False
        assert getattr(proxy_ref, "state", None) is not None
        assert getattr(proxy_ref, "state").name in ("DESTROYED", "DESTROYING")

        # Federated port should remain cached in PortManager (offline)
        assert "r1" in pm.ports

        # Meta events should include a disconnect and a cached_offline event with last_seen
        ev_keys = [(p, (c or {}).get("event")) for p, c in events]
        assert ("r1", "federated_disconnected") in ev_keys
        assert ("r1", "federated_cached_offline") in ev_keys
    finally:
        # Cleanup the stream pair
        s_writer.close()
        c_writer.close()
        await s_writer.wait_closed()
        await c_writer.wait_closed()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_live_state_flips_on_stale_and_restore():
    # Configure adapter/manager and a single path for peer
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    pm = PortManager({})
    pm.set_unified_adapters([ad])

    captured = []

    def on_meta(port_name, changes):
        captured.append((port_name, changes or {}))

    pm.register_meta_listener(on_meta)

    server, s_reader, s_writer, c_reader, c_writer = await _make_stream_pair()
    try:
        conn_id = "in:127.0.0.1:6666:1"
        ad.connections[conn_id] = {
            "writer": s_writer,
            "reader": s_reader,
            "server_id": "peerL",
            "opened_at": time.time(),
        }
        ad._wire_state[conn_id] = {"send_next": 1}
        ad._register_mpath_connection(conn_id)

        # Register one federated port under this peer
        pd = {
            "origin_server": {
                "server_id": "peerL",
                "hostname": "remote",
                "port": 0,
                "server_type": "leaf",
                "description": "",
            },
            "name": "r2",
            "description": "Remote Port 2",
            "adapter_type": "remote_muxcon",
            "max_rw_users": 1,
            "status": "connected",
        }
        await ad._register_remote_port_from_dict(conn_id, pd)
        assert "r2" in pm.ports
        proxy = pm.ports["r2"]
        assert getattr(proxy, "is_connected", False) is True

        # Make path appear stale by aging last_rx and last_ack beyond stale window
        peer_key = ad._derive_peer_key_from_conn_id(conn_id)
        grp = ad._mpath_groups.get(peer_key)
        assert grp and conn_id in grp.get("conns", {})
        now = time.time()
        # Ensure stale by setting last seen far in the past
        grp["conns"][conn_id]["last_rx_seen"] = now - 120.0
        ad._hb_state[conn_id] = {"last_ack_ts": now - 120.0}

        # Trigger live-state recomputation; expect flip to disconnected
        ad._update_peer_proxies_live_state(peer_key)
        assert getattr(proxy, "is_connected", True) is False
        # And a live_state meta event should be emitted
        assert ("r2", "federated_live_state") in [(p, (c or {}).get("event")) for p, c in captured]

        # Restore freshness and confirm flip back to connected
        grp["conns"][conn_id]["last_rx_seen"] = time.time()
        ad._hb_state[conn_id]["last_ack_ts"] = time.time()
        ad._update_peer_proxies_live_state(peer_key)
        assert getattr(proxy, "is_connected", False) is True
        assert ("r2", "federated_live_state") in [(p, (c or {}).get("event")) for p, c in captured]

        # Port remains registered in PortManager (no unregister on stale)
        assert "r2" in pm.ports
    finally:
        s_writer.close()
        c_writer.close()
        await s_writer.wait_closed()
        await c_writer.wait_closed()
        server.close()
        await server.wait_closed()
