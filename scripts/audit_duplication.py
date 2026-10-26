#!/usr/bin/env python3
"""Measure duplicated method bodies between two adapter classes.

Compares the AST source segment of every method name the two classes share and
scores similarity with difflib, after normalising tokens that are allowed to
differ between two transports. Used as the PR gate for the listener-session
extraction (GitHub issue for `docs/design/architecture-review-2026-10-25.md`
section 1): the same script must run before and after a move, under one fixed
profile, and every shared name remaining after a move must be justified (plugin
contract, ConsoleManager delegation, or a documented scope fence).

The `listener` profile (agreed 2026-10-26) normalises, in order:
  1. Config section keys (`telnet_listener` / `ssh_listener`).
  2. Transport words (`telnet` / `ssh`, any case).
  3. Session I/O handle pairs: `session.reader` and `session.process.stdin`
     share one token; `session.writer` and `session.process.stdout` share one.
`--strip-docstrings` additionally drops each method's docstring before
comparing, which separates real logic drift from comment drift. Report both
numbers when publishing a measurement; name the profile and the flags.

Usage:
    .venv/bin/python scripts/audit_duplication.py \
        openmux/server/adapters/telnet_listener.py TelnetListenerAdapter \
        openmux/server/adapters/ssh_listener.py SshListenerAdapter \
        --profile listener
"""

from __future__ import annotations

import argparse
import ast
import difflib
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _methods(path: Path, class_name: str, strip_docs: bool) -> Dict[str, str]:
    """Map method name to its exact source segment for one top-level class."""
    source = path.read_text()
    tree = ast.parse(source)
    lines = source.splitlines()
    out: Dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for member in node.body:
            if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            start = member.lineno - 1
            if member.decorator_list:
                start = min(d.lineno for d in member.decorator_list) - 1
            seg = lines[start : member.end_lineno]
            if strip_docs and _has_docstring(member):
                doc = member.body[0]
                seg = seg[: doc.lineno - 1 - start] + seg[doc.end_lineno - start :]
            out[member.name] = "\n".join(seg)
    return out


def _has_docstring(member) -> bool:
    """True when the function's first statement is a string expression."""
    body = member.body
    if body and isinstance(body[0], ast.Expr):
        value = body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return True
        if isinstance(value, ast.JoinedStr):
            return True
    return False


def _normalise(text: str, profile: str) -> str:
    """Apply the named normalisation profile. Unknown profiles leave text as-is."""
    if profile != "listener":
        return text
    # 3. I/O handle pairs first: they contain no transport words but must fold together.
    text = re.sub(r"session\.process\.stdin\b", "@IN", text)
    text = re.sub(r"session\.reader\b", "@IN", text)
    text = re.sub(r"session\.process\.stdout\b", "@OUT", text)
    text = re.sub(r"session\.writer\b", "@OUT", text)
    # 1. Section keys, then 2. bare transport words (case-insensitive).
    text = re.sub(r"(?i)telnet_listener|ssh_listener", "@KEY", text)
    text = re.sub(r"(?i)telnet|ssh", "@T", text)
    return text


def audit(
    path_a: Path, class_a: str, path_b: Path, class_b: str, profile: str, strip_docs: bool, min_ratio: float
) -> Tuple[List[str], List[str], List[Tuple[float, str]], List[Tuple[float, str]]]:
    """Return (shared, identical, at_or_above_min, below_min) method-name lists."""
    a = _methods(path_a, class_a, strip_docs)
    b = _methods(path_b, class_b, strip_docs)
    shared = sorted(set(a) & set(b))
    identical: List[str] = []
    above: List[Tuple[float, str]] = []
    below: List[Tuple[float, str]] = []
    for name in shared:
        na, nb = _normalise(a[name], profile), _normalise(b[name], profile)
        if na == nb:
            identical.append(name)
            continue
        ratio = difflib.SequenceMatcher(None, na, nb).ratio()
        (above if ratio >= min_ratio else below).append((round(ratio, 2), name))
    above.sort(reverse=True)
    below.sort(reverse=True)
    return shared, identical, above, below


def _read(path: Path) -> str:
    return path.read_text()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path_a")
    parser.add_argument("class_a")
    parser.add_argument("path_b")
    parser.add_argument("class_b")
    parser.add_argument("--profile", default="none", choices=["none", "listener"])
    parser.add_argument("--strip-docstrings", action="store_true")
    parser.add_argument("--min", type=float, default=0.90, dest="min_ratio")
    args = parser.parse_args(argv)

    shared, identical, above, below = audit(
        Path(args.path_a), args.class_a, Path(args.path_b), args.class_b, args.profile, args.strip_docstrings, args.min_ratio
    )
    la = sum(len(s.splitlines()) for s in _pick(Path(args.path_a), args.class_a, identical, args.strip_docstrings))
    lb = sum(len(s.splitlines()) for s in _pick(Path(args.path_b), args.class_b, identical, args.strip_docstrings))
    total_a = len(_read(Path(args.path_a)).splitlines())
    total_b = len(_read(Path(args.path_b)).splitlines())
    print(f"profile={args.profile} strip_docstrings={args.strip_docstrings} min={args.min_ratio}")
    print(f"lines: {args.path_a}={total_a} {args.path_b}={total_b}")
    print(f"shared names: {len(shared)}")
    print(f"identical: {len(identical)}  (lines {la} vs {lb})")
    print("  " + " ".join(sorted(identical)))
    print(f"at-or-above {args.min_ratio}: {len(identical) + len(above)}")
    print(f"below {args.min_ratio}:")
    for ratio, name in below:
        print(f"  {ratio:.2f} {name}")
    return 0


def _pick(path: Path, class_name: str, names: List[str], strip_docs: bool) -> List[str]:
    table = _methods(path, class_name, strip_docs)
    return [table[n] for n in names if n in table]


if __name__ == "__main__":
    sys.exit(main())
