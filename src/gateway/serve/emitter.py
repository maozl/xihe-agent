"""Emitter — the thread-safe bridge from the agent's sync worker-thread
callbacks to the WebSocket event queue (drained by turns.py)."""

import queue
import threading
import time
from collections import OrderedDict

from gateway.serve._common import _WS_ARGS_LIMIT, _WS_RESULT_LIMIT

# Sentinel pushed onto the emitter queue when the worker thread finishes.
_DONE = object()

# Live full-args ring: tool_call events carry a 500-char slice + an id; the
# full copy waits here for GET /toolargs (the desktop fetches on expand).
# Bounded — a turn's tool calls are transient, history turns read the trace
# endpoint instead (full args straight from sessions.db).
_ARGS_RING_CAP = 400
_args_ring: "OrderedDict[int, dict]" = OrderedDict()
_args_ring_lock = threading.Lock()
_args_ring_seq = 0


def put_tool_args(name: str, full_args: str) -> int:
    """Ring the full args; returns the id the tool_call event carries."""
    global _args_ring_seq
    with _args_ring_lock:
        _args_ring_seq += 1
        i = _args_ring_seq
        _args_ring[i] = {"id": i, "name": name, "args": full_args}
        while len(_args_ring) > _ARGS_RING_CAP:
            _args_ring.popitem(last=False)
    return i


def get_tool_args(i: int) -> "dict | None":
    with _args_ring_lock:
        return _args_ring.get(int(i))

_DELTA_INTERVAL_S = 0.2   # max coalescing latency added to a streamed delta
_DELTA_CHARS = 160        # flush a buffer early once it grows past this


class Emitter:
    """Thread-safe bridge: the agent's sync worker-thread callbacks → WebSocket.

    The agent fires ``stream_delta_callback`` / ``tool_call_*`` synchronously
    from a worker thread (see ``gateway.stream_consumer``). We push JSON events
    onto a stdlib ``queue.Queue``; the asyncio WS handler drains it. This keeps
    the hot path lock-free (``queue.Queue`` is thread-safe) and the event loop
    responsive.

    Deltas are COALESCED: one WS frame per model chunk floods the client (and
    used to re-render the desktop transcript per chunk — the long-turn freeze),
    so consecutive same-(type,by) chunks buffer and flush as ONE frame at most
    every ``_DELTA_INTERVAL_S`` / at ``_DELTA_CHARS``. Every non-delta event
    flushes first, so ordering against tool calls holds; a small flusher thread
    bounds latency through quiet stretches (no event to trigger the next flush).
    """

    def __init__(self, q: "queue.Queue", turn_id: str, conv_id: str, session_key: str):
        self._q = q
        self._turn_id = turn_id
        self._conv_id = conv_id
        self._session_key = session_key
        self._lock = threading.Lock()
        # (event_type, by) → buffered chunks; guarded by _lock
        self._pending: dict = {}
        self._last_flush = 0.0
        self._flusher = None

    def on_delta(self, text, **kw) -> None:
        # ``text is None`` is the agent's segment-boundary sentinel (fired before
        # tool dispatch, agent.py) — flush what's buffered so the phase change
        # reads promptly, then drop (the following ``tool_call`` carries it).
        if text is None:
            with self._lock:
                self._flush_locked()
            return
        kind = kw.get("kind", "content")
        by = kw.get("by")
        etype = "thought_delta" if kind == "reasoning" else "text_delta"
        with self._lock:
            chunks = self._pending.setdefault((etype, by), [])
            chunks.append(text)
            buffered = sum(len(c) for c in chunks)
            if (buffered >= _DELTA_CHARS
                    or time.monotonic() - self._last_flush >= _DELTA_INTERVAL_S):
                self._flush_locked()
            elif self._flusher is None or not self._flusher.is_alive():
                self._flusher = threading.Thread(
                    target=self._flush_loop, daemon=True,
                    name=f"ws-delta-flush-{self._turn_id[:8]}")
                self._flusher.start()

    def _flush_loop(self) -> None:
        while True:
            time.sleep(0.05)
            with self._lock:
                if not self._pending:
                    return
                if time.monotonic() - self._last_flush >= _DELTA_INTERVAL_S:
                    self._flush_locked()

    def _flush_locked(self) -> None:
        """Emit + clear all buffered deltas. Caller holds ``_lock``."""
        for (etype, by), chunks in self._pending.items():
            text = "".join(chunks)
            if not text:
                continue
            event = {"type": etype, "conv_id": self._conv_id, "text": text}
            if by:
                event["by"] = by
            event["turn_id"] = self._turn_id
            self._q.put(event)
        self._pending.clear()
        self._last_flush = time.monotonic()

    def _emit(self, event: dict) -> None:
        with self._lock:
            # Non-delta event: flush buffered deltas first so the order the
            # client reconstructs stays true.
            self._flush_locked()
            event.setdefault("turn_id", self._turn_id)
            self._q.put(event)

    def on_tool_start(self, name: str, args: str, by: str = None) -> None:
        # `args` arrives FULL from the agent — slice for the wire, ring the
        # full copy for GET /toolargs (desktop fetches on expand).
        i = put_tool_args(name, args)
        event = {"type": "tool_call", "conv_id": self._conv_id,
                 "name": name, "args": args[:_WS_ARGS_LIMIT], "id": i}
        if by:
            event["by"] = by
        self._emit(event)

    def on_tool_result(self, name: str, result: str, elapsed: float,
                       by: str = None) -> None:
        # Carries the tool's actual output (truncated), not the input summary.
        # The desktop pairs this with the matching `running` tool_call by name
        # to flip it to done and render the result card.
        text = result if isinstance(result, str) else str(result)
        if len(text) > _WS_RESULT_LIMIT:
            text = text[:_WS_RESULT_LIMIT]
            truncated = True
        else:
            truncated = False
        event = {"type": "tool_result", "conv_id": self._conv_id,
                 "name": name, "result": text,
                 "elapsed": round(float(elapsed), 3), "truncated": truncated}
        if by:
            event["by"] = by
        self._emit(event)

    def on_approval_request(self, info: dict) -> None:
        event = {"type": "approval_request", "conv_id": self._conv_id,
                 "id": info.get("id"), "name": info.get("tool"),
                 "summary": info.get("summary")}
        # Raw args (pre-truncated by the caller) — desktop approval cards
        # render a change preview from them; absent for older cores.
        if info.get("args"):
            event["args"] = info["args"]
        self._emit(event)

    def on_approval_result(self, info: dict, approved: bool, reason: str) -> None:
        # The desktop optimistically settles the card on its own button click;
        # this event covers every other resolution path (timeout / interrupt /
        # the other client's reply).
        self._emit({"type": "approval_resolved", "conv_id": self._conv_id,
                    "id": info.get("id"), "approved": bool(approved),
                    "reason": reason})

    def finish(self) -> None:
        with self._lock:
            self._flush_locked()
        self._q.put(_DONE)
