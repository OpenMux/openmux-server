import asyncio
import logging
import os
from types import SimpleNamespace

import pytest

from openmux.server.adapters.muxcon import FederationPeer, UnifiedMuxConAdapter

LOG = "openmux.adapter.muxcon.mx"


def test_validate_config_and_status_basics(tmp_path):
    cfg = {
        "muxcon": {
            "listeners": [
                {"host": "127.0.0.1", "port": 8022, "use_tls": True, "tls_autogen": True},
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
                    "advertise_filters": {"include": ["a*"], "exclude": ["b*"]},
                    "accept_filters": {"adapter_include": ["loopback"]},
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


# --- Default-deny federation filter warning (ticket #77) ---


def _warn_lines(caplog) -> list:
    return [r.getMessage() for r in caplog.records if "MuxCon federation filters default to DENY-ALL" in r.getMessage()]


def test_empty_filters_warn_at_start(tmp_path, caplog):
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {"listeners": []}})
    ad.main_port_manager = SimpleNamespace(ports={"SHELL": object(), "console1": object()})
    with caplog.at_level(logging.WARNING, logger=LOG):
        ad._log_empty_filter_warning()
    msgs = _warn_lines(caplog)
    # Both directions empty -> both are named, in deny mode.
    assert len(msgs) == 1
    assert "shares none of your 2 local port(s)" in msgs[0]
    assert "accepts no ports from any peer" in msgs[0]
    # The warning must name the allow-all opt-in so the operator can restore it.
    assert "['*']" in msgs[0]
    # Re-emit must be a no-op (once per process).
    with caplog.at_level(logging.WARNING, logger=LOG):
        ad._log_empty_filter_warning()
    assert len(_warn_lines(caplog)) == 1


def test_advertise_filters_set_no_advertise_warning(tmp_path, caplog):
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {"listeners": [], "advertise_filters": {"include": ["console_*"]}}})
    with caplog.at_level(logging.WARNING, logger=LOG):
        ad._log_empty_filter_warning()
    msgs = _warn_lines(caplog)
    assert len(msgs) == 1
    assert "shares none" not in msgs[0]
    assert "accepts no ports from any peer" in msgs[0]


def test_accept_filters_set_no_accept_warning(tmp_path, caplog):
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {"listeners": [], "accept_filters": {"server_include": ["hub-01"]}}})
    with caplog.at_level(logging.WARNING, logger=LOG):
        ad._log_empty_filter_warning()
    msgs = _warn_lines(caplog)
    assert len(msgs) == 1
    assert "shares none of your 0 local port(s)" in msgs[0]
    assert "accepts no ports" not in msgs[0]


def test_exclude_only_still_warns_include_empty(tmp_path, caplog):
    # Under default-deny, exclude does not turn anything on: an empty include
    # set is deny, so the warning fires even though exclude is set.
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {"listeners": [], "advertise_filters": {"exclude": ["debug_*"]}}})
    with caplog.at_level(logging.WARNING, logger=LOG):
        ad._log_empty_filter_warning()
    assert "shares none of your 0 local port(s)" in _warn_lines(caplog)[0]


def test_star_include_counts_as_constrained(tmp_path, caplog):
    # include: ["*"] is the explicit allow-all opt-in -> that direction is
    # no longer in deny mode, so it is not named in the warning.
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {"listeners": [], "accept_filters": {"include": ["*"]}}})
    with caplog.at_level(logging.WARNING, logger=LOG):
        ad._log_empty_filter_warning()
    msgs = _warn_lines(caplog)
    assert len(msgs) == 1
    assert "accepts no ports" not in msgs[0]


def test_both_filters_set_no_warning(tmp_path, caplog):
    ad = UnifiedMuxConAdapter(
        "mx",
        {
            "muxcon": {
                "listeners": [],
                "advertise_filters": {"include": ["console_*"]},
                "accept_filters": {"include": ["upstream_*"]},
            }
        },
    )
    with caplog.at_level(logging.WARNING, logger=LOG):
        ad._log_empty_filter_warning()
    assert _warn_lines(caplog) == []


def test_warning_survives_no_port_manager(tmp_path, caplog):
    # No port manager attached -> port count 0, but the warning still comes out.
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {"listeners": []}})
    with caplog.at_level(logging.WARNING, logger=LOG):
        ad._log_empty_filter_warning()
    assert "shares none of your 0 local port(s)" in _warn_lines(caplog)[0]


def test_unwrap_and_effective_helper_agree():
    assert UnifiedMuxConAdapter._unwrap_reconcile_config(None) == {}
    assert UnifiedMuxConAdapter._unwrap_reconcile_config("x") == {}
    assert UnifiedMuxConAdapter._unwrap_reconcile_config({"muxcon": {"advertise_filters": {"include": ["a"]}}}) == {
        "advertise_filters": {"include": ["a"]}
    }
    bare = {"listeners": [], "advertise_filters": {"include": ["a"]}}
    assert UnifiedMuxConAdapter._effective_section(bare) is bare
    assert UnifiedMuxConAdapter._effective_section({"muxcon": {"listeners": []}}) == {"listeners": []}
