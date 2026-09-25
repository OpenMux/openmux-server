"""``driver: dummy`` - in-memory PDU for development and tests.

Outlets start ON. ``options`` keys:
    outlets (list[str|int]): outlet ids (default 1..8).
    watts_on (float): simulated watts while on (default 120).
    volts (float): simulated volts while on (default 230).
    fail_discovery (bool): make list_outlets raise (tests).
    fail_reads (bool): make read_states raise (tests).

Tests reach ``_state`` directly to simulate failures. This driver is
in-memory, so it needs no read backoff: a raising read marks the PDU
offline through the adapter's normal error path.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .api import OutletReading, PduDriver

# Default dummy PDU outlet ids.
DUMMY_DEFAULT_OUTLETS = [str(i) for i in range(1, 9)]


class DummyDriver(PduDriver):
    """In-memory PDU for development and tests.

    Outlets start ON. Tests reach ``_state`` directly to simulate
    failures.
    """

    def __init__(self, options: Optional[Dict[str, Any]] = None):
        opts = options or {}
        raw = opts.get("outlets")
        if isinstance(raw, (list, tuple)) and raw:
            ids: List[str] = []
            for item in raw:
                text = str(item).strip()
                if text and text not in ids:
                    ids.append(text)
            if not ids:
                raise ValueError("dummy driver: options.outlets has no valid ids")
        else:
            ids = list(DUMMY_DEFAULT_OUTLETS)
        self._ids = ids
        self._watts_on = float(opts.get("watts_on", 120.0))
        self._volts = float(opts.get("volts", 230.0))
        self._fail_discovery = bool(opts.get("fail_discovery"))
        self._fail_reads = bool(opts.get("fail_reads"))
        self._state: Dict[str, bool] = {oid: True for oid in ids}
        self.online = True

    async def list_outlets(self) -> List[str]:
        if self._fail_discovery:
            raise RuntimeError("dummy discovery failure")
        return list(self._ids)

    def _reading(self, oid: str) -> OutletReading:
        on = self._state.get(oid)
        if on is None:
            return OutletReading(on=None)
        return OutletReading(on=on, watts=self._watts_on, volts=self._volts, amps=self._watts_on / self._volts)

    async def read_states(self) -> Optional[Dict[str, OutletReading]]:
        if self._fail_reads:
            raise RuntimeError("dummy read failure")
        return {oid: self._reading(oid) for oid in self._ids}

    async def set_state(self, outlet_id: str, on: bool) -> OutletReading:
        if outlet_id not in self._state:
            raise ValueError(f"unknown outlet {outlet_id!r}")
        self._state[outlet_id] = bool(on)
        return self._reading(outlet_id)


def info(opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the config metadata UI for the dummy driver.

    Each driver module declares, in one place, the ``options`` it
    accepts (key, type, default, and a one-line help) and a JSON
    example. The Config Editor reads this to render the driver select
    and driver-specific options help; ``options_keys`` may be None when
    the driver takes none.
    """
    if opts is None:
        opts = {}
    outlets = opts.get("outlets")
    if outlets is not None:
        example = {"outlets": outlets}
    else:
        example = {"outlets": ["1", "2", "3"]}
    return {
        "label": "Dummy",
        "description": "In-memory PDU for development and tests.",
        "options_keys": [
            {
                "key": "outlets",
                "type": "list of strings",
                "default": "1..8",
                "help": "Outlet ids the PDU reports. Default: 1..8.",
            },
            {
                "key": "watts_on",
                "type": "number",
                "default": "120",
                "help": "Simulated watts while an outlet is on.",
            },
            {
                "key": "volts",
                "type": "number",
                "default": "230",
                "help": "Simulated volts while an outlet is on.",
            },
        ],
        "options_example": example,
    }
