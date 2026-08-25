"""Example Port Action: let the operator pick from script-supplied options.

Demonstrates `session.select()` (docs/design/port_actions.md "Operator input"):
the script supplies the dropdown choices, the console page renders them as a
`<select>`, and the operator's picked value comes back to the script to build
the command that gets sent to the port.
"""

ACTION = {
    "id": "select_probe",
    "name": "Select command then send",
    "description": "Asks the operator to pick a command from a dropdown, then sends it and waits for its echo.",
    "timeout": 120.0,
    "params": [],
}

_COMMANDS = [
    {"label": "Say hello", "value": "hello"},
    {"label": "Say goodbye", "value": "goodbye"},
    {"label": "Ping", "value": "ping"},
]


async def run(session):
    session.progress("select")
    command = await session.select("Pick a command to send", _COMMANDS, timeout=90.0)
    session.progress("send")
    await session.sendline(command)
    session.progress("expect_echo")
    matched = await session.expect(command, timeout=5.0)
    session.log(f"expect_echo: matched={matched}")
    session.log("done")
