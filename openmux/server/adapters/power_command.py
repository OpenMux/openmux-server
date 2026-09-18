"""Shared POWER command interpreter for the text-protocol listeners.

The `POWER` command has the same forms and wording on every text surface (the
OpenMux client listener, telnet, and SSH), so the parsing, permission/group
gates, impact pre-flight, and reply wording live here once and are driven by
an injectable `send_line` hook. `openmux/client/console.py` is NOT covered:
it speaks the structured OMX protocol and is a separate surface.

Notice wording (the `[POWER]` / `[POWER WARNING]` lines pushed to an attached
session when a feed changes under it) is also defined here so the three
listeners all write identical text.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .listener_common import read_prompt_line


def find_power_adapter(console_manager: Any) -> Optional[Any]:
    """Return the active PDU adapter, or None.

    Scans the port manager's unified adapters for ``adapter_type == "power"``.
    Called on demand (never cached) so a soft reload of the ``power:`` section
    is picked up without restarting the listener.
    """
    try:
        pm = getattr(console_manager, "port_manager", None) if console_manager else None
        for a in getattr(pm, "unified_adapters", []) or []:
            try:
                if str(a.get_adapter_type()).lower() == "power":
                    return a
            except Exception:
                continue
    except Exception:
        # justification: optional lookup; the caller replies a POWER error line instead
        return None
    return None


def format_outlet_line(o: Dict[str, Any]) -> str:
    """Render one outlet snapshot entry as a single LIST-style line."""
    on = o.get("on")
    state = "unknown" if on is None else ("on" if on else "off")
    watts = o.get("watts")
    volts = o.get("volts")
    load = ""
    if watts is not None:
        load = " %.0fW" % watts
        if volts is not None:
            load += " %.0fV" % volts
    ports = o.get("mapped_ports") or []
    feeds = ("  -> " + ", ".join(ports)) if ports else ""
    err = "  (" + str(o.get("error")) + ")" if o.get("error") else ""
    return "POWER %s %s%s%s" % (o.get("ref"), state, load, feeds + err)


def format_power_notice(changes: Dict[str, Any]) -> str:
    """Render a ``power_outlet_changed`` meta payload as the in-terminal notice.

    Mirrors the client-listener wording so every text surface pushes the same
    two lines (feed state, or the all-lost warning) to an attached session.
    """
    outlet = str(changes.get("outlet"))
    on = changes.get("on")
    if changes.get("all_power_lost"):
        return "\r\n[POWER WARNING] all power feeds are now OFF for this console (" + outlet + ")\r\n"
    state = "on" if on is True else ("off" if on is False else "unknown")
    return "\r\n[POWER] feed " + outlet + " is now " + state + "\r\n"


async def run_power_command(
    console_manager: Any,
    command: str,
    send_line: Callable[[str], Awaitable[None]],
    username: Optional[str],
    auth_manager: Any,
    client_id: Optional[str] = None,
) -> None:
    """Interpret one ``POWER`` command and send its reply lines.

    Forms (reply wording is fixed and shared by all text surfaces):
        POWER                       - list every PDU + outlet
        POWER <pdu>                 - list one PDU's outlets
        POWER <pdu>.<outlet>        - report one outlet
        POWER <pdu>.<outlet> on|off - switch an outlet (read-write/admin,
                                      scoped to the user's console groups)

    Switching is rejected (with an ERROR line) when the identity lacks
    read-write globally, or when the outlet feeds a console the identity may
    not open (that needs admin). See PduAdapter._power_blocked_ports.

    Returns True only when the command switched an outlet successfully (the
    caller knows whether to refresh a following listing); False otherwise.
    """
    pdu = find_power_adapter(console_manager)
    if pdu is None or getattr(pdu, "enabled", True) is False:
        await send_line("ERROR:POWER: power management is not configured")
        return False
    parts = command.split()
    if len(parts) > 3:
        await send_line("ERROR:POWER: usage: POWER [<pdu>[.<outlet>] [on|off]]")
        return False
    arg = parts[1] if len(parts) >= 2 else None
    verb = parts[2].lower() if len(parts) >= 3 else None

    # Switch form: POWER <pdu>.<outlet> on|off
    if verb is not None:
        if verb not in ("on", "off"):
            await send_line("ERROR:POWER: target state must be 'on' or 'off'")
            return False
        if arg is None or "." not in arg:
            await send_line("ERROR:POWER: switching needs a full outlet ref '<pdu>.<outlet>'")
            return False
        if not await _user_can_write(auth_manager, username):
            await send_line("ERROR:POWER: insufficient permission (need read-write)")
            return False
        blocked = pdu._power_blocked_ports(arg, username)
        if blocked:
            await send_line(
                f"ERROR:POWER: {arg} feeds consoles outside your groups ({', '.join(blocked)}); switching it needs admin"
            )
            return False
        on = verb == "on"
        if not on:
            impact = pdu.compute_off_impact(arg)
            for losing in impact.get("losing_power") or []:
                desc = losing.get("description") or ""
                await send_line(
                    "WARNING:POWER: removing all power to: " + str(losing.get("port")) + (" (" + desc + ")" if desc else "")
                )
            for staying in impact.get("staying_up") or []:
                via = ", ".join(staying.get("via") or [])
                await send_line("NOTE:POWER: " + str(staying.get("port")) + " stays up via " + (via or "other feed"))
        try:
            result = await pdu.set_outlet(arg, on, user=username, client_id=client_id)
        except Exception as exc:
            await send_line("ERROR:POWER: " + str(exc))
            return False
        if not result.get("ok"):
            await send_line("ERROR:POWER: " + str(result.get("error", "switch failed")))
            return False
        reading = result.get("reading") or {}
        state_txt = "on" if reading.get("on") else "off"
        await send_line(f"POWER {arg} -> {state_txt}")
        return True

    # Listing / single-outlet form
    snap = pdu.get_power_snapshot()
    if arg is None:
        for p in snap.get("pdus") or []:
            online = "" if p.get("online") else " [OFFLINE]"
            await send_line(
                "PDU %s (%s)%s  %d/%d on"
                % (p.get("name"), p.get("driver"), online, p.get("outlets_on"), p.get("outlet_count"))
            )
            for o in p.get("outlets") or []:
                await send_line("  " + format_outlet_line(o))
        unresolved = snap.get("unresolved_refs") or []
        if unresolved:
            await send_line("NOTE:POWER: unresolved feeds: " + ", ".join(unresolved))
        return False

    # Arg is either a PDU name (list its outlets) or a full outlet ref (one line)
    pdus = {p.get("name"): p for p in snap.get("pdus") or []}
    if arg in pdus:
        p = pdus[arg]
        for o in p.get("outlets") or []:
            await send_line(format_outlet_line(o))
        return False
    if "." in arg:
        for p in snap.get("pdus") or []:
            for o in p.get("outlets") or []:
                if o.get("ref") == arg:
                    await send_line(format_outlet_line(o))
                    return False
        await send_line("ERROR:POWER: unknown outlet: " + arg)
        return False
    await send_line("ERROR:POWER: unknown PDU: " + arg)
    return False


async def _user_can_write(auth_manager: Any, username: Optional[str]) -> bool:
    """True when the identity may switch outlets (read-write or admin)."""
    try:
        if auth_manager is None or username is None:
            return False
        perm = auth_manager.get_user_permissions(username)
        return perm in ("read-write", "admin")
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Interactive power menu (telnet + SSH `p` command)
#
# Unlike the free-text forms above, the menu is scoped to the console the
# user is attached to: it lists ONLY that console's power feeds, numbered,
# one per line. A number toggles that feed; `a` toggles every feed; Enter
# exits (no change).
# ---------------------------------------------------------------------------

POWER_MENU_PROMPT = "> "


def _console_feeds(pdu: Any, port_name: str) -> List[str]:
    """Return the PDU outlet refs that feed this one console port (config order)."""
    try:
        return list(pdu.port_power_map(port_name) or [])
    except Exception:
        return []


def format_power_menu_line(ref: str, on: Optional[bool]) -> str:
    """Render one feed row: the state tag only (the number is in the prefix)."""
    if on is True:
        return "[on]   " + ref
    if on is False:
        return "[off]  " + ref
    return "[unknown] " + ref


async def _set_all_feeds_at(
    console_manager: Any,
    pdu: Any,
    refs: List[str],
    on: bool,
    username: Optional[str],
    auth_manager: Any,
    send_line: Callable[[str], Awaitable[None]],
    client_id: Optional[str] = None,
) -> bool:
    """Switch every feed that is not already in the target state.

    Goes through ``run_power_command`` per ref so the read-write and group
    permission checks apply exactly as for a single switch. Returns True when
    at least one feed actually changed.
    """
    changed = False
    for ref in refs:
        current = pdu._outlet_on_state(ref)
        if current is not None and bool(current) == on:
            continue
        ok = await run_power_command(
            console_manager,
            "POWER " + ref + (" on" if on else " off"),
            send_line,
            username,
            auth_manager,
            client_id,
        )
        if ok:
            changed = True
    return changed


async def _flush_pending_notices() -> None:
    """Yield to the loop so this session's live ``[POWER]`` notice is written
    before the menu re-renders. The notice is fanned out to the session's
    writer by the meta listener as a background task, so without this yield
    it lands after the next prompt line and visually displaces the prompt.
    """
    for _ in range(3):
        await asyncio.sleep(0)


async def run_power_menu(
    console_manager: Any,
    port_name: str,
    reader: Any,
    writer: Any,
    send_line: Callable[[str], Awaitable[None]],
    username: Optional[str],
    auth_manager: Any,
    client_id: Optional[str] = None,
) -> None:
    """Run the interactive, per-console power menu (telnet + SSH ``p`` command).

    Shows ONLY this console's power feeds, numbered, one per line, then loops
    on one prompt: a number toggles that feed (on->off or off->on); ``a``
    toggles every feed in the direction of the first feed; Enter exits with
    no change and prints ``[EXITING POWER]`` as the back-to-console marker.
    After each switch the live ``[POWER]`` notice is flushed to the session
    before the list re-renders, so the prompt never precedes the message.
    ``read_prompt_line`` (listener_common) renders the prompt and echoes typed
    input on the raw terminal.
    """
    pdu = find_power_adapter(console_manager)
    if pdu is None or getattr(pdu, "enabled", True) is False:
        await send_line("ERROR:POWER: power management is not configured")
        return
    refs = _console_feeds(pdu, port_name)
    if not refs:
        await send_line(f"POWER: {port_name} has no power feeds (Enter exits)")
        _, keep_going = await read_prompt_line(reader, writer, POWER_MENU_PROMPT)
        if keep_going:
            await send_line("[EXITING POWER]")
        return
    await send_line(f"POWER: feeds for {port_name}  (a number = toggle, a = all, Enter = exit)")
    while True:
        for i, ref in enumerate(refs, 1):
            await send_line(f"{i:>2}  " + format_power_menu_line(ref, pdu._outlet_on_state(ref)))
        text, keep_going = await read_prompt_line(reader, writer, POWER_MENU_PROMPT)
        if not keep_going:
            return  # client disconnected: the caller stops pumping the session
        entry = (text or "").strip().lower()
        if entry == "":
            await send_line("[EXITING POWER]")
            return
        if entry in ("a", "all"):
            first = pdu._outlet_on_state(refs[0])
            target = not (bool(first) if first is not None else False)
            await _set_all_feeds_at(console_manager, pdu, refs, target, username, auth_manager, send_line, client_id)
            await _flush_pending_notices()
            continue
        if entry.isdigit():
            idx = int(entry)
            if 1 <= idx <= len(refs):
                ref = refs[idx - 1]
                state = pdu._outlet_on_state(ref)
                target = not (bool(state) if state is not None else False)
                await run_power_command(
                    console_manager,
                    "POWER " + ref + (" on" if target else " off"),
                    send_line,
                    username,
                    auth_manager,
                    client_id,
                )
                await _flush_pending_notices()
            else:
                await send_line(f"POWER: number out of range (1-{len(refs)})")
            continue
        await send_line("POWER: enter a feed number, 'a' for all, or Enter to exit")
