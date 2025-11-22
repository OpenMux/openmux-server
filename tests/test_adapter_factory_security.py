import pytest

from openmux.server.adapters.base_adapter import AdapterCapability, BaseGenericAdapter
from openmux.server.adapters.factory import AdapterPlugin, GenericAdapterFactory, PluginRegistry
from openmux.server.security_policy import SecurityPolicy


class DummyAdapter(BaseGenericAdapter):
    adapter_type = "dummy"

    def get_capabilities(self):
        return {AdapterCapability.PROVIDES_PORTS}

    def get_adapter_type(self) -> str:
        return "dummy"

    async def start(self):  # pragma: no cover - start isn't exercised in tests
        self.is_running = True
        return True

    async def stop(self):  # pragma: no cover - stop isn't exercised in tests
        self.is_running = False

    @classmethod
    def validate_config(cls, config):
        return True

    async def create_port(self, port_name, config):  # pragma: no cover - not used here
        return object()

    async def destroy_port(self, port_name):  # pragma: no cover - not used here
        return None

    def get_port_configurations(self):
        return {}


@pytest.fixture
def dummy_registry():
    registry = PluginRegistry()
    registry.register_plugin(AdapterPlugin("Dummy", "dummy_section", DummyAdapter))
    return registry


def _policy(allowed_types):
    module_name = DummyAdapter.__module__
    return SecurityPolicy.from_mapping(
        {
            "adapters": {
                "allowed_modules": [module_name],
                "allowed_adapter_types": allowed_types,
            }
        }
    )


def test_factory_creates_adapter_when_policy_allows(dummy_registry):
    policy = _policy(["dummy"])
    factory = GenericAdapterFactory(dummy_registry, security_policy=policy)
    config = {"server": {}, "authentication": {}, "dummy_section": []}

    adapters = factory.create_adapters_from_config(config)

    assert len(adapters) == 1
    assert isinstance(adapters[0], DummyAdapter)


def test_factory_blocks_adapter_when_policy_denies_type(dummy_registry):
    policy = _policy(["other"])
    factory = GenericAdapterFactory(dummy_registry, security_policy=policy)
    config = {"server": {}, "authentication": {}, "dummy_section": []}

    adapters = factory.create_adapters_from_config(config)

    assert adapters == []
