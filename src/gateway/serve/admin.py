"""The management business (ServeApp's AdminMixin): management-panel reads
(MCP / skills / cron), the specialist-agent editor's CRUD over agents/*.yaml,
and the capability store (catalog view + install/uninstall/mount/refresh over
core.services.store's ledger). Secrets never cross the wire — api_key only as
api_key_set, store config keys only as secrets_set."""

import asyncio
import logging

from aiohttp import web

logger = logging.getLogger(__name__)


class AdminMixin:
    """Mixin into ServeApp — state lives in ServeApp.__init__."""

    # Each of the panel reads is a thin wrapper over an already-running
    # subsystem's in-process enumerator (MCP connections, skill scan, cron
    # scheduler). No XiheAgent is created — these are pure reads. `api_key` is
    # never touched. Enumeration hits disk (yaml/rglob/job files), so it runs
    # off-loop like the DB reads.
    async def mcp(self, request):
        from tools.mcp_tool import get_mcp_status
        loop = asyncio.get_running_loop()
        return web.json_response(
            {"servers": await loop.run_in_executor(None, get_mcp_status)})

    async def skills(self, request):
        from tools.skills_tool import _scan_skills, _USER_SKILLS_DIR

        def work():
            user_prefix = str(_USER_SKILLS_DIR)
            return [
                {
                    "name": s["name"],
                    "description": s["description"],
                    "category": s["category"],
                    # Derive bundled-vs-user from the on-disk path (user dir shadows
                    # bundled by name in _scan_skills) so the panel can mark which
                    # are editable. The raw path itself is internal — not returned.
                    "source": "user" if s["path"].startswith(user_prefix) else "bundled",
                }
                for s in _scan_skills()
            ]

        loop = asyncio.get_running_loop()
        return web.json_response({"skills": await loop.run_in_executor(None, work)})

    async def cron(self, request):
        import json as _json
        from tools.cronjob_tools import _list_jobs
        from core.services.scheduler import scheduler_health
        # _list_jobs returns a tool_result(...) JSON *string*; parse out the
        # curated job summary. include_disabled so the panel shows paused jobs
        # too (greyed via the `enabled` flag) rather than hiding them.

        def work():
            try:
                jobs = _json.loads(_list_jobs({"include_disabled": True})).get("jobs", [])
            except Exception:
                jobs = []
            return {"jobs": jobs, "scheduler": scheduler_health()}

        loop = asyncio.get_running_loop()
        return web.json_response(await loop.run_in_executor(None, work))

    # ---- cron mutations: thin wrappers over the cronjob tool handlers so job
    # semantics (pause/resume state machine, run-now scheduling, delete +
    # running-interrupt) stay single-sourced. Creation stays in-chat only.

    def _run_cron_handler(self, handler, args: dict) -> dict:
        import json as _json
        return _json.loads(handler(args))

    async def delete_cron(self, request):
        from tools.cronjob_tools import _delete_job
        args = {"job_id": request.match_info["job_id"]}
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, self._run_cron_handler, _delete_job, args)
        if "error" in result:
            return web.json_response({"ok": False, "error": result["error"]}, status=404)
        return web.json_response({"ok": True, **result})

    async def post_cron_action(self, request):
        """POST /cron/{job_id}/{action} with action = run | pause | resume."""
        from tools import cronjob_tools as ct
        action = request.match_info["action"]
        handler = {"run": ct._run_job_manual, "pause": ct._pause_job,
                   "resume": ct._resume_job}.get(action)
        if handler is None:
            return web.json_response({"ok": False, "error": "unknown action"}, status=400)
        args = {"job_id": request.match_info["job_id"]}
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, self._run_cron_handler, handler, args)
        if "error" in result:
            return web.json_response({"ok": False, "error": result["error"]}, status=404)
        return web.json_response({"ok": True, **result})

    async def get_cron_runs(self, request):
        """GET /cron/{job_id}/runs — recent run outputs for the history panel.
        Unknown job / never ran both yield empty runs (not 404) so the panel
        can render a uniform "暂无运行记录"."""
        from core.services.scheduler import list_job_runs
        job_id = request.match_info["job_id"]
        try:
            limit = max(1, min(50, int(request.query.get("limit", 10))))
        except ValueError:
            limit = 10
        loop = asyncio.get_running_loop()
        runs = await loop.run_in_executor(
            None, lambda: list_job_runs(job_id, limit))
        return web.json_response({"ok": True, "runs": runs})

    async def get_specialists(self, request):
        """Raw agents/*.yaml specs + toolset catalog for the desktop editor.

        Specs are returned verbatim (not validated AgentDefs) minus api_key —
        only the api_key_set flag escapes the server — so the editor can show
        and fix entries validation would drop. ``registered`` reads the live
        registry; writes sync it immediately, so a not-registered-but-enabled
        entry means the gate is off (user agents) or sync failed.
        """
        from core.services.agent_defs import list_raw_specs
        from core.toolsets import TOOLSETS, resolve_toolset
        from tools import registry
        from tools.mcp_tool import get_mcp_status
        from tools.specialist_agent_tool import specialists_enabled

        def work():
            # Lazy sync — hand-edited yaml (bypassing PUT/DELETE) converges
            # on the next list read, so `registered` never lies about the
            # file state.
            from tools.specialist_agent_tool import sync_specialist_agent_tools
            sync_specialist_agent_tools(self.config)
            specialists = []
            for slug, spec, bundled in list_raw_specs():
                safe = {k: v for k, v in spec.items() if k != "api_key"}
                specialists.append({
                    "slug": slug,
                    "spec": safe,
                    "api_key_set": bool(spec.get("api_key")),
                    # bundled = the winning spec comes from src/agents —
                    # editing creates a user override copy; deleting the
                    # override restores the bundled one.
                    "bundled": bundled,
                })
            registered = sorted(
                n for n in registry.snapshot_names()
                if n.startswith("run_") and n.endswith("_agent"))
            return {
                "specialists": specialists,
                "specialists_enabled": specialists_enabled(self.config),
                "toolsets": [
                    {
                        "name": name,
                        "label": ts.get("label") or name,
                        "description": ts.get("description", ""),
                        "tools": len(resolve_toolset(name)),
                    }
                    for name, ts in sorted(TOOLSETS.items())
                ],
                "mcp_servers": [
                    {"name": s.get("name"), "tools": s.get("tools", 0),
                     "connected": bool(s.get("connected"))}
                    for s in get_mcp_status()
                ],
                "registered": registered,
            }

        loop = asyncio.get_running_loop()
        return web.json_response(await loop.run_in_executor(None, work))

    async def put_specialist(self, request):
        """Write one specialist file (create or replace) agents/<slug>.yaml.

        api_key: absent from the body = keep the file's existing key; empty
        string = clear it. Validation warnings are returned but the file
        still saves (invalid entries are skipped by the sync) so drafts
        aren't lost. Registration is reconciled immediately — the next turn
        sees the new tool/roster, no restart.
        """
        from core.services.agent_defs import (
            _SLUG_RE, _load_raw, load_agent_defs, save_raw,
        )
        slug = request.match_info["slug"]
        # The slug becomes a file name — the regex doubles as the
        # path-traversal guard (no dots, slashes, or case tricks).
        if not _SLUG_RE.match(slug):
            return web.json_response({"ok": False, "error": "bad slug"}, status=400)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        spec = body.get("spec") if isinstance(body, dict) else None
        if not isinstance(spec, dict):
            return web.json_response(
                {"ok": False, "error": "'spec' must be a mapping"}, status=400)
        from core.services.agent_defs import bundled_write_violation
        violation = bundled_write_violation(slug, spec)
        if violation:
            return web.json_response({"ok": False, "error": violation}, status=400)

        if "api_key" in spec and not spec["api_key"]:
            spec.pop("api_key")  # explicit empty = clear
        elif "api_key" not in spec:
            try:
                existing = _load_raw(slug)
            except Exception:
                existing = {}
            if isinstance(existing, dict) and existing.get("api_key"):
                spec["api_key"] = existing["api_key"]

        try:
            save_raw(slug, spec)
        except Exception as e:
            logger.exception("specialist write failed")
            return web.json_response(
                {"ok": False, "error": f"write failed: {e}"}, status=500)
        warnings: list = []
        load_agent_defs(warnings)
        from tools.specialist_agent_tool import sync_specialist_agent_tools
        sync_specialist_agent_tools(self.config)
        return web.json_response({"ok": True, "slug": slug, "warnings": warnings})

    async def delete_specialist(self, request):
        """Delete one specialist file — registration is reconciled
        immediately (deregistering the tool), no restart."""
        from core.services.agent_defs import _SLUG_RE, delete_raw
        slug = request.match_info["slug"]
        if not _SLUG_RE.match(slug):
            return web.json_response({"ok": False, "error": "bad slug"}, status=400)
        deleted = delete_raw(slug)
        from tools.specialist_agent_tool import sync_specialist_agent_tools
        sync_specialist_agent_tools(self.config)
        return web.json_response({
            "ok": True, "slug": slug, "deleted": deleted,
        })

    async def store(self, request):
        """Catalog + install state for the desktop store page. Secrets never
        leave the ledger — responses carry only which config keys are filled."""
        from core.services import store as store_mod
        loop = asyncio.get_running_loop()
        catalog = await loop.run_in_executor(None, store_mod.fetch_catalog)
        return web.json_response(store_mod.catalog_view(catalog))

    async def _store_body(self, request) -> tuple | None:
        try:
            body = await request.json()
        except Exception:
            return None
        if not isinstance(body, dict):
            return None
        return body

    async def store_install(self, request):
        from core.services import store as store_mod
        body = await self._store_body(request)
        kind = str((body or {}).get("type") or "").strip()
        item_id = str((body or {}).get("id") or "").strip()
        if kind not in ("skill", "mcp") or not item_id:
            return web.json_response(
                {"ok": False, "error": "body needs 'type' (skill|mcp) and 'id'"},
                status=400)
        loop = asyncio.get_running_loop()
        item = await loop.run_in_executor(None, store_mod.find_item, kind, item_id)
        if item is None:
            return web.json_response(
                {"ok": False, "error": f"{kind} '{item_id}' not found in any store source"},
                status=404)
        try:
            if kind == "skill":
                result = await loop.run_in_executor(None, store_mod.install_skill, item)
            else:
                result = await loop.run_in_executor(
                    None, store_mod.install_mcp, item, body.get("config") or {})
        except Exception as e:
            logger.exception("store install failed")
            return web.json_response(
                {"ok": False, "error": f"install failed: {e}"}, status=500)
        if not result.get("success"):
            return web.json_response(
                {"ok": False, "error": result.get("error", "install failed")}, status=400)
        return web.json_response({"ok": True, **result})

    async def store_uninstall(self, request):
        from core.services import store as store_mod
        body = await self._store_body(request)
        kind = str((body or {}).get("type") or "").strip()
        item_id = str((body or {}).get("id") or "").strip()
        if kind not in ("skill", "mcp") or not item_id:
            return web.json_response(
                {"ok": False, "error": "body needs 'type' (skill|mcp) and 'id'"},
                status=400)
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, store_mod.uninstall, kind, item_id)
        except Exception as e:
            logger.exception("store uninstall failed")
            return web.json_response(
                {"ok": False, "error": f"uninstall failed: {e}"}, status=500)
        if not result.get("success"):
            return web.json_response(
                {"ok": False, "error": result.get("error", "uninstall failed")}, status=400)
        return web.json_response({"ok": True, **result})

    async def store_mount(self, request):
        from core.services import store as store_mod
        body = await self._store_body(request)
        kind = str((body or {}).get("type") or "").strip()
        item_id = str((body or {}).get("id") or "").strip()
        targets = (body or {}).get("targets")
        if kind not in ("skill", "mcp") or not item_id or not isinstance(targets, list):
            return web.json_response(
                {"ok": False, "error": "body needs 'type', 'id' and 'targets' (list)"},
                status=400)
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None, store_mod.set_mount, kind, item_id, targets)
        except Exception as e:
            logger.exception("store mount failed")
            return web.json_response(
                {"ok": False, "error": f"mount failed: {e}"}, status=500)
        if not result.get("success"):
            return web.json_response(
                {"ok": False, "error": result.get("error", "mount failed")}, status=400)
        # 'main' resolves its roster at process start; specialists re-read the
        # ledger on every dispatch — surface the difference to the UI.
        effect = {t: ("restart" if t == "main" else "hot")
                  for t in (result.get("mounted") or [])}
        return web.json_response({"ok": True, **result, "effect": effect})

    async def store_refresh(self, request):
        from core.services import store as store_mod
        loop = asyncio.get_running_loop()
        catalog = await loop.run_in_executor(
            None, lambda: store_mod.fetch_catalog(force=True))
        return web.json_response(store_mod.catalog_view(catalog))


def add_routes(router, app):
    """The management business's routes. ``app`` is the ServeApp instance —
    every handler here is an AdminMixin method on it."""
    router.add_get("/mcp", app.mcp)
    router.add_get("/skills", app.skills)
    router.add_get("/cron", app.cron)
    router.add_get("/cron/{job_id}/runs", app.get_cron_runs)
    router.add_delete("/cron/{job_id}", app.delete_cron)
    router.add_post("/cron/{job_id}/{action}", app.post_cron_action)
    router.add_get("/specialists", app.get_specialists)
    router.add_put("/specialists/{slug}", app.put_specialist)
    router.add_delete("/specialists/{slug}", app.delete_specialist)
    router.add_get("/store", app.store)
    router.add_post("/store/refresh", app.store_refresh)
    router.add_post("/store/install", app.store_install)
    router.add_post("/store/uninstall", app.store_uninstall)
    router.add_post("/store/mount", app.store_mount)
