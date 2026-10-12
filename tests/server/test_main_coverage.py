import asyncio
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, cast

import pytest
import yaml

from openmux.server.adapters.base_adapter import AdapterCapability, BaseGenericAdapter
from openmux.server.data_logger import DataLogger
from openmux.server.main import (
    OpenMuxServer,
    _find_config_file,
    _parse_arguments,
    _read_logging_block,
    _resolve_logging_paths,
    _setup_basic_logging,
)


class FakeAdapter(BaseGenericAdapter):
    """Minimal fake unified adapter to exercise server flows."""

    def __init__(
        self,
        name: str,
        adapter_type: str = "generic",
        capabilities: Optional[Set[AdapterCapability]] = None,
        start_ok: bool = True,
        is_running: bool = False,
    ):
        # Ensure capability backing field exists before base __init__ queries get_capabilities
        self._capabilities = capabilities or set()
        self.adapter_type = adapter_type
        super().__init__(name, config={})
        self._start_ok = start_ok
        self.is_running = is_running
        self._started = 0
        self._stopped = 0
        # Will be set by server wiring
        self.main_port_manager = None
        self._auth = None
        self._console = None

    def get_capabilities(self) -> Set[AdapterCapability]:
        return set(self._capabilities)

    async def _do_start(self) -> bool:
        self._started += 1
        return self._start_ok

    async def stop(self) -> None:
        self._stopped += 1
        self.is_running = False

    def set_auth_manager(self, auth):
        self._auth = auth

    def set_console_manager(self, console):
        self._console = console

    def get_status_info(self) -> Dict[str, Any]:
        # Provide keys referenced by _log_server_status
        endpoint = "tcp://0.0.0.0:1234" if AdapterCapability.ACCEPTS_CONNECTIONS in self._capabilities else "N/A"
        return {
            "type": self.adapter_type,
            "endpoint": endpoint,
            "clients": 0,
            "ports": "0 active",
        }

    # BaseGenericAdapter abstract method implementations
    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> bool:  # pragma: no cover - not used in this fake
        return True

    async def create_port(self, port_name: str, config: Dict[str, Any]) -> Optional[Any]:  # pragma: no cover
        return None

    async def destroy_port(self, port_name: str) -> None:  # pragma: no cover
        return None

    def get_port_configurations(self) -> Dict[str, Dict[str, Any]]:  # pragma: no cover
        return {}


class FakeSerialAdapter(FakeAdapter):
    def __init__(self, name: str = "serial", ports: Optional[List[Dict[str, Any]]] = None):
        super().__init__(name, adapter_type="serial", capabilities={AdapterCapability.PROVIDES_PORTS})
        self._last_reconcile: Optional[Dict[str, Any]] = None
        self._ports = ports or []

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> bool:
        # Accept any dict with serial_ports list
        try:
            sp = config.get("serial_ports")
            return isinstance(sp, list)
        except Exception:
            return False

    async def reconcile_ports(self, config: Dict[str, Any]) -> Dict[str, Any]:
        self._last_reconcile = dict(config)
        # Pretend nothing changed for simplicity
        return {
            "added": [],
            "removed": [],
            "updated": [],
            "unchanged": [p.get("name") for p in config.get("serial_ports", [])],
        }


def write_temp_config(tmp_path) -> str:
    server_cfg = {
        "server": {"host": "127.0.0.1", "port": 0},
        "logging": {"level": "INFO"},
    }
    auth_cfg = {
        "users": [
            {
                "username": "test",
                "password_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                "permissions": "admin",
            }
        ]
    }
    server_path = tmp_path / "server.yaml"
    auth_path = tmp_path / "authentication.yaml"
    server_path.write_text(yaml.safe_dump(server_cfg, sort_keys=False))
    auth_path.write_text(yaml.safe_dump(auth_cfg, sort_keys=False))
    return str(server_path)


def test_parse_arguments_defaults(monkeypatch):
    monkeypatch.setenv("PYTHONWARNINGS", "ignore")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    argv = ["prog"]  # no args
    monkeypatch.setattr(sys, "argv", argv, raising=False)
    args = _parse_arguments()
    assert args.config.endswith("server.yaml")
    assert args.verbose == 0


def test_find_config_file_missing_path_exits():
    # There is no silent fallback to a bundled config: a missing path is a
    # hard error (exit 1), telling the user to pass -c/--config or
    # --config-dir, run make init-config, or install to /etc/openmux.
    with pytest.raises(SystemExit):
        _find_config_file("/no/such/config/file.yaml")


def test_find_config_file_existing_path_returns_unchanged(tmp_path):
    cfg = tmp_path / "server.yaml"
    cfg.write_text("server:\n  id: x\n")
    assert _find_config_file(str(cfg)) == str(cfg)


def test_setup_basic_logging_idempotent(tmp_path, caplog):
    # Switch CWD so logs directory is safe to create
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        _setup_basic_logging("INFO")
        _setup_basic_logging("DEBUG")
        # Logs directory created and contains base log file
        assert (tmp_path / "logs").exists()
    finally:
        os.chdir(old_cwd)


def test_resolve_logging_paths_combinations(tmp_path):
    # Both unset: historical cwd-relative default
    base, main = _resolve_logging_paths(None, None)
    assert base == Path("logs") and main == Path("logs") / "openmux.log"

    # log_dir only: main file is {log_dir}/openmux.log
    base, main = _resolve_logging_paths(str(tmp_path), None)
    assert base == tmp_path and main == tmp_path / "openmux.log"

    # file only: base becomes the file's own directory
    base, main = _resolve_logging_paths(None, str(tmp_path / "custom" / "main.log"))
    assert base == tmp_path / "custom" and main == tmp_path / "custom" / "main.log"

    # both: base from log_dir, file verbatim
    base, main = _resolve_logging_paths(str(tmp_path / "logs"), str(tmp_path / "elsewhere" / "x.log"))
    assert base == tmp_path / "logs" and main == tmp_path / "elsewhere" / "x.log"


def test_resolve_logging_paths_env_default(tmp_path, monkeypatch):
    # Both unset + OPENMUX_LOG_DIR set: base from the env var.
    monkeypatch.setenv("OPENMUX_LOG_DIR", str(tmp_path / "envlogs"))
    base, main = _resolve_logging_paths(None, None)
    assert base == tmp_path / "envlogs" and main == tmp_path / "envlogs" / "openmux.log"


def test_setup_basic_logging_honors_log_dir_and_file(tmp_path):
    log_dir = str(tmp_path / "srvlogs")
    _setup_basic_logging("INFO", log_dir=log_dir)
    assert (tmp_path / "srvlogs" / "openmux.log").exists()
    assert (tmp_path / "srvlogs" / "openmux_server.log").exists()


def test_setup_basic_logging_file_collision_skips_component(tmp_path, caplog):
    # `file` named like a component log in the same dir: one writer, one file
    d = tmp_path / "collide"
    _setup_basic_logging("INFO", log_dir=str(d), log_file=str(d / "openmux_server.log"))
    root = logging.getLogger()
    main_handlers = [h for h in root.handlers if getattr(h, "baseFilename", None) == str(d / "openmux_server.log")]
    assert len(main_handlers) == 1
    comp = logging.getLogger("openmux.server")
    assert not [h for h in comp.handlers if getattr(h, "baseFilename", None) == str(d / "openmux_server.log")]
    # A record from the colliding component lands exactly once
    comp.info("collision-marker-line")
    content = (d / "openmux_server.log").read_text()
    assert content.count("collision-marker-line") == 1


def test_setup_basic_logging_path_swap_no_duplicate_handlers(tmp_path):
    import logging.handlers

    a = tmp_path / "a"
    b = tmp_path / "b"
    _setup_basic_logging("INFO", log_dir=str(a))
    _setup_basic_logging("INFO", log_dir=str(b))
    root = logging.getLogger()
    # exactly one root rotating handler, pointing at the new location (no dup)
    rot = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    root_stale = [h for h in rot if h.baseFilename.startswith(str(a))]
    assert not root_stale, f"stale handlers: {[h.baseFilename for h in root_stale]}"
    mains = [h for h in rot if h.baseFilename == str(b / "openmux.log")]
    assert len(mains) == 1
    assert (b / "openmux.log").exists()
    # component loggers were repointed too, with exactly one handler each
    comp = logging.getLogger("openmux.client")
    comp_handlers = [
        h
        for h in comp.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler) and h.baseFilename == str(b / "openmux_client.log")
    ]
    stale = [
        h for h in comp.handlers if isinstance(h, logging.handlers.RotatingFileHandler) and h.baseFilename.startswith(str(a))
    ]
    assert len(stale) == 0
    assert len(comp_handlers) == 1


def test_read_logging_block(tmp_path):
    cfg_text = yaml.safe_dump({"logging": {"level": "DEBUG", "log_dir": "x", "file": "y.log", "console": False}})
    cfg_path = tmp_path / "server.yaml"
    cfg_path.write_text(cfg_text)
    block = _read_logging_block(str(cfg_path))
    assert block == {"level": "DEBUG", "log_dir": "x", "file": "y.log", "console": False}
    # Missing file or missing block: non-fatal empty dict
    assert _read_logging_block(str(tmp_path / "nope.yaml")) == {}
    empty_path = tmp_path / "empty.yaml"
    empty_path.write_text("server: {}\n")
    assert _read_logging_block(str(empty_path)) == {}


def test_server_repoints_data_logger_base_dir(tmp_path, monkeypatch):
    """OpenMuxServer points the DataLogger at the env-based log dir."""
    log_dir = tmp_path / "srvlogs"
    monkeypatch.setenv("OPENMUX_LOG_DIR", str(log_dir))
    server_cfg = {
        "server": {"host": "127.0.0.1", "port": 0},
        "logging": {"level": "INFO"},
    }
    server_path = tmp_path / "server.yaml"
    server_path.write_text(yaml.safe_dump(server_cfg, sort_keys=False))
    # ConfigManager requires the authentication sidecar next to the server config
    (tmp_path / "authentication.yaml").write_text(
        yaml.safe_dump({"users": [{"username": "u", "password_hash": "x", "permissions": "admin"}]})
    )

    OpenMuxServer(str(server_path), log_level="INFO")
    assert DataLogger.get().base_dir == str(log_dir)
    assert DataLogger.get()._default_path("p1") == log_dir / "ports" / "p1.log"
    # Main component logs landed in the configured dir
    assert (log_dir / "openmux.log").exists()
    assert (log_dir / "openmux_server.log").exists()


def _write_pid_sock_config(tmp_path, server_cfg):
    """Write a minimal server.yaml + authentication.yaml under tmp_path."""
    cfg_path = tmp_path / "server.yaml"
    cfg = {"server": server_cfg or {}, "logging": {"level": "INFO", "log_dir": str(tmp_path / "logs")}}
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    (tmp_path / "authentication.yaml").write_text(
        yaml.safe_dump({"users": [{"username": "u", "password_hash": "x", "permissions": "admin"}]})
    )
    return cfg_path


def test_resolve_pidfile_precedence(tmp_path, monkeypatch):
    for var in ("OPENMUX_PIDFILE", "OPENMUX_RUN_DIR", "OPENMUX_CTL_SOCK"):
        monkeypatch.delenv(var, raising=False)
    # No config key, no env: dev default
    server = OpenMuxServer(str(_write_pid_sock_config(tmp_path, {})), log_level="INFO")
    assert server._resolve_pidfile() == os.path.join("logs", "openmux.pid")
    # OPENMUX_RUN_DIR places the pidfile in the runtime directory
    monkeypatch.setenv("OPENMUX_RUN_DIR", str(tmp_path / "run"))
    assert server._resolve_pidfile() == str(tmp_path / "run" / "openmux.pid")
    # Deprecated OPENMUX_PIDFILE still wins as an operational override (kept for one release)
    monkeypatch.setenv("OPENMUX_PIDFILE", str(tmp_path / "legacy.pid"))
    assert server._resolve_pidfile() == str(tmp_path / "legacy.pid")
    # The old server.pidfile key was removed: a stale value is stripped at
    # load time and the env/default resolution still wins.
    monkeypatch.delenv("OPENMUX_PIDFILE")
    server2 = OpenMuxServer(str(_write_pid_sock_config(tmp_path, {"pidfile": str(tmp_path / "cfg.pid")})), log_level="INFO")
    assert server2._resolve_pidfile() == str(tmp_path / "run" / "openmux.pid")


def test_resolve_pidfile_deprecation_warnings(tmp_path, monkeypatch, caplog):
    for var in ("OPENMUX_PIDFILE", "OPENMUX_RUN_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENMUX_PIDFILE", str(tmp_path / "legacy.pid"))
    server = OpenMuxServer(str(_write_pid_sock_config(tmp_path, {})), log_level="INFO")
    with caplog.at_level(logging.WARNING, logger="openmux.server"):
        path = server._resolve_pidfile()
    assert path == str(tmp_path / "legacy.pid")
    assert any("OPENMUX_PIDFILE is deprecated" in r.getMessage() for r in caplog.records)
    # Legacy runtime.pidfile fallback still resolves, with its own warning
    monkeypatch.delenv("OPENMUX_PIDFILE", raising=False)
    monkeypatch.delenv("OPENMUX_RUN_DIR", raising=False)
    server2 = OpenMuxServer(str(_write_pid_sock_config(tmp_path, {})), log_level="INFO")
    server2.config_manager.config["runtime"] = {"pidfile": str(tmp_path / "rt.pid")}
    with caplog.at_level(logging.WARNING, logger="openmux.server"):
        assert server2._resolve_pidfile() == str(tmp_path / "rt.pid")
    assert any("deprecated runtime.pidfile" in r.getMessage() for r in caplog.records)


def test_resolve_control_socket_path(tmp_path, monkeypatch):
    for var in ("OPENMUX_CTL_SOCK", "OPENMUX_RUN_DIR", "OPENMUX_PIDFILE"):
        monkeypatch.delenv(var, raising=False)
    server = OpenMuxServer(str(_write_pid_sock_config(tmp_path, {})), log_level="INFO")
    # No config key, no env: dev default (makedirs of logs/ is a no-op in-repo)
    assert server._resolve_control_socket_path() == os.path.join("logs", "openmux.sock")
    # OPENMUX_RUN_DIR
    monkeypatch.setenv("OPENMUX_RUN_DIR", str(tmp_path / "run"))
    assert server._resolve_control_socket_path() == str(tmp_path / "run" / "openmux.sock")
    # OPENMUX_CTL_SOCK overrides everything
    monkeypatch.setenv("OPENMUX_CTL_SOCK", str(tmp_path / "ctl" / "custom.sock"))
    assert server._resolve_control_socket_path() == str(tmp_path / "ctl" / "custom.sock")
    # The old server.control_socket key was removed: a stale value is
    # stripped at load time and the env/default resolution still wins.
    monkeypatch.delenv("OPENMUX_CTL_SOCK", raising=False)
    server2 = OpenMuxServer(
        str(_write_pid_sock_config(tmp_path, {"control_socket": str(tmp_path / "cfg.sock")})), log_level="INFO"
    )
    assert server2._resolve_control_socket_path() == str(tmp_path / "run" / "openmux.sock")
    # Legacy runtime.control_socket fallback still resolves
    monkeypatch.delenv("OPENMUX_RUN_DIR", raising=False)
    server3 = OpenMuxServer(str(_write_pid_sock_config(tmp_path, {})), log_level="INFO")
    server3.config_manager.config["runtime"] = {"control_socket": str(tmp_path / "rt.sock")}
    assert server3._resolve_control_socket_path() == str(tmp_path / "rt.sock")


def test_openmuxctl_resolve_socket_path(tmp_path, monkeypatch):
    from openmux.cli.openmuxctl import resolve_socket_path

    for var in ("OPENMUX_CTL_SOCK", "OPENMUX_RUN_DIR"):
        monkeypatch.delenv(var, raising=False)
    # --socket flag wins
    assert resolve_socket_path(str(tmp_path / "flag.sock")) == str(tmp_path / "flag.sock")
    # No env: dev default
    assert resolve_socket_path(None) == os.path.join("logs", "openmux.sock")
    # OPENMUX_RUN_DIR shared with the server
    monkeypatch.setenv("OPENMUX_RUN_DIR", str(tmp_path / "rundir"))
    assert resolve_socket_path(None) == str(tmp_path / "rundir" / "openmux.sock")
    # OPENMUX_CTL_SOCK beats OPENMUX_RUN_DIR
    monkeypatch.setenv("OPENMUX_CTL_SOCK", str(tmp_path / "ctl.sock"))
    assert resolve_socket_path(None) == str(tmp_path / "ctl.sock")


def test_setup_basic_logging_warns_once_for_uncreatable_dir(tmp_path, caplog, monkeypatch):
    """issue #42: a log dir that cannot be created warns once, not per setup call."""

    from openmux.common import fsutil

    fsutil._warned.clear()
    blocker = tmp_path / "logs"
    blocker.write_text("a regular file, not a directory")
    try:
        _setup_basic_logging("INFO", log_dir=str(tmp_path / "logs"))
        # A reload re-runs setup against the same broken dir: no second warning.
        _setup_basic_logging("INFO", log_dir=str(tmp_path / "logs"))
    finally:
        fsutil._warned.clear()
    warned = [r for r in caplog.records if "cannot create log directory" in r.getMessage().lower()]
    assert len(warned) == 1
    # The single warning carries no traceback
    assert warned[0].exc_info is None


@pytest.mark.asyncio
async def test_initialize_unified_adapters_success(tmp_path, monkeypatch):
    cfg_path = write_temp_config(tmp_path)
    srv = OpenMuxServer(cfg_path, log_level="DEBUG")

    # Prepare fake adapters: one connection endpoint and one port provider
    conn = FakeAdapter("conn1", adapter_type="tcp_server", capabilities={AdapterCapability.ACCEPTS_CONNECTIONS})
    port = FakeAdapter("port1", adapter_type="loopback", capabilities={AdapterCapability.PROVIDES_PORTS})

    class Factory:
        def create_adapters_from_config(self, config):
            return [conn, port]

        registry = SimpleNamespace(get_all_plugins=lambda: [SimpleNamespace(name="loopback", config_section="loopback_ports")])

    srv.unified_adapter_factory = cast(Any, Factory())

    await srv._initialize_unified_adapters()

    assert len(srv.unified_adapters) == 2
    # Ensure adapters were started and wired
    assert conn._started == 1 and port._started == 1
    assert conn._auth is not None and conn._console is not None
    assert getattr(conn, "main_port_manager") is srv.port_manager


@pytest.mark.asyncio
async def test_summary_adapter_count_no_double_start(tmp_path):
    """The startup summary must report without re-calling adapter.start().

    The old _start_all_adapters loop re-iterated every adapter and skipped
    the ones already running; that double pass is gone. The summary counts
    the adapters _initialize_unified_adapters already attempted.
    """
    cfg_path = write_temp_config(tmp_path)
    srv = OpenMuxServer(cfg_path, log_level="INFO")

    a1 = FakeAdapter("a1", capabilities={AdapterCapability.ACCEPTS_CONNECTIONS}, is_running=False)
    a2 = FakeAdapter("a2", capabilities={AdapterCapability.ACCEPTS_CONNECTIONS}, is_running=True)
    a3 = FakeAdapter("a3", capabilities={AdapterCapability.PROVIDES_PORTS}, is_running=True)
    srv.unified_adapters = cast(Any, [a1, a2, a3])
    # Simulate one failed start attempt
    srv._adapter_start_failed.add("a1")

    started = srv._summary_adapter_count()
    # a2, a3 are running; start was not re-attempted for any of them
    assert started == 2
    assert a1._started == 0
    assert a2._started == 0
    assert a3._started == 0


@pytest.mark.asyncio
async def test_summary_adapter_count_all_failed_returns_zero(tmp_path):
    cfg_path = write_temp_config(tmp_path)
    srv = OpenMuxServer(cfg_path, log_level="INFO")

    a1 = FakeAdapter("a1", capabilities={AdapterCapability.PROVIDES_PORTS}, start_ok=False)
    a2 = FakeAdapter("a2", capabilities={AdapterCapability.PROVIDES_PORTS}, start_ok=False)
    srv.unified_adapters = cast(Any, [a1, a2])
    srv._adapter_start_failed.update(["a1", "a2"])

    assert srv._summary_adapter_count() == 0


@pytest.mark.asyncio
async def test_log_server_status_does_not_crash(tmp_path, caplog):
    cfg_path = write_temp_config(tmp_path)
    srv = OpenMuxServer(cfg_path, log_level="INFO")
    srv.unified_adapters = cast(
        Any,
        [
            FakeAdapter("c1", adapter_type="tcp", capabilities={AdapterCapability.ACCEPTS_CONNECTIONS}, is_running=True),
            FakeAdapter("p1", adapter_type="loopback", capabilities={AdapterCapability.PROVIDES_PORTS}, is_running=True),
        ],
    )
    srv._log_server_status()
    # Check some expected markers were logged
    messages = " ".join([r.getMessage() for r in caplog.records])
    assert "OpenMux Server Status" in messages


@pytest.mark.asyncio
async def test_reload_ports_serial_path(tmp_path):
    cfg_path = write_temp_config(tmp_path)
    srv = OpenMuxServer(cfg_path, log_level="INFO")
    serial = FakeSerialAdapter()
    srv.unified_adapters = cast(Any, [serial])

    summary = await srv.reload_ports({"serial_ports": [{"name": "s0", "device": "/dev/ttyS0"}]})
    assert "serial" in summary
    assert serial._last_reconcile is not None


@pytest.mark.asyncio
async def test_run_server_loop_cancel():
    # Directly test the cancellation path of the loop
    cfg_text = {
        "server": {"host": "127.0.0.1", "port": 0},
        "authentication": {"users": []},
    }

    class DummyCM:
        def __init__(self, cfg):
            self.config = cfg

        def load_config(self):
            return self.config

        def get_authentication_config(self):
            return {"users": []}

    srv = OpenMuxServer.__new__(OpenMuxServer)  # bypass __init__
    # Minimal init to satisfy attributes
    srv.logger = cast(Any, SimpleNamespace(info=lambda *a, **k: None))
    srv.config_manager = cast(Any, DummyCM(cfg_text))
    srv.auth_manager = cast(Any, SimpleNamespace(update_config=lambda *a, **k: None))
    srv.port_manager = cast(Any, SimpleNamespace())
    srv.console_manager = cast(Any, SimpleNamespace())
    srv.unified_adapter_factory = cast(Any, SimpleNamespace())
    srv.unified_adapters = []
    srv.is_running = True

    task = asyncio.create_task(srv._run_server_loop())
    await asyncio.sleep(0)  # let it start
    task.cancel()
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_shutdown_stops_unified_adapters(tmp_path):
    cfg_path = write_temp_config(tmp_path)
    srv = OpenMuxServer(cfg_path, log_level="INFO")
    a1 = FakeAdapter("a1", capabilities={AdapterCapability.ACCEPTS_CONNECTIONS}, is_running=True)
    a2 = FakeAdapter("a2", capabilities={AdapterCapability.PROVIDES_PORTS}, is_running=True)
    srv.unified_adapters = cast(Any, [a1, a2])
    srv.is_running = True

    await srv.shutdown()
    assert a1._stopped == 1 and a2._stopped == 1
    assert srv.is_running is False


def test_get_server_status_legacy_path(tmp_path):
    cfg_path = write_temp_config(tmp_path)
    srv = OpenMuxServer(cfg_path, log_level="INFO")
    status = srv.get_server_status()
    assert isinstance(status, dict)
    assert status["summary"]["total_adapters"] == len(srv.adapters)


def test_main_entry_happy_path(monkeypatch, tmp_path):
    # Create minimal config file
    cfg_path = write_temp_config(tmp_path)

    # Patch argv for parser
    monkeypatch.setattr(sys, "argv", ["prog", "-c", cfg_path, "-v"], raising=False)

    # Make server.start return True immediately to avoid infinite loop
    started = {}

    class DummyServer(OpenMuxServer):
        async def start(self):  # type: ignore[override]
            started["ok"] = True
            return True

    # Patch constructor to use DummyServer
    import openmux.server.main as main_mod

    orig_cls = main_mod.OpenMuxServer
    main_mod.OpenMuxServer = DummyServer  # type: ignore[assignment]

    try:
        # Call main(); should not raise SystemExit
        main_mod.main()
        assert started.get("ok") is True
    finally:
        main_mod.OpenMuxServer = orig_cls


# ---------------------------------------------------------------------------
# Coverage-first tests: branches of OpenMuxServer._log_server_status not yet
# exercised above. They pin the web_console (dual-port + single-port)
# endpoint lines, the muxcon listeners loop (enabled + disabled + fallback
# endpoint string), and the generic adapter endpoints-list / endpoint-string
# / N-A fallbacks, so the bracket-C901 refactor is provably
# behavior-preserving.
# ---------------------------------------------------------------------------


def _status_adapter(name: str, adapter_type: str, status_info: Dict[str, Any], is_running: bool = True) -> Any:
    """A minimal adapter with the right type + status_info to hit one log path."""

    class _A(FakeAdapter):
        def __init__(self):
            super().__init__(
                name, adapter_type=adapter_type, capabilities={AdapterCapability.ACCEPTS_CONNECTIONS}, is_running=is_running
            )

        def get_status_info(self):
            return status_info

    return _A()


def _mk_server(tmp_path):
    return OpenMuxServer(write_temp_config(tmp_path), log_level="INFO")


@pytest.mark.asyncio
async def test_log_status_web_console_dual_port(tmp_path, caplog):
    """L1: web_console with tls + a distinct ssl_port logs an http + https line."""
    srv = _mk_server(tmp_path)
    wc = _status_adapter(
        "wc",
        "WebSocket",
        {
            "type": "WebSocket",
            "endpoint": "127.0.0.1:8000",
            "details": {
                "host": "127.0.0.1",
                "port": 8000,
                "tls": True,
                "ssl_port": 8443,
                "http_redirect": True,
            },
        },
    )
    srv.unified_adapters = cast(Any, [wc])
    srv.web_console = wc
    srv._log_server_status()
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "127.0.0.1:8000 (http redirect)" in messages
    assert "127.0.0.1:8443 (https)" in messages


@pytest.mark.asyncio
async def test_log_status_web_console_single_port_http(tmp_path, caplog):
    """L2: web_console without tls logs a single http line."""
    srv = _mk_server(tmp_path)
    wc = _status_adapter(
        "wc",
        "WebSocket",
        {"type": "WebSocket", "endpoint": "127.0.0.1:8080", "details": {"host": "127.0.0.1", "port": 8080, "tls": False}},
    )
    srv.unified_adapters = cast(Any, [wc])
    srv.web_console = wc
    srv._log_server_status()
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "127.0.0.1:8080 (http)" in messages


@pytest.mark.asyncio
async def test_log_status_web_console_single_port_https(tmp_path, caplog):
    """L2b: web_console with tls but no distinct ssl_port logs a single https line."""
    srv = _mk_server(tmp_path)
    wc = _status_adapter(
        "wc",
        "WebSocket",
        {"type": "WebSocket", "endpoint": "127.0.0.1:8443", "details": {"host": "127.0.0.1", "port": 8443, "tls": True}},
    )
    srv.unified_adapters = cast(Any, [wc])
    srv.web_console = wc
    srv._log_server_status()
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "127.0.0.1:8443 (https)" in messages


@pytest.mark.asyncio
async def test_log_status_muxcon_listeners(tmp_path, caplog):
    """L3: muxcon with listeners logs one line per enabled listener."""
    srv = _mk_server(tmp_path)
    mc = _status_adapter(
        "mc",
        "muxcon",
        {
            "type": "muxcon",
            "endpoint": "10.0.0.1:9000",
            "details": {
                "listeners": [
                    {"host": "10.0.0.1", "port": 9000, "use_tls": True, "enabled": True},
                    {"host": "10.0.0.2", "port": 9001, "use_tls": False, "enabled": True},
                    {"host": "10.0.0.3", "port": 9002, "enabled": False},
                ],
            },
        },
    )
    srv.unified_adapters = cast(Any, [mc])
    srv._log_server_status()
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "10.0.0.1:9000 (tls)" in messages
    assert "10.0.0.2:9001 (plain)" in messages
    assert "10.0.0.3:9002 " not in messages  # disabled listener skipped


@pytest.mark.asyncio
async def test_log_status_muxcon_no_listeners_fallback(tmp_path, caplog):
    """L4: muxcon with no structured listeners falls back to the endpoint string."""
    srv = _mk_server(tmp_path)
    mc = _status_adapter(
        "mc",
        "muxcon",
        {"type": "muxcon", "endpoint": "10.0.0.1:9000, 10.0.0.2:9001", "details": {}},
    )
    srv.unified_adapters = cast(Any, [mc])
    srv._log_server_status()
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "10.0.0.1:9000" in messages
    assert "10.0.0.2:9001" in messages


@pytest.mark.asyncio
async def test_log_status_generic_endpoints_list(tmp_path, caplog):
    """L5: a generic adapter with a details.endpoints list logs one line each."""
    srv = _mk_server(tmp_path)
    g = _status_adapter(
        "g",
        "generic",
        {"type": "generic", "endpoint": "N/A", "details": {"endpoints": ["192.168.1.1:2000", "192.168.1.1:2001"]}},
    )
    srv.unified_adapters = cast(Any, [g])
    srv._log_server_status()
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "192.168.1.1:2000" in messages
    assert "192.168.1.1:2001" in messages


@pytest.mark.asyncio
async def test_log_status_generic_endpoint_fallback(tmp_path, caplog):
    """L6: a generic adapter with an endpoint string (no endpoints list) splits and logs each."""
    srv = _mk_server(tmp_path)
    g = _status_adapter(
        "g",
        "generic",
        {"type": "generic", "endpoint": "3.3.3.3:1111, 4.4.4.4:2222", "details": {}},
    )
    srv.unified_adapters = cast(Any, [g])
    srv._log_server_status()
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "3.3.3.3:1111" in messages
    assert "4.4.4.4:2222" in messages


@pytest.mark.asyncio
async def test_log_status_generic_na_fallback(tmp_path, caplog):
    """L7: a generic adapter with no endpoints info at all falls back to 'N/A'."""
    srv = _mk_server(tmp_path)
    g = _status_adapter("g", "generic", {"type": "generic", "endpoint": "N/A", "details": {}})
    srv.unified_adapters = cast(Any, [g])
    srv._log_server_status()
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "N/A" in messages


@pytest.mark.asyncio
async def test_log_status_generic_blank_endpoint_fallback(tmp_path, caplog):
    """L7b: a generic adapter whose status_info has no endpoint key at all logs
    the 'N/A' placeholder in the endpoint column (the endpoints_lines empty
    fallback)."""
    srv = _mk_server(tmp_path)
    g = _status_adapter("g", "generic", {"type": "generic", "details": {}})
    srv.unified_adapters = cast(Any, [g])
    srv._log_server_status()
    # Find the line for our adapter and assert the endpoint column is the
    # 'N/A' placeholder (the f-string right-pads it to 25 chars).
    for r in caplog.records:
        msg = r.getMessage()
        if "(generic" in msg and msg.lstrip().startswith("g "):
            assert "N/A" in msg
            return
    raise AssertionError("no generic adapter log line found")


# --- C901 bracket-26: _setup_basic_logging error/guard path coverage --------


def test_setup_basic_logging_max_log_size_not_int_keeps_prior(tmp_path):
    # max_log_size str that fails int() -> the guarded except keeps the prior
    # _max_bytes (a sentinel here), and setup proceeds with that value
    setattr(_setup_basic_logging, "_max_bytes", 12345)
    _setup_basic_logging("INFO", log_dir=str(tmp_path), max_log_size="not-a-number")
    root = logging.getLogger()
    rot = [h for h in root.handlers if getattr(h, "_openmux_logging", False)]
    assert len(rot) == 1
    assert rot[0].maxBytes == 12345
    assert getattr(_setup_basic_logging, "_max_bytes") == 12345


def test_setup_basic_logging_backup_count_not_int_keeps_prior(tmp_path):
    # log_backup_count str that fails int() -> guarded except keeps the prior
    # _backup_count (a sentinel here), and setup proceeds with that value
    setattr(_setup_basic_logging, "_backup_count", 99)
    _setup_basic_logging("INFO", log_dir=str(tmp_path), log_backup_count="nope")
    root = logging.getLogger()
    rot = [h for h in root.handlers if getattr(h, "_openmux_logging", False) and h.baseFilename.endswith("openmux.log")]
    assert len(rot) == 1
    assert rot[0].backupCount == 99
    assert getattr(_setup_basic_logging, "_backup_count") == 99


def test_setup_basic_logging_console_disabled_drop_stale_console_attr(tmp_path):
    # A path swap while the console is disabled must remove the old console
    # handler and forget the _console_handler attribute
    _setup_basic_logging("INFO", log_dir=str(tmp_path / "c1"))
    old_console = getattr(_setup_basic_logging, "_console_handler")
    assert old_console is not None
    assert old_console in logging.getLogger().handlers
    _setup_basic_logging("INFO", log_dir=str(tmp_path / "c2"), console_enabled=False)
    assert not hasattr(_setup_basic_logging, "_console_handler")
    assert old_console not in logging.getLogger().handlers
    assert getattr(_setup_basic_logging, "_console_enabled") is False


def test_setup_basic_logging_main_file_open_fails_console_only(tmp_path, monkeypatch, caplog):
    # RotatingFileHandler raising OSError on the main log -> console-only
    # continuation, no main file handler, component handlers still attach
    log_dir = tmp_path / "ro"
    from logging.handlers import RotatingFileHandler

    class FailMain(RotatingFileHandler):
        def __init__(self, filename, *args, **kwargs):
            if str(filename) == str(log_dir / "openmux.log"):
                raise OSError("cannot open main log")
            super().__init__(filename, *args, **kwargs)

    monkeypatch.setattr("logging.handlers.RotatingFileHandler", FailMain)
    with caplog.at_level(logging.WARNING):
        _setup_basic_logging("INFO", log_dir=str(log_dir))
    assert not (log_dir / "openmux.log").exists()
    # the `if file_handler is not None:` guard skipped: no tagged handler at all
    root = logging.getLogger()
    assert not [h for h in root.handlers if getattr(h, "_openmux_logging", False)]
    assert any("Could not open log file" in r.getMessage() for r in caplog.records)


def test_setup_basic_logging_component_open_fails_skips_component(tmp_path, monkeypatch, caplog):
    # RotatingFileHandler raising OSError on one component file -> that
    # component is skipped; main file and the other components get handlers
    log_dir = tmp_path / "compfail"
    from logging.handlers import RotatingFileHandler

    class FailClient(RotatingFileHandler):
        def __init__(self, filename, *args, **kwargs):
            if str(filename) == str(log_dir / "openmux_client.log"):
                raise OSError("cannot open component log")
            super().__init__(filename, *args, **kwargs)

    monkeypatch.setattr("logging.handlers.RotatingFileHandler", FailClient)
    with caplog.at_level(logging.WARNING):
        _setup_basic_logging("INFO", log_dir=str(log_dir))
    assert not (log_dir / "openmux_client.log").exists()
    assert (log_dir / "openmux.log").exists()
    assert (log_dir / "openmux_server.log").exists()
    assert not logging.getLogger("openmux.client").handlers
    assert any("skipping component log" in r.getMessage() for r in caplog.records)


def test_setup_basic_logging_component_dir_not_created_skips(tmp_path, monkeypatch):
    # _openmux_mkdir returning False for the component dir -> all components
    # are skipped while the main log (in its own subdir) still gets a handler
    import openmux.server.main as srv_main

    base = tmp_path / "ld"
    base.mkdir()  # base exists for real; only the main subdir is "created"

    def fake_mkdir(path, *args, **kwargs):
        if os.path.basename(str(path)) == "sub":
            os.makedirs(str(path), exist_ok=True)
            return True
        return False

    monkeypatch.setattr(srv_main, "_openmux_mkdir", fake_mkdir)
    _setup_basic_logging("INFO", log_dir=str(base), log_file=str(base / "sub" / "openmux.log"))
    root = logging.getLogger()
    main_h = [h for h in root.handlers if getattr(h, "_openmux_logging", False)]
    assert len(main_h) == 1
    assert (base / "sub" / "openmux.log").exists()
    assert not (base / "openmux_server.log").exists()
    assert not logging.getLogger("openmux.server").handlers


def test_setup_basic_logging_level_update_guarded(tmp_path, monkeypatch):
    # A setLevel failure inside the final "update root and component loggers"
    # block is swallowed by the defensive guard; startup must not crash
    calls = []
    orig = logging.root.setLevel

    def fake_set_level(level):
        calls.append(level)
        if len(calls) > 1:  # the setup's first setLevel succeeds, the update block's fails
            raise RuntimeError("injected during level update")
        orig(level)

    monkeypatch.setattr(logging.root, "setLevel", fake_set_level)
    _setup_basic_logging("INFO", log_dir=str(tmp_path))
    assert len(calls) >= 2
    assert getattr(_setup_basic_logging, "_configured") is True
