"""
Centralized filesystem location resolution for OpenMux.

Every directory the server reads or writes resolves through one precedence
chain so that packaged installs pin locations via environment variables while
dev installs fall back to working-directory and per-user paths. Precedence
for each variable:

    process environment > /etc/defaults/openmux > systemd directory
    variable > built-in default

``/etc/defaults/openmux`` is plain ``KEY=VALUE`` lines (systemd
``EnvironmentFile`` compatible, so the unit and this code read the same file).
It exists only in packaged installs; a missing file is the normal case.
Keep it readable (0644): it holds locations, never secrets.

Web read-only assets resolve from the installed package instead (see the
``webui`` helpers; the tree lands there in a follow-up change).

This module is import-safe: each helper is a pure function of the current
environment and the defaults file. It performs no I/O beyond reading that
file once (cached) and needs no config object, so wiring it in does not
change behavior on a system without the file.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

# Defaults file for packaged installs. Format: systemd-compatible KEY=VALUE
# lines. Override with OPENMUX_ENV_FILE (tests, Docker, manual starts).
ENV_FILE_PATH = "/etc/defaults/openmux"
_ENV_FILE_CACHE: Optional[Dict[str, str]] = None

# Environment variables (set via /etc/defaults/openmux in packaged installs).
ENV_LOG_DIR = "OPENMUX_LOG_DIR"
ENV_RUN_DIR = "OPENMUX_RUN_DIR"
ENV_STATE_DIR = "OPENMUX_STATE_DIR"
ENV_CTL_SOCK = "OPENMUX_CTL_SOCK"

# systemd directory variables: present in the service process only when the
# unit declares the matching RuntimeDirectory=/StateDirectory=/
# LogsDirectory=. An unset value never breaks resolution; it just falls
# through to the built-in default.
_SYSTEMD_RUN_DIR = "RUNTIME_DIRECTORY"
_SYSTEMD_STATE_DIR = "STATE_DIRECTORY"
_SYSTEMD_LOG_DIR = "LOGS_DIRECTORY"

# Packaged run dir by default: the unit's RuntimeDirectory= value and the
# last-resort probe for one-shot clients (openmuxctl).
PACKAGED_RUN_DIR = "/run/openmux"

# Built-in dev defaults (working-directory-relative when no env is set).
_DEV_LOG_DIR = "logs"
_DEV_PIDFILE = os.path.join(_DEV_LOG_DIR, "openmux.pid")
_DEV_CTL_SOCK = os.path.join(_DEV_LOG_DIR, "openmux.sock")

# Subdirectories under the state dir for per-protocol material.
_STATE_MUXCON = "muxcon"
_STATE_SSH = "ssh_listener"
_STATE_WEB = "web_console"

# Read-only assets ship with the package under ``webui`` (ticket T2).
_WEBUI_PKG = "webui"


def _env_file_values() -> Dict[str, str]:
    """Read the defaults file once and cache the parsed values.

    The file is ``OPENMUX_ENV_FILE`` (tests/override) else
    ``/etc/defaults/openmux`` (packaged installs). A missing, unreadable, or
    malformed file yields an empty mapping: a packaged location override must
    never break startup.
    """
    global _ENV_FILE_CACHE
    if _ENV_FILE_CACHE is None:
        values: Dict[str, str] = {}
        path = os.environ.get("OPENMUX_ENV_FILE") or ENV_FILE_PATH
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for raw in handle:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    if key:
                        values[key] = _strip_quotes(value.strip())
        except OSError:  # justification: absent/corrupt defaults file means no override
            values = {}
        _ENV_FILE_CACHE = values
    return _ENV_FILE_CACHE


def _strip_quotes(value: str) -> str:
    """Remove one pair of matching single or double quotes from a value."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _env(name: str, default: str = "", systemd_var: str = "") -> str:
    """Resolve ``name``: process env, defaults file, systemd var, or ``default``.

    An unset or empty value at each level falls through to the next.
    ``systemd_var`` names the systemd directory variable for this location
    (``RUNTIME_DIRECTORY``/``STATE_DIRECTORY``/``LOGS_DIRECTORY``); it is set
    only by a unit that declares the matching ``*Directory=``, and an absent
    value there simply falls through to the next level. ``~`` is expanded so
    users may write home-relative paths.

    Args:
        name: Environment variable name.
        default: Value returned when no level provides one.
        systemd_var: Optional systemd directory variable to consult.

    Returns:
        str: Expanded value, or ``default``.
    """
    value = os.environ.get(name, "")
    if not value.strip():
        value = _env_file_values().get(name, "")
    if not value and systemd_var:
        value = os.environ.get(systemd_var, "")
    if not value:
        return default
    return os.path.expanduser(value).strip()


def env_value(name: str, default: str = "") -> str:
    """Read a location variable with process-env then defaults-file resolution.

    Public for standalone tools (openmuxctl) that must follow the same
    resolution as the server helpers without the systemd directory variable
    (those are set only inside the unit's own processes).
    """
    return _env(name, default)


def log_dir() -> str:
    """Base directory for all logs (aggregate log plus ``ports/``).

    Resolution: ``OPENMUX_LOG_DIR`` (env, then /etc/defaults/openmux), else
    systemd's ``$LOGS_DIRECTORY``, else the working-directory relative
    ``logs``. Relative values are left as-is (CWD-relative on use).
    """
    return _env(ENV_LOG_DIR, _DEV_LOG_DIR, _SYSTEMD_LOG_DIR)


def run_dir() -> Optional[str]:
    """Runtime directory for short-lived files (pid, control socket).

    Resolution: ``OPENMUX_RUN_DIR`` (env, then /etc/defaults/openmux), else
    systemd's ``$RUNTIME_DIRECTORY``, else ``None`` so callers fall back to
    the working-directory runtime location.
    """
    value = _env(ENV_RUN_DIR, "", _SYSTEMD_RUN_DIR)
    return value or None


def pidfile_path() -> str:
    """PID file location.

    Resolution: ``{run_dir}/openmux.pid`` when a run dir resolves (env,
    defaults file, or systemd's), else the working-directory ``logs``.
    """
    run = _env(ENV_RUN_DIR, "", _SYSTEMD_RUN_DIR)
    if run:
        return os.path.join(run, "openmux.pid")
    return _DEV_PIDFILE


def control_socket_path() -> str:
    """Control socket location.

    Resolution: ``OPENMUX_CTL_SOCK`` (env, then defaults file) when set,
    else ``{run_dir}/openmux.sock`` for the resolved run dir, else the
    working-directory ``logs/openmux.sock``. ``~`` is expanded on any value.
    """
    override = _env(ENV_CTL_SOCK)
    if override:
        return override
    run = _env(ENV_RUN_DIR, "", _SYSTEMD_RUN_DIR)
    if run:
        return os.path.join(run, "openmux.sock")
    return _DEV_CTL_SOCK


def state_dir() -> str:
    """Base directory for persistent, per-user material.

    Resolution: ``OPENMUX_STATE_DIR`` (env, then /etc/defaults/openmux), else
    systemd's ``$STATE_DIRECTORY``, else the home-relative ``~/.openmux``.
    This is where protocol material (muxcon TLS, SSH host keys, web console
    TLS) lives in the absence of a packaged location.
    """
    return _env(ENV_STATE_DIR, os.path.expanduser("~/.openmux"), _SYSTEMD_STATE_DIR)


def muxcon_tls_dir() -> str:
    """MuxCon TLS directory (certs, keys, known peers, federated cache)."""
    return os.path.join(state_dir(), _STATE_MUXCON)


def muxcon_known_peers_path() -> str:
    """MuxCon known-peers file location."""
    return os.path.join(muxcon_tls_dir(), "known_peers.yaml")


def muxcon_federated_cache_path() -> str:
    """MuxCon federated cache location."""
    return os.path.join(muxcon_tls_dir(), "federated_cache.json")


def ssh_host_key_dir() -> str:
    """SSH listener host-key directory."""
    return os.path.join(state_dir(), _STATE_SSH)


def web_tls_dir() -> str:
    """Web console auto-generated TLS directory."""
    return os.path.join(state_dir(), _STATE_WEB)


def _module_dir() -> Path:
    """Directory of this file (the ``openmux/server`` package dir)."""
    return Path(__file__).resolve().parent


def templates_dir() -> Path:
    """Jinja2 template dir shipped with the package.

    Points at ``openmux/server/webui/templates/web_console``, the location
    the tree has been moved to (T2). The web console uses this as its
    default; an explicit ``template_dir`` config value still wins.
    """
    return _module_dir() / _WEBUI_PKG / "templates" / "web_console"


def static_dir() -> Path:
    """Web static assets dir shipped with the package.

    Points at ``openmux/server/webui/static`` (see ``templates_dir``).
    """
    return _module_dir() / _WEBUI_PKG / "static"


# Location keys removed from the schema in favor of env-based resolution.
# They are accepted and stripped (with a warning) until the next minor
# release; a stale conffile upgrades smoothly instead of failing validation.
# _REMOVED_TOP_LEVEL is the single source of truth for both helpers below.
_REMOVED_TOP_LEVEL = (
    ("server", "control_socket"),
    ("server", "pidfile"),
    ("logging", "log_dir"),
    ("muxcon", "federated_cache_path"),
    ("web_console", "static_dir"),
    ("web_console", "template_dir"),
)
_REMOVED_LISTENER_KEYS = ("tls_dir", "tls_known_peers_path")


def removed_location_keys(config: Any) -> List[str]:
    """Dotted paths of removed location keys still present in ``config``.

    Pure detection, used by the deprecation shim (``absorb_removed_location_keys``)
    and by tests:

    Args:
        config: Parsed config mapping (the full server.yaml dict).

    Returns:
        List[str]: Dotted keys, e.g. ``["logging.log_dir"]``. Empty when the
        config is clean or not a mapping.
    """
    found: List[str] = []
    return _detect_removed_location_keys(config)


def _detect_removed_location_keys(config: Any) -> List[str]:
    """Detection core shared by :func:`removed_location_keys` and the shim."""
    found: List[str] = []
    if not isinstance(config, dict):
        return found
    for section, key in _REMOVED_TOP_LEVEL:
        sec = config.get(section)
        if isinstance(sec, dict) and key in sec:
            found.append(f"{section}.{key}")
    mux = config.get("muxcon")
    if isinstance(mux, dict):
        listeners = mux.get("listeners")
        if isinstance(listeners, list):
            for index, listener in enumerate(listeners):
                if isinstance(listener, dict):
                    for key in _REMOVED_LISTENER_KEYS:
                        if key in listener:
                            found.append(f"muxcon.listeners[{index}].{key}")
    return found


def absorb_removed_location_keys(config: Any, logger: Optional[Any] = None) -> List[str]:
    """Strip removed location keys from ``config`` (in place) and warn about them.

    One warning per removed key. This is the one-release deprecation shim:
    a stale conffile is loaded, warned, and ignored instead of failing the
    schema check. ConfigManager calls it right after the YAML parse and
    before validation, on every load — covering boot, SIGHUP soft reload,
    and the Config Editor reload actions (all of them re-run load_config).

    Args:
        config: Parsed config mapping (mutated in place).
        logger: Optional logger for the warnings; when omitted the keys are
            still detected and stripped silently.

    Returns:
        List[str]: The dotted key paths that were present and removed.
    """
    keys = removed_location_keys(config)
    if not isinstance(config, dict):
        return keys
    for key in _REMOVED_LISTENER_KEYS:
        mux = config.get("muxcon")
        if isinstance(mux, dict) and isinstance(mux.get("listeners"), list):
            for listener in mux["listeners"]:
                if isinstance(listener, dict) and key in listener:
                    del listener[key]
    for section, key in _REMOVED_TOP_LEVEL:
        sec = config.get(section)
        if isinstance(sec, dict) and key in sec:
            del sec[key]
    if logger is not None:
        for path in keys:
            try:
                logger.warning(
                    "Config key %s is no longer supported and is ignored; remove it from your "
                    "configuration (it is resolved via the environment in packaged installs)"
                    " until the next minor release.",
                    path,
                )
            except Exception:  # justification: warning is best-effort; the key is stripped either way
                pass
    return keys
