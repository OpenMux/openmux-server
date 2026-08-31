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
        # "taken_by" names the taker when the demotion was a write-slot
        # takeover (issue #59 Part 2) - local takes send it; a federation
        # relay of the same takeover does not, so fall back to the generic
        # "another user".
        by = payload.get("taken_by") or "another user"
        lines.append(f"[Your read-write access was taken by {by}]")
    elif ok is False:
        if reason == "invalid_target":
            # Targeted takeover (issue #61) where the named client_id is not
            # (or no longer) a read-write holder: no slot moved.
            lines.append("[Take refused: that user does not hold read-write access (check the id in the holders list)]")
        elif reason == "federation_denied":
            lines.append("[Take refused: the origin server did not grant the takeover]")
        elif reason == "no_holder":
            # "none"-capacity port, or a named target on a holder-less port.
            lines.append("[Take refused: this port has no read-write holder to take from]")
        elif payload.get("max_rw_users") == 0:
            # 0 = the port's write-slot capacity is "none" (issue #59): it has
            # no driver at all.
            lines.append("[Read-write is not available on this port (it has no write slots: capacity 'none')]")
        else:
            # Covers a plain request_rw denial and a takeover whose promote
            # hit capacity ("promote_failed"): either way the seat is full or
            # missing. Prefer the frame's holders, else the last holders the
            # adapter saw (the server only attaches them to request_rw
            # denials).
            holders = payload.get("rw_holders") or getattr(adapter, "rw_holders", None) or []
            who = f" (held by: {', '.join(holders)})" if holders else ""
            if reason == "promote_failed":
                lines.append(f"[Request denied: no free read-write seat{who}]")
            else:
                lines.append(f"[Read-write request denied{who} - use Take control if needed]")
    elif mode == "read-write":
        lines.append("[Read-write access granted]")
        # Targeted takeover success (issue #61): `takeover` is the demoted
        # holder's label ("[id] username@ip (rw)").
        if payload.get("takeover"):
            lines.append(f"[Taken from: {payload['takeover']}]")
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
