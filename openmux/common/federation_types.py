"""Federation data structures and parsing utilities.

This module defines the core enum types and dataclasses used by the
MuxCon / OpenMux federation layer to represent:

* Server identity and capabilities across a federation chain.
* Client handshake metadata exchanged during initial HELLO negotiation.
* Port metadata (including multi-hop provenance and alias/conflict info).
* Active federation connection bookkeeping.
* Port registration request / result messages.

All public helper methods expose Google‑style docstrings describing
inputs and return values; they intentionally avoid side effects beyond
simple data transformation or validation.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class ServerType(Enum):
    """Enumerates roles a server may assume inside a federation.

    Values:
        HUB: A central aggregation / coordination server.
        LEAF: An edge server directly hosting ports/resources.
        RELAY: An intermediate hop forwarding federation traffic.
    """

    HUB = "hub"
    LEAF = "leaf"
    RELAY = "relay"


class ClientType(Enum):
    """Different client categories recognized during handshake.

    Values:
        HUB_CLIENT: Control/management client for a hub.
        PORT_REGISTRATION: Client pushing port registration payloads.
        REGULAR_CLIENT: Standard interactive console/port client.
    """

    HUB_CLIENT = "hub_client"
    PORT_REGISTRATION = "port_registration"
    REGULAR_CLIENT = "regular_client"


class FederationType(Enum):
    """How a port became visible at the current server.

    Values:
        LOCAL: Originated locally (no federation).
        PULL: Pulled from another server on demand.
        PUSH: Pushed proactively by an upstream server.
        MULTI_HOP: Traversed two or more relay hops.
    """

    LOCAL = "local"
    PULL = "pull"
    PUSH = "push"
    MULTI_HOP = "multi_hop"


@dataclass
class ServerInfo:
    """Describes a server participating in the federation chain.

    Attributes:
        server_id: Unique identifier for this server (stable across sessions).
        hostname: Resolved host name / address used for connectivity.
        port: TCP port the server exposes for federation communication.
        server_type: Logical role of the server (hub / leaf / relay).
        description: Optional human‑readable description of the server.
        connection_time: Wall‑clock timestamp when the connection was formed.
        capabilities: Feature flags / protocol extensions supported.
    """

    server_id: str
    hostname: str
    port: int
    server_type: ServerType
    description: str = ""
    connection_time: datetime = field(default_factory=datetime.now)
    capabilities: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON‑ready dictionary.

        Returns:
            Dict[str, Any]: Mapping with primitive JSON serializable values
            representing this server record.
        """
        return {
            "server_id": self.server_id,
            "hostname": self.hostname,
            "port": self.port,
            "server_type": self.server_type.value,
            "description": self.description,
            "connection_time": self.connection_time.isoformat(),
            "capabilities": self.capabilities,
        }


@dataclass
class MuxConHandshake:
    """Parsed representation of an incoming HELLO handshake line.

    Attributes:
        version: Protocol version token from the HELLO line.
        client_type: ClientType classification (inferred / provided).
        capabilities: Advertised capability tokens.
        server_id: Optional server identifier (if provided by peer).
        instance_id: Optional unique instance/session identifier.
        pk_id: Optional Ed25519 key identifier advertised by client for
            MuxCon authentication extension (if used).
    """

    version: str
    client_type: ClientType
    capabilities: List[str] = field(default_factory=list)
    server_id: Optional[str] = None
    instance_id: Optional[str] = None
    pk_id: Optional[str] = None

    @classmethod
    def parse(cls, line: str) -> Optional["MuxConHandshake"]:
        """Parse a raw HELLO handshake line into a structured object.

            The expected minimal format is: ``HELLO <version> [KV pairs]`` where
        optional key=value fragments include TYPE, CAPS, ID, INST, PKID.

            Args:
                line: Raw newline‑stripped line beginning with ``HELLO``.

            Returns:
                MuxConHandshake | None: Parsed handshake instance on success,
                otherwise ``None`` if the line is malformed or does not begin
                with the required prefix.
        """
        parts = line.strip().split()
        if len(parts) < 2 or parts[0] != "HELLO":
            return None

        version = parts[1]
        client_type = ClientType.REGULAR_CLIENT  # default
        capabilities: List[str] = []
        server_id: Optional[str] = None
        instance_id: Optional[str] = None
        pk_id: Optional[str] = None

        for part in parts[2:]:
            if part.startswith("TYPE="):
                try:
                    client_type = ClientType(part[5:])
                except ValueError:
                    client_type = ClientType.REGULAR_CLIENT
            elif part.startswith("CAPS="):
                capabilities = part[5:].split(",") if part[5:] else []
            elif part.startswith("ID="):
                server_id = part[3:] or None
            elif part.startswith("INST="):
                instance_id = part[5:] or None
            elif part.startswith("PKID="):
                pk_id = part[5:] or None
        return cls(version, client_type, capabilities, server_id=server_id, instance_id=instance_id, pk_id=pk_id)


@dataclass
class PortMetadata:
    """Federation‑aware metadata describing an exposed port/resource.

    Attributes:
        name: Display name (local or possibly alias) of the port.
        original_name: Original canonical name prior to alias/conflict changes.
        description: Human readable description.
        adapter_type: Underlying adapter/transport type hosting the port.
        origin_server: ServerInfo for the original source server.
        server_chain: Ordered list of ServerInfo objects representing the
            traversal chain (origin first, current server last).
        status: Current availability / state indicator string.
        connected_clients: Count of currently attached client sessions.
        max_rw_users: Max simultaneous read‑write clients allowed.
        has_alias: Whether an alias is applied (conflict / rename resolution).
        alias_name: The alias used if ``has_alias`` is True.
        conflict_reason: Human explanation for alias/conflict scenario.
        federation_type: How the port entered the federation view.
        last_seen: Timestamp of last successful refresh / observation.
    """

    name: str
    original_name: str
    description: str
    adapter_type: str

    # Federation chain information
    origin_server: ServerInfo
    server_chain: List[ServerInfo]

    # Port status
    status: str
    connected_clients: int = 0
    max_rw_users: int = 1

    # Conflict resolution
    has_alias: bool = False
    alias_name: Optional[str] = None
    conflict_reason: Optional[str] = None

    # Federation info
    federation_type: FederationType = FederationType.LOCAL
    last_seen: datetime = field(default_factory=datetime.now)

    # Optional serial configuration and live line-status
    # These are included for serial-like ports to provide richer diagnostic info
    # and may be omitted for other adapter types.
    serial_config: Optional[Dict[str, Any]] = None
    line_status: Optional[Dict[str, Any]] = None

    def get_display_name(self) -> str:
        """Return the user‑facing port name.

        Prefers ``alias_name`` when an alias is active; falls back to the
        canonical ``name`` attribute otherwise.

        Returns:
            str: Preferred display name for UI / API consumers.
        """
        return self.alias_name if self.has_alias and self.alias_name else self.name

    def get_server_chain_string(self) -> str:
        """Build a human‑readable representation of the server chain.

        Returns:
            str: Single server_id when no hops; otherwise an arrow‑joined
            chain with a ``(multi-hop)`` suffix.
        """
        if len(self.server_chain) <= 1:
            return self.origin_server.server_id

        chain = " → ".join([s.server_id for s in self.server_chain])
        return f"{chain} (multi-hop)"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dictionary suitable for external API responses.

        Returns:
            Dict[str, Any]: Mapping of public attributes (with computed
            display name) to primitive JSON‑serializable values.
        """
        return {
            "name": self.get_display_name(),
            "original_name": self.original_name,
            "description": self.description,
            "adapter_type": self.adapter_type,
            "status": self.status,
            "connected_clients": self.connected_clients,
            "max_rw_users": self.max_rw_users,
            "origin_server": self.origin_server.server_id,
            "server_chain": [s.server_id for s in self.server_chain],
            "federation_type": self.federation_type.value,
            "has_alias": self.has_alias,
            "alias_name": self.alias_name,
            "conflict_reason": self.conflict_reason,
            "last_seen": self.last_seen.isoformat(),
            # Optional: include richer details when present
            **({"serial_config": self.serial_config} if self.serial_config is not None else {}),
            **({"line_status": self.line_status} if self.line_status is not None else {}),
        }

    def to_federation_dict(self) -> Dict[str, Any]:
        """Serialize for federation protocol transmission.

        Always emits the original (non‑aliased) name to avoid propagating
        local aliasing decisions upstream.

        Returns:
            Dict[str, Any]: Subset mapping required for federation messages.
        """
        return {
            "name": self.original_name,
            "description": self.description,
            "adapter_type": self.adapter_type,
            # V2: send full origin server object and detailed chain entries
            "origin_server": self.origin_server.to_dict(),
            "server_chain_info": [s.to_dict() for s in self.server_chain],
            "max_rw_users": self.max_rw_users,
            "status": self.status,
            "has_alias": self.has_alias,
            "federation_type": self.federation_type.value,
            # Optional serial details where applicable
            **({"serial_config": self.serial_config} if self.serial_config is not None else {}),
            **({"line_status": self.line_status} if self.line_status is not None else {}),
        }
