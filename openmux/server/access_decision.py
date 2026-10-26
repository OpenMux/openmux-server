"""Access decisions: the outcome vocabulary, the wording, and the result value (issue #98).

A Port grants or refuses access in five places: attach, promote, release, Takeover, and
the demotion notices that follow a Takeover. Each of those decisions used to return a
flag plus a string, and every surface kept its own table for turning that string into a
sentence, so four wordings for one outcome drifted apart.

This module is the single owner of three things:

* the Outcome code namespace (``Code``), one member per distinct outcome;
* the wording (``AccessDecision.message`` and ``.lines``), one sentence per outcome,
  identical on the Client listener, Telnet, SSH, and the web console;
* the ``client_mode`` payload (``AccessDecision.frame``), so no surface assembles keys.

It decides nothing. ``ConsoleManager`` keeps the access ladder, the Write slot
arithmetic, and the MuxCon round-trip, and returns one ``AccessDecision``.

This is a leaf: it imports the standard library, ``access_control``, and the shared
``openmux.common.access_wire`` renderer. It never imports ``console_manager`` or anything
under ``server/adapters``, never awaits, and never delivers a frame. ``openmux.client``
must not import a server module, so the CLI reads the wording this module puts on the
wire and renders it with the shared helper in ``openmux.common``.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from openmux.common.access_wire import render_terminal_text
from openmux.server.access_control import capacity_to_wire, holder_id_short, wire_to_mode

#: The one frame type for an access decision. No new frame types exist: the web console
#: hands an unrecognised control frame to the device (see ``web_console._hws_omxctrl``).
FRAME_MODE = "client_mode"
#: Frame type of the ambient viewer badge broadcast (issue #48). It is a Notice with no
#: wording, and it needs its own type because it must never read as a Mode change.
FRAME_PRESENCE = "presence"
#: The existing holders-list reply type, kept so the ``w`` command and ``query_rw_holders``
#: keep answering on a type the readers already filter on.
FRAME_HOLDERS = "rw_holders"
#: A WebSocket close reason must stay at most 123 bytes, and ``message`` doubles as one.
MESSAGE_LIMIT = 123
#: The close code every attach refusal on the web console carries.
CLOSE_ATTACH_DENIED = 4003


class Mode(str, Enum):
    """The access mode a Client holds. Both wire spellings map from this one value."""

    READ_WRITE = "read-write"
    READ_ONLY = "read-only"

    @property
    def wire(self) -> str:
        """The token on the ``CONNECTED:<port>:<token>`` line."""
        return "READ_WRITE" if self is Mode.READ_WRITE else "READ_ONLY"


class Kind(str, Enum):
    """What kind of outcome a code carries. The web console branches on this, not on a code."""

    GRANT = "grant"  # The Client holds what it asked for, or a stated lesser grant.
    REFUSAL = "refusal"  # Access control denied this identity.
    UNAVAILABLE = "unavailable"  # The Port denied it: capacity, a missing Port, a lost race.
    NOTICE = "notice"  # Unsolicited: nobody asked, the Client's standing changed under it.


class Code(str, Enum):
    """One Outcome code per distinct outcome.

    Codes already on the wire keep their shipped spelling, so a Client that has not
    upgraded yet still reads the right outcome (ADR-0001).
    """

    # Attach grants. Silent on the wire: the connect line already says it.
    ATTACHED = "attached"
    READ_ONLY_STANDING = "read_only_standing"  # not write-entitled
    CAPACITY_FULL = "capacity_full"  # entitled, but the Write slot was held
    CAPACITY_NONE = "capacity_none"  # the Port has no Write slot at all
    # Write slot grants.
    GRANTED = "ok"  # value shipped before this module; `victim` names who gave it up
    ALREADY_RW = "already_rw"
    RELEASED = "released"
    # Refusals by access control.
    NO_PERMISSIONS = "no_permissions"
    DENIED_BY_GROUP_ACL = "denied_by_group_acl"
    DENIED_BY_ACCESS_DEFAULT = "denied_by_access_default"
    NOT_ENTITLED = "not_entitled"
    INVALID_TARGET = "invalid_target"
    DEMOTE_FAILED = "demote_failed"
    # Refusals by the Port's state.
    PORT_UNAVAILABLE = "port_unavailable"  # even the read-only attach failed
    NOT_ATTACHED = "not_attached"
    PORT_MISSING = "port_missing"
    NO_HOLDER = "no_holder"
    VICTIM_NOT_CURRENT = "victim_not_current"
    TAKE_WITHOUT_SLOT = "take_without_slot"  # asked to Take on a Port that allows several writers
    NO_WRITE_SLOT = "no_write_slot"  # asked to drive a Port whose Write-slot capacity is none
    PROMOTE_FAILED = "promote_failed"
    FEDERATION_DENIED = "federation_denied"
    INTERNAL = "error"  # value shipped by the web console before this module
    UNSUPPORTED_REQUEST = "unsupported_request"
    # Notices: an unsolicited change to this Client's own standing.
    DEMOTED = "demoted"
    ACTION_SELF_DEMOTED = "action_self_demoted"
    ACTION_RESTORED = "action_restored"
    ACTION_RESTORE_DENIED = "action_restore_denied"

    @property
    def kind(self) -> Kind:
        """Which of the four outcome kinds this code belongs to."""
        return _row(self).kind


@dataclass(frozen=True)
class Holder:
    """One Client holding a Write slot. ``label`` is the only holder label in the server."""

    client_id: str
    username: str = "unknown"
    ip: str = "unknown"

    @property
    def short_id(self) -> str:
        """The short id shown in brackets, and the value a targeted Takeover matches on."""
        return holder_id_short(self.client_id)

    @property
    def label(self) -> str:
        """``[id] username@ip (rw)``, the one label every console shows."""
        return f"[{self.short_id}] {self.username}@{self.ip} (rw)"


class Grant(str, Enum):
    """The three things an identity may be allowed to do on a Port, before capacity."""

    WRITE = "write"  # may hold the Write slot, if one is free
    READ_ONLY = "read_only"  # may watch, never drive
    DENIED = "denied"  # may not attach at all


@dataclass(frozen=True)
class Entitlement:
    """What access control allows one identity to do on one Port.

    :class:`AccessDecision` answers a request that already happened; this answers the
    earlier question of whether the identity is allowed to ask. It carries no mode,
    because a slot may still be unavailable, and ``ok`` would read as "may drive" here
    but means "granted" there. ``blocked_ports_for_user`` reads ``may_drive``.
    """

    grant: Grant
    code: Optional[Code] = None

    @property
    def may_drive(self) -> bool:
        """Whether this identity may hold the Write slot when one is free."""
        return self.grant is Grant.WRITE

    @staticmethod
    def write() -> "Entitlement":
        """The identity may drive this Port."""
        return Entitlement(Grant.WRITE)

    @staticmethod
    def read_only() -> "Entitlement":
        """The identity may watch this Port and nothing more."""
        return Entitlement(Grant.READ_ONLY)

    @staticmethod
    def denied(code: Code) -> "Entitlement":
        """Access control refused the identity; ``code`` says which rule refused it."""
        refusal = _as_code(code)
        if _row(refusal).kind is not Kind.REFUSAL:
            raise ValueError(f"{refusal.value} is not a refusal code.")
        return Entitlement(Grant.DENIED, refusal)


@dataclass(frozen=True)
class _Row:
    """One row of the outcome table: kind, outcome, wording, and the notice's fixed mode."""

    kind: Kind
    ok: bool
    sentence: str
    lines: Tuple[str, ...]
    capacity_none: Optional[str] = None
    capacity_multiple: Optional[str] = None
    notice_mode: Optional[Mode] = None


_GRANT = Kind.GRANT
_REFUSE = Kind.REFUSAL
_UNAVAIL = Kind.UNAVAILABLE
_NOTICE = Kind.NOTICE

_ROWS: Dict[Code, _Row] = {
    # Attach grants: silent, because the connect line and the mode already say it.
    Code.ATTACHED: _Row(_GRANT, True, "Connected with read-write access", ()),
    Code.READ_ONLY_STANDING: _Row(_GRANT, True, "Connected read-only: you may not drive this port", ()),
    Code.CAPACITY_FULL: _Row(_GRANT, True, "Connected read-only: the read-write seat is taken", ()),
    Code.CAPACITY_NONE: _Row(_GRANT, True, "Connected read-only: this port has no write slot", ()),
    Code.GRANTED: _Row(
        _GRANT,
        True,
        "Read-write access granted",
        # The second line is one template: it renders "" and drops when nobody was displaced.
        ("Read-write access granted", "{victim_line}"),
    ),
    Code.ALREADY_RW: _Row(_GRANT, True, "You already hold read-write access", ("You already hold read-write access",)),
    Code.RELEASED: _Row(_GRANT, True, "Switched to read-only mode", ("Switched to read-only mode",)),
    Code.NO_PERMISSIONS: _Row(
        _REFUSE, False, "No permissions assigned to this account", ("No permissions assigned to this account",)
    ),
    Code.DENIED_BY_GROUP_ACL: _Row(_REFUSE, False, "Access denied to this port", ("Access denied to this port",)),
    Code.DENIED_BY_ACCESS_DEFAULT: _Row(
        _REFUSE, False, "Access denied to this port by server policy", ("Access denied to this port by server policy",)
    ),
    Code.NOT_ENTITLED: _Row(
        _REFUSE, False, "You are not entitled to drive this port", ("You are not entitled to drive this port",)
    ),
    Code.INVALID_TARGET: _Row(
        _REFUSE,
        False,
        "Take refused: that user does not hold read-write access (check the id in the holders list)",
        ("Take refused: that user does not hold read-write access (check the id in the holders list)",),
    ),
    Code.DEMOTE_FAILED: _Row(
        _REFUSE,
        False,
        "Release failed: you do not hold the read-write slot on this port",
        ("Release failed: you do not hold the read-write slot on this port",),
    ),
    Code.PORT_UNAVAILABLE: _Row(
        _UNAVAIL, False, "No console seat is free on this port", ("No console seat is free on this port",)
    ),
    Code.NOT_ATTACHED: _Row(_UNAVAIL, False, "You are not attached to this port", ("You are not attached to this port",)),
    Code.PORT_MISSING: _Row(_UNAVAIL, False, "This port does not exist", ("This port does not exist",)),
    Code.NO_HOLDER: _Row(
        _UNAVAIL,
        False,
        "Take refused: this port has no read-write holder to take from",
        ("Take refused: this port has no read-write holder to take from",),
    ),
    Code.VICTIM_NOT_CURRENT: _Row(
        _UNAVAIL,
        False,
        "Take refused: that holder no longer holds read-write access",
        ("Take refused: that holder no longer holds read-write access",),
    ),
    Code.PROMOTE_FAILED: _Row(
        _UNAVAIL,
        False,
        "The server failed to grant read-write access",
        ("The server failed to grant read-write access",),
    ),
    # A Take control on a Port that has no seat to take: the two reasons are different
    # facts about the Port, and neither is a failed promote.
    Code.NO_WRITE_SLOT: _Row(
        _UNAVAIL,
        False,
        "Read-write is not available on this port: it has no write slot",
        ("Read-write is not available on this port", "This port has no write slots (capacity: none)"),
    ),
    Code.TAKE_WITHOUT_SLOT: _Row(
        _UNAVAIL,
        False,
        "This port has no single seat to take: every write-entitled client may hold write at once",
        (
            "This port has no single write seat to take",
            "Every write-entitled client may hold write here at the same time",
        ),
    ),
    Code.FEDERATION_DENIED: _Row(
        _UNAVAIL,
        False,
        "Take refused: the origin server did not grant the takeover",
        ("Take refused: the origin server did not grant the takeover",),
    ),
    Code.INTERNAL: _Row(
        _UNAVAIL, False, "The server could not complete the request", ("The server could not complete the request",)
    ),
    Code.UNSUPPORTED_REQUEST: _Row(
        _UNAVAIL,
        False,
        "This server does not support that control request",
        ("This server does not support that control request",),
    ),
    Code.DEMOTED: _Row(
        _NOTICE,
        False,
        "Your read-write access was taken by {actor}",
        ("Your read-write access was taken by {actor}",),
        notice_mode=Mode.READ_ONLY,
    ),
    Code.ACTION_SELF_DEMOTED: _Row(
        _NOTICE,
        False,
        "Your read-write access was set aside to run a Port Action",
        ("Your read-write access was set aside to run a Port Action",),
        notice_mode=Mode.READ_ONLY,
    ),
    Code.ACTION_RESTORED: _Row(
        _NOTICE,
        True,
        "Read-write access restored after the Port Action finished",
        ("Read-write access restored after the Port Action finished",),
        notice_mode=Mode.READ_WRITE,
    ),
    Code.ACTION_RESTORE_DENIED: _Row(
        _NOTICE,
        False,
        "Read-write not restored after the Port Action: another client holds the slot",
        ("Read-write not restored after the Port Action: another client holds the slot",),
        notice_mode=Mode.READ_ONLY,
    ),
}


def _row(code: Code) -> _Row:
    """Return the table row for one code, or raise: an unworded code is a build failure."""
    try:
        return _ROWS[code]
    except (KeyError, TypeError):
        raise ValueError(f"Unknown access decision code: {code!r}") from None


def _as_code(value: Any) -> Code:
    """Coerce a code string to ``Code``, so a fake may hold the plain wire string."""
    return value if isinstance(value, Code) else Code(value)


def _as_mode(value: Any) -> Mode:
    """Coerce a mode word to ``Mode``. An unknown word is a bug, not a default."""
    if isinstance(value, Mode):
        return value
    if value is None:
        raise ValueError("An access decision must state the mode the Client holds afterwards.")
    if value is False or value is True or isinstance(value, (int, float)):
        raise ValueError(f"Unknown access mode: {value!r}")
    return Mode(str(value))


def mode_or_read_only(value: Any) -> Mode:
    """The ``Mode`` for a stored mode value, defaulting to read-only.

    A session stores its mode as a plain string, and an unknown or missing value
    must never be read back as read-write: a surface uses this to report the mode
    a Client already holds, where reading it wrong would hand it write access.
    """
    if isinstance(value, Mode):
        return value
    try:
        return Mode(str(value))
    except (ValueError, TypeError):
        # justification: an unrecognised stored mode is the safer seat, not a grant
        return Mode.READ_ONLY


@dataclass(frozen=True)
class AccessDecision:
    """The result of one Access decision, rendered by any surface.

    Build it through :func:`decide` or :func:`notice`. ``ok``, ``kind``, the wording,
    and the frame keys all come from the code table, so a caller can neither report a
    result it did not get nor write its own sentence.

    Invariants the decision owner must keep:

    * ``mode`` is the mode the Client really holds *after* the decision, read back from
      the Port. A failed release leaves a Client read-write, and a Client told it is
      read-only stops sending keystrokes.
    * ``holders`` and ``capacity`` are a snapshot taken after the change. Build the
      decision and render it; do not hold it across an ``await``.
    * Empty ``holders`` means "nobody holds the slot", not "I did not look".
    """

    code: Code
    mode: Mode
    client_id: str = ""
    port_name: str = ""
    actor: str = ""
    victim: Optional[Holder] = None
    holders: Tuple[Holder, ...] = field(default_factory=tuple)
    capacity: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _as_code(self.code))
        object.__setattr__(self, "mode", _as_mode(self.mode))
        object.__setattr__(self, "holders", tuple(h for h in (self.holders or ()) if isinstance(h, Holder)))
        # Callers pass the port's raw `max_read_write_users`, legacy integers included.
        object.__setattr__(self, "capacity", None if self.capacity is None else wire_to_mode(self.capacity))
        row = _row(self.code)
        if row.kind is Kind.NOTICE and self.mode is not row.notice_mode:
            raise ValueError(f"{self.code.value} is a notice: its mode is fixed to {row.notice_mode.value}.")

    # --- derived facts ---------------------------------------------------------------

    @property
    def ok(self) -> bool:
        """Whether the decision granted its request. Never an input."""
        return _row(self.code).ok

    @property
    def kind(self) -> Kind:
        """Whether this is a grant, a refusal, an unavailable resource, or a notice."""
        return _row(self.code).kind

    @property
    def is_notice(self) -> bool:
        """True when nobody asked: the Client's standing changed under it."""
        return self.kind is Kind.NOTICE

    # --- wording ---------------------------------------------------------------------

    def _values(self) -> Dict[str, str]:
        """The substitutions every row may use."""
        holders = ", ".join(h.label for h in self.holders)
        return {
            "victim_line": f"Taken from: {self.victim.label}" if self.victim else "",
            "holders": holders,
            "actor": self.actor or "another user",
            "port": self.port_name or "this port",
        }

    def _fill(self, template: str) -> str:
        """Fill one template. A line whose only content is empty drops out."""
        return template.format(**self._values())

    @property
    def message(self) -> str:
        """The plain one-line sentence: a close reason, an ``ERROR:CONNECT:`` tail, a log line."""
        sentence, _ = self._wording()
        # Truncate on bytes: a close reason is limited by bytes, and a username may be non-ASCII.
        encoded = self._fill(sentence).replace("\r", " ").replace("\n", " ").encode("utf-8")
        return encoded[:MESSAGE_LIMIT].decode("utf-8", "ignore")

    def _wording(self) -> Tuple[str, Sequence[str]]:
        """(sentence, line templates) for this decision, with the Port's capacity first.

        A Port with no write slot, or with unlimited seats, explains itself better
        than a generic "no free seat" does, so a refusal never names a reason the
        Port's own setting contradicts.
        """
        row = _row(self.code)
        if self.capacity == "none" and row.capacity_none:
            return row.capacity_none, (row.capacity_none,)
        if self.capacity == "multiple" and row.capacity_multiple:
            return row.capacity_multiple, (row.capacity_multiple,)
        return row.sentence, row.lines

    @property
    def lines(self) -> Tuple[str, ...]:
        """The bracketed lines a terminal prints, in order. Empty means stay silent."""
        _, templates = self._wording()
        return tuple(f"[{filled}]" for filled in (self._fill(template) for template in templates) if filled)

    @property
    def text(self) -> str:
        """The Telnet and SSH bytes: ``lines`` joined and CRLF wrapped, or ``""`` when silent."""
        return render_terminal_text(self.lines)

    # --- wire ------------------------------------------------------------------------

    def frame(self) -> Dict[str, Any]:
        """The ``client_mode`` payload for this decision. The keys are this module's to own."""
        payload: Dict[str, Any] = {
            "type": FRAME_MODE,
            "ok": self.ok,
            "mode": self.mode.value,
            "code": self.code.value,
        }
        lines = self.lines
        if lines:
            payload["lines"] = list(lines)
            payload["message"] = self.message
        if self.client_id:
            payload["client_id"] = self.client_id
        if self.holders:
            payload["rw_holders"] = [h.label for h in self.holders]
        if self.capacity is not None:
            payload["max_rw_users"] = capacity_to_wire(self.capacity)
        return payload


def decide(
    code: Code,
    *,
    mode: Mode,
    client_id: str = "",
    port_name: str = "",
    actor: str = "",
    victim: Optional[Holder] = None,
    holders: Sequence[Holder] = (),
    capacity: Optional[Any] = None,
) -> AccessDecision:
    """Answer one request. ``code`` picks the outcome, the kind, and the wording.

    ``mode`` is required: it is the mode the Client holds *after* the decision, read
    back from the Port. Pass the Port's raw ``max_read_write_users`` as ``capacity``.
    """
    row = _row(code)
    if row.kind is Kind.NOTICE:
        raise ValueError(f"{code.value} is a notice: build it with notice(), not decide().")
    return AccessDecision(
        code=code,
        mode=mode,
        client_id=client_id,
        port_name=port_name,
        actor=actor,
        victim=victim,
        holders=holders,
        capacity=capacity,
    )


def notice(
    code: Code,
    *,
    client_id: str = "",
    port_name: str = "",
    actor: str = "",
    victim: Optional[Holder] = None,
    holders: Sequence[Holder] = (),
    capacity: Optional[Any] = None,
) -> AccessDecision:
    """Report an unsolicited change to one Client's standing: a Takeover victim, a MuxCon
    relay of one, or a Port Action demotion and restore.

    A notice takes no ``mode``: the row fixes it, so a relay cannot send a mode the Port
    did not grant. ``actor`` absent renders "another user", which is what a MuxCon relay
    legitimately knows. The producer keeps the decision about whom to notify.
    """
    row = _row(code)
    if row.kind is not Kind.NOTICE:
        raise ValueError(f"{code.value} answers a request: build it with decide(), not notice().")
    return AccessDecision(
        code=code,
        mode=row.notice_mode,
        client_id=client_id,
        port_name=port_name,
        actor=actor,
        victim=victim,
        holders=holders,
        capacity=capacity,
    )


def holders_of(records: Optional[Iterable[Mapping[str, Any]]], *, ip_of: Callable[[str], str]) -> Tuple[Holder, ...]:
    """Build the Write slot holders of one Port from its ``connected_clients`` records.

    Replaces the three label builders this module deletes. ``ip_of`` is the caller's own
    resolver, because which adapter owns a Client is not this module's business. Broken
    entries are skipped: a bad record must not hide who holds the slot.
    """
    out: List[Holder] = []
    for record in records or ():
        if not isinstance(record, dict) or record.get("mode") != Mode.READ_WRITE.value:
            continue
        client_id = str(record.get("client_id") or "")
        username = str(record.get("username") or "unknown")
        try:
            ip = str(ip_of(client_id) or "unknown")
        except Exception:
            ip = "unknown"
        out.append(Holder(client_id=client_id, username=username, ip=ip))
    return tuple(out)


def holders_frame(holders: Sequence[Holder], capacity: Optional[Any] = None) -> Dict[str, Any]:
    """The answer to ``query_rw_holders`` and the Control menu ``w`` command."""
    frame: Dict[str, Any] = {"type": FRAME_HOLDERS, "holders": [h.label for h in holders]}
    if capacity is not None:
        frame["max_rw_users"] = capacity_to_wire(capacity)
    return frame


def presence_frame(viewers: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """The broadcast behind the viewer badge (issue #48).

    A viewer list is not an answer to a request, so it carries no Mode and no wording: a
    console that read it as an Access decision would set the Mode of its own Client.
    """
    return {"type": FRAME_PRESENCE, "viewers": [dict(v) for v in viewers]}
