import asyncio

import pytest

from openmux.server.adapters.base_adapter import AdapterCapability, BaseGenericAdapter
from openmux.server.adapters.lifecycle import DynamicPortManager, PortState
from openmux.server.adapters.loopback import LoopbackPort


class _MiniLoopbackAdapter(BaseGenericAdapter):
    def get_capabilities(self):
        return {AdapterCapability.PROVIDES_PORTS}

    async def start(self) -> bool:
        self.is_running = True
        self.port_manager = DynamicPortManager(self)
        await self.load_configured_ports()
        return True

    async def stop(self) -> None:
        self.is_running = False

    @classmethod
    def validate_config(cls, config):
        return True

    async def create_port(self, port_name: str, config):
        p = LoopbackPort(port_name, config, self)  # type: ignore[arg-type]
        ok = await p.start()
        return p if ok else None

    async def destroy_port(self, port_name: str):
        port = self.port_manager.active_ports.get(port_name)
        if port:
            await port.stop()

    def get_port_configurations(self):
        return {"p1": {}}


@pytest.mark.asyncio
async def test_loopback_write_data_added():
    adapter = _MiniLoopbackAdapter("lb", {})
    await adapter.start()
    port = adapter.port_manager.active_ports["p1"]
    # Port should have write_data now
    assert hasattr(port, "write_data")
    wrote = await port.write_data(b"hello")
    assert isinstance(wrote, int) and wrote == 5


@pytest.mark.asyncio
async def test_is_port_ready_true_for_loopback():
    adapter = _MiniLoopbackAdapter("lb", {})
    await adapter.start()
    assert adapter.is_port_ready("p1") is True


@pytest.mark.asyncio
async def test_status_pending_connect_zero_for_loopback():
    adapter = _MiniLoopbackAdapter("lb", {})
    await adapter.start()
    status = adapter.get_status_info()
    assert status.get("pending_connect") == 0
