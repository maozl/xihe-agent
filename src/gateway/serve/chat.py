"""The conversation business (ServeApp's ChatMixin): the /stream turn engine
(send/attach/interrupt/steer/approve, the worker→WS drain loop, the
detached-turn grace window) plus the history endpoints (/sessions, /convs/*,
/toolresult). ``_reshape_history`` is the fold the desktop transcript is
built from; Emitter (emitter.py) is the queue the turn worker fills."""

import asyncio
import json
import logging
import queue
import threading
import uuid
from pathlib import Path

from aiohttp import web

from gateway.serve._common import (
    _DEFAULT_USER,
    _PLATFORM,
    _WS_REASONING_LIMIT,
    _WS_RESULT_LIMIT,
)
from gateway.serve.emitter import Emitter, _DONE

logger = logging.getLogger(__name__)


def _ws_approval_key(cwd) -> str:
    """工作空间会话的审批记忆桶。规范化（正斜杠 + 小写——Windows 盘符大小写
    不敏感）保证同一工作空间永远落同一个桶。"""
    norm = str(cwd).replace("\\", "/").rstrip("/")
    return f"ws:{norm.lower()}"


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

    ``incomplete`` marks a turn that never closed with a final plain-text
    assistant row — interrupted mid-loop, or the process died mid-turn (a
    killed serve folds its tool calls into an ordinary-looking bubble; without
    the flag the desktop can't tell a finished reply from a severed one).
    """
    out: list = []
    cur = None  # in-flight assistant turn: {content, tools, anchor, has_reasoning, usage, settled}

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
            if cur.get("ts"):
                item["ts"] = cur["ts"]
            if cur.get("usage"):
                item["usage"] = cur["usage"]
            if not cur.get("settled"):
                item["incomplete"] = True
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
                        "id": m.get("id"), "tools": 0,
                        **({"ts": m["created_at"]} if m.get("created_at") else {})})
            continue
        if role == "assistant":
            tool_calls = m.get("tool_calls") or []
            content = m.get("content")
            if not tool_calls and not content:
                continue  # bare keepalive / protocol noise
            if cur is not None and cur.get("settled") and not tool_calls:
                # Previous turn already closed with a final reply; a new
                # no-tool-calls assistant row is a SEPARATE turn (cron push /
                # server-injected message) — flush before folding it in, or
                # every cron result merges into one giant bubble.
                flush()
            if cur is None:
                # Anchor on the first assistant row of the turn so even
                # reasoning-only turns (no tools) can lazy-load their trace.
                cur = {"content": "", "tools": 0, "anchor": m.get("id"),
                       "has_reasoning": False, "settled": False,
                       "ts": m.get("created_at")}
            if tool_calls:
                cur["tools"] += len(tool_calls)
            else:
                # A no-tool-calls row is the loop's natural end (the reply) —
                # UNLESS it carries the interrupt marker (agent.py persists
                # partial text with it on mid-generation Stop): the turn is
                # incomplete and the desktop must show 未完成 + 继续.
                if isinstance(content, str) and "[被中断" in content:
                    pass  # keep settled=False
                else:
                    cur["settled"] = True
                if not cur.get("ts") and m.get("created_at"):
                    cur["ts"] = m["created_at"]
            if m.get("reasoning"):
                cur["has_reasoning"] = True
            if isinstance(content, str) and content:
                # Strip the interrupt marker from display (the model still
                # sees it in history; the desktop just shows the clean text).
                clean = content.replace("\n\n[被中断，以上为部分输出]", "")
                cur["content"] += clean
            if m.get("usage"):
                cur["usage"] = m["usage"]
            continue
        # Unknown role: flush + pass through minimally.
        flush()
        out.append({"role": role, "content": m.get("content") or "",
                    "id": None, "tools": 0})
    flush()
    return out


class ChatMixin:
    """Mixin into ServeApp — state lives in ServeApp.__init__ (server.py)."""

    def _source(self, conv_id):
        from core.session import SessionSource
        return SessionSource(
            platform=_PLATFORM, chat_id=str(conv_id),
            user_id=_DEFAULT_USER, chat_type="dm",
        )

    def _turn_lock(self, conv_id: str) -> asyncio.Lock:
        """conv_id → asyncio.Lock: serializes overlapping turns on one
        conversation (prevents history corruption / interleaved replies).
        Event-loop thread only — dict mutation needs no extra lock; the
        asyncio.Lock guards the await span."""
        lock = self._turn_locks.get(conv_id)
        if lock is None:
            lock = asyncio.Lock()
            self._turn_locks[conv_id] = lock
        return lock

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
        self._live_sockets.add(ws)
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
            self._live_sockets.discard(ws)
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
            from core.support.approvals import try_resolve_steer
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
        from core.support.tool_result_storage import _STORAGE_DIR
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

    async def toolargs_full(self, request):
        """Full args of a LIVE tool call, by the id its tool_call event
        carried. Serves the in-memory ring (bounded) — history turns get
        full args via the trace endpoint straight from sessions.db."""
        from gateway.serve.emitter import get_tool_args
        raw = request.query.get("id", "")
        try:
            entry = get_tool_args(int(raw))
        except (TypeError, ValueError):
            entry = None
        if entry is None:
            return web.json_response(
                {"ok": False, "reason": "unknown-or-evicted"}, status=404)
        return web.json_response({"ok": True, **entry})

    async def reset_session(self, request):
        conv_id = request.match_info["conv_id"]

        # DB writes run off-loop: SQLite busy_timeout can stall a caller up
        # to 5s, which must not freeze /health and WS streaming.
        def work():
            source = self._source(conv_id)
            session_key = self.ctx.db.build_key(source)
            new_id = self.ctx.db.reset_session(session_key)
            return {"conv_id": conv_id, "session_key": session_key,
                    "reset": bool(new_id)}

        loop = asyncio.get_running_loop()
        return web.json_response(await loop.run_in_executor(None, work))

    async def delete_session(self, request):
        conv_id = request.match_info["conv_id"]

        def work():
            source = self._source(conv_id)
            session_key = self.ctx.db.build_key(source)
            deleted = self.ctx.db.delete_session(session_key)
            return {"conv_id": conv_id, "session_key": session_key,
                    "deleted": bool(deleted)}

        loop = asyncio.get_running_loop()
        return web.json_response(await loop.run_in_executor(None, work))

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

        def work():
            source = self._source(conv_id)
            session_key = self.ctx.db.build_key(source)
            entry = self.ctx.db.get_entry(session_key)
            if not entry:
                return None
            return self.ctx.db.truncate_messages_from(entry.session_id, from_id)

        # The turn lock spans the executor hop: without it a send could start a
        # turn mid-truncate (the old sync version was atomic only by running on
        # the loop). None = unknown conversation.
        loop = asyncio.get_running_loop()
        async with self._turn_lock(conv_id):
            deleted = await loop.run_in_executor(None, work)
        if deleted is None:
            return web.json_response({"ok": False, "reason": "unknown conversation"},
                                     status=404)
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

        def work():
            source = self._source(conv_id)
            session_key = self.ctx.db.build_key(source)
            entry = self.ctx.db.get_entry(session_key)
            if not entry:
                return None
            self.ctx.db.set_session_title(entry.session_id, title)
            return {"conv_id": conv_id, "title": title, "ok": True}

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, work)
        if result is None:
            return web.json_response({"ok": False, "reason": "no session"}, status=404)
        return web.json_response(result)


class DesktopChannel:
    """Outbound push channel (core.services.scheduler's registry): cron
    results → the desktop conversation that created the job. Not an Adapter —
    no inbound, no lifecycle of its own. Registered under both "desktop" and
    "serve" (the conversation platform), so `deliver: origin` on
    desktop-created jobs routes here.

    Durable-first: the message is APPENDED to the conversation's session
    first — persistence is the delivery contract. The live WS event is a
    best-effort bonus broadcast to every connected desktop client (they are
    app-level connections multiplexing all conversations, NOT per-conv —
    _conv_sockets only tracks active-turn streaming targets); each client
    routes by conv_id (loaded conv → instant append; otherwise list sync +
    system notification).
    """

    def __init__(self, app):
        self._app = app

    async def send(self, conv_id: str, message: str) -> bool:
        from core.session import SessionSource

        def _persist():
            db = self._app.ctx.db
            source = SessionSource(platform=_PLATFORM, chat_id=conv_id,
                                   chat_type="dm")
            entry = db.get_entry(db.build_key(source))
            if entry is None:
                return False  # unknown conversation — nothing to land on
            db.append_message(entry.session_id, "assistant", message)
            return True

        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, _persist)
        if not ok:
            return False
        for ws in list(self._app._live_sockets):
            await self._app._safe_send(ws, {
                "type": "cron_result", "conv_id": conv_id, "text": message,
            })
        return True


def add_routes(router, app):
    """The conversation business's routes. ``app`` is the ServeApp instance —
    every handler here is a ChatMixin method on it."""
    router.add_get("/stream", app.stream)
    router.add_get("/sessions", app.list_sessions)
    router.add_get("/convs/{conv_id}/messages", app.get_messages)
    router.add_get("/convs/{conv_id}/trace/{msg_id}", app.get_trace)
    router.add_post("/convs/{conv_id}/reset", app.reset_session)
    router.add_post("/convs/{conv_id}/truncate", app.truncate_conv)
    router.add_post("/convs/{conv_id}/title", app.set_title)
    router.add_delete("/convs/{conv_id}", app.delete_session)
    router.add_get("/toolresult", app.toolresult_full)
    router.add_get("/toolargs", app.toolargs_full)
