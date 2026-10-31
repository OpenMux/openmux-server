"""E2E fixture Port Action: report named progress steps with percents, then finish.

No shipped example reports a percent, and an absent percent makes the run panel's bar indeterminate
with no width to read. ACT-05 needs both modes in one run, so this script reports three steps with a
percent and a last step without one. The pauses are long enough that a scenario watching the page
samples each step more than once.
"""

from __future__ import annotations

import asyncio

ACTION = {
    "id": "meter_probe",
    "name": "Meter probe",
    "description": "Reports named progress steps, three with a percent and one without.",
    "timeout": 60.0,
    "params": [],
}

#: Seconds to hold each step, so a page polling its own state cannot miss a step.
STEP_HOLD_SECONDS = 1.0


async def run(session):
    session.progress("warming", 0)
    session.log("meter: warming")
    await asyncio.sleep(STEP_HOLD_SECONDS)
    session.progress("half way", 50)
    session.log("meter: half")
    await asyncio.sleep(STEP_HOLD_SECONDS)
    session.progress("nearly", 90)
    session.log("meter: nearly")
    await asyncio.sleep(STEP_HOLD_SECONDS)
    # A step with no percent: the bar has nothing to fill, and shows the step name alone. It is held like
    # the others, because the run ending hides the bar and an unheld step would exist for milliseconds.
    session.progress("finishing")
    session.log("meter done")
    await asyncio.sleep(STEP_HOLD_SECONDS)
