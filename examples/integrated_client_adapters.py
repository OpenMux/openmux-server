#!/usr/bin/env python3
"""Demonstrate the OpenMux client adapter factory.

Constructs one ``tcp`` and one ``websocket`` adapter, prints their connection
info, and lists the supported adapter types. Nothing here connects to a server.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))

from openmux.client.adapters import ClientAdapterFactory


def main() -> None:
	adapters = {
		"tcp": ClientAdapterFactory.create_adapter(
			host="localhost",
			port=8023,
			adapter_type="tcp",
			config={"use_tls": False},
		),
		"websocket": ClientAdapterFactory.create_adapter(
			host="localhost",
			port=8080,
			adapter_type="websocket",
			config={"use_tls": False, "path": "/ws"},
		),
	}

	for name, adapter in adapters.items():
		info = adapter.get_connection_info()
		print(f"{name}: {type(adapter).__name__} -> {info['host']}:{info['port']}")
		print(f"  ready: {adapter.is_ready()} | connected: {info['connected']}")

	print(f"\nsupported adapter types: {ClientAdapterFactory.get_supported_types()}")


if __name__ == "__main__":
	main()
