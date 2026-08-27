"""Modular system prompt assembly — declarative layer table + independent constants.

build_system_prompt() walks LAYERS with a PromptCtx: each layer returns its
section or None to skip. Every tool-conditional layer keys off ctx.tools so
guidance never advertises a tool the agent cannot call.
"""

import logging
import platform as _platform
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

AGENT_IDENTITY = (
    "You are Xihe Agent, a helpful AI assistant with tool-calling capabilities. "
    "You assist users with a wide range of tasks including answering questions, "
    "writing code, analyzing information, and executing actions via your tools. "
    "Be direct, efficient, and proactive."
)

# Backward compat alias
SYSTEM_PROMPT_DEFAULT = AGENT_IDENTITY

TOOL_USE_ENFORCEMENT = (
    "# Tool-Use Discipline\n\n"
    "You MUST use your tools to take action — do not describe what you would do "
    "without actually doing it. When you say you will perform an action, you MUST "
    "immediately make the corresponding tool call. Never end your turn with a "
    "promise of future action — execute it now.\n\n"
    "Every response should either (a) contain tool calls that make progress, or "
    "(b) deliver a final result to the user."
)

def build_mandatory_tool_use(tools: set) -> str:
    """Lines only for tools actually loaded — an agent without terminal must
    not be told to check the clock with it."""
    lines = []
    if {"terminal", "execute_code"} & tools:
        lines.append("- Arithmetic, math, calculations → terminal or execute_code")
    if "terminal" in tools:
        lines.append("- Current time, date, timezone → terminal")
        lines.append("- System state (OS, CPU, disk, processes) → terminal")
        lines.append("- Git history, diffs → terminal")
    if {"read_file", "search_files", "terminal"} & tools:
        lines.append("- File contents, sizes → read_file, search_files, or terminal")
    if not lines:
        return ""
    return "NEVER answer these from memory — ALWAYS use a tool:\n" + "\n".join(lines)

BROWSER_LOGIN_GUIDANCE = (
    "# Browser Login\n\n"
    "Xihe's browser is a CDP-managed real Chrome (separate profile at "
    "${AGENT_HOME}/browser/cdp-profile). Login state persists across restarts. "
    "browser_navigate auto-boots it.\n\n"
    "Flow: navigate to URL → if login wall (请登录/sso_hint/empty snapshot), "
    "navigate to the site's login page and tell the user to log in there → "
    "snapshot to verify → continue. NEVER taskkill chrome.exe or ask for "
    "credentials. browser_connect is advanced (use 127.0.0.1:9222 not localhost).\n\n"
    "For script-driven workflows (internal portals etc): navigate → login if needed → "
    "read_file the script → browser_eval to execute."
)

# Body lines of the single "# Memory" section — _memory_guidance() picks the
# ones matching the loaded tools and adds the shared header.
MEMORY_READ_GUIDANCE = (
    "Stored durable facts (preferences, conventions, tool quirks) — query with "
    "memory(action='search'/'get') before re-asking the user."
)

MEMORY_SAVE_GUIDANCE = (
    "Save durable facts with memory_manage(action='save') (preferences, "
    "conventions, tool quirks). "
    "Do NOT save task progress — use session_search for that."
)

SESSION_SEARCH_GUIDANCE = (
    "# Session Continuity\n\n"
    "User references a past conversation? Use session_search before asking them "
    "to repeat."
)

DELEGATE_GUIDANCE = (
    "# Delegation\n\n"
    "Complex independent subtasks → delegate_task. Pass all context — subagents "
    "have no memory of your conversation.\n"
    "Multi-step work with no specialist covering it (refactor + tests, whole-file "
    "translation, batch edits across files) → delegate_task too: every extra "
    "iteration in YOUR loop re-sends the full system prompt plus accumulated "
    "history, while a child's tool loop stays out of your context. Do it yourself "
    "only when it is a couple of direct calls."
)

EXTERNAL_AGENT_GUIDANCE = (
    "# External agent (claude / codex)\n\n"
    "external_agent(engine='claude'|'codex') delegates a subtask to that engine — it "
    "runs its OWN tool loop in the workspace and streams its reasoning back to the "
    "user live.\n"
    "- When the user names an engine — 用claude / 使用claude / 让claude来 / 找claude / "
    "use claude, or 用codex / 使用codex / 让codex来 / use codex — or says 外部引擎 / "
    "外部模型, or asks for that engine's opinion/review → hand the goal to "
    "external_agent with engine= set accordingly. "
    "Do NOT do the work yourself and do NOT pre-read the files to forward "
    "them — the engine reads them itself; hand it the goal (plus any "
    "paths/constraints as context).\n"
    "- No engine named → default is claude; pick codex when the user asks for a "
    "GPT/codex-style take or wants a second engine on the same problem.\n"
    "- Otherwise prefer external engines for deep reasoning, code/architecture "
    "review, or a second opinion you'd rather hand off.\n"
    "- delegate_task (internal subagent) is the right pick for parallel work that "
    "needs YOUR tools (browser/ssh/mcp/skills) — external engines do NOT have those."
)

SKILLS_GUIDANCE = (
    "# Skills\n\n"
    "After a complex task (5+ tools) or tricky fix, save as skill_manage for reuse. "
    "Found a skill outdated? Patch immediately with skill_manage(action='patch')."
)

CRON_GUIDANCE = (
    "# Cron Jobs\n\n"
    "Cron jobs run in fresh stateless sessions — prompts must be self-contained, "
    "persist state to files if needed. Pick shape by task logic:\n"
    "- Deterministic (cleanup/sync/watchdog) → script, optionally no_agent=true (0 tokens)\n"
    "- Reasoning (briefing/summary) → prompt only\n"
    "- Alert-on-new → script + wake gate ({\"wakeAgent\": false} to skip when idle)\n"
    "Scripts go in ${AGENT_HOME}/scripts/. CREATE scripts yourself (write_file + test), "
    "don't ask the user. Chain with context_from=<job_id>. Reply [SILENT] when nothing "
    "to report."
)

MCP_GUIDANCE = (
    "# MCP Servers\n\n"
    "MCP tools (mcp_{server}_{tool}) are native. To add/change servers: edit "
    "mcp_servers in ${AGENT_HOME}/config.yaml (stdio: command/args/env, http: "
    "url/headers; secrets via ${VAR}). Then tell user to /reload-mcp — you cannot "
    "connect servers yourself."
)

# Items 1-7 — item 8 is appended by _coding_guidance() so its delegate_task
# half never reaches agents that cannot call it.
CODING_GUIDANCE = (
    "# Coding discipline\n\n"
    "1. **Read before write** — read_file before editing (never from memory). "
    "Match surrounding style (naming, indent, comments).\n"
    "2. **Search before creating** — search_files for existing patterns first. "
    "Prefer extending over duplicating.\n"
    "3. **Minimal + verify** — smallest effective change; after editing run "
    "py_compile/tests to confirm. Don't claim done without verifying.\n"
    "4. **Trace full chains** — lineage/data-flow/call-graph are multi-hop; "
    "follow every hop. Prefer search_files/read_file over terminal grep.\n"
    "5. **Cite evidence** — reference file:line. Run code to confirm behavior. "
    "Never present guesses as facts.\n"
    "6. **Security** — don't introduce vulnerabilities (injection, path traversal, "
    "hardcoded secrets). Validate untrusted inputs.\n"
    "7. **User consent before changes** — before editing, tell the user the "
    "changes you plan to make and why; implement only after they agree. "
    "Read-only investigation needs no consent."
)

BEHAVIOR_RULES = (
    "# Behavior Rules\n\n"
    "1. **Act don't ask**: Obvious default outside code changes → act immediately\n"
    "2. **Safety first**: Warn before destructive operations\n"
    "3. **Error handling**: Tool fails → retry differently\n"
    "4. **Workspace clean**: Throwaway artifacts → ${AGENT_HOME}/scratch/<task>/ "
    "(absolute path, not relative). Reusable scripts → scripts/.\n"
)

# Conditional continuations of the Behavior Rules list — appended only when
# the referenced tool is actually loaded, so the rules never promise a tool
# the agent cannot call.
MODEL_INFO_RULE = (
    "5. **Know your model**: Asked which model? Call model_info — never guess or "
    "grep logs.\n"
)

REQUEST_TOOLS_RULE = (
    "6. **Expand tools (重要)**: browser_*/media_*/cronjob 不在默认工具集。两种情况必须先 request_tools: "
    "(a) 任务需要 browser/media/scheduler 但工具列表没有; "
    "(b) **skill 内容出现工具名(如 browser_navigate/ssh_exec)但你当前工具列表没有该工具**。"
    "调 request_tools([\"web\"/\"media\"/\"scheduler\"]) 展开。"
)

# 思考/回复语言引导，按 config 顶层键 `language` 取值（zh | en | auto）。
# auto / 未知值 → None（不注入，模型自行选择）。单一来源：分层 prompt、
# delegate 子代理 prompt、external_agent 的 claude 指令都从这里取。
LANGUAGE_DIRECTIVES = {
    "zh": "内部思考（reasoning）必须始终使用中文，不得用英文思考；回复默认中文，"
          "仅当用户当前消息使用其他语言时回复跟随该语言（思考仍必须是中文）。",
    "en": "Always think (reasoning) in English, never in another language; "
          "reply in English unless the user's current message is in another "
          "language (then reply in that language, still thinking in English).",
}


def language_directive(language: str) -> Optional[str]:
    return LANGUAGE_DIRECTIVES.get((language or "").strip().lower())

KBS_SUBAGENT_READ_NOTE = (
    "# Business KB (read discipline)\n\n"
    "kbs_search covers the business knowledge base. Entries under "
    "meta/candidates/ are unreviewed notes — never cite them as conclusions; "
    "prefer wiki/ pages."
)

PLATFORM_PROMPTS = {
    "wecom": (
        "You are connected via WeCom (Enterprise WeChat). "
        "Keep responses concise — WeCom has a 4000 char message limit. "
        "Do not use markdown formatting as WeCom renders it poorly.\n"
        "Media support:\n"
        "- You CAN send images using the send_image tool.\n"
        "- NEVER say you cannot send images — always use the send_image tool.\n"
    ),
    "feishu": (
        "You are connected via Feishu (Lark). "
        "You can use markdown formatting for emphasis, code, and links. "
        "Keep responses focused.\n"
    ),
    "telegram": (
        "You are connected via Telegram. "
        "Keep responses concise. Use plain text — Telegram renders limited markdown.\n"
    ),
    "discord": (
        "You are connected via Discord. "
        "You can use basic markdown. Keep responses focused.\n"
    ),
    "cli": (
        "You are running in a terminal CLI. "
        "You can use markdown formatting.\n"
    ),
}

_BUNDLED_SKILLS_DIR = Path(__file__).parent.parent / "skills"

def _get_user_skills_dir() -> Path:
    from core.config import AGENT_HOME
    return AGENT_HOME / "skills"

_SKILLS_CACHE: Optional[dict] = None
_SKILLS_CACHE_LOCK = threading.Lock()
_SKILLS_CACHE_KEY: Optional[tuple] = None  # (bundled_mtime, user_mtime)


def clear_skills_cache() -> None:
    """Drop the in-process skills index cache. Call after skill create/edit/delete."""
    global _SKILLS_CACHE, _SKILLS_CACHE_KEY
    with _SKILLS_CACHE_LOCK:
        _SKILLS_CACHE = None
        _SKILLS_CACHE_KEY = None


def _dir_cache_key(skills_dir: Path) -> Optional[float]:
    """Get a cheap cache key for a skills directory (max mtime of all SKILL.md files)."""
    if not skills_dir.exists():
        return None
    try:
        mtimes = [f.stat().st_mtime for f in skills_dir.rglob("SKILL.md")]
        return max(mtimes) if mtimes else 0.0
    except Exception:
        return None


def _parse_skill_frontmatter(content: str) -> tuple[str, str]:
    """Extract name and description from SKILL.md frontmatter.

    Returns (name, description). Falls back to directory name and first body line.
    """
    name = ""
    description = ""

    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            fm_text = content[3:end].strip()
            try:
                import yaml
                fm = yaml.safe_load(fm_text) or {}
                if isinstance(fm, dict):
                    name = str(fm.get("name", "")).strip()
                    description = str(fm.get("description", "")).strip()
            except ImportError:
                for line in fm_text.split("\n"):
                    if line.startswith("name:"):
                        name = line.split(":", 1)[1].strip().strip('"').strip("'")
                    elif line.startswith("description:"):
                        description = line.split(":", 1)[1].strip().strip('"').strip("'")

    if not description:
        # Fallback: first non-heading, non-empty line after frontmatter
        body_start = content.find("\n---", 3) + 4 if content.startswith("---") else 0
        body = content[body_start:].strip()
        for line in body.split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                description = line
                break

    return name, description


def _scan_skills_index() -> dict[str, list[tuple[str, str]]]:
    """Scan skill directories and return {category: [(name, description), ...]}.

    User skills override bundled skills with the same name.
    """
    skills_by_category: dict[str, list[tuple[str, str]]] = {}
    seen_names: set[str] = set()

    for skills_dir in (_BUNDLED_SKILLS_DIR, _get_user_skills_dir()):
        if not skills_dir.exists():
            continue
        for skill_md in skills_dir.rglob("SKILL.md"):
            if any(part in (".git", "__pycache__") for part in skill_md.parts):
                continue
            try:
                content = skill_md.read_text(encoding="utf-8")[:4000]
            except Exception:
                continue

            name, description = _parse_skill_frontmatter(content)
            if not name:
                name = skill_md.parent.name

            if name in seen_names:
                continue
            seen_names.add(name)

            try:
                rel = skill_md.relative_to(skills_dir)
                parts = rel.parts
                category = parts[0] if len(parts) >= 3 else "general"
            except ValueError:
                category = "general"

            if len(description) > 200:
                description = description[:197] + "..."

            skills_by_category.setdefault(category, []).append((name, description))

    return skills_by_category


def build_skills_prompt(allowed: set = None) -> str:
    """Build a compact skill index for the system prompt.

    ``allowed`` (a set of skill names) restricts the index to those skills;
    None includes everything. The directory scan is cached (invalidates when
    skill files change); filtering runs on the cached scan. Returns empty
    string if no skills remain.
    """
    global _SKILLS_CACHE, _SKILLS_CACHE_KEY

    bundled_key = _dir_cache_key(_BUNDLED_SKILLS_DIR)
    user_key = _dir_cache_key(_get_user_skills_dir())
    current_key = (bundled_key, user_key)

    with _SKILLS_CACHE_LOCK:
        if _SKILLS_CACHE is not None and _SKILLS_CACHE_KEY == current_key:
            skills_by_category = _SKILLS_CACHE
        else:
            skills_by_category = _scan_skills_index()
            _SKILLS_CACHE = skills_by_category
            _SKILLS_CACHE_KEY = current_key

    if allowed is not None:
        skills_by_category = {
            category: [item for item in items if item[0] in allowed]
            for category, items in skills_by_category.items()
        }

    if not any(skills_by_category.values()):
        return ""

    index_lines = []
    for category in sorted(skills_by_category.keys()):
        if not skills_by_category[category]:
            continue
        index_lines.append(f"  {category}:")
        for name, desc in sorted(skills_by_category[category], key=lambda x: x[0]):
            if desc:
                index_lines.append(f"    - {name}: {desc}")
            else:
                index_lines.append(f"    - {name}")

    return (
        "# Available Skills\n"
        "Scan the skills below. If one matches your task, load it with "
        "skill_view(name) and follow its instructions. "
        "(Save/update skills with skill_manage.)\n\n"
        "<available_skills>\n"
        + "\n".join(index_lines) + "\n"
        "</available_skills>\n\n"
        "If none match, proceed normally."
    )


def load_kbs_preamble(root: str) -> str:
    """Load the distilled KBS protocol preamble and substitute the resolved root.

    Returns an empty string if the bundled protocol file is missing, so a missing
    file degrades to no-preamble rather than crashing the system-prompt build.
    """
    try:
        text = (Path(__file__).parent / "kbs_protocol.md").read_text(encoding="utf-8")
    except Exception:
        return ""
    return text.replace("<root>", str(root))


def _platform_hint() -> str:
    """One-line OS + shell hint matching what the terminal tool actually uses.

    terminal.py runs commands via subprocess(shell=True) → cmd.exe on Windows,
    /bin/sh on POSIX. Surfacing this lets the agent pick the right command
    dialect instead of guessing from the path string.
    """
    os_name = _platform.system()  # Windows / Linux / Darwin
    shell = "cmd.exe" if os_name == "Windows" else "sh"
    return f"{os_name} · shell: {shell}"


@dataclass
class PromptCtx:
    """Everything a layer may branch on, built once per system-prompt build."""

    tools: set
    platform: str = ""
    skills_allowed: set = None
    cwd: str = None
    identity_override: str = None
    agent_roster: str = None
    kbs_preamble: str = None
    kbs_read_note: bool = False
    load_claude_md: bool = True
    load_cursorrules: bool = True
    project_context: bool = True
    language: str = "zh"


Layer = Callable[[PromptCtx], Optional[str]]


def _passthrough(field: str) -> Layer:
    def layer(ctx: PromptCtx) -> Optional[str]:
        return getattr(ctx, field)
    return layer


def _tool_guard(names: set, text: str) -> Layer:
    def layer(ctx: PromptCtx) -> Optional[str]:
        return text if names & ctx.tools else None
    return layer


def _identity(ctx: PromptCtx) -> Optional[str]:
    if ctx.identity_override:
        return ctx.identity_override
    from core.prompt_context import load_soul_md
    return load_soul_md() or AGENT_IDENTITY


def _platform_prompt(ctx: PromptCtx) -> Optional[str]:
    return PLATFORM_PROMPTS.get(ctx.platform)


def _tool_use(ctx: PromptCtx) -> Optional[str]:
    if not ctx.tools:
        return None
    parts = [TOOL_USE_ENFORCEMENT]
    mtu = build_mandatory_tool_use(ctx.tools)
    if mtu:
        parts.append(mtu)
    return "\n\n".join(parts)


def _memory_guidance(ctx: PromptCtx) -> Optional[str]:
    lines = [text for name, text in (
        ("memory", MEMORY_READ_GUIDANCE),
        ("memory_manage", MEMORY_SAVE_GUIDANCE),
    ) if name in ctx.tools]
    if not lines:
        return None
    return "# Memory\n\n" + "\n".join(lines)


def _kbs_subagent_note(ctx: PromptCtx) -> Optional[str]:
    if ctx.kbs_read_note and "kbs_search" in ctx.tools:
        return KBS_SUBAGENT_READ_NOTE
    return None


def _wants_mcp_guidance(tools: set) -> bool:
    # MCP_GUIDANCE explains editing mcp_servers in config.yaml — inject it
    # only for agents that have MCP tools or can actually edit that file.
    return any(t.startswith("mcp_") for t in tools) or \
        bool({"write_file", "patch", "terminal"} & tools)


def _mcp_guidance(ctx: PromptCtx) -> Optional[str]:
    return MCP_GUIDANCE if _wants_mcp_guidance(ctx.tools) else None


# Coding discipline governs WRITING code. Read tools live in the base
# toolset every agent has, so keying on them would push editing rules into
# read-only agents — write/execute faces are the signal.
CODING_TOOLS = {"write_file", "patch", "terminal"}


def _coding_guidance(ctx: PromptCtx) -> Optional[str]:
    if not CODING_TOOLS & ctx.tools:
        return None
    delegate = ("; delegate_task for multi-step or parallel work"
                if "delegate_task" in ctx.tools else "")
    item8 = ("8. **Plan + track** — todo for 3+ step tasks"
             f"{delegate}. Self-review high-stakes conclusions before answering.")
    return CODING_GUIDANCE + "\n" + item8


def _behavior_rules(ctx: PromptCtx) -> str:
    behavior = BEHAVIOR_RULES
    if "model_info" in ctx.tools:
        behavior += MODEL_INFO_RULE
    if "request_tools" in ctx.tools:
        behavior += REQUEST_TOOLS_RULE
    directive = language_directive(ctx.language)
    if directive:
        # Unconditional tail — numbered after the conditional 5/6 so the list
        # reads 1-4, optional 5/6, always 7, any toolset.
        behavior += f"7. **语言**: {directive}\n"
    return behavior


def _skills_index(ctx: PromptCtx) -> Optional[str]:
    if not {"skill_manage", "skill_view", "skills_list"} & ctx.tools:
        return None
    parts = []
    prompt = build_skills_prompt(allowed=ctx.skills_allowed)
    if prompt:
        parts.append(prompt)
    if "skill_manage" in ctx.tools:
        parts.append(SKILLS_GUIDANCE)
    return "\n\n".join(parts) if parts else None


def _cwd_hint(ctx: PromptCtx) -> Optional[str]:
    if not ctx.cwd:
        return None
    return f"# Working Directory\n{ctx.cwd}  ({_platform_hint()})"


def _project_context(ctx: PromptCtx) -> Optional[str]:
    if not ctx.project_context:
        return None
    from core.prompt_context import load_project_context
    return load_project_context(
        cwd=ctx.cwd,
        include_claude_md=ctx.load_claude_md,
        include_cursorrules=ctx.load_cursorrules,
    ) or None


# Table order is section order. Groups: identity → discipline → tool
# guidance → runtime context.
LAYERS: list[Layer] = [
    # —— Identity ——
    _identity,
    _passthrough("kbs_preamble"),
    _platform_prompt,
    # —— Discipline ——
    _tool_use,
    _behavior_rules,
    # —— Tool guidance (each row teaches only when its tool is loaded) ——
    _memory_guidance,
    _tool_guard({"session_search"}, SESSION_SEARCH_GUIDANCE),
    _tool_guard({"delegate_task"}, DELEGATE_GUIDANCE),
    _passthrough("agent_roster"),
    _tool_guard({"external_agent"}, EXTERNAL_AGENT_GUIDANCE),
    _tool_guard({"browser_login"}, BROWSER_LOGIN_GUIDANCE),
    _tool_guard({"cronjob"}, CRON_GUIDANCE),
    _kbs_subagent_note,
    _mcp_guidance,
    _coding_guidance,
    # —— Runtime context ——
    _skills_index,
    _cwd_hint,
    _project_context,
]


def build_system_prompt(
    platform: str = "",
    *,
    identity_override: str = None,
    available_tools: set = None,
    skills_allowed: set = None,
    project_context: bool = True,
    agent_roster: str = None,
    load_claude_md: bool = True,
    load_cursorrules: bool = True,
    kbs_preamble: str = None,
    kbs_read_note: bool = False,
    cwd: str = None,
    language: str = "zh",
) -> str:
    """Assemble the system prompt by walking LAYERS with a PromptCtx.

    Every tool-conditional section keys off available_tools so guidance never
    advertises a tool the agent cannot call. Table order == section order.

    The memory snapshot is NOT built here — chat() builds it per turn and
    injects it at the API boundary, keeping this text stable for caching.
    """
    ctx = PromptCtx(
        tools=available_tools or set(),
        platform=platform,
        skills_allowed=skills_allowed,
        cwd=cwd,
        identity_override=identity_override,
        agent_roster=agent_roster,
        kbs_preamble=kbs_preamble,
        kbs_read_note=kbs_read_note,
        load_claude_md=load_claude_md,
        load_cursorrules=load_cursorrules,
        project_context=project_context,
        language=language,
    )
    from core.config import expand_agent_vars
    return expand_agent_vars("\n\n".join(
        p for p in (layer(ctx) for layer in LAYERS) if p))
