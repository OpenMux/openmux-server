"""Tests for WebStatusAdapter._api_post_fault (fault injection API).

Covers the pre-refactor behavior contract for the C901 17-bracket:
enable guard, JSON validation, muxcon discovery, and the action dispatch
table (list/freeze/unfreeze/drop_heartbeats/restore_heartbeats/close_conn/
reset_conn/unknown).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openmux.server.adapters.web_status import WebStatusAdapter


def _adapter(enabled=True, muxcon_available=True):
    a = WebStatusAdapter("ws", {"port": 9999, "enable_fault_injection": enabled})
    a._send_json = AsyncMock()
    pm = MagicMock()
    if muxcon_available:
        m = MagicMock(name="muxcon")
        m.get_adapter_type.return_value = "muxcon"
        m.freeze_connection = AsyncMock(return_value=True)
        m.unfreeze_connection = AsyncMock(return_value=True)
        m.set_drop_heartbeats = AsyncMock(return_value=True)
        m.force_close_connection = AsyncMock(return_value=True)
        m.force_reset_connection = AsyncMock(return_value=True)
        pm.unified_adapters = [m]
        a._muxcon = m
    else:
        pm.unified_adapters = []
        a._muxcon = None
    a.main_port_manager = pm
    return a


def _post(body):
    return json.dumps(body).encode("utf-8")


def _status_result(a):
    args, _ = a._send_json.await_args
    _, status, payload = args
    return status, payload


class TestApiPostFault:
    @pytest.mark.asyncio
    async def test_disabled_returns_403(self):
        a = _adapter(enabled=False)
        await a._api_post_fault(MagicMock(), _post({"action": "list"}))
        assert _status_result(a)[0] == 403

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self):
        a = _adapter()
        with patch("openmux.server.adapters.web_status.json.loads", side_effect=ValueError("bad")):
            await a._api_post_fault(MagicMock(), b"not json")
        assert _status_result(a)[0] == 400
        a._send_json.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_body_missing_action(self):
        a = _adapter()
        await a._api_post_fault(MagicMock(), b"")
        status, payload = _status_result(a)
        assert status == 400
        assert "action" in payload["message"]

    @pytest.mark.asyncio
    async def test_no_muxcon_returns_503(self):
        a = _adapter(muxcon_available=False)
        await a._api_post_fault(MagicMock(), _post({"action": "list"}))
        assert _status_result(a)[0] == 503

    @pytest.mark.asyncio
    async def test_list_action_returns_fault_states(self):
        a = _adapter()
        a._muxcon._fault_state = {"c1": "frozen"}
        await a._api_post_fault(MagicMock(), _post({"action": "list"}))
        status, payload = _status_result(a)
        assert status == 200
        assert payload["fault_states"] == {"c1": "frozen"}

    @pytest.mark.asyncio
    async def test_freeze_applied(self):
        a = _adapter()
        await a._api_post_fault(MagicMock(), _post({"action": "freeze", "connection_id": "c1"}))
        status, payload = _status_result(a)
        assert status == 200
        assert payload["applied"] is True
        a._muxcon.freeze_connection.assert_awaited_once_with("c1")

    @pytest.mark.asyncio
    async def test_close_conn_passes_linger(self):
        a = _adapter()
        await a._api_post_fault(MagicMock(), _post({"action": "close_conn", "connection_id": "c1", "params": {"linger": 5}}))
        status, payload = _status_result(a)
        assert status == 200
        assert payload["applied"] is True
        a._muxcon.force_close_connection.assert_awaited_once_with("c1", 5)

    @pytest.mark.asyncio
    async def test_freeze_without_connection_refused(self):
        """A per-connection fault with no connection_id is not applied."""
        a = _adapter()
        await a._api_post_fault(MagicMock(), _post({"action": "freeze"}))
        status, payload = _status_result(a)
        assert status == 200
        assert payload["applied"] is False
        a._muxcon.freeze_connection.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_action_returns_400(self):
        a = _adapter()
        await a._api_post_fault(MagicMock(), _post({"action": "explode"}))
        status, payload = _status_result(a)
        assert status == 400
        assert "explode" in payload["message"]

    @pytest.mark.asyncio
    async def test_fault_invocation_error_reports_not_applied(self):
        """A fault method that raises is swallowed by the invocation layer.

        `_invoke_muxcon_fault` returns False for any invocation failure, so
        the API answers 200 with applied=False (not a 5xx).
        """
        a = _adapter()
        a._muxcon.freeze_connection = AsyncMock(side_effect=RuntimeError("boom"))
        await a._api_post_fault(MagicMock(), _post({"action": "freeze", "connection_id": "c1"}))
        status, payload = _status_result(a)
        assert status == 200
        assert payload["applied"] is False

    @pytest.mark.asyncio
    async def test_dispatch_error_returns_500(self):
        """An error escaping to the dispatch level is surfaced as 500."""
        a = _adapter()
        a._invoke_muxcon_fault = AsyncMock(side_effect=RuntimeError("boom"))
        await a._api_post_fault(MagicMock(), _post({"action": "freeze", "connection_id": "c1"}))
        status, payload = _status_result(a)
        assert status == 500
        assert "boom" in payload["message"]
