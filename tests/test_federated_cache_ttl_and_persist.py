import asyncio
import time

import pytest

from openmux.server.adapters.muxcon import UnifiedMuxConAdapter
from openmux.server.port_manager import PortManager


@pytest.mark.asyncio
async def test_ttl_cleanup_purges_offline_ports(monkeypatch):
    # TTL set low so purge removes offline ports immediately
    ad = UnifiedMuxConAdapter(
        "mx",
        {"muxcon": {"federated_cache_enabled": True, "federated_cache_ttl_sec": 0.1}},
    )
    pm = PortManager({})
    pm.set_unified_adapters([ad])

    events = []

    def on_meta(port_name, changes):
        events.append((port_name, (changes or {}).get("event")))

    pm.register_meta_listener(on_meta)

    # Seed a minimal connection so we can derive a stable peer_key
    conn_id = "in:127.0.0.1:7777:1"
    ad.connections[conn_id] = {"server_id": "peerTTL", "opened_at": time.time()}

    # Register a federated remote port under this connection
    pd = {
        "origin_server": {
            "server_id": "peerTTL",
            "hostname": "remote",
            "port": 0,
            "server_type": "leaf",
            "description": "",
        },
        "name": "rx1",
        "description": "Remote Port TTL",
        "adapter_type": "remote_muxcon",
        "max_rw_users": 1,
        "status": "connected",
    }
    await ad._register_remote_port_from_dict(conn_id, pd)
    assert "rx1" in pm.ports
    proxy = pm.ports["rx1"]

    # Mark offline and set last_seen far in the past
    proxy.is_connected = False
    proxy.last_seen = time.time() - 5.0

    # Purge once (single-pass helper) and verify removal
    ad._purge_offline_cached_ports()
    assert "rx1" not in pm.ports
    # Meta should include TTL unregistration
    assert ("rx1", "federated_port_unregistered_ttl") in events


def test_persist_and_load_cache(tmp_path):
    cache_file = tmp_path / "federated_cache.json"
    # Writer adapter persists cache
    ad1 = UnifiedMuxConAdapter(
        "mx1",
        {
            "muxcon": {
                "federated_cache_enabled": True,
                "federated_cache_path": str(cache_file),
            }
        },
    )
    pm1 = PortManager({})
    pm1.set_unified_adapters([ad1])
    # Minimal connection so derive peer_key
    conn_id = "in:10.0.0.1:8888:1"
    ad1.connections[conn_id] = {"server_id": "peerP", "opened_at": time.time()}
    pd = {
        "origin_server": {
            "server_id": "peerP",
            "hostname": "remote",
            "port": 0,
            "server_type": "leaf",
            "description": "",
        },
        "name": "rx2",
        "description": "Remote Port Persist",
        "adapter_type": "remote_muxcon",
        "max_rw_users": 1,
        "status": "connected",
    }
    # Register and then mark offline to persist with connected False
    asyncio.get_event_loop().run_until_complete(ad1._register_remote_port_from_dict(conn_id, pd))
    assert "rx2" in pm1.ports
    pr = pm1.ports["rx2"]
    pr.is_connected = False
    pr.last_seen = time.time() - 1
    ad1._save_federated_cache()
    assert cache_file.exists()

    # Reader adapter loads cache
    ad2 = UnifiedMuxConAdapter(
        "mx2",
        {
            "muxcon": {
                "federated_cache_enabled": True,
                "federated_cache_path": str(cache_file),
            }
        },
    )
    pm2 = PortManager({})
    pm2.set_unified_adapters([ad2])
    ad2._load_federated_cache()
    # Port should be present and offline
    assert "rx2" in pm2.ports
    pr2 = pm2.ports["rx2"]
    assert getattr(pr2, "is_connected", True) is False
    # last_seen restored
    assert float(getattr(pr2, "last_seen", 0)) > 0