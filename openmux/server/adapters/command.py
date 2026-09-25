"""
Unified Command Adapter for OpenMux

Provides command execution ports that run external processes.
"""

import asyncio
import logging
import os
import pty
import shlex
import signal
import socket
from typing import Any, Dict, List, Optional, Set

from ...common.identity import get_server_id
from ..access_control import InvalidWriteMode, parse_write_mode, wire_to_mode
from .base_adapter import AdapterCapability, BaseGenericAdapter
from .lifecycle import PortState

#
# issue #67 one-release deprecation shim. These per-port keys were removed
# from the command_ports schema (the schema now rejects them, so strict paths
# like --check-config and the Config Editor name each unknown key). At live
# load, ConfigManager.load_config strips them in place with one warning per
# port, mirroring the ticket-#74/locations absorb shim: a stale config keeps
# booting instead of failing, until the next minor release. always_buffer
# (issue #83): relayed output now flows unconditionally; late federation
# viewers get history from scrollback_size instead.

REMOVED_COMMAND_PORT_KEYS = (
    "auto_restart",
    "restart_delay",
    "max_restarts",
    "restart_backoff",
    "local_echo",
    "pty_force_raw",
    "pty_enter_mode",
    "spawn_mode",
    "use_pty",
    "output_crlf",
    "clean_env",
    "intercept_term_queries",
    "enable_output_batching",
    "output_batch_size",
    "output_batch_timeout",
    "output_force_flush_timeout",
    "enable_batching",
    "batch_size",
    "batch_timeout",
    "always_buffer",
)


def removed_command_port_keys(config: Any) -> List[str]:
    """Dotted paths of removed command port keys still present in ``config``.

    Pure detection (no mutation), used by the deprecation shim
    (``absorb_removed_command_port_keys``) and by tests.

    Args:
        config: Parsed config mapping (the full server.yaml dict).

    Returns:
        List[str]: Dotted keys, e.g.
        ``["command_ports[1].auto_restart"]``. Empty when the config is
        clean or has no command ports.
    """
    found: List[str] = []
    if not isinstance(config, dict):
        return found
    for index, port in enumerate(config.get("command_ports") or []):
        if not isinstance(port, dict):
            continue
        for key in REMOVED_COMMAND_PORT_KEYS:
            if key in port:
                found.append(f"command_ports[{index}].{key}")
    return found


def absorb_removed_command_port_keys(config: Any, logger: Optional[Any] = None) -> List[str]:
    """Strip removed command port keys from ``config`` (in place) and warn.

    One warning per port, naming every removed key found on it. The schema
    rejects the keys on strict paths; this shim keeps a live server booting
    during the one release the keys are deprecated.

    Args:
        config: Parsed config mapping (mutated in place).
        logger: Optional logger for the warnings; when omitted the keys are
            still detected and stripped silently.

    Returns:
        List[str]: The dotted key paths that were present and removed.
    """
    keys = removed_command_port_keys(config)
    if not keys or not isinstance(config, dict):
        return keys
    for index, port in enumerate(config.get("command_ports") or []):
        if not isinstance(port, dict):
            continue
        stale = [key for key in REMOVED_COMMAND_PORT_KEYS if key in port]
        if not stale:
            continue
        for key in stale:
            del port[key]
        if logger is not None:
            name = str(port.get("name", f"index {index}"))
            logger.warning(
                "Command port %r: removed config key(s) %s are ignored and were "
                "stripped; the behavior is now unconditional (issue #67). Remove "
                "them from your configuration until the next minor release.",
                name,
                ", ".join(sorted(stale)),
            )
    return keys


class CommandPort:
    """Command execution port wrapping a spawned process (optionally PTY-backed).

    Handles process lifecycle (spawn, monitor, optional auto-restart), I/O
    buffering, newline normalization, terminal capability interception, and
    batching of outbound and inbound data for connected clients.

    Contract reference: docs/ADAPTER_PORT_CONTRACT.md

    Configuration Keys (issue #67 consolidated the surface to these 15):
        name (str): Logical port name.
        description (str): Human-readable description.
        command (str): Command string to execute.
        shell (bool): Run under shell via ``create_subprocess_shell``.
        cwd (str): Working directory for the process.
        env (dict): Extra/override environment variables.
        interactive (bool): Preset that enables PTY + normalize_newlines at
            once.
        normalize_newlines (bool): Normalize newline sequences on input.
        max_read_write_users: Write-slot capacity (one/multiple/none).
        read_write_groups / read_only_groups: Console-group access control.
        scrollback_size (int): Bytes of output to keep for replay (0 = off).
        spawn_on_demand (bool): Spawn the process on first client attach.
        idle_timeout_sec (float): Stop the process this many seconds after
            the last client leaves; 0 = never auto-stop on idle.

    Behavior that is unconditional (issue #67 removed the config knobs):
        - The process environment is sanitized (minimal allow-list + probe-
          variable strip); ``env:`` merges extra values on top.
        - XTGETTCAP terminal capability queries are intercepted and answered,
          so editor probes never stall a session.
        - Newlines are normalized on both directions (pipe input -> LF,
          output -> CRLF on PTY, LF on pipes).
        - I/O is batched: output 1024 bytes / 2 ms idle / 1.0 s force flush;
          writes 1024 bytes / 2 ms.
        - The process is never restarted automatically after exit. The
          monitor reports the exit and marks the port offline (non-zero exit)
          or resting (code 0); press Enter in the console to respawn.

    Args:
        name: Logical port name (unique within adapter).
        config: Port configuration mapping (see keys above).
        adapter: Parent ``CommandAdapter`` instance.
    """

    state: PortState  # enforced contract annotation

    def __init__(
        self,
        name: str,
        config: Dict[str, Any],
        adapter: "CommandAdapter",
    ):
        self.name = name
        self.config = config
        self.adapter = adapter
        self.state = PortState.CONFIGURED
        self.logger = logging.getLogger(f"openmux.adapter.command.{name}")

        self.command = config.get("command", "")
        self.shell = config.get("shell", False)
        self.cwd = config.get("cwd")
        self.env = config.get("env")
        self.description = config.get("description", f"Command: {self.command}")
        # Write-slot capacity mode (issue #59): "one" (default) / "multiple" /
        # "none". Legacy ints (0/1/>=2) still work and log a one-time
        # deprecation line; any other value raises (create_port fails the port
        # with an error log).
        self.max_read_write_users: str = parse_write_mode(
            config.get("max_read_write_users", 1), port_name=name, logger=self.logger
        )
        # Console-group access control (issue #24): empty on both = open to all
        # authenticated users (implicit "user" group). See docs/ADAPTER_PORT_CONTRACT.md.
        self.read_write_groups: List[str] = list(config.get("read_write_groups") or [])
        self.read_only_groups: List[str] = list(config.get("read_only_groups") or [])
        # PDU power feeds for this console: outlet refs <pdu>.<id> (may be
        # several for A/B dual feed). Read live by the power adapter and the
        # web console; updated in place on soft reload like the group lists.
        self.power: List[str] = [str(r) for r in (config.get("power") or [])]

        # Behaviour flags. issue #67: interactive is the only config surface
        # for the terminal preset (PTY + normalize_newlines); local_echo,
        # use_pty, output_crlf, clean_env, intercept_term_queries and
        # pty_force_raw/pty_enter_mode config keys are removed. The behaviors
        # they toggled are now unconditional where the default was correct
        # (env sanitizing, XTGETTCAP interception, CRLF on output) or dropped
        # (local echo, forced PTY raw mode, enter-mode mapping). issue #83:
        # always_buffer is removed too — port output always feeds the relay
        # queue (and the scrollback ring) while a federation hold is active;
        # late remote viewers get history from scrollback_size.
        self.interactive = config.get("interactive", False)
        self.normalize_newlines = config.get("normalize_newlines", self.interactive)
        # Internal only: an interactive port always gets a PTY. The separate
        # use_pty config key is removed; the pipe path is reached by leaving
        # interactive off (issue #67).
        self.use_pty: bool = bool(self.interactive)
        self.scrollback_size = int(config.get("scrollback_size", 0))  # bytes; 0 = disabled

        # Process lifecycle policy
        self.spawn_on_demand: bool = bool(config.get("spawn_on_demand", False))
        # Idle stop: when last client disconnects, stop the process after this many seconds (>0).
        # 0 or missing => never auto-stop on idle.
        try:
            self.idle_timeout_sec: float = float(config.get("idle_timeout_sec", 0) or 0)
        except Exception:
            self.idle_timeout_sec = 0.0
        self._idle_stop_task: Optional[asyncio.Task] = None

        # Output batching config (server -> client). issue #67: batching is
        # unconditional at fixed thresholds; the config keys are removed.
        self._output_batch_size = 1024
        self._output_batch_timeout = 0.002
        self._output_force_flush_timeout = 1.0
        self._output_buffer = bytearray()
        self._output_buffer_lock = asyncio.Lock()
        self._output_flush_task: Optional[asyncio.Task] = None
        self._output_flush_event = asyncio.Event()

        # Runtime refs
        self._pty_master_fd: Optional[int] = None
        self._pty_reader_added = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.process: Optional[asyncio.subprocess.Process] = None
        self._writer = None
        self.is_running = False
        self.process_active = False
        # Contract attribute (docs/ADAPTER_PORT_CONTRACT.md): the "connected"
        # flag the web UI / API shows for this port. A resting port (on-demand
        # and not yet spawned, intentionally stopped, or having exited cleanly
        # with code 0) counts as connected. It is disconnected only while the
        # process died abnormally (non-zero exit, signal) or a spawn/respawn
        # failed -- the same condition that sets status_message, so the UI
        # banner and the info panel stay in sync.
        self.is_connected = True
        self._connect_notified = True
        self.client_count = 0
        # data_callback is set by PortManager.register_unified_port() or wired here
        # if the adapter already has a PM at construction time.
        self.data_callback = None
        _pm = getattr(getattr(self, "adapter", None), "main_port_manager", None)
        if _pm and hasattr(_pm, "send_data"):
            self.data_callback = _pm.send_data
        self._read_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._queue_fallback_logged = False

        # Automatic restart after exit is not supported (issue #67): the
        # monitor reports the exit and marks the port resting/offline; press
        # Enter in the console to respawn. Supervised daemons belong under
        # systemd (expose their socket on a TCP port instead).

        self._stopped_notice_sent = False
        # Human-readable reason the process is not running (issue #62):
        # spawn failure, process exit with non-zero code, or max-restarts.
        # Empty while the process is active or the port is in a resting state.
        self.status_message: str = ""
        self._status_changed: Optional[bool] = None

        if not self.command:
            raise ValueError(f"Command port {name} requires 'command' configuration")

    def _set_status_message(self, message: str) -> None:
        """Set or clear the process-down reason and push a meta refresh on state change.

        Args:
            message: New status message (empty string clears).
        """
        new_state = bool(message)
        if self._status_changed is None or self._status_changed != new_state:
            self._status_changed = new_state
            # Set the reason first so the meta payload carries the final text;
            # muxcon listeners read it to push live updates to peers (#62).
            self.status_message = str(message or "")
            try:
                mpm = getattr(getattr(self, "adapter", None), "main_port_manager", None)
                if mpm and hasattr(mpm, "notify_meta_updated"):
                    mpm.notify_meta_updated(
                        self.name,
                        {"event": "command_status_changed", "status_message": self.status_message},
                    )
            except Exception:
                # justification: optional notification; UI event delivery is best-effort
                pass
        else:
            self.status_message = str(message or "")

    def _set_connected(self, value: bool) -> None:
        """Update ``is_connected`` and push a meta refresh on a real change.

        Follows the serial/tcp_initiator convention: one event per state
        flip carrying ``connected`` so consumers (web console banner, info
        panel, client_listener notices, muxcon relay) can update live. The
        current status_message is included so the reason travels in the
        same frame.

        Args:
            value: New connected state (True = up or resting, False = down).
        """
        new_state = bool(value)
        if self._connect_notified == new_state:
            self.is_connected = new_state
            return
        self._connect_notified = new_state
        self.is_connected = new_state
        try:
            mpm = getattr(getattr(self, "adapter", None), "main_port_manager", None)
            if mpm and hasattr(mpm, "notify_meta_updated"):
                event = "command_connected" if new_state else "command_disconnected"
                mpm.notify_meta_updated(
                    self.name,
                    {"event": event, "connected": new_state, "status_message": self.status_message},
                )
        except Exception:
            # justification: optional notification; UI event delivery is best-effort
            pass

    async def write_data(self, data: bytes) -> int:
        """Standardized write API: return number of bytes accepted.

        Uses the internal writer when present. When `stop()` has cleared
        the writer, a lone newline still triggers the non-forced respawn
        (`_try_newline_respawn`) the PROCESS_NOT_RUNNING notice promises,
        and is delivered to the fresh writer. Other data returns 0; the
        one-shot stopped notice is emitted by `_try_newline_respawn`.
        """
        if not data:
            return 0
        writer = getattr(self, "_writer", None)
        if not writer:
            if not await self._try_newline_respawn(data):
                return 0
            new_writer = self._writer
            if new_writer is None:
                return 0
            # Write directly: a respawn created a fresh writer, and going
            # through `CommandWriter.write` would re-check liveness flags.
            await new_writer._write_direct(data)
            return len(data)
        try:
            await writer.write(data)
            return len(data)
        except Exception:  # justification: transient write failure; upstream caller treats 0 as backpressure signal
            return 0

    async def _try_newline_respawn(self, data: bytes) -> bool:
        """Respawn the process on a lone newline (non-forced restart).

        Enter (CR, LF, or CRLF) is the input the PROCESS_NOT_RUNNING notice
        tells the user to press. Shared by `write_data` (after `stop()`
        cleared the writer) and `CommandWriter.write` (natural death). The
        caller delivers the newline itself: a successful respawn creates a
        fresh writer, so delivery must use the current stream. Any other
        input emits the one-shot PROCESS_NOT_RUNNING notice.

        Returns:
            bool: True when the process is now active (respawned or already
            restarted); False otherwise.
        """
        try:
            newline_only = data in (b"\r", b"\n", b"\r\n")
        except Exception:  # justification: tolerate unexpected non-bytes input; treat as not a pure newline
            newline_only = False
        if not newline_only:
            if not self._stopped_notice_sent:
                self._stopped_notice_sent = True
                hint = "spawn" if getattr(self, "spawn_on_demand", False) else "respawn"
                notice = f"\r\n[OpenMux:PROCESS_NOT_RUNNING {self._stopped_prefix()} – press Enter to {hint}]\r\n".encode()
                self._schedule_notice_emit(notice)
            return False
        ok = await self.restart(force=False)
        return bool(ok and self.process_active)

    async def _emit_output_chunk(
        self,
        chunk: bytes,
        *,
        require_clients: Optional[bool] = None,
    ) -> None:
        """Forward process output through data_callback set by PortManager."""
        if not chunk:
            return
        cb = self.data_callback
        if cb:
            try:
                ok = await cb(self.name, chunk, require_clients=require_clients)
                if ok:
                    return
            except Exception:
                self.logger.error("data_callback failed for %s", self.name, exc_info=True)
        else:
            if not self._queue_fallback_logged:
                self.logger.error(
                    "Command port %s: data_callback not set; dropping data",
                    self.name,
                )
                self._queue_fallback_logged = True

    def _schedule_notice_emit(self, payload: bytes) -> None:
        """Schedule emission of a notice chunk via the centralized path."""
        if not payload:
            return

        async def _do_emit():
            await self._emit_output_chunk(payload, require_clients=False)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.logger.error(
                "No running event loop available to emit notice for %s; dropping notice",
                self.name,
            )
            return
        loop.create_task(_do_emit())

    def _schedule_lifecycle_notice(self, kind: str, detail: str) -> None:
        """Broadcast a bracketed process lifecycle notice to attached clients.

        Same convention as the ``PROCESS_NOT_RUNNING`` notice: a
        ``[OpenMux:…]`` line through the centralized data path, so attached
        clients see it and no delivery happens when nobody is connected.
        Callers check ``client_count`` before calling this.
        """
        message = f"\r\n[OpenMux:{kind} {self._stopped_prefix()}{detail}]\r\n".encode()
        self._schedule_notice_emit(message)

    def on_client_count_changed(self, count: int):
        """Handle change in connected client count.

        When the first client connects, ensure buffering is active and, if
        the process is not running, enqueue a status notice. When the last
        client disconnects, optionally reset flags so a future notice will
        be sent if still stopped.

        Args:
            count: New number of connected stream clients.
        """
        old = self.client_count
        self.client_count = count
        self.logger.info("Client count changed for %s: %s -> %s", self.name, old, count)
        if old == 0 and count > 0:
            # Cancel any pending idle-stop since a client re-appeared
            try:
                if self._idle_stop_task and not self._idle_stop_task.done():
                    self._idle_stop_task.cancel()
            except Exception:
                # justification: idempotent task cancel
                pass
            # If configured for on-demand spawn, ensure the process is running now
            if self.spawn_on_demand and (not self.process_active):
                try:
                    # Start synchronously in background; errors are logged within start()
                    asyncio.create_task(self.start())
                except Exception:
                    self.logger.error("Failed to trigger on-demand start for %s", self.name, exc_info=True)
            if (not self.process_active) and not self._stopped_notice_sent:
                hint = "spawn" if getattr(self, "spawn_on_demand", False) else "respawn"
                notice = f"\r\n[OpenMux:PROCESS_NOT_RUNNING {self._stopped_prefix()} – press Enter to {hint}]\r\n".encode()
                self._stopped_notice_sent = True
                self._schedule_notice_emit(notice)
        elif old > 0 and count == 0:
            if not self.process_active:
                self._stopped_notice_sent = False
            # Schedule idle stop if configured
            if self.idle_timeout_sec and self.idle_timeout_sec > 0:
                # Guard: don't schedule multiple timers
                if self._idle_stop_task is None or self._idle_stop_task.done():

                    async def _idle_stop_after_delay():
                        try:
                            await asyncio.sleep(self.idle_timeout_sec)
                            # If still idle and process is active, stop it
                            if self.client_count == 0 and self.is_running:
                                self.logger.info(
                                    "Idle timeout (%ss) reached for %s; stopping process", self.idle_timeout_sec, self.name
                                )
                                try:
                                    await self.stop()
                                except Exception:
                                    self.logger.error("Error stopping %s after idle timeout", self.name, exc_info=True)
                        except asyncio.CancelledError:
                            # justification: task was cancelled on purpose; awaiting it observes the cancellation
                            pass
                        finally:
                            self._idle_stop_task = None

                    try:
                        self._idle_stop_task = asyncio.create_task(_idle_stop_after_delay())
                    except Exception:
                        self.logger.error("Failed to schedule idle-stop task for %s", self.name, exc_info=True)

    async def start(self) -> bool:
        """Spawn the configured process (if not already running).

        Returns:
            bool: True on successful spawn (or already running), False if
            process creation failed.
        """
        if self.is_running:
            return True
        try:
            self.state = PortState.CREATING
            self.logger.info("Starting command: %s", self.command)
            ok = await self._spawn_process()
            if not ok:
                self.state = PortState.DEGRADED
                return False
            self.state = PortState.ACTIVE
            self.is_running = True
            self._spawn_monitor_task()
            self.logger.info("Command port %s started successfully", self.name)
            return True
        except Exception as e:
            self.logger.error("Failed to start command port %s: %s", self.name, e, exc_info=True)
            self.state = PortState.DEGRADED
            return False

    async def _spawn_process(self) -> bool:
        """Internal helper to spawn (or respawn) the process.

        Sets up PTY or pipe-based subprocess, registers readers, prepares
        batching tasks, and updates runtime state flags.

        Returns:
            bool: True if the process was spawned and I/O initialized.
        """
        try:
            preexec_fn = self._build_preexec_fn()
            # Do not alter the user's command based on shell name.
            # If interactive flags are needed, they should be included in the configured command.

            # Reset previous
            if self._read_task:
                try:
                    self._read_task.cancel()
                except Exception:  # justification: cancelling stale read task; failure is non-fatal and next spawn proceeds
                    pass
                self._read_task = None
            if self._pty_master_fd is not None:
                if self._loop and self._pty_reader_added:
                    try:
                        self._loop.remove_reader(self._pty_master_fd)
                    except Exception:  # justification: reader may already be detached; safe to proceed
                        pass
                    self._pty_reader_added = False
                # Close even when the reader already detached itself on EOF,
                # so a die->Enter respawn cycle cannot leak the old master fd.
                try:
                    os.close(self._pty_master_fd)
                except OSError:  # justification: fd may already be closed; next spawn proceeds
                    pass
                self._pty_master_fd = None
                self._pty_reader_added = False

            # Build environment. issue #67: sanitizing is unconditional
            # (security posture); the clean_env key is removed. The env: block
            # merges extra/override values on top of the minimal allow-list.
            env: Dict[str, str] = {}
            for k in ("PATH", "HOME", "SHELL", "USER", "LANG", "LC_ALL"):
                v = os.environ.get(k)
                if v:
                    env[k] = v
            env.setdefault("TERM", "xterm")
            for bad in (
                "LC_TERMINAL",
                "TERM_PROGRAM",
                "TERM_PROGRAM_VERSION",
                "ITERM_SESSION_ID",
                "COLORTERM",
                "KITTY_INSTALLATION_DIR",
                "KITTY_LISTEN_ON",
                "KITTY_WINDOW_ID",
                "VTE_VERSION",
            ):
                env.pop(bad, None)
            if isinstance(self.env, dict):
                env.update(self.env)

            if self.use_pty:
                try:
                    master_fd, slave_fd = pty.openpty()
                    if self.shell:
                        self.process = await asyncio.create_subprocess_shell(
                            self.command,
                            stdin=slave_fd,
                            stdout=slave_fd,
                            stderr=slave_fd,
                            cwd=self.cwd,
                            env=env,
                            preexec_fn=preexec_fn,
                        )
                    else:
                        parts = shlex.split(self.command)
                        self.process = await asyncio.create_subprocess_exec(
                            *parts,
                            stdin=slave_fd,
                            stdout=slave_fd,
                            stderr=slave_fd,
                            cwd=self.cwd,
                            env=env,
                            preexec_fn=preexec_fn,
                        )
                    try:
                        os.close(slave_fd)
                    except OSError:  # justification: slave fd already closed by subprocess; safe to ignore
                        pass
                    self._pty_master_fd = master_fd
                except Exception as e:  # justification: PTY allocation may fail on platform; fallback to pipes acceptable
                    self.logger.error("Failed to allocate PTY for %s, falling back to pipes: %s", self.name, e, exc_info=True)
                    self.use_pty = False

            if not self.use_pty:
                if self.shell:
                    self.process = await asyncio.create_subprocess_shell(
                        self.command,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        cwd=self.cwd,
                        env=env,
                        preexec_fn=preexec_fn,
                    )
                else:
                    parts = shlex.split(self.command)
                    self.process = await asyncio.create_subprocess_exec(
                        *parts,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        cwd=self.cwd,
                        env=env,
                        preexec_fn=preexec_fn,
                    )

            # Readers/Writers
            stdin_stream = None if self.use_pty else (self.process.stdin if self.process else None)
            self._writer = CommandWriter(stdin_stream, self)

            if self.use_pty:
                try:
                    self._loop = asyncio.get_running_loop()
                    if self._pty_master_fd is not None:
                        try:
                            os.set_blocking(self._pty_master_fd, False)
                        except Exception:  # justification: optional non-blocking optimization; continue in blocking mode
                            pass
                        self._loop.add_reader(self._pty_master_fd, self._on_pty_read_ready)
                        self._pty_reader_added = True
                except Exception as e:
                    self.logger.error("Failed to register PTY reader for %s: %s", self.name, e, exc_info=True)
            else:
                self._read_task = asyncio.create_task(self._stdout_reader_task())

            self._stopped_notice_sent = False
            # Lifecycle notice (issue #63): tell attached clients the process
            # is (re)starting, e.g. an on-demand spawn or an auto-restart.
            if self.client_count > 0:
                self._schedule_lifecycle_notice("PROCESS_STARTED", "process started")
            self.process_active = True
            # Process is up; mark connected. Covers the on-demand first
            # spawn, the Enter-respawn path, and auto-restart respawns --
            # all of them route through here.
            self._set_connected(True)
            if self.use_pty:
                if self._output_flush_task is None or self._output_flush_task.done():
                    self._output_flush_task = asyncio.create_task(self._output_flush_buffer_loop())
            # Process is running again; clear any prior offline reason (issue #62).
            self._set_status_message("")
            return True
        except FileNotFoundError as e:
            self.logger.error("Error spawning process for %s: %s", self.name, e, exc_info=True)
            self._set_status_message(f"Process not found: {e}")
            # A port whose process cannot be (re)started is offline for the
            # UI; the reason above explains why.
            self._set_connected(False)
            return False
        except Exception as e:
            self.logger.error("Error spawning process for %s: %s", self.name, e, exc_info=True)
            self._set_status_message(f"Process spawn failed: {e}")
            # A port whose process cannot be (re)started is offline for the
            # UI; the reason above explains why.
            self._set_connected(False)
            return False

    def get_status_snapshot(self) -> Dict[str, Any]:
        """Return config details and the offline reason for port listings."""
        snapshot: Dict[str, Any] = {
            "serial_config": {
                "device": f"shell:{self.command}",
            }
        }
        if self.status_message:
            # Surfaces in /api/ports, the status page, and the console info
            # overlay (issue #62).
            snapshot["status_message"] = self.status_message
        return snapshot

    def _build_preexec_fn(self):
        if not self.use_pty:
            return None

        def _preexec():
            os.setsid()

        return _preexec

    def _on_pty_read_ready(self):
        """Low-level PTY readability callback registered with event loop.

        Drains available PTY data non-blockingly, applies optional terminal
        query interception and newline normalization, then buffers or queues
        data based on batching configuration and client presence.
        """
        if not self.is_running or self._pty_master_fd is None:
            return
        import time

        try:
            while True:
                t0 = time.perf_counter() if self.logger.isEnabledFor(logging.DEBUG) else None
                try:
                    data = os.read(self._pty_master_fd, 1024)
                except BlockingIOError:
                    break
                if t0 is not None:
                    t1 = time.perf_counter()
                    last = getattr(self, "_last_read_attempt_time", None)
                    now = t1
                    interval = (now - last) if last is not None else 0.0
                    self._last_read_attempt_time = now
                    self.logger.debug(
                        "PTY PROFILE: os.read took %.6fs, interval %.6fs, read %d bytes",
                        t1 - t0,
                        interval,
                        len(data) if data else 0,
                    )
                if not data:
                    if self._loop and self._pty_reader_added:
                        try:
                            self._loop.remove_reader(self._pty_master_fd)
                        except Exception:  # justification: already removed or loop closing; safe to ignore
                            pass
                        self._pty_reader_added = False
                    self.process_active = False
                    return
                try:
                    data = self._intercept_xtgettcap_queries(data)
                except Exception:  # justification: interception is best-effort; raw data still usable
                    pass
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")

                async def buffer_data(d: bytes):
                    async with self._output_buffer_lock:
                        self._output_buffer += d
                        self._output_flush_event.clear()
                        now2 = asyncio.get_event_loop().time()
                        self._last_data_time = now2
                        if not hasattr(self, "_first_data_time") or self._first_data_time is None:
                            self._first_data_time = now2
                        self._output_flush_event.set()

                asyncio.create_task(buffer_data(data))
        except OSError as e:
            if self._loop and self._pty_reader_added:
                try:
                    self._loop.remove_reader(self._pty_master_fd)
                except Exception:  # justification: remove_reader failure during OSError cleanup is non-critical
                    pass
                self._pty_reader_added = False
            self.logger.debug("PTY reader closed for %s: %s", self.name, e)
            self.process_active = False
        except Exception as e:
            self.logger.error("Error in PTY reader callback for %s: %s", self.name, e, exc_info=True)
            self.process_active = False

    async def _stdout_reader_task(self):
        """Coroutine to read stdout from a pipe-based subprocess.

        Mirrors PTY callback behavior for non-PTY mode, handling batching,
        terminal query interception, and queueing for connected clients.
        Terminates on EOF, cancellation, or error.
        """
        try:
            while self.is_running and self.process and self.process.stdout:
                try:
                    data = await self.process.stdout.read(1024)
                    if not data:
                        self.process_active = False
                        break
                    try:
                        data = self._intercept_xtgettcap_queries(data)
                    except Exception:  # justification: interception is best-effort; continuing with raw stdout
                        pass
                    data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                    await self._emit_output_chunk(data)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.logger.error("Error reading from command stdout %s: %s", self.name, e, exc_info=True)
                    self.process_active = False
                    break
        except Exception as e:
            self.logger.error("Command stdout reader task error for %s: %s", self.name, e, exc_info=True)

    async def _output_flush_buffer_loop(self):
        """Flush batched output according to size and timing thresholds.

        Implements three flush triggers: batch size reached, idle timeout, or
        force-flush interval exceeded since first buffered byte. Continues
        while the port and process remain active (batching is unconditional
        now, issue #67).
        """
        self._last_data_time = asyncio.get_event_loop().time()
        self._first_data_time = None
        while self.is_running and self.process_active:
            # Wait strategy: if no buffered data, block on event (no tight polling).
            # If there is buffered data, use short timeout to honor batch/force-flush thresholds.
            try:
                async with self._output_buffer_lock:
                    buffer_empty = len(self._output_buffer) == 0
                    if buffer_empty:
                        # Clear before waiting so the next producer wake-up is observed
                        self._output_flush_event.clear()
                if buffer_empty:
                    # Block until data arrives or periodic wake (1s) to re-check liveness flags
                    await asyncio.wait_for(self._output_flush_event.wait(), timeout=1.0)
                else:
                    await asyncio.wait_for(self._output_flush_event.wait(), timeout=self._output_batch_timeout)
            except asyncio.TimeoutError:
                # justification: expected wake-up; the buffer is checked every tick
                pass

            now = asyncio.get_event_loop().time()
            flush = False
            async with self._output_buffer_lock:
                buffer_len = len(self._output_buffer)
                if buffer_len >= self._output_batch_size:
                    flush = True
                elif self._first_data_time is not None and (now - self._first_data_time) >= self._output_force_flush_timeout:
                    flush = True
                elif now - self._last_data_time >= self._output_batch_timeout and buffer_len > 0:
                    flush = True
                if flush and buffer_len > 0:
                    to_send = bytes(self._output_buffer)
                    self._output_buffer.clear()
                    self._first_data_time = None
                else:
                    to_send = None
            if to_send:
                await self._emit_output_chunk(to_send)

    async def _monitor_loop(self):
        """Wait for process exit, report it, and leave the port resting/offline.

        One-shot by design (issue #67): automatic restart after exit is not
        supported. Drains the output batcher so clients see the process's
        final output, then sends the PROCESS_EXITED notice and sets the port
        state. A clean exit (code 0) is a resting state (CONFIGURED, still
        online); a non-zero exit marks the port DEGRADED/offline. A
        successful spawn (Enter respawn, on-demand spawn) starts a fresh
        monitor for the next exit.
        """
        try:
            if not self.process:
                return
            exit_code = await self.process.wait()
            self.process_active = False
            # A clean exit (code 0) means the process ran to completion as
            # expected. A non-zero exit (or signal kill, which produces a
            # negative code) marks the port degraded/offline until respawn.
            clean_exit = not exit_code
            # Wake the output batcher and drain whatever the process emitted
            # last (e.g. its final line), so clients see the trailing output
            # before any exit notice below.
            try:
                self._output_flush_event.set()
            except Exception:  # justification: batcher best-effort; no batcher is fine
                pass
            await self._drain_output_buffer()
            # Capture the exit reason (issue #62). Code 0 means a normal
            # shutdown and gets no message; non-zero is reported. The process
            # is dead and did not exit cleanly; surface it so the UI banner
            # shows immediately. Pressing Enter respawns and clears it.
            if not clean_exit:
                self._set_status_message(f"Process exited with code {exit_code}")
                self._set_connected(False)
            if not self.is_running:
                return
            self.logger.info("Process exited (code %s) for %s; not restarting (press Enter to respawn)", exit_code, self.name)
            if self.client_count > 0:
                self._schedule_lifecycle_notice(
                    "PROCESS_EXITED",
                    self._exit_notice_detail(exit_code),
                )
            self._stopped_notice_sent = False
            self.is_running = False
            self.state = PortState.CONFIGURED if clean_exit else PortState.DEGRADED
        except asyncio.CancelledError:
            # justification: monitor cancelled during stop(); nothing to clean up
            return
        except Exception as e:
            self.logger.error("Monitor loop error for %s: %s", self.name, e, exc_info=True)
            return
        self.logger.info("Monitor loop exiting for command port %s", self.name)

    def _exit_notice_detail(self, exit_code: Optional[int]) -> str:
        """Detail text for a terminal PROCESS_EXITED notice."""
        hint = "press Enter to spawn" if getattr(self, "spawn_on_demand", False) else "press Enter to respawn"
        return f"process exited (code {exit_code}) - {hint}"

    async def _drain_output_buffer(self) -> None:
        """Flush any residual batched output immediately (best-effort).

        Awaited when the process exits: the batcher loop may already be
        winding down, and clients must see the process's final output (e.g.
        its last line) before any subsequent exit notice. The drain retries
        briefly because the process's final bytes may still be in flight (the
        PTY EOF read can land a beat after ``process.wait()`` returns).
        """
        try:
            pending = None
            for _attempt in range(10):  # worst-case ~0.2s for in-flight final bytes
                async with self._output_buffer_lock:
                    if self._output_buffer:
                        pending = bytes(self._output_buffer)
                        self._output_buffer.clear()
                        self._first_data_time = None
                        break
                await asyncio.sleep(0.02)
            if pending:
                await self._emit_output_chunk(pending, require_clients=False)
        except Exception:  # justification: residual flush is best-effort; the data path must not break
            pass

    def _spawn_monitor_task(self) -> None:
        """Ensure the process monitor runs (exit report + output drain).

        The monitor is one-shot (issue #67) and is started for every spawn,
        including Enter-respawns and on-demand first spawns: it reports the
        terminal exit to attached clients and drains the output batcher at
        process end.
        """
        try:
            if self._monitor_task is None or self._monitor_task.done():
                self._monitor_task = asyncio.create_task(self._monitor_loop())
        except Exception:
            self.logger.error("Failed to start monitor task for %s", self.name, exc_info=True)

    async def stop(self) -> None:
        """Terminate the running process and cancel I/O tasks.

        Cleans up PTY readers, stdout tasks, writers, and resets runtime
        state to configured baseline, ready for a future restart/spawn.
        Safe to call multiple times.
        """
        if not self.is_running:
            return
        try:
            self.state = PortState.DESTROYING
            self.logger.info("Stopping command port %s", self.name)
            # Cancel any pending idle-stop task first
            try:
                if self._idle_stop_task and not self._idle_stop_task.done():
                    self._idle_stop_task.cancel()
            except Exception:
                # justification: idempotent task cancel
                pass
            self._idle_stop_task = None

            if self._monitor_task:
                self._monitor_task.cancel()
                self._monitor_task = None
            if self._pty_master_fd is not None:
                if self._loop and self._pty_reader_added:
                    try:
                        self._loop.remove_reader(self._pty_master_fd)
                    except Exception:  # justification: reader may already be detached; safe to proceed
                        pass
                    self._pty_reader_added = False
                # Close even when the reader already detached itself on EOF,
                # so a dead process cannot leak its master fd (issue #63).
                try:
                    os.close(self._pty_master_fd)
                except OSError:  # justification: fd may already be closed; ignore
                    pass
                self._pty_master_fd = None
            # Wake/stop output flusher if active
            try:
                if self._output_flush_event:
                    self._output_flush_event.set()
            except Exception:
                # justification: idempotent flusher wake
                pass
            if self._output_flush_task:
                self._output_flush_task.cancel()
                self._output_flush_task = None
            if self._read_task:
                self._read_task.cancel()
                self._read_task = None
            if self._writer and self.process and self.process.stdin:
                try:
                    self.process.stdin.close()
                    if hasattr(self.process.stdin, "wait_closed"):
                        await asyncio.wait_for(self.process.stdin.wait_closed(), timeout=1.0)
                except Exception:  # justification: stdin close errors ignored during shutdown cleanup
                    pass
            if self.process:
                try:
                    if self.use_pty:
                        try:
                            pgid = os.getpgid(self.process.pid)
                            os.killpg(pgid, signal.SIGTERM)
                        except Exception:  # justification: fallback to terminate if killpg fails
                            self.process.terminate()
                except Exception:  # justification: process may already have exited; termination best-effort
                    pass
            self.logger.info("Command port %s stopped", self.name)
        except Exception as e:
            self.logger.error("Error stopping command port %s: %s", self.name, e, exc_info=True)
        finally:
            # Lifecycle notice: warn clients still attached that the process
            # was stopped (idle timeout, manual stop, adapter teardown).
            _clients = self.client_count
            self._writer = None
            self.process = None
            self.client_count = 0
            # Allow the PROCESS_NOT_RUNNING notice to fire again on the next
            # attach; otherwise a stop() would eat the banner for good.
            self._stopped_notice_sent = False
            if _clients > 0:
                self._schedule_lifecycle_notice("PROCESS_STOPPED", "process was stopped")
            # An intentional stop is a resting state, not a failure; clear the
            # offline reason so the port does not show a stale exit code, and
            # mark it connected again so the UI does not keep a stale banner.
            self._set_status_message("")
            self._set_connected(True)
            self.is_running = False
            self.process_active = False
            self.state = PortState.CONFIGURED

    async def restart(self, force: bool = False) -> bool:
        """Manually restart the underlying process.

        Args:
            force: Attempt restart even if currently running (performs a stop first).

        Returns:
            bool: True if process running after restart attempt; False otherwise.
        """
        try:
            # Case 1: Force restart regardless of state -> full stop/start cycle
            if force and self.is_running:
                self.logger.info("Force restarting command port %s", self.name)
                await self.stop()
            # Case 2: Port previously started but process has exited (is_running true, process_active false)
            if self.is_running and not self.process_active:
                self.logger.info("Respawning exited process for command port %s", self.name)
                ok = await self._spawn_process()
                if ok:
                    self.state = PortState.ACTIVE
                    self.is_running = True
                    self.process_active = True
                    self._spawn_monitor_task()
                    return True
                self.logger.error("Respawn failed for command port %s", self.name)
                return False
            # Case 3: Port fully stopped (not running)
            if not self.is_running:
                self.logger.info("Manual restart requested for stopped command port %s", self.name)
                started = await self.start()
                if not started:
                    self.logger.error("Manual restart failed for %s", self.name)
                return started
            # Case 4: Already running and active and no force flag
            self.logger.info("Restart skipped; port %s already running and active", self.name)
            return True
        except Exception as e:
            self.logger.error("Error restarting command port %s: %s", self.name, e, exc_info=True)
            return False

    def _stopped_prefix(self) -> str:
        """Return standardized prefix for stopped status messages.

        Uses the server identity (``server.id``, else system hostname —
        see the shared resolver in ``openmux.common.identity``) plus the
        port name. Generic adapter names are filtered by the caller.

        Returns:
            str: Formatted prefix including trailing space.
        """
        try:
            # server.id is the sole identity key (ticket #74); the shared
            # resolver also covers the hostname fallback.
            cfg_obj = None
            try:
                cfg_mgr = getattr(getattr(self.adapter, "main_port_manager", None), "config_manager", None)
                if cfg_mgr:
                    # Ensure config is loaded
                    cfg_obj = getattr(cfg_mgr, "config", None)
                    if cfg_obj is None:
                        try:
                            cfg_obj = cfg_mgr.load_config()
                        except Exception:  # justification: prefix derivation is best-effort; fall back to hostname below
                            cfg_obj = None
            except Exception:  # justification: prefix derivation is best-effort; fall back to hostname below
                cfg_obj = None
            server_id = None
            if isinstance(cfg_obj, dict):
                server_id = get_server_id(cfg_obj.get("server"))
            if not server_id:
                try:
                    server_id = socket.gethostname()
                except Exception:  # justification: prefix derivation is best-effort; port name still renders
                    server_id = ""
            # simplify any path-like id to last segment
            if "/" in server_id:
                server_id = server_id.rsplit("/", 1)[-1]
            if not server_id:
                return f"{self.name} "
            return f"{server_id}/{self.name} "
        except Exception:  # justification: prefix derivation best-effort; fallback to port name
            return f"{self.name} "

    # Removed previously unused get_reader / get_writer helpers (direct attribute access sufficient)

    def _intercept_xtgettcap_queries(self, data: bytes) -> bytes:
        """Intercept XTGETTCAP termcap queries and emit responses.

        Args:
            data: Bytes read from PTY or stdout to inspect.

        Returns:
            bytes: Input data with XTGETTCAP sequences removed. Responses are
            written back to the PTY master when available.
        """
        if not data:
            return data
        out = bytearray()
        i = 0
        start_seq = b"\x1bP+q"
        end_seq = b"\x1b\\"
        while True:
            j = data.find(start_seq, i)
            if j == -1:
                out += data[i:]
                break
            out += data[i:j]
            k = data.find(end_seq, j)
            if k == -1:
                out += data[j:]
                break
            payload = data[j + len(start_seq) : k]
            resp = b"\x1bP0+r" + payload + b"\x1b\\"
            try:
                if self._pty_master_fd is not None:
                    os.write(self._pty_master_fd, resp)
            except Exception:  # justification: XTGETTCAP response write optional
                pass
            i = k + len(end_seq)
        return bytes(out)


class CommandWriter:
    """Buffered / batched writer for a command port.

    Normalizes pipe input to LF when ``normalize_newlines`` is set (PTY input
    passes through unchanged; issue #67 removed the ``pty_enter_mode`` knob
    and client-side local echo). Input is always batched at fixed thresholds
    to reduce write system call frequency for high-chattiness clients. Also
    implements the convenience behavior that a lone newline sent to a stopped
    (but previously started) process will attempt a respawn and then deliver
    the newline to prompt the new shell/program.

    Args:
        stdin_stream: The process stdin stream (``StreamWriter`` like) when
            using pipe-based execution; ``None`` when under PTY mode.
        port: Parent ``CommandPort`` instance.
    """

    def __init__(self, stdin_stream, port: CommandPort):
        self.stdin_stream = stdin_stream
        self.port = port
        self.logger = port.logger
        # Batching config (server <- client writes). issue #67: unconditional
        # at fixed thresholds; the enable_batching/batch_size/batch_timeout
        # keys are removed.
        self._batch_size = 1024
        self._batch_timeout = 0.002  # 2ms
        # Buffer and flush state
        self._write_buffer = bytearray()
        self._write_buffer_lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._flush_event = asyncio.Event()

    async def write(self, data: bytes) -> None:
        """Queue or immediately write input data to the process.

        Implements input batching if enabled; otherwise writes directly.
        If the underlying process has exited, a single newline (CR, LF or
        CRLF) attempt triggers a non-forced restart. Failed respawns cause
        a standardized stopped notice to be enqueued (once) for clients.

        Args:
            data: Raw bytes provided by a client session.
        """
        if not self.stdin_stream and not getattr(self.port, "use_pty", False):
            return
        if not self.port.process_active:
            # Allow pressing Enter (CR / LF / CRLF) to respawn a dead
            # process. `_try_newline_respawn` shares this with the no-writer
            # `write_data` path and emits the one-shot stopped notice for
            # any other input.
            if await self.port._try_newline_respawn(data):
                # Process is back; write the newline to deliver a prompt.
                # Update stdin_stream reference (a respawn spawns a new one)
                if not getattr(self.port, "use_pty", False):
                    self.stdin_stream = self.port.process.stdin if self.port.process else None
                await self._write_direct(data)
            return
        # Batching mode (issue #67: unconditional at fixed thresholds)
        async with self._write_buffer_lock:
            self._write_buffer += data
            if len(self._write_buffer) >= self._batch_size:
                self._flush_event.set()
        # Start flush task if not running
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_buffer_loop())

    async def _write_direct(self, data: bytes) -> None:
        """Perform an immediate write of ``data`` honoring normalization.

        Normalizes pipe input to LF when ``normalize_newlines`` is set (PTY
        input passes through unchanged); writes to the relevant descriptor and
        suppresses all exceptions to avoid propagating transient I/O failures
        upstream.

        Args:
            data: Bytes to write.
        """
        try:
            if getattr(self.port, "normalize_newlines", False) and data and not getattr(self.port, "use_pty", False):
                # For pipe-based processes, normalize to LF. PTY input is left
                # as-is (issue #67: pty_enter_mode removed) since the terminal
                # driver handles newline translation for the program.
                _orig = data
                data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                if self.logger.isEnabledFor(logging.DEBUG) and (
                    b"\r" in _orig or b"\n" in _orig or b"\r" in data or b"\n" in data
                ):
                    self.logger.debug("Writer newline map (pipe): in=%r out=%r", _orig, data)
            if getattr(self.port, "use_pty", False) and self.port._pty_master_fd is not None:
                try:
                    os.write(self.port._pty_master_fd, data)
                except Exception as e:
                    self.logger.error("PTY write error for %s: %s", self.port.name, e, exc_info=True)
            else:
                if self.stdin_stream is not None:
                    self.stdin_stream.write(data)
                    try:
                        await self.stdin_stream.drain()
                    except Exception:  # justification: stdin drain failure non-fatal; writer continues or process will exit
                        pass
        except Exception as e:
            self.logger.error("Error writing to command %s: %s", self.port.name, e, exc_info=True)

    async def _flush_buffer_loop(self):
        """Background loop to flush batched input.

        Waits on an event or timeout; on trigger drains current buffer and
        sends it using ``_write_direct``. Exits when buffer becomes empty and
        no new data arrives before the next timeout.
        """
        while True:
            try:
                await asyncio.wait_for(self._flush_event.wait(), timeout=self._batch_timeout)
            except asyncio.TimeoutError:
                # justification: expected wake-up; the buffer is checked every tick
                pass  # Timeout reached, flush whatever is in the buffer
            self._flush_event.clear()
            async with self._write_buffer_lock:
                if not self._write_buffer:
                    break  # Nothing to flush, exit
                to_send = bytes(self._write_buffer)
                self._write_buffer.clear()
            await self._write_direct(to_send)
            # If buffer is empty after flush, exit loop
            async with self._write_buffer_lock:
                if not self._write_buffer:
                    break


class CommandAdapter(BaseGenericAdapter):  # noqa: Vulture
    """Unified command adapter providing external command execution ports.

    Creates and manages multiple command execution "ports" each wrapping a
    spawned process (optionally under a PTY) with buffered asynchronous I/O,
    newline normalization, always-on I/O batching, and terminal query
    interception. Processes are not restarted automatically after exit;
    press Enter in the console to respawn (issue #67).
    """

    def __init__(self, plugin_name: str, config: Dict[str, Any]):
        super().__init__(plugin_name, config)
        self.ports: Dict[str, CommandPort] = {}
        self.logger = logging.getLogger(f"openmux.adapter.command.{plugin_name}")
        self.security_policy = None

    def get_adapter_type(self) -> str:
        """Return adapter type for security policy and factory lookup."""
        return "command"

    def get_capabilities(self) -> Set[AdapterCapability]:
        """Return the capability set implemented by this adapter.

        Returns:
            Set[AdapterCapability]: Provides ports with bidirectional data.
        """
        return {
            AdapterCapability.PROVIDES_PORTS,
            AdapterCapability.BIDIRECTIONAL_DATA,
        }

    def set_security_policy(self, policy) -> None:
        self.security_policy = policy

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> bool:
        """Validate adapter configuration structure.

        Expects key ``command_ports`` containing a list of port definitions;
        each must include ``name`` and ``command`` fields.

        Args:
            config: Raw adapter-specific configuration mapping.

        Returns:
            bool: True if structurally valid, else False.
        """
        command_ports = config.get("command_ports", [])
        if not isinstance(command_ports, list):
            return False
        for port_config in command_ports:
            if not isinstance(port_config, dict):
                return False
            if "name" not in port_config or "command" not in port_config:
                return False
            # Write-slot capacity (issue #59): mode, legacy int, or unset. Any
            # other value is a hard error so typos fail fast at load time.
            if "max_read_write_users" in port_config:
                try:
                    parse_write_mode(port_config["max_read_write_users"])
                except InvalidWriteMode:
                    return False
        return True

    def get_port_configurations(self) -> Dict[str, Dict[str, Any]]:
        """Return mapping of configured command port definitions.

        Returns:
            Dict[str, Dict[str, Any]]: Keyed by port name with raw config dicts.
        """
        port_configs: Dict[str, Dict[str, Any]] = {}
        self.logger.debug("Getting port configurations from config: %s", self.config)
        command_ports = self.config.get("command_ports", [])
        self.logger.debug("Found %s command port configurations", len(command_ports))
        for port_config in command_ports:
            port_name = port_config["name"]
            port_configs[port_name] = port_config
            self.logger.debug(f"Configured command port: {port_name} -> {port_config.get('command', 'N/A')}")
        return port_configs

    async def start(self) -> bool:
        """Start adapter by creating all configured command ports.

        Returns:
            bool: True if startup succeeded (may be zero ports), False on error.
        """
        try:
            success = await self.load_configured_ports()
            if success:
                self.is_running = True
                self.logger.info("Command adapter %s started with %s ports", self.name, len(self.ports))
            return success
        except Exception as e:
            self.logger.error("Error starting command adapter %s: %s", self.name, e, exc_info=True)
            return False

    async def create_port(self, port_name: str, config: Dict[str, Any]) -> Optional[Any]:
        """Create and start a single command port instance.

        Args:
            port_name: Logical name of the port.
            config: Configuration mapping for the process.

        Returns:
            CommandPort | None: Created port on success; None on failure.
        """
        try:
            command_port = CommandPort(port_name, config, self)
            # Start immediately unless configured for on-demand spawn
            if getattr(command_port, "spawn_on_demand", False):
                # Do not spawn the process yet; mark as configured
                self.ports[port_name] = command_port
                # Register with the main PortManager so the port is discoverable immediately
                try:
                    if self.main_port_manager:
                        await self.main_port_manager.register_unified_port(port_name, command_port, self)
                except Exception:
                    self.logger.warning("Failed to register unified command port %s", port_name)
                self.logger.info("Created command port (on-demand): %s (will spawn on first client attach)", port_name)
                return command_port
            else:
                if await command_port.start():
                    self.ports[port_name] = command_port
                    # Register with the main PortManager
                    try:
                        if self.main_port_manager:
                            await self.main_port_manager.register_unified_port(port_name, command_port, self)
                    except Exception:
                        self.logger.warning("Failed to register unified command port %s", port_name)
                    self.logger.info("Created command port: %s", port_name)
                    return command_port
                self.logger.error("Failed to start command port: %s", port_name)
                return None
        except Exception as e:
            self.logger.error("Error creating command port %s: %s", port_name, e, exc_info=True)
            return None

    async def destroy_port(self, port_name: str) -> None:
        """Stop and remove a command port.

        Missing ports are ignored; errors during stop are logged.

        Args:
            port_name: Name of the port to destroy.
        """
        if port_name in self.ports:
            try:
                command_port = self.ports[port_name]
                # Unregister from the main PortManager first to stop broadcasts and remove from listings
                try:
                    if self.main_port_manager:
                        await self.main_port_manager.unregister_unified_port(port_name)
                except Exception:
                    self.logger.warning("Failed to unregister unified command port %s", port_name)
                await command_port.stop()
                del self.ports[port_name]
                self.logger.info("Destroyed command port: %s", port_name)
            except Exception as e:
                self.logger.error("Error destroying command port %s: %s", port_name, e, exc_info=True)

    async def stop(self) -> None:
        """Stop all command ports and mark adapter not running.

        Iterates each managed port with a bounded timeout. Errors are logged
        but do not abort remaining stops.
        """
        try:
            self.logger.info("Stopping command adapter %s with %s ports", self.name, len(self.ports))
            for port_name in list(self.ports.keys()):
                try:
                    self.logger.debug("Stopping command port %s", port_name)
                    await asyncio.wait_for(self.destroy_port(port_name), timeout=2.5)
                    self.logger.debug("Stopped command port %s", port_name)
                except asyncio.TimeoutError:
                    self.logger.error("Timeout stopping command port %s", port_name)
                except Exception as e:
                    self.logger.error("Error stopping command port %s: %s", port_name, e, exc_info=True)
            self.is_running = False
            self.logger.info("Command adapter %s stopped", self.name)
        except Exception as e:
            self.logger.error("Error stopping command adapter %s: %s", self.name, e, exc_info=True)

    # --- Live configuration reconciliation ---
    async def reconcile_ports(self, new_config: Any) -> Dict[str, Any]:
        """Incrementally reconcile command ports.

        Args:
            new_config: Dict with key 'command_ports' as list, or direct list.

        Returns:
            Summary dict: {added, removed, updated, unchanged}.
        """
        # Normalize
        items: List[Dict[str, Any]] = []  # type: ignore[name-defined]
        if isinstance(new_config, dict) and isinstance(new_config.get("command_ports"), list):
            items = list(new_config["command_ports"])  # shallow copy
        elif isinstance(new_config, list):
            items = list(new_config)
        else:
            items = []

        new_by_name: Dict[str, Dict[str, Any]] = {}
        for p in items:
            if isinstance(p, dict) and p.get("name"):
                new_by_name[str(p["name"])] = p

        old_names = set(self.ports.keys())
        new_names = set(new_by_name.keys())
        removed = sorted(old_names - new_names)
        added = sorted(new_names - old_names)
        common = sorted(old_names & new_names)

        def _material_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
            # Apply the same defaults as CommandPort.__init__ so comparison is
            # apples-to-apples. Silent normalization (wire_to_mode) on both
            # sides: the load path already logged legacy ints (issue #59).
            _interactive = bool(cfg.get("interactive", False))
            mru = wire_to_mode(cfg.get("max_read_write_users", 1))
            return {
                "command": cfg.get("command", ""),
                "shell": bool(cfg.get("shell", False)),
                "cwd": cfg.get("cwd"),
                "env": cfg.get("env"),
                "max_read_write_users": mru,
                "interactive": _interactive,
                "scrollback_size": int(cfg.get("scrollback_size", 0)),
            }

        updated: List[str] = []  # type: ignore[name-defined]
        unchanged: List[str] = []  # type: ignore[name-defined]
        for n in common:
            port = self.ports.get(n)
            old_cfg: Dict[str, Any] = {}
            if port is not None:
                try:
                    old_cfg = {
                        "command": getattr(port, "command", None),
                        "shell": getattr(port, "shell", None),
                        "cwd": getattr(port, "cwd", None),
                        "env": getattr(port, "env", None),
                        "max_read_write_users": wire_to_mode(getattr(port, "max_read_write_users", None)),
                        "interactive": getattr(port, "interactive", None),
                        "scrollback_size": getattr(port, "scrollback_size", None),
                    }
                except Exception:
                    old_cfg = {}
            _new_mat = _material_cfg(new_by_name[n])
            _untracked = set(_new_mat.keys()) - set(old_cfg.keys())
            if _untracked:
                self.logger.error(
                    f"[BUG] reconcile_ports: _material_cfg has keys not tracked in old_cfg: "
                    f"{sorted(_untracked)} — add them to old_cfg to ensure changes are detected."
                )
            if old_cfg == _new_mat:
                # Update description in-place if provided
                try:
                    desc = new_by_name[n].get("description")
                    if isinstance(desc, str) and desc:
                        setattr(port, "description", desc)
                except Exception:
                    # justification: in-place live update; the next reload retries
                    pass
                # In-place update for the RW/RO access-group lists. These are
                # deliberately NOT in _material_cfg, so a groups-only change
                # does not recreate the port; new lists apply on next connect.
                try:
                    new_rw = list(new_by_name[n].get("read_write_groups") or [])
                    new_ro = list(new_by_name[n].get("read_only_groups") or [])
                    if list(getattr(port, "read_write_groups", None) or []) != new_rw:
                        setattr(port, "read_write_groups", new_rw)
                    if list(getattr(port, "read_only_groups", None) or []) != new_ro:
                        setattr(port, "read_only_groups", new_ro)
                    # PDU power feeds (power section refs), same in-place
                    # treatment: no port recreate, power adapter re-reads.
                    new_power = [str(r) for r in (new_by_name[n].get("power") or [])]
                    if list(getattr(port, "power", None) or []) != new_power:
                        setattr(port, "power", new_power)
                except Exception:
                    # justification: in-place live update; the next reload retries
                    pass
                # In-place update of the process-lifecycle flags. These change
                # a port that stays in service (e.g. a live client session), so
                # updating the attributes in place avoids a destroy+recreate that
                # would drop connected clients. The idle timer reads
                # idle_timeout_sec lazily at fire time and spawn_on_demand is
                # read on each 0->1 connect, so the new values take effect from
                # the next client transition. Not in _material_cfg deliberately:
                # they must not force a recreate.
                try:
                    if "spawn_on_demand" in new_by_name[n]:
                        new_on_demand = bool(new_by_name[n].get("spawn_on_demand", False))
                        if isinstance(getattr(port, "spawn_on_demand", None), bool) and port.spawn_on_demand != new_on_demand:
                            port.spawn_on_demand = new_on_demand
                    if "idle_timeout_sec" in new_by_name[n]:
                        new_idle = float(new_by_name[n].get("idle_timeout_sec") or 0)
                        if (
                            isinstance(getattr(port, "idle_timeout_sec", None), (int, float))
                            and port.idle_timeout_sec != new_idle
                        ):
                            port.idle_timeout_sec = new_idle
                except Exception:
                    # justification: in-place live update; the next reload retries
                    pass
                unchanged.append(n)
            else:
                updated.append(n)

        # Remove updated/removed
        for n in removed + updated:
            try:
                await self.destroy_port(n)
            except Exception as e:
                self.logger.error("Failed to destroy command port %s: %s", n, e, exc_info=True)

        # Create added/updated
        for n in added + updated:
            cfg = new_by_name.get(n)
            if not cfg:
                continue
            try:
                await self.create_port(n, cfg)
            except Exception as e:
                self.logger.error("Failed to create command port %s: %s", n, e, exc_info=True)

        # Update adapter config snapshot
        try:
            self.config["command_ports"] = [new_by_name[k] for k in sorted(new_by_name.keys())]
        except Exception:
            # justification: optional snapshot; the authoritative config is on disk
            pass

        summary = {"added": added, "removed": removed, "updated": updated, "unchanged": unchanged}
        self.logger.info(
            "Command adapter %s reconcile: +%s ~%s -%s unchanged=%s",
            self.name,
            len(added),
            len(updated),
            len(removed),
            len(unchanged),
        )
        return summary

    async def write_to_port(self, port_name: str, data: bytes) -> int:
        """Write bytes to a specific command port.

        Args:
            port_name: Logical name of the target port.
            data: Bytes to send to the process.

        Returns:
            Number of bytes accepted (0 if port missing or write failed).
        """
        port = self.ports.get(port_name)
        if not port:
            self.logger.error("Command port %s not found", port_name)
            return 0
        try:
            return await port.write_data(data)
        except Exception as e:
            self.logger.error("Error writing to command port %s: %s", port_name, e, exc_info=True)
            return 0

    def get_status_info(self) -> Dict[str, Any]:
        """Return adapter summary suitable for status endpoints.

        Includes aggregate counts and per-port feature flags.

        Returns:
            Dict[str, Any]: Structured status information.
        """
        try:
            return {
                "type": "Command",
                "status": "running" if self.is_running else "stopped",
                "ports": f"{len(self.ports)} configured",
                "details": {
                    "adapter_name": self.name,
                    "total_ports": len(self.ports),
                    "active_ports": len([p for p in self.ports.values() if p.is_running]),
                    "port_list": [
                        {
                            "name": name,
                            "state": port.state.value,
                            "is_running": port.is_running,
                            "command": port.command,
                            "description": port.description,
                        }
                        for name, port in self.ports.items()
                    ],
                    "features": {
                        name: {
                            "interactive": getattr(port, "interactive", False),
                        }
                        for name, port in self.ports.items()
                    },
                },
            }
        except Exception:  # justification: status snapshot best-effort; failure returns minimal stopped summary
            return {
                "type": "Command",
                "status": "stopped",
                "ports": "0 configured",
            }
