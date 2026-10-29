"""Server-level tests for the config gap warning (issue #105).

The rule these pin: whenever a config file asks for something the running server does not have,
the server logs one unmistakable block naming the file and each entry that did not apply. A
clean boot and a clean reload log nothing.

Follows the harness of tests/server/test_reload_adapters_soft_coverage.py: a real OpenMuxServer on
a minimal tmp config with no adapter sections, so no real adapter or socket is created.
"""

from __future__ import annotations

import logging

import pytest

from openmux.server.adapters.serial import SerialAdapter
from openmux.server.config_gaps import GAP_MARKER
from openmux.server.main import OpenMuxServer


class _GapAdapter:
    """Adapter stub carrying config gap state and a fixed reconcile result."""

    def __init__(self, atype: str, reconcile: dict | None = None, gaps: dict | None = None, kept: list | None = None):
        self._atype = atype
        self._reconcile = reconcile or {}
        self.config_gaps = gaps or {}
        self.config_gaps_kept = kept or []
        self.main_port_manager = None

    def get_adapter_type(self) -> str:
        return self._atype

    async def reconcile_ports(self, effective):
        return dict(self._reconcile)


def _cfg_path(tmp_path):
    (tmp_path / "authentication.yaml").write_text(
        "users:\n"
        "  - username: admin\n"
        "    password_hash: 5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8\n"
        "    permissions: admin\n"
    )
    # An empty port list keeps the file schema-clean (the schema wants at least one adapter
    # section) while creating no adapter, so a "clean" test really has nothing to report.
    path = tmp_path / "server.yaml"
    path.write_text("server:\n  id: test\nloopback_ports: []\nlogging:\n  level: WARNING\n")
    return str(path)


def _server(tmp_path):
    server = OpenMuxServer(_cfg_path(tmp_path), log_level="WARNING")
    server.unified_adapters = []
    return server


# --- collecting ---


def test_a_refused_port_and_a_tolerated_violation_are_both_collected(tmp_path):
    server = _server(tmp_path)
    server.config_manager.record_schema_violation("server.yaml @ serial_ports[0].baudrate: 99 is not one of [9600]")
    server.unified_adapters = [_GapAdapter("serial", gaps={"ser9": "Parity must be N, E, O, M, or S"}, kept=["ser9"])]

    gaps = server.collect_config_gaps()

    by_port = {g.port: g for g in gaps}
    assert by_port["ser9"].section == "serial"
    assert by_port["ser9"].kept_previous is True
    assert any(g.section == "schema" and "baudrate" in g.error for g in gaps)


def test_a_server_that_matches_its_files_collects_nothing(tmp_path):
    server = _server(tmp_path)
    server.unified_adapters = [_GapAdapter("serial")]

    assert server.collect_config_gaps() == []


# --- printing ---


def test_boot_logs_one_block_naming_the_file_and_the_refused_port(tmp_path, caplog):
    server = _server(tmp_path)
    server.unified_adapters = [_GapAdapter("serial", gaps={"ser9": "Parity must be N, E, O, M, or S"})]

    with caplog.at_level(logging.WARNING):
        assert server.report_config_gaps("boot") is True

    blocks = [r.getMessage() for r in caplog.records if GAP_MARKER in r.getMessage()]
    assert len(blocks) == 1
    assert all(GAP_MARKER in line for line in blocks[0].splitlines())
    assert "server.yaml" in blocks[0]
    assert "serial/ser9" in blocks[0]
    assert "Parity must be N, E, O, M, or S" in blocks[0]


def test_a_clean_boot_logs_no_block(tmp_path, caplog):
    server = _server(tmp_path)
    server.unified_adapters = [_GapAdapter("serial")]

    with caplog.at_level(logging.WARNING):
        assert server.report_config_gaps("boot") is False

    assert GAP_MARKER not in caplog.text


def test_a_bad_serial_entry_at_boot_names_the_one_port_it_skipped(tmp_path, caplog):
    """Issue #105 at boot: one bad parity must skip one port and say so, not silence the adapter."""
    server = _server(tmp_path)
    adapter = SerialAdapter(
        "ser",
        {
            "serial_ports": [
                {"name": "ok", "device": "/tmp/openmux-gap-absent-tty", "baudrate": 9600},
                {"name": "typo", "device": "/tmp/openmux-gap-absent-tty", "baudrate": 9600, "parity": "X"},
            ]
        },
    )
    adapter.port_manager = None
    adapter.main_port_manager = None
    server.unified_adapters = [adapter]

    with caplog.at_level(logging.WARNING):
        assert server.report_config_gaps("boot") is True

    assert "ok" in adapter.serial_ports and "typo" not in adapter.serial_ports
    assert "serial/typo" in caplog.text
    assert "Parity must be N, E, O, M, or S" in caplog.text


@pytest.mark.asyncio
async def test_a_soft_reload_that_refused_a_port_logs_the_block_and_keeps_it_out_of_added(tmp_path, monkeypatch, caplog):
    refused = {
        "added": [],
        "removed": [],
        "updated": [],
        "unchanged": [],
        "failed": ["ser9"],
        "failed_detail": {"ser9": {"error": "Device not specified", "kept_previous": False}},
    }
    server = _server(tmp_path)
    server.unified_adapters = [_GapAdapter("serial", reconcile=refused)]

    async def _noop(_cfg):
        return True

    monkeypatch.setattr(server.auth_manager, "update_config", _noop)
    with caplog.at_level(logging.WARNING):
        summary = await server.reload_adapters_soft(context={"origin": "test"})

    assert summary["adapters"]["serial"]["added"] == []
    assert summary["adapters"]["serial"]["failed"] == ["ser9"]
    assert "serial/ser9" in caplog.text
    assert GAP_MARKER in caplog.text


@pytest.mark.asyncio
async def test_a_clean_soft_reload_adds_no_keys_and_logs_no_block(tmp_path, monkeypatch, caplog):
    server = _server(tmp_path)
    server.unified_adapters = [
        _GapAdapter("serial", reconcile={"added": [], "removed": [], "updated": [], "unchanged": ["ser0"]})
    ]

    async def _noop(_cfg):
        return True

    monkeypatch.setattr(server.auth_manager, "update_config", _noop)
    with caplog.at_level(logging.WARNING):
        summary = await server.reload_adapters_soft(context={"origin": "test"})

    assert summary["adapters"]["serial"] == {"added": [], "removed": [], "updated": [], "unchanged": ["ser0"]}
    assert "config_violations" not in summary
    assert GAP_MARKER not in caplog.text
