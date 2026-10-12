"""28-bracket S1 coverage: CommandAdapter.reconcile_ports error/edge branches.

The main command suite exercises the happy add/remove/update/unchanged paths.
These pin the remaining branches of reconcile_ports:

- 1641  dict input with a ``command_ports`` list (first isinstance arm)
- 1645  unknown input shape -> items=[] (existing port gets removed)
- 1690/1691  old_cfg attr read raising -> old_cfg={}
- 1695  BUG log: _material_cfg has keys not tracked in old_cfg
- 1705/1707  description in-place setattr raising
- 1723/1725  groups/power in-place setattr raising
- 1746/1748  lifecycle-flag in-place setattr raising
- 1757/1758  destroy_port raising on a removed/updated port
- 1767/1768  create_port raising on an added/updated port
- 1773/1775  config["command_ports"] snapshot write raising

Line 1764 (``if not cfg: continue``) is STRUCTURALLY DEAD: added/updated are
derived from new_by_name's keys, so the lookup always yields a config; it is
pinned dead, not covered (same treatment as the other dead guards).

Every locked-port class matches the material cfg for {"command":"echo hi"} so
reconcile lands in the "unchanged" branch, then the target in-place update
hits a raising setter.
"""

import pytest

from openmux.server.adapters.command import CommandAdapter

_NEW = {"name": "a", "command": "echo hi"}


class _BaseMatch:
    """Material-cfg-matching port for {"command":"echo hi"} (see test names)."""

    command = "echo hi"
    shell = False
    cwd = None
    env = None
    max_read_write_users = "one"
    interactive = False
    scrollback_size = 0
    description = ""
    read_write_groups = []
    read_only_groups = []
    power = []
    spawn_on_demand = False
    idle_timeout_sec = 0


class RaiseReadPort(_BaseMatch):
    @property
    def command(self):
        raise RuntimeError("attr read blocked for test")


class DescLockPort(_BaseMatch):
    @property
    def description(self):
        return ""

    @description.setter
    def description(self, v):
        raise AttributeError("description read-only for test")


class GroupsLockPort(_BaseMatch):
    @property
    def read_write_groups(self):
        return []

    @read_write_groups.setter
    def read_write_groups(self, v):
        raise AttributeError("rw groups read-only for test")


class PowerLockPort(_BaseMatch):
    @property
    def power(self):
        return []

    @power.setter
    def power(self, v):
        raise AttributeError("power read-only for test")


class LifecycleLockPort(_BaseMatch):
    @property
    def spawn_on_demand(self):
        return False

    @spawn_on_demand.setter
    def spawn_on_demand(self, v):
        raise AttributeError("spawn_on_demand read-only for test")


class _ReadOnlyConfig(dict):
    def __setitem__(self, key, value):
        raise TypeError("config snapshot read-only for test")


def _adapter(port_obj):
    adapter = CommandAdapter("cmd", {"command_ports": []})
    adapter.ports = {"a": port_obj} if port_obj is not None else {}
    return adapter


def _fakes(cap):
    async def destroy(name, **_):
        cap["destroyed"].append(name)

    async def create(name, cfg, **_):
        cap["created"].append((name, cfg))

    return destroy, create


@pytest.mark.asyncio
async def test_reconcile_accepts_dict_with_list():
    adapter = _adapter(_BaseMatch())
    cap = {"destroyed": [], "created": []}
    adapter.destroy_port, adapter.create_port = _fakes(cap)
    summary = await adapter.reconcile_ports({"command_ports": [_NEW]})
    assert summary["unchanged"] == ["a"]
    assert cap == {"destroyed": [], "created": []}


@pytest.mark.asyncio
async def test_reconcile_bad_input_shape_removes_all():
    adapter = _adapter(_BaseMatch())
    cap = {"destroyed": [], "existing": []}
    adapter.destroy_port, _ = _fakes(cap)

    async def create(name, cfg, **_):
        cap["existing"].append(name)

    adapter.create_port = create
    # command_ports present but not a list -> items=[] -> existing 'a' removed.
    summary = await adapter.reconcile_ports({"command_ports": "not-a-list"})
    assert summary["removed"] == ["a"]
    assert cap["destroyed"] == ["a"]
    assert cap["existing"] == []


@pytest.mark.asyncio
async def test_reconcile_old_cfg_read_raises_marks_updated_with_bug_log():
    # RaiseReadPort.command raises -> old_cfg={} -> BUG log -> mismatch -> updated.
    adapter = _adapter(RaiseReadPort())
    cap = {"destroyed": [], "created": []}
    adapter.destroy_port, adapter.create_port = _fakes(cap)
    summary = await adapter.reconcile_ports([_NEW])
    assert summary["updated"] == ["a"]
    assert summary["unchanged"] == []
    assert cap["destroyed"] == ["a"]
    assert cap["created"] == [("a", _NEW)]


@pytest.mark.asyncio
async def test_reconcile_description_inplace_setattr_raises():
    adapter = _adapter(DescLockPort())
    cap = {"destroyed": [], "created": []}
    adapter.destroy_port, adapter.create_port = _fakes(cap)
    summary = await adapter.reconcile_ports([dict(_NEW, description="new")])
    assert summary["unchanged"] == ["a"]
    assert cap == {"destroyed": [], "created": []}


@pytest.mark.asyncio
async def test_reconcile_groups_inplace_setattr_raises():
    adapter = _adapter(GroupsLockPort())
    cap = {"destroyed": [], "created": []}
    adapter.destroy_port, adapter.create_port = _fakes(cap)
    summary = await adapter.reconcile_ports([dict(_NEW, read_write_groups=["ops"])])
    assert summary["unchanged"] == ["a"]
    assert cap == {"destroyed": [], "created": []}


@pytest.mark.asyncio
async def test_reconcile_power_inplace_setattr_raises():
    adapter = _adapter(PowerLockPort())
    cap = {"destroyed": [], "created": []}
    adapter.destroy_port, adapter.create_port = _fakes(cap)
    summary = await adapter.reconcile_ports([dict(_NEW, power=["pdu.1"])])
    assert summary["unchanged"] == ["a"]
    assert cap == {"destroyed": [], "created": []}


@pytest.mark.asyncio
async def test_reconcile_lifecycle_inplace_setattr_raises():
    adapter = _adapter(LifecycleLockPort())
    cap = {"destroyed": [], "created": []}
    adapter.destroy_port, adapter.create_port = _fakes(cap)
    # spawn_on_demand changes False->True; the raising setter triggers the
    # lifecycle except before idle_timeout_sec is evaluated.
    summary = await adapter.reconcile_ports([dict(_NEW, spawn_on_demand=True, idle_timeout_sec=5)])
    assert summary["unchanged"] == ["a"]
    assert cap == {"destroyed": [], "created": []}


@pytest.mark.asyncio
async def test_reconcile_destroy_raises_on_removed():
    adapter = _adapter(_BaseMatch())

    async def bad_destroy(name, **_):
        raise RuntimeError("destroy blocked for test")

    created: list = []

    async def create(name, cfg, **_):
        created.append((name, cfg))

    adapter.destroy_port = bad_destroy
    adapter.create_port = create
    summary = await adapter.reconcile_ports([])
    assert summary["removed"] == ["a"]
    assert created == []


@pytest.mark.asyncio
async def test_reconcile_create_raises_on_added():
    adapter = _adapter(None)
    destroyed: list = []

    async def destroy(name, **_):
        destroyed.append(name)

    async def bad_create(name, cfg, **_):
        raise RuntimeError("create blocked for test")

    adapter.destroy_port = destroy
    adapter.create_port = bad_create
    summary = await adapter.reconcile_ports([_NEW])
    assert summary["added"] == ["a"]
    assert destroyed == []


@pytest.mark.asyncio
async def test_reconcile_config_snapshot_write_raises():
    adapter = _adapter(_BaseMatch())
    adapter.config = _ReadOnlyConfig({"command_ports": []})
    cap = {"destroyed": [], "created": []}
    adapter.destroy_port, adapter.create_port = _fakes(cap)
    summary = await adapter.reconcile_ports([_NEW])
    assert summary["unchanged"] == ["a"]
    assert cap == {"destroyed": [], "created": []}
