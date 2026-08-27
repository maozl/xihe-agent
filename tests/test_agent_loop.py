"""L2 — agent loop invariants driven by a scripted fake model (no network).

These are the tests that actually exercise ``XiheAgent.chat`` end to end:
tool dispatch, result assembly, and the max_iterations exit path. The
``FakeChatClient`` (see fakes.py) makes the loop fully deterministic.
"""
import json
import threading

import pytest

from core.session import SessionSource
from tools import registry

from tests.fakes import FakeChatClient


# --- A minimal read-only tool the scripted model can call ---------------------
# Registered once (idempotent). The FakeChatClient only ever requests this
# name, so real tools present in the schema are inert.

_ECHO_CALLED = {"count": 0}

_ECHO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "test_echo",
        "description": "Test-only echo tool.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": [],
        },
    },
}


def _echo_handler(args, **_kw):
    _ECHO_CALLED["count"] += 1
    return json.dumps({"ok": True, "echo": args.get("text", "")})


@pytest.fixture(autouse=True)
def _register_echo():
    if "test_echo" not in registry._tools:
        registry.register(
            name="test_echo",
            schema=_ECHO_SCHEMA,
            handler=_echo_handler,
            toolset="files",
            read_only=True,
        )
    _ECHO_CALLED["count"] = 0
    yield


def test_max_iterations_sets_exit_reason(make_agent):
    """A model that never produces a final answer must hit the iteration cap,
    set ``_last_exit_reason == 'max_iterations'``, and return the friendly
    Chinese closing message — not the old English sentinel / silent end.

    This directly guards the _last_exit_reason change.
    """
    client = FakeChatClient(never_finish=True)
    agent = make_agent(client)
    src = SessionSource(platform="cli", chat_id="t_maxiter", chat_type="dm")

    result = agent.chat(source=src, user_message="loop forever", max_iterations=2)

    assert agent._last_exit_reason == "max_iterations"
    assert "单轮处理上限" in result
    # The loop ran both iterations and dispatched the tool each time.
    assert _ECHO_CALLED["count"] == 2


def test_tool_call_roundtrip(make_agent):
    """Tool call -> tool result -> final answer: the dispatch + result-assembly
    path works end to end with the fake client."""
    client = FakeChatClient(script=[
        {"tool_calls": [{"id": "call_1", "name": "test_echo",
                         "arguments": '{"text": "hi"}'}]},
        {"content": "all done"},
    ])
    agent = make_agent(client)
    src = SessionSource(platform="cli", chat_id="t_roundtrip", chat_type="dm")

    result = agent.chat(source=src, user_message="call echo then finish")

    assert _ECHO_CALLED["count"] == 1                 # the tool actually ran
    assert agent._last_exit_reason == "completed"
    assert result == "all done"


# --- dispatch segmentation: reads parallelize around a sequential write ------

_DISPATCH_STATE = {"barrier": None, "events": []}
_DISPATCH_LOCK = threading.Lock()


def _barrier_handler(args, **_kw):
    tag = args.get("text", "")
    with _DISPATCH_LOCK:
        _DISPATCH_STATE["events"].append(("enter", tag))
    ok = True
    if args.get("wait"):
        # Passes only if another thread reaches the barrier concurrently —
        # sequential execution breaks it (BrokenBarrierError), which is the
        # regression signal for "one write serialized the whole batch".
        try:
            _DISPATCH_STATE["barrier"].wait(timeout=10)
        except threading.BrokenBarrierError:
            ok = False
    with _DISPATCH_LOCK:
        _DISPATCH_STATE["events"].append(("exit", tag, ok))
    return json.dumps({"ok": ok})


def _write_handler(args, **_kw):
    with _DISPATCH_LOCK:
        _DISPATCH_STATE["events"].append(("write",))
    return json.dumps({"written": True})


def _dispatch_schema(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Test-only {name}.",
            "parameters": {"type": "object",
                           "properties": {"text": {"type": "string"},
                                          "wait": {"type": "boolean"}},
                           "required": []},
        },
    }


@pytest.fixture(autouse=True)
def _register_dispatch_tools():
    if "test_barrier_echo" not in registry._tools:
        registry.register(name="test_barrier_echo", schema=_dispatch_schema("test_barrier_echo"),
                          handler=_barrier_handler, toolset="files", read_only=True)
    if "test_write_seq" not in registry._tools:
        registry.register(name="test_write_seq", schema=_dispatch_schema("test_write_seq"),
                          handler=_write_handler, toolset="files", read_only=False)
    _DISPATCH_STATE["barrier"] = threading.Barrier(2)
    _DISPATCH_STATE["events"] = []
    yield


def test_mixed_batch_parallel_reads_around_sequential_write(make_agent):
    """[read, read, write, read]: the leading read pair runs concurrently
    (barrier passes), the write runs alone after them, the trailing read
    follows the write — model order preserved."""
    client = FakeChatClient(script=[
        {"tool_calls": [
            {"id": "c1", "name": "test_barrier_echo",
             "arguments": '{"text": "a", "wait": true}'},
            {"id": "c2", "name": "test_barrier_echo",
             "arguments": '{"text": "b", "wait": true}'},
            {"id": "c3", "name": "test_write_seq", "arguments": "{}"},
            {"id": "c4", "name": "test_barrier_echo",
             "arguments": '{"text": "c"}'},
        ]},
        {"content": "done"},
    ])
    agent = make_agent(client)

    result = agent.chat(source=_src("t_mixed_batch"), user_message="mixed batch")

    assert result == "done"
    ev = _DISPATCH_STATE["events"]
    enters = [i for i, e in enumerate(ev) if e[0] == "enter"]
    exits = [i for i, e in enumerate(ev) if e[0] == "exit"]
    write = [i for i, e in enumerate(ev) if e[0] == "write"][0]
    # leading pair ran concurrently and both succeeded
    pair_ok = [e for e in ev if e[0] == "exit" and e[1] in ("a", "b")]
    assert pair_ok and all(e[2] for e in pair_ok), ev
    # both pair exits landed before the write; the solo read follows it
    assert max(i for i in exits if ev[i][1] in ("a", "b")) < write
    assert write < min(i for i in enters if ev[i][1] == "c")
    # solo read after the write also succeeded
    assert [e for e in ev if e[0] == "exit" and e[1] == "c"][0][2]


# --- empty-response escalation chain -------------------------------------------

def _src(name):
    return SessionSource(platform="cli", chat_id=name, chat_type="dm")


def test_empty_response_nudged_then_recovers(make_agent):
    """One empty response → internal nudge injected → next call answers."""
    client = FakeChatClient(script=[{"content": ""}, {"content": "recovered"}])
    agent = make_agent(client)

    result = agent.chat(source=_src("t_empty_once"), user_message="go")

    assert result == "recovered"
    assert len(client.calls) == 2
    msgs = agent.db.load_messages(agent.db.get_or_create_session(_src("t_empty_once")))
    assert any(m.get("role") == "user" and (m.get("content") or "").startswith("[系统提示]")
               for m in msgs)


def test_persistent_empties_escalate_to_compressed_retry_then_warning(make_agent):
    """Three consecutive empties: nudge → compress+nudge → give up, and the
    warning must be PERSISTED as the turn's final assistant content so a
    reloaded transcript shows the same ending the live caller received."""
    client = FakeChatClient(script=[{"content": ""}] * 3)
    agent = make_agent(client)

    result = agent.chat(source=_src("t_empty_always"), user_message="go")

    assert result.startswith("⚠️ 模型连续返回空响应")
    assert len(client.calls) == 3
    msgs = agent.db.load_messages(agent.db.get_or_create_session(_src("t_empty_always")))
    assert msgs[-1]["role"] == "assistant"
    assert msgs[-1]["content"].startswith("⚠️ 模型连续返回空响应")


def test_reshape_history_hides_internal_nudges():
    from gateway.serve import _reshape_history

    rows = [
        {"id": 1, "role": "user", "content": "go"},
        {"id": 2, "role": "assistant", "content": ""},
        {"id": 3, "role": "user", "content": "[系统提示] 上一轮输出为空。请继续当前任务并给出实质回应。"},
        {"id": 4, "role": "assistant", "content": "⚠️ 模型连续返回空响应"},
    ]
    out = _reshape_history(rows)
    # the nudge must not render; the turn folds into one warning bubble
    assert [m["role"] for m in out] == ["user", "assistant"]
    assert out[1]["content"].startswith("⚠️")
