"""WeCom AI Bot WebSocket platform adapter with media support.

Features:
- Inbound: text, voice (transcription), image (cached locally), mixed messages
- Outbound: markdown text, images, voice, files via 3-step upload protocol
- Auto-downgrade: images >10MB → file, non-AMR voice → file
- AES-CBC decryption for encrypted WeCom media
"""

import asyncio
import base64
import collections
import hashlib
import json
import logging
import mimetypes
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from platforms.base import (
    BasePlatformAdapter, MessageCallback, MessageEvent, SendResult,
    cache_image_from_bytes, cache_document_from_bytes,
)

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    import httpx
except ImportError:
    httpx = None

logger = logging.getLogger(__name__)


def _is_ws_gone(exc: Exception) -> bool:
    """True if `exc` means a WebSocket write definitively failed because the
    connection is gone or closing — safe to queue the message for redelivery
    after reconnect.

    TimeoutError is intentionally excluded: a timeout may mean the message was
    actually sent but the ack didn't return, so re-queueing risks duplicate
    delivery. The errors below mean the write itself failed.
    """
    if isinstance(exc, ConnectionError):
        return True
    if isinstance(exc, RuntimeError):
        m = str(exc).lower()
        if ("websocket not connected" in m
                or "closing transport" in m
                or "connection reset" in m):
            return True
    # aiohttp disconnect errors — match by class name to avoid a hard import
    # dependency (aiohttp is optional).
    if exc.__class__.__name__ in {
        "ServerDisconnectedError", "ClientConnectionError",
        "ClientConnectorError", "ClientConnectorSSLError",
        "WebSocketError",
    }:
        return True
    return False

# WeCom WebSocket commands
CMD_SUBSCRIBE = "aibot_subscribe"
CMD_CALLBACK = "aibot_msg_callback"
CMD_SEND = "aibot_send_msg"
CMD_RESPONSE = "aibot_respond_msg"
CMD_PING = "ping"
CMD_UPLOAD_INIT = "aibot_upload_media_init"
CMD_UPLOAD_CHUNK = "aibot_upload_media_chunk"
CMD_UPLOAD_FINISH = "aibot_upload_media_finish"

# Limits
MAX_MESSAGE_LENGTH = 4000
CONNECT_TIMEOUT = 20.0
REQUEST_TIMEOUT = 15.0
HEARTBEAT_INTERVAL = 30.0
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
DEDUP_WINDOW = 300
DEDUP_MAX = 500

IMAGE_MAX_BYTES = 10 * 1024 * 1024
VOICE_MAX_BYTES = 2 * 1024 * 1024
FILE_MAX_BYTES = 20 * 1024 * 1024
ABSOLUTE_MAX_BYTES = FILE_MAX_BYTES
UPLOAD_CHUNK_SIZE = 512 * 1024
MAX_UPLOAD_CHUNKS = 100
VOICE_SUPPORTED_MIMES = {"audio/amr"}


class WeComAdapter(BasePlatformAdapter):
    """WeCom AI Bot adapter via persistent WebSocket with media support."""

    name = "wecom"

    def __init__(self, config: dict, on_message: MessageCallback = None):
        if aiohttp is None:
            raise ImportError("aiohttp is required: pip install aiohttp")

        super().__init__(config, on_message)
        self._bot_id = config.get("bot_id", "")
        self._secret = config.get("secret", "")
        self._ws_url = config.get("ws_url", "wss://openws.work.weixin.qq.com")

        self._session = None
        self._ws = None
        self._http_client = None
        self._listen_task = None
        self._heartbeat_task = None
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._seen_messages: dict[str, float] = {}
        self._reply_req_ids: dict[str, str] = {}
        self._finished_stream_req_ids: set[str] = set()  # req_ids that got a finish=True stream
        self._msg_queue: asyncio.Queue = None
        self._session_queues: dict[str, asyncio.Queue] = {}
        self._session_tasks: dict[str, asyncio.Task] = {}

        # Outbound queue: messages buffered during WS disconnection,
        # flushed automatically after reconnection.
        self._pending_outbound: collections.deque = collections.deque(maxlen=50)
        self._ws_connected: asyncio.Event = asyncio.Event()
        self._flush_task: asyncio.Task = None

    async def start(self) -> bool:
        if not self._bot_id or not self._secret:
            logger.error("[WeCom] bot_id and secret are required")
            return False
        try:
            if httpx:
                self._http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
            await self._open_connection()
            self._running = True
            self._msg_queue = asyncio.Queue()
            self._listen_task = asyncio.create_task(self._listen_loop())
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            self._dispatch_task = asyncio.create_task(self._dispatch_loop())
            logger.info("[WeCom] Connected to %s", self._ws_url)
            return True
        except Exception as e:
            logger.error("[WeCom] Connection failed: %s", e, exc_info=True)
            await self._cleanup_ws()
            return False

    async def stop(self):
        self._running = False
        self._ws_connected.clear()
        for task in (self._listen_task, self._heartbeat_task, self._dispatch_task,
                     self._flush_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._fail_pending(RuntimeError("Shutting down"))
        await self._cleanup_ws()
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        logger.info("[WeCom] Disconnected")

    async def send(self, chat_id: str, content: str,
                   reply_to_msg_id: str = None) -> SendResult:
        if not chat_id:
            return SendResult(success=False, error="chat_id is required")

        # If WS is down, queue the message for delivery after reconnection
        if not self._ws or self._ws.closed:
            self._pending_outbound.append({
                "type": "text",
                "chat_id": chat_id,
                "content": content,
            })
            logger.info("[WeCom] WS down, queued text message to %s (%d chars, queue=%d)",
                        chat_id, len(content), len(self._pending_outbound))
            return SendResult(success=True)

        try:
            reply_req_id = self._reply_req_ids.get(reply_to_msg_id) if reply_to_msg_id else None
            if reply_req_id:
                # Skip if streaming already delivered a finished message for this req
                if reply_req_id in self._finished_stream_req_ids:
                    logger.info("[WeCom] Skipping send — stream already finished for req %s", reply_req_id)
                    return SendResult(success=True)
                response = await self._send_reply_request(reply_req_id, {
                    "msgtype": "markdown",
                    "markdown": {"content": content[:MAX_MESSAGE_LENGTH]},
                })
            else:
                response = await self._send_request(CMD_SEND, {
                    "chatid": chat_id,
                    "msgtype": "markdown",
                    "markdown": {"content": content[:MAX_MESSAGE_LENGTH]},
                })
            errcode = response.get("errcode", 0)
            if errcode not in (0, None):
                return SendResult(success=False, error=f"errcode={errcode}")
            msg_id = str(response.get("body", {}).get("msgid", "") or "") if isinstance(response.get("body"), dict) else ""
            return SendResult(success=True, message_id=msg_id)
        except asyncio.TimeoutError:
            return SendResult(success=False, error="Timeout")
        except Exception as e:
            if _is_ws_gone(e):
                self._pending_outbound.append({
                    "type": "text",
                    "chat_id": chat_id,
                    "content": content,
                })
                logger.info("[WeCom] WS send failed (%s), queued text to %s (queue=%d)",
                            type(e).__name__, chat_id, len(self._pending_outbound))
                return SendResult(success=True)
            return SendResult(success=False, error=str(e))

    async def send_stream(self, chat_id: str, content: str, stream_id: str,
                          finish: bool = False,
                          reply_req_id: str = None) -> SendResult:
        """Send or update a streaming message.

        Args:
            chat_id: Target chat.
            content: Full accumulated text so far.
            stream_id: Unique stream identifier (same across updates).
            finish: True on the final update.
            reply_req_id: If replying to an inbound message.
        """
        # If WS is down and this is the final message, queue as regular text
        # so it gets delivered after reconnection. Intermediate updates are
        # dropped — only the final content matters.
        if (not self._ws or self._ws.closed) and finish:
            self._pending_outbound.append({
                "type": "text",
                "chat_id": chat_id,
                "content": content,
            })
            logger.info("[WeCom] WS down, queued final stream to %s (%d chars, queue=%d)",
                        chat_id, len(content), len(self._pending_outbound))
            return SendResult(success=True)

        # If WS is down and this is an intermediate update, just fail
        # — the stream consumer will keep accumulating and eventually
        # call finish=True, which we'll queue.
        if not self._ws or self._ws.closed:
            return SendResult(success=False, error="WebSocket not connected")

        body = {
            "msgtype": "stream",
            "stream": {
                "id": stream_id,
                "finish": finish,
                "content": content[:MAX_MESSAGE_LENGTH],
            },
        }
        try:
            if reply_req_id:
                response = await self._send_reply_request(reply_req_id, body)
            else:
                response = await self._send_request(CMD_SEND, {
                    "chatid": chat_id,
                    **body,
                })
            errcode = response.get("errcode", 0)
            if errcode not in (0, None):
                return SendResult(success=False, error=f"errcode={errcode}")
            # Track finished streams to prevent duplicate sends
            if finish and reply_req_id:
                self._finished_stream_req_ids.add(reply_req_id)
            return SendResult(success=True)
        except asyncio.TimeoutError:
            return SendResult(success=False, error="Timeout")
        except Exception as e:
            if _is_ws_gone(e) and finish:
                self._pending_outbound.append({
                    "type": "text",
                    "chat_id": chat_id,
                    "content": content,
                })
                logger.info("[WeCom] WS stream finish failed (%s), queued to %s (queue=%d)",
                            type(e).__name__, chat_id, len(self._pending_outbound))
                return SendResult(success=True)
            return SendResult(success=False, error=str(e))

    async def send_image(self, chat_id: str, image_url: str,
                         caption: str = None,
                         reply_to_msg_id: str = None) -> SendResult:
        result = await self._send_media_source(chat_id, image_url, caption=caption,
                                                reply_to=reply_to_msg_id)
        if result.success or not _looks_like_url(image_url):
            return result
        # Fallback: send URL as text
        logger.warning("[WeCom] Falling back to text for image %s: %s", image_url, result.error)
        text = f"{caption}\n{image_url}" if caption else image_url
        return await self.send(chat_id, text, reply_to_msg_id=reply_to_msg_id)

    async def send_voice(self, chat_id: str, audio_path: str,
                         caption: str = None,
                         reply_to_msg_id: str = None) -> SendResult:
        return await self._send_media_source(chat_id, audio_path, caption=caption,
                                              reply_to=reply_to_msg_id)

    async def send_document(self, chat_id: str, file_path: str,
                            caption: str = None,
                            reply_to_msg_id: str = None) -> SendResult:
        return await self._send_media_source(chat_id, file_path, caption=caption,
                                              reply_to=reply_to_msg_id)

    async def _send_media_source(self, chat_id: str, media_source: str,
                                  caption: str = None, file_name: str = None,
                                  reply_to: str = None) -> SendResult:
        """Upload and send a media file (image, voice, or document)."""
        if not chat_id:
            return SendResult(success=False, error="chat_id is required")

        try:
            data, content_type, resolved_name = await self._load_outbound_media(
                media_source, file_name=file_name)
        except FileNotFoundError as e:
            return SendResult(success=False, error=str(e))
        except Exception as e:
            logger.error("[WeCom] Failed to load media %s: %s", media_source, e)
            return SendResult(success=False, error=str(e))

        detected_type = _detect_wecom_media_type(content_type)
        size_check = _apply_file_size_limits(len(data), detected_type, content_type)

        if size_check["rejected"]:
            await self.send(chat_id, f"⚠️ {size_check['reject_reason']}",
                            reply_to_msg_id=reply_to)
            return SendResult(success=False, error=size_check["reject_reason"])

        try:
            upload_result = await self._upload_media_bytes(
                data, size_check["final_type"], resolved_name)
            media_id = upload_result["media_id"]

            reply_req_id = self._reply_req_ids.get(reply_to) if reply_to else None
            if reply_req_id:
                await self._send_reply_media(reply_req_id, size_check["final_type"], media_id)
            else:
                await self._send_media_message(chat_id, size_check["final_type"], media_id)
        except asyncio.TimeoutError:
            return SendResult(success=False, error="Timeout sending media")
        except Exception as e:
            logger.error("[WeCom] Failed to send media: %s", e)
            return SendResult(success=False, error=str(e))

        # Send caption and downgrade note as follow-up text
        if caption:
            await self.send(chat_id, caption, reply_to_msg_id=reply_to)
        if size_check["downgraded"] and size_check["downgrade_note"]:
            await self.send(chat_id, f"ℹ️ {size_check['downgrade_note']}",
                            reply_to_msg_id=reply_to)

        return SendResult(success=True)

    async def _load_outbound_media(self, media_source: str,
                                    file_name: str = None) -> tuple[bytes, str, str]:
        """Load media from local path or URL. Returns (data, content_type, filename)."""
        source = media_source.strip()
        if not source:
            raise ValueError("media source is required")

        parsed = urlparse(source)
        if parsed.scheme in ("http", "https"):
            data, headers = await self._download_remote_bytes(source, ABSOLUTE_MAX_BYTES)
            content_disp = headers.get("content-disposition")
            resolved_name = file_name or _guess_filename(source, content_disp,
                                                          headers.get("content-type", ""))
            content_type = _normalize_content_type(headers.get("content-type", ""), resolved_name)
            return data, content_type, resolved_name

        # Local file
        local_path = Path(parsed.path if parsed.scheme == "file" else source).expanduser()
        if not local_path.is_absolute():
            local_path = (Path.cwd() / local_path).resolve()
        if not local_path.exists():
            raise FileNotFoundError(f"Media file not found: {local_path}")

        data = local_path.read_bytes()
        resolved_name = file_name or local_path.name
        content_type = _normalize_content_type("", resolved_name)
        return data, content_type, resolved_name

    async def _upload_media_bytes(self, data: bytes, media_type: str,
                                   filename: str) -> dict:
        """3-step upload: init → chunk → finish. Returns {type, media_id, created_at}."""
        if not data:
            raise ValueError("Cannot upload empty media")

        total_size = len(data)
        total_chunks = (total_size + UPLOAD_CHUNK_SIZE - 1) // UPLOAD_CHUNK_SIZE
        
        logger.info("[WeCom] Starting upload: filename=%s size=%.2fMB chunks=%d type=%s",
                   filename, total_size / 1024 / 1024, total_chunks, media_type)
        
        if total_chunks > MAX_UPLOAD_CHUNKS:
            raise ValueError(f"File too large: {total_chunks} chunks (max {MAX_UPLOAD_CHUNKS})")

        # Use longer timeout for uploads (60s per operation)
        upload_timeout = 60.0

        # Step 1: Init
        logger.info("[WeCom] Upload step 1/3: initializing...")
        init_resp = await self._send_request(CMD_UPLOAD_INIT, {
            "type": media_type,
            "filename": filename,
            "total_size": total_size,
            "total_chunks": total_chunks,
            "md5": hashlib.md5(data).hexdigest(),
        }, timeout=upload_timeout)
        _raise_for_error(init_resp, "upload init")

        init_body = init_resp.get("body") if isinstance(init_resp.get("body"), dict) else {}
        upload_id = str(init_body.get("upload_id") or "").strip()
        if not upload_id:
            raise RuntimeError(f"Upload init failed: missing upload_id")
        logger.info("[WeCom] Upload initialized: upload_id=%s", upload_id)

        # Step 2: Chunks
        logger.info("[WeCom] Upload step 2/3: sending %d chunks...", total_chunks)
        for chunk_idx, start in enumerate(range(0, total_size, UPLOAD_CHUNK_SIZE)):
            chunk = data[start:start + UPLOAD_CHUNK_SIZE]
            logger.debug("[WeCom] Uploading chunk %d/%d (%d bytes)", 
                       chunk_idx + 1, total_chunks, len(chunk))
            chunk_resp = await self._send_request(CMD_UPLOAD_CHUNK, {
                "upload_id": upload_id,
                "chunk_index": chunk_idx,
                "base64_data": base64.b64encode(chunk).decode("ascii"),
            }, timeout=upload_timeout)
            _raise_for_error(chunk_resp, f"upload chunk {chunk_idx}")
        logger.info("[WeCom] All chunks uploaded successfully")

        # Step 3: Finish
        logger.info("[WeCom] Upload step 3/3: finalizing...")
        finish_resp = await self._send_request(CMD_UPLOAD_FINISH, {
            "upload_id": upload_id,
        }, timeout=upload_timeout)
        _raise_for_error(finish_resp, "upload finish")

        finish_body = finish_resp.get("body") if isinstance(finish_resp.get("body"), dict) else {}
        media_id = str(finish_body.get("media_id") or "").strip()
        if not media_id:
            raise RuntimeError(f"Upload finish failed: missing media_id")

        logger.info("[WeCom] Upload complete: media_id=%s size=%.2fMB", 
                   media_id, total_size / 1024 / 1024)
        return {
            "type": str(finish_body.get("type") or media_type),
            "media_id": media_id,
            "created_at": finish_body.get("created_at"),
        }

    async def _send_media_message(self, chat_id: str, media_type: str,
                                   media_id: str) -> dict:
        """Send an uploaded media message via aibot_send_msg."""
        response = await self._send_request(CMD_SEND, {
            "chatid": chat_id,
            "msgtype": media_type,
            media_type: {"media_id": media_id},
        })
        _raise_for_error(response, "send media message")
        return response

    async def _send_reply_media(self, reply_req_id: str, media_type: str,
                                 media_id: str) -> dict:
        """Send an uploaded media message as a reply."""
        response = await self._send_reply_request(reply_req_id, {
            "msgtype": media_type,
            media_type: {"media_id": media_id},
        })
        _raise_for_error(response, "send reply media")
        return response

    async def _on_callback(self, payload: dict):
        body = payload.get("body")
        if not isinstance(body, dict):
            return
        msg_id = str(body.get("msgid") or self._payload_req_id(payload) or uuid.uuid4().hex)
        if self._is_duplicate(msg_id):
            return
        self._reply_req_ids[msg_id] = self._payload_req_id(payload)

        sender = body.get("from") if isinstance(body.get("from"), dict) else {}
        sender_id = str(sender.get("userid") or "").strip()
        chat_id = str(body.get("chatid") or sender_id).strip()
        if not chat_id:
            return

        text = self._extract_text(body)
        media_urls, media_types = await self._extract_media(body)
        msgtype = str(body.get("msgtype") or "").lower()

        # When media extraction fails but msgtype indicates media was sent,
        # add a fallback placeholder so the agent knows something was sent
        if not text and not media_urls:
            if msgtype == "image":
                text = "[用户发送了一张图片]"
                logger.info("[WeCom] Image extraction failed for %s, using placeholder", msg_id)
            elif msgtype == "voice":
                text = "[用户发送了一条语音]"
                logger.info("[WeCom] Voice extraction failed for %s, using placeholder", msg_id)
            elif msgtype == "file":
                text = "[用户发送了一个文件]"
                logger.info("[WeCom] File extraction failed for %s, using placeholder", msg_id)
            else:
                return

        is_group = str(body.get("chattype") or "").lower() == "group"
        chat_type = "group" if is_group else "dm"

        if not text and media_urls:
            parts = []
            for i, url in enumerate(media_urls):
                mtype = media_types[i] if i < len(media_types) else ""
                if mtype.startswith("image/"):
                    parts.append(f"[用户发送了一张图片: {url}]")
                elif mtype.startswith("audio/"):
                    parts.append(f"[用户发送了语音: {url}]")
                else:
                    parts.append(f"[用户发送了文件: {url}]")
            text = "\n".join(parts)

        # Append image paths for image-tool access even when text exists.
        # Pick the available tool (vision_analyze when configured, else image_ocr)
        # so the agent isn't hinted toward a hidden tool.
        if media_urls and text:
            image_paths = [url for i, url in enumerate(media_urls)
                          if (media_types[i] if i < len(media_types) else "").startswith("image/")]
            if image_paths:
                from tools.vision_tools import pick_image_tool
                tool = pick_image_tool()
                if tool:
                    parts = [f"[图片路径，可用{tool}查看: {p}]" for p in image_paths]
                else:
                    parts = [f"[图片路径: {p}（无可用图片识别工具）]" for p in image_paths]
                text += "\n" + "\n".join(parts)

        logger.info("[WeCom] %s from %s: %s (media=%d)",
                    "Group" if is_group else "DM", chat_id, text[:100], len(media_urls))
        event = MessageEvent(
            text=text, chat_id=chat_id, msg_id=msg_id,
            sender_id=sender_id, is_group=is_group,
            chat_type=chat_type,
            raw=payload,
            media_urls=media_urls, media_types=media_types,
        )
        # Stop intents (/stop, /cancel, or natural language like 停/停止/stop)
        # take the out-of-band control channel: call the gateway's interrupt
        # hook directly instead of entering the per-session queue. The queue
        # serializes same-session messages, so a queued stop would sit behind
        # the running turn and fire only after it finishes — useless. Going
        # direct lets interrupt_session fire immediately. Falls through to the
        # queue if no handler is registered (defensive; shouldn't happen).
        from gateway.commands import is_stop_intent
        if is_stop_intent(text) and self._on_interrupt is not None:
            await self._on_interrupt(event)
            return
        # Steer: if a turn is running, inject into it (the model reads the
        # user's refinement at the next iteration boundary) instead of queueing
        # a new turn. Handler returns True when it steered (active turn), False
        # when there's no active turn — then we fall through to the queue so it
        # starts a normal new turn.
        if self._on_steer is not None:
            try:
                if await self._on_steer(event):
                    return
            except Exception as e:
                logger.warning("[WeCom] Steer handler failed: %s", e)
                # fall through to queue on error
        # Queue message for ordered processing (non-blocking)
        if self._msg_queue is not None:
            self._msg_queue.put_nowait(event)
        else:
            await self._emit_message(event)

    async def _dispatch_loop(self):
        """Route queued messages to per-session queues.

        Messages in the same session are processed sequentially.
        Different sessions run in parallel — so a long cronjob won't
        block the user's DM.
        """
        while self._running:
            try:
                event = await asyncio.wait_for(
                    self._msg_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except Exception:
                continue

            sk = event.session_key
            if not sk:
                from core.session import build_session_key
                sk = build_session_key(event.to_session_source(self.name))
            if sk not in self._session_queues:
                q = asyncio.Queue()
                self._session_queues[sk] = q
                self._session_tasks[sk] = asyncio.create_task(
                    self._session_worker(sk, q))
            self._session_queues[sk].put_nowait(event)

    async def _session_worker(self, session_key: str, queue: asyncio.Queue):
        """Process messages for one session sequentially."""
        while self._running:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=60.0)
            except asyncio.TimeoutError:
                # Idle — clean up this worker
                break
            except Exception:
                break
            try:
                await self._emit_message(event)
            except Exception as e:
                logger.error("[WeCom] Session %s dispatch error: %s",
                             session_key, e)
        self._session_queues.pop(session_key, None)
        self._session_tasks.pop(session_key, None)

    @staticmethod
    def _extract_text(body: dict) -> str:
        parts = []
        msgtype = str(body.get("msgtype") or "").lower()
        if msgtype == "mixed":
            mixed = body.get("mixed") if isinstance(body.get("mixed"), dict) else {}
            items = mixed.get("msg_item") if isinstance(mixed.get("msg_item"), list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("msgtype") or "").lower()
                if item_type == "text":
                    tb = item.get("text") if isinstance(item.get("text"), dict) else {}
                    c = str(tb.get("content") or "").strip()
                    if c:
                        parts.append(c)
        else:
            tb = body.get("text") if isinstance(body.get("text"), dict) else {}
            c = str(tb.get("content") or "").strip()
            if c:
                parts.append(c)
            if msgtype == "voice":
                vb = body.get("voice") if isinstance(body.get("voice"), dict) else {}
                vt = str(vb.get("content") or "").strip()
                if vt:
                    parts.append(vt)
        return "\n".join(parts).strip()

    async def _extract_media(self, body: dict) -> tuple[list[str], list[str]]:
        """Extract inbound media (images, files) and cache locally."""
        media_paths = []
        media_types_list = []
        refs = []
        msgtype = str(body.get("msgtype") or "").lower()

        if msgtype == "mixed":
            mixed = body.get("mixed") if isinstance(body.get("mixed"), dict) else {}
            items = mixed.get("msg_item") if isinstance(mixed.get("msg_item"), list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("msgtype") or "").lower()
                if item_type == "image" and isinstance(item.get("image"), dict):
                    refs.append(("image", item["image"]))
        else:
            if isinstance(body.get("image"), dict):
                refs.append(("image", body["image"]))
            if msgtype == "file" and isinstance(body.get("file"), dict):
                refs.append(("file", body["file"]))

        for kind, ref in refs:
            cached = await self._cache_media(kind, ref)
            if cached:
                path, content_type = cached
                media_paths.append(path)
                media_types_list.append(content_type)

        return media_paths, media_types_list

    async def _cache_media(self, kind: str, media: dict) -> Optional[tuple[str, str]]:
        """Cache an inbound media reference to local storage."""
        # Base64 data
        if "base64" in media and media.get("base64"):
            try:
                raw = _decode_base64(media["base64"])
            except Exception as e:
                logger.warning("[WeCom] Failed to decode base64 media: %s", e)
                return None

            if kind == "image":
                ext = _detect_image_ext(raw)
                return cache_image_from_bytes(raw, ext), _mime_for_ext(ext)
            filename = str(media.get("filename") or media.get("name") or "wecom_file")
            return cache_document_from_bytes(raw, filename), \
                   mimetypes.guess_type(filename)[0] or "application/octet-stream"

        # URL download
        url = str(media.get("url") or "").strip()
        if not url:
            logger.warning("[WeCom] Media has no base64 or url, keys: %s", list(media.keys()))
            return None

        logger.debug("[WeCom] Downloading %s from %s, media keys: %s", kind, url[:80], list(media.keys()))

        try:
            raw, headers = await self._download_remote_bytes(url, ABSOLUTE_MAX_BYTES)
        except Exception as e:
            logger.warning("[WeCom] Failed to download %s from %s: %s", kind, url[:80], e)
            return None

        # Decrypt if needed
        aes_key = str(media.get("aeskey") or media.get("aes_key") or "").strip()
        if aes_key:
            logger.info("[WeCom] Attempting decryption for %s (aes_key len=%d, data len=%d)", 
                        kind, len(aes_key), len(raw))
            try:
                raw = _decrypt_file_bytes(raw, aes_key)
                logger.info("[WeCom] Successfully decrypted %s", kind)
            except Exception as e:
                logger.warning("[WeCom] Failed to decrypt %s: %s", kind, e)
                # Check if raw data is actually an unencrypted image
                if kind == "image":
                    ext = _detect_image_ext(raw)
                    logger.info("[WeCom] Checking if raw data is valid unencrypted image, detected ext=%s", ext)
                    if ext in (".png", ".jpg", ".gif", ".webp"):
                        logger.info("[WeCom] Using raw image data (appears unencrypted)")
                        return cache_image_from_bytes(raw, ext), _mime_for_ext(ext)
                    # Try to detect image from URL extension
                    url_ext = Path(urlparse(url).path).suffix.lower()
                    if url_ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                        logger.info("[WeCom] Using raw data based on URL extension %s", url_ext)
                        return cache_image_from_bytes(raw, url_ext), _mime_for_ext(url_ext)
                logger.warning("[WeCom] Cannot use raw data, returning None")
                return None

        content_type = str(headers.get("content-type") or "").split(";")[0].strip() \
                       or "application/octet-stream"
        if kind == "image":
            # Detect image format from actual binary data (especially after decryption)
            detected_ext = _detect_image_ext(raw)
            if detected_ext in (".png", ".jpg", ".gif", ".webp"):
                logger.info("[WeCom] Image detected as %s format from binary data", detected_ext)
                return cache_image_from_bytes(raw, detected_ext), _mime_for_ext(detected_ext)
            ext = _guess_extension(url, content_type, fallback=detected_ext)
            return cache_image_from_bytes(raw, ext), content_type or _mime_for_ext(ext)

        filename = _guess_filename(url, headers.get("content-disposition"), content_type)
        return cache_document_from_bytes(raw, filename), content_type

    async def _download_remote_bytes(self, url: str,
                                      max_bytes: int) -> tuple[bytes, dict[str, str]]:
        """Download a URL with size limits. Returns (bytes, headers_dict)."""
        if not httpx:
            # Fallback to aiohttp
            return await self._download_with_aiohttp(url, max_bytes)

        client = self._http_client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        created = client is not self._http_client
        try:
            async with client.stream("GET", url, headers={"User-Agent": "XiheAgent/1.0"}) as resp:
                resp.raise_for_status()
                headers = {k.lower(): v for k, v in resp.headers.items()}
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise ValueError(f"Download exceeds {max_bytes} bytes")
                return bytes(data), headers
        finally:
            if created:
                await client.aclose()

    async def _download_with_aiohttp(self, url: str,
                                      max_bytes: int) -> tuple[bytes, dict[str, str]]:
        """Fallback download using aiohttp."""
        if not self._session:
            raise RuntimeError("No HTTP session available")
        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            data = await resp.read()
            if len(data) > max_bytes:
                raise ValueError(f"Download exceeds {max_bytes} bytes")
            return data, headers

    def _is_duplicate(self, msg_id: str) -> bool:
        now = time.time()
        if len(self._seen_messages) > DEDUP_MAX:
            cutoff = now - DEDUP_WINDOW
            self._seen_messages = {k: v for k, v in self._seen_messages.items() if v > cutoff}
        if msg_id in self._seen_messages:
            return True
        self._seen_messages[msg_id] = now
        return False

    async def _open_connection(self):
        await self._cleanup_ws()
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(
            self._ws_url, heartbeat=HEARTBEAT_INTERVAL * 2, timeout=CONNECT_TIMEOUT)
        req_id = self._new_req_id("sub")
        await self._send_json({
            "cmd": CMD_SUBSCRIBE,
            "headers": {"req_id": req_id},
            "body": {"bot_id": self._bot_id, "secret": self._secret},
        })
        auth = await self._wait_for_handshake(req_id)
        errcode = auth.get("errcode", 0)
        if errcode not in (0, None):
            self._ws_connected.clear()
            raise RuntimeError(f"Auth failed: {auth.get('errmsg')} (errcode={errcode})")
        self._ws_connected.set()
        # Flush any messages queued during disconnection
        if self._pending_outbound:
            self._flush_task = asyncio.create_task(self._flush_pending_outbound())

    async def _wait_for_handshake(self, req_id: str) -> dict:
        deadline = asyncio.get_event_loop().time() + CONNECT_TIMEOUT
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError("Subscribe timeout")
            msg = await asyncio.wait_for(self._ws.receive(), timeout=remaining)
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = self._parse_json(msg.data)
                if payload and payload.get("cmd") == CMD_PING:
                    continue
                if self._payload_req_id(payload) == req_id:
                    return payload
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                raise RuntimeError("WebSocket closed during auth")

    async def _cleanup_ws(self):
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _listen_loop(self):
        backoff_idx = 0
        while self._running:
            try:
                await self._read_events()
                backoff_idx = 0
            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._running:
                    return
                self._ws_connected.clear()
                logger.warning("[WeCom] WS error: %s", e)
                self._fail_pending(RuntimeError("Connection interrupted"))
                delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
                backoff_idx += 1
                await asyncio.sleep(delay)
                try:
                    await self._open_connection()
                    backoff_idx = 0
                    logger.info("[WeCom] Reconnected")
                except Exception as re:
                    logger.warning("[WeCom] Reconnect failed: %s", re)

    async def _read_events(self):
        while self._running and self._ws and not self._ws.closed:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = self._parse_json(msg.data)
                if payload:
                    # Dispatch concurrently — don't block the read loop while
                    # a previous message is being processed. Without this, the
                    # read loop can't see new messages (like /stop) until the
                    # current turn finishes. Session lock handles per-session
                    # ordering; /stop bypasses the lock via interrupt.
                    asyncio.create_task(self._dispatch(payload))
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                              aiohttp.WSMsgType.ERROR):
                raise RuntimeError("WebSocket closed")

    async def _heartbeat_loop(self):
        try:
            while self._running:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if self._ws and not self._ws.closed:
                    try:
                        await self._send_json({
                            "cmd": CMD_PING,
                            "headers": {"req_id": self._new_req_id("ping")},
                            "body": {},
                        })
                    except Exception:
                        # heartbeat loss is the early signal of a half-dead
                        # WSS connection (messages silently stop arriving)
                        logger.debug("wecom heartbeat ping failed", exc_info=True)
        except asyncio.CancelledError:
            pass

    async def _dispatch(self, payload: dict):
        req_id = self._payload_req_id(payload)
        cmd = str(payload.get("cmd") or "")
        if req_id and req_id in self._pending_responses and cmd != CMD_CALLBACK:
            future = self._pending_responses.get(req_id)
            if future and not future.done():
                future.set_result(payload)
            return
        if cmd == CMD_CALLBACK:
            await self._on_callback(payload)

    async def _send_json(self, payload: dict):
        if not self._ws or self._ws.closed:
            raise RuntimeError("WebSocket not connected")
        await self._ws.send_json(payload)

    async def _send_request(self, cmd: str, body: dict,
                             timeout: float = REQUEST_TIMEOUT) -> dict:
        if not self._ws or self._ws.closed:
            raise RuntimeError("WebSocket not connected")
        req_id = self._new_req_id(cmd)
        future = asyncio.get_event_loop().create_future()
        self._pending_responses[req_id] = future
        try:
            await self._send_json({"cmd": cmd, "headers": {"req_id": req_id}, "body": body})
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending_responses.pop(req_id, None)

    async def _send_reply_request(self, reply_req_id: str, body: dict,
                                   timeout: float = REQUEST_TIMEOUT) -> dict:
        if not self._ws or self._ws.closed:
            raise RuntimeError("WebSocket not connected")
        future = asyncio.get_event_loop().create_future()
        self._pending_responses[reply_req_id] = future
        try:
            await self._send_json({
                "cmd": CMD_RESPONSE,
                "headers": {"req_id": reply_req_id},
                "body": body,
            })
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending_responses.pop(reply_req_id, None)

    def _fail_pending(self, exc: Exception):
        for _, f in list(self._pending_responses.items()):
            if not f.done():
                f.set_exception(exc)
        self._pending_responses.clear()

    async def _flush_pending_outbound(self):
        """Send messages that were queued during WS disconnection."""
        if not self._pending_outbound:
            return
        logger.info("[WeCom] Flushing %d queued outbound messages", len(self._pending_outbound))
        while self._pending_outbound:
            item = self._pending_outbound[0]
            try:
                if item["type"] == "text":
                    response = await self._send_request(CMD_SEND, {
                        "chatid": item["chat_id"],
                        "msgtype": "markdown",
                        "markdown": {"content": item["content"][:MAX_MESSAGE_LENGTH]},
                    })
                    errcode = response.get("errcode", 0)
                    if errcode not in (0, None):
                        logger.warning("[WeCom] Flush send failed: errcode=%s", errcode)
                        break  # WS may have issues, stop flushing
                    logger.info("[WeCom] Flushed text to %s (%d chars)", item["chat_id"], len(item["content"]))
                else:
                    logger.warning("[WeCom] Skipping unknown flush type: %s", item["type"])
            except Exception as e:
                logger.warning("[WeCom] Flush failed, stopping: %s", e)
                break  # WS went down again, stop flushing — messages stay in queue
            self._pending_outbound.popleft()  # Remove only after successful send
        if self._pending_outbound:
            logger.warning("[WeCom] %d messages still queued after flush", len(self._pending_outbound))

    @staticmethod
    def _new_req_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"

    @staticmethod
    def _payload_req_id(payload: dict) -> str:
        h = payload.get("headers")
        return str(h.get("req_id", "")) if isinstance(h, dict) else ""

    @staticmethod
    def _parse_json(raw) -> Optional[dict]:
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else None
        except Exception:
            return None


def _decode_base64(data: str) -> bytes:
    payload = data.split(",", 1)[-1].strip()
    return base64.b64decode(payload)


def _detect_image_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def _mime_for_ext(ext: str) -> str:
    return mimetypes.types_map.get(ext.lower(), "image/jpeg")


def _guess_extension(url: str, content_type: str, fallback: str = ".jpg") -> str:
    # Skip generic types that produce useless extensions like .bin
    if content_type and content_type not in ("application/octet-stream", "text/plain"):
        ext = mimetypes.guess_extension(content_type)
        if ext:
            return ext
    path_ext = Path(urlparse(url).path).suffix
    return path_ext if path_ext else fallback


def _guess_filename(url: str, content_disposition: str = None,
                    content_type: str = "") -> str:
    if content_disposition:
        match = re.search(r'filename="?([^";]+)"?', content_disposition)
        if match:
            return match.group(1)
    name = Path(urlparse(url).path).name or "document"
    if "." not in name:
        name += mimetypes.guess_extension(content_type) or ".bin"
    return name


def _normalize_content_type(content_type: str, filename: str) -> str:
    normalized = str(content_type or "").split(";")[0].strip().lower()
    guessed = mimetypes.guess_type(filename)[0] or ""
    if filename.lower().endswith(".amr"):
        guessed = "audio/amr"
    if not normalized or normalized in ("application/octet-stream", "text/plain"):
        return guessed or "application/octet-stream"
    return normalized


def _detect_wecom_media_type(content_type: str) -> str:
    mime = str(content_type or "").strip().lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/") or mime == "application/ogg":
        return "voice"
    return "file"


def _apply_file_size_limits(file_size: int, detected_type: str,
                             content_type: str = None) -> dict:
    """Check size limits and decide whether to downgrade or reject."""
    file_size_mb = file_size / (1024 * 1024)
    ct = str(content_type or "").strip().lower()

    if file_size > ABSOLUTE_MAX_BYTES:
        return {
            "final_type": detected_type, "rejected": True,
            "reject_reason": f"文件大小 {file_size_mb:.1f}MB 超过 20MB 限制，无法发送",
            "downgraded": False, "downgrade_note": None,
        }

    if detected_type == "image" and file_size > IMAGE_MAX_BYTES:
        return {
            "final_type": "file", "rejected": False,
            "reject_reason": None, "downgraded": True,
            "downgrade_note": f"图片 {file_size_mb:.1f}MB 超过 10MB 限制，已转为文件发送",
        }

    if detected_type == "voice":
        if ct and ct not in VOICE_SUPPORTED_MIMES:
            return {
                "final_type": "file", "rejected": False,
                "reject_reason": None, "downgraded": True,
                "downgrade_note": f"语音格式 {ct} 不支持(仅AMR)，已转为文件发送",
            }
        if file_size > VOICE_MAX_BYTES:
            return {
                "final_type": "file", "rejected": False,
                "reject_reason": None, "downgraded": True,
                "downgrade_note": f"语音 {file_size_mb:.1f}MB 超过 2MB 限制，已转为文件发送",
            }

    return {
        "final_type": detected_type, "rejected": False,
        "reject_reason": None, "downgraded": False, "downgrade_note": None,
    }


def _looks_like_url(source: str) -> bool:
    parsed = urlparse(str(source or ""))
    return parsed.scheme in ("http", "https")


def _decrypt_file_bytes(encrypted_data: bytes, aes_key: str) -> bytes:
    """Decrypt WeCom AES-CBC encrypted media file."""
    if not encrypted_data or not aes_key:
        raise ValueError("encrypted_data and aes_key are required")

    # Handle aes_key with or without base64 padding
    aes_key_stripped = aes_key.strip()
    missing_padding = len(aes_key_stripped) % 4
    if missing_padding:
        aes_key_stripped += '=' * (4 - missing_padding)
    
    try:
        key = base64.b64decode(aes_key_stripped)
    except Exception as e:
        raise ValueError(f"Failed to decode aes_key: {e}")
    
    if len(key) != 32:
        raise ValueError(f"Invalid AES key length: {len(key)} (expected 32)")

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise RuntimeError("cryptography package required for WeCom media decryption")

    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    decryptor = cipher.decryptor()
    decrypted = decryptor.update(encrypted_data) + decryptor.finalize()

    # PKCS#7 unpadding
    pad_len = decrypted[-1]
    if pad_len < 1 or pad_len > 32 or pad_len > len(decrypted):
        raise ValueError(f"Invalid PKCS#7 padding: {pad_len}")
    if any(b != pad_len for b in decrypted[-pad_len:]):
        raise ValueError("PKCS#7 padding mismatch")
    return decrypted[:-pad_len]


def _raise_for_error(response: dict, operation: str):
    errcode = response.get("errcode", 0)
    if errcode not in (0, None):
        errmsg = response.get("errmsg", "unknown error")
        raise RuntimeError(f"{operation} failed: errcode={errcode} {errmsg}")
