"""Memory tool — persistent key-value memory across sessions.

Stores memories in ~/.xihe-agent/memories.json as a flat JSON dict.
Memories are loaded into the system prompt at conversation start.

Includes injection/exfiltration scanning for content that gets injected
into the system prompt.
"""

import json
import logging
import re
from pathlib import Path
from typing import Optional
from tools import registry, tool_error, tool_result
from core.config import AGENT_HOME

logger = logging.getLogger(__name__)

_MEMORIES_FILE = AGENT_HOME / "memories.json"

_MEMORY_THREAT_PATTERNS = [
    # Prompt injection
    (r'ignore\s+(previous|all|above|prior)\s+instructions', "prompt_injection"),
    (r'you\s+are\s+now\s+', "role_hijack"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
    (r'act\s+as\s+(if|though)\s+you\s+(have\s+no|don\'t\s+have)\s+(restrictions|limits|rules)', "bypass_restrictions"),
    # Exfiltration via curl/wget with secrets
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_curl"),
    (r'wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_wget"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)', "read_secrets"),
    # Persistence via shell rc / SSH
    (r'authorized_keys', "ssh_backdoor"),
    (r'\$HOME/\.ssh|\~/\.ssh', "ssh_access"),
]

_INVISIBLE_CHARS = {
    '​', '‌', '‍', '⁠', '﻿',
    '‪', '‫', '‬', '‭', '‮',
}


def _scan_memory_content(content: str) -> Optional[str]:
    """Scan memory content for injection/exfil patterns. Returns error string if blocked."""
    for char in _INVISIBLE_CHARS:
        if char in content:
            return f"Blocked: content contains invisible unicode character U+{ord(char):04X} (possible injection)."

    for pattern, pid in _MEMORY_THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return (
                f"Blocked: content matches threat pattern '{pid}'. "
                "Memory entries are injected into the system prompt and must not "
                "contain injection or exfiltration payloads."
            )

    return None


def _load_memories() -> dict:
    if _MEMORIES_FILE.exists():
        try:
            return json.loads(_MEMORIES_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_memories(data: dict):
    _MEMORIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _MEMORIES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _check_memory() -> bool:
    return True


def _memory_read(args: dict, **kw) -> str:
    action = args.get("action", "list")
    key = args.get("key", "")
    memories = _load_memories()

    if action == "get":
        if not key:
            return tool_error("key is required for get")
        val = memories.get(key)
        if val is None:
            return tool_error(f"Key not found: {key}")
        return tool_result(key=key, value=val)

    elif action == "list":
        return tool_result(memories=memories, count=len(memories))

    elif action == "search":
        query = args.get("query", key).lower()
        if not query:
            return tool_error("query or key is required for search")
        results = {k: v for k, v in memories.items() if query in k.lower() or query in str(v).lower()}
        return tool_result(results=results, count=len(results))

    else:
        return tool_error(f"Unknown action: {action}. Use: get, list, search (save/delete: memory_manage)")


def _memory_manage(args: dict, **kw) -> str:
    action = args.get("action", "")
    key = args.get("key", "")
    value = args.get("value", "")

    if action == "save":
        if not key:
            return tool_error("key is required for save")
        scan_error = _scan_memory_content(value)
        if scan_error:
            return tool_error(scan_error)
        memories = _load_memories()
        memories[key] = value
        _save_memories(memories)
        logger.info("Memory saved: %s", key)
        return tool_result(success=True, action="save", key=key)

    elif action == "delete":
        if not key:
            return tool_error("key is required for delete")
        memories = _load_memories()
        if key in memories:
            del memories[key]
            _save_memories(memories)
            return tool_result(success=True, action="delete", key=key)
        return tool_error(f"Key not found: {key}")

    else:
        return tool_error(f"Unknown action: {action}. Use: save, delete (reads: memory)")


def build_memory_prompt() -> str:
    """Build memory context block for the system prompt.

    For small memory sets (< 3000 chars total): inject full values — the agent
    gets all context inline, no extra tool calls needed.
    For larger sets: inject summaries (key + 80-char preview) to avoid prompt
    bloat; the agent calls memory(action='get', key='...') for full details.

    Skips entries that fail injection scanning (defensive — prevents dirty data
    from prior sessions without scanning from leaking).
    """
    memories = _load_memories()
    if not memories:
        return ""

    # Filter out entries that fail injection scan
    safe = {}
    for k, v in memories.items():
        if _scan_memory_content(v):
            logger.warning("Skipping memory key '%s' — failed injection scan", k)
            continue
        safe[k] = v
    if not safe:
        return ""

    total_chars = sum(len(v) for v in safe.values())

    if total_chars < 3000:
        lines = ["## Memory\n"]
        for k, v in safe.items():
            lines.append(f"- {k}: {v}")
        return "\n".join(lines)
    else:
        lines = ["## Memory (summaries — use memory(action='get', key='...') for full content)\n"]
        for k, v in safe.items():
            preview = v[:80].replace("\n", " ")
            if len(v) > 80:
                preview += "..."
            lines.append(f"- {k}: {preview}")
        return "\n".join(lines)


registry.register(
    name="memory",
    schema={
        "type": "function",
        "function": {
            "name": "memory",
            "description": (
                "Read persistent memory across sessions — user preferences, environment "
                "details, tool quirks, durable facts. Memory is loaded every session. "
                "Read-only (get/list/search); saving and deleting go through memory_manage."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["get", "list", "search"],
                        "description": "Action to perform (default: list)",
                    },
                    "key": {"type": "string", "description": "Memory key"},
                    "query": {"type": "string", "description": "Search query (for search action)"},
                },
                "required": ["action"],
            },
        },
    },
    handler=lambda args, **kw: _memory_read(args, **kw),
    check_fn=_check_memory,
    read_only=True,
    toolset="base",
)

registry.register(
    name="memory_manage",
    schema={
        "type": "function",
        "function": {
            "name": "memory_manage",
            "description": (
                "Write persistent memory: save durable facts (preferences, conventions, "
                "tool quirks) with memory_manage(action='save'), or delete stale entries. "
                "Do NOT save task progress or temporary state."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["save", "delete"],
                        "description": "Action to perform",
                    },
                    "key": {"type": "string", "description": "Memory key"},
                    "value": {"type": "string", "description": "Value to save (for save action)"},
                },
                "required": ["action"],
            },
        },
    },
    handler=lambda args, **kw: _memory_manage(args, **kw),
    check_fn=_check_memory,
    toolset="memory",
)
