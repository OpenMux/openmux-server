"""Example Port Action: pause for operator confirmation before sending.

Demonstrates the operator-input channel described in docs/design/port_actions.md
("Operator input"): `session.wait_for_input()`/`session.confirm()` pause the
script until the run's launcher answers via the console page's run panel
(a `waiting_for_operator` structured event is published automatically
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


async def run(session):
    text = session.params["text"]
    session.progress("confirm")
    ok = await session.confirm(f"Send {text!r} to the port?", timeout=90.0)
    if not ok:
        session.log("done: declined")
        return
    session.progress("send")
    await session.sendline(text)
    session.progress("expect_echo")
    matched = await session.expect(text, timeout=5.0)
    session.log(f"expect_echo: matched={matched}")
    session.log("done")
