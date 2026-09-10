"""Session search tool — search across historical conversation transcripts."""

import logging
from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# Lazy reference to SessionDB — set by main.py at startup
_db = None


def set_session_db(db):
    """Wire up the SessionDB instance (called by main.py)."""
    global _db
    _db = db


def _check_session_search() -> bool:
    return _db is not None


def _session_search(args: dict, **kw) -> str:
    query = args.get("query", "")
    if not query:
        return tool_error("query is required")
    limit = int(args.get("limit", 5))

    if not _db:
        return tool_error("Session database not available")

    results = _db.search(query, limit=limit)
    if not results:
        return tool_result(results=[], message="No matching sessions found")

    return tool_result(results=results, count=len(results))


registry.register(
    name="session_search",
    schema={
        "type": "function",
        "function": {
            "name": "session_search",
            "description": (
                "Search past conversation sessions for relevant context. "
                "Use when the user references something from a previous conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results (default: 5)"},
                },
                "required": ["query"],
            },
        },
    },
    handler=lambda args, **kw: _session_search(args, **kw),
    check_fn=_check_session_search,
    toolset="memory",
    read_only=True,
)
