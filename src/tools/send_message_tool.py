"""Send message tool — proactive messaging and media delivery to chats."""

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# Annotation-only use — platforms/__init__ eagerly imports every adapter
# (wecom→websockets chain, ~870ms), which load_all_tools must not pay.
if TYPE_CHECKING:
    from platforms.base import BasePlatformAdapter

_adapter: Optional["BasePlatformAdapter"] = None
_main_loop: Optional[object] = None

# Thread-safe queue for media that should be sent to the current chat
# after the agent's chat() call returns. Gateway drains this queue.
_pending_media_lock = threading.Lock()
_pending_media: list[dict] = []


def set_adapter(adapter):
    global _adapter
    _adapter = adapter


def set_loop(loop):
    global _main_loop
    _main_loop = loop


def get_pending_media() -> list[dict]:
    """Return and clear the pending media queue. Called by gateway after chat()."""
    with _pending_media_lock:
        items = _pending_media[:]
        _pending_media.clear()
    return items


def _check_send_message() -> bool:
    return _adapter is not None


def _run_async(coro, timeout: float = 30.0):
    """Run an async coroutine from a sync worker thread via the main event loop."""
    import asyncio

    loop = _main_loop
    if loop and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)

    try:
        current_loop = asyncio.get_running_loop()
        if current_loop and current_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, current_loop)
            return future.result(timeout=timeout)
    except RuntimeError:
        pass

    return asyncio.run(coro)


def _send_message(args: dict, **kw) -> str:
    chat_id = args.get("chat_id", "")
    message = args.get("message", "")
    if not chat_id or not message:
        return tool_error("chat_id and message are required")

    if not _adapter:
        return tool_error("Platform adapter not available")

    try:
        _run_async(_adapter.send(chat_id, message))
        logger.info("Proactive message sent to %s", chat_id)
        return tool_result(success=True, chat_id=chat_id)
    except Exception as e:
        logger.exception("Failed to send message")
        return tool_error(f"Failed to send message: {e}")


def _send_image(args: dict, **kw) -> str:
    """Send an image to the current chat or a specific chat.

    For the current conversation: adds to a pending media queue that
    the gateway drains after chat() returns. No async needed in tool.

    For other chats: schedules the async send via the main event loop.
    """
    context = kw.get("context", {}) or {}
    chat_id = args.get("chat_id") or context.get("chat_id", "")
    image_path = args.get("image_path", "")
    caption = args.get("caption", "")

    logger.info("send_image called: path=%s chat_id=%s", image_path, chat_id)

    if not image_path:
        return tool_error("image_path is required")

    if not image_path.startswith(("http://", "https://")):
        p = Path(image_path).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        if not p.exists():
            return tool_error(f"File not found: {p}")
        image_path = str(p)
        logger.info("Resolved image path: %s", image_path)

    current_chat = context.get("chat_id", "")
    is_current_chat = (not chat_id) or (chat_id == current_chat)
    logger.info("is_current_chat=%s chat_id=%s current_chat=%s", is_current_chat, chat_id, current_chat)

    if is_current_chat:
        with _pending_media_lock:
            _pending_media.append({
                "type": "image",
                "path": image_path,
                "caption": caption,
                "reply_to_msg_id": context.get("msg_id", ""),
            })
        logger.info("Image queued for current chat: %s", image_path)
        return tool_result(success=True, image_path=image_path,
                           note="Image will be sent after response")
    else:
        if not _adapter:
            return tool_error("Platform adapter not available")
        if not hasattr(_adapter, 'send_image'):
            return tool_error("Current platform does not support sending images")

        try:
            result = _run_async(_adapter.send_image(chat_id, image_path, caption=caption))
            success = getattr(result, 'success', False)
            if success:
                logger.info("Image sent to %s: %s", chat_id, image_path)
                return tool_result(success=True, chat_id=chat_id, image_path=image_path)
            else:
                error = getattr(result, 'error', 'unknown error')
                logger.warning("Image send failed: %s", error)
                return tool_error(f"Failed to send image: {error}")
        except Exception as e:
            logger.exception("Failed to send image")
            return tool_error(f"Failed to send image: {e}")


registry.register(
    name="send_message",
    schema={
        "type": "function",
        "function": {
            "name": "send_message",
            "description": (
                "Send a message to a specific chat or user proactively. "
                "Use when you need to notify someone or send results to another conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string", "description": "Target chat ID or user ID"},
                    "message": {"type": "string", "description": "Message content to send"},
                },
                "required": ["chat_id", "message"],
            },
        },
    },
    handler=lambda args, **kw: _send_message(args, **kw),
    check_fn=_check_send_message,
    toolset="communication",
    subagent_blocked=True,
)

registry.register(
    name="send_image",
    schema={
        "type": "function",
        "function": {
            "name": "send_image",
            "description": (
                "Send an image to the current chat or a specific chat. "
                "Supports local file paths (e.g., C:\\Users\\xxx\\Desktop\\image.png) or URLs. "
                "If chat_id is not provided, sends to the current conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "Local file path or URL of the image to send"
                    },
                    "chat_id": {
                        "type": "string",
                        "description": "Target chat ID (optional, uses current chat if not provided)"
                    },
                    "caption": {
                        "type": "string",
                        "description": "Optional caption for the image"
                    },
                },
                "required": ["image_path"],
            },
        },
    },
    handler=lambda args, **kw: _send_image(args, **kw),
    path_params=("image_path",),
    check_fn=_check_send_message,
    toolset="communication",
    subagent_blocked=True,
)
