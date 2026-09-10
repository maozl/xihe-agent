"""SystemMixin — the system-probe business (health / readiness /
test-connection / agents) and its routes.

One business module like chat/admin/browser: a mixin on ServeApp, sharing its
state (capabilities cache lives on the shell — chat's stream hello uses it
too). The ServeApp class itself and the process assembly live in server.py,
the composition root.
"""

import asyncio
import logging

from aiohttp import web

from core.config import AGENT_HOME

logger = logging.getLogger(__name__)


class SystemMixin:
    async def health(self, request):
        return web.json_response({
            "ok": True,
            "version": self.version,
            "mode": "serve",
            "model": self.config.get("model"),
            "capabilities": self.capabilities(),
        })

    async def readiness(self, request):
        """Structured readiness for onboarding UIs — what's missing and the
        fix, instead of the client waiting for a first send to fail.

        /health stays a lean liveness probe ({ok, version}); this endpoint
        costs a registry scan and answers "why not".
        """
        from core.services.diagnostics import readiness_report
        mode = request.query.get("mode", "chat")
        if mode not in ("chat", "gateway"):
            mode = "chat"
        report = readiness_report(self.config, mode=mode)
        report["version"] = self.version
        return web.json_response(report)

    async def test_connection(self, request):
        """Server-side model-connection test (api_key never crosses the API).

        The desktop's「测试连接」button hits this instead of fetching from
        the renderer, which has no key.
        """
        from core.services.diagnostics import check_connectivity
        result = await asyncio.to_thread(
            check_connectivity, self.config.get("base_url"),
            self.config.get("api_key"))
        result["model_configured"] = self.config.get("model")
        return web.json_response(result)

    async def agents(self, request):
        return web.json_response({"agents": [{
            "id": "self",
            "name": self.config.get("agent_name", "xihe"),
            "engine": "xihe",
            "shape": "process",
            "model": self.config.get("model"),
            "status": "online",
            "capabilities": self.capabilities(),
            "dataRoot": str(AGENT_HOME),
            "description": "Local xihe instance (this serve process).",
        }]})


def add_routes(router, app):
    """The system-probe routes. ``app`` is the ServeApp instance."""
    router.add_get("/health", app.health)
    router.add_get("/readiness", app.readiness)
    router.add_post("/test-connection", app.test_connection)
    router.add_get("/agents", app.agents)
