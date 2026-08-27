"""MCP (Model Context Protocol) client with dynamic tool registration.

Connects to MCP servers via stdio or HTTP/StreamableHTTP transport using the
official ``mcp`` SDK, discovers their tools, and registers each one directly
into the tool registry so the agent can call them like any built-in tool.

No intermediate proxy tool — every MCP tool appears as a native tool with a
prefixed name (``mcp_{server}_{tool}``), and its schema is injected into the
LLM API call just like built-in tools.

Architecture:
  A background asyncio event loop runs in a daemon thread. Each MCP server
  runs as a long-lived asyncio Task on this loop, keeping its transport alive.
  Tool call coroutines are scheduled via ``run_coroutine_threadsafe()``.

  On shutdown, each server Task is signalled to exit, ensuring clean resource
  teardown happens in the same Task that opened the connection (required by
  anyio cancel-scopes).

  Auto-reconnection with exponential backoff (up to 5 retries) for servers
  that were previously connected but lost their connection.

Transports:
  - **stdio**: spawn a subprocess, communicate via stdin/stdout.
    Config: ``command``, ``args``, ``env``.
  - **HTTP/StreamableHTTP**: connect to a remote MCP server endpoint.
    Config: ``url``, ``headers``.
    Requires ``mcp`` SDK >= 1.2.0 with streamable_http support.

  Transport type is determined by:
    1. Explicit ``type`` field (``stdio`` / ``streamable-http`` / ``sse`` / ``http``)
    2. Has ``url`` field → HTTP
    3. Has ``command`` field → stdio

Security:
  - Environment variable filtering for stdio subprocesses (blocks API keys,
    tokens, secrets; only passes safe baseline variables + explicit env)
  - Credential stripping in error messages (ghp_, sk-, Bearer, token=, etc.)
  - Tool name collision detection (skips MCP tools that clash with built-ins)
  - Safe command resolution (bare commands resolved via shutil.which)

Configuration (config.yaml):
    mcp_servers:
      filesystem:                              # stdio transport
        type: stdio                            # optional, inferred from command
        command: "npx"
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        env: {}
        timeout: 120                           # per-tool-call timeout (seconds)
        connect_timeout: 60                    # initial connection timeout

      github:
        command: "npx"
        args: ["-y", "@modelcontextprotocol/server-github"]
        env:
          GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_..."
        timeout: 180

      wecom_doc:                               # HTTP/StreamableHTTP transport
        type: streamable-http
        url: "https://qyapi.weixin.qq.com/mcp/robot-doc?apikey=..."
        headers: {}                            # optional custom HTTP headers
        timeout: 120

The ``mcp`` Python package is optional -- if not installed, this module is a
no-op and logs a debug message.  HTTP transport additionally requires
``mcp.client.streamable_http`` (available in mcp >= 1.2.0).

Startup:
  Called from ``SharedContext.__init__()`` → ``discover_mcp_tools()``.
  Loads config, connects to all servers in parallel, registers tools.

Shutdown:
  ``shutdown_mcp_servers()`` — signals all server Tasks to exit, waits
  for clean teardown, then stops the background event loop.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import threading
import time
from typing import Any, Dict, List, Optional

from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

_MCP_AVAILABLE = False
_MCP_HTTP_AVAILABLE = False
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    _MCP_AVAILABLE = True
    try:
        from mcp.client.streamable_http import streamablehttp_client
        _MCP_HTTP_AVAILABLE = True
    except ImportError:
        logger.debug("mcp streamable_http not available -- HTTP transport disabled")
except ImportError:
    logger.debug("mcp package not installed -- MCP tool support disabled")


_DEFAULT_TOOL_TIMEOUT = 120      # seconds for tool calls
_DEFAULT_CONNECT_TIMEOUT = 60    # seconds for initial connection per server
_MAX_RECONNECT_RETRIES = 5
_MAX_BACKOFF_SECONDS = 60

# Security helpers (env filtering + secret scrubbing) are shared with
# ``core.external_agent`` — see ``core.safe_env``. Imported + aliased here so
# this module's internal call sites keep working without per-call edits, and so
# the external-agent driver does NOT have to import this module (whose top-level
# load requires the ``mcp`` package).
from core.safe_env import (
    build_safe_env as _build_safe_env,
    sanitize_error as _sanitize_error,
)


def _format_exc_chain(exc: BaseException, max_depth: int = 6) -> str:
    """Render an exception into a readable string, unwrapping asyncio
    ExceptionGroup (which otherwise prints as 'unhandled errors in a TaskGroup
    (N sub-exception(s))' and hides the real cause) and following __cause__.
    """
    parts: list[str] = []
    seen: set[int] = set()

    def walk(e: BaseException, depth: int, prefix: str = ""):
        if depth > max_depth or id(e) in seen:
            return
        seen.add(id(e))
        subs = getattr(e, "exceptions", None)  # ExceptionGroup / BaseExceptionGroup
        label = f"{prefix}{type(e).__name__}: {e}"
        if subs:
            parts.append(label)
            for i, sub in enumerate(subs):
                walk(sub, depth + 1, prefix=f"  sub[{i}]: ")
        else:
            parts.append(label)
        cause = e.__cause__
        if cause is not None and id(cause) not in seen:
            parts.append(f"{prefix}caused by:")
            walk(cause, depth + 1, prefix=prefix + "  ")

    walk(exc, 0)
    return "\n".join(parts) if parts else f"{type(exc).__name__}: {exc}"


def _describe_mcp_target(cfg: dict) -> str:
    """One-line, secret-safe description of where an MCP server connects to."""
    if "url" in cfg:
        return f"http {cfg['url']}"
    cmd = cfg.get("command", "")
    args = cfg.get("args", []) or []
    rendered = " ".join([str(cmd)] + [str(a) for a in args])
    return f"stdio [{_sanitize_error(rendered)}]"


def _resolve_stdio_command(command: str, env: dict) -> tuple:
    """Resolve a bare command to an absolute path if possible."""
    resolved = os.path.expanduser(str(command).strip())
    if os.sep not in resolved:
        path_val = env.get("PATH", "")
        which_hit = shutil.which(resolved, path=path_val)
        if which_hit:
            resolved = which_hit
    return resolved, env


class MCPServerTask:
    """Manages a single MCP server connection in a dedicated asyncio Task."""

    __slots__ = (
        "name", "session", "tool_timeout",
        "_task", "_ready", "_shutdown_event", "_tools", "_error", "_config",
        "_registered_tool_names",
    )

    def __init__(self, name: str):
        self.name = name
        self.session: Optional[Any] = None
        self.tool_timeout: float = _DEFAULT_TOOL_TIMEOUT
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        self._tools: list = []
        self._error: Optional[Exception] = None
        self._config: dict = {}
        self._registered_tool_names: list = []

    def _is_http(self) -> bool:
        """Check if this server uses HTTP transport.

        Resolution order:
          1. Explicit ``type`` field: "streamable-http" / "sse" → HTTP
          2. Has ``url`` but no ``command`` → HTTP
          3. Otherwise → stdio
        """
        explicit_type = (self._config.get("type") or "").lower()
        if explicit_type in ("streamable-http", "sse", "http"):
            return True
        if "url" in self._config:
            return True
        return False

    async def _discover_tools(self):
        """Discover tools from the connected session."""
        if self.session is None:
            return
        tools_result = await self.session.list_tools()
        self._tools = (
            tools_result.tools
            if hasattr(tools_result, "tools")
            else []
        )

    async def _run_stdio(self, config: dict):
        """Run the server using stdio transport."""
        command = config.get("command")
        args = config.get("args", [])
        user_env = config.get("env")

        if not command:
            raise ValueError(f"MCP server '{self.name}': no 'command' in config")

        safe_env = _build_safe_env(user_env)
        command, safe_env = _resolve_stdio_command(command, safe_env)

        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=safe_env if safe_env else None,
        )

        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                self.session = session
                await self._discover_tools()
                self._ready.set()
                await self._shutdown_event.wait()

    async def _run_http(self, config: dict):
        """Run the server using HTTP/StreamableHTTP transport."""
        if not _MCP_HTTP_AVAILABLE:
            raise ImportError(
                f"MCP server '{self.name}' requires HTTP transport but "
                "mcp.client.streamable_http is not available. "
                "Upgrade the mcp package (>=1.2.0) to get HTTP support."
            )

        url = config["url"]
        headers = dict(config.get("headers") or {})
        connect_timeout = config.get("connect_timeout", _DEFAULT_CONNECT_TIMEOUT)

        async with streamablehttp_client(url, headers=headers, timeout=float(connect_timeout)) as (
            read_stream, write_stream, _get_session_id,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                self.session = session
                await self._discover_tools()
                self._ready.set()
                await self._shutdown_event.wait()

    async def run(self, config: dict):
        """Connect, discover tools, wait, disconnect. Includes auto-reconnect."""
        self._config = config
        self.tool_timeout = config.get("timeout", _DEFAULT_TOOL_TIMEOUT)

        if "url" in config and "command" in config:
            logger.warning(
                "MCP server '%s' has both 'url' and 'command' — using HTTP ('url')",
                self.name,
            )

        retries = 0
        backoff = 1.0

        while True:
            try:
                if self._is_http():
                    await self._run_http(config)
                else:
                    await self._run_stdio(config)
                # Clean exit (shutdown requested)
                break
            except Exception as exc:
                self.session = None

                # First connection attempt failed — report and stop
                if not self._ready.is_set():
                    self._error = exc
                    self._ready.set()
                    return

                # Shutdown requested — don't reconnect
                if self._shutdown_event.is_set():
                    return

                retries += 1
                if retries > _MAX_RECONNECT_RETRIES:
                    logger.warning(
                        "MCP server '%s' failed after %d reconnection attempts: %s",
                        self.name, _MAX_RECONNECT_RETRIES, exc,
                    )
                    return

                logger.warning(
                    "MCP server '%s' connection lost (attempt %d/%d), "
                    "reconnecting in %.0fs: %s",
                    self.name, retries, _MAX_RECONNECT_RETRIES, backoff, exc,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

                if self._shutdown_event.is_set():
                    return
            finally:
                self.session = None

    async def start(self, config: dict):
        """Create the background Task and wait until ready (or failed)."""
        self._task = asyncio.ensure_future(self.run(config))
        await self._ready.wait()
        if self._error:
            raise self._error

    async def shutdown(self):
        """Signal the Task to exit and wait for clean teardown."""
        self._shutdown_event.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP server '%s' shutdown timed out, cancelling task",
                    self.name,
                )
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
        self.session = None


_servers: Dict[str, MCPServerTask] = {}

_mcp_loop: Optional[asyncio.AbstractEventLoop] = None
_mcp_thread: Optional[threading.Thread] = None
_lock = threading.Lock()


def _mcp_loop_exception_handler(loop, context):
    """Suppress benign 'Event loop is closed' noise during shutdown."""
    exc = context.get("exception")
    if isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc):
        return
    loop.default_exception_handler(context)


def _ensure_mcp_loop():
    """Start the background event loop thread if not already running."""
    global _mcp_loop, _mcp_thread
    with _lock:
        if _mcp_loop is not None and _mcp_loop.is_running():
            return
        _mcp_loop = asyncio.new_event_loop()
        _mcp_loop.set_exception_handler(_mcp_loop_exception_handler)
        _mcp_thread = threading.Thread(
            target=_mcp_loop.run_forever,
            name="mcp-event-loop",
            daemon=True,
        )
        _mcp_thread.start()


def _run_on_mcp_loop(coro, timeout: float = 30):
    """Schedule a coroutine on the MCP event loop and block until done."""
    with _lock:
        loop = _mcp_loop
    if loop is None or not loop.is_running():
        raise RuntimeError("MCP event loop is not running")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


def _stop_mcp_loop():
    """Stop the background event loop and join its thread."""
    global _mcp_loop, _mcp_thread
    with _lock:
        loop = _mcp_loop
        thread = _mcp_thread
        _mcp_loop = None
        _mcp_thread = None
    if loop is not None:
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)
        try:
            loop.close()
        except Exception:
            pass


def _load_mcp_config() -> Dict[str, dict]:
    """Read ``mcp_servers`` from config.yaml, merged with store-installed entries.

    Returns a dict of ``{server_name: server_config}`` or empty dict. Values are
    read literally from the single config.yaml source (no ``${VAR}`` expansion,
    no environment-variable override); store-installed declarations (capability
    store ledger) are added underneath — config.yaml wins on name collision,
    so a hand-written entry can always override a store install.
    """
    try:
        from core.config import load_config
        config = load_config()
    except Exception:
        return {}

    servers = config.get("mcp_servers")
    servers = dict(servers) if isinstance(servers, dict) else {}
    try:
        from core.store import store_installed_mcp
        for name, cfg in store_installed_mcp().items():
            servers.setdefault(name, cfg)
    except Exception:
        # swallowed here = store-installed MCP servers silently vanish from
        # the agent's roster
        logger.warning("store MCP merge failed — store-installed servers unavailable",
                       exc_info=True)
    return servers


def _make_tool_handler(server_name: str, tool_name: str, tool_timeout: float):
    """Return a sync handler that calls an MCP tool via the background loop."""

    def _handler(args: dict, **kwargs) -> str:
        with _lock:
            server = _servers.get(server_name)
        if not server or not server.session:
            return json.dumps({"error": f"MCP server '{server_name}' is not connected"})

        async def _call():
            result = await server.session.call_tool(tool_name, arguments=args)
            if getattr(result, "isError", False):
                error_text = ""
                for block in (result.content or []):
                    if hasattr(block, "text"):
                        error_text += block.text
                return json.dumps({"error": _sanitize_error(error_text or "MCP tool returned an error")})

            parts: List[str] = []
            for block in (result.content or []):
                if hasattr(block, "text"):
                    parts.append(block.text)
            text_result = "\n".join(parts) if parts else ""

            structured = getattr(result, "structuredContent", None)
            if structured is not None:
                return json.dumps({"result": structured})
            return json.dumps({"result": text_result})

        try:
            return _run_on_mcp_loop(_call(), timeout=tool_timeout)
        except Exception as exc:
            logger.error("MCP tool %s/%s call failed: %s", server_name, tool_name, exc)
            return json.dumps({"error": _sanitize_error(f"MCP call failed: {type(exc).__name__}: {exc}")})

    return _handler


def _make_check_fn(server_name: str):
    """Return a check function that verifies the MCP connection is alive."""

    def _check() -> bool:
        with _lock:
            server = _servers.get(server_name)
        return server is not None and server.session is not None

    return _check


def _normalize_mcp_input_schema(schema: dict | None) -> dict:
    """Normalize MCP input schemas for LLM tool-calling compatibility."""
    if not schema:
        return {"type": "object", "properties": {}}
    if schema.get("type") == "object" and "properties" not in schema:
        return {**schema, "properties": {}}
    return schema


def _sanitize_mcp_name(value: str) -> str:
    """Sanitize an MCP name component for tool naming."""
    return re.sub(r"[^A-Za-z0-9_]", "_", str(value or ""))


def _convert_mcp_schema(server_name: str, mcp_tool) -> dict:
    """Convert an MCP tool to the registry schema format.

    Tool names are prefixed as ``mcp_{server}_{tool}`` to avoid collisions.
    Schema uses the {"type": "function", "function": {...}} format expected
    by the OpenAI API and the registry's get_schemas() method.
    """
    safe_tool = _sanitize_mcp_name(mcp_tool.name)
    safe_server = _sanitize_mcp_name(server_name)
    prefixed_name = f"mcp_{safe_server}_{safe_tool}"
    return {
        "type": "function",
        "function": {
            "name": prefixed_name,
            "description": mcp_tool.description or f"MCP tool {mcp_tool.name} from {server_name}",
            "parameters": _normalize_mcp_input_schema(mcp_tool.inputSchema),
        },
    }


def _register_server_tools(name: str, server: MCPServerTask, config: dict) -> List[str]:
    """Register tools from a connected server into the registry."""
    registered_names: List[str] = []
    toolset_name = f"mcp-{name}"

    for mcp_tool in server._tools:
        schema = _convert_mcp_schema(name, mcp_tool)
        tool_name_prefixed = schema["function"]["name"]

        existing_toolset = registry.get_toolset_for_tool(tool_name_prefixed)
        if existing_toolset and not existing_toolset.startswith("mcp-"):
            logger.warning(
                "MCP server '%s': tool '%s' collides with built-in toolset '%s' — skipping",
                name, tool_name_prefixed, existing_toolset,
            )
            continue

        registry.register(
            name=tool_name_prefixed,
            toolset=toolset_name,
            schema=schema,
            handler=_make_tool_handler(name, mcp_tool.name, server.tool_timeout),
            check_fn=_make_check_fn(name),
        )
        registered_names.append(tool_name_prefixed)

    return registered_names


def _sync_mcp_toolsets():
    """Populate the ``mcp`` toolset with registered MCP tools."""
    from core.toolsets import TOOLSETS

    all_mcp_tools: List[str] = []
    with _lock:
        for server in _servers.values():
            all_mcp_tools.extend(server._registered_tool_names)

    if "mcp" in TOOLSETS:
        TOOLSETS["mcp"]["tools"] = all_mcp_tools


def _existing_tool_names() -> List[str]:
    """Return tool names for all currently connected servers."""
    names: List[str] = []
    with _lock:
        for server in _servers.values():
            names.extend(server._registered_tool_names)
    return names


async def _connect_server(name: str, config: dict) -> MCPServerTask:
    """Create an MCPServerTask, start it, and return when ready."""
    server = MCPServerTask(name)
    await server.start(config)
    return server


async def _discover_and_register_server(name: str, config: dict) -> List[str]:
    """Connect to a single MCP server, discover tools, and register them."""
    connect_timeout = config.get("connect_timeout", _DEFAULT_CONNECT_TIMEOUT)
    server = await asyncio.wait_for(
        _connect_server(name, config),
        timeout=connect_timeout,
    )
    with _lock:
        _servers[name] = server

    registered_names = _register_server_tools(name, server, config)
    server._registered_tool_names = list(registered_names)

    logger.info(
        "MCP server '%s' (%s): registered %d tool(s): %s",
        name, "http" if "url" in config else "stdio",
        len(registered_names), ", ".join(registered_names),
    )
    return registered_names


def discover_mcp_tools() -> List[str]:
    """Entry point: load config, connect to MCP servers, register tools.

    Called during agent initialization. Safe to call even when the ``mcp``
    package is not installed (returns empty list).

    Idempotent for already-connected servers.

    Returns:
        List of all registered MCP tool names.
    """
    if not _MCP_AVAILABLE:
        logger.debug("MCP SDK not available -- skipping MCP tool discovery")
        return []

    servers = _load_mcp_config()
    if not servers:
        logger.debug("No MCP servers configured")
        return []

    with _lock:
        new_servers = {
            name: cfg for name, cfg in servers.items()
            if name not in _servers
        }

    if not new_servers:
        return _existing_tool_names()

    _ensure_mcp_loop()

    async def _discover_all():
        server_names = list(new_servers.keys())
        results = await asyncio.gather(
            *(_discover_and_register_server(name, cfg) for name, cfg in new_servers.items()),
            return_exceptions=True,
        )
        for name, result in zip(server_names, results):
            if isinstance(result, BaseException):
                cfg = new_servers.get(name, {})
                logger.warning(
                    "Failed to connect to MCP server '%s' [%s]: %s",
                    name,
                    _describe_mcp_target(cfg),
                    _sanitize_error(_format_exc_chain(result)),
                )

    try:
        _run_on_mcp_loop(_discover_all(), timeout=120)
    except Exception as exc:
        logger.error("MCP discovery failed: %s", exc)

    _sync_mcp_toolsets()

    with _lock:
        connected = [n for n in new_servers if n in _servers]
        new_tool_count = sum(
            len(getattr(_servers[n], "_registered_tool_names", []))
            for n in connected
        )
    failed = len(new_servers) - len(connected)
    if new_tool_count or failed:
        summary = f"MCP: registered {new_tool_count} tool(s) from {len(connected)} server(s)"
        if failed:
            summary += f" ({failed} failed)"
        logger.info(summary)

    return _existing_tool_names()


def shutdown_mcp_servers():
    """Close all MCP server connections and stop the background loop."""
    with _lock:
        servers_snapshot = list(_servers.values())

    if not servers_snapshot:
        _stop_mcp_loop()
        return

    async def _shutdown():
        results = await asyncio.gather(
            *(server.shutdown() for server in servers_snapshot),
            return_exceptions=True,
        )
        for server, result in zip(servers_snapshot, results):
            if isinstance(result, Exception):
                logger.debug("Error closing MCP server '%s': %s", server.name, result)
        with _lock:
            _servers.clear()

    with _lock:
        loop = _mcp_loop
    if loop is not None and loop.is_running():
        try:
            future = asyncio.run_coroutine_threadsafe(_shutdown(), loop)
            future.result(timeout=15)
        except Exception as exc:
            logger.debug("Error during MCP shutdown: %s", exc)

    _stop_mcp_loop()


def remove_mcp_server(name: str) -> bool:
    """Disconnect one server, deregister its tools, and drop it from the pool.

    The store uninstall path; ``shutdown_mcp_servers`` is global-only. Returns
    False when the server isn't connected (nothing to remove).
    """
    with _lock:
        server = _servers.get(name)
        if server is None:
            return False

    async def _remove():
        try:
            await server.shutdown()
        except Exception as exc:
            logger.debug("Error closing MCP server '%s': %s", name, exc)

    with _lock:
        loop = _mcp_loop
    if loop is not None and loop.is_running():
        try:
            future = asyncio.run_coroutine_threadsafe(_remove(), loop)
            future.result(timeout=15)
        except Exception as exc:
            logger.debug("Error removing MCP server '%s': %s", name, exc)

    for tool_name in list(getattr(server, "_registered_tool_names", [])):
        registry.deregister(tool_name)
    with _lock:
        _servers.pop(name, None)
    _sync_mcp_toolsets()
    logger.info("MCP server '%s' removed", name)
    return True


def get_mcp_status() -> List[dict]:
    """Return status of all configured MCP servers for banner display."""
    result: List[dict] = []
    configured = _load_mcp_config()
    if not configured:
        return result

    with _lock:
        active_servers = dict(_servers)

    for name, cfg in configured.items():
        server = active_servers.get(name)
        transport = "http" if "url" in cfg else "stdio"
        if server and server.session is not None:
            result.append({
                "name": name,
                "transport": transport,
                "tools": len(server._registered_tool_names),
                "connected": True,
            })
        else:
            result.append({
                "name": name,
                "transport": transport,
                "tools": 0,
                "connected": False,
            })

    return result
