"""Telnet Listener Adapter.

Expose existing OpenMux ports over simple Telnet-compatible TCP sockets. Each
listener entry binds to a host/port pair and attaches clients directly to a
configured OpenMux port (local or federated). Access controls rely on per-
listener ACLs and optional read-only enforcement (client input dropped).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from .base_adapter import AdapterCapability, BaseGenericAdapter


AclEntry = Union[
    ipaddress.IPv4Address,
    ipaddress.IPv6Address,
    ipaddress.IPv4Network,
    ipaddress.IPv6Network,
]


@dataclass
class ListenerConfig:
    name: str
    bind_host: str
    bind_port: int
    target: str
    read_only: bool = False
    acl_raw: List[str] = field(default_factory=list)
    enabled: bool = True
    compiled_acl: List[AclEntry] = field(default_factory=list)
    effective_host: Optional[str] = None
    effective_port: Optional[int] = None


@dataclass
class TelnetSession:
    client_id: str
    listener: ListenerConfig
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    port_name: str
    read_only: bool
    remote_host: str
    port_mode: str = "read-only"
    bytes_in: int = 0
    bytes_out: int = 0
    task: Optional[asyncio.Task] = None


class TelnetListenerAdapter(BaseGenericAdapter):
    """Adapter exposing OpenMux ports via raw TCP/Telnet sockets."""

    adapter_type = "telnet_listener"

    def get_adapter_type(self) -> str:
        """Return adapter type for security policy and factory lookup."""
        return "telnet_listener"

    def __init__(self, name: str, config: Dict[str, Any]):
        super().__init__(name, config)
        self.logger = logging.getLogger(f"openmux.adapter.telnet_listener.{name}")
        raw_entries = config.get("telnet_listener") if isinstance(config, dict) else []
        self.listeners: List[ListenerConfig] = []
        if isinstance(raw_entries, list):
            for entry in raw_entries:
                spec = self._build_listener(entry)
                if spec:
                    self.listeners.append(spec)
        self.servers: Dict[str, asyncio.AbstractServer] = {}
        self.sessions: Dict[str, TelnetSession] = {}
        self.console_manager = None

    # ------------------------------------------------------------------
    # Adapter contract

    def get_capabilities(self) -> set:
        return {
            AdapterCapability.ACCEPTS_CONNECTIONS,
            AdapterCapability.BIDIRECTIONAL_DATA,
        }

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> bool:
        if not isinstance(config, dict):
            return False
        entries = config.get("telnet_listener")
        if entries is None:
            return True
        if not isinstance(entries, list):
            return False
        seen_names = set()
        for entry in entries:
            if not isinstance(entry, dict):
                return False
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                return False
            if name in seen_names:
                return False
            seen_names.add(name)
            target = entry.get("target")
            if not isinstance(target, str) or not target.strip():
                return False
            bind_port = entry.get("bind_port")
            try:
                port = int(bind_port)
            except (TypeError, ValueError):
                return False
            if port < 1 or port > 65535:
                return False
            bind_host = entry.get("bind_host")
            if bind_host is not None and (not isinstance(bind_host, str) or not bind_host.strip()):
                return False
            if "read_only" in entry and not isinstance(entry.get("read_only"), bool):
                return False
            if "enabled" in entry and not isinstance(entry.get("enabled"), bool):
                return False
            if "acl" in entry:
                acl = entry.get("acl")
                if acl is not None and not isinstance(acl, list):
                    return False
                if isinstance(acl, list):
                    for rule in acl:
                        if not isinstance(rule, str) or not rule.strip():
                            return False
                        try:
                            if "/" in rule:
                                ipaddress.ip_network(rule, strict=False)
                            else:
                                ipaddress.ip_address(rule)
                        except ValueError:
                            return False
        return True

    async def start(self) -> bool:
        if not self.listeners:
            self.logger.info("Telnet listener adapter has no entries; nothing to bind")
            self.is_running = True
            return True
        if not self.console_manager:
            self.logger.error("Telnet listener adapter requires a console manager reference")
            return False
        success = True
        for spec in self.listeners:
            if not spec.enabled:
                self.logger.info("Telnet listener '%s' disabled via configuration", spec.name)
                continue
            try:
                async def _connection_entry(reader, writer, listener_spec=spec):
                    await self._handle_connection(listener_spec, reader, writer)

                server = await asyncio.start_server(
                    _connection_entry,
                    spec.bind_host,
                    spec.bind_port,
                )
                self.servers[spec.name] = server
                self.logger.info(
                    "Telnet listener '%s' bound to %s", spec.name, self._format_sockname(server.sockets)
                )
                try:
                    sock = server.sockets[0]
                    if sock:
                        sockname = sock.getsockname()
                        spec.effective_host = sockname[0]
                        spec.effective_port = sockname[1]
                except Exception:
                    pass
            except Exception as exc:
                self.logger.error(
                    "Failed to start telnet listener '%s' on %s:%s: %s",
                    spec.name,
                    spec.bind_host,
                    spec.bind_port,
                    exc,
                    exc_info=True,
                )
                success = False
        self.is_running = success
        return success

    async def stop(self) -> None:
        self.is_running = False
        # Close sessions
        for client_id in list(self.sessions.keys()):
            await self._disconnect_session(client_id, reason="adapter stop")
        # Stop servers
        for name, server in list(self.servers.items()):
            try:
                server.close()
                await server.wait_closed()
                self.logger.info("Telnet listener '%s' stopped", name)
            except Exception:
                self.logger.warning("Error stopping telnet listener '%s'", name, exc_info=True)
        self.servers.clear()

    async def create_port(self, port_name: str, config: Dict[str, Any]) -> Optional[Any]:
        return None

    async def destroy_port(self, port_name: str) -> None:
        return None

    def get_port_configurations(self) -> Dict[str, Dict[str, Any]]:
        return {}

    # ------------------------------------------------------------------
    # Console manager hooks

    def set_console_manager(self, console_manager):
        self.console_manager = console_manager
        try:
            if hasattr(console_manager, "register_client_manager"):
                console_manager.register_client_manager(self)
        except Exception:
            self.logger.warning("Failed to register telnet adapter as client manager", exc_info=True)

    async def send_data_to_client(self, client_id: str, data: bytes) -> bool:
        session = self.sessions.get(client_id)
        if not session:
            return False
        try:
            session.writer.write(data)
            await session.writer.drain()
            session.bytes_out += len(data)
            return True
        except Exception as exc:
            self.logger.debug("Failed to send data to telnet client %s: %s", client_id, exc)
            await self._disconnect_session(client_id, reason="write failure")
            return False

    # ------------------------------------------------------------------
    # Connection handling

    async def _handle_connection(
        self,
        listener: ListenerConfig,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer_ip = self._peer_ip(writer)
        if not self._client_allowed(listener, peer_ip):
            await self._send_and_close(writer, b"Access denied\r\n")
            return
        if not self.main_port_manager:
            await self._send_and_close(writer, b"Server unavailable\r\n")
            return
        port_name = await self._resolve_target(listener.target)
        if not port_name:
            await self._send_and_close(writer, f"Port {listener.target} unavailable\r\n".encode())
            return
        client_id = f"telnet:{listener.name}:{uuid.uuid4()}"
        session = TelnetSession(
            client_id=client_id,
            listener=listener,
            reader=reader,
            writer=writer,
            port_name=port_name,
            read_only=listener.read_only,
            remote_host=peer_ip,
        )
        attach_ok = await self._attach_session(session)
        if not attach_ok:
            await self._send_and_close(writer, b"Failed to attach to port\r\n")
            return
        self.sessions[client_id] = session
        self.logger.info(
            "Telnet client %s connected (listener=%s, port=%s, ro=%s)",
            client_id,
            listener.name,
            port_name,
            session.read_only,
        )
        try:
            await self._pump_client_input(session)
        finally:
            await self._disconnect_session(client_id, reason="disconnect")

    async def _pump_client_input(self, session: TelnetSession) -> None:
        reader = session.reader
        while True:
            try:
                data = await reader.read(4096)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.debug("Read error for %s: %s", session.client_id, exc)
                break
            if not data:
                break
            session.bytes_in += len(data)
            if session.read_only:
                continue
            try:
                if self.console_manager:
                    await self.console_manager.write_to_port(session.port_name, data, session.client_id)
            except Exception as exc:
                self.logger.warning(
                    "Failed forwarding data from %s to port %s: %s",
                    session.client_id,
                    session.port_name,
                    exc,
                )
                break

    async def _attach_session(self, session: TelnetSession) -> bool:
        if not self.console_manager:
            return False
        username = f"telnet_{session.listener.name}"
        try:
            ok, mode = await self.console_manager.connect_client_to_port(
                session.client_id,
                session.port_name,
                username,
            )
        except Exception as exc:
            self.logger.error("Console manager attach failed for %s: %s", session.client_id, exc)
            return False
        if ok:
            session.port_mode = mode
            if mode != "read-write":
                session.read_only = True
        else:
            self.logger.warning(
                "Console manager rejected telnet client %s for port %s",
                session.client_id,
                session.port_name,
            )
        if ok and hasattr(self.console_manager, "register_client_channel"):
            try:
                self.console_manager.register_client_channel(session.client_id, self)
            except Exception:
                pass
        return bool(ok)

    async def _disconnect_session(self, client_id: str, *, reason: str) -> None:
        session = self.sessions.pop(client_id, None)
        if not session:
            return
        try:
            if self.console_manager:
                try:
                    if hasattr(self.console_manager, "unregister_client_channel"):
                        self.console_manager.unregister_client_channel(client_id)
                except Exception:
                    pass
                await self.console_manager.disconnect_client_from_port(client_id, session.port_name)
        except Exception:
            self.logger.debug("Failed disconnecting client %s from port", client_id, exc_info=True)
        try:
            writer = session.writer
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        self.logger.info(
            "Telnet client %s closed (%s) in=%d out=%d",
            client_id,
            reason,
            session.bytes_in,
            session.bytes_out,
        )

    # ------------------------------------------------------------------
    # Helpers

    def _build_listener(self, entry: Dict[str, Any]) -> Optional[ListenerConfig]:
        if not isinstance(entry, dict):
            return None
        try:
            bind_host = entry.get("bind_host", "0.0.0.0")
            if not isinstance(bind_host, str) or not bind_host.strip():
                bind_host = "0.0.0.0"
            bind_port = int(entry.get("bind_port"))
            spec = ListenerConfig(
                name=str(entry.get("name")),
                bind_host=bind_host,
                bind_port=bind_port,
                target=str(entry.get("target")),
                read_only=bool(entry.get("read_only", False)),
                acl_raw=[str(a).strip() for a in entry.get("acl", []) if str(a).strip()],
                enabled=bool(entry.get("enabled", True)),
            )
            if not spec.name:
                return None
            if not spec.target:
                return None
            compiled: List[AclEntry] = []
            for rule in spec.acl_raw:
                try:
                    if "/" in rule:
                        compiled.append(ipaddress.ip_network(rule, strict=False))
                    else:
                        compiled.append(ipaddress.ip_address(rule))
                except ValueError:
                    self.logger.warning("Ignoring invalid ACL '%s' for listener %s", rule, spec.name)
            spec.compiled_acl = compiled
            return spec
        except Exception:
            self.logger.error("Invalid telnet listener entry: %s", entry, exc_info=True)
            return None

    def _client_allowed(self, listener: ListenerConfig, peer_ip: str) -> bool:
        if not listener.compiled_acl:
            return True
        try:
            addr = ipaddress.ip_address(peer_ip)
        except ValueError:
            return False
        for rule in listener.compiled_acl:
            if isinstance(rule, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
                if addr == rule:
                    return True
            else:
                if addr in rule:
                    return True
        return False

    async def _resolve_target(self, descriptor: str) -> Optional[str]:
        if not self.main_port_manager:
            return None
        descriptor = descriptor.strip()
        if not descriptor:
            return None
        if "::" in descriptor:
            prefix, base = descriptor.split("::", 1)
            if prefix.lower() == "local":
                return base if self._is_local_port(base) else None
            return await self._resolve_remote_by_origin(base, prefix)
        if self._is_local_port(descriptor):
            return descriptor
        return await self._resolve_remote_by_origin(descriptor, None)

    def _is_local_port(self, port_name: str) -> bool:
        try:
            port = self.main_port_manager.get_port(port_name)
        except Exception:
            port = None
        if port is None:
            return False
        metadata = getattr(port, "metadata", None)
        if metadata is not None:
            origin = getattr(metadata, "origin_server", None)
            if origin is not None and getattr(origin, "server_id", None):
                return False
        return True

    async def _resolve_remote_by_origin(self, port_name: str, server_id: Optional[str]) -> Optional[str]:
        entries = []
        try:
            getter = getattr(self.main_port_manager, "get_port_list_with_federation", None)
            if getter:
                entries = await asyncio.wait_for(getter(), timeout=1.0)
        except Exception:
            entries = []
        matches = []
        for entry in entries or []:
            if entry.get("name") != port_name:
                continue
            origin = entry.get("origin_server_id")
            if server_id is None:
                if origin:
                    matches.append(entry)
                continue
            if server_id.lower() in {"local", "localhost"}:
                if origin:
                    continue
                matches.append(entry)
            elif origin == server_id:
                matches.append(entry)
        if len(matches) == 1:
            return matches[0].get("name")
        return None

    def _peer_ip(self, writer: asyncio.StreamWriter) -> str:
        try:
            peer = writer.get_extra_info("peername")
            if isinstance(peer, tuple) and peer:
                return peer[0]
            if isinstance(peer, str):
                return peer
        except Exception:
            pass
        return "unknown"

    async def _send_and_close(self, writer: asyncio.StreamWriter, payload: bytes) -> None:
        try:
            writer.write(payload)
            await writer.drain()
        except Exception:
            pass
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    def _format_sockname(self, sockets: Optional[List[socket.socket]]) -> str:
        if not sockets:
            return "unknown"
        parts = []
        for sock in sockets:
            try:
                host, port = sock.getsockname()[:2]
                parts.append(f"{host}:{port}")
            except Exception:
                continue
        return ",".join(parts) or "unknown"


__all__ = ["TelnetListenerAdapter"]
