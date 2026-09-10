# -*- coding: utf-8 -*-
"""P1：通道路由层（ChannelSender 注册表）与多目标 deliver。

通道 = 纯出站"快递"（窄契约 async send），按目标 platform 名路由；网关
适配器保留为兜底，gateway 行为零改动。多目标投递的部分失败不判死任务。
"""
import asyncio

import pytest

import core.services.scheduler as sched


class FakeChannel:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    async def send(self, target, message):
        if not self.ok:
            return False
        self.sent.append((target, message))
        return True


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(sched, "_channels", {})
    monkeypatch.setattr(sched, "_platform_adapter", None)


def _job(deliver, origin=None):
    return {"id": "j1", "name": "t", "deliver": deliver,
            "origin": origin or {"platform": "serve", "chat_id": "c1"}}


# ---- 多目标解析 ----------------------------------------------------------

def test_resolve_single_string_legacy():
    assert sched._resolve_delivery_targets(_job("desktop:c9")) == \
        [{"platform": "desktop", "chat_id": "c9"}]


def test_resolve_comma_separated_dedupes():
    ts = sched._resolve_delivery_targets(_job("desktop:c1, wecom:g2, desktop:c1"))
    assert ts == [{"platform": "desktop", "chat_id": "c1"},
                  {"platform": "wecom", "chat_id": "g2"}]


def test_resolve_list_form_and_local_member():
    # 列表写法；local 成员只把自己变 None，不影响其他目标
    ts = sched._resolve_delivery_targets(_job(["local", "desktop:c1"]))
    assert ts == [{"platform": "desktop", "chat_id": "c1"}]
    assert sched._resolve_delivery_targets(_job("local")) == []


def test_resolve_origin_uses_creating_chat():
    ts = sched._resolve_delivery_targets(_job("origin"))
    assert ts == [{"platform": "serve", "chat_id": "c1"}]


def test_legacy_single_target_view():
    assert sched._resolve_delivery_target(_job("desktop:c1, wecom:g2")) == \
        {"platform": "desktop", "chat_id": "c1"}


# ---- 路由：通道优先，适配器兜底 ------------------------------------------

def test_send_routes_to_registered_channel():
    ch = FakeChannel()
    sched.register_channel("desktop", ch)
    assert sched._send_to_chat({"platform": "desktop", "chat_id": "c1"}, "m")
    assert ch.sent == [("c1", "m")]


def test_send_unregistered_platform_falls_back_to_adapter(monkeypatch):
    class Adapter:
        name = "wecom"

        async def send(self, chat_id, message):
            self.got = (chat_id, message)
            return True

    ad = Adapter()
    monkeypatch.setattr(sched, "_platform_adapter", ad)
    # platform 没有通道 → 适配器兜底（gateway 原行为）
    assert sched._send_to_chat({"platform": "wecom", "chat_id": "g1"}, "m")
    assert ad.got == ("g1", "m")
    # 有通道时通道优先，适配器不参与
    ch = FakeChannel()
    sched.register_channel("wecom", ch)
    assert sched._send_to_chat({"platform": "wecom", "chat_id": "g1"}, "m2")
    assert ch.sent and not getattr(ad, "got", None) == ("g1", "m2")


def test_send_failed_channel_returns_false():
    sched.register_channel("desktop", FakeChannel(ok=False))
    assert sched._send_to_chat({"platform": "desktop", "chat_id": "c1"}, "m") is False


def test_send_no_route_reports_false():
    assert sched._send_to_chat({"platform": "nobody", "chat_id": "x"}, "m") is False


# ---- 投递：多目标 + 部分失败 ----------------------------------------------

def test_deliver_result_multi_target_partial_failure(monkeypatch, caplog):
    good, bad = FakeChannel(), FakeChannel(ok=False)
    sched.register_channel("desktop", good)
    sched.register_channel("wecom", bad)
    job = _job("desktop:c1,wecom:g2")
    with caplog.at_level("WARNING"):
        sched._deliver_result(job, "报告正文")
    assert good.sent and not bad.sent
    assert any("delivery failed" in r.message for r in caplog.records)


def test_deliver_result_silent_skips_all():
    ch = FakeChannel()
    sched.register_channel("desktop", ch)
    sched._deliver_result(_job("desktop:c1"), "无事 [SILENT]")
    assert ch.sent == []


def test_register_channel_multiple_names_one_sender():
    ch = FakeChannel()
    sched.register_channel("desktop", ch)
    sched.register_channel("serve", ch)  # serve 会话平台别名 → deliver origin
    assert sched._send_to_chat({"platform": "serve", "chat_id": "c1"}, "m")
    assert len(ch.sent) == 1
