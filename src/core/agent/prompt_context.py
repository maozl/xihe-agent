"""Prompt context — SOUL.md and project-level context file loading.

Loads in priority order:
  Identity: ~/.xihe-agent/SOUL.md (agent personality override)
  Project:  .xihe.md > AGENTS.md > CLAUDE.md > .cursorrules (cwd → git root)

Each source is capped at 20,000 chars with head/tail truncation.
Content is scanned for prompt injection before injection.
"""

import logging
import re
from pathlib import Path
from typing import Optional

from core.config import AGENT_HOME

logger = logging.getLogger(__name__)

CONTEXT_FILE_MAX_CHARS = 20_000
CONTEXT_TRUNCATE_HEAD_RATIO = 0.6
CONTEXT_TRUNCATE_TAIL_RATIO = 0.4

_THREAT_PATTERNS = [
    (r'ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions', "prompt_injection"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
]

_INVISIBLE_CHARS = {
    '​', '‌', '‍', '⁠', '﻿',
    '‪', '‫', '‬', '‭', '‮',
}


def _scan_content(content: str, filename: str) -> str:
    """Block content with injection patterns. Returns sanitized content or blocked notice."""
    findings = []
    for char in _INVISIBLE_CHARS:
        if char in content:
            findings.append(f"invisible unicode U+{ord(char):04X}")
    for pattern, pid in _THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            findings.append(pid)
    if findings:
        logger.warning("Context file %s blocked: %s", filename, ", ".join(findings))
        return f"[BLOCKED: {filename} contained potential prompt injection ({', '.join(findings)}). Content not loaded.]"
    return content


def _truncate(content: str, filename: str) -> str:
    """Head/tail truncation for long context files."""
    if len(content) <= CONTEXT_FILE_MAX_CHARS:
        return content
    head = int(CONTEXT_FILE_MAX_CHARS * CONTEXT_TRUNCATE_HEAD_RATIO)
    tail = int(CONTEXT_FILE_MAX_CHARS * CONTEXT_TRUNCATE_TAIL_RATIO)
    marker = f"\n\n[...truncated {filename}: kept {head}+{tail} of {len(content)} chars...]\n\n"
    return content[:head] + marker + content[-tail:]


def _strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter (--- delimited)."""
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            body = content[end + 4:].lstrip("\n")
            return body if body else content
    return content


def _find_git_root(start: Path) -> Optional[Path]:
    current = start.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def load_soul_md() -> Optional[str]:
    """Load ~/.xihe-agent/SOUL.md as agent identity override."""
    soul_path = AGENT_HOME / "SOUL.md"
    if not soul_path.exists():
        return None
    try:
        content = soul_path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        content = _scan_content(content, "SOUL.md")
        return _truncate(content, "SOUL.md")
    except Exception as e:
        logger.debug("Could not read SOUL.md: %s", e)
        return None


def _load_xihe_md(cwd: Path) -> str:
    """XIHE.md — walk from cwd to git root."""
    stop_at = _find_git_root(cwd)
    current = cwd.resolve()
    for directory in [current, *current.parents]:
        for name in ("xihe.md", "XIHE.md", ".xihe.md"):
            candidate = directory / name
            if candidate.is_file():
                try:
                    content = candidate.read_text(encoding="utf-8").strip()
                    if content:
                        content = _strip_frontmatter(content)
                        content = _scan_content(content, name)
                        return _truncate(content, name)
                except Exception:
                    pass
        if stop_at and directory == stop_at:
            break
    return ""


def _load_agents_md(cwd: Path) -> str:
    """AGENTS.md — cwd only."""
    for name in ("AGENTS.md", "agents.md"):
        candidate = cwd / name
        if candidate.exists():
            try:
                content = candidate.read_text(encoding="utf-8").strip()
                if content:
                    content = _scan_content(content, name)
                    return _truncate(content, name)
            except Exception:
                pass
    return ""


def _load_claude_md(cwd: Path) -> str:
    """CLAUDE.md — cwd only."""
    for name in ("CLAUDE.md", "claude.md"):
        candidate = cwd / name
        if candidate.exists():
            try:
                content = candidate.read_text(encoding="utf-8").strip()
                if content:
                    content = _scan_content(content, name)
                    return _truncate(content, name)
            except Exception:
                pass
    return ""


def _load_cursorrules(cwd: Path) -> str:
    """.cursorrules + .cursor/rules/*.mdc — cwd only.

    The main file returns bare (load_project_context adds the shared
    "## .cursorrules" header); each .mdc keeps its own sub-header.
    """
    parts = []
    cr_file = cwd / ".cursorrules"
    if cr_file.exists():
        try:
            content = cr_file.read_text(encoding="utf-8").strip()
            if content:
                content = _scan_content(content, ".cursorrules")
                parts.append(content)
        except Exception:
            pass

    rules_dir = cwd / ".cursor" / "rules"
    if rules_dir.is_dir():
        for mdc in sorted(rules_dir.glob("*.mdc")):
            try:
                content = mdc.read_text(encoding="utf-8").strip()
                if content:
                    content = _scan_content(content, f".cursor/rules/{mdc.name}")
                    parts.append(f"## .cursor/rules/{mdc.name}\n\n{content}")
            except Exception:
                pass

    if not parts:
        return ""
    return _truncate("\n\n".join(parts), ".cursorrules")


def load_project_context(
    cwd: str = None,
    include_claude_md: bool = True,
    include_cursorrules: bool = True,
) -> str:
    """Load ALL project context files that exist (not first-match-wins).

    Different tools can coexist: .xihe.md for xihe, AGENTS.md for agent-agnostic
    instructions, CLAUDE.md for Claude Code, .cursorrules for Cursor IDE.
    All found files are combined.

    Args:
        include_claude_md: If False, skip CLAUDE.md.
        include_cursorrules: If False, skip .cursorrules.
    """
    import os
    cwd_path = Path(cwd or os.getcwd()).resolve()

    sections = []

    ctx = _load_xihe_md(cwd_path)
    if ctx:
        sections.append(f"## .xihe.md\n\n{ctx}")

    ctx = _load_agents_md(cwd_path)
    if ctx:
        sections.append(f"## AGENTS.md\n\n{ctx}")

    if include_claude_md:
        ctx = _load_claude_md(cwd_path)
        if ctx:
            sections.append(f"## CLAUDE.md\n\n{ctx}")

    if include_cursorrules:
        ctx = _load_cursorrules(cwd_path)
        if ctx:
            sections.append(f"## .cursorrules\n\n{ctx}")

    if not sections:
        return ""

    return "# Project Context\n\n" + "\n\n".join(sections)
