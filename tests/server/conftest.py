"""Pytest configuration for server tests."""

from pathlib import Path


def pytest_ignore_collect(collection_path: Path, config):
    """Ignore duplicate test module that conflicts with management/test_client_manager.py.

    Uses pathlib.Path as required by newer Pytest versions to avoid deprecation warnings.
    """
    try:
        name = collection_path.name
    except Exception:  # Fallback in case a different type is passed
        name = str(collection_path)
    return name == "test_client_manager.py"
