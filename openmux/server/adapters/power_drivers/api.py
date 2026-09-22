"""The PDU power driver API.

Every PDU backend implements :class:`PduDriver` against one configured
PDU entry and reports readings as :class:`OutletReading` values. This
module imports nothing from the adapter so driver modules can build on
it without a circular import.

Read backoff contract: :meth:`PduDriver.read_states` may return ``None``
while the driver is inside a device-wide failure backoff (see
:mod:`.readbackoff`). The adapter keeps the last-known readings untouched
when it happens (``pdu.py::_refresh_readings``), so a driver in backoff
never performs new device IO and never spawns anything. Backoff applies
to reads only; discovery (``list_outlets``) and explicit switches
(``set_state``) always run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class OutletReading:
    """Single outlet reading from a PDU driver.

    ``on`` is None while unknown (PDU unreachable or not yet polled).
    Power figures are None when the driver does not report them or when the
    outlet is off (drivers may report zero; both are surfaced as-is).
    """

    on: Optional[bool] = None
    watts: Optional[float] = None
    amps: Optional[float] = None
    volts: Optional[float] = None
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe dict of this reading."""
        out: Dict[str, Any] = {"on": self.on, "watts": self.watts, "amps": self.amps, "volts": self.volts}
        if self.error:
            out["error"] = self.error
        return out


# A driver reports the outlet ids it knows for ONE PDU device.
OutletId = str


class PduDriver:
    """Interface every PDU backend implements.

    A driver instance is bound to exactly one configured PDU entry. Its
    constructor receives the driver's free-form ``options`` dict and
    raises ``ValueError`` on invalid config (the adapter then rejects the
    PDU entry with a log line). Outlet ids are opaque strings taken from
    the device itself (e.g. ``"1"`` or ``"A1"`` on a 3-phase unit); the
    adapter never assumes numbering.
    """

    async def list_outlets(self) -> List[str]:
        """Return the device's outlet ids (discovery is the driver's job)."""
        raise NotImplementedError

    async def read_states(self) -> Optional[Dict[str, OutletReading]]:
        """Return current readings keyed by outlet id.

        May return ``None`` while the driver is in device-wide read
        backoff (see :mod:`.readbackoff`); the adapter then keeps its
        last-known readings and performs no further action this cycle.
        """
        raise NotImplementedError

    async def set_state(self, outlet_id: str, on: bool) -> OutletReading:
        """Switch one outlet and return the resulting reading."""
        raise NotImplementedError
