"""Clarify tool — ask the user a clarification question."""

import logging
from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)


def _clarify(question: str = "", options: list = None, reason: str = "", **kw) -> str:
    if not isinstance(question, str) or not question.strip():
        return tool_error("question must be a non-empty string")
    question = question.strip()

    options = options or []
    if not isinstance(options, list):
        return tool_error("options must be a list of strings")
    # The schema says strings; coerce anyway — model-provided args can drift.
    options = list(dict.fromkeys(
        str(o).strip() for o in options if str(o).strip()))[:10]

    result = {
        "action": "clarify",
        "question": question,
        # Nothing consumes "clarify" programmatically — the model is the only
        # reader, and without this it may treat the result as data and keep
        # calling tools (ssh_tool returns it mid-connection).
        "instruction": (
            "Stop now and ask the user this question. "
            "Do not proceed with other tools until they answer."
        ),
    }
    if options:
        result["options"] = options
    if reason and isinstance(reason, str):
        result["reason"] = reason.strip()

    logger.info("Clarification requested: %s", question)
    return tool_result(result)


registry.register(
    name="clarify",
    schema={
        "type": "function",
        "function": {
            "name": "clarify",
            "description": (
                "Ask the user a clarification question when the request is ambiguous. "
                "Use when you need more information to proceed accurately."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The clarification question to ask the user"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Suggested answer options (optional)",
                    },
                    "reason": {"type": "string", "description": "Why clarification is needed (optional)"},
                },
                "required": ["question"],
            },
        },
    },
    handler=lambda args, **kw: _clarify(
        question=args.get("question", ""), options=args.get("options"),
        reason=args.get("reason", ""), **kw),
    toolset="communication",
    subagent_blocked=True,
    read_only=True,
)
