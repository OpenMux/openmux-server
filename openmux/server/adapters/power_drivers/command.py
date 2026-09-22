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

# State tokens accepted on the default token parse.
_ON_TOKENS = frozenset({"on", "1", "true", "yes"})
_OFF_TOKENS = frozenset({"off", "0", "false", "no"})

# The only placeholder substituted into PDU-level set templates.
_PLACEHOLDER = "{outlet_id}"

# Minimal environment allow-list (same base as the command adapter).
_ENV_ALLOWLIST = ("PATH", "HOME", "SHELL", "USER", "LANG", "LC_ALL")

# Valid top-level option keys for this driver.
_KNOWN_OPTIONS = frozenset({"outlets", "on_cmd", "off_cmd", "state_cmd", "cwd", "timeout", "env", "max_parallel"})
_KNOWN_OUTLET_KEYS = frozenset({"id", "on_cmd", "off_cmd", "state_cmd", "state_pattern"})


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


def _state_token(text: str) -> Optional[bool]:
    """Map one state token to True/False; None when unrecognized."""
    tok = text.strip().lower()
    if tok in _ON_TOKENS:
        return True
    if tok in _OFF_TOKENS:
        return False
    return None


def _stderr_tail(stderr: str) -> str:
    tail = stderr.strip()[-_STDERR_TAIL:]
    return f" (stderr: {tail})" if tail else ""


def _failure_text(result: _CmdResult) -> str:
    if result.timed_out:
        return "state command timed out"
    return f"state command failed (exit {result.exit_code}){_stderr_tail(result.stderr)}"


def _parse_single_token(stdout: str) -> Tuple[Optional[bool], str]:
    """Parse single-outlet state output: first non-empty line, token match."""
    for line in stdout.splitlines():
        if not line.strip():
            continue
        state = _state_token(line)
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


def _parse_single(stdout: str, pattern: Optional[Pattern]) -> Tuple[Optional[bool], str]:
    if pattern is not None:
        return _parse_single_pattern(stdout, pattern)
    return _parse_single_token(stdout)


def _parse_batch_lines(stdout: str) -> Dict[str, bool]:
    """Parse PDU-level state output: one ``<id> <state>`` per line.

    Lines with an unknown outlet id or an unparseable state token are
    skipped; they never affect other outlets.
    """
    states: Dict[str, bool] = {}
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        state = _state_token(parts[1])
        if state is None or parts[0] in states:
            continue
        states[parts[0]] = state
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
    if text is not None and _PLACEHOLDER not in text:
        raise ValueError(f"command driver: {label} template must contain {str(_PLACEHOLDER)!r}")
    return text


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
        self._env = self._build_env(_validate_env(opts.get("env")))
        pdu_on = _template_text(opts.get("on_cmd"), "on_cmd")
        pdu_off = _template_text(opts.get("off_cmd"), "off_cmd")
        batch_state = _command_text(opts.get("state_cmd"), "state_cmd")
        outlets = opts.get("outlets")
        if not isinstance(outlets, (list, tuple)) or not outlets:
            raise ValueError("command driver: options.outlets must be a non-empty list")
        self._ids: List[str] = []
        self._on_cmds: Dict[str, List[str]] = {}
        self._off_cmds: Dict[str, List[str]] = {}
        self._state_cmds: Dict[str, Optional[List[str]]] = {}
        self._state_patterns: Dict[str, Optional[Pattern]] = {}
        seen: Set[str] = set()
        for entry in outlets:
            self._add_outlet(entry, seen, pdu_on, pdu_off)
        self._batch_state_argv = _split_command(batch_state, "state_cmd") if batch_state else None
        self._last_state: Dict[str, Optional[bool]] = {oid: None for oid in self._ids}
        self._backoff = ReadBackoff("command-driver")
        self._lock = asyncio.Lock()
        self.online = True

    def _build_env(self, extra: Dict[str, str]) -> Dict[str, str]:
        """Sanitized base environment merged with the user's ``env``."""
        env: Dict[str, str] = {}
        for key in _ENV_ALLOWLIST:
            value = os.environ.get(key)
            if value:
                env[key] = value
        env.update(extra)
        return env

    def _add_outlet(self, entry: Any, seen: Set[str], pdu_on: Optional[str], pdu_off: Optional[str]) -> None:
        oid = _parse_id(entry, seen)
        seen.add(oid)
        unknown = set(entry) - _KNOWN_OUTLET_KEYS
        if unknown:
            raise ValueError(f"command driver: outlet {oid!r} has unknown key(s): {sorted(unknown)}")
        on_raw = _command_text(entry.get("on_cmd"), f"outlet {oid!r} on_cmd")
        off_raw = _command_text(entry.get("off_cmd"), f"outlet {oid!r} off_cmd")
        state_raw = _command_text(entry.get("state_cmd"), f"outlet {oid!r} state_cmd")
        if on_raw is None and pdu_on is None:
            raise ValueError(f"command driver: outlet {oid!r} needs on_cmd (per-outlet or PDU-level template)")
        if off_raw is None and pdu_off is None:
            raise ValueError(f"command driver: outlet {oid!r} needs off_cmd (per-outlet or PDU-level template)")
        pattern = _parse_pattern(entry, oid, state_raw is not None)
        self._ids.append(oid)
        self._on_cmds[oid] = _split_command(
            on_raw if on_raw is not None else pdu_on.replace(_PLACEHOLDER, oid), f"outlet {oid!r} on_cmd"
        )
        self._off_cmds[oid] = _split_command(
            off_raw if off_raw is not None else pdu_off.replace(_PLACEHOLDER, oid), f"outlet {oid!r} off_cmd"
        )
        self._state_cmds[oid] = _split_command(state_raw, f"outlet {oid!r} state_cmd") if state_raw else None
        self._state_patterns[oid] = pattern

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
        batch_map = _parse_batch_lines(batch_result.stdout) if batch_result is not None and batch_result.ok else {}
        batch_ok = batch_result is not None and batch_result.ok
        return self._build_readings(results, batch_map, batch_ok)

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
            state, error = _parse_single(result.stdout, self._state_patterns.get(oid))
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
            "device the user can address from the shell). PDU-level on_cmd/off_cmd templates use the "
            "{outlet_id} placeholder; a PDU-level state_cmd prints one '<id> <state>' line per outlet "
            "(state: on/off/0/1). Per-outlet on_cmd/off_cmd/state_cmd override the PDU level."
        ),
        "options_keys": [
            {
                "key": "on_cmd",
                "type": "string",
                "default": "-",
                "help": f"PDU-level switch-on template; must contain {_PLACEHOLDER}. Per-outlet on_cmd wins.",
            },
            {
                "key": "off_cmd",
                "type": "string",
                "default": "-",
                "help": f"PDU-level switch-off template; must contain {_PLACEHOLDER}. Per-outlet off_cmd wins.",
            },
            {
                "key": "state_cmd",
                "type": "string",
                "default": "-",
                "help": "PDU-level read command printing one '<id> <state>' line per outlet.",
            },
            {
                "key": "outlets",
                "type": "list of mappings",
                "default": "(required)",
                "help": "Each entry: id (required); optional on_cmd, off_cmd, state_cmd, state_pattern.",
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
                "help": "Extra environment variables merged over a minimal PATH/HOME/... allow-list.",
            },
            {
                "key": "max_parallel",
                "type": "integer",
                "default": "4",
                "help": f"Max concurrent per-outlet state commands per poll (max {MAX_PARALLEL_LIMIT}).",
            },
        ],
        "options_example": {
            "on_cmd": "sudo ./set_output.sh {outlet_id} on",
            "off_cmd": "sudo ./set_output.sh {outlet_id} off",
            "state_cmd": "sudo ./get_states.sh",
            "timeout": 5,
            "outlets": [{"id": "1"}, {"id": "2"}, {"id": "3"}],
        },
    }
