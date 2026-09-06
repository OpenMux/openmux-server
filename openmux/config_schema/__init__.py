"""Read-only JSON Schemas (draft 2020-12) shipped with the package.

One YAML schema per config file (server.yaml, authentication.yaml,
security.yaml, client config). Resolve a file through
``openmux.server.locations.schema_file`` or
``openmux.server.locations.server_schema_file`` instead of hardcoding a
path, so dev checkouts, editable installs, wheels, and Docker images all
find the same copy.
"""
