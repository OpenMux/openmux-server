"""Raw WebSocket streaming adapter for OpenMux client sessions.

This refactored adapter now targets the server-side ``web_console`` adapter's
per-port streaming endpoint (``/ws/<port_name>``) instead of the original
JSON control protocol. A WebSocket connection is established directly to the
desired port, and thereafter text/binary frames are forwarded as-is.

Differences vs previous implementation:
        * No JSON control messages (authenticate, list_ports, connect_to_port).
        * Authentication is performed using HTTP Basic Auth during the initial
            WebSocket handshake (leveraging the server's BasicAuth middleware).
        * Port listing (when needed) is retrieved via HTTP GET ``/api/ports``.
        * The adapter is considered "authenticated" immediately after a successful
            WebSocket upgrade (credentials validated at handshake).
        * ``connect_to_port`` is a no-op that returns True if already connected
            to the specified port; a different port requires closing and reconnecting.

Limitations:
        * Switching ports requires closing and reconnecting.
        * Does not multiplex multiple ports over one WebSocket.
        * Expects server to implement ``/api/ports`` for discovery when used.
"""

import asyncio
import base64
import json
from typing import Any, Dict, List, Optional, Union

from .base_adapter import BaseClientAdapter


class WebSocketClientAdapter(BaseClientAdapter):
    """Raw streaming WebSocket adapter.

    Configuration keys (optional):
        use_tls (bool): Use wss:// if True else ws://.
        port_name (str): Target server port name. Required before connect().
        timeout (float): Handshake timeout seconds (default 10).
        path_prefix (str): Prefix before port name (default '/ws/').
        basic_user (str): Username for Basic Auth.
        basic_password (str): Password for Basic Auth.
        list_ports_via_http (bool): If True, list_ports() will issue HTTP GET /api/ports.
        http_base_path (str): Base path for API endpoints (default '').
    """

    def __init__(self, host: str, port: int, config: Optional[Dict[str, Any]] = None):
        super().__init__(host, port, config)
        self.use_tls = bool(self.config.get("use_tls", False))
        self.port_name = self.config.get("port_name")  # required for connect
        # Optional explicit origin for disambiguation: when provided, the path becomes /ws/{server_id}/{port_name}
        self.origin_server_id = self.config.get("origin_server_id")
        self.timeout = float(self.config.get("timeout", 10.0))
        self.path_prefix = self.config.get("path_prefix", "/ws/")
        # Back-compat attribute expected by tests/older callers
        self.path = "/ws"
        self.basic_user = self.config.get("basic_user")
        self.basic_password = self.config.get("basic_password")
        self.list_ports_via_http = bool(self.config.get("list_ports_via_http", True))
        self.http_base_path = self.config.get("http_base_path", "")
        self.websocket = None
        self._aiohttp_session = None  # type: ignore
        self.protocol_version = "websocket-stream-1.0"
        # Track port up/down state from OMXCTRL meta frames
        self._port_up = None

    async def connect(self) -> bool:
        if self.is_connected:
            return True
        # Ensure required client dependency is present even for discovery mode.
        # Tests expect connect() to fail if the WebSocket/HTTP client import fails.
        try:
            import aiohttp  # noqa: F401
        except Exception as e:
            self.logger.error(f"aiohttp import failed: {e}")
            return False
        # If no port_name was supplied we operate in "discovery" mode: we do not
        # establish a WebSocket data channel yet, but still consider the adapter
        # connected so that higher‑level code can invoke list_ports() which uses
        # plain HTTP. Basic Auth credentials (if provided) are stored and used
        # by list_ports(). This lets `openmux-client -l --adapter websocket` work
        # without forcing the user to choose a port first.
        if not self.port_name:
            self.logger.debug("WebSocket adapter starting in discovery mode (no port_name). Skipping WS handshake.")
            # Mark logical connection established so caller can proceed. We also
            # treat authentication as successful if basic creds were supplied;
            # the subsequent HTTP GET /api/ports will still validate them.
            self.is_connected = True
            if self.basic_user and self.basic_password:
                self.is_authenticated = True
                self.username = self.basic_user
            return True
        try:
            import aiohttp

            protocol = "wss" if self.use_tls else "ws"
            # Determine disambiguated path. Accept forms:
            #  - plain:               <name>              -> /ws/<name>
            #  - composite string:    <sid>::<name>       -> /ws/<sid>/<name>
            #  - explicit config:     origin_server_id + port_name
            server_id: Optional[str] = None
            port_name = self.port_name
            if isinstance(self.origin_server_id, str) and self.origin_server_id:
                server_id = self.origin_server_id
            elif isinstance(port_name, str) and "::" in port_name:
                try:
                    sid, base = port_name.split("::", 1)
                    if sid and base:
                        server_id = sid
                        port_name = base
                except ValueError:
                    pass
            # Build path
            if server_id:
                path = f"/ws/{server_id}/{port_name}"
            else:
                path = f"{self.path_prefix.rstrip('/')}/{port_name}" if self.path_prefix else f"/ws/{port_name}"
            if not path.startswith("/"):
                path = "/" + path
            # Request metadata push so we can surface port up/down notices in CLI
            # Append ?meta=1 preserving simple path structure
            if "?" not in path:
                path = f"{path}?meta=1"
            else:
                # Defensive: ensure meta flag present
                if "meta=" not in path:
                    path = f"{path}&meta=1"
            # aiohttp wants http/https scheme even for websockets; ws(s) accepted in recent versions but normalize
            http_scheme = "https" if self.use_tls else "http"
            base_url = f"{http_scheme}://{self.host}:{self.port}"  # e.g. http://host:port
            url = f"{base_url}{path}"
            headers = {}
            if self.basic_user and self.basic_password:
                token = base64.b64encode(f"{self.basic_user}:{self.basic_password}".encode("utf-8")).decode("ascii")
                headers["Authorization"] = f"Basic {token}"
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self.logger.info(f"Connecting (raw WS) to {url}")
            session = aiohttp.ClientSession(timeout=timeout)
            try:
                self.websocket = await session.ws_connect(url, headers=headers)
            except Exception:
                await session.close()
                raise
            self._aiohttp_session = session  # store to close later
            # If server immediately closed (e.g., invalid port), treat as failure
            try:
                if getattr(self.websocket, "closed", False):
                    raise RuntimeError("websocket closed immediately")
                # Also peek a message with a very short timeout; close/closing indicates failure
                from aiohttp import WSMsgType

                try:
                    msg = await asyncio.wait_for(self.websocket.receive(), timeout=0.05)
                    if msg and msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                        raise RuntimeError("websocket closed on first receive")
                except asyncio.TimeoutError:
                    pass
            except Exception:
                await session.close()
                self.websocket = None
                self._aiohttp_session = None
                self.is_connected = False
                return False
            self.is_connected = True
            self.is_authenticated = True
            self.username = self.basic_user
            return True
        except Exception as e:
            self.logger.error(f"WebSocket connect failed: {e}", exc_info=True)
            return False

    async def authenticate_with_password(self, username: str, password: str) -> bool:  # compatibility shim
        self.logger.debug("authenticate_with_password called - already authenticated via handshake")
        return self.is_authenticated

    async def authenticate_with_key(self, api_key: str) -> bool:  # not supported in raw streaming mode
        self.logger.warning("API key auth not supported in raw WS mode; use Basic Auth at handshake")
        return False

    async def list_ports(self) -> List[Dict[str, Any]]:
        if not self.list_ports_via_http:
            self.logger.debug("Port listing disabled by configuration")
            return []
        # Perform HTTP GET /api/ports with same Basic Auth
        try:
            import aiohttp

            scheme = "https" if self.use_tls else "http"
            base = f"{scheme}://{self.host}:{self.port}{self.http_base_path}".rstrip("/")
            url = f"{base}/api/ports"
            headers = {}
            if self.basic_user and self.basic_password:
                token = base64.b64encode(f"{self.basic_user}:{self.basic_password}".encode()).decode()
                headers["Authorization"] = f"Basic {token}"
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        self.logger.warning(f"Port list HTTP {resp.status}")
                        return []
                    data = await resp.json(content_type=None)
                    return data.get("ports", []) if isinstance(data, dict) else []
        except Exception as e:
            self.logger.error(f"HTTP port listing failed: {e}", exc_info=True)
            return []

    async def connect_to_port(self, port_name: str) -> bool:
        # In raw mode connection is established directly to the port via URI.
        if not self.is_connected:
            return False
        return port_name == self.port_name

    async def send_data(self, data: Union[str, bytes]) -> bool:
        if not self.is_connected or not self.websocket:
            return False
        try:
            if isinstance(data, str):
                await self.websocket.send_str(data)
            else:
                await self.websocket.send_bytes(data)
            return True
        except Exception as e:
            self.logger.error(f"Send failed: {e}", exc_info=True)
            self.is_connected = False
            return False

    async def read_data(self, timeout: Optional[float] = None) -> Optional[Union[str, bytes]]:
        if not self.is_connected or not self.websocket:
            return None
        try:
            if timeout is not None:
                msg = await asyncio.wait_for(self.websocket.receive(), timeout=timeout)
            else:
                msg = await self.websocket.receive()
            from aiohttp import WSMsgType

            if msg.type == WSMsgType.TEXT:
                data = msg.data
                # Intercept control/meta frames from server
                if isinstance(data, str) and data.startswith("OMXCTRL "):
                    try:
                        payload = data[len("OMXCTRL "):]
                        info = json.loads(payload)
                        if isinstance(info, dict) and info.get("type") == "meta":
                            connected = bool(info.get("connected", False))
                            # First meta after connect: show notice if down
                            if self._port_up is None:
                                self._port_up = connected
                                if not connected:
                                    return "\r\n[Port disconnected on server]\r\n"
                                return b""  # suppress meta
                            # Transition changes
                            if self._port_up is True and not connected:
                                self._port_up = False
                                return "\r\n[Port disconnected on server]\r\n"
                            if self._port_up is False and connected:
                                self._port_up = True
                                return "\r\n[Reconnected]\r\n"
                        # For other meta updates, swallow
                        return b""
                    except Exception:
                        # If parsing fails, don't leak the raw control frame
                        return b""
                return data
            if msg.type == WSMsgType.BINARY:
                return msg.data
            if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                self.is_connected = False
                # proactively close session if not already
                if self._aiohttp_session:
                    try:
                        await self._aiohttp_session.close()
                    except Exception:
                        pass
                return None
            # For ping/pong or other control frames, return empty to indicate no payload
            return b""
        except asyncio.TimeoutError:
            # Timeout just means no data yet; return empty marker
            return b""
        except Exception as e:
            self.logger.error(f"Read failed: {e}", exc_info=True)
            self.is_connected = False
            if self._aiohttp_session:
                try:
                    await self._aiohttp_session.close()
                except Exception:
                    pass
            return None

    async def close(self):
        if not self.is_connected:
            # Even if logical state says disconnected, ensure session cleanup
            if self._aiohttp_session:
                try:
                    await self._aiohttp_session.close()
                except Exception:
                    pass
                finally:
                    self._aiohttp_session = None
            return
        try:
            if self.websocket:
                await self.websocket.close()
            if hasattr(self, "_aiohttp_session") and self._aiohttp_session:
                await self._aiohttp_session.close()
            self.logger.info("WebSocket closed")
        except Exception as e:
            self.logger.error(f"Close error: {e}", exc_info=True)
        finally:
            self.is_connected = False
            self.is_authenticated = False
            self.websocket = None
            if hasattr(self, "_aiohttp_session"):
                self._aiohttp_session = None
