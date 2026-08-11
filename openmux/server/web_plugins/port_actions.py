"""Port Actions web plugin (see docs/design/port_actions.md, rollout phase 2).

Exposes a per-port action catalog plus run/list/live-status HTTP+WS routes on
top of the `openmux.server.actions` package. Enable the plugin's routes under
`web_console.plugins`, and configure it via its own top-level `port_actions`
config section (a sibling of `web_console`, not nested under it):

    port_actions:
      actions_dir: openmux/server/actions/examples
      action_ports:
        echo_probe: [loopback1]
        health_check: ["*"]

    web_console:
      plugins:
        - module: openmux.server.web_plugins.port_actions
          enabled: true

Actions are opt-in per port via `action_ports: {action_id: [port_name, ...]}`
(action-centric: which ports an action gets, see docs/design/port_actions.md
phase 7). `action_ports` also accepts the wildcard entry `"*"` to grant an
action to every port name requested, evaluated per-request rather than
precomputed once, so a port added later is covered without a config reload. A
port with no grant (or an action id not listed for it) exposes no actions,
matching the design doc's allow-list security model.
"""

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Final, List, Optional

from aiohttp import WSMsgType, web

from openmux.server.actions.errors import ActionValidationError, PortBusyError
from openmux.server.actions.registry import ActionScript, load_action_from_file
from openmux.server.actions.runner import ActionRunner
from openmux.server.web_plugins import ADAPTER_APP_KEY

logger = logging.getLogger("openmux.server.web_plugins.port_actions")


@dataclass
class _PortActionsState:
    catalog: Dict[str, ActionScript] = field(default_factory=dict)
    action_ports: Dict[str, List[str]] = field(default_factory=dict)
    runner: Optional[ActionRunner] = None


STATE_APP_KEY: Final = web.AppKey("openmux_port_actions_state", _PortActionsState)


def _load_catalog(actions_dir: Optional[str]) -> Dict[str, ActionScript]:
    catalog: Dict[str, ActionScript] = {}
    if not actions_dir:
        return catalog
    base = Path(actions_dir)
    if not base.is_dir():
        logger.warning("port_actions actions_dir %s is not a directory; no actions loaded", actions_dir)
        return catalog
    for path in sorted(base.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            action = load_action_from_file(str(path))
        except ActionValidationError as exc:
            logger.error("Skipping invalid action script %s: %s", path, exc)
            continue
        catalog[action.id] = action
    return catalog


def _action_summary(action: ActionScript) -> Dict[str, Any]:
    return {
        "id": action.id,
        "name": action.name,
        "description": action.description,
        "timeout": action.timeout,
        "params": [
            {
                "name": p.name,
                "type": p.type,
                "required": p.required,
                "default": p.default,
                "sensitive": p.sensitive,
                "description": p.description,
                "widget": p.widget,
                "choices": p.choices,
            }
            for p in action.params
        ],
    }


def _allowed_actions(state: _PortActionsState, port_name: str) -> Dict[str, ActionScript]:
    """Actions granted to `port_name` via the action-centric `action_ports` config key.

    See docs/design/port_actions.md phase 7: `action_ports`'s `"*"` entry grants
    that action to every port name.
    """
    ids = {action_id for action_id, ports in state.action_ports.items() if port_name in ports or "*" in ports}
    return {aid: state.catalog[aid] for aid in ids if aid in state.catalog}


async def _handle_list_actions(request: web.Request) -> web.Response:
    adapter = request.app[ADAPTER_APP_KEY]
    state = request.app[STATE_APP_KEY]
    adapter._require_permission(request, ("read-write", "admin"))
    port_name = request.match_info["port_name"]
    actions = [_action_summary(a) for a in _allowed_actions(state, port_name).values()]
    active_run = state.runner.get_active_run(port_name) if state.runner else None
    return web.json_response({"actions": actions, "active_run": active_run.summary() if active_run else None})


async def _handle_run_action(request: web.Request) -> web.Response:
    adapter = request.app[ADAPTER_APP_KEY]
    state = request.app[STATE_APP_KEY]
    username = adapter._require_permission(request, ("read-write", "admin"))
    if not adapter._check_csrf(request):
        raise web.HTTPForbidden(text="CSRF check failed")
    port_name = request.match_info["port_name"]
    action_id = request.match_info["action_id"]
    action = _allowed_actions(state, port_name).get(action_id)
    if action is None:
        raise web.HTTPNotFound(text="Action not available on this port")
    if state.runner is None:
        raise web.HTTPServiceUnavailable(text="Action runner not initialized")

    try:
        body = await request.json()
    except Exception:
        body = {}
    params = body.get("params") if isinstance(body, dict) else None
    if not isinstance(params, dict):
        params = {}
    requesting_client_id = body.get("client_id") if isinstance(body, dict) else None

    try:
        run = state.runner.launch_run(action, port_name, params, username, requesting_client_id=requesting_client_id)
    except (ActionValidationError, PortBusyError) as exc:
        logger.warning("Rejected run request for action %s on port %s (user %s): %s", action_id, port_name, username, exc)
        return web.json_response({"error": True, "message": str(exc)}, status=400)
    return web.json_response({"run_id": run.run_id, "status": run.status})


async def _handle_list_runs(request: web.Request) -> web.Response:
    state = request.app[STATE_APP_KEY]
    adapter = request.app[ADAPTER_APP_KEY]
    adapter._require_permission(request, ("read-write", "admin"))
    port_name = request.match_info["port_name"]
    action_id = request.match_info["action_id"]
    runs = [
        run.summary()
        for run in (state.runner.runs.values() if state.runner else [])
        if run.port_name == port_name and run.action_id == action_id
    ]
    runs.sort(key=lambda r: r["started_at"], reverse=True)
    return web.json_response({"runs": runs})


async def _handle_ws_run_events(request: web.Request) -> web.StreamResponse:
    """Stream this run's structured events, replaying history first for late joiners.

    A client connecting after the run started still gets the full event history
    (see docs/design/port_actions.md "Late join") before switching to live events;
    a client connecting after the run finished gets just the replayed history,
    which already ends in the recorded `action_finished` event.

    Also accepts upstream `{"type": "operator_input", "text": ...}` text frames (see
    "Operator input" in the design doc), routed to `ActionRunner.submit_operator_input()`
    gated by the `client_id` query param, which must match the run's launcher, and
    `{"type": "operator_take_over"}` frames (see "Taking over as operator"), which
    reassign the run's operator to whoever's `client_id` query param sent the frame.
    """
    state = request.app[STATE_APP_KEY]
    username = request.get("username")
    if not username:
        raise web.HTTPUnauthorized()
    run_id = request.match_info["run_id"]
    run = state.runner.runs.get(run_id) if state.runner else None
    if run is None:
        raise web.HTTPNotFound(text="Unknown run")
    operator_client_id = request.query.get("client_id")

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    history = list(run.events)
    queue = state.runner.subscribe(run_id) if run.status == "running" else None

    async def _read_operator_input() -> None:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("type") == "operator_input" and isinstance(data.get("text"), str):
                state.runner.submit_operator_input(run_id, data["text"], requesting_client_id=operator_client_id)
            elif data.get("type") == "operator_take_over" and operator_client_id:
                state.runner.take_over_operator(run_id, operator_client_id)

    reader_task = asyncio.ensure_future(_read_operator_input())
    try:
        for event in history:
            await ws.send_str(json.dumps(event))
        if queue is not None:
            while True:
                event = await queue.get()
                await ws.send_str(json.dumps(event))
                if event.get("event") == "action_finished":
                    break
    finally:
        reader_task.cancel()
        with contextlib.suppress(Exception):
            await reader_task
        if queue is not None:
            state.runner.unsubscribe(run_id, queue)
        try:
            await ws.close()
        except Exception:
            pass
    return ws


def register_plugin(app: web.Application, adapter, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # `options` is only the web_console.plugins entry (module/enabled) - the plugin's own
    # settings live in the top-level `port_actions` config section, read via the adapter's
    # `server_config` (the full raw server config, wired by main.py after adapter creation).
    server_config = getattr(adapter, "server_config", None) or {}
    section = server_config.get("port_actions") or {}
    console_manager = getattr(adapter, "console_manager", None)
    port_manager = getattr(console_manager, "port_manager", None)
    state = _PortActionsState(
        catalog=_load_catalog(section.get("actions_dir")),
        action_ports={k: list(v) for k, v in (section.get("action_ports") or {}).items()},
        runner=ActionRunner(port_manager, console_manager=console_manager) if port_manager is not None else None,
    )
    if state.runner is None:
        logger.warning("port_actions plugin registered without a PortManager; actions will not be runnable")
    app[STATE_APP_KEY] = state

    base = "/api/ports/{port_name}/actions"
    app.router.add_get(base, _handle_list_actions)
    app.router.add_post(base + "/{action_id}/run", _handle_run_action)
    app.router.add_get(base + "/{action_id}/runs", _handle_list_runs)
    app.router.add_get("/ws/actions/{run_id}", _handle_ws_run_events)
    return {}
