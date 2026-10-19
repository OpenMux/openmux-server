"""Example Port Action: non-interactive health check for the sidebar annotation.

Sends a line and requires a reply pattern; when the pattern does not arrive,
`session.expect()` raises and the run ends `failed`, so the sidebar dot turns
red. The script never calls `confirm()`/`wait_for_input()`: a status action
must not block waiting for an operator (docs/design/port_actions.md).

Useful on loopback ports for trying the annotation flow without real hardware:
echo the expect text yourself, or expect the port's own echo of `text`.
"""

ACTION = {
    "id": "status_probe",
    "name": "Status probe",
    "description": "Sends a line and fails when the expected reply is missing. Non-interactive.",
    "timeout": 15.0,
    "params": [
        {
            "name": "text",
            "type": "str",
            "required": True,
            "default": "STATUS?",
            "description": "Line to send to the port",
        },
        {
            "name": "expect",
            "type": "str",
            "required": True,
            "default": "STATUS\\?",
            "description": "Regex the reply must contain",
        },
    ],
}


async def run(session):
    session.progress("send")
    session.clear_buffer()
    await session.sendline(session.params["text"])
    session.progress("expect")
    # Raises ActionTimeoutError when the pattern never arrives -> run status "failed".
    matched = await session.expect(session.params["expect"], timeout=8.0)
    session.log(f"status ok: matched={matched!r}")
