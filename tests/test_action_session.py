import asyncio

import pytest
import pytest_asyncio

from openmux.server.actions.errors import ActionSessionError, ActionTimeoutError
from openmux.server.actions.session import ActionSession


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
    session = ActionSession(pm, "p1", "c1", on_input_wait=lambda text, kind, choices, step: seen.append((text, kind, choices)))
    session.submit_operator_input("yes")
    assert await session.confirm("continue?", timeout=1.0) is True
    assert seen == [("continue?", "buttons", [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}])]


@pytest.mark.asyncio
async def test_choose_publishes_buttons_kind_with_given_choices(pm):
    seen = []
    session = ActionSession(pm, "p1", "c1", on_input_wait=lambda text, kind, choices, step: seen.append((text, kind, choices)))
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
    session = ActionSession(pm, "p1", "c1", on_input_wait=lambda text, kind, choices, step: seen.append((text, kind, choices)))
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
    session = ActionSession(pm, "p1", "c1", on_input_wait=lambda text, kind, choices, step: seen.append((text, kind, choices)))
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
    session = ActionSession(pm, "p1", "c1", on_input_wait=lambda text, kind, choices, step: seen.append((text, kind, choices)))
    session.submit_operator_input("hello")
    assert await session.wait_for_input(prompt="say something", timeout=1.0) == "hello"
    assert seen == [("say something", "text", None)]


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
        on_input_wait=lambda text, kind, choices, step: seen.append(step),
    )
    session.progress("flashing firmware", 40)
    session.submit_operator_input("yes")
    await session.confirm("reboot now?", timeout=1.0)
    assert seen == ["flashing firmware"]
