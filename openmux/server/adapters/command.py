"""
Unified Command Adapter for OpenMux

Provides command execution ports that run external processes.
"""

import asyncio
import logging
import os
import pwd
import grp
import pty
import shlex
import signal
import socket
import termios
import tty
from typing import Any, Dict, List, Optional, Set

from .base_adapter import AdapterCapability, BaseGenericAdapter
from .lifecycle import PortState
from ..security_policy import CommandPrivilegePolicy


class CommandPort:
    """Command execution port wrapping a spawned process (optionally PTY-backed).

    Handles process lifecycle (spawn, monitor, optional auto-restart), I/O
    buffering, newline normalization, terminal capability interception, and
    batching of outbound and inbound data for connected clients.

    Contract reference: docs/ADAPTER_PORT_CONTRACT.md

    Configuration Keys (selected):
        command (str): Command string to execute.
        shell (bool): Run under shell via ``create_subprocess_shell``.
        cwd (str): Working directory for the process.
        env (dict): Extra/override environment variables.
        interactive (bool): Enable interactive/PTY behavior defaults.
        always_buffer (bool): Keep buffering output even with zero clients.
        normalize_newlines (bool): Normalize newline sequences on input.
        local_echo (bool): Echo writes back into the output buffer.
        use_pty (bool): Allocate PTY; enables richer terminal behavior.
        output_crlf (bool): Convert outbound newlines to CRLF.
        clean_env (bool): Start from a minimal sanitized environment.
        intercept_term_queries (bool): Intercept XTGETTCAP queries.
        pty_force_raw (bool): Force raw mode on the PTY slave.
        pty_enter_mode (str): Input newline mapping: none|cr|lf|crlf.
        output_batch_size (int): Max buffered bytes before flush.
        output_batch_timeout (float): Idle timeout (s) before flush.
        output_force_flush_timeout (float): Hard cap flush interval (s).
        enable_output_batching (bool): Toggle server->client batching.
        auto_restart (bool): Enable automatic restart after exit.
        restart_delay (float): Base delay before first restart.
        max_restarts (int): Max restart attempts (0 = unlimited or until failure policy).
        restart_backoff (float): Multiplicative backoff factor.

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
        privilege_policy: Optional[CommandPrivilegePolicy] = None,
    ):
        self.name = name
        self.config = config
        self.adapter = adapter
        self._privilege_policy = privilege_policy
        self.state = PortState.CONFIGURED
        self.logger = logging.getLogger(f"openmux.adapter.command.{name}")

        self.command = config.get("command", "")
        self.shell = config.get("shell", False)
        self.cwd = config.get("cwd")
        self.env = config.get("env")
        self.description = config.get("description", f"Command: {self.command}")
        self.max_read_write_users = config.get("max_read_write_users", 1)

        # Behaviour flags
        self.interactive = config.get("interactive", False)
        self.always_buffer = config.get("always_buffer", self.interactive)
        self.normalize_newlines = config.get("normalize_newlines", self.interactive)
        self.local_echo = config.get("local_echo", False)
        self.use_pty = config.get("use_pty", self.interactive)
        self.output_crlf = config.get("output_crlf", True)
        self.clean_env = config.get("clean_env", True)
        self.intercept_term_queries = config.get("intercept_term_queries", True)
        # Optional: force PTY raw mode (generally not needed; most TUIs set it themselves)
        self.pty_force_raw = bool(config.get("pty_force_raw", False))
        # Optional: control how Enter/newlines are mapped for PTY input: none|cr|lf|crlf
        self.pty_enter_mode = config.get("pty_enter_mode", "none")

        # Process lifecycle policy
        # spawn_mode may be one of: "shared_eager" (default), "shared_on_demand".
        # For backward compatibility, also honor boolean flag spawn_on_demand.
        spawn_mode = str(config.get("spawn_mode", "")).strip().lower()
        self.spawn_on_demand: bool = bool(
            config.get("spawn_on_demand", False) or spawn_mode == "shared_on_demand"
        )
        # Idle stop: when last client disconnects, stop the process after this many seconds (>0).
        # 0 or missing => never auto-stop on idle.
        try:
            self.idle_timeout_sec: float = float(config.get("idle_timeout_sec", 0) or 0)
        except Exception:
            self.idle_timeout_sec = 0.0
        self._idle_stop_task: Optional[asyncio.Task] = None

        # Output batching config (server -> client)
        self._output_batch_size = config.get("output_batch_size", 1024)
        self._output_batch_timeout = config.get("output_batch_timeout", 0.002)
        self._output_force_flush_timeout = config.get("output_force_flush_timeout", 1.0)
        self._output_batching_enabled = config.get("enable_output_batching", True)
        self._output_buffer = bytearray()
        self._output_buffer_lock = asyncio.Lock()
        self._output_flush_task: Optional[asyncio.Task] = None
        self._output_flush_event = asyncio.Event()

        # Runtime refs
        self._pty_master_fd: Optional[int] = None
        self._pty_reader_added = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.process: Optional[asyncio.subprocess.Process] = None
        self._reader = None
        self._writer = None
        self.is_running = False
        self.process_active = False
        self.client_count = 0
        self.data_queue: Optional[asyncio.Queue] = None
        # Contract: port instance must expose data_callback for upstream routing
        # Signature: Callable[[str, bytes], Awaitable|None]; adapter wires it if needed.
        self.data_callback = None
        self._read_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None
        # Queue handling hints consumed by PortManager/legacy wrappers
        self.drop_oldest_on_full = True
        self._queue_fallback_logged = False

        # Restart config
        self.auto_restart = bool(config.get("auto_restart", False))
        self.restart_delay = float(config.get("restart_delay", 1.0))
        self.max_restarts = int(config.get("max_restarts", 0))
        self.restart_backoff = float(config.get("restart_backoff", 1.0))
        self.restart_count = 0

        self._stopped_notice_sent = False

        if self.always_buffer:
            self.data_queue = asyncio.Queue(maxsize=100)
            self.logger.info(f"Pre-buffering enabled for command port {self.name} (always_buffer={self.always_buffer})")

        if not self.command:
            raise ValueError(f"Command port {name} requires 'command' configuration")

    async def write_data(self, data: bytes) -> int:
        """Standardized write API: return number of bytes accepted.

        Uses internal writer if initialized; returns 0 if unavailable.
        """
        writer = getattr(self, "_writer", None)
        if not writer or not data:
            return 0
        try:
            await writer.write(data)
            return len(data)
        except Exception:  # justification: transient write failure; upstream caller treats 0 as backpressure signal
            return 0

    async def read_data(self, timeout: float = 0.0) -> bytes:
        """Standardized read API to retrieve buffered process output.

        Args:
            timeout: Seconds to wait for data; 0 for non-blocking.

        Returns:
            bytes: Next available chunk or b"" on timeout/no data.
        """
        # Stopped notice logic mirrors CommandReader.read()
        if (not self.process_active or self.state != PortState.ACTIVE) and not self._stopped_notice_sent:
            self._stopped_notice_sent = True
            hint = "spawn" if getattr(self, "spawn_on_demand", False) else "respawn"
            return f"\r\n[OpenMux:PROCESS_NOT_RUNNING {self._stopped_prefix()} – press Enter to {hint}]\r\n".encode()
        if not self.data_queue:
            return b""
        try:
            if timeout and timeout > 0:
                return await asyncio.wait_for(self.data_queue.get(), timeout=timeout)
            # Non-blocking path
            return self.data_queue.get_nowait()
        except asyncio.QueueEmpty:
            return b""
        except asyncio.TimeoutError:
            return b""

    def _port_manager(self):
        """Return the bound PortManager instance if available."""
        return getattr(self.adapter, "main_port_manager", None)

    async def _emit_output_chunk(
        self,
        chunk: bytes,
        *,
        require_clients: Optional[bool] = None,
        drop_oldest: bool = True,
    ) -> None:
        """Forward process output through the centralized logging path."""
        if not chunk:
            return
        pm = self._port_manager()
        require_clients_flag = (not self.always_buffer) if require_clients is None else require_clients
        if pm:
            try:
                ok = await pm.send_data_from_unified_port(
                    self.name,
                    chunk,
                    require_clients=require_clients_flag,
                    drop_oldest=drop_oldest,
                )
                if ok:
                    return
            except Exception:
                self.logger.error(f"PortManager forwarding failed for {self.name}", exc_info=True)
        # Fallback for early startup/tests when PortManager isn't wired
        if not self._queue_fallback_logged:
            self.logger.error(
                "Command port %s falling back to local queue; PortManager missing or send failure",
                self.name,
            )
            self._queue_fallback_logged = True
        # PortManager is the required data path; absence is an error, not a buffer opportunity.
        # Drop the chunk to avoid unbounded state accumulation.

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
        self.logger.info(f"Client count changed for {self.name}: {old} -> {count}")
        if old == 0 and count > 0:
            if self.data_queue is None:
                self.data_queue = asyncio.Queue(maxsize=100)
            # Cancel any pending idle-stop since a client re-appeared
            try:
                if self._idle_stop_task and not self._idle_stop_task.done():
                    self._idle_stop_task.cancel()
            except Exception:
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
                notice = (
                    f"\r\n[OpenMux:PROCESS_NOT_RUNNING {self._stopped_prefix()} – press Enter to {hint}]\r\n".encode()
                )
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
                                    f"Idle timeout ({self.idle_timeout_sec}s) reached for {self.name}; stopping process"
                                )
                                try:
                                    await self.stop()
                                except Exception:
                                    self.logger.error("Error stopping %s after idle timeout", self.name, exc_info=True)
                        except asyncio.CancelledError:
                            # New client connected or adapter shutting down; ignore
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
            self.logger.info(f"Starting command: {self.command}")
            ok = await self._spawn_process()
            if not ok:
                self.state = PortState.DEGRADED
                return False
            self.state = PortState.ACTIVE
            self.is_running = True
            self.process_active = True
            if self.auto_restart and not self._monitor_task:
                self._monitor_task = asyncio.create_task(self._monitor_loop())
            self.logger.info(f"Command port {self.name} started successfully")
            return True
        except Exception as e:
            self.logger.error(f"Failed to start command port {self.name}: {e}", exc_info=True)
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
            drop_spec = self._resolve_privilege_drop_spec()
            preexec_fn = self._build_preexec_fn(drop_spec)
            # Do not alter the user's command based on shell name.
            # If interactive flags are needed, they should be included in the configured command.

            # Reset previous
            if self._read_task:
                try:
                    self._read_task.cancel()
                except Exception:  # justification: cancelling stale read task; failure is non-fatal and next spawn proceeds
                    pass
                self._read_task = None
            if self._pty_master_fd is not None and self._loop and self._pty_reader_added:
                try:
                    self._loop.remove_reader(self._pty_master_fd)
                except Exception:  # justification: best-effort removal from loop; safe to proceed
                    pass
                try:
                    os.close(self._pty_master_fd)
                except Exception:  # justification: fd may already be closed; ignore
                    pass
                self._pty_master_fd = None
                self._pty_reader_added = False

            # Build environment
            if self.clean_env:
                env: Dict[str, str] = {}
                for k in ("PATH", "HOME", "SHELL", "USER", "LANG", "LC_ALL"):
                    v = os.environ.get(k)
                    if v:
                        env[k] = v
                env.setdefault("TERM", "xterm")
            else:
                env = dict(self.env or os.environ)
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
            if drop_spec:
                user_name = drop_spec.get("user_name")
                if user_name:
                    env.setdefault("USER", user_name)
                    env.setdefault("LOGNAME", user_name)
                home_dir = drop_spec.get("home")
                if home_dir:
                    env.setdefault("HOME", home_dir)

            if self.use_pty:
                try:
                    master_fd, slave_fd = pty.openpty()
                    try:
                        if self.pty_force_raw:
                            tty.setraw(slave_fd)
                            attrs = termios.tcgetattr(slave_fd)
                            attrs[6][termios.VMIN] = 1
                            attrs[6][termios.VTIME] = 0
                            termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
                    except Exception as e:
                        self.logger.warning(f"PTY mode configuration warning for {self.name}: {e}", exc_info=True)
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
                    self.logger.error(f"Failed to allocate PTY for {self.name}, falling back to pipes: {e}", exc_info=True)
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
            self._reader = CommandReader(self)
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
                    self.logger.error(f"Failed to register PTY reader for {self.name}: {e}", exc_info=True)
            else:
                self._read_task = asyncio.create_task(self._stdout_reader_task())

            self._stopped_notice_sent = False
            self.process_active = True
            if self.use_pty and self._output_batching_enabled:
                if self._output_flush_task is None or self._output_flush_task.done():
                    self._output_flush_task = asyncio.create_task(self._output_flush_buffer_loop())
            return True
        except Exception as e:
            self.logger.error(f"Error spawning process for {self.name}: {e}", exc_info=True)
            return False

    def _resolve_privilege_drop_spec(self) -> Optional[Dict[str, Any]]:
        policy = getattr(self, "_privilege_policy", None)
        if not policy:
            return None
        enabled = bool(getattr(policy, "enabled", False))
        if not enabled:
            return None
        user_identifier = getattr(policy, "user", None)
        group_identifier = getattr(policy, "group", None)
        supplementary_cfg = set(getattr(policy, "supplementary_groups", set()) or set())
        umask = getattr(policy, "umask", None)
        require_root = bool(user_identifier or group_identifier or supplementary_cfg)
        euid = os.geteuid()
        if require_root and euid != 0:
            self.logger.info(
                "Command port %s requested privilege drop (user=%r group=%r) but server runs as euid=%s; skipping drop",
                self.name,
                user_identifier,
                group_identifier,
                euid,
            )
            user_identifier = None
            group_identifier = None
            supplementary_cfg = set()
            require_root = False
        uid = None
        user_name = None
        home_dir = None
        pwd_record = None
        if user_identifier:
            pwd_record = self._lookup_user_record(user_identifier)
            uid = pwd_record.pw_uid
            user_name = pwd_record.pw_name
            home_dir = pwd_record.pw_dir
        gid = None
        group_name = None
        if group_identifier:
            gid, group_name = self._lookup_group_record(group_identifier)
        elif pwd_record is not None:
            gid = pwd_record.pw_gid
            try:
                group_entry = grp.getgrgid(gid)
                group_name = group_entry.gr_name
            except KeyError:
                group_name = None
        supplementary_ids: List[int] = []
        for grp_name in sorted(supplementary_cfg):
            gid_value, _ = self._lookup_group_record(grp_name)
            if gid_value not in supplementary_ids:
                supplementary_ids.append(gid_value)
        if uid is None and gid is None and not supplementary_ids and umask is None:
            return None
        if uid is not None or gid is not None or supplementary_ids:
            self.logger.info(
                "Command port %s will drop privileges user=%s gid=%s supp=%s",
                self.name,
                user_name or uid,
                gid,
                supplementary_ids,
            )
        return {
            "uid": uid,
            "gid": gid,
            "user_name": user_name,
            "group_name": group_name,
            "home": home_dir,
            "supplementary_gids": supplementary_ids if supplementary_ids else None,
            "umask": umask,
        }

    @staticmethod
    def _lookup_user_record(identifier: Any):
        if isinstance(identifier, int):
            try:
                return pwd.getpwuid(identifier)
            except KeyError as exc:
                raise RuntimeError(f"User id {identifier} not found") from exc
        text = str(identifier).strip()
        if not text:
            raise RuntimeError("Privilege drop user must be non-empty")
        try:
            if text.isdigit():
                return pwd.getpwuid(int(text))
            return pwd.getpwnam(text)
        except KeyError as exc:
            raise RuntimeError(f"User {text!r} not found") from exc

    @staticmethod
    def _lookup_group_record(identifier: Any):
        if isinstance(identifier, int):
            try:
                entry = grp.getgrgid(identifier)
            except KeyError as exc:
                raise RuntimeError(f"Group id {identifier} not found") from exc
            return entry.gr_gid, entry.gr_name
        text = str(identifier).strip()
        if not text:
            raise RuntimeError("Privilege drop group must be non-empty")
        try:
            if text.isdigit():
                entry = grp.getgrgid(int(text))
                return entry.gr_gid, entry.gr_name
            entry = grp.getgrnam(text)
            return entry.gr_gid, entry.gr_name
        except KeyError as exc:
            raise RuntimeError(f"Group {text!r} not found") from exc

    def _build_preexec_fn(self, drop_spec: Optional[Dict[str, Any]]):
        needs_setsid = bool(self.use_pty)
        if not needs_setsid and not drop_spec:
            return None

        def _preexec():
            if needs_setsid:
                os.setsid()
            if not drop_spec:
                return
            umask = drop_spec.get("umask")
            if isinstance(umask, int):
                os.umask(umask)
            supplementary = drop_spec.get("supplementary_gids")
            if supplementary:
                os.setgroups(supplementary)
            gid = drop_spec.get("gid")
            if gid is not None:
                os.setgid(gid)
            uid = drop_spec.get("uid")
            if uid is not None:
                os.setuid(uid)

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
                if self.intercept_term_queries and data:
                    try:
                        data = self._intercept_xtgettcap_queries(data)
                    except Exception:  # justification: interception is optional; raw data still usable
                        pass
                if self.output_crlf and data:
                    data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                if self._output_batching_enabled:

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
                else:
                    if data:
                        try:
                            asyncio.create_task(self._emit_output_chunk(data))
                        except Exception:
                            self.logger.error(f"Failed to schedule output forwarding for {self.name}", exc_info=True)
        except OSError as e:
            if self._loop and self._pty_reader_added:
                try:
                    self._loop.remove_reader(self._pty_master_fd)
                except Exception:  # justification: remove_reader failure during OSError cleanup is non-critical
                    pass
                self._pty_reader_added = False
            self.logger.debug(f"PTY reader closed for {self.name}: {e}")
            self.process_active = False
        except Exception as e:
            self.logger.error(f"Error in PTY reader callback for {self.name}: {e}", exc_info=True)
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
                    if self.intercept_term_queries:
                        try:
                            data = self._intercept_xtgettcap_queries(data)
                        except Exception:  # justification: optional interception; continuing with raw stdout
                            pass
                    if self.output_crlf and data:
                        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                    await self._emit_output_chunk(data)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.logger.error(f"Error reading from command stdout {self.name}: {e}", exc_info=True)
                    self.process_active = False
                    break
        except Exception as e:
            self.logger.error(f"Command stdout reader task error for {self.name}: {e}", exc_info=True)

    async def _output_flush_buffer_loop(self):
        """Flush batched output according to size and timing thresholds.

        Implements three flush triggers: batch size reached, idle timeout, or
        force-flush interval exceeded since first buffered byte. Continues
        while port and process remain active and batching is enabled.
        """
        self._last_data_time = asyncio.get_event_loop().time()
        self._first_data_time = None
        while self.is_running and self.process_active and self._output_batching_enabled:
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
        """Monitor process exit and perform auto-restart if enabled.

        Applies restart limits, delay, and backoff. Updates port state on
        terminal failure. Exits when adapter/port stops or restart policy
        disallows further attempts.
        """
        while self.is_running:
            try:
                if not self.process:
                    break
                await self.process.wait()
                self.process_active = False
                if not self.is_running:
                    break
                if not self.auto_restart:
                    self.logger.info(f"Process exited for {self.name}; not restarting")
                    self.state = PortState.DEGRADED
                    break
                if self.max_restarts and self.restart_count >= self.max_restarts:
                    self.logger.error(f"Max restarts reached for {self.name}; not restarting")
                    self.state = PortState.DEGRADED
                    break
                self.restart_count += 1
                delay = self.restart_delay * (self.restart_backoff ** (self.restart_count - 1))
                await asyncio.sleep(delay)
                if not await self._spawn_process():
                    self.logger.error(f"Respawn failed for {self.name}; stopping monitor")
                    self.is_running = False
                    self.state = PortState.DEGRADED
                    break
                self.logger.info(f"Respawned command port process for {self.name}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Monitor loop error for {self.name}: {e}", exc_info=True)
                break
        self.logger.info(f"Monitor loop exiting for command port {self.name}")

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
            self.logger.info(f"Stopping command port {self.name}")
            # Cancel any pending idle-stop task first
            try:
                if self._idle_stop_task and not self._idle_stop_task.done():
                    self._idle_stop_task.cancel()
            except Exception:
                pass
            self._idle_stop_task = None

            if self._monitor_task:
                try:
                    self._monitor_task.cancel()
                    await self._monitor_task
                except Exception:  # justification: monitor task cancellation error is non-critical during shutdown
                    pass
                self._monitor_task = None
            if self._pty_master_fd is not None and self._loop and self._pty_reader_added:
                try:
                    self._loop.remove_reader(self._pty_master_fd)
                except Exception:  # justification: best-effort removal from loop; safe to proceed
                    pass
                try:
                    os.close(self._pty_master_fd)
                except Exception:  # justification: fd may already be closed; ignore
                    pass
                self._pty_master_fd = None
                self._pty_reader_added = False
            # Wake/stop output flusher if active
            try:
                if self._output_flush_event:
                    self._output_flush_event.set()
            except Exception:
                pass
            if self._output_flush_task:
                try:
                    self._output_flush_task.cancel()
                    await self._output_flush_task
                except Exception:
                    pass
                self._output_flush_task = None
            if self._read_task:
                self._read_task.cancel()
                try:
                    await self._read_task
                except Exception:  # justification: awaiting cancelled read task may raise; safe to ignore
                    pass
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
            self.logger.info(f"Command port {self.name} stopped")
        except Exception as e:
            self.logger.error(f"Error stopping command port {self.name}: {e}", exc_info=True)
        finally:
            self._reader = None
            self._writer = None
            self.process = None
            self.data_queue = None
            self.client_count = 0
            self.is_running = False
            self.process_active = False
            self.state = PortState.CONFIGURED
            self.restart_count = 0

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
                self.logger.info(f"Force restarting command port {self.name}")
                await self.stop()
            # Case 2: Port previously started but process has exited (is_running true, process_active false)
            if self.is_running and not self.process_active:
                self.logger.info(f"Respawning exited process for command port {self.name}")
                ok = await self._spawn_process()
                if ok:
                    self.state = PortState.ACTIVE
                    self.is_running = True
                    self.process_active = True
                    if self.auto_restart and not self._monitor_task:
                        self._monitor_task = asyncio.create_task(self._monitor_loop())
                    return True
                self.logger.error(f"Respawn failed for command port {self.name}")
                return False
            # Case 3: Port fully stopped (not running)
            if not self.is_running:
                self.logger.info(f"Manual restart requested for stopped command port {self.name}")
                started = await self.start()
                if not started:
                    self.logger.error(f"Manual restart failed for {self.name}")
                return started
            # Case 4: Already running and active and no force flag
            self.logger.info(f"Restart skipped; port {self.name} already running and active")
            return True
        except Exception as e:
            self.logger.error(f"Error restarting command port {self.name}: {e}", exc_info=True)
            return False

    def _stopped_prefix(self) -> str:
        """Return standardized prefix for stopped status messages.

        Uses a server identifier (from config or hostname) plus port name
        if available and meaningful, filtering generic adapter names.

        Returns:
            str: Formatted prefix including trailing space.
        """
        try:
            # Try to extract server id from top-level config if available
            server_cfg = None
            try:
                cfg_mgr = getattr(getattr(self.adapter, "main_port_manager", None), "config_manager", None)
                if cfg_mgr:
                    # Ensure config is loaded
                    cfg_obj = getattr(cfg_mgr, "config", None)
                    if cfg_obj is None:
                        try:
                            cfg_obj = cfg_mgr.load_config()
                        except (
                            Exception
                        ):  # justification: newline mapping write drain best-effort; failures cause disconnect upstream
                            cfg_obj = None
                    if isinstance(cfg_obj, dict):
                        server_cfg = cfg_obj.get("server")
            except Exception:  # justification: writer transform pipeline failure; outer caller logs aggregate error
                server_cfg = None
            server_id = None
            if isinstance(server_cfg, dict):
                server_id = server_cfg.get("id") or server_cfg.get("name")
            if not server_id:
                # Fallback ONLY to hostname (do not use adapter name)
                try:
                    server_id = socket.gethostname()
                except Exception:  # justification: local echo enqueue is advisory; dropping echo is acceptable
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


class CommandReader:
    """Buffered reader for a single command port.

    Provides non-blocking retrieval of already buffered process output. It does
    not itself perform any I/O reads from the underlying subprocess; that work
    is done by the PTY readability callback or the pipe reader task in
    ``CommandPort`` which enqueue data into ``data_queue``. This thin wrapper
    normalizes the stopped-process notice injection semantics used elsewhere
    in the system so the caller always receives a consistent status banner
    before first read on a non-active process.

    Args:
        port: Parent ``CommandPort`` instance.
    """

    def __init__(self, port: CommandPort):
        self.port = port
        self.logger = port.logger

    async def read(self) -> bytes:  # noqa: Vulture (signature trimmed; size was unused)
        """Return the next buffered output chunk if available.

        Non-blocking: performs a ``get_nowait`` on the internal queue. If the
        underlying process is not currently active a one-time standardized
        status banner is returned (and suppressed on subsequent reads)
        indicating the process may be respawned by pressing Enter.

        Returns:
            bytes: Next available chunk of output, the stopped notice banner,
            or ``b""`` if no data is ready.
        """
        if (not self.port.process_active or self.port.state != PortState.ACTIVE) and not self.port._stopped_notice_sent:
            self.port._stopped_notice_sent = True
            return f"\r\n[OpenMux:PROCESS_NOT_RUNNING {self.port._stopped_prefix()} – press Enter to respawn]\r\n".encode()

        if not self.port.data_queue:
            return b""
        try:
            data = self.port.data_queue.get_nowait()
            return data
        except asyncio.QueueEmpty:
            return b""
        except Exception as e:
            self.logger.error(f"Error reading from command {self.port.name}: {e}", exc_info=True)
            return b""


class CommandWriter:
    """Buffered / batched writer for a command port.

    Handles newline normalization (depending on PTY mode and configured
    ``pty_enter_mode``) and optional client-side local echo. Supports input
    batching with size and timeout triggers to reduce write system call
    frequency for high-chattiness clients. Also implements the convenience
    behavior that a lone newline sent to a stopped (but previously started)
    process will attempt a respawn and then deliver the newline to prompt the
    new shell/program.

    Args:
        stdin_stream: The process stdin stream (``StreamWriter`` like) when
            using pipe-based execution; ``None`` when under PTY mode.
        port: Parent ``CommandPort`` instance.
    """

    def __init__(self, stdin_stream, port: CommandPort):
        self.stdin_stream = stdin_stream
        self.port = port
        self.logger = port.logger
        # Batching config (from port config or defaults)
        cfg = getattr(port, "config", {})
        self._batch_size = cfg.get("batch_size", 1024)
        self._batch_timeout = cfg.get("batch_timeout", 0.002)  # 2ms default
        self._batching_enabled = cfg.get("enable_batching", True)
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
            # Allow pressing Enter (CR / LF / CRLF) to respawn a dead process
            newline_only = False
            try:
                if data in (b"\r", b"\n", b"\r\n"):
                    newline_only = True
            except Exception:  # justification: tolerate unexpected non-bytes input; treat as not a pure newline
                pass
            if newline_only:
                # Attempt respawn (non-forced) – only if previously started
                await self.port.restart(force=False)
                if not self.port.process_active:
                    # Respawn failed; fall through to stopped notice below
                    pass
                else:
                    # Process is back; write the newline to deliver a prompt
                    # Update stdin_stream reference if needed (new process)
                    if not getattr(self.port, "use_pty", False):
                        self.stdin_stream = self.port.process.stdin if self.port.process else None
                    await self._write_direct(data)
                    return
            if not self.port._stopped_notice_sent:
                self.port._stopped_notice_sent = True
                hint = "spawn" if getattr(self.port, "spawn_on_demand", False) else "respawn"
                notice = (
                    f"\r\n[OpenMux:PROCESS_NOT_RUNNING {self.port._stopped_prefix()} – press Enter to {hint}]\r\n".encode()
                )
                try:
                    await self.port._emit_output_chunk(notice, require_clients=False)
                except Exception:
                    self.logger.debug("Failed to emit stopped notice", exc_info=True)
            return
        if not self._batching_enabled:
            await self._write_direct(data)
            return
        # Batching mode
        async with self._write_buffer_lock:
            self._write_buffer += data
            if len(self._write_buffer) >= self._batch_size:
                self._flush_event.set()
        # Start flush task if not running
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_buffer_loop())

    async def _write_direct(self, data: bytes) -> None:
        """Perform an immediate write of ``data`` honoring normalization.

        Applies newline translation rules for PTY / pipe modes, writes to the
        relevant descriptor, optionally echoes locally, and suppresses all
        exceptions to avoid propagating transient I/O failures upstream.

        Args:
            data: Bytes to write.
        """
        try:
            if getattr(self.port, "normalize_newlines", False) and data:
                _orig = data
                if getattr(self.port, "use_pty", False):
                    mode = getattr(self.port, "pty_enter_mode", "none")
                    if mode == "cr":
                        data = data.replace(b"\r\n", b"\r").replace(b"\n", b"\r")
                    elif mode == "lf":
                        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                    elif mode == "crlf":
                        tmp = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                        data = tmp.replace(b"\n", b"\r\n")
                    else:
                        # none: leave data unchanged for PTY
                        pass
                else:
                    # For pipe-based processes, normalize to LF.
                    data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                if self.logger.isEnabledFor(logging.DEBUG) and (
                    b"\r" in _orig or b"\n" in _orig or b"\r" in data or b"\n" in data
                ):
                    self.logger.debug(
                        "Writer newline map (pty=%s): in=%r out=%r",
                        getattr(self.port, "use_pty", False),
                        _orig,
                        data,
                    )
            if getattr(self.port, "use_pty", False) and self.port._pty_master_fd is not None:
                try:
                    os.write(self.port._pty_master_fd, data)
                except Exception as e:
                    self.logger.error(f"PTY write error for {self.port.name}: {e}", exc_info=True)
            else:
                if self.stdin_stream is not None:
                    self.stdin_stream.write(data)
                    try:
                        await self.stdin_stream.drain()
                    except Exception:  # justification: stdin drain failure non-fatal; writer continues or process will exit
                        pass
            if getattr(self.port, "local_echo", False):
                try:
                    await self.port._emit_output_chunk(data, require_clients=False)
                except Exception:  # justification: local echo enqueue failure is advisory; safe to ignore
                    self.logger.debug("Local echo emit failed", exc_info=True)
        except Exception as e:
            self.logger.error(f"Error writing to command {self.port.name}: {e}", exc_info=True)

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
    restart policies, newline normalization, batching, and terminal query
    interception.
    """

    def __init__(self, plugin_name: str, config: Dict[str, Any]):
        super().__init__(plugin_name, config)
        self.ports: Dict[str, CommandPort] = {}
        self.logger = logging.getLogger(f"openmux.adapter.command.{plugin_name}")
        self.security_policy = None
        self._privilege_policy: Optional[CommandPrivilegePolicy] = None

    @property
    def adapter_type(self) -> str:
        """Return stable adapter type identifier.

        Returns:
            str: The adapter type key used in configuration and status APIs.
        """
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
        try:
            self._privilege_policy = policy.get_command_privilege_policy() if policy else None
        except AttributeError:
            self._privilege_policy = None

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
        return True

    def get_port_configurations(self) -> Dict[str, Dict[str, Any]]:
        """Return mapping of configured command port definitions.

        Returns:
            Dict[str, Dict[str, Any]]: Keyed by port name with raw config dicts.
        """
        port_configs: Dict[str, Dict[str, Any]] = {}
        self.logger.debug(f"Getting port configurations from config: {self.config}")
        command_ports = self.config.get("command_ports", [])
        self.logger.debug(f"Found {len(command_ports)} command port configurations")
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
                self.logger.info(f"Command adapter {self.name} started with {len(self.ports)} ports")
            return success
        except Exception as e:
            self.logger.error(f"Error starting command adapter {self.name}: {e}", exc_info=True)
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
            command_port = CommandPort(port_name, config, self, privilege_policy=self._privilege_policy)
            # Start immediately unless configured for on-demand spawn
            if getattr(command_port, "spawn_on_demand", False):
                # Do not spawn the process yet; mark as configured
                self.ports[port_name] = command_port
                # Ensure a queue exists so the wrapper will reuse it
                if command_port.data_queue is None:
                    command_port.data_queue = asyncio.Queue(maxsize=100)
                # Register with the main PortManager so the port is discoverable immediately
                try:
                    if self.main_port_manager:
                        await self.main_port_manager.register_unified_port(port_name, command_port, self)
                except Exception:
                    self.logger.warning(f"Failed to register unified command port {port_name}")
                self.logger.info(
                    f"Created command port (on-demand): {port_name} (will spawn on first client attach)"
                )
                return command_port
            else:
                if await command_port.start():
                    self.ports[port_name] = command_port
                    # Ensure a queue exists so the wrapper will reuse it
                    if command_port.data_queue is None:
                        command_port.data_queue = asyncio.Queue(maxsize=100)
                    # Register with the main PortManager
                    try:
                        if self.main_port_manager:
                            await self.main_port_manager.register_unified_port(port_name, command_port, self)
                    except Exception:
                        self.logger.warning(f"Failed to register unified command port {port_name}")
                    self.logger.info(f"Created command port: {port_name}")
                    return command_port
                self.logger.error(f"Failed to start command port: {port_name}")
                return None
        except Exception as e:
            self.logger.error(f"Error creating command port {port_name}: {e}", exc_info=True)
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
                    self.logger.warning(f"Failed to unregister unified command port {port_name}")
                await command_port.stop()
                del self.ports[port_name]
                self.logger.info(f"Destroyed command port: {port_name}")
            except Exception as e:
                self.logger.error(f"Error destroying command port {port_name}: {e}", exc_info=True)

    async def stop(self) -> None:
        """Stop all command ports and mark adapter not running.

        Iterates each managed port with a bounded timeout. Errors are logged
        but do not abort remaining stops.
        """
        try:
            self.logger.info(f"Stopping command adapter {self.name} with {len(self.ports)} ports")
            for port_name in list(self.ports.keys()):
                try:
                    self.logger.debug(f"Stopping command port {port_name}")
                    await asyncio.wait_for(self.destroy_port(port_name), timeout=2.5)
                    self.logger.debug(f"Stopped command port {port_name}")
                except asyncio.TimeoutError:
                    self.logger.error(f"Timeout stopping command port {port_name}")
                except Exception as e:
                    self.logger.error(f"Error stopping command port {port_name}: {e}", exc_info=True)
            self.is_running = False
            self.logger.info(f"Command adapter {self.name} stopped")
        except Exception as e:
            self.logger.error(f"Error stopping command adapter {self.name}: {e}", exc_info=True)

    async def get_port_status(self, port_name: str) -> Dict[str, Any]:
        """Return detailed status for a single command port.

        Args:
            port_name: Port identifier.

        Returns:
            Dict[str, Any]: Status mapping (contains 'error' if missing).
        """
        if port_name not in self.ports:
            return {"error": f"Port {port_name} not found"}
        port = self.ports[port_name]
        return {
            "name": port_name,
            "state": port.state.value,
            "is_running": port.is_running,
            "command": port.command,
            "description": port.description,
            "adapter": self.adapter_type,
            "adapter_instance": self.name,
            "auto_restart": port.auto_restart,
            "restart_count": port.restart_count,
            "max_restarts": port.max_restarts,
        }

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
            # description is non-material; command, env, shell, cwd changes are material
            c = dict(cfg)
            c.pop("name", None)
            c.pop("description", None)
            return c

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
                        "auto_restart": getattr(port, "auto_restart", None),
                        "batch_bytes": getattr(port, "batch_bytes", None),
                        "batch_timeout_ms": getattr(port, "batch_timeout_ms", None),
                    }
                except Exception:
                    old_cfg = {}
            if old_cfg == _material_cfg(new_by_name[n]):
                # Update description in-place if provided
                try:
                    desc = new_by_name[n].get("description")
                    if isinstance(desc, str) and desc:
                        setattr(port, "description", desc)
                except Exception:
                    pass
                unchanged.append(n)
            else:
                updated.append(n)

        # Remove updated/removed
        for n in removed + updated:
            try:
                await self.destroy_port(n)
            except Exception as e:
                self.logger.error(f"Failed to destroy command port {n}: {e}", exc_info=True)

        # Create added/updated
        for n in added + updated:
            cfg = new_by_name.get(n)
            if not cfg:
                continue
            try:
                await self.create_port(n, cfg)
            except Exception as e:
                self.logger.error(f"Failed to create command port {n}: {e}", exc_info=True)

        # Update adapter config snapshot
        try:
            self.config["command_ports"] = [new_by_name[k] for k in sorted(new_by_name.keys())]
        except Exception:
            pass

        summary = {"added": added, "removed": removed, "updated": updated, "unchanged": unchanged}
        self.logger.info(
            f"Command adapter {self.name} reconcile: +{len(added)} ~{len(updated)} -{len(removed)} unchanged={len(unchanged)}"
        )
        return summary

    async def list_ports(self) -> List[Dict[str, Any]]:
        """List status dictionaries for all managed ports.

        Returns:
            List[Dict[str, Any]]: Per-port status mappings.
        """
        return [await self.get_port_status(name) for name in self.ports.keys()]

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
            self.logger.error(f"Command port {port_name} not found")
            return 0
        try:
            return await port.write_data(data)
        except Exception as e:
            self.logger.error(f"Error writing to command port {port_name}: {e}", exc_info=True)
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
                            "always_buffer": getattr(port, "always_buffer", False),
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
