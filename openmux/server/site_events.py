"""Site-wide event fan-out for the web console (``/ws/events``).

Some UI state belongs to the whole page instead of one port or one PDU: the
sidebar console list, for example, annotates every item with the last Port
Actions outcome on that port. Instead of one WebSocket per feature, producers
publish small JSON frames through `SiteEventHub.publish()` and every connected
browser tab receives them.

Frames are nudges, not data: the page re-fetches its REST endpoint when the
socket (re)opens, so a dropped frame can never strand the UI (see
docs/design/port_actions.md, "Sidebar status annotations"). Producers:
the Port Actions runner (``action_status``) and the coalesced live nudges
``status_change`` (status page, issue #85), ``power_changed`` (Power pages),
``ports_changed`` (sidebar console list), ``pdus_changed`` (Power sidebar
menu) and ``reload_applied`` (console page header, power badge, and Actions
button) -- the first three from `WebConsoleAdapter._kick_site_nudge()`,
``pdus_changed`` and ``reload_applied`` from the adapter's ``notify_*`` hooks
after a soft reload (main.py).
"""

import asyncio
import json
import logging
from typing import Any, Dict, Set

logger = logging.getLogger("openmux.server.site_events")


def _consume_send_result(task: "asyncio.Task[None]") -> None:
    """Retrieve a fire-and-forget send's exception so closed peers stay quiet."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        # justification: the client is gone; its handler's iterate-loop prunes it
        pass


class SiteEventHub:
    """Set of ``/ws/events`` WebSockets with best-effort JSON fan-out.

    `publish()` is safe to call from synchronous code running on the event
    loop (the Port Actions runner does): each send is scheduled as a task and
    failures are consumed by `_consume_send_result()`, so a dead peer never
    logs a "Task exception was never retrieved" warning.
    """

    def __init__(self) -> None:
        self._clients: Set[Any] = set()

    def add(self, ws: Any) -> None:
        """Track one connected site WebSocket (called by the ``/ws/events`` handler)."""
        self._clients.add(ws)

    def discard(self, ws: Any) -> None:
        """Stop tracking a WebSocket (called from the handler's cleanup)."""
        self._clients.discard(ws)

    def client_count(self) -> int:
        """Number of currently tracked sockets (for tests and diagnostics)."""
        return len(self._clients)

    def publish(self, event: Dict[str, Any]) -> None:
        """Send one JSON frame to every connected site socket. Never raises.

        Without a running event loop (unit tests, the CLI runner) nothing can
        be connected, so the frame is dropped silently.
        """
        if not self._clients:
            return
        try:
            text = json.dumps(event, separators=(",", ":"))
        except (TypeError, ValueError):
            # justification: one producer building a bad frame must not break the run
            logger.exception("Site event frame is not JSON-serializable: %r", event)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for ws in list(self._clients):
            try:
                task = loop.create_task(ws.send_str(text))
            except Exception:
                # justification: transport already closed; drop this client now
                self._clients.discard(ws)
                continue
            task.add_done_callback(_consume_send_result)
