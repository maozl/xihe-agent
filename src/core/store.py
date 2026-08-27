"""Capability store: catalog sources, installer, and the install ledger.

The store is a thin catalog+installer layer over existing mount points:
skills land in the shared user skills dir (disk scan picks them up), MCP
declarations live in the ledger and merge into ``mcp_tool._load_mcp_config``
(config.yaml hand-written entries win on name collision). Mounts (which agent
gets which item) are ledger records too — config.yaml and agents/*.yaml are
never programmatically rewritten.
"""

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from core.config import AGENT_HOME

logger = logging.getLogger(__name__)

STORE_DIR = AGENT_HOME / "store"
_LEDGER_PATH = STORE_DIR / "installed.json"
_USER_SKILLS_DIR = AGENT_HOME / "skills"
_BUNDLED_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

_CATALOG_TTL_SECONDS = 300
_DOWNLOAD_TIMEOUT = 20
_MAX_FILE_BYTES = 10 * 1024 * 1024
_MAX_TOTAL_BYTES = 50 * 1024 * 1024
_MAX_FILE_COUNT = 500

_VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_ALLOWED_ENTRY_NAMES = {"SKILL.md", "references", "templates", "scripts", "assets"}

_ledger_lock = threading.Lock()
_catalog_lock = threading.Lock()
_catalog_cache: dict = {"at": 0.0, "data": None}


class StoreError(Exception):
    """Raised for catalog/install failures that map to a user-facing message."""


# --- ledger -----------------------------------------------------------------

def _empty_ledger() -> dict:
    # bucket names mirror the catalog 'type' field ("skill"/"mcp") so led[kind] works
    return {"skill": {}, "mcp": {}, "mounts": {}}


def _load_ledger() -> dict:
    try:
        data = json.loads(_LEDGER_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for key in ("skill", "mcp", "mounts"):
                if not isinstance(data.get(key), dict):
                    data[key] = {}
            return data
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("store ledger unreadable, starting fresh: %s", exc)
    return _empty_ledger()


def _save_ledger(data: dict) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _LEDGER_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _LEDGER_PATH)


def store_installed_mcp() -> dict:
    """Installed MCP declarations (rendered configs) for _load_mcp_config merge."""
    led = _load_ledger()
    return {
        name: rec["config"]
        for name, rec in led["mcp"].items()
        if isinstance(rec.get("config"), dict)
    }


# --- catalog sources ----------------------------------------------------------

def load_sources() -> list:
    from core.config import load_config
    section = load_config().get("store") or {}
    sources = section.get("sources")
    if not isinstance(sources, list):
        return []
    return [s for s in sources
            if isinstance(s, dict) and str(s.get("url") or "").strip()]


def _http_get(url: str, max_bytes: int) -> bytes:
    import httpx
    buf = bytearray()
    with httpx.Client(follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_bytes(64 * 1024):
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise StoreError(f"download exceeds {max_bytes} bytes: {url}")
    return bytes(buf)


def _fetch_index(source: dict) -> dict:
    url = str(source["url"]).strip()
    if url.startswith(("http://", "https://")):
        raw = _http_get(url, _MAX_TOTAL_BYTES)
        data = json.loads(raw.decode("utf-8"))
    else:
        data = json.loads(Path(url).expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise StoreError("index must be a JSON object with an 'items' list")
    return data


def _resolve_ref(index_url: str, ref: str) -> str:
    """Resolve a zip/path reference that may be relative to its index location."""
    if ref.startswith(("http://", "https://")) or Path(ref).is_absolute():
        return ref
    if index_url.startswith(("http://", "https://")):
        from urllib.parse import urljoin
        return urljoin(index_url, ref)
    return str((Path(index_url).expanduser().parent / ref).resolve())


def _normalize_items(index: dict, source: dict) -> list:
    index_url = str(source["url"]).strip()
    source_name = str(source.get("name") or index_url)
    out = []
    for raw in index["items"]:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item_id = str(item.get("id") or "").strip()
        kind = str(item.get("type") or "").strip()
        if not item_id or kind not in ("skill", "mcp"):
            logger.warning("store source '%s': skipping malformed item %r", source_name, raw)
            continue
        item["id"] = item_id
        item["type"] = kind
        item["source_name"] = source_name
        src = item.get("source") or {}
        if isinstance(src, dict):
            for key in ("zip", "path"):
                ref = str(src.get(key) or "").strip()
                if ref:
                    src = {**src, key: _resolve_ref(index_url, ref)}
            item["source"] = src
        if kind == "mcp" and isinstance(item.get("mcp"), dict) and not item["mcp"].get("url"):
            item["unsupported"] = "stdio MCP entries are not supported yet"
        out.append(item)
    return out


def fetch_catalog(force: bool = False) -> dict:
    """Aggregate every configured source's index; one bad source never sinks the rest."""
    with _catalog_lock:
        cached = _catalog_cache["data"]
        if (not force and cached is not None
                and time.time() - _catalog_cache["at"] < _CATALOG_TTL_SECONDS):
            return cached

    items, sources = [], []
    for source in load_sources():
        name = str(source.get("name") or source.get("url"))
        try:
            items.extend(_normalize_items(_fetch_index(source), source))
            sources.append({"name": name, "ok": True, "error": None})
        except Exception as exc:
            logger.warning("store source '%s' failed: %s", name, exc)
            sources.append({"name": name, "ok": False, "error": str(exc)})

    data = {"items": items, "sources": sources}
    with _catalog_lock:
        _catalog_cache["at"] = time.time()
        _catalog_cache["data"] = data
    return data


def find_item(kind: str, item_id: str) -> dict | None:
    for item in fetch_catalog()["items"]:
        if item.get("type") == kind and item.get("id") == item_id:
            return item
    return None


# --- installed view ------------------------------------------------------------

def _mount_token(kind: str, item_id: str, record: dict) -> str:
    # skill tokens use the SKILL.md frontmatter name — that is what skills_allowed filters on
    if kind == "mcp":
        return f"mcp-{item_id}"
    return str(record.get("name") or item_id)


def mount_targets(kind: str, item_id: str) -> list:
    # read paths take no lock: only write paths need read-modify-write atomicity,
    # and catalog_view calls this (a non-reentrant Lock would deadlock)
    led = _load_ledger()
    record = led[kind].get(item_id)
    if not record:
        return []
    token = _mount_token(kind, item_id, record)
    return [agent for agent, tokens in led["mounts"].items() if token in tokens]


def catalog_view(catalog: dict | None = None) -> dict:
    """Catalog items joined with install state; installed-orphan entries and
    manually-configured resources (config.yaml mcp_servers / skills dir entries
    the ledger doesn't know) included as read-only display rows."""
    catalog = catalog or fetch_catalog()
    led = _load_ledger()
    items = []
    seen = set()
    for raw in catalog["items"]:
        entry = dict(raw)
        kind, item_id = entry["type"], entry["id"]
        seen.add((kind, item_id))
        record = led[kind].get(item_id)
        entry["installed"] = record is not None
        entry["installed_version"] = record.get("version") if record else None
        entry["upgradable"] = bool(
            record and entry.get("version") and record.get("version") != entry.get("version"))
        entry["mounted"] = mount_targets(kind, item_id)
        if kind == "mcp" and record:
            # secrets never leave the ledger — only which keys are filled
            entry["secrets_set"] = {k: True for k in (record.get("secrets") or {})}
        items.append(entry)
    for kind in ("skill", "mcp"):
        for item_id, record in led[kind].items():
            if (kind, item_id) in seen:
                continue
            items.append({
                "id": item_id, "type": kind,
                "title": record.get("title") or item_id,
                "description": record.get("description") or "",
                "version": record.get("version"),
                "source_name": record.get("source"),
                "installed": True, "installed_version": record.get("version"),
                "upgradable": False, "orphan": True,
                "config_schema": record.get("config_schema") or [],
                "mounted": mount_targets(kind, item_id),
                **({"secrets_set": {k: True for k in (record.get("secrets") or {})}}
                   if kind == "mcp" else {}),
            })
    items.extend(_manual_entries(seen, items))
    return {"items": items, "sources": catalog["sources"]}


def _url_base(url) -> str:
    """URL without the query string — identifies a service across different
    apikey query params (catalog templates carry {placeholders} there)."""
    u = str(url or "").split("?", 1)[0].rstrip("/")
    return u.lower()


def _manual_entries(seen: set, catalog_items: list) -> list:
    """Live instance resources the store ledger doesn't own: MCP servers hand-
    written in config.yaml's mcp_servers (post-merge — store-installed ones are
    already `seen`) and skills hand-placed under the user skills dir. Read-only
    rows: no install/update/uninstall, no version, but they carry the live MCP
    connection state via the /store consumer joining /mcp.

    A manual MCP server whose URL base matches an uninstalled catalog entry is
    the same service hand-configured — the catalog row is marked installed +
    hand_installed instead of emitting a duplicate manual row."""
    from core.config import load_config
    out = []

    # catalog MCP rows by URL base, for hand-configured dedup
    catalog_mcp_by_url = {
        _url_base(it.get("mcp", {}).get("url")): it
        for it in catalog_items
        if it.get("type") == "mcp" and not it.get("installed")
        and isinstance(it.get("mcp"), dict)
    }

    for name, cfg in (load_config().get("mcp_servers") or {}).items():
        if ("mcp", name) in seen:
            continue
        if isinstance(cfg, dict):
            match = catalog_mcp_by_url.get(_url_base(cfg.get("url")))
            if match is not None:
                match["installed"] = True
                match["hand_installed"] = True
                seen.add(("mcp", match["id"]))
                continue
        out.append({
            "id": name, "type": "mcp", "title": name,
            "description": "config.yaml 手动配置的 MCP 服务器",
            "installed": True, "installed_version": None,
            "upgradable": False, "orphan": False, "manual": True,
            "mounted": [], "config_schema": [],
        })
        seen.add(("mcp", name))

    try:
        from tools.skills_tool import _scan_skills, _USER_SKILLS_DIR
        led = _load_ledger()
        ledger_names = {str(r.get("name") or k) for k, r in led["skill"].items()}
        for s in _scan_skills():
            name = s.get("name") or ""
            if not name or not str(s.get("path", "")).startswith(str(_USER_SKILLS_DIR)):
                continue  # bundled skills ship with the version — not a store row
            if name in ledger_names:
                continue  # store-installed — already covered by ledger state
            if ("skill", name) in seen:
                # Present in the catalog AND on disk (e.g. hand-placed copy of a
                # catalog skill) — mark the catalog row installed rather than
                # emitting a duplicate manual row.
                for it in catalog_items:
                    if it.get("type") == "skill" and it.get("id") == name:
                        it["installed"] = True
                        it["hand_installed"] = True
                        break
                continue
            out.append({
                "id": name, "type": "skill", "title": name,
                "description": s.get("description") or "手动放置的用户 skill",
                "installed": True, "installed_version": None,
                "upgradable": False, "orphan": False, "manual": True,
                "mounted": [],
            })
            seen.add(("skill", name))
    except Exception:
        logger.debug("manual skill scan failed", exc_info=True)

    return out


# --- skill packaging ------------------------------------------------------------

def _extract_zip(zip_path: Path, dest_dir: Path) -> None:
    """Extract with zip-slip / size / count guards — never extractall()."""
    dest_resolved = dest_dir.resolve()
    total = 0
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > _MAX_FILE_COUNT:
            raise StoreError(f"zip has too many files (>{_MAX_FILE_COUNT})")
        for info in infos:
            name = info.filename
            if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
                raise StoreError(f"unsafe zip entry: {name!r}")
            parts = Path(name).parts
            if not parts or ".." in parts:
                raise StoreError(f"unsafe zip entry: {name!r}")
            if info.file_size > _MAX_FILE_BYTES:
                raise StoreError(f"zip entry too large: {name!r}")
            total += info.file_size
            if total > _MAX_TOTAL_BYTES:
                raise StoreError("zip total size exceeds limit")
            target = (dest_dir / Path(*parts)).resolve()
            if dest_resolved not in target.parents:
                raise StoreError(f"zip entry escapes destination: {name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, length=64 * 1024)


def _locate_skill_dir(root: Path) -> Path:
    """SKILL.md at the root, or exactly one top-level dir holding it."""
    if (root / "SKILL.md").is_file():
        return root
    children = [p for p in root.iterdir()]
    if len(children) == 1 and children[0].is_dir() and (children[0] / "SKILL.md").is_file():
        return children[0]
    raise StoreError("package must contain SKILL.md at the root (or one top-level dir)")


def _validate_layout(skill_dir: Path) -> None:
    for path in skill_dir.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(skill_dir)
        if rel.parts and rel.parts[0] not in _ALLOWED_ENTRY_NAMES:
            raise StoreError(f"not allowed in a skill package: {'/'.join(rel.parts)}")


def _parse_frontmatter(content: str) -> tuple[str, str]:
    """(name, description) from SKILL.md frontmatter; mirrors
    skill_manager_tool._validate_frontmatter — core must not import tools
    (module-level tool registration is an import side effect)."""
    if not content.strip() or not content.startswith("---"):
        raise StoreError("SKILL.md must start with YAML frontmatter (---)")
    import yaml
    end = re.search(r"\n---\s*\n", content[3:])
    if not end:
        raise StoreError("SKILL.md frontmatter is not closed")
    try:
        parsed = yaml.safe_load(content[3:end.start() + 3])
    except Exception as exc:
        raise StoreError(f"SKILL.md frontmatter parse error: {exc}")
    if not isinstance(parsed, dict) or "name" not in parsed or "description" not in parsed:
        raise StoreError("SKILL.md frontmatter must include 'name' and 'description'")
    return str(parsed["name"]), str(parsed["description"])


def _materialize(item: dict, tmp_root: Path) -> Path:
    extract_dir = tmp_root / "pkg"
    extract_dir.mkdir(parents=True)
    src = item.get("source") or {}
    if not isinstance(src, dict):
        raise StoreError("item has no usable source")
    local_path = str(src.get("path") or "").strip()
    zip_url = str(src.get("zip") or "").strip()
    if local_path:
        origin = Path(local_path).expanduser()
        if not origin.is_dir():
            raise StoreError(f"source path is not a directory: {local_path}")
        shutil.copytree(origin, extract_dir / "copy")
        return _locate_skill_dir(extract_dir / "copy")
    if zip_url:
        zip_file = tmp_root / "pkg.zip"
        if zip_url.startswith(("http://", "https://")):
            zip_file.write_bytes(_http_get(zip_url, _MAX_TOTAL_BYTES))
        else:
            zip_file.write_bytes(Path(zip_url).expanduser().read_bytes())
        _extract_zip(zip_file, extract_dir)
        return _locate_skill_dir(extract_dir)
    raise StoreError("item source needs 'zip' or 'path'")


def _clear_skills_cache() -> None:
    try:
        from core.prompts import clear_skills_cache
        clear_skills_cache()
    except Exception as exc:
        logger.debug("clear_skills_cache failed: %s", exc)


# --- install / uninstall ---------------------------------------------------------

def _existing_skill_names() -> dict:
    """frontmatter name → first SKILL.md path, across user + bundled dirs.

    Discovery keys skills by frontmatter name (skills_tool._scan_skills), not
    by directory — so collision checks must use the same coordinate system: a
    hand-installed nested layout (skills/<group>/<name>/SKILL.md) dodges a
    one-level directory check and installs a duplicate the scanner then
    de-dupes nondeterministically."""
    out = {}
    for root in (_USER_SKILLS_DIR, _BUNDLED_SKILLS_DIR):
        if not root.is_dir():
            continue
        for doc in root.rglob("SKILL.md"):
            try:
                name, _ = _parse_frontmatter(doc.read_text(encoding="utf-8")[:4000])
            except Exception:
                continue
            if name:
                out.setdefault(name, doc)
    return out


def install_skill(item: dict) -> dict:
    item_id = str(item.get("id") or "").strip()
    if not item_id or len(item_id) > 64 or not _VALID_NAME_RE.match(item_id):
        return {"success": False, "error": f"invalid skill id: {item_id!r}"}

    with _ledger_lock:
        led = _load_ledger()
        own = item_id in led["skill"]
    dest = _USER_SKILLS_DIR / item_id
    if dest.exists() and not own:
        return {"success": False,
                "error": f"skill '{item_id}' already exists and is not store-installed"}
    for name, doc in _existing_skill_names().items():
        if name != item_id:
            continue
        if own and doc.is_relative_to(dest):
            continue
        where = ("a bundled skill" if doc.is_relative_to(_BUNDLED_SKILLS_DIR)
                 else str(doc.parent))
        return {"success": False,
                "error": f"skill name '{item_id}' is already taken by {where}"}

    _USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_root = Path(tempfile.mkdtemp(prefix=".store-tmp-", dir=str(_USER_SKILLS_DIR)))
    try:
        skill_dir = _materialize(item, tmp_root)
        _validate_layout(skill_dir)
        name, _desc = _parse_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
        if not _VALID_NAME_RE.match(name):
            return {"success": False, "error": f"invalid skill name in frontmatter: {name!r}"}

        backup = None
        if dest.exists():
            backup = dest.with_name(dest.name + ".store-old")
            if backup.exists():
                shutil.rmtree(backup)
            os.rename(dest, backup)
        try:
            os.rename(skill_dir, dest)
        except Exception:
            if backup is not None:
                os.rename(backup, dest)
            raise
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
    except StoreError as exc:
        return {"success": False, "error": str(exc)}
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    with _ledger_lock:
        led = _load_ledger()
        led["skill"][item_id] = {
            "name": name, "title": item.get("title") or name,
            "description": item.get("description") or "",
            "version": item.get("version"), "source": item.get("source_name"),
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        _save_ledger(led)
    _clear_skills_cache()
    logger.info("store: installed skill '%s' (frontmatter name '%s')", item_id, name)
    return {"success": True, "id": item_id, "name": name,
            "mounted_hint": "skills with a fixed whitelist need a mount or config edit"}


def _render_mcp_config(mcp: dict, secrets: dict) -> dict:
    def sub(value):
        if isinstance(value, str):
            for key, secret in secrets.items():
                if secret is not None:
                    value = value.replace("{" + key + "}", str(secret))
            return value
        if isinstance(value, dict):
            return {k: sub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [sub(v) for v in value]
        return value
    return {k: sub(v) for k, v in mcp.items()}


def install_mcp(item: dict, config_values: dict | None = None) -> dict:
    item_id = str(item.get("id") or "").strip()
    mcp = item.get("mcp")
    if not isinstance(mcp, dict) or not str(mcp.get("url") or "").strip():
        return {"success": False,
                "error": "only remote (streamable-http) MCP entries are supported"}

    from core.config import load_config
    manual = load_config().get("mcp_servers") or {}
    if item_id in manual:
        return {"success": False,
                "error": f"config.yaml already declares mcp_servers.{item_id}"}

    fields = [f for f in (item.get("config_schema") or [])
              if isinstance(f, dict) and str(f.get("key") or "").strip()]
    with _ledger_lock:
        led = _load_ledger()
        old = led["mcp"].get(item_id) or {}
        secrets = dict(old.get("secrets") or {})
    for field in fields:
        value = (config_values or {}).get(field["key"])
        if value:
            secrets[field["key"]] = str(value)
    for field in fields:
        if field.get("required") and not secrets.get(field["key"]):
            return {"success": False,
                    "error": f"missing required config value: {field['key']}"}

    # tools.mcp_tool imports core lazily inside functions, so this is cycle-safe
    from tools.mcp_tool import discover_mcp_tools, get_mcp_status, remove_mcp_server
    try:
        remove_mcp_server(item_id)
    except Exception as exc:
        logger.debug("store: pre-install remove of '%s' failed: %s", item_id, exc)

    config = _render_mcp_config(mcp, secrets)
    with _ledger_lock:
        led = _load_ledger()
        led["mcp"][item_id] = {
            "title": item.get("title") or item_id,
            "description": item.get("description") or "",
            "version": item.get("version"), "source": item.get("source_name"),
            "config_schema": fields, "secrets": secrets, "config": config,
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        _save_ledger(led)
    try:
        discover_mcp_tools()
    except Exception as exc:
        logger.warning("store: MCP discovery after installing '%s' failed: %s", item_id, exc)
    status = next((s for s in get_mcp_status() if s.get("name") == item_id), None)
    logger.info("store: installed MCP '%s' (connected=%s)", item_id,
                bool(status and status.get("connected")))
    return {"success": True, "id": item_id,
            "connected": bool(status and status.get("connected")),
            "tools": int(status.get("tools") or 0) if status else 0}


def uninstall(kind: str, item_id: str) -> dict:
    if kind not in ("skill", "mcp"):
        return {"success": False, "error": f"unknown kind: {kind}"}
    with _ledger_lock:
        led = _load_ledger()
        record = led[kind].get(item_id)
        if record is None:
            return {"success": False, "error": f"{kind} '{item_id}' is not store-installed"}
        token = _mount_token(kind, item_id, record)

    if kind == "skill":
        dest = _USER_SKILLS_DIR / item_id
        if dest.exists():
            if dest.parent.resolve() != _USER_SKILLS_DIR.resolve():
                return {"success": False, "error": "refusing to delete outside the user skills dir"}
            shutil.rmtree(dest)
        _clear_skills_cache()
    else:
        from tools.mcp_tool import remove_mcp_server
        try:
            remove_mcp_server(item_id)
        except Exception as exc:
            logger.debug("store: remove_mcp_server('%s') failed: %s", item_id, exc)

    with _ledger_lock:
        led = _load_ledger()
        led[kind].pop(item_id, None)
        for tokens in led["mounts"].values():
            if token in tokens:
                tokens.remove(token)
        led["mounts"] = {k: v for k, v in led["mounts"].items() if v}
        _save_ledger(led)
    logger.info("store: uninstalled %s '%s'", kind, item_id)
    return {"success": True, "id": item_id}


# --- mounts ------------------------------------------------------------------

def set_mount(kind: str, item_id: str, targets: list) -> dict:
    if kind not in ("skill", "mcp"):
        return {"success": False, "error": f"unknown kind: {kind}"}
    with _ledger_lock:
        led = _load_ledger()
        record = led[kind].get(item_id)
        if record is None:
            return {"success": False, "error": f"{kind} '{item_id}' is not store-installed"}
        token = _mount_token(kind, item_id, record)
        mounts = led.setdefault("mounts", {})
        for tokens in mounts.values():
            if token in tokens:
                tokens.remove(token)
        for target in targets or []:
            target = str(target).strip()
            if not target:
                continue
            mounts.setdefault(target, [])
            if token not in mounts[target]:
                mounts[target].append(token)
        _save_ledger(led)
    return {"success": True, "id": item_id, "mounted": mount_targets(kind, item_id)}


def mounted_extra(agent_key: str) -> tuple[set, set]:
    """(toolset names, skill names) mounted to one agent — ('mcp-*' vs bare names)."""
    with _ledger_lock:
        led = _load_ledger()
    tokens = led["mounts"].get(agent_key) or []
    toolsets = {t for t in tokens if str(t).startswith("mcp-")}
    skills = {t for t in tokens if str(t) not in toolsets}
    return toolsets, skills


def merge_mounts(agent_key: str, toolsets, skills):
    """Union ledger mounts into a resolved roster. None means unrestricted —
    left untouched so the []-vs-None invariant (agent.py relies on it) holds."""
    extra_ts, extra_sk = mounted_extra(agent_key)
    if toolsets is not None and extra_ts:
        toolsets = set(toolsets) | extra_ts
    if skills is not None and extra_sk:
        skills = set(skills) | extra_sk
    return toolsets, skills
