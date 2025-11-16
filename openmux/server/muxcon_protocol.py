"""
MuxCon protocol handler with federation extensions
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from ..common.federation_types import (
    ClientType,
    FederationType,
    MuxConHandshake,
    PortMetadata,
    ServerInfo,
    ServerType,
)


class MuxConProtocolHandler:
    """Enhanced MuxCon protocol handler with federation support"""

    def __init__(self):
        self.logger = logging.getLogger("openmux.muxcon_protocol")

    # Frame creation methods

    def create_control_frame(self, stream_id: int, seq: int, payload: str) -> bytes:
        """Create control frame with sequence (#sid:C:len:seq:)"""
        payload_bytes = payload.encode("utf-8")
        header = f"#{stream_id}:C:{len(payload_bytes)}:{seq}:"
        return header.encode("ascii") + payload_bytes + b"\n"

    def create_command_frame(self, command: str, stream_id: int, seq: int, payload: str = "") -> bytes:
        cmd = (command or "").upper()
        payload_bytes = payload.encode("utf-8")
        header = f"#{stream_id}:{cmd}:{len(payload_bytes)}:{seq}:"
        return header.encode("ascii") + payload_bytes + b"\n"

    def create_data_frame(self, stream_id: int, seq: int, data: bytes) -> bytes:
        """Create data frame with sequence"""
        header = f"#{stream_id}:D:{len(data)}:{seq}:"
        return header.encode("ascii") + data + b"\n"

    def create_stream_open_frame(self, stream_id: int, seq: int, port_name: str) -> bytes:
        port_bytes = port_name.encode("utf-8")
        header = f"#{stream_id}:O:{len(port_bytes)}:{seq}:"
        return header.encode("ascii") + port_bytes + b"\n"

    def create_stream_close_frame(self, stream_id: int, seq: int, reason: str = "") -> bytes:
        reason_bytes = reason.encode("utf-8")
        header = f"#{stream_id}:E:{len(reason_bytes)}:{seq}:"
        return header.encode("ascii") + reason_bytes + b"\n"

    def create_ack_frame(self, acked_seq: int, seq: int) -> bytes:
        """Create a data ACK frame acknowledging a specific DATA sequence.

        The frame type is 'A' and the payload is the decimal string of the
        acknowledged DATA frame sequence number.
        """
        payload = str(int(acked_seq)).encode("utf-8")
        header = f"#0:A:{len(payload)}:{seq}:"
        return header.encode("ascii") + payload + b"\n"

    # Response creation methods

    def create_handshake_response(
        self, capabilities: List[str], server_id: Optional[str] = None, instance_id: Optional[str] = None
    ) -> str:
        """Create handshake response including server identity tokens if provided"""
        parts = ["OK", "MuxCon/1.0"]
        if capabilities:
            parts.append(f"CAPS={','.join(capabilities)}")
        if server_id:
            parts.append(f"ID={server_id}")
        if instance_id:
            parts.append(f"INST={instance_id}")
        return " ".join(parts)

    def create_federated_port_list_response(self, ports: List[PortMetadata], seq: int) -> bytes:
        """Create PORTS:FEDERATED response frame with proper length"""
        port_lines = []
        for port in ports:
            port_dict = port.to_federation_dict()
            port_lines.append(json.dumps(port_dict, separators=(",", ":")))

        port_data = "\n".join(port_lines)
        payload = f"PORTS:FEDERATED:{len(ports)}\n{port_data}\nEND:PORTS"

        return self.create_control_frame(0, seq, payload)

    # Heartbeat request/ack helpers (two-way ping/pong)
    def create_heartbeat_request(self, ts: float, seq: int) -> bytes:
        """Create a HEARTBEAT request frame with timestamp using HB command."""
        return self.create_command_frame("HB", 0, seq, f"REQ:{int(ts)}")

    def create_heartbeat_ack(self, ts: float, seq: int) -> bytes:
        """Create a HEARTBEAT ack frame echoing the timestamp using HB command."""
        return self.create_command_frame("HB", 0, seq, f"ACK:{int(ts)}")

    # Parsing methods

    def parse_handshake(self, line: str) -> Optional[MuxConHandshake]:
        """Parse enhanced HELLO line"""
        return MuxConHandshake.parse(line)

    def parse_frame_header(self, line: bytes) -> Optional[Tuple[int, str, int, int]]:
        """
        Parse MuxCon frame header

        Returns:
            Tuple of (stream_id, frame_type, payload_length, seq) or None if invalid
        """
        try:
            line_str = line.decode("ascii").strip()
            if not line_str.startswith("#"):
                return None

            # Remove # and split by :
            parts = line_str[1:].split(":", 4)
            # Expect at least 5 parts: sid, type, len, seq, '' (because header ends with ':')
            if len(parts) < 4:
                return None
            # Accept both with and without trailing empty part
            stream_id = int(parts[0])
            frame_type = parts[1]
            payload_length = int(parts[2])
            seq_part = parts[3]
            try:
                seq_val = int(seq_part)
            except ValueError:
                return None
            return stream_id, frame_type, payload_length, seq_val

        except (ValueError, UnicodeDecodeError):
            return None

    def validate_capabilities(self, capabilities: List[str]) -> List[str]:
        """Validate and filter supported capabilities"""
        supported_caps = {
            "multi_hop",
            "conflict_resolution",
            "metadata",
            "topology_discovery",
            "port_federation",
            "remote_registration",
            "chain_tracking",
        }

        return [cap for cap in capabilities if cap in supported_caps]

    def create_port_list_request(self, seq: int) -> bytes:
        """Create PORTS:LIST:FEDERATED request frame"""
        return self.create_control_frame(0, seq, "PORTS:LIST:FEDERATED")
