"""The scenario index in ``docs/`` must match the tests that exist (IDX-01, IDX-02).

These two tests are why the map cannot drift. One checks that every scenario states its id, its
title, and the surfaces it proves. The other checks that the committed table says what the tier
says. Both read the tier by importing its modules, so a run of one file still checks every file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
