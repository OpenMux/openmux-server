"""Behavior + coverage tests for the web console POST reload endpoint.

`handle_api_reload` (openmux/server/web_console.py) was 100% uncovered. These
tests drive the real module-level handler with a fake aiohttp request and a
real WebConsoleAdapter wired to fake auth/console/port managers, pinning every
branch: auth (401/403, perms lookup failure), body validation (400s),
per-adapter reconcile (validate, name rules, apply success/failure/
no-reconcile), the post-reconcile client disconnect pass, the in-memory config
snapshot update, and the aggregated response shape.
"""

import json
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from openmux.server.adapters.base_adapter import BaseGenericAdapter
from openmux.server.web_console import WebConsoleAdapter, handle_api_reload
from openmux.server.web_plugins import ADAPTER_APP_KEY

SECTIONS = ("serial_ports", "loopback_ports", "command_ports", "tcp_initiator_ports")


class FakeRequest:
    def __init__(self, adapter, username=None, json_value=None, json_error=False):
        self.app = {ADAPTER_APP_KEY: adapter}
        self._username = username
        self._json_value = json_value
        self._json_error = json_error

    def get(self, key, default=None):
        if key == "username":
            return self._username
        return default

    async def json(self):
        if self._json_error:
            raise ValueError("bad json")
        return self._json_value


class FakeAuth:
    def __init__(self, perms="admin", raise_lookup=False):
        self._perms = perms
        self._raise = raise_lookup

    def get_user_permissions(self, username):
        if self._raise:
            raise RuntimeError("perms boom")
        return self._perms


class FakePM:
    def __init__(self, adapters=None, cm=None, ports=None):
        self.unified_adapters = list(adapters or [])
        self._cm = cm
        self._ports = {name: port for name, port in (ports or [])}

    def get_port(self, name):
        if name not in self._ports:
            raise KeyError(name)
        return self._ports[name]

    @property
    def config_manager(self):
        return self._cm


class FakeConsole:
    def __init__(self, pm, raise_disc=False):
        self.port_manager = pm
        self._raise = raise_disc

    async def disconnect_client_from_port(self, cid, pname):
        if self._raise:
            raise RuntimeError("disconnect boom")


def _adapter(auth=None, console=None):
    a = WebConsoleAdapter("wc", {"web_console": {"enable_ui": False}})
    a.logger = MagicMock()
    if auth is not None:
        a.auth_manager = auth
    if console is not None:
        a.console_manager = console
    return a


def _recon_adapter(kind="loopback", name="r1", validate=True, raise_validate=False, reconcile=None, with_reconcile=True):
    """Unified adapter fake: class method validate_config + instance
    reconcile_ports, both controllable (validate class-level, reconcile
    instance-level, so each test gets independent behavior)."""

    def _validate_config(cfg):
        # Plain function: production calls aobj.__class__.validate_config
        # unbound, so the config dict arrives as the only argument.
        if raise_validate:
            raise ValueError("validate boom")
        return validate

    klass = type(
        "ReconAdapter",
        (BaseGenericAdapter,),
        {
            "get_adapter_type": lambda self: kind,
            "get_capabilities": lambda self: set(),
            "_do_start": _fake_start,
            "stop": _fake_stop,
            "validate_config": _validate_config,
            "create_port": _fake_noop,
            "destroy_port": _fake_noop,
            "get_port_configurations": lambda self: {},
        },
    )
    inst = klass(name, {"name": name})
    inst.reconciled = []
    if with_reconcile:

        async def reconcile_ports(ports):
            inst.reconciled.append(ports)
            return reconcile

        inst.reconcile_ports = reconcile_ports
    return inst


async def _fake_start(self):
    return True


async def _fake_stop(self):
    return None


async def _fake_noop(self, *args):
    return None


def _kind(pm, kind="loopback", **kw):
    a = _recon_adapter(kind=kind, **kw)
    pm.unified_adapters = [a]
    return a


def _boom_type_adapter(name="r1"):
    """Adapter whose get_adapter_type() raises — find_adapter's try/except
    swallows the error and treats it as no-match."""
    klass = type(
        "BoomTypeAdapter",
        (BaseGenericAdapter,),
        {
            "get_adapter_type": lambda self: (_ for _ in ()).throw(RuntimeError("type boom")),
            "get_capabilities": lambda self: set(),
            "_do_start": _fake_start,
            "stop": _fake_stop,
            "validate_config": lambda cfg: True,
            "create_port": _fake_noop,
            "destroy_port": _fake_noop,
            "get_port_configurations": lambda self: {},
        },
    )
    return klass(name, {"name": name})


def _jbody(resp):
    return json.loads(resp.text)


@pytest.mark.asyncio
async def test_unauthorized_without_username():
    a = _adapter(auth=FakeAuth(), console=FakeConsole(FakePM()))
    resp = await handle_api_reload(FakeRequest(a))
    assert resp.status == 401


@pytest.mark.asyncio
async def test_forbidden_permissions():
    for perms in (None, "read-only", "adminx", "operator"):
        a = _adapter(auth=FakeAuth(perms=perms), console=FakeConsole(FakePM()))
        resp = await handle_api_reload(FakeRequest(a, username="u"))
        assert resp.status == 403


@pytest.mark.asyncio
async def test_forbidden_perms_lookup_error():
    a = _adapter(auth=FakeAuth(raise_lookup=True), console=FakeConsole(FakePM()))
    resp = await handle_api_reload(FakeRequest(a, username="u"))
    assert resp.status == 403


@pytest.mark.asyncio
async def test_invalid_json_body():
    a = _adapter(auth=FakeAuth(), console=FakeConsole(FakePM()))
    assert (await handle_api_reload(FakeRequest(a, username="u", json_error=True))).status == 400
    assert (await handle_api_reload(FakeRequest(a, username="u", json_value=[1, 2]))).status == 400
    assert (await handle_api_reload(FakeRequest(a, username="u", json_value="str"))).status == 400
    assert (await handle_api_reload(FakeRequest(a, username="u"))).status == 400  # json() -> None -> not a dict


@pytest.mark.asyncio
async def test_section_not_a_list():
    for key in SECTIONS:
        a = _adapter(auth=FakeAuth(), console=FakeConsole(FakePM()))
        resp = await handle_api_reload(FakeRequest(a, username="u", json_value={key: "nope"}))
        assert resp.status == 400
        assert resp.text == f'{{"error": true, "message": "{key} must be a list"}}'


@pytest.mark.asyncio
async def test_no_supported_sections():
    a = _adapter(auth=FakeAuth(), console=FakeConsole(FakePM()))
    resp = await handle_api_reload(FakeRequest(a, username="u", json_value={"other": [1]}))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_adapter_not_active():
    pm = FakePM()
    a = _adapter(auth=FakeAuth(), console=FakeConsole(pm))
    resp = await handle_api_reload(FakeRequest(a, username="u", json_value={"loopback_ports": []}))
    assert resp.status == 200
    body = _jbody(resp)
    assert body["summary"]["loopback"] == {"error": "loopback adapter not active"}


@pytest.mark.asyncio
async def test_reconcile_success_and_aggregate_shape():
    pm = FakePM()
    result = {"added": ["a1"], "updated": ["u1"], "removed": ["r1"], "unchanged": ["c1"]}
    aobj = _kind(pm, kind="loopback", reconcile=result)
    a = _adapter(auth=FakeAuth(), console=FakeConsole(pm))
    resp = await handle_api_reload(FakeRequest(a, username="u", json_value={"loopback_ports": [{"name": "a1"}]}))
    assert resp.status == 200
    assert aobj.reconciled == [{"loopback_ports": [{"name": "a1"}]}]
    body = _jbody(resp)
    assert body["ok"] is True
    s = body["summary"]
    assert s["added"] == ["a1"] and s["updated"] == ["u1"] and s["removed"] == ["r1"] and s["unchanged"] == ["c1"]
    assert s["loopback"] == result  # per-kind result merged in as well


@pytest.mark.asyncio
async def test_validate_fails_records_error():
    pm = FakePM()
    aobj = _kind(pm, kind="loopback", validate=False, reconcile={})
    a = _adapter(auth=FakeAuth(), console=FakeConsole(pm))
    resp = await handle_api_reload(FakeRequest(a, username="u", json_value={"loopback_ports": [{"name": "n1"}]}))
    assert resp.status == 200
    assert _jbody(resp)["summary"]["loopback"] == {"error": "Invalid loopback_ports configuration"}
    assert aobj.reconciled == []


@pytest.mark.asyncio
async def test_validate_exception_ignored_and_continues():
    pm = FakePM()
    _kind(pm, kind="loopback", raise_validate=True, reconcile={"added": ["x"]})
    a = _adapter(auth=FakeAuth(), console=FakeConsole(pm))
    resp = await handle_api_reload(FakeRequest(a, username="u", json_value={"loopback_ports": [{"name": "n1"}]}))
    assert resp.status == 200
    assert _jbody(resp)["summary"]["loopback"] == {"added": ["x"]}
    a.logger.warning.assert_called()


@pytest.mark.asyncio
async def test_invalid_names_block_apply():
    pm = FakePM()
    aobj = _kind(pm, kind="loopback", reconcile={})
    a = _adapter(auth=FakeAuth(), console=FakeConsole(pm))
    bad_bodies = [
        {"loopback_ports": [{"name": 42}, {"name": "ok"}]},  # non-str name
        {"loopback_ports": [{"name": "  "}]},  # blank
        {"loopback_ports": [{"name": "same"}, {"name": "same"}]},  # duplicate
        {"loopback_ports": [42, {"name": "ok"}]},  # non-dict entries ignored; fine names only
    ]
    for body in bad_bodies[:3]:
        resp = await handle_api_reload(FakeRequest(a, username="u", json_value=body))
        assert resp.status == 200
        assert _jbody(resp)["summary"]["loopback"] == {"error": "Invalid or duplicate names in loopback_ports"}
    # non-dict entries are skipped by the name extraction (only dicts count)
    resp = await handle_api_reload(FakeRequest(a, username="u", json_value=bad_bodies[3]))
    assert _jbody(resp)["summary"]["loopback"] == {}
    assert aobj.reconciled == [{"loopback_ports": [42, {"name": "ok"}]}]


@pytest.mark.asyncio
async def test_adapter_without_reconcile():
    pm = FakePM()
    _kind(pm, kind="loopback", with_reconcile=False)
    a = _adapter(auth=FakeAuth(), console=FakeConsole(pm))
    resp = await handle_api_reload(FakeRequest(a, username="u", json_value={"loopback_ports": [{"name": "n1"}]}))
    assert resp.status == 200
    assert _jbody(resp)["summary"]["loopback"] == {"error": "Adapter does not support live reconcile"}


@pytest.mark.asyncio
async def test_reconcile_exception_records_apply_failed():
    pm = FakePM()

    async def boom(ports):
        raise RuntimeError("reconcile boom")

    aobj = _kind(pm, kind="loopback", reconcile=None)
    aobj.reconcile_ports = boom
    a = _adapter(auth=FakeAuth(), console=FakeConsole(pm))
    resp = await handle_api_reload(FakeRequest(a, username="u", json_value={"loopback_ports": [{"name": "n1"}]}))
    assert resp.status == 200
    assert _jbody(resp)["summary"]["loopback"] == {"error": "Apply failed"}
    a.logger.error.assert_called()


@pytest.mark.asyncio
async def test_disconnect_affected_clients():
    pm = FakePM(ports=[("gone", SimpleNamespace(connected_clients=[{"client_id": "c1"}, {"client_id": "c2"}]))])
    _kind(pm, kind="loopback", reconcile={"removed": ["gone"], "updated": ["gone"]})
    cm = MagicMock()
    cm.port_manager = pm
    cm.disconnect_client_from_port = AsyncMock()
    a = _adapter(auth=FakeAuth(), console=cm)
    resp = await handle_api_reload(FakeRequest(a, username="u", json_value={"loopback_ports": [{"name": "n1"}]}))
    assert resp.status == 200
    # "gone" appears in both removed and updated; disconnect is called once per (cid, port) pair seen
    assert cm.disconnect_client_from_port.await_count == 4
    assert ("c1", "gone") in [c.args for c in cm.disconnect_client_from_port.await_args_list]


@pytest.mark.asyncio
async def test_disconnect_per_port_failure_ignored():
    pm = FakePM(ports=[("gone", SimpleNamespace(connected_clients=[{"client_id": "c1"}]))])
    _kind(pm, kind="loopback", reconcile={"removed": ["gone"]})
    a = _adapter(auth=FakeAuth(), console=FakeConsole(pm, raise_disc=True))
    resp = await handle_api_reload(FakeRequest(a, username="u", json_value={"loopback_ports": [{"name": "n1"}]}))
    assert resp.status == 200


@pytest.mark.asyncio
async def test_clients_without_id_or_non_dict_ignored():
    pm = FakePM(ports=[("gone", SimpleNamespace(connected_clients=[{"no_id": 1}, "notadict", {"client_id": None}]))])
    _kind(pm, kind="loopback", reconcile={"removed": ["gone"]})
    cm = MagicMock()
    cm.port_manager = pm
    cm.disconnect_client_from_port = AsyncMock()
    a = _adapter(auth=FakeAuth(), console=cm)
    resp = await handle_api_reload(FakeRequest(a, username="u", json_value={"loopback_ports": [{"name": "n1"}]}))
    assert resp.status == 200
    assert cm.disconnect_client_from_port.await_count == 0


@pytest.mark.asyncio
async def test_missing_port_in_disconnect_pass():
    pm = FakePM(ports=[])  # get_port raises KeyError -> safe_get_port -> None
    _kind(pm, kind="loopback", reconcile={"removed": ["ghost"]})
    a = _adapter(auth=FakeAuth(), console=FakeConsole(pm))
    resp = await handle_api_reload(FakeRequest(a, username="u", json_value={"loopback_ports": [{"name": "n1"}]}))
    assert resp.status == 200


@pytest.mark.asyncio
async def test_console_manager_without_disconnect_method():
    pm = FakePM(ports=[("gone", SimpleNamespace(connected_clients=[{"client_id": "c1"}]))])
    _kind(pm, kind="loopback", reconcile={"removed": ["gone"]})

    class NoMeth:
        port_manager = pm

    a = _adapter(auth=FakeAuth(), console=NoMeth())
    resp = await handle_api_reload(FakeRequest(a, username="u", json_value={"loopback_ports": [{"name": "n1"}]}))
    assert resp.status == 200


@pytest.mark.asyncio
async def test_config_snapshot_updated():
    cm = MagicMock()
    cm.config = {"loopback_ports": "old"}
    pm = FakePM(cm=cm)
    _kind(pm, kind="loopback", reconcile={})
    a = _adapter(auth=FakeAuth(), console=FakeConsole(pm))
    new_list = [{"name": "n1"}]
    resp = await handle_api_reload(FakeRequest(a, username="u", json_value={"loopback_ports": new_list}))
    assert resp.status == 200
    assert cm.config["loopback_ports"] is new_list


@pytest.mark.asyncio
async def test_config_snapshot_skipped_when_absent():
    pm = FakePM(cm=None)
    _kind(pm, kind="loopback", reconcile={})
    a = _adapter(auth=FakeAuth(), console=FakeConsole(pm))
    resp = await handle_api_reload(FakeRequest(a, username="u", json_value={"loopback_ports": [{"name": "n1"}]}))
    assert resp.status == 200


@pytest.mark.asyncio
async def test_multiple_sections_in_order():
    pm = FakePM()
    s1 = _kind(pm, kind="serial", reconcile={"added": ["s"]})
    l1 = _kind(pm, kind="loopback", reconcile={"removed": ["l"]})
    pm.unified_adapters = [s1, l1]
    a = _adapter(auth=FakeAuth(), console=FakeConsole(pm))
    resp = await handle_api_reload(
        FakeRequest(a, username="u", json_value={"serial_ports": [{"name": "s"}], "loopback_ports": [{"name": "l"}]})
    )
    body = _jbody(resp)
    assert body["summary"]["serial"] == {"added": ["s"]}
    assert body["summary"]["loopback"] == {"removed": ["l"]}
    assert body["summary"]["added"] == ["s"]
    assert body["summary"]["removed"] == ["l"]


@pytest.mark.asyncio
async def test_get_adapter_type_exception_treated_as_no_match():
    # find_adapter swallows a raising get_adapter_type() and keeps searching.
    pm = FakePM(adapters=[_boom_type_adapter()])
    a = _adapter(auth=FakeAuth(), console=FakeConsole(pm))
    resp = await handle_api_reload(FakeRequest(a, username="u", json_value={"loopback_ports": [{"name": "n1"}]}))
    body = _jbody(resp)
    assert resp.status == 200
    assert body["summary"]["loopback"] == {"error": "loopback adapter not active"}


@pytest.mark.asyncio
async def test_disconnect_collection_failure_logs_warning():
    # A non-iterable "removed" value makes affected.extend() raise inside the
    # outer disconnect-pass try -> logger.warning("Post-reconcile ... "). The
    # handler then hits the same bad value in the (unprotected) aggregate
    # builder, which raises TypeError — so the call itself fails, but the
    # disconnect-pass warning is logged first. This pins lines 1093-1094.
    pm = FakePM()
    _kind(pm, kind="loopback", reconcile={"removed": 42})
    a = _adapter(auth=FakeAuth(), console=FakeConsole(pm))
    with pytest.raises(TypeError):
        await handle_api_reload(FakeRequest(a, username="u", json_value={"loopback_ports": [{"name": "n1"}]}))
    assert any("Post-reconcile disconnect failed" in str(c.args[0]) for c in a.logger.warning.call_args_list)


@pytest.mark.asyncio
async def test_config_snapshot_write_error_swallowed():
    # Read-only config mapping: cm.config[k] = lst raises -> except: pass
    # (justification: optional snapshot; the authoritative config is on disk).
    cm = SimpleNamespace(config=MappingProxyType({"loopback_ports": "old"}))
    pm = FakePM(cm=cm)
    _kind(pm, kind="loopback", reconcile={})
    a = _adapter(auth=FakeAuth(), console=FakeConsole(pm))
    resp = await handle_api_reload(FakeRequest(a, username="u", json_value={"loopback_ports": [{"name": "n1"}]}))
    assert resp.status == 200
