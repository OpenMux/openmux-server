"""Behavior tests for GenericAdapterFactory.create_adapters_from_config.

The function already has full line coverage from test_adapter_factory.py and
test_adapter_factory_security.py; these tests pin the OUTCOME of each control
flow branch (fail-fast resolution, unified adapter-type resolution and skips,
legacy validation/policy/type-detection skips, fail-fast abort) so the
c901 refactor is provably behavior-preserving.
"""

from typing import Any, Dict, Optional, Set

import pytest

from openmux.server.adapters.base_adapter import AdapterCapability, BaseGenericAdapter
from openmux.server.adapters.factory import AdapterPlugin, GenericAdapterFactory, PluginRegistry


class DummyAdapter(BaseGenericAdapter):
    """Minimal concrete adapter. Mirrors the one in test_adapter_factory.py.

    Works on BOTH the unified and legacy paths: the legacy
    _create_single_adapter wraps list configs and attaches a DynamicPortManager;
    nothing here requires real ports."""

    adapter_type = "dummy"

    def get_capabilities(self) -> Set[AdapterCapability]:
        return set()

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> bool:
        # Accept both unified and legacy styles for tests
        return isinstance(config, dict)

    async def _do_start(self) -> bool:
        return True

    async def stop(self) -> None:
        pass

    async def create_port(self, port_name: str, config: Dict[str, Any]) -> Optional[Any]:
        return None

    async def destroy_port(self, port_name: str) -> None:
        pass

    def get_port_configurations(self) -> Dict[str, Dict[str, Any]]:
        return {}


class NoValidateAdapter(DummyAdapter):
    # Shadow the inherited (callable) validate_config so the factory skips validation entirely.
    validate_config = None


class BadValidateAdapter(DummyAdapter):
    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> bool:
        return False


class RaisingAdapter(DummyAdapter):
    def __init__(self, name: str, config: Any):
        raise RuntimeError("ctor boom")


class LegacyRaiseTypeAdapter(DummyAdapter):
    @staticmethod
    def get_adapter_type(*a, **k):
        raise RuntimeError("type boom")


def _reg(*plugins):
    # Bypass PluginRegistry.__init__ (built-in plugin import); seed the same
    # internal maps the real registry uses, mirroring register_plugin's
    # adapter_type index rules (first plugin wins).
    reg = PluginRegistry.__new__(PluginRegistry)
    reg._plugins = {}
    reg._import_errors = {}
    for p in plugins:
        reg._plugins[p.config_section] = p
        at = getattr(p.adapter_class, "adapter_type", None)
        if isinstance(at, str) and at:
            idx = getattr(reg, "_adapter_type_index", None)
            if idx is None:
                idx = {}
                reg._adapter_type_index = idx
            if at.lower() not in idx:
                idx[at.lower()] = p
    return reg


def _plugin(name, section, cls):
    return AdapterPlugin(name, section, cls)


def _factory(reg=None, policy=None):
    return GenericAdapterFactory(reg, security_policy=policy)


# ---------------------------------------------------------------- fail-fast resolution
def test_fail_fast_default():
    fac = _factory(_reg(_plugin("Dummy", "dummy_section", DummyAdapter)))
    res = fac.create_adapters_from_config({"server": {}, "dummy_section": {}})
    assert len(res) == 1  # default enabled; nothing missing so no abort


def test_fail_fast_str_false():
    for raw in ("false", "False", "0", "no", "off"):
        fac = _factory(_reg())
        res = fac.create_adapters_from_config({"server": {"fail_fast_adapters": raw}, "unknown_section": {1: 2}})
        assert isinstance(res, list), raw
        assert len(res) == 0


def test_fail_fast_bool_false():
    fac = _factory(_reg())
    assert fac.create_adapters_from_config({"server": {"fail_fast_adapters": False}, "unknown_section": {1: 2}}) == []


def test_fail_fast_true_values_stay_on():
    for raw in (True, "yes", 1, "on", "TRUE"):
        fac = _factory(_reg())
        with pytest.raises(RuntimeError):
            fac.create_adapters_from_config({"server": {"fail_fast_adapters": raw}, "unknown_section": {1: 2}})


def test_fail_fast_top_level_key_when_server_value_none():
    # server value None -> falls back to top-level config key = "false"
    fac = _factory(_reg())
    res = fac.create_adapters_from_config(
        {"server": {"fail_fast_adapters": None}, "fail_fast_adapters": "false", "unknown_section": {1: 2}}
    )
    assert res == []


def test_fail_fast_server_value_wins():
    # explicit server-level "false" + top-level True -> server wins (checked first)
    fac = _factory(_reg())
    res = fac.create_adapters_from_config(
        {"server": {"fail_fast_adapters": "false"}, "fail_fast_adapters": True, "unknown_section": {1: 2}}
    )
    assert res == []


class _RaisingCfg(dict):
    """Seedable dict whose get() raises, to force the defensive
    fail-fast parse fallback (which must leave fail_fast on)."""

    def get(self, *a, **k):
        raise RuntimeError("cfg boom")


def test_fail_fast_parsing_exception_falls_back_to_true():
    fac = _factory(_reg())
    cfg = _RaisingCfg(unknown_section={1: 2})
    with pytest.raises(RuntimeError) as ei:
        fac.create_adapters_from_config(cfg)
    # parse failed -> fail_fast stays on -> missing section aborts startup
    assert "fail-fast" in str(ei.value)


# ---------------------------------------------------------------- unified path
def test_unified_type_none_name_fallback():
    fac = _factory(_reg())
    # no "type" -> plugin None -> warning + skip
    res = fac.create_adapters_from_config({"server": {}, "adapters": [{"name": "orphan"}, {"loopback": {}}]})
    assert res == []


def test_unified_created_via_type_index_and_bare_section_candidate():
    # 'alpha' resolvable ONLY via the dynamic adapter_type index (its section
    # name is neither <type>_ports nor <type>); 'beta_section' resolvable ONLY
    # via the bare <type> candidate, after the <type>_ports candidate misses.

    class AlphaAdapter(DummyAdapter):
        adapter_type = "alpha"

    class BetaAdapter(DummyAdapter):
        adapter_type = "beta"

    fac = _factory(_reg(_plugin("Alpha", "alpha_real", AlphaAdapter), _plugin("Beta", "beta_section", BetaAdapter)))
    res = fac.create_adapters_from_config(
        {
            "server": {},
            "adapters": [
                {"type": "alpha", "name": "a1"},
                {"type": "beta_section", "name": "b1"},
            ],
        }
    )
    assert [a.name for a in res] == ["a1", "b1"]
    assert [type(a) for a in res] == [AlphaAdapter, BetaAdapter]


def test_unified_ports_candidate_before_bare():
    # when BOTH <type>_ports and <type> sections exist, <type>_ports wins (first candidate)
    class A1(DummyAdapter):
        pass

    class A2(DummyAdapter):
        pass

    fac = _factory(_reg(_plugin("P1", "dup_ports", A1), _plugin("P2", "dup", A2)))
    res = fac.create_adapters_from_config({"server": {}, "adapters": [{"type": "dup", "name": "x"}]})
    assert len(res) == 1
    assert type(res[0]) is A1


def test_unified_no_plugin_skips():
    fac = _factory(_reg())
    res = fac.create_adapters_from_config({"server": {}, "adapters": [{"type": "nosuch", "name": "n"}]})
    assert res == []


def test_unified_bad_validate_skips():
    fac = _factory(_reg(_plugin("Bad", "bad_ports", BadValidateAdapter), _plugin("Good", "good_ports", DummyAdapter)))
    res = fac.create_adapters_from_config(
        {"server": {}, "adapters": [{"type": "bad", "name": "b"}, {"type": "good", "name": "g"}]}
    )
    assert [a.name for a in res] == ["g"]


def test_unified_no_validate_runs():
    fac = _factory(_reg(_plugin("NV", "nv_ports", NoValidateAdapter)))
    res = fac.create_adapters_from_config({"server": {}, "adapters": [{"type": "nv", "name": "n"}]})
    assert len(res) == 1


def test_unified_policy_blocks():
    class _Policy:
        def is_adapter_allowed(self, *, adapter_type):
            return adapter_type != "alpha"

    fac = _factory(_reg(_plugin("Alpha", "alpha_ports", DummyAdapter)), policy=_Policy())
    res = fac.create_adapters_from_config({"server": {}, "adapters": [{"type": "alpha", "name": "x"}]})
    assert res == []


def test_unified_ctor_error_skips_and_continues():
    fac = _factory(_reg(_plugin("Boom", "boom_ports", RaisingAdapter), _plugin("Ok", "ok_ports", DummyAdapter)))
    res = fac.create_adapters_from_config(
        {"server": {}, "adapters": [{"type": "boom", "name": "x"}, {"type": "ok", "name": "y"}]}
    )
    assert [a.name for a in res] == ["y"]


# ---------------------------------------------------------------- legacy path
def test_legacy_validates_section_wrapped():
    seen = {}

    class SeenCheck(DummyAdapter):
        @classmethod
        def validate_config(cls, config):
            seen["cfg"] = config
            return True

    fac = _factory(_reg(_plugin("Seen", "seen_section", SeenCheck)))
    res = fac.create_adapters_from_config({"server": {}, "seen_section": [{"a": 1}]})
    assert len(res) == 1
    # legacy path passes the FULL {section: <config>} mapping to validate_config
    assert seen["cfg"] == {"seen_section": [{"a": 1}]}


def test_legacy_bad_validate_skips_plugin():
    fac = _factory(_reg(_plugin("Bad", "bad_section", BadValidateAdapter), _plugin("Ok", "ok_section", DummyAdapter)))
    res = fac.create_adapters_from_config({"server": {}, "bad_section": {}, "ok_section": {}})
    assert [a.name for a in res] == ["ok_section"]


def test_legacy_no_get_adapter_type():
    class NoTypeAdapter(DummyAdapter):
        get_adapter_type = None  # not callable -> atype stays None

    class _Policy:
        def is_adapter_allowed(self, *, adapter_type):
            return True  # None section still allowed here

    fac = _factory(_reg(_plugin("NT", "nt_section", NoTypeAdapter)), policy=_Policy())
    res = fac.create_adapters_from_config({"server": {}, "nt_section": {}})
    assert len(res) == 1


def test_legacy_type_call_with_class():
    def get_adapter_type(cls=None):
        if cls is not None:
            return "resolved"
        raise TypeError("no args")

    class TypedAdapter(DummyAdapter):
        pass

    TypedAdapter.get_adapter_type = staticmethod(get_adapter_type)
    fac = _factory(_reg(_plugin("TD", "td_section", TypedAdapter)))
    res = fac.create_adapters_from_config({"server": {}, "td_section": {}})
    assert len(res) == 1


def test_legacy_type_call_falls_to_none():
    # get_adapter_type(cls) raises AND get_adapter_type() raises -> atype None
    fac = _factory(_reg(_plugin("RT", "rt_section", LegacyRaiseTypeAdapter)))
    res = fac.create_adapters_from_config({"server": {}, "rt_section": {}})
    assert len(res) == 1


def test_legacy_policy_blocks_section():
    class _Policy:
        def is_adapter_allowed(self, *, adapter_type):
            return adapter_type != "resolved"

    def get_adapter_type(cls=None):
        return "resolved"

    class TypedAdapter(DummyAdapter):
        pass

    TypedAdapter.get_adapter_type = staticmethod(get_adapter_type)
    fac = _factory(_reg(_plugin("TD", "td_section", TypedAdapter)), policy=_Policy())
    res = fac.create_adapters_from_config({"server": {}, "td_section": {}})
    assert res == []


def test_legacy_policy_raising_blocks():
    class _RaisingPolicy:
        def is_adapter_allowed(self, *, adapter_type):
            raise RuntimeError("policy boom")

    fac = _factory(_reg(_plugin("OK", "ok_section", DummyAdapter)), policy=_RaisingPolicy())
    res = fac.create_adapters_from_config({"server": {}, "ok_section": {}})
    assert res == []


def test_legacy_missing_section_warning_and_abort():
    fac = _factory(_reg())
    with pytest.raises(RuntimeError) as ei:
        fac.create_adapters_from_config({"server": {}, "ghost_section": []})
    assert "ghost_section" in str(ei.value)
    assert "no import error details" in str(ei.value)


def test_legacy_import_errors_detail_in_abort():
    reg = _reg()
    reg._import_errors["GhostAdapter"] = "ImportError: no such module"
    fac = _factory(reg)
    with pytest.raises(RuntimeError) as ei:
        fac.create_adapters_from_config({"server": {}, "ghost_section": []})
    assert "GhostAdapter: ImportError: no such module" in str(ei.value)


def test_legacy_fail_fast_disabled_no_abort():
    fac = _factory(_reg())
    res = fac.create_adapters_from_config({"server": {"fail_fast_adapters": False}, "ghost_section": []})
    assert res == []


def test_legacy_all_configured_sections_produced_no_abort():
    # every non-core configured section has a registered plugin -> no fail-fast abort
    fac = _factory(_reg(_plugin("Dummy", "dummy_section", DummyAdapter)))
    res = fac.create_adapters_from_config({"server": {}, "dummy_section": {}})
    assert len(res) == 1


# ---------------------------------------------------------------- policy attachment
def test_security_policy_param_used_over_factory_default():
    class _Policy:
        def is_adapter_allowed(self, *, adapter_type):
            return False

    fac = _factory(_reg(_plugin("A", "alpha_ports", DummyAdapter)))
    # a stricter per-call policy blocks the unified adapter
    res = fac.create_adapters_from_config(
        {"server": {}, "adapters": [{"type": "alpha", "name": "x"}]}, security_policy=_Policy()
    )
    assert res == []


def test_security_policy_param_overrides_factory_policy():
    class _DenyAll:
        def is_adapter_allowed(self, *, adapter_type):
            return False

    class _AllowDummy:
        def is_adapter_allowed(self, *, adapter_type):
            return adapter_type == "dummy"

    # factory has a deny-all policy; a per-call allow policy takes effect
    fac = _factory(_reg(_plugin("Dummy", "dummy_section", DummyAdapter)), policy=_DenyAll())
    res = fac.create_adapters_from_config(
        {"server": {}, "adapters": [{"type": "dummy", "name": "p"}]}, security_policy=_AllowDummy()
    )
    assert [a.name for a in res] == ["p"]
