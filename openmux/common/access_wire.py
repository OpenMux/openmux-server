"""Access-mode wire helpers shared by the server and the OpenMux client.

The server decides access and renders the wording (``openmux.server.access_decision``).
Every surface that shows an answer to a human prints the lines the server sent, and this
module holds the one renderer that turns those lines into terminal bytes.

It lives here, and not in ``openmux.server``, because the CLI client must format the same
frames without importing the server (issue #98).
"""

from typing import Optional, Sequence


def render_terminal_text(lines: Optional[Sequence[str]]) -> str:
    """Join rendered lines into terminal bytes: CRLF wrapped, empty when silent.

    One implementation for a session's own decision, a frame relayed from another
    surface, and the CLI client's reply handling, so a Telnet, SSH, raw TCP, or CLI
    Client sees the same bytes either way (issue #98).

    Args:
        lines: The lines the server rendered, or None for a silent frame.

    Returns:
        str: The lines joined with CRLF and wrapped in CRLF, or "" when there is no
        line to show.
    """
    text = [str(line) for line in lines or () if str(line)]
    if not text:
        return ""
    return "\r\n" + "\r\n".join(text) + "\r\n"


def holders_text(holders: Sequence[str], capacity: Optional[int] = None) -> str:
    """One sentence naming who holds a Port's Write slot, for the `w` command.

    A holders list is data and not a decision, so it carries no server-rendered line.
    Telnet, SSH, a raw TCP Client, and the CLI client say it this way (issue #98); the
    web console shows the same labels as a list.

    Args:
        holders: Holder labels, as the server sends them.
        capacity: The Port's wire capacity. 0 means the Port has no write slot at all.

    Returns:
        str: One sentence, without brackets.
    """
    if capacity == 0:
        return "Read-write is not available on this port (it has no write slot)"
    if holders:
        return "Read-write held by: " + ", ".join(str(h) for h in holders)
    return "No client currently holds read-write access"
