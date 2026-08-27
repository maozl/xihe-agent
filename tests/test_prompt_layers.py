"""L1 tests for the declarative layer table in core.prompts.build_system_prompt."""
from core.prompts import LAYERS, PromptCtx, build_system_prompt


def _prompt(tools, **kw):
    kw.setdefault("identity_override", "IDENT")
    kw.setdefault("project_context", False)
    kw.setdefault("skills_allowed", set())
    return build_system_prompt(platform="cli", available_tools=set(tools), **kw)


class TestLayerContract:
    def test_every_layer_returns_str_or_none(self):
        ctx = PromptCtx(tools=set(), platform="cli")
        for layer in LAYERS:
            out = layer(ctx)
            assert out is None or isinstance(out, str), getattr(layer, "__name__", layer)


class TestSkeleton:
    def test_empty_tools_is_identity_platform_behavior(self):
        p = _prompt(set())
        assert "IDENT" in p
        assert "terminal CLI" in p
        assert "# Behavior Rules" in p
        assert "# Tool-Use Discipline" not in p
        assert "# Memory" not in p
        assert "# Coding discipline" not in p
        assert "# Delegation" not in p

    def test_group_order_identity_discipline_guidance_context(self):
        p = _prompt({"model_info"}, cwd="E:/ws")
        assert p.index("IDENT") < p.index("# Tool-Use Discipline") \
            < p.index("# Behavior Rules") < p.index("# Working Directory")


class TestLanguage:
    """Config `language` (zh|en|auto) drives the Behavior Rules tail rule."""

    def test_zh_is_default(self):
        p = _prompt(set())
        assert "7. **语言**" in p and "必须始终使用中文" in p

    def test_auto_omits_rule(self):
        assert "7. **语言**" not in _prompt(set(), language="auto")

    def test_en_directive(self):
        assert "Always think (reasoning) in English" in _prompt(set(), language="en")

    def test_unknown_value_omits(self):
        assert "7. **语言**" not in _prompt(set(), language="")


class TestMemoryMerge:
    def test_both_tools_single_header(self):
        p = _prompt({"memory", "memory_manage"})
        assert p.count("# Memory") == 1
        assert "memory(action=" in p and "memory_manage(action=" in p

    def test_read_only_memory(self):
        p = _prompt({"memory"})
        assert p.count("# Memory") == 1
        assert "memory_manage" not in p

    def test_write_only_memory(self):
        p = _prompt({"memory_manage"})
        assert p.count("# Memory") == 1
        assert "before re-asking" not in p


class TestToolGuardLayers:
    def test_mcp_needs_mcp_tools_or_config_writes(self):
        assert "# MCP Servers" in _prompt({"write_file"})
        assert "# MCP Servers" in _prompt({"mcp_x_y"})
        assert "# MCP Servers" not in _prompt({"read_file", "search_files"})

    def test_coding_needs_write_or_execute_face(self):
        assert "# Coding discipline" in _prompt({"write_file"})
        assert "# Coding discipline" in _prompt({"terminal"})
        assert "# Coding discipline" not in _prompt({"read_file", "search_files"})

    def test_coding_item7_mentions_delegate_only_when_callable(self):
        with_d = _prompt({"write_file", "delegate_task"})
        without = _prompt({"write_file"})
        assert "delegate_task for multi-step" in with_d
        assert "delegate_task" not in without
        assert "**Plan + track**" in without

    def test_mandatory_tool_use_follows_tools(self):
        assert "NEVER answer these from memory" in _prompt({"terminal"})
        assert "NEVER answer these from memory" not in _prompt(set())

    def test_behavior_conditional_continuations(self):
        assert "Know your model" in _prompt({"model_info"})
        assert "Know your model" not in _prompt(set())
        assert "Expand tools" in _prompt({"request_tools"})
        assert "Expand tools" not in _prompt(set())


class TestPassthroughAndFlags:
    def test_kbs_preamble_after_identity(self):
        p = _prompt(set(), kbs_preamble="KBS-PREAMBLE")
        assert p.index("KBS-PREAMBLE") > p.index("IDENT")

    def test_kbs_note_needs_flag_and_tool(self):
        assert "Business KB" in _prompt({"kbs_search"}, kbs_read_note=True)
        assert "Business KB" not in _prompt({"kbs_search"})
        assert "Business KB" not in _prompt({"read_file"}, kbs_read_note=True)

    def test_skills_guidance_only_with_manage(self):
        assert "skill_manage for reuse" in _prompt({"skill_manage"})
        assert "skill_manage for reuse" not in _prompt({"skills_list"})
