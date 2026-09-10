"""Text-to-speech tool — convert text to audio via API."""

import logging
import base64
from pathlib import Path
from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

_aux = None


def set_auxiliary(aux):
    """Wire up the auxiliary client for TTS."""
    global _aux
    _aux = aux


def _check_tts() -> bool:
    return _aux is not None and _aux.is_available("tts")


def _text_to_speech(args: dict, **kw) -> str:
    text = args.get("text", "")
    if not text:
        return tool_error("text is required")

    voice = args.get("voice", "alloy")
    model = args.get("model", "tts-1")
    save_path = args.get("save_path", "")

    if not _check_tts():
        return tool_error("TTS not available (requires TTS_ENABLED=true)")

    try:
        response = _aux.text_to_speech(
            text=text,
            voice=voice,
            model=model,
        )
        if not response:
            return tool_error("TTS returned no response")

        if save_path:
            p = Path(save_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(response.content)
            return tool_result(success=True, path=str(p), size_bytes=len(response.content))

        b64 = base64.b64encode(response.content).decode("ascii")
        return tool_result(audio_b64=b64[:200] + "...", size_bytes=len(response.content), format="mp3")
    except Exception as e:
        return tool_error(f"TTS failed: {e}")


registry.register(
    name="text_to_speech",
    schema={
        "type": "function",
        "function": {
            "name": "text_to_speech",
            "description": (
                "Convert text to speech audio. Requires TTS_ENABLED=true environment variable. "
                "Save to file or return as base64."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to convert to speech"},
                    "voice": {
                        "type": "string",
                        "enum": ["alloy", "echo", "fable", "onyx", "nova", "shimmer"],
                        "description": "Voice to use (default: alloy)",
                    },
                    "model": {"type": "string", "description": "TTS model (default: tts-1)"},
                    "save_path": {"type": "string", "description": "File path to save audio (optional)"},
                },
                "required": ["text"],
            },
        },
    },
    handler=lambda args, **kw: _text_to_speech(args, **kw),
    check_fn=_check_tts,
    toolset="media",
)
