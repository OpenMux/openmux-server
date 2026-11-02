"""Listeners the adapter cannot bring up are reported, not reported as applied (issue #107).

Both listener transports share one reconcile module, so every case here runs on Telnet and SSH: the
defect this file guards lived twice because the naming rule lived twice. A refused entry must never
appear under ``added`` or ``updated``, must name its reason in ``failed_detail``, and must end its
gap once the file stops asking for it -- the same contract the port adapters keep (issue #105).
"""

import contextlib
import socket
from typing import Any, Dict, List, Optional

import pytest

from openmux.server.adapters.factory import GenericAdapterFactory
from openmux.server.adapters.ssh_listener import SshListenerAdapter
from openmux.server.adapters.telnet_listener import TelnetListenerAdapter
from openmux.server.main import OpenMuxServer

# Both transports, so a rule fixed on one cannot drift from the other.
_TRANSPORTS = [(TelnetListenerAdapter, "telnet_listener"), (SshListenerAdapter, "ssh_listener")]
IDS = ["telnet", "ssh"]


# Ports named by this module's entries, so no two listeners of any test compete for one.
_allocated_ports: set = set()
# One port per listener name, so an entry built twice is the same entry twice.
_ports_by_name: Dict[str, int] = {}


def free_port() -> int:
    """A port nothing holds, so an entry naming it can bind.

    ``bind_port`` must be 1-65535: the schema says so, and ``build_listener()`` now enforces what
    ``validate_config()`` always checked (issue #107), so a test cannot pass 0 to be handed an
    ephemeral port. Ports handed out here are remembered, because the OS gives the same one back after
    the probe socket closes and two listeners on one port cannot both bind.
    """
    while True:
        with contextlib.closing(socket.socket()) as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        if port not in _allocated_ports:
            _allocated_ports.add(port)
            return port


def port_for(name: str) -> int:
    """The same port for the same listener name, every time.

    A test reloads an unchanged entry by building it again, and two entries differing only in port are
    not the same entry: the second would be reported as an update instead of as ``unchanged``.
    """
    if name not in _ports_by_name:
        _ports_by_name[name] = free_port()
    return _ports_by_name[name]


def entry(name: str, **overrides) -> Dict[str, Any]:
    """One usable listener entry, on a port no other entry of this module was given."""
    value = {"name": name, "bind_host": "127.0.0.1", "bind_port": port_for(name), "target": "loopback1"}
    value.update(overrides)
    return value


# An entry with no `bind_port` cannot be built at all: build_listener does int(None).
NO_PORT = {"name": "bad1", "bind_host": "127.0.0.1", "target": "loopback1"}
# An entry with no name has no listener to name, so its position in the file has to.
NO_NAME = {"bind_host": "127.0.0.1", "target": "loopback1"}


@pytest.fixture(autouse=True)
def _ssh_host_key_in_tmp(tmp_path, monkeypatch):
    """Keep the SSH host key out of the developer's home directory."""
    import openmux.server.adapters.ssh_listener as ssh_module

    key_dir = str(tmp_path / "ssh_listener")
    monkeypatch.setattr(ssh_module, "_HOST_KEY_DIR", key_dir)
    monkeypatch.setattr(ssh_module, "_HOST_KEY_PATH", key_dir + "/ssh_host_key")


def _adapter(adapter_cls, section: str, *entries: Dict[str, Any]):
    adapter = adapter_cls("gap" + section, {section: [dict(e) for e in entries]})
    adapter.main_port_manager = None
    return adapter


def _listening(adapter) -> List[str]:
    return sorted(spec.name for spec in adapter.listeners)


def _serving(adapter) -> List[str]:
    return sorted(adapter.servers)


class OccupiedPort:
    """One port a foreign socket holds, and the way for a test to hand it back."""

    def __init__(self, holder: socket.socket, port: int) -> None:
        self._holder = holder
        self._port = port

    @property
    def port(self) -> int:
        return self._port

    def release(self) -> None:
        """Stop holding the port, standing in for an operator who frees it and reloads."""
        self._holder.close()


@pytest.fixture
def occupied_port():
    """A port this process holds open, so binding a listener to it fails."""
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = int(holder.getsockname()[1])
    # No entry of this module may be handed a port the fixture holds, or a later bind would fail at random.
    _allocated_ports.add(port)
    try:
        yield OccupiedPort(holder, port)
    finally:
        holder.close()


# ---------------------------------------------------------------------------
# Boot: the adapter keeps the entries a refused one does not spoil


@pytest.mark.parametrize("adapter_cls,section", _TRANSPORTS, ids=IDS)
def test_a_refused_listener_entry_names_itself_when_the_adapter_is_built(adapter_cls, section):
    adapter = _adapter(adapter_cls, section, entry("live1"), dict(NO_PORT))

    assert _listening(adapter) == ["live1"]
    assert list(adapter.config_gaps) == ["bad1"]
    assert "bind_port" in adapter.config_gaps["bad1"]
    assert adapter.config_gaps_kept == []


@pytest.mark.parametrize("adapter_cls,section", _TRANSPORTS, ids=IDS)
def test_the_factory_keeps_the_listeners_a_refused_entry_does_not_spoil(adapter_cls, section):
    """One unusable entry used to cost every listener of the section (issue #107)."""
    config = {section: [entry("live1"), dict(NO_PORT)]}

    adapters = GenericAdapterFactory().create_adapters_from_config(config)

    assert len(adapters) == 1
    adapter = adapters[0]
    assert _listening(adapter) == ["live1"]
    assert list(adapter.config_gaps) == ["bad1"]


@pytest.mark.parametrize("adapter_cls,section", _TRANSPORTS, ids=IDS)
@pytest.mark.asyncio
async def test_a_listener_that_cannot_bind_is_reported_and_the_others_stay_up(adapter_cls, section, occupied_port):
    adapter = _adapter(adapter_cls, section, entry("live1"), entry("clash", bind_port=occupied_port.port))
    adapter.console_manager = object()

    started = await adapter._do_start()
    try:
        assert started is False
        assert _serving(adapter) == ["live1"]
        assert list(adapter.config_gaps) == ["clash"]
        assert "bound" in adapter.config_gaps["clash"]
    finally:
        await adapter.stop()


@pytest.mark.parametrize("adapter_cls,section", _TRANSPORTS, ids=IDS)
@pytest.mark.asyncio
async def test_a_listener_that_binds_later_stops_being_reported(adapter_cls, section, occupied_port):
    clash = entry("clash", bind_port=occupied_port.port)
    adapter = _adapter(adapter_cls, section, clash)
    adapter.console_manager = object()
    await adapter._do_start()
    try:
        assert list(adapter.config_gaps) == ["clash"]

        # The operator frees the port and saves the file unchanged: the same entry must be built and bound
        # again, and its gap must go away with it.
        occupied_port.release()
        summary = await adapter.reconcile_ports([dict(clash)])
        assert summary["added"] == ["clash"]
        assert "clash" not in summary.get("failed", [])
        assert adapter.config_gaps == {}
        assert _listening(adapter) == ["clash"]
    finally:
        await adapter.stop()


# ---------------------------------------------------------------------------
# A transport that cannot prepare at all: every listener of that adapter is refused


@pytest.mark.parametrize("adapter_cls,section", _TRANSPORTS, ids=IDS)
@pytest.mark.asyncio
async def test_a_transport_that_cannot_prepare_refuses_every_listener_and_says_so(adapter_cls, section, monkeypatch):
    """One secret that will not load must cost gaps, not an adapter that reports itself running.

    The SSH listener reaches this when it can neither read nor create its host key. The rule is the
    module's, so it is asserted on both transports.
    """
    adapter = _adapter(adapter_cls, section, entry("live1"), entry("second"))
    adapter.console_manager = object()

    async def blocked() -> Optional[str]:
        return "the SSH host key could not be read or created"

    monkeypatch.setattr(adapter, "_prepare_listener_start", blocked)
    started = await adapter._do_start()
    try:
        assert started is False
        assert list(adapter.config_gaps) == ["live1", "second"]
        # The refused specs are dropped, so a reload treats them as adds instead of reading them as
        # unchanged and leaving them unbound for good.
        assert _listening(adapter) == []
        assert _serving(adapter) == []
    finally:
        await adapter.stop()

    monkeypatch.undo()
    summary = await adapter.reconcile_ports([entry("live1"), entry("second")])
    try:
        assert summary["added"] == ["live1", "second"]
        assert "failed" not in summary
        assert adapter.config_gaps == {}
        assert _serving(adapter) == ["live1", "second"]
    finally:
        await adapter.stop()


class _ServerView:
    """The two attributes the server's gap collector reads, so the real collector runs over an adapter.

    ``OpenMuxServer.collect_config_gaps()`` needs a running server for nothing else: it reads the adapters
    and the schema report, and formats. An empty ``config_manager`` gives it no schema violations, so the
    lines below are the adapter's own (issue #105).
    """

    collect_config_gaps = OpenMuxServer.collect_config_gaps
    _adapter_config_gaps = OpenMuxServer._adapter_config_gaps

    def __init__(self, *adapters) -> None:
        self.unified_adapters = list(adapters)
        self.config_manager = None


@pytest.mark.parametrize("adapter_cls,section", _TRANSPORTS, ids=IDS)
def test_the_servers_gap_block_names_a_refused_listener_and_its_reason(adapter_cls, section):
    """What an operator reads at boot: the section, the entry, and why it did not apply.

    Issue #107 printed nothing here, because a listener adapter recorded no gap. The section key matters
    as much as the name: the reload answer uses the same lower-cased adapter type, so the operator can
    match the line to the section of `server.yaml` they have to edit.
    """
    adapter = _adapter(adapter_cls, section, dict(NO_PORT), entry("bad2", acl=["not-an-ip"]))

    lines = [gap.describe() for gap in _ServerView(adapter).collect_config_gaps()]

    assert lines == [
        f"{section}/bad1: 'bind_port' is required and must be a port number between 1 and 65535",
        f"{section}/bad2: 'acl' holds the invalid rule 'not-an-ip'",
    ], lines


# ---------------------------------------------------------------------------
# Reload: a refused entry is never reported as applied


@pytest.mark.parametrize("adapter_cls,section", _TRANSPORTS, ids=IDS)
@pytest.mark.asyncio
async def test_a_refused_new_listener_is_reported_as_failed_and_never_as_added(adapter_cls, section):
    adapter = _adapter(adapter_cls, section, entry("live1"))

    first = await adapter.reconcile_ports([entry("live1"), dict(NO_PORT)])
    second = await adapter.reconcile_ports([entry("live1"), dict(NO_PORT)])
    try:
        assert first["added"] == []
        assert first["failed"] == ["bad1"]
        assert first.get("kept_previous", []) == []
        assert _listening(adapter) == ["live1"]
        assert list(adapter.config_gaps) == ["bad1"]
        # The same bad entry must not be announced as a new listener on the next reload.
        assert second["added"] == []
        assert second["failed"] == ["bad1"]
    finally:
        await adapter.stop()


@pytest.mark.parametrize("adapter_cls,section", _TRANSPORTS, ids=IDS)
@pytest.mark.asyncio
async def test_the_failed_detail_carries_the_reason_the_validator_gave(adapter_cls, section):
    adapter = _adapter(adapter_cls, section, entry("live1"))

    summary = await adapter.reconcile_ports([entry("live1"), dict(NO_PORT)])
    try:
        detail = summary["failed_detail"]["bad1"]
        assert detail["kept_previous"] is False
        assert "bind_port" in detail["error"]
    finally:
        await adapter.stop()


@pytest.mark.parametrize("adapter_cls,section", _TRANSPORTS, ids=IDS)
@pytest.mark.asyncio
async def test_a_refused_change_keeps_the_running_listener_and_says_so(adapter_cls, section):
    adapter = _adapter(adapter_cls, section)
    await adapter.reconcile_ports([entry("live1")])
    running = adapter.listeners[0]
    broken = dict(entry("live1"))
    broken.pop("bind_port")

    summary = await adapter.reconcile_ports([broken])
    try:
        assert adapter.listeners[0] is running
        assert _serving(adapter) == ["live1"]
        assert summary["failed"] == ["live1"]
        assert summary["kept_previous"] == ["live1"]
        assert "live1" not in summary["updated"]
        assert summary["failed_detail"]["live1"]["kept_previous"] is True
        assert adapter.config_gaps_kept == ["live1"]
    finally:
        await adapter.stop()


@pytest.mark.parametrize("adapter_cls,section", _TRANSPORTS, ids=IDS)
@pytest.mark.asyncio
async def test_an_entry_with_no_name_is_reported_by_its_place_in_the_file(adapter_cls, section):
    adapter = _adapter(adapter_cls, section, entry("live1"))

    summary = await adapter.reconcile_ports([entry("live1"), dict(NO_NAME)])
    try:
        assert summary["failed"] == ["<unnamed #2>"]
        assert adapter.config_gaps["<unnamed #2>"]
    finally:
        await adapter.stop()


@pytest.mark.parametrize("adapter_cls,section", _TRANSPORTS, ids=IDS)
@pytest.mark.asyncio
async def test_deleting_the_refused_entry_ends_the_gap(adapter_cls, section):
    adapter = _adapter(adapter_cls, section, entry("live1"))
    await adapter.reconcile_ports([entry("live1"), dict(NO_PORT)])
    assert adapter.config_gaps

    summary = await adapter.reconcile_ports([entry("live1")])
    try:
        assert summary["unchanged"] == ["live1"]
        assert "failed" not in summary
        assert adapter.config_gaps == {}
    finally:
        await adapter.stop()


@pytest.mark.parametrize("adapter_cls,section", _TRANSPORTS, ids=IDS)
@pytest.mark.asyncio
async def test_the_refused_name_is_not_reported_as_removed_on_a_later_reload(adapter_cls, section):
    """A listener that never existed cannot be removed, so the file deleting it changes nothing."""
    adapter = _adapter(adapter_cls, section, entry("live1"))
    await adapter.reconcile_ports([entry("live1"), dict(NO_PORT)])

    summary = await adapter.reconcile_ports([entry("live1")])
    try:
        assert summary["removed"] == []
    finally:
        await adapter.stop()


# ---------------------------------------------------------------------------
# The reconcile behaviour listeners already had must survive the shared module


@pytest.mark.parametrize("adapter_cls,section", _TRANSPORTS, ids=IDS)
@pytest.mark.asyncio
async def test_usable_entries_still_add_update_and_remove(adapter_cls, section):
    adapter = _adapter(adapter_cls, section)
    await adapter.reconcile_ports([entry("keep"), entry("drop"), entry("change")])

    summary = await adapter.reconcile_ports([entry("keep"), entry("change", target="loopback2"), entry("fresh")])
    try:
        assert summary["added"] == ["fresh"]
        assert summary["updated"] == ["change"]
        assert summary["removed"] == ["drop"]
        assert summary["unchanged"] == ["keep"]
        assert "failed" not in summary
        assert _serving(adapter) == ["change", "fresh", "keep"]
    finally:
        await adapter.stop()
