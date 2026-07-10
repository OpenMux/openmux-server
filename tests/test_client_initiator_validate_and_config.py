"""Validation and configuration tests for openmux_client_ports compat alias.

These tests verify that TcpInitiatorAdapter correctly handles the legacy
openmux_client_ports config format (flat remote_port / api_key / username+password)
as well as the new tcp_initiator_ports format with protocol: {type: openmux}.
"""

import pytest

from openmux.server.adapters.tcp_initiator import TcpInitiatorAdapter


# ── openmux_client_ports (legacy flat format) ────────────────────────────────

def test_validate_config_accepts_openmux_section_with_api_key():
    cfg = {
        "openmux_client_ports": [
            {"name": "p1", "host": "h", "port": 8023, "remote_port": "r1", "api_key": "k"}
        ]
    }
    assert TcpInitiatorAdapter.validate_config(cfg) is True


def test_validate_config_accepts_openmux_section_with_user_pass():
    cfg = {
        "openmux_client_ports": [
            {"name": "p2", "host": "h", "port": 8023, "remote_port": "r2",
             "username": "u", "password": "p"}
        ]
    }
    assert TcpInitiatorAdapter.validate_config(cfg) is True


@pytest.mark.parametrize("bad_cfg", [
    {},                                                          # missing section
    {"openmux_client_ports": {}},                               # wrong type
    {"openmux_client_ports": ["not-a-dict"]},                   # wrong item type
    {"openmux_client_ports": [{"host": "h", "port": 1, "remote_port": "r"}]},  # missing name
    {"openmux_client_ports": [{"name": "p", "port": 1, "remote_port": "r"}]},  # missing host
    {"openmux_client_ports": [{"name": "p", "host": "h", "remote_port": "r"}]},# missing port
    {"openmux_client_ports": [{"name": "p", "host": "h", "port": 1}]},          # missing remote_port
    {"openmux_client_ports": [{"name": "p", "host": "h", "port": 1, "remote_port": "r"}]},  # missing auth
])
def test_validate_config_rejects_invalid_openmux(bad_cfg):
    assert TcpInitiatorAdapter.validate_config(bad_cfg) is False


# ── tcp_initiator_ports with protocol: {type: openmux} (new format) ──────────

def test_validate_config_accepts_new_format_openmux():
    cfg = {
        "tcp_initiator_ports": [
            {"name": "p1", "host": "h", "port": 9000,
             "protocol": {"type": "openmux", "remote_port": "r1", "api_key": "k"}}
        ]
    }
    assert TcpInitiatorAdapter.validate_config(cfg) is True


def test_validate_config_new_format_rejects_missing_remote_port():
    cfg = {
        "tcp_initiator_ports": [
            {"name": "p1", "host": "h", "port": 9000,
             "protocol": {"type": "openmux", "api_key": "k"}}
        ]
    }
    assert TcpInitiatorAdapter.validate_config(cfg) is False


# ── get_port_configurations with openmux_client_ports ────────────────────────

def test_get_port_configurations_from_openmux_section():
    cfg = {
        "openmux_client_ports": [
            {"name": "alpha", "host": "h1", "port": 8023, "remote_port": "r1",
             "api_key": "k", "use_tls": True, "timeout": 12.5},
            {"name": "beta", "host": "h2", "port": 9000, "remote_port": "r2",
             "username": "u", "password": "p"},
        ]
    }
    adapter = TcpInitiatorAdapter("test", cfg)
    ports = adapter.get_port_configurations()
    assert set(ports.keys()) == {"alpha", "beta"}
    assert ports["alpha"]["use_tls"] is True
    assert ports["alpha"]["timeout"] == 12.5
    assert ports["beta"]["port"] == 9000
    # protocol sub-key is injected
    assert ports["alpha"]["protocol"]["type"] == "openmux"
    assert ports["alpha"]["protocol"]["remote_port"] == "r1"
    assert ports["beta"]["protocol"]["username"] == "u"


def test_adapter_type_is_tcp_initiator():
    adapter = TcpInitiatorAdapter("test", {})
    assert adapter.get_adapter_type() == "tcp_initiator"
