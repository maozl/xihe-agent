"""Toolset definitions — named groups of tools for scoping an agent's reach.

Usage:
    from core.toolsets import resolve_toolset, get_all_toolsets

    # Resolve a toolset to get all tool names
    tools = resolve_toolset("web")  # -> ["web_search", "browser_navigate", ...]

    # Multiple toolsets
    from core.toolsets import resolve_multiple_toolsets
    tools = resolve_multiple_toolsets(["web", "files"])
"""

from typing import Optional


TOOLSETS = {
    "base": {
        "label": "基础底座",
        "description": "全员强制：读面（自己/知识/文件）+ 自治 + 内存计算沙箱，XiheAgent 构造时自动并入",
        "tools": ["todo", "model_info", "skills_list", "skill_view",
                  "memory", "kbs_search", "kbs_status",
                  "read_file", "search_files", "directory_tree",
                  "run_sandbox_code"],
    },
    "web": {
        "label": "网页与搜索",
        "description": "浏览器自动化全套 + 网页搜索/抓取/截图",
        "tools": ["web_search", "web_extract", "web_crawl",
                  "browser_navigate", "browser_snapshot", "browser_click",
                  "browser_type", "browser_scroll", "browser_back",
                  "browser_forward", "browser_reload", "browser_press",
                  "browser_hover", "browser_select", "browser_upload",
                  "browser_check", "browser_uncheck", "browser_drag",
                  "browser_screenshot", "browser_console", "browser_vision",
                  "browser_wait", "browser_eval", "browser_close",
                  "browser_tab_new", "browser_tab_list",
                  "browser_tab_switch", "browser_tab_close",
                  "browser_frame", "browser_cookies",
                  "browser_state_save", "browser_state_load",
                  "browser_state_list", "browser_state_delete",
                  "browser_connect", "browser_login", "browser_logout",
                  "browser_record", "browser_record_start", "browser_record_stop"],
    },
    "files": {
        "label": "文件写",
        "description": "文件写入与补丁（读面在 base 组）",
        "tools": ["write_file", "patch"],
    },
    "terminal": {
        "label": "终端与进程",
        "description": "终端命令、进程管理",
        "tools": ["terminal", "process"],
    },
    "dev_tool": {
        "label": "代码与环境",
        "description": "代码执行、依赖/版本探测（maven/node）",
        "tools": ["execute_code", "maven_dep", "node_version"],
    },
    "http": {
        "label": "网络请求",
        "description": "通用 HTTP 请求、工具请求样例",
        "tools": ["http", "request_tools"],
    },
    "memory": {
        "label": "记忆写与会话检索",
        "description": "记忆写入/删除、历史会话检索（记忆读取在 base 组）",
        "tools": ["memory_manage", "session_search"],
    },
    "communication": {
        "label": "消息通知",
        "description": "跨平台发消息/发图、向用户澄清提问",
        "tools": ["send_message", "send_image", "clarify"],
    },
    "media": {
        "label": "图像与语音",
        "description": "视觉分析、OCR、图像生成、语音合成",
        "tools": ["vision_analyze", "image_ocr", "image_generate", "text_to_speech"],
    },
    "agent": {
        "label": "委派",
        "description": "子任务委派（todo/model_info 在 base 组）",
        "tools": ["delegate_task"],
    },
    "external_agents": {
        "label": "外置 agent",
        "description": "external_agent 工具：把子任务交给外部 CLI agent（claude）执行",
        "tools": ["external_agent"],
    },
    "skills": {
        "label": "技能管理",
        "description": "技能创建/修改/删除（索引与查看在 base 组）",
        "tools": ["skill_manage"],
    },
    "scheduler": {
        "label": "定时任务",
        "description": "cron 定时作业管理",
        "tools": ["cronjob"],
    },
    "mcp": {
        "label": "MCP 工具",
        "description": "全部 MCP 服务器工具",
        "tools": [],
    },
    "ssh": {
        "label": "SSH 远程",
        "description": "SSH 连接与远程命令执行",
        "tools": ["ssh_connect", "ssh_exec", "ssh_disconnect", "ssh_status"],
    },
    "kbs": {
        "label": "业务知识库",
        "description": "知识库初始化（检索与状态在 base 组，受 kbs.enabled 闸）",
        "tools": ["kbs_init"],
    },
    "meta": {
        "label": "能力扩展",
        "description": "按需请求追加工具集（web/media/scheduler）",
        "tools": ["request_tools"],
    },
}


def resolve_toolset(name: str) -> list[str]:
    """Resolve a toolset name to its tool names (copy; unknown → [])."""
    return list(TOOLSETS.get(name, {}).get("tools", []))


def resolve_multiple_toolsets(toolset_names: list[str]) -> list[str]:
    """Resolve multiple toolsets and combine their tools."""
    all_tools = set()
    for name in toolset_names:
        all_tools.update(resolve_toolset(name))
    return list(all_tools)


def get_all_toolsets() -> dict:
    """Return all toolset definitions."""
    return TOOLSETS.copy()


def validate_toolset(name: str) -> bool:
    """Check if a toolset name is valid."""
    return name in TOOLSETS


def get_toolset_info(name: str) -> Optional[dict]:
    """Get detailed information about a toolset including resolved tools."""
    toolset = TOOLSETS.get(name)
    if not toolset:
        return None
    resolved = resolve_toolset(name)
    return {
        "name": name,
        "label": toolset.get("label", name),
        "description": toolset["description"],
        "direct_tools": toolset["tools"],
        "resolved_tools": resolved,
        "tool_count": len(resolved),
    }

import logging as _logging
_logger = _logging.getLogger(__name__)

# Conditional toolsets: expanded on demand via the request_tools meta tool.
CONDITIONAL_TOOLSETS = {
    "web":       "browser automation (navigate/click/type on web pages), website login (SSO), screenshots, web search, recording web operations into a skill",
    "media":     "image analysis (vision), OCR (text from images), text-to-speech (voice synthesis)",
    "scheduler": "cron jobs, scheduled/recurring tasks, timers",
}

# Subagents (delegate children + specialists) never see these, regardless of
# roster — registry flags are the enforcement, this set is the documented
# source of truth that tests assert against. Rationale per group:
#   recursion:        delegate_task, run_*_agent (dynamic, not listed here)
#   user face:        clarify, send_message, send_image
#   escalation / persistent mutation:
#                     cronjob (cron jobs run unrestricted), skill_manage,
#                     kbs_init, web_record, browser_record*, browser_state_delete
#                     (shared login-state asset)
SUBAGENT_BLOCKED_TOOLS = {
    "delegate_task", "clarify", "send_message", "send_image",
    "cronjob", "skill_manage", "kbs_init",
    "web_record", "browser_record", "browser_record_start",
    "browser_record_stop", "browser_state_delete",
}


def normalize_toolset_names(names, *, where: str = "", warnings: list = None):
    """Validate a toolset-name list (main agent config and agents/*.yaml alike).

    Returns None for a list containing "*" (= every toolset, bypasses name
    filtering), otherwise the validated list. Unknown static names are dropped
    with a warning; "mcp" and "mcp-<server>" are always kept (a server may
    register later). None/absent input → [] — not configured means not loaded.
    """
    if names is None:
        return []
    clean = [n for n in names if isinstance(n, str)]
    if "*" in clean:
        return None
    out = []
    for n in clean:
        if n == "mcp" or n.startswith("mcp-") or n in TOOLSETS:
            out.append(n)
            continue
        msg = f"{where}: unknown toolset '{n}' dropped" if where else f"unknown toolset '{n}' dropped"
        _logger.warning(msg)
        if warnings is not None:
            warnings.append(msg)
    return out


def resolve_roster(spec: dict, *, where: str = "", warnings: list = None):
    """toolsets + skills from one spec mapping — shared by the main agent
    (config.yaml top-level keys) and specialists (agents/<slug>.yaml), which
    use the same key names and the same semantics:

      absent / []  → nothing loaded
      ["*"]        → None → unrestricted
      names        → whitelist ("mcp" / "mcp-<server>" always kept)

    Returns (toolsets, skills); each is a list or None (= "*").
    """
    toolsets = normalize_toolset_names(spec.get("toolsets"), where=where,
                                       warnings=warnings)
    if toolsets == []:
        msg = (f"{where}: toolsets not configured — agent gets no tools"
               if where else "toolsets not configured — agent gets no tools")
        _logger.warning(msg)
        if warnings is not None:
            warnings.append(msg)
    raw_skills = [s for s in spec.get("skills") or [] if isinstance(s, str)]
    skills = None if "*" in raw_skills else raw_skills
    return toolsets, skills
