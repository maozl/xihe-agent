"""Terminal tool — execute shell commands."""

import logging
import os
import platform
import re
import subprocess
import sys
from tools import registry, tool_error, tool_result
from tools._approvals import detect_dangerous_command as _detect_dangerous_command

logger = logging.getLogger(__name__)


def _get_os_hint() -> str:
    system = platform.system()
    if system == "Windows":
        return ("Current OS is Windows. Use Windows commands and backslash paths. "
                "Prefer PowerShell commands when more capable.")
    elif system == "Linux":
        return "Current OS is Linux. Use Unix commands and forward-slash paths."
    elif system == "Darwin":
        return "Current OS is macOS. Use Unix/macOS commands and forward-slash paths."
    return f"Current OS is {system}."


_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[^[\]()]')


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return _ANSI_RE.sub('', text)


def _interpret_exit_code(command: str, exit_code: int) -> str | None:
    """Return a human-readable note when a non-zero exit code is non-erroneous."""
    if exit_code == 0:
        return None

    segments = re.split(r'\s*(?:\|\||&&|[|;])\s*', command)
    last_segment = (segments[-1] if segments else command).strip()
    words = last_segment.split()
    base_cmd = ""
    for w in words:
        if "=" in w and not w.startswith("-"):
            continue
        base_cmd = w.split("/")[-1].split("\\")[-1]
        break

    if not base_cmd:
        return None

    semantics = {
        "grep": {1: "No matches found (not an error)"},
        "egrep": {1: "No matches found (not an error)"},
        "fgrep": {1: "No matches found (not an error)"},
        "rg": {1: "No matches found (not an error)"},
        "ag": {1: "No matches found (not an error)"},
        "diff": {1: "Files differ (expected, not an error)"},
        "find": {1: "Some directories were inaccessible"},
        "test": {1: "Condition evaluated to false (expected)"},
        "[": {1: "Condition evaluated to false (expected)"},
        "curl": {
            6: "Could not resolve host",
            7: "Failed to connect to host",
            22: "HTTP error (e.g. 404, 500)",
            28: "Operation timed out",
        },
        "git": {1: "Non-zero exit (often normal — e.g. 'git diff' returns 1 when files differ)"},
    }

    cmd_semantics = semantics.get(base_cmd)
    if cmd_semantics and exit_code in cmd_semantics:
        return cmd_semantics[exit_code]
    return None


def _execute_terminal(command: str = "", timeout: int = 120, cwd: str = None, **kw) -> str:
    if not command:
        return tool_error("No command provided")

    timeout = min(int(timeout), 300)

    is_dangerous, _, danger_desc = _detect_dangerous_command(command)
    danger_warning = f"Potentially destructive command detected: {danger_desc}." if is_dangerous else None

    # Windows: switch to UTF-8 code page
    if sys.platform == "win32":
        command = f"chcp 65001 >nul && {command}"

    # Resolve the subprocess cwd. An explicit model-supplied cwd wins (resolved
    # against the agent base dir so a relative cwd lands in the workspace); when
    # omitted, default to the agent's workspace dir; with no agent, leave None so
    # the subprocess inherits the process cwd. terminal can't use the registry's
    # path_params rewriting because its cwd is a default, not an always-present
    # path to resolve.
    from tools._paths import resolve_path, agent_base_dir
    _ag = kw.get("parent_agent")
    if cwd:
        effective_cwd = str(resolve_path(cwd, _ag))
    else:
        _base = agent_base_dir(_ag)
        effective_cwd = str(_base) if _base else None

    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=effective_cwd,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        # Register so agent.interrupt() can kill it mid-run — otherwise a long
        # command blocks communicate() until it finishes or times out, and the
        # /stop only takes effect afterwards.
        from tools.interrupt import (register_subprocess, unregister_subprocess,
                                     is_interrupted)
        register_subprocess(proc)
        try:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                return tool_error(f"Command timed out after {timeout}s")
        finally:
            unregister_subprocess(proc)

        if is_interrupted():
            try:
                proc.kill()
            except Exception:
                # a failed kill leaves an orphan the user believes was stopped
                logger.warning("terminal: kill after interrupt failed (pid=%s)",
                               getattr(proc, "pid", "?"), exc_info=True)
            return tool_result(output="[interrupted]", exit_code=130)

        output = ""
        if stdout:
            output += stdout
        if stderr:
            if output:
                output += "\n"
            output += stderr

        output = _strip_ansi(output)

        from tools.redact import redact_sensitive_text
        output = redact_sensitive_text(output)

        if len(output) > 30000:
            head = output[:12000]
            tail = output[-15000]
            omitted = len(output) - 27000
            output = head + f"\n\n... [OUTPUT TRUNCATED - {omitted:,} chars omitted] ...\n\n" + tail

        response = {
            "exit_code": proc.returncode,
            "output": output or "(no output)",
        }

        if danger_warning:
            response["_warning"] = danger_warning

        exit_note = _interpret_exit_code(command, proc.returncode)
        if exit_note:
            response["exit_code_meaning"] = exit_note

        return tool_result(response)

    except Exception as e:
        return tool_error(str(e))


registry.register(
    name="terminal",
    schema={
        "type": "function",
        "function": {
            "name": "terminal",
            "description": (
                "Execute a shell command and return stdout/stderr. "
                "Use for running code, checking system state, file operations, etc. "
                "Do NOT use cat/head/tail — use read_file. "
                "Do NOT use grep/find — use search_files. "
                "Do NOT use sed/awk — use patch. "
                "Do NOT use echo/cat heredoc — use write_file. "
                "Reserve terminal for: builds, installs, git, processes, network, package managers.\n"
                + _get_os_hint()
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 120, max 300)",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (optional)",
                    },
                },
                "required": ["command"],
            },
        },
    },
    handler=lambda args, **kw: _execute_terminal(
        command=args.get("command", ""), timeout=args.get("timeout", 120),
        cwd=args.get("cwd"), **kw),
    toolset="terminal",
)
