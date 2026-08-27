"""Derived dispatch tools for configured specialist agents (``agents/*.yaml``).

The main agent routes domain work to a configured specialist by calling
``run_<slug>_agent(goal, context)``. The specialist runs the full layered
prompt with its persona as the identity layer — unlike delegate_task's
wholesale prompt override, it keeps tool-use enforcement, behavior rules, and
the guidance layers that match its actual tools.
"""

import logging
import time

from tools import registry, tool_result, tool_error

logger = logging.getLogger(__name__)


def _configured_defs():
    from core.agent_defs import load_agent_defs

    return load_agent_defs()


def _persona_prompt(agent_def) -> str:
    parts = [agent_def.persona]
    from core.toolsets import resolve_multiple_toolsets
    # Write access is what needs the prefix discipline — memory reads moved to
    # the base toolset every agent has. toolsets None = ["*"] (unrestricted).
    if agent_def.toolsets is None or "memory_manage" in resolve_multiple_toolsets(agent_def.toolsets):
        parts.append(
            f"Your long-term memory is namespaced: prefix every memory entry "
            f"you save with 'agent:{agent_def.slug}:' so your knowledge stays "
            f"separate from other agents'."
        )
    return "\n\n".join(p for p in parts if p)


def _build_agent_instance(agent_def, parent_agent):
    from core.agent import XiheAgent
    from tools.delegate_tool import _resolve_workspace_hint

    workspace = _resolve_workspace_hint(parent_agent)

    # Overlay the specialist's connection overrides (model/base_url/api_key/
    # max_iterations) onto the main config — unset keys inherit, so a
    # specialist with its own gateway gets a client pointed at it while aux
    # (compression/titles) keeps the shared main-gateway client.
    config = parent_agent.config
    overrides = agent_def.config_overrides()
    if overrides:
        config = {**config, **overrides}

    # toolsets come from config verbatim, NOT intersected with the parent's
    # runtime scope: the config author grants a specialist its toolsets,
    # and the main agent dispatching to it is that grant in action. Store
    # mounts union in per dispatch (hot — the ledger is re-read every time).
    from core.store import merge_mounts
    toolsets, skills = merge_mounts(
        agent_def.slug, agent_def.toolsets,
        None if agent_def.skills is None else set(agent_def.skills))
    child = XiheAgent(
        config=config,
        enabled_toolsets=toolsets,
        delegate_depth=parent_agent.delegate_depth + 1,
        is_subagent=True,
        identity_override=_persona_prompt(agent_def),
        skills_allowed=skills,
        project_context=agent_def.project_context,
        shared_db=parent_agent.db,
        shared_aux=parent_agent.aux,
        shared_compressor=parent_agent.compressor,
        cwd=str(workspace) if workspace else None,
    )

    # 子代理命中审批门时，request 经共享引用走父代理（顶层 turn）注入的
    # 回调；外部 resolve 也只找得到顶层 agent。
    child._approval_shared = parent_agent._approval_shared

    if hasattr(parent_agent, '_active_children'):
        lock = getattr(parent_agent, '_active_children_lock', None)
        if lock:
            with lock:
                parent_agent._active_children.append(child)
        else:
            parent_agent._active_children.append(child)

    return child


def _make_handler(agent_def):
    def _handler(args: dict, **kw) -> str:
        from core.session import SessionSource
        from tools.delegate_tool import MAX_DEPTH, _run_single_child

        parent = kw.get("parent_agent")
        if not parent:
            return tool_error("Agent not available")
        if parent.delegate_depth >= MAX_DEPTH:
            return tool_error(
                f"Delegation depth limit reached ({MAX_DEPTH}); specialist "
                "agents cannot be spawned from subagents."
            )
        goal = (args.get("goal") or "").strip()
        if not goal:
            return tool_error("Provide 'goal'.")
        context = (args.get("context") or "").strip()
        user_message = f"{goal}\n\nCONTEXT:\n{context}" if context else goal

        child = _build_agent_instance(agent_def, parent)
        source = SessionSource(
            platform="agent",
            chat_id=f"{agent_def.slug}_{int(time.time())}",
            chat_type="dm",
        )
        entry = _run_single_child(0, user_message, child, parent, source=source,
                                  label=agent_def.slug)
        entry.pop("task_index", None)
        return tool_result(agent=agent_def.name, **entry)

    return _handler


def _tool_description(agent_def) -> str:
    return (
        f"Delegate a task to the '{agent_def.name}' specialist agent.\n"
        f"{agent_def.description}\n\n"
        "The specialist runs with its own persona, tools, and skills, and "
        "knows nothing about this conversation — pass everything it needs "
        "via goal/context. Only its final summary returns to you.\n\n"
        "WHEN TO USE:\n"
        f"- The task clearly falls in {agent_def.name}'s domain, as described "
        "above\n"
        "WHEN NOT TO USE:\n"
        "- The task is outside its domain -> handle it yourself, or use "
        "delegate_task for a general-purpose subagent\n"
        "- A single direct tool call would suffice"
    )


def _tool_schema(agent_def) -> dict:
    return {
        "type": "function",
        "function": {
            "name": agent_def.tool_name,
            "description": _tool_description(agent_def),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": (
                            "What the specialist should accomplish. "
                            "Self-contained — it cannot see this conversation."
                        ),
                    },
                    "context": {
                        "type": "string",
                        "description": (
                            "Background the specialist needs: file paths, "
                            "error messages, constraints, prior findings."
                        ),
                    },
                },
                "required": ["goal"],
            },
        },
    }


def specialists_enabled(config: dict = None) -> bool:
    """Master switch for specialist dispatch: config.yaml ``specialists.enabled``.

    Default off — ordinary users don't need specialist delegation, so an
    agents/ directory alone must not expose run_*_agent tools.
    """
    if config is None:
        from core.config import load_config
        config = load_config()
    return bool((config.get("specialists") or {}).get("enabled"))


def register_specialist_agent_tools() -> None:
    """Register one run_<slug>_agent tool per configured specialist.

    Called from load_all_tools() after every static module and MCP discovery,
    so the collision check sees the full registry. Not at this module's
    import time — glob order imports this module before its siblings.

    Skipped entirely (no tools, no roster layer) unless specialists.enabled
    is set in config.yaml.
    """
    if not specialists_enabled():
        logger.info("Specialist dispatch off (specialists.enabled not set) — "
                    "run_*_agent tools not registered")
        return
    try:
        defs = _configured_defs()
    except Exception:
        logger.exception("Failed to parse agents/*.yaml; no specialist agents registered")
        return

    for agent_def in defs:
        if registry.get_schema(agent_def.tool_name) is not None:
            logger.warning(
                "agents/%s.yaml: tool name '%s' collides with an existing tool; skipped",
                agent_def.slug, agent_def.tool_name,
            )
            continue
        registry.register(
            name=agent_def.tool_name,
            schema=_tool_schema(agent_def),
            handler=_make_handler(agent_def),
            toolset="agent",
            subagent_blocked=True,
        )
        logger.info("Specialist agent registered: %s (%s)",
                    agent_def.name, agent_def.tool_name)


def build_roster_prompt(available_tools=None) -> str:
    """Team-roster layer for the main agent's system prompt: one line per
    configured specialist. ``available_tools`` (when given) filters to
    agents whose dispatch tool is actually exposed in this call, so the
    roster never advertises tools the model cannot call. Empty string when
    none apply."""
    try:
        defs = _configured_defs()
    except Exception:
        return ""
    if available_tools is not None:
        defs = [d for d in defs if d.tool_name in available_tools]
    if not defs:
        return ""

    lines = [
        "# Specialist Agents (configured experts)",
        "You manage the specialist agents below. Routing ladder for any "
        "subagent-shaped work — apply top-down, first match wins:",
        "1. Trivial single-step actions (one read, one quick command): do "
        "them yourself with basic tools; a specialist hop is never worth it.",
        "2. A task clearly in a specialist's domain (listed below): call "
        "its run_<name>_agent tool with a self-contained goal.",
        "3. Multi-step work no specialist covers (refactor + tests, whole-file "
        "rewrite/translation, batch edits): delegate_task.",
        "4. A raw tool that a specialist WRAPS (e.g. external_agent under "
        "an external-engine specialist): use the specialist, never the raw "
        "tool — the specialist layer adds context assembly and "
        "result-fidelity discipline the raw tool lacks.",
        "",
    ]
    for d in defs:
        lines.append(f"- {d.name} ({d.tool_name}): {d.description}")
    return "\n".join(lines)
