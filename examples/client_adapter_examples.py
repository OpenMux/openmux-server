#!/usr/bin/env python3
"""
Example demonstrating the new OpenMux client adapter system
"""
import asyncio
import logging
from openmux.client.adapters import ClientAdapterFactory

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def example_tcp_standard_connection():
    """Example: Connect to OpenMux server using TCP adapter (standard protocol)"""
    print("\n=== TCP Standard Connection Example ===")
    
    # Create TCP adapter for standard server connection
    adapter = ClientAdapterFactory.create_adapter(
        host='localhost',
        port=8023,
        adapter_type='tcp',
        config={
            'protocol_type': 'standard',
            'use_tls': False
        }
    )
    
    try:
        # Connect to server
        if await adapter.connect():
            print("✓ Connected to server")
            
            # Authenticate
            if await adapter.authenticate_with_password('admin', 'admin'):
                print("✓ Authenticated successfully")
                
                # List available ports
                ports = await adapter.list_ports()
                print(f"✓ Available ports: {ports}")
                
                # Connect to a port (if available)
                if ports:
                    port_name = ports[0] if isinstance(ports[0], str) else ports[0].get('name', 'console1')
                    if await adapter.connect_to_port(port_name):
                        print(f"✓ Connected to port: {port_name}")
                        
                        # Send some data
                        if await adapter.send_data("test command\n"):
                            print("✓ Sent test command")
                            
                        # Read response
                        response = await adapter.read_data(timeout=1.0)
                        if response:
                            print(f"✓ Received: {response}")
            else:
                print("✗ Authentication failed")
        else:
            print("✗ Connection failed")
            
    except Exception as e:
        print(f"✗ Error: {e}")
        
    finally:
        await adapter.close()
        print("✓ Connection closed")




async def example_websocket_connection():
    """Example: Connect to OpenMux web server using WebSocket adapter"""
    print("\n=== WebSocket Connection Example ===")
    
    # Create WebSocket adapter for web server connection
    adapter = ClientAdapterFactory.create_adapter(
        host='localhost',
        port=8080,
        adapter_type='websocket',
        config={
            'use_tls': False,
            'path': '/ws/console1',  # Connect directly to a specific port
            'timeout': 10.0
        }
    )
    
    try:
        # Connect to WebSocket
        if await adapter.connect():
            print("✓ Connected to WebSocket")
            
            # Authenticate (if required by web server)
            if await adapter.authenticate_with_password('admin', 'admin'):
                print("✓ Authenticated successfully")
                
                # Send data through WebSocket
                if await adapter.send_data("help\n"):
                    print("✓ Sent command via WebSocket")
                    
                # Read response
                response = await adapter.read_data(timeout=2.0)
                if response:
                    print(f"✓ Received via WebSocket: {response}")
                    
            else:
                print("✗ Authentication failed")
        else:
            print("✗ WebSocket connection failed")
            
    except Exception as e:
        print(f"✗ Error: {e}")
        
    finally:
        await adapter.close()
        print("✓ WebSocket connection closed")


async def example_explicit_creation():
    """Example: Explicit adapter creation (new primary pattern)."""
    print("\n=== Explicit Adapter Creation Example ===")

    adapter = ClientAdapterFactory.create_adapter(
        host='localhost',
        port=8023,
        adapter_type='tcp',
        config={'use_tls': False}
    )

    print(f"✓ Created adapter: {type(adapter).__name__}")
    print(f"✓ Connection info: {adapter.get_connection_info()}")


async def example_configuration_driven():
    """Example: Configuration-driven adapter creation"""
    print("\n=== Configuration-Driven Example ===")
    
    # Configuration that could come from a YAML file
    connection_configs = [
        {
            'name': 'main_server',
            'adapter': 'tcp',
            'host': 'localhost',
            'port': 8023,
            'config': {
                'protocol_type': 'standard',
                'use_tls': False
            }
        },
        {
            'name': 'web_server',
            'adapter': 'websocket',
            'host': 'localhost',
            'port': 8080,
            'config': {
                'use_tls': False,
                'path': '/ws'
            }
        }
    ]
    
    # Create adapters from configuration
    adapters = {}
    for config in connection_configs:
        try:
            adapter = ClientAdapterFactory.create_adapter(
                host=config['host'],
                port=config['port'],
                adapter_type=config['adapter'],
                config=config['config']
            )
            adapters[config['name']] = adapter
            print(f"✓ Created {config['name']}: {type(adapter).__name__}")
            
        except Exception as e:
            print(f"✗ Failed to create {config['name']}: {e}")
    
    print(f"✓ Created {len(adapters)} adapters from configuration")


async def main():
    """Run all examples"""
    print("OpenMux Client Adapter System Examples")
    print("=" * 50)
    
    # Show supported adapter types
    supported_types = ClientAdapterFactory.get_supported_types()
    print(f"Supported adapter types: {supported_types}")
    
    # Run examples
    await example_explicit_creation()
    await example_configuration_driven()
    
    # Note: The connection examples would require running OpenMux servers
    # await example_tcp_standard_connection()
    # await example_websocket_connection()
    
    print("\n" + "=" * 50)
    print("All examples completed!")


if __name__ == "__main__":
    asyncio.run(main())
