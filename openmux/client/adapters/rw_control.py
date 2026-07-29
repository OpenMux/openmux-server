"""Shared access-mode (read-only/read-write) control-frame helpers.

Both the TCP adapter and the WebSocket adapter exchange the same JSON control
payloads with the server (``request_rw``, ``release_rw``, ``force_promote``,
``query_rw_holders`` and their ``client_mode``/``rw_holders`` responses); only
the wire framing differs (NUL-prefixed line vs. a WebSocket text frame). This
module centralizes response formatting so the CLI console shows identical
messages regardless of which adapter is in use, matching the web console.
"""

from typing import Any, Dict, Optional, Tuple


def apply_client_mode_response(adapter: Any, payload: Dict[str, Any]) -> str:
    """Update adapter access-mode state from a `client_mode` response and format a message.

    Args:
        adapter: Adapter instance exposing `access_mode`, `rw_holders`, `max_rw_users`.
        payload: Decoded JSON payload with at least a `mode` key.

    Returns:
        str: Human-readable status line(s) (including leading/trailing CRLF), or
        an empty string if nothing user-facing needs to be shown.
    """
    mode = "read-write" if payload.get("mode") == "read-write" else "read-only"
    adapter.access_mode = mode
    if "rw_holders" in payload:
        adapter.rw_holders = payload.get("rw_holders") or []
    if "max_rw_users" in payload:
        adapter.max_rw_users = payload.get("max_rw_users")

    ok = payload.get("ok", True)
    reason = payload.get("reason")
    lines = []
    if reason == "demoted":
        lines.append("[Your read-write access was taken by another user]")
    elif ok is False:
        if payload.get("max_rw_users") == 0:
            lines.append("[Read-write is not available on this port (configured with 0 read-write users)]")
        else:
            holders = payload.get("rw_holders") or []
            who = f" (held by: {', '.join(holders)})" if holders else ""
            lines.append(f"[Read-write request denied{who} - use force-take if needed]")
    elif mode == "read-write":
        lines.append("[Read-write access granted]")
    else:
        lines.append("[Switched to read-only mode]")

    if not lines:
        return ""
    return "\r\n" + "\r\n".join(lines) + "\r\n"


def apply_rw_holders_response(adapter: Any, payload: Dict[str, Any]) -> str:
    """Update adapter holder state from an `rw_holders` response and format a message.

    Args:
        adapter: Adapter instance exposing `rw_holders`, `max_rw_users`.
        payload: Decoded JSON payload with `holders` and optional `max_rw_users`.

    Returns:
        str: Human-readable status line (including leading/trailing CRLF).
    """
    holders = payload.get("holders") or []
    adapter.rw_holders = holders
    if "max_rw_users" in payload:
        adapter.max_rw_users = payload.get("max_rw_users")

    if adapter.max_rw_users == 0:
        text = "Read-write is disabled on this port"
    elif holders:
        text = "Held by: " + ", ".join(holders)
    else:
        text = "No current read-write holder"
    return f"\r\n[{text}]\r\n"


def format_control_response(adapter: Any, payload: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """Dispatch a decoded control-frame payload to the matching handler.

    Args:
        adapter: Adapter instance to update in-place.
        payload: Decoded JSON control payload (must be a dict).

    Returns:
        Tuple[Optional[str], str]: (payload type or None if unrecognized, message text).
    """
    msg_type = payload.get("type")
    if msg_type == "client_mode":
        return msg_type, apply_client_mode_response(adapter, payload)
    if msg_type == "rw_holders":
        return msg_type, apply_rw_holders_response(adapter, payload)
    return msg_type, ""
