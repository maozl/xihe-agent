"""Shared environment-filtering + error-sanitisation helpers.

Used by both the MCP subsystem (``tools/mcp_tool.py``) and the external-agent
driver (``core/external_agent.py``) so subprocess spawns never inherit
credential-bearing env vars and so error/log text is scrubbed of secrets.

Extracted here (rather than duplicated) so the external-agent driver does NOT
have to import ``tools.mcp_tool`` — whose module load requires the ``mcp``
package, which would couple claude/codex 接入 to MCP being installed.
"""

import os
import re
from typing import Optional

SAFE_ENV_PREFIXES = (
    "PATH", "HOME", "USER", "LANG", "LC_", "TERM",
    "TMPDIR", "TMP", "TEMP", "SHELL", "LOGNAME",
    "XDG_", "PYTHONPATH", "VIRTUAL_ENV", "CONDA",
    "SYSTEMROOT", "PROGRAMFILES", "APPDATA", "LOCALAPPDATA",
    "COMPUTERNAME", "USERNAME", "OS", "PROCESSOR",
)

SECRET_SUBSTRINGS = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL",
    "PASSWD", "AUTH", "PRIVATE",
)

_CREDENTIAL_PATTERN = re.compile(
    r"(?:"
    r"ghp_[A-Za-z0-9_]{1,255}"
    r"|sk-[A-Za-z0-9_]{1,255}"
    r"|Bearer\s+\S+"
    r"|token=[^\s&,;\"']{1,255}"
    r"|key=[^\s&,;\"']{1,255}"
    r"|API_KEY=[^\s&,;\"']{1,255}"
    r"|password=[^\s&,;\"']{1,255}"
    r"|secret=[^\s&,;\"']{1,255}"
    r")",
    re.IGNORECASE,
)


def build_safe_env(user_env: Optional[dict]) -> dict:
    """Build a filtered environment for subprocesses.

    Only safe baseline variables from the current process are passed, plus any
    explicitly configured *user_env* entries. Secret-bearing names are dropped
    so credentials never leak into a child's default environment. (Credentials
    a caller intentionally injects — e.g. ``ANTHROPIC_API_KEY`` — go in
    *user_env*, which is added verbatim.)
    """
    env = {}
    for key, value in os.environ.items():
        if any(s in key.upper() for s in SECRET_SUBSTRINGS):
            continue
        if any(key.startswith(p) for p in SAFE_ENV_PREFIXES):
            env[key] = value
    if user_env:
        env.update(user_env)
    return env


def sanitize_error(text: str) -> str:
    """Strip credential-like patterns from error/log text."""
    if not text:
        return text
    return _CREDENTIAL_PATTERN.sub("[REDACTED]", text)
