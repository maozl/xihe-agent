# -*- coding: utf-8 -*-
"""Bundled specialist agents: provenance split, override semantics, gating.

Bundled agents (src/agents/*.yaml) are harness features — they register
regardless of specialists.enabled. User agents (~/.xihe-agent/agents/*.yaml)
stay behind the gate, and a user file with a bundled slug replaces it
wholesale (enabled: false being the bundled opt-out).
"""

import core.services.agent_defs as ad
import tools.specialist_agent_tool as sat


def _write_yaml(root, slug, body):
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{slug}.yaml").write_text(body, encoding="utf-8")


_MIN_OK = """
name: %s
description: test agent
persona: you are %s
toolsets: ["base"]
"""


def _dirs(tmp_path, monkeypatch, bundled="", user=""):
    b, u = tmp_path / "bundled", tmp_path / "user"
    for slug, body in bundled:
        _write_yaml(b, slug, body)
    for slug, body in user:
        _write_yaml(u, slug, body)
    monkeypatch.setattr(ad, "bundled_agents_dir", lambda: b)
    monkeypatch.setattr(ad, "specialists_dir", lambda: u)


def _gate(monkeypatch, on):
    monkeypatch.setattr(sat, "specialists_enabled", lambda config=None: on)


# ---- loader ----------------------------------------------------------------

def test_bundled_agents_ship_and_load(tmp_path, monkeypatch):
    # Real src/agents/*.yaml must parse: name/description/persona present,
    # toolsets [] (read-only base only), flagged as bundled.
    monkeypatch.setattr(ad, "specialists_dir", lambda: tmp_path / "empty-user")
    defs = {d.slug: d for d in ad.load_agent_defs()}
    assert {"explore", "xihe_guide", "coder"} <= set(defs)
    for slug in ("explore", "xihe_guide"):
        d = defs[slug]
        assert d.bundled and d.toolsets == ["base"] and d.persona.strip()
        assert d.sticky_session is False


def test_shipped_coder_is_sticky(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "specialists_dir", lambda: tmp_path / "empty-user")
    coder = {d.slug: d for d in ad.load_agent_defs()}["coder"]
    assert coder.bundled and coder.sticky_session and coder.project_context
    # XiheAgent 构造时对非空 roster 强制并入 base——YAML 里无需显式列出
    assert set(coder.toolsets) == {"files", "terminal", "dev_tool"}


# ---- load_all_tools 解耦：静态导入不拉起网络发现 ---------------------------

def test_load_all_tools_is_static_only(monkeypatch):
    import tools as tools_pkg
    called = []
    monkeypatch.setattr(tools_pkg, "start_mcp_discovery",
                        lambda: called.append(1))
    monkeypatch.setattr(tools_pkg, "_TOOLS_LOADED", False)
    tools_pkg.load_all_tools()
    assert called == []  # MCP 发现是组合根（SharedContext）的进程级策略


# ---- bundled read-only guard (API 层) ---------------------------------------

def test_bundled_write_violation(tmp_path, monkeypatch):
    _dirs(tmp_path, monkeypatch,
          bundled=[("explore", _MIN_OK % ("Explore", "p"))], user=[])
    # 内容键 → 拒绝（无论何种组合）
    v = ad.bundled_write_violation("explore", {"persona": "改"})
    assert v and "read-only" in v
    assert ad.bundled_write_violation(
        "explore", {"name": "x", "description": "d", "persona": "p"})
    # override 键 → 放行（启停 + 连接键）
    assert ad.bundled_write_violation("explore", {"enabled": False}) is None
    assert ad.bundled_write_violation(
        "explore", {"model": "m", "base_url": "http://x"}) is None
    # delta 文件已存在也不解锁内容键
    _write_yaml(tmp_path / "user", "explore", "enabled: false\n")
    assert ad.bundled_write_violation("explore", {"persona": "改"})
    # 非 bundled 的普通写入 → 放行
    assert ad.bundled_write_violation("mine", {"persona": "x"}) is None


def test_put_specialist_rejects_bundled_content_edit(tmp_path, monkeypatch):
    import asyncio
    import json as _json
    from types import SimpleNamespace

    from gateway.serve import ServeApp
    from tests.test_serve_first_run import FakeCtx

    import tools.specialist_agent_tool as sat_mod
    _dirs(tmp_path, monkeypatch,
          bundled=[("explore", _MIN_OK % ("Explore", "p"))], user=[])
    monkeypatch.setattr(sat_mod, "registry", _FakeRegistry())
    monkeypatch.setattr(sat_mod, "_specialist_tools", set())

    app = ServeApp(FakeCtx({"api_key": "sk-x", "model": "m"}), version="test")

    def _put(spec):
        async def _body():
            return {"spec": spec}
        return asyncio.run(app.put_specialist(SimpleNamespace(
            match_info={"slug": "explore"},
            json=_body,
        )))

    # 内容键 → 400（内置内容只读）
    r = _put({"name": "Explore", "description": "test agent",
              "persona": "改", "toolsets": ["base"]})
    assert r.status == 400
    assert "read-only" in _json.loads(r.text)["error"]
    # 纯 delta（enabled:false）→ 200，落盘的就是这一行
    r2 = _put({"enabled": False})
    assert r2.status == 200
    assert (tmp_path / "user" / "explore.yaml").read_text(
        encoding="utf-8").strip() == "enabled: false"

def _fake_parent(session_key="agent:main:wecom:dm:42"):
    from types import SimpleNamespace
    return SimpleNamespace(_approval_shared={"session_key": session_key})


def test_dispatch_chat_id_sticky_scopes_to_parent():
    d = ad.AgentDef(slug="coder", name="C", description="d", persona="p",
                    sticky_session=True)
    cid = sat._dispatch_chat_id(d, _fake_parent())
    assert cid == "agent:main:wecom:dm:42:coder"


def test_dispatch_chat_id_fresh_when_not_sticky():
    import re
    d = ad.AgentDef(slug="coder", name="C", description="d", persona="p")
    assert re.fullmatch(r"coder_\d+", sat._dispatch_chat_id(d, _fake_parent()))


def test_continue_session_overrides_agent_default():
    import re
    parent = _fake_parent()
    sticky = ad.AgentDef(slug="coder", name="C", description="d", persona="p",
                         sticky_session=True)
    fresh = ad.AgentDef(slug="explore", name="E", description="d", persona="p")
    # 显式 false 关掉 sticky 默认；显式 true 打开 fresh 默认
    assert re.fullmatch(r"coder_\d+",
                        sat._dispatch_chat_id(sticky, parent, False))
    assert sat._dispatch_chat_id(fresh, parent, True) == \
        "agent:main:wecom:dm:42:explore"
    # 网关侧字符串拼写归一化："false" 绝不能落成 truthy
    assert re.fullmatch(r"coder_\d+",
                        sat._dispatch_chat_id(sticky, parent, "false"))
    assert sat._dispatch_chat_id(fresh, parent, "true") == \
        "agent:main:wecom:dm:42:explore"


def test_dispatch_chat_id_fresh_when_parent_key_missing():
    from types import SimpleNamespace
    import re
    d = ad.AgentDef(slug="coder", name="C", description="d", persona="p",
                    sticky_session=True)
    # 会话键缺失（不该发生）时退回全新会话——绝不把不同主会话串进一个专家会话
    for parent in (SimpleNamespace(_approval_shared={}),
                   SimpleNamespace()):
        assert re.fullmatch(r"coder_\d+", sat._dispatch_chat_id(d, parent))


def test_tool_description_reflects_session_memory():
    plain = ad.AgentDef(slug="e", name="E", description="d", persona="p")
    sticky = ad.AgentDef(slug="c", name="C", description="d", persona="p",
                         sticky_session=True)
    assert "knows nothing" in sat._tool_description(plain)
    assert "continue_session=true" in sat._tool_description(plain)
    assert "remembers its previous dispatches" in sat._tool_description(sticky)
    assert "continue_session=false" in sat._tool_description(sticky)
    schema = sat._tool_schema(sticky)["function"]["parameters"]["properties"]
    assert "continue_session" in schema


def test_user_file_is_shadowed_by_bundled(tmp_path, monkeypatch):
    # 内置优先：同名用户文件里的内容键被忽略（只允许 override 键），只告警
    _dirs(tmp_path, monkeypatch,
          bundled=[("explore", _MIN_OK % ("Explore", "bundled persona"))],
          user=[("explore", _MIN_OK % ("My Explore", "user persona"))])
    warnings = []
    defs = {d.slug: d for d in ad.load_agent_defs(warnings)}
    assert defs["explore"].name == "Explore"
    assert defs["explore"].bundled is True
    assert any("ignored" in w for w in warnings)


def test_user_enabled_false_opts_out_bundled(tmp_path, monkeypatch):
    # 停用 = 一行 delta：只有 enabled:false，内容随产品托管
    _dirs(tmp_path, monkeypatch,
          bundled=[("explore", _MIN_OK % ("Explore", "p"))],
          user=[("explore", "enabled: false\n")])
    assert ad.load_agent_defs() == []


def test_bundled_delta_survives_drift_and_overrides_connection(tmp_path, monkeypatch):
    # delta 只影响 override 键：产品升级改内置内容后 delta 仍精确生效；
    # 连接键（model）覆盖进入 AgentDef，内容键被忽略
    _dirs(tmp_path, monkeypatch,
          bundled=[("coder", _MIN_OK % ("Coder", "v1"))],
          user=[("coder",
                 "enabled: true\nmodel: glm-flash\npersona: 手改的内容\n")])
    _write_yaml(tmp_path / "bundled", "coder", _MIN_OK % ("Coder", "v2"))
    defs = {d.slug: d for d in ad.load_agent_defs()}
    c = defs["coder"]
    assert c.bundled and c.model == "glm-flash"          # delta 连接键生效
    assert c.persona == "you are v2"                      # 内容键被忽略，升级不漂移
    assert c.enabled is True
    # 列表显示合并后的 effective spec（内置内容 + delta）
    spec = {s: sp for s, sp, _ in ad.list_raw_specs()}["coder"]
    assert spec["model"] == "glm-flash" and spec["persona"] == "you are v2"


def test_list_raw_specs_bundled_wins(tmp_path, monkeypatch):
    _dirs(tmp_path, monkeypatch,
          bundled=[("a", "name: A\ndescription: d\npersona: p\n"),
                   ("b", "name: B\ndescription: d\npersona: p\n")],
          user=[("b", "name: B2\ndescription: d\npersona: p\n"),
                ("mine", _MIN_OK % ("Mine", "p"))])
    specs = {slug: (spec, bundled) for slug, spec, bundled in ad.list_raw_specs()}
    assert set(specs) == {"a", "b", "mine"}
    assert specs["b"][0]["name"] == "B" and specs["b"][1] is True  # 内置胜出
    assert specs["mine"][1] is False


def test_bundled_agents_sort_first(tmp_path, monkeypatch):
    # 用户 slug 字母序在前也排不到内置前面：bundled 分层优先。
    _dirs(tmp_path, monkeypatch,
          bundled=[("zeta", _MIN_OK % ("Zeta", "p"))],
          user=[("alpha", _MIN_OK % ("Alpha", "p"))])
    assert [d.slug for d in ad.load_agent_defs()] == ["zeta", "alpha"]
    assert [s for s, _, _ in ad.list_raw_specs()] == ["zeta", "alpha"]


def test_disabled_entries_sort_last_within_group(tmp_path, monkeypatch):
    # 内置组永远在前（产品语义，不因停用改变）；组内活跃在前、禁用垫底。
    # 注意 load_agent_defs 在解析层就丢弃禁用条目——排序只对编辑器列表生效
    _dirs(tmp_path, monkeypatch,
          bundled=[("zeta", _MIN_OK % ("Zeta", "p")),
                   ("alpha", _MIN_OK % ("Alpha", "p"))],
          user=[("mine", _MIN_OK % ("Mine", "p")),
                ("alpha", "enabled: false\n")])
    assert [s for s, _, _ in ad.list_raw_specs()] == ["zeta", "alpha", "mine"]
    assert [d.slug for d in ad.load_agent_defs()] == ["zeta", "mine"]


# ---- registration / roster gating ------------------------------------------

class _FakeRegistry:
    def __init__(self):
        self.registered = []

    def get_schema(self, name):
        return None

    def register(self, **kw):
        self.registered.append(kw)

    def deregister(self, name):
        before = len(self.registered)
        self.registered = [kw for kw in self.registered if kw["name"] != name]
        return len(self.registered) < before


def test_registration_gates_user_but_not_bundled(tmp_path, monkeypatch):
    _dirs(tmp_path, monkeypatch,
          bundled=[("b1", _MIN_OK % ("B1", "p"))],
          user=[("mine", _MIN_OK % ("Mine", "p"))])
    fake = _FakeRegistry()
    monkeypatch.setattr(sat, "registry", fake)
    monkeypatch.setattr(sat, "_specialist_tools", set())

    _gate(monkeypatch, on=False)
    sat.register_specialist_agent_tools()
    assert {kw["name"] for kw in fake.registered} == {"run_b1_agent"}

    _gate(monkeypatch, on=True)
    sat.register_specialist_agent_tools()
    assert {kw["name"] for kw in fake.registered} == \
        {"run_b1_agent", "run_mine_agent"}


def test_sync_lifecycle_registers_updates_deregisters(tmp_path, monkeypatch):
    # 实时生效：serve 在 PUT/DELETE 后调 sync——新建即注册、编辑换 schema、
    # 停用/删除即注销，全程无重启
    _dirs(tmp_path, monkeypatch,
          bundled=[("b1", _MIN_OK % ("B1", "p"))], user=[])
    fake = _FakeRegistry()
    monkeypatch.setattr(sat, "registry", fake)
    monkeypatch.setattr(sat, "_specialist_tools", set())
    _gate(monkeypatch, on=True)

    # 新建 → 注册
    _write_yaml(tmp_path / "user", "mine",
                "name: Mine\ndescription: 旧描述\npersona: p\ntoolsets: [\"base\"]\n")
    sat.sync_specialist_agent_tools()
    assert {kw["name"] for kw in fake.registered} == \
        {"run_b1_agent", "run_mine_agent"}

    # 编辑（description 变）→ 重注册的 schema 换新
    _write_yaml(tmp_path / "user", "mine",
                "name: Mine\ndescription: 新描述\npersona: p\ntoolsets: [\"base\"]\n")
    sat.sync_specialist_agent_tools()
    entry = [kw for kw in fake.registered if kw["name"] == "run_mine_agent"][-1]
    assert "新描述" in entry["schema"]["function"]["description"]

    # 停用（enabled: false）→ 注销
    _write_yaml(tmp_path / "user", "mine",
                "name: Mine\ndescription: d\npersona: p\nenabled: false\n")
    sat.sync_specialist_agent_tools()
    assert {kw["name"] for kw in fake.registered} == {"run_b1_agent"}

    # 删除用户文件 → 无来源保持注销；bundled b1 始终不受影响
    (tmp_path / "user" / "mine.yaml").unlink()
    sat.sync_specialist_agent_tools()
    assert {kw["name"] for kw in fake.registered} == {"run_b1_agent"}


def test_roster_lists_bundled_when_gate_off(tmp_path, monkeypatch):
    _dirs(tmp_path, monkeypatch,
          bundled=[("b1", _MIN_OK % ("B1", "p"))],
          user=[("mine", _MIN_OK % ("Mine", "p"))])
    _gate(monkeypatch, on=False)
    roster = sat.build_roster_prompt()
    assert "run_b1_agent" in roster and "run_mine_agent" not in roster


def test_effective_defs_and_registration_share_filter(tmp_path, monkeypatch):
    # roster 的 available_tools 过滤与注册分流必须同一套口径：
    # 注册了才允许出现在 roster 里。
    _dirs(tmp_path, monkeypatch,
          bundled=[("b1", _MIN_OK % ("B1", "p"))],
          user=[("mine", _MIN_OK % ("Mine", "p"))])
    _gate(monkeypatch, on=False)
    roster = sat.build_roster_prompt(available_tools={"run_mine_agent"})
    assert roster == ""  # 未注册的工具即便显式传入也不得宣传
