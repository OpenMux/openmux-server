"""Test-side projections from ``AccessDecision`` to the tuples older tests assert.

Issue #98 replaced the ``(flag, reason)`` tuples these producers returned with one
``AccessDecision``. Each helper here reads the fields of a real decision, so a test
written against the old tuple still asserts the same fact about the same object. The
outcome code keeps the string spelling it had on the wire (ADR-0001), which is why the
old ``reason`` comparisons still hold.

Use these only to keep an unrelated test readable. A test about the decision itself
asserts ``decision.code``, ``.mode``, ``.victim``, ``.lines``, or ``.frame()`` directly.
"""

from dataclasses import replace
from typing import Any, Dict, Optional, Tuple

from openmux.server.access_decision import AccessDecision, holders_frame, holders_of


def attach_result(decision: AccessDecision) -> Tuple[bool, Optional[str], Optional[str]]:
    """Project an attach decision onto ``(ok, mode, reason)``.

    ``mode`` and ``reason`` are None on the paths that used to send None: a refusal
    reports no mode, and a grant reports no reason.
    """
    if not decision.ok:
        return False, None, decision.code.value
    return True, decision.mode.value, None


def ladder_result(decision: AccessDecision) -> Tuple[Optional[str], Optional[str]]:
    """Project an access-ladder decision onto ``(mode, deny_reason)``."""
    if not decision.ok:
        return None, decision.code.value
    return decision.mode.value, None


def take_result(decision: AccessDecision) -> Tuple[bool, str]:
    """Project a takeover decision onto ``(ok, reason)``.

    A test about the displaced holder reads ``decision.victim`` instead: the takeover
    line a test used to match on is now the victim's ``label``.
    """
    return decision.ok, decision.code.value


def ok(decision: Any) -> bool:
    """Whether a promote or release decision granted its request."""
    return decision.ok


def holders_frame_of(port: Any) -> Dict[str, Any]:
    """The holders frame a ``ConsoleManager`` answers for `port` (issue #98).

    The adapter asks its console manager for this frame, so a fake that builds it here
    replies with the frame the real one builds. The real class resolves an IP through
    the adapter owning each Client, which a fake owns none of, so every holder reads
    ``unknown``.
    """
    records = getattr(port, "connected_clients", None) or []
    holders = holders_of(records, ip_of=lambda client_id: "unknown")
    return holders_frame(holders, getattr(port, "max_read_write_users", None))


def with_port_state(decision: AccessDecision, port: Any, client_id: str = "") -> AccessDecision:
    """Say who holds the Write slot of `port`, as ``ConsoleManager`` puts it on a decision.

    A fake console manager answers with a canned decision. A test that shows holders or
    the capacity needs the two fields the real class fills from the Port, plus the
    requesting client id (issue #98).
    """
    return replace(
        decision,
        client_id=decision.client_id or client_id,
        holders=holders_of(getattr(port, "connected_clients", None) or [], ip_of=lambda cid: "unknown"),
        capacity=getattr(port, "max_read_write_users", None),
    )
