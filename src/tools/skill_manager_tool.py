"""Skill Manager Tool — Agent-Managed Skill Creation & Editing.

Allows the agent to create, update, and delete skills, turning successful
approaches into reusable procedural knowledge. New skills are created in
${AGENT_HOME}/skills/. Existing skills (bundled or user-created) can be
modified or deleted wherever they live.

Skills are the agent's procedural memory: they capture *how to do a specific
type of task* based on proven experience. General memory is broad and
declarative. Skills are narrow and actionable.

Actions:
  create     — Create a new skill (SKILL.md + directory structure)
  edit       — Replace the SKILL.md content of a skill (full rewrite)
  patch      — Targeted find-and-replace within SKILL.md or any supporting file
  delete     — Remove a skill entirely
  write_file — Add/overwrite a supporting file (reference, template, script)
  remove_file— Remove a supporting file from a skill

Directory layout for user skills (always one level — dir name = skill name,
matching how discovery and the store ledger identify a skill):
    ${AGENT_HOME}/skills/
    └── my-skill/
        ├── SKILL.md
        ├── references/
        ├── templates/
        └── scripts/
"""

import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from core.config import AGENT_HOME
from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# Skills directories: bundled (read-only) + user (writable)
_BUNDLED_SKILLS_DIR = Path(__file__).parent.parent / "skills"
_USER_SKILLS_DIR = AGENT_HOME / "skills"

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_SKILL_CONTENT_CHARS = 100_000   # ~36k tokens at 2.75 chars/token
MAX_SKILL_FILE_BYTES = 1_048_576    # 1 MiB per supporting file

# Characters allowed in skill names (filesystem-safe, URL-friendly)
VALID_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]*$')

# Subdirectories allowed for write_file/remove_file
ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}


def _validate_name(name: str) -> Optional[str]:
    if not name:
        return "Skill name is required."
    if len(name) > MAX_NAME_LENGTH:
        return f"Skill name exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(name):
        return (
            f"Invalid skill name '{name}'. Use lowercase letters, numbers, "
            f"hyphens, dots, and underscores. Must start with a letter or digit."
        )
    return None


def _validate_frontmatter(content: str) -> Optional[str]:
    """Validate SKILL.md has proper YAML frontmatter with required fields."""
    if not content.strip():
        return "Content cannot be empty."
    if not content.startswith("---"):
        return "SKILL.md must start with YAML frontmatter (---). See existing skills for format."

    end_match = re.search(r'\n---\s*\n', content[3:])
    if not end_match:
        return "SKILL.md frontmatter is not closed. Ensure you have a closing '---' line."

    yaml_content = content[3:end_match.start() + 3]
    try:
        import yaml
        parsed = yaml.safe_load(yaml_content)
    except ImportError:
        # Fallback: check for name/description lines
        if "name:" not in yaml_content or "description:" not in yaml_content:
            return "Frontmatter must include 'name' and 'description' fields."
        return None
    except Exception as e:
        return f"YAML frontmatter parse error: {e}"

    if not isinstance(parsed, dict):
        return "Frontmatter must be a YAML mapping (key: value pairs)."
    if "name" not in parsed:
        return "Frontmatter must include 'name' field."
    if "description" not in parsed:
        return "Frontmatter must include 'description' field."
    if len(str(parsed["description"])) > MAX_DESCRIPTION_LENGTH:
        return f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters."

    body = content[end_match.end() + 3:].strip()
    if not body:
        return "SKILL.md must have content after the frontmatter (instructions, procedures, etc.)."

    return None


def _validate_content_size(content: str, label: str = "SKILL.md") -> Optional[str]:
    if len(content) > MAX_SKILL_CONTENT_CHARS:
        return (
            f"{label} content is {len(content):,} characters "
            f"(limit: {MAX_SKILL_CONTENT_CHARS:,}). "
            f"Consider splitting into a smaller SKILL.md with supporting files."
        )
    return None


def _validate_file_path(file_path: str) -> Optional[str]:
    """Validate file path for write_file/remove_file."""
    if not file_path:
        return "file_path is required."
    normalized = Path(file_path)
    if ".." in normalized.parts:
        return "Path traversal ('..') is not allowed."
    if not normalized.parts or normalized.parts[0] not in ALLOWED_SUBDIRS:
        allowed = ", ".join(sorted(ALLOWED_SUBDIRS))
        return f"File must be under one of: {allowed}. Got: '{file_path}'"
    if len(normalized.parts) < 2:
        return f"Provide a file path, not just a directory. Example: '{normalized.parts[0]}/myfile.md'"
    return None


def _find_skill(name: str) -> Optional[Dict[str, Any]]:
    """Find a skill by name across bundled and user directories.

    User skills take priority (can override bundled).
    Returns {"path": Path} or None.
    """
    for skills_dir in (_USER_SKILLS_DIR, _BUNDLED_SKILLS_DIR):
        if not skills_dir.exists():
            continue
        for skill_md in skills_dir.rglob("SKILL.md"):
            if skill_md.parent.name == name:
                return {"path": skill_md.parent}
    return None


def _atomic_write_text(file_path: Path, content: str, encoding: str = "utf-8") -> None:
    """Atomically write text content to a file.

    Uses a temporary file in the same directory and os.replace() so the
    target file is never left in a partially-written state.
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=str(file_path.parent),
        prefix=f".{file_path.name}.tmp.",
        suffix="",
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(temp_path, file_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def _create_skill(name: str, content: str) -> Dict[str, Any]:
    err = _validate_name(name)
    if err:
        return {"success": False, "error": err}

    err = _validate_frontmatter(content)
    if err:
        return {"success": False, "error": err}

    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name)
    if existing:
        return {
            "success": False,
            "error": f"A skill named '{name}' already exists at {existing['path']}.",
        }

    skill_dir = _USER_SKILLS_DIR / name
    skill_dir.mkdir(parents=True, exist_ok=True)

    skill_md = skill_dir / "SKILL.md"
    _atomic_write_text(skill_md, content)

    result = {
        "success": True,
        "message": f"Skill '{name}' created.",
        "path": str(skill_dir),
    }
    result["hint"] = (
        "To add reference files, templates, or scripts, use "
        f"skill_manage(action='write_file', name='{name}', "
        "file_path='references/example.md', file_content='...')"
    )
    return result


def _edit_skill(name: str, content: str) -> Dict[str, Any]:
    """Replace the SKILL.md of any existing skill (full rewrite)."""
    err = _validate_frontmatter(content)
    if err:
        return {"success": False, "error": err}

    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": f"Skill '{name}' not found. Use skills_list() to see available skills."}

    skill_md = existing["path"] / "SKILL.md"
    original_content = skill_md.read_text(encoding="utf-8") if skill_md.exists() else None

    try:
        _atomic_write_text(skill_md, content)
    except Exception as e:
        # Roll back on write failure
        if original_content is not None:
            try:
                _atomic_write_text(skill_md, original_content)
            except Exception:
                # rollback failed — SKILL.md may now be corrupt; that must
                # not be silent
                logger.warning("skill rollback FAILED — %s may be corrupt",
                               skill_md, exc_info=True)
        return {"success": False, "error": f"Failed to write SKILL.md: {e}"}

    return {
        "success": True,
        "message": f"Skill '{name}' updated.",
        "path": str(existing["path"]),
    }


def _patch_skill(
    name: str,
    old_string: str,
    new_string: str,
    file_path: str = None,
    replace_all: bool = False,
) -> Dict[str, Any]:
    """Targeted find-and-replace within a skill file."""
    if not old_string:
        return {"success": False, "error": "old_string is required for 'patch'."}
    if new_string is None:
        return {"success": False, "error": "new_string is required for 'patch'. Use empty string to delete matched text."}

    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": f"Skill '{name}' not found."}

    skill_dir = existing["path"]

    if file_path:
        err = _validate_file_path(file_path)
        if err:
            return {"success": False, "error": err}
        target = skill_dir / file_path
    else:
        target = skill_dir / "SKILL.md"

    if not target.exists():
        return {"success": False, "error": f"File not found: {target.relative_to(skill_dir)}"}

    content = target.read_text(encoding="utf-8")

    count = content.count(old_string)
    if count == 0:
        preview = content[:500] + ("..." if len(content) > 500 else "")
        return {
            "success": False,
            "error": f"old_string not found in file. Make sure the text matches exactly.",
            "file_preview": preview,
        }
    if count > 1 and not replace_all:
        return {
            "success": False,
            "error": f"Found {count} matches. Use replace_all=true to replace all, or provide a more specific old_string.",
        }

    new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)

    target_label = "SKILL.md" if not file_path else file_path
    err = _validate_content_size(new_content, label=target_label)
    if err:
        return {"success": False, "error": err}

    if not file_path:
        err = _validate_frontmatter(new_content)
        if err:
            return {"success": False, "error": f"Patch would break SKILL.md structure: {err}"}

    original_content = content
    _atomic_write_text(target, new_content)

    return {
        "success": True,
        "message": f"Patched {target_label} in skill '{name}' ({count} replacement{'s' if count > 1 else ''}).",
    }


def _delete_skill(name: str) -> Dict[str, Any]:
    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": f"Skill '{name}' not found."}

    skill_dir = existing["path"]
    shutil.rmtree(skill_dir)

    # Clean up empty category directories
    parent = skill_dir.parent
    if parent != _USER_SKILLS_DIR and parent != _BUNDLED_SKILLS_DIR:
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()

    return {"success": True, "message": f"Skill '{name}' deleted."}


def _write_file(name: str, file_path: str, file_content: str) -> Dict[str, Any]:
    """Add or overwrite a supporting file within a skill directory."""
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    if file_content is None:
        return {"success": False, "error": "file_content is required."}

    content_bytes = len(file_content.encode("utf-8"))
    if content_bytes > MAX_SKILL_FILE_BYTES:
        return {
            "success": False,
            "error": (
                f"File content is {content_bytes:,} bytes "
                f"(limit: {MAX_SKILL_FILE_BYTES:,} bytes / 1 MiB)."
            ),
        }
    err = _validate_content_size(file_content, label=file_path)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": f"Skill '{name}' not found. Create it first with action='create'."}

    target = existing["path"] / file_path
    target.parent.mkdir(parents=True, exist_ok=True)

    original_content = target.read_text(encoding="utf-8") if target.exists() else None
    _atomic_write_text(target, file_content)

    return {
        "success": True,
        "message": f"File '{file_path}' written to skill '{name}'.",
        "path": str(target),
    }


def _remove_file(name: str, file_path: str) -> Dict[str, Any]:
    """Remove a supporting file from a skill directory."""
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": f"Skill '{name}' not found."}

    skill_dir = existing["path"]
    target = skill_dir / file_path

    if not target.exists():
        available = []
        for subdir in ALLOWED_SUBDIRS:
            d = skill_dir / subdir
            if d.exists():
                for f in d.rglob("*"):
                    if f.is_file():
                        available.append(str(f.relative_to(skill_dir)))
        return {
            "success": False,
            "error": f"File '{file_path}' not found in skill '{name}'.",
            "available_files": available if available else None,
        }

    target.unlink()

    # Clean up empty subdirectories
    parent = target.parent
    if parent != skill_dir and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()

    return {"success": True, "message": f"File '{file_path}' removed from skill '{name}'."}


def _skill_manage(args: dict, **kw) -> str:
    action = args.get("action", "")
    name = args.get("name", "")

    if action == "create":
        content = args.get("content")
        if not content:
            return tool_error("content is required for 'create'. Provide the full SKILL.md text (frontmatter + body).")
        result = _create_skill(name, content)

    elif action == "edit":
        content = args.get("content")
        if not content:
            return tool_error("content is required for 'edit'. Provide the full updated SKILL.md text.")
        result = _edit_skill(name, content)

    elif action == "patch":
        old_string = args.get("old_string")
        new_string = args.get("new_string")
        if not old_string:
            return tool_error("old_string is required for 'patch'.")
        if new_string is None:
            return tool_error("new_string is required for 'patch'. Use empty string to delete matched text.")
        result = _patch_skill(
            name, old_string, new_string,
            args.get("file_path"),
            args.get("replace_all", False),
        )

    elif action == "delete":
        result = _delete_skill(name)

    elif action == "write_file":
        file_path = args.get("file_path")
        file_content = args.get("file_content")
        if not file_path:
            return tool_error("file_path is required for 'write_file'. Example: 'references/api-guide.md'")
        if file_content is None:
            return tool_error("file_content is required for 'write_file'.")
        result = _write_file(name, file_path, file_content)

    elif action == "remove_file":
        file_path = args.get("file_path")
        if not file_path:
            return tool_error("file_path is required for 'remove_file'.")
        result = _remove_file(name, file_path)

    else:
        result = {
            "success": False,
            "error": f"Unknown action '{action}'. Use: create, edit, patch, delete, write_file, remove_file",
        }

    # Invalidate skills index cache on successful mutations
    if isinstance(result, dict) and result.get("success"):
        try:
            from core.prompts import clear_skills_cache
            clear_skills_cache()
        except Exception:
            logger.debug("skills cache invalidation failed", exc_info=True)

    return json.dumps(result, ensure_ascii=False)


registry.register(
    name="skill_manage",
    toolset="skills",
    subagent_blocked=True,
    schema={
        "type": "function",
        "function": {
            "name": "skill_manage",
            "description": (
                "Manage skills (create, update, delete). Skills are reusable "
                "approaches for recurring task types. New skills go to "
                "${AGENT_HOME}/skills/; existing skills can be modified.\n\n"
                "Actions: create (full SKILL.md), "
                "patch (old_string/new_string — preferred for fixes), "
                "edit (full SKILL.md rewrite — major overhauls only), "
                "delete, write_file, remove_file.\n\n"
                "Create when: complex task succeeded (5+ calls), errors overcome, "
                "user-corrected approach worked, non-trivial workflow discovered, "
                "or user asks you to remember a procedure.\n"
                "Update when: instructions stale/wrong, missing steps or pitfalls "
                "found during use. If you used a skill and hit issues, patch it.\n\n"
                "After difficult/iterative tasks, offer to save as a skill. "
                "Skip for simple one-offs.\n\n"
                "Good skills: trigger conditions, numbered steps with exact commands, "
                "pitfalls section, verification steps. Use skill_view() to see format."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "patch", "edit", "delete", "write_file", "remove_file"],
                        "description": "The action to perform.",
                    },
                    "name": {
                        "type": "string",
                        "description": (
                            "Skill name (lowercase, hyphens/underscores, max 64 chars). "
                            "Must match an existing skill for patch/edit/delete/write_file/remove_file."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "Full SKILL.md content (YAML frontmatter + markdown body). "
                            "Required for 'create' and 'edit'. For 'edit', read the skill "
                            "first with skill_view() and provide the complete updated text."
                        ),
                    },
                    "old_string": {
                        "type": "string",
                        "description": (
                            "Text to find in the file (required for 'patch'). Must be unique "
                            "unless replace_all=true. Include enough surrounding context to "
                            "ensure uniqueness."
                        ),
                    },
                    "new_string": {
                        "type": "string",
                        "description": (
                            "Replacement text (required for 'patch'). Can be empty string "
                            "to delete the matched text."
                        ),
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "For 'patch': replace all occurrences instead of requiring a unique match (default: false).",
                    },
                    "file_path": {
                        "type": "string",
                        "description": (
                            "Path to a supporting file within the skill directory. "
                            "For 'write_file'/'remove_file': required, must be under references/, "
                            "templates/, scripts/, or assets/. "
                            "For 'patch': optional, defaults to SKILL.md if omitted."
                        ),
                    },
                    "file_content": {
                        "type": "string",
                        "description": "Content for the file. Required for 'write_file'.",
                    },
                },
                "required": ["action", "name"],
            },
        },
    },
    handler=_skill_manage,
)
