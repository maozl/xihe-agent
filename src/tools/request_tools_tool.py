"""Request additional toolsets mid-turn.

When the agent discovers it needs tools (browser/media/scheduler) that weren't
loaded by the initial toolset classification, it calls this to expand. The
agent loop re-reads schemas each iteration, so expanded tools appear on the
next API call within the same turn.
"""

import logging

from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)


def _check_request_tools() -> bool:
    return True


def _request_tools(args: dict, **kw) -> str:
    """Expand the active toolset for this turn."""
    from core.toolsets import CONDITIONAL_TOOLSETS

    requested = args.get("toolsets", [])
    if isinstance(requested, str):
        requested = [requested]

    valid = [t for t in requested if t in CONDITIONAL_TOOLSETS]
    invalid = [t for t in requested if t not in CONDITIONAL_TOOLSETS]

    parent_agent = kw.get("parent_agent")
    if parent_agent and hasattr(parent_agent, "_expansion_state"):
        parent_agent._expansion_state.update(valid)
        logger.info("Toolset expansion requested: %s", valid)
    else:
        return tool_error(
            "Cannot expand toolsets: no active agent context. "
            "The tools will be available if you retry."
        )

    msg = f"Loaded: {', '.join(valid)}." if valid else "No valid toolsets requested."
    if invalid:
        msg += f" Unknown (ignored): {', '.join(invalid)}."
        msg += f" Valid options: {', '.join(CONDITIONAL_TOOLSETS.keys())}."
    msg += " The tools are now available — call them directly."
    return tool_result(success=True, added=valid, message=msg)


registry.register(
    name="request_tools",
    toolset="http",
    schema={
        "type": "function",
        "function": {
            "name": "request_tools",
            "description": (
                "Request additional toolsets for the current task. Available: "
                "web (browser/HTTP), media (vision/OCR/TTS), scheduler (cron). "
                "Call this BEFORE attempting tasks that need these capabilities "
                "if they aren't already in your tool list. "
                "IMPORTANT: if a skill or task references a tool by name "
                "(e.g. browser_navigate, browser_eval) but it is NOT in your "
                "current tool list, call request_tools to load it FIRST — do NOT "
                "use execute_code to call these tools (they are agent tools, not "
                "sandbox functions). Tools become available on your next action."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "toolsets": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["web", "media", "scheduler"]},
                        "description": "Toolset names to load (e.g. [\"web\"]).",
                    },
                },
                "required": ["toolsets"],
            },
        },
    },
    handler=lambda args, **kw: _request_tools(args, **kw),
    check_fn=_check_request_tools,
    read_only=True,
)
