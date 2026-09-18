"""Tests for the per-port ``power:`` feed key across the four local adapters.

Each adapter must (a) store the refs from port config on the port instance
and (b) update them in place during reconcile_ports without recreating the
port (no session drop) - the power adapter reads the attribute live.
"""

import logging

import pytest

from openmux.server.adapters.command import CommandAdapter, CommandPort
from openmux.server.adapters.loopback import LoopbackAdapter, LoopbackPort
from openmux.server.adapters.serial import SerialAdapter, SerialPortWrapper
from openmux.server.adapters.tcp_initiator import TcpInitiatorAdapter, TcpInitiatorPort

asyncio_test = pytest.mark.asyncio
_LOG = logging.getLogger("openmux.test.port_power_key")


# --- storage from config ------------------------------------------------------


def test_loopback_port_stores_power():
    p = LoopbackPort("l1", {"name": "l1", "power": ["rack1.3", "rack1.A1"]}, None)
    assert p.power == ["rack1.3", "rack1.A1"]


def test_command_port_stores_power():
    port = CommandPort("c1", {"name": "c1", "command": "true", "power": ["rack1.3"]}, None)
    assert port.power == ["rack1.3"]


def test_serial_wrapper_stores_power():
    wrapper = SerialPortWrapper({"name": "s1", "device": "/dev/null", "power": ["rack1.3"]}, _LOG)
    assert wrapper.power == ["rack1.3"]


def test_tcp_initiator_port_stores_power():
    port = TcpInitiatorPort("t1", {"name": "t1", "host": "127.0.0.1", "port": 9999, "power": ["rack1.3"]}, None)
    assert port.power == ["rack1.3"]


def test_missing_power_key_defaults_empty():
    assert LoopbackPort("l2", {"name": "l2"}, None).power == []
    assert SerialPortWrapper({"name": "s2", "device": "/dev/null"}, _LOG).power == []
    assert TcpInitiatorPort("t2", {"name": "t2", "host": "127.0.0.1", "port": 1}, None).power == []


# --- in-place reconcile (no port recreate) --------------------------------------


@asyncio_test
async def test_loopback_reconcile_updates_power_in_place():
    adapter = LoopbackAdapter("loopback", {"loopback_ports": [{"name": "l1", "power": ["rack1.3"]}]})
    await adapter.create_port("l1", {"name": "l1", "power": ["rack1.3"]})
    port_before = adapter.ports["l1"]
    res = await adapter.reconcile_ports([{"name": "l1", "power": ["rack1.3", "phaseA.A1"]}])
    assert res["unchanged"] == ["l1"] and not res["added"] and not res["removed"] and not res["updated"]
    assert adapter.ports["l1"] is port_before  # not recreated
    assert port_before.power == ["rack1.3", "phaseA.A1"]
    # dropping the key empties it in place
    res = await adapter.reconcile_ports([{"name": "l1"}])
    assert res["unchanged"] == ["l1"]
    assert port_before.power == []
    await adapter.stop()


@asyncio_test
async def test_command_reconcile_updates_power_in_place():
    port_cfg = {"name": "c1", "command": "true", "power": ["rack1.3"]}
    adapter = CommandAdapter("command", {"command_ports": [dict(port_cfg)]})
    await adapter.create_port("c1", port_cfg)
    port_before = adapter.ports["c1"]
    res = await adapter.reconcile_ports([{"name": "c1", "command": "true", "power": ["rack1.3", "rack1.1"]}])
    assert res["unchanged"] == ["c1"], res
    assert adapter.ports["c1"] is port_before
    assert adapter.ports["c1"].power == ["rack1.3", "rack1.1"]
    await adapter.stop()


@asyncio_test
async def test_serial_reconcile_updates_power_in_place():
    from unittest.mock import patch

    section = [{"name": "s1", "device": "/dev/null", "power": ["rack1.3"]}]
    adapter = SerialAdapter("serial", {"serial_ports": section})
    # create the wrapper directly (no real serial line needed for the reconcile path)
    wrapper = SerialPortWrapper(section[0], adapter.logger)
    adapter.serial_ports["s1"] = wrapper
    res = await adapter.reconcile_ports([{"name": "s1", "device": "/dev/null", "power": ["rack1.3", "phaseA.A1"]}])
    assert res["unchanged"] == ["s1"], res
    assert adapter.serial_ports["s1"] is wrapper
    assert wrapper.power == ["rack1.3", "phaseA.A1"]
    res = await adapter.reconcile_ports([{"name": "s1", "device": "/dev/null"}])
    assert res["unchanged"] == ["s1"], res
    assert wrapper.power == []


@asyncio_test
async def test_tcp_initiator_reconcile_updates_power_in_place():
    section = [{"name": "t1", "host": "127.0.0.1", "port": 9999, "power": ["rack1.3"]}]
    adapter = TcpInitiatorAdapter("tcp_initiator", {"tcp_initiator_ports": section})
    adapter.ports["t1"] = TcpInitiatorPort("t1", section[0], adapter)
    res = await adapter.reconcile_ports([{"name": "t1", "host": "127.0.0.1", "port": 9999, "power": ["rack1.3", "phaseA.A1"]}])
    assert res["unchanged"] == ["t1"], res
    assert adapter.ports["t1"].power == ["rack1.3", "phaseA.A1"]
    res = await adapter.reconcile_ports([{"name": "t1", "host": "127.0.0.1", "port": 9999}])
    assert res["unchanged"] == ["t1"], res
    assert adapter.ports["t1"].power == []
