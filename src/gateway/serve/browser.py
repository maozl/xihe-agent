"""CDP Chrome window control (ServeApp's BrowserMixin) — the desktop browser
panel's lifecycle endpoints, delegating to gateway.serve.browser_window."""

import asyncio
import logging

from aiohttp import web

logger = logging.getLogger(__name__)

# Bounded wait for a Win32 entry point. All of them can block until the
# Chrome process pumps messages (cold start / hung window); the loop trades
# a "busy" answer for never being wedged. 2.5s stays under the desktop's own
# 3s fetch abort so the client gets JSON instead of a dropped connection.
_WIN_TIMEOUT_S = 2.5


class BrowserMixin:
    """Mixin into ServeApp — state lives in ServeApp.__init__."""

    async def _win(self, fn, *args):
        """Run one browser_window entry point on its dedicated Win32 thread
        (see browser_window._win_pool) with a bounded await."""
        from gateway.serve import browser_window
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(browser_window._win_pool, lambda: fn(*args)),
                _WIN_TIMEOUT_S)
        except asyncio.TimeoutError:
            # The worker stays blocked on the native call until Chrome pumps;
            # later calls queue behind it on the single thread and drain then.
            return {"ok": False, "reason": "busy"}
        except Exception:
            logger.exception("browser handler crashed")
            return {"ok": False, "reason": "internal-error"}

    async def browser_status(self, request):
        from gateway.serve import browser_window
        from tools.browser_tool import _cdp_port_open
        loop = asyncio.get_running_loop()
        running = await loop.run_in_executor(None, lambda: _cdp_port_open(0.3))
        return web.json_response(await self._win(browser_window.status, running))

    async def browser_launch(self, request):
        """Blocking Popen + port poll — executor, like store's fetch_catalog.

        launch() itself never touches browser_window._state; the trailing
        status() runs on the Win32 thread like every other entry point."""
        from gateway.serve import browser_window
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, browser_window.launch)
        out = dict(res)
        out.update(await self._win(browser_window.status))
        return web.json_response(out)

    async def browser_appearance(self, request):
        """Desktop pushes its light/dark so CDP Chrome launches matching it.
        Persists under browser/ runtime state (not config); applies to the
        next launch — a running Chrome needs /browser/restart."""
        from tools.browser_tool import set_appearance
        body = await self._browser_body(request)
        dark = body.get("dark")
        if not isinstance(dark, bool):
            return web.json_response(
                {"ok": False, "reason": "bad body: dark bool required"}, status=400)
        return self._browser_safe(lambda: set_appearance(dark))

    async def browser_restart(self, request):
        """Blocking taskkill + relaunch — executor, same shape as launch()."""
        from gateway.serve import browser_window
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, browser_window.restart)
        out = dict(res)
        out.update(await self._win(browser_window.status))
        return web.json_response(out)

    def _browser_safe(self, result_fn):
        """ctypes calls can raise despite the API's never-raise contract; a
        raw 500 text body breaks the client's JSON parse, so log + answer
        with a JSON error instead."""
        try:
            return web.json_response(result_fn())
        except Exception:
            logger.exception("browser handler crashed")
            return web.json_response({"ok": False, "reason": "internal-error"}, status=500)

    async def _browser_body(self, request) -> dict | None:
        try:
            body = await request.json()
        except Exception:
            body = {}
        return body if isinstance(body, dict) else {}

    async def browser_snap(self, request):
        from gateway.serve import browser_window
        body = await self._browser_body(request)
        vals = (body.get("x"), body.get("y"), body.get("w"), body.get("h"),
                body.get("desktop_hwnd"))
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in vals):
            return web.json_response(
                {"ok": False, "reason": "bad body: x,y,w,h,desktop_hwnd ints required"},
                status=400)
        return web.json_response(await self._win(browser_window.snap, *vals))

    async def browser_hide(self, request):
        from gateway.serve import browser_window
        return web.json_response(await self._win(browser_window.hide))

    async def browser_show(self, request):
        from gateway.serve import browser_window
        body = await self._browser_body(request)
        return web.json_response(await self._win(browser_window.show, body.get("desktop_hwnd")))

    async def browser_release(self, request):
        from gateway.serve import browser_window
        return web.json_response(await self._win(browser_window.release))


def add_routes(router, app):
    """The browser business's routes. ``app`` is the ServeApp instance —
    every handler here is a BrowserMixin method on it."""
    router.add_get("/browser/status", app.browser_status)
    router.add_post("/browser/launch", app.browser_launch)
    router.add_post("/browser/snap", app.browser_snap)
    router.add_post("/browser/hide", app.browser_hide)
    router.add_post("/browser/show", app.browser_show)
    router.add_post("/browser/release", app.browser_release)
    router.add_post("/browser/appearance", app.browser_appearance)
    router.add_post("/browser/restart", app.browser_restart)
