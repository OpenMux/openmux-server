import asyncio
import os
import textwrap
import types

import pytest

from openmux.server.main import OpenMuxServer


def _write_isolated_config(tmp_path) -> str:
    """Write a minimal, self-contained server config for reload tests.

    IMPORTANT: this must NEVER be the repo's real `config/server.yaml`. That file
    contains a `tcp_initiator_ports` entry (`remote_loopback1`) that eagerly
    connects out to 127.0.0.1:8023 as a real client (auto_reconnect, never idle
    -disconnect). If a test clears `server.unified_adapters` and then calls
    `reload_adapters_soft()`, its "bootstrap missing adapter types" logic will
    create and start a REAL adapter for every non-empty section in the loaded
    config - which, against the real config, means a real outbound connection
    to whatever is listening on 127.0.0.1:8023 (e.g. a developer's live server)
    using the real admin credentials. Keeping this config empty of adapter
    sections keeps reload_adapters_soft/full fully inert.
    """
    auth_path = tmp_path / "authentication.yaml"
    auth_path.write_text(
        textwrap.dedent(
            """
            users:
              - username: admin
                password_hash: 5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8
                permissions: admin
            """
        )
    )
    cfg_path = tmp_path / "server.yaml"
    cfg_path.write_text(
        textwrap.dedent(
            """
            server:
              id: test
            logging:
              level: WARNING
            """
        )
    )
    return str(cfg_path)


@pytest.mark.asyncio
async def test_soft_reload_method(monkeypatch, tmp_path):
    # Use an isolated, adapter-free config (never the real config/server.yaml; see
    # _write_isolated_config for why that matters).
    cfg_path = _write_isolated_config(tmp_path)

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
async def test_full_reload_method(monkeypatch, tmp_path):
    # Use an isolated, adapter-free config (never the real config/server.yaml; see
    # _write_isolated_config for why that matters).
    cfg_path = _write_isolated_config(tmp_path)

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
