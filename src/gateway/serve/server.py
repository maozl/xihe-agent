"""serve composition root: the ServeApp shell + assembly + entry point.

ServeApp — the mixin-composition shell with the shared state — lives HERE,
next to run_serve that composes and runs the process (reading "how serve is
put together" shouldn't hop files). Business handlers live in business-module
mixins (system / chat / admin / browser), each owning its add_routes."""

import asyncio
import logging
import threading

from aiohttp import web

# AccessLogger moved from aiohttp.helpers to aiohttp.web_log in 3.10; the
# helpers re-export was removed in 3.14. Try the new home first, fall back so
# this still imports on older aiohttp (internal mirror / other machines).
try:
    from aiohttp.web_log import AccessLogger
except ImportError:  # aiohttp < 3.10 kept it in helpers
    from aiohttp.helpers import AccessLogger

from gateway.serve import admin, browser, chat, knowledge, system, terminal
from gateway.serve._common import _PLATFORM
from gateway.serve.admin import AdminMixin
from gateway.serve.browser import BrowserMixin
from gateway.serve.chat import ChatMixin
from gateway.serve.system import SystemMixin

logger = logging.getLogger(__name__)


def _capabilities(toolsets) -> list[str]:
    """High-level capability descriptor advertised to the desktop.

    The desktop UI branches on these flags (capability-driven), never on the
    engine name. Scoped to the MAIN agent's resolved roster (``toolsets`` from
    config.yaml top-level keys, None = unrestricted) and filtered through the
    same ``check_fn`` gates ``XiheAgent`` applies — a slim main roster must not
    advertise browser/mcp flags the main agent can't actually call.
    """
    caps = ["text", "streaming", "tools", "interrupt", "sessions", "thoughts",
            "approvals"]
    try:
        from tools import registry
        schemas = registry.get_schemas(
            toolsets=set(toolsets) if toolsets is not None else None)
        names = {
            (s.get("function") or {}).get("name") or s.get("name") or ""
            for s in schemas
        }
        if any(n.startswith("browser") for n in names):
            caps.append("browser")
        if "vision_analyze" in names or "image_ocr" in names:
            caps.append("vision")
        if "image_generation" in names:
            caps.append("image_generation")
        if any(n.startswith("mcp_") for n in names):
            caps.append("mcp")
    except Exception:
        # capability flags silently degrade the desktop UI — leave a trace
        logger.debug("capability probe failed", exc_info=True)
    return caps


class ServeApp(SystemMixin, AdminMixin, BrowserMixin, ChatMixin):
    """Mixin-composition shell: shared state only. Business behavior comes
    from the business mixins; _source/_turn_lock live on ChatMixin."""

    def __init__(self, shared_ctx, version: str):
        self.ctx = shared_ctx
        self.config = shared_ctx.config
        self.version = version
        # conv_id → active XiheAgent, for /interrupt (guarded by _active_lock)
        self._active: dict[str, object] = {}
        self._active_lock = threading.Lock()
        # conv_id → asyncio.Lock (the dict itself is event-loop-thread only;
        # the lock guards the await span) — see ChatMixin._turn_lock.
        self._turn_locks: dict[str, asyncio.Lock] = {}
        # conv_id → WebSocketResponse an in-flight turn streams to (None after
        # the socket died — turn keeps running detached). A reconnected client
        # re-attaches via the `attach` frame. Event-loop thread only, no lock.
        self._conv_sockets: dict[str, object] = {}
        # Every live desktop WS (app-level, one per client — all conversations
        # multiplex over it). Push events (cron_result …) broadcast here and
        # the client routes by conv_id; _conv_sockets above is ONLY the
        # "socket currently streaming this conv's active turn" routing table.
        self._live_sockets: set = set()
        # Capabilities cached per registry version — /health is polled every
        # few seconds, and background MCP discovery registers tools after the
        # port opens, so a compute-once cache would permanently miss "mcp".
        self._caps: list[str] | None = None
        self._caps_ver = -1

    def capabilities(self) -> list[str]:
        from tools import registry
        if self._caps is None or self._caps_ver != registry.version():
            self._caps = _capabilities(self.ctx.main_toolsets)
            self._caps_ver = registry.version()
        return self._caps

# CORS — the Electron renderer (file/app origin) fetches REST cross-origin to
# http://127.0.0.1:<port>. WS doesn't enforce same-origin, so only REST needs it.

async def _on_response_prepare(request, response):
    if isinstance(response, web.WebSocketResponse):
        return  # WS upgrades don't go through here; skip defensively
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"


async def _options(request):
    return web.Response(status=204)


async def _kill_local_channels(app):
    """Resident processes (the `process` tool) must not outlive the supervisor
    that owns them — serve dying would otherwise orphan the web backends the
    user believes are managed."""
    from tools import _local_tap

    _local_tap.stop_all()


class _ServeAccessLogger(AccessLogger):
    """Access logger that drops the desktop's ``/health`` liveness polls.

    The desktop's ServeSupervisor GETs /health every few seconds for readiness
    + steady-state liveness (see ``serve.ts``), so those INFO lines otherwise
    dominate agent.log as near-identical noise. Every other endpoint
    (/sessions, /mcp, /skills, /cron) is rare and worth keeping for
    diagnostics — only /health is suppressed. The browser panel polls
    /browser/status every 5s and re-snaps every frame during a width-handle
    drag, so both join the suppression list.
    """

    def log(self, request, response, time):
        if request.path.startswith(("/health", "/browser/status", "/browser/snap")):
            return
        super().log(request, response, time)


def run_serve(config: dict, host: str = "127.0.0.1", port: int = 7788,
              version: str = "0.0.0"):
    """Entry point for the ``xihe serve`` subcommand."""
    from core.context import SharedContext

    # (config.yaml is seeded by the CLI entry point BEFORE load_config — see
    # cmd_serve; seeding here would be too late for this process's config.)
    if not config.get("api_key"):
        logger.warning("serve: api_key not set — chat turns will fail until "
                       "config.yaml is filled in and serve is restarted")

    shared_ctx = SharedContext(config)
    # One explicit process bootstrap (tools + MCP + specialists + cron; the
    # scheduler lock yields to a gateway on the same agent_home).
    from core.context import bootstrap_process
    bootstrap_process(shared_ctx)
    app_obj = ServeApp(shared_ctx, version=version)

    # Desktop push channel: cron results reach the creating conversation over
    # its WS. "serve" alias routes `deliver: origin` jobs; explicit
    # `desktop:<conv_id>` targets work too.
    from core.services.scheduler import register_channel
    from gateway.serve.chat import DesktopChannel
    _desktop_channel = DesktopChannel(app_obj)
    register_channel("desktop", _desktop_channel)
    register_channel("serve", _desktop_channel)

    aio = web.Application()
    aio.on_response_prepare.append(_on_response_prepare)
    aio.on_shutdown.append(_kill_local_channels)
    aio.router.add_route("OPTIONS", "/{tail:.*}", _options)
    system.add_routes(aio.router, app_obj)
    chat.add_routes(aio.router, app_obj)
    admin.add_routes(aio.router, app_obj)
    browser.add_routes(aio.router, app_obj)
    knowledge.add_routes(aio.router)
    terminal.add_routes(aio.router)

    logger.info("xihe serve listening on http://%s:%d (platform=%s, toolsets=%s)",
                host, port, _PLATFORM, shared_ctx.main_toolsets)
    print(f"xihe serve → http://{host}:{port}\n"
          f"  ws   /stream                              (send/interrupt → streaming events)\n"
          f"  get  /health /agents /sessions /convs/{{conv_id}}/messages\n"
          f"  get  /mcp /skills /cron /specialists /store   (管理面板/商店)\n"
          f"  get  /memory /kbs /kbs/page; put/del /memory  (记忆/知识库)\n"
          f"  get  /ssh/live; ws /ssh/live/stream; post /ssh/live/connect  (终端面板)\n"
          f"  get  /local/live; ws /local/live/stream; post /local/live/stop  (agent 本地命令/常驻进程)\n"
          f"  post /convs/{{conv_id}}/reset /convs/{{conv_id}}/title\n"
          f"  post /store/install /store/uninstall /store/mount /store/refresh\n"
          f"  get  /browser/status                        (浏览器面板)\n"
          f"  post /browser/launch /browser/snap /browser/hide /browser/show /browser/release /browser/appearance /browser/restart\n"
          f"  put  /specialists/{{slug}}               (专家 agents 编辑)\n"
          f"  del  /specialists/{{slug}} /convs/{{conv_id}}")
    web.run_app(aio, host=host, port=port, print=None,
                access_log_class=_ServeAccessLogger)
