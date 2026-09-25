"""Tests for the ``driver: command`` PDU driver.

Fakes not mocks: ``asyncio.create_subprocess_exec`` is monkeypatched with a
recorder that returns scripted fake processes (stdout/stderr/returncode),
captures argv/cwd/env/session flags, and records kills. The backoff window
is advanced by moving the driver's injected clock, never by sleeping.

Covers: option validation, template substitution, per-outlet overrides,
batch and per-outlet state parsing (tokens, patterns, errors), the read
backoff contract (None while in window, reset on success, no spawn
suppression of explicit switches), subprocess safety (process-group kill
on timeout, env allow-list, cwd, start_new_session, concurrency cap), and
the adapter end-to-end (snapshot, set_outlet, backoff keeping last known).
"""

import asyncio
import re

import pytest

from openmux.server.adapters.pdu import PduAdapter
from openmux.server.adapters.power_drivers import command as command_mod
from openmux.server.adapters.power_drivers.command import (
    _OFF_TOKENS,
    _ON_TOKENS,
    CommandDriver,
    _parse_batch_lines,
)
from openmux.server.data_logger import DataLogger

asyncio_test = pytest.mark.asyncio


class _CapDL:
    """DataLogger stand-in: captures record_meta calls, keeps nothing on disk."""

    def __init__(self):
        self.calls = []
        self.base_dir = None

    def record_meta(self, port_name, event, client_id=None, meta=None, port_obj=None):
        self.calls.append({"port_name": port_name, "event": event, "client_id": client_id, "meta": meta})


@pytest.fixture(autouse=True)
def _cap_data_logger(monkeypatch):
    cap = _CapDL()
    monkeypatch.setattr(DataLogger, "get", classmethod(lambda cls: cap))
    yield cap


class _Tracker:
    """Tracks peak concurrency of overlapping fake `communicate` calls."""

    def __init__(self):
        self.active = 0
        self.peak = 0

    def enter(self):
        self.active += 1
        self.peak = max(self.peak, self.active)

    def leave(self):
        self.active -= 1


class _FakeProc:
    """A fake subprocess with scripted output and an optional kill record."""

    def __init__(self, rc, out, err, delay=0.0, hang=False, tracker=None, kill_sink=None):
        self.pid = 424242
        self.returncode = rc
        self._out = out.encode() if isinstance(out, str) else out
        self._err = err.encode() if isinstance(err, str) else err
        self.delay = delay
        self.hang = hang
        self.tracker = tracker
        self.kill_sink = kill_sink
        self.wait_calls = 0

    async def communicate(self):
        if self.tracker:
            self.tracker.enter()
        if self.hang:
            await asyncio.sleep(3600)
        elif self.delay:
            await asyncio.sleep(self.delay)
        if self.tracker:
            self.tracker.leave()
        return self._out, self._err

    async def wait(self):
        self.wait_calls += 1
        if self.kill_sink is not None:
            # A wait after a kill means the child was reaped.
            self.kill_sink.append("wait")
        return self.returncode


class _Recorder:
    """Stands in for asyncio.create_subprocess_exec with scripted results."""

    def __init__(self, specs):
        self.specs = list(specs)
        self.calls = []
        self.kwargs_history = []
        self.kill_calls = []

    async def __call__(self, *argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs_history.append(kwargs)
        if len(self.calls) <= len(self.specs):
            spec = self.specs[len(self.calls) - 1]
        else:
            spec = self.specs[-1]
        if callable(spec):
            spec = spec(list(argv), kwargs)
        rc, out, err = spec["rc"], spec["out"], spec["err"]
        return _FakeProc(
            rc,
            out,
            err,
            delay=spec.get("delay", 0.0),
            hang=spec.get("hang", False),
            tracker=spec.get("tracker"),
            kill_sink=self.kill_calls,
        )


def _rec(monkeypatch, specs):
    rec = _Recorder(specs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", rec)
    return rec


def _spec(rc=0, out="", err="", **kw):
    return {"rc": rc, "out": out, "err": err, **kw}


def _driver(**opts):
    base = dict(
        on_cmd="true {outlet_id} 1",
        off_cmd="true {outlet_id} 0",
        outlets=[{"id": "1"}, {"id": "2"}],
    )
    base.update(opts)
    return CommandDriver(base)


def _fast_forward(d) -> None:
    """Put the driver's current backoff window in the past."""
    d._backoff._window_until = 0.0


# --- option validation ------------------------------------------------------


@pytest.mark.parametrize(
    "opts",
    [
        dict(),  # no outlets
        dict(outlets=[]),
        dict(outlets=[{}]),  # missing id
        dict(outlets=[{"id": " "}], on_cmd=None, off_cmd=None),
        dict(outlets=[{"id": "a.b"}]),
        dict(outlets=[{"id": "1 2"}]),
        dict(outlets=[{"id": "1"}, {"id": "1"}]),
        dict(outlets=[{"id": "1", "bogus": 1}]),
        dict(outlets=[{"id": "1", "state_pattern": "x"}]),  # pattern without state_cmd
        dict(outlets=[{"id": "1", "state_pattern": "([", "state_cmd": "true"}]),
        dict(on_cmd="no placeholder"),
        dict(off_cmd="no placeholder"),
        dict(timeout=0),
        dict(timeout=-1),
        dict(timeout="soon"),
        dict(timeout=1000),
        dict(max_parallel=0),
        dict(max_parallel=100),
        dict(max_parallel="many"),
        dict(cwd=""),
        dict(env=[]),
        dict(unknown_key=1),
        dict(outlets="1"),
        dict(outlets=[{"id": "1", "on_cmd": 'bad quote "a'}]),
    ],
)
def test_ctor_rejects_bad_options(opts):
    if "on_cmd" not in opts:
        opts = dict(on_cmd="true {outlet_id} 1", **opts)
    if "off_cmd" not in opts:
        opts = dict(opts, off_cmd="true {outlet_id} 0")
    with pytest.raises(ValueError):
        CommandDriver(opts)


def test_ctor_requires_set_commands_per_outlet():
    # No PDU-level templates and no per-outlet set commands for one op.
    with pytest.raises(ValueError):
        CommandDriver({"outlets": [{"id": "1"}, {"id": "2", "off_cmd": "true 0"}]})
    # Per-outlet override satisfies the requirement even without a template.
    d = CommandDriver(
        {"outlets": [{"id": "1", "on_cmd": "a on", "off_cmd": "a off"}, {"id": "2", "on_cmd": "b on", "off_cmd": "b off"}]}
    )
    assert d._on_cmds["1"] == ["a", "on"]


def test_ctor_quotable_commands_pre_split():
    d = _driver(outlets=[{"id": "1", "on_cmd": 'tool --flag "a b"'}])
    assert d._on_cmds["1"] == ["tool", "--flag", "a b"]


@asyncio_test
async def test_list_outlets_returns_declared_ids():
    d = _driver(outlets=[{"id": "A1"}, {"id": "C2"}])
    assert await d.list_outlets() == ["A1", "C2"]


# --- template substitution and set path --------------------------------------


@asyncio_test
async def test_set_state_uses_template_substitution(monkeypatch):
    d = _driver(outlets=[{"id": "7"}, {"id": "8", "on_cmd": "custom on"}])
    rec = _rec(monkeypatch, [_spec()])
    reading = await d.set_state("7", True)
    assert reading.on is True
    assert rec.calls[0] == ["true", "7", "1"]


@asyncio_test
async def test_per_outlet_command_overrides_template(monkeypatch):
    d = _driver(outlets=[{"id": "7"}, {"id": "8", "on_cmd": "custom --pin 8"}])
    rec = _rec(monkeypatch, [_spec()])
    await d.set_state("8", True)
    assert rec.calls[0] == ["custom", "--pin", "8"]


@asyncio_test
async def test_set_state_unknown_outlet_raises():
    d = _driver()
    with pytest.raises(ValueError):
        await d.set_state("9", True)


@asyncio_test
async def test_set_state_failure_raises(monkeypatch):
    d = _driver()
    _rec(monkeypatch, [_spec(rc=2, err="boom\n")])
    with pytest.raises(RuntimeError, match="exit 2"):
        await d.set_state("1", False)


@asyncio_test
async def test_timeout_kills_process_group(monkeypatch):
    d = _driver(timeout=0.01, state_cmd="true all")
    rec = _rec(monkeypatch, [_spec(hang=True), _spec(hang=True)])
    killpg_calls = []

    def fake_killpg(pid, sig):
        killpg_calls.append((pid, sig))

    monkeypatch.setattr(command_mod.os, "killpg", fake_killpg)
    # Read path: the hung batch command is killed and the read fails the PDU.
    assert await d.read_states() is None
    assert killpg_calls == [(424242, 9)]  # SIGKILL
    # Set path: an explicit switch during backoff still runs, and its hung
    # command is killed too; set_state then reports the failure.
    with pytest.raises(RuntimeError, match="timed out"):
        await d.set_state("1", True)
    assert len(killpg_calls) == 2
    assert len(rec.calls) == 2


@asyncio_test
async def test_spawn_flags_and_env_allowlist(monkeypatch):
    monkeypatch.setenv("OMX_TEST_MARKER", "1")
    d = _driver(state_cmd="true all", cwd="/tmp", env={"EXTRA": "yes", "PATH": "/x/bin"})
    rec = _rec(monkeypatch, [_spec(out="1 on\n2 off\n")])
    await d.read_states()
    kwargs = rec.kwargs_history[0]
    assert kwargs["start_new_session"] is True
    assert kwargs["cwd"] == "/tmp"
    env = kwargs["env"]
    assert env["EXTRA"] == "yes"
    assert env["PATH"] == "/x/bin"  # user override wins
    assert "OMX_TEST_MARKER" not in env  # nothing else leaks


@asyncio_test
async def test_per_outlet_commands_run_bounded_concurrently(monkeypatch):
    tracker = _Tracker()
    specs = [_spec(out="on", delay=0.03, tracker=tracker)] * 8
    d = _driver(outlets=[{"id": str(i), "state_cmd": "true"} for i in range(1, 9)], max_parallel=4)
    rec = _rec(monkeypatch, specs)
    readings = await d.read_states()
    assert len(rec.calls) == 8
    assert tracker.peak <= 4
    assert tracker.peak > 1  # actually ran in parallel
    assert all(r.on is True for r in readings.values())


# --- batch (PDU-level state_cmd) parsing --------------------------------------


def test_parse_batch_lines_first_line_wins_and_ignores_malformed():
    # First valid line per outlet wins; lines without two tokens are
    # ignored (an outlet id appearing ONLY on a malformed line is a
    # caller problem: the outlet will simply be unreported).
    out = "1 on\n2 OFF\n3 true\nx9 on\n1 off\n5\n"
    parsed = _parse_batch_lines(out, None, _ON_TOKENS, _OFF_TOKENS)
    assert parsed == {"1": True, "2": False, "3": True, "x9": True}
    # Unparseable state tokens are skipped too.
    assert _parse_batch_lines("1 maybe\n", None, _ON_TOKENS, _OFF_TOKENS) == {}


def test_parse_batch_lines_pattern_and_tokens():
    pattern = re.compile("sPDUOutletCtl\\.(?P<id>\\d+) = INTEGER: (?P<value>\\d+)")
    out = (
        "APC PowerNet-MIB::sPDUOutletCtl.1 = INTEGER: 1 (outletOn)\n"
        "APC PowerNet-MIB::sPDUOutletCtl.2 = INTEGER: 2 (outletOff)\n"
        "junk line with no match\n"
        "APC PowerNet-MIB::sPDUOutletCtl.1 = INTEGER: 2\n"  # first line wins
    )
    tokens_on, tokens_off = _ON_TOKENS, _OFF_TOKENS + ("2",)
    parsed = _parse_batch_lines(out, pattern, frozenset(tokens_on), frozenset(tokens_off))
    assert parsed == {"1": True, "2": False}
    # Non-token values are skipped (e.g. SNMP "no command pending").
    assert _parse_batch_lines("sPDUOutletCtl.9 = INTEGER: 3", pattern, frozenset(tokens_on), frozenset(tokens_off)) == {}


@asyncio_test
async def test_read_states_batch_assigns_all_outlets(monkeypatch):
    d = _driver(state_cmd="true all")
    rec = _rec(monkeypatch, [_spec(out="1 on\n2 off\n")])
    readings = await d.read_states()
    assert len(rec.calls) == 1
    assert readings["1"].on is True
    assert readings["2"].on is False
    assert not readings["1"].error


@asyncio_test
async def test_batch_unreported_outlet_keeps_last_state_with_error(monkeypatch):
    d = _driver(state_cmd="true all")
    _rec(monkeypatch, [_spec(out="1 on\n2 off\n"), _spec(out="1 off\n")])
    await d.read_states()
    readings = await d.read_states()
    assert readings["1"].on is False
    # Outlet 2 was reported OFF on the first read; when the batch stops
    # reporting it, the last known state (off) is kept and flagged.
    assert readings["2"].on is False
    assert "not reported" in readings["2"].error


@asyncio_test
async def test_batch_failure_starts_backoff_and_suppresses_spawns(monkeypatch):
    d = _driver(state_cmd="true all")
    rec = _rec(monkeypatch, [_spec(rc=3, err="no device\n")])
    assert await d.read_states() is None  # device-wide failure -> None
    assert d._backoff.in_backoff() is True
    # Next read inside the window: no new spawn, still None.
    assert await d.read_states() is None
    assert len(rec.calls) == 1


@asyncio_test
async def test_backoff_window_doubles_between_probes_then_resets(monkeypatch):
    d = _driver(state_cmd="true all")
    _rec(monkeypatch, [_spec(rc=3), _spec(rc=3), _spec(out="1 on\n2 on\n"), _spec(rc=3)])
    assert await d.read_states() is None  # streak 1, window 30 s
    _fast_forward(d)
    assert await d.read_states() is None  # streak 2, window 60 s
    assert d._backoff._window_until - d._backoff._now() == pytest.approx(60.0)
    _fast_forward(d)
    readings = await d.read_states()  # success resets the failure streak
    assert readings is not None
    assert d._backoff.in_backoff() is False
    _fast_forward(d)
    assert await d.read_states() is None  # a later failure restarts from the base
    assert d._backoff._window_until - d._backoff._now() == pytest.approx(30.0)


@asyncio_test
async def test_explicit_switch_runs_during_backoff(monkeypatch):
    d = _driver(state_cmd="true all")
    rec = _rec(
        monkeypatch,
        [
            _spec(rc=3),  # read fails -> backoff
            _spec(),  # set command still runs
        ],
    )
    assert await d.read_states() is None
    reading = await d.set_state("1", True)
    assert reading.on is True
    assert len(rec.calls) == 2


@asyncio_test
async def test_partial_success_does_not_start_backoff(monkeypatch):
    # Batch fails, but one outlet's own state command succeeds: the device
    # is partially reachable, so no backoff window opens. Individually
    # read outlets spawn before the batch command.
    d = _driver(
        state_cmd="true all",
        outlets=[{"id": "1"}, {"id": "2", "state_cmd": "true one"}],
    )
    _rec(
        monkeypatch,
        [
            _spec(out="on"),  # outlet 2 individual
            _spec(rc=2, err="batch down\n"),  # batch covers outlet 1
        ],
    )
    readings = await d.read_states()
    assert readings is not None
    assert d._backoff.in_backoff() is False
    assert readings["2"].on is True
    assert readings["1"].on is None  # last known, kept
    assert readings["1"].error == ""  # error text is set only by device-wide failure


@asyncio_test
async def test_all_individual_failures_start_backoff(monkeypatch):
    d = _driver(outlets=[{"id": "1", "state_cmd": "true"}, {"id": "2", "state_cmd": "true"}])
    rec = _rec(monkeypatch, [_spec(rc=1), _spec(rc=1)])
    assert await d.read_states() is None
    assert d._backoff.in_backoff() is True
    assert len(rec.calls) == 2


# --- per-outlet state parsing --------------------------------------------------


@asyncio_test
async def test_single_state_token_parsing(monkeypatch):
    d = _driver(outlets=[{"id": "1", "state_cmd": "one"}, {"id": "2", "state_cmd": "two"}, {"id": "3", "state_cmd": "three"}])
    _rec(monkeypatch, [_spec(out="  ON \n"), _spec(out="0"), _spec(out="weird")])
    readings = await d.read_states()
    assert readings["1"].on is True
    assert readings["2"].on is False
    assert readings["3"].on is None
    assert "unrecognized" in readings["3"].error


@asyncio_test
async def test_all_failures_enter_backoff_then_recover(monkeypatch):
    # Every spawned state command failed: device-wide failure -> None +
    # backoff. After the window, a good read restores the outlet.
    d = _driver(outlets=[{"id": "1", "state_cmd": "one"}])
    _rec(monkeypatch, [_spec(), _spec(rc=1, err="gone\n"), _spec(out="off")])
    await d.set_state("1", False)  # last known = off
    assert await d.read_states() is None
    assert d._backoff.in_backoff() is True
    _fast_forward(d)
    readings = await d.read_states()
    assert d._backoff.in_backoff() is False
    assert readings["1"].on is False


@asyncio_test
async def test_state_pattern_named_groups(monkeypatch):
    d = _driver(outlets=[{"id": "1", "state_cmd": "one", "state_pattern": r"Power:(?P<on>ON)|(?P<off>OFF)"}])
    _rec(monkeypatch, [_spec(out="status Power:ON end")])
    readings = await d.read_states()
    assert readings["1"].on is True


@asyncio_test
async def test_state_pattern_plain_match_and_no_match(monkeypatch):
    d = _driver(
        outlets=[
            {"id": "1", "state_cmd": "a", "state_pattern": "POWERED"},
            {"id": "2", "state_cmd": "b", "state_pattern": "POWERED"},
        ]
    )
    _rec(monkeypatch, [_spec(out="device POWERED"), _spec(out="device off")])
    readings = await d.read_states()
    assert readings["1"].on is True
    assert readings["2"].on is False


@asyncio_test
async def test_state_pattern_ambiguous(monkeypatch):
    # A single match filling BOTH named groups is ambiguous: last known
    # state is kept and the error says so.
    d = _driver(outlets=[{"id": "1", "state_cmd": "a", "state_pattern": r"(?P<on>ON).*(?P<off>OFF)"}])
    _rec(monkeypatch, [_spec(), _spec(out="ON and OFF")])
    await d.set_state("1", True)
    readings = await d.read_states()
    assert readings["1"].on is True  # last known kept
    assert "both" in readings["1"].error


@asyncio_test
async def test_no_state_commands_reports_last_set(monkeypatch):
    d = _driver()
    rec = _rec(monkeypatch, [_spec()])
    readings = await d.read_states()
    assert readings["1"].on is None
    await d.set_state("1", True)
    readings = await d.read_states()
    assert readings["1"].on is True
    assert len(rec.calls) == 1  # only the set command spawned


@asyncio_test
async def test_mixed_individual_and_batch_mode(monkeypatch):
    # One outlet read individually, the rest via the batch command: two
    # spawns per poll, correctly merged.
    d = _driver(state_cmd="all", outlets=[{"id": "1"}, {"id": "2", "state_cmd": "one"}])
    rec = _rec(monkeypatch, [_spec(out="on"), _spec(out="1 on\n")])
    readings = await d.read_states()
    assert len(rec.calls) == 2
    assert readings["1"].on is True
    assert readings["2"].on is True


# --- adapter end-to-end --------------------------------------------------------


COMMAND_SECTION = {
    "power": {
        "enabled": True,
        "pdus": [
            {
                "name": "board",
                "description": "GPIO board",
                "driver": "command",
                "poll_interval": 0,
                "options": {
                    "on_cmd": "true {outlet_id} 1",
                    "off_cmd": "true {outlet_id} 0",
                    "state_cmd": "true all",
                    "outlets": [{"id": "1"}, {"id": "2"}],
                },
            }
        ],
    }
}


class _FakePort:
    def __init__(self, name, power=()):
        self.name = name
        self.power = list(power)
        self.unified_port = self


class _FakePortManager:
    def __init__(self, ports):
        self.ports = dict(ports)
        self.meta_events = []

    def notify_meta_updated(self, port_name, changes):
        self.meta_events.append((port_name, changes))


@asyncio_test
async def test_adapter_command_pdu_end_to_end(monkeypatch):
    rec = _rec(
        monkeypatch,
        [
            _spec(out="1 on\n2 off\n"),  # startup read
            _spec(),  # set_outlet switch
            _spec(rc=1, err="gone\n"),  # failed read
        ],
    )
    pm = _FakePortManager({"console1": _FakePort("console1", ["board.1"])})
    adapter = PduAdapter("power", COMMAND_SECTION)
    adapter.main_port_manager = pm
    assert await adapter.start() is True
    await asyncio.sleep(0)
    state = adapter.pdus["board"]
    assert state.online is True
    assert state.readings["1"].on is True
    assert state.readings["2"].on is False
    snapshot = adapter.get_power_snapshot()
    board = [p for p in snapshot["pdus"] if p["name"] == "board"][0]
    outlet1 = [o for o in board["outlets"] if o["id"] == "1"][0]
    assert outlet1["on"] is True
    assert outlet1["watts"] is None

    # Switch via the control API.
    result = await adapter.set_outlet("board.1", False, user="alice")
    assert result["ok"] is True
    assert state.readings["1"].on is False
    assert pm.meta_events[-1][0] == "console1"

    # A failed read enters the driver backoff: the adapter keeps the
    # last-known values (no offline flip, no new event) and the next
    # refresh inside the window spawns nothing.
    events_before = len(pm.meta_events)
    await adapter._refresh_readings(state)
    assert state.online is True
    assert state.readings["1"].on is False  # kept, not dropped
    assert len(pm.meta_events) == events_before
    spawns_after_failure = len(rec.calls)
    await adapter._refresh_readings(state)  # inside backoff window
    assert len(rec.calls) == spawns_after_failure
    await adapter.stop()


# --- connection placeholders, index, token lists, batch pattern -----------------


APC_WALK = (
    "APC PowerNet-MIB::sPDUOutletCtl.1 = INTEGER: 1 (outletOn)\nAPC PowerNet-MIB::sPDUOutletCtl.2 = INTEGER: 2 (outletOff)\n"
)
APC_PATTERN = "(?P<id>\\d+) = INTEGER: (?P<value>\\d+)"


def _apc_driver(**overrides):
    opts = dict(
        host="10.0.0.5",
        password="private",
        on_cmd="snmpset -v2c -c {password} {host} .1.3.6.1.4.1.318.1.1.4.4.2.1.3.{outlet_index} i 1",
        off_cmd="snmpset -v2c -c {password} {host} .1.3.6.1.4.1.318.1.1.4.4.2.1.3.{outlet_index} i 2",
        state_cmd="snmpwalk -v2c -c {password} {host} .1.3.6.1.4.1.318.1.1.4.4.2.1.3",
        state_pattern=APC_PATTERN,
        state_token_off=["2"],
        outlets=[{"id": "top-rack", "index": "1"}, {"id": "mid-rack", "index": "2"}],
    )
    opts.update(overrides)
    return CommandDriver(opts)


@pytest.mark.parametrize(
    "opts",
    [
        dict(host=7),  # host not a string
        dict(host=" "),  # host blank
        dict(username=5),
        dict(password=5),
        dict(state_token_on="on"),  # not a list
        dict(state_token_on=[]),  # empty
        dict(state_token_on=["on", 1]),
        dict(state_token_on=["  "]),
        dict(state_token_on=["2"], state_token_off=["2"]),  # overlap
        dict(state_pattern="x"),  # no named groups
        dict(state_pattern="(?P<id>\\d+)"),  # missing value group
        dict(state_pattern="(["),  # does not compile
        dict(state_pattern=".+"),  # no state_cmd to parse
        dict(state_cmd="true {outlet_index} all"),  # per-outlet context in batch command
        dict(outlets=[{"id": "a", "index": 3}]),  # index not a string
        dict(outlets=[{"id": "a", "index": " "}], on_cmd="true {outlet_id}", off_cmd="true {outlet_id}"),
        dict(
            outlets=[
                {"id": "a", "index": "2"},
                {"id": "b", "index": "2"},
            ]
        ),  # duplicate index
        dict(outlets=[{"id": "a", "index": "2"}, {"id": "2"}]),  # index collides with an outlet id
        dict(outlet_id_missing_index=None),  # placeholder without index: see parametrized body below
        dict(outlets=[{"id": "a"}, {"id": "b", "on_cmd": "true {outlet_index}"}]),  # index placeholder w/o index
    ],
)
def test_ctor_connection_index_token_errors(opts):
    # The generic case: PDU-level commands reference the connection or
    # index placeholders but the matching field is absent.
    if opts.get("outlet_id_missing_index") is not None:
        with pytest.raises(ValueError):
            _apc_driver(
                outlets=[{"id": "top-rack"}, {"id": "mid-rack"}],  # no index on either outlet
            )
        return
    with pytest.raises(ValueError):
        _apc_driver(**opts)


def test_credentials_without_host_rejected():
    # Credentials can only be used when host is set.
    with pytest.raises(ValueError, match="no host is set"):
        CommandDriver(
            {
                "username": "u",
                "password": "p",
                "on_cmd": "tool {host} {username} {password} {outlet_id} on",
                "off_cmd": "tool {host} {username} {password} {outlet_id} off",
                "outlets": [{"id": "1"}],
            }
        )
    # No connection reference at all: valid without host.
    CommandDriver(
        {
            "on_cmd": "tool {outlet_id} on",
            "off_cmd": "tool {outlet_id} off",
            "outlets": [{"id": "1"}],
        }
    )


def test_ctor_apc_style_full_config_ok():
    d = _apc_driver()
    assert d._index_by_id == {"top-rack": "1", "mid-rack": "2"}
    assert d._batch_id_map == {"top-rack": "top-rack", "1": "top-rack", "mid-rack": "mid-rack", "2": "mid-rack"}
    assert "2" in d._off_tokens
    assert "on" in d._on_tokens  # built-ins preserved


def test_env_exports_pdu_credentials():
    d = _apc_driver()
    env = d._env
    assert env["PDU_HOST"] == "10.0.0.5"
    assert env["PDU_PASSWORD"] == "private"
    assert "PDU_USERNAME" not in env  # not configured


def test_env_user_override_wins():
    d = _apc_driver(env={"PDU_PASSWORD": "renamed"})
    assert d._env["PDU_PASSWORD"] == "renamed"
    assert d._env["PDU_HOST"] == "10.0.0.5"


@asyncio_test
async def test_set_state_substitutes_index_and_credentials(monkeypatch):
    d = _apc_driver()
    rec = _rec(monkeypatch, [_spec(), _spec()])
    await d.set_state("top-rack", True)
    assert rec.calls[0] == [
        "snmpset",
        "-v2c",
        "-c",
        "private",
        "10.0.0.5",
        ".1.3.6.1.4.1.318.1.1.4.4.2.1.3.1",
        "i",
        "1",
    ]
    await d.set_state("mid-rack", False)
    assert rec.calls[1] == [
        "snmpset",
        "-v2c",
        "-c",
        "private",
        "10.0.0.5",
        ".1.3.6.1.4.1.318.1.1.4.4.2.1.3.2",
        "i",
        "2",
    ]


@asyncio_test
async def test_batch_state_pattern_reads_indexed_outlets(monkeypatch):
    d = _apc_driver()
    _rec(monkeypatch, [_spec(out=APC_WALK)])
    readings = await d.read_states()
    assert readings["top-rack"].on is True
    assert readings["mid-rack"].on is False  # "2" via state_token_off
    assert not readings["top-rack"].error


@asyncio_test
async def test_batch_id_falls_back_to_outlet_id(monkeypatch):
    # No index, no pattern: the device line id IS the outlet id.
    d = _driver(state_cmd="true all")
    _rec(monkeypatch, [_spec(out="1 on\n2 off\n")])
    readings = await d.read_states()
    assert readings["1"].on is True
    assert readings["2"].on is False


@asyncio_test
async def test_single_token_path_uses_extended_tokens(monkeypatch):
    d = _driver(
        outlets=[{"id": "1", "state_cmd": "true one"}],
        state_token_off=["2"],
    )
    _rec(monkeypatch, [_spec(out="2")])
    readings = await d.read_states()
    assert readings["1"].on is False


@pytest.mark.parametrize(
    "line, expected",
    [
        ("1 on\n", True),
        ("2 off\n", False),
        ("3 1\n", True),
        ("4 0\n", False),
        ("5 yes\n", True),
        ("6 no\n", False),
        ("7 2\n", None),  # "2" is NOT a default token (state_token_off extends it)
    ],
)
def test_default_token_table_unchanged(line, expected):
    tokens_on = frozenset(_ON_TOKENS)
    tokens_off = frozenset(_OFF_TOKENS)
    parsed = _parse_batch_lines(line, None, tokens_on, tokens_off)
    if expected is None:
        assert parsed == {}
    else:
        assert parsed == {line.split()[0]: expected}
