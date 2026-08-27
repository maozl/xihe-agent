"""Image generation tool — AI image generation via API."""

import logging
import base64
from pathlib import Path
from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

_aux = None


def set_auxiliary(aux):
    """Wire up the auxiliary client for image generation."""
    global _aux
    _aux = aux


def _check_image_gen() -> bool:
    return _aux is not None and _aux.is_available("image_gen")


def _image_generate(args: dict, **kw) -> str:
    prompt = args.get("prompt", "")
    if not prompt:
        return tool_error("prompt is required")

    size = args.get("size", "1024x1024")
    n = min(int(args.get("n", 1)), 4)
    style = args.get("style", "vivid")
    save_path = args.get("save_path", "")

    if not _check_image_gen():
        return tool_error("Image generation not available (requires IMAGE_GEN_ENABLED=true)")

    try:
        response = _aux.generate_image(
            prompt=prompt,
            size=size,
            n=n,
            style=style,
        )
        if not response:
            return tool_error("Image generation returned no response")

        results = []
        for i, img in enumerate(response.data):
            if img.url:
                results.append({"url": img.url, "index": i})
            elif img.b64_json:
                if save_path:
                    p = Path(save_path)
                    if n > 1:
                        p = p.with_name(f"{p.stem}_{i}{p.suffix}")
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(base64.b64decode(img.b64_json))
                    results.append({"path": str(p), "index": i})
                else:
                    results.append({"b64_length": len(img.b64_json), "index": i})

        return tool_result(images=results, count=len(results))
    except Exception as e:
        return tool_error(f"Image generation failed: {e}")


registry.register(
    name="image_generate",
    schema={
        "type": "function",
        "function": {
            "name": "image_generate",
            "description": (
                "Generate images from text descriptions using AI. "
                "Requires IMAGE_GEN_ENABLED=true environment variable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Image description"},
                    "size": {
                        "type": "string",
                        "enum": ["256x256", "512x512", "1024x1024", "1792x1024", "1024x1792"],
                        "description": "Image size (default: 1024x1024)",
                    },
                    "n": {"type": "integer", "description": "Number of images to generate (1-4, default: 1)"},
                    "style": {
                        "type": "string",
                        "enum": ["vivid", "natural"],
                        "description": "Image style (default: vivid)",
                    },
                    "save_path": {"type": "string", "description": "Local path to save generated image (optional)"},
                },
                "required": ["prompt"],
            },
        },
    },
    handler=lambda args, **kw: _image_generate(args, **kw),
    check_fn=_check_image_gen,
    toolset="media",
)
