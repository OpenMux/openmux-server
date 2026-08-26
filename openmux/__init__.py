"""
OpenMux - Serial Port Management System.

`__version__` reads the installed distribution metadata (same source the web
console status endpoint uses) so the telnet/SSH session banners always match
the pyproject.toml version.
"""

try:  # Prefer importlib.metadata (std lib)
    from importlib.metadata import version as _dist_version  # type: ignore

    __version__ = _dist_version("openmux")
except Exception:  # pragma: no cover - justification: package not installed (bare source checkout); keep import working
    __version__ = "unknown"
