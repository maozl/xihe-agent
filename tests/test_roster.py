"""L0 tests for the config-driven roster (main + specialists + delegate).

Semantics under test (one rule — config.yaml top-level keys for the main
agent, agents/<slug>.yaml for specialists):
    absent / []  → load nothing
    ["*"]        → None → unrestricted
    names        → validated list; unknown static names dropped, mcp /
                   mcp-<server> always kept
"""
import pytest

from core.toolsets import normalize_toolset_names, resolve_roster


class TestNormalizeToolsetNames:
    def test_absent_means_empty(self):
        assert normalize_toolset_names(None) == []

    def test_empty_list_is_empty(self):
        assert normalize_toolset_names([]) == []

    def test_star_is_unrestricted(self):
        assert normalize_toolset_names(["*"]) is None
        assert normalize_toolset_names(["files", "*"]) is None

    def test_known_names_kept(self):
        assert normalize_toolset_names(["files", "http"]) == ["files", "http"]

    def test_unknown_dropped_with_warning(self):
        warnings = []
        out = normalize_toolset_names(["files", "nope"], where="main.toolsets",
                                      warnings=warnings)
        assert out == ["files"]
        assert any("nope" in w for w in warnings)

    def test_mcp_names_always_kept(self):
        # A server may register later in the process lifetime — the name is
        # kept even when it is not a static TOOLSETS entry.
        assert normalize_toolset_names(["mcp", "mcp-someserver"]) == \
            ["mcp", "mcp-someserver"]

    def test_non_string_entries_dropped(self):
        assert normalize_toolset_names(["files", 3, None]) == ["files"]


class TestResolveRoster:
    def test_absent_means_nothing(self):
        assert resolve_roster({}) == ([], [])

    def test_star(self):
        assert resolve_roster({"toolsets": ["*"], "skills": ["*"]}) == (None, None)

    def test_named(self):
        ts, sk = resolve_roster({"toolsets": ["files", "mcp-foo"],
                                 "skills": ["a", "b"]})
        assert ts == ["files", "mcp-foo"]
        assert sk == ["a", "b"]

    def test_explicit_empty(self):
        assert resolve_roster({"toolsets": [], "skills": []}) == ([], [])

    def test_empty_toolsets_warns(self):
        warnings = []
        resolve_roster({}, where="agents/x.yaml", warnings=warnings)
        assert any("no tools" in w for w in warnings)

    def test_non_string_skills_dropped(self):
        assert resolve_roster({"skills": ["a", 3]}) == ([], ["a"])


class TestAgentDefParsing:
    """_parse_def must apply the same semantics to agents/*.yaml."""

    def _parse(self, spec, slug="tester"):
        from core.agent_defs import _parse_def
        warnings = []
        d = _parse_def(slug, spec, warnings)
        return d, warnings

    def test_absent_toolsets_means_none_loaded(self):
        d, _ = self._parse({"persona": "p", "description": "d"})
        assert d.toolsets == []
        assert d.skills == []

    def test_star_toolsets_and_skills(self):
        d, _ = self._parse({"persona": "p", "description": "d",
                            "toolsets": ["*"], "skills": ["*"]})
        assert d.toolsets is None
        assert d.skills is None

    def test_named_with_mcp_scope(self):
        d, w = self._parse({"persona": "p", "description": "d",
                            "toolsets": ["files", "mcp-sqlscan"]})
        assert d.toolsets == ["files", "mcp-sqlscan"]

    def test_unknown_toolset_warns(self):
        d, w = self._parse({"persona": "p", "description": "d",
                            "toolsets": ["files", "bogus"]})
        assert d.toolsets == ["files"]
        assert any("bogus" in x for x in w)

    def test_main_spec_is_just_the_config_dict(self):
        # The main agent's spec is the loaded config.yaml itself — top-level
        # toolsets/skills keys, same resolution as a specialist yaml.
        ts, sk = resolve_roster({"model": "m", "toolsets": ["files", "mcp"],
                                 "skills": ["*"]})
        assert ts == ["files", "mcp"]
        assert sk is None


class TestLoadConfigPassthrough:
    def test_top_level_roster_keys_survive_load_config(self, tmp_path):
        # load_config copies top-level keys from an allowlist — toolsets/
        # skills must be on it or they silently vanish.
        from core.config import load_config
        f = tmp_path / "config.yaml"
        f.write_text('model: m\ntoolsets: ["files", "mcp"]\nskills: ["*"]\n',
                     encoding="utf-8")
        cfg = load_config(str(f))
        assert cfg["toolsets"] == ["files", "mcp"]
        assert cfg["skills"] == ["*"]


class TestSpecialistsGate:
    """specialists.enabled (default off) must keep run_*_agent unregistered."""

    @pytest.fixture(autouse=True)
    def env(self, tmp_path, monkeypatch):
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "helper.yaml").write_text(
            "description: d\npersona: p\ntoolsets: ['files']\n", encoding="utf-8")
        import core.agent_defs as ad
        monkeypatch.setattr(ad, "specialists_dir", lambda: agents)
        self.cfg_file = tmp_path / "config.yaml"
        monkeypatch.setenv("XIHE_CONFIG_FILE", str(self.cfg_file))
        yield
        from tools import registry
        registry.deregister("run_helper_agent")

    def _register(self):
        from tools.specialist_agent_tool import register_specialist_agent_tools
        from tools import registry
        registry.deregister("run_helper_agent")
        register_specialist_agent_tools()
        return registry.get_schema("run_helper_agent") is not None

    def test_off_by_default(self):
        self.cfg_file.write_text("model: m\n", encoding="utf-8")
        assert self._register() is False

    def test_off_when_section_absent_value_false(self):
        self.cfg_file.write_text("specialists:\n  enabled: false\n", encoding="utf-8")
        assert self._register() is False

    def test_on_registers_dispatch_tool(self):
        self.cfg_file.write_text("specialists:\n  enabled: true\n", encoding="utf-8")
        assert self._register() is True


class TestXiheAgentRoster:
    """XiheAgent must distinguish [] (nothing) from None (unrestricted)."""

    def test_empty_list_loads_no_tools(self, make_agent):
        from tools import registry
        agent = make_agent(None, enabled_toolsets=[])
        assert agent.enabled_toolsets == set()
        schemas = registry.get_schemas(toolsets=agent.enabled_toolsets,
                                       subagent=True)
        assert schemas == []

    def test_none_is_unrestricted(self, make_agent):
        from tools import registry
        agent = make_agent(None, enabled_toolsets=None)
        assert agent.enabled_toolsets is None
        assert registry.get_schemas(
            toolsets=agent.enabled_toolsets, subagent=True) is not None

    def test_skills_empty_set_vs_none(self, make_agent):
        assert make_agent(None, skills_allowed=set()).skills_allowed == set()
        assert make_agent(None, skills_allowed=None).skills_allowed is None


class TestDelegateResolution:
    def test_no_request_uses_broad_default(self):
        from tools.delegate_tool import _resolve_allowed_toolsets, DELEGATE_DEFAULT_TOOLSETS
        assert _resolve_allowed_toolsets(None) == DELEGATE_DEFAULT_TOOLSETS
        assert _resolve_allowed_toolsets([]) == DELEGATE_DEFAULT_TOOLSETS

    def test_star_is_unrestricted(self):
        from tools.delegate_tool import _resolve_allowed_toolsets
        assert _resolve_allowed_toolsets(["*"]) is None

    def test_requested_honored_without_parent_intersection(self):
        from tools.delegate_tool import _resolve_allowed_toolsets
        assert _resolve_allowed_toolsets(["web", "media"]) == ["media", "web"]

    def test_all_unknown_falls_back_to_default(self):
        from tools.delegate_tool import _resolve_allowed_toolsets, DELEGATE_DEFAULT_TOOLSETS
        assert _resolve_allowed_toolsets(["bogus"]) == DELEGATE_DEFAULT_TOOLSETS


def test_roster_prompt_declares_routing_ladder(monkeypatch):
    """The roster layer is the single arbitration point for tool-vs-specialist
    overlap — a raw tool a specialist wraps must lose to the specialist."""
    from types import SimpleNamespace

    from tools import specialist_agent_tool as sat

    fake = SimpleNamespace(
        name="claude", tool_name="run_claude_agent", description="外部引擎调度"
    )
    monkeypatch.setattr(sat, "_configured_defs", lambda: [fake])
    p = sat.build_roster_prompt()
    assert "WRAPS" in p and "run_claude_agent" in p
    assert p.index("Trivial") < p.index("specialist's domain")
    assert p.index("specialist's domain") < p.index("delegate_task")
