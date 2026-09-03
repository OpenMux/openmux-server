"""Asynchronous per-port data logger.

Provides a lightweight, rotation-less logger for adapter ports. Records
either line-oriented text or JSON Lines (jsonl) with byte-hex previews.

Features:
- Per-port files with configurable path via port config (`log_file`).
- Two formats: `line` (default) and `jsonl` (config key `log_format`).
- Direction filters via `log_direction`/`log_directions` ("in", "out"),
    where "in" means data entering from the port adapter toward clients and
    "out" means client traffic headed toward the port adapter. Lifecycle
    "meta" events bypass direction filters and are always recorded.
- Backpressure-safe: uses an `asyncio.Queue` and a single writer task.

Usage example:
    DataLogger.get().record(port_name, data, direction, client_id, meta, port_obj)
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ..common.fsutil import ensure_directory


@dataclass
class LogEvent:
    """Structured log event consumed by the writer loop.

    Attributes:
        ts: Event timestamp (epoch seconds, UTC).
        port: Logical port name.
        direction: Flow direction. "in" = from port adapter toward clients,
            "out" = client writes headed to the port adapter.
        size: Size of `data` in bytes.
        client_id: Optional client/session identifier.
        data: Raw bytes payload (may include binary data).
        meta: Optional extra metadata to include (jsonl mode only).
    """

    ts: float
    port: str
    direction: str  # "in" or "out" or "meta"
    size: int
    client_id: Optional[str]
    data: bytes
    meta: Optional[Dict[str, Any]] = None


class DataLogger:
    """Asynchronous per-port JSONL logger with simple rotation-less files.

        Usage:
            DataLogger.get().record(port_name, data, direction, client_id, meta)
            DataLogger.get().record_meta(port_name, event, client_id, meta)

    Filenames are resolved as:
      1) port-specific config key: `log_file` on the port's per-port config
         (a dict, or a per-port attribute) if present
      2) default pattern: {log_dir}/ports/{port_name}.log, where `log_dir` is the base
         directory set via `set_base_dir()` (config `logging.log_dir`), defaulting to `logs`

    Writes are buffered through an asyncio.Queue and a single background task.
    """

    _instance: Optional["DataLogger"] = None

    @classmethod
    def get(cls) -> "DataLogger":
        """Return process-wide singleton instance.

        Lazily creates a new instance on first access.

        Returns:
            DataLogger: The singleton logger instance.
        """
        if cls._instance is None:
            cls._instance = DataLogger()
        return cls._instance

    def __init__(self) -> None:
        """Initialize the logger with defaults (the queue is created lazily on first use).

        Sets default format to `line` and prepares internal caches for
        line buffering and direction filtering.
        """
        # Loop-bound primitives are created lazily in _ensure_task (NOT here). In
        # Python 3.9 an asyncio.Queue binds its futures to events.get_event_loop()
        # at construction time. This process-wide singleton is constructed during
        # OpenMuxServer.__init__, before main() calls new_event_loop()/set_event_loop(),
        # so an eager asyncio.Queue() would bind to a throwaway loop. When the writer
        # task later runs on the real loop, Queue.get() then raises "Future attached
        # to a different loop". Defer creation to the first use on the running loop.
        self.queue: Optional["asyncio.Queue[LogEvent]"] = None
        self._queue_loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        self._task_loop: Optional[asyncio.AbstractEventLoop] = None
        self._files: Dict[str, Any] = {}
        self.enabled = True  # can be toggled later via config
        # Base directory for the default `{base}/ports/{port_name}.log` pattern. Null
        # means the historical cwd-relative `logs/ports` (issue #47: the server sets this
        # from config `logging.log_dir` at startup and on each reload).
        self.base_dir: Optional[str] = None
        self.logger = logging.getLogger("openmux.server.data_logger")
        # Format configuration
        # Supported formats: 'line' (default), 'jsonl'
        self.default_format = "line"
        # Default line template: "YYYY-MM-DDTHH:MM:SSZ: <text>"
        self.default_line_template = "{ts}: {text}"
        # Per-file buffers for partial lines when using line format
        self._line_buffers: Dict[str, bytearray] = {}
        # Direction filters cache: port_name -> set({'in','out'})
        self._direction_cache = {}

    def _default_path(self, port_name: str) -> Optional[Path]:
        """Return default log path for a port.

        Creates parent directories as needed under ``{base_dir or "logs"}/ports`` (issue #47).

        Args:
            port_name: Logical port name.

        Returns:
            Optional[Path]: Filesystem path for the port log file, or None when the
                log directory cannot be created (issue #42; already warned once).
        """
        base = Path(self.base_dir or "logs") / "ports"
        if not ensure_directory(base):
            return None
        return base / f"{port_name}.log"

    def _override_log_path(self, raw: Optional[str]) -> Optional[Path]:
        """Return `raw` as a Path when its parent directory can be created.

        Returns None when `raw` is falsy or its parent directory cannot be
        created, so the caller falls back to the default path (issue #42).
        """
        if not raw:
            return None
        p = Path(str(raw))
        if ensure_directory(p.parent):
            return p
        return None

    def _resolve_path_for_port(self, port_name: str, port_obj: Optional[Any] = None) -> Optional[Path]:
        """Resolve log file path for a port, honoring config overrides.

        Supports `log_file` in the port's `config` (dict or attribute) or
        a direct `log_file` attribute on the port object. Port-level `log_file`
        overrides are honored even when the default log directory is not writable.

        Args:
            port_name: Logical port name.
            port_obj: Optional port instance (or wrapper) used to inspect config.

        Returns:
            Optional[Path]: Filesystem path to write the log entry to, or None
                when no log file can be created (issue #42; already warned once).
        """
        # Port-specific configured filename if available
        try:
            obj = port_obj
            # Unwrap unified wrapper if present
            if obj is not None and hasattr(obj, "unified_port"):
                obj = getattr(obj, "unified_port", obj)

            cfg = getattr(obj, "config", None)
            # Check config dict first, then an object/dataclass attribute, then
            # the port object itself. The first override whose directory can be
            # created wins; the default path is the last resort.
            override = self._override_log_path(cfg.get("log_file") if isinstance(cfg, dict) else None)
            if override is None:
                override = self._override_log_path(getattr(cfg, "log_file", None))
            if override is None:
                override = self._override_log_path(getattr(obj, "log_file", None))
            if override is not None:
                return override
        except Exception:
            self.logger.error("Error resolving log path for port", exc_info=True)
        return self._default_path(port_name)

    def get_log_path(self, port_name: str, port_obj: Optional[Any] = None) -> Optional[Path]:
        """Return the filesystem path for a port's log file.

        This is a thin wrapper around the internal resolver so external
        components (like the Web Console) can discover the same location the
        logger writes to.

        Returns:
            Optional[Path]: Path, or None when the log directory cannot be created
            (issue #42; the warning was already emitted once).
        """

        return self._resolve_path_for_port(port_name, port_obj)

    async def _writer_loop(self) -> None:
        """Background task that serializes events to per-port files.

        Uses the `queue` that the matching `_ensure_task` call created on this
        same event loop (guaranteed non-None at this point). Captured locally so
        the loop doesn't re-read an attribute that may be rebuilt later.
        """
        queue = self.queue
        assert queue is not None  # _ensure_task sets it before creating this task
        while True:
            ev = await queue.get()
            try:
                port_obj = getattr(ev, "port_obj", None)
                path = self._resolve_path_for_port(ev.port, port_obj)
                if path is None:
                    # Log dir is not writable (issue #42); already warned once.
                    continue
                fmt = self._resolve_format_for_port(ev.port, port_obj)
                # Cache open handles per-path
                fh = self._files.get(str(path))
                if fh is None:
                    # Open in append text mode
                    fh = open(path, "a", encoding="utf-8")
                    self._files[str(path)] = fh

                if fmt == "jsonl":
                    # JSONL record with base64-safe hex plus ascii preview
                    ascii_preview = "".join(chr(b) if 32 <= b <= 126 else "." for b in ev.data[:128])
                    rec = {
                        "ts": datetime.utcfromtimestamp(ev.ts).isoformat() + "Z",
                        "port": ev.port,
                        "dir": ev.direction,
                        "size": ev.size,
                        "client": ev.client_id,
                        "hex": ev.data.hex(),
                        "ascii": ascii_preview,
                    }
                    # For meta events, expose event name at top-level
                    if ev.direction == "meta" and ev.meta and isinstance(ev.meta, dict):
                        evt = ev.meta.get("event")
                        if evt:
                            rec["event"] = evt
                    if ev.meta:
                        rec["meta"] = ev.meta
                    fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
                    fh.flush()
                else:
                    # Line-oriented format: buffer until a newline is observed.
                    key = str(path)
                    buf = self._line_buffers.get(key)
                    if buf is None:
                        buf = bytearray()
                        self._line_buffers[key] = buf
                    if ev.direction == "meta":
                        # Emit a single meta line immediately (no buffering)
                        ts = datetime.utcfromtimestamp(ev.ts).strftime("%Y-%m-%dT%H:%M:%SZ")
                        template = self._resolve_line_template_for_port(ev.port, port_obj)
                        evt = None
                        extras = ""
                        try:
                            if ev.meta and isinstance(ev.meta, dict):
                                evt = ev.meta.get("event")
                                extras = " ".join(f"{k}={v}" for k, v in ev.meta.items() if k != "event")
                        except Exception:
                            pass
                        text = f"[event] {evt or 'meta'}" + (f" {extras}" if extras else "")
                        line = template.format(
                            ts=ts,
                            port=ev.port,
                            dir=ev.direction,
                            client=(ev.client_id or ""),
                            text=text,
                        )
                        fh.write(line + "\n")
                        fh.flush()
                    else:
                        buf.extend(ev.data)
                        while True:
                            try:
                                idx = buf.index(0x0A)  # '\n'
                            except ValueError:
                                break
                            raw_line = bytes(buf[:idx])
                            del buf[: idx + 1]
                            if raw_line.endswith(b"\r"):
                                raw_line = raw_line[:-1]
                            text = raw_line.decode("utf-8", errors="replace")
                            ts = datetime.utcfromtimestamp(ev.ts).strftime("%Y-%m-%dT%H:%M:%SZ")
                            template = self._resolve_line_template_for_port(ev.port, port_obj)
                            line = template.format(
                                ts=ts,
                                port=ev.port,
                                dir=ev.direction,
                                client=(ev.client_id or ""),
                                text=text,
                            )
                            fh.write(line + "\n")
                            fh.flush()
            except Exception:
                # Best-effort logging; log traceback then drop
                self.logger.error("DataLogger writer loop error", exc_info=True)
            finally:
                queue.task_done()

    def _ensure_task(self) -> None:
        """Ensure the writer queue and task are bound to the running event loop.

        Rebuilds the queue and the writer task whenever the current running loop
        differs from the loop that created them. In Python 3.9 an asyncio.Queue
        hands out futures bound to the loop that constructed it, so a queue made on
        a prior/throwaway loop (or a pre-loop-swap loop) would make Queue.get() raise
        "Future attached to a different loop". Rebuilding on the running loop keeps
        the logger working across an event-loop swap (e.g. a full reload).
        """
        loop = asyncio.get_running_loop()
        if self.queue is None or self._queue_loop is not loop:
            # Constructed while `loop` is running, so asyncio.Queue() binds to `loop`.
            self.queue = asyncio.Queue(maxsize=1000)
            self._queue_loop = loop
        if self._task is None or self._task.done() or self._task_loop is not loop:
            self._task_loop = loop
            self._task = loop.create_task(self._writer_loop())

    def configure(self, enabled: Optional[bool] = None) -> None:
        """Update runtime configuration.

        Args:
            enabled: Toggle logging on/off. When disabled, calls to `record`
                are ignored.
        """
        if enabled is not None:
            self.enabled = bool(enabled)

    def set_base_dir(self, base: Optional[str]) -> None:
        """Set the base directory for default per-port log files (issue #47).

        Args:
            base: Directory that holds the `ports/` subdirectory, e.g. the resolved
                config `logging.log_dir`; `None` falls back to `logs/ports`.

        When the base directory changes, the open file handles and line buffers
        belonging to the previous base are closed and dropped, so the next write
        after the swap reopens the files under the new directory.
        """
        base = str(base).strip() if base is not None else None
        if base == "":
            base = None
        if base == self.base_dir:
            return
        # Close file handles under the previous base (default `logs/ports`
        # when none was ever set) BEFORE repointing, so a swap never keeps a
        # write-target handle open under the old directory.
        previous_base = str(Path(self.base_dir).resolve()) if self.base_dir is not None else str(Path("logs").resolve())
        self.base_dir = base
        try:
            previous_ports = Path(previous_base) / "ports"
            for key in list(self._files.keys()):
                if Path(key).is_relative_to(previous_ports):
                    fh = self._files.pop(key, None)
                    if fh is not None:
                        try:
                            fh.close()
                        except Exception:  # justification: best-effort close of stale handles
                            pass
                    self._line_buffers.pop(key, None)
        except Exception:  # justification: stale-handle cleanup must never break records
            pass

    def _resolve_format_for_port(self, port_name: str, port_obj: Optional[Any]) -> str:
        """Resolve output format for a port.

        Reads `log_format` from the port's config (dict or attribute). Valid
        values are "jsonl" and "line"; defaults to the logger's
        `default_format`.

        Args:
            port_name: Logical port name.
            port_obj: Optional port object used to inspect config.

        Returns:
            str: "jsonl" or "line".
        """
        try:
            obj = port_obj
            if obj is not None and hasattr(obj, "unified_port"):
                obj = getattr(obj, "unified_port", obj)
            cfg = getattr(obj, "config", None)
            if isinstance(cfg, dict):
                fmt = (cfg.get("log_format") or "").strip().lower()
                if fmt in ("jsonl", "line"):
                    return fmt
            if cfg is not None and hasattr(cfg, "log_format"):
                fmt = str(getattr(cfg, "log_format", "")).strip().lower()
                if fmt in ("jsonl", "line"):
                    return fmt
        except Exception:
            self.logger.error("Error resolving log format for port", exc_info=True)
        return self.default_format

    def _resolve_line_template_for_port(self, port_name: str, port_obj: Optional[Any]) -> str:
        """Resolve line template for a port when using `line` format.

        Reads `log_line_template` from the port's config (dict or attribute)
        and falls back to the logger's `default_line_template`.

        Args:
            port_name: Logical port name.
            port_obj: Optional port object used to inspect config.

        Returns:
            str: Python format string like "{ts}: {text}".
        """
        try:
            obj = port_obj
            if obj is not None and hasattr(obj, "unified_port"):
                obj = getattr(obj, "unified_port", obj)
            cfg = getattr(obj, "config", None)
            if isinstance(cfg, dict):
                tpl = cfg.get("log_line_template")
                if isinstance(tpl, str) and tpl:
                    return tpl
            if cfg is not None and hasattr(cfg, "log_line_template"):
                tpl = getattr(cfg, "log_line_template", None)
                if isinstance(tpl, str) and tpl:
                    return tpl
        except Exception:
            self.logger.error("Error resolving line template for port", exc_info=True)
        return self.default_line_template

    def record(
        self,
        port_name: str,
        data: bytes,
        direction: str,
        client_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        port_obj: Optional[Any] = None,
    ) -> None:
        """Enqueue a log event for asynchronous persistence.

        Applies direction filtering and starts the writer task if needed.
        When the queue is full, events are dropped to avoid backpressure.

        Args:
            port_name: Logical port name.
            data: Raw bytes to log.
            direction: Flow direction ("in" or "out").
            client_id: Optional client/session identifier.
            meta: Optional metadata to attach (included in jsonl records).
            port_obj: Optional port object to derive file path/format.
        """
        if not self.enabled:
            return
        try:
            # Direction filtering
            if not self._direction_allowed(port_name, port_obj, direction):
                return
            # Avoid recording empty payloads for in/out directions
            if direction in ("in", "out") and not data:
                return
            self._ensure_task()
            ev = LogEvent(
                ts=datetime.utcnow().timestamp(),
                port=port_name,
                direction=direction,
                size=len(data),
                client_id=client_id,
                data=bytes(data),
                meta=meta,
            )
            # Attach the port_obj for filename resolution
            setattr(ev, "port_obj", port_obj)
            try:
                self.queue.put_nowait(ev)
            except asyncio.QueueFull:
                # Drop if overloaded
                pass
        except Exception:
            self.logger.error("Error recording data event", exc_info=True)

    def invalidate_port_cache(self, port_name: str) -> None:
        """Invalidate cached direction filters for a port.

        Must be called when a port is removed or re-registered so that
        updated log_direction config is re-read on the next record call.

        Args:
            port_name: Logical port name whose cache entry to drop.
        """
        self._direction_cache.pop(port_name, None)

    def _direction_allowed(self, port_name: str, port_obj: Optional[Any], direction: str) -> bool:
        """Return whether the direction is allowed for the given port.

        Uses cached results per port and consults port config via
        `log_direction` or `log_directions` (string or iterable) to
        restrict to one or both of {"in", "out"}. Defaults to
        {"in"} (port -> client traffic only).

        Args:
            port_name: Logical port name.
            port_obj: Optional port object for config lookup.
            direction: Direction value to test (case-insensitive).

        Returns:
            bool: True if the direction is permitted by configuration.
        """
        # Normalize direction
        d = direction.lower()
        # Lifecycle/meta events bypass direction filtering
        if d == "meta":
            return True
        if d not in ("in", "out"):
            return True
        # Cached?
        cached = self._direction_cache.get(port_name)
        if cached is not None:
            return d in cached
        # Resolve from config (default to port->client traffic only)
        allowed = {"in"}
        try:
            obj = port_obj
            if obj is not None and hasattr(obj, "unified_port"):
                obj = getattr(obj, "unified_port", obj)
            cfg = getattr(obj, "config", None)
            # Accept single string key log_direction or list/str log_directions
            raw = None
            if isinstance(cfg, dict):
                raw = cfg.get("log_direction") or cfg.get("log_directions")
            elif cfg is not None:
                raw = getattr(cfg, "log_direction", None) or getattr(cfg, "log_directions", None)
            if raw:
                if isinstance(raw, str):
                    vals = [raw]
                elif isinstance(raw, (list, tuple, set)):
                    vals = list(raw)
                else:
                    vals = []
                norm = {str(v).strip().lower() for v in vals if str(v).strip()}
                norm = {v for v in norm if v in ("in", "out")}
                if norm:
                    allowed = norm
        except Exception:
            self.logger.error("Error resolving direction filters for port", exc_info=True)
        self._direction_cache[port_name] = allowed
        return d in allowed

    def record_meta(
        self,
        port_name: str,
        event: str,
        client_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        port_obj: Optional[Any] = None,
    ) -> None:
        """Record a lifecycle/meta event for a port.

        Emits a single log record regardless of output format. Meta events are
        not subject to direction filtering and can be recorded without data.

        Args:
            port_name: Logical port name.
            event: Event name (e.g. "client_connected").
            client_id: Optional client/session identifier.
            meta: Optional extra metadata; merged with {'event': event}.
            port_obj: Optional port object to derive file path/format.
        """
        m = {"event": event}
        if meta and isinstance(meta, dict):
            m.update(meta)
        # Delegate to record with direction='meta' and empty data
        self.record(
            port_name=port_name,
            data=b"",
            direction="meta",
            client_id=client_id,
            meta=m,
            port_obj=port_obj,
        )
