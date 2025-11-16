import io
import logging
import os

import pytest

from openmux.server.logging_manager import (
    LoggingManager,
    SafeFormatter,
    TerminalFormatter,
    TerminalStreamHandler,
)


class ErroringStream:
    def __init__(self, exc):
        self.exc = exc

    def write(self, msg):
        raise self.exc

    def flush(self):
        return


def snapshot_root_handlers():
    root = logging.getLogger()
    return list(root.handlers)


def restore_root_handlers(orig_handlers):
    root = logging.getLogger()
    # Remove all current
    for h in list(root.handlers):
        root.removeHandler(h)
    # Re-add originals
    for h in orig_handlers:
        root.addHandler(h)


@pytest.mark.asyncio
async def test_terminal_stream_handler_emits_crlf_and_unregisters_on_error(monkeypatch):
    # Normal write yields CRLF termination
    mem = io.StringIO()
    h = TerminalStreamHandler(mem)
    fmt = TerminalFormatter("%(message)s")
    h.setFormatter(fmt)
    rec = logging.LogRecord(name="t", level=logging.INFO, pathname=__file__, lineno=1, msg="hello", args=(), exc_info=None)
    h.emit(rec)
    assert mem.getvalue().endswith("\r\n")

    # Attach to root and ensure broken pipe unregisters handler
    root = logging.getLogger()
    root.addHandler(h)
    err_stream = ErroringStream(BrokenPipeError())
    h.stream = err_stream
    h.emit(rec)
    assert h._is_shutting_down is True
    assert h not in logging.getLogger().handlers


def test_terminal_formatter_crlf_joining():
    f = TerminalFormatter("%(message)s")
    rec = logging.LogRecord(name="t", level=logging.INFO, pathname=__file__, lineno=1, msg="a\nb", args=(), exc_info=None)
    out = f.format(rec)
    assert out == "a\r\nb"


def test_safe_formatter_sanitizes_and_handles_unicode():
    f = SafeFormatter("%(message)s")
    msg = "bad\x07\x1b msg 日本語"
    rec = logging.LogRecord(name="t", level=logging.INFO, pathname=__file__, lineno=1, msg=msg, args=(), exc_info=None)
    out = f.format(rec)
    # Control chars removed, unicode preserved
    assert "\x07" not in out and "\x1b" not in out
    assert "日本語" in out


def test_logging_manager_initializes_root_and_components(tmp_path):
    log_dir = tmp_path / "logs"
    cfg = {
        "log_level": "DEBUG",
        "log_dir": str(log_dir),
        "max_log_size": 1024,
        "log_backup_count": 2,
    }
    orig_handlers = snapshot_root_handlers()
    try:
        lm = LoggingManager(cfg)
        root = logging.getLogger()
        # Root has console and file handler
        types = {type(h) for h in root.handlers}
        assert any(isinstance(h, TerminalStreamHandler) for h in root.handlers)
        assert any(h.__class__.__name__ == "RotatingFileHandler" for h in root.handlers)
        # Console formatter type is TerminalFormatter
        cons = [h for h in root.handlers if isinstance(h, TerminalStreamHandler)][0]
        assert isinstance(cons.formatter, TerminalFormatter)
        # File formatter type is SafeFormatter
        file_handlers = [h for h in root.handlers if h.__class__.__name__ == "RotatingFileHandler"]
        assert any(isinstance(h.formatter, SafeFormatter) for h in file_handlers)

        # Component loggers have rotating file handlers
        for comp in ["server", "client", "serial", "auth", "config", "console"]:
            lg = logging.getLogger(f"openmux.{comp}")
            assert any(h.__class__.__name__ == "RotatingFileHandler" for h in lg.handlers)

        # Files created
        assert (log_dir / "openmux.log").exists()
        for comp in ["server", "client", "serial", "auth", "config", "console"]:
            assert (log_dir / f"openmux_{comp}.log").exists()

        # Rotation settings present on handlers
        for h in file_handlers:
            assert getattr(h, "maxBytes", None) == 1024
            assert getattr(h, "backupCount", None) == 2
    finally:
        # Cleanup: restore root handlers and remove component handlers
        restore_root_handlers(orig_handlers)
        for comp in ["server", "client", "serial", "auth", "config", "console"]:
            lg = logging.getLogger(f"openmux.{comp}")
            for h in list(lg.handlers):
                lg.removeHandler(h)


def test_logging_manager_get_logger(tmp_path):
    cfg = {"log_dir": str(tmp_path / "logs")}
    orig_handlers = snapshot_root_handlers()
    try:
        lm = LoggingManager(cfg)
        lg = lm.get_logger("openmux.server")
        lg.debug("hi")
        assert isinstance(lg, logging.Logger)
    finally:
        restore_root_handlers(orig_handlers)
        for comp in ["server", "client", "serial", "auth", "config", "console"]:
            lg2 = logging.getLogger(f"openmux.{comp}")
            for h in list(lg2.handlers):
                lg2.removeHandler(h)
