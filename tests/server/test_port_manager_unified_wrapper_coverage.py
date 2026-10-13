"""Behavior + coverage tests for `UnifiedPortWrapper`, built by
`PortManager._create_unified_port_wrapper` (openmux/server/port_manager.py).

The factory nests the whole wrapper class, so it scored 37 under C901 and was
poorly covered for the async write/disconnect paths. These tests drive the real
factory with fakes and pin, per method: `__init__` defaults vs provided values,
`get_status` (state/is_running/connected, the three get_status_snapshot forms,
readiness), `add_client`, `remove_client` (client-queue pop + last-client
queue drain), `write_data` (adapter write_to_port bool/int/other/raise, then
the `_writer` write/async_write/absent fallbacks), and `disconnect` (stop vs
disconnect, each with a raising hook).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from openmux.server.port_manager import PortManager


class _Adapter:
    """Unified adapter: get_adapter_type is the only member the wrapper needs."""

    def __init__(self, port_type: str = "serial", write_to_port=None):
        self._type = port_type
        if write_to_port is not None:
            # Bind as a real method on this instance.
            self.write_to_port = write_to_port

    def get_adapter_type(self) -> str:
        return self._type


def _make(unified_port, adapter_type: str = "serial", write_to_port=None):
    w = PortManager({})._create_unified_port_wrapper(unified_port, _Adapter(adapter_type, write_to_port))
    w.logger = MagicMock()
    return w


def _port(**overrides):
    base = SimpleNamespace(name="up", state=SimpleNamespace(value="active"), is_connected=True, is_running=True)
    base.__dict__.update(overrides)
    return base


# --- __init__ / properties -------------------------------------------------


def test_init_defaults_and_provided():
    w = _make(_port(), "loopback")
    assert w.name == "up"
    assert w.adapter_type == "loopback"
    assert w.loopback is True
    assert w.description.startswith("Unified loopback")
    assert w.is_running is True
    assert w.max_read_write_users == 5
    assert w.connected_clients == []
    assert w.client_queues == {}
    assert w.dropped_chunks == 0
    assert w._federation_viewer_count == 0
    assert w.scrollback_size == 0
    assert isinstance(w.data_queue, asyncio.Queue)

    up = _port(description="desc", is_running=False, max_read_write_users=2, scrollback_size="7")
    w2 = _make(up, "serial")
    assert w2.description == "desc"
    assert w2.is_running is False
    assert w2.max_read_write_users == 2
    assert w2.scrollback_size == 7
    assert w2.loopback is False


def test_group_properties_read_live():
    up = _port(read_write_groups=["rw"], read_only_groups=["ro"])
    w = _make(up)
    assert w.read_write_groups == ["rw"]
    assert w.read_only_groups == ["ro"]
    up.read_write_groups.append("rw2")  # same object; live property must see it
    assert w.read_write_groups == ["rw", "rw2"]
    up2 = _port()
    assert _make(up2).read_write_groups == []


# --- get_status ------------------------------------------------------------


def test_get_status_core_fields():
    w = _make(_port(is_connected=False, is_running=True))
    st = w.get_status()
    assert st["name"] == "up"
    assert st["state"] == "active"
    assert st["is_running"] is True
    assert st["connected"] is False  # is_connected present -> used over is_running
    assert st["connected_clients"] == 0
    assert st["dropped_chunks"] == 0


def test_get_status_missing_state():
    up = _port()
    del up.state  # no `state` attr -> "active"
    assert _make(up).get_status()["state"] == "active"


def test_get_status_connected_falls_back_to_running():
    up = _port(is_running=True)
    del up.is_connected  # no is_connected -> connected mirrors is_running
    assert _make(up).get_status()["connected"] is True


def test_get_status_snapshot_callable():
    w = _make(_port(get_status_snapshot=lambda: {"extra": 1}))
    assert w.get_status()["extra"] == 1


def test_get_status_snapshot_callable_non_dict():
    w = _make(_port(get_status_snapshot=lambda: ("not", "dict")))
    assert "not" not in w.get_status()


def test_get_status_snapshot_callable_none():
    w = _make(_port(get_status_snapshot=lambda: None))  # None or {} -> no update
    assert "extra" not in w.get_status()


def test_get_status_snapshot_callable_raises():
    def boom():
        raise RuntimeError("snap boom")

    w = _make(_port(get_status_snapshot=boom))
    st = w.get_status()  # exception swallowed, core fields intact
    assert st["name"] == "up"
    w.logger.debug.assert_called_once()


def test_get_status_snapshot_attr_dict():
    w = _make(_port(status_snapshot={"attr_snap": 5}))
    assert w.get_status()["attr_snap"] == 5


def test_get_status_snapshot_attr_non_dict():
    w = _make(_port(status_snapshot=["not", "dict"]))
    assert "not" not in w.get_status()


def test_get_status_readiness_and_message():
    w = _make(_port())
    assert w.get_status()["readiness"] in ("active", "idle", "offline")


def test_get_status_readiness_derive_raises_swallowed():
    # A snapshot whose status_message object raises on bool() makes
    # derive_port_readiness raise mid-call; the optional-readiness except
    # swallows it and the core status fields survive.
    class BoomBool:
        def __bool__(self):
            raise RuntimeError("bool boom")

    w = _make(_port(get_status_snapshot=lambda: {"status_message": BoomBool()}))
    st = w.get_status()
    assert st["name"] == "up"
    assert "readiness" not in st


# --- add_client / remove_client -------------------------------------------


def test_add_client_adds_and_hooks():
    calls = []
    w = _make(_port(on_client_count_changed=calls.append))
    w.add_client({"client_id": "c1"})
    assert w.connected_clients == [{"client_id": "c1"}]
    assert calls == [1]


def test_add_client_duplicate_ignored():
    calls = []
    w = _make(_port(on_client_count_changed=calls.append))
    w.add_client({"client_id": "c1"})
    w.add_client({"client_id": "c1"})
    assert w.connected_clients == [{"client_id": "c1"}]
    assert calls == [1]


def test_add_client_hook_raises_logged():
    def boom(_count):
        raise RuntimeError("hook boom")

    w = _make(_port(on_client_count_changed=boom))
    w.add_client({"client_id": "c1"})  # must not propagate
    assert len(w.connected_clients) == 1
    w.logger.error.assert_called_once()


def test_remove_client_pops_queue_and_drains():
    calls = []
    w = _make(_port(on_client_count_changed=calls.append))
    w.add_client({"client_id": "c1"})
    w.client_queues["c1"] = asyncio.Queue()
    w.data_queue.put_nowait(b"x")
    w.remove_client({"client_id": "c1"})
    assert w.connected_clients == []
    assert "c1" not in w.client_queues  # queued dropped on removal
    assert w.data_queue.empty()  # last client -> drain
    assert calls == [1, 0]


def test_remove_client_non_dict_and_no_queue():
    w = _make(_port())
    w.add_client("plain-string")
    w.remove_client("plain-string")
    assert w.connected_clients == []


def test_remove_client_absent_is_noop():
    w = _make(_port())
    w.remove_client({"client_id": "ghost"})
    assert w.connected_clients == []


def test_remove_client_last_with_viewer_keeps_queue():
    w = _make(_port())
    w._federation_viewer_count = 1
    w.data_queue.put_nowait(b"keep")
    w.add_client({"client_id": "c1"})
    w.remove_client({"client_id": "c1"})
    assert not w.data_queue.empty()  # federation hold keeps the buffered chunk


def test_remove_client_hook_raises_logged():
    def boom(_count):
        raise RuntimeError("hook boom")

    w = _make(_port(on_client_count_changed=boom))
    w.add_client({"client_id": "c1"})
    w.logger.reset_mock()
    w.remove_client({"client_id": "c1"})
    w.logger.error.assert_called_once()


# --- write_data ------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_data_adapter_bool():
    async def wtp(name, data):
        return False

    w = _make(_port(), "serial", write_to_port=wtp)
    assert (await w.write_data(b"x")) is False

    async def wtp_true(name, data):
        return True

    assert (await _make(_port(), "serial", write_to_port=wtp_true).write_data(b"x")) is True


@pytest.mark.asyncio
async def test_write_data_adapter_int():
    async def wtp_zero(name, data):
        return 0

    assert (await _make(_port(), "serial", write_to_port=wtp_zero).write_data(b"x")) is False

    async def wtp_one(name, data):
        return 5

    assert (await _make(_port(), "serial", write_to_port=wtp_one).write_data(b"x")) is True


@pytest.mark.asyncio
async def test_write_data_adapter_other_returns_true():
    async def wtp(name, data):
        return object()  # neither bool nor int -> True

    assert (await _make(_port(), "serial", write_to_port=wtp).write_data(b"x")) is True


@pytest.mark.asyncio
async def test_write_data_adapter_raises():
    async def wtp(name, data):
        raise RuntimeError("wtp boom")

    w = _make(_port(), "serial", write_to_port=wtp)
    assert (await w.write_data(b"x")) is False
    w.logger.error.assert_called_once()


@pytest.mark.asyncio
async def test_write_data_writer_write_fallback():
    # No adaptor.write_to_port -> the _writer.write path (bytes written > 0).
    class W:
        async def write(self, data):
            return 3

    assert (await _make(_port(_writer=W())).write_data(b"x")) is True

    class WZero:
        async def write(self, data):
            return 0

    assert (await _make(_port(_writer=WZero())).write_data(b"x")) is False


@pytest.mark.asyncio
async def test_write_data_async_write_fallback():
    class W:
        async def async_write(self, data):
            return 4

    assert (await _make(_port(_writer=W())).write_data(b"x")) is True

    class WZero:
        async def async_write(self, data):
            return 0

    assert (await _make(_port(_writer=WZero())).write_data(b"x")) is False


@pytest.mark.asyncio
async def test_write_data_writer_raises():
    class W:
        async def write(self, data):
            raise RuntimeError("writer boom")

    w = _make(_port(_writer=W()))
    assert (await w.write_data(b"x")) is False
    w.logger.error.assert_called_once()


@pytest.mark.asyncio
async def test_write_data_no_writer_returns_false():
    assert (await _make(_port()).write_data(b"x")) is False  # adapter has no write_to_port, no _writer


# --- disconnect ------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_stop():
    calls = []

    up = _port()

    async def stop():
        calls.append("stop")

    up.stop = stop
    w = _make(up)
    await w.disconnect()
    assert calls == ["stop"]


@pytest.mark.asyncio
async def test_disconnect_stop_raises_logged():
    up = _port()

    async def boom():
        raise RuntimeError("stop boom")

    up.stop = boom
    w = _make(up)
    await w.disconnect()  # must not propagate
    w.logger.error.assert_called_once()


@pytest.mark.asyncio
async def test_disconnect_only():
    calls = []

    up = _port()

    async def dc():
        calls.append("dc")

    up.disconnect = dc
    w = _make(up)
    await w.disconnect()
    assert calls == ["dc"]


@pytest.mark.asyncio
async def test_disconnect_only_raises_logged():
    up = _port()

    async def boom():
        raise RuntimeError("dc boom")

    up.disconnect = boom
    w = _make(up)
    await w.disconnect()
    w.logger.error.assert_called_once()


@pytest.mark.asyncio
async def test_disconnect_noop_when_neither():
    w = _make(_port())  # port has neither stop nor disconnect
    await w.disconnect()  # must not raise
