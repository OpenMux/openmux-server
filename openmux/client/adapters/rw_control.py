"""Shared access-mode (read-only/read-write) control-frame helpers.

Both the TCP adapter and the WebSocket adapter exchange the same JSON control
Since issue #98 the server sends the wording of every access answer in the frame's
``lines``, so this module holds no reason-to-sentence table. It records the state a
frame reports and prints the lines it was given. A frame with no line prints nothing,
which is how an attach grant stays silent behind the connect banner.
"""

from typing import Any, Dict, Optional, Tuple

from openmux.common.access_wire import holders_text, render_terminal_text


def apply_client_mode_response(adapter: Any, payload: Dict[str, Any]) -> str:
    """Update adapter access-mode state from a `client_mode` reply and format its message.

    Args:
        adapter: Adapter instance exposing `access_mode`, `rw_holders`, `max_rw_users`.
        payload: Decoded JSON payload with at least a `mode` key, plus the `lines` and
            `code` the server rendered for this outcome.

    Returns:
        str: Human-readable status line(s) (including leading/trailing CRLF), or
        an empty string if nothing user-facing needs to be shown.
    """
    adapter.access_mode = "read-write" if payload.get("mode") == "read-write" else "read-only"
    if "rw_holders" in payload:
        adapter.rw_holders = payload.get("rw_holders") or []
    if "max_rw_users" in payload:
        adapter.max_rw_users = payload.get("max_rw_users")
    return render_terminal_text(payload.get("lines"))


def apply_rw_holders_response(adapter: Any, payload: Dict[str, Any]) -> str:
    """Update adapter holder state from an `rw_holders` reply and format its message.

    A holders list is data rather than a decision, so its sentence is the one shared with
    the Telnet, SSH, and raw TCP `w` command (`openmux.common.access_wire`).

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
    return render_terminal_text([f"[{holders_text(holders, adapter.max_rw_users)}]"])


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
