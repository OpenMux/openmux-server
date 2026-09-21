"""PDU power management adapter.

Manages power distribution units (PDUs) and their outlets: live on/off
state, optional watts/volts/amps readings, and on/off control. Outlets are
NOT OpenMux console ports; this adapter provides no ``adapter.ports`` and
never calls ``register_unified_port``. It keeps its own outlet state and
exposes a snapshot API plus a state listener for the web plugin and the
client-listener text notices.

Configuration (top-level ``power`` section, object style):
    power:
      enabled: true
      pdus:
        - name: rack1            # user-chosen, no dots/whitespace
          description: "Rack 1 PDU"
          driver: dummy           # driver registry key
          poll_interval: 10      # per-PDU seconds; 0 = poll on demand only
          options: {}            # free-form, driver-specific
          outlets:               # optional annotations by device outlet id
            - id: "3"
              description: "Switch A"

Console-port linkage is declared on the PORT side (``power: [rack1.3, ...]``
on a port entry); this adapter only reads those live from the port
registry, so it holds no cross-adapter cached state.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from ..data_logger import DataLogger
from .base_adapter import AdapterCapability, BaseGenericAdapter

# Default poll cadence (seconds) when a PDU entry omits `poll_interval`.
DEFAULT_POLL_INTERVAL = 10.0
# Default dummy PDU outlet ids.
DUMMY_DEFAULT_OUTLETS = [str(i) for i in range(1, 9)]

# A driver reports the outlet ids it knows for ONE PDU device.
OutletId = str


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


class PduDriver:
    """Interface every PDU backend implements.

    A driver instance is bound to exactly one configured PDU entry. Outlet
    ids are opaque strings taken from the device itself (e.g. ``"1"`` or
    ``"A1"`` on a 3-phase unit); the adapter never assumes numbering.
    """

    async def list_outlets(self) -> List[str]:
        """Return the device's outlet ids (discovery is the driver's job)."""
        raise NotImplementedError

    async def read_states(self) -> Dict[str, OutletReading]:
        """Return current readings keyed by outlet id."""
        raise NotImplementedError

    async def set_state(self, outlet_id: str, on: bool) -> OutletReading:
        """Switch one outlet and return the resulting reading."""
        raise NotImplementedError


class DummyDriver(PduDriver):
    """In-memory PDU for development and tests.

    ``options`` keys:
        outlets (list[str|int]): outlet ids (default 1..8).
        watts_on (float): simulated watts while on (default 120).
        volts (float): simulated volts while on (default 230).
        fail_discovery (bool): make list_outlets raise (tests).
        fail_reads (bool): make read_states raise (tests).

    Outlets start ON. Tests reach ``_state`` directly to simulate failures.
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

    async def read_states(self) -> Dict[str, OutletReading]:
        if self._fail_reads:
            raise RuntimeError("dummy read failure")
        return {oid: self._reading(oid) for oid in self._ids}

    async def set_state(self, outlet_id: str, on: bool) -> OutletReading:
        if outlet_id not in self._state:
            raise ValueError(f"unknown outlet {outlet_id!r}")
        self._state[outlet_id] = bool(on)
        return self._reading(outlet_id)


# Driver registry: name -> class(options) factory. Real drivers (Raritan,
# APC, ...) plug in here without touching the adapter or the UI.
DRIVERS: Dict[str, Callable[[Dict[str, Any]], PduDriver]] = {
    "dummy": lambda opts: DummyDriver(opts),
}


def _driver_info(opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the config metadata UI for one PDU driver.

    Each driver declares, in one place, the ``options`` it accepts (key,
    type, default, and a one-line help) and a JSON example. The Config
    Editor reads this to render a driver select and driver-specific
    options help; ``pdu.py`` is the single place to edit when a new driver
    is added. ``options_keys`` may be None when the driver takes none.
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


# Per-driver config metadata for the Config Editor. The keys MUST match
# DRIVERS. Add a new entry here (and to DRIVERS) for every driver so the
# editor's driver select and options help stay current.
DRIVER_INFO: Dict[str, Callable[[Optional[Dict[str, Any]]], Dict[str, Any]]] = {
    "dummy": _driver_info,
}


def driver_catalog() -> List[Dict[str, Any]]:
    """Return a JSON-safe per-driver metadata list for the Config Editor.

    One entry per key in ``DRIVERS`` (so the select always matches the
    registry). Each entry carries the driver key, a human label, a short
    description, the ``options`` keys it accepts, and a JSON example. The
    ``options`` example honors the per-PDU ``outlets`` override when given,
    but the catalog is driver-level, so it uses the driver default.
    """
    out: List[Dict[str, Any]] = []
    for name in DRIVERS:
        info_fn = DRIVER_INFO.get(name)
        base: Dict[str, Any] = {
            "driver": name,
            "label": name,
            "description": "",
            "options_keys": None,
            "options_example": None,
        }
        if info_fn is not None:
            try:
                meta = info_fn()
            except Exception:
                # justification: a buggy info function must not break driver
                # discovery; fall back to the bare driver key.
                meta = {}
            base.update(meta)
        out.append(base)
    return out


class PduState:
    """Runtime state for one configured PDU."""

    def __init__(self, name: str, cfg: Dict[str, Any], driver: PduDriver):
        self.name = name
        self.description: str = str(cfg.get("description") or "")
        self.driver_name: str = str(cfg.get("driver"))
        self.poll_interval: float = _as_poll_interval(cfg.get("poll_interval"))
        self.options: Dict[str, Any] = dict(cfg.get("options") or {})
        self.annotations: Dict[str, str] = _parse_annotations(cfg.get("outlets"))
        self.driver = driver
        self.online: Optional[bool] = None
        self.readings: Dict[str, OutletReading] = {}
        self.task: Optional[asyncio.Task] = None

    def material(self) -> Dict[str, Any]:
        """Fields whose change forces a PDU re-create on soft reload."""
        return {"driver": self.driver_name, "poll_interval": self.poll_interval, "options": self.options}


def _as_poll_interval(value: Any) -> float:
    """Normalize a poll_interval setting to a non-negative float."""
    if value is None:
        return DEFAULT_POLL_INTERVAL
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"poll_interval must be a number, got {value!r}")
    interval = float(value)
    if interval < 0:
        raise ValueError(f"poll_interval must be >= 0, got {interval}")
    return interval


def _parse_annotations(value: Any) -> Dict[str, str]:
    """Parse the optional per-outlet description annotations.

    Returns a mapping of outlet id -> description. Raises ValueError on a
    malformed entry so validate_config can reject the config early.
    """
    out: Dict[str, str] = {}
    if value is None:
        return out
    if not isinstance(value, list):
        raise ValueError("pdu 'outlets' must be a list of {id, description} entries")
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("pdu outlet annotations must be mappings")
        oid = entry.get("id")
        if oid is None or not str(oid).strip():
            raise ValueError("pdu outlet annotation requires a non-empty 'id'")
        oid = str(oid).strip()
        if "." in oid or any(ch.isspace() for ch in oid):
            raise ValueError(f"pdu outlet id {oid!r} must not contain dots or whitespace")
        desc = entry.get("description")
        out[oid] = str(desc) if desc is not None else ""
    return out


def _valid_ref_token(text: str) -> bool:
    return bool(text) and "." not in text and not any(ch.isspace() for ch in text)


# Separator between the origin server id and the local outlet ref in a remote
# (federated-node-owned) outlet ref: "<origin_server_id>::<pdu_name>.<outlet_id>".
# The same double-colon convention the federation layer already uses for
# origin-qualified names (e.g. remote port display strings) keeps one mental
# model. The local part "<pdu>.<id>" always has exactly one dot (the pdu name /
# outlet id tokens may not contain dots) and can never contain "::", so the
# FIRST "::" is always the separator - the origin may itself be a dotted FQDN
# (server_ids often are), and the pdu/id split is taken from the local part.
REMOTE_REF_SEPARATOR = "::"


def split_remote_ref(ref: Any) -> Optional[Tuple[str, str]]:
    """Split a remote outlet ref into (origin_server_id, local_outlet_ref).

    Returns None when the ref is not a remote ref (no well-formed "origin::"-
    prefixed part whose local half is exactly "<pdu>.<id>"). A well-formed
    remote ref carries the separator "::" EXACTLY once (neither an origin
    server_id nor a "<pdu>.<id>" local ref ever contains "::"), which fixes the
    split point. The origin is validated only as a non-empty, whitespace-free
    token so dotted FQDN server_ids round-trip; the local half must be a
    well-formed local ref (exactly one dot, dot-free tokens). Fed consoles
    carry their feeds globally unique this way, so an origin and a peer can
    both have an outlet named "rack1.1" without the names colliding (outlet
    federation).
    """
    if not isinstance(ref, str):
        return None
    if ref.count(REMOTE_REF_SEPARATOR) != 1:
        return None
    origin, local = ref.split(REMOTE_REF_SEPARATOR, 1)
    if not origin or any(ch.isspace() for ch in origin):
        return None
    if local.count(".") != 1:
        return None
    pdu_name, outlet_id = local.split(".", 1)
    if not _valid_ref_token(pdu_name) or not _valid_ref_token(outlet_id):
        return None
    return origin, local


def remote_ref(origin: Any, ref: str) -> str:
    """Qualify a local outlet ref with the origin server id (outlet federation)."""
    return f"{origin}{REMOTE_REF_SEPARATOR}{ref}"


class PduAdapter(BaseGenericAdapter):  # noqa: Vulture
    """Adapter managing PDU outlets (power on/off + readings).

    Portless: provides no console ports and runs a per-PDU poll task. The
    console-side ``power: [outlet refs]`` mapping is read live from the
    global port registry on every snapshot, so soft reload of port sections
    needs nothing here.
    """

    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        section = self._effective_section(config)
        self.enabled: bool = bool(section.get("enabled", True))
        self.pdus: Dict[str, PduState] = {}
        self.console_manager = None
        self.auth_manager = None
        self._state_listeners: Set[Callable[[str, Optional[bool], List[str]], Awaitable[None]]] = set()
        self.logger = logging.getLogger(f"openmux.adapter.power.{self.name}")

    # --- config handling -------------------------------------------------

    @staticmethod
    def _effective_section(config: Dict[str, Any]) -> Dict[str, Any]:
        """Return the flat power section from wrapped or flat config.

        The factory passes list sections as ``{section: [...]}``; object
        sections are passed flat. The soft-reload bootstrap may pass an
        empty list under the section key (means "no PDUs yet").
        """
        val = config.get("power") if isinstance(config, dict) else None
        if isinstance(val, dict):
            return val
        if isinstance(val, list):
            return {"pdus": []}
        return config if isinstance(config, dict) else {}

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> bool:
        """Validate the power section structure (fast, pre-instantiation).

        Accepts the wrapped ``{"power": {...}}`` shape used by the factory
        and the flat entry used by the unified ``adapters:`` format.
        """
        section = cls._effective_section(config) if isinstance(config, dict) else {}
        if not isinstance(section, dict):
            return False
        if section.get("enabled", True) is False:
            return True
        pdus = section.get("pdus", [])
        if pdus is None:
            return True
        if not isinstance(pdus, list):
            return False
        seen_names = set()
        for pdu in pdus:
            if not isinstance(pdu, dict):
                return False
            pname = pdu.get("name")
            if not isinstance(pname, str) or not _valid_ref_token(pname):
                return False
            if pname in seen_names:
                return False
            seen_names.add(pname)
            driver = pdu.get("driver")
            if not isinstance(driver, str) or driver not in DRIVERS:
                return False
            try:
                _as_poll_interval(pdu.get("poll_interval"))
            except ValueError:
                return False
            options = pdu.get("options")
            if options is not None and not isinstance(options, dict):
                return False
            try:
                _parse_annotations(pdu.get("outlets"))
            except ValueError:
                return False
        return True

    def get_capabilities(self) -> Set[AdapterCapability]:
        return {AdapterCapability.MANAGES_POWER}

    def get_adapter_type(self) -> str:
        """Return the stable adapter type key (works unbound too)."""
        return "power"

    def get_port_configurations(self) -> Dict[str, Dict[str, Any]]:
        """Return empty mapping (adapter does not provide console ports)."""
        return {}

    async def create_port(self, port_name: str, config: Dict[str, Any]) -> Optional[Any]:  # vulture: ignore
        """No-op: outlets are not console ports."""
        return None

    async def destroy_port(self, port_name: str) -> None:
        """No-op: outlets are not console ports."""

    # --- optional dependency injection -----------------------------------

    def set_console_manager(self, console_manager) -> None:
        """Inject optional console manager dependency (symmetry with peers)."""
        self.console_manager = console_manager

    def set_auth_manager(self, auth_manager) -> None:
        """Inject optional auth manager dependency."""
        self.auth_manager = auth_manager

    # --- OMXCTRL power control frames (CLI over TCP / WebSocket) ------------

    async def handle_power_frame(
        self,
        port_name: str,
        req: Dict[str, Any],
        username: Optional[str],
        client_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Serve one ``power_query`` / ``power_switch`` OMXCTRL control frame.

        Shared by the client-listener (raw TCP, NUL-prefixed frames) and the
        web console (``OMXCTRL `` text frames); the console CLI's ``p`` power
        menu drives it. Returns the response payload (sent back as an
        ``OMXCTRL`` frame) or None when the type is not a power type (the
        caller swallows it). Switches enforce the same read-write plus
        console-group rules as the web POST path. ``client_id`` is the server-
        side id of the requesting session (for the audit line); it is NOT read
        from the frame.
        """
        rtype = req.get("type")
        if rtype not in ("power_query", "power_switch"):
            return None
        if rtype == "power_query":
            feeds = []
            ppl = None
            if hasattr(self, "port_power_payload"):
                try:
                    ppl = self.port_power_payload(port_name)
                except Exception:
                    ppl = None
            if isinstance(ppl, dict) and isinstance(ppl.get("feeds"), list):
                feeds = [
                    {"ref": str(f.get("ref")), "on": f.get("on"), "watts": f.get("watts")} for f in ppl.get("feeds") or []
                ]
            return {
                "type": "power_feeds",
                "feeds": feeds,
                "feeds_total": len(feeds),
                "state": (ppl or {}).get("state", "unknown"),
            }
        # read-write / admin required to switch, same as the web POST path
        try:
            perm = self.auth_manager.get_user_permissions(username) if (self.auth_manager and username) else None
        except Exception:
            perm = None
        if perm not in ("read-write", "admin"):
            return {"type": "power_switch", "ok": False, "error": "insufficient permission (need read-write)"}
        ref = req.get("ref")
        on = req.get("on")
        if not isinstance(ref, str) or not ref or not isinstance(on, bool):
            return {"type": "power_switch", "ok": False, "error": "invalid power switch request"}
        blocked = self._power_blocked_ports(ref, username)
        if blocked:
            return {
                "type": "power_switch",
                "ok": False,
                "error": f"{ref} feeds consoles outside your groups ({', '.join(blocked)}); switching it needs admin",
            }
        try:
            result = await self.set_outlet(ref, on, user=username, client_id=client_id)
        except Exception as exc:
            return {"type": "power_switch", "ok": False, "error": str(exc)}
        if not result.get("ok"):
            # The refusal carries the same off-impact preview the web POST
            # path returns, so the in-session menu can name the consoles
            # that would lose all power (the switch is refused, so nothing
            # changes).
            return {
                "type": "power_switch",
                "ok": False,
                "error": str(result.get("error", "switch failed")),
                "impact": result.get("impact"),
            }
        reading = result.get("reading") or {}
        state_txt = "unknown"
        if reading.get("on") is True:
            state_txt = "on"
        elif reading.get("on") is False:
            state_txt = "off"
        return {
            "type": "power_switch",
            "ok": True,
            "ref": ref,
            "on": bool(on),
            "state": state_txt,
            "impact": result.get("impact"),
        }

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> bool:
        """Build PDU state, run initial reads, start per-PDU poll tasks.

        A PDU that is unreachable at start does not fail adapter startup;
        its outlets stay unknown and the poll task retries.
        """
        section = self._effective_section(self.config)
        for pdu_cfg in section.get("pdus") or []:
            await self._build_pdu(pdu_cfg)
        self.is_running = self.enabled
        if not self.enabled:
            self.logger.info("Power management is disabled by configuration")
            return True
        for state in self.pdus.values():
            try:
                ids = await state.driver.list_outlets()
            except Exception as exc:
                self.logger.error("PDU %s outlet discovery failed: %s", state.name, exc, exc_info=True)
                state.online = False
                continue
            for oid in ids:
                state.readings.setdefault(oid, OutletReading())
            await self._refresh_readings(state)
            self._start_poll_task(state)
        self.logger.info(
            "Power adapter running with %d PDU(s): %s",
            len(self.pdus),
            ", ".join(sorted(self.pdus)) or "none",
        )
        return True

    async def stop(self) -> None:
        """Cancel poll tasks and release PDU state."""
        self.is_running = False
        for state in self.pdus.values():
            self._stop_poll_task(state)
        self.pdus.clear()

    async def _build_pdu(self, pdu_cfg: Dict[str, Any]) -> None:
        """Create (or replace) runtime state for one configured PDU."""
        if not isinstance(pdu_cfg, dict):
            self.logger.error("Ignoring malformed PDU entry (not a mapping)")
            return
        try:
            state = PduState(
                str(pdu_cfg.get("name")), pdu_cfg, DRIVERS[str(pdu_cfg.get("driver"))](pdu_cfg.get("options") or {})
            )
        except (KeyError, TypeError, ValueError) as exc:
            self.logger.error("Ignoring invalid PDU entry %r: %s", pdu_cfg, exc)
            return
        if not _valid_ref_token(state.name):
            self.logger.error("PDU name %r must be non-empty without dots or whitespace", state.name)
            return
        self._stop_poll_task(self.pdus.get(state.name))  # placeholder task if replacing
        self.pdus[state.name] = state

    def _start_poll_task(self, state: PduState) -> None:
        if state.poll_interval <= 0 or state.task is not None:
            return
        state.task = asyncio.ensure_future(self._poll_pdu(state))

    def _stop_poll_task(self, state: Optional[PduState]) -> None:
        if state is None:
            return
        if state.task is not None and not state.task.done():
            state.task.cancel()
        state.task = None

    async def _poll_pdu(self, state: PduState) -> None:
        """Background poll loop for one PDU (owns its retry cadence)."""
        try:
            while self.is_running and self.pdus.get(state.name) is state:
                await asyncio.sleep(state.poll_interval)
                if self.pdus.get(state.name) is not state:
                    return
                await self._refresh_readings(state)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # pragma: no cover - defensive: loop must not die
            self.logger.error("Poll loop for PDU %s ended unexpectedly: %s", state.name, exc, exc_info=True)

    async def _refresh_readings(self, state: PduState) -> None:
        """Read all outlets once; drop unknown readings when the PDU is down.

        On success: update readings and emit ``power_outlet_changed`` for
        every outlet whose on/off flag (including None<->value) changed.
        On failure: mark the PDU offline and drop on/off to None so badges
        and impact logic see "unknown" instead of stale truth.
        """
        try:
            readings = await state.driver.read_states()
        except Exception as exc:
            if state.online is not False:
                self.logger.warning("PDU %s unreachable: %s", state.name, exc)
            self._mark_pdu_offline(state, str(exc))
            return
        state.online = True
        previous = state.readings
        changed: List[tuple] = []
        for oid, reading in readings.items():
            prev = previous.get(oid)
            if prev is not None and prev.error:
                reading.error = ""
            previous[oid] = reading
            if prev is None or prev.on != reading.on:
                changed.append((oid, prev.on if prev else None, reading.on))
        # Drop outlet ids the driver no longer reports (device changed).
        for oid in list(previous.keys()):
            if oid not in readings:
                del previous[oid]
        for oid, old_on, new_on in changed:
            self._emit_outlet_change(state.name, oid, old_on, new_on)

    def _mark_pdu_offline(self, state: PduState, error: str) -> None:
        state.online = False
        for oid, reading in state.readings.items():
            if reading.on is not None:
                reading.error = error
                self._emit_outlet_change(state.name, oid, reading.on, None)
                reading.on = None
            else:
                reading.error = error
                reading.watts = reading.amps = reading.volts = None

    # --- control -----------------------------------------------------------

    @staticmethod
    def _parse_ref(ref: str) -> tuple:
        """Split a LOCAL outlet ref ``<pdu>.<id>``; raise ValueError when malformed.

        Remote (federated-node-owned) refs carry an "origin::" prefix and must
        not arrive here: callers relay them (``_remote_origin_for_ref``) or
        strip the prefix first (``split_remote_ref``).
        """
        if split_remote_ref(ref) is not None:
            raise ValueError(f"invalid local outlet ref {ref!r}; the origin prefix belongs to a remote ref")
        if not isinstance(ref, str) or ref.count(".") != 1:
            raise ValueError(f"invalid outlet ref {ref!r}; expected <pdu>.<id>")
        pdu_name, outlet_id = ref.split(".", 1)
        if not _valid_ref_token(pdu_name) or not _valid_ref_token(outlet_id):
            raise ValueError(f"invalid outlet ref {ref!r}")
        return pdu_name, outlet_id

    async def set_outlet(
        self, ref: str, on: bool, user: Optional[str] = None, client_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Switch one outlet and broadcast the change to affected consoles.

        Returns ``{ok, reading, impact}``; on pre-flight failure (bad ref,
        unknown PDU/outlet, driver error) ``ok`` is False and no event is
        emitted. ``impact`` is computed BEFORE the change so the caller can
        show what will go offline.

        On success the control audit log records one server-log line
        (``POWER CONTROL``) naming the user, the outlet ref, the new state, and
        the consoles losing all power; each affected console port's data log
        gains a ``power_control_notice`` meta event with the ``[POWER]``
        notice wording (single line, log-appropriate). Failures are not
        audit-logged (each logs its own error line).
        """
        # Outlet federation: a ref owned by a remote node is switched on the
        # origin. The ref arrives prefixed with the origin's server id
        # ("origin::<pdu>.<id>" - the same "::" convention as the federation
        # display strings), so it resolves unambiguously even when this node
        # has an outlet of the same name. The peer pre-checks what it can see
        # (read-write above, the group check for visible fed ports done by the
        # surface that owns username) and relays the switch anchored on a
        # federated session on a port the ref powers; the origin re-verifies
        # the anchor and the coverage of its FULL fed set, then runs this same
        # method locally.
        if split_remote_ref(ref) is not None:
            return await self._relay_power_switch(ref, bool(on), client_id=client_id)
        try:
            pdu_name, outlet_id = self._parse_ref(ref)
        except ValueError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "impact": {"change": "off" if not on else "on", "losing_power": [], "staying_up": []},
            }
        if self.enabled is False:
            return {"ok": False, "error": "power management is disabled", "impact": self._empty_impact(on)}
        # Bare-ref fallback: a fed port advertising unprefixed feeds (an older
        # origin, or a stray config) is still classified by the declaring port.
        origin_id = self._remote_origin_for_ref(ref)
        if origin_id:
            return await self._relay_power_switch(ref, bool(on), client_id=client_id)
        state = self.pdus.get(pdu_name)
        if state is None:
            return {"ok": False, "error": f"PDU {pdu_name} is not configured", "impact": self._empty_impact(on)}
        impact = self._empty_impact(on)
        if not on:
            impact = self.compute_off_impact(ref, local_ref=True)
        try:
            reading = await state.driver.set_state(outlet_id, bool(on))
        except Exception as exc:
            self.logger.error("set_state failed for %s: %s", ref, exc)
            return {"ok": False, "error": str(exc), "impact": impact}
        if reading.error:
            return {"ok": False, "error": reading.error, "reading": reading.to_dict(), "impact": impact}
        prev = state.readings.get(outlet_id)
        if prev is not None and prev.error:
            reading.error = ""
        state.readings[outlet_id] = reading
        if state.online is False:
            state.online = True
        if prev is None or prev.on != reading.on:
            self._emit_outlet_change(pdu_name, outlet_id, prev.on if prev else None, reading.on)
        self._audit_control(ref, bool(on), user, client_id, impact)
        return {"ok": True, "reading": reading.to_dict(), "impact": impact}

    def _find_muxcon_adapter(self) -> Optional[Any]:
        """The active muxcon adapter, or None (outlet federation relay path)."""
        pm = self.main_port_manager
        adapters = getattr(pm, "unified_adapters", None) if pm is not None else None
        for uad in adapters or []:
            try:
                if str(uad.get_adapter_type()).lower() == "muxcon":
                    return uad
            except Exception:
                continue
        return None

    async def _relay_power_switch(self, ref: str, on: bool, client_id: Optional[str] = None) -> Dict[str, Any]:
        """Relay one outlet switch to its origin node (outlet federation).

        The group check for visible fed ports was enforced by the calling
        surface (handle_power_frame / run_power_command). The anchor is the
        ACTING user's own open session on a federated port that the ref feeds
        (``client_id`` is that user's server-side session id; the web REST path
        carries none, so it cannot anchor and keeps the typed refusal). The
        muxcon relay then pins the stream to the user's own session (the origin
        audits + checks the mirror of the anchored session). The claims list is
        every port on this node declaring the ref, which the origin cross-checks
        against the true fed set to catch consoles this node never saw.
        """
        pm = self.main_port_manager
        ports = getattr(pm, "ports", None) if pm is not None else None
        if not isinstance(ports, dict):
            return {"ok": False, "error": "no console registry available for the relay", "impact": self._empty_impact(on)}
        origin_split = split_remote_ref(ref)
        origin = origin_split[0] if origin_split else self._remote_origin_for_ref(ref)
        origin_local_ref = origin_split[1] if origin_split else ref
        if not client_id:
            return {
                "ok": False,
                "error": f"{ref} is owned by federated node {origin}; switch it from a session attached to a port it powers",
                "impact": self._empty_impact(on),
            }
        anchor = None
        # Claims (the ports this node declares for the ref) use the prefixed
        # form the origin understands when it re-checks coverage of its full
        # fed set.
        claims: List[str] = [
            name for name, obj in list(ports.items()) if ref in self._refs_of(getattr(obj, "unified_port", obj))
        ]
        for name, obj in list(ports.items()):
            if ref not in self._refs_of(getattr(obj, "unified_port", obj)):
                continue
            if (
                anchor is None
                and getattr(obj, "remote_port_name", None) is not None
                and getattr(obj, "is_connected", True)
                and client_id in (getattr(obj, "_client_sessions", None) or {})
            ):
                anchor = obj
        if anchor is None:
            return {
                "ok": False,
                "error": f"{ref} is owned by federated node {origin}; switch it from a session attached to a port it powers",
                "impact": self._empty_impact(on),
            }
        muxcon = self._find_muxcon_adapter()
        if muxcon is None or not hasattr(muxcon, "relay_power_switch"):
            return {"ok": False, "error": "no active federation link to the origin node", "impact": self._empty_impact(on)}
        # The wire carries the ORIGIN-LOCAL ref: the origin strips this node's
        # "origin::" prefix and checks/executes the ref on its own PDUs, naming
        # its own local ports in any coverage refusal. The prefixed form stays
        # in the audit trail and every refusal shown to the requester.
        result = await muxcon.relay_power_switch(anchor.name, origin_local_ref, on, claims, client_id=client_id)
        if isinstance(result, dict) and result.get("ok"):
            reading = {"on": bool(result.get("on")), "watts": None, "amps": None, "volts": None, "error": ""}
            return {"ok": True, "reading": reading, "impact": self._empty_impact(on)}
        return {"ok": False, "error": str((result or {}).get("error", "switch not relayed")), "impact": self._empty_impact(on)}

    def _remote_origin_for_ref(self, ref: str) -> Optional[str]:
        """Origin node id when ref is declared only on federated ports (outlet
        federation).

        Fed ports carry their feeds with the "origin::" prefix, so a prefixed
        ref is unambiguous and resolves to its origin without a registry scan.
        A bare ref falls back to scanning remote proxies (defensive: a
        malformed advertisement could carry an un-prefixed feed) and to local
        ports taking precedence, so this returns None for locally owned refs.
        """
        split = split_remote_ref(ref)
        if split is not None:
            return split[0]
        pm = self.main_port_manager
        ports = getattr(pm, "ports", None) if pm is not None else None
        if not isinstance(ports, dict):
            return None
        local_declares = False
        origin_id: Optional[str] = None
        for _name, obj in list(ports.items()):
            if hasattr(obj, "remote_port_name"):
                if origin_id is None and ref in self._power_refs_of(obj):
                    origin = getattr(getattr(obj, "metadata", None), "origin_server", None)
                    sid = getattr(origin, "server_id", None)
                    if isinstance(sid, str) and sid:
                        origin_id = sid
                continue
            if ref in self._refs_of(getattr(obj, "unified_port", obj)):
                local_declares = True
        if local_declares:
            return None
        return origin_id

    @staticmethod
    def _empty_impact(on: bool) -> Dict[str, Any]:
        return {"change": "on" if on else "off", "losing_power": [], "staying_up": []}

    # --- control audit + port-log notice ------------------------------------

    def _port_obj_for_name(self, port_name: str) -> Any:
        """Return the port registry entry for one port (used for log paths)."""
        pm = self.main_port_manager
        try:
            if pm is not None:
                ports = getattr(pm, "ports", {})
                if isinstance(ports, dict):
                    return ports.get(port_name)
        except Exception:
            pass
        return None

    def _control_notice_text(self, ref: str, on: Optional[bool]) -> str:
        """The single-line ``[POWER]`` wording for this change (for the port log).

        Mirrors the wording of the client-listener terminal notice, which is
        built separately (with its ``\r\n`` framing and, in the all-lost case,
        the outlet named in parens for the attached clients). The port log
        keeps only the plain sentence: the outlet ref is already in the
        record's ``outlet=`` field.
        """
        if on is True:
            return "[POWER] feed " + ref + " is now on"
        if on is False:
            return "[POWER] feed " + ref + " is now off"
        return "[POWER UNKNOWN] feed " + ref + " state unknown"

    def _control_all_lost_notice_text(self) -> str:
        """The log-appropriate warning for a console that lost its last feed.

        In the port log ``this console`` is the port the file belongs to, and
        the outlet that just went off sits in the record's ``outlet=`` field,
        so the parenthetical the terminal notice uses would mislead.
        """
        return "[POWER WARNING] all power feeds to this console are now off"

    def _audit_control(
        self, ref: str, on: bool, user: Optional[str], client_id: Optional[str], impact: Dict[str, Any]
    ) -> None:
        """Record one control event: server-log audit line + per-affected-port
        data-log notice. Runs only on successful switches (the caller logs
        driver failures itself). Best-effort: a logging fault never changes
        the switch result.
        """
        ref = str(ref)
        state_txt = "on" if on else "off"
        losing = [str(e.get("port")) for e in impact.get("losing_power") or [] if isinstance(e, dict) and e.get("port")]
        staying = [str(e.get("port")) for e in impact.get("staying_up") or [] if isinstance(e, dict) and e.get("port")]
        meta_text = self._control_notice_text(ref, on)
        warn_text = self._control_all_lost_notice_text()
        parts = ["POWER CONTROL: user " + (user or "unknown") + " turned " + ref + " " + state_txt]
        if losing:
            parts.append("losing all power: " + ", ".join(losing))
        if staying:
            parts.append("staying up: " + ", ".join(staying))
        if client_id:
            parts.append("client " + str(client_id))
        self.logger.info("; ".join(parts))
        # Port data log: one meta event per affected console port. The text
        # mirrors the attached-session notice; the all-lost case is computed
        # port-wise (that port's other feeds all off), same rule as the
        # per-port fan-out in _emit_outlet_change.
        for port_name in self._mapped_ports_for_ref(ref):
            port_obj = self._port_obj_for_name(port_name)
            other_outlets_on = [r for r in self.port_power_map(port_name) if r != ref and self._outlet_on_state(r) is True]
            event_text = warn_text if (on is False and not other_outlets_on) else meta_text
            try:
                DataLogger.get().record_meta(
                    port_name=port_name,
                    event="power_control_notice",
                    client_id=client_id,
                    meta={"outlet": ref, "state": "on" if on else "off", "text": event_text, "user": user or "unknown"},
                    port_obj=port_obj,
                )
            except Exception:
                self.logger.debug("Power control port-log record failed for %s", port_name, exc_info=True)

    # --- console-side mapping (live) ----------------------------------------

    def _port_objects(self) -> List[tuple]:
        """Yield (name, inner_port) pairs from the global port registry."""
        pm = self.main_port_manager
        if pm is None:
            return []
        ports = getattr(pm, "ports", None)
        if not isinstance(ports, dict):
            return []
        pairs = []
        for name, obj in list(ports.items()):
            inner = getattr(obj, "unified_port", obj)
            pairs.append((str(name), inner))
        return pairs

    def port_power_map(self, port_name: str) -> List[str]:
        """Return the outlet refs declared on one console port (live read).

        The single place web/CLI/badge code reads the console->outlet
        mapping; it is a plain attribute on the port object set by the
        port adapters from each port's ``power:`` config key. For a
        federated port (RemotePortProxy) the same attribute is set from
        the origin's advertised feed list, so the mapping works with no
        caller changes (outlet federation).
        """
        obj = self._port_obj_for_name(port_name)
        if obj is None:
            return []
        return self._power_refs_of(obj)

    @staticmethod
    def _power_refs_of(port_obj: Any) -> List[str]:
        """Return the raw ``power`` feed refs stored on one port object.

        Reads the plain ``power`` attribute (set by the port adapters from
        the ``power:`` config key, or from the origin's advertised feed list
        on a federated proxy) and normalizes it to a list of strings.
        """
        inner = getattr(port_obj, "unified_port", port_obj)
        refs = getattr(inner, "power", None)
        if isinstance(refs, (list, tuple)):
            return [str(r) for r in refs]
        return []

    def _is_remote_port(self, port_name: str) -> bool:
        """True when the port is a federated RemotePortProxy (outlet federation).

        The proxy carries a ``remote_port_name`` attribute that local ports
        never do; this is the same discriminator the muxcon status relay
        uses to decide whether to re-broadcast an event.
        """
        try:
            obj = self._port_obj_for_name(port_name)
            return obj is not None and hasattr(obj, "remote_port_name")
        except Exception:
            return False

    def _remote_feed_states(self, port_name: str) -> Dict[str, Optional[bool]]:
        """Outlet state for a federated port's feeds, as last reported by the
        origin node over the muxcon POWER:STATE channel ({} when the port is
        not a remote proxy or carries no state).

        The proxy caches states under the GLOBALLY qualified ref
        (``origin::<ref>``) - registration and the POWER:STATE apply path both
        store the prefixed form - which is exactly the same form the port's
        feed list (``power``) and the switch path use, so this is a plain
        passthrough (outlet federation).
        """
        try:
            obj = self._port_obj_for_name(port_name)
            states = getattr(obj, "_feed_states", None) if obj is not None else None
            if isinstance(states, dict):
                return {str(k): states[k] for k in states}
        except Exception:
            pass
        return {}

    def feed_states(self, port_name: str) -> Dict[str, Optional[bool]]:
        """Current on/off state for one console port's declared feeds.

        Returns ``{ref: on|off|None}`` (None = unknown) covering every feed
        in the port's ``power`` list. Local ports read the live local PDU
        readings; a federated port's feeds are backed by the origin node,
        so this returns the origin's last-reported state cached on the
        remote proxy (outlet federation). Ports without feeds -> {}.
        """
        obj = self._port_obj_for_name(port_name)
        if obj is None:
            return {}
        refs = self._power_refs_of(obj)
        if not refs:
            return {}
        if self._is_remote_port(port_name):
            return self._remote_feed_states(port_name)
        return {ref: self._outlet_on_state(ref) for ref in refs}

    def _mapped_ports_for_ref(self, ref: str) -> List[str]:
        """Return all console port names declaring the given outlet ref."""
        return [name for name, inner in self._port_objects() if self._refs_of(inner) and ref in self._refs_of(inner)]

    def _power_blocked_ports(self, ref: str, username: Optional[str]) -> List[str]:
        """Return the consoles fed by ``ref`` that ``username`` may not drive.

        Delegates to the console manager's attach-time access ladder, so a
        read-write user may only switch an outlet whose fed consoles they can
        all open (read-write). An outlet feeding no console is never blocked.
        """
        if not username:
            return []
        try:
            cm = self.console_manager
            check = getattr(cm, "blocked_ports_for_user", None)
            if cm is None or not callable(check):
                return []
            return check(self._mapped_ports_for_ref(ref), username)
        except Exception:
            self.logger.debug("Power group check failed for %s", ref, exc_info=True)
            return []

    @staticmethod
    def _refs_of(port_obj: Any) -> List[str]:
        refs = getattr(port_obj, "power", None)
        if isinstance(refs, (list, tuple)):
            return [str(r) for r in refs]
        return []

    def _port_description(self, port_name: str) -> str:
        """A short label for one port in user-facing text (off-impact preview).

        A federated port is labeled origin-qualified ("origin::name"), the same
        double-colon origin convention the federation display strings use, so
        the preview points at the right console on the right node. Local ports
        keep their plain description (or an empty label).
        """
        pm = self.main_port_manager
        try:
            if pm is None:
                return ""
            ports = getattr(pm, "ports", {})
            obj = ports.get(port_name) if isinstance(ports, dict) else None
            if obj is None:
                return ""
            origin = getattr(getattr(obj, "metadata", None), "origin_server", None)
            origin_id = getattr(origin, "server_id", None)
            if hasattr(obj, "remote_port_name") and isinstance(origin_id, str) and origin_id:
                name = getattr(obj, "remote_port_name", None) or port_name
                return f"{origin_id}::{name}"
            desc = getattr(obj, "description", None)
            if isinstance(desc, str) and desc:
                return desc
        except Exception:
            pass
        return ""

    def _outlet_on_state(self, ref: str) -> Optional[bool]:
        """Live on/off flag for a ref (None = unknown/unresolvable)."""
        try:
            pdu_name, outlet_id = self._parse_ref(ref)
        except ValueError:
            return None
        state = self.pdus.get(pdu_name)
        if state is None:
            return None
        reading = state.readings.get(outlet_id)
        return reading.on if reading is not None else None

    def compute_off_impact(self, ref: str, local_ref: bool = False) -> Dict[str, Any]:
        """Pure set logic: who loses ALL power if this outlet goes off.

        A console loses all power when, after the hypothetical change, no
        feed in its ``power`` list reads on (unknown/unresolvable feeds do
        not count as on). Consoles with at least one other live feed are
        reported separately (dual-feed A/B awareness).

        ``local_ref=True`` compares the ref against the port's LOCAL feed
        entries (``<pdu>.<id>``): only the set_outlet local switch path passes
        it, and a port fed by an outlet of the same name on ANOTHER node
        (``origin::rack1.1``) is not affected by this node's local ref
        (outlet federation).
        """
        losing: List[Dict[str, str]] = []
        staying: List[Dict[str, Any]] = []
        for name, inner in self._port_objects():
            declared = self._refs_of(inner)
            if local_ref:
                declared = [r for r in declared if split_remote_ref(r) is None]
            if ref not in declared:
                continue
            refs = [r for r in declared if r != ref]
            live = [r for r in refs if self._outlet_on_state(r) is True]
            entry: Dict[str, Any] = {"port": name, "description": self._port_description(name)}
            if live:
                entry["via"] = live
                staying.append(entry)
            else:
                losing.append(entry)
        return {"change": "off", "losing_power": losing, "staying_up": staying}

    # --- event fan-out -------------------------------------------------------

    def register_state_listener(self, callback: Callable[[str, Optional[bool], List[str]], Awaitable[None]]) -> None:
        """Subscribe to (ref, new_on, affected_ports) change callbacks."""
        self._state_listeners.add(callback)

    def unregister_state_listener(self, callback: Callable[[str, Optional[bool], List[str]], Awaitable[None]]) -> None:
        self._state_listeners.discard(callback)

    def _emit_outlet_change(self, pdu_name: str, outlet_id: str, old_on: Optional[bool], new_on: Optional[bool]) -> None:
        """Fan one outlet change out to every affected console port.

        Each mapped port gets a Per-Port meta event (the web badge frame and
        the client-listener text notice both key off this event). Plugin
        listeners (e.g. the /ws/power socket) get the raw callback.
        """
        ref = f"{pdu_name}.{outlet_id}"
        affected = self._mapped_ports_for_ref(ref)
        pm = self.main_port_manager
        for port_name in affected:
            if pm is None or not hasattr(pm, "notify_meta_updated"):
                continue
            feeds = [r for r in self._refs_of_safe(port_name) if r != ref]
            on_others = [r for r in feeds if self._outlet_on_state(r) is True]
            all_lost = new_on is not True and not on_others
            try:
                pm.notify_meta_updated(
                    port_name,
                    {
                        "event": "power_outlet_changed",
                        "outlet": ref,
                        "on": new_on,
                        "all_power_lost": all_lost,
                        "other_outlets_on": on_others,
                    },
                )
            except Exception:
                # justification: best-effort meta push; state is already stored
                pass
        for callback in list(self._state_listeners):
            try:
                coro = callback(ref, new_on, list(affected))
                if asyncio.iscoroutine(coro):
                    asyncio.ensure_future(coro)
            except Exception:
                # justification: listener failures must not break the emitter
                pass

    def _refs_of_safe(self, port_name: str) -> List[str]:
        try:
            return self.port_power_map(port_name)
        except Exception:
            return []

    # --- snapshot -------------------------------------------------------------

    def get_power_snapshot(self) -> Dict[str, Any]:
        """Full derived view of every PDU + console-side mapping resolution.

        This is the single JSON source for /api/power, the Power pages, the
        status-page merge, and the per-port badge payload; the per-port
        inversion is recomputed here so nothing is cached across adapters.
        """
        pdus_out: List[Dict[str, Any]] = []
        resolved_refs: Set[str] = set()
        for state in self.pdus.values():
            outlets_out: List[Dict[str, Any]] = []
            ordered_ids = sorted(state.readings.keys())
            outlets_on = 0
            total_watts: Optional[float] = None
            for oid in ordered_ids:
                reading = state.readings.get(oid)
                if reading is None:
                    continue
                ref = f"{state.name}.{oid}"
                resolved_refs.add(ref)
                if reading.on is True:
                    outlets_on += 1
                    if reading.watts is not None:
                        total_watts = (total_watts or 0.0) + reading.watts
                mapped = self._mapped_ports_for_ref(ref)
                entry: Dict[str, Any] = {
                    "id": oid,
                    "ref": ref,
                    "description": state.annotations.get(oid, ""),
                    "online": state.online,
                    "off_impact": self.compute_off_impact(ref),
                }
                entry.update(reading.to_dict())
                entry["mapped_ports"] = mapped
                entry["any_mapped"] = bool(mapped)
                outlets_out.append(entry)
            pdus_out.append(
                {
                    "name": state.name,
                    "description": state.description,
                    "driver": state.driver_name,
                    "online": state.online,
                    "poll_interval": state.poll_interval,
                    "outlets": outlets_out,
                    "outlets_on": outlets_on,
                    "outlet_count": len(outlets_out),
                    "total_watts": total_watts,
                }
            )
        # Refs declared on console ports that resolve to nothing (renamed
        # PDU, typo, unknown outlet id) - surfaced for the consistency view.
        # Federated ports are skipped: their refs point at the origin node's
        # PDUs, which are not local here, so they would only be noise
        # (outlet federation).
        unresolved: List[str] = []
        for name, inner in self._port_objects():
            if hasattr(inner, "remote_port_name"):
                continue
            for ref in self._refs_of(inner):
                if split_remote_ref(ref) is not None:
                    continue  # origin-owned feed (outlet federation); not local here
                if ref not in resolved_refs and ref not in unresolved:
                    unresolved.append(ref)
        pdus_out.sort(key=lambda p: p["name"])
        return {
            "enabled": self.enabled,
            "pdus": pdus_out,
            "unresolved_refs": sorted(unresolved),
        }

    def port_power_payload(self, port_name: str) -> Optional[Dict[str, Any]]:
        """Compact per-port power summary for the status page + meta frame.

        Returns None when the port declares no ``power:`` feed at all
        (callers then omit the key). ``state`` is one of all|some|none|
        unknown so the client can color without re-deriving rules. For a
        federated port the feed refs and states come from the origin's
        advertisement (outlet federation): same shape, but watts is None
        (the peer does not see origin telemetry) and the on-state is the
        origin's last-reported value.
        """
        feeds = self.port_power_map(port_name)
        if not feeds:
            return None
        remote = self._is_remote_port(port_name)
        remote_states = self._remote_feed_states(port_name) if remote else {}
        feed_out: List[Dict[str, Any]] = []
        feeds_on = 0
        known = 0
        for ref in feeds:
            watts = None
            if remote:
                on = remote_states.get(ref)
            else:
                on = self._outlet_on_state(ref)
                try:
                    pdu_name, outlet_id = self._parse_ref(ref)
                except ValueError:
                    pdu_name, outlet_id = None, None
                if pdu_name is not None:
                    state = self.pdus.get(pdu_name)
                    if state is not None:
                        reading = state.readings.get(outlet_id)
                        if reading is not None:
                            watts = reading.watts
            feed_out.append({"ref": ref, "on": on, "watts": watts})
            if on is True:
                feeds_on += 1
                known += 1
            elif on is False:
                known += 1
            # on is None -> unknown (PDU down / not polled / bad ref)
        if known == 0:
            state = "unknown"
        elif feeds_on == len(feeds):
            state = "all"
        elif feeds_on == 0:
            state = "none"
        else:
            state = "some"
        return {
            "feeds": feed_out,
            "feeds_on": feeds_on,
            "feeds_total": len(feeds),
            "state": state,
            "all_power_lost": state == "none",
        }

    # --- soft reload ------------------------------------------------------------

    async def reconcile_ports(self, new_config: Any) -> Dict[str, Any]:
        """Incrementally update PDUs on soft reload.

        Args:
            new_config: The ``power`` section dict (possibly empty), or None.

        Returns:
            Summary dict: {added, removed, updated, unchanged}.
        """
        if isinstance(new_config, dict) and isinstance(new_config.get("power"), (dict, list)):
            section = new_config["power"]
            if isinstance(section, list):
                section = {"pdus": []}
        elif isinstance(new_config, dict):
            section = new_config
        else:
            section = {}
        if "pdus" not in section and "enabled" not in section:
            # main.py passes {} when the section is dropped: remove all PDUs
            section = {"pdus": []}
        if section is None:
            section = {"pdus": []}
        new_enabled = bool(section.get("enabled", True))
        old_enabled = self.enabled
        self.enabled = new_enabled

        new_by_name: Dict[str, Dict[str, Any]] = {}
        for pdu in section.get("pdus") or []:
            if isinstance(pdu, dict) and _valid_ref_token(str(pdu.get("name") or "")):
                new_by_name[str(pdu["name"])] = pdu

        old_names = set(self.pdus.keys())
        new_names = set(new_by_name.keys())
        removed = sorted(old_names - new_names)
        added = sorted(new_names - old_names)
        common = sorted(old_names & new_names)

        updated: List[str] = []
        unchanged: List[str] = []
        for pname in common:
            state = self.pdus[pname]
            cfg = new_by_name[pname]
            try:
                new_mat = {
                    "driver": str(cfg.get("driver")),
                    "poll_interval": _as_poll_interval(cfg.get("poll_interval")),
                    "options": dict(cfg.get("options") or {}),
                }
            except (KeyError, TypeError, ValueError):
                new_mat = None  # invalid new entry: keep the running instance
            if new_mat is not None and state.material() != new_mat:
                updated.append(pname)
                continue
            # In-place: description + outlet annotations only
            state.description = str(cfg.get("description") or "")
            try:
                state.annotations = _parse_annotations(cfg.get("outlets"))
            except ValueError:
                pass
            unchanged.append(pname)

        for pname in removed:
            state = self.pdus.pop(pname)
            self._stop_poll_task(state)
        for pname in added + updated:
            await self._build_pdu(new_by_name[pname])
            state = self.pdus.get(pname)
            if state is None:
                continue
            if new_enabled:
                try:
                    ids = await state.driver.list_outlets()
                except Exception as exc:
                    self.logger.error("PDU %s outlet discovery failed on reload: %s", pname, exc, exc_info=True)
                    state.online = False
                    continue
                for oid in ids:
                    state.readings.setdefault(oid, OutletReading())
                await self._refresh_readings(state)
                self._start_poll_task(state)

        # If the feature was just enabled, start polling for running PDUs
        if new_enabled and not old_enabled:
            for state in self.pdus.values():
                if state.task is None and state.readings:
                    self._start_poll_task(state)
        elif not new_enabled:
            for state in self.pdus.values():
                self._stop_poll_task(state)

        return {"added": added, "removed": removed, "updated": updated, "unchanged": unchanged}

    # --- status -------------------------------------------------------------

    def get_status_info(self) -> Dict[str, Any]:
        """Return standardized adapter status dict (power-flavored)."""
        pdus = sorted(self.pdus.keys())
        online = [name for name in pdus if self.pdus[name].online is True]
        return {
            "type": self.get_adapter_type(),
            "status": "running" if self.is_running else "stopped",
            "ports": f"{len(pdus)} pdus",
            "details": {
                "enabled": self.enabled,
                "pdus": pdus,
                "pdu_online": online,
            },
        }
