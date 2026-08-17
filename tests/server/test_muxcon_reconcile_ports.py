"""Regression tests for UnifiedMuxConAdapter.reconcile_ports() (issue #50).

Covers the hot-reload fix: soft reload (SIGHUP) previously silently ignored
`muxcon:` config changes since the adapter had no `reconcile_ports()` method.
These tests exercise the new method directly (add/remove/update listeners,
add/remove initiators, wholesale public-key replacement) without going
through the SIGHUP signal path itself (that's covered by test_reload_signals.py).
"""

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openmux.server.adapters.muxcon import UnifiedMuxConAdapter


def _listener(port: int, **overrides):
    lst = {"host": "127.0.0.1", "port": port, "use_tls": False, "enabled": True}
    lst.update(overrides)
    return lst


@pytest.mark.asyncio
async def test_reconcile_ports_adds_and_removes_listener_without_disturbing_others():
    a = UnifiedMuxConAdapter("mx", {"listeners": [_listener(18391)], "heartbeat_interval": 0})
    assert await a.start() is True
    try:
        assert ("127.0.0.1", 18391) in a._servers
        original_server = a._servers[("127.0.0.1", 18391)]

        # Add a second listener; first one must be untouched (same server object).
        res = await a.reconcile_ports({"listeners": [_listener(18391), _listener(18392)]})
        assert res["listeners"]["added"] == ["127.0.0.1:18392"]
        assert res["listeners"]["unchanged"] == ["127.0.0.1:18391"]
        assert ("127.0.0.1", 18392) in a._servers
        assert a._servers[("127.0.0.1", 18391)] is original_server

        # Remove the first listener; second must remain bound.
        res = await a.reconcile_ports({"listeners": [_listener(18392)]})
        assert res["listeners"]["removed"] == ["127.0.0.1:18391"]
        assert ("127.0.0.1", 18391) not in a._servers
        assert ("127.0.0.1", 18392) in a._servers

        # Materially changing a listener's config restarts just that listener.
        second_server_before = a._servers[("127.0.0.1", 18392)]
        res = await a.reconcile_ports({"listeners": [_listener(18392, path_pref="wan")]})
        assert res["listeners"]["updated"] == ["127.0.0.1:18392"]
        assert a._servers[("127.0.0.1", 18392)] is not second_server_before
        assert a.listeners_conf[0]["path_pref"] == "wan"
    finally:
        await a.stop()


@pytest.mark.asyncio
async def test_reconcile_ports_removes_all_listeners_on_empty_config():
    a = UnifiedMuxConAdapter("mx", {"listeners": [_listener(18393)], "heartbeat_interval": 0})
    assert await a.start() is True
    try:
        res = await a.reconcile_ports({})
        assert res["listeners"]["removed"] == ["127.0.0.1:18393"]
        assert a._servers == {}
        assert a.listeners_conf == []
    finally:
        await a.stop()


@pytest.mark.asyncio
async def test_reconcile_ports_adds_and_removes_initiator():
    a = UnifiedMuxConAdapter("mx", {"listeners": [], "heartbeat_interval": 0})
    assert await a.start() is True
    try:
        res = await a.reconcile_ports({"initiators": [{"host": "127.0.0.1", "port": 18490}]})
        assert res["initiators"]["added"] == ["127.0.0.1:18490"]
        assert ("127.0.0.1", 18490) in a._initiator_tasks
        assert [(p.host, p.port) for p in a.peers] == [("127.0.0.1", 18490)]

        res = await a.reconcile_ports({"initiators": []})
        assert res["initiators"]["removed"] == ["127.0.0.1:18490"]
        assert a._initiator_tasks == {}
        assert a.peers == []
    finally:
        await a.stop()


@pytest.mark.asyncio
async def test_reconcile_ports_replaces_public_keys_wholesale():
    a = UnifiedMuxConAdapter("mx", {"listeners": [], "heartbeat_interval": 0})
    assert await a.start() is True
    try:
        priv1 = Ed25519PrivateKey.generate()
        pub1 = (
            priv1.public_key()
            .public_bytes(
                encoding=serialization.Encoding.OpenSSH,
                format=serialization.PublicFormat.OpenSSH,
            )
            .decode()
        )
        priv2 = Ed25519PrivateKey.generate()
        pub2 = (
            priv2.public_key()
            .public_bytes(
                encoding=serialization.Encoding.OpenSSH,
                format=serialization.PublicFormat.OpenSSH,
            )
            .decode()
        )

        res = await a.reconcile_ports({"public_keys": [{"key_id": "k1", "public_key": pub1}]})
        assert res["public_keys"] == {"before": 0, "after": 1}
        assert set(a._auth_pubkeys.keys()) == {"k1"}

        res = await a.reconcile_ports({"public_keys": [{"key_id": "k2", "public_key": pub2}]})
        assert res["public_keys"] == {"before": 1, "after": 1}
        assert set(a._auth_pubkeys.keys()) == {"k2"}
    finally:
        await a.stop()


def test_reconcile_ports_unwraps_top_level_muxcon_key():
    assert UnifiedMuxConAdapter._unwrap_reconcile_config({"muxcon": {"listeners": []}}) == {"listeners": []}
    assert UnifiedMuxConAdapter._unwrap_reconcile_config({"listeners": []}) == {"listeners": []}
    assert UnifiedMuxConAdapter._unwrap_reconcile_config(None) == {}
    assert UnifiedMuxConAdapter._unwrap_reconcile_config("not-a-dict") == {}
