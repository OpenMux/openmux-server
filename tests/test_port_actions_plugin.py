import asyncio
import base64
import json
from pathlib import Path

import pytest
from aiohttp import ClientSession, TCPConnector

from openmux.server.adapters.loopback import LoopbackAdapter
from openmux.server.auth_manager import AuthManager
from openmux.server.console_manager import ConsoleManager
from openmux.server.port_manager import PortManager
from openmux.server.web_console import WebConsoleAdapter

ACTIONS_DIR = str(Path(__file__).resolve().parents[1] / "openmux" / "server" / "actions" / "examples")

USERS = [{"username": "u", "password_hash": "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"}]
AUTH_HEADER = {"Authorization": "Basic " + base64.b64encode(b"u:password").decode()}


async def _start_console(
    http_port: int,
    action_ports: dict,
    enable_ui: bool = False,
    max_read_write_users: int = 1,
):
    pm = PortManager([])
    loop_adapter = LoopbackAdapter("loop", {"loopback_ports": [{"name": "p1", "max_read_write_users": max_read_write_users}]})
    loop_adapter.main_port_manager = pm
    pm.set_unified_adapters([loop_adapter])
    assert await loop_adapter.start() is True

    auth = AuthManager({"users": USERS})
    cm = ConsoleManager(pm, auth)

    config = {
        "port_actions": {
            "actions_dir": ACTIONS_DIR,
            "action_ports": action_ports,
        },
        "web_console": {
            "host": "127.0.0.1",
            "port": http_port,
            "enable_ui": enable_ui,
            "enable_probes": False,
            "plugins": [
                {
                    "module": "openmux.server.web_plugins.port_actions",
                    "enabled": True,
                }
            ],
        },
    }
    web_adapter = WebConsoleAdapter("wc", config)
    # Mirrors main.py's post-creation wiring: the adapter's own `web_console` config
    # section (passed to the constructor) doesn't include sibling top-level sections.
    web_adapter.server_config = config
    web_adapter.set_auth_manager(auth)
    web_adapter.set_console_manager(cm)
    assert await web_adapter.start()
    return web_adapter, pm, loop_adapter


@pytest.mark.asyncio
async def test_list_actions_only_shows_allowed_actions():
    web_adapter, pm, loop_adapter = await _start_console(8951, {"echo_probe": ["p1"]})
    try:
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.get("http://127.0.0.1:8951/api/ports/p1/actions", headers=AUTH_HEADER) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert [a["id"] for a in data["actions"]] == ["echo_probe"]

            async with session.get("http://127.0.0.1:8951/api/ports/other/actions", headers=AUTH_HEADER) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["actions"] == []
    finally:
        await web_adapter.stop()
        await loop_adapter.stop()


@pytest.mark.asyncio
async def test_console_page_renders_actions_button_when_plugin_active():
    web_adapter, pm, loop_adapter = await _start_console(8957, {"echo_probe": ["p1"]}, enable_ui=True)
    try:
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.get("http://127.0.0.1:8957/console?port=p1", headers=AUTH_HEADER) as resp:
                assert resp.status == 200
                body = await resp.text()
                assert 'id="actionsToggle"' in body
                assert 'id="actionsOverlay"' in body
    finally:
        await web_adapter.stop()
        await loop_adapter.stop()


@pytest.mark.asyncio
async def test_list_actions_requires_auth():
    web_adapter, pm, loop_adapter = await _start_console(8952, {"echo_probe": ["p1"]})
    try:
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.get("http://127.0.0.1:8952/api/ports/p1/actions") as resp:
                assert resp.status == 401
    finally:
        await web_adapter.stop()
        await loop_adapter.stop()


@pytest.mark.asyncio
async def test_run_action_then_poll_runs_and_stream_ws():
    web_adapter, pm, loop_adapter = await _start_console(8953, {"echo_probe": ["p1"]})
    try:
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.post(
                "http://127.0.0.1:8953/api/ports/p1/actions/echo_probe/run",
                headers=AUTH_HEADER,
                json={"params": {"text": "hi"}},
            ) as resp:
                assert resp.status == 200
                data = await resp.json()
                run_id = data["run_id"]
                assert data["status"] == "running"

            # Live event stream: read until action_finished.
            events = []
            async with session.ws_connect(f"http://127.0.0.1:8953/ws/actions/{run_id}", headers=AUTH_HEADER) as ws:
                async for msg in ws:
                    events.append(json.loads(msg.data))
                    if events[-1].get("event") == "action_finished":
                        break
            assert events[-1]["status"] == "success"

            for _ in range(50):
                async with session.get(
                    "http://127.0.0.1:8953/api/ports/p1/actions/echo_probe/runs", headers=AUTH_HEADER
                ) as resp:
                    data = await resp.json()
                    if data["runs"] and data["runs"][0]["status"] != "running":
                        break
                await asyncio.sleep(0.02)
            assert data["runs"][0]["run_id"] == run_id
            assert data["runs"][0]["status"] == "success"
    finally:
        await web_adapter.stop()
        await loop_adapter.stop()


@pytest.mark.asyncio
async def test_run_action_not_allowed_on_port_returns_404():
    web_adapter, pm, loop_adapter = await _start_console(8954, {})
    try:
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.post(
                "http://127.0.0.1:8954/api/ports/p1/actions/echo_probe/run",
                headers=AUTH_HEADER,
                json={"params": {"text": "hi"}},
            ) as resp:
                assert resp.status == 404
    finally:
        await web_adapter.stop()
        await loop_adapter.stop()


@pytest.mark.asyncio
async def test_run_action_reports_failed_when_rw_slot_unavailable():
    web_adapter, pm, loop_adapter = await _start_console(8955, {"echo_probe": ["p1"]})
    try:
        assert await pm.add_client_to_port("p1", "human1", username="human1", mode="read-write") is True
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.post(
                "http://127.0.0.1:8955/api/ports/p1/actions/echo_probe/run",
                headers=AUTH_HEADER,
                json={"params": {"text": "hi"}},
            ) as resp:
                # The read-write-slot conflict is only discovered once the run's background
                # task actually tries to attach, so the POST itself still accepts the run.
                assert resp.status == 200
                run_id = (await resp.json())["run_id"]

            data = {"runs": []}
            for _ in range(50):
                async with session.get(
                    "http://127.0.0.1:8955/api/ports/p1/actions/echo_probe/runs", headers=AUTH_HEADER
                ) as resp:
                    data = await resp.json()
                    if data["runs"] and data["runs"][0]["status"] != "running":
                        break
                await asyncio.sleep(0.02)
            assert data["runs"][0]["run_id"] == run_id
            assert data["runs"][0]["status"] == "failed"
    finally:
        await web_adapter.stop()
        await loop_adapter.stop()


@pytest.mark.asyncio
async def test_late_join_ws_replays_history_then_streams_live():
    """A WS client connecting after a run has started still sees its full event history
    (docs/design/port_actions.md "Late join"), not just events published from then on."""
    web_adapter, pm, loop_adapter = await _start_console(8958, {"slow_noop": ["p1"]})
    try:
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.post(
                "http://127.0.0.1:8958/api/ports/p1/actions/slow_noop/run",
                headers=AUTH_HEADER,
                json={"params": {"seconds": 0.3}},
            ) as resp:
                assert resp.status == 200
                run_id = (await resp.json())["run_id"]

            # Wait until the run has published at least its "sleeping" event, and confirm
            # it's surfaced as the port's active_run before connecting the late-joining WS.
            active_run = None
            for _ in range(50):
                async with session.get("http://127.0.0.1:8958/api/ports/p1/actions", headers=AUTH_HEADER) as resp:
                    data = await resp.json()
                    active_run = data.get("active_run")
                if active_run is not None:
                    break
                await asyncio.sleep(0.02)
            assert active_run is not None
            assert active_run["run_id"] == run_id
            assert active_run["action_id"] == "slow_noop"
            await asyncio.sleep(0.1)  # let the "sleeping" event land before we join late

            events = []
            async with session.ws_connect(f"http://127.0.0.1:8958/ws/actions/{run_id}", headers=AUTH_HEADER) as ws:
                async for msg in ws:
                    events.append(json.loads(msg.data))
                    if events[-1].get("event") == "action_finished":
                        break
            event_names = [e.get("event") for e in events]
            assert event_names == ["action_started", "sleeping", "done", "action_finished"]
            assert events[-1]["status"] == "success"

            # The run is over: no longer surfaced as active.
            async with session.get("http://127.0.0.1:8958/api/ports/p1/actions", headers=AUTH_HEADER) as resp:
                data = await resp.json()
                assert data["active_run"] is None
    finally:
        await web_adapter.stop()
        await loop_adapter.stop()


@pytest.mark.asyncio
async def test_run_action_while_another_is_active_returns_400():
    web_adapter, pm, loop_adapter = await _start_console(8956, {"echo_probe": ["p1"], "slow_noop": ["p1"]})
    try:
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.post(
                "http://127.0.0.1:8956/api/ports/p1/actions/slow_noop/run",
                headers=AUTH_HEADER,
                json={"params": {"seconds": 1.0}},
            ) as resp:
                assert resp.status == 200

            async with session.post(
                "http://127.0.0.1:8956/api/ports/p1/actions/echo_probe/run",
                headers=AUTH_HEADER,
                json={"params": {"text": "hi"}},
            ) as resp:
                assert resp.status == 400
                data = await resp.json()
                assert data["error"] is True
    finally:
        await web_adapter.stop()
        await loop_adapter.stop()


@pytest.mark.asyncio
async def test_operator_input_ws_round_trip():
    """A `confirm_probe` run pauses for operator input (docs/design/port_actions.md
    "Operator input"); the launcher answers it over the same run WS, gated by the
    `client_id` query param matching the run's launcher client_id."""
    web_adapter, pm, loop_adapter = await _start_console(8959, {"confirm_probe": ["p1"]})
    try:
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.post(
                "http://127.0.0.1:8959/api/ports/p1/actions/confirm_probe/run",
                headers=AUTH_HEADER,
                json={"params": {"text": "hi"}, "client_id": "launcher1"},
            ) as resp:
                assert resp.status == 200
                run_id = (await resp.json())["run_id"]

            events = []
            async with session.ws_connect(
                f"http://127.0.0.1:8959/ws/actions/{run_id}?client_id=launcher1", headers=AUTH_HEADER
            ) as ws:
                async for msg in ws:
                    events.append(json.loads(msg.data))
                    if events[-1].get("event") == "waiting_for_operator":
                        await ws.send_str(json.dumps({"type": "operator_input", "text": "yes"}))
                    if events[-1].get("event") == "action_finished":
                        break
            event_names = [e.get("event") for e in events]
            assert "waiting_for_operator" in event_names
            prompt_event = next(e for e in events if e.get("event") == "waiting_for_operator")
            # confirm() renders as Yes/No buttons (see session.py's ActionSession.confirm).
            assert prompt_event["kind"] == "buttons"
            assert prompt_event["choices"] == [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}]
            assert events[-1]["status"] == "success"
    finally:
        await web_adapter.stop()
        await loop_adapter.stop()


@pytest.mark.asyncio
async def test_operator_input_select_probe_ws_round_trip():
    """`select_probe` uses `session.select()` - the script supplies the dropdown
    choices, which must be published verbatim on the `waiting_for_operator`
    event (kind="select") for the console page to render."""
    web_adapter, pm, loop_adapter = await _start_console(8961, {"select_probe": ["p1"]})
    try:
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.post(
                "http://127.0.0.1:8961/api/ports/p1/actions/select_probe/run",
                headers=AUTH_HEADER,
                json={"params": {}, "client_id": "launcher1"},
            ) as resp:
                assert resp.status == 200
                run_id = (await resp.json())["run_id"]

            events = []
            async with session.ws_connect(
                f"http://127.0.0.1:8961/ws/actions/{run_id}?client_id=launcher1", headers=AUTH_HEADER
            ) as ws:
                async for msg in ws:
                    events.append(json.loads(msg.data))
                    if events[-1].get("event") == "waiting_for_operator":
                        await ws.send_str(json.dumps({"type": "operator_input", "text": "ping"}))
                    if events[-1].get("event") == "action_finished":
                        break
            prompt_event = next(e for e in events if e.get("event") == "waiting_for_operator")
            assert prompt_event["kind"] == "select"
            assert {"label": "Ping", "value": "ping"} in prompt_event["choices"]
            assert events[-1]["status"] == "success"
    finally:
        await web_adapter.stop()
        await loop_adapter.stop()


@pytest.mark.asyncio
async def test_operator_input_ws_rejects_wrong_client_id():
    web_adapter, pm, loop_adapter = await _start_console(8960, {"confirm_probe": ["p1"]})
    try:
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.post(
                "http://127.0.0.1:8960/api/ports/p1/actions/confirm_probe/run",
                headers=AUTH_HEADER,
                json={"params": {"text": "hi"}, "client_id": "launcher1"},
            ) as resp:
                assert resp.status == 200
                run_id = (await resp.json())["run_id"]

            # Wrong client_id: connect, observe the prompt, try (and fail) to answer it. Break
            # out immediately after sending rather than draining to action_finished, since a
            # rejected answer never arrives and the run keeps waiting.
            async with session.ws_connect(
                f"http://127.0.0.1:8960/ws/actions/{run_id}?client_id=someone_else", headers=AUTH_HEADER
            ) as ws:
                async for msg in ws:
                    event = json.loads(msg.data)
                    if event.get("event") == "waiting_for_operator":
                        await ws.send_str(json.dumps({"type": "operator_input", "text": "yes"}))
                        break

            await asyncio.sleep(0.2)
            async with session.get(
                "http://127.0.0.1:8960/api/ports/p1/actions/confirm_probe/runs", headers=AUTH_HEADER
            ) as resp:
                data = await resp.json()
                assert data["runs"][0]["run_id"] == run_id
                assert data["runs"][0]["status"] == "running"  # rejected input never unblocked it

            # Correct client_id: a fresh connection can still answer the still-pending prompt.
            events = []
            async with session.ws_connect(
                f"http://127.0.0.1:8960/ws/actions/{run_id}?client_id=launcher1", headers=AUTH_HEADER
            ) as ws:
                async for msg in ws:
                    events.append(json.loads(msg.data))
                    if events[-1].get("event") == "waiting_for_operator":
                        await ws.send_str(json.dumps({"type": "operator_input", "text": "yes"}))
                    if events[-1].get("event") == "action_finished":
                        break
            assert events[-1]["status"] == "success"
    finally:
        await web_adapter.stop()
        await loop_adapter.stop()


@pytest.mark.asyncio
async def test_operator_take_over_ws_round_trip():
    """A second viewer can take over as the run's operator mid-run (mirrors the port's
    own "Force take read-write"); the previous operator is notified via `operator_changed`
    on the run's live event stream, and only the new operator can answer afterward."""
    web_adapter, pm, loop_adapter = await _start_console(8962, {"confirm_probe": ["p1"]})
    try:
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.post(
                "http://127.0.0.1:8962/api/ports/p1/actions/confirm_probe/run",
                headers=AUTH_HEADER,
                json={"params": {"text": "hi"}, "client_id": "launcher1"},
            ) as resp:
                assert resp.status == 200
                run_id = (await resp.json())["run_id"]

            launcher_events = []

            async def _drive_launcher_ws():
                async with session.ws_connect(
                    f"http://127.0.0.1:8962/ws/actions/{run_id}?client_id=launcher1", headers=AUTH_HEADER
                ) as ws:
                    async for msg in ws:
                        event = json.loads(msg.data)
                        launcher_events.append(event)
                        if event.get("event") == "action_finished":
                            break

            launcher_task = asyncio.create_task(_drive_launcher_ws())

            # Wait until the launcher's own connection has actually observed the prompt
            # before taking over, so take-over reliably happens while one is pending.
            for _ in range(50):
                if any(e.get("event") == "waiting_for_operator" for e in launcher_events):
                    break
                await asyncio.sleep(0.02)
            assert any(e.get("event") == "waiting_for_operator" for e in launcher_events)

            async with session.ws_connect(
                f"http://127.0.0.1:8962/ws/actions/{run_id}?client_id=newop", headers=AUTH_HEADER
            ) as ws:
                await ws.send_str(json.dumps({"type": "operator_take_over"}))
                # The new operator answers the still-pending prompt itself.
                await ws.send_str(json.dumps({"type": "operator_input", "text": "yes"}))
                async for msg in ws:
                    event = json.loads(msg.data)
                    if event.get("event") == "action_finished":
                        break

            await launcher_task
            assert any(
                e.get("event") == "operator_changed" and e.get("operator_client_id") == "newop" for e in launcher_events
            )
            assert launcher_events[-1]["status"] == "success"
    finally:
        await web_adapter.stop()
        await loop_adapter.stop()


@pytest.mark.asyncio
async def test_action_run_notice_broadcasts_live_to_other_console_viewers():
    """A console viewing the port's main terminal (not the Actions overlay) sees a live
    'script running'/'finished' notice the moment someone else starts a run on that port -
    no page refresh needed. Delivered as an `OMXCTRL {"type": "action_run", ...}` frame on
    the viewer's own main port WebSocket (ConsoleManager.broadcast_control_frame_to_port)."""
    web_adapter, pm, loop_adapter = await _start_console(8963, {"slow_noop": ["p1"]}, max_read_write_users=2)
    try:
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            action_run_events = []

            async def _collect_action_run_events(ws):
                async for msg in ws:
                    if not (isinstance(msg.data, str) and msg.data.startswith("OMXCTRL ")):
                        continue
                    payload = json.loads(msg.data[len("OMXCTRL ") :])
                    if payload.get("type") == "action_run":
                        action_run_events.append(payload)
                        if payload.get("event") == "action_finished":
                            return

            async with session.ws_connect("http://127.0.0.1:8963/ws/p1?meta=1", headers=AUTH_HEADER) as viewer_ws:
                collector_task = asyncio.create_task(_collect_action_run_events(viewer_ws))

                async with session.post(
                    "http://127.0.0.1:8963/api/ports/p1/actions/slow_noop/run",
                    headers=AUTH_HEADER,
                    json={"params": {"seconds": 0.1}, "client_id": "launcher1"},
                ) as resp:
                    assert resp.status == 200

                await asyncio.wait_for(collector_task, timeout=5.0)

            assert [e["event"] for e in action_run_events] == ["action_started", "action_finished"]
            assert action_run_events[0]["action_id"] == "slow_noop"
            assert action_run_events[0]["action_name"] == "Slow no-op"
            assert action_run_events[0]["operator_client_id"] == "launcher1"
            assert action_run_events[0]["run_id"] == action_run_events[1]["run_id"]
    finally:
        await web_adapter.stop()
        await loop_adapter.stop()


@pytest.mark.asyncio
async def test_action_ports_wildcard_grants_action_to_every_port():
    """`action_ports: {<id>: ["*"]}` grants the action to every port, without listing each."""
    web_adapter, pm, loop_adapter = await _start_console(8964, {"echo_probe": ["*"]})
    try:
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            for port_name in ("p1", "some_other_port"):
                async with session.get(f"http://127.0.0.1:8964/api/ports/{port_name}/actions", headers=AUTH_HEADER) as resp:
                    assert resp.status == 200
                    data = await resp.json()
                    assert [a["id"] for a in data["actions"]] == ["echo_probe"]
    finally:
        await web_adapter.stop()
        await loop_adapter.stop()


@pytest.mark.asyncio
async def test_cancel_run_ws_round_trip_stops_a_running_action():
    """A `{"type": "cancel_run"}` frame from the run's operator stops it mid-execution
    (see docs/design/port_actions.md "Stopping a run"); the run's own event stream reports
    `status == "cancelled"` in its final `action_finished` event, same as any other outcome."""
    web_adapter, pm, loop_adapter = await _start_console(8965, {"slow_noop": ["p1"]})
    try:
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.post(
                "http://127.0.0.1:8965/api/ports/p1/actions/slow_noop/run",
                headers=AUTH_HEADER,
                json={"params": {"seconds": 30.0}, "client_id": "launcher1"},
            ) as resp:
                assert resp.status == 200
                run_id = (await resp.json())["run_id"]

            events = []
            async with session.ws_connect(
                f"http://127.0.0.1:8965/ws/actions/{run_id}?client_id=launcher1", headers=AUTH_HEADER
            ) as ws:
                await ws.send_str(json.dumps({"type": "cancel_run"}))
                async for msg in ws:
                    events.append(json.loads(msg.data))
                    if events[-1].get("event") == "action_finished":
                        break

            assert events[-1]["status"] == "cancelled"
    finally:
        await web_adapter.stop()
        await loop_adapter.stop()


@pytest.mark.asyncio
async def test_cancel_run_ws_ignored_from_non_operator():
    """A `cancel_run` frame from someone other than the run's operator is silently
    ignored, mirroring `operator_input`'s permission model - the run keeps going."""
    web_adapter, pm, loop_adapter = await _start_console(8966, {"slow_noop": ["p1"]})
    try:
        async with ClientSession(connector=TCPConnector(ssl=False)) as session:
            async with session.post(
                "http://127.0.0.1:8966/api/ports/p1/actions/slow_noop/run",
                headers=AUTH_HEADER,
                json={"params": {"seconds": 0.2}, "client_id": "launcher1"},
            ) as resp:
                assert resp.status == 200
                run_id = (await resp.json())["run_id"]

            events = []
            async with session.ws_connect(
                f"http://127.0.0.1:8966/ws/actions/{run_id}?client_id=someone_else", headers=AUTH_HEADER
            ) as ws:
                await ws.send_str(json.dumps({"type": "cancel_run"}))
                async for msg in ws:
                    events.append(json.loads(msg.data))
                    if events[-1].get("event") == "action_finished":
                        break

            assert events[-1]["status"] == "success"
    finally:
        await web_adapter.stop()
        await loop_adapter.stop()
