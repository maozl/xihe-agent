"""Tool result persistence — prevents context overflow from large tool outputs.

Three-layer defense (adapted from Hermes):

1. **Per-tool output cap**: Each tool truncates its own output (existing behavior).
2. **Per-result persistence** (maybe_persist_tool_result): If a single tool result
   exceeds a threshold, write the full content to a temp file and replace the
   in-context content with a preview + file path. The model can use read_file
   to access the full output later.
3. **Per-turn aggregate budget** (enforce_turn_budget): If the total chars of
   all tool results in a single turn exceed the budget, spill the largest
   results to disk until under budget.
"""

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_RESULT_SIZE = 15_000
DEFAULT_TURN_BUDGET = 80_000
DEFAULT_PREVIEW_SIZE = 1_000

# Tools that should NEVER have their results persisted (prevents infinite loops)
PINNED_INF = {"read_file"}  # read_file → persist → read_file → persist ...

PERSISTED_TAG = "<persisted-output>"
PERSISTED_CLOSING_TAG = "</persisted-output>"

_STORAGE_DIR = Path(tempfile.gettempdir()) / "xihe-agent-results"

# Spilled results are only re-read within their turn (read_file on the path in
# the persisted stub) — a week is far past any useful lifetime.
_TTL_DAYS = 7
_swept = False


def _sweep_stale() -> None:
    """Drop side-store files older than the TTL. The store lives in the OS
    temp dir, which Windows never cleans on its own — without this, spills
    accumulate forever."""
    global _swept
    _swept = True
    cutoff = time.time() - _TTL_DAYS * 86400
    try:
        for p in _STORAGE_DIR.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _ensure_storage_dir() -> Path:
    _STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    if not _swept:
        _sweep_stale()
    return _STORAGE_DIR


def _generate_preview(content: str, max_chars: int = DEFAULT_PREVIEW_SIZE) -> tuple[str, bool]:
    """Truncate at last newline within max_chars. Returns (preview, has_more)."""
    if len(content) <= max_chars:
        return content, False
    truncated = content[:max_chars]
    last_nl = truncated.rfind("\n")
    if last_nl > max_chars // 2:
        truncated = truncated[:last_nl + 1]
    return truncated, True


def _write_to_file(content: str, tool_use_id: str) -> Optional[str]:
    """Write content to a temp file. Returns file path on success, None on failure."""
    try:
        storage_dir = _ensure_storage_dir()
        file_path = storage_dir / f"{tool_use_id}.txt"
        file_path.write_text(content, encoding="utf-8")
        return str(file_path)
    except Exception as e:
        logger.warning("Failed to persist tool result %s: %s", tool_use_id, e)
        return None


def _build_persisted_message(
    preview: str,
    has_more: bool,
    original_size: int,
    file_path: str,
) -> str:
    """Build the <persisted-output> replacement block."""
    size_kb = original_size / 1024
    if size_kb >= 1024:
        size_str = f"{size_kb / 1024:.1f} MB"
    else:
        size_str = f"{size_kb:.1f} KB"

    msg = f"{PERSISTED_TAG}\n"
    msg += f"This tool result was too large ({original_size:,} characters, {size_str}).\n"
    msg += f"Full output saved to: {file_path}\n"
    msg += "Use the read_file tool with offset and limit to access specific sections.\n\n"
    msg += f"Preview (first {len(preview)} chars):\n"
    msg += preview
    if has_more:
        msg += "\n..."
    msg += f"\n{PERSISTED_CLOSING_TAG}"
    return msg


def maybe_persist_tool_result(
    content: str,
    tool_name: str,
    tool_use_id: str,
    threshold: int = DEFAULT_RESULT_SIZE,
) -> str:
    """If content exceeds threshold, persist to file and return preview + path.

    Args:
        content: Raw tool result string.
        tool_name: Name of the tool (for pinned check).
        tool_use_id: Unique ID for this tool call (used as filename).
        threshold: Character threshold for persistence.

    Returns:
        Original content if small, or <persisted-output> replacement.
    """
    # Pinned tools never get persisted (prevents infinite loop)
    if tool_name in PINNED_INF:
        # For read_file, apply inline truncation instead
        if len(content) > threshold:
            preview, has_more = _generate_preview(content, max_chars=threshold)
            return (
                preview + "\n\n"
                f"[Truncated: tool response was {len(content):,} chars. "
                f"Use read_file with offset/limit to read specific sections.]"
            )
        return content

    if len(content) <= threshold:
        return content

    file_path = _write_to_file(content, tool_use_id)
    if file_path:
        preview, has_more = _generate_preview(content)
        logger.info(
            "Persisted large tool result: %s (%s, %d chars -> %s)",
            tool_name, tool_use_id, len(content), file_path,
        )
        return _build_persisted_message(preview, has_more, len(content), file_path)

    # Fallback: inline truncation
    logger.info(
        "Inline-truncating large tool result: %s (%d chars)",
        tool_name, len(content),
    )
    preview, has_more = _generate_preview(content, max_chars=threshold)
    return (
        preview + "\n\n"
        f"[Truncated: tool response was {len(content):,} chars. "
        f"Full output could not be saved to disk.]"
    )


def enforce_turn_budget(
    tool_messages: list[dict],
    budget: int = DEFAULT_TURN_BUDGET,
) -> list[dict]:
    """If total chars across all tool results exceed budget, spill largest to disk.

    Mutates the list in-place and returns it. Rows whose ``role`` is neither
    ``tool`` nor absent are ignored — user/assistant messages may share the
    slice (callers pass the whole turn tail), and spilling those would replace
    a user's words with a persisted-output stub.
    """
    candidates = []
    total_size = 0
    for i, msg in enumerate(tool_messages):
        if msg.get("role") not in (None, "tool"):
            continue
        content = msg.get("content", "")
        size = len(content)
        total_size += size
        # Only consider messages that aren't already persisted
        if PERSISTED_TAG not in content and size > DEFAULT_PREVIEW_SIZE * 2:
            candidates.append((i, size))

    if total_size <= budget:
        return tool_messages

    # Sort by size descending — spill largest first
    candidates.sort(key=lambda x: x[1], reverse=True)

    for idx, size in candidates:
        if total_size <= budget:
            break
        msg = tool_messages[idx]
        content = msg["content"]
        tool_use_id = msg.get("tool_call_id", f"budget_{idx}")

        replacement = maybe_persist_tool_result(
            content=content,
            tool_name="__budget__",
            tool_use_id=tool_use_id,
            threshold=0,  # force persist regardless of individual size
        )
        if replacement != content:
            total_size -= size
            total_size += len(replacement)
            tool_messages[idx]["content"] = replacement
            logger.info(
                "Budget enforcement: persisted tool result %s (%d chars)",
                tool_use_id, size,
            )

    return tool_messages
