"""Web plugins package for OpenMux WebConsole.

Each plugin module should expose a function:

    register_plugin(app, adapter, options=None) -> dict | None

The function should register routes on the provided aiohttp ``app`` and may
optionally return a mapping with metadata such as navigation entries:

- nav: list of dicts like {"title": "Config", "path": "/plugins/config-editor", "require": "admin"}

Security: Plugins should use adapter._require_permission(request, ("admin",))
to gate privileged operations and adapter._check_csrf(request) for state-changing
POST/PUT/PATCH/DELETE requests when session cookies are used.
"""

from typing import Final

from aiohttp import web

from openmux.server.adapters.base_adapter import BaseGenericAdapter

ADAPTER_APP_KEY: Final = web.AppKey("openmux_adapter", BaseGenericAdapter)
