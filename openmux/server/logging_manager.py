"""
Logging manager for OpenMux server
"""

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional


class TerminalStreamHandler(logging.StreamHandler):
    """A stream handler that ensures proper terminal behavior"""

    def __init__(self, stream=None):
        super().__init__(stream)
        self._is_shutting_down = False

    def emit(self, record):
        """Emit a record with proper terminal handling"""
        if self._is_shutting_down:
            return

        try:
            msg = self.format(record)
            stream = self.stream

            # Use a try/except block to handle potential IOErrors
            # that might occur if we're trying to write during shutdown
            try:
                # Explicitly write carriage return before newlines for terminal clarity
                # This helps ensure each new log line starts at the beginning of the line
                stream.write(msg)
                # Always end with an explicit CRLF sequence
                if not msg.endswith("\r\n"):
                    stream.write("\r\n")
                self.flush()
            except (IOError, BrokenPipeError, OSError):
                # Mark as shutting down so we don't keep trying to write
                self._is_shutting_down = True
                # Unregister this handler from all loggers
                self._unregister_handler()
        except Exception:
            # Broad exception handler justification: logging emission must never raise to
            # application code; errors here are handled via handleError which logs to stderr
            # and unregisters the faulty handler to preserve overall logging integrity.
            self.handleError(record)

    def flush(self):
        """Safely flush the stream."""
        if self._is_shutting_down:
            return

        try:
            super().flush()
        except (IOError, BrokenPipeError, OSError):
            # Mark as shutting down and unregister
            self._is_shutting_down = True
            self._unregister_handler()

    def _unregister_handler(self):
        """Remove this handler from all loggers to prevent further issues"""
        # Get the root logger
        root = logging.getLogger()

        # Remove this handler from root logger
        if self in root.handlers:
            root.removeHandler(self)

        # Remove from all other existing loggers
        for name in logging.root.manager.loggerDict:
            logger = logging.getLogger(name)
            if self in logger.handlers:
                logger.removeHandler(self)

    def handleError(self, record):
        """Handle error raised during logging"""
        # Mark as shutting down to prevent further errors
        self._is_shutting_down = True
        # Use the parent's handler for actual error handling
        super().handleError(record)
        # Unregister this handler
        self._unregister_handler()


class TerminalFormatter(logging.Formatter):
    """A formatter specifically designed for terminal output with proper line handling"""

    def __init__(self, fmt=None, datefmt=None):
        super().__init__(fmt, datefmt)

    def format(self, record):
        """Format log record with proper terminal line handling"""
        # Apply standard formatting
        formatted = super().format(record)

        # Ensure each line has proper line endings for terminal display
        # This adds explicit CR+LF to ensure proper terminal behavior
        lines = formatted.splitlines()
        return "\r\n".join(lines)


class SafeFormatter(logging.Formatter):
    """A formatter that sanitizes control characters in log messages"""

    def __init__(self, fmt=None, datefmt=None, style="%"):
        if style == "%":
            super().__init__(fmt, datefmt, style)
        else:
            # Just use default style
            super().__init__(fmt, datefmt)
        # Pattern to match control characters except newline, carriage return, tab
        self.control_pattern = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")

    def format(self, record):
        """Format log record with sanitized message"""
        # Make a copy of the record to avoid modifying the original
        new_record = logging.makeLogRecord(record.__dict__)

        # Sanitize the message if it's a string
        if isinstance(new_record.msg, str):
            # Replace control characters with visible alternatives or remove them
            new_record.msg = self.control_pattern.sub("", new_record.msg)

            # Ensure proper encoding if there are high Unicode characters
            try:
                new_record.msg.encode("ascii")
            except UnicodeEncodeError:
                # If there are non-ASCII characters, handle them carefully
                new_record.msg = new_record.msg.encode("utf-8", errors="replace").decode("utf-8")

        # Apply standard formatting
        return super().format(new_record)


class LoggingManager:
    """Manages logging for the OpenMux server"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize the logging manager"""
        self.config = config or {}

        # Set up root logger
        self._setup_root_logger()

        # Set up component loggers
        self._setup_component_loggers()

    def _setup_root_logger(self):
        """Set up the root logger"""
        # Get log level from config or use INFO as default
        log_level_name = self.config.get("log_level", "INFO")
        log_level = getattr(logging, log_level_name, logging.INFO)

        # Configure root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)

        # Create custom terminal handler
        console_handler = TerminalStreamHandler(sys.stdout)
        console_handler.setLevel(log_level)

        # Create formatter - using terminal-specific formatter for console with date/time and line numbers
        console_formatter = TerminalFormatter(
            "%(asctime)s.%(msecs)03d %(filename)s:%(lineno)d %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console_handler.setFormatter(console_formatter)

        # Add handler to root logger
        root_logger.addHandler(console_handler)

        # Create log directory if it doesn't exist
        log_dir = self.config.get("log_dir", "logs")
        os.makedirs(log_dir, exist_ok=True)

        # Add file handler for main log
        main_log_file = os.path.join(log_dir, "openmux.log")
        file_handler = self._create_rotating_file_handler(main_log_file)

        # Use more detailed formatter for file logs with milliseconds and line numbers
        file_formatter = SafeFormatter(
            "%(asctime)s.%(msecs)03d %(filename)s:%(lineno)d %(name)s %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)

    def _setup_component_loggers(self):
        """Set up loggers for each component"""
        log_dir = self.config.get("log_dir", "logs")

        # Create component loggers
        components = [
            "server",
            "client",
            "serial",
            "auth",
            "config",
            "console",
        ]

        for component in components:
            # Create logger
            logger = logging.getLogger(f"openmux.{component}")

            # Create file handler
            log_file = os.path.join(log_dir, f"openmux_{component}.log")
            file_handler = self._create_rotating_file_handler(log_file)

            # Create formatter with control character handling, milliseconds and line numbers
            formatter = SafeFormatter(
                "%(asctime)s.%(msecs)03d %(filename)s:%(lineno)d %(name)s %(levelname)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            file_handler.setFormatter(formatter)

            # Add handler to logger
            logger.addHandler(file_handler)

    def _create_rotating_file_handler(self, log_file):
        """Create a rotating file handler for a log file"""
        # Get rotation settings from config
        max_bytes = self.config.get("max_log_size", 10 * 1024 * 1024)  # 10 MB default
        backup_count = self.config.get("log_backup_count", 5)

        # Create handler
        handler = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count)

        return handler

    def get_logger(self, name):
        """Get a logger by name"""
        return logging.getLogger(name)
