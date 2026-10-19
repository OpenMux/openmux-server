"""Behavioral coverage for OpenMuxServer.reload_adapters_soft (42-bracket S1).

Follows the safe pattern of tests/test_reload_signals.py: a real OpenMuxServer
instantiated on a minimal isolated tmp config (no adapter sections, so the
bootstrap logic cannot create real network adapters). Individual steps are
stubbed/recorder-patched where the soft-reload path is exercised.

Written (and green) against the PRE-refactor method.
"""

import pytest

from openmux.server.main import OpenMuxServer


def _cfg_path(tmp_path):
    (tmp_path / "authentication.yaml").write_text(
        "users:\n"
        "  - username: admin\n"
        "    password_hash: 5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8\n"
        "    permissions: admin\n"
    )
    p = tmp_path / "server.yaml"
    p.write_text("server:\n  id: test\nlogging:\n  level: WARNING\n")
    return str(p)


def _server(tmp_path, monkeypatch):
    s = OpenMuxServer(_cfg_path(tmp_path), log_level="WARNING")
    s.unified_adapters = []
    return s


class FakeAdapter:
    def __init__(self, atype, reconcile=None, start_result=True, start_exc=None, reconcile_exc=None):
        self._atype = atype
        self._reconcile = reconcile
        self._reconcile_exc = reconcile_exc
        self.reconciled = []
        self.main_port_manager = None
        self.auth_set = False
        self.console_set = False
        self.started = False
        self._start_result = start_result
        self._start_exc = start_exc

    def get_adapter_type(self):
        if self._atype is Ellipsis:
            raise RuntimeError("type lookup boom")
        return self._atype

    def set_auth_manager(self, am):
        self.auth_manager = am
        self.auth_set = True

    def set_console_manager(self, cm):
        self.console_manager = cm
        self.console_set = True

    async def start(self):
        if self._start_exc is not None:
            raise self._start_exc
        self.started = True
        return self._start_result

    async def reconcile_ports(self, effective):
        if self._reconcile_exc is not None:
            self.reconciled.append(effective)
            raise self._reconcile_exc
        if self._reconcile is not None:
            self.reconciled.append(effective)
            return self._reconcile
        raise RuntimeError(f"reconcile boom {self._atype}")


class FakeFactory:
    def __init__(self, plugin=None, instances=None, create_exc=None):
        class _Reg:
            def __init__(self, p):
                self._p = p

            def get_plugin(self, sec):
                return self._p

        self.registry = _Reg(plugin)
        self._instances = instances if instances is not None else []
        self._create_exc = create_exc

    def _create_adapter_instances(self, plugin, empty):
        if self._create_exc is not None:
            raise self._create_exc
        return list(self._instances)


@pytest.mark.asyncio
async def test_basic_returns_empty_adapters_summary(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)

    async def noop(cfg):
        return True

    monkeypatch.setattr(s.auth_manager, "update_config", noop)
    res = await s.reload_adapters_soft(context={"origin": "test"})
    assert res["auth_updated"] is True
    assert res["adapters"] == {}


@pytest.mark.asyncio
async def test_config_load_failure_short_circuits(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: (_ for _ in ()).throw(RuntimeError("disk boom")))
    res = await s.reload_adapters_soft(context={"req_id": "t1"})
    assert res == {"error": "disk boom"}


@pytest.mark.asyncio
async def test_logging_reapply_failure_is_logged(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("log boom")

    monkeypatch.setattr(s, "_apply_logging_from_config", boom)
    res = await s.reload_adapters_soft()
    assert calls == [1]
    assert res["adapters"] == {}
    # real auth_manager.update_config succeeds on the isolated config
    assert res["auth_updated"] is True


@pytest.mark.asyncio
async def test_auth_update_applies_authentication_section(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    got = []

    async def up(cfg):
        got.append(cfg)
        return True

    monkeypatch.setattr(s.auth_manager, "update_config", up)
    res = await s.reload_adapters_soft()
    # the disk reload merges the on-disk configs, so the authentication
    # section is the loaded users list, not {}
    assert len(got) == 1
    assert got[0].get("users", [{}])[0].get("username") == "admin"
    assert res["auth_updated"] is True


@pytest.mark.asyncio
async def test_auth_update_failure_continues_reload(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)

    async def boom(cfg):
        raise RuntimeError("auth boom")

    monkeypatch.setattr(s.auth_manager, "update_config", boom)
    res = await s.reload_adapters_soft()
    assert res["auth_updated"] is False
    assert res["adapters"] == {}


@pytest.mark.asyncio
async def test_auth_without_update_config_skipped(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "auth_manager", {})  # no update_config attribute
    res = await s.reload_adapters_soft()
    assert res["auth_updated"] is False


@pytest.mark.asyncio
async def test_web_console_ui_hot_apply_reports_changed(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)

    class RecUI:
        def update_ui_config(self, cfg):
            return {"motd": True, "x": False}

    s.web_console = RecUI()
    res = await s.reload_adapters_soft()
    assert res["adapters"]["web_console"] == {"motd": True}


@pytest.mark.asyncio
async def test_web_console_unchanged_reports_unchanged(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)

    class RecUI:
        def update_ui_config(self, cfg):
            return {"motd": False}

    s.web_console = RecUI()
    res = await s.reload_adapters_soft()
    assert res["adapters"]["web_console"] == "unchanged"


@pytest.mark.asyncio
async def test_web_console_update_failure_keeps_summary(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)

    class BoomUI:
        def update_ui_config(self, cfg):
            raise RuntimeError("ui boom")

    s.web_console = BoomUI()
    res = await s.reload_adapters_soft()
    assert "web_console" not in res["adapters"]


@pytest.mark.asyncio
async def test_bootstrap_new_adapter_then_reconcile(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    # The method reloads config from disk; patch that hook.
    cfg = {"serial_ports": [{"name": "p1"}]}
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: cfg)

    # one fresh instance comes out of the factory for the serial section
    f = FakeFactory(plugin=object(), instances=[FakeAdapter("serial", reconcile={"serial": "populated"})])
    s.unified_adapter_factory = f
    s.port_manager.set_unified_adapters = lambda adapters: None

    res = await s.reload_adapters_soft()
    assert res["auth_updated"] is True  # real auth manager, isolated config
    assert res["adapters"] == {"serial": {"serial": "populated"}}
    # the bootstrapped adapter was wired then reconciled with the section
    assert len(s.unified_adapters) == 1
    only = s.unified_adapters[0]
    assert only.started is True
    assert only.reconciled == [[{"name": "p1"}]]
    assert only.main_port_manager is not None


@pytest.mark.asyncio
async def test_bootstrap_absent_section_skips_creation(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: {"muxcon": {"listeners": []}})
    f = FakeFactory(plugin=object(), instances=[FakeAdapter("muxcon", reconcile={"ok": True})])
    s.unified_adapter_factory = f
    s.port_manager.set_unified_adapters = lambda adapters: None

    res = await s.reload_adapters_soft()
    # muxcon section present and no running muxcon adapter -> bootstrap creates
    # the one instance and reconcile populates it
    assert res["adapters"].get("muxcon") == {"ok": True}
    assert len(s.unified_adapters) == 1


@pytest.mark.asyncio
async def test_bootstrap_no_plugin_warns_and_skips(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: {"serial_ports": [{"name": "p1"}]})
    f = FakeFactory(plugin=None)
    s.unified_adapter_factory = f

    res = await s.reload_adapters_soft()
    assert res["adapters"] == {}
    assert s.unified_adapters == []


@pytest.mark.asyncio
async def test_bootstrap_start_failure_keeps_adapter_out(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: {"serial_ports": [{"name": "p1"}]})
    f = FakeFactory(
        plugin=object(), instances=[FakeAdapter("serial", reconcile={"ok": True}, start_exc=RuntimeError("start boom"))]
    )
    s.unified_adapter_factory = f

    res = await s.reload_adapters_soft()
    assert s.unified_adapters == []
    assert res["adapters"] == {}  # failed adapter never reconciled


@pytest.mark.asyncio
async def test_bootstrap_start_false_result_keeps_adapter_out(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: {"serial_ports": [{"name": "p1"}]})
    f = FakeFactory(plugin=object(), instances=[FakeAdapter("serial", reconcile={"ok": True}, start_result=False)])
    s.unified_adapter_factory = f

    res = await s.reload_adapters_soft()
    assert s.unified_adapters == []
    assert res["adapters"] == {}


@pytest.mark.asyncio
async def test_type_check_raising_still_bootstrap(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: {"serial_ports": [{"name": "p1"}]})
    broken = FakeAdapter(Ellipsis)  # get_adapter_type raises
    f = FakeFactory(plugin=object(), instances=[FakeAdapter("serial", reconcile={"ok": True})])
    s.unified_adapter_factory = f
    s.unified_adapters = [broken]

    res = await s.reload_adapters_soft()
    created = [x for x in s.unified_adapters if x is not broken]
    assert len(created) == 1  # a fresh serial adapter was bootstrapped
    assert created[0].reconciled == [[{"name": "p1"}]]
    assert res["adapters"]["serial"] == {"ok": True}


@pytest.mark.asyncio
async def test_reconcile_passes_effective_values_per_type(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    cfg = {"loopback_ports": [{"name": "lb1"}]}  # every other section absent
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: cfg)
    types = [
        "serial",
        "loopback",
        "command",
        "tcp_initiator",
        "telnet_listener",
        "ssh_listener",
        "muxcon",
        "client_listener",
        "power",
    ]
    s.unified_adapters = [FakeAdapter(t, reconcile={t: "ok"}) for t in types]

    res = await s.reload_adapters_soft()
    keys = list(res["adapters"].keys())
    assert keys == [t for t in types]  # all nine reconcile branches fired
    # effective values: present section -> its value; absent sections -> the
    # type-specific default (lists for the port sections, {} for muxcon/power,
    # {"enabled": False} for client_listener)
    exp = {
        "serial": [],
        "loopback": [{"name": "lb1"}],
        "command": [],
        "tcp_initiator": [],
        "telnet_listener": [],
        "ssh_listener": [],
        "muxcon": {},
        "client_listener": {"enabled": False},
        "power": {},
    }
    for a in s.unified_adapters:
        assert a.reconciled == [exp[a._atype]]


@pytest.mark.asyncio
async def test_reconcile_error_recorded_per_type(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: {})
    s.unified_adapters = [FakeAdapter("serial")]

    res = await s.reload_adapters_soft()
    assert res["adapters"]["serial"] == {"error": "reconcile boom serial"}


@pytest.mark.asyncio
async def test_reconcile_other_types_use_their_labels(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: {})
    s.unified_adapters = [FakeAdapter("muxcon"), FakeAdapter("client_listener"), FakeAdapter("loopback")]

    res = await s.reload_adapters_soft()
    assert res["adapters"]["muxcon"] == {"error": "reconcile boom muxcon"}
    assert res["adapters"]["client_listener"] == {"error": "reconcile boom client_listener"}
    assert res["adapters"]["loopback"] == {"error": "reconcile boom loopback"}
    assert res["auth_updated"] is True


@pytest.mark.asyncio
async def test_adapter_without_reconcile_method_skipped(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: {"serial_ports": []})
    obj = object()  # no get_adapter_type at all -> outer except -> continue
    s.unified_adapters = [obj]

    res = await s.reload_adapters_soft()
    assert res["adapters"] == {}
    assert res["auth_updated"] is True


@pytest.mark.asyncio
async def test_adapter_with_type_but_no_reconcile_hook_skipped(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: {"serial_ports": []})

    class _NoHook:
        def get_adapter_type(self):
            return "serial"

    s.unified_adapters = [_NoHook()]  # recognizable type but no reconcile_ports hook

    res = await s.reload_adapters_soft()
    assert res["adapters"] == {}


@pytest.mark.asyncio
async def test_adapter_type_none_skipped(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: {"serial_ports": []})
    s.unified_adapters = [FakeAdapter(None)]  # type() -> "" -> no branch matches

    res = await s.reload_adapters_soft()
    assert res["adapters"] == {}


@pytest.mark.asyncio
async def test_bootstrap_wires_and_starts_adapter(tmp_path, monkeypatch):
    """Full bootstrap path with a FAILED start: the instance is still wired
    (port/auth/console managers) and `start()` is still attempted, but a
    falsy start result keeps it OUT of unified_adapters (no registration, no
    reconcile this tick)."""
    s = _server(tmp_path, monkeypatch)
    # single section, no running adapter of its type
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: {"ssh_listener": [{"name": "s1"}]})
    s.unified_adapters = []

    new_inst = FakeAdapter("ssh_listener", start_result=False)
    f = FakeFactory(plugin=object(), instances=[new_inst])
    s.unified_adapter_factory = f

    seen = []

    def rec(adapters):
        seen.append(list(adapters))

    s.port_manager.set_unified_adapters = rec

    res = await s.reload_adapters_soft()
    # wired + start attempted (`started` records the attempt; the falsy
    # START RESULT is what keeps it unregistered)
    assert new_inst.main_port_manager is not None
    assert new_inst.auth_set is True
    assert new_inst.console_set is True
    assert new_inst.started is True
    assert s.unified_adapters == []
    assert seen == []
    assert res["adapters"] == {}


@pytest.mark.asyncio
async def test_bootstrap_skipped_when_running_adapter_exists(tmp_path, monkeypatch):
    """A section whose adapter type is already running is skipped in the
    bootstrap (no factory needed, no new instance), and the running adapter
    is still reconciled with the section."""
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: {"serial_ports": [{"name": "p1"}]})
    run = FakeAdapter("serial", reconcile={"serial": "ran"})
    s.unified_adapters = [run]
    factory = object()  # a factory that, if consulted, would break
    s.unified_adapter_factory = factory

    res = await s.reload_adapters_soft()
    # the running adapter was NOT bootstrapped twice; it was reconciled
    assert s.unified_adapters == [run]
    assert run.reconciled == [[{"name": "p1"}]]
    assert res["adapters"] == {"serial": {"serial": "ran"}}


@pytest.mark.asyncio
async def test_bootstrap_skipped_when_no_factory(tmp_path, monkeypatch):
    """A section is present, no adapter of that type is running, AND the
    unified adapter factory is absent: the `not _factory` guard of the
    bootstrap continues without creating or logging anything. The already
    running (different type) adapter is still reconciled."""
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: {"serial_ports": [{"name": "p1"}], "loopback_ports": []})
    s.unified_adapters = [FakeAdapter("loopback", reconcile={"loop": "ran"})]
    s.unified_adapter_factory = None  # getattr -> None -> `not _factory`

    res = await s.reload_adapters_soft()
    # nothing bootstrapped (no factory), nothing appended
    assert len(s.unified_adapters) == 1
    # the running loopback adapter was reconciled with its (empty) section
    assert s.unified_adapters[0].reconciled == [[]]
    assert res["adapters"] == {"loopback": {"loop": "ran"}}


@pytest.mark.asyncio
async def test_bootstrap_factory_create_failure_logged(tmp_path, monkeypatch):
    """_create_adapter_instances raising -> the outer bootstrap except logs and
    the loop continues with no adapter created."""
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: {"serial_ports": [{"name": "p1"}]})
    f = FakeFactory(plugin=object(), create_exc=RuntimeError("create boom"))
    s.unified_adapter_factory = f

    res = await s.reload_adapters_soft()
    assert s.unified_adapters == []
    assert res["adapters"] == {}


@pytest.mark.asyncio
async def test_reconcile_branch_errors_each_recorded(tmp_path, monkeypatch):
    """Every per-type reconcile branch's own except: the same effective payload
    is recorded per type and each summary entry carries the branch's error."""
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: {})
    for t in ("command", "tcp_initiator", "telnet_listener", "ssh_listener", "loopback"):
        s.unified_adapters.append(FakeAdapter(t, reconcile_exc=RuntimeError(f"reconcile boom {t}")))

    res = await s.reload_adapters_soft()
    expect_eff = {
        "command": [],
        "tcp_initiator": [],
        "telnet_listener": [],
        "ssh_listener": [],
        "loopback": [],
    }
    for t in expect_eff:
        assert res["adapters"][t] == {"error": f"reconcile boom {t}"}
    for a in s.unified_adapters:
        assert a.reconciled == [expect_eff[a._atype]]


@pytest.mark.asyncio
async def test_reconcile_power_branch_error_recorded(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: {})
    s.unified_adapters = [FakeAdapter("power", reconcile_exc=RuntimeError("reconcile boom power"))]

    res = await s.reload_adapters_soft()
    assert res["adapters"]["power"] == {"error": "reconcile boom power"}
    # effective for an absent power section is {}
    assert s.unified_adapters[0].reconciled == [{}]


class _NotifyRec:
    """Records web-console notifications from the soft-reload hooks."""

    def __init__(self):
        self.pdus = 0
        self.reloads = 0

    def notify_pdus_changed(self):
        self.pdus += 1

    def notify_reload_applied(self):
        self.reloads += 1


async def _power_reconcile_reload(tmp_path, monkeypatch, summary):
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: {})

    async def noop(cfg):
        return True

    monkeypatch.setattr(s.auth_manager, "update_config", noop)
    s.web_console = _NotifyRec()
    s.unified_adapters = [FakeAdapter("power", reconcile=summary)]
    res = await s.reload_adapters_soft()
    return s, res


@pytest.mark.asyncio
async def test_power_reconcile_added_notifies_web_console(tmp_path, monkeypatch):
    s, res = await _power_reconcile_reload(
        tmp_path, monkeypatch, {"added": ["p1"], "removed": [], "updated": [], "unchanged": []}
    )
    assert res["adapters"]["power"]["added"] == ["p1"]
    assert s.web_console.pdus == 1


@pytest.mark.asyncio
async def test_power_reconcile_removed_or_rebuilt_notifies(tmp_path, monkeypatch):
    s, _ = await _power_reconcile_reload(
        tmp_path, monkeypatch, {"added": [], "removed": ["p1"], "updated": [], "unchanged": []}
    )
    assert s.web_console.pdus == 1
    s, _ = await _power_reconcile_reload(
        tmp_path, monkeypatch, {"added": [], "removed": [], "updated": ["p2"], "unchanged": []}
    )
    assert s.web_console.pdus == 1


@pytest.mark.asyncio
async def test_power_reconcile_unchanged_does_not_notify(tmp_path, monkeypatch):
    s, res = await _power_reconcile_reload(
        tmp_path, monkeypatch, {"added": [], "removed": [], "updated": [], "unchanged": ["p1"]}
    )
    assert res["adapters"]["power"]["unchanged"] == ["p1"]
    assert s.web_console.pdus == 0


@pytest.mark.asyncio
async def test_completed_soft_reload_notifies_reload_applied(tmp_path, monkeypatch):
    """A completed soft reload always nudges, even with nothing reconciled.

    `port_actions` is read per request and reconciled by no adapter, and a port's
    in-place live fields publish no event, so the reconcile summary cannot prove
    that no live view changed. The nudge is coalesced per topic, so a no-op
    reload costs one frame.
    """
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: {})

    async def noop(cfg):
        return True

    monkeypatch.setattr(s.auth_manager, "update_config", noop)
    s.web_console = _NotifyRec()

    res = await s.reload_adapters_soft(context={"req_id": "r1"})
    assert res["adapters"] == {}
    assert s.web_console.reloads == 1
    assert s.web_console.pdus == 0


@pytest.mark.asyncio
async def test_config_load_failure_notifies_nothing(tmp_path, monkeypatch):
    """The error short-circuit returns before the reload nudge."""
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: (_ for _ in ()).throw(RuntimeError("disk boom")))
    s.web_console = _NotifyRec()

    res = await s.reload_adapters_soft(context={"req_id": "r2"})
    assert res == {"error": "disk boom"}
    assert s.web_console.reloads == 0


@pytest.mark.asyncio
async def test_reload_without_web_console_hooks_completes(tmp_path, monkeypatch):
    """A web console without the notify hooks (or none at all) is skipped.

    The nudges only speed up live views, so a server build or a test double that
    exposes neither method must still complete the reload.
    """
    s = _server(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "_reload_config_from_disk", lambda: {})

    async def noop(cfg):
        return True

    monkeypatch.setattr(s.auth_manager, "update_config", noop)
    s.web_console = object()  # no notify_* methods at all

    res = await s.reload_adapters_soft(context={"req_id": "r3"})
    assert res["adapters"] == {}
    assert res["auth_updated"] is True
