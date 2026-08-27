"""L1 tests for live-stream attribution (`by`) and the external_agent result
forwarding fix.

Three links in the chain, each pinned here:
  1. serve's Emitter puts ``by`` on WS events (absent for main-agent events).
  2. delegate_tool's bridge tags child events ``by=label``; an inner ``by``
     (claude nested inside a specialist) wins over the outer label.
  3. external_agent's forwarder routes tool_result to the RESULT stash —
     regression: it used to read ``_active_tool_call_cb``, which serve never
     sets, so claude's tool rows spun forever with no result.
"""

import queue
from types import SimpleNamespace

import core.external_agent as ea
import tools.delegate_tool as dt
from gateway.serve import Emitter
from tools import external_agent_tool as ext


class _Rec:
    """Records (args, kwargs) per callback name."""

    def __init__(self):
        self.calls = []

    def fn(self, name):
        def _cb(*a, **kw):
            self.calls.append((name, a, kw))
        return _cb


# ---------------------------------------------------------------- Emitter ---

def _drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def test_emitter_tags_events_with_by():
    q = queue.Queue()
    em = Emitter(q, "t1", "c1", "s")
    em.on_delta("think", kind="reasoning", by="claude")
    em.on_tool_start("Read", "pyproject", by="claude")
    em.on_tool_result("Read", "ok", 1.5, by="claude")
    events = _drain(q)
    assert [e["type"] for e in events] == ["thought_delta", "tool_call", "tool_result"]
    assert all(e["by"] == "claude" for e in events)


def test_emitter_main_agent_events_have_no_by():
    q = queue.Queue()
    em = Emitter(q, "t1", "c1", "s")
    em.on_delta("hi")
    em.on_tool_start("read_file", "a")
    em.on_tool_result("read_file", "r", 0.1)
    events = _drain(q)
    assert len(events) == 3
    assert all("by" not in e for e in events)


# ------------------------------------------------------- delegate bridge ----

class FakeChild:
    max_iterations = 5
    _last_exit_reason = "completed"

    def __init__(self):
        self.chat_kwargs = None

    def chat(self, source=None, user_message=None, **kwargs):
        self.chat_kwargs = kwargs
        # Fire exactly as agent.py does: kind= as kwarg, tools positionally.
        kwargs["stream_delta_callback"]("thinking", kind="reasoning")
        kwargs["tool_call_start_callback"]("read_file", "args")
        kwargs["tool_result_callback"]("read_file", "res", 1.0)
        # Inner source (claude under a specialist) must win over the label.
        kwargs["tool_call_start_callback"]("Read", "a", by="claude")
        return "summary"


def test_bridge_labels_child_events(monkeypatch):
    rec = _Rec()
    parent = SimpleNamespace(
        _active_stream_delta_cb=rec.fn("stream"),
        _active_tool_call_start_cb=rec.fn("start"),
        _active_tool_call_cb=rec.fn("done"),
        _active_tool_result_cb=rec.fn("result"),
    )
    monkeypatch.setattr(dt, "_extract_tool_trace", lambda child, source: [])

    entry = dt._run_single_child(0, "goal", FakeChild(), parent,
                                 source=object(), label="itsm")

    assert entry["status"] == "completed"
    assert ("stream", ("thinking",), {"kind": "reasoning", "by": "itsm"}) in rec.calls
    assert ("start", ("read_file", "args"), {"by": "itsm"}) in rec.calls
    assert ("result", ("read_file", "res", 1.0), {"by": "itsm"}) in rec.calls
    # innermost attribution wins
    assert ("start", ("Read", "a"), {"by": "claude"}) in rec.calls


# ------------------------------------------------- external_agent forward ---

def _run_external(rec, monkeypatch):
    parent = SimpleNamespace(
        config={},
        _active_stream_delta_cb=rec.fn("stream"),
        _active_tool_call_start_cb=rec.fn("start"),
        _active_tool_call_cb=rec.fn("done"),
        _active_tool_result_cb=rec.fn("result"),
    )

    class FakeDriver:
        def run_turn(self, session_key, prompt, spec, on_event):
            # Driver shape: result payload lives in `args`.
            on_event({"type": "thought_delta", "text": "hm"})
            on_event({"type": "tool_call", "name": "Read", "args": "path"})
            on_event({"type": "tool_result", "name": "Read",
                      "args": "file body", "elapsed": 2})
            return SimpleNamespace(exit_reason="completed", final_text="done",
                                   duration_seconds=2.0, tool_trace=[],
                                   error=None, session_id="s1")

    monkeypatch.setattr(ea, "get_driver", lambda engine: FakeDriver())
    monkeypatch.setattr(ext, "_resolve_engine_bin", lambda config, engine: "/fake/bin")
    out = ext._external_agent(
        {"prompt": "analyze"}, parent_agent=parent,
        context={"session_key": "test-ext"})
    return parent, rec, out


def test_forwarder_routes_result_to_result_stash(monkeypatch):
    rec = _Rec()
    _run_external(rec, monkeypatch)
    kinds = [(n, a, kw) for (n, a, kw) in rec.calls]
    assert ("stream", ("hm",), {"kind": "reasoning", "by": "claude"}) in kinds
    assert ("start", ("Read", "path"), {"by": "claude"}) in kinds
    # the fixed link: result → _active_tool_result_cb with full payload
    assert ("result", ("Read", "file body", 2.0), {"by": "claude"}) in kinds
    # done stash must NOT receive the result (no double-fire on serve)
    assert not any(n == "done" for (n, _, _) in kinds)


def test_forwarder_falls_back_to_done_stash_for_cli(monkeypatch):
    # CLI wires only tool_call_callback (tool_finish) — no result stash set.
    rec = _Rec()
    parent = SimpleNamespace(
        config={},
        _active_stream_delta_cb=rec.fn("stream"),
        _active_tool_call_start_cb=rec.fn("start"),
        _active_tool_call_cb=rec.fn("done"),
        _active_tool_result_cb=None,
    )

    class FakeDriver:
        def run_turn(self, session_key, prompt, spec, on_event):
            on_event({"type": "tool_result", "name": "Read",
                      "args": "body", "elapsed": 1})
            return SimpleNamespace(exit_reason="completed", final_text="x",
                                   duration_seconds=1.0, tool_trace=[],
                                   error=None, session_id=None)

    monkeypatch.setattr(ea, "get_driver", lambda engine: FakeDriver())
    monkeypatch.setattr(ext, "_resolve_engine_bin", lambda config, engine: "/fake/bin")
    ext._external_agent({"prompt": "p"}, parent_agent=parent,
                        context={"session_key": "k"})
    assert ("done", ("Read", "body", 1.0), {"by": "claude"}) in rec.calls


def test_prompt_carries_language_and_windows_encoding_directives(monkeypatch):
    import os as _os

    prompts = []
    rec = _Rec()
    parent = SimpleNamespace(
        config={},
        _active_stream_delta_cb=None,
        _active_tool_call_start_cb=None,
        _active_tool_call_cb=None,
        _active_tool_result_cb=None,
    )

    class FakeDriver:
        def run_turn(self, session_key, prompt, spec, on_event):
            prompts.append(prompt)
            return SimpleNamespace(exit_reason="completed", final_text="x",
                                   duration_seconds=1.0, tool_trace=[],
                                   error=None, session_id=None)

    monkeypatch.setattr(ea, "get_driver", lambda engine: FakeDriver())
    monkeypatch.setattr(ext, "_resolve_engine_bin", lambda config, engine: "/fake/bin")
    ext._external_agent({"prompt": "p"}, parent_agent=parent,
                        context={"session_key": "k"})

    # language defaults to zh via config defaults → thinking directive present
    assert "必须始终使用中文" in prompts[0]
    # Windows mojibake self-recovery rule (GBK CLI output / GBK files)
    if _os.name == "nt":
        assert "iconv -f GBK -t UTF-8" in prompts[0]
