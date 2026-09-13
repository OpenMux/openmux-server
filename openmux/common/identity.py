"""Shared server identity resolution.

One place that knows how the server identifies itself. Every surface that
shows the server's name (web console, Basic-Auth headers, telnet/SSH menu
banners, the CLI client's LIST banner, the MuxCon federation advert, the
autogen TLS CN) uses the same ladder instead of a per-surface default:

1. ``server.description``  - the operator's free-form label
2. ``OpenMux {id}``        - derived from ``server.id``, ``server.name``,
                             or the system hostname, in that order

The derived form keeps "OpenMux" as a product prefix and appends the node
identity, so several OpenMux consoles are distinguishable in browser dialogs
and terminal banners without any configuration.
"""

from __future__ import annotations

from typing import Any, Optional


def get_server_id(server_cfg: Optional[Any]) -> Optional[str]:
    """Return the server's identity value from a ``server`` config section.

    Resolution order: ``server.id``, ``server.name``, ``server.server_id``.
    Returns None when none is set (callers fall back to the hostname).
    """
    if not isinstance(server_cfg, dict):
        return None
    for key in ("id", "name", "server_id"):
        val = server_cfg.get(key)
        if val is not None:
            text = str(val).strip()
            if text:
                return text
    return None


def get_server_label(server_cfg: Optional[Any], hostname: Optional[str] = None) -> str:
    """Return the human-readable server label for display surfaces.

    Args:
        server_cfg: The ``server`` section of server.yaml (may be None).
        hostname: System hostname, used only when the server section carries
            no id of its own. Resolved via ``socket.gethostname()`` when
            omitted.

    Returns:
        ``server.description`` when set, else ``"OpenMux {id}"`` where id is
        ``server.id|name|server_id|hostname``. Never empty.
    """
    desc = None
    if isinstance(server_cfg, dict):
        raw = server_cfg.get("description")
        if raw is not None:
            desc = str(raw).strip()
    if desc:
        return desc
    sid = get_server_id(server_cfg)
    if not sid:
        if hostname is None:
            import socket

            try:
                hostname = socket.gethostname()
            except Exception:
                hostname = ""
        sid = str(hostname).strip() or "server"
    return f"OpenMux {sid}"


def _sanitize_label(text: str) -> str:
    """Make a label safe for a ``WWW-Authenticate: Basic realm="..."`` header.

    Strips NUL bytes and quotes the result, escaping backslashes and double
    quotes per HTTP semantics.
    """
    safe = (text or "").replace("\x00", "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{safe}"'


def basic_authenticate_header(realm: str) -> str:
    """Build a ``WWW-Authenticate: Basic`` header value for a realm string."""
    return f"Basic realm={_sanitize_label(realm)}"
