"""Port Actions catalog and web routes (see docs/design/port_actions.md, rollout phase 2).

Exposes a per-port action catalog plus run/list/live-status HTTP+WS routes on
top of the `openmux.server.actions` package. The routes are CORE, like the
power routes: the web console registers them on every start
(`WebConsoleAdapter._register_routes()` calls `register_routes()`), so no
`web_console.plugins` entry is needed. The feature is gated by its own
top-level `port_actions` config section instead (a sibling of `web_console`,
not nested under it):

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
        # One port name needs no list brackets: `reboot_probe: loopback1`.

With no `port_actions` section, no `actions_dir`, or no `action_ports` grant,
the routes stay registered and answer empty: no script file is imported, the
catalog stays empty, and every port reports `actions: []`. That is what lets a
`port_actions` section added or edited on a soft reload - or saved from the
Config Editor - take effect without a restart (issue #92).

`_parse_section()` is the only reader of this section, for the start-time
registration and for every later read. It never raises: a value of the wrong
shape is dropped in favour of an empty grant, with one log line naming the key.
A raise during registration would cost the routes for the whole life of the
process, because registration runs once per start and a soft reload does not
repeat it (issue #97).

Actions are opt-in per port via `action_ports: {action_id: [port_name, ...]}`
(action-centric: which ports an action gets, see docs/design/port_actions.md
phase 7). `action_ports` also accepts the wildcard entry `"*"` to grant an
action to every port name requested, evaluated per-request rather than
precomputed once, so a port added later is covered without a config reload. A
port with no grant (or an action id not listed for it) exposes no actions,
matching the design doc's allow-list security model.

The section is re-read from the running config on every use (see
`_live_section()`), so a grant or directory saved from the Config Editor or
picked up by a soft reload applies on the port's next use, without a server
reload (issue #92).

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
from openmux.server.web_plugins import get_web_adapter

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


def _normalize_grant_ports(action_id: str, raw: Any) -> Optional[List[str]]:
    """Normalize one `action_ports` value to a port-name list; None when unusable.

    One port name may be written as a plain string, like `actions_dir` accepts
    one path (issue #97). Iterating a string yields characters, which would
    grant the action to every port named by one of those letters, so a string
    names exactly one port here. A value that is neither a string nor a list
    returns None, so the caller names the action id in its warning.
    """
    if isinstance(raw, str):
        items: List[Any] = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    elif raw is None:
        items = []
    else:
        return None
    ports: List[str] = []
    for item in items:
        if not isinstance(item, str):
            logger.warning("Ignoring non-string port name in the action_ports grant for %s: %r", action_id, item)
            continue
        name = item.strip()
        if name and name not in ports:
            ports.append(name)
    return ports


def _normalize_action_grants(value: Any) -> Dict[str, List[str]]:
    """Normalize the `action_ports` config value to `{action_id: [port_name, ...]}`.

    Every entry that survives holds port names, so the two consumers can compare
    and join it without checking types: `_allowed_actions()` matches a port name,
    and `_load_errors_payload()` lists the ports of a broken grant. An entry of the
    wrong shape is dropped with a log line naming it, which is safer than guessing
    a port from it.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        logger.error(
            "port_actions action_ports must be a mapping of action id to port name(s); ignoring the %s value",
            type(value).__name__,
        )
        return {}
    grants: Dict[str, List[str]] = {}
    for raw_id, raw_ports in value.items():
        action_id = raw_id.strip() if isinstance(raw_id, str) else ""
        if not action_id:
            logger.warning("Ignoring an action_ports entry with a non-string action id: %r", raw_id)
            continue
        ports = _normalize_grant_ports(action_id, raw_ports)
        if ports is None:
            logger.warning(
                "Ignoring the action_ports grant for %s: port names must be one name or a list of names, not %s",
                action_id,
                type(raw_ports).__name__,
            )
            continue
        grants[action_id] = ports
    return grants


def _parse_section(section: Any) -> Tuple[Dict[str, List[str]], List[str]]:
    """Read a `port_actions` section into (grants, action-script directories).

    The single reader for the section shape, used by `register_routes()` at start
    and by `_apply_section()` on every later read, so one value cannot be tolerated
    by one reader and fatal for the other (issue #97). A section that is not a
    mapping returns no grants and no directories after one ERROR names its type:
    the routes stay registered and answer empty, as they do with no section at all.
    """
    if section is None:
        return {}, []
    if not isinstance(section, dict):
        logger.error(
            "The port_actions section must be a mapping of config keys; ignoring the %s value. "
            "No action script is available until the config is fixed.",
            type(section).__name__,
        )
        return {}, []
    return _normalize_action_grants(section.get("action_ports")), _normalize_action_dirs(section.get("actions_dir"))


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


# Action-script directories this module inserted into `sys.path` (see
# `_sync_action_paths()`). Kept module-level so a `register_routes()` re-run on
# a full config reload removes directories the module itself added earlier.
_ADDED_SYS_PATH_DIRS: Set[str] = set()


def _sync_action_paths(actions_dirs: Optional[List[str]]) -> None:
    """Put the configured action-script directories on `sys.path`.

    Each existing directory is inserted at the front of `sys.path` so an action
    script can import helper modules from its own directory (for example a
    shared client for an internal system, next to several action scripts).
    Entries are added idempotently and are removed again once a directory is
    no longer configured. A directory already on `sys.path` by other means is
    used as-is and never removed by this module.
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


def _running_config(adapter) -> Optional[Dict[str, Any]]:
    """Return the running server config dict, or None when unreachable.

    Probes the same `console_manager` attachment points
    `config_editor._find_config_manager()` uses: the back-reference to the
    server (`console_manager.server.config_manager`, set by main.py) first,
    then `console_manager.port_manager.config_manager`. The returned dict is
    the ConfigManager's live view, so a Config Editor save or a soft reload
    (both run `load_config`/`save_config`) is visible without any further
    reload.
    """
    console_manager = getattr(adapter, "console_manager", None)
    if console_manager is None:
        return None
    for holder in (getattr(console_manager, "server", None), getattr(console_manager, "port_manager", None), console_manager):
        cm = getattr(holder, "config_manager", None)
        cfg = getattr(cm, "config", None)
        if isinstance(cfg, dict):
            return cfg
    return None


def _live_section(adapter) -> Any:
    """Resolve the current `port_actions` section (issue #92).

    Prefers the running config, where a save or a soft reload already landed;
    falls back to the snapshot captured at `register_routes()` time when the
    running ConfigManager is not reachable (e.g. a test harness that sets
    `server_config` directly).

    Returns the section as configured rather than as a mapping: `_parse_section()`
    judges the shape and reports what it drops, so one reader cannot hide a typo
    from the other. An absent section is `{}` and not None, because
    `_refresh_catalog()` skips its config check on None and a removal must still
    clear the grants (issue #92).
    """
    cfg = _running_config(adapter)
    source = cfg if cfg is not None else (getattr(adapter, "server_config", None) or {})
    if not isinstance(source, dict):
        return {}
    section = source.get("port_actions", None)
    return {} if section is None else section


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
                    # justification: optional mtime cache; the script reloads on next check
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


def _apply_section(state: _PortActionsState, section: Any) -> bool:
    """Apply the live `port_actions` section to `state`; report a change.

    A grant or directory added or removed in the config (saved from the
    Config Editor or picked up by a soft reload) changes the import scope
    (`action_ports` is the loader's scope), so the catalog must rebuild when
    this reports a change - on the very next use, without a server reload
    (issue #92). Shapes are judged by `_parse_section()`, the same reader
    `register_routes()` uses, so a value never reads differently on the two paths.
    """
    new_ports, new_dirs = _parse_section(section)
    changed = (new_ports != state.action_ports) or (new_dirs != (state.actions_dir or []))
    if changed:
        state.action_ports = new_ports
        state.actions_dir = new_dirs
        logger.info("Port actions config changed (action_ports/actions_dir); catalog scope updated")
    return changed


def _refresh_catalog(state: _PortActionsState, section: Any = None) -> None:
    """Re-import action scripts that changed on disk, including sibling helper modules.

    Cheap when nothing changed: this only `stat()`s each `.py` file in the
    configured actions directories, and re-imports only when some mtime moved.
    Helper modules imported by bare name are stat'ed too, so editing one
    re-imports the action scripts from its directory - and the file-level load
    error list (issue #43) stays current alongside the catalog. A script with
    a syntax error is logged and skipped, keeping whatever version last loaded
    successfully. When `section` is given, a change to `action_ports` or
    `actions_dir` in it also triggers a rebuild (issue #92). Called before
    serving the catalog or the health list and before launching a run, so
    edits take effect on a script's very next use - no server reload needed.
    """
    section_changed = _apply_section(state, section) if section is not None else False
    # Cheap no-op when nothing changed on disk and no config change: a
    # directory created after startup and an edited script both show up in the
    # mtime scan on the next use, without a server reload. A config change
    # always rebuilds, because a grant or directory change is a scope change
    # the mtime map alone would not reflect (issue #92). An emptied
    # `actions_dir` clears the catalog: a removal must not leave a stale one.
    if not state.actions_dir and not section_changed:
        return
    _sync_action_paths(state.actions_dir)
    current_mtimes = _catalog_file_mtimes(state.actions_dir)
    if current_mtimes == state.script_mtimes and not section_changed:
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
    adapter = get_web_adapter(request)
    state = request.app[STATE_APP_KEY]
    adapter._require_permission(request, ("read-write", "admin"))
    port_name = request.match_info["port_name"]
    _refresh_catalog(state, _live_section(adapter))
    actions = [_action_summary(a) for a in _allowed_actions(state, port_name).values()]
    active_run = state.runner.get_active_run(port_name) if state.runner else None
    open_finished_run = state.runner.get_open_finished_run(port_name) if state.runner else None
    return web.json_response(
        {
            "actions": actions,
            "active_run": active_run.summary() if active_run else None,
            # Issue #90 slice 2: the port's finished-but-not-closed run, visible to every
            # viewer until its operator closes (acks) it. Separate from `active_run`,
            # which keeps meaning "running" and still gates the one-run-at-a-time lock.
            "open_finished_run": open_finished_run.summary() if open_finished_run else None,
            "action_load_errors": _load_errors_payload(state),
        }
    )


async def _handle_action_health(request: web.Request) -> web.Response:
    """Portless load-error listing (issue #43: the errors are directory-scoped).

    The per-port catalog route also carries `action_load_errors`, but the
    errors do not depend on the port, so the Config Editor queries this route
    instead of borrowing a port name from the assignments table.
    """
    adapter = get_web_adapter(request)
    state = request.app[STATE_APP_KEY]
    adapter._require_permission(request, ("read-write", "admin"))
    _refresh_catalog(state, _live_section(adapter))
    return web.json_response({"action_load_errors": _load_errors_payload(state)})


async def _handle_run_action(request: web.Request) -> web.Response:
    adapter = get_web_adapter(request)
    state = request.app[STATE_APP_KEY]
    username = adapter._require_permission(request, ("read-write", "admin"))
    if not adapter._check_csrf(request):
        raise web.HTTPForbidden(text="CSRF check failed")
    port_name = request.match_info["port_name"]
    action_id = request.match_info["action_id"]
    _refresh_catalog(state, _live_section(adapter))
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


def _status_entry(run: Any, state_name: str, ts: float) -> Dict[str, Any]:
    """One sidebar-annotation entry for `run` (see `_handle_action_status`).

    `ts` orders entries for the client's stale-frame guard: the finished case
    uses `ended_at`, the running case `started_at`, so a later (re)bootstrap
    can never replace a fresher live frame with an older snapshot value.
    """
    return {
        "action_id": run.action_id,
        "state": state_name,
        "run_id": run.run_id,
        "ts": ts,
        "error": run.error,
    }


def _status_payload(state: _PortActionsState) -> Dict[str, Any]:
    """Per-port sidebar-annotation map, read from the runner's live registries.

    One entry per port: the active run when the port has one (state
    ``running``), else the port's single finished-but-not-closed outcome
    (``success``/``failed``/``timeout``). Closing that run - by its operator,
    or automatically when a new run launches on the port - evicts it from the
    registry, so the dot disappears and never returns after a reload.
    ``cancelled`` runs annotate nothing: a launch already settled the port's
    previous outcome, so the port stays quiet until the next finished run.
    """
    if state.runner is None:
        return {"ports": {}}
    ports: Dict[str, Dict[str, Any]] = {}
    for run in state.runner.iter_open_finished_runs():
        if run.status in ("success", "failed", "timeout") and run.ended_at is not None:
            ports[run.port_name] = _status_entry(run, run.status, run.ended_at)
    for run in state.runner.iter_active_runs():
        ports[run.port_name] = _status_entry(run, "running", run.started_at)
    return {"ports": ports}


async def _handle_action_status(request: web.Request) -> web.Response:
    """Sidebar bootstrap: the last action outcome per port (any granted action).

    ``{"ports": {"<port>": {"action_id", "state", "run_id", "ts", "error"}}}``. A
    port gets an entry only once it had a run, so un-granted ports stay quiet
    without extra configuration. Every authenticated user may read it (the
    sidebar renders for read-only users too) - unlike the run endpoints, which
    stay read-write/admin. Live updates ride ``action_status`` frames on
    ``/ws/events`` (see docs/design/port_actions.md, "Sidebar status annotations").
    """
    state = request.app[STATE_APP_KEY]
    adapter = get_web_adapter(request)
    adapter._require_permission(request, ("read-only", "read-write", "admin"))
    return web.json_response(_status_payload(state))


async def _handle_list_runs(request: web.Request) -> web.Response:
    state = request.app[STATE_APP_KEY]
    adapter = get_web_adapter(request)
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


def _run_or_404(request: web.Request) -> Any:
    """Resolve a run from the path for the close/takeover routes (issue #90 slice 2).

    The path names port + action + run so the routes live under the same tree as the
    run history; a run whose port/action differ from the path is a mismatched URL and
    404s like an unknown run.
    """
    state = request.app[STATE_APP_KEY]
    if state.runner is None:
        raise web.HTTPServiceUnavailable(text="Action runner not initialized")
    run = state.runner.runs.get(request.match_info["run_id"])
    if run is None or run.port_name != request.match_info["port_name"] or run.action_id != request.match_info["action_id"]:
        raise web.HTTPNotFound(text="Unknown run")
    return run


async def _request_client_id(request: web.Request) -> Optional[str]:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return body.get("client_id") if isinstance(body, dict) else None


async def _handle_close_run(request: web.Request) -> web.Response:
    """Close (ack) a finished run for everyone (issue #90 slice 2).

    Only the run's operator may close it; a run with no operator (e.g. a CLI launch)
    is closeable by any read-write/admin caller. Auth/CSRF match `_handle_run_action`.
    """
    adapter = get_web_adapter(request)
    adapter._require_permission(request, ("read-write", "admin"))
    if not adapter._check_csrf(request):
        raise web.HTTPForbidden(text="CSRF check failed")
    run = _run_or_404(request)
    state = request.app[STATE_APP_KEY]
    requesting_client_id = await _request_client_id(request)
    result = state.runner.close_run(run.run_id, requesting_client_id=requesting_client_id)
    if result == "closed":
        # The closed notice fans out after the response: closing must not wait on
        # console clients. Same frame as the new-run auto-close, plus who closed it.
        asyncio.ensure_future(state.runner.broadcast_run_closed(run, requesting_client_id))
        return web.json_response({"ok": True, "run_id": run.run_id, "closed_at": run.closed_at})
    if result == "denied":
        return web.json_response(
            {"error": True, "message": "Only the run's operator can close it; take over as operator first"},
            status=403,
        )
    if result == "running":
        return web.json_response({"error": True, "message": "The run is still running; stop it first"}, status=409)
    if result == "already_closed":
        return web.json_response({"error": True, "message": "The run is already closed"}, status=409)
    raise web.HTTPNotFound(text="Unknown run")


async def _handle_takeover_finished_run(request: web.Request) -> web.Response:
    """Take over a finished-but-not-closed run to become the client allowed to close it
    (issue #90 slice 2). Mirrors the WS `operator_take_over` frame, which a finished
    run no longer has a stream to carry (its run WS closes after `action_finished`).
    A still-running run answers 409 pointing at that existing WS path.
    """
    adapter = get_web_adapter(request)
    adapter._require_permission(request, ("read-write", "admin"))
    if not adapter._check_csrf(request):
        raise web.HTTPForbidden(text="CSRF check failed")
    run = _run_or_404(request)
    state = request.app[STATE_APP_KEY]
    requesting_client_id = await _request_client_id(request)
    if not requesting_client_id:
        raise web.HTTPBadRequest(text="client_id is required")
    if run.status == "running":
        return web.json_response(
            {"error": True, "message": "The run is still running; take over over its run stream instead"},
            status=409,
        )
    if run.closed_at is not None:
        return web.json_response({"error": True, "message": "The run is already closed"}, status=409)
    state.runner.take_over_operator(run.run_id, requesting_client_id)
    return web.json_response({"ok": True, "run_id": run.run_id, "operator_client_id": run.operator_client_id})


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
        await _stream_run_events(ws, history, queue)
    finally:
        reader_task.cancel()
        with contextlib.suppress(Exception):
            await reader_task
        if queue is not None:
            state.runner.unsubscribe(run_id, queue)
        try:
            await ws.close()
        except Exception:
            # justification: shutdown cleanup; the transport may already be closed
            pass
    return ws


async def _stream_run_events(ws: web.WebSocketResponse, history: list, queue: Optional[Any]) -> None:
    """Send a run's event history, then its live queue until finished.

    History replay covers late joiners (the run's recorded events in order);
    a live queue keeps sending until the ``action_finished`` event arrives
    (a finished run has no queue - history replay is then all there is).
    """
    for event in history:
        await ws.send_str(json.dumps(event))
    if queue is not None:
        while True:
            event = await queue.get()
            await ws.send_str(json.dumps(event))
            if event.get("event") == "action_finished":
                break


def register_routes(app: web.Application, adapter) -> Dict[str, Any]:
    """Register the core Port Actions routes on the web console app; idempotent.

    The web console calls this on every start, like the power routes, so the
    endpoints exist whether or not the feature is configured: with no
    `actions_dir` or no `action_ports` grant, the catalog stays empty, no script
    file is imported, and every port reports no actions. A section of the wrong
    shape reads as no section, with one ERROR naming it (issue #97).

    The action settings are read from the top-level `port_actions` config
    section, not from a `web_console.plugins` entry: through `_live_section()`,
    which prefers the running config and falls back to the adapter's
    `server_config` (the full raw server config, wired by main.py after adapter
    creation).
    """
    if app.get(STATE_APP_KEY) is not None:
        # Already registered by the core path above. A config that still lists
        # this module under `web_console.plugins` lands here through the plugin
        # loader (`register_plugin()`), and a second `add_get` on the same path
        # would raise a duplicate-route error.
        logger.debug("Port Actions routes are already registered; ignoring the extra registration")
        return {}
    section = _live_section(adapter)
    console_manager = getattr(adapter, "console_manager", None)
    port_manager = getattr(console_manager, "port_manager", None)
    # `_parse_section()` reports a bad shape instead of raising: a raise here
    # would leave the routes unregistered for the whole life of the process,
    # because registration runs once per start (issue #97).
    grants, action_dirs = _parse_section(section)
    state = _PortActionsState(
        action_ports=grants,
        runner=(
            ActionRunner(
                port_manager,
                console_manager=console_manager,
                # Sidebar status annotations: run frames ride the web console's
                # global /ws/events hub (see docs/design/port_actions.md).
                # A web console without publish_site_event (older build) -> None -> silent.
                site_event_publisher=getattr(adapter, "publish_site_event", None),
            )
            if port_manager is not None
            else None
        ),
        actions_dir=action_dirs,
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
        logger.warning("Port Actions registered without a PortManager; actions will not be runnable")
    app[STATE_APP_KEY] = state

    base = "/api/ports/{port_name}/actions"
    app.router.add_get(base, _handle_list_actions)
    app.router.add_post(base + "/{action_id}/run", _handle_run_action)
    app.router.add_get(base + "/{action_id}/runs", _handle_list_runs)
    # Issue #90 slice 2: close (ack) / takeover of a finished run ride HTTP POSTs, not
    # the run WS - that stream closes itself right after `action_finished`.
    app.router.add_post(base + "/{action_id}/runs/{run_id}/close", _handle_close_run)
    app.router.add_post(base + "/{action_id}/runs/{run_id}/takeover", _handle_takeover_finished_run)
    app.router.add_get("/ws/actions/{run_id}", _handle_ws_run_events)
    # Portless: the load errors are directory-scoped, not port-scoped (issue #43).
    app.router.add_get("/api/port-actions/health", _handle_action_health)
    # Sidebar status annotations bootstrap (docs/design/port_actions.md).
    app.router.add_get("/api/port-actions/status", _handle_action_status)
    return {}


def register_plugin(app: web.Application, adapter, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Backward-compat entry point for a `web_console.plugins` list entry.

    The routes are core since the web console registers them itself, so a normal
    config needs no entry here. A deployment that still lists
    `openmux.server.web_plugins.port_actions` arrives through the plugin loader
    and gets the same registration, which `register_routes()` folds into a
    no-op because the core call already ran.
    """
    return register_routes(app, adapter)
