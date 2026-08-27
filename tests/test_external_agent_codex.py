"""L1 tests for the codex driver (core/external_agent.py).

Hermetic: _spawn_cli/_resolve_bin/_add_pid/_remove_pid/_maybe_sweep and the
tools.interrupt register pair are monkeypatched so no real codex child
launches. Fake JSONL pins the exec-protocol contract verified live against
codex 0.146: argv shape (incl. the mandatory ``--disable multi_agent`` —
litellm-family gateways 500 on the namespace tool type), stdin write-then-CLOSE
(codex blocks until EOF), and the NDJSON→event mapping.
"""

import io
import json
from unittest import mock

import pytest

import core.external_agent as ea
import tools.interrupt as iv
from tools.external_agent_tool import _resolve_llm_creds


class FakeStdin(io.BytesIO):
    """BytesIO rejects getvalue() after close() — record writes as they land."""
    closed_explicitly = False

    def __init__(self):
        super().__init__()
        self.written = b""

    def write(self, b):
        self.written += b
        return super().write(b)

    def close(self):
        self.closed_explicitly = True
        super().close()


def _jsonl(events):
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)


class FakeProc:
    def __init__(self, stdout_bytes):
        self.stdin = FakeStdin()
        self.stdout = io.BytesIO(stdout_bytes)
        self.stderr = io.BytesIO(b"")
        self.pid = 777


@pytest.fixture
def wiring(monkeypatch):
    registered, unregistered, killed = [], [], []
    monkeypatch.setattr(iv, "register_subprocess",
                        lambda h: registered.append(h))
    monkeypatch.setattr(iv, "unregister_subprocess",
                        lambda h: unregistered.append(h))
    monkeypatch.setattr(ea, "kill_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(ea, "_add_pid", lambda entry: None)
    monkeypatch.setattr(ea, "_remove_pid", lambda pid: None)
    monkeypatch.setattr(ea, "_resolve_bin", lambda b: "C:/fake/codex.cmd")
    # key isolation across tests: wipe the module-global resume map
    monkeypatch.setattr(ea, "_resume_ids", {})

    captured = {}

    def fake_spawn_cli(bin_resolved, args, cwd, env):
        captured["bin"] = bin_resolved
        captured["args"] = args
        captured["cwd"] = cwd
        captured["env"] = env
        return FakeProc(captured.setdefault("stdout", b""))

    monkeypatch.setattr(ea, "_spawn_cli", fake_spawn_cli)

    driver = ea.CodexDriver()
    monkeypatch.setattr(driver, "_maybe_sweep", lambda: None)
    return driver, captured, registered, unregistered, killed


def _feed(captured, events):
    captured["stdout"] = _jsonl(events).encode("utf-8")


def _run(driver, prompt="hi", spec=None):
    events = []
    res = driver.run_turn(
        "k", prompt, spec or ea.TurnSpec(cwd="E:/w", llm={}),
        on_event=events.append)
    return res, events


def _completed_turn(tid="t1"):
    return [
        {"type": "thread.started", "thread_id": tid},
        {"type": "turn.completed", "usage": {"input_tokens": 10,
                                             "output_tokens": 5}},
    ]


def test_spawn_argv_shape(wiring):
    """one-shot 契约：exec --json + 强制 --disable multi_agent + 提示词走
    stdin（argv 末位 "-"，凭证走 env 不进 argv）。"""
    driver, captured, _, _, _ = wiring
    _feed(captured, _completed_turn())
    res, _ = _run(driver, prompt="你好")
    assert res.exit_reason == "completed"

    a = captured["args"]
    assert a[0] == "exec"
    for flag in ("--json", "--skip-git-repo-check"):
        assert flag in a
    # litellm 网关 500 on namespace tool type —— 必带，不是优化项
    assert "--disable" in a and a[a.index("--disable") + 1] == "multi_agent"
    assert a[a.index("-C") + 1] == "E:/w"
    assert a[-1] == "-"                    # prompt from stdin
    assert "resume" not in a               # fresh session
    assert "-c" not in a                   # no base_url → no inline provider

    # env: no creds in argv; UTF-8 forcing like claude
    assert "CODEX_API_KEY" not in captured["env"]
    assert captured["env"]["PYTHONUTF8"] == "1"
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"


def test_stdin_written_then_closed(wiring):
    driver, captured, _, _, _ = wiring
    stdin_holder = {}

    def fake_spawn_cli(bin_resolved, args, cwd, env):
        p = FakeProc(captured.setdefault("stdout", b""))
        stdin_holder["s"] = p.stdin
        return p

    # re-patch to grab the stdin object (wiring's version doesn't keep the proc)
    with mock.patch.object(ea, "_spawn_cli", fake_spawn_cli):
        _feed(captured, _completed_turn())
        res, _ = _run(driver, prompt="hi")
    assert res.exit_reason == "completed"
    s = stdin_holder["s"]
    assert s.closed_explicitly is True     # EOF gate — codex boots on close
    assert s.written == b"hi"              # prompt bytes actually delivered


def test_api_key_goes_to_env_not_argv(wiring):
    driver, captured, _, _, _ = wiring
    _feed(captured, _completed_turn())
    _run(driver, spec=ea.TurnSpec(cwd="E:/w", llm={"api_key": "sk-x"}))
    assert captured["env"]["CODEX_API_KEY"] == "sk-x"
    assert not any("sk-x" in str(x) for x in captured["args"])


def test_permission_mode_mapping(wiring):
    driver, captured, _, _, _ = wiring
    _feed(captured, _completed_turn())
    _run(driver, spec=ea.TurnSpec(cwd="E:/w", llm={},
                                  permission_mode="read-only"))
    assert captured["args"][captured["args"].index("-s") + 1] == "read-only"

    _feed(captured, _completed_turn())
    _run(driver, spec=ea.TurnSpec(cwd="E:/w", llm={},
                                  permission_mode="bypassPermissions"))
    assert "--dangerously-bypass-approvals-and-sandbox" in captured["args"]
    assert "-s" not in captured["args"]

    # claude-style value that isn't a codex sandbox mode → safe default
    _feed(captured, _completed_turn())
    _run(driver, spec=ea.TurnSpec(cwd="E:/w", llm={}, permission_mode="weird"))
    assert captured["args"][captured["args"].index("-s") + 1] == "workspace-write"


def test_event_mapping_full_turn(wiring):
    driver, captured, _, _, _ = wiring
    _feed(captured, [
        {"type": "thread.started", "thread_id": "t9"},
        {"type": "item.started",
         "item": {"type": "command_execution", "command": "ls -la"}},
        {"type": "item.completed",
         "item": {"type": "command_execution", "command": "ls -la",
                  "aggregated_output": "f1\nf2", "status": "completed"}},
        {"type": "item.completed",
         "item": {"type": "reasoning", "text": "先看目录"}},
        {"type": "item.updated",
         "item": {"type": "agent_message", "text": "半截"}} ,
        {"type": "item.completed",
         "item": {"type": "agent_message", "text": "答案一"}},
        {"type": "item.completed",
         "item": {"type": "agent_message", "text": "答案二"}},
        {"type": "item.completed",
         "item": {"type": "file_change",
                  "changes": [{"path": "a.py", "kind": "add"}]}},
        {"type": "item.started",
         "item": {"type": "mcp_tool_call", "server": "srv", "tool": "search"}},
        {"type": "item.completed",
         "item": {"type": "mcp_tool_call", "server": "srv", "tool": "search",
                  "arguments": "{}", "result": "hit"}},
        {"type": "turn.completed", "usage": {}},
    ])
    res, ev = _run(driver)

    kinds = [e["type"] for e in ev]
    assert kinds == ["tool_call", "tool_result", "thought_delta",
                     "text_delta", "text_delta",
                     "tool_call", "tool_result",
                     "tool_call", "tool_result",
                     "complete"]
    # item.updated (half message) must NOT emit — only completed carries text
    texts = [e["text"] for e in ev if e["type"] == "text_delta"]
    assert texts == ["答案一", "答案二"]
    assert ev[-1]["text"] == "答案一\n\n答案二"

    assert res.exit_reason == "completed"
    assert res.final_text == "答案一\n\n答案二"
    assert res.session_id == "t9"
    assert ea._resume_ids[("codex", "k")] == "t9"

    trace = {t["tool"]: t for t in res.tool_trace}
    assert trace["shell"]["args"] == "ls -la"
    assert trace["shell"]["result"] == "f1\nf2"
    assert trace["shell"]["status"] == "ok"
    assert trace["file_change"]["status"] == "ok"
    assert trace["srv.search"]["result"] == "hit"


def test_failed_shell_marks_trace_error(wiring):
    driver, captured, _, _, _ = wiring
    _feed(captured, [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed",
         "item": {"type": "command_execution", "command": "make",
                  "aggregated_output": "boom", "status": "failed"}},
        {"type": "turn.completed", "usage": {}},
    ])
    res, _ = _run(driver)
    assert res.tool_trace[0]["status"] == "error"


def test_turn_failed_maps_to_error(wiring):
    driver, captured, _, _, _ = wiring
    _feed(captured, [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.failed",
         "error": {"message": "stream error: high demand"}},
    ])
    res, ev = _run(driver)
    assert res.exit_reason == "failed"
    assert "high demand" in res.error
    assert ev[-1]["type"] == "error"


def test_reconnecting_noise_is_ignored(wiring):
    """顶层 error "Reconnecting..." 是重试噪声——终局以 turn.completed 为准，
    不得提前 fail、也不得把 error 带进结果。"""
    driver, captured, _, _, _ = wiring
    _feed(captured, [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "error", "message": "Reconnecting... (1/5)"},
        {"type": "item.completed",
         "item": {"type": "agent_message", "text": "好了"}},
        {"type": "turn.completed", "usage": {}},
    ])
    res, ev = _run(driver)
    assert res.exit_reason == "completed"
    assert res.error is None
    assert res.final_text == "好了"
    assert ev[-1]["type"] == "complete"

    # non-Reconnecting top-level errors are held as the EOF fallback message
    _feed(captured, [{"type": "error", "message": "real problem"}])
    res2, _ = _run(driver)
    assert res2.exit_reason == "failed"
    assert "real problem" in res2.error


def test_resume_argv_and_engine_key_isolation(wiring):
    """续轮：exec [flags] resume <tid> -；resume id 按 (engine, session_key)
    隔离——同一 xihe 会话交替两引擎时不得串线。"""
    driver, captured, _, _, _ = wiring
    ea._resume_ids[("codex", "k")] = "tid-cx"
    ea._resume_ids[("claude", "k")] = "tid-cl"
    _feed(captured, _completed_turn(tid="tid-cx2"))
    res, _ = _run(driver)
    a = captured["args"]
    assert a[a.index("resume") + 1] == "tid-cx"
    assert a[-1] == "-"
    # thread.started re-captures the (possibly rolled) thread id
    assert ea._resume_ids[("codex", "k")] == "tid-cx2"
    assert ea._resume_ids[("claude", "k")] == "tid-cl"


def test_interrupt_registration_and_one_shot_cleanup(wiring):
    """单轮生命周期：turn 内注册 tree-kill（agent.interrupt 依赖），结束即
    kill_tree + 注销 PID——codex 不留跨轮进程。"""
    driver, captured, registered, unregistered, killed = wiring
    _feed(captured, _completed_turn())
    res, _ = _run(driver)
    assert res.exit_reason == "completed"
    assert len(registered) == 1
    assert registered[0].pid == 777
    assert unregistered == [registered[0]]
    assert killed == [777]


def test_get_driver_codex_singleton():
    assert ea.get_driver("codex") is ea.get_driver("codex")


def _c_overrides(args):
    """argv 里 -c 对 → {dotted.key: value}（value 保留 TOML 引号原样）。"""
    ov = {}
    for i, x in enumerate(args):
        if x == "-c" and i + 1 < len(args):
            k, _, v = args[i + 1].partition("=")
            ov[k] = v
    return ov


def test_base_url_inline_provider(wiring):
    """external_agents.codex.base_url → 五连 -c 内联定义 provider xihe
    （TOML 字符串带引号），wire_api 随行；权限旗标不受影响。"""
    driver, captured, _, _, _ = wiring
    _feed(captured, _completed_turn())
    _run(driver, spec=ea.TurnSpec(
        cwd="E:/w", permission_mode="workspace-write",
        llm={"base_url": "http://g/v1/", "wire_api": "chat"}))
    ov = _c_overrides(captured["args"])
    assert ov['model_provider'] == '"xihe"'
    assert ov['model_providers.xihe.name'] == '"xihe"'
    # trailing slash stripped, /v1 kept verbatim (codex appends the endpoint)
    assert ov['model_providers.xihe.base_url'] == '"http://g/v1"'
    assert ov['model_providers.xihe.env_key'] == '"CODEX_API_KEY"'
    assert ov['model_providers.xihe.wire_api'] == '"chat"'
    assert captured["args"][captured["args"].index("-s") + 1] == "workspace-write"

    # wire_api 缺省/非法 → responses（内部网关实测形态）
    _feed(captured, _completed_turn())
    _run(driver, spec=ea.TurnSpec(
        cwd="E:/w", llm={"base_url": "http://g", "wire_api": "bogus"}))
    assert _c_overrides(captured["args"])['model_providers.xihe.wire_api'] \
        == '"responses"'


def test_extra_args_appended_before_resume(wiring):
    """extra_args 原样追加，位置在 resume/- 之前（不挤掉 stdin 占位）。"""
    driver, captured, _, _, _ = wiring
    ea._resume_ids[("codex", "k")] = "tid"
    _feed(captured, _completed_turn())
    _run(driver, spec=ea.TurnSpec(cwd="E:/w", llm={},
                                  extra_args=["--ephemeral", "-c", "x=1"]))
    a = captured["args"]
    assert a[a.index("resume") + 1] == "tid"
    assert a[-1] == "-"
    assert a.index("--ephemeral") < a.index("resume")
    assert a[a.index("-c", a.index("--ephemeral")) + 1] == "x=1"


def test_max_tokens_overrides_and_order(wiring):
    """max_tokens → -c model_max_output_tokens=<n>（纯数字，非 TOML 字符串），
    位置在 extra_args 之前——extra_args 的同键 -c 保留最后决定权。"""
    driver, captured, _, _, _ = wiring
    _feed(captured, _completed_turn())
    _run(driver, spec=ea.TurnSpec(
        cwd="E:/w", llm={"max_tokens": 32768},
        extra_args=["-c", "model_max_output_tokens=9999"]))
    a = captured["args"]
    # ours lands first → the extra_args copy (later argv position) can override
    assert a.index("model_max_output_tokens=32768") \
        < a.index("model_max_output_tokens=9999")

    # unset → no injection, engine defaults (config.toml) stay authoritative
    _feed(captured, _completed_turn())
    _run(driver, spec=ea.TurnSpec(cwd="E:/w", llm={}))
    assert "model_max_output_tokens" not in _c_overrides(captured["args"])


def test_resolve_llm_creds_max_tokens():
    """max_tokens：显式才生效——主配置的 max_completion_tokens 不回退（引擎各有
    自身上限：claude 内部 / codex config.toml，继承值会悄悄改写它们）；非法值
    丢弃不炸。两引擎共用同一解析。"""
    llm = _resolve_llm_creds(
        {"external_agents": {"claude": {"max_tokens": 16384}}}, "claude")
    assert llm["max_tokens"] == 16384

    # engine key unset → main max_completion_tokens must NOT leak in
    llm2 = _resolve_llm_creds({"max_completion_tokens": 8192}, "codex")
    assert "max_tokens" not in llm2

    # malformed values dropped, not fatal
    for bad in ("bogus", -5, 0, None):
        llm3 = _resolve_llm_creds(
            {"external_agents": {"codex": {"max_tokens": bad}}}, "codex")
        assert "max_tokens" not in llm3

    llm4 = _resolve_llm_creds(
        {"external_agents": {"codex": {"max_tokens": "32768"}}}, "codex")
    assert llm4["max_tokens"] == 32768        # numeric string coerces


def test_resolve_llm_creds_codex_base_url_is_opt_in():
    """codex base_url 只认显式 external_agents.codex.base_url——不回退主
    base_url（否则每个实例都会悄悄改写 config.toml 的 provider 选择）。"""
    llm = _resolve_llm_creds(
        {"api_key": "sk-main", "base_url": "http://main/v1",
         "external_agents": {"codex": {"base_url": "http://g/v1/"}}},
        "codex")
    assert llm["base_url"] == "http://g/v1"       # no /v1 strip (unlike claude)
    assert llm["wire_api"] == "responses"

    # 未设置 codex.base_url → 主 base_url 不得进入 llm
    llm2 = _resolve_llm_creds(
        {"api_key": "sk", "base_url": "http://main/v1"}, "codex")
    assert "base_url" not in llm2 and "wire_api" not in llm2

    llm3 = _resolve_llm_creds(
        {"external_agents": {"codex": {"base_url": "http://g",
                                       "wire_api": "bogus"}}}, "codex")
    assert llm3["wire_api"] == "responses"
