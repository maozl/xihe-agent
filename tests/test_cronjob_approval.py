"""L1 — cron 审批接线：任务名记忆桶、审批卡投递与入站折批复、无通道回退。

`_execute_job` 的调度/落盘周边全部打桩，只验证审批维度的接线。
"""
import threading
import time

import pytest

import tools._approvals as _approvals
import tools.cronjob_tools as cron


@pytest.fixture(autouse=True)
def _clean_active(monkeypatch):
    monkeypatch.setattr(cron, "_active_sessions", {})
    monkeypatch.setattr(cron, "_active_agents", {})
    monkeypatch.setattr(cron, "_cancel_flags", set())
    yield


class _StubAdapter:
    name = "wecom"

    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class _FakeCronAgent:
    def __init__(self):
        self.kwargs = None

    def chat(self, **kw):
        self.kwargs = kw
        return "done"


def _stub_execute_env(monkeypatch, agent):
    monkeypatch.setattr(cron, "_get_agent", lambda: agent)
    monkeypatch.setattr(cron, "_save_job_output", lambda *a: None)
    monkeypatch.setattr(cron, "_mark_job_run", lambda *a, **k: None)
    monkeypatch.setattr(cron, "_deliver_result", lambda *a: None)


def test_execute_job_keys_approval_memory_by_job_name(monkeypatch):
    # 记忆桶按任务名（非 job_id、非每次运行的时间戳）——同任务跨运行共享
    fake = _FakeCronAgent()
    _stub_execute_env(monkeypatch, fake)
    cron._execute_job({"id": "j-123", "name": "nightly-clean", "prompt": "hi"})
    assert fake.kwargs["approval_key"] == "cron_job:nightly-clean"
    # 无投递通道（无 adapter）：不接回调，维持无人值守即拒
    assert "approval_request_callback" not in fake.kwargs


def test_execute_job_with_channel_wires_callbacks(monkeypatch):
    fake = _FakeCronAgent()
    _stub_execute_env(monkeypatch, fake)
    monkeypatch.setattr(cron, "_platform_adapter", _StubAdapter())
    job = {"id": "j-1", "name": "deploy", "prompt": "hi",
           "origin": {"platform": "wecom", "chat_id": "chat-9"}}
    cron._execute_job(job)
    assert "approval_request_callback" in fake.kwargs
    assert "approval_result_callback" in fake.kwargs


def test_no_delivery_channel_returns_no_callbacks(monkeypatch):
    monkeypatch.setattr(cron, "_platform_adapter", _StubAdapter())
    # deliver=local：没有确认通道，不弹卡
    assert cron._make_approval_callbacks(
        {"id": "j1", "name": "x", "deliver": "local"}, object()) == (None, None)


def test_approval_card_roundtrip_reply_resolves_agent(monkeypatch, make_agent):
    """端到端：卡投到 deliver 聊天 → 登记路由表 → 入站 a 折批复 → 决议回投。"""
    from tests.fakes import FakeChatClient
    adapter = _StubAdapter()
    monkeypatch.setattr(cron, "_platform_adapter", adapter)
    agent = make_agent(FakeChatClient())
    agent.config["approvals"] = {"timeout": 5}
    job = {"id": "j-1", "name": "deploy", "prompt": "x",
           "origin": {"platform": "wecom", "chat_id": "chat-9"}}
    req, res = cron._make_approval_callbacks(job, agent)
    assert req is not None
    agent._approval_shared["request_cb"] = req
    agent._approval_shared["result_cb"] = res

    box = {}
    t = threading.Thread(
        target=lambda: box.update(
            zip(("approved", "reason", "always"),
                agent.request_approval("terminal", "危险命令：rm -rf /tmp/x"))))
    t.start()
    deadline = time.time() + 5
    while not adapter.sent and time.time() < deadline:
        time.sleep(0.02)
    # 卡片带上任务名，回复指引 y/n/a
    assert adapter.sent and "deploy" in adapter.sent[0][1] and "不再询问" in adapter.sent[0][1]
    # 该聊天上的整词回复折给挂起的审批
    assert _approvals.resolve_pending_reply("wecom", "chat-9", "a") is True
    t.join(5)
    assert box["approved"] is True and box["always"] is True
    # 决议回投 + 路由表已清（result 回调负责注销）
    assert any("已批准" in m[1] for m in adapter.sent[1:])
    assert ("wecom", "chat-9") not in _approvals._pending_external


def test_card_delivery_failure_denies_immediately(monkeypatch, make_agent):
    """卡片发不出去（通道挂了）= 没有确认通道：立即拒绝，不空等超时。"""
    from tests.fakes import FakeChatClient

    class _DeadAdapter(_StubAdapter):
        async def send(self, chat_id, text, **kw):
            raise RuntimeError("connection gone")

    monkeypatch.setattr(cron, "_platform_adapter", _DeadAdapter())
    agent = make_agent(FakeChatClient())
    agent.config["approvals"] = {"timeout": 5}
    job = {"id": "j-1", "name": "deploy", "prompt": "x",
           "origin": {"platform": "wecom", "chat_id": "chat-9"}}
    req, res = cron._make_approval_callbacks(job, agent)
    agent._approval_shared["request_cb"] = req
    agent._approval_shared["result_cb"] = res

    approved, reason, _ = agent.request_approval("terminal", "危险命令：rm -rf /tmp/x")
    assert approved is False
    assert "发送失败" in reason
    assert not _approvals._pending_external


def test_interrupt_job_run_breaks_approval_wait(monkeypatch, make_agent):
    """删除/暂停任务时打断执行：挂在审批等待上的任务立即解除，不等超时。"""
    from tests.fakes import FakeChatClient
    agent = make_agent(FakeChatClient())
    agent.config["approvals"] = {"timeout": 30}
    agent._approval_shared["request_cb"] = lambda info: None
    agent._approval_shared["result_cb"] = lambda *a: None
    cron._active_agents["j-1"] = agent

    box = {}
    t = threading.Thread(
        target=lambda: box.update(
            approved=agent.request_approval("terminal", "危险命令")[0]))
    t.start()
    time.sleep(0.3)
    cron._interrupt_job_run("j-1")
    t.join(5)
    assert box["approved"] is False
