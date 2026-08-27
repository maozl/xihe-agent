#!/usr/bin/env python3
"""xihe serve — run xihe as an HTTP + WebSocket service.

Third run mode alongside ``xihe chat`` (interactive CLI) and ``xihe gateway``
(messaging platforms). ``serve`` exposes the *same* agent core over a local
network API so other frontends — the desktop app, a script, a web UI — can drive
xihe without embedding an engine. The desktop connects to ``/stream`` over
WebSocket the way any client connects to a backend.

Architecture mirrors the gateway: one ``SharedContext`` (SQLite / auxiliary LLM
/ compressor, wired once) + a fresh ``XiheAgent`` per turn. Deltas are bridged
from the agent's worker thread to the WebSocket via a stdlib ``queue.Queue`` —
the same pattern as ``gateway.stream_consumer.StreamConsumer``.

REST (stateless):
    GET  /health                          liveness + capability descriptor
    GET  /readiness                       structured what's-missing report (onboarding UIs)
    POST /test-connection                 server-side model-connection probe (key stays server-side)
    GET  /agents                          the xihe "self" agent (P0; personas later)
    GET  /sessions                        serve-platform sessions (most-recent first)
    GET  /convs/{conv_id}/messages        transcript for one conversation
    POST /convs/{conv_id}/reset           start a fresh session round for a conversation
    POST /convs/{conv_id}/truncate        roll back: drop a user row + everything after (resend)
    POST /convs/{conv_id}/title           rename a conversation
    DELETE /convs/{conv_id}               delete a conversation + its transcript
    GET  /specialists                     specialist-agent files (editor view)
    PUT  /specialists/{slug}              write one specialist file
    DELETE /specialists/{slug}            delete one specialist file
    GET  /store                           capability-store catalog + install state
    POST /store/install                   install a skill/mcp catalog item
    POST /store/uninstall                 remove a store-installed item
    POST /store/mount                     set which agents an item is mounted to
    POST /store/refresh                   force re-fetch of catalog sources
    GET  /browser/status                  agent CDP Chrome state (panel poll)
    POST /browser/launch                  cold-start the CDP Chrome
    POST /browser/snap                    move Chrome over the desktop panel region
    POST /browser/hide                    hide Chrome while the desktop is unfocused
    POST /browser/show                    re-show + re-place the snapped Chrome
    POST /browser/release                 un-snap: float Chrome beside the desktop
    POST /browser/appearance              desktop pushes light/dark (persists, applies at next launch)
    POST /browser/restart                 kill + relaunch the CDP Chrome (re-apply appearance)

WebSocket  /stream  (streaming turn):
    client → server   {"type":"send","conv_id":...,"text":...}
                      {"type":"attach","conv_id":...}
                      {"type":"interrupt","conv_id":...}
                      {"type":"steer","conv_id":...,"text":...}
    server → client   hello · attached · turn_start · text_delta ·
                      thought_delta · tool_call · tool_result · complete · error

Session mapping: a desktop conversation id (``conv_id``) maps to
``SessionSource(platform="serve", chat_id=conv_id, user_id="desktop",
chat_type="dm")`` → deterministic key ``agent:main:serve:dm:{conv_id}`` → a
persistent xihe session (history lives in ``sessions.db``; survives serve
restarts and shares nothing with CLI/gateway sessions unless you reuse a key).
"""

import asyncio
import json
import logging
import queue
import threading
import time
import uuid
from pathlib import Path

from aiohttp import web

# AccessLogger moved from aiohttp.helpers to aiohttp.web_log in 3.10; the
# helpers re-export was removed in 3.14. Try the new home first, fall back so
# this still imports on older aiohttp (internal mirror / other machines).
try:
    from aiohttp.web_log import AccessLogger
except ImportError:  # aiohttp < 3.10 kept it in helpers
    from aiohttp.helpers import AccessLogger

from core.config import AGENT_HOME

logger = logging.getLogger(__name__)

# Platform id for serve sessions; grep-visible in sessions.db/logs. Coupled to
# the desktop's session filters — rename in both places.
_PLATFORM = "serve"
_DEFAULT_USER = "desktop"

# Sentinel pushed onto the emitter queue when the worker thread finishes.
_DONE = object()

# Cap on a tool_result payload sent over the WS. Aligned with the
# tool_result_storage spill threshold: at or below it the full content IS in
# sessions.db, so nothing is lost by sending it whole; above it the history
# only ever held the preview + side-store path, which /toolresult serves.
_WS_RESULT_LIMIT = 15_000

# Cap on a persisted-reasoning payload sent over the WS (historical 思考 card).
# Reasoning can run long; the desktop collapses it past a preview, and the full
# text stays in sessions.db. Generous so normal thinking survives intact.
_WS_REASONING_LIMIT = 8000


def _capabilities(toolsets) -> list[str]:
    """High-level capability descriptor advertised to the desktop.

    The desktop UI branches on these flags (capability-driven), never on the
    engine name. Scoped to the MAIN agent's resolved roster (``toolsets`` from
    config.yaml top-level keys, None = unrestricted) and filtered through the
    same ``check_fn`` gates ``XiheAgent`` applies — a slim main roster must not
    advertise browser/mcp flags the main agent can't actually call.
    """
    caps = ["text", "streaming", "tools", "interrupt", "sessions", "thoughts",
            "approvals"]
    try:
        from tools import registry
        schemas = registry.get_schemas(
            toolsets=set(toolsets) if toolsets is not None else None)
        names = {
            (s.get("function") or {}).get("name") or s.get("name") or ""
            for s in schemas
        }
        if any(n.startswith("browser") for n in names):
            caps.append("browser")
        if "vision_analyze" in names or "image_ocr" in names:
            caps.append("vision")
        if "image_generation" in names:
            caps.append("image_generation")
        if any(n.startswith("mcp_") for n in names):
            caps.append("mcp")
    except Exception:
        # capability flags silently degrade the desktop UI — leave a trace
        logger.debug("capability probe failed", exc_info=True)
    return caps


def _reshape_history(rows: list) -> list:
    """Fold a raw OpenAI message stream into per-bubble chat history.

    The model-facing stream interleaves assistant tool-call frames and tool
    result frames (role='tool') between the user's message and the assistant's
    final text. The desktop shows ONE assistant bubble per turn (the final
    text) with tool calls collapsed under it. This fold:

      * emits one {role:'user'} bubble per real user message;
      * aggregates each turn's assistant text into one {role:'assistant'}
        bubble, counting tool calls and remembering the first assistant row id
        as a stable anchor for lazy trace fetch (set for every assistant turn,
        not just tool-bearing ones, so reasoning-only turns are fetchable);
      * drops role='system' and role='tool' rows (the latter are tool results,
        folded into the bubble's tool count, not shown as their own bubbles).

    Each item: {role, content, id, tools, has_reasoning, usage} where ``id`` is
    a serve row id (assistant: the turn's anchor row; user: its own row — the
    desktop's resend addresses truncation by it), ``tools`` is the number of tool
    calls in that turn, ``has_reasoning`` flags whether any assistant row
    carried persisted reasoning (so the desktop shows a 思考 badge before
    lazy-load), and ``usage`` carries the turn's token usage (set on the turn's
    final assistant row; last-wins during the fold) for cost badges.
    """
    out: list = []
    cur = None  # in-flight assistant turn: {content, tools, anchor, has_reasoning, usage}

    def flush():
        nonlocal cur
        if cur is not None:
            item = {
                "role": "assistant",
                "content": cur["content"],
                "id": cur["anchor"],
                "tools": cur["tools"],
                "has_reasoning": cur["has_reasoning"],
            }
            if cur.get("usage"):
                item["usage"] = cur["usage"]
            out.append(item)
            cur = None

    for m in rows:
        role = m.get("role")
        if role == "system":
            continue
        if role == "tool":
            # Tool result — belongs to the current assistant turn; folded in,
            # never emitted as its own bubble.
            if cur is None:
                cur = {"content": "", "tools": 0, "anchor": None, "has_reasoning": False}
            continue
        if role == "user":
            # Agent-internal nudges (empty-response recovery) ride the history
            # as user rows the model must see, but must not render as chat
            # bubbles — after a reload they'd read as the user's own words
            # closing the turn.
            if (m.get("content") or "").startswith("[系统提示]"):
                continue
            # A real user message starts a new turn: close any in-flight
            # assistant bubble, then emit the user bubble.
            flush()
            out.append({"role": "user", "content": m.get("content") or "",
                        "id": m.get("id"), "tools": 0})
            continue
        if role == "assistant":
            tool_calls = m.get("tool_calls") or []
            content = m.get("content")
            if not tool_calls and not content:
                continue  # bare keepalive / protocol noise
            if cur is None:
                # Anchor on the first assistant row of the turn so even
                # reasoning-only turns (no tools) can lazy-load their trace.
                cur = {"content": "", "tools": 0, "anchor": m.get("id"),
                       "has_reasoning": False}
            if tool_calls:
                cur["tools"] += len(tool_calls)
            if m.get("reasoning"):
                cur["has_reasoning"] = True
            if isinstance(content, str) and content:
                cur["content"] += content
            if m.get("usage"):
                cur["usage"] = m["usage"]
            continue
        # Unknown role: flush + pass through minimally.
        flush()
        out.append({"role": role, "content": m.get("content") or "",
                    "id": None, "tools": 0})
    flush()
    return out


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
        event = {"type": "tool_call", "conv_id": self._conv_id,
                 "name": name, "args": args}
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
        self._emit({"type": "approval_request", "conv_id": self._conv_id,
                    "id": info.get("id"), "name": info.get("tool"),
                    "summary": info.get("summary")})

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


def _ws_approval_key(cwd) -> str:
    """工作空间会话的审批记忆桶。规范化（正斜杠 + 小写——Windows 盘符大小写
    不敏感）保证同一工作空间永远落同一个桶。"""
    norm = str(cwd).replace("\\", "/").rstrip("/")
    return f"ws:{norm.lower()}"


class ServeApp:
    def __init__(self, shared_ctx, version: str):
        self.ctx = shared_ctx
        self.config = shared_ctx.config
        self.version = version
        # conv_id → active XiheAgent, for /interrupt (guarded by _active_lock)
        self._active: dict[str, object] = {}
        self._active_lock = threading.Lock()
        # conv_id → asyncio.Lock: serializes overlapping turns on one
        # conversation (prevents history corruption / interleaved replies).
        # All access is from the single event-loop thread, so dict mutation
        # needs no extra lock; the asyncio.Lock guards the await span.
        self._turn_locks: dict[str, asyncio.Lock] = {}
        # conv_id → WebSocketResponse an in-flight turn streams to (None after
        # the socket died — turn keeps running detached). A reconnected client
        # re-attaches via the `attach` frame. Event-loop thread only, no lock.
        self._conv_sockets: dict[str, object] = {}
        # Capabilities cached per registry version — /health is polled every
        # few seconds, and background MCP discovery registers tools after the
        # port opens, so a compute-once cache would permanently miss "mcp".
        self._caps: list[str] | None = None
        self._caps_ver = -1

    def capabilities(self) -> list[str]:
        from tools import registry
        if self._caps is None or self._caps_ver != registry.version():
            self._caps = _capabilities(self.ctx.main_toolsets)
            self._caps_ver = registry.version()
        return self._caps

    def _source(self, conv_id: str):
        from core.session import SessionSource
        return SessionSource(
            platform=_PLATFORM, chat_id=str(conv_id),
            user_id=_DEFAULT_USER, chat_type="dm",
        )

    def _turn_lock(self, conv_id: str) -> asyncio.Lock:
        lock = self._turn_locks.get(conv_id)
        if lock is None:
            lock = asyncio.Lock()
            self._turn_locks[conv_id] = lock
        return lock

    async def health(self, request):
        return web.json_response({
            "ok": True,
            "version": self.version,
            "mode": "serve",
            "model": self.config.get("model"),
            "capabilities": self.capabilities(),
        })

    async def readiness(self, request):
        """Structured readiness for onboarding UIs — what's missing and the
        fix, instead of the client waiting for a first send to fail.

        /health stays a lean liveness probe ({ok, version}); this endpoint
        costs a registry scan and answers "why not".
        """
        from core.diagnostics import readiness_report
        mode = request.query.get("mode", "chat")
        if mode not in ("chat", "gateway"):
            mode = "chat"
        report = readiness_report(self.config, mode=mode)
        report["version"] = self.version
        return web.json_response(report)

    async def test_connection(self, request):
        """Server-side model-connection test (api_key never crosses the API).

        The desktop's「测试连接」button hits this instead of fetching from
        the renderer, which has no key.
        """
        from core.diagnostics import check_connectivity
        result = await asyncio.to_thread(
            check_connectivity, self.config.get("base_url"),
            self.config.get("api_key"))
        result["model_configured"] = self.config.get("model")
        return web.json_response(result)

    async def agents(self, request):
        return web.json_response({"agents": [{
            "id": "self",
            "name": self.config.get("agent_name", "xihe"),
            "engine": "xihe",
            "shape": "process",
            "model": self.config.get("model"),
            "status": "online",
            "capabilities": self.capabilities(),
            "dataRoot": str(AGENT_HOME),
            "description": "Local xihe instance (this serve process).",
        }]})

    async def list_sessions(self, request):
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(
            None, lambda: self.ctx.db.list_sessions(limit=100, platform=_PLATFORM))
        return web.json_response({"sessions": [
            {
                "conv_id": r.get("chat_id"),
                "session_key": r.get("session_key"),
                "title": r.get("title"),
                "updated_at": r.get("updated_at"),
                "msg_count": r.get("msg_count", 0),
            }
            for r in rows
        ]})

    async def get_messages(self, request):
        conv_id = request.match_info["conv_id"]

        def work():
            source = self._source(conv_id)
            session_key = self.ctx.db.build_key(source)
            entry = self.ctx.db.get_entry(session_key)
            rows = self.ctx.db.load_messages_with_id(entry.session_id) if entry else []
            return {"conv_id": conv_id, "messages": _reshape_history(rows)}

        # SQLite reads + the fold run off-loop: a long transcript must not stall
        # WS streaming (all turns share one event loop).
        loop = asyncio.get_running_loop()
        return web.json_response(await loop.run_in_executor(None, work))

    async def get_trace(self, request):
        """Lazy-fetch one turn's tool-call trace by its anchor row id.

        The desktop renders assistant bubbles collapsed; expanding one calls
        this with that turn's anchor id (the first tool-bearing assistant row,
        attached to the bubble by ``_reshape_history``) to pull the tool calls
        without paying for them on every history load.
        """
        conv_id = request.match_info["conv_id"]
        try:
            anchor_id = int(request.match_info["msg_id"])
        except (KeyError, ValueError, TypeError):
            return web.json_response({"trace": []})

        def work():
            source = self._source(conv_id)
            session_key = self.ctx.db.build_key(source)
            entry = self.ctx.db.get_entry(session_key)
            if not entry:
                return {"conv_id": conv_id, "trace": []}
            rows = self.ctx.db.load_messages_with_id(entry.session_id)
            start = None
            for i, m in enumerate(rows):
                if m.get("id") == anchor_id:
                    start = i
                    break
            trace: list = []
            if start is not None:
                # Walk the turn (anchor → next user message) in row order, emitting
                # reasoning (thought) and tool-call items interleaved as they
                # occurred during the turn. Tool results (role='tool') are paired
                # back to their call by tool_call_id in a second pass. A turn may
                # span several assistant/tool round-trips.
                results_by_id: dict[str, str] = {}
                ordered: list = []  # ("thought", text) | ("tool", tcid, name, args)
                for m in rows[start:]:
                    role = m.get("role")
                    if role == "user":
                        break
                    if role == "tool":
                        tcid = m.get("tool_call_id")
                        if tcid:
                            results_by_id[tcid] = m.get("content") or ""
                    elif role == "assistant":
                        reasoning = m.get("reasoning")
                        if reasoning:
                            text = reasoning
                            if len(text) > _WS_REASONING_LIMIT:
                                text = text[:_WS_REASONING_LIMIT]
                            ordered.append(("thought", text))
                        for tc in (m.get("tool_calls") or []):
                            fn = tc.get("function") or {}
                            ordered.append(("tool", tc.get("id"), fn.get("name", "tool"),
                                            fn.get("arguments", "") or ""))
                for item in ordered:
                    if item[0] == "thought":
                        trace.append({"kind": "thought", "text": item[1]})
                        continue
                    _, tcid, name, args = item
                    te = {"kind": "tool", "name": name, "args": args, "status": "done"}
                    result = results_by_id.get(tcid) if tcid else None
                    if isinstance(result, str):
                        text = result
                        truncated = len(text) > _WS_RESULT_LIMIT
                        if truncated:
                            text = text[:_WS_RESULT_LIMIT]
                        te["result"] = text
                        te["truncated"] = truncated
                    trace.append(te)
            return {"conv_id": conv_id, "trace": trace}

        loop = asyncio.get_running_loop()
        return web.json_response(await loop.run_in_executor(None, work))

    async def toolresult_full(self, request):
        """Full content of a spilled tool result. Only paths inside the
        tool_result_storage side-store dir are honored — the path arrives
        from a client, so an escape attempt is rejected, not read."""
        from tools.tool_result_storage import _STORAGE_DIR
        raw = request.query.get("path", "")

        def work():
            p = Path(raw).resolve()
            root = Path(_STORAGE_DIR).resolve()
            if root not in p.parents:
                return None
            return p.read_text(encoding="utf-8", errors="replace")

        loop = asyncio.get_running_loop()
        try:
            content = await loop.run_in_executor(None, work)
        except OSError:
            return web.json_response({"ok": False, "reason": "read-failed"})
        if content is None:
            return web.json_response({"ok": False, "reason": "path-rejected"})
        return web.json_response({"ok": True, "content": content, "chars": len(content)})

    async def reset_session(self, request):
        conv_id = request.match_info["conv_id"]
        source = self._source(conv_id)
        session_key = self.ctx.db.build_key(source)
        new_id = self.ctx.db.reset_session(session_key)
        return web.json_response({
            "conv_id": conv_id, "session_key": session_key, "reset": bool(new_id),
        })

    async def delete_session(self, request):
        conv_id = request.match_info["conv_id"]
        source = self._source(conv_id)
        session_key = self.ctx.db.build_key(source)
        deleted = self.ctx.db.delete_session(session_key)
        return web.json_response({
            "conv_id": conv_id, "session_key": session_key, "deleted": bool(deleted),
        })

    async def truncate_conv(self, request):
        """Roll a conversation back: delete the given user row and everything
        after it (desktop 重新发送). Refused while a turn is running on that
        conversation — truncating under a live turn corrupts its history
        rewrite."""
        conv_id = request.match_info["conv_id"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        from_id = int((body or {}).get("from_msg_id") or 0)
        if not from_id:
            return web.json_response({"ok": False, "reason": "from_msg_id required"},
                                     status=400)
        if self._turn_lock(conv_id).locked():
            return web.json_response({"ok": False, "reason": "turn in progress"},
                                     status=409)
        source = self._source(conv_id)
        session_key = self.ctx.db.build_key(source)
        entry = self.ctx.db.get_entry(session_key)
        if not entry:
            return web.json_response({"ok": False, "reason": "unknown conversation"},
                                     status=404)
        deleted = self.ctx.db.truncate_messages_from(entry.session_id, from_id)
        return web.json_response({"ok": True, "deleted": deleted})

    async def set_title(self, request):
        """Rename a conversation's session title.

        A serve session is created lazily on first send, so renaming one that
        has no session yet (entry is None) returns ok:false so the client keeps
        its local placeholder instead of pretending success.
        """
        conv_id = request.match_info["conv_id"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        title = str((body or {}).get("title") or "").strip()
        if not title:
            return web.json_response({"ok": False, "reason": "empty title"}, status=400)
        title = title[:120]
        source = self._source(conv_id)
        session_key = self.ctx.db.build_key(source)
        entry = self.ctx.db.get_entry(session_key)
        if not entry:
            return web.json_response({"ok": False, "reason": "no session"}, status=404)
        self.ctx.db.set_session_title(entry.session_id, title)
        return web.json_response({"conv_id": conv_id, "title": title, "ok": True})

    # Each is a thin wrapper over an already-running subsystem's in-process
    # enumerator (MCP connections, skill scan, cron scheduler). No XiheAgent is
    # created — these are pure reads. `api_key` is never touched.
    async def mcp(self, request):
        from tools.mcp_tool import get_mcp_status
        return web.json_response({"servers": get_mcp_status()})

    async def skills(self, request):
        from tools.skills_tool import _scan_skills, _USER_SKILLS_DIR
        user_prefix = str(_USER_SKILLS_DIR)
        out = [
            {
                "name": s["name"],
                "description": s["description"],
                "category": s["category"],
                # Derive bundled-vs-user from the on-disk path (user dir shadows
                # bundled by name in _scan_skills) so the panel can mark which
                # are editable. The raw path itself is internal — not returned.
                "source": "user" if s["path"].startswith(user_prefix) else "bundled",
            }
            for s in _scan_skills()
        ]
        return web.json_response({"skills": out})

    async def cron(self, request):
        import json as _json
        from tools.cronjob_tools import _list_jobs, scheduler_health
        # _list_jobs returns a tool_result(...) JSON *string*; parse out the
        # curated job summary. include_disabled so the panel shows paused jobs
        # too (greyed via the `enabled` flag) rather than hiding them.
        try:
            jobs = _json.loads(_list_jobs({"include_disabled": True})).get("jobs", [])
        except Exception:
            jobs = []
        return web.json_response({"jobs": jobs, "scheduler": scheduler_health()})

    async def get_specialists(self, request):
        """Raw agents/*.yaml specs + toolset catalog for the desktop editor.

        Specs are returned verbatim (not validated AgentDefs) minus api_key —
        only the api_key_set flag escapes the server — so the editor can show
        and fix entries validation would drop. ``registered`` reads the live
        registry, so it lags file edits until a serve restart — exactly the
        editor's 待重启 badge.
        """
        from core.agent_defs import list_raw_specs
        from core.toolsets import TOOLSETS, resolve_toolset
        from tools import registry
        from tools.mcp_tool import get_mcp_status
        from tools.specialist_agent_tool import specialists_enabled

        specialists = []
        for slug, spec in list_raw_specs():
            safe = {k: v for k, v in spec.items() if k != "api_key"}
            specialists.append({
                "slug": slug,
                "spec": safe,
                "api_key_set": bool(spec.get("api_key")),
            })
        registered = sorted(
            n for n in registry.snapshot_names()
            if n.startswith("run_") and n.endswith("_agent"))
        return web.json_response({
            "specialists": specialists,
            "specialists_enabled": specialists_enabled(self.config),
            "toolsets": [
                {
                    "name": name,
                    "label": ts.get("label") or name,
                    "description": ts.get("description", ""),
                    "tools": len(resolve_toolset(name)),
                }
                for name, ts in sorted(TOOLSETS.items())
            ],
            "mcp_servers": [
                {"name": s.get("name"), "tools": s.get("tools", 0),
                 "connected": bool(s.get("connected"))}
                for s in get_mcp_status()
            ],
            "registered": registered,
        })

    async def put_specialist(self, request):
        """Write one specialist file (create or replace) agents/<slug>.yaml.

        api_key: absent from the body = keep the file's existing key; empty
        string = clear it. Validation warnings are returned but the file
        still saves (invalid entries are skipped at next startup) so drafts
        aren't lost. Dispatch tools only change after a serve restart.
        """
        from core.agent_defs import (
            _SLUG_RE, _load_raw, load_agent_defs, save_raw,
        )
        slug = request.match_info["slug"]
        # The slug becomes a file name — the regex doubles as the
        # path-traversal guard (no dots, slashes, or case tricks).
        if not _SLUG_RE.match(slug):
            return web.json_response({"ok": False, "error": "bad slug"}, status=400)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        spec = body.get("spec") if isinstance(body, dict) else None
        if not isinstance(spec, dict):
            return web.json_response(
                {"ok": False, "error": "'spec' must be a mapping"}, status=400)

        if "api_key" in spec and not spec["api_key"]:
            spec.pop("api_key")  # explicit empty = clear
        elif "api_key" not in spec:
            try:
                existing = _load_raw(slug)
            except Exception:
                existing = {}
            if isinstance(existing, dict) and existing.get("api_key"):
                spec["api_key"] = existing["api_key"]

        try:
            save_raw(slug, spec)
        except Exception as e:
            logger.exception("specialist write failed")
            return web.json_response(
                {"ok": False, "error": f"write failed: {e}"}, status=500)
        warnings: list = []
        load_agent_defs(warnings)
        return web.json_response({"ok": True, "slug": slug, "warnings": warnings})

    async def delete_specialist(self, request):
        """Delete one specialist file (dispatch tool unregisters on restart)."""
        from core.agent_defs import _SLUG_RE, delete_raw
        slug = request.match_info["slug"]
        if not _SLUG_RE.match(slug):
            return web.json_response({"ok": False, "error": "bad slug"}, status=400)
        return web.json_response({
            "ok": True, "slug": slug, "deleted": delete_raw(slug),
        })

    # ---- capability store -----------------------------------------------------

    async def store(self, request):
        """Catalog + install state for the desktop store page. Secrets never
        leave the ledger — responses carry only which config keys are filled."""
        from core import store as store_mod
        loop = asyncio.get_running_loop()
        catalog = await loop.run_in_executor(None, store_mod.fetch_catalog)
        return web.json_response(store_mod.catalog_view(catalog))

    async def _store_body(self, request) -> tuple | None:
        try:
            body = await request.json()
        except Exception:
            return None
        if not isinstance(body, dict):
            return None
        return body

    async def store_install(self, request):
        from core import store as store_mod
        body = await self._store_body(request)
        kind = str((body or {}).get("type") or "").strip()
        item_id = str((body or {}).get("id") or "").strip()
        if kind not in ("skill", "mcp") or not item_id:
            return web.json_response(
                {"ok": False, "error": "body needs 'type' (skill|mcp) and 'id'"},
                status=400)
        loop = asyncio.get_running_loop()
        item = await loop.run_in_executor(None, store_mod.find_item, kind, item_id)
        if item is None:
            return web.json_response(
                {"ok": False, "error": f"{kind} '{item_id}' not found in any store source"},
                status=404)
        try:
            if kind == "skill":
                result = await loop.run_in_executor(None, store_mod.install_skill, item)
            else:
                result = await loop.run_in_executor(
                    None, store_mod.install_mcp, item, body.get("config") or {})
        except Exception as e:
            logger.exception("store install failed")
            return web.json_response(
                {"ok": False, "error": f"install failed: {e}"}, status=500)
        if not result.get("success"):
            return web.json_response(
                {"ok": False, "error": result.get("error", "install failed")}, status=400)
        return web.json_response({"ok": True, **result})

    async def store_uninstall(self, request):
        from core import store as store_mod
        body = await self._store_body(request)
        kind = str((body or {}).get("type") or "").strip()
        item_id = str((body or {}).get("id") or "").strip()
        if kind not in ("skill", "mcp") or not item_id:
            return web.json_response(
                {"ok": False, "error": "body needs 'type' (skill|mcp) and 'id'"},
                status=400)
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, store_mod.uninstall, kind, item_id)
        except Exception as e:
            logger.exception("store uninstall failed")
            return web.json_response(
                {"ok": False, "error": f"uninstall failed: {e}"}, status=500)
        if not result.get("success"):
            return web.json_response(
                {"ok": False, "error": result.get("error", "uninstall failed")}, status=400)
        return web.json_response({"ok": True, **result})

    async def store_mount(self, request):
        from core import store as store_mod
        body = await self._store_body(request)
        kind = str((body or {}).get("type") or "").strip()
        item_id = str((body or {}).get("id") or "").strip()
        targets = (body or {}).get("targets")
        if kind not in ("skill", "mcp") or not item_id or not isinstance(targets, list):
            return web.json_response(
                {"ok": False, "error": "body needs 'type', 'id' and 'targets' (list)"},
                status=400)
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None, store_mod.set_mount, kind, item_id, targets)
        except Exception as e:
            logger.exception("store mount failed")
            return web.json_response(
                {"ok": False, "error": f"mount failed: {e}"}, status=500)
        if not result.get("success"):
            return web.json_response(
                {"ok": False, "error": result.get("error", "mount failed")}, status=400)
        # 'main' resolves its roster at process start; specialists re-read the
        # ledger on every dispatch — surface the difference to the UI.
        effect = {t: ("restart" if t == "main" else "hot")
                  for t in (result.get("mounted") or [])}
        return web.json_response({"ok": True, **result, "effect": effect})

    async def store_refresh(self, request):
        from core import store as store_mod
        loop = asyncio.get_running_loop()
        catalog = await loop.run_in_executor(
            None, lambda: store_mod.fetch_catalog(force=True))
        return web.json_response(store_mod.catalog_view(catalog))

    # ---- browser window snap (desktop browser panel) --------------------------

    async def browser_status(self, request):
        from gateway import browser_window
        return web.json_response(browser_window.status())

    async def browser_launch(self, request):
        """Blocking Popen + port poll — executor, like store's fetch_catalog.

        launch() itself never touches browser_window._state (the loop owns
        it); status() is called here on the loop thread afterwards."""
        from gateway import browser_window
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, browser_window.launch)
        out = dict(res)
        out.update(browser_window.status())
        return web.json_response(out)

    async def browser_appearance(self, request):
        """Desktop pushes its light/dark so CDP Chrome launches matching it.
        Persists under browser/ runtime state (not config); applies to the
        next launch — a running Chrome needs /browser/restart."""
        from tools.browser_tool import set_appearance
        body = await self._browser_body(request)
        dark = body.get("dark")
        if not isinstance(dark, bool):
            return web.json_response(
                {"ok": False, "reason": "bad body: dark bool required"}, status=400)
        return self._browser_safe(lambda: set_appearance(dark))

    async def browser_restart(self, request):
        """Blocking taskkill + relaunch — executor, same shape as launch()."""
        from gateway import browser_window
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, browser_window.restart)
        out = dict(res)
        out.update(browser_window.status())
        return web.json_response(out)

    def _browser_safe(self, result_fn):
        """ctypes calls can raise despite the API's never-raise contract; a
        raw 500 text body breaks the client's JSON parse, so log + answer
        with a JSON error instead."""
        try:
            return web.json_response(result_fn())
        except Exception:
            logger.exception("browser handler crashed")
            return web.json_response({"ok": False, "reason": "internal-error"}, status=500)

    async def _browser_body(self, request) -> dict | None:
        try:
            body = await request.json()
        except Exception:
            body = {}
        return body if isinstance(body, dict) else {}

    async def browser_snap(self, request):
        from gateway import browser_window
        body = await self._browser_body(request)
        vals = (body.get("x"), body.get("y"), body.get("w"), body.get("h"),
                body.get("desktop_hwnd"))
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in vals):
            return web.json_response(
                {"ok": False, "reason": "bad body: x,y,w,h,desktop_hwnd ints required"},
                status=400)
        return self._browser_safe(lambda: browser_window.snap(*vals))

    async def browser_hide(self, request):
        from gateway import browser_window
        return self._browser_safe(browser_window.hide)

    async def browser_show(self, request):
        from gateway import browser_window
        body = await self._browser_body(request)
        return self._browser_safe(lambda: browser_window.show(body.get("desktop_hwnd")))

    async def browser_release(self, request):
        from gateway import browser_window
        return self._browser_safe(browser_window.release)

    # A detached turn (its socket died) gets this long for the client to
    # reconnect + `attach` before it's interrupted; the desktop retries every
    # 3s, so a healthy reconnect lands well inside the window. heartbeat=60
    # also widens aiohttp's pong deadline to 30s — a busy machine can stall
    # the browser process longer than the old 15s window without the socket
    # being torn down at all.
    _GRACE_SECONDS = 60

    async def stream(self, request):
        ws = web.WebSocketResponse(heartbeat=self._GRACE_SECONDS)
        await ws.prepare(request)
        await self._safe_send(ws, {
            "type": "hello", "version": self.version, "mode": "serve",
            "model": self.config.get("model"),
            "capabilities": self.capabilities(),
        })
        started_convs = set()
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        cmd = json.loads(msg.data)
                    except json.JSONDecodeError:
                        await self._safe_send(ws, {"type": "error", "code": "invalid_json",
                                                   "message": "invalid json"})
                        continue
                    ctype = cmd.get("type")
                    if ctype == "send":
                        conv_id = str(cmd.get("conv_id") or "")
                        started_convs.add(conv_id)
                        self._conv_sockets[conv_id] = ws
                        # Dispatch as a background task so this read loop keeps
                        # pulling frames. Awaiting _handle_send inline blocks the
                        # loop for the whole turn — an `interrupt` arriving
                        # mid-turn would sit unread in the WS buffer until the
                        # turn finishes (== Stop did nothing). Per-conv ordering
                        # is still enforced by _turn_lock inside _handle_send.
                        t = asyncio.create_task(self._handle_send(ws, cmd))
                    elif ctype == "attach":
                        if await self._handle_attach(ws, cmd):
                            started_convs.add(str(cmd.get("conv_id") or ""))
                    elif ctype == "interrupt":
                        self._interrupt(str(cmd.get("conv_id") or ""))
                    elif ctype == "steer":
                        self._steer(str(cmd.get("conv_id") or ""),
                                    str(cmd.get("text") or ""))
                    elif ctype == "approve":
                        self._approve(str(cmd.get("conv_id") or ""),
                                      cmd.get("id"),
                                      bool(cmd.get("approved")),
                                      bool(cmd.get("always")))
                elif msg.type == web.WSMsgType.ERROR:
                    logger.warning("serve ws error: %s", ws.exception())
        finally:
            # Client gone: DON'T kill its turns outright. A pong-timeout /
            # transient dropout is far more common than a deliberate close,
            # and the desktop reconnects within seconds — the turn keeps
            # running (results persist to the session) and the drain loop
            # resumes streaming if the client re-attaches. Only interrupt
            # turns nobody re-attached to within the grace window.
            for conv_id in started_convs:
                cur = self._conv_sockets.get(conv_id)
                if cur is ws or cur is None:
                    self._conv_sockets[conv_id] = None
                self._schedule_grace(conv_id)
        return ws

    async def _handle_attach(self, ws, cmd) -> bool:
        """Reconnected client adopts a detached turn's event stream (see
        _GRACE_SECONDS). Returns True when the socket was registered as the
        conv's stream target (caller tracks it for disconnect cleanup).

        Acks with ``attached{running}`` either way: ``running=false`` tells
        the client any local pending bubble is for a turn that already
        settled — no complete is coming, so it should refetch instead of
        waiting on a dead bubble."""
        conv_id = str(cmd.get("conv_id") or "")
        with self._active_lock:
            running = conv_id in self._active
        adopted = bool(conv_id and running)
        if adopted:
            self._conv_sockets[conv_id] = ws
        if conv_id:
            await self._safe_send(ws, {"type": "attached", "conv_id": conv_id,
                                       "running": adopted})
        return adopted

    async def _handle_send(self, ws, cmd):
        conv_id = str(cmd.get("conv_id") or uuid.uuid4().hex)
        text = cmd.get("text") or ""
        if not str(text).strip():
            await self._safe_send(ws, {"type": "error", "code": "empty_text",
                                       "conv_id": conv_id, "message": "empty text"})
            return

        source = self._source(conv_id)
        session_key = self.ctx.db.build_key(source)
        turn_id = uuid.uuid4().hex

        # Serialize overlapping turns on the same conversation.
        async with self._turn_lock(conv_id):
            # The turn may start long after `send` registered the socket
            # (queued behind a previous turn): re-resolve the target so a
            # client that dropped + re-attached in between still gets it.
            if not await self._safe_send(self._conv_sockets.get(conv_id) or ws, {
                "type": "turn_start", "turn_id": turn_id, "conv_id": conv_id,
                "session_key": session_key,
            }):
                return  # client gone before we started

            # cwd threaded from the desktop: a serve conversation bound to a
            # workspace sends its workdir here so the agent's relative paths (and
            # terminal subprocess cwd) resolve to the workspace, not the serve
            # process's cwd. Unbound conversations omit it → agent.cwd=None →
            # tools fall back to the process cwd (unchanged behaviour).
            cwd = cmd.get("cwd")
            if cwd and not Path(str(cwd)).is_dir():
                logger.warning("serve: ignoring non-existent cwd %r", cwd)
                cwd = None
            # 绑定工作空间的对话（带 cwd）把审批记忆换到工作空间桶：同空间
            # 所有对话共享"批准且不再询问"；未绑定的维持按对话。
            approval_key = _ws_approval_key(cwd) if cwd else None
            # Agent construction can raise (e.g. openai>=2 refuses
            # OpenAI(api_key="") at __init__) — that must reach the client as
            # an error event, not die inside the fire-and-forget turn task
            # and leave the desktop waiting on turn_start forever.
            try:
                agent = self.ctx.create_agent(
                    enabled_toolsets=self.ctx.main_toolsets,
                    skills_allowed=self.ctx.main_skills,
                    cwd=cwd)
            except Exception as e:
                logger.exception("serve: agent creation failed (conv=%s)", conv_id)
                msg = ("api_key 未配置：请在桌面「设置」页填写模型连接并保存，然后点击「重启 xihe」"
                       if not self.config.get("api_key")
                       else f"创建 agent 失败：{e}")
                await self._safe_send(ws, {"type": "error", "turn_id": turn_id,
                                           "conv_id": conv_id, "message": msg,
                                           "code": ("api_key_missing"
                                                    if not self.config.get("api_key")
                                                    else "agent_create_failed")})
                return
            with self._active_lock:
                self._active[conv_id] = agent

            q: queue.Queue = queue.Queue()
            emitter = Emitter(q, turn_id, conv_id, session_key)
            loop = asyncio.get_running_loop()

            def _worker():
                try:
                    final = agent.chat(
                        source=source,
                        user_message=str(text),
                        stream_delta_callback=emitter.on_delta,
                        tool_call_start_callback=emitter.on_tool_start,
                        tool_result_callback=emitter.on_tool_result,
                        approval_request_callback=emitter.on_approval_request,
                        approval_result_callback=emitter.on_approval_result,
                        approval_key=approval_key,
                    )
                    usage = dict(getattr(agent, "_turn_usage", {}))
                    return final, getattr(agent, "_last_exit_reason", None), usage, None
                except Exception as e:
                    logger.exception("serve turn failed (conv=%s)", conv_id)
                    return None, None, {}, str(e)
                finally:
                    emitter.finish()

            fut = loop.run_in_executor(None, _worker)
            try:
                # Drain emitter → WS while the worker runs. The target socket
                # is re-resolved per item: `attach` from a reconnected client
                # swaps it mid-turn, and a dead socket only DETACHES — events
                # are dropped but the turn keeps running (it persists to the
                # session) and dies at the grace deadline if nobody
                # re-attaches. Keep draining even while detached so the
                # worker's queue can never back up.
                while True:
                    try:
                        item = q.get_nowait()
                    except queue.Empty:
                        if fut.done():
                            break
                        await asyncio.sleep(0.02)
                        continue
                    if item is _DONE:
                        break
                    target = self._conv_sockets.get(conv_id)
                    if target is None:
                        continue
                    if target.closed or not await self._safe_send(target, item):
                        if self._conv_sockets.get(conv_id) is target:
                            self._conv_sockets[conv_id] = None
                            self._schedule_grace(conv_id)
                # Flush anything enqueued between the last get_nowait and _DONE
                # (ordering: _DONE is the worker's final action, so once the
                # worker is done all real events are already queued).
                while True:
                    try:
                        item = q.get_nowait()
                    except queue.Empty:
                        break
                    if item is _DONE:
                        continue
                    target = self._conv_sockets.get(conv_id)
                    if target is not None and not target.closed:
                        await self._safe_send(target, item)

                final, exit_reason, usage, err = await fut
                # Route the terminal event via the registry too — a client
                # that re-attached mid-turn is waiting for this on its new
                # socket.
                target = self._conv_sockets.get(conv_id) or ws
                if err:
                    await self._safe_send(target, {"type": "error", "turn_id": turn_id,
                                                   "conv_id": conv_id, "message": err,
                                                   "code": "turn_failed"})
                else:
                    # Tag interrupted turns so the desktop can render a stop
                    # indicator, and api failures (chat() returns "API error:
                    # …" instead of raising) so they render as errors, not
                    # normal replies.
                    done = {"type": "complete", "turn_id": turn_id,
                            "conv_id": conv_id, "text": final or ""}
                    if exit_reason in ("interrupted", "api_error", "api_timeout"):
                        done["reason"] = exit_reason
                    if usage:
                        done["usage"] = usage
                    await self._safe_send(target, done)
            finally:
                # Steers that landed during final generation (after the last
                # iteration boundary) were never read. The agent is fresh per
                # turn so they can't leak into the next turn — drop + log so a
                # "my steer did nothing" report stays diagnosable. (The gateway
                # instead re-queues, because a messaging steer is a real user
                # message that must be answered; a desktop redirect arriving
                # too late is just moot.)
                _leftover = agent._drain_steer()
                if _leftover:
                    logger.info("conv=%s: dropped %d late steer(s)", conv_id, len(_leftover))
                with self._active_lock:
                    if self._active.get(conv_id) is agent:
                        self._active.pop(conv_id, None)
                # Drop this turn's socket registration unless a NEWER client
                # already replaced it (attach / overlapping send).
                if self._conv_sockets.get(conv_id) in (ws, None):
                    self._conv_sockets.pop(conv_id, None)

    def _schedule_grace(self, conv_id: str) -> None:
        """Arm the grace timer for a detached conv. Call from coroutines only."""
        asyncio.get_running_loop().call_later(
            self._GRACE_SECONDS, self._grace_interrupt, conv_id)

    def _grace_interrupt(self, conv_id: str) -> None:
        # Fires on the event loop. Skip convs a new client re-attached to —
        # their turn is being watched again and must run to completion.
        target = self._conv_sockets.get(conv_id)
        if target is None or target.closed:
            logger.info("serve: conv=%s not re-attached within %ss; interrupting",
                        conv_id, self._GRACE_SECONDS)
            self._interrupt(conv_id)

    def _interrupt(self, conv_id: str):
        if not conv_id:
            return
        with self._active_lock:
            agent = self._active.get(conv_id)
        if agent:
            try:
                agent.interrupt()
            except Exception:
                logger.warning("interrupt failed for conv=%s", conv_id)

    def _steer(self, conv_id: str, text: str) -> None:
        """Inject a non-interrupting steer into the active turn (if any).

        Mirrors gateway ``steer_session``: the model reads the message at the
        next iteration boundary (``agent._drain_steer`` inside the chat loop)
        without stopping the turn. If no turn is active, this is a no-op — the
        desktop only sends steer mid-turn, and a late/stray one is dropped.
        """
        if not conv_id or not str(text).strip():
            return
        with self._active_lock:
            agent = self._active.get(conv_id)
        if agent:
            # A bare y/n while an approval is pending is the user's verdict,
            # not a steer for the model.
            from tools._approvals import try_resolve_steer
            if try_resolve_steer(agent, str(text)):
                logger.info("conv=%s: steer consumed as approval reply", conv_id)
                return
            try:
                agent.steer(str(text))
            except Exception:
                logger.warning("steer failed for conv=%s", conv_id)

    def _approve(self, conv_id: str, approval_id, approved: bool,
                 always: bool = False) -> None:
        """Resolve the active turn's pending approval (desktop card buttons).
        always=True pairs with approval ("本会话不再询问")."""
        if not conv_id:
            return
        with self._active_lock:
            agent = self._active.get(conv_id)
        if agent:
            try:
                if not agent.resolve_approval(str(approval_id) if approval_id else None,
                                              approved, always=always):
                    logger.info("approve for conv=%s matched no pending approval "
                                "(id=%s)", conv_id, approval_id)
            except Exception:
                logger.warning("approve failed for conv=%s", conv_id, exc_info=True)

    async def _safe_send(self, ws, obj) -> bool:
        """Send a JSON frame. Returns False if the client is gone, so the caller
        can stop streaming instead of pushing to a dead socket."""
        try:
            if ws.closed:
                return False
            await ws.send_json(obj)
            return True
        except Exception as e:
            logger.debug("serve ws send failed: %s", e)
            return False


# CORS — the Electron renderer (file/app origin) fetches REST cross-origin to
# http://127.0.0.1:<port>. WS doesn't enforce same-origin, so only REST needs it.

async def _on_response_prepare(request, response):
    if isinstance(response, web.WebSocketResponse):
        return  # WS upgrades don't go through here; skip defensively
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"


async def _options(request):
    return web.Response(status=204)


class _ServeAccessLogger(AccessLogger):
    """Access logger that drops the desktop's ``/health`` liveness polls.

    The desktop's ServeSupervisor GETs /health every few seconds for readiness
    + steady-state liveness (see ``serve.ts``), so those INFO lines otherwise
    dominate agent.log as near-identical noise. Every other endpoint
    (/sessions, /mcp, /skills, /cron) is rare and worth keeping for
    diagnostics — only /health is suppressed. The browser panel polls
    /browser/status every 5s and re-snaps every frame during a width-handle
    drag, so both join the suppression list.
    """

    def log(self, request, response, time):
        if request.path.startswith(("/health", "/browser/status", "/browser/snap")):
            return
        super().log(request, response, time)


def run_serve(config: dict, host: str = "127.0.0.1", port: int = 7788,
              version: str = "0.0.0"):
    """Entry point for the ``xihe serve`` subcommand."""
    from cli.app import SharedContext

    # (config.yaml is seeded by the CLI entry point BEFORE load_config — see
    # cmd_serve; seeding here would be too late for this process's config.)
    if not config.get("api_key"):
        logger.warning("serve: api_key not set — chat turns will fail until "
                       "config.yaml is filled in and serve is restarted")

    shared_ctx = SharedContext(config)
    app_obj = ServeApp(shared_ctx, version=version)

    aio = web.Application()
    aio.on_response_prepare.append(_on_response_prepare)
    aio.router.add_route("OPTIONS", "/{tail:.*}", _options)
    aio.router.add_get("/health", app_obj.health)
    aio.router.add_get("/readiness", app_obj.readiness)
    aio.router.add_post("/test-connection", app_obj.test_connection)
    aio.router.add_get("/agents", app_obj.agents)
    aio.router.add_get("/sessions", app_obj.list_sessions)
    aio.router.add_get("/convs/{conv_id}/messages", app_obj.get_messages)
    aio.router.add_get("/convs/{conv_id}/trace/{msg_id}", app_obj.get_trace)
    aio.router.add_post("/convs/{conv_id}/reset", app_obj.reset_session)
    aio.router.add_post("/convs/{conv_id}/truncate", app_obj.truncate_conv)
    aio.router.add_post("/convs/{conv_id}/title", app_obj.set_title)
    aio.router.add_delete("/convs/{conv_id}", app_obj.delete_session)
    aio.router.add_get("/mcp", app_obj.mcp)
    aio.router.add_get("/skills", app_obj.skills)
    aio.router.add_get("/cron", app_obj.cron)
    aio.router.add_get("/specialists", app_obj.get_specialists)
    aio.router.add_put("/specialists/{slug}", app_obj.put_specialist)
    aio.router.add_delete("/specialists/{slug}", app_obj.delete_specialist)
    aio.router.add_get("/store", app_obj.store)
    aio.router.add_post("/store/install", app_obj.store_install)
    aio.router.add_post("/store/uninstall", app_obj.store_uninstall)
    aio.router.add_post("/store/mount", app_obj.store_mount)
    aio.router.add_post("/store/refresh", app_obj.store_refresh)
    aio.router.add_get("/browser/status", app_obj.browser_status)
    aio.router.add_post("/browser/launch", app_obj.browser_launch)
    aio.router.add_post("/browser/snap", app_obj.browser_snap)
    aio.router.add_post("/browser/hide", app_obj.browser_hide)
    aio.router.add_post("/browser/show", app_obj.browser_show)
    aio.router.add_post("/browser/release", app_obj.browser_release)
    aio.router.add_post("/browser/appearance", app_obj.browser_appearance)
    aio.router.add_post("/browser/restart", app_obj.browser_restart)
    aio.router.add_get("/stream", app_obj.stream)
    aio.router.add_get("/toolresult", app_obj.toolresult_full)

    logger.info("xihe serve listening on http://%s:%d (platform=%s, toolsets=%s)",
                host, port, _PLATFORM, shared_ctx.main_toolsets)
    print(f"xihe serve → http://{host}:{port}\n"
          f"  ws   /stream                              (send/interrupt → streaming events)\n"
          f"  get  /health /agents /sessions /convs/{{conv_id}}/messages\n"
          f"  get  /mcp /skills /cron /specialists /store   (管理面板/商店)\n"
          f"  post /convs/{{conv_id}}/reset /convs/{{conv_id}}/title\n"
          f"  post /store/install /store/uninstall /store/mount /store/refresh\n"
          f"  get  /browser/status                        (浏览器面板)\n"
          f"  post /browser/launch /browser/snap /browser/hide /browser/show /browser/release /browser/appearance /browser/restart\n"
          f"  put  /specialists/{{slug}}               (专家 agents 编辑)\n"
          f"  del  /specialists/{{slug}} /convs/{{conv_id}}")
    web.run_app(aio, host=host, port=port, print=None,
                access_log_class=_ServeAccessLogger)
