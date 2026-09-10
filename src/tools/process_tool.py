"""Process tool — resident processes started from a conversation.

A started process streams into its own channel (``proc:{session_key}:{name}``)
in the local-tap registry, so the desktop terminal panel shows it live and the
logs outlive the tool call. Processes are named and scoped to the
conversation: two conversations never collide, and starting again under the
same explicit name replaces the previous process (the restart flow). ``stop``
kills the whole tree (kill_tree), not just the shell wrapper — a web backend
spawned through ``shell=True`` always has grandchildren.
"""

import codecs
import logging
import os
import subprocess
import sys
import threading
import time

from core.support.proc_utils import kill_tree
from tools import _local_tap, registry, tool_error, tool_result
from tools._ssh_tap import strip_ansi

logger = logging.getLogger(__name__)

STARTUP_DEFAULT_S = 15
STARTUP_MAX_S = 60
OUTPUT_DEFAULT_CHARS = 4_000
OUTPUT_MAX_CHARS = 20_000


def _session_key(kw: dict) -> str:
    return (kw.get("context") or {}).get("session_key") or ""


def _derive_name(command: str) -> str:
    tok = command.strip().split()[0] if command.strip() else "proc"
    base = tok.split("/")[-1].split("\\")[-1].removesuffix(".exe")
    return base or "proc"


def _unique_name(session_key: str, base: str) -> str:
    """A derived name colliding with a LIVE process in this conversation gets
    a suffix instead of replacing it — `python a.py` must not silently kill
    `python b.py`. Explicit names keep replace semantics."""
    name = base
    i = 2
    while True:
        ch = _local_tap.get(f"proc:{session_key}:{name}")
        if ch is None or ch.live_proc() is None:
            return name
        name = f"{base}-{i}"
        i += 1


def _own_names(session_key: str) -> str:
    names = [s["name"] for s in _local_tap.list_channels()
             if s["kind"] == "proc" and s["session_key"] == session_key]
    return ", ".join(names)


def _resolve_channel(args: dict, kw: dict):
    name = str(args.get("name") or args.get("process_id") or "").strip()
    if not name:
        return None, tool_error("name is required (or process_id)")
    session_key = _session_key(kw)
    ch = _local_tap.get(f"proc:{session_key}:{name}")
    if ch is None:
        own = _own_names(session_key)
        hint = f" (this conversation's processes: {own})" if own else ""
        return None, tool_error(f"Process not found: {name}{hint}")
    return ch, None


def _sanitize(text: str) -> str:
    from core.support.redact import redact_sensitive_text
    return redact_sensitive_text(strip_ansi(text))


def _process(args: dict, **kw) -> str:
    action = args.get("action", "list")
    if action == "start":
        return _start_process(args, **kw)
    elif action == "stop":
        return _stop_process(args, **kw)
    elif action == "list":
        return _list_processes()
    elif action == "check":
        return _check_process_status(args, **kw)
    elif action == "output":
        return _get_output(args, **kw)
    else:
        return tool_error(f"Unknown action: {action}. Use: start, stop, list, check, output")


def _start_process(args: dict, **kw) -> str:
    command = args.get("command", "")
    if not command:
        return tool_error("command is required")

    session_key = _session_key(kw)
    explicit = str(args.get("name") or "").strip()
    name = explicit or _unique_name(session_key, _derive_name(command))
    ch = _local_tap.proc_channel(session_key, name)

    # Replace semantics for an explicit name: stop the previous run first so
    # the channel's exit line lands before the new header. A derived name
    # can't get here (_unique_name skips live collisions); only an explicit
    # restart or a start-race reaches the kill, and killing is right for both.
    old = ch.live_proc()
    if old is not None:
        kill_tree(old.pid)
        for _ in range(20):
            if old.poll() is not None:
                break
            time.sleep(0.05)

    from core.support.paths import resolve_path, agent_base_dir
    _ag = kw.get("parent_agent")
    cwd = args.get("cwd")
    if cwd:
        effective_cwd = str(resolve_path(cwd, _ag))
    else:
        _base = agent_base_dir(_ag)
        effective_cwd = str(_base) if _base else None

    display_command = command
    if sys.platform == "win32":
        command = f"chcp 65001 >nul && {command}"

    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=effective_cwd,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except Exception as e:
        return tool_error(f"Failed to start process: {e}")

    ch.publish(f"\r\n\x1b[90m—— agent $ {display_command}"
               + (f"  [{name}]" if explicit else "")
               + "\x1b[0m\r\n")
    if effective_cwd:
        ch.publish(f"\x1b[90m   ({effective_cwd})\x1b[0m\r\n")
    ch.attach_proc(proc, display_command, effective_cwd)

    parts: list[str] = []

    def _drain(pipe, decoder) -> None:
        try:
            while True:
                data = pipe.read1(65536)
                if not data:
                    break
                s = decoder.decode(data)
                if s:
                    parts.append(s)
                    ch.publish(s)
        except Exception:
            pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    readers = [
        threading.Thread(target=_drain, daemon=True, name=f"proc-{name}-out",
                         args=(proc.stdout, codecs.getincrementaldecoder("utf-8")("replace"))),
        threading.Thread(target=_drain, daemon=True, name=f"proc-{name}-err",
                         args=(proc.stderr, codecs.getincrementaldecoder("utf-8")("replace"))),
    ]
    for t in readers:
        t.start()

    started = time.time()

    def _reaper() -> None:
        try:
            code = proc.wait()
        except Exception:
            return
        ch.proc_exit(code, time.time() - started)

    threading.Thread(target=_reaper, daemon=True, name=f"proc-{name}-wait").start()

    # Startup window: fast-fail if the command dies immediately (bad command,
    # port in use), and stay interruptible while it runs. Registered ONLY in
    # the window — after handoff the process is resident and must survive the
    # turn's end (and its interrupt).
    try:
        startup_timeout = int(args.get("startup_timeout") or STARTUP_DEFAULT_S)
    except (TypeError, ValueError):
        startup_timeout = STARTUP_DEFAULT_S
    startup_timeout = max(1, min(startup_timeout, STARTUP_MAX_S))

    from core.support.interrupt import (is_interrupted, register_subprocess,
                                        unregister_subprocess)
    register_subprocess(proc)
    try:
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None or is_interrupted():
                break
            time.sleep(0.15)
    finally:
        unregister_subprocess(proc)

    code = proc.poll()
    if code is not None:
        for t in readers:
            t.join(2.0)
        output = _sanitize("".join(parts))[-8000:]
        return tool_result(
            started=False, name=name, pid=proc.pid,
            exit_code=code,
            output=output or "(no output)",
            error=f"process exited during startup window ({startup_timeout}s) — likely a startup failure",
        )

    if is_interrupted():
        kill_tree(proc.pid)
        return tool_result(output="[interrupted]", exit_code=130, name=name)

    early = _sanitize("".join(parts))[-4000:]
    logger.info("process start [%s] %s (pid=%s)", name, display_command, proc.pid)
    return tool_result(
        started=True, name=name, pid=proc.pid, channel=ch.key,
        output=early or "(no output yet)",
        hint=f"running in background; process action=output name={name} reads logs, "
             f"action=stop name={name} stops it; live output is in the desktop terminal panel",
    )


def _stop_process(args: dict, **kw) -> str:
    ch, error = _resolve_channel(args, kw)
    if error:
        return error
    proc = ch.live_proc()
    if proc is not None:
        kill_tree(proc.pid)
        for _ in range(20):
            if proc.poll() is not None:
                break
            time.sleep(0.05)
    return tool_result(success=True, name=ch.name, status="stopped" if proc is not None else "already exited")


def _list_processes() -> str:
    rows = []
    for snap in _local_tap.list_channels():
        if snap["kind"] != "proc":
            continue
        rows.append({
            "name": snap["name"],
            "session_key": snap["session_key"],
            "command": snap["command"],
            "pid": snap["pid"],
            "running": snap["running"],
            "uptime": int(snap["ts"] - snap["started_at"]) if snap["running"] and snap.get("started_at") else None,
            "exit_code": snap["exit_code"],
        })
    return tool_result(processes=rows, count=len(rows))


def _check_process_status(args: dict, **kw) -> str:
    ch, error = _resolve_channel(args, kw)
    if error:
        return error
    snap = ch.snapshot()
    uptime = int(snap["ts"] - snap["started_at"]) if snap["running"] and snap.get("started_at") else None
    return tool_result(
        name=snap["name"], running=snap["running"],
        pid=snap["pid"], exit_code=snap["exit_code"], uptime=uptime,
    )


def _get_output(args: dict, **kw) -> str:
    ch, error = _resolve_channel(args, kw)
    if error:
        return error
    try:
        tail = int(args.get("tail") or OUTPUT_DEFAULT_CHARS)
    except (TypeError, ValueError):
        tail = OUTPUT_DEFAULT_CHARS
    tail = max(200, min(tail, OUTPUT_MAX_CHARS))
    text = _sanitize(ch.tail_text(tail))
    return tool_result(name=ch.name, output=text or "(no output yet)", running=ch.live_proc() is not None)


registry.register(
    name="process",
    schema={
        "type": "function",
        "function": {
            "name": "process",
            "description": (
                "Manage resident background processes that must outlive the "
                "tool call (web servers, watchers). start waits a startup "
                "window (default 15s): if the process exits inside it you get "
                "its output + exit code back as a failure; if it survives, it "
                "keeps running in the background and its output streams to a "
                "per-process console. Processes are named and scoped to this "
                "conversation — starting again with the same explicit name "
                "stops the previous one first (restart). Use terminal for "
                "one-shot commands."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["start", "stop", "list", "check", "output"],
                        "description": "Action to perform (default: list)",
                    },
                    "command": {"type": "string", "description": "Command to run (start)"},
                    "name": {
                        "type": "string",
                        "description": "Process name: start labels the process (defaults to the program name); "
                                       "stop/check/output select by it",
                    },
                    "cwd": {"type": "string", "description": "Working directory (start, optional)"},
                    "startup_timeout": {
                        "type": "integer",
                        "description": f"Seconds to confirm the process stays alive (start, default {STARTUP_DEFAULT_S}, max {STARTUP_MAX_S})",
                    },
                    "tail": {
                        "type": "integer",
                        "description": f"Log tail chars to return (output, default {OUTPUT_DEFAULT_CHARS}, max {OUTPUT_MAX_CHARS})",
                    },
                    "process_id": {"type": "string", "description": "Legacy alias of name"},
                },
                "required": ["action"],
            },
        },
    },
    handler=lambda args, **kw: _process(args, **kw),
    toolset="terminal",
)
