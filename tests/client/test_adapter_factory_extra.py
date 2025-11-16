"""Extra coverage for ClientAdapterFactory branches not exercised in core tests."""

import pytest

from openmux.client.adapters.factory import ClientAdapterFactory


def test_create_adapter_unknown_type():
    with pytest.raises(ValueError) as exc:
        ClientAdapterFactory.create_adapter("h", 1, adapter_type="nope")
    assert "Unknown adapter type" in str(exc.value)


def test_validate_config_unknown_type():
    with pytest.raises(ValueError) as exc:
        ClientAdapterFactory.validate_config("mystery")
    assert "Unknown adapter type" in str(exc.value)


def test_validate_config_valid_tcp():
    assert ClientAdapterFactory.validate_config("tcp", {"use_tls": False}) is True


# Force constructor failure path by injecting a bad class temporarily


def test_create_adapter_constructor_failure(monkeypatch):
    class BadAdapter:
        def __init__(self, *a, **k):  # noqa: ANN001
            raise RuntimeError("ctor boom")

    monkeypatch.setitem(ClientAdapterFactory.ADAPTER_TYPES, "bad", BadAdapter)
    try:
        with pytest.raises(ValueError) as exc:
            ClientAdapterFactory.create_adapter("h", 1, adapter_type="bad")
        assert "Failed to create bad adapter" in str(exc.value)
    finally:
        ClientAdapterFactory.ADAPTER_TYPES.pop("bad", None)
