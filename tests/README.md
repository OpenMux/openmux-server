# OpenMux Tests

This directory contains the tests for the OpenMux project. The tests are organized by component and functionality.

## Requirements

To run the tests, you need to install the development dependencies:

```bash
pip install -r requirements-dev.txt
```

## Running Tests

To run all tests:

```bash
pytest
```

To run tests with coverage:

```bash
pytest --cov=openmux
```

To run only unit tests:

```bash
pytest -m unit
```

To run tests for a specific component:

```bash
pytest -m server  # Server tests
pytest -m client  # Client tests
```

To run slow tests (disabled by default):

```bash
pytest --run-slow
```

## Test Structure

The tests are organized as follows:

- `test_server.py`: Tests for the server components
- `test_client.py`: Tests for the client components
- `test_missing_features.py`: Tests for features marked as NOT IMPLEMENTED

## Adding Tests

When adding new tests, please follow these guidelines:

1. Use the appropriate marker for your test:
   - `@pytest.mark.unit` for unit tests
   - `@pytest.mark.integration` for integration tests
   - `@pytest.mark.server` or `@pytest.mark.client` for component-specific tests
   - `@pytest.mark.slow` for tests that take a long time to run
   - `@pytest.mark.feature` for tests of features that are not yet implemented

2. Use the provided fixtures from `conftest.py` when possible

3. Mock external dependencies appropriately

4. Write descriptive docstrings for each test function

## Test Coverage

The goal is to achieve high test coverage for all components of the OpenMux project. To check the current coverage:

```bash
pytest --cov=openmux --cov-report=html
```

This will generate an HTML coverage report in the `htmlcov` directory.
