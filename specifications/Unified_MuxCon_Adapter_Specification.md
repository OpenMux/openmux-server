# Unified MuxCon Adapter Specification

## Overview

This specification defines the design for a unified MuxCon adapter that consolidates the current separate client and server adapters into a single, flexible component. The unified adapter eliminates code duplication while supporting bidirectional federation over single TCP connections with flexible capability-based configuration.

## Background

### Current Architecture Issues
- **Code Duplication**: Separate `MuxConServerAdapter` and `MuxConClientAdapter` with ~70-80% overlapping functionality
- **Rigid Roles**: Fixed initiator/listener relationships that don't support flexible topologies
- **Configuration Complexity**: Different configuration patterns for client vs server sides
- **Limited Bidirectionality**: Difficulty supporting nodes with different federation behaviors to different neighbors

### Design Goals
1. **Eliminate Duplication**: Single adapter supporting both client and server modes
2. **Flexible Topologies**: Nodes can have different federation behaviors to different neighbors
3. **Unified Configuration**: Same capability format for all connection types
4. **Bidirectional Support**: Full port sharing in both directions over single TCP connections
5. **Pattern-Based Policies**: Flexible node matching for scalable configuration

## Architecture Design

### Core Principles

#### 1. Connection Direction vs Federation Behavior
- **TCP Connection Direction**: Purely a networking detail (initiator vs listener)
- **Federation Behavior**: Determined by capabilities, independent of connection direction
- **Unified Capabilities**: Same capability system whether connecting or accepting connections

#### 2. Capability-Based Configuration
Three fundamental capabilities replace rigid connection roles:
- **`share_ports`**: Ports we expose to the remote node
- **`accept_ports`**: Port patterns we accept registrations for from remote node  
- **`request_ports`**: Ports we want to access on the remote node

#### 3. Session-Based Communication
- **No Connection Forwarding**: All connections terminate and restart at each node
- **Session Multiplexing**: Multiple port sessions over single TCP connection
- **Bidirectional Data Flow**: Data can flow in both directions simultaneously

## Configuration Specification

### Unified Configuration Format

```yaml
# Server Identity
server:
  host: 0.0.0.0
  port: 7822
  node_name: datacenter-hub-01  # Our identity in federation

# Local Ports
ports:
  - name: server_console
    adapter: serial
    device: /dev/ttyS0
  - name: switch_console
    adapter: telnet
    host: 192.168.1.10

# Unified MuxCon Configuration
muxcon:
  # Listener Configuration - Accept Incoming TCP Connections
  listeners:
    - enabled: true
      host: 0.0.0.0
      port: 7822
      accept_regular_clients: true  # Non-federation MuxCon clients
    
    # Federation Policies - Pattern-Based Node Matching
    federation_policies:
      - node_pattern: "edge-*"
        share_ports: []                    # Don't share our ports
        accept_ports: ["edge_*"]           # Accept their edge ports
        request_ports: []                  # Don't request from them
        max_connections: 10
        auth:
          api_key_prefix: "edge-"
          
      - node_pattern: "regional-hub-*"
        share_ports: ["server_console"]    # Share with regional nodes
        accept_ports: ["regional_*"]       # Accept regional ports
        request_ports: ["regional_db"]     # Request specific resources
        max_connections: 3
        auth:
          api_key_prefix: "regional-"
          
      - node_pattern: "*"                 # Catch-all policy
        share_ports: []
        accept_ports: []
        request_ports: []
        max_connections: 1
        auth:
          require_admin: true
  
  # Initiator Configuration - Make Outgoing TCP Connections
  initiators:
    - node_name: corporate-hq
      host: corporate.example.com
      port: 7822
      share_ports: ["server_console", "edge_*"]   # Share local + aggregated
      accept_ports: []                            # Don't accept from corporate
      request_ports: ["corporate_*", "global_*"]  # Request corporate resources
      auth:
        api_key: datacenter-hub-key
      connection:
        auto_reconnect: true
        heartbeat_interval: 30
        retry_interval: 60
        
    - node_name: backup-site
      host: backup.example.com
      port: 7822
      share_ports: ["server_console"]             # Emergency access only
      accept_ports: []
      request_ports: []
      auth:
        api_key: backup-connection-key
```

### Pattern Matching System

#### Node Pattern Syntax
```yaml
# Exact match
node_pattern: "edge-device-1"

# Wildcard patterns
node_pattern: "lab-*"           # Matches lab-1, lab-foo, lab-anything
node_pattern: "*-backup"        # Matches any-backup

# Regex patterns
node_pattern:
  regex: "^datacenter-[0-9]+$"  # Matches datacenter-1, datacenter-2, etc.

# Prefix matching
node_pattern:
  prefix: "edge"               # Matches edge, edge-1, edge-device, etc.

# Multiple patterns (OR logic)
node_patterns: ["test-*", "dev-*", "staging-*"]
```

#### Port Pattern Syntax
```yaml
# Exact port names
share_ports: ["console1", "switch1"]

# Wildcard patterns
share_ports: ["lab_*", "test_*"]

# Exclusion patterns
share_ports: ["*", "!admin_*", "!secret_*"]  # All except admin/secret

# Regex patterns
share_ports:
  - pattern: "console[0-9]+"
    type: regex

# Dynamic patterns (runtime evaluation)
share_ports:
  - pattern: "dynamic"
    ports_function: "get_available_ports"
```

## Implementation Architecture

### Unified Adapter Class Structure

```python
class UnifiedMuxConAdapter(BaseServerAdapter):
    """Unified MuxCon adapter supporting both client and server modes"""
    
    def __init__(self, name: str, config: Dict[str, Any]):
        # Initialize common components
        self.protocol = MuxConProtocolHandler()
        self.session_manager = SessionManager()
        self.capability_engine = CapabilityEngine(config)
        
        # Initialize listener if enabled
    listeners_cfg = [l for l in config.get('listeners', []) if l.get('enabled')]
    self.listeners = [MuxConListener(lc) for lc in listeners_cfg]
            
        # Initialize initiators
        self.initiators = [
            MuxConInitiator(init_config) 
            for init_config in config.get('initiators', [])
        ]
    
    async def start(self):
        """Start both listener and initiators"""
        tasks = []
        
    for l in self.listeners:
      tasks.append(l.start())
            
        for initiator in self.initiators:
            tasks.append(initiator.start())
            
        await asyncio.gather(*tasks)
    
    async def handle_connection(self, reader, writer, is_initiator=False):
        """Unified connection handler for both directions"""
        # Common handshake and capability negotiation
        # Session management and data routing
        # Bidirectional port registration
```

### Capability Engine

```python
class CapabilityEngine:
    """Manages capability negotiation and port matching"""
    
    def find_matching_policy(self, node_name: str) -> Optional[FederationPolicy]:
        """Find policy matching node name using pattern matching"""
        
    def negotiate_capabilities(self, local_caps: List[str], 
                             remote_caps: List[str]) -> Dict[str, Any]:
        """Negotiate final capabilities between nodes"""
        
    def get_shareable_ports(self, policy: FederationPolicy) -> List[str]:
        """Get ports matching share_ports patterns"""
        
    def should_accept_port(self, port_name: str, policy: FederationPolicy) -> bool:
        """Check if port matches accept_ports patterns"""
```

### Session Manager

```python
class SessionManager:
    """Manages bidirectional sessions over single TCP connection"""
    
    def create_session(self, port_name: str, client_id: str) -> int:
        """Create new session with unique ID"""
        
    def route_data(self, session_id: int, data: bytes):
        """Route data to appropriate port/client"""
        
    def cleanup_session(self, session_id: int):
        """Clean up session resources"""
```

## Protocol Extensions

### Enhanced Handshake

```text
# Enhanced handshake with capabilities
C→S: HELLO MuxCon/1.0 TYPE=federation NODE_NAME=edge-device-17 CAPS=register_ports,request_ports
S→C: OK MuxCon/1.0 NODE_NAME=regional-hub CAPS=accept_port_registrations,expose_ports POLICY_MATCH=edge-*

# Authentication
C→S: AUTH api edge-device-key-123
S→C: OK AUTHENTICATED

# Bidirectional capability negotiation
C→S: #0:C:67:CAPABILITIES:DECLARE share=edge_console,edge_sensors accept= request=regional_*,shared_*
S→C: #0:C:45:CAPABILITIES:DECLARE share= accept=edge_* request=

# Capability confirmation
C→S: #0:C:35:CAPABILITIES:CONFIRMED share=edge_console,edge_sensors request=shared_storage
S→C: #0:C:28:CAPABILITIES:CONFIRMED accept=edge_console,edge_sensors share=shared_storage
```

### Bidirectional Port Registration

```text
# Both sides can register ports simultaneously
C→S: #0:C:234:PORTS:REGISTER:edge-device-17:1
     [{"name":"edge_console","description":"Edge device console",...}]
     
S→C: #0:C:189:PORTS:REGISTER:regional-hub:1  
     [{"name":"shared_storage","description":"Regional shared storage",...}]

# Both sides acknowledge
S→C: #0:C:89:PORTS:REGISTERED:1:0
     [{"name":"edge_console","final_name":"edge_console","accepted":true}]
     
C→S: #0:C:67:PORTS:REGISTERED:1:0
     [{"name":"shared_storage","final_name":"shared_storage","accepted":true}]
```

## Topology Examples

### Example 1: Hierarchical Tree
```yaml
# Corporate HQ (Root)
muxcon:
  listener:
    federation_policies:
      - node_pattern: "regional-*"
        share_ports: ["corporate_*"]
        accept_ports: ["regional_*", "edge_*"]
  initiators: []

# Regional Node (Branch)  
muxcon:
  listener:
    federation_policies:
      - node_pattern: "edge-*"
        share_ports: ["corporate_*", "regional_*"]
        accept_ports: ["edge_*"]
  initiators:
    - node_name: corporate-hq
      share_ports: ["regional_summary", "edge_*"]
      request_ports: ["corporate_*"]

# Edge Device (Leaf)
muxcon:
  listener:
    enabled: false
  initiators:
    - node_name: regional-node
      share_ports: ["edge_*"]
      request_ports: ["corporate_*", "regional_*"]
```

### Example 2: Peer Mesh
```yaml
# Each peer has same configuration with different targets
muxcon:
  listener:
    federation_policies:
      - node_pattern: "peer-*"
        share_ports: ["local_*"]
        accept_ports: ["peer_*"]
        request_ports: ["peer_shared_*"]
  initiators:
    - node_name: peer-west
      share_ports: ["local_*"]
      accept_ports: ["peer_*"]
      request_ports: ["peer_shared_*"]
    - node_name: peer-south  
      share_ports: ["local_*"]
      accept_ports: ["peer_*"]
      request_ports: ["peer_shared_*"]
```

### Example 3: Hybrid Topology
```yaml
# Node with different federation behaviors to different neighbors
muxcon:
  listener:
    federation_policies:
      # Accept ports from edge devices, don't share
      - node_pattern: "edge-*"
        share_ports: []
        accept_ports: ["edge_*"]
        request_ports: []
        
      # Bidirectional sharing with other regional nodes
      - node_pattern: "regional-*"
        share_ports: ["regional_services"]
        accept_ports: ["peer_regional_*"] 
        request_ports: ["peer_shared_*"]
        
  initiators:
    # Share aggregated data upstream, request corporate resources
    - node_name: corporate-hq
      share_ports: ["edge_*", "regional_*"]
      accept_ports: []
      request_ports: ["corporate_*"]
      
    # Bidirectional sharing with backup site
    - node_name: backup-datacenter
      share_ports: ["critical_*"]
      accept_ports: ["backup_*"]
      request_ports: ["backup_emergency_*"]
```

## Migration Strategy

### Phase 1: Unified Adapter Implementation
1. Create `UnifiedMuxConAdapter` class
2. Implement capability engine and pattern matching
3. Add bidirectional session management
4. Update configuration parsing

### Phase 2: Protocol Enhancements
1. Enhance handshake for capability negotiation
2. Add bidirectional port registration
3. Update frame routing for unified sessions
4. Add authentication integration

### Phase 3: Backwards Compatibility
1. Support legacy configuration formats
2. Gradual migration path from separate adapters
3. Protocol version negotiation
4. Fallback modes for older clients

### Phase 4: Deprecation
1. Mark old adapters as deprecated
2. Update documentation and examples
3. Migration tooling for existing configurations
4. Remove old adapters in future release

## Benefits Summary

### Code Reduction
- **~70-80% code reuse** between client/server modes
- **Single protocol implementation** for frame handling
- **Unified session management** for both directions
- **Shared capability and authentication systems**

### Configuration Simplification  
- **Same format** for initiator and listener capabilities
- **Pattern-based policies** reduce configuration duplication
- **Template support** for common topology patterns
- **Clear separation** between connection and federation behavior

### Operational Flexibility
- **Dynamic topologies** - nodes can have different federation behaviors per neighbor
- **Bidirectional federation** over single TCP connections
- **Mesh, tree, and hybrid** topologies supported
- **Runtime capability negotiation** for adaptive behavior

### Maintainability
- **Single point of maintenance** for MuxCon protocol logic
- **Centralized testing** for all federation scenarios  
- **Consistent behavior** across all connection types
- **Simplified debugging** with unified logging and metrics

This unified design addresses all identified limitations while providing a clean, flexible foundation for future federation enhancements.
