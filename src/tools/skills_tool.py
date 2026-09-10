"""Skills tool — file-based skill/prompt template management.

Skills are reusable prompt templates stored as SKILL.md files in the skills/
directory. Inspired by Hermes's progressive disclosure architecture:
  - Metadata (name, description) - shown in skills_list
  - Full Instructions - loaded via skill_view when needed
  - Linked Files (references, templates) - loaded on demand

Directory Structure:
    skills/
    ├── category/
    │   ├── DESCRIPTION.md       # Category description (optional)
    │   └── my-skill/
    │       ├── SKILL.md         # Main instructions (required)
    │       ├── references/      # Supporting documentation
    │       └── templates/       # Templates for output
"""

import logging
import platform
import re
from pathlib import Path
from typing import Optional

from core.config import AGENT_HOME, expand_agent_vars
from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# Skills directory: bundled skills/ + user skills in ~/.xihe-agent/skills/
_BUNDLED_SKILLS_DIR = Path(__file__).parent.parent / "skills"
_USER_SKILLS_DIR = AGENT_HOME / "skills"

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024

_EXCLUDED_DIRS = frozenset((".git", "__pycache__"))

# Platform map for frontmatter filtering
_PLATFORM_MAP = {
    "macos": "darwin",
    "linux": "linux",
    "windows": "win32",
}


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown content."""
    if not content.startswith("---"):
        return {}, content
    end = content.find("\n---", 3)
    if end == -1:
        return {}, content
    fm_text = content[3:end].strip()
    body = content[end + 4:].lstrip("\n")

    frontmatter = {}
    try:
        import yaml
        frontmatter = yaml.safe_load(fm_text) or {}
    except ImportError:
        # Fallback: parse simple key: value pairs
        for line in fm_text.split("\n"):
            if ":" in line:
                key, _, val = line.partition(":")
                frontmatter[key.strip()] = val.strip().strip('"').strip("'")

    return frontmatter, body


def _skill_matches_platform(frontmatter: dict) -> bool:
    """Check if a skill is compatible with the current OS."""
    platforms = frontmatter.get("platforms")
    if not platforms:
        return True
    if isinstance(platforms, str):
        platforms = [platforms]
    current = platform.system().lower()
    for p in platforms:
        mapped = _PLATFORM_MAP.get(p.lower(), p.lower())
        if mapped == current or mapped in current:
            return True
    return False


def _get_category(skill_md: Path, skills_dir: Path) -> Optional[str]:
    """Extract category from skill path (e.g., 'software-development')."""
    try:
        rel = skill_md.relative_to(skills_dir)
        parts = rel.parts
        if len(parts) >= 3:
            return parts[0]
    except ValueError:
        pass
    return None


def _scan_skills() -> list[dict]:
    """Recursively find all skills from bundled and user directories."""
    skills = []
    seen_names: set = set()

    # Bundled first, then user (user overrides bundled)
    for skills_dir in (_BUNDLED_SKILLS_DIR, _USER_SKILLS_DIR):
        if not skills_dir.exists():
            continue
        for skill_md in skills_dir.rglob("SKILL.md"):
            if any(part in _EXCLUDED_DIRS for part in skill_md.parts):
                continue
            try:
                content = skill_md.read_text(encoding="utf-8")[:4000]
                fm, body = _parse_frontmatter(content)

                if not _skill_matches_platform(fm):
                    continue

                name = fm.get("name", skill_md.parent.name)[:MAX_NAME_LENGTH]
                if name in seen_names:
                    continue

                description = fm.get("description", "")
                if not description:
                    for line in body.strip().split("\n"):
                        line = line.strip()
                        if line and not line.startswith("#"):
                            description = line
                            break
                if len(description) > MAX_DESCRIPTION_LENGTH:
                    description = description[:MAX_DESCRIPTION_LENGTH - 3] + "..."

                category = _get_category(skill_md, skills_dir)
                seen_names.add(name)
                skills.append({
                    "name": name,
                    "description": description,
                    "category": category,
                    "path": str(skill_md),
                })
            except Exception as e:
                logger.debug("Failed to read skill %s: %s", skill_md, e)

    return skills


def _load_category_description(category_dir: Path) -> Optional[str]:
    """Load category description from DESCRIPTION.md."""
    desc_file = category_dir / "DESCRIPTION.md"
    if not desc_file.exists():
        return None
    try:
        content = desc_file.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(content)
        desc = fm.get("description", "")
        if not desc:
            for line in body.strip().split("\n"):
                line = line.strip()
                if line and not line.startswith("#"):
                    desc = line
                    break
        return desc[:MAX_DESCRIPTION_LENGTH] if desc else None
    except Exception:
        return None


def _skills_list(args: dict, **kw) -> str:
    """List available skills (progressive disclosure tier 1)."""
    category = args.get("category")

    all_skills = _scan_skills()
    if not all_skills:
        return tool_result(success=True, skills=[], message="No skills found.")

    if category:
        all_skills = [s for s in all_skills if s.get("category") == category]

    all_skills.sort(key=lambda s: (s.get("category") or "", s["name"]))

    categories = {}
    for s in all_skills:
        cat = s.get("category")
        if cat and cat not in categories:
            categories[cat] = None

    for skills_dir in (_BUNDLED_SKILLS_DIR, _USER_SKILLS_DIR):
        if not skills_dir.exists():
            continue
        for cat_dir in skills_dir.iterdir():
            if cat_dir.is_dir() and cat_dir.name in categories and categories[cat_dir.name] is None:
                desc = _load_category_description(cat_dir)
                if desc:
                    categories[cat_dir.name] = desc

    result_categories = [
        {"name": name, "description": desc or ""}
        for name, desc in sorted(categories.items())
    ]

    # Don't expose internal path in list view
    output_skills = [
        {"name": s["name"], "description": s["description"], "category": s["category"]}
        for s in all_skills
    ]

    return tool_result(
        success=True,
        skills=output_skills,
        categories=result_categories,
        count=len(output_skills),
        hint="Use skill_view(name) to see full content, references, and templates",
    )


def _skill_view(args: dict, **kw) -> str:
    """View a skill's content or a linked file (progressive disclosure tier 2-3)."""
    name = args.get("name", "")
    file_path = args.get("file_path")

    if not name:
        return tool_error("name is required")

    skill_dir = None
    skill_md = None
    skill_category = None

    for skills_dir in (_BUNDLED_SKILLS_DIR, _USER_SKILLS_DIR):
        if not skills_dir.exists():
            continue

        # Try direct path: category/skill-name
        direct = skills_dir / name
        if direct.is_dir() and (direct / "SKILL.md").exists():
            skill_dir = direct
            skill_md = direct / "SKILL.md"
            skill_category = _get_category(skill_md, skills_dir)
            break

        # Search by directory name
        for found in skills_dir.rglob("SKILL.md"):
            if found.parent.name == name:
                skill_dir = found.parent
                skill_md = found
                skill_category = _get_category(found, skills_dir)
                break
        if skill_md:
            break

    if not skill_md or not skill_md.exists():
        available = [s["name"] for s in _scan_skills()[:20]]
        return tool_result(
            success=False,
            error=f"Skill '{name}' not found.",
            available_skills=available,
            hint="Use skills_list to see all available skills",
        )

    if file_path and skill_dir:
        normalized = Path(file_path)
        if ".." in normalized.parts:
            return tool_error("Path traversal ('..') is not allowed.")

        target = skill_dir / file_path
        try:
            if not target.resolve().is_relative_to(skill_dir.resolve()):
                return tool_error("Path escapes skill directory boundary.")
        except (OSError, ValueError):
            return tool_error(f"Invalid file path: '{file_path}'")

        if not target.exists():
            available = {}
            for f in skill_dir.rglob("*"):
                if f.is_file() and f.name != "SKILL.md":
                    rel = str(f.relative_to(skill_dir))
                    for prefix in ("references/", "templates/", "scripts/"):
                        if rel.startswith(prefix):
                            available.setdefault(prefix.rstrip("/"), []).append(rel)
                            break
                    else:
                        available.setdefault("other", []).append(rel)
            return tool_result(
                success=False,
                error=f"File '{file_path}' not found in skill '{name}'.",
                available_files={k: v for k, v in available.items() if v},
            )

        try:
            content = target.read_text(encoding="utf-8")
            content = expand_agent_vars(content)
            return tool_result(success=True, name=name, file=file_path, content=content)
        except UnicodeDecodeError:
            return tool_result(
                success=True,
                name=name,
                file=file_path,
                content=f"[Binary file: {target.name}, size: {target.stat().st_size} bytes]",
                is_binary=True,
            )

    try:
        content = skill_md.read_text(encoding="utf-8")
        content = expand_agent_vars(content)
    except Exception as e:
        return tool_error(f"Failed to read skill: {e}")

    fm, _ = _parse_frontmatter(content[:4000])

    linked_files = {}
    if skill_dir:
        for subdir in ("references", "templates", "scripts"):
            d = skill_dir / subdir
            if d.exists():
                files = [str(f.relative_to(skill_dir)) for f in d.rglob("*") if f.is_file()]
                if files:
                    linked_files[subdir] = files

    hermes_meta = {}
    metadata = fm.get("metadata")
    if isinstance(metadata, dict):
        hermes_meta = metadata.get("hermes", {}) or {}
    tags = hermes_meta.get("tags", fm.get("tags", []))
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.strip("[]").split(",") if t.strip()]

    result = {
        "success": True,
        "name": fm.get("name", skill_dir.name if skill_dir else name),
        "description": fm.get("description", ""),
        "category": skill_category,
        "tags": tags,
        "content": content,
    }
    if linked_files:
        result["linked_files"] = linked_files
        result["usage_hint"] = (
            "To view linked files, call skill_view with file_path parameter "
            "(e.g., 'references/api.md')"
        )

    return tool_result(result)


registry.register(
    name="skills_list",
    toolset="base",
    schema={
        "type": "function",
        "function": {
            "name": "skills_list",
            "description": (
                "List available skills (name + description). "
                "Use skill_view(name) to load full content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Optional category filter (e.g., 'software-development')",
                    },
                },
                "required": [],
            },
        },
    },
    handler=lambda args, **kw: _skills_list(args, **kw),
    check_fn=lambda: True,
    read_only=True,
)

registry.register(
    name="skill_view",
    toolset="base",
    schema={
        "type": "function",
        "function": {
            "name": "skill_view",
            "description": (
                "Load a skill's full content or access its linked files "
                "(references, templates, scripts). First call returns SKILL.md "
                "content plus 'linked_files' showing available resources. "
                "Call again with file_path to access those files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name (use skills_list to see available skills)",
                    },
                    "file_path": {
                        "type": "string",
                        "description": (
                            "Optional: path to a linked file within the skill "
                            "(e.g., 'references/api.md', 'templates/config.yaml')"
                        ),
                    },
                },
                "required": ["name"],
            },
        },
    },
    handler=lambda args, **kw: _skill_view(args, **kw),
    check_fn=lambda: True,
    read_only=True,
)
