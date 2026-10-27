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

No test carries the `slow` marker today, so this option changes nothing yet. `pytest.ini` declares it
for tests that need it later.

## Two tiers

| Tier | What it runs | How to run it |
| --- | --- | --- |
| In-process tests | Everything in `tests/` except `tests/e2e`. These build objects, call methods, and use fakes. No server process starts. | `make test` or `pytest` |
| End-to-end tests | `tests/e2e`. These start the real server as a child process and drive the web console, the client listener protocol, Telnet, and the HTTP API. | `make test-e2e` |

The end-to-end tier is off unless you pass `--run-e2e`, so `make test` collects and runs only the
in-process tests. See [`e2e/README.md`](e2e/README.md) for that tier: how to run one scenario, what a
failure bundle holds, and how to add a scenario.

## Test Structure

The tests are organized as follows:

- `tests/test_*.py`: Tests for the server, the client, and the shared code
- `tests/server/`, `tests/client/`: Tests grouped by component
- `tests/support/`: Helpers that tests import
- `tests/e2e/`: The end-to-end tier, deselected unless you pass `--run-e2e`

## Adding Tests

When adding new tests, please follow these guidelines:

1. Use a marker that exists in `pytest.ini` and that a test actually needs:
   - `@pytest.mark.unit` for a fast in-process test of one function or class
   - `@pytest.mark.feature` for a test of a feature that is not yet implemented
   - `@pytest.mark.e2e`, `@pytest.mark.e2e_browser`, and `@pytest.mark.e2e_surfaces` for
     `tests/e2e`, described in [`e2e/README.md`](e2e/README.md)
   - `@pytest.mark.slow` for a test that must stay out of the default run
   - `@pytest.mark.integration`, `@pytest.mark.server`, `@pytest.mark.client`, and
     `@pytest.mark.management` are declared but unused. Do not add new tests that only carry one of
     those: prefer a file name and a directory that say the same thing.

2. Use the provided fixtures from `conftest.py` when possible

3. Mock external dependencies appropriately

4. Write descriptive docstrings for each test function

## Test Coverage

The goal is to achieve high test coverage for all components of the OpenMux project. To check the current coverage:

```bash
pytest --cov=openmux --cov-report=html
```

This will generate an HTML coverage report in the `htmlcov` directory.
