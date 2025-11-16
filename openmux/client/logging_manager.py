"""Logging utilities for the OpenMux client.

Provides a configurable logging manager that can direct output to stdout,
rotating log files, or both. Configuration is controlled by CLI and optional
config file entries; environment variables are not used for behavior here.
"""

import logging
import logging.handlers
import os
import sys
from typing import Any, Dict, Optional


class ClientLoggingManager:
    """Configure and retrieve logging facilities for client components.

    Supports console-only, file-only, or combined output targets. File logging
    can optionally use a size-based rotating handler. Configuration is provided
    via the explicit dict passed to the constructor; internal defaults apply
    when keys are omitted.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Create a new logging manager instance.

        Args:
            config: Optional dictionary overriding logging behavior. Supported
                keys include ``log_level``, ``file_only``, ``file_logging_enabled``,
                ``log_dir``, ``log_file``, ``log_max_size_mb`` and ``log_backups``.
        """
        self.config = config or {}

        # Set up root logger
        self._setup_root_logger()

    def _setup_root_logger(self):
        """Apply configuration to the process root logger.

        Determines base log level, attaches stream and/or file handlers, and
        configures rotation parameters if requested. Idempotent in that it
        resets existing handlers to prevent duplication when re-run.
        """
        # Level from config dict only (CLI/config controls); default WARNING
        log_level_name = self.config.get("log_level", "WARNING")
        log_level = getattr(logging, str(log_level_name).upper(), logging.WARNING)

        # Configure root logger and ensure level propagates universally
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)
        # Ensure child loggers inherit
        root_logger.propagate = True

        # Reset existing handlers to avoid duplicates when reconfigured
        root_logger.handlers = []

        # Determine file-only mode from config
        file_only = bool(self.config.get("file_only", False))

        if not file_only:
            # Create console handler for general logs (honors root level)
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(logging.NOTSET)
            console_formatter = logging.Formatter("%(levelname)s: %(message)s")
            console_handler.setFormatter(console_formatter)
            root_logger.addHandler(console_handler)

            # Create a dedicated UI logger that always prints to stdout
            # regardless of root log level, so CLI output (e.g., -l listings)
            # remains visible without requiring -v.
            ui_logger = logging.getLogger("openmux.client.ui")
            ui_logger.setLevel(logging.INFO)
            ui_logger.propagate = False
            # Replace any existing handlers to prevent duplicates on reconfig
            ui_logger.handlers = []
            ui_console = logging.StreamHandler(sys.stdout)
            ui_console.setLevel(logging.NOTSET)
            # UI output should be clean; omit log level prefix
            ui_console.setFormatter(logging.Formatter("%(message)s"))
            ui_logger.addHandler(ui_console)

        # Create log directory if it doesn't exist and file logging is enabled
        if self.config.get("file_logging_enabled", False) or file_only:
            log_dir = self.config.get("log_dir", "logs")
            os.makedirs(log_dir, exist_ok=True)

            # Determine log file name
            log_file_name = self.config.get("log_file", "openmux_client.log")
            main_log_file = os.path.join(log_dir, log_file_name)

            # Check if we should use rotating file handler
            max_size_mb = int(self.config.get("log_max_size_mb", 10))
            backup_count = int(self.config.get("log_backups", 5))

            if max_size_mb > 0 and backup_count > 0:
                # Use rotating file handler
                file_handler = logging.handlers.RotatingFileHandler(
                    main_log_file, maxBytes=max_size_mb * 1024 * 1024, backupCount=backup_count
                )
            else:
                # Use regular file handler
                file_handler = logging.FileHandler(main_log_file)

            # Use more detailed formatter for file logs
            file_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
            file_handler.setFormatter(file_formatter)
            # Keep handler permissive to avoid suppressing DEBUG
            file_handler.setLevel(logging.NOTSET)
            root_logger.addHandler(file_handler)

        # Apply chosen level to all current openmux.* loggers and their handlers
        try:
            for name in list(logging.root.manager.loggerDict.keys()):
                if isinstance(name, str) and (name == "openmux" or name.startswith("openmux.")):
                    # Do not override the dedicated UI logger configuration
                    if name == "openmux.client.ui":
                        continue
                    lg = logging.getLogger(name)
                    lg.setLevel(log_level)
                    lg.propagate = True
                    for h in list(lg.handlers):
                        h.setLevel(logging.NOTSET)
        except Exception:
            # Defensive: logging reconfiguration should never crash the app
            pass

    def get_logger(self, name):
        """Return (and create if necessary) a named logger.

        Args:
            name: Logger name (usually a dotted module path).

        Returns:
            logging.Logger: Configured logger instance.
        """
        return logging.getLogger(name)


def print_client_info(message: str, level: str = "INFO"):
    """Emit a user-facing informational line via the UI logger.

    Wrapper around the logging subsystem that standardizes output channel and
    formatting for console-facing messages instead of direct ``print`` usage.

    Args:
        message: Human-readable message body to display.
        level: Case-insensitive level name (``INFO``, ``WARNING``, ``ERROR``,
            or ``DEBUG``). Unrecognized values default to ``INFO``.
    """
    logger = logging.getLogger("openmux.client.ui")

    # Map level string to logging level
    level_map = {
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "DEBUG": logging.DEBUG,
    }

    # Log at the appropriate level
    logger.log(level_map.get(level, logging.INFO), message)
