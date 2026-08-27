"""Delegate Tool — Subagent Architecture

Spawns child XiheAgent instances with isolated context, restricted toolsets,
and focused system prompts. Supports single-task and batch (parallel) modes.

Each child gets:
  - A fresh conversation (no parent history)
  - A restricted toolset (blocked toolsets always stripped)
  - A focused system prompt built from the delegated goal + context
  - Its own iteration budget (max_iterations)

The parent's context only sees the delegation call and the summary result,
never the child's intermediate tool calls or reasoning.

Defense against hangs (no thread-based timeout — follows Hermes design):
  1. max_iterations caps the agent loop (default: 30)
  2. API call timeout prevents HTTP hangs (120s per call)
  3. Parent interrupt propagates to children via _active_children
  4. Context compression keeps messages under model limit
"""

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS = 30
# Memory deliberately absent: children are one-shot; their durable facts go
# through the parent. Reads still reach them via the base toolset.
DELEGATE_DEFAULT_TOOLSETS = ["files", "terminal", "dev_tool", "http", "web", "media"]
MAX_DEPTH = 2          # parent (0) -> child (1) -> grandchild rejected (2)
MAX_CONCURRENT_CHILDREN = 3


def _check_delegate() -> bool:
    return True


def _build_child_system_prompt(
    goal: str,
    context: Optional[str] = None,
    *,
    workspace_path: Optional[str] = None,
    language: str = None,
) -> str:
    parts = [
        "You are a focused subagent working on a specific delegated task.",
        "",
        f"YOUR TASK:\n{goal}",
    ]
    if context and context.strip():
        parts.append(f"\nCONTEXT:\n{context}")
    if workspace_path and str(workspace_path).strip():
        parts.append(
            f"\nWORKSPACE PATH:\n{workspace_path}\n"
            "Use this exact path for local operations unless the task explicitly says otherwise."
        )
    parts.append(
        "\nComplete this task using the tools available to you. "
        "When finished, provide a clear, concise summary of:\n"
        "- What you did\n"
        "- What you found or accomplished\n"
        "- Any files you created or modified\n"
        "- Any issues encountered\n\n"
        "Be thorough but concise — your response is returned to the "
        "parent agent as a summary."
    )
    from core.prompts import language_directive
    directive = language_directive(language)
    if directive:
        parts.append(f"\n\nLANGUAGE:\n{directive}")
    return "\n".join(parts)


def _resolve_workspace_hint(parent_agent) -> Optional[str]:
    # Reuse the shared agent_base_dir (single source of truth for the cwd
    # priority order); keep the is_dir() safety so a child agent never inherits
    # a non-existent working directory.
    from tools._paths import agent_base_dir
    base = agent_base_dir(parent_agent)
    return str(base) if base and base.is_dir() else None


def _resolve_allowed_toolsets(requested: Optional[List[str]]):
    """Determine which toolsets a child agent can use.

    Children are scoped independently of the parent's roster: the main agent
    is deliberately slim (config main.toolsets) while delegation is the
    escape hatch for heavy tools, so requested names are honored as-is and
    no request means the broad child default. Per-tool blocking
    (delegate_task/skill_manage/send_message/cronjob/etc.) is handled by the
    `subagent_blocked` tag filtered in registry.get_schemas(subagent=True).
    """
    from core.toolsets import normalize_toolset_names
    if not requested:
        return list(DELEGATE_DEFAULT_TOOLSETS)
    ts = normalize_toolset_names(requested)
    if ts is None:          # ["*"] → unrestricted
        return None
    return sorted(ts) if ts else list(DELEGATE_DEFAULT_TOOLSETS)


def _build_child_agent(
    task_index: int,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    model: Optional[str],
    max_iterations: int,
    parent_agent,
):
    """Build a child XiheAgent with restricted toolsets and focused prompt.

    Registers child in parent's _active_children for interrupt propagation.
    """
    from core.agent import XiheAgent

    allowed_toolsets = _resolve_allowed_toolsets(toolsets)

    workspace_hint = _resolve_workspace_hint(parent_agent)
    child_prompt = _build_child_system_prompt(
        goal, context, workspace_path=workspace_hint,
        language=parent_agent.config.get("language"),
    )

    delegation_cfg = parent_agent.config.get("delegation", {})
    child_model = model or delegation_cfg.get("model") or parent_agent.model

    # Pass shared heavy objects directly to __init__ (avoids building a
    # throwaway SessionDB/AuxiliaryClient/ContextCompressor only to overwrite).
    child = XiheAgent(
        config=parent_agent.config,
        enabled_toolsets=allowed_toolsets,
        delegate_depth=parent_agent.delegate_depth + 1,
        is_subagent=True,
        system_prompt_override=child_prompt,
        shared_db=parent_agent.db,
        shared_aux=parent_agent.aux,
        shared_compressor=parent_agent.compressor,
    )

    if child_model != parent_agent.model:
        child.model = child_model
        child.aux._default_model = child_model

    child.max_iterations = max_iterations

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


def _unregister_child(child, parent_agent):
    """Remove child from parent's active_children after completion."""
    if hasattr(parent_agent, '_active_children'):
        try:
            lock = getattr(parent_agent, '_active_children_lock', None)
            if lock:
                with lock:
                    parent_agent._active_children.remove(child)
            else:
                parent_agent._active_children.remove(child)
        except ValueError:
            pass


def _extract_tool_trace(child, source) -> list[dict]:
    """Extract tool call trace from child session history for diagnostics."""
    tool_trace = []
    try:
        session_id = child.db.get_or_create_session(source)
        messages = child.db.load_messages(session_id)
        trace_by_id: dict = {}
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "assistant":
                for tc in (msg.get("tool_calls") or []):
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    entry_t = {
                        "tool": fn.get("name", "unknown"),
                        "args_bytes": len(fn.get("arguments", "")),
                    }
                    tool_trace.append(entry_t)
                    tc_id = tc.get("id") if isinstance(tc, dict) else None
                    if tc_id:
                        trace_by_id[tc_id] = entry_t
            elif msg.get("role") == "tool":
                content = msg.get("content", "")
                is_error = bool(content and "error" in content[:80].lower())
                result_meta = {
                    "result_bytes": len(content),
                    "status": "error" if is_error else "ok",
                }
                tc_id = msg.get("tool_call_id")
                target = trace_by_id.get(tc_id) if tc_id else None
                if target is not None:
                    target.update(result_meta)
                elif tool_trace:
                    tool_trace[-1].update(result_meta)
    except Exception:
        logger.debug("tool trace extraction failed", exc_info=True)
    return tool_trace


def _run_single_child(
    task_index: int,
    goal: str,
    child=None,
    parent_agent=None,
    source=None,
    label=None,
    **_kwargs,
) -> Dict[str, Any]:
    """Run a pre-built child agent. Called from within a thread (batch) or directly (single).

    ``source`` overrides the default delegate SessionSource, letting callers
    that pre-build their own child (e.g. business agents) own the session key.
    ``label`` tags every forwarded event with ``by=<label>`` so the client can
    tell child activity (specialist slug / delegate's "subagent-N") from the
    main agent's.

    No thread-based timeout — defense against hangs comes from:
      1. max_iterations cap on the agent loop
      2. API call timeout (120s per call in _call_with_retry)
      3. Parent interrupt propagation via _active_children

    Returns a structured result dict with tool_trace for diagnostics.
    """
    from core.session import SessionSource

    child_start = time.monotonic()
    logger.info("[subagent-%d] starting (max_iter=%d)",
                task_index, child.max_iterations)

    try:
        if source is None:
            source = SessionSource(
                platform="delegate",
                chat_id=f"subagent_{task_index}_{int(child_start)}",
                chat_type="dm",
            )

        # Bridge the parent's live-stream stash into the child's chat so a
        # specialist/delegate running a long external_agent turn isn't a
        # black box: reasoning + tool events keep flowing to the user's trace.
        # Content deltas are dropped — forwarding them would splice the
        # child's prose into the parent's in-flight assistant message; the
        # text still arrives as the tool result summary.
        # Events are tagged by=<label>; an inner ``by`` (claude nested inside
        # a specialist) wins over the outer label — innermost source is the
        # more precise attribution.
        parent_stream = getattr(parent_agent, "_active_stream_delta_cb", None)
        parent_tool_start = getattr(parent_agent, "_active_tool_call_start_cb", None)
        parent_tool_done = getattr(parent_agent, "_active_tool_call_cb", None)
        parent_tool_result = getattr(parent_agent, "_active_tool_result_cb", None)

        def _tag(inner):
            return inner or label

        def _child_stream(text, kind=None, by=None):
            if parent_stream and kind == "reasoning":
                try:
                    parent_stream(text, kind="reasoning", by=_tag(by))
                except Exception:
                    logger.debug("child stream forward failed", exc_info=True)

        def _child_tool_start(name, args, by=None):
            if parent_tool_start:
                try:
                    parent_tool_start(name, args, by=_tag(by))
                except Exception:
                    logger.debug("child tool_start forward failed", exc_info=True)

        def _child_tool_result(name, result, elapsed, by=None):
            if parent_tool_result:
                try:
                    parent_tool_result(name, result, elapsed, by=_tag(by))
                except Exception:
                    logger.debug("child tool_result forward failed", exc_info=True)

        chat_kwargs = {}
        if parent_stream:
            chat_kwargs["stream_delta_callback"] = _child_stream
        if parent_tool_start:
            chat_kwargs["tool_call_start_callback"] = _child_tool_start
        if parent_tool_done:
            chat_kwargs["tool_call_callback"] = parent_tool_done
        if parent_tool_result:
            chat_kwargs["tool_result_callback"] = _child_tool_result

        response = child.chat(source=source, user_message=goal, **chat_kwargs)

        duration = round(time.monotonic() - child_start, 2)

        tool_trace = _extract_tool_trace(child, source)

        response = response or ""

        # Classify exit reason — prefer the structured attribute over fragile
        # string-prefix matching. The return string is a user-facing message,
        # not a stable contract; _last_exit_reason is set at every chat() exit.
        reason = getattr(child, "_last_exit_reason", "completed")
        if not response:
            status = "failed"
            exit_reason = "failed"
        elif reason == "max_iterations":
            status = "max_iterations"
            exit_reason = "max_iterations"
        elif reason == "interrupted":
            status = "interrupted"
            exit_reason = "interrupted"
        elif reason == "api_timeout":
            status = "api_timeout"
            exit_reason = "api_timeout"
        elif reason == "api_error":
            status = "api_error"
            exit_reason = "api_error"
        else:
            status = "completed"
            exit_reason = "completed"

        entry = {
            "task_index": task_index,
            "label": label,
            "status": status,
            "summary": response,
            "duration_seconds": duration,
            "exit_reason": exit_reason,
            "tool_trace": tool_trace,
        }

        if status == "failed":
            entry["error"] = "Subagent did not produce a response."
        elif status in ("api_timeout", "api_error"):
            entry["error"] = response

        return entry

    except Exception as exc:
        duration = round(time.monotonic() - child_start, 2)
        logger.exception("[subagent-%d] failed", task_index)
        return {
            "task_index": task_index,
            "status": "error",
            "summary": None,
            "error": str(exc),
            "duration_seconds": duration,
        }

    finally:
        _unregister_child(child, parent_agent)


def _delegate_task(args: dict, **kw) -> str:
    goal = args.get("goal", "")
    context = args.get("context", "")
    toolsets = args.get("toolsets")
    tasks = args.get("tasks")
    max_iterations = args.get("max_iterations")

    agent = kw.get("parent_agent")
    if not agent:
        return tool_error("Agent not available for delegation")

    if agent.delegate_depth >= MAX_DEPTH:
        return tool_error(
            f"Delegation depth limit reached ({MAX_DEPTH}). "
            "Subagents cannot spawn further subagents."
        )

    delegation_cfg = agent.config.get("delegation", {})
    default_max_iter = delegation_cfg.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    effective_max_iter = min(
        max_iterations or default_max_iter,
        DEFAULT_MAX_ITERATIONS * 2,  # hard cap
    )

    if tasks and isinstance(tasks, list):
        task_list = tasks[:MAX_CONCURRENT_CHILDREN]
    elif goal and isinstance(goal, str) and goal.strip():
        task_list = [{"goal": goal, "context": context, "toolsets": toolsets}]
    else:
        return tool_error("Provide either 'goal' (single task) or 'tasks' (batch).")

    for i, task in enumerate(task_list):
        if not task.get("goal", "").strip():
            return tool_error(f"Task {i} is missing a 'goal'.")

    overall_start = time.monotonic()
    results = []
    n_tasks = len(task_list)

    model_override = delegation_cfg.get("model")

    children = []
    for i, t in enumerate(task_list):
        child = _build_child_agent(
            task_index=i,
            goal=t["goal"],
            context=t.get("context"),
            toolsets=t.get("toolsets") or toolsets,
            model=model_override,
            max_iterations=effective_max_iter,
            parent_agent=agent,
        )
        children.append((i, t, child))

    # Per-parent label sequence (subagent-1, subagent-2, …): a shared
    # "subagent" label made parallel children's trace rows indistinguishable
    # on the client. Scoped to the agent instance and NOT the batch, so a
    # second delegate call in the same turn doesn't restart at subagent-1.
    seq = getattr(agent, "_delegate_seq", 0) + 1
    agent._delegate_seq = seq + len(children) - 1

    if n_tasks == 1:
        # Single task — run directly on current thread (no pool overhead)
        _i, _t, child = children[0]
        result = _run_single_child(_i, _t["goal"], child, agent,
                                   label=f"subagent-{seq}")
        results.append(result)
    else:
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CHILDREN) as executor:
            futures = {}
            for i, t, child in children:
                future = executor.submit(
                    _run_single_child,
                    task_index=i,
                    goal=t["goal"],
                    child=child,
                    parent_agent=agent,
                    label=f"subagent-{seq + i}",
                )
                futures[future] = i

            for future in as_completed(futures):
                try:
                    entry = future.result()
                except Exception as exc:
                    idx = futures[future]
                    entry = {
                        "task_index": idx,
                        "status": "error",
                        "summary": None,
                        "error": str(exc),
                        "duration_seconds": 0,
                    }
                results.append(entry)

        # Sort by task_index so results match input order
        results.sort(key=lambda r: r["task_index"])

    total_duration = round(time.monotonic() - overall_start, 2)

    return tool_result(results=results, total_duration_seconds=total_duration)


registry.register(
    name="delegate_task",
    schema={
        "type": "function",
        "function": {
            "name": "delegate_task",
            "description": (
                "Spawn subagent(s) for isolated multi-step tasks. Each subagent gets "
                "its own conversation and restricted toolset. Only the final summary "
                "is returned — intermediate results never enter your context.\n\n"
                "MODES (one of 'goal' or 'tasks' is required):\n"
                "1. Single task: provide 'goal' (+ optional context, toolsets)\n"
                "2. Batch (parallel): provide 'tasks' array with up to 3 items.\n\n"
                "WHEN TO USE:\n"
                "- Reasoning-heavy subtasks (debugging, code review, research)\n"
                "- Tasks that would flood your context with intermediate data\n"
                "- Parallel independent workstreams\n"
                "- Broad codebase exploration / multi-hop tracing (lineage, call-graph, "
                "'where is X defined/fed from'): fan out one read-only child per code path, "
                "have each return a conclusion + evidence (file:line), then you synthesize. "
                "This catches paths a single linear grep misses.\n\n"
                "WHEN NOT TO USE:\n"
                "- Single tool call -> just call the tool directly\n"
                "- Tasks needing user interaction -> subagents cannot use clarify\n"
                "- A configured specialist agent covers the domain -> prefer its "
                "run_<name>_agent tool\n\n"
                "IMPORTANT:\n"
                "- Subagents have NO memory of your conversation. Pass all relevant "
                "info via the 'context' field.\n"
                "- Subagents CANNOT call tools in the blocked set (recursion, user "
                "interaction, scheduling, persistent mutation) — an error about a "
                "missing tool means handle that part yourself.\n"
                "- Results are always returned as an array, one entry per task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": (
                            "What the subagent should accomplish. Be specific and "
                            "self-contained — the subagent knows nothing about your "
                            "conversation history."
                        ),
                    },
                    "context": {
                        "type": "string",
                        "description": (
                            "Background information the subagent needs: file paths, "
                            "error messages, project structure, constraints."
                        ),
                    },
                    "toolsets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Toolsets to enable for this subagent (files, terminal, "
                            "dev_tool, http, web, media, memory, mcp, ssh, scheduler, "
                            "skills, kbs, external_agents, meta). Omitted → default: "
                            "files, terminal, dev_tool, http, web, media. "
                            "[\"*\"] = everything. Read faces (file reads, skills/"
                            "memory/kbs lookups, todo, model_info) are always included "
                            "via the base toolset; write faces stay opt-in. A small "
                            "blocked set (recursion, user interaction, scheduling, "
                            "persistent mutation of shared assets) is stripped "
                            "regardless. external_agent is allowed (claude holds no "
                            "xihe tools — no recursion; each subagent gets its own "
                            "claude session)."
                        ),
                    },
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "goal": {"type": "string", "description": "Task goal"},
                                "context": {"type": "string", "description": "Task-specific context"},
                                "toolsets": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Toolsets for this specific task.",
                                },
                            },
                            "required": ["goal"],
                        },
                        "maxItems": 3,
                        "description": (
                            "Batch mode: up to 3 tasks to run in parallel. Each gets "
                            "its own subagent with isolated context. When provided, "
                            "top-level goal/context/toolsets are ignored."
                        ),
                    },
                    "max_iterations": {
                        "type": "integer",
                        "description": (
                            "Max tool-calling turns per subagent (default: 30). "
                            "Only set lower for simple tasks."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    handler=lambda args, **kw: _delegate_task(args, **kw),
    check_fn=_check_delegate,
    toolset="agent",
    subagent_blocked=True,
)
