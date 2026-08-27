"""L1 tests for the claude driver's interrupt wiring (core/external_agent.py).

Hermetic: _spawn/_teardown/_maybe_sweep are monkeypatched so no real claude
child launches; the assertions pin the register/unregister contract that
agent.interrupt() → kill_subprocesses() depends on.

Regression background: registration used to live in _spawn and referenced a
name imported function-locally inside run_turn — LOAD_GLOBAL raised NameError,
swallowed by except, so /stop never killed anything.
"""

import io

import pytest

import core.external_agent as ea
import tools.interrupt as iv


class FakeProc:
    def __init__(self):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(b"")   # immediate EOF unblocks the read loop
        self.stderr = io.BytesIO()
        self.pid = 4242

    def poll(self):
        return 0    # dead — run_turn takes the reaped-session path


@pytest.fixture
def wiring(monkeypatch):
    registered, unregistered, killed = [], [], []
    monkeypatch.setattr(iv, "register_subprocess",
                        lambda handle: registered.append(handle))
    monkeypatch.setattr(iv, "unregister_subprocess",
                        lambda handle: unregistered.append(handle))
    monkeypatch.setattr(ea, "kill_tree", lambda pid: killed.append(pid))

    driver = ea.ClaudeDriver()
    monkeypatch.setattr(driver, "_maybe_sweep", lambda: None)

    proc = FakeProc()

    def fake_spawn(session_key, spec):
        s = ea._ClaudeSession(session_key, spec)
        s.proc = proc
        return s

    monkeypatch.setattr(driver, "_spawn", fake_spawn)
    monkeypatch.setattr(driver, "_teardown", lambda s: None)
    return driver, proc, registered, unregistered, killed


def _run(driver):
    return driver.run_turn(
        "k", "hi", ea.TurnSpec(cwd=None, llm={}),
        on_event=lambda e: None)


def test_turn_registers_tree_kill_handle(wiring):
    driver, proc, registered, unregistered, _ = wiring
    _run(driver)
    assert len(registered) == 1
    handle = registered[0]
    assert handle.pid == proc.pid
    assert handle is not proc          # duck-typed handle, not the raw Popen
    assert unregistered == [handle]    # paired unregister on turn exit


def test_handle_kill_takes_the_whole_tree(wiring):
    driver, proc, registered, _, killed = wiring
    _run(driver)
    registered[0].kill()
    assert killed == [proc.pid]        # kill_tree semantics, not proc.kill()


def test_warm_session_reattaches_each_turn(wiring):
    driver, proc, registered, unregistered, _ = wiring
    _run(driver)
    # simulate a second turn reusing the warm session (proc still alive here
    # only via FakeProc.poll's fixed value; the session was torn down, so
    # _spawn runs again — either way the next turn must register again)
    _run(driver)
    assert len(registered) == 2
    assert len(unregistered) == 2


def test_spawn_env_forces_python_utf8(monkeypatch):
    """中文 Windows 子进程 stdio 默认 GBK，claude 按 UTF-8 捕获即乱码且字节
    已丢——spawn 时强制 python 系 UTF-8 从源头修（GBK 内部 CLI 靠 prompt
    指令的 iconv 兜底，另一处测试覆盖）。"""
    captured = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            captured["env"] = kw["env"]
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b"")   # EOF for the read loop
            self.stderr = io.BytesIO(b"")   # EOF for the drain thread
            self.pid = 9999
            self.poll = lambda: 0

    monkeypatch.setattr(ea.subprocess, "Popen", FakePopen)
    driver = ea.ClaudeDriver()
    monkeypatch.setattr(driver, "_maybe_sweep", lambda: None)
    try:
        s = driver._spawn("utf8-env-test", ea.TurnSpec(cwd=None, llm={}))
        assert s is not None
        assert captured["env"]["PYTHONUTF8"] == "1"
        assert captured["env"]["PYTHONIOENCODING"] == "utf-8"
    finally:
        driver.dispose("utf8-env-test")


def test_spawn_max_tokens_env(monkeypatch):
    """external_agents.claude.max_tokens → CLAUDE_CODE_MAX_OUTPUT_TOKENS env
    （claude 无 --max-tokens 旗标，env 是唯一通道）；未设则不注入。"""
    captured = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            captured["env"] = kw["env"]
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b"")   # EOF for the read loop
            self.stderr = io.BytesIO(b"")   # EOF for the drain thread
            self.pid = 9997
            self.poll = lambda: 0

    monkeypatch.setattr(ea, "_resolve_bin", lambda b: "C:/fake/claude.exe")
    monkeypatch.setattr(ea, "_add_pid", lambda entry: None)
    monkeypatch.setattr(ea.subprocess, "Popen", FakePopen)
    driver = ea.ClaudeDriver()
    monkeypatch.setattr(driver, "_maybe_sweep", lambda: None)
    try:
        s = driver._spawn("max-tokens-test", ea.TurnSpec(
            cwd=None, llm={"max_tokens": 32768}))
        assert s is not None
        assert captured["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "32768"

        s2 = driver._spawn("max-tokens-test2", ea.TurnSpec(cwd=None, llm={}))
        assert s2 is not None
        assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in captured["env"]
    finally:
        driver.dispose("max-tokens-test")
        driver.dispose("max-tokens-test2")


def test_spawn_extra_args_before_resume(monkeypatch):
    """external_agents.claude.extra_args 原样进 argv，位置在 --resume 之前
    （与 codex 侧同一语义）。"""
    captured = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b"")   # EOF for the read loop
            self.stderr = io.BytesIO(b"")   # EOF for the drain thread
            self.pid = 9998
            self.poll = lambda: 0

    monkeypatch.setattr(ea, "_resolve_bin", lambda b: "C:/fake/claude.exe")
    monkeypatch.setattr(ea, "_add_pid", lambda entry: None)
    monkeypatch.setattr(ea, "_resume_ids",
                        {("claude", "extra-args-test"): "sid-1"})
    monkeypatch.setattr(ea.subprocess, "Popen", FakePopen)
    driver = ea.ClaudeDriver()
    monkeypatch.setattr(driver, "_maybe_sweep", lambda: None)
    try:
        s = driver._spawn("extra-args-test", ea.TurnSpec(
            cwd=None, llm={}, extra_args=["--settings", '{"env": true}']))
        assert s is not None
        a = captured["argv"]
        assert a[a.index("--resume") + 1] == "sid-1"
        assert a.index("--settings") < a.index("--resume")
        assert a[a.index("--settings") + 1] == '{"env": true}'
    finally:
        driver.dispose("extra-args-test")
