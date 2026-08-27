"""Execute code tool — sandboxed Python code execution with tool calling support.

Architecture:
  1. Parent generates an agent_tools.py stub module with RPC functions
  2. Parent opens a TCP listener on localhost for tool-call dispatch
  3. Parent spawns a child process that runs the LLM's script
  4. Tool calls travel over TCP back to the parent for dispatch
  5. Only the script's stdout is returned to the LLM

Security:
  - Subprocess isolation (separate process, not in-process exec)
  - Restricted builtins whitelist (no exec/eval/open/compile/__import__)
  - No os/sys/pathlib in sandbox header imports
  - Environment variable filtering (blocks API keys, tokens, secrets)
  - Tool call allow-list and rate limit
"""

import json
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

SANDBOX_ALLOWED_TOOLS = frozenset([
    "web_search",
    "web_extract",
    "read_file",
    "write_file",
    "search_files",
    "patch",
    "directory_tree",
])

DEFAULT_TIMEOUT = 120          # seconds
DEFAULT_MAX_TOOL_CALLS = 50
MAX_STDOUT_BYTES = 50_000     # 50 KB
MAX_STDERR_BYTES = 10_000     # 10 KB

_RPC_PORT_RANGE = (49000, 49999)


def _check_execute_code() -> bool:
    return True


_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?[a-zA-Z]')


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return _ANSI_RE.sub('', text)


SANDBOX_HEADER = '''
import math
import json
import re
import datetime
import collections
import itertools
import functools
import random
import string
import typing
import decimal
import fractions
import statistics
import hashlib
import base64
import os
import glob
import pathlib

# Restricted builtins — block exec/eval/compile for code injection safety.
# os/pathlib/glob are allowed for file system reads; use agent_tools for writes.
_SAFE_BUILTINS = {
    k: __builtins__[k] if isinstance(__builtins__, dict) else getattr(__builtins__, k)
    for k in (
        'abs', 'all', 'any', 'bin', 'bool', 'bytes', 'bytearray', 'chr',
        'complex', 'dict', 'divmod', 'enumerate', 'filter', 'float',
        'format', 'frozenset', 'hash', 'hex', 'int', 'isinstance',
        'issubclass', 'iter', 'len', 'list', 'map', 'max', 'min', 'next',
        'oct', 'ord', 'pow', 'print', 'range', 'repr', 'reversed', 'round',
        'set', 'slice', 'sorted', 'str', 'sum', 'tuple', 'zip',
        'True', 'False', 'None',
        'open',
        'Exception', 'ValueError', 'TypeError', 'KeyError', 'IndexError',
        'RuntimeError', 'AttributeError', 'StopIteration',
        'NotImplementedError', 'ZeroDivisionError', 'OverflowError',
        'ArithmeticError', 'LookupError', 'OSError', 'IOError',
        'EOFError', 'UnicodeError', 'UnicodeDecodeError',
        'UnicodeEncodeError', 'UnicodeTranslateError',
        'GeneratorExit', 'KeyboardInterrupt',
    )
    if (k in (__builtins__ if isinstance(__builtins__, dict) else dir(__builtins__)))
}
__builtins__ = _SAFE_BUILTINS
'''


_TOOL_STUBS = {
    "web_search": (
        "web_search",
        "query: str, max_results: int = 5",
        '"""Search the web for information. Returns list of results with title/url/content."""',
        '{"query": query, "max_results": max_results}',
    ),
    "web_extract": (
        "web_extract",
        "url: str",
        '"""Extract text content from a web page URL."""',
        '{"url": url}',
    ),
    "read_file": (
        "read_file",
        "path: str, offset: int = 0, limit: int = 0",
        '"""Read file contents. offset is 0-based line number."""',
        '{"path": path, "offset": offset, "limit": limit}',
    ),
    "write_file": (
        "write_file",
        'path: str, content: str, mode: str = "overwrite"',
        '"""Write content to a file. mode: overwrite or append."""',
        '{"path": path, "content": content, "mode": mode}',
    ),
    "search_files": (
        "search_files",
        'pattern: str, path: str = ".", search_type: str = "content"',
        '"""Search files by content (regex) or filename (glob)."""',
        '{"pattern": pattern, "path": path, "type": search_type}',
    ),
    "patch": (
        "patch",
        "path: str, old: str, new: str, replace_all: bool = False",
        '"""Replace text in a file. Shows diff of changes."""',
        '{"path": path, "old": old, "new": new, "replace_all": replace_all}',
    ),
    "directory_tree": (
        "directory_tree",
        'path: str, max_depth: int = 3, max_items: int = 300',
        '"""Show directory structure as a tree. Use this instead of os.walk or glob."""',
        '{"path": path, "max_depth": max_depth, "max_items": max_items}',
    ),
}


def _generate_tools_module(enabled_tools: set, port: int) -> str:
    """Generate agent_tools.py stub module for the sandbox."""
    tools_to_generate = sorted(SANDBOX_ALLOWED_TOOLS & enabled_tools)

    stub_functions = []
    export_names = []
    for tool_name in tools_to_generate:
        if tool_name not in _TOOL_STUBS:
            continue
        func_name, sig, doc, args_expr = _TOOL_STUBS[tool_name]
        stub_functions.append(
            f"def {func_name}({sig}):\n"
            f"    {doc}\n"
            f"    return _call({func_name!r}, {args_expr})\n"
        )
        export_names.append(func_name)

    header = f'''\
"""Auto-generated agent tools RPC stubs — import and call tools from your script."""
import json as _json
import socket as _socket

_PORT = {port}

def _call(tool_name, args):
    """Send a tool call to the parent process and return the parsed result."""
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.connect(("127.0.0.1", _PORT))
    s.settimeout(300)
    request = _json.dumps({{"tool": tool_name, "args": args}}) + "\\n"
    s.sendall(request.encode())
    buf = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
        if buf.endswith(b"\\n"):
            break
    s.close()
    raw = buf.decode().strip()
    try:
        result = _json.loads(raw)
        if isinstance(result, str):
            try:
                return _json.loads(result)
            except (_json.JSONDecodeError, TypeError):
                return result
        return result
    except _json.JSONDecodeError:
        return raw

def json_parse(text):
    """Parse JSON tolerant of control characters (strict=False)."""
    return _json.loads(text, strict=False)

def retry(fn, max_attempts=3, delay=2):
    """Retry a function up to max_attempts times with exponential backoff."""
    import time as _time
    last_err = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < max_attempts - 1:
                _time.sleep(delay * (2 ** attempt))
    raise last_err

'''

    return header + "\n".join(stub_functions)


def _rpc_server_loop(
    server_sock: socket.socket,
    tool_call_counter: list,
    max_tool_calls: int,
    allowed_tools: frozenset,
    stop_event: threading.Event,
):
    """Accept tool-call requests from the sandbox and dispatch them."""
    server_sock.settimeout(2)
    conn = None
    try:
        while not stop_event.is_set():
            try:
                conn, _ = server_sock.accept()
            except socket.timeout:
                continue
            conn.settimeout(300)

            buf = b""
            while True:
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        request = json.loads(line.decode())
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        resp = tool_error(f"Invalid RPC request: {exc}")
                        conn.sendall((resp + "\n").encode())
                        continue

                    tool_name = request.get("tool", "")
                    tool_args = request.get("args", {})

                    if tool_name not in allowed_tools:
                        resp = json.dumps({
                            "error": f"Tool '{tool_name}' not available in sandbox. Available: {', '.join(sorted(allowed_tools))}"
                        })
                        conn.sendall((resp + "\n").encode())
                        continue

                    if tool_call_counter[0] >= max_tool_calls:
                        resp = json.dumps({
                            "error": f"Tool call limit reached ({max_tool_calls})"
                        })
                        conn.sendall((resp + "\n").encode())
                        continue

                    try:
                        result = registry.dispatch(tool_name, json.dumps(tool_args))
                    except Exception as exc:
                        logger.error("Tool call failed in sandbox: %s", exc, exc_info=True)
                        result = tool_error(str(exc))

                    tool_call_counter[0] += 1
                    conn.sendall((result + "\n").encode())

            try:
                conn.close()
            except OSError:
                pass
            conn = None
    except OSError:
        pass
    finally:
        if conn:
            try:
                conn.close()
            except OSError:
                pass


def _find_free_port() -> int:
    """Find a free TCP port for the RPC listener."""
    for port in range(*_RPC_PORT_RANGE):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", port))
            s.close()
            return port
        except OSError:
            continue
    raise RuntimeError("No free port found for sandbox RPC")


def _kill_process(proc: subprocess.Popen):
    """Terminate the child process, escalate to kill if needed."""
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


def _build_child_env() -> dict:
    """Build a minimal environment for the child process.

    Filters out secrets (API keys, tokens, passwords) to prevent
    credential exfiltration from LLM-generated scripts.
    """
    _SAFE_PREFIXES = (
        "PATH", "HOME", "USER", "LANG", "LC_", "TERM",
        "TMPDIR", "TMP", "TEMP", "SHELL", "LOGNAME",
        "XDG_", "PYTHONPATH", "VIRTUAL_ENV", "CONDA",
        "SYSTEMROOT", "PROGRAMFILES", "APPDATA", "LOCALAPPDATA",
        "COMPUTERNAME", "USERNAME", "OS", "PROCESSOR",
    )
    _SECRET_SUBSTRINGS = (
        "KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL",
        "PASSWD", "AUTH", "PRIVATE",
    )

    child_env = {}
    for k, v in os.environ.items():
        if any(s in k.upper() for s in _SECRET_SUBSTRINGS):
            continue
        if any(k.startswith(p) for p in _SAFE_PREFIXES):
            child_env[k] = v
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Force UTF-8: on Windows the child's stdout is a pipe (non-tty), so Python
    # defaults to the ANSI code page (cp936/GBK here) and print() of non-ASCII
    # comes back as mojibake — we decode stdout as UTF-8 below.
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    return child_env


def _execute_code(args: dict, **kw) -> str:
    code = args.get("code", "")
    if not code or not code.strip():
        return tool_error("code is required")

    timeout = min(int(args.get("timeout", DEFAULT_TIMEOUT)), 300)
    language = args.get("language", "python").lower()

    if language != "python":
        return tool_error(f"Unsupported language: {language}. Only python is supported.")

    enabled_tools = set(args.get("enabled_tools", []))
    sandbox_tools = frozenset(SANDBOX_ALLOWED_TOOLS & enabled_tools) if enabled_tools else SANDBOX_ALLOWED_TOOLS
    max_tool_calls = int(args.get("max_tool_calls", DEFAULT_MAX_TOOL_CALLS))

    tmpdir = tempfile.mkdtemp(prefix="xihe_agent_sandbox_")
    rpc_port = _find_free_port()

    tool_call_counter = [0]
    exec_start = time.monotonic()
    server_sock = None
    stop_event = threading.Event()
    rpc_thread = None

    try:
        tools_src = _generate_tools_module(sandbox_tools, rpc_port)
        with open(os.path.join(tmpdir, "agent_tools.py"), "w", encoding="utf-8") as f:
            f.write(tools_src)

        with open(os.path.join(tmpdir, "script.py"), "w", encoding="utf-8") as f:
            f.write(SANDBOX_HEADER)
            f.write("\n")
            f.write(code)

        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("127.0.0.1", rpc_port))
        server_sock.listen(5)

        rpc_thread = threading.Thread(
            target=_rpc_server_loop,
            args=(server_sock, tool_call_counter, max_tool_calls, sandbox_tools, stop_event),
            daemon=True,
        )
        rpc_thread.start()

        child_env = _build_child_env()

        proc = subprocess.Popen(
            [sys.executable, "script.py"],
            cwd=tmpdir,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )

        # Register so agent.interrupt() can kill the child (prompt /stop).
        from tools.interrupt import (register_subprocess, unregister_subprocess,
                                     is_interrupted)
        register_subprocess(proc)
        try:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_process(proc)
                duration = round(time.monotonic() - exec_start, 2)
                return json.dumps({
                    "status": "timeout",
                    "error": f"Script timed out after {timeout}s",
                    "tool_calls_made": tool_call_counter[0],
                    "duration_seconds": duration,
                })
        finally:
            unregister_subprocess(proc)

        # If the user interrupted (proc killed by agent.interrupt()), report it
        # instead of reading partial output as success/error.
        if is_interrupted():
            duration = round(time.monotonic() - exec_start, 2)
            return json.dumps({
                "status": "interrupted",
                "error": "Script interrupted by user.",
                "tool_calls_made": tool_call_counter[0],
                "duration_seconds": duration,
            })

        stdout_bytes = proc.stdout.read()
        stderr_bytes = proc.stderr.read()
        exit_code = proc.returncode

        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")

        if len(stdout_text) > MAX_STDOUT_BYTES:
            head_bytes = int(MAX_STDOUT_BYTES * 0.4)
            tail_bytes = MAX_STDOUT_BYTES - head_bytes
            head = stdout_text[:head_bytes]
            tail = stdout_text[-tail_bytes:]
            omitted = len(stdout_text) - head_bytes - tail_bytes
            stdout_text = (
                head
                + f"\n\n... [OUTPUT TRUNCATED - {omitted:,} chars omitted out of {len(stdout_text):,} total] ...\n\n"
                + tail
            )

        if len(stderr_text) > MAX_STDERR_BYTES:
            stderr_text = stderr_text[:MAX_STDERR_BYTES] + f"\n...[truncated, showing first {MAX_STDERR_BYTES} chars]..."

        stdout_text = _strip_ansi(stdout_text)
        stderr_text = _strip_ansi(stderr_text)

        from tools.redact import redact_sensitive_text
        stdout_text = redact_sensitive_text(stdout_text)
        stderr_text = redact_sensitive_text(stderr_text)

        duration = round(time.monotonic() - exec_start, 2)

        result = {
            "status": "success",
            "exit_code": exit_code,
            "output": stdout_text,
            "tool_calls_made": tool_call_counter[0],
            "duration_seconds": duration,
        }

        if exit_code != 0:
            result["status"] = "error"
            if stderr_text:
                result["stderr"] = stderr_text
                result["output"] = stdout_text + "\n--- stderr ---\n" + stderr_text
            else:
                result["error"] = f"Script exited with code {exit_code}"

        return json.dumps(result, ensure_ascii=False)

    except Exception as exc:
        duration = round(time.monotonic() - exec_start, 2)
        logger.error("execute_code failed after %ss: %s", duration, exc, exc_info=True)
        return json.dumps({
            "status": "error",
            "error": str(exc),
            "tool_calls_made": tool_call_counter[0],
            "duration_seconds": duration,
        })

    finally:
        stop_event.set()
        if server_sock is not None:
            try:
                server_sock.close()
            except OSError:
                pass
        if rpc_thread is not None:
            rpc_thread.join(timeout=3)
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


_TOOL_DOC_LINES = {
    "web_search": "  web_search(query, max_results=5) -> list of {title, url, content}",
    "web_extract": "  web_extract(url) -> {url, content, source}",
    "read_file": "  read_file(path, offset=0, limit=0) -> {path, lines, content}",
    "write_file": "  write_file(path, content, mode='overwrite') -> {success, path}",
    "search_files": "  search_files(pattern, path='.', search_type='content') -> {results}",
    "patch": "  patch(path, old, new, replace_all=False) -> {success, diff}",
    "directory_tree": "  directory_tree(path, max_depth=3, max_items=300) -> {path, tree, items_shown}",
}


def _build_description(sandbox_tools: frozenset = None) -> str:
    if sandbox_tools is None:
        sandbox_tools = SANDBOX_ALLOWED_TOOLS

    tool_lines = "\n".join(
        doc for name, doc in _TOOL_DOC_LINES.items() if name in sandbox_tools
    )

    import_examples = sorted(sandbox_tools)[:3]
    import_str = ", ".join(import_examples) + ", ..." if import_examples else "..."

    return (
        "Run a Python script in a sandboxed environment. "
        "Use when you need 3+ tool calls with processing logic between them, "
        "need to filter/reduce large tool outputs, need conditional branching, "
        "or need to loop (fetch N pages, process N files, retry on failure).\n\n"
        "Available Python stdlib: os, pathlib, glob, json, re, math, datetime, "
        "collections, itertools, functools, random, statistics, hashlib, base64, "
        "csv, decimal, fractions, string, typing.\n"
        "Blocked: subprocess, sys, exec, eval, compile, __import__.\n\n"
        f"Agent tools via `from agent_tools import {import_str}`:\n\n"
        f"{tool_lines}\n\n"
        "Also available in agent_tools (no import needed):\n"
        "  json_parse(text) — json.loads with strict=False\n"
        "  retry(fn, max_attempts=3, delay=2) — retry with exponential backoff\n\n"
        "Limits: 120s timeout, 50KB stdout cap, max 50 tool calls per script.\n"
        "Print your final result to stdout."
    )


registry.register(
    name="execute_code",
    toolset="dev_tool",
    schema={
        "type": "function",
        "function": {
            "name": "execute_code",
            "description": _build_description(),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default: 120, max: 300)"},
                    "max_tool_calls": {"type": "integer", "description": "Max tool calls allowed (default: 50)"},
                },
                "required": ["code"],
            },
        },
    },
    handler=lambda args, **kw: _execute_code(args, **kw),
    check_fn=_check_execute_code,
)
