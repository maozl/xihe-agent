"""Simplified Feishu/Lark platform adapter.

Supports:
- WebSocket long connection (via lark_oapi SDK)
- Direct-message and group @mention text receive/send
- Inbound image/file caching
- Outbound image, file, voice via lark_oapi upload APIs
- Message editing (for streaming support)
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateFileRequest,
        CreateFileRequestBody,
        CreateImageRequest,
        CreateImageRequestBody,
        CreateMessageRequest,
        CreateMessageRequestBody,
        GetMessageResourceRequest,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
        UpdateMessageRequest,
        UpdateMessageRequestBody,
    )
    from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    from lark_oapi.ws import Client as FeishuWSClient

    FEISHU_AVAILABLE = True
except ImportError:
    FEISHU_AVAILABLE = False
    lark = None
    EventDispatcherHandler = None
    FeishuWSClient = None
    FEISHU_DOMAIN = None
    LARK_DOMAIN = None

from platforms.base import (
    BasePlatformAdapter,
    MessageCallback,
    MessageEvent,
    SendResult,
    cache_image_from_bytes,
    cache_document_from_bytes,
)

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 8000
DEDUP_TTL_SECONDS = 3600
_MENTION_RE = re.compile(r"@_user_\d+")
_MULTISPACE_RE = re.compile(r"[ \t]{2,}")
_MARKDOWN_HINT_RE = re.compile(
    r"(^#{1,6}\s)|(^\s*[-*]\s)|(^\s*\d+\.\s)|(```)|(`[^`\n]+`)|(\*\*[^*\n].+?\*\*)|(\[[^\]]+\]\([^)]+\))",
    re.MULTILINE,
)


def _build_markdown_post_payload(content: str) -> str:
    return json.dumps(
        {"zh_cn": {"content": [[{"tag": "md", "text": content}]]}},
        ensure_ascii=False,
    )


def _normalize_inbound_text(text: str) -> str:
    text = _MENTION_RE.sub(" ", text or "")
    text = _MULTISPACE_RE.sub(" ", text)
    return text.strip()


def _load_feishu_payload(raw: str) -> dict:
    try:
        d = json.loads(raw) if raw else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


class FeishuAdapter(BasePlatformAdapter):
    """Simplified Feishu/Lark bot adapter (WebSocket mode)."""

    name = "feishu"

    def __init__(self, config: dict, on_message: MessageCallback = None):
        if not FEISHU_AVAILABLE:
            raise ImportError("lark_oapi is required: pip install lark_oapi")

        super().__init__(config, on_message)
        self._app_id = config.get("app_id", "")
        self._app_secret = config.get("app_secret", "")
        self._domain_name = config.get("domain", "") or "feishu"
        self._domain_name = self._domain_name.strip().lower()

        self._client: Optional[Any] = None
        self._ws_client: Optional[Any] = None
        self._ws_future: Optional[asyncio.Future] = None
        self._ws_thread_loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._seen_message_ids: Dict[str, float] = {}

    async def start(self) -> bool:
        if not self._app_id or not self._app_secret:
            logger.error("[Feishu] FEISHU_APP_ID and FEISHU_APP_SECRET are required")
            return False

        self._loop = asyncio.get_running_loop()

        try:
            domain = FEISHU_DOMAIN if self._domain_name != "lark" else LARK_DOMAIN
            self._client = lark.Client.builder() \
                .app_id(self._app_id) \
                .app_secret(self._app_secret) \
                .domain(domain) \
                .log_level(lark.LogLevel.WARNING) \
                .build()

            event_handler = self._build_event_handler()
            if not event_handler:
                logger.error("[Feishu] Failed to build event handler")
                return False

            self._ws_client = FeishuWSClient(
                app_id=self._app_id,
                app_secret=self._app_secret,
                log_level=lark.LogLevel.INFO,
                event_handler=event_handler,
                domain=domain,
            )

            self._ws_future = self._loop.run_in_executor(
                None, self._run_ws_client, self._ws_client,
            )

            self._running = True
            logger.info("[Feishu] Connected via WebSocket (%s)", self._domain_name)
            return True
        except Exception as e:
            logger.error("[Feishu] Connection failed: %s", e, exc_info=True)
            return False

    async def stop(self):
        self._running = False
        # The WS client runs in its own thread — cancel its future
        if self._ws_thread_loop and not self._ws_thread_loop.is_closed():
            tasks = [t for t in asyncio.all_tasks(self._ws_thread_loop) if not t.done()]
            for task in tasks:
                task.cancel()
            self._ws_thread_loop.call_soon_threadsafe(self._ws_thread_loop.stop)

        if self._ws_future:
            try:
                await asyncio.wait_for(asyncio.shield(self._ws_future), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                pass

        self._ws_future = None
        self._ws_thread_loop = None
        self._loop = None
        logger.info("[Feishu] Disconnected")

    def _build_event_handler(self):
        if EventDispatcherHandler is None:
            return None
        return (
            EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_event)
            .build()
        )

    def _on_message_event(self, data: Any) -> None:
        """Normalize Feishu inbound events into MessageEvent."""
        if not self._loop:
            return
        future = asyncio.run_coroutine_threadsafe(
            self._handle_message_data(data), self._loop,
        )
        future.add_done_callback(lambda f: f.exception() if f.exception() else None)

    async def _handle_message_data(self, data: Any) -> None:
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        sender_id_obj = getattr(sender, "sender_id", None) if sender else None

        if not message or not sender_id_obj:
            return

        message_id = str(getattr(message, "message_id", "") or "")
        if not message_id or self._is_duplicate(message_id):
            return

        # Skip bot-originated messages
        if getattr(sender, "sender_type", "") == "bot":
            return

        chat_type = getattr(message, "chat_type", "p2p")
        chat_id = str(getattr(message, "chat_id", "") or "")
        user_id = str(getattr(sender_id_obj, "open_id", "") or
                      getattr(sender_id_obj, "user_id", "") or "")

        raw_content = str(getattr(message, "content", "") or "")
        raw_type = str(getattr(message, "message_type", "") or "").lower()
        text = self._extract_text(raw_type, raw_content)

        media_urls, media_types = [], []
        if raw_type in ("image", "file", "audio"):
            media_urls, media_types = await self._cache_message_resources(
                message_id, raw_type, raw_content)

        # Group messages: only respond to @mentions
        if chat_type != "p2p" and not text.strip():
            return

        is_group = chat_type != "p2p"
        effective_chat_type = "group" if is_group else "dm"

        logger.info("[Feishu] %s from %s: %s (media=%d)",
                    "Group" if is_group else "DM", chat_id, text[:100], len(media_urls))

        await self._emit_message(MessageEvent(
            text=text, chat_id=chat_id, msg_id=message_id,
            sender_id=user_id, is_group=is_group,
            chat_type=effective_chat_type,
            raw=data,
            media_urls=media_urls, media_types=media_types,
        ))

    @staticmethod
    def _extract_text(message_type: str, raw_content: str) -> str:
        payload = _load_feishu_payload(raw_content)
        if message_type == "text":
            return _normalize_inbound_text(str(payload.get("text", "") or ""))
        if message_type == "post":
            return _normalize_feishu_post(payload)
        if message_type == "image":
            return "[用户发送了一张图片]"
        if message_type == "file":
            filename = str(payload.get("file_name", "") or "文件")
            return f"[用户发送了文件: {filename}]"
        if message_type == "audio":
            return "[用户发送了语音]"
        return ""

    async def _cache_message_resources(
        self, message_id: str, message_type: str, raw_content: str,
    ) -> tuple[list[str], list[str]]:
        """Download and cache inbound media resources."""
        paths, types = [], []
        payload = _load_feishu_payload(raw_content)

        if message_type == "image":
            image_key = str(payload.get("image_key", "") or "")
            if image_key:
                cached = await self._download_resource(
                    message_id, image_key, "image")
                if cached:
                    paths.append(cached)
                    types.append("image/jpeg")

        elif message_type == "file":
            file_key = str(payload.get("file_key", "") or "")
            filename = str(payload.get("file_name", "") or "document")
            if file_key:
                cached = await self._download_resource(
                    message_id, file_key, "file", filename)
                if cached:
                    paths.append(cached)
                    ct = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                    types.append(ct)

        elif message_type == "audio":
            file_key = str(payload.get("file_key", "") or "")
            if file_key:
                cached = await self._download_resource(
                    message_id, file_key, "file", "audio.ogg")
                if cached:
                    paths.append(cached)
                    types.append("audio/ogg")

        return paths, types

    async def _download_resource(
        self, message_id: str, file_key: str, resource_type: str,
        filename: str = None,
    ) -> Optional[str]:
        """Download a Feishu message resource and cache locally."""
        if not self._client:
            return None
        try:
            request = GetMessageResourceRequest.builder() \
                .message_id(message_id) \
                .file_key(file_key) \
                .type(resource_type) \
                .build()

            response = await asyncio.to_thread(
                self._client.im.v1.message_resource.get, request)

            if not response.success():
                logger.warning("[Feishu] Resource download failed: %s", response.msg)
                return None

            data = response.file
            if not data:
                return None

            if resource_type == "image":
                ext = ".png"
                return cache_image_from_bytes(data, ext)

            name = filename or file_key
            return cache_document_from_bytes(data, name)

        except Exception as e:
            logger.warning("[Feishu] Failed to download resource %s: %s", file_key, e)
            return None

    async def send(self, chat_id: str, content: str,
                   reply_to_msg_id: str = None) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="Not connected")

        msg_type, payload = self._build_outbound_payload(content)

        try:
            if reply_to_msg_id:
                body = ReplyMessageRequestBody.builder() \
                    .content(payload) \
                    .msg_type(msg_type) \
                    .build()
                request = ReplyMessageRequest.builder() \
                    .message_id(reply_to_msg_id) \
                    .request_body(body) \
                    .build()
                response = await asyncio.to_thread(
                    self._client.im.v1.message.reply, request)
            else:
                body = CreateMessageRequestBody.builder() \
                    .receive_id(chat_id) \
                    .msg_type(msg_type) \
                    .content(payload) \
                    .uuid(str(uuid.uuid4())) \
                    .build()
                request = CreateMessageRequest.builder() \
                    .receive_id_type("chat_id") \
                    .request_body(body) \
                    .build()
                response = await asyncio.to_thread(
                    self._client.im.v1.message.create, request)

            if not response.success():
                # Fallback: post → plain text
                if msg_type == "post":
                    plain = json.dumps({"text": content}, ensure_ascii=False)
                    if reply_to_msg_id:
                        body = ReplyMessageRequestBody.builder() \
                            .content(plain).msg_type("text").build()
                        request = ReplyMessageRequest.builder() \
                            .message_id(reply_to_msg_id) \
                            .request_body(body).build()
                        response = await asyncio.to_thread(
                            self._client.im.v1.message.reply, request)
                    else:
                        body = CreateMessageRequestBody.builder() \
                            .receive_id(chat_id).msg_type("text") \
                            .content(plain).uuid(str(uuid.uuid4())).build()
                        request = CreateMessageRequest.builder() \
                            .receive_id_type("chat_id") \
                            .request_body(body).build()
                        response = await asyncio.to_thread(
                            self._client.im.v1.message.create, request)

                if not response.success():
                    return SendResult(success=False, error=response.msg or "send failed")

            msg_id = ""
            data = response.data
            if data:
                msg_id = str(getattr(data, "message_id", "") or "")

            return SendResult(success=True, message_id=msg_id)

        except Exception as e:
            logger.error("[Feishu] Send error: %s", e)
            return SendResult(success=False, error=str(e))

    async def edit_message(self, chat_id: str, message_id: str,
                           content: str) -> SendResult:
        """Edit a previously sent message."""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        msg_type, payload = self._build_outbound_payload(content)

        try:
            body = UpdateMessageRequestBody.builder() \
                .msg_type(msg_type) \
                .content(payload) \
                .build()
            request = UpdateMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(body) \
                .build()
            response = await asyncio.to_thread(
                self._client.im.v1.message.update, request)

            if not response.success() and msg_type == "post":
                plain = json.dumps({"text": content}, ensure_ascii=False)
                body = UpdateMessageRequestBody.builder() \
                    .msg_type("text").content(plain).build()
                request = UpdateMessageRequest.builder() \
                    .message_id(message_id) \
                    .request_body(body).build()
                response = await asyncio.to_thread(
                    self._client.im.v1.message.update, request)

            if not response.success():
                return SendResult(success=False, error=response.msg or "edit failed")
            return SendResult(success=True)
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_image(self, chat_id: str, image_url: str,
                         caption: str = None,
                         reply_to_msg_id: str = None) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="Not connected")

        try:
            image_data = await self._load_media_data(image_url)
            if not image_data:
                return SendResult(success=False, error="Failed to load image")

            body = CreateImageRequestBody.builder() \
                .image_type("message") \
                .image(image_data) \
                .build()
            request = CreateImageRequest.builder() \
                .request_body(body) \
                .build()
            upload_resp = await asyncio.to_thread(
                self._client.im.v1.image.create, request)

            if not upload_resp.success():
                return SendResult(success=False, error=upload_resp.msg or "image upload failed")

            image_key = str(getattr(upload_resp.data, "image_key", "") or "")
            if not image_key:
                return SendResult(success=False, error="No image_key returned")

            payload = json.dumps({"image_key": image_key}, ensure_ascii=False)
            return await self._send_raw(chat_id, "image", payload, reply_to_msg_id)

        except Exception as e:
            logger.error("[Feishu] Send image error: %s", e)
            # Fallback to text
            text = f"{caption}\n{image_url}" if caption else image_url
            return await self.send(chat_id, text, reply_to_msg_id=reply_to_msg_id)

    async def send_document(self, chat_id: str, file_path: str,
                            caption: str = None,
                            reply_to_msg_id: str = None) -> SendResult:
        if not self._client:
            return SendResult(success=False, error="Not connected")

        try:
            file_data = await self._load_media_data(file_path)
            if not file_data:
                return SendResult(success=False, error="Failed to load file")

            filename = Path(file_path).name or "document"
            ext = Path(filename).suffix.lower()
            upload_type = "stream"
            if ext in (".pdf",):
                upload_type = "pdf"
            elif ext in (".doc", ".docx"):
                upload_type = "doc"
            elif ext in (".xls", ".xlsx"):
                upload_type = "xls"
            elif ext in (".ppt", ".pptx"):
                upload_type = "ppt"

            body = CreateFileRequestBody.builder() \
                .file_type(upload_type) \
                .file_name(filename) \
                .file(file_data) \
                .build()
            request = CreateFileRequest.builder() \
                .request_body(body) \
                .build()
            upload_resp = await asyncio.to_thread(
                self._client.im.v1.file.create, request)

            if not upload_resp.success():
                return SendResult(success=False, error=upload_resp.msg or "file upload failed")

            file_key = str(getattr(upload_resp.data, "file_key", "") or "")
            if not file_key:
                return SendResult(success=False, error="No file_key returned")

            payload = json.dumps({"file_key": file_key}, ensure_ascii=False)
            result = await self._send_raw(chat_id, "file", payload, reply_to_msg_id)

            if caption:
                await self.send(chat_id, caption, reply_to_msg_id=reply_to_msg_id)

            return result

        except Exception as e:
            logger.error("[Feishu] Send document error: %s", e)
            return SendResult(success=False, error=str(e))

    async def send_voice(self, chat_id: str, audio_path: str,
                         caption: str = None,
                         reply_to_msg_id: str = None) -> SendResult:
        # Feishu doesn't have a native voice type — send as file
        return await self.send_document(chat_id, audio_path, caption=caption,
                                        reply_to_msg_id=reply_to_msg_id)

    async def _send_raw(self, chat_id: str, msg_type: str, content: str,
                        reply_to: str = None) -> SendResult:
        """Send a raw Feishu message."""
        if reply_to:
            body = ReplyMessageRequestBody.builder() \
                .content(content).msg_type(msg_type).build()
            request = ReplyMessageRequest.builder() \
                .message_id(reply_to).request_body(body).build()
            response = await asyncio.to_thread(
                self._client.im.v1.message.reply, request)
        else:
            body = CreateMessageRequestBody.builder() \
                .receive_id(chat_id).msg_type(msg_type) \
                .content(content).uuid(str(uuid.uuid4())).build()
            request = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(body).build()
            response = await asyncio.to_thread(
                self._client.im.v1.message.create, request)

        if not response.success():
            return SendResult(success=False, error=response.msg or "send failed")
        return SendResult(success=True)

    @staticmethod
    def _build_outbound_payload(content: str) -> tuple[str, str]:
        if _MARKDOWN_HINT_RE.search(content):
            return "post", _build_markdown_post_payload(content)
        return "text", json.dumps({"text": content}, ensure_ascii=False)

    async def _load_media_data(self, source: str) -> Optional[bytes]:
        """Load media data from local path or URL."""
        source = source.strip()
        parsed = Path(source)

        if source.startswith(("http://", "https://")):
            try:
                import httpx
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                    resp = await client.get(source)
                    resp.raise_for_status()
                    return resp.content
            except ImportError:
                pass
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(source, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        resp.raise_for_status()
                        return await resp.read()
            except ImportError:
                logger.error("[Feishu] No HTTP client available for downloads")
                return None
            except Exception as e:
                logger.warning("[Feishu] Failed to download %s: %s", source[:80], e)
                return None

        # Local file
        local_path = parsed.expanduser()
        if not local_path.is_absolute():
            local_path = (Path.cwd() / local_path).resolve()
        if not local_path.exists():
            logger.warning("[Feishu] File not found: %s", local_path)
            return None
        return local_path.read_bytes()

    def _is_duplicate(self, message_id: str) -> bool:
        now = time.time()
        # Clean expired entries
        if len(self._seen_message_ids) > 1000:
            cutoff = now - DEDUP_TTL_SECONDS
            self._seen_message_ids = {
                k: v for k, v in self._seen_message_ids.items() if v > cutoff
            }
        if message_id in self._seen_message_ids:
            return True
        self._seen_message_ids[message_id] = now
        return False

    def _run_ws_client(self, ws_client: Any) -> None:
        """Run the official Lark WS client in its own thread."""
        import lark_oapi.ws.client as ws_client_module

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        ws_client_module.loop = loop
        self._ws_thread_loop = loop

        try:
            ws_client.start()
        except Exception:
            logger.warning("[Feishu] WS client exited with error", exc_info=True)
        finally:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            try:
                loop.stop()
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
            self._ws_thread_loop = None


def _normalize_feishu_post(payload: dict) -> str:
    """Extract text from a Feishu post message."""
    parts = []
    for locale_key in ("zh_cn", "en_us", "ja_jp"):
        locale_data = payload.get(locale_key)
        if not isinstance(locale_data, dict):
            continue
        title = str(locale_data.get("title", "") or "").strip()
        if title:
            parts.append(title)
        for row in locale_data.get("content", []) or []:
            if not isinstance(row, list):
                continue
            for element in row:
                if not isinstance(element, dict):
                    continue
                tag = element.get("tag", "")
                if tag == "text":
                    text = str(element.get("text", "") or "").strip()
                    if text:
                        parts.append(text)
                elif tag == "md":
                    text = str(element.get("text", "") or "").strip()
                    if text:
                        parts.append(text)
                elif tag == "a":
                    text = str(element.get("text", "") or "").strip()
                    href = str(element.get("href", "") or "").strip()
                    if text:
                        parts.append(f"{text}({href})" if href else text)
        break  # Only process the first matching locale

    return _normalize_inbound_text("\n".join(parts))
