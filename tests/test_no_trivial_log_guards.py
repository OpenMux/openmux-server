"""Issue #79 gate: reject try/except guards that only protect logging calls.

The stdlib logging module is exception-safe: a handler that fails writes its
error to stderr and never raises (``logging.raiseExceptions`` is on by
default in this codebase's configuration). A ``try`` around a single
``logger.<level>(...)`` call therefore catches nothing that can escape.
The guard is dead weight and hides real risk surface from readers.

This test parses every ``openmux/**/*.py`` file and fails when a ``try``
whose body is exactly one logging call is paired with a broad
``except Exception`` / ``except BaseException`` handler whose body is only
``pass`` (or a bare ``return <simple value>``). It is a regression gate:
it passes on the current tree and fails when a new guard of this shape is
introduced.

The check is deliberately narrow (single-statement log-call bodies only).
Guards that wrap real work and merely *end* with a log line are out of
scope; those are judged case by case.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
OPENMUX_ROOT = REPO_ROOT / "openmux"

# Method names on logging.Logger; a call ending in one of these is treated
# as a logging call regardless of the receiver (self.logger, logger,
# logging, a module-level logger variable, ...).
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "fatal", "log"}
BROAD_TYPES = {"Exception", "BaseException"}


def _is_log_call(stmt: ast.stmt) -> bool:
    """True when `stmt` is exactly `Expr(<call ending in a log method>)`."""
    if not isinstance(stmt, ast.Expr):
        return False
    call = stmt.value
    return isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr in LOG_METHODS


def _handler_is_broad_trivial(handler: ast.ExceptHandler) -> bool:
    """True for `except Exception:`/`except BaseException:` (no `as` name) with a body of only `pass`
    or a bare `return <constant/name/attribute/subscript>`."""
    is_broad = False
    if handler.type is None:
        return False
    if isinstance(handler.type, ast.Name) and handler.type.id in BROAD_TYPES and handler.name is None:
        is_broad = True
    if not is_broad:
        return False
    if len(handler.body) != 1:
        return False
    body = handler.body[0]
    if isinstance(body, ast.Pass):
        return True
    if isinstance(body, ast.Return):
        v = body.value
        return v is None or isinstance(v, (ast.Constant, ast.Name, ast.Attribute, ast.Subscript))
    return False


def _offending_sites(path: Path):
    """Yield try-start line numbers where the try body is one log call and every handler is broad+trivial."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if len(node.body) != 1 or not _is_log_call(node.body[0]):
            continue
        if not node.handlers:
            continue
        if all(_handler_is_broad_trivial(h) for h in node.handlers):
            yield node.lineno
        elif any(_handler_is_broad_trivial(h) for h in node.handlers):
            # Mixed handler lists: only flag when every handler is trivial,
            # since a non-trivial handler may be doing real work the guard
            # exists for.
            continue


def test_no_trivial_log_call_guards():
    """The gate: zero `try: logger.x(...) except Exception: pass` sites in openmux/."""
    offenders = []
    for path in sorted(OPENMUX_ROOT.rglob("*.py")):
        for lineno in _offending_sites(path):
            rel = path.relative_to(REPO_ROOT)
            offenders.append(f"{rel}:{lineno}")
    if offenders:
        detail = "\n".join(f"  {line}" for line in offenders)
        pytest.fail(
            "Found try/except guards that only protect logging calls "
            "(issue #79). The stdlib logger never raises from a handler "
            "failure; unwrap the try and keep the log call:\n" + detail
        )
