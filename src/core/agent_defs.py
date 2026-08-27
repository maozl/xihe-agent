"""Specialist agent definitions from ``<agent_home>/agents/*.yaml``.

One file per specialist — the file name is the slug. A specialist may
override connection keys the main agent owns (model / base_url / api_key /
max_iterations); unset keys fall back to the main config at dispatch time,
so a file can be as small as persona + description.

Unlike delegate_task's ad-hoc subagents, specialists are stable named experts
with their own toolsets, skills whitelist, and identity — declared by whoever
owns the config, not invented at runtime.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from core.toolsets import resolve_roster

logger = logging.getLogger(__name__)

# Tool names are run_<slug>_agent, so the slug doubles as a tool-name
# namespace: lowercase identifier, no leading digit.
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")


def specialists_dir() -> Path:
    from core.config import AGENT_HOME
    return AGENT_HOME / "agents"


@dataclass
class AgentDef:
    slug: str
    name: str
    description: str
    persona: str
    # Same semantics as the main agent's config: absent → [] (nothing loaded),
    # ["*"] → None (everything). skills None only ever comes from an explicit "*".
    toolsets: list = field(default_factory=list)
    skills: "list | None" = field(default_factory=list)
    # Connection overrides — empty/None means "inherit the main config".
    model: str = ""
    base_url: str = ""
    # Never echoed to clients (GET /specialists returns only api_key_set);
    # compared/persisted server-side only.
    api_key: str = ""
    max_iterations: int | None = None
    project_context: bool = False
    enabled: bool = True

    @property
    def tool_name(self) -> str:
        return f"run_{self.slug}_agent"

    def config_overrides(self) -> dict:
        """Non-empty connection keys, ready to overlay onto the main config."""
        out = {}
        if self.model:
            out["model"] = self.model
        if self.base_url:
            out["base_url"] = self.base_url
        if self.api_key:
            out["api_key"] = self.api_key
        if self.max_iterations:
            out["max_iterations"] = self.max_iterations
        return out


def _load_raw(slug: str) -> dict:
    import yaml
    path = specialists_dir() / f"{slug}.yaml"
    spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return spec if isinstance(spec, dict) else {}


def save_raw(slug: str, spec: dict) -> None:
    """Persist one specialist file. Atomic via tmp + rename."""
    import yaml
    d = specialists_dir()
    d.mkdir(parents=True, exist_ok=True)
    body = yaml.dump(spec, allow_unicode=True, default_flow_style=False,
                     sort_keys=False, width=100)
    path = d / f"{slug}.yaml"
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(body, encoding="utf-8")
    import os
    os.replace(tmp, path)


def delete_raw(slug: str) -> bool:
    path = specialists_dir() / f"{slug}.yaml"
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _yaml_files() -> list:
    d = specialists_dir()
    if not d.is_dir():
        return []
    return [f for f in sorted(d.glob("*.yaml")) if not f.stem.startswith(".")]


def list_raw_specs() -> list:
    """``[(slug, raw spec dict), …]`` — the editor's view of agents/*.yaml.

    Unvalidated by design (the desktop shows entries validation would drop so
    they can be fixed); unreadable / non-mapping files are skipped. Specs may
    carry ``api_key`` — callers must strip it before echoing to a client.
    """
    out = []
    for f in _yaml_files():
        try:
            spec = _load_raw(f.stem)
        except Exception:
            continue
        if isinstance(spec, dict):
            out.append((f.stem, spec))
    return out


def _warn(warnings: list, msg: str) -> None:
    logger.warning(msg)
    if warnings is not None:
        warnings.append(msg)


def _mcp_tools_registered(toolset: str) -> bool:
    try:
        from tools import registry
        return toolset in set(registry.get_tool_to_toolset_map().values())
    except Exception:
        return False


def _parse_def(slug: str, spec: dict, warnings: list) -> "AgentDef | None":
    """Validate one spec dict → AgentDef. Warns and returns None on bad input.

    Invalid entries are skipped, never raised — a bad specialist file must not
    break the main agent's startup.
    """
    where = f"agents/{slug}.yaml"
    if not _SLUG_RE.match(slug):
        _warn(warnings, f"{where}: slug must match {_SLUG_RE.pattern}; skipped")
        return None
    if not spec.get("enabled", True):
        return None
    persona = str(spec.get("persona") or "").strip()
    description = str(spec.get("description") or "").strip()
    if not persona or not description:
        _warn(warnings, f"{where}: 'persona' and 'description' are required; skipped")
        return None

    toolsets, skills = resolve_roster(spec, where=where, warnings=warnings)
    if toolsets:
        for t in toolsets:
            # Per-server opt-in: get_schemas matches registry entries by
            # toolset name, so mcp-<server> exposes just that server's tools.
            # Kept even when the server is unknown/disconnected — resolves to
            # nothing until it registers.
            if t.startswith("mcp-") and not _mcp_tools_registered(t):
                _warn(warnings, f"{where}: no tools registered under '{t}' "
                                f"(unknown or disconnected MCP server); kept")

    max_iter = spec.get("max_iterations")
    if max_iter is not None:
        try:
            max_iter = int(max_iter)
        except (TypeError, ValueError):
            _warn(warnings, f"{where}: max_iterations not an int; inheriting main config")
            max_iter = None

    return AgentDef(
        slug=slug,
        name=str(spec.get("name") or slug),
        description=description,
        persona=persona,
        toolsets=toolsets,
        skills=skills,
        model=str(spec.get("model") or ""),
        base_url=str(spec.get("base_url") or ""),
        api_key=str(spec.get("api_key") or ""),
        max_iterations=max_iter,
        project_context=bool(spec.get("project_context", False)),
        enabled=True,
    )


def load_agent_defs(warnings: list = None) -> list:
    """Load + validate every ``agents/*.yaml`` into AgentDefs, sorted by slug."""
    defs = []
    for f in _yaml_files():
        try:
            spec = _load_raw(f.stem)
        except Exception as e:
            _warn(warnings, f"agents/{f.name}: unreadable ({e}); skipped")
            continue
        if not isinstance(spec, dict):
            _warn(warnings, f"agents/{f.name}: not a mapping; skipped")
            continue
        d0 = _parse_def(f.stem, spec, warnings)
        if d0 is not None:
            defs.append(d0)
    return defs
