"""E2E fixture Port Action: ask the operator one prompt of each kind.

The shipped examples show one kind each (`confirm_probe.py` shows buttons, `select_probe.py` shows a
dropdown). ACT-06 needs one run to move through all four kinds, so a scenario can prove the operator
box swaps its controls per kind and that each answer reaches the script and the port. Each answer is
sent to the port and awaited as its echo, so one answer finishes before the next prompt opens.
"""

from __future__ import annotations

ACTION = {
    "id": "ask_probe",
    "name": "Ask every kind",
    "description": "Asks one prompt of each kind, then sends each answer to the port.",
    "timeout": 180.0,
    "params": [],
}

_SPEEDS = [
    {"label": "Slow mode", "value": "slow"},
    {"label": "Fast mode", "value": "fast"},
]

_LINKS = [
    {"label": "Copper link", "value": "copper"},
    {"label": "Fibre link", "value": "fibre"},
]

_TIMEOUT_SECONDS = 120.0


async def _send_and_await_echo(session, text: str) -> None:
    """Send the operator's answer and wait for the port's echo of it."""
    session.progress(f"send {text}")
    await session.sendline(text)
    await session.expect(text, timeout=15.0)


async def run(session):
    session.progress("ask label")
    label = await session.wait_for_input("Type a label to send", color="red", timeout=_TIMEOUT_SECONDS)
    await _send_and_await_echo(session, label)

    session.progress("ask mode")
    mode = await session.choose("Pick a mode to run in", ["calm", "lively"], timeout=_TIMEOUT_SECONDS)
    await _send_and_await_echo(session, mode)

    session.progress("ask speed")
    speed = await session.select("Pick a speed", _SPEEDS, timeout=_TIMEOUT_SECONDS)
    await _send_and_await_echo(session, speed)

    session.progress("ask link")
    link = await session.radio("Pick a link", _LINKS, timeout=_TIMEOUT_SECONDS)
    await _send_and_await_echo(session, link)

    session.log(f"asked every kind: {label} {mode} {speed} {link}")
