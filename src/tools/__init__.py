"""Lightweight tool registry with auto-discovery.

Each tool module calls ``registry.register()`` at import time to declare its
schema, handler, toolset membership, and availability check.  The agent loop
queries the registry for schemas and dispatches tool calls through it.

Import chain (circular-import safe):
    tools/__init__.py  (no imports from agent or tool files)
           ^
    tools/*.py  (import from tools at module level)
           ^
    core/agent.py  (imports tools.registry + all tool modules via load_all_tools)
"""

import importlib
import json
import logging
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from tools._approvals import evaluate, remember_rule

logger = logging.getLogger(__name__)


class ToolEntry:
    """Metadata for a single registered tool."""

    __slots__ = (
        "name", "toolset", "schema", "handler", "check_fn",
        "requires_env", "is_async", "description",
        "max_result_size_chars", "read_only", "description_modifier",
        "subagent_blocked", "path_params",
    )

    def __init__(self, name, toolset, schema, handler, check_fn,
                 requires_env, is_async, description,
                 max_result_size_chars=None, read_only=False,
                 description_modifier=None, subagent_blocked=False,
                 path_params=()):
        self.name = name
        self.toolset = toolset
        self.schema = schema
        self.handler = handler
        self.check_fn = check_fn
        self.requires_env = requires_env or []
        self.is_async = is_async
        self.description = description or schema.get("description", "")
        self.max_result_size_chars = max_result_size_chars
        self.read_only = read_only
        self.description_modifier = description_modifier
        self.subagent_blocked = subagent_blocked
        # Names of args that are filesystem paths relative to the agent's cwd.
        # ``dispatch`` rewrites these (absolute paths pass through; URLs skipped)
        # before the handler runs, so handlers don't each reimplement cwd logic.
        self.path_params = tuple(path_params or ())


class ToolRegistry:
    """Central registry that collects tool schemas + handlers from tool files."""

    def __init__(self):
        self._tools: Dict[str, ToolEntry] = {}
        self._toolset_checks: Dict[str, Callable] = {}
        # Guards _tools/_toolset_checks mutation vs get_schemas iteration —
        # background MCP discovery registers tools while agent turns are
        # already resolving schemas (dict insert during values() iteration
        # raises RuntimeError).
        self._lock = threading.RLock()
        # Bumped on every register/deregister — lets external caches
        # (serve capabilities) detect late-arriving MCP tools cheaply.
        self._version = 0
        # get_schemas result cache — see get_schemas for the key/invalidation.
        self._schemas_cache: Dict[tuple, List[dict]] = {}

    def register(
        self,
        name: str,
        schema: dict,
        handler: Callable,
        check_fn: Optional[Callable] = None,
        toolset: str = "default",
        requires_env: list = None,
        is_async: bool = False,
        description: str = "",
        max_result_size_chars: int | float | None = None,
        read_only: bool = False,
        description_modifier: Optional[Callable[[str, set], str]] = None,
        subagent_blocked: bool = False,
        path_params=(),
    ):
        """Register a tool.  Called at module-import time by each tool file.

        Args:
            description_modifier: Optional callback ``fn(base_description, available_tools) -> str``
                that dynamically adjusts the tool description based on which other tools
                are currently available.  *available_tools* is the set of tool names
                that passed their ``check_fn`` in the current ``get_schemas()`` call.
        """
        with self._lock:
            existing = self._tools.get(name)
            if existing and existing.toolset != toolset:
                logger.warning(
                    "Tool name collision: '%s' (toolset '%s') is being "
                    "overwritten by toolset '%s'",
                    name, existing.toolset, toolset,
                )
            self._tools[name] = ToolEntry(
                name=name,
                toolset=toolset,
                schema=schema,
                handler=handler,
                check_fn=check_fn,
                requires_env=requires_env or [],
                is_async=is_async,
                description=description or schema.get("description", ""),
                max_result_size_chars=max_result_size_chars,
                read_only=read_only,
                description_modifier=description_modifier,
                subagent_blocked=subagent_blocked,
                path_params=path_params,
            )
            if check_fn and toolset not in self._toolset_checks:
                self._toolset_checks[toolset] = check_fn
            self._schemas_cache.clear()
            self._version += 1

    def deregister(self, name: str) -> None:
        """Remove a tool from the registry.

        Also cleans up the toolset check if no other tools remain in the
        same toolset.  Used by MCP dynamic tool discovery.
        """
        with self._lock:
            entry = self._tools.pop(name, None)
            if entry is None:
                return
            if entry.toolset in self._toolset_checks and not any(
                e.toolset == entry.toolset for e in self._tools.values()
            ):
                self._toolset_checks.pop(entry.toolset, None)
            self._schemas_cache.clear()
            self._version += 1
        logger.debug("Deregistered tool: %s", name)

    def version(self) -> int:
        """Change counter over register/deregister — external caches key on
        this to notice late-arriving MCP tools without rescanning."""
        return self._version

    def get_schemas(self, names: Optional[Set[str]] = None,
                    toolsets: Optional[Set[str]] = None,
                    subagent: bool = False) -> list[dict]:
        """Return tool schemas for the API call, only including available tools.

        If *names* is provided, only those tools are considered.
        If *toolsets* is provided, the toolsets are first resolved (via
        ``core.toolsets.resolve_toolset`` for composable includes) to get
        tool names, then only those tools are included.
        Only tools whose ``check_fn()`` returns True (or have no check_fn)
        are included.

        Tool descriptions are dynamically adjusted via ``description_modifier``
        callbacks based on which tools are available in this call.

        Results are cached per (config stamp, roster, subagent): the agent
        loop calls this every iteration and the uncached cost measured ~95ms
        — check_fns re-read config.yaml and the schema rebuild re-expands
        ${AGENT_HOME} over every tool every time. Invalidation: register/
        deregister clears; the config stamp catches config-gated check_fns
        (kbs.enabled etc.) and check_fn outcomes they influence.
        """
        from core.config import config_stamp

        # Whole call under the registry lock: the _tools iteration below
        # races concurrent register() from background MCP discovery, and the
        # check_fn calls are cheap (import probes / config reads). register()
        # never runs inside a get_schemas call, so a stale-cache store after
        # a concurrent clear can't happen.
        with self._lock:
            cache_key = (
                config_stamp(),
                frozenset(toolsets) if toolsets is not None else None,
                frozenset(names) if names is not None else None,
                subagent,
            )
            cached = self._schemas_cache.get(cache_key)
            if cached is not None:
                return cached

            # Resolve composable toolsets to tool names
            resolved_tool_names = None
            if toolsets is not None:
                resolved_tool_names = set()
                for ts in toolsets:
                    # Try composable resolution first
                    try:
                        from core.toolsets import resolve_toolset
                        resolved_tool_names.update(resolve_toolset(ts))
                    except (ImportError, Exception):
                        pass
                    # Also include tools directly in that toolset in the registry
                    for entry in self._tools.values():
                        if entry.toolset == ts:
                            resolved_tool_names.add(entry.name)

            # Phase 1: determine which tools are available
            check_results: Dict[Callable, bool] = {}
            available_entries: list[ToolEntry] = []
            tool_names = names if names is not None else set(self._tools.keys())

            for name in sorted(tool_names):
                entry = self._tools.get(name)
                if not entry:
                    continue
                if subagent and entry.subagent_blocked:
                    continue
                if resolved_tool_names is not None and name not in resolved_tool_names:
                    continue
                if entry.check_fn:
                    if entry.check_fn not in check_results:
                        try:
                            check_results[entry.check_fn] = bool(entry.check_fn())
                        except Exception:
                            check_results[entry.check_fn] = False
                            logger.debug("Tool %s check raised; skipping", name)
                    if not check_results[entry.check_fn]:
                        logger.debug("Tool %s unavailable (check failed)", name)
                        continue
                available_entries.append(entry)

            available_names = {e.name for e in available_entries}

            # Phase 2: build schemas with dynamic descriptions
            from core.config import expand_agent_vars

            def _expand_strs(obj):
                """Recursively expand ${AGENT_HOME} in all schema strings (top-level
                description AND nested parameter descriptions) so they reference the
                actual data root."""
                if isinstance(obj, str):
                    return expand_agent_vars(obj)
                if isinstance(obj, dict):
                    return {k: _expand_strs(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [_expand_strs(x) for x in obj]
                return obj

            result = []
            for entry in available_entries:
                schema = entry.schema
                if "function" in schema:
                    func = {**schema["function"], "name": entry.name}
                    # Apply description modifier if present
                    if entry.description_modifier:
                        base_desc = func.get("description", entry.description)
                        func["description"] = entry.description_modifier(base_desc, available_names)
                    # Expand ${AGENT_HOME} everywhere in the schema (top-level + nested)
                    func = _expand_strs(func)
                    result.append({"type": "function", "function": func})
                else:
                    result.append({**schema, "name": entry.name})
            if len(self._schemas_cache) > 64:
                self._schemas_cache.clear()
            self._schemas_cache[cache_key] = result
            return result

    _PW_THREAD_ERROR = "cannot switch to a different thread"

    def dispatch(self, name: str, arguments: str, context: dict = None, **kwargs) -> str:
        """Execute a tool handler by name, return JSON string result.

        * Handlers receive ``(args, **kw)`` — context/kwargs flow through
          as keyword arguments, NOT injected into the args dict.
        * Async handlers are bridged automatically via ``_run_async()``.
        * All exceptions are caught and returned as ``{"error": "..."}``
          for consistent error format.
        * Playwright greenlet death is detected and handled with recovery
          guidance (browser_state_load / browser_login).

        Args:
            context: Optional dict passed as kwarg ``context=...``
                     containing chat_id, platform, session_key, etc.
            **kwargs: Extra kwargs forwarded to the handler (e.g. task_id).
        """
        entry = self._tools.get(name)
        if not entry:
            return tool_error(f"Unknown tool: {name}")
        try:
            args = json.loads(arguments) if arguments else {}
            kw = dict(kwargs)
            if context:
                kw["context"] = context
            # 危险操作审批门（唯一汇聚点）。无 parent_agent（cron no_agent
            # 脚本、测试直调）= 用户直接驱动，跳过。deny 规则命中即拒——
            # 不等用户、不弹窗；"ask" 批准且 always 时把该调用记入会话记忆。
            _ag = kw.get("parent_agent")
            if _ag is not None:
                _sk = getattr(_ag, "_approval_shared", {}).get("session_key")
                _decision, _summary = evaluate(
                    name, args, getattr(_ag, "config", None), _sk,
                    aux=getattr(_ag, "aux", None))
                if _decision == "deny":
                    return tool_error(
                        f"操作被审批策略拒绝（{_summary}），未执行。不得改用其他工具或"
                        f"命令实现相同效果；如确需调整，请向用户说明，由用户修改审批配置。",
                        blocked=True, approval=_summary,
                    )
                if _decision == "ask":
                    _approved, _why, _always = _ag.request_approval(name, _summary)
                    if not _approved:
                        return tool_error(
                            f"操作未获批准（{_why}），未执行。这是用户的否决：不得改用任何"
                            f"其他工具或命令（terminal、patch、重定向等）实现相同效果。"
                            f"如确有必要，请向用户说明理由，等用户明确批准后再执行。",
                            blocked=True, approval=_summary,
                        )
                    if _always:
                        remember_rule(_sk, name, args,
                                      getattr(_ag, "config", None))
            # Resolve cwd-relative path args against the invoking agent's cwd,
            # centrally, so individual tools don't each reimplement it. Tools
            # declare their path-typed params via ``path_params`` at registration.
            # Absolute paths pass through; URLs are skipped (image tools accept
            # path|URL).
            if entry.path_params:
                from tools._paths import resolve_path
                ag = kw.get("parent_agent")
                for _pk in entry.path_params:
                    _pv = args.get(_pk)
                    if not _pv:
                        continue
                    if isinstance(_pv, str) and _pv.startswith(("http://", "https://")):
                        continue
                    if isinstance(_pv, list):
                        args[_pk] = [str(resolve_path(str(x), ag)) for x in _pv if x]
                    else:
                        args[_pk] = str(resolve_path(str(_pv), ag))
            if entry.is_async:
                result = _run_async(entry.handler(args, **kw))
            else:
                result = entry.handler(args, **kw)
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False)

            # Do NOT call _full_restart() here — it would touch Playwright from
            # the dispatch (agent) thread and reintroduce the cross-thread bug.
            # Recovery is the caller's job (browser_state_load / browser_login).
            if name.startswith("browser_") and ToolRegistry._PW_THREAD_ERROR in result:
                return tool_error(
                    "Browser session lost (Playwright internal thread exited). "
                    "To recover: call browser_state_load(name='wiki') to restore a "
                    "saved session, or browser_login(url='...') to start fresh, "
                    "then retry your operation."
                )

            return result
        except Exception as e:
            if name.startswith("browser_") and ToolRegistry._PW_THREAD_ERROR in str(e):
                return tool_error(
                    "Browser session lost (Playwright internal thread exited). "
                    "To recover: call browser_state_load(name='wiki') or "
                    "browser_login(url='...')."
                )
            logger.exception("Tool %s dispatch error: %s", name, e)
            return tool_error(f"Tool execution failed: {type(e).__name__}: {e}")

    def get_max_result_size(self, name: str, default: int | float | None = None) -> int | float:
        """Return per-tool max result size, or *default*."""
        entry = self._tools.get(name)
        if entry and entry.max_result_size_chars is not None:
            return entry.max_result_size_chars
        if default is not None:
            return default
        return 30000

    def get_all_tool_names(self) -> List[str]:
        """Return sorted list of all registered tool names."""
        return sorted(self._tools.keys())

    def get_schema(self, name: str) -> Optional[dict]:
        """Return a tool's raw schema dict, bypassing check_fn filtering."""
        entry = self._tools.get(name)
        return entry.schema if entry else None

    def get_toolset_for_tool(self, name: str) -> Optional[str]:
        """Return the toolset a tool belongs to, or None."""
        entry = self._tools.get(name)
        return entry.toolset if entry else None

    def is_read_only(self, name: str) -> bool:
        """Check if a tool is marked as read-only (safe for parallel execution)."""
        entry = self._tools.get(name)
        return entry.read_only if entry else False

    def get_tool_to_toolset_map(self) -> Dict[str, str]:
        """Return ``{tool_name: toolset_name}`` for every registered tool."""
        with self._lock:
            return {name: e.toolset for name, e in self._tools.items()}

    def snapshot_names(self) -> List[str]:
        """Locked copy of all registered tool names (safe while background
        MCP discovery registers)."""
        with self._lock:
            return list(self._tools.keys())

    def is_toolset_available(self, toolset: str) -> bool:
        """Check if a toolset's requirements are met."""
        check = self._toolset_checks.get(toolset)
        if not check:
            return True
        try:
            return bool(check())
        except Exception:
            logger.debug("Toolset %s check raised; marking unavailable", toolset)
            return False

    def check_toolset_requirements(self) -> Dict[str, bool]:
        """Return ``{toolset: available_bool}`` for every toolset."""
        with self._lock:
            toolsets = set(e.toolset for e in self._tools.values())
        return {ts: self.is_toolset_available(ts) for ts in sorted(toolsets)}

    def get_available_toolsets(self) -> Dict[str, dict]:
        """Return toolset metadata for UI display."""
        toolsets: Dict[str, dict] = {}
        with self._lock:
            entries = list(self._tools.values())
        for entry in entries:
            ts = entry.toolset
            if ts not in toolsets:
                toolsets[ts] = {
                    "available": self.is_toolset_available(ts),
                    "tools": [],
                    "requirements": [],
                }
            toolsets[ts]["tools"].append(entry.name)
            for env in entry.requires_env:
                if env not in toolsets[ts]["requirements"]:
                    toolsets[ts]["requirements"].append(env)
        return toolsets

    def check_tool_availability(self, quiet: bool = False):
        """Return (available_toolsets, unavailable_info)."""
        available = []
        unavailable = []
        seen = set()
        with self._lock:
            entries = list(self._tools.values())
        for entry in entries:
            ts = entry.toolset
            if ts in seen:
                continue
            seen.add(ts)
            if self.is_toolset_available(ts):
                available.append(ts)
            else:
                unavailable.append({
                    "name": ts,
                    "env_vars": entry.requires_env,
                    "tools": [e.name for e in entries if e.toolset == ts],
                })
        return available, unavailable


registry = ToolRegistry()


def tool_error(message, **extra) -> str:
    """Return a JSON error string for tool handlers.

    >>> tool_error("file not found")
    '{"error": "file not found"}'
    >>> tool_error("bad input", path="/foo")
    '{"error": "bad input", "path": "/foo"}'
    """
    result = {"error": str(message)}
    if extra:
        result.update(extra)
    return json.dumps(result, ensure_ascii=False)


def tool_result(data=None, **kwargs) -> str:
    """Return a JSON result string for tool handlers.

    Accepts a dict positional arg *or* keyword arguments (not both):

    >>> tool_result(success=True, count=42)
    '{"success": true, "count": 42}'
    >>> tool_result({"key": "value"})
    '{"key": "value"}'
    """
    if data is not None:
        return json.dumps(data, ensure_ascii=False)
    return json.dumps(kwargs, ensure_ascii=False)


def _run_async(coro, timeout: float = 60.0):
    """Run an async coroutine from sync context."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result(timeout=timeout)
    except RuntimeError:
        pass

    return asyncio.run(coro)


_TOOLS_LOADED = False

# Background MCP discovery state — see _start_mcp_discovery.
_MCP_READY = threading.Event()
_MCP_THREAD_STARTED = False


def _start_mcp_discovery() -> None:
    """Kick off MCP discovery in a daemon thread.

    Discovery connects to every configured server over the network (measured
    ~10s against the internal ones) — running it inline blocked every process
    start and the first agent turn on it. Tools register as servers come up:
    registry.register is serialized against get_schemas by the registry lock,
    and register clears the schemas cache, so running turns pick them up on
    the next iteration. Long-running modes (gateway/serve/CLI REPL) just let
    it land; short-lived callers use wait_for_mcp().
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
    """
    global _TOOLS_LOADED
    if _TOOLS_LOADED:
        return
    _TOOLS_LOADED = True
    tools_dir = Path(__file__).parent
    for py_file in sorted(tools_dir.glob("*.py")):
        name = py_file.stem
        if name.startswith("_") or name == "__init__":
            continue
        try:
            importlib.import_module(f"tools.{name}")
        except Exception as e:
            logger.warning("Failed to load tool module %s: %s", name, e)

    _start_mcp_discovery()

    # After the full registry is in place — the specialist-agent collision
    # check needs to see every statically registered tool name.
    try:
        from tools.specialist_agent_tool import register_specialist_agent_tools
        register_specialist_agent_tools()
    except Exception as e:
        logger.warning("Specialist-agent registration failed: %s", e)
