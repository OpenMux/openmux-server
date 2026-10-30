"""Adapter factory tests for one unusable port entry (issue #105 follow-up).

``SerialAdapter`` parses its own entries and skips the one it cannot build, so a bad ``parity``
costs one port. The other three port adapters answer the same question inside ``validate_config``,
which covers the whole section, and ``_validate_legacy_config`` turns a False there into
``Invalid configuration for <plugin>``: the factory creates no adapter, so every port of that
section is lost and no object exists to record a config gap. These tests boot through the real
factory, because that gate is only on this path.

Two rules hold the line:
* one entry the adapter can name a reason for costs only that entry, and its reason reaches the
  adapter's config gaps, so the ``CONFIG_GAP`` block still prints at boot;
* nothing an adapter cannot explain changes behaviour: a section the adapter refuses without
  naming an entry, and any section that is not a list of port entries, is skipped whole as before.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from openmux.server.adapters.base_adapter import AdapterCapability, BaseGenericAdapter
from openmux.server.adapters.factory import AdapterPlugin, GenericAdapterFactory, PluginRegistry

CAT = {"name": "cat0", "command": "/bin/cat"}
CAT_2 = {"name": "cat1", "command": "/bin/echo"}
NO_COMMAND = {"name": "nocmd"}
NO_NAME = {"command": "/bin/cat"}
LB = {"name": "a"}
LB_BAD_MODE = {"name": "b", "max_read_write_users": "two"}
TCP = {"name": "t0", "host": "127.0.0.1", "port": 1}
TCP_NO_HOST = {"name": "t1"}


def _boot(config: Dict[str, Any]) -> List[BaseGenericAdapter]:
    """Create adapters the way startup and a full reload do: one call into the factory."""
    return GenericAdapterFactory().create_adapters_from_config(dict(config, server={}))


def _only(adapters: List[BaseGenericAdapter], kind: str) -> BaseGenericAdapter:
    """The one adapter of ``kind``, so a lost section fails as 'no adapter', not an IndexError."""
    found = [a for a in adapters if a.get_adapter_type() == kind]
    assert len(found) == 1, f"expected exactly one {kind} adapter, got {[type(a).__name__ for a in adapters]}"
    return found[0]


def test_a_command_entry_without_command_costs_only_that_command_port():
    """``command_ports`` holds one entry with no ``command``: the other command ports still start."""
    adapters = _boot({"command_ports": [dict(CAT), dict(CAT_2), dict(NO_COMMAND)]})

    adapter = _only(adapters, "command")
    assert sorted(adapter.get_port_configurations()) == ["cat0", "cat1"], "the bad entry survived or the good ones went"
    assert "nocmd" in adapter.config_gaps, adapter.config_gaps
    assert "command" in adapter.config_gaps["nocmd"].lower(), adapter.config_gaps


def test_a_loopback_entry_with_an_unusable_capacity_costs_only_that_loopback_port():
    """``loopback_ports`` holds an unknown write-slot capacity: the port beside it is not lost."""
    adapters = _boot({"loopback_ports": [dict(LB), dict(LB_BAD_MODE)]})

    adapter = _only(adapters, "loopback")
    assert sorted(adapter.get_port_configurations()) == ["a"], adapter.get_port_configurations()
    assert "b" in adapter.config_gaps, adapter.config_gaps
    assert "max_read_write_users" in adapter.config_gaps["b"], adapter.config_gaps


def test_a_tcp_entry_with_no_host_costs_only_that_tcp_port():
    """``tcp_initiator_ports`` holds an entry with no ``host``: the port beside it is not lost."""
    adapters = _boot({"tcp_initiator_ports": [dict(TCP), dict(TCP_NO_HOST)]})

    adapter = _only(adapters, "tcp_initiator")
    assert sorted(adapter.get_port_configurations()) == ["t0"], adapter.get_port_configurations()
    assert "t1" in adapter.config_gaps, adapter.config_gaps


def test_a_command_entry_with_no_name_is_reported_by_its_position():
    """An entry with no ``name`` has no port name to report, so its place in the file is named."""
    adapters = _boot({"command_ports": [dict(CAT), dict(NO_NAME)]})

    adapter = _only(adapters, "command")
    assert sorted(adapter.get_port_configurations()) == ["cat0"], adapter.get_port_configurations()
    reported = list(adapter.config_gaps)
    assert reported == ["<unnamed #2>"], f"the gap should name the entry position: {adapter.config_gaps}"
    assert "name" in adapter.config_gaps[reported[0]].lower(), adapter.config_gaps


def test_a_clean_section_is_untouched_by_the_check():
    """A section with nothing wrong with it is passed to the adapter whole, with no gap."""
    adapters = _boot({"command_ports": [dict(CAT), dict(CAT_2)]})

    adapter = _only(adapters, "command")
    assert sorted(adapter.get_port_configurations()) == ["cat0", "cat1"], adapter.get_port_configurations()
    assert adapter.config_gaps == {}, adapter.config_gaps


def test_a_section_where_every_entry_is_refused_still_reports_every_reason():
    """All entries bad leaves no port, but leaves an adapter that says why instead of a silent skip."""
    adapters = _boot({"serial_ports": [{"name": "p0", "baudrate": 115200}, {"name": "p1", "baudrate": 9600}]})

    adapter = _only(adapters, "Serial")
    assert adapter.get_port_configurations() == {}, adapter.get_port_configurations()
    assert sorted(adapter.config_gaps) == ["p0", "p1"], adapter.config_gaps


def test_a_reason_the_adapter_already_reported_is_not_printed_twice(caplog):
    """``SerialAdapter`` reports a bad entry as it parses the section: the load must not repeat it."""
    with caplog.at_level("ERROR"):
        _boot({"serial_ports": [{"name": "p0", "baudrate": 115200}]})

    repeated = [r.getMessage() for r in caplog.records if "device path" in r.getMessage()]
    assert len(repeated) == 1, f"one entry should produce one reason line: {repeated}"


# --- what must NOT change ----------------------------------------------------------------------


class _OpaqueAdapter(BaseGenericAdapter):
    """An adapter that refuses its section but cannot say which entry: no per-port reason exists."""

    adapter_type = "opaque_section"

    def get_capabilities(self):  # pragma: no cover - capability set is not under test
        return {AdapterCapability.PROVIDES_PORTS}

    def get_adapter_type(self) -> str:
        return "opaque_section"

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> bool:
        return False

    def get_port_configurations(self) -> Dict[str, Dict[str, Any]]:  # pragma: no cover - never started
        return {}

    async def create_port(self, port_name: str, config: Dict[str, Any]):  # pragma: no cover - never started
        return None

    async def destroy_port(self, port_name: str) -> None:  # pragma: no cover - never started
        pass

    async def _do_start(self) -> bool:  # pragma: no cover - never started
        return True

    async def stop(self) -> None:  # pragma: no cover - never started
        pass


def _boot_opaque(section_config: Any) -> List[BaseGenericAdapter]:
    registry = PluginRegistry()
    registry.register_plugin(AdapterPlugin("Opaque", "opaque_ports", _OpaqueAdapter))
    factory = GenericAdapterFactory(registry)
    return factory.create_adapters_from_config({"server": {}, "opaque_ports": section_config})


def test_a_section_the_adapter_cannot_explain_is_still_skipped_whole():
    """No per-entry reason means no guess: the old all-or-nothing skip stays."""
    assert _boot_opaque([{"name": "p0"}]) == []


def test_a_section_that_is_not_a_port_list_is_still_skipped_whole():
    """A mapping where a list of port entries belongs is a section error, not an entry error."""
    assert _boot_opaque({"name": "p0"}) == []


@pytest.mark.parametrize("section_config", [None, "not-even-a-mapping", 7])
def test_a_section_of_the_wrong_kind_is_still_skipped_whole(section_config: Any):
    """Whatever the adapter says about a section of the wrong kind, nothing is created."""
    assert _boot_opaque(section_config) == []


def test_an_adapter_without_port_pre_validation_leaves_the_section_alone():
    """The helper itself must say "nothing dropped" instead of raising when it cannot pre-validate."""
    adapter = _OpaqueAdapter("opaque", {"opaque_ports": [dict(LB)]})

    assert adapter.reject_unusable_entries("opaque_ports") == []
    assert adapter.config_gaps == {}, adapter.config_gaps
    assert len(adapter.config["opaque_ports"]) == 1, "the section must be passed on untouched"


def test_a_section_that_is_not_a_list_is_left_alone_by_the_helper():
    """A mapping under the section key is not a list of entries, so the helper has nothing to judge."""
    adapter = _OpaqueAdapter("opaque", {"opaque_ports": dict(LB)})

    assert adapter.reject_unusable_entries("opaque_ports") == []
    assert adapter.config_gaps == {}, adapter.config_gaps
