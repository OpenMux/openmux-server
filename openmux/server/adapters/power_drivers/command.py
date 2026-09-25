"""``driver: command`` - PDU backend that runs CLI commands.

Lets the user wire any CLI tool into OpenMux power control: GPIO scripts
on a Raspberry Pi, custom USB power devices, or any other device the
user can address with a shell command. No shell is used: commands are
parsed with ``shlex.split`` and spawned as plain process groups.

Two command levels are supported, per-outlet values win:

* PDU level (in ``options``): ``on_cmd`` / ``off_cmd`` templates that
  must contain the literal ``{outlet_id}`` placeholder, and one
  ``state_cmd`` that reports every outlet as ``<id> <state>`` lines.
* Outlet level (on an ``outlets`` entry): ``on_cmd`` / ``off_cmd`` and a
  single-outlet ``state_cmd`` whose output is one state token (or a
  ``state_pattern`` regex).

Placeholders: ``{outlet_id}`` (the outlet id) and ``{outlet_index}`` (the
device index/name on the ``index`` key of the outlet, when it differs from
the id) may appear in on/off commands; ``{host}``, ``{username}`` and
``{password}`` map to the PDU-level options of the same name and are also
exported to the child environment as ``PDU_HOST`` / ``PDU_USERNAME`` /
``PDU_PASSWORD``. A PDU-level ``state_pattern`` (a regex with named
``id`` / ``value`` groups) parses ``state_cmd`` output line by line;
``state_token_on`` / ``state_token_off`` extend the state-token table.

Safety: every command runs in its own session/group and is killed as a
group (plus a bounded reap) when its ``timeout`` elapses; per-outlet
state commands run under a bounded semaphore (``max_parallel``); the
shared :class:`~.readbackoff.ReadBackoff` suppresses device IO after
device-wide read failures. Explicit switches are never backoff-throttled.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import signal
from typing import Any, Dict, List, Optional, Pattern, Set, Tuple

from .api import OutletReading, PduDriver
from .readbackoff import ReadBackoff

logger = logging.getLogger(__name__)

# Defaults and limits.
DEFAULT_TIMEOUT = 5.0
DEFAULT_MAX_PARALLEL = 4
MAX_TIMEOUT = 300.0
MAX_PARALLEL_LIMIT = 64
# Bounded wait after SIGKILL to the process group (reap the child).
_MAX_REAP_WAIT = 3.0
# How much stderr is quoted in failure messages.
_STDERR_TAIL = 200

# Default state tokens accepted on the token parse; the PDU-level
# state_token_on / state_token_off options extend these per driver.
_ON_TOKENS = ("on", "1", "true", "yes")
_OFF_TOKENS = ("off", "0", "false", "no")

# Placeholders substituted on resolved command strings at startup.
_PLACEHOLDER_ID = "{outlet_id}"
_PLACEHOLDER_INDEX = "{outlet_index}"
# Environment variables exported to spawned commands for the PDU-level
# connection fields (host / username / password), when configured.
_ENV_HOST = "PDU_HOST"
_ENV_USERNAME = "PDU_USERNAME"
_ENV_PASSWORD = "PDU_PASSWORD"

# Minimal environment allow-list (same base as the command adapter).
_ENV_ALLOWLIST = ("PATH", "HOME", "SHELL", "USER", "LANG", "LC_ALL")

# Valid option keys for this driver: PDU level (in ``options``) and
# per-outlet (on an ``outlets`` entry).
_KNOWN_OPTIONS = frozenset(
    {
        "outlets",
        "on_cmd",
        "off_cmd",
        "state_cmd",
        "state_pattern",
        "state_token_on",
        "state_token_off",
        "host",
        "username",
        "password",
        "cwd",
        "timeout",
        "env",
        "max_parallel",
    }
)
_KNOWN_OUTLET_KEYS = frozenset({"id", "index", "on_cmd", "off_cmd", "state_cmd", "state_pattern"})


class _CmdResult:
    """Outcome of one spawned command."""

    __slots__ = ("exit_code", "stdout", "stderr", "timed_out")

    def __init__(self, exit_code: Optional[int], stdout: str, stderr: str, timed_out: bool):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def _state_token(text: str, on_tokens: frozenset, off_tokens: frozenset) -> Optional[bool]:
    """Map one state token to True/False; None when unrecognized."""
    tok = text.strip().lower()
    if tok in on_tokens:
        return True
    if tok in off_tokens:
        return False
    return None


def _stderr_tail(stderr: str) -> str:
    tail = stderr.strip()[-_STDERR_TAIL:]
    return f" (stderr: {tail})" if tail else ""


def _failure_text(result: _CmdResult) -> str:
    if result.timed_out:
        return "state command timed out"
    return f"state command failed (exit {result.exit_code}){_stderr_tail(result.stderr)}"


def _parse_single_token(stdout: str, on_tokens: frozenset, off_tokens: frozenset) -> Tuple[Optional[bool], str]:
    """Parse single-outlet state output: first non-empty line, token match."""
    for line in stdout.splitlines():
        if not line.strip():
            continue
        state = _state_token(line, on_tokens, off_tokens)
        if state is not None:
            return state, ""
        return None, f"unrecognized state output: {line.strip()[:80]!r}"
    return None, "state command printed no output"


def _parse_single_pattern(stdout: str, pattern: Pattern) -> Tuple[Optional[bool], str]:
    """Parse single-outlet output with a regex.

    A pattern with named ``on``/``off`` groups requires exactly one
    match. A plain pattern (no such groups) means: match = on, no
    match = off.
    """
    named = "on" in pattern.groupindex or "off" in pattern.groupindex
    match = pattern.search(stdout)
    if not named:
        return (True if match is not None else False), ""
    on_hit = match is not None and match.group("on") is not None
    off_hit = match is not None and match.group("off") is not None
    if on_hit == off_hit:
        if match is None:
            return None, "state pattern matched nothing"
        return None, "state pattern matched both on and off"
    return on_hit, ""


def _parse_single(
    stdout: str, pattern: Optional[Pattern], on_tokens: frozenset, off_tokens: frozenset
) -> Tuple[Optional[bool], str]:
    if pattern is not None:
        return _parse_single_pattern(stdout, pattern)
    return _parse_single_token(stdout, on_tokens, off_tokens)


def _parse_batch_lines(
    stdout: str, pattern: Optional[Pattern], on_tokens: frozenset, off_tokens: frozenset
) -> Dict[str, bool]:
    """Parse PDU-level state output: one line per outlet.

    Each line is either matched against ``pattern`` (its named ``id`` and
    ``value`` groups) or split on whitespace into ``<id> <state>``. The
    line id is a device identity (an outlet ``id`` or ``index``); the
    value is classified by the state-token table. Lines that do not
    parse are skipped; the first line per device identity wins. The
    result maps device identity to state.
    """
    states: Dict[str, bool] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        if pattern is not None:
            match = pattern.search(line)
            if match is None:
                continue
            line_id = match.group("id")
            value = match.group("value")
        else:
            parts = line.split()
            if len(parts) < 2:
                continue
            line_id = parts[0]
            value = parts[1]
        state = _state_token(value, on_tokens, off_tokens)
        if state is None or line_id in states:
            continue
        states[line_id] = state
    return states


def _command_text(value: Any, label: str) -> Optional[str]:
    """Validate an optional command string option; None when absent."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"command driver: {label} must be a non-empty string")
    return value.strip()


def _template_text(value: Any, label: str) -> Optional[str]:
    text = _command_text(value, label)
    if text is not None and _PLACEHOLDER_ID not in text and _PLACEHOLDER_INDEX not in text:
        raise ValueError(
            f"command driver: {label} template must contain {str(_PLACEHOLDER_ID)!r} or {str(_PLACEHOLDER_INDEX)!r}"
        )
    return text


def _validate_host(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"command driver: host must be a non-empty string, got {value!r}")
    return value.strip()


def _validate_credential(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"command driver: {label} must be a string, got {value!r}")
    return value


def _validate_token_list(value: Any, label: str) -> List[str]:
    """Validate an option adding state tokens (state_token_on / _off).

    Returns the lower-cased, de-duplicated tokens. Rejects a non-list, an
    empty list, a non-string entry, or an entry that is only whitespace.
    """
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"command driver: {label} must be a list of strings")
    tokens: List[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"command driver: {label} entries must be non-empty strings")
        tok = entry.strip().lower()
        if tok not in tokens:
            tokens.append(tok)
    if not tokens:
        raise ValueError(f"command driver: {label} must not be empty")
    return tokens


def _parse_batch_pattern(value: Any, has_state_cmd: bool) -> Optional[Pattern]:
    """Validate the PDU-level batch state_pattern; None when absent.

    The pattern is a line regex with the named groups ``id`` (the outlet's
    device identity: its ``id`` or ``index``) and ``value`` (classifier by
    the state-token table). Non-matching lines are skipped.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("command driver: state_pattern must be a non-empty string")
    try:
        pattern = re.compile(value)
    except re.error as exc:
        raise ValueError(f"command driver: state_pattern does not compile: {exc}")
    missing = [g for g in ("id", "value") if g not in pattern.groupindex]
    if missing:
        raise ValueError(f"command driver: state_pattern needs named group(s): {', '.join(missing)}")
    if not has_state_cmd:
        raise ValueError("command driver: state_pattern requires a state_cmd")
    return pattern


def _validate_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"command driver: timeout must be a number, got {value!r}")
    timeout = float(value)
    if not 0 < timeout <= MAX_TIMEOUT:
        raise ValueError(f"command driver: timeout must be in (0, {MAX_TIMEOUT:g}], got {value!r}")
    return timeout


def _validate_max_parallel(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"command driver: max_parallel must be an integer, got {value!r}")
    if not 1 <= value <= MAX_PARALLEL_LIMIT:
        raise ValueError(f"command driver: max_parallel must be in [1, {MAX_PARALLEL_LIMIT}], got {value!r}")
    return value


def _validate_cwd(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"command driver: cwd must be a non-empty string, got {value!r}")
    return value


def _validate_env(value: Any) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("command driver: env must be a mapping of variable names to strings")
    return {str(k): ("" if v is None else str(v)) for k, v in value.items()}


def _parse_id(entry: Any, seen: Set[str]) -> str:
    """Validate one outlet entry's id; return its normalized string form."""
    if not isinstance(entry, dict):
        raise ValueError("command driver: each outlet entry must be a mapping")
    raw_id = entry.get("id")
    if raw_id is None or not str(raw_id).strip():
        raise ValueError("command driver: each outlet entry needs a non-empty 'id'")
    oid = str(raw_id).strip()
    if "." in oid or any(ch.isspace() for ch in oid):
        raise ValueError(f"command driver: outlet id {oid!r} must not contain dots or whitespace")
    if oid in seen:
        raise ValueError(f"command driver: duplicate outlet id {oid!r}")
    return oid


def _parse_pattern(entry: Dict[str, Any], oid: str, has_state_cmd: bool) -> Optional[Pattern]:
    raw_pattern = entry.get("state_pattern")
    if raw_pattern is None:
        return None
    if not isinstance(raw_pattern, str) or not raw_pattern.strip():
        raise ValueError(f"command driver: outlet {oid!r} state_pattern must be a non-empty string")
    try:
        pattern = re.compile(raw_pattern)
    except re.error as exc:
        raise ValueError(f"command driver: outlet {oid!r} state_pattern does not compile: {exc}")
    if not has_state_cmd:
        raise ValueError(f"command driver: outlet {oid!r} state_pattern requires a state_cmd")
    return pattern


def _split_command(command: str, label: str) -> List[str]:
    """Pre-split a configured command so quote errors show at startup."""
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"command driver: {label}: {exc}")
    if not argv:
        raise ValueError(f"command driver: {label}: no command parts after splitting")
    return argv


class CommandDriver(PduDriver):
    """PDU backend that runs one CLI command per operation.

    Every command runs in a fresh process group (``start_new_session``)
    and is killed as a group when ``timeout`` elapses, so a user script
    cannot outlive the timeout with its children attached to our pipes.
    No device IO is spawned while the driver is in its read backoff.
    """

    def __init__(self, options: Optional[Dict[str, Any]] = None):
        opts = dict(options or {})
        unknown = set(opts) - _KNOWN_OPTIONS
        if unknown:
            raise ValueError(f"command driver: unknown option(s): {sorted(unknown)}")
        self._timeout = _validate_timeout(opts.get("timeout", DEFAULT_TIMEOUT))
        self._max_parallel = _validate_max_parallel(opts.get("max_parallel", DEFAULT_MAX_PARALLEL))
        self._cwd = _validate_cwd(opts.get("cwd"))
        self._conn: Dict[str, str] = {}
        host = _validate_credential(opts.get("host"), "host")
        if host is not None and not host.strip():
            raise ValueError("command driver: host must not be blank")
        for key in ("host", "username", "password"):
            value = _validate_credential(opts.get(key), key)
            if value is not None:
                self._conn[key] = value
        self._validate_connection(opts)
        self._env = self._build_env(_validate_env(opts.get("env")))
        token_on = _validate_token_list(opts.get("state_token_on"), "state_token_on")
        token_off = _validate_token_list(opts.get("state_token_off"), "state_token_off")
        self._on_tokens = frozenset(_ON_TOKENS) | frozenset(token_on)
        self._off_tokens = frozenset(_OFF_TOKENS) | frozenset(token_off)
        overlap = self._on_tokens & self._off_tokens
        if overlap:
            raise ValueError(f"command driver: state token(s) in both on and off lists: {sorted(overlap)}")
        pdu_on = _template_text(opts.get("on_cmd"), "on_cmd")
        pdu_off = _template_text(opts.get("off_cmd"), "off_cmd")
        batch_state = _command_text(opts.get("state_cmd"), "state_cmd")
        outlets = opts.get("outlets")
        if not isinstance(outlets, (list, tuple)) or not outlets:
            raise ValueError("command driver: options.outlets must be a non-empty list")
        if batch_state and _PLACEHOLDER_INDEX in batch_state:
            raise ValueError(f"command driver: state_cmd must not contain {str(_PLACEHOLDER_INDEX)!r}")
        self._ids: List[str] = []
        self._index_by_id: Dict[str, str] = {}
        self._on_cmds: Dict[str, List[str]] = {}
        self._off_cmds: Dict[str, List[str]] = {}
        self._state_cmds: Dict[str, Optional[List[str]]] = {}
        self._state_patterns: Dict[str, Optional[Pattern]] = {}
        seen: Set[str] = set()
        for entry in outlets:
            self._add_outlet(entry, seen, pdu_on, pdu_off)
        self._batch_state_argv = _split_command(batch_state, "state_cmd") if batch_state else None
        self._batch_state_pattern = _parse_batch_pattern(opts.get("state_pattern"), batch_state is not None)
        self._batch_id_map = self._build_batch_id_map()
        self._last_state: Dict[str, Optional[bool]] = {oid: None for oid in self._ids}
        self._backoff = ReadBackoff("command-driver")
        self._lock = asyncio.Lock()
        self.online = True

    def _validate_connection(self, opts: Dict[str, Any]) -> None:
        """Enforce host when any command references the connection fields."""
        if not self._conn.get("host"):
            commands = [
                opts.get("on_cmd"),
                opts.get("off_cmd"),
                opts.get("state_cmd"),
            ]
            for entry in opts.get("outlets") or []:
                if isinstance(entry, dict):
                    commands.extend((entry.get("on_cmd"), entry.get("off_cmd"), entry.get("state_cmd")))
            if any(
                isinstance(cmd, str) and any(field in cmd for field in ("{host}", "{username}", "{password}"))
                for cmd in commands
            ):
                raise ValueError("command driver: commands use {host}/{username}/{password} but no host is set")

    def _build_env(self, extra: Dict[str, str]) -> Dict[str, str]:
        """Sanitized base environment merged with the user's ``env``.

        The user's values win, so a custom env key (for example a driver
        specific credential name) can override the PDU_* defaults. When a
        PDU_* default is not overridden, the matching connection field is
        exported.
        """
        env: Dict[str, str] = {}
        for key in _ENV_ALLOWLIST:
            value = os.environ.get(key)
            if value:
                env[key] = value
        env.update(extra)
        for key, var in (("host", _ENV_HOST), ("username", _ENV_USERNAME), ("password", _ENV_PASSWORD)):
            value = self._conn.get(key)
            if value is not None and var not in env:
                env[var] = value
        return env

    def _add_outlet(self, entry: Any, seen: Set[str], pdu_on: Optional[str], pdu_off: Optional[str]) -> None:
        oid = _parse_id(entry, seen)
        seen.add(oid)
        unknown = set(entry) - _KNOWN_OUTLET_KEYS
        if unknown:
            raise ValueError(f"command driver: outlet {oid!r} has unknown key(s): {sorted(unknown)}")
        index = entry.get("index")
        if index is not None:
            if not isinstance(index, str) or not index.strip():
                raise ValueError(f"command driver: outlet {oid!r} index must be a non-empty string")
            index = index.strip()
            if index in seen:
                raise ValueError(f"command driver: outlet {oid!r} index {index!r} collides with an outlet id or index")
            self._index_by_id[oid] = index
            seen.add(index)
        on_raw = _command_text(entry.get("on_cmd"), f"outlet {oid!r} on_cmd")
        off_raw = _command_text(entry.get("off_cmd"), f"outlet {oid!r} off_cmd")
        state_raw = _command_text(entry.get("state_cmd"), f"outlet {oid!r} state_cmd")
        if on_raw is None and pdu_on is None:
            raise ValueError(f"command driver: outlet {oid!r} needs on_cmd (per-outlet or PDU-level template)")
        if off_raw is None and pdu_off is None:
            raise ValueError(f"command driver: outlet {oid!r} needs off_cmd (per-outlet or PDU-level template)")
        pattern = _parse_pattern(entry, oid, state_raw is not None)
        on_text = on_raw if on_raw is not None else pdu_on
        off_text = off_raw if off_raw is not None else pdu_off
        index_text = self._index_by_id.get(oid)
        self._ids.append(oid)
        self._on_cmds[oid] = _split_command(self._substitute(on_text, oid, index_text), f"outlet {oid!r} on_cmd")
        self._off_cmds[oid] = _split_command(self._substitute(off_text, oid, index_text), f"outlet {oid!r} off_cmd")
        self._state_cmds[oid] = _split_command(state_raw, f"outlet {oid!r} state_cmd") if state_raw else None
        self._state_patterns[oid] = pattern

    def _substitute(self, text: str, oid: str, index_text: Optional[str]) -> str:
        """Resolve every placeholder in one command string at startup."""
        result = text.replace(_PLACEHOLDER_ID, oid)
        if _PLACEHOLDER_INDEX in result:
            if index_text is None:
                raise ValueError(f"command driver: outlet {oid!r} uses {_PLACEHOLDER_INDEX} but has no index")
            result = result.replace(_PLACEHOLDER_INDEX, index_text)
        for key, placeholder in (
            ("host", "{host}"),
            ("username", "{username}"),
            ("password", "{password}"),
        ):
            if placeholder in result:
                if key in self._conn:
                    result = result.replace(placeholder, self._conn[key])
                else:
                    raise ValueError(f"command driver: outlet {oid!r} uses {placeholder!r} but {key} is not set")
        return result

    def _build_batch_id_map(self) -> Dict[str, str]:
        """Map each device identity (outlet id and index) to the outlet id."""
        batch_map: Dict[str, str] = {}
        for oid in self._ids:
            batch_map[oid] = oid
            index = self._index_by_id.get(oid)
            if index is not None:
                batch_map[index] = oid
        return batch_map

    async def list_outlets(self) -> List[str]:
        return list(self._ids)

    async def read_states(self) -> Optional[Dict[str, OutletReading]]:
        async with self._lock:
            if self._backoff.in_backoff():
                # Inside the device-wide failure window: no new IO.
                return None
            results, batch_result = await self._spawn_reads()
            return self._merge_reads(results, batch_result)

    async def _spawn_reads(self) -> Tuple[Dict[str, _CmdResult], Optional[_CmdResult]]:
        """Run this poll's state commands; return (per-outlet, batch) results.

        Per-outlet commands run concurrently under the ``max_parallel``
        semaphore; the batch command (when it covers any outlet that has
        no per-outlet state command) runs once per poll.
        """
        individual = [oid for oid in self._ids if self._state_cmds.get(oid)]
        results: Dict[str, _CmdResult] = {}
        if individual:
            semaphore = asyncio.Semaphore(self._max_parallel)

            async def _one(oid: str) -> Tuple[str, _CmdResult]:
                async with semaphore:
                    return oid, await self._run(self._state_cmds[oid])

            results = dict(await asyncio.gather(*(_one(oid) for oid in individual)))
        batch_result: Optional[_CmdResult] = None
        if self._batch_state_argv is not None and len(individual) < len(self._ids):
            batch_result = await self._run(self._batch_state_argv)
        return results, batch_result

    def _merge_reads(
        self, results: Dict[str, _CmdResult], batch_result: Optional[_CmdResult]
    ) -> Optional[Dict[str, OutletReading]]:
        """Apply a poll's command results to readings and the backoff.

        A device-wide failure (the batch command failed, or every spawned
        state command failed) starts the backoff window and returns None
        (the adapter then keeps the last-known readings).
        """
        spawned = list(results.values())
        if batch_result is not None:
            spawned.append(batch_result)
        if spawned and all(not r.ok for r in spawned):
            self._backoff.note_failure()
            return None
        if spawned:
            self._backoff.note_success()
        batch_ids = self._parse_batch_ids(batch_result)
        batch_map = {self._batch_id_map[k]: v for k, v in batch_ids.items() if k in self._batch_id_map}
        batch_ok = batch_result is not None and batch_result.ok
        return self._build_readings(results, batch_map, batch_ok)

    def _parse_batch_ids(self, batch_result: Optional[_CmdResult]) -> Dict[str, bool]:
        """Parse a batch state command into device-identity -> state."""
        if batch_result is None or not batch_result.ok:
            return {}
        return _parse_batch_lines(batch_result.stdout, self._batch_state_pattern, self._on_tokens, self._off_tokens)

    def _build_readings(
        self,
        results: Dict[str, _CmdResult],
        batch_map: Dict[str, bool],
        batch_ok: bool,
    ) -> Dict[str, OutletReading]:
        readings: Dict[str, OutletReading] = {}
        for oid in self._ids:
            if oid in results:
                readings[oid] = self._reading_from_single(oid, results[oid])
            elif oid in batch_map:
                self._last_state[oid] = batch_map[oid]
                readings[oid] = OutletReading(on=batch_map[oid])
            elif batch_ok:
                readings[oid] = self._stale_reading(oid, "outlet not reported by state command")
            else:
                readings[oid] = self._stale_reading(oid, "")
        return readings

    def _reading_from_single(self, oid: str, result: _CmdResult) -> OutletReading:
        if result.ok:
            state, error = _parse_single(result.stdout, self._state_patterns.get(oid), self._on_tokens, self._off_tokens)
            if state is not None:
                self._last_state[oid] = state
                return OutletReading(on=state)
            return self._stale_reading(oid, error)
        return self._stale_reading(oid, _failure_text(result))

    def _stale_reading(self, oid: str, error: str) -> OutletReading:
        """Reading that keeps the last known state, with an error note."""
        return OutletReading(on=self._last_state.get(oid), error=error)

    async def set_state(self, outlet_id: str, on: bool) -> OutletReading:
        if outlet_id not in self._ids:
            raise ValueError(f"unknown outlet {outlet_id!r}")
        async with self._lock:
            argv = self._on_cmds[outlet_id] if on else self._off_cmds[outlet_id]
            result = await self._run(argv)
            if not result.ok:
                if result.timed_out:
                    raise RuntimeError(f"power switch timed out after {self._timeout:g}s")
                raise RuntimeError(f"power switch failed (exit {result.exit_code}){_stderr_tail(result.stderr)}")
            self._last_state[outlet_id] = bool(on)
            return OutletReading(on=bool(on))

    async def _run(self, argv: List[str]) -> _CmdResult:
        """Spawn one command in its own group; kill the group on timeout."""
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
            start_new_session=True,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=self._timeout)
            return _CmdResult(
                process.returncode, stdout_b.decode("utf-8", "replace"), stderr_b.decode("utf-8", "replace"), False
            )
        except asyncio.TimeoutError:
            await self._kill_group(process)
            return _CmdResult(None, "", "", True)

    async def _kill_group(self, process: asyncio.subprocess.Process) -> None:
        """SIGKILL the process group, then reap the child in a bounded wait."""
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            # justification: the group may already be gone between the
            # timeout and the kill; reaping below then completes fast.
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=_MAX_REAP_WAIT)
        except asyncio.TimeoutError:
            logger.warning("power command pid %s did not exit within %.0fs after SIGKILL", process.pid, _MAX_REAP_WAIT)


def info(opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the config metadata UI for the command driver."""
    return {
        "label": "Command",
        "description": (
            "Runs CLI commands to read and switch outlets (GPIO scripts, USB power tools, or any "
            "device the user can address from the shell). PDU-level on_cmd/off_cmd templates use "
            "the {outlet_id} and {outlet_index} placeholders plus the {host}/{username}/{password} "
            "connection fields; a PDU-level state_cmd prints one '<id> <state>' line per outlet, "
            "or the lines can be parsed with a state_pattern (named id/value groups). "
            "Per-outlet on_cmd/off_cmd/state_cmd/index override the PDU level."
        ),
        "options_keys": [
            {
                "key": "on_cmd",
                "type": "string",
                "default": "-",
                "help": "PDU-level switch-on template; must contain {outlet_id} or {outlet_index}. Per-outlet on_cmd wins.",
            },
            {
                "key": "off_cmd",
                "type": "string",
                "default": "-",
                "help": "PDU-level switch-off template; must contain {outlet_id} or {outlet_index}. Per-outlet off_cmd wins.",
            },
            {
                "key": "state_cmd",
                "type": "string",
                "default": "-",
                "help": (
                    "PDU-level read command. Default output: one '<id> <state>' line per outlet; "
                    "with state_pattern the device's own line format."
                ),
            },
            {
                "key": "state_pattern",
                "type": "string",
                "default": "-",
                "help": (
                    "Regex with named groups id (device identity: outlet id or index) and "
                    "value (state token), matched on each state_cmd line."
                ),
            },
            {
                "key": "state_token_on",
                "type": "list of strings",
                "default": "on,1,true,yes (built in)",
                "help": "Extra tokens that count as on, added to the built-in list.",
            },
            {
                "key": "state_token_off",
                "type": "list of strings",
                "default": "off,0,false,no (built in)",
                "help": (
                    "Extra tokens that count as off, added to the built-in list. " 'Example: ["2"] for the APC PowerNet enum.'
                ),
            },
            {
                "key": "host",
                "type": "string",
                "default": "-",
                "help": (
                    "Device host; {host} placeholder, exported as PDU_HOST to the commands. "
                    "Required when the connection placeholders are used."
                ),
            },
            {
                "key": "username",
                "type": "string",
                "default": "-",
                "help": "Device username; {username} placeholder. Exported as PDU_USERNAME to the commands.",
            },
            {
                "key": "password",
                "type": "string",
                "default": "-",
                "help": (
                    "Device password (SNMP community, etc.); {password} placeholder, "
                    "exported as PDU_PASSWORD to the commands."
                ),
            },
            {
                "key": "outlets",
                "type": "list of mappings",
                "default": "(required)",
                "help": (
                    "Each entry: id (required, the outlet ref part); optional index (device-side "
                    "identity behind {outlet_index}), on_cmd, off_cmd, state_cmd, state_pattern."
                ),
            },
            {
                "key": "cwd",
                "type": "string",
                "default": "server cwd",
                "help": "Working directory for the commands.",
            },
            {
                "key": "timeout",
                "type": "number",
                "default": "5",
                "help": f"Per-command timeout in seconds (max {MAX_TIMEOUT:g}). The process group is killed on timeout.",
            },
            {
                "key": "env",
                "type": "mapping",
                "default": "sanitized base",
                "help": (
                    "Extra environment variables over a minimal PATH/HOME/... allow-list. "
                    "Wins over the exported PDU_HOST/PDU_USERNAME/PDU_PASSWORD defaults."
                ),
            },
            {
                "key": "max_parallel",
                "type": "integer",
                "default": "4",
                "help": f"Max concurrent per-outlet state commands per poll (max {MAX_PARALLEL_LIMIT}).",
            },
        ],
        "options_example": {
            "host": "10.0.0.5",
            "password": "private",
            "on_cmd": "snmpset -v2c -c {password} {host} .1.3.6.1.4.1.318.1.1.4.4.2.1.3.{outlet_index} i 1",
            "off_cmd": "snmpset -v2c -c {password} {host} .1.3.6.1.4.1.318.1.1.4.4.2.1.3.{outlet_index} i 2",
            "state_cmd": "snmpwalk -v2c -c {password} {host} .1.3.6.1.4.1.318.1.1.4.4.2.1.3",
            "timeout": 15,
            "state_pattern": "(?P<id>\\d+) = INTEGER: (?P<value>\\d+)",
            "state_token_off": ["2"],
            "outlets": [
                {"id": "top-rack", "index": "1"},
                {"id": "mid-rack", "index": "2"},
            ],
        },
    }
