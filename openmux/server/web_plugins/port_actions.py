"""Port Actions web plugin (see docs/design/port_actions.md, rollout phase 2).

Exposes a per-port action catalog plus run/list/live-status HTTP+WS routes on
top of the `openmux.server.actions` package. Enable the plugin's routes under
`web_console.plugins`, and configure it via its own top-level `port_actions`
config section (a sibling of `web_console`, not nested under it):

    port_actions:
      # One directory, or a list of directories, holding action scripts
      # (e.g. a site-local directory of internal scripts next to the standard ones).
      # Each directory is also put on sys.path so a script can import sibling
      # helper modules (e.g. a shared client for an internal provisioning system):
      actions_dir:
        - openmux/server/actions/examples
        - custom_actions
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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Final, List, Optional, Set

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
    # Normalized list of action-script directories (see `_normalize_action_dirs()`).
    actions_dir: Optional[List[str]] = None
    # action id -> script file mtime as of its last (re)load; drives _refresh_catalog().
    catalog_mtimes: Dict[str, float] = field(default_factory=dict)


def _normalize_action_dirs(value: Any) -> List[str]:
    """Normalize the `actions_dir` config value to a list of directory paths.

    Accepts a single path or a list of paths (so, for example, a site-local
    directory of internal scripts can be loaded next to the standard examples
    directory). Blank entries are dropped and duplicates removed; non-string
    entries are ignored with a warning.
    """
    if value is None:
        return []
    items: List[Any] = [value] if isinstance(value, str) else list(value) if isinstance(value, (list, tuple)) else []
    if not isinstance(value, (str, list, tuple)):
        logger.warning("port_actions actions_dir must be a path or a list of paths; ignoring %r value", type(value).__name__)
        return []
    dirs: List[str] = []
    for item in items:
        if not isinstance(item, str):
            logger.warning("Ignoring non-string actions_dir entry: %r", item)
            continue
        item = item.strip()
        if item and item not in dirs:
            dirs.append(item)
    return dirs


def _is_catalog_skipped(filename: str) -> bool:
    """Whether a `.py` file in an actions directory is not an action script.

    Files starting with `_` (helpers) or `test_` (test modules co-located with
    the scripts they test) are skipped, so a directory can hold both actions
    and their tests without the catalog trying to load the tests as actions.
    """
    return filename.startswith("_") or filename.startswith("test_")


# Action-script directories this plugin inserted into `sys.path` (see
# `_sync_action_paths()`). Kept module-level so a `register_plugin()` re-run on
# a full config reload removes directories the plugin itself added earlier.
_ADDED_SYS_PATH_DIRS: Set[str] = set()


def _sync_action_paths(actions_dirs: Optional[List[str]]) -> None:
    """Put the configured action-script directories on `sys.path`.

    Each existing directory is inserted at the front of `sys.path` so an action
    script can import helper modules from its own directory (for example a
    shared client for an internal system, next to several action scripts).
    Entries are added idempotently and are removed again once a directory is
    no longer configured. A directory already on `sys.path` by other means is
    used as-is and never removed by this plugin.
    """
    wanted: Set[str] = set()
    for directory in actions_dirs or []:
        base = Path(directory)
        if base.is_dir():
            wanted.add(str(base.resolve()))
    for old in list(_ADDED_SYS_PATH_DIRS):
        if old not in wanted:
            _ADDED_SYS_PATH_DIRS.discard(old)
            if old in sys.path:
                sys.path.remove(old)
    for new in sorted(wanted):
        if new in _ADDED_SYS_PATH_DIRS or new in sys.path:
            continue
        sys.path.insert(0, new)
        _ADDED_SYS_PATH_DIRS.add(new)


STATE_APP_KEY: Final = web.AppKey("openmux_port_actions_state", _PortActionsState)


def _load_catalog(actions_dirs: Optional[List[str]], mtimes: Optional[Dict[str, float]] = None) -> Dict[str, ActionScript]:
    catalog: Dict[str, ActionScript] = {}
    for base in (Path(d) for d in (actions_dirs or [])):
        if not base.is_dir():
            logger.warning("port_actions actions_dir %s is not a directory; no actions loaded from it", base)
            continue
        for path in sorted(base.glob("*.py")):
            if _is_catalog_skipped(path.name):
                continue
            try:
                action = load_action_from_file(str(path))
            except ActionValidationError as exc:
                logger.error("Skipping invalid action script %s: %s", path, exc)
                continue
            catalog[action.id] = action
            if mtimes is not None:
                try:
                    mtimes[action.id] = path.stat().st_mtime
                except OSError:
                    pass
    return catalog


def _refresh_catalog(state: _PortActionsState) -> None:
    """Reload any action script whose file changed since it was last loaded.

    Cheap when nothing changed: this only `stat()`s each file in the configured
    actions directories and re-imports (`load_action_from_file`) just the ones whose
    mtime moved. Also picks
    up added/removed script files. Called before serving the catalog and before
    launching a run, so editing a script's ACTION metadata or run() body takes
    effect on its very next use - no server reload needed. A script with a syntax
    error is logged and skipped, keeping whatever version last loaded successfully.
    """
    if not state.actions_dir:
        return
    # A directory created after startup becomes importable (and loadable) on
    # its next use, without a server reload.
    _sync_action_paths(state.actions_dir)
    bases = [Path(d) for d in state.actions_dir]
    seen_ids = set()
    for base in bases:
        if not base.is_dir():
            logger.warning("port_actions actions_dir %s is not a directory; skipping", base)
            continue
        for path in sorted(base.glob("*.py")):
            if _is_catalog_skipped(path.name):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            existing_id = next((aid for aid, a in state.catalog.items() if a.module_path == str(path)), None)
            if existing_id is not None and state.catalog_mtimes.get(existing_id) == mtime:
                seen_ids.add(existing_id)
                continue
            try:
                action = load_action_from_file(str(path))
            except ActionValidationError as exc:
                logger.error("Skipping invalid action script %s: %s", path, exc)
                continue
            if existing_id is not None and existing_id != action.id:
                state.catalog.pop(existing_id, None)
                state.catalog_mtimes.pop(existing_id, None)
            state.catalog[action.id] = action
            state.catalog_mtimes[action.id] = mtime
            seen_ids.add(action.id)
            logger.info("Reloaded action script %s (id=%s)", path.name, action.id)

    base_set = set(bases)
    stale = [aid for aid, a in state.catalog.items() if Path(a.module_path).parent in base_set and aid not in seen_ids]
    for aid in stale:
        del state.catalog[aid]
        state.catalog_mtimes.pop(aid, None)
        logger.info("Removed action %s: script file no longer present", aid)


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
    _refresh_catalog(state)
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
    _refresh_catalog(state)
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


def _dispatch_run_ws_frame(
    state: _PortActionsState, run_id: str, operator_client_id: Optional[str], data: Dict[str, Any]
) -> None:
    """Handle one upstream JSON frame on `/ws/actions/<run_id>` (operator input/take-over/cancel)."""
    frame_type = data.get("type")
    if frame_type == "operator_input" and isinstance(data.get("text"), str):
        state.runner.submit_operator_input(run_id, data["text"], requesting_client_id=operator_client_id)
    elif frame_type == "operator_take_over" and operator_client_id:
        state.runner.take_over_operator(run_id, operator_client_id)
    elif frame_type == "cancel_run":
        state.runner.cancel_run(run_id, requesting_client_id=operator_client_id)


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
    Also accepts `{"type": "cancel_run"}` (see "Stopping a run"), routed to
    `ActionRunner.cancel_run()` under the same `client_id`-gated permission check as
    `operator_input`.
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
            _dispatch_run_ws_frame(state, run_id, operator_client_id, data)

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
        action_ports={k: list(v) for k, v in (section.get("action_ports") or {}).items()},
        runner=ActionRunner(port_manager, console_manager=console_manager) if port_manager is not None else None,
        actions_dir=_normalize_action_dirs(section.get("actions_dir")),
    )
    # Before the first catalog load, so a script's `import` of a sibling
    # helper module resolves (see `_sync_action_paths()`).
    _sync_action_paths(state.actions_dir)
    state.catalog = _load_catalog(state.actions_dir, state.catalog_mtimes)
    if state.runner is None:
        logger.warning("port_actions plugin registered without a PortManager; actions will not be runnable")
    app[STATE_APP_KEY] = state

    base = "/api/ports/{port_name}/actions"
    app.router.add_get(base, _handle_list_actions)
    app.router.add_post(base + "/{action_id}/run", _handle_run_action)
    app.router.add_get(base + "/{action_id}/runs", _handle_list_runs)
    app.router.add_get("/ws/actions/{run_id}", _handle_ws_run_events)
    return {}
