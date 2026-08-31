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

A grant is also the loader's scope: a script file is imported when its
filename (without the `.py` extension) matches a grant's id - see
`_load_catalog()`. A file whose `ACTION` id differs from its filename is not
imported and is listed as an id mismatch in the Script health section.
"""

import asyncio
import contextlib
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Final, List, Optional, Set, Tuple

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
    # every .py file path -> mtime as of the last catalog refresh.
    # Also covers shared helper modules (imported by bare name), so editing a
    # helper re-imports the action scripts from its directory.
    script_mtimes: Dict[str, float] = field(default_factory=dict)
    # script file path -> load-error message (issue #43): every probed .py
    # file in the actions directories that did not load as an action is
    # recorded here (import failure, syntax error, malformed run/params, no
    # module-level ACTION dict, or an ACTION id that differs from the file
    # name), so a broken script a grant points at stays visible instead of
    # being silently dropped from the catalog.
    load_errors: Dict[str, str] = field(default_factory=dict)


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


def _missing_action_note(path: Path) -> str:
    """Error message for a scoped `.py` file without an `ACTION` dict.

    Only reached when a grant names the file, so this is where an admin sees
    that the grant resolves to a helper-like file (underscore prefixed or
    imported by bare name, legitimately imported by sibling scripts) rather
    than to an action: listed with an explanatory note instead of a "broken"
    error (issue #43).
    """
    if path.name.startswith("_"):
        return "no module-level ACTION dict; helper-like file - imported by sibling scripts, not loaded as an action"
    return "no module-level ACTION dict; not an action script"


def _record_load_failure(path: Path, exc: ActionValidationError, errors: Dict[str, str]) -> bool:
    """Record one failed probe of `path` in `errors` and log it (issue #43).

    Returns True when the file lacks a module-level ACTION dict: underscore
    prefixed helper files are logged at INFO, other validation/import
    failures at ERROR. The file is always listed in `errors`, so the
    directory contents stay visible in the load errors list.
    """
    msg = str(exc)
    if "missing a module-level ACTION dict" in msg:
        errors[str(path)] = _missing_action_note(path)
        if path.name.startswith("_"):
            logger.info("Skipping non-action file %s", path.name)
        else:
            logger.error("Skipping file without an ACTION dict %s", path)
        return True
    errors[str(path)] = msg
    logger.error("Skipping invalid action script %s: %s", path, exc)
    return False


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


def _evict_stale_siblings(state: _PortActionsState) -> None:
    """Drop cached sibling-helper modules that no longer point at a current directory.

    Action scripts import sibling helpers by bare name, so the first import
    binds the module in `sys.modules` (see `registry._forget_sibling_modules()`
    for the same issue scoped to one script's directory). A module cached
    from a previous load - for example a helper from an actions directory
    that is no longer configured, as when a test or a config reload swaps
    directories in one process - would otherwise keep serving stale code to
    every re-imported script, because a per-script directory check never
    looks outside that script's own directory. Evict any cached module whose
    bare name matches a `.py` file in the current directories but whose file
    lives outside them, so the next import re-resolves via sys.path.
    """
    current_dirs = {str(Path(d).resolve()) for d in (state.actions_dir or []) if Path(d).is_dir()}
    bare_names = {p.stem for d in current_dirs for p in Path(d).glob("*.py")}
    for name in list(sys.modules):
        module_file = getattr(sys.modules[name], "__file__", None)
        if name in bare_names and module_file and str(Path(module_file).resolve().parent) not in current_dirs:
            del sys.modules[name]


def _load_catalog(
    actions_dirs: Optional[List[str]],
    mtimes: Optional[Dict[str, float]] = None,
    scoped_stems: Optional[Set[str]] = None,
) -> Tuple[Dict[str, ActionScript], Dict[str, str]]:
    """Load the scoped action scripts under `actions_dirs`; report load errors.

    A script file is imported when its filename stem matches an `action_ports`
    grant id (the filename = id convention). A grant points at a file by name
    before anything inside the file is known, so a file whose `ACTION` id
    differs from its filename is not imported and is listed as an id-mismatch
    load error instead (issue #43).
    """
    catalog: Dict[str, ActionScript] = {}
    errors: Dict[str, str] = {}
    for base in (Path(d) for d in (actions_dirs or [])):
        if not base.is_dir():
            logger.warning("port_actions actions_dir %s is not a directory; no actions loaded from it", base)
            continue
        for path in sorted(path for path in base.glob("*.py") if scoped_stems is None or path.stem in scoped_stems):
            try:
                action = load_action_from_file(str(path))
            except ActionValidationError as exc:
                # A file without an ACTION dict is listed (not just dropped)
                # when it is a grant's own file: that is the fault of the
                # script the grant points at, not of an unrelated file (#43).
                _record_load_failure(path, exc, errors)
                continue
            if action.id != path.stem:
                errors[str(path)] = (
                    f"id mismatch: filename stem is {path.stem!r} but the module-level ACTION id is "
                    f"{action.id!r}; a grant names the file by filename, so rename the file to match "
                    "the id or the grant to match the filename"
                )
                logger.error("Skipping action script with an id/stem mismatch %s: %s", path.name, action.id)
                continue
            catalog[action.id] = action
            if mtimes is not None:
                try:
                    mtimes[action.id] = path.stat().st_mtime
                except OSError:
                    pass
    return catalog, errors


def _catalog_file_mtimes(actions_dirs: Optional[List[str]]) -> Dict[str, float]:
    """Map every .py file in the actions directories to its mtime.

    Covers all files, including shared helper modules (imported by a script
    by bare name), so a change to one triggers a catalog rebuild.
    """
    mtimes: Dict[str, float] = {}
    for base in (Path(d) for d in (actions_dirs or [])):
        if not base.is_dir():
            logger.warning("port_actions actions_dir %s is not a directory; skipping", base)
            continue
        for path in sorted(base.glob("*.py")):
            try:
                mtimes[str(path)] = path.stat().st_mtime
            except OSError:
                continue
    return mtimes


def _rebuild_catalog(
    state: _PortActionsState, current_mtimes: Dict[str, float]
) -> Tuple[Dict[str, ActionScript], Dict[str, float], Dict[str, str]]:
    """Re-import the scoped action scripts; keep the last good version of a file that now fails.

    Only files a grant can name are imported (the granted-stem scope, see
    `_load_catalog()`), so a script removed from `action_ports` is dropped
    from the catalog on the next refresh. The mtime map itself still tracks
    every file in the directory (including shared helpers), so a change to a
    helper still triggers a rebuild. A file that now fails to load is
    reported in the returned errors dict (issue #43) while the catalog keeps
    its last good version; a file whose ACTION id no longer matches its
    filename is reported as an id mismatch.
    """
    catalog: Dict[str, ActionScript] = {}
    mtimes: Dict[str, float] = {}
    errors: Dict[str, str] = {}
    scoped_stems = set(state.action_ports)
    for path_str, mtime in sorted(current_mtimes.items()):
        if Path(path_str).stem not in scoped_stems:
            continue
        try:
            action = load_action_from_file(str(path_str))
        except ActionValidationError as exc:
            is_helper = _record_load_failure(Path(path_str), exc, errors)
            stale = next((a for a in state.catalog.values() if a.module_path == path_str), None)
            if stale is not None and not is_helper:
                catalog[stale.id] = stale
                mtimes[stale.id] = state.catalog_mtimes.get(stale.id, mtime)
            continue
        if action.id != Path(path_str).stem:
            errors[path_str] = (
                f"id mismatch: filename stem is {Path(path_str).stem!r} but the module-level ACTION id "
                f"is {action.id!r}; a grant names the file by filename, so rename the file to match "
                "the id or the grant to match the filename"
            )
            logger.error("Skipping action script with an id/stem mismatch %s: %s", Path(path_str).name, action.id)
            continue
        catalog[action.id] = action
        mtimes[action.id] = mtime
    return catalog, mtimes, errors


def _refresh_catalog(state: _PortActionsState) -> None:
    """Re-import action scripts that changed on disk, including sibling helper modules.

    Cheap when nothing changed: this only `stat()`s each `.py` file in the
    configured actions directories, and re-imports only when some mtime moved.
    Helper modules imported by bare name are stat'ed too, so editing one
    re-imports the action scripts from its directory - and the file-level load
    error list (issue #43) stays current alongside the catalog. A script with
    a syntax error is logged and skipped, keeping whatever version last loaded
    successfully. Called before serving the catalog or the health list and
    before launching a run, so edits take effect on a script's very next use -
    no server reload needed.
    """
    if not state.actions_dir:
        return
    # A directory created after startup becomes importable (and loadable) on
    # its next use, without a server reload.
    _sync_action_paths(state.actions_dir)
    current_mtimes = _catalog_file_mtimes(state.actions_dir)
    if current_mtimes == state.script_mtimes:
        return
    _evict_stale_siblings(state)
    state.catalog, state.catalog_mtimes, state.load_errors = _rebuild_catalog(state, current_mtimes)
    state.script_mtimes = current_mtimes
    logger.info("Port actions catalog rebuilt after script changes")


def _action_file_stems(state: _PortActionsState) -> Set[str]:
    """Stems of every `.py` file the scan probed (issue #43).

    Every such file is either in the catalog (loaded) or in `load_errors`
    (failed), so their union covers all on-disk file names the file-level
    list can name.
    """
    stems = {Path(a.module_path).stem for a in state.catalog.values()}
    stems.update(Path(path).stem for path in state.load_errors)
    return stems


def _load_errors_payload(state: _PortActionsState) -> List[Dict[str, Any]]:
    """Render `state.load_errors` plus unresolved `action_ports` grants (issue #43).

    An `action_ports` entry names an action id, not a file. A grant whose id
    matches no on-disk file (the script was deleted, or a never-written id
    was configured) is flagged, so a broken grant is visible without reading
    the config by hand. A grant that names a file the file-level list already
    covers (e.g. a helper file) is not double-listed.
    """
    out: List[Dict[str, Any]] = []
    for path, message in sorted(state.load_errors.items()):
        entry: Dict[str, Any] = {"file": Path(path).name, "path": path, "error": message}
        # A still-cataloged copy is "stale" for a load failure; a file without
        # an ACTION dict is never in the catalog, so it never flags as stale.
        if any(a.module_path == path for a in state.catalog.values()):
            entry["stale"] = True
        out.append(entry)
    stems_on_disk = _action_file_stems(state)
    for action_id, ports in sorted(state.action_ports.items()):
        if action_id in state.catalog or action_id in stems_on_disk:
            continue
        assigned_to = f"unresolved action id: assigned to {', '.join(ports)} in action_ports"
        out.append(
            {
                "file": action_id,
                "path": "",
                "error": f"{assigned_to}, but no script in the actions directories loads with this id",
            }
        )
    return out


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
    return web.json_response(
        {
            "actions": actions,
            "active_run": active_run.summary() if active_run else None,
            "action_load_errors": _load_errors_payload(state),
        }
    )


async def _handle_action_health(request: web.Request) -> web.Response:
    """Portless load-error listing (issue #43: the errors are directory-scoped).

    The per-port catalog route also carries `action_load_errors`, but the
    errors do not depend on the port, so the Config Editor queries this route
    instead of borrowing a port name from the assignments table.
    """
    adapter = request.app[ADAPTER_APP_KEY]
    state = request.app[STATE_APP_KEY]
    adapter._require_permission(request, ("read-write", "admin"))
    _refresh_catalog(state)
    return web.json_response({"action_load_errors": _load_errors_payload(state)})


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
    # helper module resolves (see `_sync_action_paths()`); and so a stale
    # helper cached from a previous directory set is evicted first.
    _sync_action_paths(state.actions_dir)
    _evict_stale_siblings(state)
    state.catalog, state.load_errors = _load_catalog(
        state.actions_dir, state.catalog_mtimes, scoped_stems=set(state.action_ports)
    )
    # Baseline for `_refresh_catalog()` (tracks every file, not just scoped ones).
    state.script_mtimes = _catalog_file_mtimes(state.actions_dir)
    if state.runner is None:
        logger.warning("port_actions plugin registered without a PortManager; actions will not be runnable")
    app[STATE_APP_KEY] = state

    base = "/api/ports/{port_name}/actions"
    app.router.add_get(base, _handle_list_actions)
    app.router.add_post(base + "/{action_id}/run", _handle_run_action)
    app.router.add_get(base + "/{action_id}/runs", _handle_list_runs)
    app.router.add_get("/ws/actions/{run_id}", _handle_ws_run_events)
    # Portless: the load errors are directory-scoped, not port-scoped (issue #43).
    app.router.add_get("/api/port-actions/health", _handle_action_health)
    return {}
