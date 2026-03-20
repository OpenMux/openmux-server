

.PHONY: reload-soft reload-full
reload-soft:
	@if [ -f "$$OPENMUX_PIDFILE" ]; then pidfile="$$OPENMUX_PIDFILE"; elif [ -f logs/openmux.pid ]; then pidfile="logs/openmux.pid"; else echo "PID file not found (set OPENMUX_PIDFILE or ensure logs/openmux.pid exists)"; exit 1; fi; \
	pid=$$(cat $$pidfile); \
	echo "Sending SIGHUP to $$pid (pidfile=$$pidfile)"; \
	kill -HUP $$pid

reload-full:
	@if [ -f "$$OPENMUX_PIDFILE" ]; then pidfile="$$OPENMUX_PIDFILE"; elif [ -f logs/openmux.pid ]; then pidfile="logs/openmux.pid"; else echo "PID file not found (set OPENMUX_PIDFILE or ensure logs/openmux.pid exists)"; exit 1; fi; \
	pid=$$(cat $$pidfile); \
	echo "Sending SIGUSR1 to $$pid (pidfile=$$pidfile)"; \
	kill -USR1 $$pid
# ============================================================================
# OpenMux Project Makefile
# ============================================================================
# 
# This Makefile provides comprehensive build, test, package, and deployment
# automation for the OpenMux serial port management system.
#
# Quick Start:
#   make help          - Show all available targets
#   make test          - Run unit tests  
#   make package       - Create distribution packages
#   make install-user  - Install for current user
#   make deploy-remote - Deploy to remote system
#
# Requirements:
#   - Python 3.9+
#   - pip, venv
#   - SSH access to remote systems (for deployment)
#
# ============================================================================


.PHONY: test test-unit test-integration test-all test-coverage lint format format-check clean \
	venv venv-dev venv-destroy \
	build package install install-dev install-user uninstall \
	dist upload upload-test check \
	deploy-remote help vulture \
	validate-config validate-all-configs

# Project configuration
PROJECT_NAME = openmux
# Read version from pyproject.toml using sed; default to 1.0.0 if not found
VERSION = $(shell v=$$(sed -nE 's/^version\s*=\s*"([^"]+)"/\1/p' pyproject.toml | head -n1); \
        if [ -n "$$v" ]; then echo $$v; else echo 1.0.0; fi)
PACKAGE_NAME = $(PROJECT_NAME)-$(VERSION)

# Virtual environment paths
VENV_DIR = .venv
PYTHON = python3
PIP = $(VENV_DIR)/bin/pip
PYTHON_VENV = $(VENV_DIR)/bin/python

# Build and distribution directories
BUILD_DIR = build
DIST_DIR = dist
EGG_INFO = $(PROJECT_NAME).egg-info

# Remote deployment configuration
REMOTE_HOST ?= openconsole
REMOTE_USER ?= $(shell whoami)

# Colors for output
COLOR_GREEN = \033[0;32m
COLOR_YELLOW = \033[1;33m
COLOR_RED = \033[0;31m
COLOR_BLUE = \033[0;34m
COLOR_RESET = \033[0m

# Helper function to print colored output
define print_status
	@printf '%b %s\n' "$(COLOR_YELLOW)[INFO]$(COLOR_RESET)" $(1)
endef

define print_success
	@printf '%b %s\n' "$(COLOR_GREEN)[SUCCESS]$(COLOR_RESET)" $(1)
endef

define print_error
	@printf '%b %s\n' "$(COLOR_RED)[ERROR]$(COLOR_RESET)" $(1)
endef

# Default target: show help
help:
	@echo "$(COLOR_BLUE)OpenMux Project Makefile$(COLOR_RESET)"
	@echo "======================="
	@echo ""
	@echo "$(COLOR_GREEN)Development:$(COLOR_RESET)"
	@echo "  venv           Create virtual environment"
	@echo "  venv-dev       Create development virtual environment"
	@echo "  venv-destroy   Remove virtual environment"
	@echo ""
	@echo "$(COLOR_GREEN)Testing:$(COLOR_RESET)"
	@echo "  test           Run unit tests (default)"
	@echo "  test-unit      Run only unit tests"
	@echo "  test-integration Run only integration tests"
	@echo "  test-all       Run all tests including slow tests"
	@echo "  test-coverage  Run tests with coverage report"
	@echo "  lint           Run linters"
	@echo "  format         Run code formatters (black, isort, whitespace cleanup)"
	@echo "  format-check   Check if code formatting is needed (no changes)"
	@echo "  ci             Run all CI checks"
	@echo ""
	@echo "$(COLOR_GREEN)Building and Packaging:$(COLOR_RESET)"
	@echo "  build          Build the package"
	@echo "  package        Create distribution packages"
	@echo "  dist           Create source and wheel distributions"
	@echo "  check          Check package for common issues"
	@echo ""
	@echo "$(COLOR_GREEN)Installation:$(COLOR_RESET)"
	@echo "  install        Install package system-wide (requires sudo)"
	@echo "  install-user   Install package for current user"
	@echo "  install-dev    Install package in development mode"
	@echo "  uninstall      Uninstall package"
	@echo ""
	@echo "$(COLOR_GREEN)Deployment:$(COLOR_RESET)"
	@echo "  deploy-remote  Deploy to remote system (REMOTE_HOST=$(REMOTE_HOST))"
	@echo ""
	@echo "$(COLOR_GREEN)Utilities:$(COLOR_RESET)"
	@echo "  clean          Clean up temporary files"
	@echo "  run-server     Run OpenMux server"
	@echo "  run-client     Run OpenMux client"
	@echo "  run-management Run OpenMux management server"
	@echo "  vulture        Run dead code analysis (vulture)"
	@echo "  reload-soft    Send SIGHUP to running server"
	@echo "  reload-full    Send SIGUSR1 to running server"
	@echo "  validate-config Validate a single config (CONFIG=path/to/file.yaml)"
	@echo "  validate-all-configs Validate all configs in ./config against schema"
	@echo "  deb            Build a Debian package (.deb) using dh+pybuild"
	@echo ""

# Create a basic virtual environment
venv:
	@if [ ! -d "$(VENV_DIR)" ]; then \
		echo "Creating virtual environment..."; \
		$(PYTHON) -m venv $(VENV_DIR); \
		$(VENV_DIR)/bin/pip install -U pip setuptools wheel; \
	fi
	@echo "Installing package in development mode..."
	$(VENV_DIR)/bin/pip install -e . --config-settings editable_mode=strict

# Create a dev virtual environment with all dev dependencies
venv-dev: venv
	@echo "Installing development dependencies..."
	$(VENV_DIR)/bin/pip install -e .[dev] --config-settings editable_mode=strict
	$(VENV_DIR)/bin/pip install -r requirements-dev.txt

# Destroy the virtual environment
venv-destroy:
	@echo "Removing virtual environment..."
	rm -rf $(VENV_DIR)

# ============================================================================
# Building and Packaging Targets
# ============================================================================

# Build the package
build: venv
	$(call print_status,"Building package...")
	$(PIP) install --upgrade build
	$(PYTHON_VENV) -m build
	$(call print_success,"Package built successfully")

# Create distribution packages (both source and wheel)
package: clean venv
	$(call print_status,"Creating distribution packages...")
	$(PIP) install --upgrade build wheel
	$(PYTHON_VENV) -m build
	$(call print_success,"Distribution packages created in $(DIST_DIR)/")
	@ls -la $(DIST_DIR)/

# Alias for package
dist: package

# Check package for common issues
check: package
	$(call print_status,"Checking package integrity...")
	$(PIP) install --upgrade twine
	$(PYTHON_VENV) -m twine check $(DIST_DIR)/*
	$(call print_success,"Package check completed")

# ============================================================================
# Installation Targets  
# ============================================================================

# Install package system-wide (requires sudo)
install: package
	$(call print_status,"Installing $(PROJECT_NAME) system-wide...")
	sudo $(PYTHON) -m pip install $(DIST_DIR)/$(PROJECT_NAME)-$(VERSION)-py3-none-any.whl
	$(call print_success,"$(PROJECT_NAME) installed system-wide")
	@echo "$(COLOR_BLUE)Available commands:$(COLOR_RESET)"
	@echo "  openmux-server, openmux-client, openmux-mgmt, openmux-cli"

# Install package for current user only
install-user: package
	$(call print_status,"Installing $(PROJECT_NAME) for current user...")
	$(PYTHON) -m pip install --user $(DIST_DIR)/$(PROJECT_NAME)-$(VERSION)-py3-none-any.whl
	$(call print_success,"$(PROJECT_NAME) installed for user $(shell whoami)")
	@echo "$(COLOR_BLUE)Available commands:$(COLOR_RESET)"
	@echo "  openmux-server, openmux-client, openmux-mgmt, openmux-cli"

# Install package in development mode
install-dev: venv
	$(call print_status,"Installing $(PROJECT_NAME) in development mode...")
	$(PIP) install -e . --config-settings editable_mode=strict
	$(call print_success,"$(PROJECT_NAME) installed in development mode")

# Uninstall package
uninstall:
	$(call print_status,"Uninstalling $(PROJECT_NAME)...")
	$(PYTHON) -m pip uninstall -y $(PROJECT_NAME) || true
	$(PYTHON) -m pip uninstall --user -y $(PROJECT_NAME) || true
	$(call print_success,"$(PROJECT_NAME) uninstalled")

# ============================================================================
# Deployment Targets
# ============================================================================

# Deploy to remote system
deploy-remote:
	$(call print_status,"Deploying to $(REMOTE_USER)@$(REMOTE_HOST)...")
	./deploy_remote.sh
	$(call print_success,"Deployment to $(REMOTE_HOST) completed")

# ============================================================================
# Testing Targets
# ============================================================================

# Default test target: run unit tests
test: venv-dev
	$(call print_status,"Running unit tests...")
	$(PYTHON_VENV) -m pytest -v
	$(call print_success,"Unit tests completed")

# Run only unit tests
test-unit: venv-dev
	$(call print_status,"Running unit tests...")
	$(PYTHON_VENV) -m pytest -v -m "unit or not integration"
	$(call print_success,"Unit tests completed")

# Run only integration tests
test-integration: venv-dev
	$(call print_status,"Running integration tests...")
	$(PYTHON_VENV) -m pytest -v -m "integration"
	$(call print_success,"Integration tests completed")

# Run all tests including slow tests
test-all: venv-dev
	$(call print_status,"Running all tests...")
	$(PYTHON_VENV) -m pytest -v -m "not slow"
	$(call print_success,"All tests completed")

# Run tests with coverage
test-coverage: venv-dev
	$(call print_status,"Running tests with coverage...")
	$(PYTHON_VENV) -m pytest -v --cov=openmux --cov-report=term-missing --cov-report=html
	$(call print_success,"Coverage tests completed")

# Run linters
lint: venv-dev
	$(call print_status,"Running linters...")
	$(PYTHON_VENV) -m flake8 openmux/ --select=E9,F63,F7,F82
	$(PYTHON_VENV) -m flake8 openmux/ --exit-zero
	$(call print_success,"Linting completed")

# Run code formatters
format: venv-dev
	$(call print_status,"Running code formatters...")
	@echo "$(COLOR_BLUE)Formatting with black...$(COLOR_RESET)"
	$(PYTHON_VENV) -m black openmux/ tests/ scripts/ --line-length 127
	@echo "$(COLOR_BLUE)Organizing imports with isort...$(COLOR_RESET)"
	$(PYTHON_VENV) -m isort openmux/ tests/ scripts/ --profile black
	@echo "$(COLOR_BLUE)Removing trailing whitespace...$(COLOR_RESET)"
	@find openmux/ tests/ scripts/ -name "*.py" -exec sed -i '' 's/[[:space:]]*$$//' {} \; 2>/dev/null || true
	@echo "$(COLOR_BLUE)Checking for remaining whitespace issues...$(COLOR_RESET)"
	@$(PYTHON_VENV) -m flake8 openmux/ tests/ --select=W293,W291 --count || echo "$(COLOR_YELLOW)Note: Some whitespace issues may remain$(COLOR_RESET)"
	$(call print_success,"Code formatting completed")

# Check if code formatting is needed (without making changes)
format-check: venv-dev
	$(call print_status,"Checking code formatting...")
	@echo "$(COLOR_BLUE)Checking black formatting...$(COLOR_RESET)"
	@$(PYTHON_VENV) -m black openmux/ tests/ scripts/ --line-length 127 --check --diff || echo "$(COLOR_YELLOW)Files need black formatting$(COLOR_RESET)"
	@echo "$(COLOR_BLUE)Checking isort import organization...$(COLOR_RESET)"
	@$(PYTHON_VENV) -m isort openmux/ tests/ scripts/ --profile black --check-only --diff || echo "$(COLOR_YELLOW)Files need import reorganization$(COLOR_RESET)"
	@echo "$(COLOR_BLUE)Checking for whitespace issues...$(COLOR_RESET)"
	@$(PYTHON_VENV) -m flake8 openmux/ tests/ --select=W293,W291 --count || echo "$(COLOR_YELLOW)Whitespace issues found$(COLOR_RESET)"
	$(call print_success,"Format check completed")

# Run CI mode (all checks)
ci: venv-dev
	$(call print_status,"Running CI checks...")
	$(PYTHON_VENV) -m pytest -v --cov=openmux --cov-report=term-missing
	$(PYTHON_VENV) -m flake8 openmux/ tests/
	$(call print_success,"CI checks completed")
	$(call print_success,"CI checks completed")

# ==========================================================================
# Debian packaging
# ==========================================================================

.PHONY: deb
DEB_REVISION ?= 1
DEB_DIST ?= unstable
DEB_SNAPSHOT ?= off
deb: clean
	$(call print_status,"Building Debian package with dh+pybuild...")
	@if ! command -v dpkg-buildpackage >/dev/null 2>&1; then \
		echo "dpkg-buildpackage not found. Install dpkg-dev: sudo apt-get install dpkg-dev devscripts"; \
		exit 1; \
	fi
	@# Sync debian/changelog version from pyproject.toml
	DEB_REVISION=$(DEB_REVISION) DEB_DIST=$(DEB_DIST) DEB_SNAPSHOT=$(DEB_SNAPSHOT) \
		$(PYTHON) scripts/update_deb_changelog.py --package $(PROJECT_NAME) --message "Automated build"
	@# Build source package and binary without signing
	dpkg-buildpackage -us -uc -b
	$(call print_success,".deb built in parent directory (../)")

# Run dead code analysis with vulture
VULTURE_ARGS ?= --ignore-names "get_supported_types,get_plugin,get_registry,register_external_plugin,get_logger,authenticate_user,authenticate_key,get_key_permissions,generate_api_key,hash_password,get_server_host,get_server_port,get_serial_ports_config,is_web_server_enabled,get_port_config,save_config,list_consoles,promote_client_to_read_write,get_connection_info,handle_lifecycle_event"
vulture: venv-dev
	$(call print_status,"Running vulture dead code analysis...")
	@# Exclude whitelist helper file and any legacy 'old/' quarantined code
	@# Vulture exits non-zero when dead code is found; keep exit code for CI visibility
	$(PYTHON_VENV) -m vulture openmux/ --exclude vulture_whitelist.py,old $(VULTURE_ARGS)
	$(call print_success,"Vulture analysis completed")

# ============================================================================
# Utility Targets
# ============================================================================

# Clean up temporary files and build artifacts
clean:
	$(call print_status,"Cleaning up temporary files...")
	rm -rf $(BUILD_DIR) $(DIST_DIR) $(EGG_INFO)
	rm -rf .coverage htmlcov .pytest_cache coverage.xml
	rm -rf .tox .cache
	find . -name "*.pyc" -delete
	find . -name "__pycache__" -delete -type d
	find . -name "*.pyo" -delete
	find . -name "*.orig" -delete
	find . -name "*.rej" -delete
	find . -name ".DS_Store" -delete
	$(call print_success,"Cleanup completed")

# --------------------------------------------------------------------------
# Configuration Validation
# --------------------------------------------------------------------------

# Default config and schema paths (override with `make validate-config CONFIG=...`)
CONFIG ?= config/server.yaml
SCHEMA ?= docs/openmux_config_schema.yaml

validate-config: venv-dev
	$(call print_status,"Validating configuration $(CONFIG) against $(SCHEMA)...")
	$(PYTHON_VENV) scripts/validate_config.py --config $(CONFIG) --schema $(SCHEMA)
	$(call print_success,"Configuration $(CONFIG) is valid")

validate-all-configs: venv-dev
	$(call print_status,"Validating all configuration files in ./config against $(SCHEMA)...")
	set -e; \
	fail=0; \
	for f in config/*.yaml; do \
		echo "--- $$f"; \
		if ! $(PYTHON_VENV) scripts/validate_config.py --config "$$f" --schema $(SCHEMA); then \
			fail=1; \
		fi; \
	done; \
	if [ $$fail -ne 0 ]; then \
		echo "$(COLOR_RED)One or more configurations failed validation$(COLOR_RESET)"; \
		exit 1; \
	else \
		echo "$(COLOR_GREEN)All configurations passed validation$(COLOR_RESET)"; \
	fi

# ============================================================================
# Runtime Targets
# ============================================================================

# Run the OpenMux server
run-server: venv
	$(call print_status,"Starting OpenMux server...")
	$(PYTHON_VENV) -m openmux.server.main

# Run the OpenMux client
run-client: venv
	$(call print_status,"Starting OpenMux client...")
	$(PYTHON_VENV) -m openmux.client.main

# Run the OpenMux management server
run-management: venv
	$(call print_status,"Starting OpenMux management server...")
	$(PYTHON_VENV) -m openmux.management.main

# Run the OpenMux server with loopback test config
run-server-loopback: venv
	$(call print_status,"Starting OpenMux server with loopback configuration...")
	$(PYTHON_VENV) -m openmux.server.main --config ./config/loopback_test.yaml

# ============================================================================
# Development Targets
# ============================================================================

# Run a quick development test
dev-test: venv
	$(call print_status,"Running quick development test...")
	$(PYTHON_VENV) old/test_scripts/simple_echo_test.py

# Show project information
info:
	@echo "$(COLOR_BLUE)Project Information:$(COLOR_RESET)"
	@echo "  Name: $(PROJECT_NAME)"
	@echo "  Version: $(VERSION)"
	@echo "  Python: $(shell $(PYTHON) --version)"
	@echo "  Virtual Environment: $(VENV_DIR)"
	@echo "  Remote Host: $(REMOTE_HOST)"
	@echo "  Remote User: $(REMOTE_USER)"
