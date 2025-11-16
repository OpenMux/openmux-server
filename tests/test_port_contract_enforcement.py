import asyncio
import importlib
import inspect
import pkgutil
from typing import Set, Type, get_type_hints

import openmux.server.adapters as adapters_pkg
from openmux.server.adapters.lifecycle import PortState

# Names to ignore (utility modules, not port classes)
IGNORE_CLASS_NAMES = {"DynamicPortManager", "PortLifecycleEvent", "PortState", "BaseGenericAdapter"}

# Adapters we consider in scope (avoid pulling extremely large/legacy ones unless loaded)
ADAPTER_MODULE_NAME_FILTERS = ("loopback", "serial", "command", "tcp_initiator", "client_initiator", "muxcon")


def iter_adapter_modules():
    prefix = adapters_pkg.__name__ + "."
    for modinfo in pkgutil.iter_modules(adapters_pkg.__path__, prefix):  # type: ignore
        name = modinfo.name.rsplit(".", 1)[-1]
        if name.startswith("_"):
            continue
        if name not in ADAPTER_MODULE_NAME_FILTERS:
            continue
        try:
            yield importlib.import_module(modinfo.name)
        except Exception:
            # Skip modules that fail to import in test environment
            continue


def is_port_class(obj):
    if not inspect.isclass(obj):
        return False
    if obj.__name__ in IGNORE_CLASS_NAMES:
        return False
    # Accept classes ending with 'Port' OR 'Proxy' (federated remote ports), but not private prefixed
    if not (obj.__name__.endswith("Port") or obj.__name__.endswith("Proxy")):
        return False
    if obj.__name__.startswith("_"):
        return False
    # Heuristic: has start/stop coroutines and a write_data coroutine attribute
    required = ["start", "stop", "write_data"]
    for attr in required:
        if not hasattr(obj, attr):
            return False
    # Must define async methods for start/stop/write_data
    for attr in required:
        fn = getattr(obj, attr)
        if not inspect.iscoroutinefunction(fn):
            return False
    return True


def collect_port_classes():
    classes = []
    for mod in iter_adapter_modules():
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            if obj.__module__ != mod.__name__:
                continue
            if is_port_class(obj):
                classes.append(obj)
    return classes


NETWORK_PORT_NAME_HINTS = ("Tcp", "OpenMux", "Serial", "Remote", "MuxCon")


async def _ensure_write_and_callback_contract(cls):
    # Validate write_data return annotation
    write_fn = getattr(cls, "write_data")
    hints = get_type_hints(write_fn)
    ret = hints.get("return")
    assert ret is int, f"Port {cls.__name__} write_data must return int (got {ret})"
    # Validate data_callback attribute existence and type hint (if present in annotations)
    # We check annotations on the class dict if provided; not all classes may annotate at class-level
    annotations = getattr(cls, "__annotations__", {}) or {}
    has_attr = hasattr(cls, "data_callback") or "data_callback" in annotations
    if not has_attr:
        # Fallback: scan source for assignment to data_callback in __init__
        try:
            src = inspect.getsource(cls)
            has_attr = "data_callback" in src
        except OSError:
            pass
    assert has_attr, f"Port {cls.__name__} must define data_callback attribute per contract"

    # write_bytes alias requirement removed: ports must provide write_data only

    # start/stop return annotations (best-effort)
    start_fn = getattr(cls, "start")
    stop_fn = getattr(cls, "stop")
    s_hints = get_type_hints(start_fn)
    st_ret = s_hints.get("return")
    # start should advertise bool (some legacy may omit annotation; warn instead of fail if absent)
    assert st_ret in (bool, None), f"Port {cls.__name__} start should return bool (got {st_ret})"
    t_hints = get_type_hints(stop_fn)
    sp_ret = t_hints.get("return")
    assert sp_ret in (type(None), None), f"Port {cls.__name__} stop should return None (got {sp_ret})"

    # state attribute expectation with explicit annotation
    annotations2 = getattr(cls, "__annotations__", {}) or {}
    state_ann = annotations2.get("state")
    assert state_ann is PortState, f"Port {cls.__name__} must annotate state: PortState (got {state_ann})"

    # Network-style ports should expose is_connected flag for readiness logic
    if any(hint in cls.__name__ for hint in NETWORK_PORT_NAME_HINTS):
        annotations3 = getattr(cls, "__annotations__", {}) or {}
        ic_ann = annotations3.get("is_connected")
        assert ic_ann is bool, f"Port {cls.__name__} must annotate is_connected: bool for network readiness (got {ic_ann})"


def test_all_port_write_and_callback_contract():
    ports = collect_port_classes()
    # Sanity: we should find at least one
    assert ports, "No port classes discovered for contract enforcement test"
    loop = asyncio.new_event_loop()
    try:
        for cls in ports:
            loop.run_until_complete(_ensure_write_and_callback_contract(cls))
    finally:
        loop.close()
