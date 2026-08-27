"""KBS tools — business knowledge-base maintenance.

Gated by config ``kbs.enabled`` (see ``_check_kbs``). When enabled, exposes:
  - ``kbs_init``:   stamp a blank scaffold (from ``core/kbs_templates/``) at ``<agent_home>/.biz_kbs``
  - ``kbs_status``: freshness/health digest (lint-status + active + recent)

Raw file read/write on the KBS reuses the always-on core file tools
(``read_file`` / ``write_file`` / ``search_files`` / ``patch``). These two tools
add only what raw I/O cannot do well: deterministic scaffold stamping, and a
derived health summary.
"""

import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# Bundled blank scaffold, shipped with the package next to core/.
_TEMPLATES_DIR = Path(__file__).parent.parent / "core" / "kbs_templates"


def _check_kbs() -> bool:
    """Availability gate: KBS tools exist iff config ``kbs.enabled`` is true."""
    try:
        from core.config import load_config
        return bool(load_config().get("kbs", {}).get("enabled", False))
    except Exception:
        return False


def _resolve_root() -> Path:
    """Resolve the KBS root: ``<AGENT_HOME>/.biz_kbs`` (the data root's kbs dir).

    No separate path config — the KBS lives directly under agent_home, so it
    follows ``--config`` / ``AGENT_HOME`` like sessions.db, skills/, browser/, …
    """
    from core.config import AGENT_HOME
    return AGENT_HOME / ".biz_kbs"


def _is_populated(root: Path) -> bool:
    """True if root exists and holds any real content.

    ``.gitkeep`` markers and empty files are ignored, so a freshly scaffolded
    (but content-less) layout is NOT considered populated until real pages exist.
    """
    if not root.exists():
        return False
    for entry in root.rglob("*"):
        if not entry.is_file():
            continue
        if entry.name == ".gitkeep" or entry.stat().st_size == 0:
            continue
        return True
    return False


def init_kbs(root: Path, force: bool = False) -> dict:
    """Stamp the blank scaffold from ``kbs_templates/`` into *root*.

    Refuses to overwrite a populated root unless ``force=True``. Existing files
    are skipped by default (``force`` overwrites them). Returns a summary dict.
    """
    if not _TEMPLATES_DIR.exists():
        raise RuntimeError(f"KBS templates not found: {_TEMPLATES_DIR}")

    root = Path(root)
    if _is_populated(root) and not force:
        raise RuntimeError(
            f"KBS root already populated: {root}. Pass force=true to overwrite "
            "(destructive — confirm with the user first)."
        )

    root.mkdir(parents=True, exist_ok=True)
    created, skipped, overwritten = [], [], []
    for src in _TEMPLATES_DIR.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(_TEMPLATES_DIR)
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            if force:
                shutil.copy2(src, dst)
                overwritten.append(str(rel))
            else:
                skipped.append(str(rel))
        else:
            shutil.copy2(src, dst)
            created.append(str(rel))

    return {
        "root": str(root),
        "created": len(created),
        "skipped_existing": len(skipped),
        "overwritten": len(overwritten),
        "created_files": created,
    }


def _kbs_init(args: dict, **kw) -> str:
    """Initialize (or refresh) the KBS scaffold at the configured root."""
    force = bool(args.get("force", False))
    try:
        root = _resolve_root()
        result = init_kbs(root, force=force)
    except Exception as e:
        return tool_error(str(e))
    msg = (
        f"KBS scaffold ready at {result['root']}: "
        f"{result['created']} created, {result['skipped_existing']} skipped."
    )
    if result["overwritten"]:
        msg += f" {result['overwritten']} overwritten (force)."
    msg += " Maintain content per the protocol in <root>/AGENT.md."
    result["message"] = msg
    return tool_result(result)


def _parse_dt(value):
    """Parse an ISO-8601 / YYYY-MM-DD string into an aware UTC datetime, or None."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    try:
        if len(s) == 10:  # date-only
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _kbs_status(args: dict, **kw) -> str:
    """Freshness/health digest: lint-status + active workstreams + staleness hints."""
    try:
        root = _resolve_root()
    except Exception as e:
        return tool_error(f"Failed to resolve KBS root: {e}")

    if not root.exists():
        digest = {"root": str(root), "initialized": False}
        digest["message"] = (
            f"KBS root {root} does not exist. Offer to initialize it via kbs_init "
            "(confirm with the user first)."
        )
        return tool_result(digest)

    status_path = root / "meta" / "lint-status.json"
    status = {}
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception as e:
            status = {"_parse_error": str(e)}

    now = datetime.now(timezone.utc)
    last_lint = _parse_dt(status.get("last_lint"))
    if last_lint is None:
        lint_age_hours, lint_stale = None, True
    else:
        lint_age_hours = (now - last_lint).total_seconds() / 3600.0
        lint_stale = lint_age_hours > 24

    # Active workstreams: non-placeholder bullet lines from active.md
    active_summary = None
    active_path = root / "wiki" / "active.md"
    if active_path.exists():
        try:
            lines = [
                ln.strip().lstrip("- ").strip()
                for ln in active_path.read_text(encoding="utf-8").splitlines()
                if ln.strip().startswith("- ") and "none" not in ln.lower()
            ]
            active_summary = lines[:8]
        except Exception:
            logger.debug("kbs digest: active.md unreadable", exc_info=True)

    # Recent-update row count (excludes header/separator/placeholder rows)
    recent_count = None
    recent_path = root / "wiki" / "recent.md"
    if recent_path.exists():
        try:
            recent_count = sum(
                1 for ln in recent_path.read_text(encoding="utf-8").splitlines()
                if ln.strip().startswith("| ")
                and "none" not in ln.lower()
                and "---" not in ln
                and "Date" not in ln
            )
        except Exception:
            logger.debug("kbs digest: recent.md unreadable", exc_info=True)

    hints = []
    if lint_stale:
        hints.append("last_lint missing or older than 24h — suggest a maintenance/lint pass.")
    if status.get("total_pages", 0) == 0:
        hints.append("No formal pages yet — the KB is empty; ingest per AGENT.md when the user asks.")

    digest = {
        "root": str(root),
        "initialized": True,
        "lint_status": status,
        "last_lint_age_hours": round(lint_age_hours, 1) if lint_age_hours is not None else None,
        "lint_stale": lint_stale,
        "active_workstreams": active_summary,
        "recent_update_rows": recent_count,
        "hints": hints,
    }
    digest["message"] = "; ".join(hints) if hints else "KBS is healthy."
    return tool_result(digest)


def _parse_kbs_index(root: Path):
    """Parse the curated index into (entries, domain_aliases).

    entries: [{title, type, domain, summary}] from wiki/index.md.
    domain_aliases: {lowercased_alias: slug} from wiki/domains/index.md.
    Best-effort; returns ([], {}) on failure so the caller falls back to grep.
    """
    entries, domain_aliases = [], {}

    reg_path = root / "wiki" / "domains" / "index.md"
    if reg_path.exists():
        try:
            text = reg_path.read_text(encoding="utf-8")
            for m in re.finditer(r"^\|\s*`([a-z0-9-]+)`\s*\|([^|]*)\|([^|]*)\|", text, re.M):
                slug = m.group(1)
                for col in (m.group(2), m.group(3)):
                    for a in re.split(r"[/,、]", col):
                        a = a.strip()
                        if a:
                            domain_aliases[a.lower()] = slug
                domain_aliases[slug] = slug
        except Exception:
            logger.debug("kbs domain alias parse failed", exc_info=True)

    idx_path = root / "wiki" / "index.md"
    if not idx_path.exists():
        return entries, domain_aliases
    try:
        cur_type, cur_domain = None, None
        for line in idx_path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("### "):
                m = re.match(r"([a-z0-9-]+)", s[4:])
                if m:
                    cur_type, cur_domain = "entity", m.group(1)
                continue
            if s.startswith("## "):
                name = s[3:].strip().lower()
                cur_type = {"entities": "entity", "insights": "insight",
                            "concepts": "concept"}.get(name)
                cur_domain = None
                continue
            m = re.match(r"-\s*\[\[([^\]]+)\]\]\s*[—\-]?\s*(.*)", s)
            if m and cur_type:
                title = m.group(1).strip()
                summary = m.group(2).strip()
                dm = re.search(r"domain[:：]\s*([^\)\]]+)", summary)
                entries.append({"title": title, "type": cur_type,
                                "domain": (dm.group(1).strip() if dm else cur_domain),
                                "summary": summary})
    except Exception:
        # design: fall back to grep search — but a silently empty index makes
        # kbs_search quietly worse, so leave a trace
        logger.debug("kbs index parse failed (falling back to grep)", exc_info=True)
    return entries, domain_aliases


def _tokenize(query: str) -> list:
    return [t for t in re.split(r"[\s|,，、;；]+", (query or "").strip()) if t]


def _kbs_search(args: dict, **kw) -> str:
    """Locate pages via the curated index first; fall back to content scan.

    Reads wiki/index.md + the domain registry, matches the query against page
    titles/summaries/domain aliases, and returns curated hits with domain +
    summary + path. Only if the index has no hit does it scan file contents —
    so using this tool enforces index-first retrieval instead of blind grep.
    """
    query = (args.get("query") or "").strip()
    if not query:
        return tool_error("query is required")
    try:
        root = _resolve_root()
    except Exception as e:
        return tool_error(f"Failed to resolve KBS root: {e}")
    if not root.exists():
        digest = {"root": str(root), "initialized": False}
        digest["message"] = f"KBS root {root} missing — offer kbs_init first."
        return tool_result(digest)

    want_domain = (args.get("domain") or "").strip().lower() or None
    read_content = bool(args.get("read_content", False))
    tokens = _tokenize(query)
    entries, aliases = _parse_kbs_index(root)

    if not want_domain:
        for tok in tokens:
            if tok.lower() in aliases:
                want_domain = aliases[tok.lower()]
                break

    def _score(e):
        hay = (e["title"] + " " + e["summary"]).lower()
        sc = sum(1 for t in tokens if t.lower() in hay)
        if want_domain and e.get("domain") and want_domain in str(e["domain"]).lower():
            sc += 2
        return sc

    matched = sorted((e for e in entries if _score(e) > 0), key=_score, reverse=True)[:10]

    # Resolve title -> path via a stem map over the page directories
    stem_map = {}
    for sub in ("entities", "insights", "concepts"):
        d = root / "wiki" / sub
        if d.exists():
            for p in d.rglob("*.md"):
                stem_map[p.stem.lower()] = p

    if matched:
        out, top_path = [], None
        for e in matched:
            path = stem_map.get(e["title"].lower())
            if top_path is None and path:
                top_path = path
            out.append({"title": e["title"], "type": e["type"], "domain": e.get("domain"),
                        "path": str(path) if path else None, "summary": e["summary"]})
        result = {"via": "index", "count": len(out), "matches": out}
        if read_content and top_path and top_path.exists():
            try:
                result["top_content"] = top_path.read_text(encoding="utf-8")[:4000]
            except Exception:
                pass
        result["message"] = f"{len(out)} match(es) via curated index. Read a path with read_file."
        return tool_result(result)

    # Fallback: content scan (index had no hit)
    rx = re.compile("|".join(re.escape(t) for t in tokens) if tokens else re.escape(query), re.I)
    hits = []
    for p in root.rglob("*.md"):
        try:
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                if rx.search(line):
                    hits.append({"path": str(p), "rel": p.relative_to(root).as_posix(),
                                 "line": line.strip()[:160]})
                    break
        except Exception:
            continue
        if len(hits) >= 15:
            break
    result = {"via": "fallback-grep", "count": len(hits), "matches": hits,
              "note": "No curated-index match; fell back to content scan. Consider adding the topic to wiki/index.md."}
    result["message"] = f"{len(hits)} hit(s) via fallback grep (index had no match)."
    return tool_result(result)


registry.register(
    name="kbs_init",
    toolset="kbs",
    schema={
        "type": "function",
        "function": {
            "name": "kbs_init",
            "description": (
                "Initialize the business knowledge base (KBS) at the configured root "
                "(kbs.root) by stamping a blank scaffold: directory tree, page schemas, "
                "an empty business-domain registry, blank entry pages, and zeroed "
                "lint-status. Idempotent — existing files are skipped unless force=true. "
                "Refuses to overwrite an already-populated KBS unless force=true. Only "
                "call when the KBS root is missing/empty (or the user explicitly asks to "
                "reinitialize); confirm with the user first for any non-empty root."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": "Overwrite existing files / a populated KBS. Destructive — confirm first.",
                    },
                },
                "required": [],
            },
        },
    },
    handler=lambda args, **kw: _kbs_init(args, **kw),
    check_fn=_check_kbs,
    read_only=False,
    subagent_blocked=True,
)

registry.register(
    name="kbs_status",
    toolset="base",
    schema={
        "type": "function",
        "function": {
            "name": "kbs_status",
            "description": (
                "Report the business knowledge base's freshness and health: lint-status "
                "metadata, whether the last lint pass is stale (>24h or missing), current "
                "active workstreams, recent-update count, and suggested next actions. "
                "Call this at the start of a KBS-related conversation to decide whether "
                "maintenance or ingestion is needed. Read-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    handler=lambda args, **kw: _kbs_status(args, **kw),
    check_fn=_check_kbs,
    read_only=True,
)

registry.register(
    name="kbs_search",
    toolset="base",
    schema={
        "type": "function",
        "function": {
            "name": "kbs_search",
            "description": (
                "Locate pages in the business knowledge base via the CURATED INDEX first: "
                "reads wiki/index.md and the domain registry, matches the query against page "
                "titles/summaries/domain aliases, and returns hits with domain + one-line "
                "summary + path. Falls back to a content scan only if the index has no match. "
                "Use this — not search_files — to query the KBS, so retrieval goes through the "
                "structured index (domain context + cross-links) instead of blind grep. Read-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to look for: a system/topic name or tokens, e.g. 'WTSS' or '数据地图'.",
                    },
                    "domain": {
                        "type": "string",
                        "description": "Optional: scope to a domain slug (data-governance) or alias (数据地图).",
                    },
                    "read_content": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, also return the top match's content (truncated to 4000 chars).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    handler=lambda args, **kw: _kbs_search(args, **kw),
    check_fn=_check_kbs,
    read_only=True,
)
