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


# --- Soft-reload: adapter-level federation filter re-read + warning re-check (ticket #77) ---


@pytest.mark.asyncio
async def test_reconcile_rereads_adapter_level_federation_filters():
    """A soft-reload that adds an include list must update the effective flags,
    so new ports/connections use the new rules. (Pre-fix, the flat filter
    keys were only read in __init__ and a soft reload silently ignored them.)
    """
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {"listeners": [], "initiators": []}})
    assert ad._adv_name_inc == [] and ad._acc_name_inc == []
    await ad.reconcile_ports(
        {
            "muxcon": {
                "listeners": [],
                "initiators": [],
                "advertise_filters": {"include": ["console_*"], "exclude": ["debug_*"]},
                "accept_filters": {"server_include": ["hub-01"]},
            }
        }
    )
    assert ad._adv_name_inc == ["console_*"]
    assert ad._adv_name_exc == ["debug_*"]
    assert ad._acc_server_inc == ["hub-01"]
    # Both directions have a non-empty include dimension -> out of deny mode,
    # and their warning flags must be cleared.
    assert ad._warned_adv_deny is False
    assert ad._warned_acc_deny is False


@pytest.mark.asyncio
async def test_reconcile_reentry_into_deny_warns_again(caplog):
    """After a soft reload that clears an include list, the direction re-enters
    deny mode and must re-warn (the flag resets when the direction LEAVES deny
    mode, so re-entry is a new event). A direction that never left deny mode
    must NOT re-warn (no log spam)."""
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {"listeners": [], "advertise_filters": {"include": ["*"]}}})
    with caplog.at_level(logging.WARNING, logger=LOG):
        ad._log_empty_filter_warning()
    # No advertise warning (include: ["*"]); accept warns once.
    assert len(_warn_lines(caplog)) == 1
    assert "accepts no ports from any peer" in _warn_lines(caplog)[0]
    caplog.clear()
    # Soft reload: drop the advertise include (advertise re-enters deny mode);
    # accept stays in deny mode for the whole process.
    with caplog.at_level(logging.WARNING, logger=LOG):
        await ad.reconcile_ports({"muxcon": {"listeners": [], "initiators": []}})
    msgs = _warn_lines(caplog)
    assert len(msgs) == 1
    # Re-entered advertise direction warns again...
    assert "shares none of your 0 local port(s)" in msgs[0]
    # ...but accept, which never left deny mode, does not re-warn.
    assert "accepts no ports from any peer" not in msgs[0]


@pytest.mark.asyncio
async def test_reconcile_staying_in_deny_does_not_respawn_warning(caplog):
    """Repeated soft reloads while the direction stays in deny-all must not
    re-warn every time (no log spam)."""
    ad = UnifiedMuxConAdapter("mx", {"muxcon": {"listeners": [], "initiators": []}})
    with caplog.at_level(logging.WARNING, logger=LOG):
        ad._log_empty_filter_warning()
    assert len(_warn_lines(caplog)) == 1
    caplog.clear()
    # Two more reconciles with still-empty filters: no re-warn.
    with caplog.at_level(logging.WARNING, logger=LOG):
        await ad.reconcile_ports({"muxcon": {"listeners": [], "initiators": []}})
        await ad.reconcile_ports({"muxcon": {"listeners": [], "initiators": []}})
    assert len(_warn_lines(caplog)) == 0


@pytest.mark.asyncio
async def test_reconcile_warns_only_for_reentered_direction(caplog):
    """Per-direction independence: if only the accept direction re-enters deny
    mode, only the accept direction is named in the re-warn."""
    ad = UnifiedMuxConAdapter(
        "mx",
        {
            "muxcon": {
                "listeners": [],
                "initiators": [],
                "advertise_filters": {"include": ["*"]},
                "accept_filters": {"include": ["*"]},
            }
        },
    )
    # Both directions out of deny -> no warning.
    with caplog.at_level(logging.WARNING, logger=LOG):
        ad._log_empty_filter_warning()
    assert _warn_lines(caplog) == []
    caplog.clear()
    # Reconcile: keep the advertise include, clear the accept include (accept
    # re-enters deny mode only).
    with caplog.at_level(logging.WARNING, logger=LOG):
        await ad.reconcile_ports({"muxcon": {"listeners": [], "initiators": [], "advertise_filters": {"include": ["*"]}}})
    msgs = _warn_lines(caplog)
    assert len(msgs) == 1
    assert "accepts no ports from any peer" in msgs[0]
    assert "shares none" not in msgs[0]
    assert UnifiedMuxConAdapter._unwrap_reconcile_config({"muxcon": {"advertise_filters": {"include": ["a"]}}}) == {
        "advertise_filters": {"include": ["a"]}
    }
    bare = {"listeners": [], "advertise_filters": {"include": ["a"]}}
    assert UnifiedMuxConAdapter._effective_section(bare) is bare
    assert UnifiedMuxConAdapter._effective_section({"muxcon": {"listeners": []}}) == {"listeners": []}
