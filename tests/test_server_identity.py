"""Shared identity resolver tests (ticket #74).

``server.id`` is the sole identity key. The resolver ignores the removed
``name`` / ``server_id`` keys (callers fall back to the hostname, which the
callers supply), and the ConfigManager deprecation shim detects and strips
those keys from a loaded config (shim tests live in tests/test_locations.py).
"""

import socket

from openmux.common.identity import get_server_id, get_server_label

FAKE_HOST = "fake-host-74"
FAKE_HOSTNAME = "fallback-host-74"


def test_get_server_id_uses_id():
    assert get_server_id({"id": "node-1"}) == "node-1"
    # Whitespace is stripped; blank values are treated as unset.
    assert get_server_id({"id": "  node-1  "}) == "node-1"
    assert get_server_id({"id": "   "}) is None
    assert get_server_id({"id": ""}) is None
    # Non-string values are coerced to str (a YAML int id still works).
    assert get_server_id({"id": 7}) == "7"


def test_get_server_id_ignores_removed_keys():
    # The removed fallback keys are not identity (ticket #74); None means the
    # caller falls back to the system hostname.
    assert get_server_id({"name": "leaf-1"}) is None
    assert get_server_id({"server_id": "leaf-1"}) is None
    assert get_server_id({"name": "a", "server_id": "b"}) is None


def test_get_server_id_id_wins_over_removed_keys():
    # A config the deprecation shim has NOT yet stripped: server.id wins and
    # the removed keys are ignored, so the identity is unambiguous.
    assert get_server_id({"id": "id1", "name": "n1", "server_id": "s1"}) == "id1"


def test_get_server_id_bad_inputs():
    assert get_server_id(None) is None
    assert get_server_id("not-a-dict") is None
    assert get_server_id({"id": None}) is None


def test_get_server_label_uses_description_when_set():
    assert get_server_label({"description": "Rack A hub"}) == "Rack A hub"
    assert get_server_label({"description": "Rack A hub", "id": "x"}) == "Rack A hub"


def test_get_server_label_falls_back_to_id():
    assert get_server_label({"id": "node-1"}) == "OpenMux node-1"


def test_get_server_label_falls_back_to_hostname(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: FAKE_HOST)
    # No id: the hostname. The removed keys do not change the result.
    assert get_server_label({}) == f"OpenMux {FAKE_HOST}"
    assert get_server_label({"name": "n", "server_id": "s"}) == f"OpenMux {FAKE_HOST}"
    # An explicit hostname argument is used over the socket lookup.
    assert get_server_label({}, hostname=FAKE_HOSTNAME) == f"OpenMux {FAKE_HOSTNAME}"


def test_get_server_label_never_empty(monkeypatch):
    def boom():
        raise OSError("hostname unavailable")

    monkeypatch.setattr(socket, "gethostname", boom)
    # Last resort: a non-empty placeholder keeps the label usable.
    assert get_server_label({}) == "OpenMux server"
