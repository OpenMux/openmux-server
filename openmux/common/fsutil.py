"""Filesystem helpers shared by the server and client.

Kept dependency-free (logging + os only) so both sides can import it without
an import cycle.
"""

import logging
import os
from typing import Set, Union

# Directories for which the "cannot create log dir" warning has already been
# emitted in this process (issue #42). Reset by tests via `_warned.clear()`.
_warned: Set[str] = set()


def _norm(key: Union[str, os.PathLike]) -> str:
    """Normalize a directory path for use as a warning-key.

    Relative paths resolve against the current working directory so a path
    given as ``logs`` and one given as ``./logs`` map to the same key.
    """
    return os.path.abspath(str(key))


def ensure_directory(path: Union[str, os.PathLike]) -> bool:
    """Create ``path`` (and parents) for log files and report failure once.

    On success (or when the directory already exists) returns True and stays
    silent. When the directory cannot be created (for example a missing
    parent owned by another user), logs a single warning per directory for
    the life of the process and returns False so the caller can fall back to
    console-only mode.

    The warning is emitted once per resolved directory. Creating the same
    directory again later (a config reload, the next data-log event) logs
    nothing further. A fresh process logs it again, which is the first
    startup line a user needs. No exception traceback is attached: the
    error is a plain, known condition.

    Args:
        path: Directory path to create.

    Returns:
        bool: True when the directory now exists, False when creation failed.
    """
    try:
        os.makedirs(_norm(path), exist_ok=True)
        return True
    except Exception as exc:  # justification: any makedirs fault maps to console-only
        key = _norm(path)
        if key not in _warned:
            _warned.add(key)
            logging.warning("Cannot create log directory %s: %s; continuing in console-only mode", path, exc)
        return False
