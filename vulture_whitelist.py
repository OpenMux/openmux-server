"""Effective Vulture whitelist.

Vulture only treats names *defined* in this file as used (a list of string
names alone is ignored). We therefore create minimal stub symbols for any
APIs that are:
  * accessed indirectly via reflection / configuration
  * referenced only by string name (e.g. factory registrations)
  * public protocol builders or lifecycle hooks reserved for external use
  * dataclass field names that appear unused in isolation

Each stub uses ``...`` (Ellipsis) to avoid accidental runtime usage. The file
has no side‑effects when imported.
"""

# Fault injection / admin action endpoints (invoked via WebStatus or tests)
freeze_connection = ...  # vulture: ignore
unfreeze_connection = ...  # vulture: ignore
set_drop_heartbeats = ...  # vulture: ignore
force_close_connection = ...  # vulture: ignore
force_reset_connection = ...  # vulture: ignore

# Core lifecycle dispatch method invoked indirectly via managers
handle_lifecycle_event = ...  # vulture: ignore

# Public client API helpers (used in examples / external integrations)
get_connection_info = ...  # vulture: ignore
get_supported_types = ...  # vulture: ignore

# Dataclass configuration fields accessed dynamically
dtr = ...  # vulture: ignore
rts = ...  # vulture: ignore

def _export():  # pragma: no cover
    return [
        name
        for name in globals().keys()
        if name
        in {
            "freeze_connection",
            "unfreeze_connection",
            "set_drop_heartbeats",
            "force_close_connection",
            "force_reset_connection",
            "handle_lifecycle_event",
            "get_connection_info",
            "get_supported_types",
            "dtr",
            "rts",
        }
    ]

if __name__ == "__main__":  # pragma: no cover
    print("Whitelist entries:", len(_export()))
