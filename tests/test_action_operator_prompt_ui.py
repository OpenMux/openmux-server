"""Contract tests for how the console retires an action's operator prompt box.

A script's prompt is shown to *every* viewer of a run, but only its operator can answer it.
Answering used to be invisible to everyone except the tab that sent it, so a second viewer kept
a live-looking prompt box until the run finished (docs/design/port_actions.md "Operator
input"). These tests pin the static contract that keeps the box in step with the server:
`openmux/server/actions/runner.py` publishes `operator_prompt_closed`, and `console.js` closes
the matching box when it arrives instead of when it sends an answer.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSOLE_JS = (ROOT / "openmux" / "server" / "webui" / "static" / "js" / "console.js").read_text(encoding="utf-8")
RUNNER_PY = (ROOT / "openmux" / "server" / "actions" / "runner.py").read_text(encoding="utf-8")
SESSION_PY = (ROOT / "openmux" / "server" / "actions" / "session.py").read_text(encoding="utf-8")

CLOSE_EVENT = "operator_prompt_closed"


def _js_function(source: str, name: str) -> str:
    """Return the body of a top-level `function <name>(...)` in `source`."""
    start = source.index(f"function {name}(")
    end = source.index("\n}", start)
    return source[start:end]


def _event_branches(source: str, event_name: str) -> list:
    """Return every `msg.event === '<event_name>'` branch in `source`.

    The run-stream handler mentions an event twice - once to build its transcript line, once to
    act on it - so callers check the branches they mean instead of assuming one match.
    """
    marker = f"msg.event === '{event_name}'"
    branches = []
    start = source.find(marker)
    while start != -1:
        rest = source[start:]
        end = len(rest)
        for terminator in ("} else if", "} else {"):
            pos = rest.find(terminator, len(marker))
            if pos != -1:
                end = min(end, pos)
        branches.append(rest[:end])
        start = source.find(marker, start + len(marker))
    assert branches, f"console.js never handles the {event_name!r} event"
    return branches


def _format_event_for_log_body() -> str:
    start = RUNNER_PY.index("def _format_event_for_log(")
    end = RUNNER_PY.index("\nclass ", start)
    return RUNNER_PY[start:end]


def test_the_close_event_is_named_by_both_sides():
    """The event name must match exactly, or the box never closes and nothing reports it."""
    assert f'"{CLOSE_EVENT}"' in RUNNER_PY
    assert f"'{CLOSE_EVENT}'" in CONSOLE_JS
    # The per-run transcript words the same event the same way the live view does (the
    # `_format_event_for_log` comment in runner.py requires the two to stay in sync).
    assert CLOSE_EVENT in _format_event_for_log_body()


def test_the_close_event_hides_the_matching_prompt_box():
    """Receiving the close must retire the box, its flash, the badge and the strip.

    `hideOperatorPrompt()` owns all four, so pinning that call plus the id check pins the fix:
    the close for a *different* prompt (an earlier prompt in a replayed history) must be
    ignored, and the run-state cues must come back for the viewer who never answered anything.
    """
    branches = _event_branches(CONSOLE_JS, CLOSE_EVENT)
    acting = [b for b in branches if "hideOperatorPrompt()" in b]
    assert acting, "the close event is only logged, never acted on"
    assert any("currentPromptId" in b for b in acting), "the close must be matched to its prompt"


def test_the_open_prompt_records_its_id():
    """The rendered box keeps the server's id so the close event can be matched to it."""
    show = _js_function(CONSOLE_JS, "showOperatorPrompt")
    assert "promptId" in show
    assert "currentPromptId = promptId" in show
    assert "currentPromptId = null" in _js_function(CONSOLE_JS, "hideOperatorPrompt")
    assert "msg.prompt_id" in _event_branches(CONSOLE_JS, "waiting_for_operator")[-1]


def test_sending_an_answer_no_longer_hides_the_box():
    """Only the server's event may close the box.

    The optimistic hide is what made the bug asymmetric: the answering tab looked correct while
    every other viewer stayed stale, and it also hid the box when the server refused the answer.
    """
    send = _js_function(CONSOLE_JS, "sendOperatorInput")
    assert "hideOperatorPrompt()" not in send
    # Sending still has to feel instant, and a second Enter must not queue a duplicate answer for
    # the script's next prompt: the controls go dead until the close arrives (or the ack
    # watchdog gives them back, for an answer the server refused and never announced).
    assert "operatorInputPending = true" in send
    disabled = _js_function(CONSOLE_JS, "applyOperatorInputDisabledState")
    assert "!operatorInputPending" in disabled
    # The read-only note explains the operator rule; a brief ack wait must not claim otherwise.
    assert "actionsOperatorReadonlyNote.style.display = isOperator" in disabled


def test_the_answer_is_reported_by_the_session_not_the_runner():
    """The answer text travels as an ordinary run log line, from the resumed script.

    `ActionRunner.submit_operator_input()` must stay silent: it runs before the script reads the
    text, so announcing there would resolve the prompt for viewers even when the answer is
    refused. The close frame carries no answer text either (it is a pure state change).
    """
    assert "operator_answered" in SESSION_PY
    assert "operator_input_timeout" in SESSION_PY
    assert "operator_answered" not in RUNNER_PY
    close_publisher = RUNNER_PY[RUNNER_PY.index("def _close_prompt(") : RUNNER_PY.index("def _publish(")]
    assert "operator_prompt_closed" in close_publisher
    for leaked in ("answer", "text"):
        assert f'"{leaked}"' not in close_publisher, f"the close frame must not carry the answer ({leaked})"
