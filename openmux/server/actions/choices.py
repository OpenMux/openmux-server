"""Shared choice-list normalization for Port Actions.

Used both by operator on-demand input (`session.py`'s `ActionSession.prompt(kind=
"buttons"|"select"|"radio", choices=...)`) and by start-of-run action parameters
(`registry.py`'s `ActionParam(widget="select"|"radio", choices=...)`).
"""

from typing import Any, Dict, List, Union

# A choice is either a plain value (str/int/...) or a {"label": ..., "value": ...} dict
# to show a different label than the value the script/param gets back.
Choice = Union[str, Dict[str, Any]]


def normalize_choices(choices: List[Choice]) -> List[Dict[str, str]]:
    """Turn a list of plain values/`{"label", "value"}` dicts into `{"label", "value"}` dicts."""
    normalized = []
    for choice in choices:
        if isinstance(choice, dict):
            value = str(choice.get("value", choice.get("label", "")))
            label = str(choice.get("label", value))
        else:
            value = str(choice)
            label = value
        normalized.append({"label": label, "value": value})
    return normalized
