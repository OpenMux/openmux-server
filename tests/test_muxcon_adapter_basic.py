import asyncio
import os
from types import SimpleNamespace

import pytest

from openmux.server.adapters.muxcon import FederationPeer, UnifiedMuxConAdapter


def test_validate_config_and_status_basics(tmp_path):
    cfg = {
        "muxcon": {
            "listeners": [
                {"host": "127.0.0.1", "port": 8022, "use_tls": True, "tls_autogen": True, "tls_dir": str(tmp_path)},
            ],
            "initiators": [{"host": "localhost", "port": 8022}],
        }
    }
    assert UnifiedMuxConAdapter.validate_config(cfg) is True
    ad = UnifiedMuxConAdapter("mx", cfg)
    st = ad.get_status_info()
    assert st["type"] == "muxcon"
    assert "details" in st and isinstance(st["details"], dict)


def test_auth_manager_key_import_and_filters():
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})

    class FakeAM:
        def get_ed25519_pubkeys_for_use(self, use):
            assert use == "muxcon"
            return {"kid1": object()}

        def get_public_keys_for_use(self, use):
            return [
                {
                    "key_id": "kid1",
                    "muxcon": {
                        "advertise_filters": {"include": ["a*"], "exclude": ["b*"]},
                        "accept_filters": {"adapter_include": ["loopback"]},
                    },
                }
            ]

    ad.set_auth_manager(FakeAM())
    # Filters populated for key id
    assert "kid1" in ad._key_filters


def test_known_peers_save_and_load(tmp_path, monkeypatch):
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {"listeners": []}})
    ad._known_peers_path = os.path.join(tmp_path, "known.yaml")
    mapping = {"h:1": "sha256:abcd"}
    ad._save_known_peers(mapping)
    loaded = ad._load_known_peers()
    assert loaded == mapping


@pytest.mark.asyncio
async def test_client_ssl_context_options(tmp_path):
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    peer = FederationPeer(host="localhost", port=9, options={"use_tls": True, "ssl_verify": False})
    ctx = await ad._create_client_ssl_context(peer)
    assert ctx is not None
    # With verify disabled, check_hostname must be False
    assert ctx.check_hostname is False


@pytest.mark.asyncio
async def test_start_stop_without_listeners():
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {"listeners": []}})
    ok = await ad.start()
    assert ok is True
    await ad.stop()
    assert ad.is_running is False
