"""Tests for the config gap report (issue #105).

The gap is the difference between what a config file asks for and what the running server
actually holds. These tests cover what counts as a gap, how the reload summary is read, and
that every line of the printed warning is findable with one grep.
"""

from __future__ import annotations

import logging

from openmux.server.config_gaps import (
    GAP_MARKER,
    ConfigGap,
    format_warning,
    log_gap_warning,
    unique_gaps,
    violations_from_summary,
)


def _summary() -> dict:
    """Return a reload summary shaped like the one reload_adapters_soft() returns."""
    return {
        "auth_updated": True,
        "config_violations": ["serial_ports.ser9.baudrate: 99 is not one of [9600, 19200]"],
        "adapters": {
            "serial": {
                "added": ["ser1"],
                "removed": [],
                "updated": [],
                "unchanged": ["ser0"],
                "failed": ["ser2", "ser3"],
                "kept_previous": ["ser3"],
                "failed_detail": {
                    "ser2": {"error": "Parity must be N, E, O, M, or S", "kept_previous": False},
                    "ser3": {"error": "Device not specified", "kept_previous": True},
                },
            },
            "loopback": {"added": [], "removed": [], "updated": [], "unchanged": ["lb1"]},
            "muxcon": {"error": "listener port in use", "reloaded": True},
        },
    }


# --- reading the summary ---


def test_a_summary_with_no_gap_reads_as_no_gap():
    clean = {"auth_updated": False, "adapters": {"serial": {"added": [], "removed": [], "updated": ["s"], "unchanged": []}}}

    assert violations_from_summary(clean) == []


def test_every_failure_kind_in_a_summary_becomes_a_gap():
    gaps = violations_from_summary(_summary())
    kinds = {(g.section, g.port, g.error, g.kept_previous) for g in gaps}

    assert ("serial", "ser2", "Parity must be N, E, O, M, or S", False) in kinds
    assert ("serial", "ser3", "Device not specified", True) in kinds
    assert ("muxcon", None, "listener port in use", False) in kinds
    assert any(g.section == "schema" and "baudrate" in g.error for g in gaps)


def test_a_refused_port_without_a_detail_entry_still_becomes_a_gap():
    summary = {"adapters": {"serial": {"failed": ["ser8"], "unchanged": []}}}

    gaps = violations_from_summary(summary)

    assert [g.port for g in gaps] == ["ser8"]
    # The block always shows a reason, even when the adapter recorded none.
    assert gaps[0].describe()


def test_each_port_is_reported_once_even_when_listed_twice():
    gaps = violations_from_summary(_summary())

    assert sum(1 for g in gaps if g.port == "ser3") == 1


def test_a_full_reload_failure_names_the_adapter_and_the_reason_it_gave():
    """The four dict shapes ``reload_adapters_full()`` appends, each read as its own gap."""
    summary = {
        "stopped": 3,
        "started": 2,
        "errors": [
            {"adapter": "serial_ports", "start_error": "start returned False"},
            {"adapter": "telnet_listener", "stop_error": "listener busy"},
            {"adapter": "web_console", "wire_error": "'NoneType' object has no attribute 'x'"},
            {"phase": "create_adapters", "error": "Invalid configuration for Serial Ports"},
        ],
    }

    gaps = violations_from_summary(summary)

    assert [(g.section, g.port, g.error) for g in gaps] == [
        ("serial_ports", None, "start returned False"),
        ("telnet_listener", None, "listener busy"),
        ("web_console", None, "'NoneType' object has no attribute 'x'"),
        ("create_adapters", None, "Invalid configuration for Serial Ports"),
    ]


def test_a_full_reload_timeout_gets_a_sentence_because_the_entry_holds_only_a_number():
    summary = {"errors": [{"adapter": "serial_ports", "start_timeout": 10.0}, {"adapter": "muxcon", "stop_timeout": 5}]}

    gaps = violations_from_summary(summary)

    assert [(g.section, g.error) for g in gaps] == [
        ("serial_ports", "start timed out after 10.0s"),
        ("muxcon", "stop timed out after 5s"),
    ]


def test_an_error_the_reporter_does_not_recognise_is_still_reported():
    """A failure must never be dropped because its shape is unexpected."""
    summary = {"errors": ["Serial adapter ser failed to start", {"adapter": "serial_ports", "other": 1}]}

    gaps = violations_from_summary(summary)

    assert [(g.section, g.error) for g in gaps] == [
        ("adapter", "Serial adapter ser failed to start"),
        ("adapter", "{'adapter': 'serial_ports', 'other': 1}"),
    ]


def test_per_port_schema_lines_are_reported_as_gaps():
    summary = {"adapters": {"serial": {"updated": [], "violations": ["baudrate: 99 is not one of [9600]"]}}}

    gaps = violations_from_summary(summary)

    assert [(g.section, g.error) for g in gaps] == [("serial", "baudrate: 99 is not one of [9600]")]


def test_a_summary_that_names_no_adapter_yields_only_the_schema_gaps():
    """A summary holding nothing the reporter reads yields no gaps, so a clean reload logs nothing."""
    summary = {"adapters": {"serial": {"added": [], "removed": [], "updated": [], "unchanged": ["ser0"]}}}

    assert violations_from_summary(summary) == []


# --- rendering ---


def test_the_same_port_reported_twice_is_printed_once():
    """Adapter state and the reload summary describe the same refusal; merge must not double it."""
    gaps = unique_gaps(
        [
            ConfigGap("serial", "ser2", "Parity must be N, E, O, M, or S"),
            ConfigGap("serial", "ser2", "Parity must be N, E, O, M, or S"),
            ConfigGap("serial", "ser3", "Device not specified"),
        ]
    )

    assert [(g.section, g.port) for g in gaps] == [("serial", "ser2"), ("serial", "ser3")]


def test_two_problems_in_one_section_are_both_kept():
    gaps = unique_gaps([ConfigGap("schema", None, "bad baudrate"), ConfigGap("schema", None, "unknown key")])

    assert len(gaps) == 2


def test_no_gap_renders_no_block():
    assert format_warning("soft reload", []) == ""


def test_the_block_names_the_occasion_the_file_and_every_gap():
    text = format_warning("soft reload", violations_from_summary(_summary()), source="/etc/openmux/server.yaml")
    lines = text.splitlines()

    # The block opens and closes with a rule so it cannot be missed in a scrolling log.
    assert set(lines[0][len(GAP_MARKER) :].strip()) == {"!"}
    assert set(lines[-1][len(GAP_MARKER) :].strip()) == {"!"}
    assert "soft reload" in text
    assert "/etc/openmux/server.yaml" in text
    assert any("ser2" in line and "Parity must be N, E, O, M, or S" in line for line in lines)
    assert any("ser3" in line and "kept" in line for line in lines)
    assert any("baudrate" in line for line in lines)


def test_only_a_kept_port_claims_the_old_config_is_still_running():
    gaps = [
        ConfigGap("serial", "fresh", "Device not specified", kept_previous=False),
        ConfigGap("serial", "live", "Parity must be N, E, O, M, or S", kept_previous=True),
    ]
    lines = [line for line in format_warning("boot", gaps).splitlines() if "fresh" in line or "live" in line]

    kept_line = next(line for line in lines if "live" in line)
    refused_new_line = next(line for line in lines if "fresh" in line)
    assert "kept" in kept_line
    assert "kept" not in refused_new_line


def test_every_line_of_the_block_carries_the_marker():
    """One grep must return the whole block, even in an interleaved log."""
    text = format_warning("boot", violations_from_summary(_summary()), source="server.yaml")

    lines = text.splitlines()
    assert len(lines) > 4
    assert all(GAP_MARKER in line for line in lines)


def test_the_adapter_section_and_port_are_both_named():
    text = format_warning("boot", [ConfigGap("serial", "ser2", "Device not specified")])

    assert "serial" in text and "ser2" in text


# --- logging ---


def test_log_gap_warning_is_silent_when_nothing_is_wrong():
    logger = logging.getLogger("test.config_gaps.quiet")
    assert log_gap_warning(logger, "soft reload", []) is False


def test_log_gap_warning_emits_one_record_whose_lines_all_match_the_marker(caplog):
    logger = logging.getLogger("test.config_gaps.loud")
    with caplog.at_level(logging.WARNING):
        assert log_gap_warning(logger, "soft reload", violations_from_summary(_summary())) is True

    records = [r for r in caplog.records if GAP_MARKER in r.getMessage()]
    assert len(records) == 1
    physical = records[0].getMessage().splitlines()
    assert all(GAP_MARKER in line for line in physical)
    assert records[0].levelname == "WARNING"
    assert records[0].config_gap is True
