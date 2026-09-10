"""Tool package bootstrap.

The registry contract (``ToolRegistry`` / ``registry`` / ``tool_result`` /
``tool_error`` / ``_run_async``) lives in ``core.registry`` and is
re-exported here so tool modules keep the
``from tools import registry, tool_result, tool_error`` import form.

This module stays import-light — no tool-module imports at module level:
importing a tool module registers its tools (side effect), and that must
only happen via ``load_all_tools()`` from the composition root
(core.context). Primitives this package exposes: ``load_all_tools()``
(static imports only), ``start_mcp_discovery()`` (background MCP connect),
``wait_for_mcp()``. Process-level orchestration — calling these in order,
plus specialist registration — lives in SharedContext, not here.
"""

import importlib
import logging
import threading
from pathlib import Path

from core.registry import (  # re-exported contract — see core.registry
    ToolEntry,
    ToolRegistry,
    _run_async,
    registry,
    tool_error,
    tool_result,
)

__all__ = [
    "ToolEntry", "ToolRegistry", "_run_async", "registry",
    "tool_error", "tool_result", "load_all_tools", "start_mcp_discovery",
    "wait_for_mcp",
]

logger = logging.getLogger(__name__)


_TOOLS_LOADED = False

# Background MCP discovery state — see _start_mcp_discovery.
_MCP_READY = threading.Event()
_MCP_THREAD_STARTED = False


def start_mcp_discovery() -> None:
    """Kick off MCP discovery in a daemon thread.

    Discovery connects to every configured server over the network (measured
    ~10s against the internal ones) — running it inline blocked every process
    start and the first agent turn on it. Tools register as servers come up:
    registry.register is serialized against get_schemas by the registry lock,
    and register clears the schemas cache, so running turns pick them up on
    the next iteration. Long-running modes (gateway/serve/CLI REPL) just let
    it land; short-lived callers use wait_for_mcp(). Idempotent; owned by the
    composition root (SharedContext), not load_all_tools.
    """
    global _MCP_THREAD_STARTED
    if _MCP_THREAD_STARTED:
        return
    _MCP_THREAD_STARTED = True

    def _run():
        try:
            from tools.mcp_tool import discover_mcp_tools
            discover_mcp_tools()
        except Exception:
            logger.warning("MCP discovery failed", exc_info=True)
        finally:
            _MCP_READY.set()

    threading.Thread(target=_run, name="mcp-discovery", daemon=True).start()


def wait_for_mcp(timeout: float = 15.0) -> None:
    """Block until background MCP discovery finishes (or *timeout* passes).

    For one-shot processes whose single turn may want MCP tools — a
    long-running mode is better served by starting immediately and letting
    tools arrive mid-flight.
    """
    _MCP_READY.wait(timeout)


def load_all_tools():
    """Import all tool modules so they self-register via registry.register().

    Scans the tools/ directory for .py files (excluding __init__.py and
    private _*.py modules) and imports them. Each module calls
    registry.register() at import time. Idempotent: called from both
    SharedContext and XiheAgent.__init__, but only the first call does work.

    PyInstaller frozen builds: the .py sources live inside the PYZ archive
    (no physical files in the tools/ dir), so the directory scan finds
    nothing — fall back to the explicit module list below. Keep it in sync
    when adding/renaming tool modules.

    Static registration only — no network, no dynamic tools. MCP discovery
    (``start_mcp_discovery``) and specialist registration are process-level
    policy owned by the composition root (SharedContext), not this function.
    """
    global _TOOLS_LOADED
    if _TOOLS_LOADED:
        return
    _TOOLS_LOADED = True
    tools_dir = Path(__file__).parent
    py_files = sorted(tools_dir.glob("*.py"))
    if py_files:
        names = [
            p.stem for p in py_files
            if not p.stem.startswith("_") and p.stem != "__init__"
        ]
    else:
        # Frozen (PyInstaller) environment: physical sources absent → explicit list.
        names = [
            "browser_tool", "clarify_tool", "computer_tool", "cronjob_tools",
            "delegate_tool", "execute_code", "external_agent_tool", "file_tools",
            "http_tool", "image_generation_tool", "kbs_tool", "maven_tool",
            "mcp_tool", "memory_tool", "model_info_tool", "node_version_tool",
            "ocr_tool", "process_tool", "request_tools_tool", "sandbox_tool",
            "send_message_tool", "session_search_tool", "skills_tool",
            "skill_manager_tool", "specialist_agent_tool", "ssh_tool",
            "terminal", "todo_tool", "tts_tool", "vision_tools",
            "web_record_tool", "web_tools",
        ]
    for name in names:
        try:
            importlib.import_module(f"tools.{name}")
        except Exception as e:
            logger.warning("Failed to load tool module %s: %s", name, e)
