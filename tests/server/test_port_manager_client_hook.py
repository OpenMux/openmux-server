"""Tests that the canonical PortManager client paths fire the adapter hook (issue #63).

Before this, only the rarely-used UnifiedPortWrapper.add_client / remove_client
fired ``on_client_count_changed``; the production add_client_to_port /
remove_client_from_port paths mutated connected_clients directly, so the serial
signal-line behavior (and command idle_timeout_sec / tcp disconnect_when_idle)
never fired.
"""

import logging
from types import SimpleNamespace
from typing import List

import pytest

from openmux.server.data_logger import DataLogger
from openmux.server.port_manager import PortManager


class _DummyDataLogger:
    @staticmethod
    def record_meta(**_kw):
        return None


class RecordingPort:
    """A unified port recording every on_client_count_changed call."""

    def __init__(self, name: str = "rec1"):
        self.name = name
        self.calls: List[int] = []
        self.connected_clients: List[dict] = []

    def on_client_count_changed(self, count: int) -> None:
        self.calls.append(count)


class NoHookPort:
    """A unified port without the hook (e.g. loopback) - must be a no-op."""

    def __init__(self, name: str = "plain"):
        self.name = name
        self.connected_clients: List[dict] = []


class RecordingAdapter:
    """Minimal unified adapter; only get_adapter_type is consulted."""

    name = "serial_test"

    def get_adapter_type(self) -> str:
        return "serial"


@pytest.fixture
def pm(monkeypatch) -> PortManager:
    monkeypatch.setattr(DataLogger, "get", classmethod(lambda cls: _DummyDataLogger()), raising=False)
    return PortManager([])


class TestFireClientCountHook:
    @pytest.mark.asyncio
    async def test_hook_fires_on_add_and_remove(self, pm: PortManager):
        """Counts 1/2/1/0 are delivered as clients attach and detach."""
        adapter = RecordingAdapter()
        port = RecordingPort()
        assert await pm.register_unified_port("rec1", port, adapter) is True

        assert await pm.add_client_to_port("rec1", "c1", "alice") is True
        assert await pm.add_client_to_port("rec1", "c2", "bob") is True
        assert await pm.remove_client_from_port("rec1", "c1") is True
        assert await pm.remove_client_from_port("rec1", "c2") is True

        assert port.calls == [1, 2, 1, 0], f"hook saw: {port.calls}"

    @pytest.mark.asyncio
    async def test_hook_fires_on_inplace_readd_with_no_count_change(self, pm: PortManager):
        """Re-adding the same client_id is a count-neutral in-place update; the hook
        still fires with the unchanged count (adapters are idempotent on equals)."""
        adapter = RecordingAdapter()
        port = RecordingPort()
        await pm.register_unified_port("rec1", port, adapter)

        assert await pm.add_client_to_port("rec1", "c1", "alice") is True
        assert await pm.add_client_to_port("rec1", "c1", "alice", "read-write") is True
        assert await pm.remove_client_from_port("rec1", "c1") is True

        assert port.calls == [1, 1, 0], f"hook saw: {port.calls}"

    @pytest.mark.asyncio
    async def test_hook_noop_for_ports_without_hook(self, pm: PortManager):
        """A port lacking on_client_count_changed (loopback) must not error."""
        adapter = SimpleNamespace(name="loopback_test", get_adapter_type=lambda: "loopback")
        port = NoHookPort()
        await pm.register_unified_port("plain", port, adapter)

        assert await pm.add_client_to_port("plain", "c1", "alice") is True
        assert await pm.remove_client_from_port("plain", "c1") is True

    @pytest.mark.asyncio
    async def test_hook_failure_does_not_block_client_remove(self, pm: PortManager, caplog):
        """A hook exception is logged; the removal still completes."""
        adapter = RecordingAdapter()
        port = RecordingPort()

        def _boom(count: int) -> None:
            raise RuntimeError("hook exploded")

        port.on_client_count_changed = _boom  # type: ignore[method-assign]
        await pm.register_unified_port("rec1", port, adapter)

        assert await pm.add_client_to_port("rec1", "c1", "alice") is True
        with caplog.at_level(logging.ERROR, logger=""):
            assert await pm.remove_client_from_port("rec1", "c1") is True

        removed = [c for c in pm.get_port("rec1").connected_clients if c["client_id"] == "c1"]
        assert removed == [], "client must be removed even when the hook raised"

    def test_helper_tolerates_unhooked_ports(self):
        """_fire_client_count_hook no-ops on None and hook-less port objects."""
        pm = PortManager([])
        pm._fire_client_count_hook(None)  # must not raise
        pm._fire_client_count_hook(SimpleNamespace(name="x", connected_clients=[{"client_id": "c"}]))

    def test_helper_resolves_unified_port(self, monkeypatch):
        """The helper targets the wrapper's underlying unified port."""
        from openmux.server import data_logger as dl_mod

        monkeypatch.setattr(dl_mod.DataLogger, "get", classmethod(lambda cls: _DummyDataLogger()), raising=False)
        pm = PortManager([])
        adapter = RecordingAdapter()
        port = RecordingPort()
        wrapper = SimpleNamespace(name="rec1", unified_port=port, connected_clients=[{"client_id": "c"}])
        pm._fire_client_count_hook(wrapper)
        assert port.calls == [1]
