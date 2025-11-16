import asyncio
import os
import types
import pytest

from openmux.server.main import OpenMuxServer


@pytest.mark.asyncio
async def test_soft_reload_method(monkeypatch, tmp_path):
    # Use sample config shipped with repo
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "config", "server.yaml")
    cfg_path = os.path.abspath(cfg_path)

    # Instantiate server without starting adapters
    server = OpenMuxServer(cfg_path, log_level="DEBUG")

    # Ensure unified_adapters list is empty to avoid any network side-effects
    server.unified_adapters = []

    # Monkeypatch auth_manager.update_config to be a no-op coroutine
    async def _noop_update(cfg):
        return True
    monkeypatch.setattr(server.auth_manager, "update_config", _noop_update, raising=True)

    # Call soft reload and check the shape of the result
    res = await server.reload_adapters_soft(context={"origin": "test"})
    assert isinstance(res, dict)
    assert "auth_updated" in res
    assert "adapters" in res


@pytest.mark.asyncio
async def test_full_reload_method(monkeypatch):
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "config", "server.yaml")
    cfg_path = os.path.abspath(cfg_path)

    server = OpenMuxServer(cfg_path, log_level="DEBUG")

    # Stub out adapter factory to avoid creating any real adapters
    # Monkeypatch factory method to avoid creating real adapters
    monkeypatch.setattr(server.unified_adapter_factory, "create_adapters_from_config", lambda cfg: [], raising=True)

    # Call full reload; it should succeed and report zero started
    res = await server.reload_adapters_full(context={"origin": "test"})
    assert isinstance(res, dict)
    assert "stopped" in res
    assert "started" in res
    assert "errors" in res

