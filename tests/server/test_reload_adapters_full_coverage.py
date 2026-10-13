"""Behavior + coverage tests for OpenMuxServer.reload_adapters_full.

`reload_adapters_full` was ~42% line-covered (only the happy/empty-adapter path
from test_reload_signals.test_full_reload_method). These tests pin the outcome
of every control-flow branch: stop-phase (incl. self-WebConsole deferral and
stop timeout/error), config-load fail-fast, adapter-creation fail-fast, the
wiring loop, the start loop (incl. deferred WebConsole start, start timeout,
start failure, endpoint resolution), deferred-restart scheduling, and the
top-level exception fallback. Real server built from an isolated config (same
safety as test_reload_signals); all external side effects (stop/start, config
load, port-manager wiring, task scheduling) are monkeypatched.
"""

import asyncio
import textwrap

import pytest

from openmux.server.main import OpenMuxServer


def _write_isolated_config(tmp_path) -> str:
    """Minimal, adapter-free, self-contained server config (never the real one).

    Mirrors test_reload_signals._write_isolated_config: no adapter sections so a
    reload cannot create/start real adapters or open real connections.
    """
    (tmp_path / "authentication.yaml").write_text(
        textwrap.dedent(
            """
            users:
              - username: admin
                password_hash: 5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8
                permissions: admin
            """
        )
    )
    (tmp_path / "server.yaml").write_text(
        textwrap.dedent(
            """
            server:
              id: test
            logging:
              level: WARNING
            """
        )
    )
    return str(tmp_path / "server.yaml")


class FakeAdapter:
    """Fake unified adapter with controllable stop/start/wiring/status behavior."""

    def __init__(
        self,
        name="adp",
        atype="loopback",
        stop_behavior="ok",
        start_behavior=True,
        status=None,
        wire_error=None,
    ):
        self.name = name
        self._atype = atype
        self.stop_behavior = stop_behavior  # "ok" | "timeout" | "error"
        self.start_behavior = start_behavior  # True | False | "timeout" | "error"
        self._status = status
        self.wire_error = wire_error
        self.wired = {}

    def get_adapter_type(self):
        if self.wire_error is not None:
            # used only by the wiring-log path in some tests; default is fine
            return self._atype
        return self._atype

    async def stop(self):
        if self.stop_behavior == "timeout":
            raise asyncio.TimeoutError()
        if self.stop_behavior == "error":
            raise RuntimeError("stop boom")

    async def start(self):
        if self.start_behavior == "timeout":
            raise asyncio.TimeoutError()
        if self.start_behavior == "error":
            raise RuntimeError("start boom")
        return self.start_behavior

    def get_status_info(self):
        return self._status


def _make_server(tmp_path, monkeypatch):
    cfg = _write_isolated_config(tmp_path)
    server = OpenMuxServer(cfg, log_level="WARNING")
    server.unified_adapters = []
    server.web_console = None

    # Recorder/controllable fakes for the external side effects.
    class PM:
        raises = False
        calls = []

        def set_unified_adapters(self, adapters):
            if self.raises:
                raise RuntimeError("pm boom")
            PM.calls.append(list(adapters))
            # mirror the real PortManager: back-reference each adapter
            for adapter in adapters:
                adapter.main_port_manager = self

    server.port_manager = PM()
    server.config_manager.config = {"server": {"id": "test"}}

    def _noop_cfg():
        return {"server": {"id": "test"}}

    server._reload_config_from_disk = _noop_cfg
    server._apply_logging_from_config = lambda: None
    server._deferred_restart_web_console = _dummy_deferred

    return server


async def _dummy_deferred(*a, **k):
    return None


def _set_created(server, monkeypatch, adapters, raise_exc=None):
    def _create(cfg):
        if raise_exc is not None:
            raise raise_exc
        return list(adapters)

    monkeypatch.setattr(server.unified_adapter_factory, "create_adapters_from_config", _create, raising=True)


@pytest.mark.asyncio
async def test_stop_timeout_records_error(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)
    server.unified_adapters = [FakeAdapter(name="a1", stop_behavior="timeout")]
    _set_created(server, monkeypatch, [])
    res = await server.reload_adapters_full(context={"origin": "test"})
    assert res["stopped"] == 1
    assert any(e.get("stop_timeout") for e in res["errors"])


@pytest.mark.asyncio
async def test_stop_error_records_error(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)
    server.unified_adapters = [FakeAdapter(name="a1", stop_behavior="error")]
    _set_created(server, monkeypatch, [])
    res = await server.reload_adapters_full(context={"origin": "test"})
    assert res["stopped"] == 0
    assert any(e.get("stop_error") for e in res["errors"])


@pytest.mark.asyncio
async def test_stop_success_stopped_adapters(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)
    server.unified_adapters = [FakeAdapter(name="a1", atype="loopback"), FakeAdapter(name="a2", atype="serial")]
    _set_created(server, monkeypatch, [])
    res = await server.reload_adapters_full(context={"origin": "test"})
    assert res["stopped"] == 2
    assert res["stopped_adapters"] == [{"name": "a1", "type": "loopback"}, {"name": "a2", "type": "serial"}]


@pytest.mark.asyncio
async def test_defer_self_webconsole_stop(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)
    wc = FakeAdapter(name="wc1", atype="webconsole")
    other = FakeAdapter(name="lo1", atype="loopback")
    server.unified_adapters = [wc, other]
    server.web_console = wc
    _set_created(server, monkeypatch, [other])
    res = await server.reload_adapters_full(context={"origin": "config-editor", "web_adapter_name": "wc1"})
    # the self web console was deferred (not stopped here); the other stopped
    assert res["stopped"] == 1
    assert [a["name"] for a in res["stopped_adapters"]] == ["lo1"]
    # no NEW webconsole was created, so web_console is None (the old one is
    # still running, tracked only via the deferred task); no restart deferred.
    assert server.web_console is None
    assert res["web_console_restart_deferred"] is False


@pytest.mark.asyncio
async def test_port_manager_set_raises_is_ignored(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)
    server.port_manager.raises = True
    server.unified_adapters = [FakeAdapter(name="lo1")]
    _set_created(server, monkeypatch, [])
    res = await server.reload_adapters_full(context={"origin": "test"})
    assert "error" not in res
    assert res["stopped"] == 1


@pytest.mark.asyncio
async def test_config_load_failure_early_return(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)
    server.unified_adapters = [FakeAdapter(name="lo1")]

    def _boom():
        raise RuntimeError("cfg load boom")

    server._reload_config_from_disk = _boom
    res = await server.reload_adapters_full(context={"origin": "test"})
    assert any(e.get("phase") == "load_config" for e in res["errors"])
    assert res["started"] == 0


@pytest.mark.asyncio
async def test_apply_logging_failure_contines(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)
    server.unified_adapters = []

    def _boom():
        raise RuntimeError("logging boom")

    server._apply_logging_from_config = _boom
    _set_created(server, monkeypatch, [])
    res = await server.reload_adapters_full(context={"origin": "test"})
    # logging failure is non-fatal; reload still completes
    assert "error" not in res
    assert res["stopped"] == 0


@pytest.mark.asyncio
async def test_adapter_creation_failure_early_return(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)
    server.unified_adapters = [FakeAdapter(name="lo1")]
    _set_created(server, monkeypatch, [], raise_exc=RuntimeError("create boom"))
    res = await server.reload_adapters_full(context={"origin": "test"})
    assert any(e.get("phase") == "create_adapters" for e in res["errors"])
    assert server.unified_adapters == []
    assert res["started"] == 0


@pytest.mark.asyncio
async def test_created_adapters_listed(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)
    server.unified_adapters = []
    _set_created(server, monkeypatch, [FakeAdapter(name="n1", atype="serial")])
    res = await server.reload_adapters_full(context={"origin": "test"})
    assert res["created_adapters"] == [{"name": "n1", "type": "serial"}]


@pytest.mark.asyncio
async def test_created_entry_type_raises_is_skipped(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)

    class BoomType(FakeAdapter):
        def get_adapter_type(self):
            raise RuntimeError("type boom")

    _set_created(server, monkeypatch, [BoomType(name="b1")])
    res = await server.reload_adapters_full(context={"origin": "test"})
    # the per-adapter record raises -> skipped (cosmetic), but reload still OK
    assert "error" not in res
    assert res["created_adapters"] == []


@pytest.mark.asyncio
async def test_wiring_sets_attributes_and_calls(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)

    class WireAdapter(FakeAdapter):
        server_config = None  # pre-defined so the wiring hasattr-check sets it

        def set_auth_manager(self, m):
            self.wired["auth"] = m

        def set_console_manager(self, m):
            self.wired["console"] = m

    adp = WireAdapter(name="w1", atype="loopback")
    _set_created(server, monkeypatch, [adp])
    res = await server.reload_adapters_full(context={"origin": "test"})
    assert "error" not in res
    assert adp.wired["auth"] is server.auth_manager
    assert adp.wired["console"] is server.console_manager
    assert adp.main_port_manager is server.port_manager
    assert adp.server_config == {"server": {"id": "test"}}


@pytest.mark.asyncio
async def test_wiring_non_callable_setters_skipped(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)

    class NoSetters(FakeAdapter):
        # no set_auth_manager / set_console_manager -> skipped
        pass

    adp = NoSetters(name="w2", atype="loopback")
    _set_created(server, monkeypatch, [adp])
    res = await server.reload_adapters_full(context={"origin": "test"})
    assert "error" not in res


@pytest.mark.asyncio
async def test_wiring_setter_raises_records_wire_error(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)

    class BoomSet(FakeAdapter):
        server_config = None  # pre-defined so the wiring path reaches setters

        def set_auth_manager(self, m):
            raise RuntimeError("set_auth boom")

    adp = BoomSet(name="w3", atype="loopback")
    _set_created(server, monkeypatch, [adp])
    res = await server.reload_adapters_full(context={"origin": "test"})
    assert any(e.get("wire_error") for e in res["errors"])


@pytest.mark.asyncio
async def test_start_success_started_adapters(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)
    _set_created(server, monkeypatch, [FakeAdapter(name="s1", atype="loopback", status=None)])
    res = await server.reload_adapters_full(context={"origin": "test"})
    assert res["started"] == 1
    assert res["started_adapters"] == [{"name": "s1", "type": "loopback"}]


@pytest.mark.asyncio
async def test_start_endpoint_from_si_endpoint(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)
    _set_created(
        server,
        monkeypatch,
        [FakeAdapter(name="s1", atype="loopback", status={"endpoint": "9.9.9.9:1"})],
    )
    res = await server.reload_adapters_full(context={"origin": "test"})
    assert res["started_adapters"][0].get("endpoint") == "9.9.9.9:1"


@pytest.mark.asyncio
async def test_start_endpoint_from_details_host_port(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)
    _set_created(
        server,
        monkeypatch,
        [FakeAdapter(name="s1", atype="loopback", status={"details": {"host": "h1", "port": 2}})],
    )
    res = await server.reload_adapters_full(context={"origin": "test"})
    assert res["started_adapters"][0].get("endpoint") == "h1:2"


@pytest.mark.asyncio
async def test_start_status_info_raises_is_ignored(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)

    class BoomStatus(FakeAdapter):
        def get_status_info(self):
            raise RuntimeError("status boom")

    _set_created(server, monkeypatch, [BoomStatus(name="s1", atype="loopback")])
    res = await server.reload_adapters_full(context={"origin": "test"})
    assert res["started"] == 1
    assert "endpoint" not in res["started_adapters"][0]


@pytest.mark.asyncio
async def test_start_timeout_records_error(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)
    _set_created(server, monkeypatch, [FakeAdapter(name="s1", atype="loopback", start_behavior="timeout")])
    res = await server.reload_adapters_full(context={"origin": "test"})
    assert res["started"] == 0
    assert any(e.get("start_timeout") for e in res["errors"])


@pytest.mark.asyncio
async def test_start_returns_false_records_error(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)
    _set_created(server, monkeypatch, [FakeAdapter(name="s1", atype="loopback", start_behavior=False)])
    res = await server.reload_adapters_full(context={"origin": "test"})
    assert res["started"] == 0
    assert any(e.get("start_error") == "start returned False" for e in res["errors"])


@pytest.mark.asyncio
async def test_start_error_records_error(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)
    _set_created(server, monkeypatch, [FakeAdapter(name="s1", atype="loopback", start_behavior="error")])
    res = await server.reload_adapters_full(context={"origin": "test"})
    assert res["started"] == 0
    assert any(e.get("start_error") for e in res["errors"])


@pytest.mark.asyncio
async def test_defer_webconsole_restart_scheduled(tmp_path, monkeypatch):
    # origin config-editor, old wc deferred, new wc created and is the deferred
    # one -> web_console_restart_deferred True and a restart task is scheduled.
    server = _make_server(tmp_path, monkeypatch)
    old_wc = FakeAdapter(name="wc0", atype="webconsole")
    server.unified_adapters = [old_wc, FakeAdapter(name="lo1")]
    server.web_console = old_wc

    new_wc = FakeAdapter(name="wc1", atype="webconsole")
    _set_created(server, monkeypatch, [new_wc])

    scheduled = []

    def _fake_create_task(coro):
        scheduled.append(coro)
        coro.close()  # consume the coroutine so it is not left un-awaited
        return "task"

    monkeypatch.setattr("asyncio.create_task", _fake_create_task)

    res = await server.reload_adapters_full(context={"origin": "config-editor", "web_adapter_name": "wc0"})
    assert res["web_console_restart_deferred"] is True
    assert len(scheduled) == 1
    assert server.web_console is new_wc


@pytest.mark.asyncio
async def test_defer_restart_schedule_failure_is_ignored(tmp_path, monkeypatch):
    server = _make_server(tmp_path, monkeypatch)
    old_wc = FakeAdapter(name="wc0", atype="webconsole")
    server.unified_adapters = [old_wc]
    server.web_console = old_wc
    new_wc = FakeAdapter(name="wc1", atype="webconsole")
    _set_created(server, monkeypatch, [new_wc])

    def _boom(coro):
        coro.close()  # avoid a never-awaited coroutine warning
        raise RuntimeError("scheduling boom")

    monkeypatch.setattr("asyncio.create_task", _boom)
    res = await server.reload_adapters_full(context={"origin": "config-editor", "web_adapter_name": "wc0"})
    # scheduling failure is non-fatal
    assert res["web_console_restart_deferred"] is True


@pytest.mark.asyncio
async def test_top_level_exception_returns_error(tmp_path, monkeypatch):
    # A raise OUTSIDE the load/create try-blocks reaches the outermost except.
    # `full_config = self.config_manager.config or {}` is such a read.
    server = _make_server(tmp_path, monkeypatch)
    server.unified_adapters = []

    class _BoomingCfg:
        @property
        def config(self):
            raise RuntimeError("config attr boom")

    server.config_manager = _BoomingCfg()
    res = await server.reload_adapters_full(context={"origin": "test"})
    assert "error" in res
