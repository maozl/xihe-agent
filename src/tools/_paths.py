"""Path resolution against the invoking agent's working directory.

Centralised here so every tool resolves user-supplied relative paths the same
way, instead of each handler calling ``Path(path).expanduser().resolve()``
(which silently uses the *process* cwd). The registry's ``dispatch`` rewrites
declared path params via :func:`resolve_path` before a tool's handler runs, and
``terminal`` / ``delegate`` reuse :func:`agent_base_dir` directly.

NOTE: cwd is a *default base for relative paths*, NOT a sandbox. Absolute paths
pass through unchanged — the model can still address any directory with an
absolute path. Sandboxing (confining tools to the workspace) is a separate,
later concern.
"""

import os
from pathlib import Path
from typing import Optional


def agent_base_dir(parent_agent) -> Optional[Path]:
    """Best-effort absolute working directory for *parent_agent*.

    First absolute candidate wins, checked in priority order:
      1. ``TERMINAL_CWD`` env var      (legacy hook, currently unset)
      2. ``parent_agent._terminal_cwd`` (legacy hook, currently unset)
      3. ``parent_agent.cwd``           (the real source — set by ``XiheAgent``
         from the cwd threaded through ``SharedContext.create_agent`` / serve)

    Returns ``None`` when nothing usable is available (no agent, or cwd unset)
    so callers fall back to the process cwd — i.e. today's behaviour for CLI,
    gateway, tests, and serve conversations that aren't bound to a workspace.
    """
    candidates = []
    env_cwd = os.getenv("TERMINAL_CWD")
    candidates.append(env_cwd)
    if parent_agent is not None:
        candidates.append(getattr(parent_agent, "_terminal_cwd", None))
        candidates.append(getattr(parent_agent, "cwd", None))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            p = Path(os.path.expanduser(str(candidate))).resolve()
        except Exception:
            continue
        if p.is_absolute():
            return p
    return None


def resolve_path(path, parent_agent) -> Path:
    """Resolve a user-supplied path against the agent's working directory.

    * Absolute paths pass through unchanged (cwd is a default base, not a
      sandbox).
    * Relative paths resolve against :func:`agent_base_dir`.
    * With no agent base, fall back to ``Path.resolve()`` (process cwd).
    """
    p = Path(str(path)).expanduser()
    if p.is_absolute():
        return p
    base = agent_base_dir(parent_agent)
    return (base / p).resolve() if base else p.resolve()
