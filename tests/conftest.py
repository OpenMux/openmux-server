"""
Shared pytest fixtures and configuration for all OpenMux tests
"""

import asyncio
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add the project root to the Python path so we can import openmux
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Verify the package can be imported
try:
    import openmux
except ImportError as e:
    print(f"ERROR: Failed to import openmux package: {e}")
    print(f"Python path: {sys.path}")
    raise

# Disable logging during tests
logging.basicConfig(level=logging.CRITICAL)

# Enable asyncio debugging
os.environ["PYTHONASYNCIODBG"] = "1"


# Optional: announce current running test to stderr to help identify hangs.
# Enable by setting environment variable PYTEST_ANNOUNCE_TEST=1.
_ANNOUNCE = os.environ.get("PYTEST_ANNOUNCE_TEST", "0") == "1"


def pytest_runtest_logstart(nodeid, location):  # type: ignore[override]
    if _ANNOUNCE:
        sys.stderr.write(f"START {nodeid}\n")
        sys.stderr.flush()


def pytest_runtest_logfinish(nodeid, location):  # type: ignore[override]
    if _ANNOUNCE:
        sys.stderr.write(f"END   {nodeid}\n")
        sys.stderr.flush()


# Define pytest markers
def pytest_configure(config):
    """Configure pytest markers"""
    config.addinivalue_line("markers", "unit: mark test as a unit test")
    config.addinivalue_line("markers", "integration: mark test as an integration test")
    config.addinivalue_line("markers", "server: mark test as server-related")
    config.addinivalue_line("markers", "client: mark test as client-related")
    config.addinivalue_line("markers", "management: mark test as management-related")
    config.addinivalue_line("markers", "slow: mark test as slow (skipped by default)")
    config.addinivalue_line(
        "markers",
        "feature: mark test as testing a feature (usually unimplemented)",
    )


# Skip slow tests by default
def pytest_addoption(parser):
    parser.addoption("--run-slow", action="store_true", default=False, help="run slow tests")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-slow"):
        skip_slow = pytest.mark.skip(reason="need --run-slow option to run")
        for item in items:
            if "slow" in item.keywords:
                item.add_marker(skip_slow)


# Common fixtures
@pytest.fixture
def event_loop():
    """Create an instance of the default event loop for each test"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    # Make this loop current so any code calling get_event_loop / create_task
    # outside of a running task attaches to this per-test loop instead of the
    # global default loop (prevents lingering tasks at session end).
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        finally:
            try:
                asyncio.set_event_loop(None)
            except Exception:
                pass
            loop.close()


@pytest.fixture
def mock_serial_port():
    """Mock serial port for testing"""
    mock_port = AsyncMock()
    mock_port.name = "ttyS0"
    mock_port.is_connected = True
    mock_port.read = AsyncMock(return_value=b"test data")
    mock_port.write = AsyncMock(return_value=None)
    mock_port.close = AsyncMock(return_value=None)
    return mock_port


@pytest.fixture
def mock_reader():
    """Mock asyncio StreamReader for testing"""
    reader = AsyncMock()
    reader.read = AsyncMock(return_value=b"test data")
    reader.readline = AsyncMock(return_value=b"test line\n")
    reader.readuntil = AsyncMock(return_value=b"test data\n")
    reader.readexactly = AsyncMock(return_value=b"test")
    reader.at_eof = MagicMock(return_value=False)
    return reader


@pytest.fixture
def mock_writer():
    """Mock asyncio StreamWriter for testing"""
    writer = AsyncMock()
    writer.write = AsyncMock()
    writer.drain = AsyncMock()
    writer.close = AsyncMock()
    writer.wait_closed = AsyncMock()
    writer.get_extra_info = MagicMock(return_value=("127.0.0.1", 12345))
    return writer


@pytest.fixture
def yaml_config_file(tmp_path):
    """Create a temporary YAML config file for testing"""
    import yaml

    config_data = {
        "server": {"host": "127.0.0.1", "port": 8023},
        "authentication": {
            "users": [
                {
                    "username": "admin",
                    "password_hash": "hash",
                    "permissions": "admin",
                },
                {
                    "username": "user",
                    "password_hash": "hash",
                    "permissions": "user",
                },
            ],
            "api_keys": [{"name": "test", "key": "testkey", "permissions": "read-only"}],
        },
        "serial_ports": [
            {
                "name": "console1",
                "description": "Test Console",
                "device": "/dev/ttyS0",
                "baudrate": 9600,
                "bytesize": 8,
                "parity": "N",
                "stopbits": 1,
            }
        ],
    }

    config_file = tmp_path / "config.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config_data, f)

    return str(config_file)


# Session-level cleanup to avoid 'Task was destroyed but it is pending!' warnings
@pytest.fixture(scope="session", autouse=True)
def cleanup_pending_tasks_session():
    """After the entire test session, cancel and await any lingering asyncio Tasks.

    Some adapter/background connection manager tasks (e.g., OpenMuxClientPort
    connection monitor) may still be pending at interpreter shutdown causing noisy
    warnings. This fixture runs after all tests and tries to shut them down cleanly.
    """
    yield
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    if loop.is_closed():
        return

    pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
    if not pending:
        return

    for task in pending:
        task.cancel()
    # Wait briefly for cancellation
    try:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        # Allow callbacks/futures a final iteration
        loop.run_until_complete(asyncio.sleep(0))
    except Exception:
        pass
