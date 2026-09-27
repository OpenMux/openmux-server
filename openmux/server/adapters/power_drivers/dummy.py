"""``driver: dummy`` - in-memory PDU for development and tests.

Outlets start ON. The outlet ids come from the per-PDU ``outlets`` list,
which the adapter merges into the driver's ``options`` as ``outlets``
before constructing; an entry may be a bare id string or a
``{id: ...}`` mapping (mapping entries need ``id``; the other keys, like
``description``, belong to the adapter and are ignored here). Other
``options`` keys:
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
                if isinstance(item, dict):
                    # Mapping entry from the per-PDU outlet list: it needs
                    # 'id' (the other keys, like description, are the
                    # adapter's).
                    raw_id = item.get("id")
                    if raw_id is None or not str(raw_id).strip():
                        raise ValueError(f"dummy driver: outlet mapping entry needs a non-empty 'id': {item!r}")
                    text = str(raw_id).strip()
                else:
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
    the driver takes none. The outlet list is NOT an option: it is the
    per-PDU ``outlets`` list, which the adapter merges into the driver's
    ``options`` before constructing (see :mod:`.api`).
    """
    return {
        "label": "Dummy",
        "description": "In-memory PDU for development and tests.",
        "options_keys": [
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
        "options_example": {},
    }
