"""Vision tools — image analysis via LLM vision capability.

Supports:
  - Local file paths (base64 encoded)
  - HTTP/HTTPS URLs (downloaded then base64 encoded)
  - Multiple model fallback: vision_model → main model
  - Reasoning model extraction (content, reasoning, reasoning_content)

Reference: Hermes vision_tools with provider routing + base64 fallback.
"""

import base64
import json
import logging
import uuid
from pathlib import Path
from typing import Optional

import httpx

from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# Lazy reference set by init_agent
_aux = None


def set_auxiliary(aux):
    """Wire up the auxiliary client for vision calls."""
    global _aux
    _aux = aux


def _check_vision() -> bool:
    return _aux is not None and _aux.is_available("vision")


def pick_image_tool() -> str:
    """Return the tool name the agent should use to read an image, by availability.

    Priority: ``vision_analyze`` (when a vision_model is configured) >
    ``image_ocr`` (when paddleocr is installed) > ``""`` (none available).

    Used by gateways/platforms to hint the agent with the correct tool name
    instead of hardcoding ``vision_analyze`` (which is hidden when
    ``vision_model`` is empty).
    """
    if _check_vision():
        return "vision_analyze"
    try:
        from tools.ocr_tool import _check_ocr
        if _check_ocr():
            return "image_ocr"
    except Exception:
        pass
    return ""


def _detect_mime_type(image_path: Path) -> Optional[str]:
    """Detect MIME type from file header (not extension)."""
    try:
        with image_path.open("rb") as f:
            header = f.read(64)

        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if header.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if header.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if header.startswith(b"BM"):
            return "image/bmp"
        if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
            return "image/webp"
    except Exception:
        pass

    # Fallback to extension
    ext = image_path.suffix.lower()
    mime_map = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    }
    return mime_map.get(ext)


def _resolve_image_to_data_url(image: str) -> str:
    """Resolve image source to a base64 data URL.

    Accepts local file paths or HTTP/HTTPS URLs.
    Downloads remote images before encoding.
    """
    p = Path(image).expanduser().resolve()
    if p.is_file():
        mime = _detect_mime_type(p)
        if not mime:
            raise ValueError(f"Unsupported image format: {p.suffix}")
        data = p.read_bytes()
        if len(data) > 10 * 1024 * 1024:
            raise ValueError(f"Image too large: {len(data)} bytes (max 10MB)")
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{b64}"

    if image.startswith(("http://", "https://")):
        try:
            resp = httpx.get(
                image,
                timeout=30,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; XiheAgent/1.0)",
                    "Accept": "image/*,*/*;q=0.8",
                },
            )
            resp.raise_for_status()
            data = resp.content
            if len(data) > 10 * 1024 * 1024:
                raise ValueError(f"Image too large: {len(data)} bytes (max 10MB)")
            # Detect MIME from content, fallback to Content-Type header
            mime = _detect_mime_from_bytes(data) or resp.headers.get(
                "content-type", "image/jpeg"
            ).split(";")[0].strip()
            b64 = base64.b64encode(data).decode("ascii")
            return f"data:{mime};base64,{b64}"
        except httpx.HTTPError as e:
            raise ValueError(f"Failed to download image: {e}")

    raise ValueError(f"Invalid image source: {image}. Use a file path or HTTP URL.")


def _detect_mime_from_bytes(data: bytes) -> Optional[str]:
    """Detect MIME type from binary header."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _call_vision_with_fallback(messages: list[dict]) -> Optional[str]:
    """Call vision API with model fallback.

    Try order:
      1. Dedicated vision_model (if configured)
      2. Main model
    """
    from core.auxiliary_client import extract_content_or_reasoning

    if not _aux:
        return None

    response = _aux.call_vision(messages=messages, max_tokens=2000)
    if response:
        content = extract_content_or_reasoning(response)
        if content:
            return content
        logger.warning("Vision model returned empty content, trying main model")

    response = _aux.call_llm(
        task="",  # skip vision-specific model resolution
        messages=messages,
        max_tokens=2000,
    )
    if response:
        content = extract_content_or_reasoning(response)
        if content:
            return content

    return None


def _classify_vision_error(error: Exception) -> tuple[str, str]:
    """Classify a vision error for better user feedback.

    Returns (error_type, user_message).
    """
    err_str = str(error).lower()
    if any(hint in err_str for hint in (
        "402", "insufficient", "payment required", "credits", "billing",
    )):
        return "billing", (
            "Insufficient credits or payment required. Please top up your "
            f"API provider account and try again. Error: {error}"
        )
    if any(hint in err_str for hint in (
        "does not support", "not support image", "multimodal",
        "invalid_request", "image_url", "unrecognized request argument",
        "image input",
    )):
        return "unsupported", (
            f"Vision not supported by current model. Error: {error}"
        )
    return "generic", f"Vision analysis failed: {error}"


def _vision_analyze(args: dict, **kw) -> str:
    image = args.get("image", "")
    prompt = args.get("prompt", "Describe this image in detail.")
    if not image:
        return tool_error("image (path or URL) is required")

    if not _check_vision():
        return tool_error("Vision not available — auxiliary client not configured")

    try:
        image_url = _resolve_image_to_data_url(image)
    except ValueError as e:
        return tool_error(str(e))

    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_url}},
        ],
    }]

    try:
        analysis = _call_vision_with_fallback(messages)

        # Retry once on empty content (reasoning-only response)
        if not analysis:
            logger.warning("Vision LLM returned empty content, retrying once")
            analysis = _call_vision_with_fallback(messages)

        if not analysis:
            return tool_error("Vision analysis returned no response")
        from tools.redact import redact_sensitive_text
        analysis = redact_sensitive_text(analysis)
        return tool_result(analysis=analysis)
    except Exception as e:
        error_type, user_msg = _classify_vision_error(e)
        hint = None
        if error_type == "unsupported":
            hint = "Set vision_model in config.yaml to a vision-capable model (e.g. gpt-4o)"
        elif error_type == "billing":
            hint = "Check your API provider account balance"
        return tool_error(user_msg, hint=hint)


def describe_image_sync(image_path: str, prompt: str = None) -> str:
    """Synchronous image description for gateway auto-processing.

    Returns description text or error message. Never raises.
    """
    if not _check_vision():
        return "(vision not available)"

    try:
        result = _vision_analyze({
            "image": image_path,
            "prompt": prompt or "Briefly describe this image. Focus on text, objects, and key visual information.",
        })
        parsed = json.loads(result)
        if parsed.get("analysis"):
            return parsed["analysis"]
        return parsed.get("error", "(vision analysis failed)")
    except Exception as e:
        return f"(vision error: {e})"


registry.register(
    name="vision_analyze",
    schema={
        "type": "function",
        "function": {
            "name": "vision_analyze",
            "description": (
                "Analyze an image using vision AI. Provide an image path or URL "
                "and a question about the image."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image": {"type": "string", "description": "Image file path or URL"},
                    "prompt": {"type": "string", "description": "Question or instruction about the image (default: describe)"},
                },
                "required": ["image"],
            },
        },
    },
    handler=lambda args, **kw: _vision_analyze(args, **kw),
    path_params=("image",),
    check_fn=_check_vision,
    toolset="media",
    read_only=True,
)
