"""Example Port Action: show and round-trip this port's power feeds.

Demonstrates the power-control calls described in
docs/design/port_actions.md ("Power control"): `session.list_power()`,
`session.power_off()` and `session.power_on()`. Switching is opt-in via the
`switch` parameter; without it the script only reports, and it no-ops cleanly
when no PDU is configured or the port declares no `power:` feeds.
"""

ACTION = {
    "id": "power_probe",
    "name": "Power probe",
    "description": "Lists this port's power feeds; optionally switches one feed off and back on.",
    "timeout": 60.0,
    "params": [
        {
            "name": "switch",
            "type": "bool",
            "required": False,
            "description": "Switch the first feed off and back on (default: report only)",
        },
    ],
}


async def run(session):
    session.progress("list_power")
    power = session.list_power()
    if not power.get("available"):
        session.log("power control is not available for this port (no PDU or no feeds declared)")
        session.log("done")
        return
    session.log(
        f"feeds={power['feeds_on']}/{power['feeds_total']} state={power['state']} "
        + " ".join(f"{f['ref']}={'on' if f['on'] else ('off' if f['on'] is False else 'unknown')}" for f in power["feeds"])
    )
    if not session.params.get("switch"):
        session.log("done")
        return

    ref = power["feeds"][0]["ref"]
    was_on = power["feeds"][0]["on"] is True
    session.progress(f"switch {ref}")
    if was_on:
        result = await session.power_off(ref)
        losing = result.get("impact", {}).get("losing_power") or []
        session.log(f"powered off {ref}; consoles losing all feeds: {[c['port'] for c in losing] or 'none'}")
        await session.power_on(ref)
        session.log(f"powered {ref} back on")
    else:
        await session.power_on(ref)
        session.log(f"powered on {ref} (it was off or unknown)")
    session.log("done")
