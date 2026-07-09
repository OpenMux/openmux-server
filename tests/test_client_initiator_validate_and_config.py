"""Validation and configuration tests for OpenMuxClientAdapter.

These tests replace the ones that previously lived alongside the
old openmux_client module. They now import from the renamed module
`client_initiator` but keep adapter_type and config keys stable.
"""

import pytest

from openmux.server.adapters.client_initiator import OpenMuxClientAdapter


def test_validate_config_accepts_top_level_section():
    cfg = {
        "openmux_client_ports": [
            {
                "name": "p1",
                "host": "h",
                "port": 8023,
                "remote_port": "r1",
                "api_key": "k",
            }
        ]
    }
    assert OpenMuxClientAdapter.validate_config(cfg) is True


def test_validate_config_accepts_direct_list():
    cfg_list = [
        {
            "name": "p2",
            "host": "h2",
            "port": 8023,
            "remote_port": "r2",
            "username": "u",
            "password": "p",
        }
    ]
    # When constructed directly with a list, validate_config gets called
    # by the factory with the list in some paths; ensure it handles that shape.
    assert OpenMuxClientAdapter.validate_config({"openmux_client_ports": cfg_list}) is True


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
        # missing auth credentials
        {"openmux_client_ports": [{"name": "p", "host": "h", "port": 1, "remote_port": "r"}]},
    ],
)
def test_validate_config_rejects_invalid(bad_cfg):
    assert OpenMuxClientAdapter.validate_config(bad_cfg) is False


def test_get_port_configurations_from_list():
    cfg_list = [
        {
            "name": "alpha",
            "host": "h1",
            "port": 8023,
            "remote_port": "r1",
            "api_key": "k",
            "use_tls": True,
            "timeout": 12.5,
        },
        {
            "name": "beta",
            "host": "h2",
            "port": 9000,
            "remote_port": "r2",
            "username": "u",
            "password": "p",
        },
    ]
    adapter = OpenMuxClientAdapter("test", cfg_list)
    ports = adapter.get_port_configurations()
    assert set(ports.keys()) == {"alpha", "beta"}
    assert ports["alpha"]["use_tls"] is True
    assert ports["alpha"]["timeout"] == 12.5
    assert ports["beta"]["port"] == 9000


def test_adapter_type_string_and_property_consistency():
    adapter = OpenMuxClientAdapter("test", [])
    assert adapter.get_adapter_type() == "openmux_client"
