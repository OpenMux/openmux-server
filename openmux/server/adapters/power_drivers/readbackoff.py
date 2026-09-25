"""Shared read-failure backoff for PDU power drivers.

Every power driver (command today; SNMP, HTTP, ...) wraps its
device-wide read path with this helper so a dead or slow device is not
polled at full cadence forever. The envelope is fixed by the helper, not
per driver: the FIRST failed probe suppresses further reads for
``BACKOFF_BASE_SECONDS``; each further failed probe (one per lapsed
window, because the driver performs no device IO inside the window)
starts a window double the previous one, capped at
``BACKOFF_CAP_SECONDS``.

The contract (enforced by the adapter's ``None`` branch in
``pdu.py::_refresh_readings`` for any driver that follows it):

1. While :meth:`ReadBackoff.in_backoff` is true the driver performs NO
   new device IO and ``read_states()`` returns ``None``.
2. A device-wide read failure (connection refused, timeout, whole-device
   error reply) calls :meth:`ReadBackoff.note_failure` and ``read_states()``
   returns ``None``.
3. A device-wide successful read calls :meth:`ReadBackoff.note_success`
   and ``read_states()`` returns the real readings (per-outlet problems
   ride as ``error`` on the individual readings).

Per-outlet failures do NOT start or extend the backoff window: they are
recorded as errors on the affected readings and the device still counts
as reachable. Backoff never applies to ``list_outlets`` or ``set_state``:
discovery runs once at startup, and an explicit user switch always runs
exactly one bounded attempt.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

# First failure suppresses reads for this long (seconds).
BACKOFF_BASE_SECONDS = 30.0
# Backoff window never grows beyond this (seconds).
BACKOFF_CAP_SECONDS = 300.0


class ReadBackoff:
    """Device-wide read-failure backoff window for one PDU driver.

    One instance per driver instance. The clock is injectable
    (``time.monotonic`` by default) so tests advance it without sleeping.
    """

    def __init__(self, name: str, now: Optional[Callable[[], float]] = None):
        self.name = name
        self._now = now or time.monotonic
        self._streak = 0
        self._window_until: Optional[float] = None

    def in_backoff(self) -> bool:
        """Return True while the suppression window has not elapsed."""
        return self._window_until is not None and self._now() < self._window_until

    def note_failure(self) -> float:
        """Record a device-wide read failure; return this window's length.

        The failure streak grows by one and the new window is
        ``BACKOFF_BASE_SECONDS * 2 ** (streak - 1)``, capped at
        ``BACKOFF_CAP_SECONDS``, starting from now. A failed probe only
        occurs while outside a window (reads inside one are suppressed),
        so this is what doubles the window between probes.
        """
        self._streak += 1
        window = min(BACKOFF_BASE_SECONDS * (2 ** (self._streak - 1)), BACKOFF_CAP_SECONDS)
        self._window_until = self._now() + window
        return window

    def note_success(self) -> None:
        """Record a successful device-wide read; reset the failure streak.

        Idempotent: successes outside a backoff state are no-ops.
        """
        self._streak = 0
        self._window_until = None
