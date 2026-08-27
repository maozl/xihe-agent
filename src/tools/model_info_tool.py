"""Model info tool — let the agent query its own runtime model on demand.

When asked "what model are you using", the agent calls this tool to get the
accurate, live answer — instead of guessing from stale identity/CLAUDE.md text
or having the model name baked into the system prompt (which would bust prompt
cache on every switch and create an injection surface).

Returns: {current_model, available_models}.
"""

import logging

from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)


def _check_model_info() -> bool:
    return True


def _model_info(args: dict, **kw) -> str:
    """Return the current effective model + available models from the catalog."""
    parent_agent = kw.get("parent_agent")
    ctx = kw.get("context") or {}
    session_key = ctx.get("session_key")

    if parent_agent:
        current = parent_agent._effective_model(session_key)
        try:
            available = parent_agent.list_models()
        except Exception:
            available = [{"name": current, "current": True}]
    else:
        # Fallback: read from config (no agent context — e.g., CLI early init)
        try:
            from core.config import load_config
            cfg = load_config()
            current = cfg.get("model", "unknown")
            models_cfg = cfg.get("models", {})
            available = [
                {"name": n, "current": n == current, **(m if isinstance(m, dict) else {})}
                for n, m in models_cfg.items()
            ] or [{"name": current, "current": True}]
        except Exception as e:
            return tool_error(f"Failed to get model info: {e}")

    return tool_result(
        current_model=current,
        available_models=available,
        message=(
            f"Current model: {current}. Switch with /model <name> "
            f"(persists per chat). Available: {', '.join(m.get('name','?') for m in available)}."
        ),
    )


registry.register(
    name="model_info",
    toolset="base",
    schema={
        "type": "function",
        "function": {
            "name": "model_info",
            "description": (
                "Get the current runtime model and the list of available models. "
                "Use this when the user asks which model you're using or wants to "
                "know what models are available — always call this rather than "
                "guessing from memory."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    handler=lambda args, **kw: _model_info(args, **kw),
    check_fn=_check_model_info,
    read_only=True,
)
