"""The knowledge business: the desktop's 记忆 page (long-term memory read/
write) and 知识库 page (read-only KBS browsing) — stateless module functions,
no ServeApp state.

Memory is the same store the agent tools use: same injection scan, same
atomic write, plus a ``memories.json.bak`` before each write and a
corruption guard (a corrupt file plus one ordinary save would silently
wipe the store)."""

import json
import logging

from aiohttp import web

from gateway.serve._common import HttpError, err, run

logger = logging.getLogger(__name__)


async def get_memory(request):
    from tools import memory_tool

    def work():
        data, parse_err = memory_tool.load_memories_checked()
        items = sorted(
            ({"key": k, "value": v if isinstance(v, str) else str(v)}
             for k, v in data.items()),
            key=lambda e: e["key"])
        return {"items": items, "count": len(items),
                "corrupt": bool(parse_err), "error": parse_err}

    return web.json_response(await run(work))


async def put_memory(request):
    """Upsert one entry; ``previous_key`` performs a rename in the same write."""
    from tools import memory_tool

    try:
        body = await request.json()
    except Exception:
        return err(400, "invalid json")
    if not isinstance(body, dict):
        return err(400, "body must be a mapping")
    key, value = body.get("key"), body.get("value")
    previous_key = body.get("previous_key")
    if (not isinstance(key, str) or not key or key != key.strip()
            or len(key) > 200):
        return err(400, "key must be a non-empty trimmed string (<= 200 chars)")
    if not isinstance(value, str):
        return err(400, "value must be a string")
    if len(value) > 100_000:
        return err(400, "value too large (max 100000 chars)")
    if previous_key is not None and not isinstance(previous_key, str):
        return err(400, "previous_key must be a string")

    # keys are injected into the system prompt verbatim next to values —
    # the tool surface never scanned them, this one does
    for label, content in (("key", key), ("value", value)):
        scan_err = memory_tool._scan_memory_content(content)
        if scan_err:
            return err(400, f"{label}: {scan_err}")

    renaming = bool(previous_key) and previous_key != key

    def work():
        data, parse_err = memory_tool.load_memories_checked()
        if parse_err and not body.get("overwrite_corrupt"):
            raise HttpError(
                409, "memories.json 解析失败 — 已保留原文件(备份见 memories.json.bak);"
                     "确认丢弃损坏内容后可强制保存")
        if renaming and previous_key not in data:
            raise HttpError(409, "原键不存在(数据已被修改),请刷新后重试")
        if renaming:
            del data[previous_key]
        data[key] = value
        memory_tool._backup_memories()
        memory_tool._save_memories(data)
        out = {"ok": True, "key": key}
        if renaming:
            out["renamed_from"] = previous_key
        return out

    try:
        return web.json_response(await run(work))
    except HttpError as e:
        return err(e.status, e.msg)
    except Exception as e:
        logger.exception("memory write failed")
        return err(500, f"write failed: {e}")


async def delete_memory(request):
    from tools import memory_tool

    key = request.query.get("key", "")
    if not key:
        return err(400, "key is required")
    force = request.query.get("force") in ("1", "true")

    def work():
        data, parse_err = memory_tool.load_memories_checked()
        if parse_err and not force:
            raise HttpError(
                409, "memories.json 解析失败 — 已保留原文件;确认丢弃后可强制删除(?force=1)")
        if key not in data:
            return {"ok": True, "deleted": False, "key": key}
        del data[key]
        memory_tool._backup_memories()
        memory_tool._save_memories(data)
        return {"ok": True, "deleted": True, "key": key}

    try:
        return web.json_response(await run(work))
    except HttpError as e:
        return err(e.status, e.msg)
    except Exception as e:
        logger.exception("memory delete failed")
        return err(500, f"delete failed: {e}")


async def get_kbs(request):
    from tools import kbs_tool

    def work():
        root = kbs_tool._resolve_root()
        exists = root.exists()
        try:
            digest = json.loads(kbs_tool._kbs_status({}))
        except Exception:
            digest = {}
        return {
            "enabled": kbs_tool._check_kbs(),
            "initialized": exists,
            "root": str(root),
            "status": digest,
            "domains": kbs_tool._list_domains(root) if exists else [],
            "pages": kbs_tool._list_kbs_pages(root) if exists else [],
            "wiki": kbs_tool._list_wiki_files(root) if exists else [],
            "candidates": kbs_tool._list_kbs_candidates(root) if exists else [],
        }

    return web.json_response(await run(work))


async def get_kbs_page(request):
    from tools import kbs_tool

    rel = request.query.get("path", "")

    def work():
        # the path arrives from a client — escapes are rejected, not read
        root = kbs_tool._resolve_root().resolve()
        p = (root / rel).resolve()
        if root not in p.parents or p.suffix.lower() != ".md":
            return {"ok": False, "error": "path-rejected"}
        if not p.is_file():
            return {"ok": False, "error": "not-found"}
        text = p.read_text(encoding="utf-8", errors="replace")
        return {"ok": True, "rel": rel, "title": p.stem,
                "content": text, "bytes": len(text)}

    try:
        return web.json_response(await run(work))
    except OSError as e:
        return web.json_response({"ok": False, "error": f"read failed: {e}"})


def add_routes(router):
    router.add_get("/memory", get_memory)
    router.add_put("/memory", put_memory)
    router.add_delete("/memory", delete_memory)
    router.add_get("/kbs", get_kbs)
    router.add_get("/kbs/page", get_kbs_page)
