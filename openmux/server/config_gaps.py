"""Report the gap between a config file and the running server (issue #105).

A reload can leave part of a config file unapplied: a port the adapter refuses to build, a
schema violation in a key the server tolerated, or an adapter that did not start. Before this
module, each of those left only a single log line between the startup banner and the next
message, and a refused port was reported as ``added`` on every later reload.

The module has two jobs:

* :func:`violations_from_summary` turns a reload summary into a list of :class:`ConfigGap`.
* :func:`log_gap_warning` prints the whole list as one block where every line carries
  ``CONFIG_GAP``, so a log viewer finds the entire block with one search.

This module must not import the server. It only formats, which keeps it usable from the
adapters, ``main.py``, and the tests without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Set, Tuple

# Every physical line of the warning block carries this token. Use it to grep the block.
GAP_MARKER = "CONFIG_GAP"

_RULE_FILL = "!" * 66
_UNKNOWN_ERROR = "no reason recorded"


@dataclass(frozen=True)
class ConfigGap:
    """One config entry that is present in a file but not applied to the running server.

    Attributes:
        section: Where the entry lives: an adapter kind such as ``serial``, an adapter or phase name
            such as ``serial_ports`` for a reload failure, or ``schema`` for a schema violation whose
            path already names its section.
        port: Port name, or None when the gap is not tied to one port.
        error: Why the entry did not apply, in the words of the code that refused it.
        kept_previous: True when the previously running port was kept, so the live console is
            still serving the old config. The warning says so in plain words.
    """

    section: str
    port: Optional[str] = None
    error: str = ""
    kept_previous: bool = False

    def describe(self) -> str:
        """Return the one-line description shown inside the warning block."""
        where = f"{self.section}/{self.port}" if self.port else self.section
        reason = (self.error or _UNKNOWN_ERROR).strip() or _UNKNOWN_ERROR
        outcome = "  -> kept the running port; it still serves the previous config" if self.kept_previous else ""
        return f"{where}: {reason}{outcome}"


# A gap report is logged with extra={"config_gap": True}, so a log filter can select the blocks
# by record.config_gap without matching on the marker text.


def _extract_adapter_gaps(section: str, result: Any, gaps: List[ConfigGap]) -> None:
    """Collect gaps from one adapter's reconcile result, as a soft reload stores it.

    Soft reload stores the result a reconcile call returned, keyed by adapter kind. Full reload
    reports what failed under ``"errors"`` of its own summary, which
    :func:`violations_from_summary` reads through :func:`_gap_from_reload_error`.
    """
    if not isinstance(result, dict):
        return
    for violation in _as_list(result.get("violations")):
        gaps.append(ConfigGap(section=section, error=_text(violation)))
    detail = result.get("failed_detail")
    detail = detail if isinstance(detail, dict) else {}
    for port_name in _as_list(result.get("failed")) + _as_list(result.get("refused")):
        name = _text(port_name)
        entry = detail.get(name)
        entry = entry if isinstance(entry, dict) else {}
        gaps.append(
            ConfigGap(
                section=section,
                port=name,
                error=_text(entry.get("error")),
                kept_previous=bool(entry.get("kept_previous")) or name in _as_list(result.get("kept_previous")),
            )
        )
    for port_name in _as_list(result.get("kept_previous")):
        name = _text(port_name)
        if name not in {g.port for g in gaps if g.section == section}:
            gaps.append(ConfigGap(section=section, port=name, error=_UNKNOWN_ERROR, kept_previous=True))
    error = result.get("error")
    if error:
        gaps.append(ConfigGap(section=section, error=_text(error)))


def unique_gaps(gaps: Sequence[ConfigGap]) -> List[ConfigGap]:
    """Return ``gaps`` without repeats, keeping the order they were reported in.

    A port entry is identified by its adapter kind and name; a report that names no port is
    identified by its text as well, so two different problems in one section both survive.

    Args:
        gaps: Gaps from one or more sources, usually adapter state and a reload summary.

    Returns:
        List[ConfigGap]: The gaps, each listed once.
    """
    seen: Set[Tuple[str, Optional[str], str]] = set()
    unique: List[ConfigGap] = []
    for gap in gaps:
        key = (gap.section, gap.port, gap.error if gap.port is None else "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(gap)
    return unique


def violations_from_summary(summary: Any) -> List[ConfigGap]:
    """Return every gap reported by a reload summary.

    Args:
        summary: The summary dict built by ``reload_adapters_soft()`` or
            ``reload_adapters_full()``. Anything else yields no gaps.

    Returns:
        List[ConfigGap]: Gaps in the order reported, without duplicates for the same
        adapter kind and port. A list is empty when the reload applied everything.
    """
    if not isinstance(summary, dict):
        return []
    gaps: List[ConfigGap] = []
    for violation in _as_list(summary.get("config_violations")) + _as_list(summary.get("violations")):
        gaps.append(ConfigGap(section="schema", error=_text(violation)))
    # A full reload summary records one entry per adapter or phase that failed.
    for problem in _as_list(summary.get("errors")):
        gaps.append(_gap_from_reload_error(problem))
    adapters = summary.get("adapters")
    if isinstance(adapters, dict):
        for section, result in adapters.items():
            _extract_adapter_gaps(_text(section) or "unknown", result, gaps)
    return unique_gaps(gaps)


def format_warning(where: str, gaps: Sequence[ConfigGap], source: Optional[str] = None) -> str:
    """Return the warning block for a set of gaps, or "" when there is nothing to report.

    Args:
        where: What produced the gaps, for example ``"boot"`` or ``"soft reload"``.
        gaps: The gaps to list, usually from :func:`violations_from_summary`.
        source: Config file the gaps refer to, when one file is the source.

    Returns:
        str: A block whose every line starts with :data:`GAP_MARKER`, or an empty string when
        ``gaps`` is empty. The block is not indented.
    """
    if not gaps:
        return ""
    file_part = f" in {source}" if source else ""
    lines: List[str] = [
        f"{GAP_MARKER} {_RULE_FILL}",
        f"{GAP_MARKER} WARNING: {where} left part of the config{file_part} unapplied ({len(gaps)} item(s)):",
    ]
    lines.extend(f"{GAP_MARKER}   {gap.describe()}" for gap in gaps)
    lines.extend(
        [
            f"{GAP_MARKER} A listed port is not running and has no console, so plugging a device in",
            f"{GAP_MARKER} will not bring it up. Fix the file, then reload again. A port marked",
            f'{GAP_MARKER} "kept" still serves its previous config, so its users are not disconnected.',
        ]
    )
    lines.append(f"{GAP_MARKER} {_RULE_FILL}")
    return "\n".join(lines)


def log_gap_warning(logger: Any, where: str, gaps: Sequence[ConfigGap], source: Optional[str] = None) -> bool:
    """Log the gap block through ``logger`` at WARNING level.

    Args:
        logger: Logger of the caller (server or adapter logger).
        where: What produced the gaps, for example ``"boot"`` or ``"soft reload"``.
        gaps: The gaps to report.
        source: Config file the gaps refer to, when one file is the source.

    Returns:
        bool: True when a block was logged, False when there was nothing to report.
    """
    text = format_warning(where, gaps, source=source)
    if not text:
        return False
    logger.warning(text, extra={"config_gap": True})
    return True


def _as_list(value: Any) -> List[Any]:
    """Return ``value`` as a list, treating None as empty and a scalar as one item."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _text(value: Any) -> str:
    """Return a value as stripped text, with None rendered as an empty string."""
    if value is None:
        return ""
    return str(value).strip()


# The keys ``reload_adapters_full()`` writes into one "errors" entry. The first three carry a reason
# in the words of the code that failed; a timeout entry carries only a number, so it gets a sentence.
_ERROR_REASON_KEYS = ("error", "start_error", "stop_error", "wire_error")
_ERROR_TIMEOUT_KEYS = (("start_timeout", "start timed out after"), ("stop_timeout", "stop timed out after"))


def _gap_from_reload_error(problem: Any) -> ConfigGap:
    """Return the gap for one entry of a full reload summary's ``"errors"`` list.

    ``reload_adapters_full()`` appends one dict per failure, named by the adapter it was stopping,
    wiring, or starting, or by the phase that failed: ``{"adapter": "serial_ports", "start_error":
    "start returned False"}`` or ``{"phase": "create_adapters", "error": "..."}``. The gap is named
    after that adapter or phase, so the block says which part of the file did not apply.

    A plain string is kept as its own reason. A dict this function does not recognise is reported
    verbatim rather than dropped, so an unexpected shape still reaches the operator.
    """
    if not isinstance(problem, dict):
        return ConfigGap(section="adapter", error=_text(problem))
    where = _text(problem.get("adapter")) or _text(problem.get("phase")) or "adapter"
    for key in _ERROR_REASON_KEYS:
        if key in problem:
            return ConfigGap(section=where, error=_text(problem.get(key)))
    for key, sentence in _ERROR_TIMEOUT_KEYS:
        if key in problem:
            return ConfigGap(section=where, error=f"{sentence} {_text(problem.get(key))}s")
    return ConfigGap(section="adapter", error=_text(problem))


__all__ = [
    "GAP_MARKER",
    "ConfigGap",
    "format_warning",
    "log_gap_warning",
    "unique_gaps",
    "violations_from_summary",
]
