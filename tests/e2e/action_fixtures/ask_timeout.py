"""E2E fixture Port Action: ask one prompt that expires with no answer.

ACT-11 needs a prompt that closes on its own, so a scenario can prove the box a viewer was
shown disappears on every page when nobody answers it. Only the script's own answer timeout does this:
answering it would close the box on the answering page too, and finishing the run would hide the box on
its own. This script therefore keeps running after the prompt expires, so a scenario sees the box
alone while the run is still live.
"""

from __future__ import annotations

import asyncio

from openmux.server.actions.errors import ActionTimeoutError

ACTION = {
    "id": "ask_timeout",
    "name": "Ask and expire",
    "description": "Asks one prompt, finishes it when the answer timeout expires, and reports that nobody answered.",
    "timeout": 120.0,
    "params": [],
}

#: How long the prompt stays open. Long enough for a second page to join and be shown the same box,
#: short enough that the scenario never waits on it.
PROMPT_TIMEOUT_SECONDS = 6.0

#: How long the run stays live after the prompt expired. A scenario checks three pages in this window,
#: and the run must stay running while it does, so that only the expiry can explain a closed box.
LIVE_AFTER_EXPIRY_SECONDS = 12.0

#: The prompt the script asks, and never gets an answer to.
QUESTION = "Type a label nobody types"


async def run(session):
    session.progress("ask once")
    try:
        await session.wait_for_input(QUESTION, timeout=PROMPT_TIMEOUT_SECONDS)
        session.log("someone answered after all")
    except ActionTimeoutError:
        # The expiry already closed the open prompt and told every viewer (session.py). Carry on so the
        # run stays live: an operator must still be able to work with a run whose prompt timed out.
        session.progress("nobody answered")
    await asyncio.sleep(LIVE_AFTER_EXPIRY_SECONDS)
    session.log("ask_timeout finished with no answer")
