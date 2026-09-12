"""Validation tests for TcpInitiatorAdapter openmux connections.

These verify the ``tcp_initiator_ports`` format with
``protocol: {type: openmux}``. The deprecated ``openmux_client_ports``
compat section was removed (ticket #72); configs using it are rejected
by the config manager (schema validation), and the adapter no longer reads it.
"""

import pytest

from openmux.server.adapters.tcp_initiator import TcpInitiatorAdapter

# ── openmux_client_ports (removed compat section; now rejected) ─────────────────


def test_validate_config_rejects_removed_openmux_section_api_key():
    # The compat alias is gone; only tcp_initiator_ports is read.
    cfg = {"openmux_client_ports": [{"name": "p1", "host": "h", "port": 8023, "remote_port": "r1", "api_key": "k"}]}
    assert TcpInitiatorAdapter.validate_config(cfg) is False


def test_validate_config_rejects_removed_openmux_section_user_pass():
    cfg = {
        "openmux_client_ports": [
            {"name": "p2", "host": "h", "port": 8023, "remote_port": "r2", "username": "u", "password": "p"}
        ]
    }
    assert TcpInitiatorAdapter.validate_config(cfg) is False


@pytest.mark.parametrize(
    "bad_cfg",
    [
        {},  # missing section
        {"openmux_client_ports": {}},  # wrong type
        {"openmux_client_ports": ["not-a-dict"]},  # wrong item type
        {"openmux_client_ports": [{"host": "h", "port": 1, "remote_port": "r"}]},  # missing name
        {"openmux_client_ports": [{"name": "p", "port": 1, "remote_port": "r"}]},  # missing host
        {"openmux_client_ports": [{"name": "p", "host": "h", "remote_port": "r"}]},  # missing port
        {"openmux_client_ports": [{"name": "p", "host": "h", "port": 1}]},  # missing remote_port
        {"openmux_client_ports": [{"name": "p", "host": "h", "port": 1, "remote_port": "r"}]},  # missing auth
    ],
)
def test_validate_config_rejects_invalid_openmux(bad_cfg):
    assert TcpInitiatorAdapter.validate_config(bad_cfg) is False


# ── tcp_initiator_ports with protocol: {type: openmux} (new format) ──────────


def test_validate_config_accepts_new_format_openmux():
    cfg = {
        "tcp_initiator_ports": [
            {"name": "p1", "host": "h", "port": 9000, "protocol": {"type": "openmux", "remote_port": "r1", "api_key": "k"}}
        ]
    }
    assert TcpInitiatorAdapter.validate_config(cfg) is True


def test_validate_config_new_format_rejects_missing_remote_port():
    cfg = {"tcp_initiator_ports": [{"name": "p1", "host": "h", "port": 9000, "protocol": {"type": "openmux", "api_key": "k"}}]}
    assert TcpInitiatorAdapter.validate_config(cfg) is False


# ── get_port_configurations ignores the removed openmux section ─────────────────


def test_get_port_configurations_ignores_removed_openmux_section():
    # The removed compat section is no longer read, so no ports are derived from it.
    cfg = {
        "openmux_client_ports": [
            {"name": "alpha", "host": "h1", "port": 8023, "remote_port": "r1", "api_key": "k"},
            {"name": "beta", "host": "h2", "port": 9000, "remote_port": "r2", "username": "u", "password": "p"},
        ]
    }
    adapter = TcpInitiatorAdapter("test", cfg)
    assert adapter.get_port_configurations() == {}


def test_adapter_type_is_tcp_initiator():
    adapter = TcpInitiatorAdapter("test", {})
    assert adapter.get_adapter_type() == "tcp_initiator"
