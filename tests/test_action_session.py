import asyncio

import pytest
import pytest_asyncio

from openmux.server.actions.errors import ActionPowerError, ActionSessionError, ActionTimeoutError
from openmux.server.actions.session import ActionSession, _PowerContext


class FakePort:
    def __init__(self):
        self.client_queues = {}


class FakePortManager:
    def __init__(self):
        self.ports = {}
        self.writes = []
        self.write_ok = True

    async def write_to_port(self, port_name, data, client_id):
        self.writes.append((port_name, bytes(data), client_id))
        return self.write_ok


@pytest_asyncio.fixture
async def pm():
    manager = FakePortManager()
    port = FakePort()
    port.client_queues["c1"] = asyncio.Queue()
    manager.ports["p1"] = port
    return manager


@pytest.mark.asyncio
async def test_send_and_sendline_write_expected_bytes(pm):
    session = ActionSession(pm, "p1", "c1")
    await session.send("abc")
    await session.sendline("def")
    assert pm.writes == [
        ("p1", b"abc", "c1"),
        ("p1", b"def\n", "c1"),
    ]


@pytest.mark.asyncio
async def test_send_raises_when_write_rejected(pm):
    pm.write_ok = False
    session = ActionSession(pm, "p1", "c1")
    with pytest.raises(ActionSessionError):
        await session.send("abc")


@pytest.mark.asyncio
async def test_expect_matches_already_buffered_data(pm):
    session = ActionSession(pm, "p1", "c1")
    session._buffer.extend(b"hello world")
    matched = await session.expect(r"wor\w+", timeout=1.0)
    assert matched == "world"


@pytest.mark.asyncio
async def test_expect_matches_data_arriving_via_queue(pm):
    session = ActionSession(pm, "p1", "c1")
    queue = pm.ports["p1"].client_queues["c1"]
    await queue.put(b"partial ")
    await queue.put(b"line\n")

    matched = await session.expect(r"line", timeout=1.0)
    assert matched == "line"


@pytest.mark.asyncio
async def test_expect_times_out_when_pattern_never_matches(pm):
    session = ActionSession(pm, "p1", "c1")
    with pytest.raises(ActionTimeoutError):
        await session.expect(r"nomatch", timeout=0.05)


@pytest.mark.asyncio
async def test_expect_raises_without_a_client_queue(pm):
    session = ActionSession(pm, "missing-port", "c1")
    with pytest.raises(ActionSessionError):
        await session.expect(r"anything", timeout=0.05)


@pytest.mark.asyncio
async def test_expect_consumes_matched_bytes_so_repeat_expect_waits_for_new_data(pm):
    session = ActionSession(pm, "p1", "c1")
    queue = pm.ports["p1"].client_queues["c1"]
    await queue.put(b"login: ")

    matched = await session.expect(r"login:", timeout=1.0)
    assert matched == "login:"

    with pytest.raises(ActionTimeoutError):
        await session.expect(r"login:", timeout=0.05)

    await queue.put(b"login: ")
    matched = await session.expect(r"login:", timeout=1.0)
    assert matched == "login:"


@pytest.mark.asyncio
async def test_expect_leaves_unmatched_trailing_bytes_in_buffer(pm):
    session = ActionSession(pm, "p1", "c1")
    session._buffer.extend(b"foo bar baz")
    matched = await session.expect(r"bar", timeout=1.0)
    assert matched == "bar"
    assert bytes(session._buffer) == b" baz"


@pytest.mark.asyncio
async def test_read_buffer_returns_buffered_data_without_consuming(pm):
    session = ActionSession(pm, "p1", "c1")
    session._buffer.extend(b"hello world")
    assert session.read_buffer() == "hello world"
    assert bytes(session._buffer) == b"hello world"


@pytest.mark.asyncio
async def test_read_buffer_drains_pending_queue_data(pm):
    session = ActionSession(pm, "p1", "c1")
    queue = pm.ports["p1"].client_queues["c1"]
    await queue.put(b"partial ")
    await queue.put(b"line\n")
    assert session.read_buffer() == "partial line\n"
    assert queue.empty()


@pytest.mark.asyncio
async def test_read_buffer_consume_clears_the_buffer(pm):
    session = ActionSession(pm, "p1", "c1")
    session._buffer.extend(b"stale output")
    assert session.read_buffer(consume=True) == "stale output"
    assert bytes(session._buffer) == b""
    assert session.read_buffer() == ""


@pytest.mark.asyncio
async def test_clear_buffer_discards_buffered_data(pm):
    session = ActionSession(pm, "p1", "c1")
    session._buffer.extend(b"stale output")
    session.clear_buffer()
    assert bytes(session._buffer) == b""

    with pytest.raises(ActionTimeoutError):
        await session.expect(r"stale", timeout=0.05)


@pytest.mark.asyncio
async def test_wait_for_input_and_confirm(pm):
    session = ActionSession(pm, "p1", "c1")
    session.submit_operator_input("yes")
    assert await session.confirm("continue?", timeout=1.0) is True


@pytest.mark.asyncio
async def test_wait_for_input_times_out(pm):
    session = ActionSession(pm, "p1", "c1")
    with pytest.raises(ActionTimeoutError):
        await session.wait_for_input(prompt="anything?", timeout=0.05)


@pytest.mark.asyncio
async def test_confirm_publishes_buttons_kind_with_yes_no_choices(pm):
    seen = []
    session = ActionSession(
        pm, "p1", "c1", on_input_wait=lambda text, kind, choices, step, color: seen.append((text, kind, choices))
    )
    session.submit_operator_input("yes")
    assert await session.confirm("continue?", timeout=1.0) is True
    assert seen == [("continue?", "buttons", [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}])]


@pytest.mark.asyncio
async def test_choose_publishes_buttons_kind_with_given_choices(pm):
    seen = []
    session = ActionSession(
        pm, "p1", "c1", on_input_wait=lambda text, kind, choices, step, color: seen.append((text, kind, choices))
    )
    session.submit_operator_input("cancel")
    result = await session.choose("continue or cancel?", ["continue", "cancel"], timeout=1.0)
    assert result == "cancel"
    assert seen == [
        (
            "continue or cancel?",
            "buttons",
            [{"label": "continue", "value": "continue"}, {"label": "cancel", "value": "cancel"}],
        )
    ]


@pytest.mark.asyncio
async def test_select_publishes_select_kind_with_label_value_choices(pm):
    seen = []
    session = ActionSession(
        pm, "p1", "c1", on_input_wait=lambda text, kind, choices, step, color: seen.append((text, kind, choices))
    )
    session.submit_operator_input("115200")
    result = await session.select(
        "Pick a baud rate",
        [{"label": "9600 baud", "value": "9600"}, {"label": "115200 baud", "value": "115200"}],
        timeout=1.0,
    )
    assert result == "115200"
    assert seen == [
        (
            "Pick a baud rate",
            "select",
            [{"label": "9600 baud", "value": "9600"}, {"label": "115200 baud", "value": "115200"}],
        )
    ]


@pytest.mark.asyncio
async def test_prompt_requires_choices_for_buttons_and_select(pm):
    session = ActionSession(pm, "p1", "c1")
    with pytest.raises(ValueError):
        await session.prompt("pick one", kind="buttons", choices=[])
    with pytest.raises(ValueError):
        await session.prompt("pick one", kind="select")


@pytest.mark.asyncio
async def test_radio_publishes_radio_kind_with_given_choices(pm):
    seen = []
    session = ActionSession(
        pm, "p1", "c1", on_input_wait=lambda text, kind, choices, step, color: seen.append((text, kind, choices))
    )
    session.submit_operator_input("switch")
    result = await session.radio("Pick a device type", ["router", "switch"], timeout=1.0)
    assert result == "switch"
    assert seen == [
        (
            "Pick a device type",
            "radio",
            [{"label": "router", "value": "router"}, {"label": "switch", "value": "switch"}],
        )
    ]


@pytest.mark.asyncio
async def test_prompt_requires_choices_for_radio(pm):
    session = ActionSession(pm, "p1", "c1")
    with pytest.raises(ValueError):
        await session.prompt("pick one", kind="radio")


@pytest.mark.asyncio
async def test_wait_for_input_publishes_text_kind_with_no_choices(pm):
    seen = []
    session = ActionSession(
        pm, "p1", "c1", on_input_wait=lambda text, kind, choices, step, color: seen.append((text, kind, choices))
    )
    session.submit_operator_input("hello")
    assert await session.wait_for_input(prompt="say something", timeout=1.0) == "hello"
    assert seen == [("say something", "text", None)]


@pytest.mark.asyncio
async def test_prompt_passes_color_to_on_input_wait(pm):
    seen = []
    session = ActionSession(pm, "p1", "c1", on_input_wait=lambda text, kind, choices, step, color: seen.append(color))
    session.submit_operator_input("hello")
    await session.prompt("say something", timeout=1.0)
    session.submit_operator_input("hi")
    await session.prompt("say something", color="red", timeout=1.0)
    assert seen == ["none", "red"]


@pytest.mark.asyncio
async def test_prompt_rejects_invalid_color(pm):
    session = ActionSession(pm, "p1", "c1")
    with pytest.raises(ValueError):
        await session.prompt("pick one", color="chartreuse")


def test_progress_invokes_on_progress_with_step_and_percent(pm):
    seen = []
    session = ActionSession(pm, "p1", "c1", on_progress=lambda step, percent: seen.append((step, percent)))
    session.progress("flashing firmware", 40)
    session.progress("rebooting")
    assert seen == [("flashing firmware", 40), ("rebooting", None)]


def test_progress_rejects_out_of_range_percent(pm):
    session = ActionSession(pm, "p1", "c1")
    with pytest.raises(ValueError):
        session.progress("flashing firmware", 101)
    with pytest.raises(ValueError):
        session.progress("flashing firmware", -1)


def test_progress_callback_exceptions_are_swallowed(pm):
    def boom(step, percent):
        raise RuntimeError("boom")

    session = ActionSession(pm, "p1", "c1", on_progress=boom)
    session.progress("some step", 10)  # must not raise


@pytest.mark.asyncio
async def test_prompt_passes_last_progress_step_to_on_input_wait(pm):
    seen = []
    session = ActionSession(
        pm,
        "p1",
        "c1",
        on_input_wait=lambda text, kind, choices, step, color: seen.append(step),
    )
    session.progress("flashing firmware", 40)
    session.submit_operator_input("yes")
    await session.confirm("reboot now?", timeout=1.0)
    assert seen == ["flashing firmware"]


# --- power control (issue #93) -------------------------------------------------


class DummyPdu:
    """Stands in for PduAdapter in session power-control tests (issue #93)."""

    def __init__(self, feeds=None, states=None, blocked=None, failures=None, raising=()):
        self.feeds = feeds or {}  # port_name -> [refs]
        self.states = states or {}  # ref -> True/False/None
        self.blocked = blocked or {}  # ref -> [ports the user may not drive]
        self.failures = failures or {}  # ref -> error text (ok=False result)
        self.raising = set(raising)  # refs whose set_outlet raises
        self.calls = []  # (ref, on, user, client_id)

    def port_power_map(self, port_name):
        return list(self.feeds.get(port_name, []))

    def port_power_payload(self, port_name):
        refs = self.feeds.get(port_name)
        if not refs:
            return None
        on = sum(1 for r in refs if self.states.get(r) is True)
        state = "all" if on == len(refs) else ("none" if on == 0 else "some")
        return {
            "feeds": [{"ref": r, "on": self.states.get(r), "watts": None} for r in refs],
            "feeds_on": on,
            "feeds_total": len(refs),
            "state": state,
            "all_power_lost": state == "none",
        }

    def feed_states(self, port_name):
        return {r: self.states.get(r) for r in self.feeds.get(port_name, [])}

    def _power_blocked_ports(self, ref, username):
        return list(self.blocked.get(ref, []))

    async def set_outlet(self, ref, on, user=None, client_id=None):
        self.calls.append((ref, on, user, client_id))
        if ref in self.raising:
            raise ValueError(f"driver exploded for {ref}")
        if ref in self.failures:
            return {
                "ok": False,
                "error": self.failures[ref],
                "impact": {"change": "off", "losing_power": [], "staying_up": []},
            }
        self.states[ref] = on
        return {
            "ok": True,
            "reading": {"ref": ref, "on": on},
            "impact": {"change": "on" if on else "off", "losing_power": [], "staying_up": []},
        }


def _power_session(pm, pdu, port_name="p1", username="tester", run_client_id="action:r1"):
    return ActionSession(
        pm,
        port_name,
        "c1",
        power_context=_PowerContext(pdu=pdu, username=username, client_id=run_client_id),
    )


def test_list_power_unavailable_without_context(pm):
    assert ActionSession(pm, "p1", "c1").list_power() == {"available": False}


def test_list_power_unavailable_when_port_has_no_feeds(pm):
    pdu = DummyPdu(states={"rack1.1": True})
    session = _power_session(pm, pdu)
    assert session.list_power() == {"available": False}


def test_list_power_available_returns_payload(pm):
    pdu = DummyPdu(feeds={"p1": ["rack1.1", "rack1.2"]}, states={"rack1.1": True, "rack1.2": False})
    session = _power_session(pm, pdu)
    out = session.list_power()
    assert out["available"] is True
    assert out["feeds_total"] == 2 and out["feeds_on"] == 1 and out["state"] == "some"


@pytest.mark.asyncio
async def test_power_on_single_ref_passes_operator_identity(pm):
    pdu = DummyPdu(feeds={"p1": ["rack1.1"]}, states={"rack1.1": False})
    session = _power_session(pm, pdu, username="opsue", run_client_id="action:xyz")
    result = await session.power_on("rack1.1")
    assert result["ok"] is True
    assert pdu.calls == [("rack1.1", True, "opsue", "action:xyz")]


@pytest.mark.asyncio
async def test_power_off_single_ref_returns_impact(pm):
    pdu = DummyPdu(feeds={"p1": ["rack1.1"]}, states={"rack1.1": True})
    session = _power_session(pm, pdu)
    result = await session.power_off("rack1.1")
    assert result["ok"] is True
    assert "impact" in result


@pytest.mark.asyncio
async def test_power_raises_without_injected_context(pm):
    with pytest.raises(ActionPowerError, match="not available"):
        await ActionSession(pm, "p1", "c1").power_on("rack1.1")


@pytest.mark.asyncio
async def test_power_refuses_group_blocked_outlet(pm):
    pdu = DummyPdu(feeds={"p1": ["rack1.1"]}, states={"rack1.1": True}, blocked={"rack1.1": ["p2"]})
    session = _power_session(pm, pdu)
    with pytest.raises(ActionPowerError, match="outside your groups") as excinfo:
        await session.power_off("rack1.1")
    assert excinfo.value.ref == "rack1.1"
    assert pdu.calls == []


@pytest.mark.asyncio
async def test_power_raises_on_failed_switch(pm):
    pdu = DummyPdu(feeds={"p1": ["rack1.1"]}, states={"rack1.1": False}, failures={"rack1.1": "PDU offline"})
    session = _power_session(pm, pdu)
    with pytest.raises(ActionPowerError, match="PDU offline") as excinfo:
        await session.power_on("rack1.1")
    assert excinfo.value.ref == "rack1.1"


@pytest.mark.asyncio
async def test_power_wraps_backend_exception(pm):
    pdu = DummyPdu(feeds={"p1": ["rack1.1"]}, raising=["rack1.1"])
    session = _power_session(pm, pdu)
    with pytest.raises(ActionPowerError, match="driver exploded"):
        await session.power_on("rack1.1")


@pytest.mark.asyncio
async def test_power_refuses_ref_of_another_port(pm):
    pdu = DummyPdu(feeds={"p1": ["rack1.1"], "p2": ["rack1.9"]}, states={"rack1.9": False})
    session = _power_session(pm, pdu)
    with pytest.raises(ActionPowerError, match="not a power feed"):
        await session.power_on("rack1.9")
    assert pdu.calls == []


@pytest.mark.asyncio
async def test_power_refuses_federated_ref(pm):
    pdu = DummyPdu(feeds={"p1": ["node2::rack1.1"]}, states={"node2::rack1.1": False})
    session = _power_session(pm, pdu)
    with pytest.raises(ActionPowerError, match="federated outlet"):
        await session.power_on("node2::rack1.1")


@pytest.mark.asyncio
async def test_power_omitted_ref_skips_outlets_already_in_state(pm):
    pdu = DummyPdu(
        feeds={"p1": ["rack1.1", "rack1.2", "rack1.3"]}, states={"rack1.1": True, "rack1.2": False, "rack1.3": None}
    )
    session = _power_session(pm, pdu)
    result = await session.power_off(ref=None)
    # rack1.2 is already off -> skipped; rack1.1 is on -> switched; rack1.3 is
    # unknown -> switched (unknown never counts as "already in state").
    assert result["skipped"] == ["rack1.2"]
    assert result["changed"] == ["rack1.1", "rack1.3"]
    assert result["failed"] == []


@pytest.mark.asyncio
async def test_power_list_form_dedupes_refs(pm):
    pdu = DummyPdu(feeds={"p1": ["rack1.1"]}, states={"rack1.1": False})
    session = _power_session(pm, pdu)
    result = await session.power_on(["rack1.1", "rack1.1"])
    assert result["changed"] == ["rack1.1"]
    assert pdu.calls == [("rack1.1", True, "tester", "action:r1")]


@pytest.mark.asyncio
async def test_power_rejects_empty_ref_list(pm):
    pdu = DummyPdu(feeds={"p1": ["rack1.1"]})
    session = _power_session(pm, pdu)
    with pytest.raises(ActionPowerError, match="empty"):
        await session.power_on([])


@pytest.mark.asyncio
async def test_power_omitted_ref_without_feeds_raises(pm):
    pdu = DummyPdu(feeds={})
    session = _power_session(pm, pdu)
    with pytest.raises(ActionPowerError, match="no power feeds"):
        await session.power_off()


@pytest.mark.asyncio
async def test_power_many_one_failure_does_not_stop_the_rest(pm):
    pdu = DummyPdu(
        feeds={"p1": ["rack1.1", "rack1.2", "rack1.3"]},
        states={"rack1.1": False, "rack1.2": False, "rack1.3": False},
        failures={"rack1.2": "PDU offline"},
        blocked={"rack1.3": ["p9"]},
    )
    session = _power_session(pm, pdu)
    result = await session.power_on(ref=None)
    assert result["changed"] == ["rack1.1"]
    assert sorted(f["ref"] for f in result["failed"]) == ["rack1.2", "rack1.3"]
