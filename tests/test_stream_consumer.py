"""L1 — gateway StreamConsumer process frame / reply separation.

``on_delta`` receives kind="reasoning"/"content" deltas plus tool callbacks.
Flat platform messages (WeCom/Feishu) can't fold thinking, so the live frame
is a compact feed — one gist line per thinking run, emoji lines per tool —
and the first content delta replaces the whole body with the reply.
"""
import asyncio

from gateway.stream_consumer import StreamConsumer, StreamConsumerConfig


class _FakeAdapter:
    """Records every stream update; no real platform."""

    def __init__(self):
        self.updates: list[tuple[str, bool]] = []

    async def send_stream(self, chat_id, content, stream_id, finish=False,
                          reply_req_id=None):
        self.updates.append((content, finish))
        from platforms.base import SendResult
        return SendResult(success=True)


def _run(consumer, seconds=0.15):
    async def run():
        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(seconds)
        consumer.finish()
        await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(run())


def test_process_frame_shows_compact_feed_then_reply_replaces_it():
    adapter = _FakeAdapter()
    c = StreamConsumer(adapter, chat_id="c1",
                       config=StreamConsumerConfig(edit_interval=0.05))
    c.on_delta("User asks about db compare. First search the docs.\n"
               "then read the feature file", kind="reasoning")
    c.on_tool_start("search_files", '{"query": "db compare"}')
    c.on_tool_result("search_files", '{"success": true, "count": 30}', 0.2)
    _run(c, seconds=0.2)

    # feed lines present while processing: gist folded to one line, tool lines
    assert any("处理中" in t and "💭" in t and "🔍 search_files" in t
               for t, _ in adapter.updates)
    feed_update = next(t for t, _ in adapter.updates if "💭" in t)
    assert feed_update.count("💭") == 1  # whole thinking run = one line
    assert "✅" in feed_update

    c.on_delta("库对比功能概览…", kind="content")
    _run(c)
    last = adapter.updates[-1][0]
    assert "库对比功能概览" in last
    assert "💭" not in last and "处理中" not in last


def test_reasoning_gist_is_first_line_and_bounded():
    adapter = _FakeAdapter()
    c = StreamConsumer(adapter, chat_id="c1",
                       config=StreamConsumerConfig(edit_interval=0.05))
    long_thought = ("first actionable line\n" + "detail " * 500)
    c.on_delta(long_thought, kind="reasoning")
    c.on_delta(None)  # boundary flushes the gist
    _run(c)
    feed_update = next((t for t, _ in adapter.updates if "💭" in t), "")
    assert "first actionable line" in feed_update
    assert "detail detail" not in feed_update  # tail dropped, not scrolled


def test_tool_result_gist_bounded():
    adapter = _FakeAdapter()
    c = StreamConsumer(adapter, chat_id="c1",
                       config=StreamConsumerConfig(edit_interval=0.05))
    c.on_tool_start("read_file", '{"path": "docs/db-compare.md"}')
    c.on_tool_result("read_file", "# 数据库对比（DB Compare）\n" + "body " * 200, 0.1)
    _run(c)
    feed_update = next((t for t, _ in adapter.updates if "✅" in t), "")
    assert "数据库对比" in feed_update
    assert "body body" not in feed_update


def test_feed_is_capped_to_last_lines():
    adapter = _FakeAdapter()
    c = StreamConsumer(adapter, chat_id="c1",
                       config=StreamConsumerConfig(edit_interval=0.05))
    for i in range(20):
        c.on_tool_start("read_file", '{"path": "f%d.md"}' % i)
    _run(c)
    feed_update = next((t for t, _ in adapter.updates if "f19" in t), "")
    assert "f19" in feed_update
    assert "f0.md" not in feed_update  # old lines dropped


def test_final_finished_message_contains_only_content():
    adapter = _FakeAdapter()
    c = StreamConsumer(adapter, chat_id="c1",
                       config=StreamConsumerConfig(edit_interval=0.05))
    c.on_delta("hidden reasoning run", kind="reasoning")
    c.on_tool_start("read_file", '{"path": "x.md"}')
    c.on_delta("the actual reply", kind="content")
    _run(c)

    finished = [t for t, fin in adapter.updates if fin]
    assert finished, "a finish=True update must land"
    assert finished[-1] == "the actual reply"


def test_pure_content_has_no_process_frame():
    adapter = _FakeAdapter()
    c = StreamConsumer(adapter, chat_id="c1",
                       config=StreamConsumerConfig(edit_interval=0.05))
    c.on_delta("just a reply", kind="content")
    _run(c)

    full = "".join(t for t, _ in adapter.updates)
    assert "just a reply" in full
    assert "处理中" not in full


def test_content_delivered_flag_tracks_real_content():
    adapter = _FakeAdapter()
    c = StreamConsumer(adapter, chat_id="c1",
                       config=StreamConsumerConfig(edit_interval=0.05))
    assert not c.content_delivered
    c.on_delta("some thinking", kind="reasoning")
    c.on_tool_start("read_file", '{"path": "x.md"}')
    assert not c.content_delivered
    c.on_delta("real text", kind="content")
    assert c.content_delivered


def test_events_after_content_are_dropped():
    # once the reply is streaming, later process events must not splice in
    adapter = _FakeAdapter()
    c = StreamConsumer(adapter, chat_id="c1",
                       config=StreamConsumerConfig(edit_interval=0.05))
    c.on_delta("first reply", kind="content")
    c.on_delta(None)
    c.on_delta("more hidden thinking", kind="reasoning")
    c.on_tool_start("read_file", '{"path": "late.md"}')
    c.on_delta(" continued reply", kind="content")
    _run(c)

    last = adapter.updates[-1][0]
    assert "first reply" in last
    assert "continued reply" in last
    assert "hidden thinking" not in last
    assert "late.md" not in last
