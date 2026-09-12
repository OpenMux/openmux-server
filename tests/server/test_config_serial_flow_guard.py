"""Config-manager validation for serial flow-control / signal-line combos (issue #63).

``flow_control: rtscts`` hands the RTS pin to the kernel CRTSCTS handshake,
so a configured ``rts`` value is a misconfiguration: the Config Manager
rejects it at load/save/reload, which is the authoritative enforcement
point (the same routine the Config Editor pre-validate/save path runs).
"""

import pytest

from openmux.server.config_manager import ConfigManager


def _cm(config: dict) -> ConfigManager:
    """Build a ConfigManager with a pre-set config, bypassing load_config."""
    cm = ConfigManager.__new__(ConfigManager)
    cm.config = config
    return cm


class TestRtsctsGuard:
    @pytest.mark.parametrize("bad_rts", ["on", "off", "presence-on", "presence-off", True, False])
    def test_rtscts_with_managed_rts_rejected(self, bad_rts):
        cm = _cm({"serial_ports": [{"name": "p", "device": "/dev/ttyX", "flow_control": "rtscts", "rts": bad_rts}]})
        with pytest.raises(ValueError, match="rtscts"):
            cm._validate_serial_ports_config()

    def test_rtscts_with_none_rts_ok(self):
        cm = _cm({"serial_ports": [{"name": "p", "device": "/dev/ttyX", "flow_control": "rtscts", "rts": "none"}]})
        cm._validate_serial_ports_config()

    def test_rtscts_omitted_rts_ok(self):
        cm = _cm({"serial_ports": [{"name": "p", "device": "/dev/ttyX", "flow_control": "rtscts"}]})
        cm._validate_serial_ports_config()

    def test_rtscts_with_dtr_ok(self):
        """DTR is not affected by any flow-control mode."""
        for flow in ("rtscts", "dsrdtr", "xonxoff", "none"):
            cm = _cm({"serial_ports": [{"name": "p", "device": "/dev/ttyX", "flow_control": flow, "dtr": "presence-on"}]})
            cm._validate_serial_ports_config()

    def test_error_names_the_port(self):
        cm = _cm(
            {
                "serial_ports": [
                    {"name": "ok", "device": "/dev/ttyA"},
                    {"name": "bad", "device": "/dev/ttyB", "flow_control": "rtscts", "rts": "on"},
                ]
            }
        )
        with pytest.raises(ValueError) as exc:
            cm._validate_serial_ports_config()
        assert "bad" in str(exc.value)

    def test_unified_dict_format_rejected(self):
        """The historical unified adapter-dict form is rejected, array-only."""
        cm = _cm(
            {
                "serial_ports": {
                    "adapter_type": "serial",
                    "ports": [{"name": "p", "device": "/dev/ttyX", "flow_control": "rtscts", "rts": "off"}],
                }
            }
        )
        with pytest.raises(ValueError, match="unified adapter dict"):
            cm._validate_serial_ports_config()

    def test_non_list_section_rejected_and_named(self):
        cm = _cm({"serial_ports": {"nope": 1}})
        with pytest.raises(ValueError, match="serial_ports must be a list of port entries") as exc:
            cm._validate_serial_ports_config()
        assert "no longer supported" in str(exc.value)

    def test_port_entry_without_name_rejected(self):
        cm = _cm({"serial_ports": [{"device": "/dev/ttyX"}]})
        with pytest.raises(ValueError, match="missing required 'name'"):
            cm._validate_serial_ports_config()

    def test_non_dict_port_entry_rejected(self):
        cm = _cm({"serial_ports": ["not-a-dict"]})
        with pytest.raises(ValueError, match="must be a mapping of port fields"):
            cm._validate_serial_ports_config()


class TestLinePolicyManaged:
    @pytest.mark.parametrize("unmanaged", [None, "none", " NONE ", ""])
    def test_unmanaged_values(self, unmanaged):
        cm = _cm({"serial_ports": []})
        assert cm._line_policy_managed(unmanaged) is False

    @pytest.mark.parametrize("managed", ["on", "off", "presence-on", "presence-off", True, False, "weird"])
    def test_managed_values(self, managed):
        cm = _cm({"serial_ports": []})
        assert cm._line_policy_managed(managed) is True
