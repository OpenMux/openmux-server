"""Behavioral coverage for OpenMuxClient.list_ports (35-bracket S1).

Drives the real list_ports method against a fake authenticated adapter and
captures every print_client_info line so the exact rendered output is pinned.
These tests are written (and green) against the PRE-refactor method so the
decomposition below can be verified as behavior-identical.

The method uses no instance state, so a bare OpenMuxClient instance
(object.__new__) is sufficient.
"""

import asyncio

import openmux.client.main as cli_main
from openmux.client.main import OpenMuxClient

_MISSING = object()


class _FakeConn:
    """Minimal stand-in for an authenticated client adapter."""

    def __init__(self, ports, host="h", port=1234, lpm=_MISSING, lsi=_MISSING):
        self._ports = ports
        self.host = host
        self.port = port
        if lpm is not _MISSING:
            self.last_port_metadata = lpm
        if lsi is not _MISSING:
            self.last_server_info = lsi

    async def list_ports(self):
        return self._ports


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _client():
    return OpenMuxClient.__new__(OpenMuxClient)


def _patch_print(monkeypatch):
    seen = []

    def fake(msg, level="INFO"):
        seen.append((msg, level))

    monkeypatch.setattr(cli_main, "print_client_info", fake)
    return seen


def _msgs(seen):
    return [m for m, _lvl in seen]


def _call(monkeypatch, conn):
    seen = _patch_print(monkeypatch)
    result = _run(_client().list_ports(conn))
    return result, _msgs(seen), seen


def test_empty_ports_warns_and_returns_list(monkeypatch):
    result, msgs, seen = _call(monkeypatch, _FakeConn([]))
    assert result == []
    assert msgs == ["No ports available"]
    assert seen[0][1] == "WARNING"


def test_dict_entries_minimal_plain_header(monkeypatch):
    result, msgs, _seen = _call(monkeypatch, _FakeConn([{"name": "a"}]))
    assert result == [{"name": "a"}]
    assert msgs == ["Ports on h:1234:", "  1. a  - Disconnected"]


def test_full_entry_all_fields(monkeypatch):
    entry = {
        "name": "p",
        "id": "sid::p",
        "description": "d",
        "device": "dev0",
        "connected": True,
        "client_count": 2,
        "max_read_write_users": 5,
        "origin_server_hostname": "oh",
        "origin_server_id": "oid",
        "origin_server": {"description": "od", "port": 9999},
        "federation_type": "muxcon",
    }
    result, msgs, _seen = _call(monkeypatch, _FakeConn([entry]))
    assert result == [entry]
    assert msgs == [
        "Ports on h:1234:",
        "  1. p  (sid::p) - d  - dev0, Connected, 2 clients/5 rw, "
        "origin=[id=oid, desc=od, hostname=oh, port=9999] (muxcon)",
    ]


def test_origin_remote_fallback_remote_connection_id(monkeypatch):
    entry = {
        "name": "r",
        "origin_server_hostname": "remote",
        "remote_connection_id": "node:abc",
        "origin_server_id": "OID",
    }
    _result, msgs, _seen = _call(monkeypatch, _FakeConn([entry]))
    assert msgs == [
        "Ports on h:1234:",
        "  1. r  - Disconnected, origin=[id=OID, hostname=node:abc]",
    ]


def test_origin_remote_fallback_server_chain(monkeypatch):
    entry = {
        "name": "s",
        "origin_server_hostname": "remote",
        "server_chain": ["c1", "c2"],
        "origin_server": {"server_id": "OSID", "port": 88},
    }
    _result, msgs, _seen = _call(monkeypatch, _FakeConn([entry]))
    assert msgs == [
        "Ports on h:1234:",
        "  1. s  - Disconnected, origin=[id=c1, hostname=remote, port=88]",
    ]


def test_server_header_label_and_id(monkeypatch):
    conn = _FakeConn([{"name": "a"}], lsi={"description": "D", "id": "S1"})
    _result, msgs, _seen = _call(monkeypatch, conn)
    assert msgs[0] == "Ports on h:1234 (D, server_id=S1):"


def test_server_header_label_only(monkeypatch):
    conn = _FakeConn([{"name": "a"}], lsi={"description": "D"})
    _result, msgs, _seen = _call(monkeypatch, conn)
    assert msgs[0] == "Ports on h:1234 (D):"


def test_server_header_non_dict_plain(monkeypatch):
    conn = _FakeConn([{"name": "a"}], lsi="weird")
    _result, msgs, _seen = _call(monkeypatch, conn)
    assert msgs[0] == "Ports on h:1234:"


def test_plain_names_fallback(monkeypatch):
    result, msgs, _seen = _call(monkeypatch, _FakeConn(["x", "y"]))
    assert result == ["x", "y"]
    assert msgs == ["Ports on h:1234:", "  1. x", "  2. y"]


def test_last_port_metadata_list_used(monkeypatch):
    conn = _FakeConn(["x"], lpm=[{"name": "m0"}, {"name": "m1"}])
    result, msgs, _seen = _call(monkeypatch, conn)
    # raw ports returned, but listing rendered from the metadata list
    assert result == ["x"]
    assert msgs == ["Ports on h:1234:", "  1. m0  - Disconnected", "  2. m1  - Disconnected"]


def test_last_port_metadata_not_list_ignored(monkeypatch):
    conn = _FakeConn(["x"], lpm={"name": "m"})
    result, msgs, _seen = _call(monkeypatch, conn)
    assert result == ["x"]
    assert msgs == ["Ports on h:1234:", "  1. x"]


def test_connected_via_is_running_and_connected_clients(monkeypatch):
    entry = {"name": "p", "is_running": True, "connected_clients": 3}
    _result, msgs, _seen = _call(monkeypatch, _FakeConn([entry]))
    assert msgs == ["Ports on h:1234:", "  1. p  - Connected, 3 clients"]


def test_name_from_port_key_and_origin_from_object_only(monkeypatch):
    entry = {"port": "onlyport", "origin_server": {"hostname": "oh2", "server_id": "oi2"}}
    _result, msgs, _seen = _call(monkeypatch, _FakeConn([entry]))
    assert msgs == ["Ports on h:1234:", "  1. onlyport  - Disconnected, origin=[id=oi2, hostname=oh2]"]


def test_dup_names_hint_limits_examples_to_three(monkeypatch):
    ports = [
        {"name": "dup", "id": "a::1"},
        {"name": "dup", "id": "a::2"},
        {"name": "dup", "id": "a::3"},
        {"name": "dup", "id": "a::4"},
    ]
    _result, msgs, _seen = _call(monkeypatch, _FakeConn(ports))
    # every entry is rendered, then the hint with at most 3 ids
    hint = msgs[-1]
    assert hint.startswith("Duplicate port names detected")
    assert "a::1, a::2, a::3" in hint
    assert "a::4" not in hint


def test_dup_names_without_ids_no_hint(monkeypatch):
    _result, msgs, _seen = _call(monkeypatch, _FakeConn([{"name": "dup"}, {"name": "dup"}]))
    assert not any(m.startswith("Duplicate") for m in msgs)


def test_unknown_name(monkeypatch):
    _result, msgs, _seen = _call(monkeypatch, _FakeConn([{"description": "d"}]))
    assert msgs == ["Ports on h:1234:", "  1. <unknown> - d  - Disconnected"]


def test_origin_flat_port(monkeypatch):
    entry = {"name": "p", "origin_server_port": 77, "origin_server_hostname": "oh"}
    _result, msgs, _seen = _call(monkeypatch, _FakeConn([entry]))
    assert msgs == ["Ports on h:1234:", "  1. p  - Disconnected, origin=[hostname=oh, port=77]"]


class _RaisingGet(dict):
    """dict whose .get raises exactly on the Nth cumulative call."""

    calls = 0
    threshold = None

    def get(self, key, default=None):
        type(self).calls += 1
        if type(self).calls == type(self).threshold:
            raise RuntimeError("boom")
        return super().get(key, default)


class _LpmBoomConn(_FakeConn):
    """last_port_metadata property raises a non-AttributeError."""

    @property
    def last_port_metadata(self):
        raise RuntimeError("boom")


def test_meta_discovery_except_falls_back_to_plain(monkeypatch):
    _result, msgs, _seen = _call(monkeypatch, _LpmBoomConn(["x"]))
    assert msgs == ["Ports on h:1234:", "  1. x"]


def test_dup_count_except_skips_hint(monkeypatch):
    _RaisingGet.calls = 0
    _RaisingGet.threshold = 1
    ports = [_RaisingGet({"name": "dup"}), _RaisingGet({"name": "dup"})]
    _result, msgs, _seen = _call(monkeypatch, _FakeConn(ports))
    assert msgs == ["Ports on h:1234:", "  1. dup  - Disconnected", "  2. dup  - Disconnected"]


def test_dup_examples_except_skips_hint(monkeypatch):
    # The count pass makes 2 .get calls and the main loop 18 per entry
    # (36 for two), so the first .get in the examples pass is call 39.
    _RaisingGet.calls = 0
    _RaisingGet.threshold = 39
    ports = [_RaisingGet({"name": "dup"}), _RaisingGet({"name": "dup"})]
    _result, msgs, _seen = _call(monkeypatch, _FakeConn(ports))
    assert msgs == ["Ports on h:1234:", "  1. dup  - Disconnected", "  2. dup  - Disconnected"]
