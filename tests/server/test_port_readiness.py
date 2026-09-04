"""Tests for port readiness (issue #68).

Readiness is a derived UI-facing axis (active / idle / offline), separate from
the ``PortState`` lifecycle axis. It is computed from ``(alive, status_message)``
at the status-snapshot point; a non-empty ``status_message`` always wins (red),
"no reason and not running" is yellow idle, otherwise green active.

Covers:
- the derivation matrix (``derive_port_readiness`` / ``port_is_alive``)
- the wrapper snapshot (``UnifiedPortWrapper.get_status()['readiness']``)
- the tcp_initiator intentional-rest fix (``_disconnect`` no longer sets a reason)
- the command adapter scenarios (clean exit, fresh on-demand, max-restarts,
  non-zero exit) at the wrapper level
- the federated proxy (``RemotePortProxy.get_status()['readiness']``) including
  link-down precedence over a stale origin value
"""

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

from openmux.server.adapters.lifecycle import (
    READINESS_ACTIVE,
    READINESS_IDLE,
    READINESS_OFFLINE,
    derive_port_readiness,
    port_is_alive,
)
from openmux.server.port_manager import PortManager

# ---------------------------------------------------------------------------
# Derivation matrix
# ---------------------------------------------------------------------------


def test_derive_readiness_matrix():
    assert derive_port_readiness(True, None) == READINESS_ACTIVE
    assert derive_port_readiness(True, "") == READINESS_ACTIVE
    assert derive_port_readiness(False, None) == READINESS_IDLE
    assert derive_port_readiness(False, "") == READINESS_IDLE
    # A reason always wins, even while "alive" (a link that reports a reason is
    # offline by definition).
    assert derive_port_readiness(True, "Connection refused") == READINESS_OFFLINE
    assert derive_port_readiness(False, "Process exited with code 2") == READINESS_OFFLINE


def test_port_is_alive_prefers_process_active():
    # The command-port discriminator: a healthy resting shell keeps
    # is_connected=True while process_active=False. The process flag must win,
    # else a resting shell would read "active".
    assert port_is_alive(SimpleNamespace(process_active=False, is_connected=True)) is False
    assert port_is_alive(SimpleNamespace(process_active=True, is_connected=True)) is True
    # No process flag: falls through to is_connected.
    assert port_is_alive(SimpleNamespace(is_connected=False)) is False
    assert port_is_alive(SimpleNamespace(is_connected=True)) is True
    # Falls through to is_running.
    assert port_is_alive(SimpleNamespace(is_running=False)) is False
    assert port_is_alive(SimpleNamespace(is_running=True)) is True
    # No liveness signal at all.
    assert port_is_alive(SimpleNamespace(name="x")) is False
    # Non-bool values are ignored (a live check, not a truthiness check).
    assert port_is_alive(SimpleNamespace(process_active="nope", is_connected=True)) is True


# ---------------------------------------------------------------------------
# PortManager wrapper snapshot
# ---------------------------------------------------------------------------


class _FakeAdapter:
    def __init__(self, port_type: str = "command"):
        self._type = port_type

    def get_adapter_type(self) -> str:
        return self._type


def _make_wrapper(unified_port, adapter_type: str = "command"):
    pm = PortManager({})
    return pm._create_unified_port_wrapper(unified_port, _FakeAdapter(adapter_type))


def _cmd_port(
    *,
    process_active: bool = False,
    is_connected: bool = True,
    is_running: bool = False,
    status_message: str = "",
) -> SimpleNamespace:
    def snapshot() -> Dict[str, Any]:
        snap: Dict[str, Any] = {"serial_config": {"device": "shell:test"}}
        if status_message:
            snap["status_message"] = status_message
        return snap

    return SimpleNamespace(
        name="cp",
        state=SimpleNamespace(value="active"),
        process_active=process_active,
        is_connected=is_connected,
        is_running=is_running,
        max_read_write_users=1,
        get_status_snapshot=snapshot,
    )


def test_wrapper_readiness_idle_for_resting_command_port():
    # Motivating case: clean code-0 exit, auto_restart off. Not running, no reason.
    w = _make_wrapper(_cmd_port(process_active=False, is_connected=True, is_running=False))
    assert w.get_status()["readiness"] == READINESS_IDLE


def test_wrapper_readiness_active_for_running_command_port():
    w = _make_wrapper(_cmd_port(process_active=True, is_connected=True, is_running=True))
    assert w.get_status()["readiness"] == READINESS_ACTIVE


def test_wrapper_readiness_offline_for_reason_bearing_port():
    w = _make_wrapper(_cmd_port(process_active=False, is_connected=False, status_message="Process exited with code 3"))
    st = w.get_status()
    assert st["readiness"] == READINESS_OFFLINE
    assert st["status_message"] == "Process exited with code 3"


def test_wrapper_readiness_active_for_connected_tcp():
    # Serial/tcp/loopback style: no process_active; is_connected is the signal.
    port = SimpleNamespace(
        name="tcp1",
        state=SimpleNamespace(value="active"),
        is_connected=True,
        is_running=True,
        max_read_write_users=1,
        get_status_snapshot=lambda: {"serial_config": {"device": "tcp:h:1"}},
    )
    w = _make_wrapper(port, "tcp")
    assert w.get_status()["readiness"] == READINESS_ACTIVE


def test_wrapper_readiness_idle_for_disconnected_tcp_without_reason():
    port = SimpleNamespace(
        name="tcp2",
        state=SimpleNamespace(value="configured"),
        is_connected=False,
        is_running=False,
        max_read_write_users=1,
        get_status_snapshot=lambda: {"serial_config": {"device": "tcp:h:1"}},
    )
    w = _make_wrapper(port, "tcp")
    assert w.get_status()["readiness"] == READINESS_IDLE


# ---------------------------------------------------------------------------
# tcp_initiator: intentional rest must not set an error reason
# ---------------------------------------------------------------------------


class _FakeStreamWriter:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_tcp_disconnect_clears_status_message_on_intentional_rest():
    from openmux.server.adapters.tcp_initiator import TcpInitiatorPort

    notified: list = []
    port = TcpInitiatorPort("tcp-rest", {"host": "h", "port": 1234}, SimpleNamespace(name="mx"))
    port._meta_notify = lambda name, payload: notified.append(payload)
    port.is_connected = True
    port.reader = object()
    port.writer = _FakeStreamWriter()
    # Establish the pre-disconnect state: connected, healthy.
    port._set_status_message("", connected=True)

    await port._disconnect()

    assert port.is_connected is False
    # The fix: an intentional rest leaves no reason, so readiness derives idle.
    assert port.status_message == ""
    assert derive_port_readiness(False, port.status_message) == READINESS_IDLE
    # A meta refresh was pushed with an empty reason so peers / UI update.
    assert any(payload.get("status_message") == "" for payload in notified)


@pytest.mark.asyncio
async def test_tcp_read_loop_still_sets_reason_on_remote_close():
    from openmux.server.adapters.tcp_initiator import TcpInitiatorPort

    port = TcpInitiatorPort("tcp-close", {"host": "h", "port": 1234}, SimpleNamespace(name="mx"))

    class _Reader:
        async def read(self, n: int) -> bytes:
            return b""  # EOF: closed by remote

    port.is_connected = True
    port.reader = _Reader()
    await port._read_loop()

    # A remote close is a genuine failure: the reason is set and stays red.
    assert "closed by remote" in port.status_message
    assert derive_port_readiness(False, port.status_message) == READINESS_OFFLINE


# ---------------------------------------------------------------------------
# Command adapter: scenarios at the port level via _monitor_loop
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, code: int) -> None:
        self._code = code

    async def wait(self) -> int:
        return self._code


class _CapturingPortManager:
    def __init__(self) -> None:
        self.ports: Dict[str, Any] = {}
        self.pushed: list = []

    def notify_meta_updated(self, name: str, payload: Optional[Dict[str, Any]]) -> None:
        self.pushed.append(payload or {})


def _make_cmd_port(config: Dict[str, Any]) -> Any:
    from openmux.server.adapters.command import CommandPort

    pm = _CapturingPortManager()
    adapter = SimpleNamespace(main_port_manager=pm)
    port = CommandPort("cp-r", config, adapter)
    port.use_pty = False
    port.is_running = True
    port._read_task = None
    return port


@pytest.mark.asyncio
async def test_command_clean_exit_zero_auto_restart_off_is_idle():
    port = _make_cmd_port({"command": "echo", "auto_restart": False})
    port.process = _FakeProc(0)
    await port._monitor_loop()
    assert port.status_message == ""
    assert port.is_connected is True
    assert port.process_active is False
    assert derive_port_readiness(port_is_alive(port), port.status_message) == READINESS_IDLE


@pytest.mark.asyncio
async def test_command_nonzero_exit_auto_restart_off_is_offline():
    port = _make_cmd_port({"command": "echo", "auto_restart": False})
    port.process = _FakeProc(2)
    await port._monitor_loop()
    assert "code 2" in port.status_message
    assert derive_port_readiness(port_is_alive(port), port.status_message) == READINESS_OFFLINE


@pytest.mark.asyncio
async def test_command_fresh_on_demand_never_spawned_is_idle():
    port = _make_cmd_port({"command": "echo", "spawn_on_demand": True})
    port.is_running = False
    # Never spawned: process_active False, is_connected True (contract), no reason.
    assert port.process_active is False
    assert port.is_connected is True
    assert port.status_message == ""
    assert derive_port_readiness(port_is_alive(port), port.status_message) == READINESS_IDLE


@pytest.mark.asyncio
async def test_command_max_restarts_is_offline():
    port = _make_cmd_port({"command": "echo", "auto_restart": True, "max_restarts": 2, "restart_delay": 0.0})
    port.restart_count = 2
    port.process = _FakeProc(0)
    await port._monitor_loop()
    assert "Max restarts reached" in port.status_message
    assert derive_port_readiness(port_is_alive(port), port.status_message) == READINESS_OFFLINE


@pytest.mark.asyncio
async def test_command_auto_restart_gap_sets_reason_until_respawn(monkeypatch):
    """The auto-restart gap must be red, not idle (issue #68: stays red).

    A resting-with-reason port derives offline; the gap therefore needs a
    reason. The respawn path clears it on success (see _spawn_process).
    """
    port = _make_cmd_port({"command": "echo", "auto_restart": True, "restart_delay": 5.0})
    port.process = _FakeProc(0)

    recorded: list = []
    orig_set_msg = port._set_status_message

    def recording_set_msg(message: str) -> None:
        recorded.append(message)
        return orig_set_msg(message)

    port._set_status_message = recording_set_msg  # type: ignore[method-assign]

    calls = {"n": 0}

    async def fake_spawn() -> bool:
        calls["n"] += 1
        if calls["n"] >= 2:
            port.is_running = False  # end the monitor loop after two gaps
        return True

    port._spawn_process = fake_spawn  # type: ignore[method-assign]

    async def fast_sleep(_delay):
        await asyncio.sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    await port._monitor_loop()

    # Each gap pass set a red reason before respawning.
    assert any(m.startswith("Restarting in 5.0s (exit code 0)") for m in recorded)
    # While the gap reason was set, the port derived offline, not idle.
    assert derive_port_readiness(port_is_alive(port), "Restarting in 5.0s (exit code 0)") == READINESS_OFFLINE


# ---------------------------------------------------------------------------
# Federated proxy: RemotePortProxy.get_status readiness
# ---------------------------------------------------------------------------


def _make_muxcon_adapter():
    from openmux.server.adapters.muxcon import UnifiedMuxConAdapter

    a = UnifiedMuxConAdapter("mx", {"muxcon": {}})
    return a


def _make_proxy(
    a, name: str = "remote1", *, status_message=None, readiness=None, is_connected: bool = True, link_reason: str = ""
):
    from openmux.common.federation_types import PortMetadata, ServerInfo, ServerType

    si = ServerInfo(server_id="peer1", hostname="peer1", port=0, server_type=ServerType.LEAF, description="")
    meta = PortMetadata(
        name=name,
        original_name=name,
        description="Remote",
        adapter_type="remote_muxcon",
        origin_server=si,
        server_chain=[si],
        status="disconnected" if status_message else "connected",
        max_rw_users=1,
        status_message=status_message,
        readiness=readiness,
    )
    proxy = a.RemotePortProxy(a, "node:peer", name, meta)
    proxy.is_connected = is_connected
    proxy.link_reason = link_reason
    return proxy


def test_proxy_readiness_prefers_origin_value():
    a = _make_muxcon_adapter()
    proxy = _make_proxy(a, readiness=READINESS_IDLE, is_connected=True)
    st = proxy.get_status()
    assert st["readiness"] == READINESS_IDLE
    assert "status_message" not in st


def test_proxy_readiness_offline_stale_origin_when_link_down():
    # A down muxcon link overrides a stale origin readiness (link is freshest).
    a = _make_muxcon_adapter()
    proxy = _make_proxy(a, readiness=READINESS_ACTIVE, is_connected=False, link_reason="MuxCon link to p1 is down")
    st = proxy.get_status()
    assert st["readiness"] == READINESS_OFFLINE
    assert st["status_message"] == "MuxCon link to p1 is down"


def test_proxy_readiness_derives_when_no_origin_value():
    # Older peer sends no readiness: derive from link state + merged reason.
    a = _make_muxcon_adapter()
    # Link up, no reason -> active.
    p1 = _make_proxy(a, name="r1", readiness=None, is_connected=True)
    assert p1.get_status()["readiness"] == READINESS_ACTIVE
    # Link down, no reason -> idle (derivation fallback).
    p2 = _make_proxy(a, name="r2", readiness=None, is_connected=False)
    assert p2.get_status()["readiness"] == READINESS_IDLE
    # Link down, link reason -> offline.
    p3 = _make_proxy(a, name="r3", readiness=None, is_connected=False, link_reason="MuxCon link to p1 is down")
    assert p3.get_status()["readiness"] == READINESS_OFFLINE


def test_proxy_readiness_uses_origin_status_message_when_no_origin_readiness():
    # Origin forwarded a reason but predates readiness (mixed version): it must
    # still show offline with that reason even while the link is up.
    a = _make_muxcon_adapter()
    proxy = _make_proxy(a, status_message="Connection refused by h:1", readiness=None, is_connected=True)
    st = proxy.get_status()
    assert st["readiness"] == READINESS_OFFLINE
    assert st["status_message"] == "Connection refused by h:1"
