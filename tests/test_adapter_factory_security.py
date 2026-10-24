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

    async def _do_start(self):  # pragma: no cover - start isn't exercised in tests
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
    # Built directly (not via from_mapping) since "dummy" is a test-only
    # adapter type that isn't in the real, fixed known-adapter-type universe
    # that from_mapping() validates against.
    return SecurityPolicy(allowed_adapter_types=set(allowed_types))


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


class _RejectingAdapter(DummyAdapter):
    """Adapter whose section validation always fails."""

    adapter_type = "rejecting"

    @classmethod
    def validate_config(cls, config):
        return False


def test_factory_logs_one_failure_line_for_a_rejected_section(dummy_registry, caplog):
    """Issue #96: a rejected section reports once, so the adapter's reason line stands out."""
    registry = PluginRegistry()
    registry.register_plugin(AdapterPlugin("Rejecting", "rejecting_section", _RejectingAdapter))
    factory = GenericAdapterFactory(registry)
    with caplog.at_level("INFO", logger="openmux.server.adapters.factory"):
        caplog.clear()
        assert factory.create_adapters_from_config({"server": {}, "rejecting_section": []}) == []
    lines = [r.getMessage() for r in caplog.records if r.name == "openmux.server.adapters.factory"]
    assert sum(1 for line in lines if "Invalid configuration for Rejecting" in line) == 1
