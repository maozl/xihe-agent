"""L0/L1 — 危险操作审批：判定表、dispatch 汇聚点门、XiheAgent 协调等待、
steer 折批复。纯函数 + 内存事件，无网络、无子进程（terminal handler 打桩）。
"""
import json
import threading
from types import SimpleNamespace

import pytest

from tools import registry
import tools._approvals as _approvals
from tools._approvals import (
    _danger_detail,
    evaluate,
    parse_approval_reply,
    register_pending,
    remember_rule,
    resolve_pending_reply,
    try_resolve_steer,
    unregister_pending,
)

MANUAL = {"approvals": {"mode": "manual"}}


class _StubAgent:
    """dispatch 门测试替身：记录请求，按预设批复。"""

    def __init__(self, approved=True, config=None):
        self.config = config if config is not None else MANUAL
        self.approved = approved
        self.requests = []

    def request_approval(self, tool, summary):
        self.requests.append((tool, summary))
        return self.approved, "stub", False


@pytest.fixture
def terminal_stub(monkeypatch):
    """Swap the real terminal handler for a recorder — the gate keys on the
    tool NAME ("terminal" → dangerous-command check), so the dangerous-command
    path must run under this name while never spawning a shell."""
    import tools.terminal  # noqa: F401 (registers "terminal")
    calls = {"count": 0}

    def _recorder(args, **_kw):
        calls["count"] += 1
        return json.dumps({"ok": True})

    monkeypatch.setattr(registry._tools["terminal"], "handler", _recorder)
    return calls


# ---- L0: evaluate 危险判定 ---------------------------------------------------


def test_terminal_dangerous_command_asks():
    decision, summary = evaluate("terminal", {"command": "rm -rf /tmp/x"}, MANUAL)
    assert decision == "ask"
    assert "rm -rf /tmp/x" in summary


def test_terminal_safe_command_passes():
    decision, summary = evaluate("terminal", {"command": "ls -la"}, MANUAL)
    assert decision == "allow" and summary == ""


def test_auto_mode_lets_everything_through():
    cfg = {"approvals": {"mode": "auto", "ask": ["write_file"]}}
    assert evaluate("terminal", {"command": "rm -rf /"}, cfg)[0] == "allow"
    assert evaluate("ssh_exec", {"command": "reboot"}, cfg)[0] == "allow"
    assert evaluate("write_file", {"path": "a.txt"}, cfg)[0] == "allow"


def test_missing_config_defaults_to_manual():
    assert evaluate("terminal", {"command": "mkfs /dev/sda"}, None)[0] == "ask"


@pytest.mark.parametrize("name,args,dangerous", [
    ("ssh_exec", {"target_ip": "10.0.0.1", "command": "uptime"}, True),
    ("process", {"action": "stop", "name": "worker"}, True),
    ("process", {"action": "list"}, False),
    ("browser_logout", {"wipe_profile": True}, True),
    ("browser_logout", {}, False),
    ("skill_manage", {"action": "delete", "name": "x"}, True),
    ("skill_manage", {"action": "create", "name": "x"}, False),
    ("kbs_init", {"force": True}, True),
    ("kbs_init", {}, False),
    ("cronjob", {"action": "delete", "name": "job"}, True),
    ("cronjob", {"action": "create", "name": "job"}, False),
    ("node_version", {"action": "install", "version": "20"}, True),
    ("node_version", {"action": "uninstall", "version": "20"}, True),
    ("node_version", {"action": "list"}, False),
    # 高危表刻意不含写工具——圈定写工具走 ask 规则
    ("write_file", {"path": "a.txt", "content": "x"}, False),
    ("patch", {"path": "a.txt", "old": "x", "new": "y"}, False),
])
def test_high_risk_table_conditions(name, args, dangerous):
    detail = _danger_detail(name, args)
    assert (detail is not None) is dangerous


# ---- L0: ask 规则（任意工具可圈定）-------------------------------------------


def test_ask_rule_whole_tool_asks_with_summary():
    cfg = {"approvals": {"ask": ["write_file"]}}
    decision, summary = evaluate("write_file",
                                 {"path": "src/app.py", "content": "x"}, cfg)
    assert decision == "ask"
    assert "write_file" in summary and "src/app.py" in summary
    # 未圈定的工具不受影响
    assert evaluate("read_file", {"path": "src/app.py"}, cfg)[0] == "allow"


def test_ask_rule_absent_by_default_write_tools_allow():
    # 不配 ask 规则 = 行为与历史版本一致，写工具不弹审批
    assert evaluate("write_file", {"path": "a.txt"}, MANUAL)[0] == "allow"
    assert evaluate("patch", {"path": "a.txt"}, MANUAL)[0] == "allow"


def test_ask_rule_path_qualifier_with_windows_separator():
    cfg = {"approvals": {"ask": ["write_file(/etc/**)", "write_file(C:/etc/**)"]}}
    assert evaluate("write_file", {"path": "/etc/hosts"}, cfg)[0] == "ask"
    # 反斜杠路径规范化为正斜杠后，命中正斜杠写的模式
    assert evaluate("write_file", {"path": r"C:\etc\config.ini"}, cfg)[0] == "ask"
    assert evaluate("write_file", {"path": "src/app.py"}, cfg)[0] == "allow"


def test_ask_rule_allow_carveout():
    cfg = {"approvals": {"ask": ["write_file"], "allow": ["write_file(src/**)"]}}
    assert evaluate("write_file", {"path": "src/app.py"}, cfg)[0] == "allow"
    assert evaluate("write_file", {"path": "docs/x.md"}, cfg)[0] == "ask"


def test_ask_rule_deny_beats_ask():
    cfg = {"approvals": {"ask": ["write_file"], "deny": ["write_file(/etc/**)"]}}
    assert evaluate("write_file", {"path": "/etc/hosts"}, cfg)[0] == "deny"


def test_ask_rule_session_memory_once_per_rule():
    cfg = {"approvals": {"ask": ["write_file", "patch"]}}
    assert evaluate("write_file", {"path": "a.txt"}, cfg,
                    session_key="s1")[0] == "ask"
    remember_rule("s1", "write_file", {"path": "a.txt"}, cfg)
    # 同规则换目标文件不再问（按规则记，非按路径记）
    assert evaluate("write_file", {"path": "b.txt"}, cfg,
                    session_key="s1")[0] == "allow"
    # 另一条 ask 规则各自记：patch 首调仍要问
    assert evaluate("patch", {"path": "a.txt"}, cfg,
                    session_key="s1")[0] == "ask"
    # 其他会话不共享
    assert evaluate("write_file", {"path": "a.txt"}, cfg,
                    session_key="s2")[0] == "ask"
    _approvals._SESSION_RULES.clear()


# ---- L0: 会话记忆落盘 ---------------------------------------------------------


def _sim_restart():
    """模拟进程重启：清进程内缓存与水合标记，落盘文件保留。"""
    _approvals._SESSION_RULES.clear()
    _approvals._hydrated.clear()


def test_memory_persists_across_process_restart():
    cfg = {"approvals": {"ask": ["write_file"]}}
    remember_rule("s1", "write_file", {"path": "a.txt"}, cfg)
    _sim_restart()
    assert evaluate("write_file", {"path": "b.txt"}, cfg,
                    session_key="s1")[0] == "allow"
    # 落盘按会话分文件：别的会话仍要问
    assert evaluate("write_file", {"path": "b.txt"}, cfg,
                    session_key="s2")[0] == "ask"


def test_memory_invalid_days_falls_back_to_default():
    # 0/负数/非数字都视为非法：回落默认天数，落盘照常工作
    for bad in (0, -3, "abc", None):
        cfg = {"approvals": {"ask": ["write_file"], "memory_days": bad}}
        remember_rule("s1", "write_file", {"path": "a.txt"}, cfg)
        _sim_restart()
        assert evaluate("write_file", {"path": "b.txt"}, cfg,
                        session_key="s1")[0] == "allow", bad


def test_memory_expired_entries_dropped():
    cfg = {"approvals": {"ask": ["write_file"]}}
    remember_rule("s1", "write_file", {"path": "a.txt"}, cfg)
    path = _approvals._memory_file("s1")
    data = json.loads(path.read_text(encoding="utf-8"))
    for item in data:
        item["ts"] = 0  # 改到 TTL 之外 → 过期即重问
    path.write_text(json.dumps(data), encoding="utf-8")
    _sim_restart()
    assert evaluate("write_file", {"path": "a.txt"}, cfg,
                    session_key="s1")[0] == "ask"


def test_memory_corrupt_file_fails_open():
    cfg = {"approvals": {"ask": ["write_file"]}}
    path = _approvals._memory_file("s1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    _approvals._hydrated.clear()
    # 坏文件按空记忆处理：宁可重问，不可崩溃或误放
    assert evaluate("write_file", {"path": "a.txt"}, cfg,
                    session_key="s1")[0] == "ask"


def test_memory_first_always_after_restart_keeps_old_entries():
    # 重启后先 hydrate（读到旧条目）再 always 批准一条新的：保存是全量写，
    # 旧条目不能被冲掉
    cfg = {"approvals": {"ask": ["write_file", "patch"]}}
    remember_rule("s1", "write_file", {"path": "a.txt"}, cfg)
    _sim_restart()
    remember_rule("s1", "patch", {"path": "b.txt"}, cfg)
    _sim_restart()
    assert evaluate("write_file", {"path": "c.txt"}, cfg,
                    session_key="s1")[0] == "allow"
    assert evaluate("patch", {"path": "d.txt"}, cfg,
                    session_key="s1")[0] == "allow"


# ---- L1: dispatch 汇聚点门 ----------------------------------------------------


def test_dispatch_denied_blocks_handler(terminal_stub):
    agent = _StubAgent(approved=False)
    result = json.loads(registry.dispatch(
        "terminal", json.dumps({"command": "rm -rf /tmp/x"}),
        parent_agent=agent))
    assert terminal_stub["count"] == 0
    assert result.get("error")
    assert result.get("blocked") is True
    assert len(agent.requests) == 1


def test_dispatch_denial_message_forbids_workaround(terminal_stub):
    """被拒后的回话必须明说不得换工具实现相同效果——旧文案'可调整方案'
    被模型读成了'换个写法再试'（内部系统配置绕过事件的直接诱因之一）。"""
    agent = _StubAgent(approved=False)
    result = json.loads(registry.dispatch(
        "terminal", json.dumps({"command": "rm -rf /tmp/x"}),
        parent_agent=agent))
    assert "不得改用" in result["error"]


def test_dispatch_approved_runs_handler(terminal_stub):
    agent = _StubAgent(approved=True)
    result = json.loads(registry.dispatch(
        "terminal", json.dumps({"command": "rm -rf /tmp/x"}),
        parent_agent=agent))
    assert terminal_stub["count"] == 1
    assert result.get("ok") is True


def test_dispatch_without_parent_agent_skips_gate(terminal_stub):
    agent = _StubAgent(approved=False)
    result = json.loads(registry.dispatch(
        "terminal", json.dumps({"command": "rm -rf /tmp/x"})))
    assert terminal_stub["count"] == 1
    assert agent.requests == []


def test_dispatch_safe_command_never_asks(terminal_stub):
    agent = _StubAgent(approved=False)
    registry.dispatch("terminal", json.dumps({"command": "echo hi"}),
                      parent_agent=agent)
    assert terminal_stub["count"] == 1
    assert agent.requests == []


# ---- L1: XiheAgent 协调 ------------------------------------------------------


def _wire(agent, request_cb=None, result_cb=None):
    agent._approval_shared["request_cb"] = request_cb
    agent._approval_shared["result_cb"] = result_cb


def test_request_approval_resolved_allow(make_agent):
    from tests.fakes import FakeChatClient
    agent = make_agent(FakeChatClient())

    def _cb(info):
        agent.resolve_approval(info["id"], True)

    _wire(agent, request_cb=_cb)
    approved, reason, _always = agent.request_approval("terminal", "危险命令")
    assert approved is True and reason == "用户批准"
    assert agent.pending_approval is None


def test_request_approval_resolved_deny(make_agent):
    from tests.fakes import FakeChatClient
    agent = make_agent(FakeChatClient())

    def _cb(info):
        agent.resolve_approval(info["id"], False)

    _wire(agent, request_cb=_cb)
    approved, _, _always = agent.request_approval("terminal", "危险命令")
    assert approved is False
    assert agent._approval_shared["pending"] is None


def test_request_approval_timeout_denies_by_default(make_agent):
    from tests.fakes import FakeChatClient
    agent = make_agent(FakeChatClient())
    agent.config["approvals"] = {"timeout": 0.4}
    _wire(agent, request_cb=lambda info: None)
    approved, reason, _always = agent.request_approval("terminal", "危险命令")
    assert approved is False
    assert "超时" in reason and "拒绝" in reason


def test_request_approval_timeout_action_allow(make_agent):
    from tests.fakes import FakeChatClient
    agent = make_agent(FakeChatClient())
    agent.config["approvals"] = {"timeout": 0.4, "timeout_action": "allow"}
    _wire(agent, request_cb=lambda info: None)
    approved, reason, _always = agent.request_approval("terminal", "危险命令")
    assert approved is True
    assert "放行" in reason


def test_request_approval_interrupt_cancels(make_agent):
    from tests.fakes import FakeChatClient
    agent = make_agent(FakeChatClient())
    _wire(agent, request_cb=lambda info: agent.interrupt())
    approved, reason, _always = agent.request_approval("terminal", "危险命令")
    assert approved is False
    assert "打断" in reason


def test_request_approval_without_callback_denies_immediately(make_agent):
    from tests.fakes import FakeChatClient
    agent = make_agent(FakeChatClient())
    approved, reason, _always = agent.request_approval("terminal", "危险命令")
    assert approved is False
    assert "无人值守" in reason


def test_request_approval_callback_failure_denies(make_agent):
    from tests.fakes import FakeChatClient
    agent = make_agent(FakeChatClient())

    def _boom(_info):
        raise RuntimeError("ui gone")

    _wire(agent, request_cb=_boom)
    approved, reason, _always = agent.request_approval("terminal", "危险命令")
    assert approved is False
    assert "发送失败" in reason


def test_request_approval_second_pending_rejected(make_agent):
    from tests.fakes import FakeChatClient
    agent = make_agent(FakeChatClient())
    shared = agent._approval_shared
    shared["pending"] = {"id": "held", "event": threading.Event(),
                         "approved": None, "reason": ""}
    _wire(agent, request_cb=lambda info: None)
    approved, reason, _always = agent.request_approval("terminal", "危险命令")
    assert approved is False
    assert "另一个" in reason


def test_result_callback_fires_on_resolution(make_agent):
    from tests.fakes import FakeChatClient
    agent = make_agent(FakeChatClient())
    seen = {}

    def _cb(info):
        agent.resolve_approval(info["id"], True, note="ok")

    _wire(agent, request_cb=_cb,
          result_cb=lambda info, approved, reason: seen.update(
              id=info["id"], approved=approved, reason=reason))
    agent.request_approval("terminal", "危险命令")
    assert seen["approved"] is True


# ---- L0: 后台（cron）审批路由表 -------------------------------------------------


def test_pending_registry_folds_exact_reply():
    got = {}
    register_pending("wecom", "c1", "ap1",
                     lambda approved, always: got.update(a=approved, al=always))
    assert resolve_pending_reply("wecom", "c1", "a") is True
    assert got == {"a": True, "al": True}
    unregister_pending("wecom", "c1", "ap1")
    assert resolve_pending_reply("wecom", "c1", "y") is False


def test_pending_registry_ignores_prose():
    # 长文本是补充说明，不当批复——落回正常消息流
    register_pending("wecom", "c1", "ap1", lambda *a: None)
    assert resolve_pending_reply("wecom", "c1", "可以的，但是先看下目录") is False
    unregister_pending("wecom", "c1", "ap1")


def test_pending_registry_scoped_to_chat():
    got = []
    register_pending("wecom", "c1", "ap1", lambda *a, **k: got.append(1))
    assert resolve_pending_reply("wecom", "other", "y") is False
    assert resolve_pending_reply("feishu", "c1", "y") is False
    assert got == []
    unregister_pending("wecom", "c1", "ap1")


def test_pending_registry_resolves_newest_card():
    got = {}
    register_pending("wecom", "c1", "ap1",
                     lambda *a, **k: got.update(which="old"))
    register_pending("wecom", "c1", "ap2",
                     lambda *a, **k: got.update(which="new"))
    assert resolve_pending_reply("wecom", "c1", "n") is True
    assert got["which"] == "new"


def test_chat_approval_key_overrides_bucket(make_agent, monkeypatch):
    # cron 按任务名 / 工作空间按目录换审批记忆桶：只换桶，不动会话键
    from core import title_generator
    from core.session import SessionSource
    from tests.fakes import FakeChatClient
    monkeypatch.setattr(title_generator, "maybe_auto_title",
                        lambda *a, **k: None)
    agent = make_agent(FakeChatClient([{"content": "ok"}]))
    agent.is_subagent = False  # 只有顶层 chat 写审批键
    src = SessionSource(platform="cli", chat_id="conv-1", chat_type="dm")
    agent.chat(source=src, user_message="hi", approval_key="cron_job:nightly")
    assert agent._approval_shared["session_key"] == "cron_job:nightly"


def test_ws_approval_key_normalization():
    from gateway.serve import _ws_approval_key
    # Windows 盘符大小写不敏感 + 正反斜杠 + 尾斜杠 → 同一工作空间同一桶
    assert _ws_approval_key("E:\\Proj\\X") == _ws_approval_key("e:/proj/x")
    assert _ws_approval_key("e:/proj/x/") == "ws:e:/proj/x"


# ---- L0: steer 折批复 ---------------------------------------------------------


def test_parse_reply_word_sets():
    for word in ("y", "Y", "yes", "好", "批准", "allow"):
        assert parse_approval_reply(word) is True
    for word in ("n", "N", "no", "否", "拒绝", "deny"):
        assert parse_approval_reply(word) is False
    # 长文本是补充说明，不是批复
    assert parse_approval_reply("可以的，但是先看一下目标目录") is None
    assert parse_approval_reply("") is None


def _make_pending(agent, approval_id="ap-1"):
    agent._approval_shared["pending"] = {
        "id": approval_id, "tool": "terminal", "summary": "危险命令",
        "event": threading.Event(), "approved": None, "reason": "",
    }


def test_try_resolve_steer_yes(make_agent):
    from tests.fakes import FakeChatClient
    agent = make_agent(FakeChatClient())
    _make_pending(agent)
    assert try_resolve_steer(agent, "y") is True
    assert agent._approval_shared["pending"]["approved"] is True
    assert agent._approval_shared["pending"]["event"].is_set()


def test_try_resolve_steer_no(make_agent):
    from tests.fakes import FakeChatClient
    agent = make_agent(FakeChatClient())
    _make_pending(agent)
    assert try_resolve_steer(agent, "n") is True
    assert agent._approval_shared["pending"]["approved"] is False


def test_try_resolve_steer_prose_is_steer(make_agent):
    from tests.fakes import FakeChatClient
    agent = make_agent(FakeChatClient())
    _make_pending(agent)
    assert try_resolve_steer(agent, "继续但别删文件") is False
    assert agent._approval_shared["pending"]["approved"] is None


def test_try_resolve_steer_without_pending(make_agent):
    from tests.fakes import FakeChatClient
    agent = make_agent(FakeChatClient())
    assert try_resolve_steer(agent, "y") is False


# ---- L0: 规则列表与会话记忆 ---------------------------------------------------


def test_evaluate_deny_rule_blocks_without_asking():
    cfg = {"approvals": {"deny": ["terminal(*mkfs*)"]}}
    decision, summary = evaluate("terminal", {"command": "mkfs /dev/sda"}, cfg)
    assert decision == "deny"
    assert "mkfs" in summary


def test_evaluate_deny_beats_allow():
    cfg = {"approvals": {"deny": ["terminal(*mkfs*)"],
                         "allow": ["terminal(mkfs *)"]}}
    assert evaluate("terminal", {"command": "mkfs /dev/sda"}, cfg)[0] == "deny"


def test_evaluate_allow_rule_skips_prompt():
    cfg = {"approvals": {"allow": ["terminal(rm -rf /tmp/*)"]}}
    assert evaluate("terminal", {"command": "rm -rf /tmp/build"}, cfg)[0] == "allow"
    # 未命中白名单的危险命令仍要问
    assert evaluate("terminal", {"command": "rm -rf /"}, cfg)[0] == "ask"


def test_evaluate_bare_tool_rule_matches_all_args():
    cfg = {"approvals": {"deny": ["ssh_exec"]}}
    assert evaluate("ssh_exec", {"command": "uptime"}, cfg)[0] == "deny"
    assert evaluate("terminal", {"command": "ls"}, cfg)[0] == "allow"


def test_evaluate_action_rule_text():
    cfg = {"approvals": {"allow": ["node_version(install *)"]}}
    assert evaluate("node_version", {"action": "install", "version": "20"}, cfg)[0] == "allow"
    assert evaluate("node_version", {"action": "uninstall", "version": "20"}, cfg)[0] == "ask"


def test_session_memory_scope_and_danger_class():
    cfg = {"approvals": {}}
    assert evaluate("terminal", {"command": "rm -rf /tmp/a"}, cfg,
                    session_key="s1")[0] == "ask"
    remember_rule("s1", "terminal", {"command": "rm -rf /tmp/a"}, cfg)
    assert evaluate("terminal", {"command": "rm -rf /tmp/a"}, cfg,
                    session_key="s1")[0] == "allow"
    # 类记忆：同危险类换目标不再问（命令文本内嵌目标名，逐字匹配等于每条重问）
    assert evaluate("terminal", {"command": "rm -rf /var/log/b"}, cfg,
                    session_key="s1")[0] == "allow"
    # 其他会话不共享；非危险命令不受记忆影响；换一类危险仍要问
    assert evaluate("terminal", {"command": "rm -rf /tmp/a"}, cfg,
                    session_key="s2")[0] == "ask"
    assert evaluate("terminal", {"command": "ls /tmp"}, cfg,
                    session_key="s1")[0] == "allow"
    assert evaluate("terminal", {"command": "mkfs /dev/sda"}, cfg,
                    session_key="s1")[0] == "ask"
    assert evaluate("ssh_exec", {"command": "reboot"}, cfg,
                    session_key="s1")[0] == "ask"
    _approvals._SESSION_RULES.clear()


def test_session_memory_high_risk_tool_whole_class():
    cfg = {"approvals": {}}
    remember_rule("s1", "ssh_exec", {"command": "df -h", "target_ip": "10.1.1.5"},
                  cfg)
    # 高危表工具的类键 = 工具名：本会话任意 ssh_exec 不再问
    assert evaluate("ssh_exec", {"command": "reboot", "target_ip": "10.9.9.9"},
                    cfg, session_key="s1")[0] == "allow"
    _approvals._SESSION_RULES.clear()


def test_session_memory_beaten_by_deny_rule():
    remember_rule("s1", "terminal", {"command": "rm -rf /tmp/a"},
                  {"approvals": {}})
    cfg = {"approvals": {"deny": ["terminal(rm *)"]}}
    assert evaluate("terminal", {"command": "rm -rf /tmp/a"}, cfg,
                    session_key="s1")[0] == "deny"
    _approvals._SESSION_RULES.clear()


def test_parse_reply_always_words():
    for word in ("a", "A", "ya", "always", "不再询问"):
        assert parse_approval_reply(word) == "always"


def test_try_resolve_steer_always(make_agent):
    from tests.fakes import FakeChatClient
    agent = make_agent(FakeChatClient())
    _make_pending(agent)
    assert try_resolve_steer(agent, "a") is True
    assert agent._approval_shared["pending"]["approved"] is True
    assert agent._approval_shared["pending"]["always"] is True


def test_request_approval_returns_always_flag(make_agent):
    from tests.fakes import FakeChatClient
    agent = make_agent(FakeChatClient())

    def _cb(info):
        agent.resolve_approval(info["id"], True, always=True)

    _wire(agent, request_cb=_cb)
    approved, reason, always = agent.request_approval("terminal", "危险命令")
    assert approved is True and always is True


def test_dispatch_always_remembered_second_call_skips_prompt(
        make_agent, terminal_stub):
    """端到端：第一次"批准且不再询问"→ 会话记忆 → 同一命令第二次直过。"""
    from tests.fakes import FakeChatClient
    agent = make_agent(FakeChatClient())
    agent._approval_shared["session_key"] = "sess-1"
    asked = {"n": 0}

    def _cb(info):
        asked["n"] += 1
        agent.resolve_approval(info["id"], True, always=True)

    _wire(agent, request_cb=_cb)
    args = json.dumps({"command": "rm -rf /tmp/x"})
    registry.dispatch("terminal", args, parent_agent=agent)
    registry.dispatch("terminal", args, parent_agent=agent)
    assert terminal_stub["count"] == 2
    assert asked["n"] == 1
    _approvals._SESSION_RULES.clear()


# ---- LLM 语义判定层 --------------------------------------------------------

class _FakeAux:
    """判定层替身：固定回复内容，统计调用次数。"""

    def __init__(self, content):
        self.content = content
        self.calls = 0

    def is_available(self, task=None):
        return True

    def call_llm(self, task, messages, **kw):
        self.calls += 1
        msg = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


# 正则漏网、语义可辨的形态：.NET API 枚举回收站后删文件（无 rm/del 等
# 正则动词，漏斗靠 namespace(0xa)/delete 命中）
_EVASION_CMD = ("powershell -Command \"$s = New-Object -ComObject "
                "Shell.Application; $p = $s.Namespace(0xA).Items()[0].Path; "
                "[IO.File]::Delete($p)\"")


def test_llm_judge_asks_when_regex_misses():
    aux = _FakeAux('{"risk": "dangerous", "category": "delete", '
                   '"reason": "不可逆删除", "effect": "枚举回收站并删除文件"}')
    decision, summary = evaluate("terminal", {"command": _EVASION_CMD},
                                 {"approvals": {}}, aux=aux)
    assert decision == "ask"
    assert "delete" in summary and "枚举回收站" in summary
    assert aux.calls == 1


def test_llm_judge_warning_and_safe_tiers_allow():
    # 三档里只有 dangerous 弹审批；warning/safe 放行（防审批疲劳）
    for content in ('{"risk": "warning", "category": "other", '
                    '"reason": "日常", "effect": "删除单个日志文件"}',
                    '{"risk": "safe", "category": "other", '
                    '"reason": "只读", "effect": "查看目录"}'):
        aux = _FakeAux(content)
        assert evaluate("terminal", {"command": "del build\\old.log"},
                        {"approvals": {}}, aux=aux)[0] == "allow"
        assert aux.calls == 1


def test_llm_judge_no_funnel_no_call():
    aux = _FakeAux('{"risk": "dangerous", "category": "delete", "reason": "x"}')
    assert evaluate("terminal", {"command": "git status && npm run build"},
                    {"approvals": {}}, aux=aux)[0] == "allow"
    assert aux.calls == 0


def test_llm_judge_not_wired_behaves_as_before():
    assert evaluate("terminal", {"command": "del a.txt"},
                    {"approvals": {}}, aux=None)[0] == "allow"


def test_llm_judge_fail_open_on_garbage():
    for aux in (_FakeAux(None), _FakeAux("这不是 JSON")):
        assert evaluate("terminal", {"command": "Remove-Item junk.tmp"},
                        {"approvals": {}}, aux=aux)[0] == "allow"
        assert aux.calls == 1


def test_extract_json_object_skips_echoed_command_braces():
    # 2026-08-20 实测：模型在输出里回显被审命令的 PowerShell if/else 花括号，
    # 旧贪婪 \{.*\} 把它当 JSON 边界 → bad json。括号配对扫描应跳过这些
    # 候选、拿到后面真正的 verdict 对象。
    echo = ("if (Get-Process -Id 26952) { Write-Output 'still running' } "
            "else { Write-Output 'over' }")
    verdict = ('{"risk": "safe", "category": "process", "reason": "只读", '
               '"effect": "查进程状态"}')
    data = _approvals._extract_json_object(f"分析：{echo}\n结论：{verdict}")
    assert data == json.loads(verdict)
    # 只有命令回显、没有 verdict → None（fail-open）
    assert _approvals._extract_json_object(echo) is None


def test_llm_judge_salvages_quoted_risk_from_prose():
    # JSON 解析失败但散文里有引号形式的结论 → 回收，不白白 fail-open
    aux = _FakeAux('经评审 "risk": "dangerous"，该脚本会清空回收站')
    assert evaluate("terminal", {"command": _EVASION_CMD},
                    {"approvals": {}}, aux=aux)[0] == "ask"
    _approvals._SESSION_RULES.clear()


# 实测故障（agent.log "approval llm judge bad json"）：模型没直接输出 JSON，
# 先回显了被评审的 PowerShell if/else —— 贪婪 {.*} 把命令里的花括号当 JSON
# 边界切进来。提取层须按配对括号跳过回显、捞出真正的判定对象。
_ECHO_THEN_JSON = (
    "if (Get-Process -Id 26952) { Write-Output 'still running' } "
    "else { Write-Output 'xihe (PID 26952) 已终止' }\n"
    '{"risk": "dangerous", "category": "process", "reason": "杀进程", '
    '"effect": "检查并报告进程状态"}'
)


def test_llm_judge_recovers_json_after_command_echo():
    aux = _FakeAux(_ECHO_THEN_JSON)
    decision, summary = evaluate(
        "terminal", {"command": "stop-process -Id 26952"},
        {"approvals": {}}, aux=aux)
    assert decision == "ask"
    assert "process" in summary
    assert aux.calls == 1


def test_llm_judge_pure_command_echo_fails_open():
    aux = _FakeAux("if ($p) { Write-Output 'still running' } "
                   "else { Write-Output 'xihe 已终止' }")
    assert evaluate("terminal", {"command": "stop-process -Id 26952"},
                    {"approvals": {}}, aux=aux)[0] == "allow"
    assert aux.calls == 1


def test_llm_judge_keyword_fallback_from_prose():
    aux = _FakeAux('这条命令会杀掉 xihe 进程。结论："risk": "dangerous"')
    assert evaluate("terminal", {"command": "stop-process -Id 26952"},
                    {"approvals": {}}, aux=aux)[0] == "ask"


def test_llm_judge_disabled_by_config():
    aux = _FakeAux('{"risk": "dangerous", "category": "delete", "reason": "x"}')
    cfg = {"approvals": {"llm_judge": False}}
    assert evaluate("terminal", {"command": _EVASION_CMD}, cfg,
                    aux=aux)[0] == "allow"
    assert aux.calls == 0


def test_llm_judge_class_memory_same_category():
    cfg = {"approvals": {}}
    aux = _FakeAux('{"risk": "dangerous", "category": "delete", '
                   '"reason": "r", "effect": "e"}')
    cmd1 = {"command": "[IO.File]::Delete('a.txt')"}
    assert evaluate("terminal", cmd1, cfg, session_key="s1",
                    aux=aux)[0] == "ask"
    remember_rule("s1", "terminal", cmd1, cfg)
    cmd2 = {"command": "[IO.File]::Delete('b.txt')"}
    assert evaluate("terminal", cmd2, cfg, session_key="s1",
                    aux=aux)[0] == "allow"
    _approvals._SESSION_RULES.clear()


def test_llm_judge_class_memory_other_category_still_asks():
    cfg = {"approvals": {}}
    aux = _FakeAux('{"risk": "dangerous", "category": "delete", '
                   '"reason": "r", "effect": "e"}')
    cmd1 = {"command": "[IO.File]::Delete('a.txt')"}
    assert evaluate("terminal", cmd1, cfg, session_key="s1",
                    aux=aux)[0] == "ask"
    remember_rule("s1", "terminal", cmd1, cfg)
    aux.content = ('{"risk": "dangerous", "category": "system", '
                   '"reason": "r", "effect": "e"}')
    cmd2 = {"command": "bcdedit /set safeboot minimal"}
    assert evaluate("terminal", cmd2, cfg, session_key="s1",
                    aux=aux)[0] == "ask"
    _approvals._SESSION_RULES.clear()
