import pytest

from openmux.server.adapters.muxcon import UnifiedMuxConAdapter


def test_initiator_always_authenticated_for_advertisement():
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    # Simulate a client-role connection without auth_ok
    cid = "out:127.0.0.1:9999:1"
    ad.connections[cid] = {"role": "client", "auth_ok": False}
    assert ad._is_conn_authenticated(cid) is True
