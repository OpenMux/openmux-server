"""Example Port Action: sleep for a configurable duration.

Does not touch the port; used to test the runner's one-action-per-port lock
and background-task timing (see tests/test_port_actions_plugin.py).
"""

import asyncio

ACTION = {
    "id": "slow_noop",
    "name": "Slow no-op",
    "description": "Sleeps for `seconds` without touching the port.",
    "timeout": 30.0,
    "params": [
        {"name": "seconds", "type": "float", "required": False, "default": 1.0, "description": "How long to sleep"},
    ],
}


async def run(session, params, log):
    seconds = params["seconds"]
    log("sleeping")
    await asyncio.sleep(seconds)
    log("done")
