"""Server-level tests for the config gap warning (issue #105).

The rule these pin: whenever a config file asks for something the running server does not have,
the server logs one unmistakable block naming the file and each entry that did not apply. A
clean boot and a clean reload log nothing.

Follows the harness of tests/server/test_reload_adapters_soft_coverage.py: a real OpenMuxServer on
a minimal tmp config with no adapter sections, so no real adapter or socket is created.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Dict, List

import pytest

from openmux.server.adapters.loopback import LoopbackAdapter, LoopbackPort
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


class _FailedStartAdapter:
    """Adapter stub the full reload can create and wire, but cannot start.

    ``start()`` raises, which is the case the reload records as ``start_error``: the adapter exists,
    its port does not, and only the summary carries why.
    """

    def __init__(self, name: str, atype: str) -> None:
        self.name = name
        self._atype = atype
        self.main_port_manager = None

    def get_adapter_type(self) -> str:
        return self._atype

    async def start(self) -> bool:
        raise RuntimeError("listener port in use")


class _RefusingAdapter:
    """Adapter stub that starts and holds one entry it refused to build.

    This is the CFG-19 case as the running server sees it: the adapter works, one port of its section
    is missing, and the refusal sits in ``config_gaps`` because no reconcile call reported it.
    """

    def __init__(self, name: str, atype: str, gaps: Dict[str, str]) -> None:
        self.name = name
        self._atype = atype
        self.config_gaps = dict(gaps)
        self.config_gaps_kept: List[str] = []
        self.main_port_manager = None

    def get_adapter_type(self) -> str:
        return self._atype

    async def stop(self) -> bool:
        return True

    async def start(self) -> bool:
        return True


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


@pytest.mark.asyncio
async def test_a_full_reload_that_could_not_start_an_adapter_names_it_and_its_reason(tmp_path, monkeypatch, caplog):
    """The block reads the summary a real full reload built, not a shape a test invented (issue #105).

    The reload records one dict per failure, so this asserts that dict as the producer writes it and
    then asserts what the operator reads. A failed adapter is the gap a status page never shows: the
    adapter object exists, no port of it runs, and a listener that lost its port says nothing.
    """
    server = _server(tmp_path)
    created = _FailedStartAdapter("telnet_listener", "telnet_listener")

    def _create(_config):
        return [created]

    monkeypatch.setattr(server.unified_adapter_factory, "create_adapters_from_config", _create)
    with caplog.at_level(logging.WARNING):
        summary = await server.reload_adapters_full(context={"origin": "test"})

    assert summary["errors"] == [{"adapter": "telnet_listener", "start_error": "listener port in use"}]
    assert "telnet_listener: listener port in use" in caplog.text, caplog.text
    assert GAP_MARKER in caplog.text
    # The reason is a sentence naming the adapter, never the dict the summary holds.
    assert "{'adapter'" not in caplog.text, caplog.text


@pytest.mark.asyncio
async def test_a_full_reload_that_refused_a_port_names_that_port_in_its_answer(tmp_path, monkeypatch, caplog):
    """A caller that reads the full reload answer learns what a reader of the log learns (issue #105).

    A full reload reconciles nothing, so a refused entry can reach the summary only through the adapter
    that refused it. Without those keys the answer says ``errors: []`` - true, and useless - while the
    log names the port. The pair a person needs is the answer that says the reload finished and the
    name that says the file still needs a fix.
    """
    server = _server(tmp_path)
    created = _RefusingAdapter("loopback_ports", "loopback", {"lb9": "Invalid max_read_write_users for lb9: 'two'"})
    monkeypatch.setattr(server.unified_adapter_factory, "create_adapters_from_config", lambda _config: [created])

    with caplog.at_level(logging.WARNING):
        summary = await server.reload_adapters_full(context={"origin": "test"})

    assert summary["errors"] == [], summary
    assert summary["adapters"]["loopback"]["failed"] == ["lb9"], summary
    detail = summary["adapters"]["loopback"]["failed_detail"]["lb9"]
    assert detail["error"] == "Invalid max_read_write_users for lb9: 'two'", detail
    assert detail["kept_previous"] is False, detail
    # One refusal, seen twice (adapter state and the summary), is still one line in the block.
    block = [line for line in caplog.text.splitlines() if "loopback/lb9:" in line]
    assert len(block) == 1, caplog.text


@pytest.mark.asyncio
async def test_a_full_reload_that_applied_everything_adds_no_gap_keys(tmp_path, monkeypatch, caplog):
    """The keys appear only when something stayed unapplied, so a clean answer keeps its own shape."""
    server = _server(tmp_path)
    created = _RefusingAdapter("loopback_ports", "loopback", {})
    monkeypatch.setattr(server.unified_adapter_factory, "create_adapters_from_config", lambda _config: [created])

    with caplog.at_level(logging.WARNING):
        summary = await server.reload_adapters_full(context={"origin": "test"})

    assert "adapters" not in summary, summary
    assert "config_violations" not in summary, summary
    assert GAP_MARKER not in caplog.text


@pytest.mark.asyncio
async def test_a_reload_stops_naming_a_port_the_operator_deleted(tmp_path, caplog):
    """A gap whose entry the file no longer lists must leave both the answer and the log."""
    adapter = LoopbackAdapter("loopback", {"loopback_ports": [{"name": "lb0"}]})
    adapter.main_port_manager = None
    adapter.ports["lb0"] = LoopbackPort("lb0", {"name": "lb0"}, adapter)
    server = _server(tmp_path)
    server.unified_adapters = [adapter]
    path = pathlib.Path(server.config_manager.config_path)

    _write_loopback(path, [{"name": "lb0"}, {"name": "lb1", "max_read_write_users": "two"}])
    with caplog.at_level(logging.INFO, logger="openmux"):
        refused = await server.reload_adapters_soft({})
    assert refused["adapters"]["loopback"]["failed"] == ["lb1"]
    assert "loopback/lb1:" in caplog.text

    # Deleting the entry fixes the file too. Nothing is missing after that, so the next reload
    # reports no gap and prints no block (issue #105).
    _write_loopback(path, [{"name": "lb0"}])
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="openmux"):
        deleted = await server.reload_adapters_soft({})

    # The soft answer always carries the per-adapter reconcile results, so the gap keys are what
    # has to be gone here.
    assert "failed" not in deleted["adapters"]["loopback"]
    assert "failed_detail" not in deleted["adapters"]["loopback"]
    assert "config_violations" not in deleted
    assert GAP_MARKER not in caplog.text
    assert "lb1" not in adapter.ports
    if "lb0" in adapter.ports:
        await adapter.ports["lb0"].stop()


def _write_loopback(path, entries):
    """Rewrite the config file so its loopback_ports section lists ``entries``."""
    import yaml

    document = {"server": {"id": "test"}, "loopback_ports": entries, "logging": {"level": "WARNING"}}
    path.write_text(yaml.safe_dump(document, sort_keys=False))
