"""Gateway mode — messaging platform server (WeCom, Telegram, etc.)."""

import asyncio
import json
import logging
import re
import signal
import sys
import threading
import time

from core.config import load_config, api_key_missing_message, seed_default_config, AGENT_HOME
from core.diagnostics import (PLATFORM_REQUIRED_FIELDS,
                              platform_config_missing_message,
                              platform_missing_fields)
from gateway.commands import handle_command

logger = logging.getLogger("gateway")


# Sent once per chat, ever — the session row is created by the first turn,
# so "no session yet" doubles as the first-contact marker.
FIRST_CONTACT_WELCOME = (
    "👋 你好，我是 xihe，第一次在这里为你服务。\n"
    "我能操作浏览器、跑终端命令、读写文件、查内部系统，也能定时干活——"
    "直接用自然语言下任务即可。\n"
    "发 /help 看命令列表，/tools 看我当前会的工具。"
)


def _friendly_error(e: Exception) -> str:
    """Turn an agent-loop exception into something a chat end-user can act on.

    Model/auth failures are the common case and actionable (admin fixes
    config.yaml); raw exceptions otherwise, truncated for chat display.
    """
    text = str(e)
    low = text.lower()
    if "401" in text or "api_key" in low or "authentication" in low:
        return (f"⚠️ 模型连接失败（{text[:200]}）\n"
                "请联系管理员检查 config.yaml 里的 api_key / base_url。")
    if "timeout" in low or "timed out" in low:
        return f"⚠️ 请求超时：{text[:200]}。可以让我重试，或稍后再试。"
    return f"⚠️ 处理出错：{text[:300]}"

# If agent takes longer than this (seconds), send an "in progress" ack
# before the platform callback expires. WeCom callback window ≈ 300s.
ACK_THRESHOLD_SECONDS = 10
ACK_MESSAGE = "⏳ 正在处理，请稍候…"

# Module-level so gateway/commands.py can import them. handle_message mutates
# the dict but never rebinds, so normal global lookup applies.
_active_agents: dict[str, object] = {}
_active_agents_lock = threading.Lock()


def interrupt_session(session_key: str) -> bool:
    """Interrupt the active agent turn for a session, if one is running.

    The single out-of-band interrupt entry point. Returns True if an active
    turn was found and interrupted, False if nothing was running. Safe to
    call from any thread; used by the adapter's stop-intent hook, the /stop
    slash command (fallback), and handle_message's safety net.
    """
    with _active_agents_lock:
        agent = _active_agents.get(session_key)
    if agent is None:
        return False
    try:
        agent.interrupt()
        logger.info("Interrupted active agent for session %s", session_key)
        return True
    except Exception:
        logger.warning("Failed to interrupt agent for session %s", session_key)
        return False


def _active_agent_for(session_key: str):
    with _active_agents_lock:
        return _active_agents.get(session_key)


def steer_session(session_key: str, text: str) -> bool:
    """Inject a non-interrupting steer message into the active turn, if running.

    Returns True if a turn was active and the message was steered into it,
    False if no turn was active (caller should treat the message as a normal
    new turn). Safe to call from any thread.
    """
    agent = _active_agent_for(session_key)
    if agent is None:
        return False
    try:
        agent.steer(text)
        logger.info("Steered message into session %s", session_key)
        return True
    except Exception:
        logger.warning("Steer failed for session %s", session_key)
        return False


def _split_response(text: str, max_len: int = 3500) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at <= max_len // 2:
            split_at = text.rfind("。", 0, max_len)
            if split_at <= max_len // 2:
                split_at = text.rfind(". ", 0, max_len)
            if split_at <= max_len // 2:
                split_at = max_len
        chunks.append(text[:split_at + 1])
        text = text[split_at + 1:]
    return chunks


def _describe_image(image_path: str) -> str:
    """Auto-describe an image, or hint the agent to read it when vision is off.

    When a vision model is configured, auto-describes via vision_analyze (as
    before). When it is not, we do NOT run OCR automatically (the agent decides
    whether text extraction is worth it); instead we return a hint pointing at
    the available tool and the image path, so the agent can call it on demand.
    """
    from tools.vision_tools import pick_image_tool
    tool = pick_image_tool()

    if tool == "vision_analyze":
        try:
            from tools.vision_tools import _vision_analyze
            result = json.loads(_vision_analyze({
                "image": image_path,
                "prompt": "简短描述这张图片的内容，包括文字、物体、场景等关键信息。",
            }))
            return result.get("analysis", "图片分析失败")
        except Exception as e:
            logger.warning("Vision auto-describe failed for %s: %s", image_path, e)
            return f"图片分析失败: {e}"

    if tool == "image_ocr":
        return f"（图片未自动识别，路径: {image_path}，可用 image_ocr 工具读取其中的文字）"
    return f"（图片未自动识别，路径: {image_path}，无可用图片识别工具）"


def _extract_media_from_response(text: str) -> tuple[list[str], str]:
    """Extract MEDIA: tags from agent response. Returns (media_paths, clean_text)."""
    media_paths = []
    # Match MEDIA:/path/to/file or MEDIA:https://...
    pattern = r"MEDIA:\s*(\S+)"
    for match in re.finditer(pattern, text):
        media_paths.append(match.group(1))
    clean = re.sub(pattern, "", text).strip()
    return media_paths, clean


def run_gateway(args):
    """Entry point for the gateway subcommand."""
    from cli import SharedContext, setup_logging
    from platforms import create_adapter
    from platforms.base import MessageEvent

    config = load_config(getattr(args, "config", None))
    seeded = seed_default_config(getattr(args, "config", None))
    if not config["api_key"]:
        print(api_key_missing_message(getattr(args, "config", None), seeded=seeded),
              file=sys.stderr)
        return 1

    setup_logging(level=logging.INFO, also_file=True)
    logger = logging.getLogger("gateway")

    platform_name = getattr(args, "platform", None) or config.get("platform", "wecom")
    platform_config = config.get("platforms", {}).get(platform_name, {})
    missing = platform_missing_fields(platform_name, platform_config)
    if missing:
        print(platform_config_missing_message(platform_name, missing), file=sys.stderr)
        return 2

    # Per-session asyncio lock: serializes same-chat turns so a new message
    # waits for the in-flight turn (and its delivery) to finish before starting.
    # Without this, freeing the event loop (asyncio.to_thread join) lets turn A's
    # late delivery interleave with turn B's ACK/replies -> messages appear out
    # of order in the chat. Each turn holds its session lock from agent start
    # through final delivery.
    _session_locks: dict[str, asyncio.Lock] = {}

    def _get_session_lock(session_key: str) -> asyncio.Lock:
        lock = _session_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            _session_locks[session_key] = lock
        return lock

    async def _send_text(chat_id: str, content: str,
                         reply_to_msg_id: str = None) -> bool:
        """Send text with reply-first, proactive-fallback strategy.

        1. Try sending as a reply (uses platform callback channel)
        2. If reply fails, send as proactive message (no reply context)
        Returns True if any send succeeded.
        """
        result = await platform_adapter.send(chat_id, content,
                                              reply_to_msg_id=reply_to_msg_id)
        if result.success:
            logger.info("Sent reply to %s (%d chars)", chat_id, len(content))
            return True

        logger.warning("Reply send failed (%s), falling back to proactive send",
                        getattr(result, 'error', 'unknown'))

        result = await platform_adapter.send(chat_id, content)
        if result.success:
            logger.info("Sent proactive message to %s (%d chars)", chat_id, len(content))
            return True

        logger.error("Both reply and proactive send failed to %s: %s",
                      chat_id, getattr(result, 'error', 'unknown'))
        return False

    async def _handle_stop_intent(event: MessageEvent):
        """Out-of-band stop: interrupt the active turn (if any) and reply.

        Called directly by the platform adapter when it detects a stop intent,
        bypassing the per-session message queue — a separate control plane so
        the interrupt can't get stuck behind a running turn (the bug the naive
        /stop-as-message approach hit). Mirrors Claude Code's control channel
        for interrupts.
        """
        from core.session import build_session_key
        session_key = build_session_key(event.to_session_source(platform_adapter.name))
        interrupted = interrupt_session(session_key)
        msg = ("⏹ 已发送停止信号，正在中断当前任务..."
               if interrupted else "没有正在运行的任务。")
        try:
            await platform_adapter.send(event.chat_id, msg,
                                        reply_to_msg_id=event.msg_id)
        except Exception as e:
            logger.warning("Stop-intent reply failed: %s", e)

    async def _handle_steer(event: MessageEvent) -> bool:
        """Route a mid-turn message into the active turn as a steer.

        Called by the adapter when a message arrives while a turn is running
        (after the stop-intent check). If a turn is active, inject the text via
        steer_session (the model reads it at the next iteration boundary) and
        ack the user; return True so the adapter does NOT also queue it. If no
        turn is active, return False and the adapter queues it as a new turn.
        """
        from core.session import build_session_key
        session_key = build_session_key(event.to_session_source(platform_adapter.name))
        # A bare y/n while an approval is pending is the user's verdict, not a
        # steer for the model. Consumed silently here — the result callback in
        # handle_message delivers the 已批准/未批准 reply.
        agent = _active_agent_for(session_key)
        if agent is not None:
            from tools._approvals import try_resolve_steer
            if try_resolve_steer(agent, event.text):
                logger.info("Approval resolved from inbound message (session=%s)",
                            session_key)
                return True
        # 后台（cron）挂起的审批：该会话没有活动 turn，y/n/a 折给等着的任务。
        # 静默消费——cron 的 result 回调会回投 已批准/已拒绝。
        from tools._approvals import resolve_pending_reply
        if resolve_pending_reply(platform_adapter.name, event.chat_id,
                                 event.text):
            logger.info("Background approval resolved from inbound message "
                        "(chat=%s)", event.chat_id)
            return True
        if not steer_session(session_key, event.text):
            return False  # no active turn — let the adapter queue it as a new turn
        try:
            await platform_adapter.send(
                event.chat_id, "📝 收到，会结合这条继续处理。",
                reply_to_msg_id=event.msg_id)
        except Exception as e:
            logger.warning("Steer ack failed: %s", e)
        return True

    async def handle_message(event: MessageEvent):
        text = event.text.strip()
        if not text and not event.media_urls:
            return

        image_descriptions = []
        for i, url in enumerate(event.media_urls):
            mtype = event.media_types[i] if i < len(event.media_types) else ""
            if mtype.startswith("image/"):
                try:
                    desc = await asyncio.to_thread(_describe_image, url)
                    image_descriptions.append(desc)
                    logger.info("Auto-described image %s: %s", url[:50], desc[:80])
                except Exception as e:
                    logger.warning("Vision auto-describe failed for %s: %s", url, e)
                    image_descriptions.append(f"(自动识别失败，图片路径: {url})")

        if image_descriptions:
            img_ctx = "\n".join(
                f"[用户发送了图片: {d}]" for d in image_descriptions
            )
            text = f"{img_ctx}\n{text}" if text else img_ctx

        if not text:
            return

        source = event.to_session_source(platform_adapter.name)
        session_key = shared_ctx.db.build_key(source)

        # Safety net: if a turn is somehow still active for this session (e.g.
        # a platform without per-session queue serialization), interrupt it
        # before starting a new one. Normally a no-op — the queue ensures the
        # previous turn already finished. Stop intents never reach here; the
        # adapter routes them out-of-band via _handle_stop_intent.
        interrupt_session(session_key)

        # Slash commands (use a lightweight agent just for command context).
        # In normal gateway operation the adapter intercepts stop intents
        # before this point; /stop here is a fallback for platforms/modes
        # that route it as an ordinary message.
        if text.startswith("/"):
            agent = shared_ctx.create_agent()
            cmd_ctx = {
                "agent": agent,
                "session_key": session_key,
                "platform_adapter": platform_adapter,
            }
            response = handle_command(text, cmd_ctx)
            if response and response not in ("__CLEAR__", "__QUIT__"):
                await platform_adapter.send(event.chat_id, response,
                                            reply_to_msg_id=event.msg_id)
                return

        # First contact in this chat ever (no session row yet): a short intro
        # before the first turn — /help exists, but nothing told the user.
        if shared_ctx.db.get_entry(session_key) is None:
            try:
                await platform_adapter.send(
                    event.chat_id, FIRST_CONTACT_WELCOME,
                    reply_to_msg_id=event.msg_id)
            except Exception as e:
                logger.warning("Welcome send failed: %s", e)

        # Per-message agent instance — shared heavy state, fresh XiheAgent.
        # Main-agent roster from config.yaml top-level toolsets/skills;
        # web/media/scheduler stay expandable via request_tools when http
        # (which owns request_tools) is in the roster.
        agent = shared_ctx.create_agent(
            enabled_toolsets=shared_ctx.main_toolsets,
            skills_allowed=shared_ctx.main_skills)
        logger.info("Main agent roster: toolsets=%s skills=%s",
                    shared_ctx.main_toolsets,
                    "*" if shared_ctx.main_skills is None
                    else sorted(shared_ctx.main_skills))

        # Track as active agent for this session (for interrupt)
        with _active_agents_lock:
            _active_agents[session_key] = agent

        was_interrupted = False  # set True if this turn ends via user /stop
        try:
            # Serialize same-chat turns so the previous turn's late reply
            # can't interleave with this turn's ACK/replies.
            session_lock = _get_session_lock(session_key)
            await session_lock.acquire()
            logger.info("Processing: session=%s model=%s text=%s",
                        session_key, agent._effective_model(session_key),
                        text[:100])

            # Set up streaming if the platform supports it
            stream_consumer = None
            stream_task = None
            # Stream only when there's a valid reply context — a real inbound
            # msg_id maps to a reply_req_id in the adapter. Re-queued/synthetic
            # events (e.g. unconsumed steers re-run as new turns) have none, so
            # they fall back to the normal send path; otherwise WeCom rejects
            # the stream send (errcode 40008).
            reply_req_id = None
            if hasattr(platform_adapter, '_reply_req_ids'):
                reply_req_id = platform_adapter._reply_req_ids.get(event.msg_id)
            use_streaming = (
                reply_req_id is not None
                and (hasattr(platform_adapter, 'send_stream')
                     or hasattr(platform_adapter, 'edit_message'))
            )

            if use_streaming:
                from gateway.stream_consumer import StreamConsumer
                stream_consumer = StreamConsumer(
                    adapter=platform_adapter,
                    chat_id=event.chat_id,
                    reply_req_id=reply_req_id,
                )
                stream_task = asyncio.create_task(stream_consumer.run())

            agent_result = None
            agent_exception = None
            ack_sent = False
            agent_start = time.monotonic()

            # 审批回调在工具线程触发（dispatch 内），发消息要跨回事件循环。
            # 请求回调阻塞等待发送结果——发不出去就当通知失败拒绝，不能让
            # 用户永远看不到提示干等超时；结果回调只投递不等待。
            _loop = asyncio.get_running_loop()

            def _approval_request_cb(info: dict):
                msg = (f"⚠️ 危险操作待确认\n{info.get('summary', '')}\n\n"
                       f"回复 y 批准 / n 拒绝 / a 本会话不再询问")
                fut = asyncio.run_coroutine_threadsafe(
                    platform_adapter.send(event.chat_id, msg,
                                          reply_to_msg_id=event.msg_id),
                    _loop)
                fut.result(timeout=30)

            def _approval_result_cb(info: dict, approved: bool, reason: str):
                verdict = "✅ 已批准，继续执行" if approved else f"❌ 未批准（{reason}）"
                fut = asyncio.run_coroutine_threadsafe(
                    platform_adapter.send(event.chat_id, verdict,
                                          reply_to_msg_id=event.msg_id),
                    _loop)
                fut.add_done_callback(
                    lambda f: f.exception() and logger.warning(
                        "approval verdict send failed: %s", f.exception()))

            def _run_agent():
                nonlocal agent_result, agent_exception
                try:
                    agent_result = agent.chat(
                        source=source,
                        user_message=text,
                        stream_delta_callback=stream_consumer.on_delta if stream_consumer else None,
                        tool_call_start_callback=(
                            stream_consumer.on_tool_start if stream_consumer else None),
                        tool_result_callback=(
                            stream_consumer.on_tool_result if stream_consumer else None),
                        tool_call_callback=lambda name, args, elapsed: logger.info(
                            "Tool %s completed (%.1fs)", name, elapsed),
                        approval_request_callback=_approval_request_cb,
                        approval_result_callback=_approval_result_cb,
                    )
                except Exception as e:
                    agent_exception = e

            agent_thread = threading.Thread(target=_run_agent, daemon=True)
            agent_thread.start()

            # Wait for agent while checking if we need to send an ACK.
            # IMPORTANT: use asyncio.to_thread so the event loop stays free while
            # the agent thread runs. A plain blocking agent_thread.join() would
            # deadlock the single-threaded event loop, which starves:
            #   - the stream consumer (progressive WeCom stream updates time out)
            #   - inbound WebSocket reads (new messages can't be handled mid-turn,
            #     so same-chat messages serialize and cross-chat turns block each other)
            # Keeping the loop free is required for streaming delivery, concurrency,
            # and any future interrupt/HITL features. Do NOT revert to blocking join.
            while agent_thread.is_alive():
                await asyncio.to_thread(agent_thread.join, 5.0)
                if not agent_thread.is_alive():
                    break
                elapsed = time.monotonic() - agent_start
                # Send ACK before platform callback expires
                # Use proactive send (no reply_to_msg_id) to avoid consuming
                # the reply channel — leave it for the final stream response.
                if not ack_sent and elapsed >= ACK_THRESHOLD_SECONDS:
                    ack_sent = True
                    # ACK only covers the no-streaming fallback path: with a
                    # stream consumer the live thinking frame (🧠 思考中) is
                    # already on screen — a separate "正在处理" is noise.
                    if stream_consumer and stream_consumer.already_sent:
                        logger.info("Agent running %.0fs but stream already "
                                    "delivered — skipping ACK", elapsed)
                    elif stream_consumer:
                        logger.info("Agent running %.0fs with stream attached — "
                                    "thinking frame replaces ACK", elapsed)
                    else:
                        logger.info("Agent still running after %.0fs, sending ACK",
                                    elapsed)
                        try:
                            await platform_adapter.send(event.chat_id, ACK_MESSAGE)
                        except Exception as e:
                            logger.warning("Failed to send ACK: %s", e)

            if agent_exception:
                raise agent_exception

            response = agent_result

            if stream_consumer:
                stream_consumer.finish()

            if not response:
                response = "(empty response)"

            # Wait for stream consumer to finish sending
            if stream_task:
                try:
                    await asyncio.wait_for(stream_task, timeout=30.0)
                except Exception as e:
                    logger.warning("Stream consumer finish error: %s", e)

            # If streaming already delivered the text, skip normal send.
            # Placeholder-only sends (thinking, no content yet) don't count —
            # otherwise an error/interrupt right after "🤔 思考中…" would
            # leave that as the turn's final message.
            stream_delivered = (stream_consumer
                                and stream_consumer.content_delivered)
            was_interrupted = getattr(agent, "_last_exit_reason", None) == "interrupted"

            if was_interrupted:
                # The turn was interrupted. Send a clear stop confirmation — the
                # /stop ack already said "stopping"; this closes the loop once it
                # actually has. Replaces the terse "[interrupted]" sentinel and
                # guarantees a final message reaches the user (without it, a
                # non-streaming interrupted turn can leave the chat silent).
                try:
                    await platform_adapter.send(
                        event.chat_id, "✅ 任务已停止。",
                        reply_to_msg_id=event.msg_id if not ack_sent else None)
                except Exception as e:
                    logger.warning("Stop-confirmation send failed: %s", e)
            elif not stream_delivered:
                media_paths, clean_response = _extract_media_from_response(response)

                if clean_response:
                    for i, chunk in enumerate(_split_response(clean_response)):
                        # If ACK was sent, don't use reply_to (callback expired)
                        reply_to = event.msg_id if (i == 0 and not ack_sent) else None
                        await _send_text(event.chat_id, chunk,
                                         reply_to_msg_id=reply_to)

                for media_path in media_paths:
                    try:
                        mtype = _guess_media_type(media_path)
                        reply_to = event.msg_id if not ack_sent else None
                        if mtype == "image":
                            await platform_adapter.send_image(event.chat_id, media_path,
                                                              reply_to_msg_id=reply_to)
                        elif mtype == "voice":
                            await platform_adapter.send_voice(event.chat_id, media_path,
                                                              reply_to_msg_id=reply_to)
                        else:
                            await platform_adapter.send_document(event.chat_id, media_path,
                                                                 reply_to_msg_id=reply_to)
                    except Exception as e:
                        logger.warning("Failed to send media %s: %s", media_path, e)

            from tools.send_message_tool import get_pending_media
            pending_items = get_pending_media()
            logger.info("Processing %d pending media items", len(pending_items))
            for item in pending_items:
                try:
                    logger.info("Sending media: type=%s path=%s", item["type"], item["path"])
                    reply_to = item.get("reply_to_msg_id") if not ack_sent else None
                    if item["type"] == "image":
                        result = await platform_adapter.send_image(
                            event.chat_id, item["path"],
                            caption=item.get("caption"),
                            reply_to_msg_id=reply_to)
                        logger.info("Image send result: success=%s error=%s",
                                   result.success, getattr(result, 'error', None))
                    elif item["type"] == "voice":
                        result = await platform_adapter.send_voice(
                            event.chat_id, item["path"],
                            caption=item.get("caption"),
                            reply_to_msg_id=reply_to)
                        logger.info("Voice send result: success=%s", result.success)
                    else:
                        result = await platform_adapter.send_document(
                            event.chat_id, item["path"],
                            reply_to_msg_id=reply_to)
                        logger.info("Document send result: success=%s", result.success)
                except Exception as e:
                    logger.exception("Failed to send queued media %s", item["path"])

        except Exception as e:
            logger.exception("Agent error")
            # Try reply first, then proactive
            await _send_text(event.chat_id, _friendly_error(e),
                             reply_to_msg_id=event.msg_id)
        finally:
            with _active_agents_lock:
                _active_agents.pop(session_key, None)
            # Release the per-session turn lock (acquired above). Safe even if
            # acquire never ran (AttributeError guard).
            try:
                session_lock.release()
            except (AssertionError, UnboundLocalError):
                pass

        # Handle steered messages this turn never consumed.
        _remaining = agent._drain_steer()
        if _remaining and was_interrupted:
            # User explicitly stopped — drop the steered message. Re-queueing it
            # would immediately start a new task, making "停止" look like it
            # didn't work (the bot would keep going on the steered message).
            logger.info("Dropping %d unconsumed steer(s): turn was stopped by user.",
                        len(_remaining))
        else:
            # Turn ended naturally with a steer that arrived too late to consume
            # (e.g. during the final-generation window). A steer is a real user
            # message, so give each one its own fresh turn instead of dropping it.
            for _steer_text in _remaining:
                _requeue = MessageEvent(
                    text=_steer_text,
                    chat_id=event.chat_id,
                    msg_id=f"steer-redir-{time.monotonic_ns()}",
                    sender_id=event.sender_id,
                    is_group=event.is_group,
                    chat_type=event.chat_type,
                )
                logger.info("Re-queueing unconsumed steer as a new turn: %s",
                            _steer_text[:80])
                asyncio.create_task(handle_message(_requeue))

    # Create adapter first (needed for shared context init)
    platform_adapter = create_adapter(platform_name, platform_config, handle_message)
    if not platform_adapter:
        logger.error("Failed to create platform adapter for '%s'", platform_name)
        return 1

    # Register the out-of-band interrupt handler so the adapter can route stop
    # intents directly to interrupt_session, bypassing the per-session queue.
    platform_adapter.set_interrupt_handler(_handle_stop_intent)
    # Register the steer handler so a message arriving mid-turn is injected
    # into the active turn instead of queueing as a new one.
    platform_adapter.set_steer_handler(_handle_steer)

    # Create shared context (heavy state: db, aux client, compressor)
    shared_ctx = SharedContext(config)

    from tools import send_message_tool
    from tools.cronjob_tools import set_platform_adapter, set_agent_factory
    send_message_tool.set_adapter(platform_adapter)
    set_platform_adapter(platform_adapter)
    # Give the cron scheduler an agent factory so jobs run autonomously after
    # gateway startup (without waiting for a chat-side cronjob tool call to
    # inject _agent). Fresh agent per job → concurrency-safe.
    set_agent_factory(shared_ctx.create_agent)

    logger.info("Gateway initialized: model=%s base_url=%s", config["model"], config["base_url"])

    async def _run():
        # Store main event loop for tools that need to schedule async work
        from tools import send_message_tool
        send_message_tool.set_loop(asyncio.get_event_loop())

        connected = await platform_adapter.start()
        if not connected:
            logger.error("Failed to connect to %s", platform_name)
            return 1

        logger.info("Xihe Agent gateway running on %s — 去给机器人发条消息试试。"
                    "日志: %s | Ctrl+C 停止。",
                    platform_name, AGENT_HOME / "agent.log")
        stop_event = asyncio.Event()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                asyncio.get_event_loop().add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass  # Windows

        try:
            await stop_event.wait()
        except KeyboardInterrupt:
            pass
        finally:
            await platform_adapter.stop()
            try:
                from tools.browser_tool import shutdown_browser
                shutdown_browser()
            except Exception:
                pass
            logger.info("Goodbye.")
        return 0

    return asyncio.run(_run())


def _guess_media_type(path: str) -> str:
    """Guess media type from file extension."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"):
        return "image"
    if ext in ("amr", "mp3", "wav", "ogg", "m4a", "flac"):
        return "voice"
    return "file"
