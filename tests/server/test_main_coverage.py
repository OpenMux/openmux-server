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

    async def start(self) -> bool:
        self._started += 1
        self.is_running = self._start_ok
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


def test_find_config_file_fallback_uses_repo_config():
    # Provide a definitely missing path
    choose = _find_config_file("/no/such/config/file.yaml")
    assert os.path.exists(choose)
    # Should point to repository config/server.yaml
    assert choose.endswith(os.path.join("config", "server.yaml"))


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


def test_server_repoints_data_logger_base_dir(tmp_path):
    """OpenMuxServer applies logging.log_dir to the DataLogger default path."""
    log_dir = tmp_path / "srvlogs"
    server_cfg = {
        "server": {"host": "127.0.0.1", "port": 0},
        "logging": {"level": "INFO", "log_dir": str(log_dir)},
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


def test_setup_basic_logging_warns_once_for_uncreatable_dir(tmp_path, caplog, monkeypatch):
    """issue #42: a log dir that cannot be created warns once, not per setup call."""
    import logging

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
async def test_start_all_adapters_with_mixed_states(tmp_path):
    cfg_path = write_temp_config(tmp_path)
    srv = OpenMuxServer(cfg_path, log_level="INFO")

    a1 = FakeAdapter("a1", capabilities={AdapterCapability.ACCEPTS_CONNECTIONS}, is_running=False)
    a2 = FakeAdapter("a2", capabilities={AdapterCapability.ACCEPTS_CONNECTIONS}, is_running=True)
    a3 = FakeAdapter("a3", capabilities={AdapterCapability.PROVIDES_PORTS}, is_running=True)
    srv.unified_adapters = cast(Any, [a1, a2, a3])

    started = await srv._start_all_adapters()
    # Only connection endpoints count; a1 started, a2 already running
    assert started == 2
    assert a1._started == 1


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
