"""File tools — read_file, write_file, search_files, patch, directory_tree.

Enhanced with (adapted from Hermes):
- Sensitive write path protection (.ssh, .aws, /etc, etc.)
- Device path blocklist (/dev/zero, etc.)
- Read dedup (returns lightweight stub for unchanged re-reads)
- Similar file suggestions on not-found
- search_files output_mode (files_only, count) and pagination (offset)
- Read-loop detection (prevents repeated reads)
- File staleness warnings (detects external edits between read and write)
- Binary file guard
- Fuzzy matching for patch
- Cross-tool reset for loop detection
"""

import logging
import os
import re
import difflib
import threading
from pathlib import Path
from tools import registry, tool_error, tool_result
from tools.interrupt import interruptible_iter, is_interrupted

logger = logging.getLogger(__name__)

# Appended to search results when the user interrupted the scan mid-way.
_SEARCH_INTERRUPTED_NOTE = "搜索被用户中断，以上为部分结果，可能不完整。"

# Binary file extensions that should not be read as text
_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".tif",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv", ".flv", ".wmv",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat",
    ".pyc", ".pyo", ".o", ".obj", ".class", ".jar", ".war",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".sqlite", ".db", ".idx",
})


def _has_binary_extension(path: str) -> bool:
    return Path(path).suffix.lower() in _BINARY_EXTENSIONS


def _check_file() -> bool:
    return True


# Device path blocklist (adapted from Hermes)
_BLOCKED_DEVICE_PATHS = frozenset({
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
    "/dev/stdin", "/dev/tty", "/dev/console",
    "/dev/stdout", "/dev/stderr",
    "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
})


def _is_blocked_device(filepath: str) -> bool:
    normalized = os.path.expanduser(filepath)
    if normalized in _BLOCKED_DEVICE_PATHS:
        return True
    if normalized.startswith("/proc/") and normalized.endswith(("/fd/0", "/fd/1", "/fd/2")):
        return True
    return False


# Sensitive write path protection (adapted from Hermes)
_HOME = str(Path.home())

_WRITE_DENIED_PATHS = {
    os.path.realpath(p) for p in [
        os.path.join(_HOME, ".ssh", "authorized_keys"),
        os.path.join(_HOME, ".ssh", "id_rsa"),
        os.path.join(_HOME, ".ssh", "id_ed25519"),
        os.path.join(_HOME, ".ssh", "config"),
        os.path.join(_HOME, ".bashrc"),
        os.path.join(_HOME, ".zshrc"),
        os.path.join(_HOME, ".profile"),
        os.path.join(_HOME, ".bash_profile"),
        os.path.join(_HOME, ".netrc"),
        os.path.join(_HOME, ".pgpass"),
        "/etc/sudoers",
        "/etc/passwd",
        "/etc/shadow",
    ] if os.path.exists(os.path.dirname(p))
}

_WRITE_DENIED_PREFIXES = [
    os.path.realpath(p) + os.sep for p in [
        os.path.join(_HOME, ".ssh"),
        os.path.join(_HOME, ".aws"),
        os.path.join(_HOME, ".gnupg"),
        os.path.join(_HOME, ".kube"),
        "/etc/sudoers.d",
        "/etc/systemd",
    ] if os.path.exists(p)
]


def _is_write_denied(path: str) -> bool:
    try:
        resolved = os.path.realpath(os.path.expanduser(str(path)))
    except (OSError, ValueError):
        resolved = str(path)
    if resolved in _WRITE_DENIED_PATHS:
        return True
    for prefix in _WRITE_DENIED_PREFIXES:
        if resolved.startswith(prefix):
            return True
    return False


_read_tracker_lock = threading.RLock()
_read_tracker: dict = {}


def _get_task_tracker(task_id: str = "default") -> dict:
    with _read_tracker_lock:
        return _read_tracker.setdefault(task_id, {
            "last_key": None, "consecutive": 0,
            "dedup": {}, "read_timestamps": {},
        })


def _check_read_loop(key: tuple, task_id: str = "default") -> int:
    with _read_tracker_lock:
        data = _get_task_tracker(task_id)
        if data["last_key"] == key:
            data["consecutive"] += 1
        else:
            data["last_key"] = key
            data["consecutive"] = 1
        return data["consecutive"]


def _check_read_dedup(filepath: str, offset: int, limit, task_id: str = "default") -> str | None:
    """Return dedup stub if the file was already read unchanged, or None."""
    try:
        resolved = str(Path(filepath).expanduser().resolve())
    except (OSError, ValueError):
        return None
    dedup_key = (resolved, offset, limit)
    with _read_tracker_lock:
        data = _get_task_tracker(task_id)
        cached_mtime = data.get("dedup", {}).get(dedup_key)
    if cached_mtime is None:
        return None
    try:
        current_mtime = os.path.getmtime(resolved)
        if current_mtime == cached_mtime:
            return tool_result({
                "content": (
                    "File unchanged since last read. The content from "
                    "the earlier read_file result in this conversation is "
                    "still current — refer to that instead of re-reading."
                ),
                "path": filepath,
                "dedup": True,
            })
    except OSError:
        pass
    return None


def _record_read_mtime(filepath: str, offset: int = 0, limit=None, task_id: str = "default"):
    try:
        resolved = str(Path(filepath).expanduser().resolve())
        mtime = os.path.getmtime(resolved)
    except OSError:
        return
    dedup_key = (resolved, offset, limit)
    with _read_tracker_lock:
        data = _get_task_tracker(task_id)
        data["read_timestamps"][resolved] = mtime
        data["dedup"][dedup_key] = mtime


def _check_staleness(filepath: str, task_id: str = "default") -> str | None:
    try:
        resolved = str(Path(filepath).expanduser().resolve())
    except (OSError, ValueError):
        return None
    try:
        current_mtime = os.path.getmtime(resolved)
    except OSError:
        return None
    with _read_tracker_lock:
        data = _get_task_tracker(task_id)
        read_mtime = data.get("read_timestamps", {}).get(resolved)
    if read_mtime is None:
        return None
    if current_mtime != read_mtime:
        return (f"Warning: {filepath} was modified since you last read it "
                "(external edit or concurrent agent). Consider re-reading before writing.")
    return None


def _update_read_timestamp(filepath: str, task_id: str = "default"):
    try:
        resolved = str(Path(filepath).expanduser().resolve())
        mtime = os.path.getmtime(resolved)
    except OSError:
        return
    with _read_tracker_lock:
        data = _get_task_tracker(task_id)
        data["read_timestamps"][resolved] = mtime


def _require_prior_read(filepath: str, task_id: str = "default") -> str | None:
    """Return an error message if the file EXISTS but wasn't read this session.

    Enforces read-before-edit discipline: prevents blind edits to files the agent
    hasn't examined. New-file creation (path doesn't exist) is allowed without a
    prior read.
    """
    try:
        resolved = str(Path(filepath).expanduser().resolve())
    except (OSError, ValueError):
        return None
    if not os.path.exists(resolved):
        return None  # new file — no prior read needed
    with _read_tracker_lock:
        data = _get_task_tracker(task_id)
        if resolved not in data.get("read_timestamps", {}):
            return (
                f"read-before-edit guard: '{filepath}' exists but you haven't read it this "
                "session. Call read_file on it first so you understand the surrounding code, "
                "then edit. (If you just compressed context, re-read the file and retry.)"
            )
    return None


def notify_other_tool_call(task_id: str = "default"):
    """Reset consecutive read/search counter when a non-read tool is called.

    This prevents false positives where the agent reads a file, does
    something else (write, patch, terminal), then reads again — which
    is fine and shouldn't trigger the loop detector.
    """
    with _read_tracker_lock:
        data = _read_tracker.get(task_id)
        if data:
            data["last_key"] = None
            data["consecutive"] = 0


def reset_file_dedup(task_id: str = None):
    """Clear the deduplication cache after context compression.

    After compression, original content is lost so the model needs
    full content if it reads the same file again.
    """
    with _read_tracker_lock:
        if task_id:
            data = _read_tracker.get(task_id)
            if data and "dedup" in data:
                data["dedup"].clear()
        else:
            for data in _read_tracker.values():
                if "dedup" in data:
                    data["dedup"].clear()


# Similar file suggestions (adapted from Hermes)
def _suggest_similar_files(filepath: str) -> list[str]:
    """Suggest similar filenames when the requested file is not found."""
    p = Path(filepath)
    parent = p.parent
    filename = p.name.lower()

    if not parent.exists():
        return []

    try:
        candidates = list(parent.iterdir())[:50]
    except PermissionError:
        return []

    similar = []
    for candidate in candidates:
        cname = candidate.name.lower()
        common = set(filename) & set(cname)
        if len(common) >= len(filename) * 0.5 and len(common) >= 2:
            similar.append(str(candidate))

    return similar[:5]


def _normalize_for_fuzzy(text: str) -> str:
    return re.sub(r'[ \t]+', ' ', text).strip()


def _fuzzy_find_match(content: str, old: str) -> str | None:
    """Try multiple fuzzy strategies to find old in content."""
    # Strategy 1: Exact match
    if old in content:
        return old

    # Strategy 2: Ignore trailing whitespace
    def strip_trailing(text):
        return '\n'.join(line.rstrip() for line in text.split('\n'))

    old_stripped = strip_trailing(old)
    content_stripped = strip_trailing(content)
    if old_stripped in content_stripped:
        old_stripped_lines = old_stripped.split('\n')
        content_stripped_lines = content_stripped.split('\n')
        content_lines = content.split('\n')
        for start_i in range(len(content_stripped_lines) - len(old_stripped_lines) + 1):
            if content_stripped_lines[start_i:start_i + len(old_stripped_lines)] == old_stripped_lines:
                return '\n'.join(content_lines[start_i:start_i + len(old_stripped_lines)])

    # Strategy 3: Ignore leading whitespace per line
    def strip_leading(text):
        return '\n'.join(line.lstrip() for line in text.split('\n'))

    old_nolead = strip_leading(old)
    content_nolead = strip_leading(content)
    if old_nolead in content_nolead:
        old_lines = old.split('\n')
        content_lines = content.split('\n')
        for start_i in range(len(content_lines) - len(old_lines) + 1):
            match = True
            for j in range(len(old_lines)):
                if content_lines[start_i + j].lstrip() != old_lines[j].lstrip():
                    match = False
                    break
            if match:
                return '\n'.join(content_lines[start_i:start_i + len(old_lines)])

    # Strategy 4: difflib close match
    old_lines = old.split('\n')
    content_lines = content.split('\n')
    if len(old_lines) <= 50:
        for start_i in range(max(0, len(content_lines) - len(old_lines) + 1)):
            window = content_lines[start_i:start_i + len(old_lines)]
            if len(window) != len(old_lines):
                continue
            ratio = difflib.SequenceMatcher(None, window, old_lines).ratio()
            if ratio >= 0.8:
                return '\n'.join(window)

    return None


_MAX_READ_CHARS = 100_000


def _read_file(args: dict, **kw) -> str:
    path = args.get("path", "")
    if not path:
        return tool_error("path is required. Provide the file path to read, e.g. read_file(path='src/main.py')")
    try:
        if _is_blocked_device(path):
            return tool_error(f"Cannot read '{path}': this is a device file that would block or produce infinite output.")

        if _has_binary_extension(path):
            return tool_error(f"Cannot read binary file '{path}' ({Path(path).suffix}). Use terminal to inspect binary files.")

        p = Path(path).expanduser().resolve()
        if not p.exists():
            similar = _suggest_similar_files(path)
            err = f"File not found: {p}"
            if similar:
                err += f". Did you mean: {', '.join(similar)}?"
            return tool_error(err)
        if p.is_dir():
            return tool_error(f"Path is a directory: {p}. Use directory_tree to explore directories.")

        offset = max(0, int(args.get("offset", 0)))
        limit = int(args.get("limit", 0) or 0)
        limit = limit if limit > 0 else None

        dedup_result = _check_read_dedup(str(p), offset, limit)
        if dedup_result is not None:
            return dedup_result

        encoding = args.get("encoding", "utf-8")

        # Stream the window instead of readlines(): lines outside the window
        # are counted, never retained, so an offset/limit read of a huge file
        # costs O(window) memory — and an over-cap read drops the window at
        # the cap and only counts the rest.
        total = 0
        lines: list = []
        content_len = 0
        too_large = False
        with open(p, "r", encoding=encoding, errors="replace") as f:
            for i, line in enumerate(f):
                total = i + 1
                if i < offset:
                    continue
                if limit is not None and i >= offset + limit:
                    continue
                content_len += len(line)
                if too_large:
                    continue
                lines.append(line)
                if content_len > _MAX_READ_CHARS:
                    too_large = True
                    lines.clear()

        start = offset
        content = "".join(lines)
        truncated = False

        if too_large:
            return tool_error(
                f"Read produced {content_len:,} characters which exceeds "
                f"the safety limit ({_MAX_READ_CHARS:,} chars). "
                "Use offset and limit to read a smaller range. "
                f"The file has {total} lines total.",
                path=str(p), total_lines=total,
            )

        read_key = ("read", str(p), offset, limit)
        count = _check_read_loop(read_key)
        if count >= 4:
            return tool_error(f"BLOCKED: You have read this file region {count} times. The content has NOT changed. Stop re-reading and proceed.")
        if count >= 3:
            content += f"\n\n[WARNING: You have read this file region {count} times consecutively. The content has not changed.]"

        _record_read_mtime(str(p), offset, limit)

        result = {
            "path": str(p),
            "lines": total,
            "offset_returned": start,
            "limit_returned": len(lines),
            "content": content,
            "truncated": truncated,
        }

        file_size = p.stat().st_size
        if file_size > 512000 and (not limit or limit > 200):
            result["_hint"] = f"This file is large ({file_size:,} bytes). Consider reading only the section you need with offset and limit."

        return tool_result(result)
    except Exception as e:
        return tool_error(str(e))


def _write_file(args: dict, **kw) -> str:
    path = args.get("path", "")
    content = args.get("content", "")
    if not path:
        return tool_error("path and content are required. Provide the file path and content, e.g. write_file(path='src/main.py', content='...')")

    if _is_write_denied(path):
        return tool_error(f"Write denied: '{path}' is a protected system/credential file. Use terminal with sudo if you need to modify system files.")

    prior_err = _require_prior_read(path)
    if prior_err:
        return tool_error(prior_err)

    try:
        p = Path(path).expanduser().resolve()
        p.parent.mkdir(parents=True, exist_ok=True)

        mode = args.get("mode", "overwrite")
        encoding = args.get("encoding", "utf-8")

        stale_warning = _check_staleness(str(p))

        if mode == "append":
            with open(p, "a", encoding=encoding) as f:
                f.write(content)
        else:
            with open(p, "w", encoding=encoding) as f:
                f.write(content)

        _update_read_timestamp(str(p))

        result = {"success": True, "path": str(p), "bytes_written": len(content.encode(encoding))}
        if stale_warning:
            result["_warning"] = stale_warning
        return tool_result(result)
    except Exception as e:
        return tool_error(str(e))


_SKIP_SEARCH_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", ".cache",
})


def _search_files(args: dict, **kw) -> str:
    pattern = args.get("pattern", "")
    path = args.get("path", ".")
    search_type = args.get("type", "content")
    output_mode = args.get("output_mode", "content")
    search_offset = int(args.get("offset", 0))

    if not pattern:
        return tool_error("pattern is required. Provide a search pattern, e.g. search_files(pattern='TODO', path='src')")
    try:
        root = Path(path).expanduser().resolve()
        if not root.exists():
            return tool_error(f"Path not found: {root}")

        max_results = int(args.get("max_results", 30))

        search_key = ("search", pattern, str(root))
        count = _check_read_loop(search_key)
        if count >= 4:
            return tool_error(f"BLOCKED: You have run this exact search {count} times. Results have NOT changed. Stop re-searching.")

        if search_type == "filename":
            results = []
            for f in interruptible_iter(root.rglob(pattern), every=64):
                if len(results) >= max_results + search_offset:
                    break
                if any(part in _SKIP_SEARCH_DIRS for part in f.parts):
                    continue
                rel = str(f.relative_to(root))
                results.append({"path": rel, "type": "dir" if f.is_dir() else "file"})

            total = len(results)
            results = results[search_offset:search_offset + max_results]

            result = {"results": results, "total": total}
            if search_offset + max_results < total:
                result["truncated"] = True
                result["_hint"] = f"Use offset={search_offset + max_results} to see more results."
        else:
            try:
                regex = re.compile(pattern, re.IGNORECASE)
            except re.error:
                regex = re.compile(re.escape(pattern), re.IGNORECASE)

            file_pattern = args.get("include", "*.py,*.js,*.ts,*.md,*.txt,*.yaml,*.yml,*.json,*.toml,*.cfg,*.ini,*.html,*.css,*.sh,*.go,*.rs,*.java,*.c,*.cpp,*.h")
            include_globs = [g.strip() for g in file_pattern.split(",") if g.strip()]
            context_lines = int(args.get("context", 2))

            if output_mode == "files_only":
                results = []
                seen = set()
                for f in interruptible_iter(root.rglob("*"), every=16):
                    if len(results) >= max_results + search_offset:
                        break
                    if any(part in _SKIP_SEARCH_DIRS for part in f.parts):
                        continue
                    if not f.is_file():
                        continue
                    if _has_binary_extension(str(f)):
                        continue
                    if f.stat().st_size > 1024 * 1024:
                        continue
                    has_star = any(g in ("*", "**") for g in include_globs)
                    suffixes = set()
                    exact_names = set()
                    for g in include_globs:
                        if g in ("*", "**"):
                            continue
                        s = g.lstrip("*").lower()
                        if s.startswith("."):
                            s = s[1:]
                        if s and "." not in s:
                            suffixes.add(s)            # extension, e.g. "ini"
                        else:
                            exact_names.add(g.lower())  # exact filename
                    file_ext = f.suffix.lower().lstrip(".")
                    if has_star or file_ext in suffixes or f.name.lower() in exact_names:
                        try:
                            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                                for line in fh:
                                    if regex.search(line):
                                        rel = str(f.relative_to(root))
                                        if rel not in seen:
                                            seen.add(rel)
                                            results.append({"path": rel})
                                        break
                        except Exception:
                            continue

                total = len(results)
                results = results[search_offset:search_offset + max_results]
                result = {"results": results, "total": total}
                if search_offset + max_results < total:
                    result["truncated"] = True
                    result["_hint"] = f"Use offset={search_offset + max_results} to see more results."
                if is_interrupted():
                    result["interrupted"] = True
                    result["_note"] = _SEARCH_INTERRUPTED_NOTE
                return tool_result(result)

            if output_mode == "count":
                counts = {}
                for f in interruptible_iter(root.rglob("*"), every=16):
                    if any(part in _SKIP_SEARCH_DIRS for part in f.parts):
                        continue
                    if not f.is_file():
                        continue
                    if _has_binary_extension(str(f)):
                        continue
                    if f.stat().st_size > 1024 * 1024:
                        continue
                    has_star = any(g in ("*", "**") for g in include_globs)
                    suffixes = set()
                    exact_names = set()
                    for g in include_globs:
                        if g in ("*", "**"):
                            continue
                        s = g.lstrip("*").lower()
                        if s.startswith("."):
                            s = s[1:]
                        if s and "." not in s:
                            suffixes.add(s)            # extension, e.g. "ini"
                        else:
                            exact_names.add(g.lower())  # exact filename
                    file_ext = f.suffix.lower().lstrip(".")
                    if has_star or file_ext in suffixes or f.name.lower() in exact_names:
                        try:
                            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                                c = sum(1 for line in fh if regex.search(line))
                                if c > 0:
                                    counts[str(f.relative_to(root))] = c
                        except Exception:
                            continue
                if is_interrupted():
                    return tool_result({"counts": counts, "total_count": sum(counts.values()),
                                        "interrupted": True, "_note": _SEARCH_INTERRUPTED_NOTE})
                return tool_result({"counts": counts, "total_count": sum(counts.values())})

            results = []
            seen_files = set()
            has_star = any(g in ("*", "**") for g in include_globs)
            suffixes = set()
            exact_names = set()
            for g in include_globs:
                if g in ("*", "**"):
                    continue
                s = g.lstrip("*").lower()
                if s.startswith("."):
                    s = s[1:]
                if s and "." not in s:
                    suffixes.add(s)
                else:
                    exact_names.add(g.lower())

            match_files = []
            for f in interruptible_iter(root.rglob("*"), every=64):
                if len(results) >= max_results + search_offset:
                    break
                if any(part in _SKIP_SEARCH_DIRS for part in f.parts):
                    continue
                if not f.is_file():
                    continue
                file_ext = f.suffix.lower().lstrip(".")
                if has_star or file_ext in suffixes or f.name.lower() in exact_names:
                    if f not in seen_files:
                        seen_files.add(f)
                        match_files.append(f)

            for f in interruptible_iter(match_files, every=16):
                if len(results) >= max_results + search_offset:
                    break
                if f.stat().st_size > 1024 * 1024:
                    continue
                if _has_binary_extension(str(f)):
                    continue
                try:
                    with open(f, "r", encoding="utf-8", errors="replace") as fh:
                        file_lines = fh.readlines()
                except Exception:
                    continue

                rel = str(f.relative_to(root))
                for i, line in enumerate(file_lines):
                    if regex.search(line):
                        start = max(0, i - context_lines)
                        end = min(len(file_lines), i + context_lines + 1)
                        ctx = "".join(file_lines[start:end])
                        results.append({
                            "path": rel,
                            "line": i + 1,
                            "match": line.strip(),
                            "context": ctx,
                        })
                        if len(results) >= max_results + search_offset:
                            break

            total = len(results)
            results = results[search_offset:search_offset + max_results]
            result = {"results": results, "total": total}
            if search_offset + max_results < total:
                result["truncated"] = True
                result["_hint"] = f"Use offset={search_offset + max_results} to see more results."

        if count >= 3:
            result["_warning"] = f"You have run this search {count} times consecutively. Results have not changed."
        if is_interrupted():
            result["interrupted"] = True
            result["_note"] = _SEARCH_INTERRUPTED_NOTE
        return tool_result(result)
    except Exception as e:
        return tool_error(str(e))


def _patch(args: dict, **kw) -> str:
    path = args.get("path", "")
    old = args.get("old", "")
    new = args.get("new", "")
    if not path or not old:
        return tool_error("path and old are required")

    if _is_write_denied(path):
        return tool_error(f"Write denied: '{path}' is a protected system/credential file.")

    prior_err = _require_prior_read(path)
    if prior_err:
        return tool_error(prior_err)

    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return tool_error(f"File not found: {p}")

        if p.stat().st_size > 5 * 1024 * 1024:
            return tool_error(f"File too large: {p.stat().st_size} bytes (max 5MB)")

        stale_warning = _check_staleness(str(p))

        content = p.read_text(encoding="utf-8")

        count = content.count(old)
        actual_old = old

        if count == 0:
            fuzzy_match = _fuzzy_find_match(content, old)
            if fuzzy_match is not None:
                actual_old = fuzzy_match
                count = content.count(actual_old)
                if count == 0:
                    old_lines = old.split('\n')
                    content_lines = content.split('\n')
                    for start_i in range(len(content_lines) - len(old_lines) + 1):
                        window = content_lines[start_i:start_i + len(old_lines)]
                        actual_old = '\n'.join(window)
                        if actual_old in content:
                            count = content.count(actual_old)
                            break
            if count == 0:
                return tool_error(
                    "old string not found in file (fuzzy match also failed). "
                    "Use read_file to verify the current content, or search_files to locate the text.",
                    path=str(p),
                )

        if count > 1 and not args.get("replace_all", False):
            return tool_error(f"old string found {count} times; set replace_all=true or provide more context", path=str(p))

        if args.get("replace_all", False):
            new_content = content.replace(actual_old, new)
        else:
            new_content = content.replace(actual_old, new, 1)

        p.write_text(new_content, encoding="utf-8")

        _update_read_timestamp(str(p))

        diff = list(difflib.unified_diff(
            content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=str(p),
            tofile=str(p),
        ))

        result = {
            "success": True,
            "path": str(p),
            "replacements": count if args.get("replace_all") else 1,
            "diff": "".join(diff)[:5000],
        }
        if stale_warning:
            result["_warning"] = stale_warning
        if actual_old != old:
            result["_fuzzy_matched"] = True
        return tool_result(result)
    except Exception as e:
        return tool_error(str(e))


_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    ".idea", ".vscode", "target", "dist", ".next", ".cache", ".tox",
    "egg-info", ".eggs", ".mypy_cache", ".pytest_cache",
})

_SKIP_EXTENSIONS = frozenset({
    ".pyc", ".pyo", ".class", ".jar", ".war", ".o", ".obj",
})


def _directory_tree(args: dict, **kw) -> str:
    path = args.get("path", ".")
    max_depth = int(args.get("max_depth", 3))
    max_items = int(args.get("max_items", 300))

    try:
        root = Path(path).expanduser().resolve()
    except (OSError, ValueError):
        return tool_error(f"Invalid path: {path}")
    if not root.exists():
        return tool_error(f"Path not found: {root}")
    if not root.is_dir():
        return tool_error(f"Not a directory: {root}")

    lines = []
    item_count = 0

    def _walk(directory: Path, prefix: str, depth: int):
        nonlocal item_count
        if depth > max_depth or item_count >= max_items:
            return

        try:
            entries = sorted(directory.iterdir(),
                             key=lambda e: (not e.is_dir(), e.name.lower()))
        except PermissionError:
            lines.append(f"{prefix}[permission denied]")
            return

        visible = []
        for entry in entries:
            if entry.name.startswith(".") and entry.name not in (".env", ".env.local", ".gitignore"):
                continue
            if entry.is_dir() and entry.name in _SKIP_DIRS:
                continue
            if entry.is_file() and entry.suffix.lower() in _SKIP_EXTENSIONS:
                continue
            visible.append(entry)

        for i, entry in enumerate(visible):
            if item_count >= max_items:
                lines.append(f"{prefix}... (truncated at {max_items} items)")
                return

            is_last = (i == len(visible) - 1)
            connector = "└── " if is_last else "├── "
            child_prefix = "    " if is_last else "│   "

            if entry.is_dir():
                lines.append(f"{prefix}{connector}{entry.name}/")
                _walk(entry, prefix + child_prefix, depth + 1)
            else:
                item_count += 1
                try:
                    size = entry.stat().st_size
                    if size >= 1024 * 1024:
                        size_str = f"  ({size // (1024 * 1024)}MB)"
                    elif size >= 1024:
                        size_str = f"  ({size // 1024}KB)"
                    else:
                        size_str = ""
                except OSError:
                    size_str = ""
                lines.append(f"{prefix}{connector}{entry.name}{size_str}")

    lines.append(f"{root.name}/")
    _walk(root, "", 1)

    return tool_result({
        "path": str(root),
        "tree": "\n".join(lines),
        "items_shown": item_count,
        "max_depth": max_depth,
    })


registry.register(
    name="read_file",
    schema={
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a text file with line numbers and pagination. Use offset and limit for large files. "
                "Cannot read binary files (images, PDFs, etc.) — use vision_analyze for images. "
                "Reads exceeding ~100K characters are rejected; use offset and limit to read specific sections."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read"},
                    "offset": {"type": "integer", "description": "Line number to start from (0-based, default: 0)"},
                    "limit": {"type": "integer", "description": "Max number of lines to read (max: 2000)"},
                    "encoding": {"type": "string", "description": "File encoding (default: utf-8)"},
                },
                "required": ["path"],
            },
        },
    },
    handler=lambda args, **kw: _read_file(args, **kw),
    path_params=("path",),
    check_fn=_check_file,
    toolset="base",
    read_only=True,
    description_modifier=lambda desc, avail: (
        desc + " Use search_files to locate files before reading."
        if "search_files" in avail else desc
    ),
)

registry.register(
    name="write_file",
    schema={
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write content to a file, completely replacing existing content. "
                "Creates parent directories automatically. OVERWRITES the entire file — use patch for targeted edits. "
                "Protected paths (.ssh, .aws, /etc) are blocked — use terminal with sudo for system files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write"},
                    "content": {"type": "string", "description": "Content to write"},
                    "mode": {"type": "string", "enum": ["overwrite", "append"], "description": "Write mode (default: overwrite)"},
                    "encoding": {"type": "string", "description": "File encoding (default: utf-8)"},
                },
                "required": ["path", "content"],
            },
        },
    },
    handler=lambda args, **kw: _write_file(args, **kw),
    path_params=("path",),
    check_fn=_check_file,
    toolset="files",
)

registry.register(
    name="search_files",
    schema={
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Search file contents (regex) or find files by name (glob pattern). "
                "Use this instead of grep/rg/find in terminal. "
                "For exploring project structure, use directory_tree instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Search pattern (regex for content, glob for filename)"},
                    "path": {"type": "string", "description": "Root directory to search (default: .)"},
                    "type": {"type": "string", "enum": ["content", "filename"], "description": "Search type (default: content)"},
                    "include": {"type": "string", "description": "File glob patterns, comma-separated (default: common code files)"},
                    "max_results": {"type": "integer", "description": "Max results (default: 30)"},
                    "offset": {"type": "integer", "description": "Skip first N results for pagination (default: 0)"},
                    "output_mode": {"type": "string", "enum": ["content", "files_only", "count"], "description": "Output format for content search: 'content' shows matches with line numbers, 'files_only' lists file paths, 'count' shows match counts per file (default: content)"},
                    "context": {"type": "integer", "description": "Context lines around content match (default: 2)"},
                },
                "required": ["pattern"],
            },
        },
    },
    handler=lambda args, **kw: _search_files(args, **kw),
    path_params=("path",),
    check_fn=_check_file,
    toolset="base",
    read_only=True,
)

registry.register(
    name="directory_tree",
    schema={
        "type": "function",
        "function": {
            "name": "directory_tree",
            "description": (
                "Show directory structure as a tree. Use this to quickly understand "
                "a project's layout instead of running multiple 'dir' or 'ls' commands. "
                "Skips .git, node_modules, __pycache__, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Root directory path"},
                    "max_depth": {"type": "integer", "description": "Max directory depth (default: 3)"},
                    "max_items": {"type": "integer", "description": "Max files/dirs to show (default: 300)"},
                },
                "required": ["path"],
            },
        },
    },
    handler=lambda args, **kw: _directory_tree(args, **kw),
    path_params=("path",),
    check_fn=_check_file,
    toolset="base",
    read_only=True,
)

registry.register(
    name="patch",
    schema={
        "type": "function",
        "function": {
            "name": "patch",
            "description": (
                "Targeted find-and-replace edits in files. Uses fuzzy matching so minor "
                "whitespace/indentation differences won't break it. Returns a unified diff. "
                "Protected paths (.ssh, .aws, /etc) are blocked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to patch"},
                    "old": {"type": "string", "description": "Text to find (fuzzy matched)"},
                    "new": {"type": "string", "description": "Replacement text"},
                    "replace_all": {"type": "boolean", "description": "Replace all occurrences (default: false)"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    handler=lambda args, **kw: _patch(args, **kw),
    path_params=("path",),
    check_fn=_check_file,
    toolset="files",
)
