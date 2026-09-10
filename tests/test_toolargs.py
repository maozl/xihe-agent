# -*- coding: utf-8 -*-
"""按需全量参数：emitter 的 args 环 + GET /toolargs。

实时 tool_call 事件只带 500 字切片 + id；全量留在 serve 内存环里等桌面
展开时拉取。历史轮次走 trace 端点（sessions.db 直出全量）。
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

import gateway.serve.emitter as em


@pytest.fixture(autouse=True)
def _reset_ring(monkeypatch):
    # 模块级环与计数器是跨用例共享的——全套跑时其他用例可能已推进
    monkeypatch.setattr(em, "_args_ring", __import__("collections").OrderedDict())
    monkeypatch.setattr(em, "_args_ring_seq", 0)


def test_ring_put_get_and_eviction():
    for i in range(1, em._ARGS_RING_CAP + 5):
        n = em.put_tool_args(f"t{i}", "x" * 10)
        assert n == i
    # 超容量 → 最老的被挤出
    assert em.get_tool_args(1) is None
    assert em.get_tool_args(em._ARGS_RING_CAP + 4)["name"] == f"t{em._ARGS_RING_CAP + 4}"


def test_emitter_slices_and_carries_id():
    e = em.Emitter.__new__(em.Emitter)  # 不起队列线程，只测 on_tool_start
    e._q = None
    e._conv_id = "c1"
    e._turn_id = "t1"
    captured = {}
    e._emit = lambda ev: captured.update(ev)
    big = "y" * 900
    e.on_tool_start("write_file", big)
    assert captured["args"] == big[:500]
    assert captured["id"] is not None
    assert em.get_tool_args(captured["id"])["args"] == big


def test_toolargs_endpoint(monkeypatch):
    from gateway.serve import ServeApp
    from tests.test_serve_first_run import FakeCtx

    i = em.put_tool_args("write_file", "z" * 900)
    app = ServeApp(FakeCtx({"api_key": "k", "model": "m",
                            "compression_threshold": 0.5}), version="t")

    def _get(qid):
        req = SimpleNamespace(query={"id": qid})
        return asyncio.run(app.toolargs_full(req))

    r = _get(str(i))
    body = json.loads(r.text)
    assert body["ok"] and len(body["args"]) == 900
    assert _get("999999").status == 404      # 未知 id
    assert _get("abc").status == 404         # 非法 id
