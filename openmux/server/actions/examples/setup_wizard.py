"""Example Port Action: a multi-step "wizard" exercising every operator-input kind.

Demonstrates all five `session.*()` input primitives (docs/design/port_actions.md
"Operator input") in one realistic-looking flow: confirm a start, type a free-text
value, pick from a fixed pair of buttons, pick from a script-supplied dropdown, pick
from a script-supplied set of radio buttons, then drive the port through a few steps
that each "take a bit of time" (simulated via `asyncio.sleep`) before waiting for a
device prompt - the classic expect-style "wait for the prompt, then proceed" pattern.
It also declares one `widget="select"` and one `widget="radio"` start-run parameter, so
the run-launch form (not just the in-run operator prompts) exercises both widgets too.

Runs fine against a loopback port for testing/demo purposes: the loopback adapter
itself emits `[ENTER]\\r\\n` whenever it sees a newline in the incoming data, so
sending a bare newline (`session.send("\\n")`) is enough to trigger the prompt that
`session.expect()` waits for below - no real device needed.
"""

import asyncio

from openmux.server.actions.errors import ActionTimeoutError

ACTION = {
    "id": "setup_wizard",
    "name": "Setup wizard (demo)",
    "description": "Confirm, name, mode, baud rate, verbosity, a simulated flash, then a reboot prompt.",
    "timeout": 400.0,
    "params": [
        {
            "name": "step_seconds",
            "type": "float",
            "required": False,
            "default": 1.0,
            "description": "How long each simulated processing stage sleeps",
        },
        {
            "name": "device_type",
            "widget": "select",
            "choices": ["router", "switch", "access_point"],
            "required": False,
            "default": "router",
            "description": "Device type (dropdown)",
        },
        {
            "name": "priority",
            "widget": "radio",
            "choices": ["low", "normal", "high"],
            "required": False,
            "default": "normal",
            "description": "Setup priority (radio buttons)",
        },
    ],
}


async def run(session, params):
    step_seconds = params["step_seconds"]
    session.debug(f"launch_params: device_type={params['device_type']} priority={params['priority']}")

    session.progress("confirm_start", 5)
    ok = await session.confirm("Start the setup wizard?", color="blue", timeout=10.0)
    if not ok:
        session.log("done: declined")
        return

    session.progress("device_name", 15)
    name = await session.wait_for_input(
        "Enter a name for this device, if you enter 'crash' we will look for something that will timeout",
        color="green",
        timeout=120.0,
    )
    if name.lower() == "crash":
        await session.send(f"waiting for non-existant prompt to time out (10s)\n")
        await session.expect(r"\[NON-EXISTANT-PROMPT\]", timeout=10.0)

    session.progress("mode", 25)
    mode = await session.choose("Pick a setup mode", ["quick", "full"], color="purple", timeout=120.0)

    session.progress("baud_rate", 35)
    baud = await session.select(
        "Pick a baud rate",
        [
            {"label": "9600 baud", "value": "9600"},
            {"label": "19200 baud", "value": "19200"},
            {"label": "115200 baud", "value": "115200"},
        ],
        color="orange",
        timeout=120.0,
    )
    session.log(f"inputs: name={name} mode={mode} baud={baud}")

    session.progress("verbosity", 45)
    verbosity = await session.radio(
        "Pick a log verbosity for this run", ["quiet", "normal", "verbose"], color="pink", timeout=120.0
    )
    session.log(f"inputs: verbosity={verbosity}")

    # Simulate a device flash that takes a moment, in a few visible stages.
    flash_percent = {"erasing": 55, "writing": 70, "verifying": 85}
    for stage in ("erasing", "writing", "verifying"):
        await session.send(f"Flashing device: {stage}...\n")
        matched = await session.expect(r"\[ENTER\]", timeout=10.0)

        session.progress(f"flash: {stage}", flash_percent[stage])
        await asyncio.sleep(step_seconds)
    session.log("done: flash complete")

    session.progress("waiting for non-existant prompt to time out (5s)", 90)
    try:
        await session.send(f"waiting for non-existant prompt to time out (5s)\n")
        await session.expect(r"\[NON-EXISTANT-PROMPT\]", timeout=5.0)
    except ActionTimeoutError:
        session.log("timed out waiting for non-existant prompt (5s)")

    # Simulate a reboot prompt: a bare newline is enough to make the loopback
    # adapter emit "[ENTER]", exercising the "wait for the device's prompt" pattern.
    session.progress("reboot", 95)
    await session.send("\n")
    matched = await session.expect(r"\[ENTER\]", timeout=10.0)
    session.log(f"reboot: matched={matched}")

    session.progress("reboot", 98)
    await session.wait_for_input("Press Enter once the device has finished rebooting", color="yellow", timeout=120.0)

    session.progress("done", 100)
    session.log(f"done: name={name} mode={mode} baud={baud} verbosity={verbosity}")
