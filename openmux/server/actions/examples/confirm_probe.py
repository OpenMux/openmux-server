"""Example Port Action: pause for operator confirmation before sending.

Demonstrates the operator-input channel described in docs/design/port_actions.md
("Operator input"): `session.wait_for_input()`/`session.confirm()` pause the
script until the run's launcher answers via the console page's run panel
(a `step_waiting_for_operator` structured event is published automatically
while waiting).
"""

ACTION = {
    "id": "confirm_probe",
    "name": "Confirm then send",
    "description": "Asks the operator to confirm, then sends `text` and waits for its echo.",
    "timeout": 120.0,
    "params": [
        {"name": "text", "type": "str", "required": True, "description": "Text to send after confirmation"},
    ],
}


async def run(session, params, log):
    text = params["text"]
    log("step_started", {"step": "confirm"})
    ok = await session.confirm(f"Send {text!r} to the port?", timeout=90.0)
    if not ok:
        log("done", {"status": "declined"})
        return
    log("step_started", {"step": "send"})
    await session.sendline(text)
    log("step_started", {"step": "expect_echo"})
    matched = await session.expect(text, timeout=5.0)
    log("step_matched", {"step": "expect_echo", "matched": matched})
    log("done", {"status": "success"})
