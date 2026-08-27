"""External Agent Tool — delegate a subtask to an external reasoning engine.

Thin exposure layer over :mod:`core.external_agent`. Mirrors
:mod:`tools.delegate_tool`'s return shape so the parent agent treats the result
like any other delegation summary. Unlike ``delegate_task`` (which spawns an
internal ``XiheAgent`` child with its own context), this drives an **external**
CLI (``claude`` or ``codex``) that runs its OWN tool loop as a headless
subprocess; we surface the final summary AND stream its reasoning/text back to
the user live via the parent's stream callback.

Trigger semantic = 方案 A + 流式 (xihe stays the primary orchestrator and invokes
the engine as a reasoning "brain" for a subtask). The two engines differ only in
lifecycle (see core/external_agent.py): claude = warm long-lived child, codex =
fresh process per turn resumed by thread id — both share one driver interface.

Result shape == delegate_task's (``task_index``/``status``/``summary``/
``duration_seconds``/``exit_reason``/``tool_trace``) plus an ``engine`` tag.
``read_only=False`` so a turn forces sequential dispatch in the agent loop
(spawning a writable subprocess is inherently a side effect, and it avoids any
concern about parallel tools sharing the parent's stream callback).
"""

import logging
import os
from pathlib import Path
from typing import Optional

from tools import registry, tool_error, tool_result
from tools._paths import agent_base_dir

logger = logging.getLogger(__name__)

_ENGINES = ("claude", "codex")


def _resolve_engine_bin(config: dict, engine: str) -> Optional[str]:
    """Return the configured engine binary if it's actually runnable, else None.

    Order: ``external_agents.<engine>.command`` → engine name on PATH. Used by
    the availability gate (any engine) and the per-turn check (this engine);
    the driver re-resolves via TurnSpec.bin.
    """
    cfg = (config.get("external_agents") or {}).get(engine) or {}
    command = str(cfg.get("command") or engine)
    if os.path.isabs(command) and os.path.exists(command):
        return command
    import shutil
    return shutil.which(command)


def _check_external_agent() -> bool:
    """Availability gate — silently drop ``external_agent`` when NO engine
    binary is installed (same pattern as browser tools gating on Playwright).
    Symptom of a missing binary: the agent simply never sees this tool. With
    only one engine installed the tool still registers; the missing engine
    fails per-turn with an explicit tool_error.
    """
    try:
        from core.config import load_config
        config = load_config()
    except Exception:
        config = {}
    return any(_resolve_engine_bin(config, e) is not None for e in _ENGINES)


def _resolve_llm_creds(config: dict, engine: str) -> dict:
    """Resolve LLM credentials for the engine child, reusing xihe's by default.

    Both engines share xihe's gateway, so the default key source is xihe's own
    main ``api_key`` — no separate credential set to maintain. Priority:
    explicit ``external_agents.<engine>.*`` override → xihe's main config
    (single source — no env fallback).

    claude base_url note: xihe stores it OpenAI-style WITH a trailing ``/v1``
    (calls ``…/v1/chat/completions``); the claude CLI reads
    ``ANTHROPIC_BASE_URL`` as a bare base and appends ``/v1/messages`` itself.
    So when reusing xihe's value we strip the trailing ``/v1`` to avoid a
    doubled ``…/v1/v1/messages`` path. (This still requires the gateway to
    accept Anthropic Messages-API requests at ``<base>/v1/messages`` — a
    gateway-compat concern, not a cred one.)

    codex takes the key (driver injects it as ``CODEX_API_KEY``) plus optional
    provider wiring: setting ``external_agents.codex.base_url`` makes the driver
    define an inline provider via ``exec -c model_providers.xihe.*`` overrides
    (verified 0.146.1), overriding config.toml's provider choice for the run.
    Deliberately opt-in — NO fallback to xihe's main base_url, which would
    silently override every user's ``~/.codex/config.toml`` provider. The value
    passes verbatim (no ``/v1`` strip): codex appends ``/responses`` or
    ``/chat/completions`` to whatever base it gets. ``wire_api`` rides along
    (``responses`` default — verified against the internal litellm gateway;
    ``chat`` for chat-completions-only gateways).

    ``max_tokens`` (per-engine, explicit only) maps to each engine's output
    cap: claude → ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` env, codex → ``-c
    model_max_output_tokens=``. No fallback to the main ``max_completion_tokens``
    — the engines default to their own caps (claude internal / codex
    config.toml) and an inherited value would silently override them.

    Returned dict is injected verbatim into the child env by the driver; values
    never enter logs (driver uses ``build_safe_env``).
    """
    cfg = (config.get("external_agents") or {}).get(engine) or {}
    api_key = cfg.get("api_key") or config.get("api_key")
    llm = {}
    if api_key:
        llm["api_key"] = api_key
    try:
        mt = int(cfg.get("max_tokens") or 0)
        if mt > 0:
            llm["max_tokens"] = mt
    except (TypeError, ValueError):
        pass  # malformed key must not break spawning
    if engine == "claude":
        base_url = cfg.get("base_url") or config.get("base_url")
        if base_url:
            base_url = base_url.rstrip("/")
            if base_url.endswith("/v1"):
                base_url = base_url[:-3]
            llm["base_url"] = base_url
    elif engine == "codex":
        base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
        if base_url:
            llm["base_url"] = base_url
            wire = str(cfg.get("wire_api") or "responses").strip().lower()
            llm["wire_api"] = wire if wire in ("responses", "chat") else "responses"
    return llm


def _norm_extra_args(raw) -> list:
    """``external_agents.<engine>.extra_args`` → flat list of argv strings.

    Appended verbatim before the engine's resume flags (claude ``--resume`` /
    codex ``resume``/``"-"``). Non-list / blank entries are dropped rather
    than erroring — a malformed config key must not break spawning.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(a) for a in raw if str(a).strip()]


def _external_agent(args: dict, **kw) -> str:
    from core.external_agent import TurnSpec, get_driver

    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return tool_error("external_agent needs a non-empty 'prompt'.")

    engine = (args.get("engine") or "claude").strip()
    context_hint = args.get("context") or ""

    parent_agent = kw.get("parent_agent")
    config = getattr(parent_agent, "config", None) or {}

    # Build the full prompt handed to claude: the subtask + any context the
    # parent supplied. claude knows nothing of xihe's conversation, so context
    # must be self-contained (same contract as delegate_task).
    full_prompt = prompt
    if context_hint and context_hint.strip():
        full_prompt = f"{prompt}\n\n--- context ---\n{context_hint.strip()}"

    if engine not in _ENGINES:
        return tool_error(f"未知的外部 agent 引擎：{engine}（支持：{'、'.join(_ENGINES)}）")

    # Gate passed on "any engine installed" — this turn's engine may still be
    # the missing one. Fail explicitly rather than letting Popen error opaquely.
    if _resolve_engine_bin(config, engine) is None:
        return tool_error(
            f"{engine} 二进制不可用：请在 config.yaml 的 external_agents.{engine}.command "
            "配置路径，或将其加入 PATH。")

    # Workspace + session identity (same source of truth as delegate_task).
    cwd = agent_base_dir(parent_agent)
    ctx = kw.get("context") or {}
    session_key = ctx.get("session_key") or "external-default"

    # Headless engines auto-load their workspace conventions file into context
    # (claude: CLAUDE.md; codex: AGENTS.md) with no tool call, so the trace
    # shows nothing — but empirically they don't reliably FOLLOW it. A
    # one-line directive fixes compliance at ~20 tokens; injecting the file
    # itself would just duplicate what's already loaded.
    _CONV_FILE = {"claude": "CLAUDE.md", "codex": "AGENTS.md"}
    conv_file = _CONV_FILE[engine]
    if cwd and (Path(str(cwd)) / conv_file).is_file():
        full_prompt += (
            f"\n\n--- project conventions ---\n"
            f"工作目录的 {conv_file} 已自动加载在你的上下文中。开始分析前先对照其中的"
            "项目约定，过程与结论必须遵守其中的规则。"
        )

    # The engine's thinking defaults to English on technical tasks even when
    # the prompt is Chinese — a directive line only raises the Chinese ratio
    # (no API switch exists, so this is best-effort). Keyed on config
    # `language`: en/auto → the engine's natural English, no directive.
    if str(config.get("language", "zh")).strip().lower() == "zh":
        full_prompt += ("\n\n--- thinking language ---\n"
                        "内部思考必须始终使用中文，不得用英文思考；"
                        "回复跟随用户语言。")

    # 中文 Windows 的编码坑：内部 CLI与部分旧文档是 GBK，引擎
    # 按 UTF-8 捕获/读取 → 乱码，且字节在解码时就丢了，下游无法恢复——只能
    # 教它识别并立刻换编码重取，禁止基于乱码猜测内容。
    if os.name == "nt":
        full_prompt += (
            "\n\n--- Windows 编码注意 ---\n"
            "本机是中文 Windows：部分内部 CLI和旧文档输出/存储 GBK 编码，"
            "你按 UTF-8 捕获会得到乱码（�）。规则：\n"
            "1. 命令输出出现乱码 → 不要猜测内容，用 `原命令 | iconv -f GBK -t UTF-8` "
            "重跑后再读。\n"
            "2. Read 文件出现乱码 → 该文件是 GBK 或混合编码，用 "
            "`iconv -f GBK -t UTF-8 文件`（或 head 限制行数）重读。\n"
            "3. python 读写文件显式指定 encoding（utf-8/gbk），不要依赖默认值。"
        )

    engine_cfg = (config.get("external_agents") or {}).get(engine) or {}
    spec = TurnSpec(
        cwd=str(cwd) if cwd else None,
        llm=_resolve_llm_creds(config, engine),
        # claude: its --permission-mode values (default bypassPermissions);
        # codex: read-only|workspace-write|danger-full-access → -s (default
        # workspace-write), bypassPermissions → bypass flag.
        permission_mode=engine_cfg.get(
            "permission_mode", "bypassPermissions" if engine == "claude" else "workspace-write"),
        model=args.get("model") or engine_cfg.get("model"),
        bin=engine_cfg.get("command") or engine,
        debug=bool(engine_cfg.get("debug", False)),
        extra_args=_norm_extra_args(engine_cfg.get("extra_args")),
    )

    # Stream forwarder: the engine's text/reasoning/tool events → the parent's
    # live stream callbacks (stashed on the agent at chat() entry). The
    # engine's own tool calls (read_file/terminal during an analysis) surface
    # in the turn trace just like xihe's tools, so a slow turn isn't a black
    # box. Every event is tagged by=<engine> so clients can set it apart from
    # the main agent's activity. No callbacks set (non-streaming mode / tests)
    # → dropped per-branch; the summary still comes back via the result.
    #
    # NOTE: `kind` MUST be passed as a keyword — serve's Emitter.on_delta is
    # `(text, **kw)` and reads `kw["kind"]`; a positional second arg raises
    # TypeError (silently swallowed here), which is why streaming didn't work
    # before. agent.py itself always calls with kind=.
    stream_cb = getattr(parent_agent, "_active_stream_delta_cb", None)
    tool_start_cb = getattr(parent_agent, "_active_tool_call_start_cb", None)
    tool_done_cb = getattr(parent_agent, "_active_tool_call_cb", None)
    # Result events belong on the result stash — the done stash is completion-
    # without-payload and serve never sets it, which is why claude's tool rows
    # used to spin forever with no result. CLI wires only the done variant
    # (tool_finish), so fall back to it there.
    tool_result_cb = getattr(parent_agent, "_active_tool_result_cb", None) or tool_done_cb

    def on_event(event: dict) -> None:
        et = event.get("type")
        try:
            if et == "text_delta":
                if stream_cb:
                    stream_cb(event.get("text", ""), kind="content", by=engine)
            elif et == "thought_delta":
                if stream_cb:
                    stream_cb(event.get("text", ""), kind="reasoning", by=engine)
            elif et == "tool_call":
                # The engine invoked one of ITS tools mid-turn — surface it in
                # the trace so the user sees e.g. "claude is reading
                # pyproject.toml".
                if tool_start_cb:
                    tool_start_cb(event.get("name", f"{engine}_tool"),
                                  str(event.get("args", ""))[:120], by=engine)
            elif et == "tool_result":
                # Driver puts the result payload in `args` (see
                # core/external_agent.py tool_result emission).
                if tool_result_cb:
                    tool_result_cb(event.get("name", f"{engine}_tool"),
                                   str(event.get("args", "")),
                                   float(event.get("elapsed") or 0), by=engine)
        except Exception:
            logger.debug("external_agent stream forward failed", exc_info=True)

    try:
        result = get_driver(engine).run_turn(
            session_key=session_key,
            prompt=full_prompt,
            spec=spec,
            on_event=on_event,
        )
    except Exception as exc:
        logger.exception("external_agent run_turn failed")
        return tool_error(f"external_agent 执行失败：{type(exc).__name__}: {exc}")

    # Map the driver's exit_reason (completed|interrupted|failed) onto the
    # delegate return shape so the parent treats this like any delegation.
    entry = {
        "task_index": 0,
        "status": result.exit_reason,
        "summary": result.final_text or "",
        "duration_seconds": result.duration_seconds,
        "exit_reason": result.exit_reason,
        "tool_trace": result.tool_trace,
        "engine": engine,
    }
    if result.error:
        entry["error"] = result.error
    if result.session_id:
        entry["session_id"] = result.session_id

    return tool_result(results=[entry])


registry.register(
    name="external_agent",
    schema={
        "type": "function",
        "function": {
            "name": "external_agent",
            "description": (
                "Delegate a reasoning-heavy subtask to an external agent engine "
                "(claude or codex). The external agent runs its OWN full tool "
                "loop in your workspace (same cwd) and returns a summary; its "
                "reasoning and text stream back to the user live.\n\n"
                "WHEN TO USE:\n"
                "- Hard reasoning / deep analysis you'd rather hand to another "
                "engine\n"
                "- A self-contained subtask with its own multi-step tool needs\n"
                "- A second opinion or independent implementation pass on a problem\n\n"
                "WHEN NOT TO USE:\n"
                "- Single tool calls → call the tool directly\n"
                "- Tasks needing xihe's internal tools (browser/ssh/kbs/MCP/skills) "
                "→ those are YOUR tools, not the external agent's; use delegate_task "
                "or do it yourself\n\n"
                "IMPORTANT:\n"
                "- It runs in YOUR workspace (same cwd) with its OWN file/shell "
                "tools. For 'explore / analyze the project' tasks, delegate the "
                "GOAL directly — do NOT pre-read files just to forward their "
                "contents; it reads them itself.\n"
                "- It has NO memory of your conversation. Forward ONLY what it "
                "can't see itself — decisions, constraints, errors, or paths "
                "named earlier in THIS chat — via 'prompt' (+ optional 'context').\n"
                "- Same session + same engine → the external agent remembers "
                "prior external_agent turns.\n"
                "- Available when at least one engine binary (claude/codex) is "
                "installed; a per-engine turn fails with a hint if that engine's "
                "binary is missing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "The subtask to hand off (the user message the external "
                            "agent receives). State the goal clearly; for in-workspace "
                            "work it explores on its own — don't paste file contents "
                            "it can read itself."
                        ),
                    },
                    "context": {
                        "type": "string",
                        "description": (
                            "Optional background (file paths, errors, constraints) "
                            "appended after the prompt. The external agent knows "
                            "nothing of your conversation history."
                        ),
                    },
                    "engine": {
                        "type": "string",
                        "enum": ["claude", "codex"],
                        "description": (
                            "External engine to use (default: claude). codex takes "
                            "its provider/model wiring from ~/.codex/config.toml "
                            "by default; setting external_agents.codex.base_url "
                            "overrides the provider inline for the run."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Override the external agent's model for this turn. "
                            "Default: configured external_agents.<engine>.model."
                        ),
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    handler=lambda args, **kw: _external_agent(args, **kw),
    check_fn=_check_external_agent,
    toolset="external_agents",
    read_only=False,
)
