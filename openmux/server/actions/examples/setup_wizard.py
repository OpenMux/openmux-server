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


async def run(session, params, log):
    step_seconds = params["step_seconds"]
    log("step_started", {"step": "launch_params", "device_type": params["device_type"], "priority": params["priority"]})

    log("step_started", {"step": "confirm_start"})
    ok = await session.confirm("Start the setup wizard?", timeout=120.0)
    if not ok:
        log("done", {"status": "declined"})
        return

    log("step_started", {"step": "device_name"})
    name = await session.wait_for_input("Enter a name for this device", timeout=120.0)

    log("step_started", {"step": "mode"})
    mode = await session.choose("Pick a setup mode", ["quick", "full"], timeout=120.0)

    log("step_started", {"step": "baud_rate"})
    baud = await session.select(
        "Pick a baud rate",
        [
            {"label": "9600 baud", "value": "9600"},
            {"label": "19200 baud", "value": "19200"},
            {"label": "115200 baud", "value": "115200"},
        ],
        timeout=120.0,
    )
    log("step_matched", {"name": name, "mode": mode, "baud": baud})

    log("step_started", {"step": "verbosity"})
    verbosity = await session.radio("Pick a log verbosity for this run", ["quiet", "normal", "verbose"], timeout=120.0)
    log("step_matched", {"verbosity": verbosity})

    # Simulate a device flash that takes a moment, in a few visible stages.
    for stage in ("erasing", "writing", "verifying"):
        await session.send(f"Flashing device: {stage}...\n")
        matched = await session.expect(r"\[ENTER\]", timeout=10.0)

        log("step_started", {"step": f"flash: {stage}"})
        await asyncio.sleep(step_seconds)
    log("done", {"step": "flash", "status": "success"})

    # Simulate a reboot prompt: a bare newline is enough to make the loopback
    # adapter emit "[ENTER]", exercising the "wait for the device's prompt" pattern.
    log("step_started", {"step": "reboot"})
    await session.send("\n")
    matched = await session.expect(r"\[ENTER\]", timeout=10.0)
    log("step_matched", {"step": "reboot", "matched": matched})

    log("step_started", {"step": "continue_prompt"})
    await session.wait_for_input("Press Enter once the device has finished rebooting", timeout=120.0)

    log("done", {"status": "success", "name": name, "mode": mode, "baud": baud, "verbosity": verbosity})
