"""The tier checks its own reporting: the scenario index and the uncovered-run notice (IDX-01 to IDX-03).

Three tests, and none of them starts a server. Two keep the map honest: one checks that every scenario
states its id, its title, and the surfaces it proves, and the other checks that the committed table says
what the tier says. The third checks that a run without a browser says which scenarios asserted nothing.
All three read the tier by importing its modules, so a run of one file still checks every file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from . import conftest
from . import console as e2e_console
from .console import browser_skip_notice, note_browser_skip
from .index import differences, read_index, render, scan_e2e_dir
from .story import surfaces

pytestmark = [pytest.mark.e2e]

#: The committed index, written by ``make e2e-doc``.
INDEX_PATH = Path(__file__).resolve().parents[2] / "docs" / "E2E_TESTS.md"


@surfaces("collection")
def test_idx01_every_scenario_states_its_id_title_and_surfaces():
    """Every end-to-end test carries a scenario id, a one-line title, and a surface list."""
    rows = scan_e2e_dir()
    assert rows, "no end-to-end scenarios were found, so the tier would be unproven"
    incomplete = [row for row in rows if row.problem]
    report = "\n".join(f"  {row.location}: {row.problem}" for row in incomplete)
    assert not incomplete, f"{len(incomplete)} scenario(s) are missing what the index needs:\n{report}"


@surfaces("collection")
def test_idx02_the_committed_index_matches_the_scenarios_that_exist():
    """docs/E2E_TESTS.md is current with the scenarios that exist."""
    problems = differences(read_index(INDEX_PATH), render(scan_e2e_dir()))
    report = "\n".join(f"  {line}" for line in problems)
    assert not problems, f"docs/E2E_TESTS.md is stale:\n{report}\nRegenerate with: make e2e-doc"


@surfaces("collection")
def test_idx03_a_run_without_a_browser_names_the_scenarios_that_asserted_nothing(monkeypatch):
    """A run that could not start a browser says how many scenarios it covered, not only how many passed.

    A machine without Chromium answers ``60 passed, 41 skipped``: the pass count alone reads like
    success, and the skip reason stays hidden unless the caller passes ``-rs``. The tier prints a notice
    so an uncovered run does not look like a covered one. This drives the notice builder directly,
    because whether this machine has a browser is not what is under test.
    """
    monkeypatch.setattr(e2e_console, "_BROWSER_SKIPPED", {})
    assert browser_skip_notice() == [], "an all-covered run must print no notice"
    # One reason, multi-line: the browser answers with a banner, and the notice keeps the first line.
    banner = "Chromium cannot start: BrowserType.launch: Executable doesn't exist\n║ a banner line ║\nRun: install chromium"
    note_browser_skip("tests/e2e/test_console.py::test_con01", banner)
    note_browser_skip("tests/e2e/test_power.py::test_pwr07", banner)
    lines = browser_skip_notice()
    assert lines[0].startswith("2 end-to-end scenario"), lines
    assert any("make e2e-install" in line for line in lines), lines
    reason_lines = [line for line in lines if "Chromium cannot start" in line]
    assert len(reason_lines) == 1, f"one reason became {len(reason_lines)} lines: {lines}"
    assert "banner" not in "".join(lines), f"a banner bled into the notice: {lines}"
    # A second reason gets its own line, and the header counts every scenario across both reasons.
    note_browser_skip("tests/e2e/test_console.py::test_con02", "playwright is not installed (No module named playwright)")
    lines = browser_skip_notice()
    assert lines[0].startswith("3 end-to-end scenario"), lines
    assert any("1 scenario: playwright is not installed" in line for line in lines), lines
    assert any("2 scenarios: Chromium cannot start" in line for line in lines), lines
    # The hook must be registered, or the builder above never reaches the console.
    assert hasattr(conftest, "pytest_terminal_summary"), "the run prints no uncovered-run notice"
