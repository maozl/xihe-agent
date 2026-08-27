"""L0/L1 tests for the base-toolset floor, the blocked manifest, and the
session-scoped todo store — the three-layer tool model:

    tools = (base ∪ roster) − subagent_blocked
"""
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.toolsets import SUBAGENT_BLOCKED_TOOLS, TOOLSETS


def _import_all_static_tool_modules():
    """Load every self-registering tool module except MCP discovery (network)
    and specialist dispatch (config-gated, dynamic)."""
    import tools
    for f in sorted(Path(tools.__file__).parent.glob("*.py")):
        if f.stem.startswith("_") or f.stem == "__init__":
            continue
        if f.stem in ("mcp_tool", "specialist_agent_tool"):
            continue
        importlib.import_module(f"tools.{f.stem}")


@pytest.fixture(scope="module", autouse=True)
def static_registry():
    _import_all_static_tool_modules()
    from tools import registry
    return registry


class TestBaseToolsetDefinition:
    def test_base_tools_all_registered(self, static_registry):
        for name in TOOLSETS["base"]["tools"]:
            assert static_registry.get_schema(name) is not None, name

    def test_base_toolset_listed_in_toolsets_table(self):
        # Split relocations must keep table and registry registrations aligned.
        assert "read_file" in TOOLSETS["base"]["tools"]
        assert "read_file" not in TOOLSETS["files"]["tools"]
        assert "write_file" in TOOLSETS["files"]["tools"]
        assert "memory_manage" in TOOLSETS["memory"]["tools"]
        assert "memory" in TOOLSETS["base"]["tools"]
        assert "kbs_init" in TOOLSETS["kbs"]["tools"]
        assert "kbs_search" in TOOLSETS["base"]["tools"]
        assert "skill_manage" in TOOLSETS["skills"]["tools"]
        assert "skills_list" in TOOLSETS["base"]["tools"]
        assert TOOLSETS["agent"]["tools"] == ["delegate_task"]


class TestBaseUnion:
    def test_nonempty_roster_gains_base(self, make_agent):
        agent = make_agent(None, enabled_toolsets=["files"])
        assert "base" in agent.enabled_toolsets
        assert "files" in agent.enabled_toolsets

    def test_empty_roster_stays_empty(self, make_agent):
        # [] is the pure-chat contract — the base floor must not break it.
        agent = make_agent(None, enabled_toolsets=[])
        assert agent.enabled_toolsets == set()

    def test_none_stays_unrestricted(self, make_agent):
        agent = make_agent(None, enabled_toolsets=None)
        assert agent.enabled_toolsets is None

    def test_base_tools_visible_through_get_schemas(self, make_agent, static_registry):
        agent = make_agent(None, enabled_toolsets=["files"])
        schemas = static_registry.get_schemas(
            toolsets=agent.enabled_toolsets, subagent=True)
        names = {s["function"]["name"] for s in schemas}
        # base reads arrive with the roster's write face
        assert {"read_file", "model_info", "todo", "skills_list"} <= names
        assert {"write_file", "patch"} <= names


class TestSubagentBlockedManifest:
    def test_registry_matches_documented_manifest(self, static_registry):
        blocked = {name for name, e in static_registry._tools.items()
                   if e.subagent_blocked and not name.startswith("run_")}
        assert blocked == SUBAGENT_BLOCKED_TOOLS

    def test_browser_record_and_state_delete_blocked(self, static_registry):
        from tools import registry
        for name in ("browser_record", "browser_record_start",
                     "browser_record_stop", "browser_state_delete"):
            assert registry._tools[name].subagent_blocked, name

    def test_http_and_browser_never_read_only(self, static_registry):
        # http takes arbitrary methods (POST); browser_* share one Playwright
        # page bound to a single thread — parallel dispatch corrupts it.
        from tools import registry
        assert not registry.is_read_only("http")
        for name in registry.get_all_tool_names():
            if name.startswith("browser_"):
                assert not registry.is_read_only(name), name


class TestTodoSessionIsolation:
    @pytest.fixture(autouse=True)
    def todo_file(self, tmp_path, monkeypatch):
        import tools.todo_tool as tt
        monkeypatch.setattr(tt, "_TODO_FILE", tmp_path / "todos.json")
        self.tt = tt
        yield

    def test_child_list_never_lands_in_main(self):
        r = self.tt._todo({"action": "add", "title": "main-task"},
                          context={"session_key": "agent:main:cli:dm:s1"})
        assert '"success": true' in r
        child = self.tt._todo({"action": "list"},
                              context={"session_key": "subagent_1"})
        assert '"count": 0' in child
        main = self.tt._todo({"action": "list"},
                             context={"session_key": "agent:main:cli:dm:s1"})
        assert '"main-task"' in main

    def test_no_context_keeps_bare_name(self):
        self.tt._todo({"action": "add", "title": "bare"})
        out = self.tt._todo({"action": "list"})
        assert '"bare"' in out

    def test_named_lists_scoped_per_session(self):
        self.tt._todo({"action": "add", "title": "a", "list": "plan"},
                      context={"session_key": "s1"})
        other = self.tt._todo({"action": "list", "list": "plan"},
                              context={"session_key": "s2"})
        assert '"count": 0' in other


class TestSpecialistMemoryDiscipline:
    def _def(self, toolsets):
        return SimpleNamespace(persona="p", toolsets=toolsets, slug="itsm")

    def test_memory_roster_still_gets_namespace_rule(self):
        # "memory" roster now resolves to memory_manage — the prefix rule must
        # follow the write tool, not the (relocated) read tool.
        from tools.specialist_agent_tool import _persona_prompt
        assert "agent:itsm:" in _persona_prompt(self._def(["memory"]))

    def test_files_only_roster_gets_no_namespace_rule(self):
        from tools.specialist_agent_tool import _persona_prompt
        assert "agent:itsm:" not in _persona_prompt(self._def(["files"]))

    def test_unrestricted_gets_namespace_rule(self):
        from tools.specialist_agent_tool import _persona_prompt
        assert "agent:itsm:" in _persona_prompt(self._def(None))


class TestDelegateDefaultToolsets:
    def test_memory_absent_from_delegate_default(self):
        from tools.delegate_tool import DELEGATE_DEFAULT_TOOLSETS
        assert "memory" not in DELEGATE_DEFAULT_TOOLSETS
        assert set(DELEGATE_DEFAULT_TOOLSETS) == {"files", "terminal", "dev_tool",
                                                  "http", "web", "media"}
