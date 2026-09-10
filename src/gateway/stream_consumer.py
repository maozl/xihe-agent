"""Stream consumer — bridges sync agent deltas to async platform delivery.

Supports two transport modes:
1. WeCom stream: sends progressive stream messages via send_stream()
2. Edit-based (Feishu, etc.): sends initial message, then edits it via edit_message()

The agent fires stream_delta_callback(text) synchronously from its worker thread.
StreamConsumer:
  1. Receives deltas via on_delta() (thread-safe, sync)
  2. Queues them to an asyncio task via queue.Queue
  3. The async run() task buffers, rate-limits, and progressively delivers
"""

import asyncio
import logging
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_DONE = object()
_NEW_SEGMENT = object()
_PROCESS_BEGIN = object()
_CONTENT_BEGIN = object()
_CUT = object()

# Flat platform messages (WeCom/Feishu) can't fold thinking, so the process
# frame is a compact live feed: one collapsed gist line per thinking run plus
# emoji tool-activity lines. The first content delta replaces the whole body
# with the reply — process visible while it happens, final message clean.
_PROCESS_HEADER = "🧠 处理中（完成后展示回复）"
_REASONING_GIST = 80   # chars shown per thinking run (its first line)
_FEED_LINES = 10       # process lines kept in the frame

_TOOL_EMOJI = (
    ("search", "🔍"), ("find", "🔍"), ("kbs", "🔍"),
    ("read", "📖"), ("view", "📖"),
    ("terminal", "⌨️"), ("execute", "⌨️"), ("sandbox", "⌨️"),
    ("browser", "🌐"), ("navigate", "🌐"),
    ("write", "✏️"), ("edit", "✏️"), ("save", "✏️"),
    ("agent", "🤝"), ("delegate", "🤝"),
)


def _tool_emoji(name: str) -> str:
    n = name.lower()
    for key, emoji in _TOOL_EMOJI:
        if key in n:
            return emoji
    return "🔧"


def _gist(text: str, limit: int = _REASONING_GIST) -> str:
    """First non-empty line of a thinking run, truncated."""
    for line in text.splitlines():
        s = line.strip()
        if s:
            return s[:limit] + ("…" if len(s) > limit else "")
    return ""


def _arg_gist(args_summary: str) -> str:
    """First short JSON *value* in the args (skip keys), else a raw prefix."""
    m = re.search(r':\s*"([^"\\]{2,40})"', args_summary or "")
    if m:
        return m.group(1)
    m = re.search(r'"([^"\\]{2,40})"', args_summary or "")
    return m.group(1) if m else (args_summary or "")[:40]


def _result_gist(result: str) -> str:
    """First non-empty line of a tool result, truncated."""
    for line in (result or "").splitlines():
        s = line.strip()
        if s:
            return s[:60] + ("…" if len(s) > 60 else "")
    return ""


@dataclass
class StreamConsumerConfig:
    edit_interval: float = 0.5
    buffer_threshold: int = 40
    max_message_length: int = 4000


class StreamConsumer:
    """Async consumer that progressively delivers streamed text.

    Automatically detects the transport mode:
    - If adapter has send_stream() → WeCom stream mode
    - If adapter has edit_message() → edit-based mode (Feishu, etc.)

    Usage::

        consumer = StreamConsumer(adapter, chat_id, reply_req_id)
        # Pass consumer.on_delta as stream_delta_callback to agent.chat()
        task = asyncio.create_task(consumer.run())
        # ... run agent in thread pool ...
        consumer.finish()
        await task
    """

    def __init__(self, adapter, chat_id: str,
                 reply_req_id: str = None,
                 config: StreamConsumerConfig = None):
        self.adapter = adapter
        self.chat_id = chat_id
        self.reply_req_id = reply_req_id
        self.cfg = config or StreamConsumerConfig()
        self._queue: queue.Queue = queue.Queue()
        self._accumulated = ""
        self._feed: list[str] = []  # compact process lines (thinking gists + tool activity)
        self._pending_gist = ""
        self._content_sent = False
        self._frame_open = False
        self._stream_id = f"stream-{uuid.uuid4().hex}"
        self._already_sent = False
        self._last_send_time = 0.0
        self._message_id: Optional[str] = None

        self._use_wecom_stream = hasattr(adapter, 'send_stream')
        self._use_edit = hasattr(adapter, 'edit_message')

    @property
    def already_sent(self) -> bool:
        """True if at least one stream message was sent."""
        return self._already_sent

    @property
    def content_delivered(self) -> bool:
        """True once real (non-placeholder) content was streamed."""
        return self._content_sent

    def on_delta(self, text: str, **kwargs) -> None:
        """Thread-safe callback — called from the agent's worker thread.

        When text is None, signals a tool boundary.
        kind="reasoning" deltas collapse into one gist line per thinking run
        in the live process frame; the first kind="content" delta replaces
        the frame with the reply.
        """
        if kwargs.get("kind") == "reasoning":
            # Buffer each thinking run; emit its gist on the next boundary
            # (tool start / content / done) so mid-run deltas don't spam.
            if text and not self._content_sent:
                self._open_process_frame()
                self._pending_gist += text
            return
        if text is None:
            self._flush_gist()
            return
        self._flush_gist()
        if not self._content_sent:
            self._queue.put(_CONTENT_BEGIN)
        self._content_sent = True
        self._queue.put(text)

    def _open_process_frame(self) -> None:
        """Enter the process frame once, on the first pre-reply event."""
        if not self._frame_open:
            self._frame_open = True
            self._queue.put(_PROCESS_BEGIN)

    def _flush_gist(self) -> None:
        """Fold buffered thinking into one gist line; no-op when empty."""
        if self._pending_gist.strip():
            self._open_process_frame()
            self._feed.append(f"💭 {_gist(self._pending_gist)}")
        self._pending_gist = ""

    def on_tool_start(self, name: str, args_summary: str, by: str = None) -> None:
        self._flush_gist()
        if not self._content_sent:
            self._open_process_frame()
            tag = f"[{by}] " if by else ""
            self._feed.append(f"{tag}{_tool_emoji(name)} {name}({_arg_gist(args_summary)})")

    def on_tool_result(self, name: str, result: str, elapsed: float,
                       by: str = None) -> None:
        if not self._content_sent:
            self._open_process_frame()
            self._feed.append(f"✅ {_result_gist(result)}")

    def finish(self) -> None:
        """Signal that the stream is complete."""
        self._queue.put(_DONE)

    def cut(self) -> None:
        """Thread-safe. Freeze the current platform message so the next event
        opens a fresh one (new stream id / new edit target).

        Used when approval cards must sit between the pre- and post-approval
        progress: without the cut, the post-approval feed keeps mutating the
        message sitting ABOVE the cards — the chat reads out of order. The old
        message keeps its last body and is deliberately NOT finished: the reply
        channel stays open for the cards and the continuation (finish-then-
        reopen on one req is unproven against the WeCom gateway).

        Blocks until run() has flushed the old segment's final update, so the
        approval card sent right after cut() can never jump AHEAD of the frozen
        frame (both deliveries race on the event loop otherwise). Bounded wait:
        a dead consumer must not block the approval prompt forever.
        """
        ev = threading.Event()
        self._queue.put((_CUT, ev))
        ev.wait(timeout=5.0)

    async def run(self) -> None:
        """Async task that drains the queue and sends platform updates."""
        _safe_limit = max(500, self.cfg.max_message_length - 100)
        # True while the message body is the compact process frame; the first
        # content delta flips it and the reply replaces the whole body.
        in_process_phase = False
        last_feed_len = 0

        try:
            while True:
                # Drain all available items
                got_done = False
                got_segment_break = False
                got_cut = False
                cut_ev = None
                while True:
                    try:
                        item = self._queue.get_nowait()
                        if item is _DONE:
                            got_done = True
                            break
                        if item is _NEW_SEGMENT:
                            got_segment_break = True
                            break
                        if isinstance(item, tuple) and item[0] is _CUT:
                            got_cut = True
                            cut_ev = item[1]
                            break
                        if item is _PROCESS_BEGIN:
                            in_process_phase = True
                            continue
                        if item is _CONTENT_BEGIN:
                            in_process_phase = False
                            self._accumulated = ""
                            continue
                        if in_process_phase:
                            continue
                        self._accumulated += item
                    except queue.Empty:
                        break

                if got_cut:
                    # Last update on the old segment, then a fresh one: new
                    # stream id / edit target, empty feed. Items queued after
                    # the cut belong to the new segment and are drained on the
                    # next pass. _content_sent is kept — it gates the gateway's
                    # final-send dedup for the whole turn, not per segment.
                    try:
                        if in_process_phase and self._feed:
                            body = "\n".join(
                                [_PROCESS_HEADER, *self._feed[-_FEED_LINES:]])
                            await self._send_update(body, finish=False)
                        elif self._accumulated:
                            await self._send_update(self._accumulated,
                                                    finish=False)
                    finally:
                        self._accumulated = ""
                        self._feed = []
                        self._pending_gist = ""
                        self._frame_open = False
                        self._message_id = None
                        self._stream_id = f"stream-{uuid.uuid4().hex}"
                        in_process_phase = False
                        last_feed_len = 0
                        if cut_ev is not None:
                            cut_ev.set()
                    continue

                now = time.monotonic()
                elapsed = now - self._last_send_time

                if in_process_phase:
                    should_send = (
                        got_done or got_segment_break
                        or (elapsed >= self.cfg.edit_interval
                            and len(self._feed) != last_feed_len)
                    )
                    if should_send and self._feed:
                        body = "\n".join(
                            [_PROCESS_HEADER, *self._feed[-_FEED_LINES:]])
                        await self._send_update(body, finish=False)
                        last_feed_len = len(self._feed)
                        self._last_send_time = time.monotonic()
                    if got_done:
                        # process-only turn (interrupted mid-run): close the
                        # frame; the gateway's normal send path still delivers
                        # the final text (content_delivered stays False).
                        return
                    await asyncio.sleep(0.05)
                    continue

                should_send = (
                    got_done
                    or got_segment_break
                    or (elapsed >= self.cfg.edit_interval and self._accumulated)
                    or len(self._accumulated) >= self.cfg.buffer_threshold
                )

                if should_send and self._accumulated:
                    if len(self._accumulated) > _safe_limit:
                        chunks = self._split_text(self._accumulated, _safe_limit)
                        for chunk in chunks:
                            await self._send_update(chunk, finish=False)
                        self._accumulated = ""
                        self._last_send_time = time.monotonic()
                    else:
                        is_final = got_done and not got_segment_break
                        await self._send_update(
                            self._accumulated, finish=is_final)
                        self._last_send_time = time.monotonic()
                        if is_final:
                            self._accumulated = ""
                            return

                if got_done:
                    if self._accumulated:
                        await self._send_update(self._accumulated, finish=True)
                    return

                # Add a newline after each segment (tool-call boundary) so
                # the next segment starts on a new line, not glued to the
                # previous text.
                if got_segment_break and self._accumulated:
                    if not self._accumulated.endswith("\n"):
                        self._accumulated += "\n"

                await asyncio.sleep(0.05)

        except asyncio.CancelledError:
            if self._accumulated:
                try:
                    await self._send_update(self._accumulated, finish=True)
                except Exception:
                    logger.warning("final accumulated message lost on cancel",
                                   exc_info=True)
        except Exception as e:
            logger.error("Stream consumer error: %s", e)

    @staticmethod
    def _split_text(text: str, limit: int) -> list[str]:
        if len(text) <= limit:
            return [text]
        chunks = []
        while len(text) > limit:
            split_at = text.rfind("\n", 0, limit)
            if split_at < limit // 2:
                split_at = limit
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")
        if text:
            chunks.append(text)
        return chunks

    async def _send_update(self, content: str, finish: bool) -> None:
        """Send a stream update using the appropriate transport."""
        content = self._clean_for_display(content)
        if not content.strip():
            return

        try:
            if self._use_wecom_stream:
                result = await self.adapter.send_stream(
                    chat_id=self.chat_id,
                    content=content,
                    stream_id=self._stream_id,
                    finish=finish,
                    reply_req_id=self.reply_req_id,
                )
                if result.success:
                    self._already_sent = True
                else:
                    logger.warning("Stream send failed (finish=%s): %s",
                                    finish, getattr(result, 'error', 'unknown'))

            elif self._use_edit:
                if self._message_id:
                    result = await self.adapter.edit_message(
                        chat_id=self.chat_id,
                        message_id=self._message_id,
                        content=content,
                    )
                    if not result.success:
                        logger.debug("Edit failed, disabling streaming edits")
                        self._use_edit = False
                else:
                    result = await self.adapter.send(
                        chat_id=self.chat_id,
                        content=content,
                        reply_to_msg_id=self.reply_req_id,
                    )
                    if result.success:
                        self._message_id = result.message_id
                        self._already_sent = True

        except Exception as e:
            logger.warning("Stream send error: %s", e)

    _MEDIA_RE = re.compile(r'''[`"']?MEDIA:\s*\S+[`"']?''')

    @staticmethod
    def _clean_for_display(text: str) -> str:
        if "MEDIA:" not in text and "[[audio_as_voice]]" not in text:
            return text
        cleaned = text.replace("[[audio_as_voice]]", "")
        cleaned = StreamConsumer._MEDIA_RE.sub("", cleaned)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        return cleaned.rstrip()


# Backward compat alias
WeComStreamConsumer = StreamConsumer
