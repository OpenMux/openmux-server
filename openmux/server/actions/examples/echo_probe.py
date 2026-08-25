"""Example Port Action: send a line, wait for it to be echoed back.

Demonstrates the ACTION metadata + `run()` contract described in
docs/design/port_actions.md ("Script format"). Written against a loopback
port's echo behavior; used by tests/test_action_runner.py.
"""

ACTION = {
    "id": "echo_probe",
    "name": "Echo probe",
    "description": "Sends a line to the port and waits for it to be echoed back.",
    "timeout": 10.0,
    "params": [
        {"name": "text", "type": "str", "required": True, "description": "Text to send"},
    ],
}


async def run(session, params):
    text = params["text"]
    session.progress("send")
    await session.sendline(text)
    session.progress("expect_echo")
    matched = await session.expect(text, timeout=5.0)
    session.log(f"expect_echo: matched={matched}")
    session.log("done")
