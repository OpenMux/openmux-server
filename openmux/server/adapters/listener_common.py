"""Pure leaf helpers below the listener session seam (issue #100, ADR-0002).

Transport-agnostic logic that both listener transports still need during their
auth/menu phase: login-line delimiter parsing, ACL compilation/matching, entry
validation, port-list text rendering, and the echoing prompt primitives
(``read_prompt_line``, ``read_take_target``).

``read_prompt_line`` must stay here, not in ``listener_session``: the power menu
(``power_command.run_power_menu``) imports it, and importing it from the session
module would close an import cycle. Everything with session state moved to
``openmux/server/listener_session.py``.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

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


def validate_listener_entries(entries: List[Any], seen_names: Set[str]) -> bool:
    """Validate one SSH/Telnet listener entry list (shared by both adapters).

    Both adapters' configs are shaped identically, so they share this single
    validator (the only difference is the top-level section key, whose list the
    caller passes as ``entries``). Returns True only when every entry passes;
    ``seen_names`` is mutated to reject duplicate port names.
    """
    for entry in entries:
        if not _valid_listener_entry(entry, seen_names):
            return False
    return True


def _valid_listener_entry(entry: Any, seen_names: Set[str]) -> bool:
    """True when one SSH/Telnet listener dict entry is well-formed."""
    if not isinstance(entry, dict):
        return False
    if not _valid_listener_str_field(entry, "name"):
        return False
    if entry["name"] in seen_names:
        return False
    seen_names.add(entry["name"])
    if not _valid_listener_str_field(entry, "target"):
        return False
    if not _valid_listener_port(entry.get("bind_port")):
        return False
    if not _valid_listener_bind_host(entry.get("bind_host")):
        return False
    if not _valid_listener_bool_flags(entry):
        return False
    if not _valid_listener_acl_optional(entry):
        return False
    return True


def _valid_listener_str_field(entry: Dict[str, Any], key: str) -> bool:
    """True when ``entry[key]`` is a non-empty string (name/target)."""
    value = entry.get(key)
    return isinstance(value, str) and bool(value.strip())


def _valid_listener_bind_host(bind_host: Any) -> bool:
    """True when ``bind_host`` is absent/None or a non-empty string."""
    if bind_host is None:
        return True
    return isinstance(bind_host, str) and bool(bind_host.strip())


def _valid_listener_port(bind_port: Any) -> bool:
    """True when ``bind_port`` coerces to an int in 1-65535."""
    try:
        port = int(bind_port)
    except (TypeError, ValueError):
        return False
    return 1 <= port <= 65535


def _valid_listener_bool_flags(entry: Dict[str, Any]) -> bool:
    """True when read_only/enabled/require_auth, if present, are all bools."""
    for bool_key in ("read_only", "enabled", "require_auth"):
        if bool_key in entry and not isinstance(entry[bool_key], bool):
            return False
    return True


def _valid_listener_acl_optional(entry: Dict[str, Any]) -> bool:
    """True when ``acl`` is absent or a list of valid IP / CIDR strings."""
    if "acl" not in entry:
        return True
    acl = entry["acl"]
    if acl is None:
        return True
    if not isinstance(acl, list):
        return False
    for rule in acl:
        if not isinstance(rule, str) or not rule.strip():
            return False
        try:
            if "/" in rule:
                ipaddress.ip_network(rule, strict=False)
            else:
                ipaddress.ip_address(rule)
        except ValueError:
            return False
    return True


def render_port_list(entries: List[Dict[str, Any]], header: Optional[str] = None) -> bytes:
    """Render a port-list entry sequence as CRLF-terminated plain text.

    When ``header`` is a non-empty server label, the leading line becomes
    ``Available ports on <header>:`` so a user at an interactive menu can
    tell which OpenMux node they are talking to. Otherwise it stays
    ``Available ports:``.
    """
    lines = []
    if header:
        lines.append(f"Available ports on {header}:\r\n".encode())
    else:
        lines.append(b"Available ports:\r\n")
    for entry in entries or []:
        name = entry.get("name", "?")
        origin = entry.get("origin_server_id")
        status = entry.get("status", "")
        label = f"{origin}::{name}" if origin else name
        lines.append(f"  {label}  {status}\r\n".encode())
    return b"".join(lines)


def resolve_server_label(main_port_manager: Any) -> str:
    """Resolve the human server label for listener menu banners.

    Reads the ``server`` section from the running config reachable through the
    port manager's injected config manager and applies the shared identity
    ladder. Returns an empty string when it cannot be resolved, which leaves
    the port-list header unchanged (no "on " suffix).
    """
    try:
        from openmux.common.identity import get_server_label

        cfg_mgr = getattr(main_port_manager, "config_manager", None)
        cfg = getattr(cfg_mgr, "config", None)
        section = cfg.get("server") if isinstance(cfg, dict) else None
        if not isinstance(section, dict):
            return ""
        label = get_server_label(section)
        return label if label else ""
    except Exception:
        return ""


#: Prompt shown by the `f` control-menu command before a targeted takeover
#: (issue #61). The `w` command's `[id] username@ip (rw)` labels carry the
#: client_id to type here; Enter takes the most recently attached holder.
TAKE_TARGET_PROMPT = "[Take from holder (client_id, or Enter for latest): ]"


async def _echo_bytes(writer: Any, data: bytes) -> None:
    """Echo bytes to the session transport, absorbing a mid-prompt disconnect.

    A failed write/drain (client left while typing) is swallowed on purpose:
    the read loop sees the EOF and stops the prompt anyway.
    """
    writer.write(data)
    try:
        await writer.drain()
    except Exception:
        # justification: best-effort prompt echo; the client may have left mid-prompt
        pass


async def read_prompt_line(reader: Any, writer: Any, prompt: str) -> Tuple[Optional[str], bool]:
    """Prompt for one input line, echoing each typed byte as it arrives.

    Shared by the telnet and SSH interactive prompts (write-slot takeover
    target, the `p` power menu): both feed stdin through an
    `asyncio.StreamReader`-style `read(n)`, so the loop is transport-agnostic.
    CR or LF terminates the line. A CR also swallows the LF a Telnet client
    sends with it (see `_drop_line_feed_tail`), so the next prompt does not read
    that LF as an empty answer.

    Args:
        reader: Object with `async read(n)` returning bytes (``b""`` at EOF).
        writer: Object with `write(bytes)`; the prompt and byte echoes go
            straight to the session transport (NOT through the port
            forwarding path, which would leak them to the port).

    Returns:
        Tuple[Optional[str], bool]: (line or None, keep_going). The line is
        ``''`` when the client pressed Enter at an empty prompt. `keep_going`
        is False only when the client disconnected while answering; the caller
        should stop pumping.
    """
    writer.write(prompt.encode())
    try:
        await writer.drain()
    except Exception:
        # justification: best-effort prompt echo; the client may have left mid-prompt
        pass
    line = ""
    while True:
        try:
            data = await reader.read(1)
        except Exception:
            return line, True
        if not data:
            return line, False  # client left mid-prompt
        ch = data[:1]
        if ch in (b"\r", b"\n"):
            if ch == b"\r":
                _drop_line_feed_tail(reader)
            await _echo_bytes(writer, b"\r\n")
            return line, True
        if ch in (b"\x7f", b"\x08"):  # backspace: erase the echoed byte
            await _echo_bytes(writer, b"\b \b")
            if line:
                line = line[:-1]
            continue
        await _echo_bytes(writer, ch)
        line += ch.decode("latin1", errors="ignore")


def _drop_line_feed_tail(reader: Any) -> None:
    """Tell the transport the line just read ended at a CR, so its paired LF is not read as the next line.

    A Telnet client sends CR LF. This reader stops at the CR, so without this the LF becomes the first
    byte the NEXT prompt reads and looks like an empty answer: the `p` power menu exits by itself and the
    keystroke the operator typed next lands on the Port. A transport that cannot tell (SSH delivers LF
    line ends) has no `drop_line_feed` and is left alone, and a client that sends CR alone is unaffected
    because a next byte that is not an LF is handed back as data.
    """
    drop = getattr(reader, "drop_line_feed", None)
    if callable(drop):
        drop()


async def read_take_target(reader: Any, writer: Any) -> Tuple[Optional[str], bool]:
    """Prompt for the client_id a write-slot takeover should target (issue #61).

    Shared by the telnet and SSH listeners' `f` command; wraps
    `read_prompt_line` and normalizes an empty answer to None (the no-target
    fallback: take the most recently attached holder).
    """
    raw, keep_going = await read_prompt_line(reader, writer, TAKE_TARGET_PROMPT)
    if not keep_going:
        return None, False
    return raw or None, True


def _origin_entry_matches(entry: Dict[str, Any], port_name: str, server_id: Optional[str]) -> bool:
    """Whether a port-list entry matches ``port_name`` + the origin filter.

    Shared by the telnet and SSH listeners' ``_resolve_remote_by_origin``.
    """
    if entry.get("name") != port_name:
        return False
    origin = entry.get("origin_server_id")
    if server_id is None:
        return bool(origin)
    if server_id.lower() in {"local", "localhost"}:
        return not origin
    return origin == server_id


__all__ = [
    "AclEntry",
    "parse_login",
    "compile_acl",
    "ip_allowed",
    "validate_listener_entries",
    "render_port_list",
    "resolve_server_label",
    "TAKE_TARGET_PROMPT",
    "read_prompt_line",
    "read_take_target",
]
