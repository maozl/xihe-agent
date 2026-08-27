"""Abstract platform adapter — all platforms implement this interface.

To add a new platform:
  1. Create platforms/<name>.py with a class inheriting BasePlatformAdapter
  2. Implement start(), stop(), send()
  3. Register in platforms/__init__.py PLATFORM_REGISTRY
  4. Add config to config.yaml platforms section
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional
from core.config import AGENT_HOME

logger = logging.getLogger(__name__)


@dataclass
class MessageEvent:
    """Normalized inbound message from any platform."""
    text: str
    chat_id: str
    msg_id: str
    sender_id: str = ""
    is_group: bool = False
    session_key: str = ""
    chat_type: str = ""        # "dm", "group", "channel", "thread" — auto-derived from is_group if empty
    thread_id: str = ""        # Sub-topic/thread ID (Telegram topics, Discord threads, etc.)
    chat_name: str = ""        # Human-readable chat name
    user_name: str = ""        # Sender display name
    raw: Optional[dict] = None
    # Media attachments (local cached file paths for vision/agent access)
    media_urls: list[str] = field(default_factory=list)
    media_types: list[str] = field(default_factory=list)

    @property
    def effective_chat_type(self) -> str:
        """Resolve chat_type, falling back to is_group heuristic."""
        if self.chat_type:
            return self.chat_type
        return "group" if self.is_group else "dm"

    def to_session_source(self, platform: str) -> "SessionSource":
        """Convert to SessionSource for session key generation."""
        from core.session import SessionSource
        return SessionSource(
            platform=platform,
            chat_id=self.chat_id,
            chat_name=self.chat_name or None,
            chat_type=self.effective_chat_type,
            user_id=self.sender_id or None,
            user_name=self.user_name or None,
            thread_id=self.thread_id or None,
        )


@dataclass
class SendResult:
    """Result of an outbound send operation."""
    success: bool
    error: str = ""
    message_id: str = ""


# Type alias for the message callback
MessageCallback = Callable[..., Coroutine[Any, Any, None]]

# Media cache directories
_IMAGE_CACHE_DIR = AGENT_HOME / "cache" / "images"
_DOC_CACHE_DIR = AGENT_HOME / "cache" / "documents"


def cache_image_from_bytes(data: bytes, ext: str = ".jpg") -> str:
    """Save raw image bytes to cache and return the absolute file path."""
    _IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    import uuid
    filename = f"img_{uuid.uuid4().hex[:12]}{ext}"
    filepath = _IMAGE_CACHE_DIR / filename
    filepath.write_bytes(data)
    return str(filepath)


def cache_document_from_bytes(data: bytes, filename: str) -> str:
    """Save raw document bytes to cache and return the absolute file path."""
    _DOC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    filepath = _DOC_CACHE_DIR / filename
    filepath.write_bytes(data)
    return str(filepath)


class BasePlatformAdapter(ABC):
    """All platform adapters implement this interface."""

    # Subclass sets this — used for config lookup and logging
    name: str = "base"

    def __init__(self, config: dict, on_message: MessageCallback = None):
        """
        Args:
            config: Platform-specific config dict (from config.yaml platforms.<name>)
            on_message: Async callback invoked on inbound messages.
                        Signature: async (event: MessageEvent) -> None
        """
        self._config = config
        self._on_message = on_message
        # Out-of-band interrupt handler (set by the gateway via
        # set_interrupt_handler). When a platform detects a stop intent it
        # calls this directly instead of routing the message through the
        # queue — a separate control plane so the interrupt can never get
        # stuck behind a running turn. Signature: async (event) -> None.
        self._on_interrupt = None
        # Steer handler (set by the gateway). When a message arrives while a
        # turn is running, the adapter calls this instead of queueing it as a
        # new turn; the gateway injects it into the active turn. Signature:
        #   async (event: MessageEvent) -> bool
        # True = steered (handled), False = no active turn (adapter queues it).
        self._on_steer = None
        self._running = False

    @abstractmethod
    async def start(self) -> bool:
        """Connect to the platform. Return True on success."""

    @abstractmethod
    async def stop(self):
        """Disconnect and clean up."""

    @abstractmethod
    async def send(self, chat_id: str, content: str,
                  reply_to_msg_id: str = None) -> SendResult:
        """Send a text message to a chat."""

    async def send_image(self, chat_id: str, image_url: str,
                         caption: str = None,
                         reply_to_msg_id: str = None) -> SendResult:
        """Send an image. Default: send URL as text."""
        text = f"{caption}\n{image_url}" if caption else image_url
        return await self.send(chat_id, text, reply_to_msg_id=reply_to_msg_id)

    async def send_voice(self, chat_id: str, audio_path: str,
                         caption: str = None,
                         reply_to_msg_id: str = None) -> SendResult:
        """Send a voice message. Default: send path as text."""
        text = f"{caption}\n{audio_path}" if caption else audio_path
        return await self.send(chat_id, text, reply_to_msg_id=reply_to_msg_id)

    async def send_document(self, chat_id: str, file_path: str,
                            caption: str = None,
                            reply_to_msg_id: str = None) -> SendResult:
        """Send a file. Default: not supported."""
        return SendResult(success=False, error="File sending not supported")

    @property
    def running(self) -> bool:
        return self._running

    async def _emit_message(self, event: MessageEvent):
        """Invoke the message callback — subclasses call this on inbound messages."""
        if self._on_message:
            await self._on_message(event)

    def set_interrupt_handler(self, handler: MessageCallback) -> None:
        """Register the out-of-band interrupt handler (called by the gateway).

        When set, the adapter routes stop intents to this handler directly,
        bypassing the normal message queue so the interrupt takes effect
        immediately even while a turn is running. Signature:
            async (event: MessageEvent) -> None
        """
        self._on_interrupt = handler

    def set_steer_handler(self, handler: MessageCallback) -> None:
        """Register the steer handler (called by the gateway).

        When set, a message arriving while a turn is running is routed here
        instead of the queue, so it's injected into the active turn (the model
        reads it at the next iteration boundary) rather than waiting to become
        a new turn. Signature:
            async (event: MessageEvent) -> bool
        Return True if steered/handled, False to fall through to the queue
        (e.g. no active turn, so it should start a new one).
        """
        self._on_steer = handler
