"""ACTIONS: a Port Action run from its first click to its last notice (ACT-01 to ACT-11).

A Port Action is a script the operator runs against one port from the console page. Two things make it
worth its own tier: it reaches the port through the same Write slot a human holds, and it reports itself
through eight separate regions of the page. ``docs/design/port_actions.md`` states both, and only a real
server and a real page show them together.

Three measured facts shape these bodies:

* A page opened on a port takes the Write slot, and a Port Action needs that slot. A run the page itself
  launches therefore sets the page's own access aside, and takes it back when the run ends. A run another
  caller launches against that port fails fast: an action never displaces a human.
* Every action region is built at page load and hidden with an inline ``display``, so its stale text
  stays in the DOM after it is hidden. A scenario reads ``page.action_state()``, which pairs each region's text
  with whether it shows, and never asserts on the presence of a node.
* The granted list comes from ``action_ports``, which maps an action id to port names. A port no grant
  names answers an empty roster, and the page hides the button.

The world grants three scripts to ``loop1`` and none to ``cat1``: two shipped examples and one fixture
under ``action_fixtures``. Every account of the default world exists here, plus ``user2`` for a scenario
that needs a second write-entitled operator.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest

from openmux.server.access_decision import Code, Mode, decide, notice

from .conftest import DEFAULT_USERS, REPO_ROOT
from .story import step, surfaces

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_browser]

#: The scripts the shipped package installs, so the tier exercises what an operator really has.
EXAMPLE_ACTIONS_DIR = REPO_ROOT / "openmux" / "server" / "actions" / "examples"

#: The scripts only this tier has: percent progress, and one operator prompt of each kind.
FIXTURE_ACTIONS_DIR = REPO_ROOT / "tests" / "e2e" / "action_fixtures"

#: Granted to ``loop1`` only, so an un-granted port stays available to compare against.
GRANTED_ON_LOOP1 = ("echo_probe", "slow_noop", "meter_probe")

#: The default world's accounts plus a second write-entitled one, for a scenario with two operators.
ACTION_USERS = tuple(DEFAULT_USERS) + (("user2", "read-write"),)

#: The phrase the runner gives when it cannot get the Write slot. ``runner.py`` owns it, and no accessor
#: exists, so the scenario matches the phrase rather than the whole sentence.
SLOT_UNAVAILABLE_PHRASE = "read-write slot is unavailable"

#: The phrase one-run-per-port answers with, from ``ActionRunner.launch_run``.
ALREADY_RUNNING_PHRASE = "already running"


def _action_grants(builder: Any, granted: Tuple[str, ...]) -> Any:
    """Grant ``granted`` to ``loop1``, searched for in both script directories."""
    return builder.with_actions(
        {action_id: ["loop1"] for action_id in granted},
        actions_dir=[str(EXAMPLE_ACTIONS_DIR), str(FIXTURE_ACTIONS_DIR)],
    )


def _actions_world(servers: Any, name: str, granted: Tuple[str, ...] = GRANTED_ON_LOOP1) -> Any:
    """One started server whose only granted port is the Loopback port ``loop1``."""
    return servers(
        name=name,
        users=ACTION_USERS,
        command_ports=("cat1",),
        configure=lambda builder: _action_grants(builder, granted),
        start=True,
    )


def _lines_for(code: Code, mode: Mode = Mode.READ_ONLY, **fields: Any) -> List[str]:
    """The terminal lines the product itself renders for one Outcome code.

    A scenario never restates a sentence the server owns, so a reworded notice fails here instead of
    passing against a copy. A prompt takes the mode the Client holds after the request; a notice about
    a change nobody asked for fixes its own mode, which is why the two builders are tried in turn.
    """
    try:
        decision = decide(code, mode=mode, **fields)
    except ValueError:
        decision = notice(code, **fields)
    return list(decision.lines)


def _form_value(state: Dict[str, Any], name: str) -> Optional[str]:
    """The value one run-panel field holds, or None when the panel shows no field of that name."""
    for field in state.get("form_fields") or []:
        if str(field.get("name")) == name:
            return str(field.get("value"))
    return None


def _start_run(api: Any, action_id: str, params: Optional[Dict[str, Any]] = None, *, port: str = "loop1") -> Dict[str, Any]:
    """Start one run over the HTTP API, as another system would, and return the server's answer.

    This caller carries no client id, so the run has no operator, and it cannot displace the page's Write
    slot by self-demoting anyone.
    """
    _, _, text = api.post(f"/api/ports/{port}/actions/{action_id}/run", {"params": params or {}})
    payload = json.loads(text)
    assert isinstance(payload, dict) and payload.get("run_id"), payload
    return payload


def _runs_of(api: Any, action_id: str, *, port: str = "loop1") -> List[Dict[str, Any]]:
    """Every run of one action the API lists, newest first, a running run included."""
    payload = api.json(f"/api/ports/{port}/actions/{action_id}/runs")
    return [run for run in payload.get("runs") or [] if isinstance(run, dict)]


def _run_record(api: Any, action_id: str, run_id: str, *, port: str = "loop1") -> Dict[str, Any]:
    """The run history entry for one run id, as the API reports it, once the run leaves the running state."""
    payload: List[Dict[str, Any]] = []
    for _ in range(100):
        payload = _runs_of(api, action_id, port=port)
        matches = [run for run in payload if run.get("run_id") == run_id]
        if len(matches) == 1 and matches[0].get("status") != "running":
            return matches[0]
        time.sleep(0.1)
    raise AssertionError(f"no finished run record for {run_id} after 10s: {payload}")


def _sample_progress(page: Any, limit: float = 25.0) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Read the run panel's progress bar repeatedly while the run is in flight.

    Returns every distinct bar state seen, in order, and the last full action picture. The bar is
    transient: it is right for one event only, so a scenario samples it instead of trusting one read made
    after the run ends.
    """
    seen: List[Dict[str, Any]] = []
    latest: Dict[str, Any] = {}
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        latest = page.action_state()
        bar = latest["progress"]
        if bar["shown"] and (not seen or seen[-1] != bar):
            seen.append(bar)
        if latest["outcome"]["text"] or str(latest["tag"]).startswith("Finished:"):
            break
        time.sleep(0.05)
    return seen, latest


@surfaces("web:rw", "web:ro")
def test_act01_the_actions_button_follows_the_grant_and_the_permission(servers, console, api_for_server, story):
    """The Actions button appears for a write-entitled visitor on a granted Port, and lists the scripts that Port was granted."""
    server = _actions_world(servers, "grants")
    api = api_for_server(server, "rw")
    page = console.console("web:rw", server, "loop1", user="rw")
    with step("a write-entitled page on the granted port shows the button"):
        page.wait_for_view("(v) => v.connected === true", timeout=20)
        assert page.wait_for_action_state(lambda a: a["button"])["button"] is True, page.action_state()
    with step("the roster lists one row per grant, and names each script by the name the script declares"):
        page.show_action_list()
        listed = page.wait_for_action_state(lambda a: len(a["items"]) == len(GRANTED_ON_LOOP1))
        assert sorted(str(item["id"]) for item in listed["items"]) == sorted(GRANTED_ON_LOOP1), listed["items"]
        by_id = {str(item["id"]): str(item["text"]) for item in listed["items"]}
        for action_id, declared in (("echo_probe", "Echo probe"), ("slow_noop", "Slow no-op"), ("meter_probe", "Meter probe")):
            assert declared in by_id[action_id], by_id
    with step("a port of the same server that no grant names has no button and no roster"):
        other = console.console("web:rw2", server, "cat1", user="rw")
        other.wait_for_view("(v) => v.connected === true", timeout=20)
        assert other.action_state()["button"] is False, other.action_state()
        assert other.action_state()["items"] == [], other.action_state()["items"]
    with step("a read-only visitor is refused the roster by the server, not hidden by the page"):
        status, _, _ = api_for_server(server, "ro").request("GET", "/api/ports/loop1/actions", expect=(403,))
        assert status == 403, status
        visitor = console.console("web:ro", server, "loop1", user="ro")
        visitor.wait_for_view("(v) => v.connected === true", timeout=20)
        assert visitor.action_state()["button"] is False, visitor.action_state()
    with step("the roster the page listed is the roster the API gives"):
        listed_api = api.json("/api/ports/loop1/actions")
        assert sorted(str(entry["id"]) for entry in listed_api.get("actions") or []) == sorted(GRANTED_ON_LOOP1), listed_api


@surfaces("web:rw")
def test_act02_the_page_shows_the_run_output_and_the_outcome_the_server_reported(servers, console, api_for_server, story):
    """A run started with the panel's Run button ends in the panel with the script's own lines and the server's own outcome."""
    server = _actions_world(servers, "run")
    api = api_for_server(server, "rw")
    page = console.console("web:rw", server, "loop1", user="rw")
    page.wait_for_view("(v) => v.connected === true", timeout=20)
    token = "act02-echo-me"
    with step("the run panel opens on the action's name, with one field per declared parameter"):
        page.pick_action("echo_probe")
        panel = page.wait_for_action_state(lambda a: a["run_title"] == "Echo probe")
        assert panel["form_fields"] == [{"name": "text", "type": "text", "value": "", "required": True}], panel["form_fields"]
        assert panel["form_missing_required"] == ["text"], panel["form_missing_required"]
        assert panel["run_submit_enabled"] is True, panel
    with step("one field filled, then the panel's own Run button starts the run"):
        page.fill('#actionsRunForm input[name="text"]', token)
        # Opening the overlay refetches the catalog, and that refetch rebuilds the form from the action's
        # definition. Waiting for the typed value to still be there keeps a wiped field from turning into
        # a run with different parameters, or a refusal nobody explains.
        page.wait_for_action_state(lambda a: _form_value(a, "text") == token, what="the panel to keep the parameter")
        page.click("#actionsRunSubmit")
    with step("the panel streams the run and ends on the outcome the server reported"):
        # ``echo_probe`` finishes in tens of milliseconds, sooner than one hook read costs, so a poll can
        # not rely on catching the transient "Running" status: the assertions here are on what stays.
        # A scenario that needs a live run picks a script that lasts, and ACT-03 and ACT-04 do.
        done = page.wait_for_action_state(lambda a: str(a["tag"]).startswith("Finished: success"), timeout=40)
        assert done["pane"] is True, done
        assert done["tag_class"] == "ok", done
        assert re.fullmatch(r"[0-9a-f]{12}", str(done["run_id"])), done
    with step("the panel's terminal holds the script's own lines, newest last"):
        assert any("expect_echo: matched=" in str(line) for line in done["lines"]), done["lines"]
        assert any(str(line).rstrip().endswith("done") for line in done["lines"]), done["lines"]
    with step("the outcome banner is the success one, and the transient bar is gone"):
        assert done["outcome"]["class"] == "ok", done["outcome"]
        assert "finished successfully" in str(done["outcome"]["text"]), done["outcome"]
        assert done["progress"]["shown"] is False, done["progress"]
    with step("the bytes the run sent came back through the port, so the port terminal shows them unfiltered"):
        assert token in page.text(), page.lines()[-20:]
    with step("the run the page followed is the one run the server recorded, for that user"):
        record = _run_record(api, "echo_probe", str(done["run_id"]))
        assert record["status"] == "success", record
        assert record["username"] == "rw", record
        assert len(_runs_of(api, "echo_probe")) == 1, _runs_of(api, "echo_probe")
    with step("the page that ran it is the run's operator, and its buttons say so"):
        assert done["operator"] is True, done
        assert done["close_run"] is True, done
        assert done["take_over"] is False, done
        assert done["stop"] is False, done


@surfaces("web:rw", "cli:rw")
def test_act03_a_run_borrows_the_write_slot_from_the_page_that_launched_it(servers, console, clients, story):
    """A page that launches a run on the Port it drives loses the Write slot for the run, is told so, and gets it back when the run ends."""
    server = _actions_world(servers, "slot")
    page = console.console("web:rw", server, "loop1", user="rw")
    page.wait_for_view("(v) => v.connected === true && v.mode === 'read-write'", timeout=20)
    asker = clients.cli(user="rw", surface="cli:asker", server=server)
    asker.attach(port="loop1")
    with step("the page holds the only seat, so a second writer is refused it"):
        refusal = asker.request_read_write()
        assert refusal.get("ok") is False, refusal
        for line in _lines_for(Code.PROMOTE_FAILED):
            asker.wait_for(line, timeout=10)
        holders = [str(label) for label in asker.query_holders().get("holders") or []]
        assert len(holders) == 1 and "action:" not in holders[0], holders
    with step("the page launches a script that takes a few seconds, and the run panel opens on it"):
        panel = page.pick_action("meter_probe")
        panel = page.wait_for_action_state(lambda a: a["run_title"] == "Meter probe")
        assert panel["form_fields"] == [], panel["form_fields"]
        page.click("#actionsRunSubmit")
    with step("launching sets the page's own access aside, in the server's own sentence"):
        running = page.wait_for_action_state(lambda a: a["tag"] == "Running", timeout=20)
        assert running["pane"] is True, running
        assert page.wait_for_view("(v) => v.mode === 'read-only'", timeout=20)["mode"] == "read-only", page.view()
        for sentence in _lines_for(Code.ACTION_SELF_DEMOTED):
            page.expect_notice(sentence)
    with step("the holder the Port now names is the run itself"):
        while_running = [str(label) for label in asker.query_holders().get("holders") or []]
        assert len(while_running) == 1, while_running
        assert while_running[0].startswith("[") and "action:meter_probe" in while_running[0], while_running
        assert asker.mode() == "read-only", asker.mode()
    with step("when the run ends the page has its seat back, again in the server's own sentence"):
        finished = page.wait_for_action_state(lambda a: str(a["tag"]).startswith("Finished:"), timeout=40)
        assert finished["tag"].endswith("success"), finished
        assert page.wait_for_view("(v) => v.mode === 'read-write'", timeout=20)["mode"] == "read-write", page.view()
        for sentence in _lines_for(Code.ACTION_RESTORED):
            page.expect_notice(sentence)
        restored = [str(label) for label in asker.query_holders().get("holders") or []]
        assert len(restored) == 1 and "action:" not in restored[0], restored


@surfaces("web:rw", "web:rw2")
def test_act04_a_run_never_displaces_a_human_and_never_runs_two_at_once(servers, console, api_for_server, story):
    """A launch that would displace the page's Write slot fails, one run per Port holds, and a finished run stops holding it."""
    server = _actions_world(servers, "busy")
    api = api_for_server(server, "rw")
    page = console.console("web:rw", server, "loop1", user="rw")
    page.wait_for_view("(v) => v.connected === true && v.mode === 'read-write'", timeout=20)
    with step("a launch from outside the page is answered, but the run it started cannot get the seat the page holds"):
        started = _start_run(api, "slow_noop", {"seconds": 3})
        assert started["status"] == "running", started
        record = _run_record(api, "slow_noop", started["run_id"])
        assert record["status"] == "failed", record
        assert SLOT_UNAVAILABLE_PHRASE in str(record.get("error") or ""), record
    with step("the page joins that run when it next loads the catalog, because nobody closed it"):
        # A run nobody closed stays on the Port (docs/design/port_actions.md "Late join", issue #90): the
        # first page to load the catalog joins it and streams its transcript, and starting to stream
        # closes the Actions overlay. Without dismissing it here, the page would do that mid-step and the
        # list row of the next step would vanish under the click.
        page.click("#actionsToggle")
        joined = page.wait_for_action_state(
            lambda a: a["run_id"] == started["run_id"] and str(a["tag"]).startswith("Finished:"), timeout=20
        )
        failed_run_id = str(joined["run_id"])
        assert joined["pane"] is True, joined
        assert joined["tag"] == "Finished: failed", joined
        assert joined["overlay"] is False, joined
        page.click("#actionTermClose")
    with step("the page still holds its own seat, which the failed run never took"):
        assert page.wait_for_view("(v) => v.mode === 'read-write'", timeout=20)["mode"] == "read-write", page.view()
    with step("the page launches one itself, which it may, because it gives the seat up first"):
        page.pick_action("slow_noop")
        page.fill('#actionsRunForm input[name="seconds"]', "8")
        page.wait_for_action_state(lambda a: _form_value(a, "seconds") == "8", what="the panel to keep the parameter")
        page.click("#actionsRunSubmit")
        running = page.wait_for_action_state(
            lambda a: a["tag"] == "Running" and a["pane"] and a["run_id"] != failed_run_id, timeout=20
        )
        run_id = str(running["run_id"])
        assert run_id and run_id != started["run_id"], running
        # The panel's status line names whichever run holds the Port, from the catalog refetch that opened
        # the overlay -- here that is the run this page launched a moment ago, so the line jumped past
        # "Running (run ...)" to its own holder before the first read. The id is what carries the claim.
        assert f"(run {run_id})" in str(running["run_status"]) or running["run_status"].startswith("Running (run "), running
    with step("a second launch over the API is refused while that run holds the port's one lock"):
        status, _, text = api.post("/api/ports/loop1/actions/echo_probe/run", {"params": {"text": "too-soon"}}, expect=(400,))
        assert status == 400, status
        assert ALREADY_RUNNING_PHRASE in text.lower(), text
    with step("a second page on the same Port sees which action occupies it"):
        # The launching page keeps its run panel state, and a catalog refresh redraws the roster only when
        # no run panel is open (refreshActionsCatalog() in console.js). So the marker is read from a page
        # that arrives while the run holds the Port, which is also who needs to see it. That page cannot
        # take the seat either, which the next step of this scenario already proved over the API.
        other = console.console("web:rw2", server, "loop1", user="user2")
        other.wait_for_view("(v) => v.connected === true", timeout=20)
        other.show_action_list()
        marked = other.wait_for_action_state(lambda a: any("(running)" in str(item["text"]) for item in a["items"]))
        running_rows = [item for item in marked["items"] if "(running)" in str(item["text"])]
        assert [str(item["id"]) for item in running_rows] == ["slow_noop"], marked["items"]
    with step("the run panel of another action says what the API said, and its Run button is out of reach"):
        held = page.pick_action("echo_probe")
        assert held["run_submit_title"] == "Another action is running on this port", held
        assert "another action is running on this port" in held["run_status"].lower(), held["run_status"]
        assert f"(run {run_id})" in held["run_status"], held["run_status"]
    with step("when the run ends the Port is free again, on the panel that was refused a moment ago"):
        finished = page.wait_for_action_state(lambda a: str(a["tag"]).startswith("Finished: success"), timeout=40)
        assert finished["run_id"] == run_id, finished
        # The refusal the panel showed while the run held the Port is recomputed on every catalog read
        # (updateActionsRunButton() in console.js), so its disappearance is the state a page really has.
        # The roster text is not used here: a page redraws the roster only when no run panel is open, so
        # a stale absence would prove nothing, while the marker on the second page above did.
        released = page.pick_action("echo_probe")
        assert "another action is running" not in released["run_status"].lower(), released
        assert released["run_submit_title"] == "", released
    with step("and the page can start another run, which the server records as its own"):
        previous = str(finished["run_id"])
        page.pick_action("meter_probe")
        page.click("#actionsRunSubmit")
        again = page.wait_for_action_state(
            lambda a: str(a["run_id"]) != previous and str(a["tag"]).startswith("Finished: success"),
            timeout=40,
            what="the second run to finish",
        )
        assert len(_runs_of(api, "meter_probe")) == 1, _runs_of(api, "meter_probe")
        assert str(_runs_of(api, "meter_probe")[0]["run_id"]) == again["run_id"], _runs_of(api, "meter_probe")


@surfaces("web:rw")
def test_act05_the_bar_shows_every_step_the_script_reports_and_the_last_one_without_a_percent(servers, console, story):
    """A progress event reaches the bar, a step without a percent shows as indeterminate, and the run end hides the bar."""
    server = _actions_world(servers, "progress")
    page = console.console("web:rw", server, "loop1", user="rw")
    page.wait_for_view("(v) => v.connected === true && v.mode === 'read-write'", timeout=20)
    with step("the page starts the script that reports four steps"):
        page.pick_action("meter_probe")
        page.click("#actionsRunSubmit")
        page.wait_for_action_state(lambda a: a["tag"] == "Running", timeout=20, what="the run to start")
    with step("the bar carries each step the script reported, in the order it reported them"):
        steps, last = _sample_progress(page)
        labels = [str(step["label"]) for step in steps]
        assert "warming (0%)" in labels, steps
        half, nearly = labels.index("half way (50%)"), labels.index("nearly (90%)")
        assert labels.index("warming (0%)") < half < nearly, labels
        assert [step["width"] for step in steps][:3] == ["0%", "50%", "90%"], steps
        assert all(step["indeterminate"] is False for step in steps[:3]), steps
        assert last["pane"] is True, last
    with step("the step with no percent keeps its name and loses its fill"):
        endless = [step for step in steps if step["indeterminate"]]
        assert len(endless) == 1, steps
        assert endless[0]["label"] == "finishing" and endless[0]["width"] == "", endless
    with step("the run end hides the bar, and the outcome banner answers in its place"):
        done = page.wait_for_action_state(lambda a: str(a["tag"]).startswith("Finished: success"), timeout=40)
        assert done["progress"]["shown"] is False, done
        assert done["progress"]["label"] == "", done["progress"]
        assert "finished successfully" in str(done["outcome"]["text"]), done["outcome"]


@surfaces("web:rw")
def test_act06_the_operator_prompt_shows_the_control_each_kind_needs(servers, console, story):
    """One run asks four prompts, and the page answers each with the control that kind of prompt builds."""
    server = _actions_world(servers, "prompts", GRANTED_ON_LOOP1 + ("ask_probe",))
    page = console.console("web:rw", server, "loop1", user="rw")
    page.wait_for_view("(v) => v.connected === true && v.mode === 'read-write'", timeout=20)
    with step("the page starts the script, whose first prompt arrives as a field"):
        page.pick_action("ask_probe")
        page.click("#actionsRunSubmit")
        # The pane's tag is not sampled here: this script asks its first prompt before a poll could read
        # "Running", and a run waiting on the operator carries a different tag than a running one.
        asked = page.wait_for_action_state(
            lambda a: a["prompt"]["shown"] and a["prompt"]["kind"] == "text", timeout=30, what="a text prompt"
        )
        assert asked["prompt"]["text"] == "Type a label to send", asked["prompt"]
        assert asked["prompt"]["color"] == "red", asked["prompt"]
        assert asked["prompt"]["attention"] is True, asked["prompt"]
        assert asked["progress"]["waiting"] is True, asked["progress"]
        assert asked["operator"] is True, asked
        assert asked["prompt"]["send_enabled"] is True, asked["prompt"]
        assert asked["prompt"]["buttons"] == [] and asked["prompt"]["options"] == [], asked["prompt"]
        page.answer_prompt("act06-label")
    with step("a choices prompt arrives as buttons, one per choice, and needs no Send"):
        asked = page.wait_for_action_state(lambda a: a["prompt"]["kind"] == "buttons", timeout=30, what="a button prompt")
        assert asked["prompt"]["text"] == "Pick a mode to run in", asked["prompt"]
        assert [str(label) for label in asked["prompt"]["buttons"]] == ["calm", "lively"], asked["prompt"]
        page.answer_prompt("lively")
    with step("a select prompt arrives as a dropdown holding the values the script named"):
        asked = page.wait_for_action_state(lambda a: a["prompt"]["kind"] == "select", timeout=30, what="a dropdown")
        assert asked["prompt"]["text"] == "Pick a speed", asked["prompt"]
        assert [str(value) for value in asked["prompt"]["options"]] == ["slow", "fast"], asked["prompt"]
        page.answer_prompt("fast")
    with step("a radio prompt preselects its first choice, and the page sends another one"):
        asked = page.wait_for_action_state(lambda a: a["prompt"]["kind"] == "radio", timeout=30, what="a radio prompt")
        assert [str(value) for value in asked["prompt"]["radios"]] == ["copper", "fibre"], asked["prompt"]
        assert asked["prompt"]["choice"] == "copper", asked["prompt"]
        page.answer_prompt("fibre")
    with step("every answer came back off the Port, because the script sends each one and waits for it"):
        for answer in ("act06-label", "lively", "fast", "fibre"):
            page.wait_for_text(answer, timeout=30)
    with step("the run finishes with each answer the page sent, reported by the script itself"):
        done = page.wait_for_action_state(lambda a: str(a["tag"]).startswith("Finished: success"), timeout=40)
        assert done["prompt"]["shown"] is False, done["prompt"]
        # The action pane is a fraction of the window, so the script's own line wraps across buffer lines:
        # the answers are looked for in the joined transcript, not in one line.
        transcript = " ".join(str(line) for line in done["lines"])
        assert "asked every kind" in transcript, done["lines"]
        for answer in ("act06-label", "lively", "fast", "fibre"):
            assert answer in transcript, done["lines"]


@surfaces("web:rw", "web:rw2")
def test_act07_a_second_viewer_takes_over_and_both_pages_see_the_answer_and_the_finish(servers, console, story):
    """A viewer that did not launch the run answers its prompts after a Take over, and the launcher sees every step."""
    server = _actions_world(servers, "handover", GRANTED_ON_LOOP1 + ("ask_probe",))
    launcher = console.console("web:rw", server, "loop1", user="rw")
    launcher.wait_for_view("(v) => v.connected === true && v.mode === 'read-write'", timeout=20)
    launcher.pick_action("ask_probe")
    launcher.click("#actionsRunSubmit")
    with step("the page that launched the run is the operator of the prompt it opened"):
        asked = launcher.wait_for_action_state(
            lambda a: a["prompt"]["shown"] and a["prompt"]["kind"] == "text", timeout=30, what="a text prompt"
        )
        run_id = str(asked["run_id"])
        assert launcher.wait_for_view("(v) => v.mode === 'read-only'", timeout=20)["mode"] == "read-only", launcher.view()
        assert asked["operator"] is True, asked
        assert asked["take_over"] is False, asked
        assert asked["prompt"]["readonly_note"] is False, asked["prompt"]
    with step("a second page joins the run, read-only on the Port, and is told it may not answer"):
        watcher = console.console("web:rw2", server, "loop1", user="user2")
        joined = watcher.wait_for_action_state(
            lambda a: a["run_id"] == run_id and a["prompt"]["shown"], timeout=30, what="the second page to join the run"
        )
        assert watcher.wait_for_view("(v) => v.mode === 'read-only'", timeout=20)["mode"] == "read-only", watcher.view()
        assert joined["pane"] is True, joined
        assert joined["operator"] is False, joined
        assert joined["prompt"]["text"] == "Type a label to send", joined["prompt"]
        assert joined["prompt"]["readonly_note"] is True, joined["prompt"]
        assert joined["prompt"]["send_enabled"] is False, joined["prompt"]
        assert joined["take_over"] is True, joined
    with step("it takes over, and the launcher is told in the page's own words"):
        watcher.click("#actionTermTakeOver")
        taken = watcher.wait_for_action_state(
            lambda a: a["operator"] is True and a["take_over"] is False, timeout=20, what="the takeover to land"
        )
        assert taken["prompt"]["readonly_note"] is False, taken["prompt"]
        assert taken["prompt"]["send_enabled"] is True, taken["prompt"]
        lost = launcher.wait_for_action_state(
            lambda a: "took over as operator" in str(a["toast"]["text"]), timeout=20, what="the launcher to be told"
        )
        assert lost["operator"] is False, lost
        assert lost["prompt"]["shown"] is True, lost["prompt"]
        assert lost["prompt"]["readonly_note"] is True, lost["prompt"]
        assert lost["toast"]["shown"] is True, lost["toast"]
    with step("the new operator answers all four prompts, and each answer retires the box on the other page"):
        # Every wait below reads the page that did NOT answer. A box the operator answered elsewhere has to
        # close there by itself (`operator_prompt_closed`), and the next prompt has to arrive there as a
        # copy nobody on that page can answer. A box that closed only on the answering page would leave the
        # first operator looking at a prompt the script stopped waiting for long ago.
        answers_and_next = [("act07-label", "buttons"), ("lively", "select"), ("fast", "radio")]
        for answer, next_kind in answers_and_next:
            watcher.answer_prompt(answer)
            following = launcher.wait_for_action_state(
                lambda a: a["prompt"]["kind"] == next_kind, timeout=30, what="the launcher to see the next prompt"
            )
            assert following["prompt"]["readonly_note"] is True, following["prompt"]
            assert following["prompt"]["send_enabled"] is False, following["prompt"]
        watcher.answer_prompt("copper")
        cleared = launcher.wait_for_action_state(
            lambda a: a["prompt"]["shown"] is False, timeout=30, what="the last prompt to close on the launcher"
        )
        assert cleared["progress"]["waiting"] is False, cleared["progress"]
    with step("both pages hold all four closings and the same finish"):
        for page in (launcher, watcher):
            done = page.wait_for_action_state(lambda a: str(a["tag"]).startswith("Finished: success"), timeout=40)
            transcript = " ".join(str(line) for line in done["lines"])
            assert done["run_id"] == run_id, done
            assert "operator_changed" in transcript, done["lines"]
            assert transcript.count("operator_prompt_closed (answered)") == 4, done["lines"]
            assert "asked every kind" in transcript, done["lines"]
            assert done["outcome"]["class"] == "ok", done["outcome"]
            assert done["prompt"]["shown"] is False, done["prompt"]
        assert watcher.action_state()["operator"] is True, watcher.action_state()
        assert launcher.action_state()["operator"] is False, launcher.action_state()


@surfaces("web:dl")
def test_act08_a_deep_link_starts_the_action_it_names_and_starts_nothing_without_a_required_answer(
    servers, console, api_for_server, story
):
    """``?action=…&autorun=1`` launches through the page's own call, and a missing required answer launches nothing."""
    server = _actions_world(servers, "deeplink")
    api = api_for_server(server, "rw")
    with step("the link naming one action and autorun says what it is starting, then starts it"):
        page = console.console("web:dl", server, "loop1", user="rw", params={"action": "meter_probe", "autorun": "1"})
        announced = page.wait_for_action_state(
            lambda a: a["toast"]["shown"], timeout=20, what="the toast naming the auto-start"
        )
        assert "Auto-starting action" in str(announced["toast"]["text"]), announced["toast"]
        assert "Meter probe" in str(announced["toast"]["text"]), announced["toast"]
        assert announced["run_panel"] is True and announced["run_title"] == "Meter probe", announced
        started = page.wait_for_action_state(lambda a: str(a["tag"]).startswith("Finished: success"), timeout=40)
        records = _runs_of(api, "meter_probe")
        assert len(records) == 1, records
        assert str(records[0]["run_id"]) == started["run_id"], records
    with step("the parameters the link carries arrive in the form and are the ones the run uses"):
        # One page runs both links. Page one holds the port's read-write seat after its own run ended, so a
        # second page on this port attaches read-only and its launch is refused — that is ACT-04's claim,
        # not this one. Its finished run is closed first, because a page loading a port joins any finished
        # run still open there (issue #90) and would take the panel over.
        page.click("#actionTermCloseRun")
        page.wait_for_action_state(lambda a: a["tag"] == "Closed", timeout=20)
        page.open(port="loop1", params={"action": "slow_noop", "seconds": "3", "autorun": "1"})
        page.wait_for_view("(v) => v.connected === true && v.mode === 'read-write'", timeout=20)
        panel = page.wait_for_action_state(lambda a: a["run_title"] == "Slow no-op", timeout=20)
        # 3 s comes from the URL, the parameter's default is 1 s, and a catalog refresh rebuilding the form
        # used to leave the field back at that default.
        assert _form_value(panel, "seconds") == "3", panel["form_fields"]
        started = page.wait_for_action_state(lambda a: str(a["tag"]).startswith("Finished: success"), timeout=40)
        record = _run_record(api, "slow_noop", str(started["run_id"]))
        assert float(record["ended_at"]) - float(record["started_at"]) >= 2.5, record
        assert len(_runs_of(api, "slow_noop")) == 1, _runs_of(api, "slow_noop")


@surfaces("web:nodeeplink")
def test_act09_a_deep_link_that_cannot_start_leaves_the_form_open_and_the_port_untouched(
    servers, console, api_for_server, story
):
    """An ``autorun`` link whose required answer is missing opens the prefilled form and starts no run."""
    server = _actions_world(servers, "deeplink-missing")
    api = api_for_server(server, "rw")
    page = console.console("web:nodeeplink", server, "loop1", user="rw", params={"action": "echo_probe", "autorun": "1"})
    with step("the form opens with the missing answer named, and nothing was announced or started"):
        panel = page.wait_for_action_state(lambda a: a["run_panel"] and a["run_title"] == "Echo probe", timeout=20)
        assert panel["form_missing_required"] == ["text"], panel["form_fields"]
        assert panel["run_status"] == "", panel["run_status"]
        assert page.action_state()["toast"]["shown"] is False, page.action_state()["toast"]
        assert _runs_of(api, "echo_probe") == [], _runs_of(api, "echo_probe")
    with step("the operator types the answer the link could not carry, and that starts the run"):
        page.fill('#actionsRunForm input[name="text"]', "typed-after-deeplink")
        page.wait_for_action_state(
            lambda a: _form_value(a, "text") == "typed-after-deeplink", what="the panel to keep the parameter"
        )
        page.click("#actionsRunSubmit")
        page.wait_for_action_state(lambda a: str(a["tag"]).startswith("Finished: success"), timeout=40)
        assert len(_runs_of(api, "echo_probe")) == 1, _runs_of(api, "echo_probe")


@surfaces("web:rw", "web:rw2")
def test_act10_stopping_a_run_cancels_it_and_closing_it_takes_it_off_the_port_for_everyone(
    servers, console, api_for_server, story
):
    """Stop asks first and then records cancelled; the ✕ retires the pane for one page, Close run for every page."""
    server = _actions_world(servers, "stop-close", GRANTED_ON_LOOP1 + ("ask_probe",))
    api = api_for_server(server, "rw")
    page = console.console("web:rw", server, "loop1", user="rw")
    page.wait_for_view("(v) => v.connected === true && v.mode === 'read-write'", timeout=20)
    watcher = console.console("web:rw2", server, "loop1", user="user2")
    accept_stop_dialog = [False]
    asked_for = page.answer_dialogs(lambda message: accept_stop_dialog[0])
    with step("the page starts a script that waits for an answer, and declines its own Stop button"):
        page.pick_action("ask_probe")
        page.click("#actionsRunSubmit")
        asked = page.wait_for_action_state(lambda a: a["prompt"]["shown"], timeout=30, what="a prompt")
        run_id = str(asked["run_id"])
        assert asked["stop"] is True, asked
        page.click("#actionTermStop")
        page.wait_for_dialogs(asked_for, 1)
        assert "Stop this running action script" in asked_for[0], asked_for
        still_open = page.wait_for_action_state(lambda a: a["prompt"]["shown"] and a["run_id"] == run_id, timeout=10)
        assert str(still_open["tag"]).startswith("Waiting"), still_open
        assert still_open["progress"]["waiting"] is True, still_open
        # History lists a run while it runs, not only once it ends, so a declined Stop leaves the run exactly as it was.
        running = [run for run in _runs_of(api, "ask_probe") if run.get("run_id") == run_id]
        assert len(running) == 1 and running[0]["status"] == "running", _runs_of(api, "ask_probe")
    with step("the ✕ that viewer pressed retires only that viewer's pane, and its strip brings the run back"):
        # The ✕ is a per-tab dismissal (sessionStorage), Close run is the shared one. Asserted in both
        # directions here, because a dismissal that reached the other page would hide a prompt the
        # operator still has to answer, and a shared ✕ would strand a run nobody can watch.
        joined = watcher.wait_for_action_state(
            lambda a: a["run_id"] == run_id and a["pane"] and a["prompt"]["shown"],
            timeout=30,
            what="the watcher to open the run that started on its port",
        )
        assert joined["operator"] is False, joined
        watcher.click("#actionTermClose")
        dismissed = watcher.wait_for_action_state(lambda a: a["pane"] is False and a["strip"]["shown"], timeout=20)
        assert "click to open" in str(dismissed["strip"]["text"]), dismissed["strip"]
        untouched = page.wait_for_action_state(
            lambda a: a["pane"] is True and a["prompt"]["shown"] and a["streaming"] is True, timeout=10
        )
        assert untouched["prompt"]["text"] == "Type a label to send", untouched["prompt"]
        watcher.click("#actionRunStrip")
        reopened = watcher.wait_for_action_state(
            lambda a: a["pane"] is True and a["run_id"] == run_id and a["prompt"]["shown"],
            timeout=20,
            what="the dismissed live run to reopen from its strip",
        )
        assert reopened["prompt"]["text"] == "Type a label to send", reopened["prompt"]
    with step("stopping it for real gives a cancelled run, in the page's words and in the history"):
        accept_stop_dialog[0] = True
        page.click("#actionTermStop")
        page.wait_for_dialogs(asked_for, 2)
        done = page.wait_for_action_state(lambda a: a["tag"] == "Finished: cancelled", timeout=40)
        assert done["tag_class"] == "muted", done
        assert done["run_id"] == run_id, done
        assert "Cancelled by user" in str(done["outcome"]["text"]), done["outcome"]
        assert done["outcome"]["class"] == "muted", done["outcome"]
        assert done["prompt"]["shown"] is False, done["prompt"]
        assert done["stop"] is False, done
        transcript = " ".join(str(line) for line in done["lines"])
        assert "action_cancelled: Cancelled by user" in transcript, done["lines"]
        record = _run_record(api, "ask_probe", run_id)
        assert record["status"] == "cancelled" and record["error"] == "Cancelled by user", record
    with step("the viewer that only watched shows the same cancelled finish, and loses the box it was shown too"):
        watched = watcher.wait_for_action_state(
            lambda a: a["run_id"] == run_id and a["tag"] == "Finished: cancelled", timeout=30
        )
        assert watched["outcome"]["class"] == "muted", watched["outcome"]
        # A cancelled prompt is never answered, so no `operator_prompt_closed` follows it: the finish event
        # is the only thing that can retire a box the viewer was shown. Both pages must agree.
        assert watched["prompt"]["shown"] is False, watched["prompt"]
        assert watched["progress"]["waiting"] is False, watched["progress"]
    with step("the finished run stays on the Port, offered to whoever opens it, until its operator closes it"):
        offered = api.json("/api/ports/loop1/actions")
        assert (offered.get("open_finished_run") or {}).get("run_id") == run_id, offered
        assert offered.get("active_run") is None, offered
        closer = page.wait_for_action_state(lambda a: a["close_run"] is True and a["take_over"] is False, timeout=20)
        # The strip is the cue for a page whose pane is shut: the page hides it while the pane is open, so it
        # never overlaps the prompt box. This page still holds its pane open, so its cue is the pane — the
        # Close button, over the tag that names the outcome.
        assert closer["pane"] is True and closer["tag"] == "Finished: cancelled", closer
        assert closer["strip"]["shown"] is False, closer["strip"]
    with step("the operator closes it, and both pages and the catalog drop it"):
        page.click("#actionTermCloseRun")
        closed = page.wait_for_action_state(lambda a: a["tag"] == "Closed", timeout=20)
        assert "Run closed by you" in str(closed["toast"]["text"]), closed["toast"]
        assert closed["close_run"] is False and closed["pane"] is False, closed
        also = watcher.wait_for_action_state(lambda a: a["tag"] == "Closed", timeout=20)
        assert "Run closed by another user" in str(also["toast"]["text"]), also["toast"]
        after = api.json("/api/ports/loop1/actions")
        assert after.get("open_finished_run") is None and after.get("active_run") is None, after
        records = _runs_of(api, "ask_probe")
        assert len(records) == 1 and records[0]["closed"] is True and records[0]["closed_at"], records


@surfaces("web:rw", "web:rw2", "web:late")
def test_act11_an_operator_prompt_nobody_answers_closes_on_every_page(servers, console, api_for_server, story):
    """A prompt its own timeout closes leaves no box on any open page, and none for a viewer arriving after it."""
    server = _actions_world(servers, "prompt-expiry", GRANTED_ON_LOOP1 + ("ask_timeout",))
    api = api_for_server(server, "rw")
    page = console.console("web:rw", server, "loop1", user="rw")
    page.wait_for_view("(v) => v.connected === true && v.mode === 'read-write'", timeout=20)
    page.pick_action("ask_timeout")
    page.click("#actionsRunSubmit")
    with step("two pages are shown the same open prompt"):
        asked = page.wait_for_action_state(lambda a: a["prompt"]["shown"], timeout=30, what="a prompt")
        run_id = str(asked["run_id"])
        assert asked["prompt"]["text"] == "Type a label nobody types", asked["prompt"]
        watcher = console.console("web:rw2", server, "loop1", user="user2")
        joined = watcher.wait_for_action_state(
            lambda a: a["run_id"] == run_id and a["prompt"]["shown"], timeout=30, what="the watcher to be asked"
        )
        assert joined["prompt"]["text"] == "Type a label nobody types", joined["prompt"]
        assert joined["prompt"]["readonly_note"] is True, joined["prompt"]
    with step("nobody answers, and the prompt retires itself on both pages while the run stays live"):
        # This is the only path that closes a box a page did not answer itself: an answer closes it on the
        # answering page, and a finished run hides it on its own. So the run must still be running here,
        # which the "Running" tag below is what pins down.
        for name, viewer in (("launcher", page), ("watcher", watcher)):
            expired = viewer.wait_for_action_state(
                lambda a: a["run_id"] == run_id and a["prompt"]["shown"] is False,
                timeout=30,
                what=f"{name} to lose the prompt",
            )
            transcript = " ".join(str(line) for line in expired["lines"])
            assert expired["pane"] is True, (name, expired)
            assert expired["tag"] == "Running", (name, expired)
            assert expired["progress"]["waiting"] is False, (name, expired["progress"])
            assert "waiting_for_operator" in transcript, (name, expired["lines"])
            assert "operator_prompt_closed (expired)" in transcript, (name, expired["lines"])
    with step("a page that arrives after the expiry is told the prompt came and went, and gets no box"):
        late = console.console("web:late", server, "loop1", user="rw")
        arrived = late.wait_for_action_state(
            lambda a: a["run_id"] == run_id and a["pane"] is True, timeout=30, what="the late page to join the run"
        )
        transcript = " ".join(str(line) for line in arrived["lines"])
        assert arrived["prompt"]["shown"] is False, arrived["prompt"]
        assert arrived["tag"] == "Running", arrived
        assert "operator_prompt_closed (expired)" in transcript, arrived["lines"]
    with step("the run nobody answered still finishes by itself, and every page says so"):
        for viewer in (page, watcher, late):
            done = viewer.wait_for_action_state(lambda a: str(a["tag"]).startswith("Finished: success"), timeout=40)
            assert done["run_id"] == run_id, done
            assert done["outcome"]["class"] == "ok", done["outcome"]
        assert _run_record(api, "ask_timeout", run_id)["status"] == "success", _runs_of(api, "ask_timeout")
