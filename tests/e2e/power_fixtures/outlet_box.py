"""E2E fixture PDU device: one box of outlets behind a CLI, with its state in a file.

The ``command`` power driver (`openmux/server/adapters/power_drivers/command.py`) spawns one command per read and
one per switch. This script is the device those commands address. It imports nothing from ``openmux`` and needs no
hardware, so the tier drives a real child process instead of a patched one: the argv the driver builds, the
environment it passes, and the kill it performs on a timeout are all exercised for real.

Modes, as the last argument:

- ``state``: print one ``<id> <state>`` line per outlet. This is what a batch ``state_cmd`` prints.
- ``read <id>``: print one state token, which is what a per-outlet ``state_cmd`` prints.
- ``on <id>`` / ``off <id>``: switch one outlet. The line the box was told to run is appended to the journal.
- ``flip <id>``: change one outlet with no switch command, which is how a scenario moves the device on its own.

A scenario builds a fault into one command template with these flags, so a fault belongs to one PDU and not to the
whole world:

- ``--fail-state``: reads exit 4 and name the cause on stderr, as a device with a dead sense line does.
- ``--fail-switch``: switches exit 3 and name the cause on stderr.
- ``--slow N``: wait ``N`` seconds before answering any mode, for a driver timeout.
- ``--child``: with ``--slow``, start a ``sleep 30`` child first and record its pid, so a scenario can prove the
  driver killed the whole process group and not only the command it spawned.

Two ways to fail, because a scenario needs both. The flags above are baked into one command template, so they model
a device that stays broken. A fault file at ``STATE.faults``, one word per line (``state``, ``switch``), is read on
every call, so a scenario can break a device and heal it without a reload. A flag and the file do the same thing to
the same mode.

State is a JSON file named by ``--state``, and the journal defaults to that path plus ``.journal``. Outlet ids come
from ``--ids`` when the file does not exist yet, and from the file afterwards: the device owns its own ids, the way
a real box does.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

#: Exit codes, kept distinct so a scenario can tell which half of the device failed.
EXIT_STATE_FAILED = 4
EXIT_SWITCH_FAILED = 3

#: The stderr sentences. A scenario reads what the driver quotes back instead of restating them.
STATE_FAILURE = "outlet box: sense line is dead"
SWITCH_FAILURE = "outlet box: relay refused to move"


def _load(state_path: Path, ids: List[str]) -> Dict[str, Any]:
    """Read the device's state file, seeding every outlet off when it does not exist yet.

    The seed is written at once. Without that, a read-only first call would answer from memory and leave no file,
    so the next call would meet an empty box and report every outlet unknown.
    """
    if state_path.exists():
        return json.loads(state_path.read_text(encoding="utf-8"))
    state = {"outlets": {outlet: False for outlet in ids}, "journal": []}
    _save(state_path, state)
    return state


def _save(state_path: Path, state: Dict[str, Any]) -> None:
    """Write the device's state file, so the next command sees this one."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def _journal_path(args: argparse.Namespace) -> Path:
    """Where this box records the commands it was told to run."""
    return Path(args.journal) if args.journal else Path(f"{args.state}.journal")


def _faults(args: argparse.Namespace) -> set:
    """The run-time faults this box is under: one word per line in ``STATE.faults``.

    A scenario writes and removes that file, which is how a device is broken and healed between two reads while one
    config stays in force.
    """
    path = Path(f"{args.state}.faults")
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def _record(args: argparse.Namespace, line: str) -> None:
    """Append one line to the journal, so a scenario can read what the device really received."""
    path = _journal_path(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        # The box records argv as the driver passed it, one word per line, so quoting questions are visible.
        handle.write(line + "\n")


def _token(on: bool) -> str:
    """The state word this device prints."""
    return "on" if on else "off"


def _spawn_child(args: argparse.Namespace) -> None:
    """Start a long ``sleep`` and record its pid, as a device helper process would."""
    child = subprocess.Popen(["sleep", "30"])
    path = Path(f"{args.state}.child")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(child.pid), encoding="utf-8")
    child.wait()


def _run_read(args: argparse.Namespace, state: Dict[str, Any]) -> int:
    """Answer one read mode: ``state`` for every outlet, ``read <id>`` for one."""
    if args.fail_state or "state" in _faults(args):
        sys.stderr.write(STATE_FAILURE + "\n")
        return EXIT_STATE_FAILED
    if args.mode == "state":
        for outlet in sorted(state["outlets"]):
            print(f"{outlet} {_token(state['outlets'][outlet])}")
        return 0
    outlet = args.outlet
    if outlet not in state["outlets"]:
        sys.stderr.write(f"outlet box: unknown outlet {outlet!r}\n")
        return EXIT_STATE_FAILED
    print(_token(state["outlets"][outlet]))
    return 0


def _run_switch(args: argparse.Namespace, state: Dict[str, Any]) -> int:
    """Answer one switch or flip, and record what the box was told."""
    if args.mode in ("on", "off") and (args.fail_switch or "switch" in _faults(args)):
        sys.stderr.write(SWITCH_FAILURE + "\n")
        return EXIT_SWITCH_FAILED
    if args.outlet not in state["outlets"]:
        sys.stderr.write(f"outlet box: unknown outlet {args.outlet!r}\n")
        return EXIT_SWITCH_FAILED
    state["outlets"][args.outlet] = args.mode == "on"
    _save(Path(args.state), state)
    _record(args, " ".join([args.mode, args.outlet]))
    return 0


def main(argv: List[str]) -> int:
    """Run one command against one box, and answer with the process exit code."""
    parser = argparse.ArgumentParser(prog="outlet_box.py", description="E2E fixture PDU device")
    parser.add_argument("--state", required=True, help="path of this box's state file")
    parser.add_argument("--journal", help="path of this box's journal, default: STATE.journal")
    parser.add_argument("--ids", default="", help="outlet ids to seed, comma separated")
    parser.add_argument("--fail-state", action="store_true", help="fail every read")
    parser.add_argument("--fail-switch", action="store_true", help="fail every switch")
    parser.add_argument("--slow", type=float, default=0.0, help="seconds to wait before answering")
    parser.add_argument("--child", action="store_true", help="start a sleep child, for a group-kill proof")
    parser.add_argument("mode", choices=("state", "read", "on", "off", "flip"))
    parser.add_argument("outlet", nargs="?", default=None, help="outlet id for read, on, off, and flip")
    args = parser.parse_args(argv)

    if args.slow:
        if args.child:
            _spawn_child(args)
        else:
            time.sleep(args.slow)

    state = _load(Path(args.state), [item for item in args.ids.split(",") if item])
    if args.mode in ("read", "on", "off", "flip") and args.outlet is None:
        sys.stderr.write("outlet box: this mode needs an outlet id\n")
        return 2
    if args.mode in ("state", "read"):
        return _run_read(args, state)
    if args.mode == "flip":
        # A device-side change with no switch command: the journal stays empty on purpose. An id the box does not
        # have is refused, because a device does not invent an outlet because it was named.
        if args.outlet not in state["outlets"]:
            sys.stderr.write(f"outlet box: unknown outlet {args.outlet!r}\n")
            return EXIT_SWITCH_FAILED
        state["outlets"][args.outlet] = not state["outlets"][args.outlet]
        _save(Path(args.state), state)
        return 0
    return _run_switch(args, state)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
