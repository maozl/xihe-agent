"""Process tool — background process management."""

import logging
import signal
import subprocess
import threading
import time
from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

_processes: dict[str, dict] = {}
_lock = threading.Lock()
_next_id = 0


def _check_process() -> bool:
    return True


def _next_proc_id() -> str:
    global _next_id
    with _lock:
        _next_id += 1
        return f"proc_{_next_id}"


def _process(args: dict, **kw) -> str:
    action = args.get("action", "list")
    if action == "start":
        return _start_process(args)
    elif action == "stop":
        return _stop_process(args)
    elif action == "list":
        return _list_processes()
    elif action == "check":
        return _check_process_status(args)
    elif action == "output":
        return _get_output(args)
    else:
        return tool_error(f"Unknown action: {action}. Use: start, stop, list, check, output")


def _start_process(args: dict) -> str:
    command = args.get("command", "")
    if not command:
        return tool_error("command is required")

    name = args.get("name", "")
    pid = _next_proc_id()

    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception as e:
        return tool_error(f"Failed to start process: {e}")

    output_lines = []

    def _reader():
        try:
            for line in proc.stdout:
                output_lines.append(line)
                if len(output_lines) > 5000:
                    output_lines.pop(0)
        except Exception:
            pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    label = name or command[:50]
    with _lock:
        _processes[pid] = {
            "id": pid,
            "name": label,
            "command": command,
            "pid": proc.pid,
            "started": time.time(),
            "process": proc,
            "output": output_lines,
            "reader_thread": t,
        }

    logger.info("Started process %s: %s (pid=%d)", pid, command, proc.pid)
    return tool_result(success=True, process_id=pid, pid=proc.pid, name=label)


def _stop_process(args: dict) -> str:
    pid = args.get("process_id", "")
    if not pid:
        return tool_error("process_id is required")

    with _lock:
        info = _processes.pop(pid, None)
    if not info:
        return tool_error(f"Process not found: {pid}")

    proc = info["process"]
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            logger.warning("process stop: terminate failed (pid=%s)",
                           getattr(proc, "pid", "?"), exc_info=True)

    return tool_result(success=True, process_id=pid, status="stopped")


def _list_processes() -> str:
    with _lock:
        result = []
        for pid, info in _processes.items():
            proc = info["process"]
            running = proc.poll() is None
            result.append({
                "id": pid,
                "name": info["name"],
                "command": info["command"],
                "pid": info["pid"],
                "running": running,
                "uptime": int(time.time() - info["started"]) if running else None,
            })
    return tool_result(processes=result, count=len(result))


def _check_process_status(args: dict) -> str:
    pid = args.get("process_id", "")
    if not pid:
        return tool_error("process_id is required")

    with _lock:
        info = _processes.get(pid)
    if not info:
        return tool_error(f"Process not found: {pid}")

    proc = info["process"]
    running = proc.poll() is None
    return tool_result(
        id=pid,
        name=info["name"],
        running=running,
        exit_code=proc.returncode,
        uptime=int(time.time() - info["started"]) if running else None,
    )


def _get_output(args: dict) -> str:
    pid = args.get("process_id", "")
    if not pid:
        return tool_error("process_id is required")

    with _lock:
        info = _processes.get(pid)
    if not info:
        return tool_error(f"Process not found: {pid}")

    tail = int(args.get("tail", 50))
    lines = info["output"][-tail:]
    output = "".join(lines)
    if len(output) > 20000:
        output = output[-20000:]

    return tool_result(process_id=pid, output=output, lines=len(lines))


registry.register(
    name="process",
    schema={
        "type": "function",
        "function": {
            "name": "process",
            "description": (
                "Manage background processes. Start long-running commands, "
                "check their status, and retrieve output. "
                "Use for tasks that run over time like servers or watches."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["start", "stop", "list", "check", "output"],
                        "description": "Action to perform (default: list)",
                    },
                    "command": {"type": "string", "description": "Command to start (for start action)"},
                    "name": {"type": "string", "description": "Friendly name for the process (optional)"},
                    "process_id": {"type": "string", "description": "Process ID (for stop/check/output)"},
                    "tail": {"type": "integer", "description": "Number of output lines to return (default: 50)"},
                },
                "required": ["action"],
            },
        },
    },
    handler=lambda args, **kw: _process(args, **kw),
    check_fn=_check_process,
    toolset="terminal",
)
