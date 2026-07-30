"""Shared helpers for interactive listener adapters (telnet_listener, ssh_listener).

Pure, transport-agnostic logic used by both listener adapters: login-line
delimiter parsing, ACL compilation/matching, and port-list text rendering.
Keeping these here avoids duplicating identical behavior across adapters that
speak different wire protocols (raw TCP/Telnet vs SSH).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

AclEntry = Union[
    ipaddress.IPv4Address,
    ipaddress.IPv6Address,
    ipaddress.IPv4Network,
    ipaddress.IPv6Network,
]


def parse_login(line: str) -> Tuple[str, Optional[str]]:
    """Split a login line into (username, embedded_port_descriptor).

    Supports ``<username>+<port>`` and ``<username>:<port>``. A single ``:``
    is treated as the delimiter only when it is not part of a ``::`` run, so
    composed targets like ``alice:myserver::prod-serial0`` still resolve
    correctly (the ``::`` is left intact in the descriptor).
    """
    line = line.strip()
    plus_idx = line.find("+")
    if plus_idx != -1:
        return line[:plus_idx].strip(), line[plus_idx + 1 :].strip()

    idx = 0
    while True:
        colon_idx = line.find(":", idx)
        if colon_idx == -1:
            break
        if line[colon_idx : colon_idx + 2] == "::":
            idx = colon_idx + 2
            continue
        return line[:colon_idx].strip(), line[colon_idx + 1 :].strip()

    return line, None


def compile_acl(rules: List[str], on_invalid: Optional[Callable[[str], None]] = None) -> List[AclEntry]:
    """Compile a list of IP/CIDR strings into ipaddress objects.

    Invalid entries are skipped; `on_invalid(rule)` is called for each one so
    callers can log a warning with adapter-specific context.
    """
    compiled: List[AclEntry] = []
    for rule in rules:
        try:
            if "/" in rule:
                compiled.append(ipaddress.ip_network(rule, strict=False))
            else:
                compiled.append(ipaddress.ip_address(rule))
        except ValueError:
            if on_invalid:
                on_invalid(rule)
    return compiled


def ip_allowed(peer_ip: str, compiled_acl: List[AclEntry]) -> bool:
    """Return True if `peer_ip` matches the compiled ACL (empty ACL allows all)."""
    if not compiled_acl:
        return True
    try:
        addr = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    for rule in compiled_acl:
        if isinstance(rule, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            if addr == rule:
                return True
        else:
            if addr in rule:
                return True
    return False


def render_port_list(entries: List[Dict[str, Any]]) -> bytes:
    """Render a port-list entry sequence as CRLF-terminated plain text."""
    lines = [b"Available ports:\r\n"]
    for entry in entries or []:
        name = entry.get("name", "?")
        origin = entry.get("origin_server_id")
        status = entry.get("status", "")
        label = f"{origin}::{name}" if origin else name
        lines.append(f"  {label}  {status}\r\n".encode())
    return b"".join(lines)


@dataclass
class EscapeState:
    """Per-session state for detecting the in-band control-menu escape sequence.

    `escape_char1`/`escape_char2` are always exactly one byte each; the `e`
    control command lets a session change them at runtime (see
    `feed_escape_byte`'s state==2 handling in the calling adapter).
    """

    escape_char1: bytes = b"\x05"  # Ctrl+E
    escape_char2: bytes = b"c"
    state: int = 0


def feed_escape_byte(state: EscapeState, b: bytes) -> Tuple[bytes, Optional[str]]:
    """Feed one byte through the control-menu escape-sequence state machine.

    Mirrors the CLI client's (`openmux/client/console.py`) escape handling:
    a lone first byte that isn't followed by the second escape byte is
    replayed as ordinary data rather than dropped.

    Args:
        state: Session-scoped state, mutated across calls to carry a partial
            sequence across chunk/read boundaries.
        b: Single input byte to evaluate.

    Returns:
        Tuple[bytes, Optional[str]]: bytes that should be forwarded to the
        port right now (empty if `b` was absorbed into pending escape
        state), and a completed single-character command mnemonic, or None.
    """
    if state.state == 0:
        if b == state.escape_char1:
            state.state = 1
            return b"", None
        return b, None
    if state.state == 1:
        state.state = 0
        if b == state.escape_char2:
            state.state = 2
            return b"", None
        return state.escape_char1 + b, None
    # state.state == 2: this byte is the command mnemonic.
    state.state = 0
    return b"", b.decode("latin1", errors="ignore")


CONTROL_MENU_HELP = (
    "\r\n--- OpenMux Control Menu ---\r\n"
    "a  Request read-write access\r\n"
    "f  Force-take read-write access\r\n"
    "s  Release read-write access (switch to read-only)\r\n"
    "w  Show who holds read-write access\r\n"
    "i  Show session info\r\n"
    "v  Show version\r\n"
    "e  Change escape sequence\r\n"
    ".  Disconnect\r\n"
    "?  Show this menu\r\n"
)


def format_rw_notice(payload: Dict[str, Any]) -> str:
    """Render a read-write control payload as CRLF-terminated human text.

    Used by telnet/SSH sessions, which are raw terminals and cannot parse the
    JSON control frames used by the TCP/WebSocket client adapters. Wording is
    kept consistent with the CLI client (`console.py`) and `rw_control.py`.

    Args:
        payload: Control payload, e.g. `{"type": "client_mode", "ok": True,
            "mode": "read-write"}` or `{"type": "rw_holders", "holders": [...]}`.

    Returns:
        str: CRLF-terminated message ready to write directly to the session.
    """
    if payload.get("type") == "rw_holders":
        holders = payload.get("holders") or []
        if holders:
            return "\r\n[Read-write held by: " + ", ".join(holders) + "]\r\n"
        return "\r\n[No client currently holds read-write access]\r\n"

    ok = payload.get("ok")
    mode = payload.get("mode")
    if payload.get("reason") == "demoted":
        return "\r\n[Your read-write access was taken by another user]\r\n"
    if ok and mode == "read-write":
        return "\r\n[Read-write access granted]\r\n"
    if ok and mode == "read-only":
        return "\r\n[Switched to read-only mode]\r\n"
    if not ok:
        holders = payload.get("rw_holders") or []
        if holders:
            return "\r\n[Read-write request denied (held by: " + ", ".join(holders) + ") - use force-take if needed]\r\n"
        return "\r\n[Read-write is not available on this port]\r\n"
    return "\r\n[Access mode updated]\r\n"


__all__ = [
    "AclEntry",
    "parse_login",
    "compile_acl",
    "ip_allowed",
    "render_port_list",
    "EscapeState",
    "feed_escape_byte",
    "CONTROL_MENU_HELP",
    "format_rw_notice",
]
