import pytest

from openmux.server.adapters.web_console import WebConsoleAdapter
from openmux.server.auth_manager import AuthManager
from openmux.server.console_manager import ConsoleManager
from openmux.server.port_manager import PortManager


def test_snapshot_composite_id_derivation_local():
    wc = WebConsoleAdapter("wc", {"web_console": {"enable_ui": False}})
    auth = AuthManager({"users": []})
    pm = PortManager([])
    cm = ConsoleManager(pm, auth)
    wc.set_auth_manager(auth)
    wc.set_console_manager(cm)

    # Inject a dummy port into pm with a minimal get_status
    class Dummy:
        def __init__(self, name):
            self.name = name
        def get_status(self):
            return {"name": self.name, "adapter": "loopback", "is_running": True}

    pm.ports["P"] = Dummy("P")  # type: ignore
    snap = wc._get_ports_snapshot()
    entry = next(p for p in snap if p["name"] == "P")
    assert entry["id"].endswith("local::P")
