"""Cross-platform process-tree kill + command-line lookup.

Ported from ``xihe-desktop/src/main/proc.ts`` so the external-agent driver can
clean up the full claude/codex process tree. The codebase had no tree-kill
before this — every existing kill was a bare ``proc.kill()`` (direct child
only), which orphans grandchildren (claude's node helpers, a python
console-script trampoline) on interrupt.

* :func:`kill_tree` uses SIGTERM on POSIX (not SIGKILL) deliberately: it lets
  claude flush its persisted session file on the way down, which is what makes
  cold ``--resume`` work after an interrupt/crash.
* :func:`list_process_command_line` is the orphan-sweep fingerprint check.
  Returns ``None`` when it can't be obtained (process gone / wmic blocked by
  EDR / non-win32) — callers then degrade to trusting the PID file alone.
"""

import os
import re
import subprocess
import sys

_WMIC_RE = re.compile(r"CommandLine=(.*)")


def kill_tree(pid) -> None:
    """Best-effort kill of an entire process tree rooted at *pid*. Never raises.

    - Windows: ``taskkill /PID <pid> /T /F`` walks the tree.
    - POSIX:   ``os.killpg(os.getpgid(pid), SIGTERM)`` — requires the child to
               have been spawned with ``start_new_session=True`` so it leads its
               own process group. SIGTERM (not KILL) lets the child flush state.
    """
    if not pid:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=10, shell=False,
            )
        else:
            import signal
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
    except Exception:
        # Best effort — process may already be gone, or pgid lookup may fail.
        pass


def list_process_command_line(pid) -> "str | None":
    """Look up a pid's full command line for the orphan-sweep fingerprint.

    Windows via ``wmic`` (5s timeout). Returns None if it can't be obtained
    (process gone / wmic blocked or timed out / non-win32). Synchronous and
    blocking — only call once at startup sweep. Caller should confirm the
    process is alive separately (``os.kill(pid, 0)``).
    """
    if sys.platform != "win32" or not pid:
        return None
    try:
        proc = subprocess.run(
            ["wmic", "process", "where", f"ProcessId={pid}",
             "get", "CommandLine", "/value"],
            capture_output=True, timeout=5, text=True,
        )
        m = _WMIC_RE.search(proc.stdout or "")
        if m:
            val = m.group(1).strip()
            return val or None
        return None
    except Exception:
        return None


def process_alive(pid) -> bool:
    """True if *pid* is currently running. ``os.kill(pid, 0)`` probe."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
