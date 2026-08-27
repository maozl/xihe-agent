"""Node version manager tool (wraps nvm-windows / fnm / n).

Lets the agent manage installed Node.js versions: list, switch, install,
uninstall. Detects whichever version manager is on PATH (nvm-windows here).
Analysis/state operations are read-only; install/uninstall/use mutate state.

Gated by check_fn: the tool disappears entirely if no Node version manager
(nvm/fnm/volta/n) is on PATH.
"""

import logging
import os
import re
import shutil
import subprocess

from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 180
_VERSION_RE = re.compile(r"v?(\d+\.\d+\.\d+)")


def _detect_manager() -> tuple[str | None, str | None]:
    """Return (name, executable_path) for the first available manager."""
    for name in ("nvm", "fnm", "volta", "n"):
        exe = shutil.which(name)
        if exe:
            return name, exe
    # nvm-windows often sets NVM_HOME even if not on PATH
    nvm_home = os.environ.get("NVM_HOME")
    if nvm_home:
        candidate = os.path.join(nvm_home, "nvm.exe")
        if os.path.exists(candidate):
            return "nvm", candidate
    return None, None


_MANAGER, _MANAGER_EXE = _detect_manager()


def _check_node_manager() -> bool:
    return _MANAGER is not None


def _run(args: list, timeout: int) -> tuple[int, str, str]:
    logger.info("node_version run: %s %s", _MANAGER, " ".join(args))
    proc = subprocess.run(
        [_MANAGER_EXE] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _tail(text: str, n: int = 25) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:]) if len(lines) > n else text


def _parse_list(text: str) -> list[dict]:
    """Parse `nvm list` output. Returns [{version, current}]."""
    out = []
    for line in text.splitlines():
        m = _VERSION_RE.search(line)
        if not m:
            continue
        out.append({"version": m.group(1), "current": "*" in line or "Currently" in line})
    return out


def _parse_available(text: str) -> list[dict]:
    """Parse `nvm list available`. Returns [{version, lts?}]."""
    out = []
    for line in text.splitlines():
        # table rows have leading pipes/spaces then version tokens
        for tok in re.findall(r"\b(\d{1,2}\.\d+\.\d+)\b", line):
            if not any(d["version"] == tok for d in out):
                out.append({"version": tok})
    return out


def _node_version(args: dict, **kw) -> str:
    if not _MANAGER:
        return tool_error(
            "No Node version manager found (nvm/fnm/volta/n). Install one to use node_version."
        )

    action = (args.get("action") or "list").strip().lower()
    timeout = min(int(args.get("timeout", _DEFAULT_TIMEOUT)), 600)
    version = (args.get("version") or "").strip()

    try:
        # nvm-windows uses subcommands directly; fnm/volta/n differ — support nvm first
        # since that's what's detected here, with light shims for others.
        if action == "list":
            if _MANAGER == "fnm":
                rc, so, se = _run(["list"], timeout)
            else:  # nvm / volta / n
                rc, so, se = _run(["list"], timeout)
            if rc != 0:
                return tool_error(f"nvm list failed (exit {rc}):\n{_tail(se or so)}")
            return tool_result(action="list", manager=_MANAGER,
                               versions=_parse_list(so))

        if action == "current":
            # node -v / npm -v are the source of truth for the active version
            node_rc, node_v, _ = _run_node_directly(timeout)
            return tool_result(action="current", manager=_MANAGER,
                               node=node_v or None, manager_state=_current_manager_state(timeout))

        if action == "available":
            if _MANAGER != "nvm":
                return tool_error("'available' is nvm-specific; not supported by " + _MANAGER)
            rc, so, se = _run(["list", "available"], timeout)
            if rc != 0:
                return tool_error(f"nvm list available failed (exit {rc}):\n{_tail(se or so)}")
            limit = int(args.get("limit", 30))
            return tool_result(action="available", manager=_MANAGER,
                               versions=_parse_available(so)[:limit])

        if action in ("install", "uninstall", "use"):
            if not version:
                return tool_error(f"version is required for action '{action}'")
            rc, so, se = _run([action, version], timeout)
            if rc != 0:
                return tool_error(
                    f"{_MANAGER} {action} {version} failed (exit {rc}):\n{_tail(se or so)}"
                )
            return tool_result(action=action, manager=_MANAGER, version=version,
                               output=_tail(so, 40))

        return tool_error(
            f"Unknown action: '{action}'. Use: list, current, available, install, uninstall, use"
        )
    except subprocess.TimeoutExpired:
        return tool_error(f"{_MANAGER} {action} timed out after {timeout}s")
    except Exception as e:
        return tool_error(f"node_version {action} failed: {e}")


def _run_node_directly(timeout: int) -> tuple[int, str, str]:
    """Run `node -v` directly to get the active version (bypasses nvm)."""
    node_exe = shutil.which("node")
    if not node_exe:
        return 0, "", ""
    proc = subprocess.run([node_exe, "-v"], capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _current_manager_state(timeout: int) -> str:
    try:
        rc, so, se = _run(["list"], timeout)
        if rc == 0:
            for line in so.splitlines():
                if "*" in line or "Currently" in line:
                    return line.strip()
    except Exception:
        pass
    return ""


registry.register(
    name="node_version",
    toolset="dev_tool",
    schema={
        "type": "function",
        "function": {
            "name": "node_version",
            "description": (
                "Manage installed Node.js versions via the system's version manager "
                "(nvm-windows / fnm / volta / n — auto-detected). Actions: "
                "list (installed versions, marks current), current (active node version), "
                "available (remote versions — nvm only, needs network), "
                "install/uninstall/use a specific version."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "current", "available", "install", "uninstall", "use"],
                        "description": "Action (default: list).",
                    },
                    "version": {
                        "type": "string",
                        "description": "Node version for install/uninstall/use (e.g. '20.18.3').",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "For 'available': max remote versions to return (default 30).",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 180, max 600).",
                    },
                },
                "required": [],
            },
        },
    },
    handler=lambda args, **kw: _node_version(args, **kw),
    check_fn=_check_node_manager,
    read_only=False,
)
