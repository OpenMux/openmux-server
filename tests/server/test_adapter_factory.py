from typing import Any, Dict, List, Optional, Set

import pytest

from openmux.server.adapters.factory import AdapterPlugin, GenericAdapterFactory, PluginRegistry
from openmux.server.adapters.base_adapter import AdapterCapability, BaseGenericAdapter


class DummyAdapter(BaseGenericAdapter):
    adapter_type = "dummy"

    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        self.started = False

    def get_capabilities(self) -> Set[AdapterCapability]:
        return set()

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> bool:
        # Accept both unified and legacy styles for tests
        return isinstance(config, dict)

    async def start(self) -> bool:
        self.started = True
        return True

    async def stop(self) -> None:
        self.started = False

    async def create_port(self, port_name: str, config: Dict[str, Any]) -> Optional[Any]:
        return None

    async def destroy_port(self, port_name: str) -> None:
        return None

    def get_port_configurations(self) -> Dict[str, Dict[str, Any]]:
        return {}


def test_plugin_registry_register_and_lookup():
    reg = PluginRegistry()
    plugin = AdapterPlugin("Dummy", "dummy_section", DummyAdapter)
    reg.register_plugin(plugin)
    # Lookup by section
    assert reg.get_plugin("dummy_section") is plugin
    # Lookup by adapter_type (case-insensitive)
    assert reg.get_by_adapter_type("DuMmY") is plugin
    # Discover active plugins
    cfg = {"dummy_section": {"x": 1}}
    active = reg.discover_active_plugins(cfg)
    assert plugin in active


def test_factory_unified_adapters_success_and_skip_invalid():
    fac = GenericAdapterFactory()  # uses built-in registry
    cfg = {
        "server": {},
        "adapters": [
            {"type": "loopback", "name": "lb1", "loopback_ports": [{"name": "a"}]},
            {"type": "loopback", "name": "bad"},  # invalid, missing loopback_ports
        ],
    }
    result = fac.create_adapters_from_config(cfg)
    # Only the valid one should be created
    names = {a.name for a in result}
    assert "lb1" in names
    assert all(getattr(a, "name", None) != "bad" for a in result)


def test_factory_legacy_success_loopback_list_config():
    fac = GenericAdapterFactory()
    cfg = {
        "server": {},
        "loopback_ports": [{"name": "a"}],
    }
    result = fac.create_adapters_from_config(cfg)
    assert len(result) >= 1
    # The created adapter corresponds to the loopback plugin
    assert any(getattr(a, "name", None) == "loopback_ports" for a in result)


def test_factory_legacy_fail_fast_missing_section_raises():
    fac = GenericAdapterFactory()
    cfg = {
        "server": {},
        "unknown_section": {"foo": 1},
    }
    with pytest.raises(RuntimeError):
        fac.create_adapters_from_config(cfg)


def test_factory_legacy_fail_fast_disabled_no_raise():
    fac = GenericAdapterFactory()
    cfg = {
        "server": {"fail_fast_adapters": False},
        "unknown_section": {"foo": 1},
    }
    res = fac.create_adapters_from_config(cfg)
    assert isinstance(res, list) and len(res) == 0


def test_factory_register_external_plugin_and_create_instance():
    reg = PluginRegistry()
    fac = GenericAdapterFactory(registry=reg)
    plugin = AdapterPlugin("Dummy", "dummy_section", DummyAdapter)
    fac.register_external_plugin(plugin)
    cfg = {
        "server": {},
        "dummy_section": {"hello": "world"},
    }
    res = fac.create_adapters_from_config(cfg)
    assert len(res) == 1 and isinstance(res[0], DummyAdapter)